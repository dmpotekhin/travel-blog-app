"""P12 CLI — drive the pipeline from the terminal.

Usage::

    ./run.sh --cli scan [--archive PATH]
    ./run.sh --cli content --city CITY_ID
    ./run.sh --cli approve
    ./run.sh --cli plan
    ./run.sh --cli publish
    ./run.sh --cli tick
    ./run.sh --cli stats
    ./run.sh --cli dashboard

Each command opens a fresh Database, runs the service, and prints a small JSON
result. ``dashboard`` is a hint (the UI is a Streamlit app, run ``./run.sh``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from core import models as m
from core.config import Config
from core.database import Database


def _load_config() -> Config:
    """Single config source for the CLI entrypoint (ADR-101)."""
    from core.config import load_config_file

    return load_config_file()


async def _dispatch(args) -> dict:
    cfg = _load_config()
    db = Database(getattr(args, "db", None))
    await db.connect()
    try:
        cmd = args.command
        if cmd == "scan":
            from modules.scanner import Scanner

            archive = getattr(cfg, "archive", None)
            path = args.archive or (getattr(archive, "path", None) if archive else None)
            if not path:
                return {"error": "no archive path (pass --archive or set config)"}
            res = await Scanner(db, archive_path=path).scan()
            return {"cities_scanned": res}

        if cmd == "content":
            from modules.content.engine import ContentEngine

            result = await ContentEngine(db, cfg).process_city(args.city)
            return {"ok": True, "city_id": args.city, "result": result}

        if cmd == "approve":
            from modules.drafts import DraftManager

            res = await DraftManager(db).auto_approve()
            return {"auto_approved": res}

        if cmd == "plan":
            from modules.scheduler import Scheduler

            planned = await Scheduler(db, cfg).plan()
            return {"planned": [{"city_id": p.city_id, "platform": p.platform,
                                 "status": p.status, "scheduled_at": p.scheduled_at}
                                for p in planned]}

        if cmd == "publish":
            from modules.scheduler import Scheduler

            res = await Scheduler(db, cfg).run_due(limit=args.limit)
            return {"published": [
                {"platform": r.platform, "status": r.status.value}
                for r in res if r.status == m.PublicationStatus.PUBLISHED
            ]}

        if cmd == "tick":
            from modules.scheduler import Scheduler

            return await Scheduler(db, cfg).tick()

        if cmd == "stats":
            from modules.stats import StatsService

            svc = StatsService(db)
            return {
                "summary": await svc.summary(),
                "by_status": await svc.by_status(),
                "by_platform": await svc.by_platform(),
            }

        if cmd == "carousel":
            from core.config import get_secrets

            from modules.carousels.service import CarouselFactory

            carousel = CarouselFactory(db, cfg, get_secrets())
            action = args.action
            if action == "create":
                if not args.source:
                    return {"error": "create needs --source (url / owner/repo#123)"}
                job = await carousel.create_job(
                    source_type=args.source_type,
                    source_ref=args.source,
                    vertical=args.vertical or None,
                    title=args.title,
                    created_by=args.by,
                )
                return {"job_id": job.id, "status": job.status.value}
            if action == "list":
                jobs = await carousel.list_jobs(status=args.status or None, limit=args.limit)
                return {"jobs": [
                    {"job_id": j.id, "status": j.status.value, "vertical": j.vertical.value,
                     "title": j.title, "dry_run": j.dry_run}
                    for j in jobs
                ]}
            if action == "queue":
                jobs = await carousel.approval_queue(limit=args.limit)
                return {"queue": [{"job_id": j.id, "vertical": j.vertical.value} for j in jobs]}
            if not args.job_id:
                return {"error": f"`carousel {action}` needs --job-id"}
            if action == "status":
                return await carousel.status_report(args.job_id)
            if action == "research":
                # ``--fixture`` is the honest way to walk the pipeline offline:
                # the facts come from a JSON file the operator wrote, never from
                # the code guessing what a source would have said.
                resolver = None
                if args.fixture:
                    from core.models import CarouselSourceContext

                    from modules.carousels.sources.mock import MockSourceResolver

                    payload = json.loads(
                        Path(args.fixture).read_text(encoding="utf-8")
                    )
                    resolver = MockSourceResolver(
                        context=CarouselSourceContext.model_validate(payload)
                    )
                job = await carousel.research(args.job_id, resolver=resolver)
                return {
                    "job_id": args.job_id,
                    "status": job.status.value,
                    "warnings": list(job.warnings),
                }
            if action in {"draft", "plan", "render", "verify", "submit"}:
                method = {
                    "research": "research",
                    "draft": "draft_narrative",
                    "plan": "plan_slides",
                    "render": "render_slides",
                    "verify": "verify_slides",
                    "submit": "submit_for_approval",
                }[action]
                result = await getattr(carousel, method)(args.job_id)
                if isinstance(result, m.CarouselBundle):
                    return {
                        "job_id": args.job_id,
                        "status": result.job.status.value,
                        "slides": len(result.slides),
                        "issues": result.issues,
                    }
                if isinstance(result, list):  # verify_slides -> per-slide reports
                    return {
                        "job_id": args.job_id,
                        "checked": len(result),
                        "passed": sum(1 for report in result if report.passed),
                        "failed": [
                            {"slide": report.order, "issues": report.issues}
                            for report in result
                            if not report.passed
                        ],
                    }
                return {
                    "job_id": args.job_id,
                    "status": result.status.value,
                    "warnings": list(result.warnings),
                }
            if action == "approve":
                job = await carousel.approve(args.job_id, approved_by=args.by, note=args.note)
                return {"job_id": args.job_id, "status": job.status.value}
            if action == "reject":
                if not args.note:
                    return {"error": "reject needs --note with the reason (spec §13)"}
                job = await carousel.reject(args.job_id, reason=args.note, rejected_by=args.by)
                return {"job_id": args.job_id, "status": job.status.value}
            if action == "publish":
                bundle = await carousel.publish(args.job_id)
                return {"job_id": args.job_id, "publications": [
                    {"platform": pub.platform, "status": pub.status.value,
                     "request_id": pub.request_id, "url": pub.post_url}
                    for pub in bundle.publications
                ]}
            if action == "metrics":
                metric_rows = await carousel.list_metrics(args.job_id)
                return {"metrics": [
                    {"platform": row.platform, "metric": row.metric_name,
                     "value": row.metric_value, "raw": row.raw_value}
                    for row in metric_rows
                ]}
            if action == "collect":
                metric_rows = await carousel.collect_metrics(args.job_id)
                return {"job_id": args.job_id, "collected": len(metric_rows), "metrics": [
                    {"platform": row.platform, "metric": row.metric_name,
                     "value": row.metric_value, "raw": row.raw_value}
                    for row in metric_rows
                ]}
            if action == "score":
                score = await carousel.score_job(args.job_id)
                return {
                    "job_id": args.job_id,
                    "score": score.score,
                    "basis": score.basis,
                    "components": score.components,
                    "sample_size": score.sample_size,
                    "warnings": score.warnings,
                    "summary": score.explain(),
                }
            if action == "learnings":
                learning_rows = await carousel.list_learnings(
                    scope_type=args.scope or None, limit=args.limit
                )
                return {"learnings": [
                    {"scope": f"{row.scope_type.value}/{row.scope_value}",
                     "metric": row.metric_name, "value": row.metric_value,
                     "sample_size": row.sample_size, "confidence": row.confidence}
                    for row in learning_rows
                ]}
            if action == "refresh":
                written = await carousel.refresh_learnings(vertical=args.vertical or None)
                return {"rows": len(written), "learnings": [
                    {"scope": f"{row.scope_type.value}/{row.scope_value}",
                     "metric": row.metric_name, "value": row.metric_value,
                     "sample_size": row.sample_size, "confidence": row.confidence}
                    for row in written
                ]}
            if action == "advice":
                picks = await carousel.recommendations(
                    vertical=args.vertical or None, limit=args.limit
                )
                return {"advice": [
                    {"scope": f"{pick.scope_type}/{pick.scope_value}",
                     "metric": pick.metric_name, "value": pick.metric_value,
                     "n": pick.sample_size, "rationale": pick.rationale}
                    for pick in picks
                ]}
            return {"error": f"unknown carousel action: {action}"}

        if cmd == "dashboard":
            return {"hint": "ui/dashboard.py is a Streamlit app; run './run.sh' to serve it"}

        return {"error": f"unknown command: {cmd}"}
    finally:
        await db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli", description="Travel Blog Automation CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # ``--db`` on every subcommand: experiments and CI run against a throwaway
    # SQLite file instead of the live travel_blog.db.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="SQLite path (default: travel_blog.db)")

    sub.add_parser("scan", parents=[common]).add_argument("--archive", default=None)
    sub.add_parser("content", parents=[common]).add_argument("--city", type=int, required=True)
    sub.add_parser("approve", parents=[common])
    sub.add_parser("plan", parents=[common])
    sub.add_parser("publish", parents=[common]).add_argument("--limit", type=int, default=20)
    sub.add_parser("tick", parents=[common])
    sub.add_parser("stats", parents=[common])
    sub.add_parser("dashboard", parents=[common])

    carousel = sub.add_parser(
        "carousel", parents=[common], help="Carousel Factory: pipeline, approval, analytics"
    )
    carousel.add_argument(
        "action",
        choices=[
            "create", "list", "status", "research", "draft", "plan", "render",
            "verify", "submit", "queue", "approve", "reject", "publish",
            "collect", "metrics", "score", "refresh", "learnings", "advice",
        ],
    )
    carousel.add_argument("--job-id", type=int, default=None)
    carousel.add_argument("--source", default="")
    carousel.add_argument(
        "--source-type", default="url", choices=[kind.value for kind in m.CarouselSourceType]
    )
    carousel.add_argument(
        "--vertical", default="", choices=["", "auto", "travel", "qa", "vibecoding", "hybrid"]
    )
    carousel.add_argument("--title", default="")
    carousel.add_argument("--status", default="")
    carousel.add_argument("--scope", default="")
    carousel.add_argument("--note", default="")
    carousel.add_argument(
        "--fixture", default="", help="JSON CarouselSourceContext for offline research"
    )
    carousel.add_argument("--by", default="cli")
    carousel.add_argument("--limit", type=int, default=20)
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should surface a clean error
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

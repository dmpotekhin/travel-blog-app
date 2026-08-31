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

from core.config import Config
from core.database import Database


async def _dispatch(args) -> dict:
    cfg = Config()
    db = Database()
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
            return {"published": [r for r in res if getattr(r, "success", False)]}

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

        if cmd == "dashboard":
            return {"hint": "ui/dashboard.py is a Streamlit app; run './run.sh' to serve it"}

        return {"error": f"unknown command: {cmd}"}
    finally:
        await db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli", description="Travel Blog Automation CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan").add_argument("--archive", default=None)
    sub.add_parser("content").add_argument("--city", type=int, required=True)
    sub.add_parser("approve")
    sub.add_parser("plan")
    sub.add_parser("publish").add_argument("--limit", type=int, default=20)
    sub.add_parser("tick")
    sub.add_parser("stats")
    sub.add_parser("dashboard")
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

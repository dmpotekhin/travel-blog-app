# Project State

**Project:** Travel Blog Automation Platform (travel-blog-app)

## Current Position

- **Milestone:** M1 — Foundation
- **Phase:** P12 — Final verification
- **Status:** execute
- **Current task:** P12 complete (Final verification — app.py FastAPI + cli.py CLI + scheduler daemon wiring; run.sh no longer gap) — verified (tests/test_api.py: 4 passed; full suite 35 passed; cli.py stats/tick smoke).
- **Last updated:** 2026-08-28

## Active Decisions

- [x] D1: Modular monolith + strict layering; external services via ABC adapters — status: implemented
- [x] D2: exifread + google-generativeai + reverse_geocoder replaced/optional (see ARCHITECTURE §3) — status: implemented
- [x] D3: platform reality — Zen/Trip/Instagram/YouTube = manual-assisted; Telegram/VK = automatic; FB = partial — status: documented (ARCHITECTURE §7)
- [x] D4: Single global RateLimiter; AI cache keyed by sha256+provider+model+prompt_hash — status: planned (P4)
- [x] D5: dry_run=true default; mock publishers for dev — status: implemented (P4 mock provider, P8 BuildPublisher dry_run->MockPublisher)
- [x] D6: VibeCoding content type — httpx image-gen (Replicate/OpenAI/HF + Pillow mock under dry_run), own VibeCodingStatus state machine, honest manual platforms, scheduler cron job — status: verified (F1-F7, tests/test_vibecoding.py 8 passed, full suite 49 passed, compile clean)
- [x] D7: Content compliance validation — TripValidator + VibeCodingValidator over shared validation_base; soft gate (block_non_compliant=false) logging via Loguru + UI checklists; blockable per guideline — status: verified (G1-G7, tests/test_validators.py 10 passed, full suite 59 passed)

## Blockers

- Нет Xcode CLT → native builds (scikit-learn для reverse_geocoder) могут не собраться. Fallback: city detection из folder/filename (см. ARCHITECTURE R2).

## Progress

- [x] P1: Foundation — structure, config, logging, exceptions, models, database. Verified 2026-08-28 (config load, DB CRUD, duplicate protection, idempotency, state machine, gemini counters, AI cache).
- [x] P2: Scanner — incremental scan, EXIF, city detection, content-dedup, corrupt->failed, deletion detection. Verified 2026-08-28 (tests/test_scanner.py: 1 passed).
- [x] P3: Queue — city selection by priority/year, claim/requeue via state machine, require_photos gating. Verified 2026-08-28 (tests/test_queue.py).
- [x] P4: AI — BaseAIProvider + ImageAnalysis, Gemini(httpx)/DeepSeek providers, AI cache, rate limiter, mock provider (dev/dry_run), factory. Verified 2026-08-28 (tests/test_ai.py).
- [x] P5: Content — photo selection, analysis, base story, per-platform drafts, city DRAFTED/ERROR transitions. Verified 2026-08-28 (tests/test_content.py).
- [x] P6: Media — image optimize/orient/resize/size-cap + per-platform asset sets via Pillow. Verified 2026-08-28 (tests/test_media.py). VIDEO sub-step BLOCKED: moviepy 2.2.1 installed but import/ffmpeg resolution hangs (network) — awaiting decision.
- [x] P7: Drafts — approve/reject/reset via state machine, auto-approve rule (telegram+vk), counts. Verified 2026-08-28 (tests/test_drafts.py).
- [x] P8: Publishers — async publisher layer, honest auto/partial/manual modes, build_publisher (dry_run->Mock), PublishService (approved draft -> platform -> Publication), MANUAL_PLATFORMS=(zen,trip_com,instagram,youtube), PublicationStatus.MANUAL. Verified 2026-08-28 (tests/test_publish.py: 4 passed).
- [x] P9: Scheduler — plan() (approved drafts -> SCHEDULED slots per posts_per_day/publish_time/tz; MANUAL for manual platforms), run_due() (publish scheduled/pending due via PublishService), retry_failed() (exponential backoff bounded by retry.max_attempts), tick(), lazy AsyncIOScheduler wrapper. Publication records progressed via upsert (PublishService no longer short-circuits on existing (city,platform) rows). Verified 2026-08-28 (tests/test_scheduler.py: 5 passed).
- [x] P10: Dashboard — StatsService (read-only, unit-tested) + Streamlit app (ui/dashboard.py). Verified 2026-08-28 (tests/test_stats.py; full suite 30 passed; smoke-tested vs real DB).
- [x] P11: Tests — comprehensive per-phase coverage + end-to-end pipeline test (scan -> content -> approve -> schedule -> publish). Verified 2026-08-28 (tests/test_e2e_pipeline.py; full suite 31 passed).
- [x] P12: Final verification — app.py (FastAPI admin API) + cli.py (CLI orchestration) + scheduler daemon wiring; run.sh fully functional. Verified 2026-08-28 (tests/test_api.py: 4 passed; full suite 35 passed; cli.py stats/tick smoke; e2e dry-run test_e2e_pipeline passes).
- [x] P13: Architecture review (software-architect) + ADR-101..105 — full audit via docs/architecture-review-2026-09-04.md; all five ADRs implemented and verified. Verified 2026-09-04 (full suite 80 passed, 1 warning).

## ADR-101..105 (completed 2026-09-04)

- [x] ADR-101: API/CLI load config.yaml as the single config source (was bare Config()).
- [x] ADR-102: honest publishing — VK photo -> manual; error classes permanent/retryable; publishing.* flags honoured (plan() consults BasePublisher.enabled).
- [x] ADR-103: atomic publish claim (SCHEDULED/PENDING -> processing) + state-machine enforcement in update_publication (no double publish).
- [x] ADR-104: media path honest — Telegram sends a media group (up to 10), degraded flag + loud log when caption truncated/photos dropped.
- [x] ADR-105: removed ~210 dead wrapper lines in core/database.py; fixed /api/scheduler/publish-due (always returned []); BasePublisher.enabled wired; architectural guard tests; docs synced.

## Recent Activity

- 2026-08-28 — state: initialized project state
- 2026-08-28 — execute: P1 Foundation complete + smoke-verified
- 2026-08-28 — execute: P2 Scanner complete + pytest-verified (tests/test_scanner.py)
- 2026-08-28 — execute: P3 City Queue complete + pytest-verified (tests/test_queue.py)
- 2026-08-28 — analyze: ARCHITECTURE.md written (PHASE 0: arch, risk, tech compat, DB, state machine, structure, plan)
- 2026-08-28 — analyze: decision — PHASE 6 video deferred as manual/optional (moviepy import/ffmpeg hangs on network; no approval; card-media is the P6 core)
- 2026-08-28 — execute: P8 Publishers complete + pytest-verified (tests/test_publish.py, full suite 24 passed)
- 2026-08-28 — execute: P9 Scheduler complete + pytest-verified (tests/test_scheduler.py, full suite 29 passed)
- 2026-08-28 — execute: P10 Dashboard complete (StatsService + ui/dashboard.py) — verified (tests/test_stats.py, full suite 30 passed, smoke vs real DB)
- 2026-08-28 — execute: P11 Tests complete (comprehensive + e2e pipeline scan->publish) — verified (tests/test_e2e_pipeline.py, full suite 31 passed)
- 2026-08-28 — execute: P12 Final verification complete (app.py FastAPI + cli.py CLI + scheduler daemon; run.sh gap closed) — verified (tests/test_api.py 4 passed, full suite 35 passed, cli.py stats/tick smoke)
- 2026-08-28 — verify: REAL end-to-end dry-run via cli.py (dry_run mode, mock AI + mock publishers, no network/tokens) on a real photo archive (/tmp/tb_archive: Moscow/Rome/Tokyo). scan → content → approve → plan → publish all green: 3 cities, 9 photos new, 3 failed (corrupt), 6 drafts approved, 6 scheduled (telegram+vk), manual platforms stay pending_approval (honest), run_due correctly leaves future slots unpublished (published=0). DoD accepted.
- 2026-08-29 — execute: VibeCoding feature (F1-F7) complete — config/enum/DB (vibecoding_posts + CRUD + state machine), modules/vibecoding_generator.py (DeepSeek text + httpx image-gen + Pillow mock), modules/media.prepare_vibecoding_media, modules/publishers/vibecoding.VibeCodingPublisherService (honest auto/manual), scheduler.publish_vibecoding_due + cron job, UI (vibecoding_page, settings_page, dashboard tabs + VibeCoding KPI). Verified: tests/test_vibecoding.py 8 passed; full suite 49 passed.
- 2026-08-29 — verify: coverage check passed — all F1-F7 requirement items covered (DB, generator, media, publisher, scheduler, UI, config); decisions D6-D10 verified; compile clean; security-review clean (no HIGH/MEDIUM). DoD accepted.
- 2026-08-29 — execute: Content validation feature (G1-G7) complete — modules/validation_base.py (ValidationCheck/Result + helpers), modules/trip_validator.py (TripValidator), modules/vibecoding_validator.py (VibeCodingValidator), config blocks trip_guidelines/vibecoding_guidelines (soft gate block_non_compliant=false), scheduler _trip_gate/_vibe_gate + block logic in run_due/publish_vibecoding_due, UI checklists (ui/validation_view.py wired into Posts tab + VibeCoding page). Verified: tests/test_validators.py 10 passed; full suite 59 passed.

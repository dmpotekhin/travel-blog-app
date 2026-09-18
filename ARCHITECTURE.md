# ARCHITECTURE — Travel Blog Automation Platform

> **ADR-101..105 (implemented 2026-09-04).** Drift between this document and the
> code was fixed: API/CLI now load `config.yaml` (ADR-101), publishing is honest
> (VK photo -> `manual`; error classes permanent/retryable; `publishing.*` flags
> honoured) (ADR-102), publishing is claim-guarded and goes through the state
> machine (ADR-103), the media path no longer silently degrades (ADR-104), and
> dead code + doc/code drift + the `publish-due` endpoint were fixed (ADR-105).
> See `docs/architecture-review-2026-09-04.md` for the audit findings.

This document is both the **PHASE 0 analysis** (the design produced before code)
and the living **record of technical decisions** (section 77). Update it whenever
a decision changes. It is the source of truth for *why* the code is shaped the
way it is.

---

## 1. Architecture Summary

A **modular monolith** — one deployable app, strict internal layering, and a
stable interface boundary around every external service so any one of them can be
swapped without touching business logic.

```
UI (Streamlit, FastAPI)
   ↓ REST/async orchestration
Application Services (modules/*)
   ↓
Business Logic (generator, content, queue, state machine)
   ↓
Repositories (core/database.py)   ← the ONLY place that touches SQL
   ↓
SQLite (WAL)
```

External services are reached **only through adapters**:

```
                 ┌──────────────────────────── ┐
Application ───▶ │ Publisher (ABC)             │──▶ FacebookPublisher / VKPublisher /
                 │ AIProvider (ABC)            │    TelegramPublisher / ZenPublisher /
                 │ MediaProcessor              │    TripPublisher (+ Mocks)
                 └──────────────────────────── ┘
```

**Why a modular monolith and not microservices?** A single user, a ~1 TB local
archive, no horizontal scaling need. A monolith keeps deployment trivial (`run.sh`),
while the ABC/adaptor boundaries preserve the option to extract a service later.

**Non-negotiable separation rules (section 78):**
- UI never talks to Gemini / Telegram / Facebook directly — it goes through services.
- Publishers never talk to the DB directly — they receive `publish(draft: Draft, media_paths)`; the draft is validated before publication (ADR-105).
- Scanner/queue never hardcode a platform — they dispatch through `Publisher`.

---

## 2. Risk Analysis

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R1 | **Platform APIs don't match the spec's optimistic assumptions** | High | Every publisher first does a validation pass (section 72). Where official automation is impossible (Zen, Trip.com, Instagram, YouTube, Facebook-scheduling), we implement **manual-assisted mode**, not faked success. See §7. |
| R2 | Native Python builds fail on this machine (no Xcode CLT) | High | `reverse_geocoder`/`scikit-learn` may fail to build. City detection falls back to folder/filename parsing; reverse-geocoding is optional. Core deps are pure-Python/wheels. |
| R3 | 1 TB archive must never be loaded into RAM | High | Streaming, incremental, per-file hashing only when needed, DB indexes, pagination. |
| R4 | Duplicate publications on restart/retry | High | UNIQUE(city_id, platform) + idempotent `save_published` + state-machine terminal guard. |
| R5 | AI overselling unverified facts | Medium | Factual content policy: every claim needs an EXIF/path/user-source; otherwise flagged, not asserted. |
| R6 | Deprecated libraries in the spec's stack | Medium | `exifread` → Pillow `Image.getexif()`. `google-generativeai` → `google-genai` SDK. MoviePy version pinned + adapted. See §3. |
| R7 | Gemini quota exhaustion mid-run | Medium | Single global `RateLimiter` + persistent counters + `pending_tasks` deferral. |
| R8 | Credentials leak | High | Secrets only in `.env` (gitignored); masked in UI; never logged. Pre-commit credential scan. |
| R9 | Automation against anti-bot walls (Trip.com) | Medium | No CAPTCHA bypass. Auto attempt + graceful manual-assisted fallback + fixed retry budget. |

---

## 3. Technology Compatibility

Runtime: Python 3.11.7 (>= 3.10 required). SQLite via `aiosqlite` (WAL, FK).

| Library (spec) | Status | Action |
|----------------|--------|--------|
| Python 3.10+ | ✅ | 3.11 present |
| FastAPI, Streamlit | ✅ | current stable |
| SQLite + aiosqlite | ✅ | installed |
| APScheduler | ✅ | AsyncIOScheduler (no duplicate jobs after restart) |
| Pillow | ✅ | primary image toolkit; also used for EXIF |
| **exifread** | ⚠️ deprecated/unmaintained | **replaced** with Pillow `Image.getexif()` |
| **reverse_geocoder** | ⚠️ unmaintained, needs scikit-learn | offline-first, but **optional** — fallback to folder/filename detection; consider `geopy`+local datasource |
| **google-generativeai** | ⚠️ legacy SDK | **replaced** with `google-genai` (current SDK) |
| openai (DeepSeek) | ✅ | OpenAI-compatible `base_url=https://api.deepseek.com` |
| Selenium + webdriver-manager | ✅ | used only for Trip.com manual-assisted |
| MoviePy | ⚠️ v1/v2 API split | pin + adapt; ffmpeg via `imageio-ffmpeg` |
| Loguru, Pydantic v2, Pydantic-Settings, python-dotenv, PyYAML | ✅ | installed & verified |
| pandas, plotly | ✅ | analytics, dashboard charts |

**Gemini access via `google-genai`.** The previous `google-generativeai` package is
being retired. **(2026-08-28) Implemented over the REST ``:generateContent`` API
via httpx instead of the ``google-genai`` SDK** — httpx is pure-Python (no native
build), already a dependency, and avoids the SDK's transitive dependency on a
native ``cryptography`` wheel, which cannot build on this machine (no Xcode CLT).
The provider is isolated in ``modules/ai/gemini.py`` so swapping to the official
SDK is a one-file change.

**DeepSeek is text-only** (the chat API does not accept image input). Image *analysis*
is Gemini's job; DeepSeek is used for text drafting. This maps the pipeline cleanly:
Gemini → visual facts; DeepSeek → prose.

---

## 4. Database Design

Single SQLite file `travel_blog.db` (WAL). All writes through `core/database.py`.
Timestamps stored as ISO-8601 UTC text.

### Tables, keys, constraints

**cities** — the content unit (city + year).
`id PK · name · country · year · latitude · longitude · folder_path · status · priority · created_at · updated_at`
`UNIQUE(name, country, year)` → no duplicate cities.

**photos** — per-photo metadata (never the image file itself).
`id PK · city_id FK→cities(id) CASCADE · path UNIQUE · filename · size · modified_at · sha256 · taken_at · latitude · longitude · country · city · year · scan_status · created_at · updated_at`
`UNIQUE(path)` → incremental scan upserts by path.

**drafts** — one per (city, platform).
`id PK · city_id FK CASCADE · platform · title · content · photos_json · status · content_version · ai_provider · ai_model · created_at · updated_at`
`UNIQUE(city_id, platform)`.

**published** — idempotent publication record.
`id PK · city_id FK CASCADE · platform · external_id · url · status · scheduled_at · published_at · content_version · error_message · retry_count · created_at`
`UNIQUE(city_id, platform)` → **cannot publish the same city+platform twice**.

**pending_tasks** — deferred work (quota wait, retries, delayed processing).
`id PK · city_id · task_type · payload_json · status · retry_count · next_attempt_at · created_at · updated_at`
Index on `(status, next_attempt_at)`.

**gemini_stats** — per-day usage.
`id PK · date UNIQUE · requests_count · successful_requests · failed_requests · rate_limit_errors · server_errors`

**ai_cache** — SHA-256 keyed AI response cache.
`id PK · image_hash · provider · model · prompt_hash · response · created_at`
`UNIQUE(image_hash, provider, model, prompt_hash)`

**operation_logs** — every important op.
`id PK · correlation_id · city_id · task_id · operation · start_time · end_time · duration · status · error_message`

All deletes cascade (city → photos/drafts/published). See `core/database.py`.

---

## 5. State Machine

Centralized in `core/models.py`; every transition is validated by
`check_transition()` and illegal ones raise `StateTransitionError`.

**City:** `queued → processing → drafted → approved → publishing → published`
Errors: `processing → error`, `publishing → error`. Recovery: `error → queued`.
`published` is terminal (no transition *without* explicit admin action).

**Draft:** `pending → approved | rejected` · `approved → published | pending` ·
`rejected → pending` · `published` terminal.

**Publication:** `pending → processing → scheduled → published` ·
`processing/scheduled → failed` · `failed → pending | disabled` ·
`published`/`disabled` terminal.

Publication status is **per (city, platform)** and is independent of the city's own
status, so one city can be `published` on Facebook while its Zen draft is still
`approved` (section 38).

---

## 6. Project Structure

```
travel-blog-app/
├── app.py                  # FastAPI backend (127.0.0.1:8000)
├── cli.py                  # scan/generate/process/publish/status/queue/retry/approve
├── config.yaml             # all settings (no secrets)
├── .env.example            # template for secrets → copy to .env
├── .gitignore
├── requirements.txt
├── README.md
├── ARCHITECTURE.md
├── run.sh / run.bat
├── core/                   # config, exceptions, logging, models, database, scheduler
├── modules/
│   ├── scanner.py queue_manager.py generator.py approver.py
│   ├── ai/       base gemini deepseek mock rate_limiter cache generator
│   ├── content/  base generator profiles validators
│   ├── media/    processor presets video
│   └── publishers/ base facebook vk telegram zen trip (+ mocks)
├── ui/            dashboard queue drafts publications settings components
├── drafts/ published/ media_ready/ logs/     # runtime artifacts (gitignored)
└── tests/
```

---

## 7. Platform Adapter Decisions (validated reality — section 72)

| Platform | Official API | Automatic publishing available? | Decision |
|----------|--------------|--------------------------------|----------|
| **Telegram** | Bot API ✅ | text/photo/media-group/video via JSON POST | ✅ automatic; **scheduling is done by the app scheduler** (Bot API has no native scheduled send). |
| **VKontakte** | VK API ✅ | `wall.post` with `publish_date` (≤~7 days ahead) supports scheduled posts | ✅ automatic; resolve latest stable `api_version` at runtime (not hardcoded). |
| **Facebook** | Graph API ⚠️ | Page posts yes; **scheduled posts for Pages is version/approval-dependent** | Partial: text+photos automatic; scheduling via app scheduler or page-eligible path; verify current Graph version. |
| **Zen/Dzen** | ❌ no public publish API | no | **manual-assisted mode** (per spec §32): prepare the full article + media, hand to user to paste/publish. |
| **Trip.com** | ❌ no public API | web automation only | **manual-assisted** (per spec §36): Selenium attempt; on CAPTCHA/block → prepared content + instructions. Never bypass CAPTCHA. |
| **Instagram** | Graph API ⚠️ | requires Instagram Business + app review + own content publishing approval | MVP: **manual-assisted / staged**; document the approval path. |
| **YouTube** | Data API v3 ⚠️ | requires OAuth2 consent + channel auth + upload quota | MVP: **manual-assisted**; document the OAuth consent flow. |

**Rule honored everywhere:** if official automation is impossible, we DO NOT fake a
successful publication — we produce the fully prepared post (draft + media) and
report `manual` so a human completes it (ADR-102: e.g. VK photo uploads are flagged
`manual`, never a fake `published`). The publication record tracks this as
`manual`/`failed` with a clear `error_message`, never as `published`.

---

## 8. AI Architecture

```
AIProvider (ABC)
├── GeminiProvider   # vision analysis (google-genai SDK)
├── DeepSeekProvider # text drafting (OpenAI-compatible)
└── MockAIProvider   # deterministic, no API, for tests/dry-run/development
```

Interface: `generate_text() · analyze_image() · generate_post() · health_check()`.

**One global `RateLimiter`** through which every Gemini request passes: requests-per-
minute, requests-per-day, min-interval + jitter (4.3s ± 0.3s), 429 → 60s retry (max 3),
500 → 5/15/45s backoff. Every request lands in `gemini_stats`. Exhausted daily quota →
task moves to `pending_tasks` for the next permitted window.

**AI cache (section 13):** key = `sha256(image) + provider + model + prompt_hash`.
Identical request → cached `response`, no repeat Gemini call. Guarantees *analyze once,
reuse everywhere* (section 81).

**Factual content policy (section 14):** every claimed fact must trace to EXIF,
folder, filename, or a verified source. Unverifiable statements are produced as
*observations/questions*, never asserted as fact.

---

## 9. Retry & Error Classification (section 39)

```yaml
retry: { max_attempts: 3, initial_delay_seconds: 60, exponential_backoff: true }
```

Errors are classified `temporary | permanent | authentication | rate_limit |
validation`. **Permanent** and **validation** errors stop retrying immediately; only
temporary/rate-limit/authentication errors are retried with exponential backoff.
A single failed platform never blocks the others (section 67).

---

## 10. Idempotency & Recovery (sections 37, 53)

- Before publishing: check `(city_id, platform, content_version, status, external_id)`
  in `published`; if already `published` → skip, never republish.
- `save_published` inserts only if the (city, platform) row is absent.
- On restart: find stuck `processing`/`publishing` cities, mark them via `error`,
  requeue. Successful publications are never re-run.

---

## 11. Security (section 65)

- Secrets ONLY in `.env` (gitignored). Nothing secret in `config.yaml` or source.
- Path validation (no traversal) on archive/media paths.
- Masked secrets in the Settings UI.
- No credentials/session data in logs or Git.
- Pre-commit credential scan (gitleaks-style) before any commit.

---

## 12. Implementation Plan (PHASE PLAN)

- **P1 Foundation** ✅ — structure, config, logging, exceptions, models, database.
- **P2 Scanner** — recursive/incremental scan, EXIF, city detection, photo DB.
- **P3 Queue** — city queue, priority, state machine enforcement, retry, recovery.
- **P4 AI** — provider abstraction, Gemini, DeepSeek, rate limiter, cache, mock.
- **P5 Content** — base story, profiles, platform generation, factual validation.
- **P6 Media** — image processing, presets, video generation.
- **P7 Drafts** — draft DB, Streamlit review, approve/reject/edit/regenerate.
- **P8 Publishers** — Telegram, VK, Facebook (partial), Zen (manual), Trip (manual), + mocks.
- **P9 Scheduler** — daily processing, publication, retry, pending tasks, recovery.
  - `plan()` maps approved, media-ready drafts -> Publication rows: auto/partial get a `scheduled` row with a real `scheduled_at` (honouring `schedule.posts_per_day`, `publish_time`, `timezone`, spread `24/posts_per_day` apart); manual platforms get a `manual` row (human finishes them — never faked `published`).
  - `run_due()` publishes every `scheduled`/`pending` publication whose `scheduled_at` has passed, via `PublishService`; `retry_failed()` requeues `failed` rows as `pending` with exponential backoff bounded by `retry.max_attempts`.
  - **Decision (2026-08-28):** `PublishService` now *upserts* the publication row (updates the existing `(city, platform)` row in place) instead of relying on `save_published`'s idempotent no-op. Without this the scheduler could not progress a pre-created `scheduled` row to `published`. Scheduler is pure async logic; APScheduler wiring lives in a lazy `build_async_scheduler()` so tests never spin up a daemon.
- **P10 Dashboard** — KPI, charts, publication matrix, errors, activity.
  - `modules/stats.py` — `StatsService` (read-only, unit-tested): `summary()` (cities/photos/drafts/publications + scheduled/published/errors), `by_status()`, `by_platform()`, `recent()`. No Streamlit dependency.
  - `ui/dashboard.py` — Streamlit read-only view (key metrics, pipeline-by-status charts, publications-by-platform matrix, recent publications). Fresh SQLite connection per rerun via a single event loop (`asyncio.run(_collect())`), connection closed in `finally` — avoids aiosqlite dead-loop leakage on reruns.
- **P11 Tests** — full pytest suite (isolated, no real API/quota/files).
- **P12 Final verification** — clean install, real startup, end-to-end dry-run.
  - `app.py` — FastAPI admin (lifespan opens one async Database on the event
    loop; GET /health, /api/stats, /api/calendar; POST /api/scheduler/tick,
    /api/scheduler/publish-due, /api/pipeline/content/{city_id}). `run.sh` no
    longer gap. Verified: `python -m uvicorn app:app` boots ("Application startup
    complete"), endpoints 200 via TestClient.
  - `cli.py` — orchestration: scan / content / approve / plan / publish / tick /
    stats / dashboard (argparse + one async dispatch; prints JSON).
  - `modules/scheduler.py::build_async_scheduler()` constructs an
    AsyncIOScheduler with the tick job (apscheduler 3.11 verified).
  - `requirements.txt` trimmed to the installable+used set (google-genai and
    reverse_geocoder commented out — native build fails without Xcode CLT).
  - `modules/ingest.py` — browser photo ingestion: validates bytes (Pillow),
    writes to `{archive}/{City}_{Year}/`, upserts the City (status QUEUED),
    records every photo (scan_status=SCANNED), skips duplicates by sha256,
    rejects non-images. Framework-agnostic `(name, bytes)` input. Wired into the
    Streamlit dashboard as an "Upload photos" form (city + year + file uploader).
    Tested: `tests/test_ingest.py` (create city, dedup, reject non-image).
  - `ui/dashboard.py` — 3 tabs: Upload (ingest), Posts (review/copy content,
    complete MANUAL publications), Stats (read-only KPIs). The Posts tab shows the
    generated post (title + content) in a copyable code block, and for manual
    platforms (zen/instagram/youtube/trip_com) exposes "Mark as manually published".
    Bootstrap: project root added to `sys.path` before imports — Streamlit runs the
    script with cwd=`ui/`, so `core`/`modules` are otherwise not importable.
  - `modules/publishers/service.py::mark_manual_published(draft_id)` — a human has
    placed a manual-platform post. Marks the publication PUBLISHED (+published_at),
    or creates a PUBLISHED publication if none exists yet (plan() never ran), and
    transitions the APPROVED draft to PUBLISHED so stats stay honest. Idempotent.
  - `core/models.py::_PUBLICATION_TRANSITIONS` — added `MANUAL -> {PUBLISHED,
    DISABLED}`. Previously MANUAL had no legal outgoing transition, so a manual
    platform could never be completed. SCHEDULED is deliberately NOT reachable from
    MANUAL (no fake scheduling for human posts).
  - Full suite 40 passed; e2e dry-run `test_e2e_pipeline` passes.

Completion is gated on the FINAL ACCEPTANCE CHECKLIST and the dry-run pipeline test
(section 75).

---

## 13. VibeCoding content type (feature F1–F7, added 2026-08-29)

A new content type that generates blog posts about "вайбкодинг" (vibe coding):
topic → text (DeepSeek) + unique image → platform publishing. It reuses the
existing media/publisher/scheduler machinery rather than inventing new ones.

### Decisions (recorded in state-machine/models + ARCHITECTURE register)

- **D6 (implemented).** Image generation uses **httpx REST** directly, NOT the
  `replicate` / `openai` SDKs (neither is installed; the project calls all
  external services over httpx — same as Gemini/DeepSeek). Providers:
  Replicate (`POST /v1/models/{model}/predictions` + poll), OpenAI DALL-E
  (`POST /v1/images/generations`), HuggingFace inference. Under
  `app.dry_run` (the default) the `ImageGenerator` falls back to a Pillow
  placeholder so the whole pipeline runs offline with no keys.
- **D7 (implemented).** VibeCoding posts have their own small state machine
  (`VibeCodingStatus`, `core/models.py::_VIBECODING_TRANSITIONS`) mirroring the
  project rule "never write a status directly — go through validated
  transitions". `draft → pending/published/error`, terminal `published`.
- **D8 (implemented).** Publishing reuses the honest publisher adapters via
  `build_publisher`. A `VibeCodingPublisherService` (`modules/publishers/vibecoding.py`)
  maps a post onto the existing `BasePublisher` contract: auto platforms
  (telegram/vk/facebook) are published through their adapters (Mock under
  dry_run); manual platforms (trip_com/zen/instagram) are prepared and flagged
  `manual` — the pipeline never fakes `published`. Overall post status:
  `published` if any auto platform succeeded, `pending` if content prepared for
  a human, `error` if all failed. Idempotent: an `published` post is returned
  as-is on re-publish.
- **D9 (implemented).** Scheduling: `Scheduler.publish_vibecoding_due()`
  publishes `pending` posts always, and `draft` posts only when
  `vibecoding.auto_publish` is true. A separate APScheduler cron job
  (`vibecoding-publish`) fires at `vibecoding.schedule_time` on
  `vibecoding.schedule_days`. The whole feature is gated by `vibecoding.enabled`.
- **D10 (implemented).** Secrets: `REPLICATE_API_TOKEN` / `OPENAI_API_KEY` /
  `HUGGINGFACE_API_KEY` live only in `.env` (added to `core.config.Secrets`);
  `config.yaml` holds only settings (block style). The settings UI writes keys
  to `.env` and settings to `config.yaml`, never the other way round.

### Files

- `core/models.py` — `VibeCodingStatus`, `_VIBECODING_TRANSITIONS`,
  `vibecoding_transition()`, `VibeCodingPost`.
- `core/database.py` — `vibecoding_posts` table + CRUD (add/get/list/update/
  delete + `update_vibecoding_status`).
- `core/config.py` — `VibeCodingConfig` (+ `ImageGenerationConfig`,
  `TextGenerationConfig`, `VibeDefaultPrompts`) and secrets.
- `modules/vibecoding_generator.py` — `ImageGenerator` (httpx + mock) and
  `VibeCodingGenerator` (`generate_post`, `save_post`, `generate_and_save`).
- `modules/media.py` — `prepare_vibecoding_media()` (per-platform assets under
  `media_ready/vibecoding/{post_id}/`).
- `modules/publishers/vibecoding.py` — `VibeCodingPublisherService`.
- `modules/scheduler.py` — `publish_vibecoding_due()` + cron job.
- `ui/vibecoding_page.py`, `ui/settings_page.py` — new dashboard tabs; the Stats
  tab gained a VibeCoding KPI block.
- `tests/test_vibecoding.py` — 8 tests (DB + state machine, generator, media,
  publisher, scheduler), all dry_run/mock (no network, no keys).

### Verification

Full suite passes: `pytest tests/` → 49 passed.

---

## 14. Content compliance validation (feature G1–G7, added 2026-08-29)

Automatic validation of both content types against platform/expert guidelines —
Trip.com (Trip Moments) and VibeCoding (educational/expert). Runs both in the UI
(checklist panels) and in the scheduler (before publishing).

### Decisions (recorded in the architecture register)

- **D11 (implemented).** Two independent validators share a common scaffolding:
  `modules/validation_base.py` (dataclasses `ValidationCheck`/`ValidationResult` +
  text helpers word_count/count_hashtags/contains_emoji/has_question/find_forbidden).
  `TripValidator.validate(draft, city)` and `VibeCodingValidator.validate(post)`
  never raise — they return a `ValidationResult` ({checks, compliant, score,
  recommendations}). Severity per check: `error` gates compliance, `warning`/`info`
  are recommendations that don't block.
- **D12 (implemented).** The gate is **soft by default**: both guideline blocks add
  `block_non_compliant: false`. Validation ALWAYS runs, is logged via Loguru
  (logger.bind(validator, post_id)) and shown in the UI; publishing is only blocked
  when `block_non_compliant: true`. This preserves the existing honest-automation
  flow while giving the user clear "recommendations to improve" (returned by the
  validator and rendered by `ui/validation_view.py`).
- **D13 (implemented).** Realistic heuristics, not fake assertions: Trip
  restaurant minimum (5 photos) is inferred from per-photo AI `analysis` keywords;
  video-duration checks are marked *info* "not applicable" because the current
  Draft/VibeCodingPost shape has no video fields (never faked as passed/blocked);
  VibeCoding `min_photos: 2` is a warning (a post carries one AI image) while
  `min_words` below the floor is an error (thin content).

### Files

- `modules/validation_base.py` — shared `ValidationCheck`/`ValidationResult` + helpers.
- `modules/trip_validator.py` — `TripValidator` (photos/min/restaurant/interior,
  min_words, forbidden patterns, geotag, AI disclaimer, title emoji, video-info).
- `modules/vibecoding_validator.py` — `VibeCodingValidator` (word range, photos,
  title length, hashtags, forbidden clickbait, engagement question, media,
  code-screenshot, video-info).
- `core/config.py` + `config.yaml` — `trip_guidelines`, `vibecoding_guidelines`.
- `modules/scheduler.py` — `_trip_gate()`/`_vibe_gate()` + block logic in
  `run_due()` (trip_com) and `publish_vibecoding_due()`.
- `ui/validation_view.py` — shared Streamlit checklist renderer; wired into the
  Posts tab (Trip) and the VibeCoding page.
- `tests/test_validators.py` — 10 tests (validators + soft/block gates).

### Verification

Full suite passes: `pytest tests/` → 59 passed.

---

## 15. Visual Narrative Studio (feature P14, ADR-106, added 2026-09-18)

A visual-narrative layer between the AI photo analysis and the platform content.
It turns a folder of analysed photographs + facts into an *intentional* visual
story: a narrative arc, an emotional journey, a storyboard (ordered shots with
captions, alt-texts, crop/focus hints and pacing) and the accessibility /
cultural-sensitivity notes that must accompany it.

```
PHOTO SELECTION
    v
AI IMAGE ANALYSIS
    v
VISUAL NARRATIVE STUDIO   (modules/visual_narrative_studio.py)
    v
BASE TRAVEL STORY         (prompt gets the storyboard block)
    v
PLATFORM CONTENT
```

### Decisions (recorded in the architecture register)

- **ADR-106 (implemented).** The studio is an *additive step inside* the existing
  pipeline, not a new city status. `modules/content/engine.py` calls
  `_build_visual_narrative()` after `_analyse_photos()` and appends the rendered
  storyboard to the base-story prompt; the city machine stays
  `QUEUED → PROCESSING → DRAFTED/ERROR` (public contract: CLI, API, tests, UI).
  The human gate lives in the storyboard's own state machine
  (`draft → approved → archived`, `core/models.py`), so
  `visual_narrative.require_approval: true` (default **false**) is the only way to
  make platform content wait for approval — the current pipeline is unchanged by
  default.
- **ADR-106b.** All AI work goes through `BaseAIProvider`: the new
  `generate_visual_narrative_plan(context, max_photos=...)` has a **default
  implementation in `base.py`** (one text call → one JSON document → domain plan),
  so Gemini/DeepSeek inherit it through their existing `_generate_text_raw` HTTP
  paths without touching their code. `MockProvider` overrides it with a
  deterministic plan (`modules/narrative_heuristics.local_plan`), and
  `provider: local_vlm` is a reserved extension point resolved by
  `modules/ai/registry.py` (raises a clear `ConfigurationError` until
  `modules/ai/local_vlm.py` exists). `analyze_photo_sequence()` reuses the
  pipeline's own analysis prompt so `ai_cache` hits and no quota is re-spent.
- **ADR-106c.** Honest degradation instead of silent failure: a provider error
  falls back to the local heuristics and marks the plan `degraded` with a reason
  (reported by the API/UI), a city without photos is skipped, and the whole step
  is wrapped so the studio can never break a city.
- **ADR-106d.** Narrative logic has exactly one home: pure helpers in
  `modules/narrative_heuristics.py` (arc, pacing, crop/focus heuristics, alt-text
  validation, `ensure_arc` invariants: canonical beat order, required beats
  synthesized if the material cannot afford them, one hero image, shots restricted
  to analysed photos), prompt text in `modules/narrative_prompts.py`, SQL in
  `core/database.py`, orchestration in `modules/visual_narrative_studio.py`. The
  API and the Streamlit page share the same `StoryboardPatch`/`ShotPatch`/
  `StoryboardUpdateRequest` objects — no duplicated business logic.
- **ADR-106e.** Storyboards are versioned rows: every generation writes a new
  `version` (unique per city), so history is immutable and the edit path is
  explicit — editing an `approved` storyboard demotes it to `draft`.
  Accessibility and cultural-sensitivity notes are persisted on the storyboard
  (`*_notes_json`), because losing them would silently drop the a11y layer.

### Files

- `core/models.py` — `NarrativeBeatType`, `StoryboardStatus` (+ transition table),
  `NarrativePhoto`, `NarrativeContext`, `NarrativeBeat`, `StoryboardShot`,
  `Storyboard`, `VisualNarrativePlan`, `StoryboardBundle`, `VisualNarrativeResult`.
- `core/database.py` — `storyboards`, `narrative_beats`, `storyboard_shots`
  (idempotent `CREATE TABLE IF NOT EXISTS`), CRUD/atomic graph write, column
  whitelists, row converters.
- `core/config.py` + `config.yaml` + `.env.example` — `visual_narrative`
  (`enabled`, `provider`, `max_photos`, `require_alt_text`,
  `enforce_narrative_arc`, `allow_manual_override`, `require_approval`) and the
  reserved `LOCAL_VLM_*` variables.
- `core/exceptions.py` — `StoryboardValidationError` (carries `issues`).
- `modules/ai/base.py` — `generate_visual_narrative_plan()` default implementation,
  `analyze_photo_sequence()`, JSON extraction + payload→plan mapping.
- `modules/ai/mock.py`, `modules/ai/registry.py` — deterministic mock plan, the
  `local_vlm` extension point.
- `modules/narrative_heuristics.py`, `modules/narrative_prompts.py`,
  `modules/visual_narrative_studio.py` — pure logic, prompt data, service.
- `modules/content/engine.py` — `_build_visual_narrative()`, storyboard block in
  `generate_base_story(city, facts, narrative_block="")` (backwards compatible).
- `app.py` — `GET /api/cities/{id}/storyboard`,
  `POST /api/cities/{id}/storyboard/generate`, `PUT /api/cities/{id}/storyboard`,
  `POST /api/cities/{id}/storyboard/approve`, `GET /api/storyboards/{id}` +
  domain-error handlers (404/409/422).
- `ui/storyboard_page.py` + `ui/dashboard.py` — «🎬 Storyboard» tab (generate,
  beats, shot order, captions/alt-texts, hero image, save, approve).
- `tests/test_storyboard_models.py`, `tests/test_visual_narrative_studio.py`,
  `tests/test_storyboard_api.py` — 49 tests.

### Verification

Full suite passes: `pytest -q` → 130 passed (81 before the feature);
`python -m compileall app.py cli.py core modules ui tests` → clean.

## 16. Tri-Face Carousel Factory (feature P15, added 2026-09-18/19)

Universal carousel engine for three verticals — **Travel / QA / Vibecoding** — fed by a URL
or a GitHub object (repo / issue / PR / discussion / release) and producing six 768×1376 JPG
slides for TikTok + Instagram.

Pipeline: `SOURCE → SOURCE RESOLVER → VERTICAL DETECTION → RESEARCH CONTEXT → HOOK ENGINE →
NARRATIVE ENGINE → SLIDE PLANNER → HYBRID RENDERER → QA → DRAFT → HUMAN APPROVAL →
UPLOAD-POST → ANALYTICS → LEARNINGS → NEXT RECOMMENDATION`.

### Decisions (recorded in the architecture register)

- **Canonical models live in `core/models.py`; `modules/carousels/{enums,models}.py` are
  re-export facades.** `core/database.py` must know the models, and importing `modules.*`
  from `core.*` would create a cycle (same reason as ADR-106). All SQL stays in
  `core/database.py`; `modules/carousels/database_helpers.py` only delegates.
- **The state machine is a thin facade with the human-in-the-loop guard inside.** Two
  switches are needed for autonomy — `autonomy.mode=full_autonomous` **and**
  `require_human_approval: false`; anything else keeps a person in the loop.
  `PublishNotApprovedError` is the refusal, mapped to 409 by the API.
- **Every external service sits behind an abstraction:** `BaseSourceResolver` (URL over
  httpx + stdlib HTML parsing, GitHub over REST/GraphQL, mock for dry runs),
  `BaseSlideRenderer` (Pillow today), `BackgroundProvider` (gradient / source image /
  Gemini with a stated reason), `BaseCarouselPublisher` (mock / Upload-Post),
  `BaseMetricsCollector` (live / offline stand-in).
- **Deterministic rendering.** Exact text, code, metrics and lists are painted by Pillow
  from the slide plan; every image carries a sha256 and a `.layout.json` sidecar, and no
  text may enter the bottom 20% of the slide (TikTok UI overlays).
- **Facts stay traceable.** Slide claims are checked by the fact guard against the resolved
  source context; the CTA slide is the only claim-free slide (its text comes from the
  vertical profile). Thin sources produce `low_confidence` and a manual follow-up instead of
  invented content.
- **Publishing is idempotent per `(request_id, platform)`** (one Upload-Post request id can
  cover two platforms), enforced by the partial unique index
  `idx_carousel_publications_request_id`; a repeated publish reuses the stored rows.
  Dry-run writes `PublicationStatus.MANUAL` and uploads nothing.
- **Analytics reads, never guesses.** `PlatformMetrics.is_empty()` is a legitimate answer: an
  empty provider response stays empty (no zeros, no averages, no "likely" numbers). The
  carousel score is a weighted sum of **rates** (comparable across account sizes) with the
  weight of a missing metric redistributed, and it is single-sourced — `cohort_for(outcomes,
  job_id)` always excludes the carousel being scored, so the score endpoint and the learnings
  refresh can never disagree.
- **Learnings aggregate only `published` jobs that actually returned metrics**, and only
  groups with ≥ `min_sample_size` produce recommendations; no ML, no invented numbers.

### Files

- `core/models.py` — carousel enums/models (`CarouselVertical`, `CarouselSourceType`,
  `CarouselStatus`, `CarouselSlide`, `CarouselJob`, `CarouselPublication`, `CarouselMetric`,
  `CarouselLearning`, `_CAROUSEL_TRANSITIONS`, `CAROUSEL_PUBLISHABLE_STATUSES`).
- `core/database.py` — ten additive tables (`CREATE ... IF NOT EXISTS`) + indexes and their
  CRUD; `modules/carousels/database_helpers.py` — delegation facade.
- `modules/carousels/` — `service.py` (the ten stages as service methods),
  `state_machine.py`, `vertical_profiles.py`, `fact_guard.py`, `sources/`, `hooks/`,
  `narrative/planner.py`, `render/`, `publishing/`, `analytics/`.
- `app.py` — carousel routes (create/status/research/narrative/slides/render/verify/submit/
  approve/reject/publish/queue/publications/metrics/learnings/score).
- `ui/carousel_page.py` + `ui/dashboard.py` — «🎠 Carousels» tab (Hub, pipeline, approval
  queue, analytics, learnings). `cli.py carousel` — 18 actions, `--db`, `--fixture`.
- `tests/test_carousel_{models,state_machine,database,service,sources,hooks,planner,
  planning,render,rendering,publishing,approval,api,analytics,learnings,cli}.py`.

### Verification

`pytest -q` → **392 passed** (131 at baseline); `compileall app.py cli.py core modules ui
tests` → clean; `scan_credentials.py --staged` clean before every commit. Beyond the unit
suite: an offline CLI walk on an isolated DB reached `awaiting_approval → approved →
published (manual)`, and a live end-to-end analytics run over real loopback HTTP (four
carousels published to a local stand-in of `api.upload-post.com`) proved the honest-metrics
path and caught the two-scores defect described above.


# Tri-Face Carousel Factory (ADR-107) — фазы 1–7

Draft PR: подготовка к ревью и мержу. **Мерж — только по решению владельца.**
Ветка `feature/tri-face-carousel-factory`, 12 коммитов впереди `main`.

Один движок каруселей с тремя вертикалями (Travel / QA / Vibecoding):
вход — URL статьи или GitHub (repo / issue / PR / discussion / release),
выход — 6 слайдов 768×1376 JPG для TikTok и Instagram, с обязательным
человеческим согласованием перед публикацией.

**Что не делалось:** ни одного обращения к боевым сервисам, ни одного реального
ключа (Gemini / Upload-Post / GitHub / платформенные API), ничего не публиковалось
в соцсети, `full_autonomous` не включался, в `main` ничего не мержилось.

## 1. Что закрыто по фазам

| Фаза | Коммит | Содержание |
|------|--------|------------|
| 1 | `26d36f5` | Схема и модели: 10 таблиц, enum'ы, CRUD, state machine с human-in-the-loop guard |
| 2 | `01b4b7e` | Источники: URL/HTML и GitHub (repo, issue, PR, discussion, release), `canonical_url` как ключ дедупликации, аудит-запись источника |
| 3 | `ce93e70` | Hook engine, narrative planner, fact guard (`_active_context` — единственный источник фактов) |
| 4 | `a55eee0`, `0c61393` | Рендер (Pillow): детерминированная вёрстка, 6 JPG 768×1376, нижние 20 % не заняты текстом, `.layout.json` рядом, верификация слайдов |
| 5 | `ad3e63e` | Согласование + публикация: `BaseCarouselPublisher`, mock и Upload-Post, 22 API-роута |
| 6 | `1d9eeb0` | Аналитика: сбор метрик только из ответа провайдера, скоринг по RATE, learnings без ML |
| 7 | `091ba21` | Streamlit-страница «🎠 Carousels» (Hub / Pipeline / Approval Queue / Analytics / Learnings) и CLI `carousel` |
| docs | `4843dfd`, `4de7943` | README, ARCHITECTURE (раздел 16), `.planning/STATE.md`, строка ADR-107 в таблице ADR |
| pre-PR | *(этот коммит)* | Streamlit smoke, mypy/ruff по carousel-коду, живой QA/GitHub E2E |

## 2. Что появилось в репозитории

**Новые модули** — `modules/carousels/**` (34 файла): `service.py`,
`sources/{base,detect,registry,url,html,github,mock}.py`,
`hooks/engine.py`, `narrative/planner.py`, `render/{base,pillow_renderer,fonts,providers,verification}.py`,
`publishing/{base,mock,uploadpost}.py`, `analytics/{base,collectors,scoring,learnings}.py`,
`state_machine.py`, `fact_guard.py`, `vertical_profiles.py`, `database_helpers.py`,
`enums.py`, `models.py`.

**Расширено:** `core/models.py` (модели и enum'ы каруселей), `core/database.py`
(10 таблиц, CRUD, частичные уникальные индексы), `core/config.py` (`carousels.*`
включая `health`, `learning`, `upload_post`, `analytics`), `core/exceptions.py`.

**Таблицы (10):** `carousel_jobs`, `carousel_sources`, `carousel_slides`,
`carousel_hook_candidates`, `carousel_publications`, `carousel_metrics`,
`carousel_learnings`, `carousel_templates`, `qa_artifacts`, `vibecoding_sessions`.
Идемпотентность публикации — частичный уникальный индекс
`idx_carousel_publications_request_id` по `(request_id, platform)`.

**API (22 роута, `app.py`):**

```
GET  /api/carousels/jobs                    GET  /api/carousels/jobs/{job_id}
POST /api/carousels/jobs                    GET  /api/carousels/jobs/{job_id}/status
GET  /api/carousels/queue                   GET  /api/carousels/jobs/{job_id}/publications
GET  /api/carousels/learnings               GET  /api/carousels/jobs/{job_id}/metrics
GET  /api/carousels/recommendations         GET  /api/carousels/jobs/{job_id}/score
GET  /api/carousels/analytics/summary       POST /api/carousels/jobs/{job_id}/research
POST /api/carousels/learnings/refresh       POST /api/carousels/jobs/{job_id}/narrative
POST /api/carousels/jobs/{job_id}/slides    POST /api/carousels/jobs/{job_id}/render
POST /api/carousels/jobs/{job_id}/verify    POST /api/carousels/jobs/{job_id}/submit
POST /api/carousels/jobs/{job_id}/approve   POST /api/carousels/jobs/{job_id}/reject
POST /api/carousels/jobs/{job_id}/publish   POST /api/carousels/jobs/{job_id}/metrics/collect
```

**UI:** `ui/carousel_page.py` (новая страница) + вкладка в `ui/dashboard.py`.
**CLI:** `python -m cli carousel …` — все стадии конвейера, `--db PATH`
(изолированная БД) и `--fixture PATH` (офлайн-факты, которые написал оператор).

## 3. Тесты

| | до фичи | после фичи |
|---|---|---|
| Весь сьют | 130 passed (`main`) | **404 passed, 1 warning** |
| Из них carousel | — | 273 (21 файл `tests/test_carousel_*.py`) |

Новое в этом коммите: `tests/test_carousel_ui.py` (8) и
`tests/test_carousel_e2e_qa.py` (4).

## 4. Что работает end-to-end в mock/dry-run

* **QA-вертикаль, полный конвейер** (`tests/test_carousel_e2e_qa.py`):
  GitHub issue → факты → hook-кандидаты → 6 слайдов → 6 JPG 768×1376 (проверено
  через Pillow) → верификация 6/6 → submit → approve → publish против
  HTTP-заглушки Upload-Post (`request_id` `req-qa-1` разобран из ответа и сохранён,
  одна multipart-отправка несёт 6 фото и обе платформы) → метрики ровно те, что
  отдала заглушка (`views` 12000, `likes` «1.2K» → 1200, `shares` 40, `views` 9000,
  `saves` 250) → score только по RATE → learnings.
* **GitHub-резолвер без токена**: issue / PR / repo резолвятся, ни в одном
  запросе нет заголовка `Authorization`; discussion без токена деградирует честно —
  warning про токен, confidence < 0.5 и ноль метрик вместо выдуманных чисел.
* **Streamlit-страница в живом Streamlit** (`AppTest`, реальный код страницы,
  сервисный слой подменён записывающей заглушкой): Hub отдаёт типы источников и
  вертикали из enum'ов, Pipeline рисует слайды и пишет про отсутствующий JPG
  только для неотрендеренных, очередь показывает job в `awaiting_approval`,
  обе кнопки решения вызывают `approve`/`reject`, Analytics/Learnings печатают
  ровно те числа, что вернул сервис, publish недоступен без APPROVED
  (кнопка `disabled`, клик не вызывает сервис), ни одного сетевого соединения
  за весь прогон страницы (перехвачены и сокет, и httpx).
* **CLI**: изолированная БД + `--fixture`, проход create → research → draft →
  plan → render → verify → submit → approve → publish → collect → score → reject
  (7 тестов `tests/test_carousel_cli.py`).
* **Travel-вертикаль** проверялась тем же конвейером в Phase 4–6 (6 JPG,
  publish, аналитика).

## 5. Проверки этого коммита

```
pytest -q                          → 404 passed, 1 warning in 106.95s
python -m compileall app.py cli.py core modules ui tests   → OK
mypy modules/carousels ui/carousel_page.py tests/test_carousel_ui.py tests/test_carousel_e2e_qa.py
   (--ignore-missing-imports --follow-imports=silent)      → без ошибок
ruff check --isolated --select F,E9,B … carousel scope      → All checks passed
scan_credentials.py --staged                               → exit 0
```

`mypy`/`ruff` поставлены в `.venv` для этого прогона (в `requirements.txt` их нет,
на остальной проект конфиг линтеров не расширялся).

Что нашлось и исправлено по дороге:

1. `cli.py` — ветка `learnings` переиспользовала переменную из ветки `metrics`
   (typing-несоответствие, при правке легко получить чтение не тех полей);
   имена разведены, `score` теперь возвращает структурированный ответ вместо строки.
2. `service.py` — id строк, прочитанных из БД, типизированы как `int | None`;
   добавлен `_require_id(value, what)` с внятной ошибкой вместо записи с `None`.
3. `sources/github.py` — список метрик PR мог содержать `None` до фильтра
   (mypy `list[Metric | None]`).
4. `render/{providers,pillow_renderer}.py` — `Image.load()` типизирован как
   `PixelAccess | None`; добавлена проверка перед индексацией.
5. `hooks/engine.py`, `narrative/planner.py` — переменная цикла переиспользовалась
   для строк и для `SourceFact` в одном скоупе.
6. `ui/carousel_page.py` — селектор типа источника предлагал `github`/`manual`/`mock`,
   которых нет в `CarouselSourceType` (создание из UI падало бы); список строится
   из enum'а, «auto» для вертикали передаётся как пустая строка.
7. `ui/carousel_page.py` — Pipeline читал `slide.image_path`, а поле называется
   `final_image_path`: отрендеренные JPG никогда не показывались.
8. `ui/carousel_page.py` — кнопка публикации теперь берёт разрешение у сервиса
   (`can_publish` из state machine) и блокируется, пока согласования нет.

## 6. Что осталось проверить с реальными ключами

Всё это требует ваших ключей и вашего решения — до этого код проверен только на
заглушках:

* `POST https://api.upload-post.com/api/upload_photos` с реальным ключом,
  реальным `user` и живыми TikTok/Instagram аккаунтами;
* сбор метрик платформ через Upload-Post (`carousel_metrics` получает только
  ответ провайдера, поэтому на живых аккаунтах проверяется формат ответа);
* Gemini-фон/иллюстрации (по умолчанию `dry_run: true`, фон не запрашивается);
* GitHub-сценарии с токеном: приватные репозитории и discussions через GraphQL
  (без токена discussion осознанно деградирует с warning);
* производительность на 10+ каруселях подряд и реальная нагрузка на state machine.

## 7. ADR-107

ADR-107 — «Tri-Face Carousel Factory»: один движок, три вертикали, публикация
только после человеческого согласования. Регистрируется в таблице ADR
`README.md` этим же коммитом, рядом с ADR-106 (канон моделей/enum'ов в
`core/models.py`, `modules/carousels/*` — фасады).

## 8. Известные ограничения (честно)

* **Публичный GitHub по реальному HTTP не проверялся**: в песочнице прямой
  сетевой вызов из терминала требует ручного подтверждения, которое не было дано.
  GitHub-резолвер проверен на `httpx.MockTransport` (без токена) — это и оставлено.
* **Легаси-ошибки типизации вне carousel-кода не трогались**: `mypy cli.py` даёт
  4 замечания в старых ветках (`res` переиспользуется под `dict` и под
  `list[Publication]`, строки 62 и 76) — к каруселям не относятся, поведение не
  менялось, отдельной задачей.
* **Streamlit-прогон — `AppTest`**, а не браузер: сокет и httpx запрещены, поэтому
  проверяется UI-контракт (какие элементы и какие вызовы сервиса), а не внешний вид.
  Layout 768×1376 проверен на реальных JPG из Pillow.
* `ruff` с конфигом по умолчанию даёт ~579 замечаний по стилю (SIM/E501 и т.п.) на
  carousel-коде — это выбранный стиль проекта, автофиксы не применялись; прогонялся
  набор реальных багов `F,E9,B`.

## 9. Как проверить локально

```bash
cd ~/projects/travel-blog-app
.venv/bin/python -m pytest -q                     # 404 passed
.venv/bin/python -m pytest tests/test_carousel_ui.py tests/test_carousel_e2e_qa.py -q
.venv/bin/python -m cli carousel --db /tmp/c.db create \
    --source-type url --source "https://example.com/post" --vertical travel
```

**Как мержить:** после вашего решения; требуются `carousels.enabled: true` в
`config.yaml` и — для реальной публикации — ключи Upload-Post в `.env`
(в репозитории только `.env.example`).

## 10. Как открыть этот PR

Ветка `feature/tri-face-carousel-factory` уже на GitHub. Текст выше —
готовое описание PR (лежит в `docs/pr-tri-face-carousel-factory.md`).

* Кнопкой: https://github.com/dmpotekhin/travel-blog-app/pull/new/feature/tri-face-carousel-factory
  → вставить описание из этого файла (черновиком — как в задаче и просили).
* Через GitHub CLI, если он появится в системе:

```bash
brew install gh && gh auth login
gh pr create --draft --base main --head feature/tri-face-carousel-factory \
  --title "Tri-Face Carousel Factory (ADR-107) — фазы 1–7" \
  --body-file docs/pr-tri-face-carousel-factory.md
```

PR не был создан автоматически: у инструментов в этой среде нет рабочей
авторизации в GitHub (`Bad credentials`), а чужие токены не подставляются.

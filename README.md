# Travel Blog Automation Platform

Автоматический конвейер: превращает архив фотографий путешествий в готовый
контент для нескольких площадок. Один город → выбор фотографий → AI-анализ →
базовая история → адаптация под платформы → черновики → одобрение человеком →
обработка медиа → планировщик → публикаторы → публикации → статистика.

Проект рассчитан на архив: 255 городов, 50 стран, вся Россия, ~1 ТБ фото с 2003
года, ~3000 подписчиков в Facebook.

---

## Pipeline

```
PHOTO ARCHIVE
   │  incremental scan
   ▼
EXIF → GPS / CITY / COUNTRY / YEAR
   ▼
CITY QUEUE
   ▼
PHOTO SELECTION
   ▼
AI IMAGE ANALYSIS
   ▼
BASE TRAVEL STORY
   ▼
PLATFORM CONTENT (Telegram, VK, Facebook, Zen, Trip.com, Instagram, YouTube)
   ▼
DRAFTS
   ▼
HUMAN APPROVAL
   ▼
MEDIA PROCESSING (image resize/presets; video — optional/deferred)
   ▼
SCHEDULER (posts_per_day, publish_time, timezone)
   ▼
PUBLISHERS (auto / partial / manual-assisted)
   ▼
PUBLICATION
   ▼
STATISTICS
```

Каждый этап — отдельный модуль с собственными тестами и состоянием в SQLite
(машина состояний `city`, `draft`, `publication`). Публикаторы честно разделены
на `auto` (Telegram, VK), `partial` (Facebook) и `manual-assisted`
(Zen/Dzen, Trip.com, Instagram, YouTube) — ручные платформы никогда не
имитируют успешную публикацию.

Помимо тревел-контента платформа генерирует и публикует **VibeCoding** — посты
о вайбкодинге: тема → текст (DeepSeek) + уникальное изображение (Replicate /
OpenAI / HuggingFace) → публикация по площадкам, со своим планировщиком
(`vibecoding.schedule_time` / `schedule_days`) и честными manual-платформами.

Каждый тип контента **автоматически валидируется** перед публикацией:
`TripValidator` — гайдлайны Trip.com (Trip Moments), `VibeCodingValidator` —
правила образовательного/экспертного контента. Проверки работают и в UI
(панель чек-листа с рекомендациями по улучшению), и в планировщике. По умолчанию
это **мягкий гейт** (`block_non_compliant: false`): проверка всегда выполняется,
логируется (Loguru) и показывается, но не блокирует публикацию; жёсткий блок —
`block_non_compliant: true` в соответствующем блоке `config.yaml`.

---

## Tech stack

- **Python** 3.11+, typed (`from __future__ import annotations`)
- **FastAPI** + **uvicorn** — админ-API
- **Streamlit** + **pandas** — дашборд статистики
- **aiosqlite** (SQLite, WAL) — хранение
- **APScheduler** — демон-планировщик
- **httpx** — AI-провайдеры (Gemini REST API, DeepSeek OpenAI-совместимый)
- **Pillow** — обработка изображений
- **loguru** — логи (daily rotation, gzip, correlation id)
- **pytest** — тесты

---

## Структура

```
travel-blog-app/
├── app.py                  # FastAPI admin (health, stats, calendar, scheduler API)
├── cli.py                  # CLI-оркестрация (scan/content/approve/plan/publish/tick/stats/dashboard)
├── run.sh                  # запуск: --cli → cli.py; иначе FastAPI + Streamlit UI
├── config.yaml             # настройки (без секретов)
├── .env.example            # шаблон секретов (скопировать в .env)
├── requirements.txt
├── core/
│   ├── config.py           # Config (app/server/archive/schedule/ai/gemini/deepseek/publishing)
│   ├── database.py         # Database (async, WAL), методы по всем сущностям
│   ├── models.py           # dataclass'ы + машина состояний (transitions)
│   └── exceptions.py       # ConfigurationError, MediaProcessingError, ...
├── modules/
│   ├── scanner.py          # инкрементальный скан архива, EXIF/GPS → город
│   ├── queue.py            # очередь городов
│   ├── ai/                 # провайдеры: gemini, deepseek, mock + registry/ratelimit/cache
│   ├── content/            # базовые истории, контент под платформы, черновики
│   ├── media.py            # обработка изображений (пресеты под платформы)
│   ├── drafts.py           # DraftManager (одобрение)
│   ├── publishers/         # base + telegram/vk/facebook/zen/trip/instagram/youtube
│   │                       # + mock + vibecoding (VibeCodingPublisherService)
│   ├── scheduler.py        # plan / run_due / retry_failed / tick / AsyncIOScheduler
│   │                       # + publish_vibecoding_due + compliance-гейты
│   ├── stats.py            # StatsService (summary/by_status/by_platform/recent)
│   ├── vibecoding_generator.py  # VibeCoding: текст DeepSeek + изображение (httpx/mock)
│   ├── validation_base.py       # ValidationCheck/Result + text-хелперы
│   ├── trip_validator.py        # TripValidator (гайдлайны Trip.com)
│   └── vibecoding_validator.py  # VibeCodingValidator (экспертный контент)
├── ui/
│   ├── dashboard.py        # Streamlit-дашборд (вкладки: Upload/Posts/Stats/VibeCoding/Settings)
│   ├── vibecoding_page.py  # генерация и публикация VibeCoding-постов
│   ├── settings_page.py    # настройки VibeCoding (провайдер, ключи, расписание)
│   └── validation_view.py  # общий рендерер чек-листа валидации
└── tests/                  # pytest: 59 тестов, включая e2e scan→publish, API и валидацию
```

---

## Установка

```bash
cd travel-blog-app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # затем заполнить токены (см. ниже)
```

> `requirements.txt` — минимальный набор, который одновременно используется
> кодом и ставится без сборки нативных модулей. Эти зависимости **вынесены в
> комментарий и не нужны для базового запуска**: `google-genai` (заменён на
> httpx REST), `reverse_geocoder` (не обязателен, город определяется по имени
> папки/файла), `selenium`/`webdriver-manager` (нужны только для
> manual-assisted Trip.com), `plotly`, `openai` (не используются). Для ручной
> установки дополнительных опций — включите их обратно.

---

## Где вставить токены

Все секреты — **только в `.env`** (файл `.env` никогда не коммитится, `.gitignore`
его игнорирует). В `config.yaml` секретов нет — только настройки. Значения
никогда не логируются.

Скопируйте шаблон и впишите значения:

```bash
cp .env.example .env
```

Откройте `.env` (Python/Anaconda) и заполните:

```
# --- AI провайдеры ---
GEMINI_API_KEY=вставьте_сюда_ключ_google_gemini
DEEPSEEK_API_KEY=вставьте_сюда_ключ_deepseek

# --- Facebook (Graph API, Page access) ---
FACEBOOK_ACCESS_TOKEN=...
FACEBOOK_PAGE_ID=...

# --- VKontakte ---
VK_ACCESS_TOKEN=...
VK_GROUP_ID=...

# --- Telegram (Bot API) ---
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# --- Trip.com (web automation, manual-assisted) ---
TRIP_USERNAME=...
TRIP_PASSWORD=...

# --- Генерация изображений для VibeCoding (только если используется этот провайдер) ---
REPLICATE_API_TOKEN=...            # Replicate
OPENAI_API_KEY=...                 # OpenAI (DALL-E)
HUGGINGFACE_API_KEY=...            # HuggingFace Inference
```

### Выбор AI-провайдера

В `config.yaml` секция `ai.provider` — `gemini` | `deepseek` | `mock`:

- **gemini** — анализ изображений + текст (vision). Ключ: `GEMINI_API_KEY`
  (важно: хост-машина должна иметь доступ к Google API).
- **deepseek** — генерация текста (OpenAI-совместимый, `base_url` в конфиге).
  Ключ: `DEEPSEEK_API_KEY`.
- **mock** — детерминированный, без сети и ключей (для разработки).

> Пока `app.dry_run: true` (по умолчанию), платформа автоматически использует
> mock-провайдер и mock-публикаторов — запускается «из коробки» без ключей и
> сети. Чтобы перейти к реальным вызовам: `dry_run: false` (в config.yaml) и
> заполненные ключи в `.env`.

---

## Запуск

```bash
# веб-режим: FastAPI-API (http://127.0.0.1:8000) + Streamlit-дашборд (UI-порт)
./run.sh

# только FastAPI-API
python app.py

# только дашборд
streamlit run ui/dashboard.py

# Дашборд открывается в браузере с пятью вкладками:
#   📤 Upload  — «Upload photos»: выберите город, укажите год и прикрепите фото —
#                они валидируются, попадают в локальный архив
#                `{archive}/{Город}_{Год}/` и встают в очередь (город QUEUED,
#                фото SCANNED) для этапа content.
#   📝 Posts   — сгенерированные посты: текст (title + content) выводится в код-блоке
#                с кнопкой копирования. Для manual-платформ (zen/instagram/youtube/
#                trip_com) есть кнопка «Mark as manually published» — после того как
#                вы вручную разместили пост в своём аккаунте, она помечает публикацию
#                PUBLISHED (и черновик PUBLISHED), чтобы статистика была честной.
#                Для trip_com доступна кнопка «Проверить гайдлайны Trip» — чек-лист
#                соответствия Trip Moments с рекомендациями.
#   📊 Stats   — read-only сводка (метрики, статусы, матрица платформ, недавнее) +
#                блок «VibeCoding» (всего/опубликовано/черновики/очередь/ошибки).
#   ✨ VibeCoding — генерация VibeCoding-постов: тема → текст (DeepSeek) + изображение;
#                черновик, «Опубликовать сейчас», «Запланировать», «Проверить»
#                (чек-лист гайдлайнов), список с фильтром по статусу.
#   ⚙️ Settings — настройки VibeCoding: провайдер изображений, ключи (в .env),
#                модель, расписание, автопубликация.
```

### CLI (оркестрация из терминала)

```bash
./run.sh --cli scan --archive /path/to/archive       # просканировать архив
./run.sh --cli content --city 1                      # сгенерировать контент для города
./run.sh --cli approve                               # автоматически одобрить черновики
./run.sh --cli plan                                  # распланировать публикации
./run.sh --cli publish --limit 20                    # опубликовать «созревшие»
./run.sh --cli tick                                  # один производственный цикл
./run.sh --cli stats                                 # сводная статистика
./run.sh --cli dashboard                             # подсказка про дашборд
```

---

## Конфигурация (`config.yaml`)

| Раздел | Ключ |
|--------|------|
| `app` | `dry_run` (true → mock-режим), `auto_publish` (false → ничего не публикуется без одобрения), `environment`, `timezone` |
| `archive` | `path`, `recursive`, `incremental` |
| `schedule` | `posts_per_day`, `publish_time`, `timezone` |
| `ai` | `provider` (gemini/deepseek/mock), `requests_per_minute`, `model`, `temperature` |
| `publishing` | флаги `facebook/vk/telegram/zen/instagram/youtube/trip_com` — включать площадку или нет. Теперь реально уважаются: выключенная площадка не генерирует публикации (ADR-102/105). |
| `vibecoding` | `enabled`, `schedule_time`, `schedule_days`, `auto_publish`, `image_generation` (provider/model/api_key_env/width/height), `text_generation` (max_tokens/temperature), `default_prompts` |
| `trip_guidelines` | правила валидации Trip.com: `min_photos_attraction/restaurant`, `min_words`, `forbidden_patterns`, `geotag_required`, `ai_disclaimer`, `block_non_compliant` |
| `vibecoding_guidelines` | правила валидации VibeCoding: `min_words/max_words`, `hashtag_count_min/max`, `forbidden_patterns`, `engagement_question_required`, `block_non_compliant` |
| `media_presets` | размеры/форматы под каждую площадку (проверять актуальные лимиты перед prod) |

---

## Тесты

```bash
pytest tests/ -p no:cacheprovider
```

Покрытие включает: scanner, queue, AI, content, media, drafts, publishers,
scheduler, stats, VibeCoding (generator/publisher/scheduler), API (FastAPI),
валидацию контента (Trip + VibeCoding validators и гейты) и сквозной e2e
`scan → content → approve → plan → publish`. Полный набор проходит (59 passed).

---

## Известные ограничения

- **Видео (Phase 6)** — отложено как manual/опциональное. `moviepy` 2.2.1 /
  разрешение ffmpeg-исполняемого файла при инициализации требует сетевого
  вызова и зависает на этой машине (нет Xcode CLT / одобрения). Нужно только
  для Instagram/YouTube (они и так manual-assisted). Ядро медиа (обработка
  изображений) работает.
- **reverse_geocoder** — опционален: если модуль не установлен, город
  определяется по имени папки/файла (fallback).
- **Валидация видео** — проверки `video_min/max_duration_sec` помечены как
  «не применимо»: в текущих моделях Draft/VibeCodingPost нет видео-полей,
  поэтому длительность честно не проверяется (без фейка прохождения).
- **VibeCoding-картинки** — для реальной генерации изображений (не в `dry_run`)
  нужен ключ выбранного провайдера в `.env` (`REPLICATE_API_TOKEN` /
  `OPENAI_API_KEY` / `HUGGINGFACE_API_KEY`). В mock-режиме используется
  подстановочная картинка (Pillow).
- **Машина без Xcode Command Line Tools** — нативные сборки
  (`cryptography` для google-genai, `scikit-learn` для reverse_geocoder,
  `go`/`native` для brew) не собираются. Всё в требованиях собрано так, чтобы
  ставиться без них.

---

## Политика секретов

- Только `.env`; `.env` в git не попадает (в `.gitignore`).
- Никаких реальных токенов в репозитории, `config.yaml`, коде, логах.
- `.env.example` — только шаблон с пустыми значениями.

Бизнес-логика, архитектура и решения по каждой фазе — в `ARCHITECTURE.md`;
текущий статус фаз — в `.planning/STATE.md`.

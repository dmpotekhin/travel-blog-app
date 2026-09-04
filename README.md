<div align="center">

# 🧳 Travel Blog Automation Platform

**Превращает архив фотографий путешествий в готовый контент для 7 площадок — за один конвейер.**

Скан архива → AI-анализ → базовая история → адаптация под платформы → черновики →
одобрение человеком → обработка медиа → планировщик → публикаторы → публикации → статистика.

![Python 3.11](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite%20%2B%20aiosqlite-003B57?logo=sqlite&logoColor=white)
![APScheduler](https://img.shields.io/badge/APScheduler-000000?logo=clockify&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B?logo=streamlit&logoColor=white)
![tested](https://img.shields.io/badge/tests-80%20passed%20✓-2ea44f)

<div>
 <sub>рассчитан на архив: <b>255 городов</b> · <b>50 стран</b> · ~1 ТБ фото с 2003 · ~3000 подписчиков в Facebook</sub>
</div>

</div>

---

## Почему это существует

У вас есть **большой, но неиспользуемый архив путешествий** — сотни городов, терабайты
фотографий. Вручную превращать это в посты для Telegram, VK, Facebook, Zen, Trip.com,
Instagram и YouTube — непосильно и разрозненно.

Платформа делает это **автоматически и честно**:

- **Авто** (Telegram, VK) — публикует напрямую через API.
- **Partial** (Facebook) — автоматизирует, что возможно, честно помечает деградацию.
- **Manual-assisted** (Zen/Dzen, Trip.com, Instagram, YouTube) — **никогда не имитирует**
  успешную публикацию: готовит полностью оформленный пост и помечает его как `manual`,
  чтобы вы разместили его сами, а статистика осталась правдивой.

---

## Конвейер

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
MEDIA PROCESSING (resize / presets под площадки; видео — опционально/отложено)
   ▼
SCHEDULER (posts_per_day, publish_time, timezone)
   ▼
PUBLISHERS (auto / partial / manual-assisted)
   ▼
PUBLICATION
   ▼
STATISTICS
```

Каждый этап — **отдельный модуль** со своими тестами и состоянием в SQLite (машины
состояний `city`, `draft`, `publication`). Один и тот же конвейер обрабатывает и **VibeCoding** —
посты о вайбкодинге: тема → текст (DeepSeek) + уникальное изображение (Replicate / OpenAI /
HuggingFace) → публикация по площадкам, со своим планировщиком и `manual`-платформами.

Каждый тип контента **автоматически валидируется** перед публикацией:

- `TripValidator` — гайдлайны Trip.com (Trip Moments);
- `VibeCodingValidator` — правила образовательного/экспертного контента.

Проверки видны и в UI (чек-лист с рекомендациями), и в планировщике. По умолчанию это
**мягкий гейт** (`block_non_compliant: false`): проверка всегда выполняется, логируется
(Loguru) и показывается, но не блокирует публикацию. Жёсткий блок — `true` в
соответствующем блоке `config.yaml`.

---

## 🏛️ Архитектура и решения

Модульный монолит с жёстким внутренним слоем и ABC-границей вокруг каждого внешнего
сервиса — любой из них можно заменить, не трогая бизнес-логику.

- **`docs/architecture-review-2026-09-04.md`** — архитектурное ревью персоной Software
  Architect: 11 разделов, находки F1–F11, план из 5 ADR.
- **Реализованы ADR-101…105** (все изменения прошли TDD, полный сьют — **80 passed**):

| ADR | Суть |
|-----|------|
| **101** | API/CLI читают `config.yaml` как единственный источник настроек (раньше — bare `Config()`). |
| **102** | Честная публикация: фото-VK → `manual`; классы ошибок `permanent`/`retryable`; флаги `publishing.*` реально уважаются. |
| **103** | Атомарный claim публикации (нет двойной отправки из CLI+API+планировщика) + enforcement машины состояний. |
| **104** | Честный медиа-путь: Telegram шлёт альбом (`sendMediaGroup`, до 10 фото) + флаг `degraded` вместо тихой деградации. |
| **105** | Удалено ~210 строк мёртвого кода; починен `/api/scheduler/publish-due`; синхронизированы доки; добавлены архитектурные guard-тесты. |

> Подробный дизайн и решения по каждой фазе — в [`ARCHITECTURE.md`](ARCHITECTURE.md);
> текущий статус фаз — в [`.planning/STATE.md`](.planning/STATE.md).

---

## 🧰 Технологии

| Слой | Стек |
|------|------|
| Язык | Python 3.11+, typed (`from __future__ import annotations`) |
| API | FastAPI + uvicorn (админ-эндпоинты) |
| UI | Streamlit + pandas (дашборд статистики) |
| Хранение | aiosqlite (SQLite, WAL) |
| Планировщик | APScheduler (демон) |
| AI | httpx → Gemini REST API, DeepSeek (OpenAI-совместимый), mock |
| Медиа | Pillow (presets/resize) |
| Логи | loguru (daily rotation, gzip, correlation id) |
| Тесты | pytest |

---

## 📁 Структура

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
│   ├── models.py           # dataclass'ы + машина состояний (transitions, claim)
│   └── exceptions.py       # ConfigurationError, StateTransitionError, MediaProcessingError, ...
├── modules/
│   ├── scanner.py          # инкрементальный скан архива, EXIF/GPS → город
│   ├── queue.py            # очередь городов
│   ├── ai/                 # gemini, deepseek, mock + registry/ratelimit/cache
│   ├── content/            # базовые истории, контент под платформы, черновики
│   ├── media.py            # обработка изображений (пресеты под платформы)
│   ├── drafts.py           # DraftManager (одобрение)
│   ├── publishers/         # base + telegram/vk/facebook/zen/trip/instagram/youtube
│   │                       # + mock + vibecoding (VibeCodingPublisherService)
│   ├── scheduler.py        # plan / run_due / retry_failed / tick + compliance-гейты
│   ├── stats.py            # StatsService (summary/by_status/by_platform/recent)
│   ├── vibecoding_generator.py  # VibeCoding: текст DeepSeek + изображение (httpx/mock)
│   ├── validation_base.py       # ValidationCheck/Result + text-хелперы
│   ├── trip_validator.py        # TripValidator (гайдлайны Trip.com)
│   └── vibecoding_validator.py  # VibeCodingValidator (экспертный контент)
├── ui/
│   ├── dashboard.py        # Streamlit-дашборд (Upload/Posts/Stats/VibeCoding/Settings)
│   ├── vibecoding_page.py  # генерация и публикация VibeCoding-постов
│   ├── settings_page.py    # настройки VibeCoding (провайдер, ключи, расписание)
│   └── validation_view.py  # общий рендерер чек-листа валидации
├── docs/
│   └── architecture-review-2026-09-04.md  # отчёт Software Architect (ADR-101…105)
└── tests/                  # pytest: 80 passed, включая e2e scan→publish, API и валидацию
```

---

## 🚀 Установка

```bash
cd travel-blog-app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # затем заполнить токены (см. ниже)
```

> `requirements.txt` — минимальный набор, который ставится **без сборки нативных модулей**.
> В комментарии вынесены опциональные зависимости, не нужные для базового запуска:
> `google-genai` (заменён на httpx REST), `reverse_geocoder` (город определяется по имени
> папки/файла), `selenium`/`webdriver-manager` (только для manual-assisted Trip.com),
> `plotly`, `openai`. При необходимости — верните их в список.

---

## 🔑 Где вставить токены

Все секреты — **только в `.env`** (файл в `.gitignore`, никогда не коммитится).
В `config.yaml` секретов нет — только настройки. Значения никогда не логируются.

```bash
cp .env.example .env
```

Заполните `.env`:

```ini
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

| Провайдер | Назначение | Ключ |
|-----------|------------|------|
| `gemini` | анализ изображений + текст (vision) | `GEMINI_API_KEY` (нужен доступ к Google API) |
| `deepseek` | генерация текста (OpenAI-совместимый) | `DEEPSEEK_API_KEY` |
| `mock` | детерминированный, без сети и ключей | — (для разработки) |

> Пока `app.dry_run: true` (по умолчанию), платформа автоматически использует mock-провайдера
> и mock-публикаторов — запускается «из коробки» без ключей и сети. Для реальных вызовов:
> `dry_run: false` и заполненные ключи в `.env`.

---

## ▶️ Запуск

```bash
# веб-режим: FastAPI-API (http://127.0.0.1:8000) + Streamlit-дашборд
./run.sh

# только FastAPI-API
python app.py

# только дашборд
streamlit run ui/dashboard.py
```

Дашборд открывается в браузере с пятью вкладками:

- 📤 **Upload** — «Upload photos»: выберите город, укажите год и прикрепите фото — они
  валидируются, попадают в `{archive}/{Город}_{Год}/` и встают в очередь (город `QUEUED`).
- 📝 **Posts** — сгенерированные посты (title + content). Для manual-платформ есть кнопка
  «Mark as manually published» (после ручной публикации помечает запись `PUBLISHED`, чтобы
  статистика была честной) и «Проверить гайдлайны Trip» (чек-лист Trip Moments).
- 📊 **Stats** — read-only сводка: метрики, статусы, матрица платформ, недавнее + блок VibeCoding.
- ✨ **VibeCoding** — генерация постов: тема → текст (DeepSeek) + изображение; черновик,
  «Опубликовать сейчас», «Запланировать», «Проверить» (чек-лист гайдлайнов).
- ⚙️ **Settings** — настройки VibeCoding: провайдер изображений, ключи (`.env`), модель,
  расписание, автопубликация.

### CLI (оркестрация из терминала)

```bash
./run.sh --cli scan --archive /path/to/archive   # просканировать архив
./run.sh --cli content --city 1                  # сгенерировать контент для города
./run.sh --cli approve                           # автоматически одобрить черновики
./run.sh --cli plan                              # распланировать публикации
./run.sh --cli publish --limit 20                # опубликовать «созревшие»
./run.sh --cli tick                              # один производственный цикл
./run.sh --cli stats                             # сводная статистика
./run.sh --cli dashboard                         # подсказка про дашборд
```

---

## ⚙️ Конфигурация (`config.yaml`)

| Раздел | Ключ |
|--------|------|
| `app` | `dry_run` (true → mock-режим), `auto_publish` (false → ничего без одобрения), `environment`, `timezone` |
| `archive` | `path`, `recursive`, `incremental` |
| `schedule` | `posts_per_day`, `publish_time`, `timezone` |
| `ai` | `provider` (gemini/deepseek/mock), `requests_per_minute`, `model`, `temperature` |
| `publishing` | флаги `facebook/vk/telegram/zen/instagram/youtube/trip_com` — включать площадку или нет *(реально уважаются, ADR-102/105)* |
| `vibecoding` | `enabled`, `schedule_time`, `schedule_days`, `auto_publish`, `image_generation` (provider/model/api_key_env/width/height), `text_generation` (max_tokens/temperature), `default_prompts` |
| `trip_guidelines` | правила Trip.com: `min_photos_attraction/restaurant`, `min_words`, `forbidden_patterns`, `geotag_required`, `ai_disclaimer`, `block_non_compliant` |
| `vibecoding_guidelines` | правила VibeCoding: `min_words/max_words`, `hashtag_count_min/max`, `forbidden_patterns`, `engagement_question_required`, `block_non_compliant` |
| `media_presets` | размеры/форматы под каждую площадку (проверять актуальные лимиты перед prod) |

---

## ✅ Тесты

```bash
pytest tests/ -p no:cacheprovider
```

Покрытие: scanner, queue, AI, content, media, drafts, publishers, scheduler, stats,
VibeCoding (generator/publisher/scheduler), API (FastAPI), валидация контента (Trip +
VibeCoding) и сквозной `e2e scan → content → approve → plan → publish`. Полный набор —
**80 passed**.

---

## ⚠️ Известные ограничения

- **Видео (Phase 6)** — отложено как manual/опциональное. `moviepy` 2.2.1 / разрешение
  ffmpeg-исполняемого файла при инициализации требует сетевого вызова и зависает на этой
  машине (нет Xcode CLT). Нужно только для Instagram/YouTube (и так manual-assisted).
  Ядро медиа (обработка изображений) работает.
- **`reverse_geocoder`** — опционален: если модуль не установлен, город определяется по
  имени папки/файла (fallback).
- **Валидация видео** — проверки `video_min/max_duration_sec` помечены как «не применимо»:
  в текущих моделях Draft/VibeCodingPost нет видео-полей, поэтому длительность честно
  не проверяется (без фейка прохождения).
- **VibeCoding-картинки** — для реальной генерации (не в `dry_run`) нужен ключ выбранного
  провайдера в `.env`. В mock-режиме — подстановочная картинка (Pillow).
- **Машина без Xcode Command Line Tools** — нативные сборки (`cryptography`,
  `scikit-learn`, `go`/`native` для brew) не собираются. Всё в требованиях собрано так,
  чтобы ставиться без них.

---

## 🔐 Политика секретов

- Только `.env`; `.env` в git не попадает (в `.gitignore`).
- Никаких реальных токенов в репозитории, `config.yaml`, коде, логах.
- `.env.example` — только шаблон с пустыми значениями.
- Перед каждым коммитом — автоматический скан на утечки (`credential-scan`).

---

<div align="center">
<b>Логика, архитектура и решения по каждой фазе</b> — в <a href="ARCHITECTURE.md">ARCHITECTURE.md</a><br>
<b>Текущий статус фаз</b> — в <a href=".planning/STATE.md">.planning/STATE.md</a><br>
<b>Ревью и план улучшений</b> — в <a href="docs/architecture-review-2026-09-04.md">docs/architecture-review-2026-09-04.md</a>
</div>

# ТЕХНИЧЕСКОЕ ЗАДАНИЕ: Сервис транскрибации и диаризации звонков

**Версия:** 2.0 (финальная)
**Дата:** 22 июня 2026 г.
**Целевое железо:** NVIDIA Tesla T4 (16 GB VRAM)

---

## 1. Назначение сервиса

Сервис принимает аудиофайл звонка и выполняет:

1. Распознавание речи (ASR) с покадровыми таймстемпами слов.
2. Диаризацию (разделение на спикеров).
3. Сохранение результата в БД в виде JSON.
4. Выдачу результата по `task_id` через polling или webhook-уведомление.
5. Автоматическое удаление записей старше 24 часов.

Сервис **не хранит** исходные аудиофайлы — только итоговый JSON-транскрипт.

---

## 2. Технологический стек (актуально на июнь 2026)

| Слой                  | Технология                       | Версия           |
| --------------------- | -------------------------------- | ---------------- |
| Язык                  | Python                           | 3.12             |
| Web                   | FastAPI + Pydantic v2            | 0.115+           |
| ASR                   | WhisperX (OpenAI large-v3-turbo) | актуальная       |
| Диаризация            | pyannote.audio                   | 3.1+             |
| ML-фреймворк          | PyTorch                          | 2.5+ (CUDA 12.6) |
| Очередь задач         | arq (async, на базе Redis)       | 0.11+            |
| БД                    | PostgreSQL                       | 16               |
| Кэш/Broker            | Redis                            | 7.2+             |
| ORM                   | SQLAlchemy 2.0 + Alembic         | —                |
| HTTP-клиент (webhook) | httpx                            | 0.27+            |
| Пакетный менеджер     | uv                               | актуальная       |
| Контейнеризация       | Docker + docker-compose          | —                |

---

## 3. Аппаратные ограничения и стратегия использования VRAM

**GPU:** NVIDIA Tesla T4, 16 GB VRAM, compute capability 7.5 (Turing).

> ⚠️ **Важно:** Tesla T4 не поддерживает Flash Attention 2 (требует sm_80+). PyTorch автоматически использует `math` backend для `scaled_dot_product_attention`. Это учтено в расчёте VRAM.

### 3.1. Стратегия (модели в VRAM одновременно)

| Компонент                          | Тип данных                   | VRAM         |
| ---------------------------------- | ---------------------------- | ------------ |
| WhisperX `large-v3-turbo`          | **int8** (через CTranslate2) | ~3.5 ГБ      |
| pyannote `speaker-diarization-3.1` | FP16                         | ~3 ГБ        |
| Wav2Vec2 alignment model           | FP16                         | ~1.5 ГБ      |
| CUDA context + ОС                  | —                            | ~2 ГБ        |
| **Итого пик**                      |                              | **~10 ГБ**   |
| **Запас**                          |                              | **~6 ГБ** ✅ |

### 3.2. Почему именно такая конфигурация

- **WhisperX в int8:** на T4 даёт прирост скорости 30–50% и экономит ~50% VRAM. T4 поддерживает INT8 Tensor Cores (sm_75).
- **pyannote в FP16:** модель небольшая (~20M параметров), INT8-квантизация на T4 не даёт выигрыша и часто замедляет работу из-за overhead.
- **Без Flash Attention:** корректно для T4, учтено в расчётах.

### 3.3. Обязательные оптимизации

- Все модели загружаются **один раз при старте Worker'а** и живут в VRAM всё время (singleton-паттерн).
- `compute_type="int8"` в faster-whisper (бэкенд WhisperX).
- `torch_dtype=torch.float16` для pyannote и alignment-модели.
- `batch_size` в WhisperX до 32 (int8 позволяет больше).

---

## 4. Архитектура и контейнеризация

### 4.1. Структура сервисов

Один Docker-образ, два сервиса в `docker-compose.yml`:

```yaml
services:
  api:
    build: .
    command: uvicorn app.api.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [postgres, redis]

  worker:
    build: .
    command: python -m arq app.worker.main.WorkerSettings
    env_file: .env
    depends_on: [postgres, redis]
    tmpfs:
      - /tmp/transcripts:size=2G,noexec
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: transcript
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes: ["pgdata:/var/lib/postgresql/data"]

  redis:
    image: redis:7-alpine
    volumes: ["redisdata:/data"]
```

> API-сервер не требует GPU — он только принимает запросы и кладёт задачи в очередь. GPU нужен только Worker'у.

### 4.2. Требования к Dockerfile

- Базовый образ: `nvidia/cuda:12.6.1-cudnn-runtime-ubuntu22.04`.
- Multi-stage build:
  - **Stage 1 (builder):** установка `gcc`, `cmake`, компиляция зависимостей, установка моделей через `huggingface-cli download` в `/models`.
  - **Stage 2 (runtime):** только runtime-пакеты, `ffmpeg`, `sox`, скопированные `/models` и Python-зависимости.
- Модели **вшиваются в образ** (в `/models`), чтобы избежать скачивания при каждом старте и гарантировать воспроизводимость.
- Переменные: `HF_HOME=/models`, `TRANSFORMERS_CACHE=/models`.
- Итоговый размер образа: **~8–10 ГБ** (с моделями).

### 4.3. Структура проекта

```
app/
├── api/
│   ├── main.py            # FastAPI app, lifespan
│   ├── routes.py          # POST /transcribe, GET /tasks/{id}, GET /health
│   ├── auth.py            # Bearer-аутентификация
│   └── schemas.py         # Pydantic-схемы
├── worker/
│   ├── main.py            # arq WorkerSettings, startup/shutdown
│   ├── tasks.py           # transcribe_task, deliver_webhook
│   ├── pipeline.py        # singleton: загрузка моделей в VRAM
│   └── webhook.py         # логика отправки webhook'ов
├── core/
│   ├── config.py          # pydantic-settings
│   ├── db.py              # async SQLAlchemy engine
│   ├── redis.py           # arq + aioredis
│   └── models.py          # таблицы tasks, api_tokens
├── cli/
│   └── __main__.py        # CLI для админа
└── migrations/            # Alembic
```

---

## 5. API

### 5.1. Аутентификация

- Заголовок: `Authorization: Bearer <token>`.
- Токен — случайная строка (`secrets.token_urlsafe(48)`).
- В БД хранится **только SHA-256 хэш** токена (защита от утечки БД).
- Проверка: хэшируем входящий токен → ищем в БД → проверяем `is_active` и `expires_at`.
- Защита от timing-атак: сравнение хэшей через `hmac.compare_digest`.

### 5.2. Эндпоинты

#### `POST /api/v1/transcribe`

- **Content-Type:** `multipart/form-data`
- **Параметры:**
  - `file` (binary): wav / mp3 / ogg / flac.
  - `call_id` (string): внешний ID звонка.
  - `webhook_url` (string, **опционально**): URL для уведомления о завершении. Только `https://`.
- **Ограничения:**
  - Макс. размер файла: **300 МБ**.
  - Макс. длительность: **180 минут (3 часа)**.
- **Ответ 202:**
  ```json
  {
    "task_id": "uuid",
    "call_id": "...",
    "status": "queued",
    "webhook_enabled": true
  }
  ```
- **Ошибки:** 400 (битый файл, неверный формат), 401 (неверный токен), 413 (превышен размер/длительность).

#### `GET /api/v1/tasks/{task_id}`

- **Ответ 200 (задача выполнена):**
  ```json
  {
    "task_id": "uuid",
    "call_id": "...",
    "status": "completed",
    "completed_at": "2026-06-22T14:30:00Z",
    "webhook_status": "delivered",
    "transcript": { ... }
  }
  ```
- **Ответ 200 (в процессе):**
  ```json
  { "task_id": "...", "status": "processing", "progress": 45 }
  ```
- **Ответ 200 (в очереди):** `{ "status": "queued" }`
- **Ответ 200 (ошибка):**
  ```json
  {
    "status": "failed",
    "error_code": "OOM_ERROR",
    "error_message": "GPU out of memory during alignment stage"
  }
  ```
- **Ошибки HTTP:** 401 (нет токена), 404 (задача не найдена или удалена по TTL).

#### `POST /api/v1/tasks/{task_id}/webhook/retry`

- **Описание:** Повторно отправить webhook для завершённой задачи.
- **Ответ 202:**
  ```json
  { "task_id": "uuid", "webhook_status": "pending" }
  ```
- **Ошибки:** 400 (у задачи не был указан webhook_url), 404 (задача не найдена).

#### `GET /api/v1/health`

- Без авторизации.
- Возвращает статус: `postgres`, `redis`, `gpu_available`, `models_loaded`, `gpu_memory_used_mb`.

### 5.3. Формат итогового JSON (транскрипт)

```json
{
  "language": "ru",
  "duration": 125.3,
  "speakers": ["SPEAKER_00", "SPEAKER_01"],
  "segments": [
    {
      "start": 0.0,
      "end": 3.5,
      "text": "Алло, здравствуйте.",
      "speaker": "SPEAKER_00",
      "words": [
        {
          "word": "Алло",
          "start": 0.1,
          "end": 0.5,
          "score": 0.95,
          "speaker": "SPEAKER_00"
        }
      ]
    }
  ]
}
```

**Особенности:**

- Фиксированный язык: `ru` (передаётся в WhisperX, чтобы не тратить время на lang detection).
- Если pyannote не смог выполнить диаризацию (например, аудио < 3 сек), поле `speaker` устанавливается в `"UNKNOWN"`, но транскрипт всё равно возвращается (graceful degradation).

---

## 6. Worker и очередь задач

### 6.1. Очередь

- Broker: Redis.
- Библиотека: `arq` (асинхронная, легковесная).
- Один воркер — одна задача в работе (GPU-ресурс делим).
- Retry: 2 попытки при транзиентных ошибках (например, сбой ffmpeg). OOM — без retry.
- Таймаут обработки задачи: **45 минут** (с запасом на 3 часа аудио в int8 на T4).

### 6.2. Пайплайн обработки (в `pipeline.py`)

**При старте Worker'а (в `startup` хуке arq):**

1. Загружается `whisperx` с моделью `large-v3-turbo` (int8) → в VRAM.
2. Загружается `pyannote` speaker-diarization (FP16) → в VRAM.
3. Загружается модель выравнивания (Wav2Vec2 для русского, FP16) → в VRAM.
4. Все три объекта сохраняются как синглтоны в модуле.

**При обработке задачи:**

1. Создание временной директории: `/tmp/transcripts/{task_id}/`.
2. Сохранение загруженного файла в `input.{ext}`.
3. `ffprobe` → проверка длительности (если > 180 мин → `TOO_LONG`).
4. WhisperX транскрибация (читает файл с диска).
5. pyannote диаризация (читает файл с диска).
6. Alignment и сборка JSON.
7. Запись JSON в PostgreSQL.
8. Если `task.webhook_url` не пуст — постановка задачи `deliver_webhook(task_id, attempt=1)` в arq.
9. **В блоке `finally`** — рекурсивное удаление `/tmp/transcripts/{task_id}/` (независимо от успеха/ошибки).

### 6.3. Обработка ошибок

| Код ошибки           | Ситуация                                               |
| -------------------- | ------------------------------------------------------ |
| `INVALID_AUDIO`      | Файл не читается / битый                               |
| `TOO_LONG`           | Длительность > 180 мин                                 |
| `TOO_LARGE`          | Размер > 300 МБ                                        |
| `OOM_ERROR`          | GPU out of memory                                      |
| `DIARIZATION_FAILED` | pyannote упал, но ASR успешен → сохраняем без спикеров |
| `UNKNOWN_ERROR`      | Всё остальное, с трейсбеком в `error_message`          |

---

## 7. База данных

### 7.1. Таблица `tasks`

```sql
CREATE TABLE tasks (
    id UUID PRIMARY KEY,
    call_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','processing','completed','failed')),
    progress SMALLINT,
    error_code TEXT,
    error_message TEXT,
    transcript_data JSONB,
    webhook_url TEXT,
    webhook_status TEXT CHECK (webhook_status IN ('pending', 'delivered', 'failed', null)),
    webhook_attempts SMALLINT DEFAULT 0,
    webhook_last_error TEXT,
    token_hash BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_tasks_status_completed ON tasks(status, completed_at)
    WHERE status = 'completed';
```

### 7.2. Таблица `api_tokens`

```sql
CREATE TABLE api_tokens (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash BYTEA NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
```

### 7.3. Очистка по TTL (24 часа)

Фоновая задача в Worker'е (через `arq` cron или `asyncio`-цикл):

```sql
DELETE FROM tasks
WHERE status IN ('completed', 'failed')
  AND COALESCE(completed_at, created_at) < NOW() - INTERVAL '24 hours';
```

Запускается **каждые 15 минут**.

---

## 8. Webhook-уведомления

### 8.1. Общие принципы

- Webhook — **опциональный** канал. Polling продолжает работать всегда.
- Подпись webhook'ов: **HMAC-SHA256**, ключ — тот же Bearer-токен клиента.
- Доставка через отдельную очередь arq (`deliver_webhook`), чтобы не блокировать GPU-воркер.
- Независимость от статуса задачи: задача считается `completed` сразу после сохранения JSON в БД. Если webhook не дошёл — это не откатывает статус.

### 8.2. Формат payload

**При успешном завершении (`status: completed`):**

```json
{
  "event": "task.completed",
  "task_id": "uuid",
  "call_id": "external_123",
  "completed_at": "2026-06-22T14:30:00Z",
  "transcript": { ... }
}
```

**При ошибке (`status: failed`):**

```json
{
  "event": "task.failed",
  "task_id": "uuid",
  "call_id": "external_123",
  "failed_at": "2026-06-22T14:30:00Z",
  "error_code": "OOM_ERROR",
  "error_message": "GPU out of memory"
}
```

### 8.3. Подпись (HMAC-SHA256)

Заголовок: `X-Webhook-Signature: sha256=<hex>`

**Алгоритм подписи:**

```
signature = HMAC-SHA256(
    key = <Bearer-токен клиента в исходном виде>,
    message = <тело запроса как UTF-8 строка без пробелов>
)
```

**Дополнительные заголовки:**

- `X-Webhook-Task-Id`: UUID задачи.
- `User-Agent: TranscriptionService-Webhook/1.0`.

**На стороне клиента (Python-пример для проверки):**

```python
import hmac, hashlib

def verify_webhook(payload_bytes: bytes, signature: str, my_token: str) -> bool:
    expected = hmac.new(
        my_token.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

### 8.4. Retry-политика

| Попытка | Задержка | Комментарий                   |
| ------- | -------- | ----------------------------- |
| 1       | сразу    | Сразу после завершения задачи |
| 2       | +30 сек  | Если первая не удалась        |
| 3       | +120 сек | Финальная попытка             |

После 3 неудач:

- `webhook_status = 'failed'`
- `webhook_last_error = "<текст ошибки>"`
- Задача всё равно остаётся `completed` (если транскрипт сохранён).
- Клиент может узнать статус доставки через `GET /tasks/{id}` (поле `webhook_status`).
- Клиент может запросить повторную отправку через `POST /tasks/{id}/webhook/retry`.

### 8.5. Требования к `webhook_url`

- Только `https://` (http запрещён для безопасности, можно разрешить через `.env` для тестов).
- Валидация при приёме запроса (через `pydantic.AnyUrl`).
- Таймаут ответа сервера клиента: **10 секунд**.
- Ожидаемый HTTP-код: **2xx**. Всё остальное — retry.

### 8.6. Логирование webhook'ов

В логах (structlog) для каждого webhook'а:

```json
{
  "event": "webhook.delivered",
  "task_id": "...",
  "url": "https://...",
  "attempt": 1,
  "status_code": 200,
  "duration_ms": 245
}
```

---

## 9. CLI для администратора

Запуск: `docker compose exec api python -m app.cli <command>`

### Команды

```bash
# Создать токен (бессрочный)
python -m app.cli create_token --name "integration_1"

# Создать токен с TTL
python -m app.cli create_token --name "temp_test" --expires-in 7d

# Список всех токенов (без самих значений — только хэши и метаданные)
python -m app.cli list_tokens

# Отозвать токен по имени
python -m app.cli revoke_token --name "integration_1"

# Отозвать все токены
python -m app.cli revoke_all
```

**Важно:** значение токена выводится в stdout **только один раз** при создании. Админ должен его сохранить.

---

## 10. Переменные окружения (.env)

```env
# DB
DB_HOST=postgres
DB_PORT=5432
DB_NAME=transcript
DB_USER=app
DB_PASSWORD=***

# Redis
REDIS_URL=redis://redis:6379/0

# HuggingFace (ОБЯЗАТЕЛЬНО для pyannote)
HF_TOKEN=hf_***

# Модели
WHISPER_MODEL=large-v3-turbo
WHISPER_LANGUAGE=ru
WHISPER_COMPUTE_TYPE=int8
WHISPER_BATCH_SIZE=32
DIARIZATION_MODEL=pyannote/speaker-diarization-3.1
MIN_SPEAKERS=2
MAX_SPEAKERS=4

# Лимиты
MAX_FILE_SIZE_MB=300
MAX_DURATION_MIN=180
TASK_TIMEOUT_SECONDS=2700
TTL_HOURS=24
TMP_DIR=/tmp/transcripts

# Webhook
WEBHOOK_TIMEOUT_SECONDS=10
WEBHOOK_MAX_ATTEMPTS=3
WEBHOOK_BACKOFF_SECONDS=5,30,120
WEBHOOK_ALLOW_HTTP=false

# Логирование
LOG_LEVEL=INFO
```

---

## 11. OpenAPI 3.1 Спецификация

FastAPI генерирует её автоматически по адресу `/openapi.json`, но ниже — каноническая спецификация для документации и клиентов.

```yaml
openapi: 3.1.0
info:
  title: Transcription & Diarization Service
  version: 2.0.0
  description: |
    Сервис транскрибации и диаризации звонков на базе WhisperX + pyannote.
    Все эндпоинты (кроме /health) требуют Bearer-токен.
    Поддерживает polling и webhook-уведомления.
  contact:
    name: Admin
servers:
  - url: /api/v1
    description: Production

security:
  - bearerAuth: []

tags:
  - name: Transcription
    description: Операции с транскрибацией
  - name: Tasks
    description: Получение статусов и результатов
  - name: Webhooks
    description: Управление webhook-уведомлениями
  - name: System
    description: Служебные эндпоинты

paths:
  /health:
    get:
      tags: [System]
      summary: Проверка работоспособности
      security: []
      responses:
        "200":
          description: Сервис жив
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/HealthResponse"

  /transcribe:
    post:
      tags: [Transcription]
      summary: Поставить задачу транскрибации в очередь
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema:
              type: object
              required: [file, call_id]
              properties:
                file:
                  type: string
                  format: binary
                  description: Аудиофайл (wav, mp3, ogg, flac). Макс. 300 МБ.
                call_id:
                  type: string
                  description: Внешний идентификатор звонка
                  example: "call_2026_06_22_001"
                webhook_url:
                  type: string
                  format: uri
                  pattern: "^https://.*"
                  description: |
                    Опционально. URL для уведомления о завершении задачи.
                    Только HTTPS. Подписывается HMAC-SHA256 с ключом = Bearer-токен.
                  example: "https://api.client.com/webhooks/transcription"
      responses:
        "202":
          description: Задача принята в очередь
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/TaskAccepted"
        "400":
          description: Некорректный файл или call_id
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Error"
        "401":
          $ref: "#/components/responses/Unauthorized"
        "413":
          description: Файл слишком большой или длительность > 180 мин
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Error"

  /tasks/{task_id}:
    get:
      tags: [Tasks]
      summary: Получить статус и результат задачи
      parameters:
        - name: task_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        "200":
          description: Статус задачи (queued / processing / completed / failed)
          content:
            application/json:
              schema:
                oneOf:
                  - $ref: "#/components/schemas/TaskQueued"
                  - $ref: "#/components/schemas/TaskProcessing"
                  - $ref: "#/components/schemas/TaskCompleted"
                  - $ref: "#/components/schemas/TaskFailed"
        "401":
          $ref: "#/components/responses/Unauthorized"
        "404":
          description: Задача не найдена или удалена по TTL
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Error"

  /tasks/{task_id}/webhook/retry:
    post:
      tags: [Webhooks]
      summary: Повторно отправить webhook для завершённой задачи
      description: |
        Полезно, если клиент не получил уведомление и хочет запросить повторную отправку
        без пересоздания задачи. Работает только для задач со статусом completed/failed.
      parameters:
        - name: task_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        "202":
          description: Задача на повторную отправку поставлена в очередь
          content:
            application/json:
              schema:
                type: object
                properties:
                  task_id: { type: string, format: uuid }
                  webhook_status: { type: string, enum: [pending] }
        "400":
          description: У задачи не был указан webhook_url
        "404":
          description: Задача не найдена

components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      description: Токен, полученный через CLI `python -m app.cli create_token`

  responses:
    Unauthorized:
      description: Отсутствует или неверен Bearer-токен
      content:
        application/json:
          schema:
            $ref: "#/components/schemas/Error"
          example:
            error_code: UNAUTHORIZED
            error_message: Invalid or expired bearer token

  schemas:
    Error:
      type: object
      required: [error_code, error_message]
      properties:
        error_code:
          type: string
          enum:
            - UNAUTHORIZED
            - INVALID_AUDIO
            - TOO_LONG
            - TOO_LARGE
            - NOT_FOUND
            - OOM_ERROR
            - DIARIZATION_FAILED
            - UNKNOWN_ERROR
        error_message:
          type: string

    HealthResponse:
      type: object
      properties:
        status:
          type: string
          enum: [ok, degraded]
        postgres:
          type: boolean
        redis:
          type: boolean
        gpu_available:
          type: boolean
        models_loaded:
          type: boolean
        gpu_memory_used_mb:
          type: integer

    TaskAccepted:
      type: object
      required: [task_id, call_id, status]
      properties:
        task_id:
          type: string
          format: uuid
        call_id:
          type: string
        status:
          type: string
          enum: [queued]
        webhook_enabled:
          type: boolean

    TaskQueued:
      type: object
      properties:
        task_id:
          type: string
          format: uuid
        call_id:
          type: string
        status:
          type: string
          enum: [queued]
        created_at:
          type: string
          format: date-time

    TaskProcessing:
      type: object
      properties:
        task_id:
          type: string
          format: uuid
        call_id:
          type: string
        status:
          type: string
          enum: [processing]
        progress:
          type: integer
          minimum: 0
          maximum: 100
          description: Прогресс в процентах

    TaskCompleted:
      type: object
      required: [task_id, call_id, status, completed_at, transcript]
      properties:
        task_id:
          type: string
          format: uuid
        call_id:
          type: string
        status:
          type: string
          enum: [completed]
        completed_at:
          type: string
          format: date-time
        webhook_status:
          type: string
          enum: [pending, delivered, failed, null]
          description: Статус доставки webhook'а (если webhook_url был указан)
        webhook_attempts:
          type: integer
          minimum: 0
          maximum: 3
        transcript:
          $ref: "#/components/schemas/Transcript"

    TaskFailed:
      type: object
      required: [task_id, call_id, status, error_code, error_message]
      properties:
        task_id:
          type: string
          format: uuid
        call_id:
          type: string
        status:
          type: string
          enum: [failed]
        webhook_status:
          type: string
          enum: [pending, delivered, failed, null]
        error_code:
          type: string
        error_message:
          type: string

    Transcript:
      type: object
      required: [language, duration, speakers, segments]
      properties:
        language:
          type: string
          enum: [ru]
        duration:
          type: number
          format: float
          description: Длительность аудио в секундах
        speakers:
          type: array
          items:
            type: string
          description: Список идентификаторов спикеров
        segments:
          type: array
          items:
            $ref: "#/components/schemas/Segment"

    Segment:
      type: object
      required: [start, end, text, speaker]
      properties:
        start:
          type: number
          format: float
        end:
          type: number
          format: float
        text:
          type: string
        speaker:
          type: string
          description: Идентификатор спикера или "UNKNOWN"
        words:
          type: array
          items:
            $ref: "#/components/schemas/Word"

    Word:
      type: object
      required: [word, start, end, score, speaker]
      properties:
        word:
          type: string
        start:
          type: number
          format: float
        end:
          type: number
          format: float
        score:
          type: number
          format: float
          minimum: 0
          maximum: 1
        speaker:
          type: string
```

### Использование спецификации

1. **Автоматически:** FastAPI отдаёт её на `GET /openapi.json`, Swagger UI на `/docs`, ReDoc на `/redoc`.
2. **Для клиентов:** генерация через `openapi-generator-cli`:
   ```bash
   openapi-generator-cli generate -i openapi.yaml -g python -o ./client
   ```
3. **Валидация:** `npx @redocly/cli lint openapi.yaml`.

---

## 12. Нефункциональные требования

- **Логирование:** структурированное (JSON), через `structlog`. В логах — `task_id`, `call_id`, длительность обработки, пиковый VRAM.
- **Безопасность:** токены в логах **никогда** не выводятся (фильтр в structlog).
- **Graceful shutdown:** при `SIGTERM` Worker завершает текущую задачу, но не берёт новые.
- **Метрики (опционально, на будущее):** Prometheus-экспортер с метриками `tasks_total`, `task_duration_seconds`, `gpu_memory_used_bytes`, `webhook_failures_total`.

---

## 13. Чек-лист перед первым запуском

1. ☐ Получить HF-токен на https://huggingface.co/settings/tokens.
2. ☐ Принять условия лицензий на моделях:
   - `pyannote/speaker-diarization-3.1`
   - `pyannote/segmentation-3.0`
3. ☐ Прописать `HF_TOKEN` в `.env`.
4. ☐ Убедиться, что на хосте установлен `nvidia-container-toolkit` (иначе GPU не пробросится в контейнер).
5. ☐ Выполнить `docker compose build` (первая сборка ~20–30 минут из-за моделей).
6. ☐ Выполнить `docker compose up -d`.
7. ☐ Применить миграции: `docker compose exec api alembic upgrade head`.
8. ☐ Создать первый токен: `docker compose exec api python -m app.cli create_token --name "admin"`.
9. ☐ Проверить `curl http://localhost:8000/api/v1/health` — убедиться, что `gpu_available: true`.
10. ☐ **Тестовый прогон:** отправить 10-минутный файл → убедиться, что `/tmp/transcripts/` очищается после обработки (`docker compose exec worker ls /tmp/transcripts` должно быть пусто).
11. ☐ **Тест webhook'ов:** запустить локальный HTTP-сервер (`python -m http.server 9000` или ngrok), отправить задачу с `webhook_url=http://localhost:9000/webhook` (в тестовом режиме разрешить HTTP через `WEBHOOK_ALLOW_HTTP=true`), убедиться, что пришёл POST с правильной подписью.
12. ☐ **Тест retry:** указать несуществующий URL, дождаться 3 попыток, проверить `webhook_status = 'failed'` через `GET /tasks/{id}`.
13. ☐ **Тест повторной отправки:** вызвать `POST /tasks/{id}/webhook/retry` для задачи с упавшим webhook'ом — убедиться, что статус сбросился в `pending` и пришла новая попытка.

---

## 14. Стратегии использования клиента

Клиент может выбрать один из трёх сценариев:

| Стратегия          | Поведение                                                                                                         |
| ------------------ | ----------------------------------------------------------------------------------------------------------------- |
| **Только polling** | Не передаёт `webhook_url`, дёргает `GET /tasks/{id}` с интервалом 5–10 сек.                                       |
| **Только webhook** | Передаёт `webhook_url`, получает уведомление, polling не делает.                                                  |
| **Гибридная**      | Передаёт `webhook_url` как основной канал, но на всякий случай поллит раз в минуту (если webhook вдруг не дошёл). |

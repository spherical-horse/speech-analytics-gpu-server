# Speech Analytics GPU Server

Сервис транскрибации и диаризации звонков на базе **WhisperX** + **pyannote.audio**.

- Распознавание речи с таймстемпами слов (WhisperX large-v3-turbo, int8)
- Разделение по спикерам (pyannote speaker-diarization-3.1, FP16)
- Результат — JSON-транскрипт, доступен по polling или webhook
- Целевое железо: NVIDIA Tesla T4 (16 GB VRAM)

---

## Требования

- Docker + Docker Compose с поддержкой GPU (`nvidia-container-toolkit`)
- NVIDIA GPU с CUDA 12.6+
- Аккаунт на [HuggingFace](https://huggingface.co) с принятыми лицензиями моделей (см. ниже)

---

## Первый запуск

### 1. HuggingFace — токен и лицензии

Создайте токен на https://huggingface.co/settings/tokens (тип **Read**).

Примите лицензии на обе модели (требуется вход в аккаунт):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

### 2. Конфигурация

```bash
cp .env.example .env
```

Откройте `.env` и заполните минимум:

```env
DB_PASSWORD=ваш_пароль
HF_TOKEN=hf_ваш_токен
```

### 3. Сборка образа

Первая сборка занимает 30–60 минут — скачиваются ML-модели (~8 ГБ) и зависимости.

```bash
docker compose build --build-arg HF_TOKEN=hf_ваш_токен
```

### 4. Запуск

```bash
docker compose up -d
```

### 5. Миграции БД

```bash
docker compose exec api alembic upgrade head
```

### 6. Создание первого токена API

```bash
docker compose exec api python -m app.cli create_token --name admin
```

Токен выводится **один раз** — сохраните его.

### 7. Проверка

```bash
curl http://localhost:8000/api/v1/health
```

Ожидаемый ответ:

```json
{
  "status": "ok",
  "postgres": true,
  "redis": true,
  "gpu_available": true,
  "models_loaded": true,
  "gpu_memory_used_mb": 8192
}
```

---

## Расшифровка тестового звонка

### Отправить файл на транскрибацию

```bash
TOKEN="ваш_токен_из_create_token"

curl -X POST http://localhost:8000/api/v1/transcribe \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/путь/к/звонку.wav" \
  -F "call_id=test_call_001"
```

Ответ:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "call_id": "test_call_001",
  "status": "queued",
  "webhook_enabled": false
}
```

Сохраните `task_id`.

### Опросить статус

```bash
TASK_ID="550e8400-e29b-41d4-a716-446655440000"

curl http://localhost:8000/api/v1/tasks/$TASK_ID \
  -H "Authorization: Bearer $TOKEN"
```

Пока обрабатывается — в ответе будет `"status": "processing"` и поле `progress` (0–100).

После завершения — `"status": "completed"` и полный транскрипт:

```json
{
  "task_id": "...",
  "status": "completed",
  "transcript": {
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
          { "word": "Алло", "start": 0.1, "end": 0.5, "score": 0.95, "speaker": "SPEAKER_00" }
        ]
      }
    ]
  }
}
```

### Ориентировочное время обработки на T4

| Длительность звонка | Время обработки |
|---------------------|----------------|
| 5 минут             | ~1 мин         |
| 30 минут            | ~5 мин         |
| 1 час               | ~10 мин        |
| 3 часа (максимум)   | ~30 мин        |

### Транскрибация с webhook

Если хотите получить результат push-уведомлением вместо polling:

```bash
curl -X POST http://localhost:8000/api/v1/transcribe \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/путь/к/звонку.wav" \
  -F "call_id=test_call_002" \
  -F "webhook_url=https://ваш-сервер.ru/webhook"
```

Сервис пришлёт POST на указанный URL с транскриптом и заголовком `X-Webhook-Signature` для проверки подписи.

---

## Управление токенами (CLI)

```bash
# Создать токен (бессрочный)
docker compose exec api python -m app.cli create_token --name "integration_1"

# Создать токен с TTL
docker compose exec api python -m app.cli create_token --name "temp" --expires-in 7d

# Список токенов
docker compose exec api python -m app.cli list_tokens

# Отозвать токен
docker compose exec api python -m app.cli revoke_token --name "integration_1"
```

---

## Документация API

После запуска доступны:

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json

---

## Переменные окружения

Все настройки описаны в `.env.example`. Ключевые:

| Переменная | Описание | По умолчанию |
|---|---|---|
| `DB_PASSWORD` | Пароль PostgreSQL | — |
| `HF_TOKEN` | HuggingFace токен | — |
| `MAX_FILE_SIZE_MB` | Макс. размер файла | 300 |
| `MAX_DURATION_MIN` | Макс. длительность (мин) | 180 |
| `MIN_SPEAKERS` / `MAX_SPEAKERS` | Диапазон числа спикеров | 2 / 4 |
| `WEBHOOK_ALLOW_HTTP` | Разрешить http:// для webhook (тесты) | false |

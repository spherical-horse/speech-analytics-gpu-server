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
- Caddy (устанавливается автоматически скриптом деплоя)
- Аккаунт на [HuggingFace](https://huggingface.co) с принятыми лицензиями моделей (см. ниже)

---

## Деплой на чистый сервер

Скрипт `deploy.sh` автоматически устанавливает все зависимости, собирает образы и запускает сервис.

### Требования к серверу

- Ubuntu 22.04 LTS
- NVIDIA GPU (Tesla T4 или аналог)
- Открытые порты: **80**, **443**
- Минимум 20 ГБ свободного места (образ с моделями ~10 ГБ + запас)

### Подготовка HuggingFace

До запуска скрипта необходимо создать токен и принять лицензии моделей под своим аккаунтом:

1. Создайте токен (тип **Read**): https://huggingface.co/settings/tokens
2. Примите лицензию: https://huggingface.co/pyannote/speaker-diarization-3.1
3. Примите лицензию: https://huggingface.co/pyannote/segmentation-3.0

### Запуск

**Вариант 1 — с локальной машины** (рекомендуется):

```bash
bash deploy-remote.sh user@server-ip
# или с указанием ключа:
bash deploy-remote.sh user@server-ip -i ~/.ssh/id_rsa
```

Скрипт синхронизирует файлы через `rsync` и запускает деплой по SSH. Если на сервере установлен `tmux` — деплой идёт в tmux-сессии и переживёт обрыв соединения; переподключиться можно командой `tmux attach -t deploy`.

**Вариант 2 — с самого сервера**:

```bash
git clone <repo-url> /opt/speech-analytics
cd /opt/speech-analytics
sudo bash deploy.sh
```

Скрипт в интерактивном режиме спросит:

| Параметр | Описание |
|---|---|
| Домен | Например `api.example.com`. Caddy автоматически получит TLS-сертификат. |
| HF_TOKEN | Токен HuggingFace (начинается с `hf_`). |
| DB_PASSWORD | Пароль PostgreSQL. Если пропустить — сгенерируется автоматически. |

Первая сборка занимает **30–60 минут** — скачиваются ML-модели (~8 ГБ).

По завершении скрипт выведет токен API — сохраните его, он показывается один раз.

### Что делает скрипт

1. Устанавливает Docker CE
2. Проверяет NVIDIA-драйвер; если отсутствует — устанавливает через `ubuntu-drivers` и просит перезагрузиться
3. Устанавливает `nvidia-container-toolkit`
4. Устанавливает Caddy как системный сервис (через официальный apt-репозиторий)
5. Создаёт `.env` из `.env.example`
6. Настраивает и запускает Caddy (пишет `/etc/caddy/Caddyfile`, включает в systemd)
7. Собирает Docker-образы со встроенными ML-моделями
8. Запускает контейнеры (`api`, `worker`, `postgres`, `redis`)
9. Применяет Alembic-миграции
10. Создаёт первый токен API с именем `admin`

### Повторный запуск / обновление

Скрипт идемпотентен — безопасно запускать повторно. Если `.env` уже существует, предложит использовать его без повторного опроса.

```bash
# Обновить код и пересобрать
git pull
sudo bash deploy.sh
```

---

## Первый запуск (вручную)

### 1. HuggingFace — токен и лицензии

Создайте токен на https://huggingface.co/settings/tokens (тип **Read**).

Примите лицензии на обе модели (требуется вход в аккаунт):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

### 2. Установка Caddy

```bash
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy
```

### 3. Конфигурация

```bash
cp .env.example .env
```

Откройте `.env` и заполните минимум:

```env
DB_PASSWORD=ваш_пароль
HF_TOKEN=hf_ваш_токен
```

Настройте `/etc/caddy/Caddyfile`:

```
api.example.com {
    reverse_proxy localhost:8000
}
```

Затем запустите Caddy:

```bash
systemctl enable --now caddy
```

### 4. Сборка образа

Первая сборка занимает 30–60 минут — скачиваются ML-модели (~8 ГБ) и зависимости.

```bash
docker compose build --build-arg HF_TOKEN=hf_ваш_токен
```

### 5. Запуск

```bash
docker compose up -d
```

### 6. Миграции БД

```bash
docker compose exec api alembic upgrade head
```

### 7. Создание первого токена API

```bash
docker compose exec api python -m app.cli create_token --name admin
```

Токен выводится **один раз** — сохраните его.

### 8. Проверка

```bash
curl https://localhost/api/v1/health --insecure
```

> `--insecure` нужен только если Caddy настроен на `localhost` (self-signed сертификат).
> На реальном домене флаг не нужен.

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

curl -X POST https://localhost/api/v1/transcribe \
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

curl https://localhost/api/v1/tasks/$TASK_ID \
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
curl -X POST https://localhost/api/v1/transcribe \
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

- Swagger UI: `https://ваш-домен/docs`
- ReDoc: `https://ваш-домен/redoc`
- OpenAPI JSON: `https://ваш-домен/openapi.json`

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

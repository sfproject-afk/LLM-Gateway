# LLM-Gateway

Лёгкий **reverse proxy** для нескольких vLLM-бэкендов с токен-аутентификацией, маршрутизацией по модели, управлением thinking-режимом и проксированием image backend.

Ядро написано на чистом Python: только стандартная библиотека, без обязательных `pip`-зависимостей.

**English:** Lightweight reverse proxy for multiple vLLM backends with Bearer-token authentication, model-based routing, thinking-mode control, SSE streaming support, and optional image-backend proxying.

---

## Возможности

- **Multi-token Bearer auth** — несколько токенов с лейблами клиентов
- **Маршрутизация по модели** — каждая модель идёт к своему vLLM-бэкенду (по `host:port`)
- **Переименование моделей** — внешние псевдонимы прозрачно переписываются во внутренние ID
- **Thinking-модели** — глобальный контроль фазы `<think>`: отключить / задать бюджет
- **Виртуальные варианты** — для мультимодальных моделей автоматически строятся `{model}-thinking` и `{model}-fast`
- **SSE стриминг** — корректный проксий Server-Sent Events с реал-тайм flush
- **Встраивание reasoning** — `reasoning_content` → `<think>…</think>` в `delta.content` (опционально)
- **Image backend** — проксирование запросов генерации изображений на отдельный сервис
- **Курируемый `/v1/models`** — опрашивает все бэкенды и отдаёт единый список видимых виртуальных моделей для UI

---

## Архитектура

```text
Client / Open WebUI / Agent
       |
       v
  LLM-Gateway (:8080)
    |        |        \
    |        |         \
    |        |          -> Image backend (:8091)
    |        |
    |        -> vLLM backend B (:8001)
    |
    -> vLLM backend A (:8000)
```

Основная логика:

1. Gateway принимает OpenAI-совместимый HTTP-запрос.
2. Проверяет Bearer-токен.
3. Извлекает `model` из JSON body.
4. Выбирает нужный backend по `VLLM_BACKENDS`.
5. При необходимости:
  - переписывает ID модели через `MODEL_REWRITES`
  - инжектирует `chat_template_kwargs` для thinking-моделей
  - проксирует image-запросы на отдельный image backend
6. Возвращает ответ клиенту, сохраняя SSE-стриминг.

---

## Структура репозитория

```text
.
├── model_gateway_v2.py     # основной HTTP proxy
├── model-gateway.service   # пример systemd unit
├── .env.example            # шаблон конфигурации
├── scripts/
│   └── smoke_test.sh       # быстрый smoke test для /v1/models и /v1/chat/completions
└── README.md
```

---

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/sfproject-afk/LLM-Gateway.git
cd LLM-Gateway

# 2. Скопировать и заполнить конфиг
cp .env.example .env

# 3. Запустить напрямую из .env
set -a
source .env
set +a
python3 model_gateway_v2.py
```

Или через systemd:

```bash
sudo cp model-gateway.service /etc/systemd/system/
# отредактировать пути в service-файле и создать .env рядом с проектом
sudo systemctl daemon-reload
sudo systemctl enable --now model-gateway
```

---

## Конфигурация

Все параметры задаются переменными окружения.

Рекомендуемый вариант для production: хранить их в `.env`, а systemd подключать через `EnvironmentFile=`.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `MODEL_GATEWAY_HOST` | `0.0.0.0` | Адрес прослушивания |
| `MODEL_GATEWAY_PORT` | `8080` | Порт |
| `MODEL_GATEWAY_TOKEN` | `""` | Одиночный Bearer-токен |
| `MODEL_GATEWAY_TOKENS` | `""` | Мультитокен: `tok1\|label1,tok2\|label2` |
| `VLLM_BACKENDS` | `""` | JSON: `{"model-id":"host:port", ...}` |
| `VLLM_PRIMARY` | первый из `VLLM_BACKENDS` | Fallback-бэкенд |
| `MODEL_REWRITES` | `""` | JSON: `{"alias":"internal-id"}` |
| `THINKING_MODELS` | `""` | Comma-separated имена thinking-моделей |
| `THINKING_BUDGET` | `-1` | `-1`=не управлять, `0`=отключить, `>0`=лимит токенов |
| `IMAGE_BACKEND_URL` | `""` | URL image-сервиса (напр. `http://127.0.0.1:8091`) |
| `IMAGE_BACKEND_TOKEN` | `""` | Bearer-токен для image-сервиса |

### Пример VLLM_BACKENDS

```json
{
  "my-model":      "127.0.0.1:8000",
  "vision-model":  "127.0.0.1:8001",
  "coder-model":   "127.0.0.1:8002",
  "fast-model":    "127.0.0.1:8003"
}
```

### Пример MODEL_REWRITES

```json
{
  "public-alias": "Actual-Model-File.gguf",
  "another-alias": "another-model-id"
}
```

---

## Маршрутизация запросов

```
GET /v1/models
  → опрашивает ВСЕ бэкенды
  → отбирает только видимые base-модели
  → публикует виртуальные варианты {model}-thinking / {model}-fast

POST /v1/chat/completions
  → читает поле "model" из JSON body
  → VLLM_BACKENDS[model] → нужный бэкенд
  → иначе → PRIMARY backend

POST /v1/images/generations
GET  /zimage/*
  → IMAGE_BACKEND_URL

все остальные пути
  → то же, что chat/completions (по model field, fallback → PRIMARY)
```

### Виртуальные варианты

Gateway автоматически опрашивает бэкенды при старте. Для моделей из `PREFERRED_VISIBLE_BASE_MODELS` (по умолчанию `qwen3.6-35b`, `gemma4-26b`) строятся два виртуальных ID, если такие модели реально присутствуют на backend'ах:

- `{model}-thinking` → включает `enable_thinking=true`
- `{model}-fast` → включает `enable_thinking=false`

---

## Управление thinking

Для моделей из `THINKING_MODELS` gateway инжектирует `chat_template_kwargs` в тело запроса:

| THINKING_BUDGET | Поведение |
|---|---|
| `-1` | нет инжекции — модель использует дефолт |
| `0` | `{"enable_thinking": false}` — отключить рассуждения |
| `N > 0` | `{"thinking_budget": N}` — ограничить токены на `<think>` |

Клиент может передать `chat_template_kwargs` самостоятельно — тогда инжекция не происходит.

---

## Встраивание reasoning в стриминг

По умолчанию `reasoning_content` передаётся клиенту как отдельное delta-поле (стандарт OpenAI/DeepSeek).

Для встраивания в `delta.content` как `<think>…</think>` используйте:

```http
X-Embed-Thinking: true
```

или в теле запроса:

```json
{"chat_embed_thinking": true, ...}
```

---

## Примеры запросов

### Простой текст

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-model",
    "messages": [{"role": "user", "content": "Привет!"}],
    "max_tokens": 200,
    "stream": false
  }'
```

### Стриминг с отключённым thinking

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-model",
    "messages": [{"role": "user", "content": "Расскажи о Python"}],
    "stream": true
  }'
```

### Список моделей

```bash
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Деплой через systemd

1. Клонируйте репозиторий в постоянную директорию, например `/opt/llm-gateway`.
2. Скопируйте `.env.example` в `.env` и заполните значения.
3. Откройте `model-gateway.service` и замените:
   - `YOUR_USER`
   - `/path/to/model-gateway`
4. Установите unit:

```bash
sudo cp model-gateway.service /etc/systemd/system/model-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now model-gateway
sudo systemctl status model-gateway --no-pager
```

Если менялся `.env` или unit:

```bash
sudo systemctl daemon-reload
sudo systemctl restart model-gateway
```

---

## Smoke tests

### Готовый скрипт

```bash
chmod +x scripts/smoke_test.sh
TOKEN=YOUR_TOKEN MODEL=my-model ./scripts/smoke_test.sh
```

### Ручная проверка `/v1/models`

```bash
curl -fsS http://127.0.0.1:8080/v1/models \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Ручная проверка `/v1/chat/completions`

```bash
curl -fsS http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-model",
    "messages": [{"role": "user", "content": "Ответь одним словом: pong"}],
    "max_tokens": 32,
    "stream": false
  }'
```

---

## Операционные замечания

- У gateway **нет отдельного `/health` endpoint** для chat proxy; для проверки доступности используйте `/v1/models` или тестовый `chat/completions`.
- Маршрут `/zimage/health` работает только если настроен `IMAGE_BACKEND_URL`.
- Если `MODEL_GATEWAY_TOKENS` задан, он имеет приоритет над `MODEL_GATEWAY_TOKEN`.
- `/v1/models` публикует не «все сырые backend ID», а отфильтрованный список видимых виртуальных моделей для UI.

---

## Auth

Если ни `MODEL_GATEWAY_TOKEN`, ни `MODEL_GATEWAY_TOKENS` не заданы — auth отключена (опасно для публичного сервера).

При задании `MODEL_GATEWAY_TOKENS` — одиночный `MODEL_GATEWAY_TOKEN` игнорируется.

Каждый запрос должен содержать заголовок:

```http
Authorization: Bearer <token>
```

---

## Добавление новой модели

1. Запустить vLLM-контейнер с нужной моделью на свободном порту
2. Добавить запись в `VLLM_BACKENDS`
3. Если модель thinking — добавить имя в `THINKING_MODELS`
4. Если нужно переименование публичного ID — добавить в `MODEL_REWRITES`
5. Перезапустить gateway: `systemctl restart model-gateway`

---

## Зависимости

**Ядро прокси** (`model_gateway_v2.py`) — только стандартная библиотека Python 3.10+:
- `http.server`, `http.client`, `json`, `os`, `threading`, `urllib`

Никаких `pip install` для запуска не требуется.

---

## Лицензия

MIT

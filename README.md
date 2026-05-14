# model-gateway-v2

Лёгкий **reverse proxy** для нескольких vLLM-бэкендов с токен-аутентификацией, маршрутизацией по модели и управлением thinking-режимом.

Написан на чистом Python (только стандартная библиотека — никаких зависимостей для ядра прокси).

---

## Возможности

- **Multi-token Bearer auth** — несколько токенов с лейблами клиентов
- **Маршрутизация по модели** — каждая модель идёт к своему vLLM-бэкенду (по `host:port`)
- **Переименование моделей** — внешние псевдонимы прозрачно переписываются во внутренние ID
- **Thinking-модели** — глобальный контроль fase `<think>`: отключить / задать бюджет
- **Виртуальные варианты** — для мультимодальных моделей автоматически строятся `{model}-thinking` и `{model}-fast`
- **SSE стриминг** — корректный проксий Server-Sent Events с реал-тайм flush
- **Встраивание reasoning** — `reasoning_content` → `<think>…</think>` в `delta.content` (опционально)
- **Image backend** — проксирование запросов генерации изображений на отдельный сервис
- **Агрегация `/v1/models`** — опрашивает все бэкенды и отдаёт единый список

---

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/sfproject-afk/model-gateway-v2.git
cd model-gateway-v2

# 2. Скопировать и заполнить конфиг
cp .env.example .env

# 3. Запустить напрямую
MODEL_GATEWAY_TOKEN=mytoken \
VLLM_BACKENDS='{"my-model":"127.0.0.1:8000"}' \
python3 model_gateway_v2.py
```

Или через systemd:

```bash
sudo cp model-gateway.service /etc/systemd/system/
# отредактировать пути и env-переменные в service-файле
sudo systemctl daemon-reload
sudo systemctl enable --now model-gateway
```

---

## Конфигурация

Все параметры задаются переменными окружения (в systemd — через `Environment=`).

| Переменная | По умолчанию | Описание |
|---|---|---|
| `MODEL_GATEWAY_HOST` | `0.0.0.0` | Адрес прослушивания |
| `MODEL_GATEWAY_PORT` | `8080` | Порт |
| `MODEL_GATEWAY_TOKEN` | `""` | Одиночный Bearer-токен |
| `MODEL_GATEWAY_TOKENS` | `""` | Мультитокен: `tok1\|label1,tok2\|label2` |
| `VLLM_BACKENDS` | `""` | JSON: `{"model-id":"host:port", ...}` |
| `VLLM_PRIMARY` | первый из BACKENDS | Fallback-бэкенд |
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
  → опрашивает ВСЕ бэкенды → виртуальные варианты {model}-thinking / {model}-fast

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

Gateway автоматически опрашивает бэкенды при старте. Для моделей из `PREFERRED_VISIBLE_BASE_MODELS` (по умолчанию `qwen3.6-35b`, `gemma4-26b`) строятся два виртуальных ID:

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

#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
TOKEN="${TOKEN:-${MODEL_GATEWAY_TOKEN:-}}"
MODEL="${MODEL:-}"
PROMPT="${PROMPT:-Ответь одним словом: pong}"
TIMEOUT="${TIMEOUT:-30}"

curl_args=(--silent --show-error --fail --max-time "$TIMEOUT")
headers=()
if [[ -n "$TOKEN" ]]; then
  headers+=(-H "Authorization: Bearer $TOKEN")
fi

printf '==> Проверка %s/v1/models\n' "$BASE_URL"
models_json="$(curl "${curl_args[@]}" "${headers[@]}" "$BASE_URL/v1/models")"
printf '%s\n' "$models_json" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(json.dumps(data, ensure_ascii=False, indent=2))'

if [[ -z "$MODEL" ]]; then
  MODEL="$(printf '%s\n' "$models_json" | python3 -c 'import json,sys; data=json.load(sys.stdin); items=data.get("data") or []; print(items[0]["id"] if items else "")')"
fi

if [[ -z "$MODEL" ]]; then
  echo 'ERROR: Не удалось определить MODEL автоматически. Укажите MODEL=... вручную.' >&2
  exit 1
fi

printf '\n==> Проверка %s/v1/chat/completions для model=%s\n' "$BASE_URL" "$MODEL"
chat_payload="$(python3 - "$MODEL" "$PROMPT" <<'PY'
import json, sys
model = sys.argv[1]
prompt = sys.argv[2]
print(json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 32,
    "stream": False,
}, ensure_ascii=False))
PY
)"

curl "${curl_args[@]}" "${headers[@]}" \
  -H 'Content-Type: application/json' \
  -d "$chat_payload" \
  "$BASE_URL/v1/chat/completions" \
  | python3 -c 'import json,sys; data=json.load(sys.stdin); print(json.dumps(data, ensure_ascii=False, indent=2))'

if [[ -n "${IMAGE_HEALTH:-}" ]]; then
  printf '\n==> Проверка image backend health: %s/zimage/health\n' "$BASE_URL"
  curl "${curl_args[@]}" "$BASE_URL/zimage/health"
  printf '\n'
fi

printf '\nOK: smoke test завершён успешно.\n'

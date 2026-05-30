#!/usr/bin/env python3
"""
model_gateway_v2.py — Token-authenticated reverse proxy for multiple vLLM backends.

Routing rules:
  • GET /v1/models           — queries ALL backends, merges the model lists
  • POST /v1/chat/completions — reads `model` field from JSON body, routes accordingly
  • everything else           — routed by `model` body field, falls back to PRIMARY

Environment variables:
  MODEL_GATEWAY_HOST   Listen host           (default: 0.0.0.0)
  MODEL_GATEWAY_PORT   Listen port           (default: 8080)
  MODEL_GATEWAY_TOKEN  Required Bearer token (default: "")
  VLLM_BACKENDS        JSON map model-name → "host:port"
                       Example: '{"universal-nemotron":"127.0.0.1:8000","qwen-vl":"127.0.0.1:8001"}'
  VLLM_PRIMARY         Fallback backend "host:port" when model not in map
    MODEL_REWRITES       JSON map incoming model-name -> forwarded model-name
                                             Example: '{"universal-nemotron":"qwen35-uncensored"}'
"""

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN       = os.environ.get("MODEL_GATEWAY_TOKEN", "")
LISTEN_HOST = os.environ.get("MODEL_GATEWAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MODEL_GATEWAY_PORT", "8080"))

# Multi-token auth: MODEL_GATEWAY_TOKENS is a comma-separated list of valid
# Bearer tokens (takes precedence over single MODEL_GATEWAY_TOKEN).
# Each token may optionally be suffixed with "|<client-name>" for logging,
# e.g. "tok1|admin,tok2|zmproject"
_TOKENS_RAW = os.environ.get("MODEL_GATEWAY_TOKENS", "")
if _TOKENS_RAW:
    _token_map: dict[str, str] = {}  # token → client label
    for _entry in _TOKENS_RAW.split(","):
        _entry = _entry.strip()
        if "|" in _entry:
            _tok, _label = _entry.split("|", 1)
            _token_map[_tok.strip()] = _label.strip()
        elif _entry:
            _token_map[_entry] = "unknown"
    ALLOWED_TOKENS: frozenset[str] = frozenset(_token_map)
    TOKEN_LABELS: dict[str, str] = _token_map
elif TOKEN:
    ALLOWED_TOKENS = frozenset({TOKEN})
    TOKEN_LABELS = {TOKEN: "admin"}
else:
    ALLOWED_TOKENS = frozenset()
    TOKEN_LABELS = {}

# Parse VLLM_BACKENDS env: JSON map  model_name → "host:port"
_BACKENDS_RAW = os.environ.get("VLLM_BACKENDS", "")
if _BACKENDS_RAW:
    _bmap: dict[str, tuple[str, int, str]] = {}
    for _name, _hostport in json.loads(_BACKENDS_RAW).items():
        _h, _p = _hostport.rsplit(":", 1)
        _bmap[_name] = (_h, int(_p), "http")
else:
    # Fallback: legacy single-backend env
    _url   = os.environ.get("UNIVERSAL_CHAT_URL_HIGH", "http://127.0.0.1:8000")
    _ps    = urllib.parse.urlparse(_url)
    _bmap  = {
        "universal-nemotron": (
            _ps.hostname or "127.0.0.1",
            _ps.port or 8000,
            _ps.scheme or "http",
        )
    }

BACKENDS: dict[str, tuple[str, int, str]] = _bmap
# Unique backend addresses in insertion order (for /v1/models fan-out)
ALL_BACKENDS: list[tuple[str, int, str]] = list(dict.fromkeys(BACKENDS.values()))

# Primary backend: explicit override or first entry
_PRIMARY_RAW = os.environ.get("VLLM_PRIMARY", "")
if _PRIMARY_RAW:
    _ph, _pp      = _PRIMARY_RAW.rsplit(":", 1)
    PRIMARY_BACKEND: tuple[str, int, str] = (_ph, int(_pp), "http")
else:
    PRIMARY_BACKEND = next(iter(BACKENDS.values()))

_REWRITES_RAW = os.environ.get("MODEL_REWRITES", "")
MODEL_REWRITES: dict[str, str] = json.loads(_REWRITES_RAW) if _REWRITES_RAW else {}

_XAI_MODELS_RAW = os.environ.get("XAI_MODELS", "").strip() or os.environ.get("XAI_MODEL", "").strip()
XAI_MODELS: frozenset[str] = (
    frozenset(m.strip() for m in _XAI_MODELS_RAW.split(",") if m.strip())
    if _XAI_MODELS_RAW else frozenset()
)
XAI_API_BASE_URL = os.environ.get("XAI_API_BASE_URL", "https://api.x.ai/v1").rstrip("/")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_PROXY_URL = (
    os.environ.get("XAI_PROXY_URL", "").strip()
    or os.environ.get("HTTPS_PROXY", "").strip()
    or os.environ.get("https_proxy", "").strip()
    or os.environ.get("HTTP_PROXY", "").strip()
    or os.environ.get("http_proxy", "").strip()
)

_IMAGE_BACKEND_RAW = os.environ.get("IMAGE_BACKEND_URL", "").strip()
if _IMAGE_BACKEND_RAW:
    _image_parts = urllib.parse.urlparse(_IMAGE_BACKEND_RAW)
    IMAGE_BACKEND: tuple[str, int, str, str] | None = (
        _image_parts.scheme or "http",
        _image_parts.hostname or "127.0.0.1",
        _image_parts.port or (443 if _image_parts.scheme == "https" else 80),
        _image_parts.path.rstrip("/"),
    )
else:
    IMAGE_BACKEND = None
IMAGE_BACKEND_TOKEN = os.environ.get("IMAGE_BACKEND_TOKEN", "")

# SSE streaming chunk size (8 KB)
STREAM_CHUNK = 8 * 1024

# Thinking-model config: control reasoning/thinking behaviour per model.
# THINKING_MODELS — comma-separated model names that support thinking mode.
# THINKING_BUDGET  — controls thinking injection:
#   -1  = no injection (model uses its default / client-controlled)
#    0  = inject enable_thinking=false (disable thinking, fast responses)
#   >0  = inject thinking_budget=N (soft cap on reasoning tokens)
_THINKING_MODELS_RAW = os.environ.get("THINKING_MODELS", "")
THINKING_MODELS: frozenset[str] = (
    frozenset(m.strip() for m in _THINKING_MODELS_RAW.split(",") if m.strip())
    if _THINKING_MODELS_RAW else frozenset()
)
THINKING_BUDGET: int = int(os.environ.get("THINKING_BUDGET", "-1"))

# Only these two multimodal models should be exposed in the UI and via /v1/models.
PREFERRED_VISIBLE_BASE_MODELS: frozenset[str] = frozenset({"qwen3.6-35b", "gemma4-26b"})
VISIBLE_BASE_MODELS: frozenset[str] = frozenset()
VISIBLE_VARIANT_IDS: frozenset[str] = frozenset()

# Virtual model variants — auto-generated for visible multimodal models.
#   {model}-thinking  → injects enable_thinking=true  (full reasoning trace)
#   {model}-fast      → injects enable_thinking=false (skip thinking, faster)
MODEL_VARIANTS: dict[str, tuple[str, bool]] = {}

# Headers that MUST NOT be forwarded per RFC 2616 §13.5.1
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "proxy-connection",
})

START_TIME = time.time()
STATS_LOCK = threading.Lock()
GATEWAY_STATS = {
        "total_requests": 0,
        "route_counts": {},
        "model_counts": {},
        "status_counts": {},
        "recent_requests": [],
        "recent_errors": [],
        "last_request_ts": None,
}

MANAGER_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>LLM Gateway Manager</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #0b1020;
            --panel: #121933;
            --panel-2: #1a2345;
            --text: #e7ecff;
            --muted: #9aa7d1;
            --accent: #6ea8fe;
            --good: #30c48d;
            --warn: #f6c760;
            --bad: #ff6b6b;
            --border: rgba(255,255,255,0.08);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Inter, system-ui, sans-serif;
            background: linear-gradient(180deg, #0a0f1f 0%, #111936 100%);
            color: var(--text);
        }
        .wrap {
            max-width: 1380px;
            margin: 0 auto;
            padding: 24px;
        }
        .hero {
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 20px;
        }
        .hero h1 {
            margin: 0;
            font-size: 32px;
            line-height: 1.1;
        }
        .hero p {
            margin: 8px 0 0;
            color: var(--muted);
        }
        .toolbar {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }
        input, button {
            border-radius: 12px;
            border: 1px solid var(--border);
            background: var(--panel);
            color: var(--text);
            padding: 12px 14px;
            font: inherit;
        }
        input { min-width: 320px; }
        button {
            cursor: pointer;
            background: linear-gradient(180deg, #2e4b91, #243b73);
            border: none;
            min-width: 140px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
            margin-bottom: 18px;
        }
        .card {
            background: rgba(18,25,51,0.9);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.18);
        }
        .label { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
        .value { font-size: 30px; font-weight: 700; }
        .section {
            margin-top: 18px;
            display: grid;
            grid-template-columns: 1.1fr 1fr;
            gap: 18px;
        }
        .section.single { grid-template-columns: 1fr; }
        .panel-title {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            gap: 12px;
        }
        .panel-title h2 {
            margin: 0;
            font-size: 18px;
        }
        .pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 10px;
            border-radius: 999px;
            background: rgba(255,255,255,0.06);
            color: var(--muted);
            font-size: 12px;
        }
        .status-online { color: var(--good); }
        .status-offline { color: var(--bad); }
        .status-disabled { color: var(--warn); }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        th, td {
            padding: 10px 8px;
            border-bottom: 1px solid var(--border);
            text-align: left;
            vertical-align: top;
        }
        th { color: var(--muted); font-weight: 600; }
        .mono { font-family: ui-monospace, SFMono-Regular, monospace; }
        .tag {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 8px;
            background: rgba(110,168,254,0.14);
            color: #b9d2ff;
            font-size: 12px;
            margin-right: 6px;
        }
        .muted { color: var(--muted); }
        .error-box {
            white-space: pre-wrap;
            background: rgba(255,107,107,0.08);
            border: 1px solid rgba(255,107,107,0.2);
            padding: 12px;
            border-radius: 12px;
            color: #ffd0d0;
        }
        @media (max-width: 980px) {
            .section { grid-template-columns: 1fr; }
            input { min-width: 0; width: 100%; }
            .toolbar { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="hero">
            <div>
                <h1>LLM Gateway Manager</h1>
                <p>Статистика, модели, состояние backend'ов и статус загрузки в одном окне.</p>
            </div>
            <div class="toolbar">
                <input id="tokenInput" type="password" placeholder="Bearer token для manager API" />
                <button id="saveBtn">Сохранить токен</button>
                <button id="refreshBtn">Обновить</button>
            </div>
        </div>

        <div id="summary" class="grid"></div>

        <div class="section">
            <div class="card">
                <div class="panel-title">
                    <h2>Backend статус</h2>
                    <span class="pill" id="lastRefresh">—</span>
                </div>
                <div id="backendTable"></div>
            </div>
            <div class="card">
                <div class="panel-title">
                    <h2>Публикуемые модели</h2>
                    <span class="pill" id="publishedCount">0</span>
                </div>
                <div id="modelTable"></div>
            </div>
        </div>

        <div class="section single">
            <div class="card">
                <div class="panel-title">
                    <h2>Последние запросы</h2>
                    <span class="pill">in-memory</span>
                </div>
                <div id="recentTable"></div>
            </div>
        </div>

        <div class="section single">
            <div class="card">
                <div class="panel-title">
                    <h2>Последние ошибки</h2>
                    <span class="pill">max 20</span>
                </div>
                <div id="errorsBox" class="muted">Ошибок пока нет.</div>
            </div>
        </div>
    </div>

    <script>
        const tokenInput = document.getElementById('tokenInput');
        const saveBtn = document.getElementById('saveBtn');
        const refreshBtn = document.getElementById('refreshBtn');
        const summary = document.getElementById('summary');
        const backendTable = document.getElementById('backendTable');
        const modelTable = document.getElementById('modelTable');
        const recentTable = document.getElementById('recentTable');
        const errorsBox = document.getElementById('errorsBox');
        const lastRefresh = document.getElementById('lastRefresh');
        const publishedCount = document.getElementById('publishedCount');

        tokenInput.value = localStorage.getItem('gateway_manager_token') || '';

        function fmtTime(ts) {
            if (!ts) return '—';
            return new Date(ts * 1000).toLocaleString('ru-RU');
        }

        function escapeHtml(value) {
            return String(value ?? '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;');
        }

        async function loadDashboard() {
            const token = tokenInput.value.trim();
            const headers = token ? { Authorization: `Bearer ${token}` } : {};
            try {
                const res = await fetch('/manager/api/dashboard', { headers });
                if (!res.ok) {
                    const text = await res.text();
                    throw new Error(`HTTP ${res.status}: ${text}`);
                }
                const data = await res.json();
                renderDashboard(data);
            } catch (err) {
                summary.innerHTML = `<div class="card error-box">${escapeHtml(err.message)}</div>`;
                backendTable.innerHTML = '';
                modelTable.innerHTML = '';
                recentTable.innerHTML = '';
                errorsBox.innerHTML = `<div class="error-box">${escapeHtml(err.message)}</div>`;
            }
        }

        function renderDashboard(data) {
            const cards = [
                ['Uptime', data.summary.uptime_human],
                ['Всего запросов', data.summary.total_requests],
                ['Моделей опубликовано', data.summary.published_models],
                ['Backend online', `${data.summary.backends_online}/${data.summary.backends_total}`],
                ['Последний запрос', data.summary.last_request_at ? fmtTime(data.summary.last_request_at) : '—'],
            ];
            summary.innerHTML = cards.map(([label, value]) => `
                <div class="card">
                    <div class="label">${escapeHtml(label)}</div>
                    <div class="value">${escapeHtml(value)}</div>
                </div>
            `).join('');

            lastRefresh.textContent = `refresh ${new Date().toLocaleTimeString('ru-RU')}`;
            publishedCount.textContent = String(data.published_models.length);

            backendTable.innerHTML = `<table>
                <thead><tr><th>Backend</th><th>Тип</th><th>Статус</th><th>Latency</th><th>Модели</th><th>Детали</th></tr></thead>
                <tbody>${data.backends.map(row => `
                    <tr>
                        <td class="mono">${escapeHtml(row.name)}</td>
                        <td>${escapeHtml(row.kind)}</td>
                        <td><span class="status-${escapeHtml(row.status)}">${escapeHtml(row.status)}</span></td>
                        <td>${row.latency_ms == null ? '—' : `${row.latency_ms} ms`}</td>
                        <td>${row.models_count == null ? '—' : row.models_count}</td>
                        <td class="muted mono">${escapeHtml(row.detail || '')}</td>
                    </tr>
                `).join('')}</tbody>
            </table>`;

            modelTable.innerHTML = `<table>
                <thead><tr><th>Model</th><th>Source</th><th>Route</th><th>Visible</th></tr></thead>
                <tbody>${data.published_models.map(row => `
                    <tr>
                        <td class="mono">${escapeHtml(row.id)}</td>
                        <td>${escapeHtml(row.owned_by || row.source || '—')}</td>
                        <td class="muted mono">${escapeHtml(row.route || '—')}</td>
                        <td>${row.visible ? '<span class="tag">UI</span>' : '—'}</td>
                    </tr>
                `).join('')}</tbody>
            </table>`;

            recentTable.innerHTML = `<table>
                <thead><tr><th>Время</th><th>Path</th><th>Model</th><th>Status</th><th>Client</th><th>Duration</th><th>Upstream</th></tr></thead>
                <tbody>${(data.stats.recent_requests || []).map(row => `
                    <tr>
                        <td>${fmtTime(row.ts)}</td>
                        <td class="mono">${escapeHtml(row.path)}</td>
                        <td class="mono">${escapeHtml(row.model || '—')}</td>
                        <td>${escapeHtml(row.status)}</td>
                        <td>${escapeHtml(row.client || '—')}</td>
                        <td>${row.duration_ms == null ? '—' : `${row.duration_ms} ms`}</td>
                        <td>${escapeHtml(row.upstream || '—')}</td>
                    </tr>
                `).join('')}</tbody>
            </table>`;

            if (data.stats.recent_errors && data.stats.recent_errors.length) {
                errorsBox.innerHTML = data.stats.recent_errors.map(err => `
                    <div class="error-box" style="margin-bottom:10px;">
                        <strong>${fmtTime(err.ts)}</strong> — ${escapeHtml(err.path)} — ${escapeHtml(err.error)}
                    </div>
                `).join('');
            } else {
                errorsBox.innerHTML = '<div class="muted">Ошибок пока нет.</div>';
            }
        }

        saveBtn.addEventListener('click', () => {
            localStorage.setItem('gateway_manager_token', tokenInput.value.trim());
            loadDashboard();
        });
        refreshBtn.addEventListener('click', loadDashboard);
        loadDashboard();
        setInterval(loadDashboard, 10000);
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

def _normalize_model_name(model_name: str | None) -> str | None:
    """Normalize UI/client model aliases to canonical routing names.

    Open WebUI commonly sends model IDs with a trailing `:latest` suffix.
    Our backend maps and thinking-model lists are keyed by the base name,
    so strip that suffix before routing / rewrite / thinking checks.
    """
    if not model_name:
        return model_name
    if model_name.endswith(":latest"):
        return model_name[:-7]
    return model_name

def _make_connection(host: str, port: int, timeout: float, scheme: str = "http"):
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _path_only(path: str) -> str:
    parsed = urllib.parse.urlsplit(path)
    normalized = parsed.path.rstrip("/")
    return normalized or "/"


def _backend_for_model(model_name: str | None) -> tuple[str, int, str]:
    """Return (host, port) for the given model name, falling back to PRIMARY."""
    model_name = _normalize_model_name(model_name)
    if model_name:
        if model_name in BACKENDS:
            return BACKENDS[model_name]
        if model_name in MODEL_VARIANTS:
            base, _ = MODEL_VARIANTS[model_name]
            if base in BACKENDS:
                return BACKENDS[base]
    return PRIMARY_BACKEND


def _is_xai_model(model_name: str | None) -> bool:
    model_name = _normalize_model_name(model_name)
    return bool(model_name and model_name in XAI_MODELS and XAI_API_KEY)


def _xai_model_entries() -> list[dict]:
    return [
        {"id": model_id, "object": "model", "owned_by": "xai"}
        for model_id in sorted(XAI_MODELS)
    ]


def _sanitize_gateway_payload(body: bytes, normalized_model: str | None = None) -> bytes:
    if not body:
        return body
    try:
        payload = json.loads(body)
    except Exception:
        return body

    changed = False
    if normalized_model and payload.get("model") != normalized_model:
        payload["model"] = normalized_model
        changed = True
    if "chat_embed_thinking" in payload:
        del payload["chat_embed_thinking"]
        changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _build_proxy_opener(proxy_url: str | None):
    if proxy_url:
        return urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        }))
    return urllib.request.build_opener()


def _join_base_url(base_url: str, request_path: str, query: str = "") -> str:
    base_parts = urllib.parse.urlsplit(base_url)
    base_path = base_parts.path.rstrip("/")
    req_path = request_path or "/"

    if base_path and req_path.startswith(base_path + "/"):
        joined_path = req_path
    elif base_path and req_path == base_path:
        joined_path = req_path
    elif base_path and base_path.endswith("/v1") and req_path.startswith("/v1/"):
        joined_path = req_path
    else:
        joined_path = f"{base_path}{req_path}" if base_path else req_path

    url = urllib.parse.urlunsplit((base_parts.scheme, base_parts.netloc, joined_path, query, ""))
    return url


def _extract_model(body: bytes) -> str | None:
    """Try to parse JSON body and extract the `model` field."""
    if not body:
        return None
    try:
        return _normalize_model_name(json.loads(body).get("model"))
    except Exception:
        return None


def _rewrite_model_in_body(body: bytes, from_model: str | None) -> bytes:
    """Rewrite payload.model for backend compatibility if configured."""
    from_model = _normalize_model_name(from_model)
    if not body or not from_model or from_model not in MODEL_REWRITES:
        return body
    try:
        payload = json.loads(body)
        payload["model"] = MODEL_REWRITES[from_model]
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body


def _effective_model_name(body: bytes, fallback_model: str | None) -> str | None:
    """Return the effective model name after any payload rewrites."""
    fallback_model = _normalize_model_name(fallback_model)
    if not body:
        return fallback_model
    try:
        return _normalize_model_name(json.loads(body).get("model")) or fallback_model
    except Exception:
        return fallback_model


def _fetch_models(host: str, port: int, scheme: str = "http", timeout: float = 5.0) -> list:
    """Fetch /v1/models from a backend, return the data list (empty on error)."""
    try:
        conn = _make_connection(host, port, timeout=timeout, scheme=scheme)
        conn.request("GET", "/v1/models", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            resp.read()
            conn.close()
            return []
        data = json.loads(resp.read())
        conn.close()
        return data.get("data") or []
    except Exception:
        return []


def _refresh_live_model_maps() -> None:
    """Augment backend routing from live backend model IDs and rebuild visible variants."""
    global VISIBLE_BASE_MODELS, VISIBLE_VARIANT_IDS, MODEL_VARIANTS

    discovered: dict[str, tuple[str, int, str]] = {}
    for host, port, scheme in ALL_BACKENDS:
        for entry in _fetch_models(host, port, scheme=scheme):
            model_id = _normalize_model_name(entry.get("id"))
            if model_id:
                discovered.setdefault(model_id, (host, port, scheme))

    for model_id, backend in discovered.items():
        BACKENDS.setdefault(model_id, backend)

    VISIBLE_BASE_MODELS = frozenset(
        model_id for model_id in PREFERRED_VISIBLE_BASE_MODELS if model_id in BACKENDS
    )
    MODEL_VARIANTS = {}
    visible_variant_ids: set[str] = set()
    for model_id in VISIBLE_BASE_MODELS:
        MODEL_VARIANTS[f"{model_id}-thinking"] = (model_id, True)
        visible_variant_ids.add(f"{model_id}-thinking")
        MODEL_VARIANTS[f"{model_id}-fast"] = (model_id, False)
        visible_variant_ids.add(f"{model_id}-fast")

    VISIBLE_VARIANT_IDS = frozenset(visible_variant_ids)


_refresh_live_model_maps()


def _inc_counter(bucket: dict, key: str, value: int = 1) -> None:
    bucket[key] = int(bucket.get(key, 0)) + value


def _record_request_stat(
    *,
    path: str,
    status: int,
    model: str | None = None,
    client: str | None = None,
    duration_ms: int | None = None,
    upstream: str | None = None,
    error: str | None = None,
) -> None:
    ts = time.time()
    with STATS_LOCK:
        GATEWAY_STATS["total_requests"] += 1
        _inc_counter(GATEWAY_STATS["route_counts"], path)
        _inc_counter(GATEWAY_STATS["status_counts"], str(status))
        if model:
            _inc_counter(GATEWAY_STATS["model_counts"], model)
        GATEWAY_STATS["last_request_ts"] = ts
        recent = GATEWAY_STATS["recent_requests"]
        recent.append({
            "ts": ts,
            "path": path,
            "status": status,
            "model": model,
            "client": client,
            "duration_ms": duration_ms,
            "upstream": upstream,
        })
        del recent[:-30]
        if error:
            recent_errors = GATEWAY_STATS["recent_errors"]
            recent_errors.append({
                "ts": ts,
                "path": path,
                "status": status,
                "error": error[:500],
            })
            del recent_errors[:-20]


def _uptime_human(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _build_published_models() -> list[dict]:
    merged: list[dict] = []
    base_entries: dict[str, dict] = {}
    for host, port, scheme in ALL_BACKENDS:
        for entry in _fetch_models(host, port, scheme=scheme):
            mid = _normalize_model_name(entry.get("id", ""))
            if mid in VISIBLE_BASE_MODELS and mid not in base_entries:
                base_entries[mid] = entry

    for virtual_id, (base_id, _) in MODEL_VARIANTS.items():
        if virtual_id not in VISIBLE_VARIANT_IDS:
            continue
        base = base_entries.get(base_id, {"id": base_id, "object": "model", "owned_by": "llamacpp"})
        entry = dict(base)
        entry.update({
            "id": virtual_id,
            "route": base_id,
            "visible": True,
            "source": "vllm",
        })
        merged.append(entry)

    seen_ids = {entry.get("id") for entry in merged}
    for entry in _xai_model_entries():
        if entry["id"] not in seen_ids:
            merged.append({
                **entry,
                "route": "xai",
                "visible": True,
                "source": "xai",
            })
    return merged


def _probe_vllm_backend(name: str, host: str, port: int, scheme: str) -> dict:
    started = time.time()
    models = _fetch_models(host, port, scheme=scheme, timeout=4.0)
    latency = int((time.time() - started) * 1000)
    if models:
        return {
            "name": name,
            "kind": "vllm",
            "status": "online",
            "latency_ms": latency,
            "models_count": len(models),
            "detail": f"{scheme}://{host}:{port}",
        }
    return {
        "name": name,
        "kind": "vllm",
        "status": "offline",
        "latency_ms": latency,
        "models_count": 0,
        "detail": f"{scheme}://{host}:{port}",
    }


def _probe_url_json(name: str, url: str, headers: dict[str, str] | None = None, proxy_url: str | None = None) -> dict:
    started = time.time()
    opener = _build_proxy_opener(proxy_url)
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latency = int((time.time() - started) * 1000)
        items = data.get("data") or []
        return {
            "name": name,
            "kind": "remote",
            "status": "online",
            "latency_ms": latency,
            "models_count": len(items),
            "detail": url,
        }
    except Exception as exc:
        latency = int((time.time() - started) * 1000)
        return {
            "name": name,
            "kind": "remote",
            "status": "offline",
            "latency_ms": latency,
            "models_count": 0,
            "detail": f"{url} ({type(exc).__name__})",
        }


def _probe_image_backend() -> dict:
    if IMAGE_BACKEND is None:
        return {
            "name": "image-backend",
            "kind": "image",
            "status": "disabled",
            "latency_ms": None,
            "models_count": None,
            "detail": "not configured",
        }

    scheme, host, port, base_path = IMAGE_BACKEND
    started = time.time()
    conn = _make_connection(host, port, timeout=4.0, scheme=scheme)
    try:
        conn.request("GET", f"{base_path}/health")
        resp = conn.getresponse()
        resp.read()
        latency = int((time.time() - started) * 1000)
        status = "online" if 200 <= resp.status < 300 else "offline"
        return {
            "name": "image-backend",
            "kind": "image",
            "status": status,
            "latency_ms": latency,
            "models_count": None,
            "detail": f"{scheme}://{host}:{port}{base_path}/health",
        }
    except Exception as exc:
        latency = int((time.time() - started) * 1000)
        return {
            "name": "image-backend",
            "kind": "image",
            "status": "offline",
            "latency_ms": latency,
            "models_count": None,
            "detail": f"{scheme}://{host}:{port}{base_path}/health ({type(exc).__name__})",
        }
    finally:
        conn.close()


def _build_dashboard_payload() -> dict:
    published_models = _build_published_models()
    backends: list[dict] = []
    for name, (host, port, scheme) in BACKENDS.items():
        backends.append(_probe_vllm_backend(name, host, port, scheme))

    if XAI_MODELS:
        backends.append(_probe_url_json(
            "xai",
            _join_base_url(XAI_API_BASE_URL, "/models"),
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Accept": "application/json",
            } if XAI_API_KEY else {"Accept": "application/json"},
            proxy_url=XAI_PROXY_URL or None,
        ))
    else:
        backends.append({
            "name": "xai",
            "kind": "remote",
            "status": "disabled",
            "latency_ms": None,
            "models_count": 0,
            "detail": "not configured",
        })

    backends.append(_probe_image_backend())

    with STATS_LOCK:
        stats = {
            "total_requests": GATEWAY_STATS["total_requests"],
            "route_counts": dict(GATEWAY_STATS["route_counts"]),
            "model_counts": dict(GATEWAY_STATS["model_counts"]),
            "status_counts": dict(GATEWAY_STATS["status_counts"]),
            "recent_requests": list(GATEWAY_STATS["recent_requests"]),
            "recent_errors": list(GATEWAY_STATS["recent_errors"]),
            "last_request_at": GATEWAY_STATS["last_request_ts"],
        }

    online = sum(1 for row in backends if row["status"] == "online")
    return {
        "summary": {
            "uptime_human": _uptime_human(time.time() - START_TIME),
            "total_requests": stats["total_requests"],
            "published_models": len(published_models),
            "backends_online": online,
            "backends_total": len(backends),
            "last_request_at": stats["last_request_at"],
        },
        "published_models": published_models,
        "backends": backends,
        "stats": stats,
    }


def _rewrite_sse_embed_thinking(resp, wfile) -> None:
    """Forward SSE stream, rewriting delta.reasoning_content as <think>…</think> in delta.content.

    vLLM with enable_thinking=true returns reasoning tokens in a separate
    delta.reasoning_content field (OpenAI-style).  Most OpenAI-compatible
    clients (including qwen-agent / openai-python SDK) only accumulate
    delta.content and silently drop reasoning_content.  This rewriter
    merges reasoning into the content stream as <think>…</think> so that
    downstream agents can see and extract the reasoning text.
    """
    thinking_open = False
    buf = b""
    try:
        while True:
            chunk = resp.read(512)
            if not chunk:
                break
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line_bytes = buf[:nl]
                buf = buf[nl + 1:]
                line = line_bytes.rstrip(b"\r").decode("utf-8", errors="replace")

                if not line:
                    wfile.write(b"\n")
                    wfile.flush()
                    continue

                if not line.startswith("data: "):
                    wfile.write((line + "\n").encode("utf-8"))
                    wfile.flush()
                    continue

                data = line[6:]
                if data.strip() == "[DONE]":
                    if thinking_open:
                        close_chunk = ("data: " + json.dumps({
                            "choices": [{"index": 0, "delta": {"content": "\n</think>\n\n"}, "finish_reason": None}]
                        }, ensure_ascii=False) + "\n\n")
                        wfile.write(close_chunk.encode("utf-8"))
                        thinking_open = False
                    wfile.write(b"data: [DONE]\n\n")
                    wfile.flush()
                    continue

                try:
                    obj = json.loads(data)
                except Exception:
                    wfile.write((line + "\n").encode("utf-8"))
                    wfile.flush()
                    continue

                choices = obj.get("choices") or []
                if choices:
                    choice = choices[0]
                    delta = dict(choice.get("delta") or {})
                    reasoning = delta.pop("reasoning_content", None)
                    content = delta.get("content")
                    finish_reason = choice.get("finish_reason")

                    if reasoning is not None:
                        prefix = "<think>\n" if not thinking_open else ""
                        thinking_open = True
                        delta["content"] = f"{prefix}{reasoning}"
                    elif content is not None and thinking_open:
                        delta["content"] = f"\n</think>\n\n{content}"
                        thinking_open = False
                    elif delta.get("tool_calls") and thinking_open:
                        # Model is starting a tool call: close thinking first by
                        # emitting a separate SSE chunk with the closing tag.
                        close_chunk = ("data: " + json.dumps({
                            "choices": [{"index": 0, "delta": {"content": "\n</think>\n\n"}, "finish_reason": None}]
                        }, ensure_ascii=False) + "\n\n")
                        wfile.write(close_chunk.encode("utf-8"))
                        thinking_open = False
                    elif finish_reason and thinking_open:
                        delta["content"] = "\n</think>\n\n"
                        thinking_open = False

                    choice["delta"] = delta
                    obj["choices"] = [choice]

                out = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                wfile.write(out.encode("utf-8"))
                wfile.flush()

        # Flush any trailing data
        if buf.strip():
            wfile.write(buf)
            wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass



# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class GatewayHandler(BaseHTTPRequestHandler):
    server_version   = "vLLMGateway/2.1"
    protocol_version = "HTTP/1.1"

    # ---- Logging --------------------------------------------------------

    def log_message(self, fmt, *args):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {self.address_string()} {fmt % args}", flush=True)

    def log_request_detail(self, model_name: str | None, backend_port: int, body: bytes):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ua = self.headers.get("User-Agent", "—")[:60]
        try:
            payload = json.loads(body) if body else {}
            msgs = payload.get("messages", [])
            last_msg = msgs[-1] if msgs else {}
            # content может быть строкой или списком (vision)
            content = last_msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            content_preview = str(content)[:120].replace("\n", " ")
            role = last_msg.get("role", "?")
            n_msgs = len(msgs)
            stream = payload.get("stream", False)
        except Exception:
            content_preview, role, n_msgs, stream = "?", "?", 0, False
        print(
            f"[{ts}] DETAIL  model={model_name!r}  port={backend_port}"
            f"  msgs={n_msgs}  role={role}  stream={stream}"
            f"  client={self._client_label()!r}"
            f"  ua={ua!r}"
            f"  preview={content_preview!r}",
            flush=True
        )

    # ---- Auth -----------------------------------------------------------

    def _authorized(self) -> bool:
        if not ALLOWED_TOKENS:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:]
        return tok in ALLOWED_TOKENS

    def _client_label(self) -> str:
        """Return the label associated with the request's Bearer token."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return TOKEN_LABELS.get(auth[7:], "unknown")
        return "anon"

    # ---- Helpers --------------------------------------------------------

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, code: int, html: str):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _fwd_headers(self, host: str, port: int, body: bytes) -> dict[str, str]:
        fwd = {
            "Host": f"{host}:{port}",
            "Accept": "application/json",
            "User-Agent": "model-gateway-v2",
        }
        content_type = self.headers.get("Content-Type")
        if content_type:
            fwd["Content-Type"] = content_type
        elif body:
            fwd["Content-Type"] = "application/json"
        if body:
            fwd["Content-Length"] = str(len(body))
        return fwd

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                size_text = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunk = self.rfile.read(size)
                chunks.append(chunk)
                self.rfile.read(2)
            return b"".join(chunks)

        content_length = self.headers.get("Content-Length")
        if content_length:
            return self.rfile.read(int(content_length))
        return b""

    def _handle_manager_html(self):
        started = time.time()
        self._send_html(200, MANAGER_HTML)
        _record_request_stat(
            path="/manager/html",
            status=200,
            client=self._client_label(),
            duration_ms=int((time.time() - started) * 1000),
            upstream="embedded-ui",
        )

    def _handle_manager_dashboard(self):
        started = time.time()
        if not self._authorized():
            self._send_json(401, {
                "error": "unauthorized",
                "message": "Invalid or missing Bearer token",
            })
            _record_request_stat(
                path="/manager/api/dashboard",
                status=401,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream="embedded-ui",
                error="unauthorized",
            )
            return

        payload = _build_dashboard_payload()
        self._send_json(200, payload)
        _record_request_stat(
            path="/manager/api/dashboard",
            status=200,
            client=self._client_label(),
            duration_ms=int((time.time() - started) * 1000),
            upstream="embedded-ui",
        )

    def _handle_models(self):
        """Expose only the four visible fast/thinking variants for Gemma and Qwen 3.6."""
        started = time.time()
        merged: list = []
        base_entries: dict[str, dict] = {}
        for host, port, scheme in ALL_BACKENDS:
            for entry in _fetch_models(host, port, scheme=scheme):
                mid = _normalize_model_name(entry.get("id", ""))
                if mid in VISIBLE_BASE_MODELS and mid not in base_entries:
                    base_entries[mid] = entry
        # Add only virtual thinking/fast variants.
        for virtual_id, (base_id, _) in MODEL_VARIANTS.items():
            if virtual_id not in VISIBLE_VARIANT_IDS:
                continue
            base = base_entries.get(base_id, {"id": base_id, "object": "model", "owned_by": "llamacpp"})
            entry = dict(base)
            entry["id"] = virtual_id
            merged.append(entry)
        seen_ids = {entry.get("id") for entry in merged}
        for entry in _xai_model_entries():
            if entry["id"] not in seen_ids:
                merged.append(entry)
        self._send_json(200, {"object": "list", "data": merged})

        _record_request_stat(
            path="/v1/models",
            status=200,
            client=self._client_label(),
            duration_ms=int((time.time() - started) * 1000),
            upstream="fanout",
        )

    def _proxy_xai_request(self, body: bytes, model_name: str | None = None):
        started = time.time()
        if not XAI_API_KEY:
            self._send_json(502, {"error": "xai_not_configured", "message": "XAI_API_KEY is missing"})
            _record_request_stat(
                path=_path_only(self.path),
                status=502,
                model=model_name,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream="xai",
                error="xai_not_configured",
            )
            return

        parts = urllib.parse.urlsplit(self.path)
        upstream_url = _join_base_url(XAI_API_BASE_URL, parts.path, parts.query)

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in {"host", "authorization", "expect", "content-length"}
        }
        headers["Authorization"] = f"Bearer {XAI_API_KEY}"
        if body:
            headers["Content-Length"] = str(len(body))

        opener = _build_proxy_opener(XAI_PROXY_URL or None)
        request = urllib.request.Request(
            upstream_url,
            data=body or None,
            headers=headers,
            method=self.command,
        )

        try:
            resp = opener.open(request, timeout=3600)
        except urllib.error.HTTPError as exc:
            resp = exc
        except Exception as exc:
            self._send_json(502, {"error": "xai_backend_unreachable", "message": str(exc)})
            _record_request_stat(
                path=_path_only(self.path),
                status=502,
                model=model_name,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream="xai",
                error=str(exc),
            )
            return

        content_type = resp.headers.get("Content-Type", "")
        is_stream = "text/event-stream" in content_type

        self.send_response(resp.status)
        for name, value in resp.headers.items():
            if name.lower() in HOP_BY_HOP:
                continue
            if name.lower() == "content-length" and is_stream:
                continue
            self.send_header(name, value)
        if is_stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            if is_stream:
                while True:
                    chunk = resp.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                self.wfile.write(resp.read())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

        _record_request_stat(
            path=_path_only(self.path),
            status=resp.status,
            model=model_name,
            client=self._client_label(),
            duration_ms=int((time.time() - started) * 1000),
            upstream="xai",
            error=None if resp.status < 400 else f"upstream_http_{resp.status}",
        )

        self.close_connection = True

    def _proxy_image_request(self):
        started = time.time()
        if IMAGE_BACKEND is None:
            self._send_json(404, {"error": "not_found", "message": "Image backend is not configured"})
            _record_request_stat(
                path=_path_only(self.path),
                status=404,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream="image",
                error="image_backend_not_configured",
            )
            return

        scheme, host, port, base_path = IMAGE_BACKEND
        path_only = _path_only(self.path)
        if path_only == "/zimage/health":
            upstream_path = f"{base_path}/health"
        elif path_only == "/zimage/generate":
            if not self._authorized():
                self._send_json(401, {
                    "error": "unauthorized",
                    "message": "Invalid or missing Bearer token",
                })
                _record_request_stat(
                    path=path_only,
                    status=401,
                    client=self._client_label(),
                    duration_ms=int((time.time() - started) * 1000),
                    upstream="image",
                    error="unauthorized",
                )
                return
            upstream_path = f"{base_path}/v1/generate"
        elif path_only == "/v1/images/generations":
            if not self._authorized():
                self._send_json(401, {
                    "error": "unauthorized",
                    "message": "Invalid or missing Bearer token",
                })
                _record_request_stat(
                    path=path_only,
                    status=401,
                    client=self._client_label(),
                    duration_ms=int((time.time() - started) * 1000),
                    upstream="image",
                    error="unauthorized",
                )
                return
            upstream_path = f"{base_path}/v1/images/generations"
        elif urllib.parse.urlsplit(self.path).path.startswith("/zimage/images/"):
            upstream_path = f"{base_path}{self.path[len('/zimage'):] }"
        else:
            self._send_json(404, {"error": "not_found", "message": "Unknown zimage route"})
            _record_request_stat(
                path=path_only,
                status=404,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream="image",
                error="unknown_zimage_route",
            )
            return

        body = self._read_request_body()
        if path_only == "/v1/images/generations":
            preview = body[:200].decode("utf-8", errors="replace").replace("\n", " ")
            self.log_message(
                'IMAGE  te=%r cl=%r ct=%r expect=%r bytes=%d preview=%r',
                self.headers.get("Transfer-Encoding"),
                self.headers.get("Content-Length"),
                self.headers.get("Content-Type"),
                self.headers.get("Expect"),
                len(body),
                preview,
            )

        headers = self._fwd_headers(host, port, body)
        if IMAGE_BACKEND_TOKEN and path_only in {"/zimage/generate", "/v1/images/generations"}:
            headers["Authorization"] = f"Bearer {IMAGE_BACKEND_TOKEN}"

        conn = _make_connection(host, port, timeout=3600, scheme=scheme)
        try:
            conn.request(self.command, upstream_path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            self._send_json(502, {"error": "image_backend_unreachable", "message": str(exc)})
            conn.close()
            _record_request_stat(
                path=path_only,
                status=502,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream=f"image:{host}:{port}",
                error=str(exc),
            )
            return

        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in HOP_BY_HOP:
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(resp.read())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()
            conn.close()
        _record_request_stat(
            path=path_only,
            status=resp.status,
            client=self._client_label(),
            duration_ms=int((time.time() - started) * 1000),
            upstream=f"image:{host}:{port}",
            error=None if resp.status < 400 else f"upstream_http_{resp.status}",
        )
        self.close_connection = True

    # ---- Core proxy logic -----------------------------------------------

    def _proxy(self):
        started = time.time()
        path_only = _path_only(self.path)

        if self.command == "GET" and path_only in {"/", "/manager", "/manager/", "/manager/html"}:
            self._handle_manager_html()
            return

        if self.command == "GET" and path_only == "/manager/api/dashboard":
            self._handle_manager_dashboard()
            return

        if urllib.parse.urlsplit(self.path).path.startswith("/zimage/") or path_only == "/v1/images/generations":
            self._proxy_image_request()
            return

        # 1. Token auth
        if not self._authorized():
            self._send_json(401, {
                "error": "unauthorized",
                "message": "Invalid or missing Bearer token",
            })
            _record_request_stat(
                path=path_only,
                status=401,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream="gateway",
                error="unauthorized",
            )
            return

        # 2. Special case: GET /v1/models — merge from all backends
        if self.command == "GET" and path_only == "/v1/models":
            self._handle_models()
            return

        # 3. Read request body (if any)
        body = self._read_request_body()

        # 4. Pick backend by model name from body
        model_name = _extract_model(body)

        # 4a. Route configured xAI/Grok models to external OpenAI-compatible API.
        if _is_xai_model(model_name):
            if path_only.endswith("chat/completions"):
                self.log_request_detail(model_name, 443, body)
            body = _sanitize_gateway_payload(body, normalized_model=model_name)
            self._proxy_xai_request(body, model_name=model_name)
            return

        host, port, scheme = _backend_for_model(model_name)

        # Log detailed request info for analysis
        if path_only.endswith("chat/completions"):
            self.log_request_detail(model_name, port, body)

        # 4a. Rewrite legacy/external alias model names if needed.
        body = _sanitize_gateway_payload(_rewrite_model_in_body(body, model_name), normalized_model=model_name)

        # 4b. Inject thinking kwargs and handle virtual variants
        # Virtual variants (-thinking / -fast): rewrite model field + inject enable_thinking.
        # THINKING_BUDGET == -1 → no injection for non-variant requests
        # THINKING_BUDGET ==  0 → disable thinking
        # THINKING_BUDGET  > 0 → set thinking_budget=N
        effective_model = _effective_model_name(body, model_name)
        needs_think_embed = False  # whether to rewrite reasoning_content into content
        if (
            body
            and path_only.endswith("chat/completions")
        ):
            try:
                payload = json.loads(body)
                body_changed = False

                if model_name in MODEL_VARIANTS:
                    # Virtual variant: rewrite model to base and inject enable_thinking
                    base_model, variant_thinking = MODEL_VARIANTS[model_name]
                    payload["model"] = base_model
                    if "chat_template_kwargs" not in payload:
                        ctk: dict = {"enable_thinking": variant_thinking}
                        if variant_thinking and THINKING_BUDGET > 0:
                            ctk["thinking_budget"] = THINKING_BUDGET
                        payload["chat_template_kwargs"] = ctk
                    body_changed = True
                elif THINKING_BUDGET != -1 and effective_model in THINKING_MODELS:
                    # Global budget injection for non-variant thinking models
                    if "chat_template_kwargs" not in payload:
                        if THINKING_BUDGET == 0:
                            payload["chat_template_kwargs"] = {"enable_thinking": False}
                        else:
                            payload["chat_template_kwargs"] = {"thinking_budget": THINKING_BUDGET}
                        body_changed = True

                if body_changed:
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

                # SSE embed mode: merge reasoning_content into delta.content as <think>…</think>.
                # OFF by default — reasoning_content passes through as a separate delta field,
                # which is the OpenAI/DeepSeek standard that programmatic clients expect.
                # Enable only when client explicitly opts in via header or body flag:
                #   Header:  X-Embed-Thinking: true
                #   Body:    "chat_embed_thinking": true
                embed_hdr = self.headers.get("X-Embed-Thinking", "").lower() in ("1", "true", "yes")
                needs_think_embed = embed_hdr or bool(payload.get("chat_embed_thinking"))
            except Exception:
                pass  # body not valid JSON — leave as-is

        # 5. Send request to vLLM backend
        conn = _make_connection(host, port, timeout=3600, scheme=scheme)
        fwd = self._fwd_headers(host, port, body)
        try:
            conn.request(self.command, self.path, body=body or None, headers=fwd)
            resp = conn.getresponse()
        except Exception as exc:
            self._send_json(502, {"error": "backend_unreachable", "message": str(exc)})
            conn.close()
            _record_request_stat(
                path=path_only,
                status=502,
                model=effective_model,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream=f"{scheme}://{host}:{port}",
                error=str(exc),
            )
            return

        # 6. Detect streaming response (SSE)
        content_type = resp.getheader("Content-Type", "")
        is_stream    = "text/event-stream" in content_type

        # 7. Relay response headers
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in HOP_BY_HOP:
                continue
            if name.lower() == "content-length" and is_stream:
                continue
            self.send_header(name, value)
        if is_stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        # 8. Relay body
        try:
            if is_stream:
                if needs_think_embed:
                    # Rewrite reasoning_content as <think>…</think> in content
                    _rewrite_sse_embed_thinking(resp, self.wfile)
                else:
                    # Flush every chunk immediately for real-time streaming
                    while True:
                        chunk = resp.read(STREAM_CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            else:
                self.wfile.write(resp.read())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()
            conn.close()

        _record_request_stat(
            path=path_only,
            status=resp.status,
            model=effective_model,
            client=self._client_label(),
            duration_ms=int((time.time() - started) * 1000),
            upstream=f"{scheme}://{host}:{port}",
            error=None if resp.status < 400 else f"upstream_http_{resp.status}",
        )

        self.close_connection = True

    # ---- HTTP method dispatchers ----------------------------------------

    def do_GET(self):     self._proxy()
    def do_POST(self):    self._proxy()
    def do_DELETE(self):  self._proxy()
    def do_PUT(self):     self._proxy()
    def do_PATCH(self):   self._proxy()
    def do_HEAD(self):    self._proxy()
    def do_OPTIONS(self): self._proxy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    summary = ", ".join(f"{n}→{scheme}://{h}:{p}" for n, (h, p, scheme) in BACKENDS.items())
    server  = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), GatewayHandler)
    print(
        f"[gateway] {LISTEN_HOST}:{LISTEN_PORT}  "
        f"backends=[{summary}]  "
        f"primary={PRIMARY_BACKEND[2]}://{PRIMARY_BACKEND[0]}:{PRIMARY_BACKEND[1]}  "
        f"auth={'token' if ALLOWED_TOKENS else 'DISABLED'}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    print("[gateway] stopped", flush=True)


if __name__ == "__main__":
    main()

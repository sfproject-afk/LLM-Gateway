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
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
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

# Per-backend queueing.  The gateway accepts many client connections, but each
# physical local LLM backend should only receive one active request at a time.
MODEL_QUEUE_MAX_SIZE = int(os.environ.get("MODEL_QUEUE_MAX_SIZE", "5"))
MODEL_QUEUE_TIMEOUT = float(os.environ.get("MODEL_QUEUE_TIMEOUT", "60"))

MANAGER_PIN = os.environ.get("MODEL_GATEWAY_MANAGER_PIN", "2064564")
MANAGER_PIN_MAX_ATTEMPTS = int(os.environ.get("MODEL_GATEWAY_MANAGER_PIN_MAX_ATTEMPTS", "3"))
MANAGER_PIN_BAN_SECONDS = int(os.environ.get("MODEL_GATEWAY_MANAGER_PIN_BAN_SECONDS", "1800"))
MANAGER_SESSION_TTL = int(os.environ.get("MODEL_GATEWAY_MANAGER_SESSION_TTL", "43200"))

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
        "load_buckets": {},
}

QUEUE_LOCK = threading.Condition()
QUEUE_STATES: dict[str, dict] = {}
MANAGER_AUTH_LOCK = threading.Lock()
MANAGER_PIN_FAILURES: dict[str, dict] = {}
MANAGER_SESSIONS: dict[str, dict] = {}

MANAGER_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>LLM Gateway Manager</title>
    <style>
        :root {
            color-scheme: dark;
            --background: 240 10% 3.9%;
            --foreground: 0 0% 98%;
            --muted: 240 3.7% 15.9%;
            --muted-foreground: 240 5% 64.9%;
            --card: 240 10% 3.9%;
            --card-foreground: 0 0% 98%;
            --popover: 240 10% 3.9%;
            --popover-foreground: 0 0% 98%;
            --border: 240 3.7% 15.9%;
            --input: 240 3.7% 15.9%;
            --primary: 142.1 76.2% 36.3%;
            --primary-foreground: 0 0% 98%;
            --secondary: 240 3.7% 15.9%;
            --secondary-foreground: 0 0% 98%;
            --destructive: 0 62.8% 30.6%;
            --destructive-foreground: 0 0% 98%;
            --ring: 142.1 76.2% 36.3%;
            --radius: 0.5rem;
            --success: 142.1 70.6% 45.3%;
            --warning: 47.9 95.8% 53.1%;
            --sigma-green: #00db4d;
            --shell: rgba(7, 10, 14, .72);
            --panel: rgba(13, 17, 23, .58);
            --hairline: rgba(255,255,255,.08);
            --text-soft: rgba(244,244,245,.72);
        }
        * { box-sizing: border-box; }
        html { scroll-behavior: smooth; }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            font-size: 14px;
            line-height: 1.45;
            background:
                radial-gradient(circle at 82% 18%, rgba(115, 91, 105, .42), transparent 28rem),
                radial-gradient(circle at 8% 74%, rgba(0, 92, 106, .34), transparent 34rem),
                linear-gradient(180deg, #080b10 0%, #05070a 100%);
            color: hsl(var(--foreground));
        }
        body::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background:
                linear-gradient(90deg, rgba(0,0,0,.52), transparent 34%, rgba(0,0,0,.42)),
                repeating-linear-gradient(0deg, rgba(255,255,255,.018), rgba(255,255,255,.018) 1px, transparent 1px, transparent 43px);
        }
        body.locked .topbar,
        body.locked .app-shell { display: none; }
        a { color: inherit; text-decoration: none; }
        .topbar {
            position: sticky;
            top: 0;
            z-index: 20;
            height: 48px;
            display: grid;
            grid-template-columns: 280px minmax(0, 1fr) 280px;
            align-items: center;
            border-bottom: 1px solid var(--hairline);
            background: rgba(8, 10, 14, .76);
            backdrop-filter: blur(18px);
        }
        .brand {
            height: 48px;
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 0 20px;
            font-size: 13px;
            font-weight: 700;
            letter-spacing: .01em;
        }
        .brand-mark {
            width: 22px;
            height: 22px;
            display: inline-grid;
            place-items: center;
            border: 1px solid rgba(255,255,255,.24);
            background: rgba(255,255,255,.06);
            clip-path: polygon(50% 0, 100% 26%, 100% 74%, 50% 100%, 0 74%, 0 26%);
            color: var(--sigma-green);
            font-size: 12px;
        }
        .top-nav {
            display: flex;
            align-items: center;
            gap: 20px;
            color: hsl(var(--muted-foreground));
            font-size: 12px;
        }
        .top-nav .active {
            color: hsl(var(--foreground));
            font-weight: 600;
        }
        .top-actions {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 8px;
            padding: 0 18px;
        }
        .app-shell {
            position: relative;
            z-index: 1;
            display: grid;
            grid-template-columns: 280px minmax(0, 760px) 280px;
            justify-content: center;
            min-height: calc(100vh - 48px);
        }
        .sidebar,
        .toc {
            position: sticky;
            top: 48px;
            height: calc(100vh - 48px);
            overflow: auto;
            padding: 18px 18px 28px;
            color: hsl(var(--muted-foreground));
            background: rgba(5, 9, 13, .24);
        }
        .sidebar { border-right: 1px solid var(--hairline); }
        .toc { border-left: 1px solid var(--hairline); }
        .green-strip {
            height: 22px;
            margin: -2px 0 14px;
            border: 1px solid rgba(0,219,77,.22);
            background: linear-gradient(90deg, rgba(0,219,77,.78), rgba(0,219,77,.28));
            box-shadow: 0 0 22px rgba(0,219,77,.22);
        }
        .nav-group { margin: 0 0 18px; }
        .nav-title,
        .toc-title {
            margin: 0 0 9px;
            color: hsl(var(--foreground));
            font-size: 12px;
            font-weight: 650;
        }
        .nav-link,
        .toc a {
            display: flex;
            align-items: center;
            justify-content: space-between;
            min-height: 24px;
            padding: 3px 10px;
            border-left: 1px solid transparent;
            color: hsl(var(--muted-foreground));
            font-size: 12px;
        }
        .nav-link.active {
            color: hsl(var(--foreground));
            border-left-color: var(--sigma-green);
            background: linear-gradient(90deg, rgba(0,219,77,.12), transparent);
        }
        .nav-count {
            color: rgba(244,244,245,.46);
            font-family: ui-monospace, SFMono-Regular, monospace;
            font-size: 11px;
        }
        .content {
            min-width: 0;
            padding: 38px 24px 80px;
        }
        .page-kicker {
            margin: 0 0 10px;
            color: hsl(var(--muted-foreground));
            font-size: 12px;
        }
        .page-head {
            margin-bottom: 28px;
            padding-bottom: 22px;
            border-bottom: 1px solid var(--hairline);
        }
        .page-head h1 {
            margin: 0;
            font-size: 31px;
            line-height: 1.08;
            letter-spacing: 0;
        }
        .page-head p {
            max-width: 680px;
            margin: 8px 0 0;
            color: hsl(var(--muted-foreground));
        }
        .login-shell {
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 24px;
        }
        body.unlocked .login-shell { display: none; }
        .login-card {
            width: min(420px, 100%);
            position: relative;
            z-index: 1;
            background: var(--shell);
            border: 1px solid var(--hairline);
            border-radius: var(--radius);
            padding: 24px;
            box-shadow: 0 24px 90px rgba(0,0,0,0.45);
            backdrop-filter: blur(18px);
        }
        .login-card h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
        .login-card p { margin: 0 0 18px; color: hsl(var(--muted-foreground)); line-height: 1.5; }
        .toolbar {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }
        input, button {
            border-radius: var(--radius);
            border: 1px solid hsl(var(--input));
            background: rgba(13, 17, 23, .7);
            color: hsl(var(--foreground));
            padding: 8px 11px;
            font: inherit;
            font-size: 12px;
        }
        input:focus { outline: 2px solid hsl(var(--ring) / .35); outline-offset: 2px; }
        input { min-width: 280px; }
        button {
            cursor: pointer;
            min-width: 0;
            background: rgba(255,255,255,.92);
            border-color: rgba(255,255,255,.92);
            color: #09090b;
            font-weight: 600;
        }
        button.secondary {
            background: rgba(13, 17, 23, .45);
            border-color: var(--hairline);
            color: hsl(var(--foreground));
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
            gap: 1px;
            margin-bottom: 18px;
            overflow: hidden;
            border: 1px solid var(--hairline);
            border-radius: var(--radius);
            background: var(--hairline);
        }
        .card {
            background: var(--panel);
            border: 1px solid var(--hairline);
            border-radius: calc(var(--radius) - 2px);
            padding: 15px;
            box-shadow: none;
            backdrop-filter: blur(12px);
        }
        .grid > .card,
        .grid > div {
            border: 0;
            border-radius: 0;
            background: rgba(13,17,23,.56);
        }
        .label { color: hsl(var(--muted-foreground)); font-size: 12px; margin-bottom: 6px; }
        .value { font-size: 24px; font-weight: 680; letter-spacing: 0; }
        .section {
            margin-top: 24px;
            display: grid;
            grid-template-columns: 1.1fr 1fr;
            gap: 16px;
        }
        .section.single { grid-template-columns: 1fr; }
        .panel-title {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            gap: 12px;
            padding-bottom: 11px;
            border-bottom: 1px solid var(--hairline);
        }
        .panel-title h2 {
            margin: 0;
            font-size: 18px;
            letter-spacing: 0;
        }
        .pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 3px 8px;
            border-radius: 6px;
            background: rgba(39, 45, 54, .74);
            color: hsl(var(--secondary-foreground));
            font-size: 11px;
            border: 1px solid var(--hairline);
        }
        .status-online { color: hsl(var(--success)); }
        .status-offline { color: hsl(var(--destructive)); }
        .status-disabled { color: hsl(var(--warning)); }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }
        th, td {
            padding: 9px 8px;
            border-bottom: 1px solid var(--hairline);
            text-align: left;
            vertical-align: top;
        }
        th { color: hsl(var(--muted-foreground)); font-weight: 650; }
        tbody tr:hover { background: rgba(255,255,255,.025); }
        .mono { font-family: ui-monospace, SFMono-Regular, monospace; }
        .tag {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 5px;
            background: rgba(39,45,54,.7);
            color: hsl(var(--secondary-foreground));
            font-size: 11px;
            margin-right: 6px;
            border: 1px solid var(--hairline);
        }
        .muted { color: hsl(var(--muted-foreground)); }
        .hidden { display: none !important; }
        .metric-sub { margin-top: 6px; color: hsl(var(--muted-foreground)); font-size: 12px; }
        .chart {
            display: flex;
            align-items: end;
            gap: 3px;
            height: 120px;
            padding-top: 12px;
            border-top: 1px solid var(--hairline);
        }
        .bar {
            flex: 1;
            min-width: 3px;
            background: linear-gradient(180deg, rgba(0,219,77,.95), rgba(0,219,77,.32));
            border-radius: 2px 2px 0 0;
            opacity: .9;
        }
        .bar.err { background: hsl(var(--destructive)); }
        .queue-meter {
            height: 8px;
            overflow: hidden;
            border-radius: 999px;
            background: rgba(39,45,54,.8);
            border: 1px solid var(--hairline);
            margin-top: 8px;
        }
        .queue-meter > span { display: block; height: 100%; background: var(--sigma-green); }
        .error-box {
            white-space: pre-wrap;
            background: rgba(255,107,107,0.08);
            border: 1px solid rgba(255,107,107,0.2);
            padding: 12px;
            border-radius: 12px;
            color: #ffd0d0;
        }
        @media (max-width: 1180px) {
            .topbar { grid-template-columns: 220px 1fr auto; }
            .app-shell { grid-template-columns: 220px minmax(0, 1fr); }
            .toc { display: none; }
        }
        @media (max-width: 820px) {
            .topbar { grid-template-columns: 1fr auto; }
            .top-nav { display: none; }
            .brand { padding-left: 14px; }
            .app-shell { display: block; }
            .sidebar { display: none; }
            .content { padding: 26px 14px 56px; }
            .section { grid-template-columns: 1fr; }
            input { min-width: 0; width: 100%; }
            .toolbar { width: 100%; }
            .top-actions .toolbar { width: auto; }
        }
    </style>
</head>
<body class="locked">
    <div class="login-shell">
        <form id="pinForm" class="login-card">
            <h1>LLM Gateway Manager</h1>
            <p>Введите PIN-код для доступа к панели загрузки, статистики и очередей.</p>
            <input id="pinInput" type="password" inputmode="numeric" autocomplete="current-password" placeholder="PIN-код" />
            <div class="toolbar" style="margin-top:12px;">
                <button type="submit">Войти</button>
            </div>
            <div id="pinMessage" class="metric-sub"></div>
        </form>
    </div>
    <header class="topbar">
        <div class="brand">
            <span class="brand-mark">S</span>
            <span>SIGMA-UI</span>
        </div>
        <nav class="top-nav" aria-label="Top navigation">
            <a href="#overview">Home</a>
            <a href="#load">Docs</a>
            <a class="active" href="#queues">Components</a>
            <a href="#stats">Blocks <span class="pill">Alpha</span></a>
            <a href="#errors">Changelog <span class="pill">v2</span></a>
        </nav>
        <div class="top-actions">
            <div class="toolbar">
                <button id="refreshBtn" class="secondary">Refresh</button>
                <button id="logoutBtn" class="secondary">Logout</button>
            </div>
        </div>
    </header>

    <div class="app-shell">
        <aside class="sidebar">
            <div class="green-strip"></div>
            <div class="nav-group">
                <p class="nav-title">Config options</p>
                <a class="nav-link active" href="#overview">gateway <span class="nav-count">live</span></a>
                <a class="nav-link" href="#load">load <span class="nav-count">60m</span></a>
                <a class="nav-link" href="#queues">queue <span class="nav-count">1x</span></a>
            </div>
            <div class="nav-group">
                <p class="nav-title">Components</p>
                <a class="nav-link" href="#backends">Backend status</a>
                <a class="nav-link" href="#models">Published models</a>
                <a class="nav-link" href="#stats">Recent requests</a>
                <a class="nav-link" href="#errors">Recent errors</a>
            </div>
            <div class="nav-group">
                <p class="nav-title">Instructions</p>
                <div class="nav-link">PIN attempts <span class="nav-count">3</span></div>
                <div class="nav-link">Ban window <span class="nav-count">30m</span></div>
                <div class="nav-link">Per model <span class="nav-count">1 active</span></div>
            </div>
        </aside>

        <main class="content">
            <header id="overview" class="page-head">
                <p class="page-kicker">Gateway manager</p>
                <h1>LLM Gateway</h1>
                <p>Загрузка, историческая статистика, backend status и очередь запросов в темной теме Sigma UI.</p>
            </header>

            <div id="summary" class="grid"></div>

        <div id="load" class="section">
            <div class="card">
                <div class="panel-title">
                    <h2>Текущая загрузка</h2>
                    <span class="pill" id="loadNow">—</span>
                </div>
                <div id="loadPanel"></div>
            </div>
            <div class="card">
                <div class="panel-title">
                    <h2>Историческая загрузка</h2>
                    <span class="pill">last 60m</span>
                </div>
                <div id="loadChart"></div>
            </div>
        </div>

        <div class="section">
            <div id="backends" class="card">
                <div class="panel-title">
                    <h2>Backend статус</h2>
                    <span class="pill" id="lastRefresh">—</span>
                </div>
                <div id="backendTable"></div>
            </div>
            <div id="models" class="card">
                <div class="panel-title">
                    <h2>Публикуемые модели</h2>
                    <span class="pill" id="publishedCount">0</span>
                </div>
                <div id="modelTable"></div>
            </div>
        </div>

        <div id="stats" class="section single">
            <div class="card">
                <div class="panel-title">
                    <h2>Последние запросы</h2>
                    <span class="pill">in-memory</span>
                </div>
                <div id="recentTable"></div>
            </div>
        </div>

        <div id="queues" class="section single">
            <div class="card">
                <div class="panel-title">
                    <h2>Очереди</h2>
                    <span class="pill">1 active per backend</span>
                </div>
                <div id="queueTable"></div>
            </div>
        </div>

        <div id="errors" class="section single">
            <div class="card">
                <div class="panel-title">
                    <h2>Последние ошибки</h2>
                    <span class="pill">max 20</span>
                </div>
                <div id="errorsBox" class="muted">Ошибок пока нет.</div>
            </div>
        </div>
        </main>

        <aside class="toc">
            <p class="toc-title">Table of content</p>
            <a href="#overview">Overview</a>
            <a href="#load">Load</a>
            <a href="#backends">Backends</a>
            <a href="#models">Models</a>
            <a href="#stats">Statistics</a>
            <a href="#queues">Queue</a>
            <a href="#errors">Errors</a>
            <div class="nav-group" style="margin-top:24px;">
                <p class="toc-title">Queue rules</p>
                <div class="nav-link">1 active request</div>
                <div class="nav-link">others wait</div>
                <div class="nav-link">position in headers</div>
            </div>
        </aside>
    </div>

    <script>
        const pinForm = document.getElementById('pinForm');
        const pinInput = document.getElementById('pinInput');
        const pinMessage = document.getElementById('pinMessage');
        const refreshBtn = document.getElementById('refreshBtn');
        const logoutBtn = document.getElementById('logoutBtn');
        const summary = document.getElementById('summary');
        const backendTable = document.getElementById('backendTable');
        const modelTable = document.getElementById('modelTable');
        const recentTable = document.getElementById('recentTable');
        const queueTable = document.getElementById('queueTable');
        const loadPanel = document.getElementById('loadPanel');
        const loadChart = document.getElementById('loadChart');
        const loadNow = document.getElementById('loadNow');
        const errorsBox = document.getElementById('errorsBox');
        const lastRefresh = document.getElementById('lastRefresh');
        const publishedCount = document.getElementById('publishedCount');

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

        function setLocked(locked, message = '') {
            document.body.classList.toggle('locked', locked);
            document.body.classList.toggle('unlocked', !locked);
            pinMessage.textContent = message;
            if (locked) setTimeout(() => pinInput.focus(), 50);
        }

        function sum(values, key) {
            return values.reduce((acc, row) => acc + Number(row[key] || 0), 0);
        }

        function renderBars(buckets) {
            const rows = (buckets || []).slice(-60);
            const max = Math.max(1, ...rows.map(row => Number(row.total || 0)));
            loadChart.innerHTML = `
                <div class="chart" title="Requests per minute">
                    ${rows.map(row => {
                        const h = Math.max(4, Math.round((Number(row.total || 0) / max) * 110));
                        const cls = Number(row.errors || 0) ? 'bar err' : 'bar';
                        return `<span class="${cls}" style="height:${h}px" title="${fmtTime(row.ts)} — ${escapeHtml(row.total)} req, ${escapeHtml(row.errors)} errors"></span>`;
                    }).join('') || '<div class="muted">Истории пока нет.</div>'}
                </div>
            `;
        }

        function renderLoad(data) {
            const queues = data.queues || [];
            const buckets = data.stats.load_buckets || [];
            const last = buckets[buckets.length - 1] || {};
            const active = queues.filter(row => row.active).length;
            const waiting = sum(queues, 'waiting');
            const errors = Number(last.errors || 0);
            loadNow.textContent = `${active} active / ${waiting} waiting`;
            loadPanel.innerHTML = `
                <div class="grid" style="margin-bottom:0;">
                    <div>
                        <div class="label">Requests this minute</div>
                        <div class="value">${escapeHtml(last.total || 0)}</div>
                        <div class="metric-sub">${escapeHtml(errors)} errors</div>
                    </div>
                    <div>
                        <div class="label">Active backends</div>
                        <div class="value">${escapeHtml(active)}</div>
                        <div class="metric-sub">${escapeHtml(waiting)} waiting</div>
                    </div>
                    <div>
                        <div class="label">Queue rejects</div>
                        <div class="value">${escapeHtml(sum(queues, 'full_total'))}</div>
                        <div class="metric-sub">${escapeHtml(sum(queues, 'timeout_total'))} timeouts</div>
                    </div>
                </div>
            `;
            renderBars(buckets);
        }

        async function loadDashboard() {
            try {
                const res = await fetch('/manager/api/dashboard', { credentials: 'same-origin' });
                if (!res.ok) {
                    if (res.status === 401) {
                        setLocked(true, 'Введите PIN-код.');
                        return;
                    }
                    const text = await res.text();
                    throw new Error(`HTTP ${res.status}: ${text}`);
                }
                const data = await res.json();
                setLocked(false);
                renderDashboard(data);
            } catch (err) {
                summary.innerHTML = `<div class="card error-box">${escapeHtml(err.message)}</div>`;
                backendTable.innerHTML = '';
                modelTable.innerHTML = '';
                recentTable.innerHTML = '';
                queueTable.innerHTML = '';
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
            renderLoad(data);

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

            queueTable.innerHTML = `<table>
                <thead><tr><th>Backend</th><th>Active</th><th>Waiting</th><th>Limit</th><th>Last wait</th><th>Avg wait</th><th>Totals</th></tr></thead>
                <tbody>${(data.queues || []).map(row => `
                    <tr>
                        <td class="mono">${escapeHtml(row.queue_key)}</td>
                        <td>${row.active ? `<span class="tag">${escapeHtml(row.active_model || 'active')}</span> ${escapeHtml(row.active_seconds || 0)}s` : '—'}</td>
                        <td>${escapeHtml(row.waiting)}<div class="queue-meter"><span style="width:${Math.min(100, (Number(row.waiting || 0) / Math.max(1, Number(row.max_queue_size || 1))) * 100)}%"></span></div></td>
                        <td>${escapeHtml(row.max_queue_size)} / ${escapeHtml(row.timeout_seconds)}s</td>
                        <td>${escapeHtml(row.last_wait_ms || 0)} ms</td>
                        <td>${escapeHtml(row.avg_wait_ms || 0)} ms</td>
                        <td class="muted">queued ${escapeHtml(row.queued_total)}, full ${escapeHtml(row.full_total)}, timeout ${escapeHtml(row.timeout_total)}</td>
                    </tr>
                `).join('') || '<tr><td colspan="7" class="muted">Очередей пока нет.</td></tr>'}</tbody>
            </table>`;

            recentTable.innerHTML = `<table>
                <thead><tr><th>Время</th><th>Path</th><th>Model</th><th>Status</th><th>Queue</th><th>Client</th><th>Duration</th><th>Upstream</th></tr></thead>
                <tbody>${(data.stats.recent_requests || []).map(row => `
                    <tr>
                        <td>${fmtTime(row.ts)}</td>
                        <td class="mono">${escapeHtml(row.path)}</td>
                        <td class="mono">${escapeHtml(row.model || '—')}</td>
                        <td>${escapeHtml(row.status)}</td>
                        <td>${row.queue_key ? `${escapeHtml(row.queue_position ?? '—')} / ${escapeHtml(row.queue_wait_ms ?? 0)} ms` : '—'}</td>
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

        pinForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            pinMessage.textContent = 'Проверяю PIN...';
            try {
                const res = await fetch('/manager/api/login', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pin: pinInput.value.trim() }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    pinInput.value = '';
                    pinMessage.textContent = data.message || `HTTP ${res.status}`;
                    return;
                }
                pinInput.value = '';
                setLocked(false);
                loadDashboard();
            } catch (err) {
                pinMessage.textContent = err.message;
            }
        });
        logoutBtn.addEventListener('click', async () => {
            await fetch('/manager/api/logout', { method: 'POST', credentials: 'same-origin' });
            setLocked(true, 'Сессия закрыта.');
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


def _queue_key_for_backend(host: str, port: int, scheme: str) -> str:
    return f"{scheme}://{host}:{port}"


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


def _queue_state(queue_key: str) -> dict:
    state = QUEUE_STATES.get(queue_key)
    if state is None:
        state = {
            "active": False,
            "active_model": None,
            "active_since": None,
            "waiting": deque(),
            "queued_total": 0,
            "completed_total": 0,
            "full_total": 0,
            "timeout_total": 0,
            "last_wait_ms": 0,
            "total_wait_ms": 0,
        }
        QUEUE_STATES[queue_key] = state
    return state


def _acquire_model_slot(queue_key: str, model: str | None, client: str) -> dict:
    started = time.time()
    entry = {
        "token": object(),
        "model": model,
        "client": client,
        "enqueued_at": started,
    }
    with QUEUE_LOCK:
        state = _queue_state(queue_key)
        if not state["active"] and not state["waiting"]:
            state["active"] = True
            state["active_model"] = model
            state["active_since"] = time.time()
            return {
                "ok": True,
                "queue_key": queue_key,
                "initial_position": 0,
                "wait_ms": 0,
            }

        waiting = state["waiting"]
        initial_position = len(waiting) + 1
        if len(waiting) >= MODEL_QUEUE_MAX_SIZE:
            state["full_total"] += 1
            return {
                "ok": False,
                "error": "queue_full",
                "queue_key": queue_key,
                "initial_position": initial_position,
                "wait_ms": 0,
                "waiting": len(waiting),
            }

        waiting.append(entry)
        state["queued_total"] += 1
        deadline = started + MODEL_QUEUE_TIMEOUT

        while True:
            if waiting and waiting[0] is entry and not state["active"]:
                waiting.popleft()
                state["active"] = True
                state["active_model"] = model
                state["active_since"] = time.time()
                wait_ms = int((time.time() - started) * 1000)
                state["last_wait_ms"] = wait_ms
                state["total_wait_ms"] += wait_ms
                return {
                    "ok": True,
                    "queue_key": queue_key,
                    "initial_position": initial_position,
                    "wait_ms": wait_ms,
                }

            remaining = deadline - time.time()
            if remaining <= 0:
                try:
                    waiting.remove(entry)
                except ValueError:
                    pass
                state["timeout_total"] += 1
                QUEUE_LOCK.notify_all()
                return {
                    "ok": False,
                    "error": "queue_timeout",
                    "queue_key": queue_key,
                    "initial_position": initial_position,
                    "wait_ms": int((time.time() - started) * 1000),
                    "waiting": len(waiting),
                }

            QUEUE_LOCK.wait(remaining)


def _release_model_slot(queue_key: str) -> None:
    with QUEUE_LOCK:
        state = _queue_state(queue_key)
        state["active"] = False
        state["active_model"] = None
        state["active_since"] = None
        state["completed_total"] += 1
        QUEUE_LOCK.notify_all()


def _queue_snapshot() -> list[dict]:
    now = time.time()
    with QUEUE_LOCK:
        rows: list[dict] = []
        for queue_key, state in sorted(QUEUE_STATES.items()):
            waiting = list(state["waiting"])
            completed = max(1, int(state["completed_total"]))
            rows.append({
                "queue_key": queue_key,
                "active": bool(state["active"]),
                "active_model": state["active_model"],
                "active_seconds": int(now - state["active_since"]) if state["active_since"] else 0,
                "waiting": len(waiting),
                "waiting_models": [entry.get("model") for entry in waiting[:10]],
                "max_queue_size": MODEL_QUEUE_MAX_SIZE,
                "timeout_seconds": MODEL_QUEUE_TIMEOUT,
                "queued_total": state["queued_total"],
                "completed_total": state["completed_total"],
                "full_total": state["full_total"],
                "timeout_total": state["timeout_total"],
                "last_wait_ms": state["last_wait_ms"],
                "avg_wait_ms": int(state["total_wait_ms"] / completed),
            })
        return rows


def _record_request_stat(
    *,
    path: str,
    status: int,
    model: str | None = None,
    client: str | None = None,
    duration_ms: int | None = None,
    upstream: str | None = None,
    error: str | None = None,
    queue_key: str | None = None,
    queue_position: int | None = None,
    queue_wait_ms: int | None = None,
) -> None:
    ts = time.time()
    bucket_ts = int(ts // 60) * 60
    with STATS_LOCK:
        GATEWAY_STATS["total_requests"] += 1
        _inc_counter(GATEWAY_STATS["route_counts"], path)
        _inc_counter(GATEWAY_STATS["status_counts"], str(status))
        if model:
            _inc_counter(GATEWAY_STATS["model_counts"], model)
        GATEWAY_STATS["last_request_ts"] = ts
        load_buckets = GATEWAY_STATS["load_buckets"]
        bucket = load_buckets.setdefault(bucket_ts, {
            "ts": bucket_ts,
            "total": 0,
            "ok": 0,
            "errors": 0,
            "queued": 0,
            "queue_wait_ms": 0,
        })
        bucket["total"] += 1
        if status < 400:
            bucket["ok"] += 1
        else:
            bucket["errors"] += 1
        if queue_key:
            bucket["queued"] += 1
            bucket["queue_wait_ms"] += int(queue_wait_ms or 0)
        for old_ts in sorted(load_buckets)[:-180]:
            del load_buckets[old_ts]
        recent = GATEWAY_STATS["recent_requests"]
        recent.append({
            "ts": ts,
            "path": path,
            "status": status,
            "model": model,
            "client": client,
            "duration_ms": duration_ms,
            "upstream": upstream,
            "queue_key": queue_key,
            "queue_position": queue_position,
            "queue_wait_ms": queue_wait_ms,
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
            "load_buckets": [dict(row) for _, row in sorted(GATEWAY_STATS["load_buckets"].items())],
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
        "queues": _queue_snapshot(),
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

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _manager_cookie_token(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "gateway_manager_session" and value:
                return value
        return None

    def _manager_authorized(self) -> bool:
        if self._authorized():
            return True
        token = self._manager_cookie_token()
        if not token:
            return False
        now = time.time()
        with MANAGER_AUTH_LOCK:
            session = MANAGER_SESSIONS.get(token)
            if not session:
                return False
            if now - session["created_at"] > MANAGER_SESSION_TTL:
                del MANAGER_SESSIONS[token]
                return False
            session["last_seen_at"] = now
            return True

    def _handle_manager_login(self):
        started = time.time()
        ip = self._client_ip()
        now = time.time()
        with MANAGER_AUTH_LOCK:
            failures = MANAGER_PIN_FAILURES.setdefault(ip, {"count": 0, "banned_until": 0})
            banned_until = float(failures.get("banned_until") or 0)
            if banned_until > now:
                retry_after = int(banned_until - now)
                self._send_json(429, {
                    "error": "pin_banned",
                    "message": "Слишком много неверных PIN. Доступ временно заблокирован.",
                    "retry_after_seconds": retry_after,
                }, headers={"Retry-After": str(retry_after)})
                _record_request_stat(
                    path="/manager/api/login",
                    status=429,
                    client=f"pin:{ip}",
                    duration_ms=int((time.time() - started) * 1000),
                    upstream="embedded-ui",
                    error="pin_banned",
                )
                return

        body = self._read_request_body()
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}
        pin = str(payload.get("pin", ""))

        if not secrets.compare_digest(pin, MANAGER_PIN):
            with MANAGER_AUTH_LOCK:
                failures = MANAGER_PIN_FAILURES.setdefault(ip, {"count": 0, "banned_until": 0})
                failures["count"] = int(failures.get("count") or 0) + 1
                remaining = max(0, MANAGER_PIN_MAX_ATTEMPTS - failures["count"])
                headers = {}
                status = 401
                error = "invalid_pin"
                if failures["count"] >= MANAGER_PIN_MAX_ATTEMPTS:
                    failures["banned_until"] = time.time() + MANAGER_PIN_BAN_SECONDS
                    headers["Retry-After"] = str(MANAGER_PIN_BAN_SECONDS)
                    status = 429
                    error = "pin_banned"
                self._send_json(status, {
                    "error": error,
                    "message": (
                        "Неверный PIN. Доступ заблокирован на 30 минут."
                        if status == 429 else
                        "Неверный PIN. Повторите ввод."
                    ),
                    "remaining_attempts": remaining,
                    "ban_seconds": MANAGER_PIN_BAN_SECONDS if status == 429 else 0,
                }, headers=headers)
            _record_request_stat(
                path="/manager/api/login",
                status=status,
                client=f"pin:{ip}",
                duration_ms=int((time.time() - started) * 1000),
                upstream="embedded-ui",
                error=error,
            )
            return

        token = secrets.token_urlsafe(32)
        with MANAGER_AUTH_LOCK:
            MANAGER_PIN_FAILURES.pop(ip, None)
            MANAGER_SESSIONS[token] = {
                "created_at": time.time(),
                "last_seen_at": time.time(),
                "ip": ip,
            }
        self._send_json(200, {
            "ok": True,
            "message": "Доступ открыт.",
            "session_ttl_seconds": MANAGER_SESSION_TTL,
        }, headers={
            "Set-Cookie": (
                "gateway_manager_session="
                f"{token}; Path=/manager; Max-Age={MANAGER_SESSION_TTL}; HttpOnly; SameSite=Lax"
            )
        })
        _record_request_stat(
            path="/manager/api/login",
            status=200,
            client=f"pin:{ip}",
            duration_ms=int((time.time() - started) * 1000),
            upstream="embedded-ui",
        )

    def _handle_manager_logout(self):
        started = time.time()
        token = self._manager_cookie_token()
        if token:
            with MANAGER_AUTH_LOCK:
                MANAGER_SESSIONS.pop(token, None)
        self._send_json(200, {"ok": True}, headers={
            "Set-Cookie": "gateway_manager_session=; Path=/manager; Max-Age=0; HttpOnly; SameSite=Lax"
        })
        _record_request_stat(
            path="/manager/api/logout",
            status=200,
            client=f"pin:{self._client_ip()}",
            duration_ms=int((time.time() - started) * 1000),
            upstream="embedded-ui",
        )

    # ---- Helpers --------------------------------------------------------

    def _send_json(self, code: int, payload: dict, headers: dict[str, str] | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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
        if not self._manager_authorized():
            self._send_json(401, {
                "error": "unauthorized",
                "message": "Введите PIN-код для доступа к панели gateway.",
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

        if self.command == "POST" and path_only == "/manager/api/login":
            self._handle_manager_login()
            return

        if self.command == "POST" and path_only == "/manager/api/logout":
            self._handle_manager_logout()
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

        # 5. Wait for this physical backend's single active slot.
        queue_key = _queue_key_for_backend(host, port, scheme)
        queue_info = _acquire_model_slot(queue_key, effective_model, self._client_label())
        if not queue_info.get("ok"):
            error_code = queue_info["error"]
            if error_code == "queue_full":
                payload = {
                    "error": "queue_full",
                    "message": (
                        "Модель сейчас занята, а очередь заполнена. "
                        "Повторите запрос позже или уменьшите параллелизм клиента."
                    ),
                    "model": effective_model,
                    "queue_key": queue_key,
                    "queue_position": queue_info["initial_position"],
                    "max_queue_size": MODEL_QUEUE_MAX_SIZE,
                    "retry_after_seconds": int(MODEL_QUEUE_TIMEOUT),
                }
            else:
                payload = {
                    "error": "queue_timeout",
                    "message": (
                        "Запрос слишком долго ожидал свободного слота модели. "
                        "Повторите запрос позже или уменьшите параллелизм клиента."
                    ),
                    "model": effective_model,
                    "queue_key": queue_key,
                    "queue_position": queue_info["initial_position"],
                    "waited_seconds": round(queue_info["wait_ms"] / 1000, 3),
                    "timeout_seconds": MODEL_QUEUE_TIMEOUT,
                }
            self._send_json(503, payload, headers={"Retry-After": str(int(MODEL_QUEUE_TIMEOUT))})
            _record_request_stat(
                path=path_only,
                status=503,
                model=effective_model,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream=f"{scheme}://{host}:{port}",
                error=error_code,
                queue_key=queue_key,
                queue_position=queue_info["initial_position"],
                queue_wait_ms=queue_info["wait_ms"],
            )
            return

        # 6. Send request to vLLM backend
        conn = _make_connection(host, port, timeout=3600, scheme=scheme)
        slot_acquired = True
        resp = None
        fwd = self._fwd_headers(host, port, body)
        try:
            conn.request(self.command, self.path, body=body or None, headers=fwd)
            resp = conn.getresponse()
        except Exception as exc:
            self._send_json(502, {"error": "backend_unreachable", "message": str(exc)})
            conn.close()
            if slot_acquired:
                _release_model_slot(queue_key)
            _record_request_stat(
                path=path_only,
                status=502,
                model=effective_model,
                client=self._client_label(),
                duration_ms=int((time.time() - started) * 1000),
                upstream=f"{scheme}://{host}:{port}",
                error=str(exc),
                queue_key=queue_key,
                queue_position=queue_info["initial_position"],
                queue_wait_ms=queue_info["wait_ms"],
            )
            return

        # 7. Detect streaming response (SSE)
        content_type = resp.getheader("Content-Type", "")
        is_stream    = "text/event-stream" in content_type

        # 8. Relay response headers
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in HOP_BY_HOP:
                continue
            if name.lower() == "content-length" and is_stream:
                continue
            self.send_header(name, value)
        self.send_header("X-Gateway-Queue-Key", queue_key)
        self.send_header("X-Gateway-Queue-Initial-Position", str(queue_info["initial_position"]))
        self.send_header("X-Gateway-Queue-Wait-Ms", str(queue_info["wait_ms"]))
        if is_stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        # 9. Relay body
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
            if resp is not None:
                resp.close()
            conn.close()
            if slot_acquired:
                _release_model_slot(queue_key)

        _record_request_stat(
            path=path_only,
            status=resp.status,
            model=effective_model,
            client=self._client_label(),
            duration_ms=int((time.time() - started) * 1000),
            upstream=f"{scheme}://{host}:{port}",
            error=None if resp.status < 400 else f"upstream_http_{resp.status}",
            queue_key=queue_key,
            queue_position=queue_info["initial_position"],
            queue_wait_ms=queue_info["wait_ms"],
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

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
import time
import urllib.parse
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
    _bmap: dict[str, tuple[str, int]] = {}
    for _name, _hostport in json.loads(_BACKENDS_RAW).items():
        _h, _p = _hostport.rsplit(":", 1)
        _bmap[_name] = (_h, int(_p))
else:
    # Fallback: legacy single-backend env
    _url   = os.environ.get("UNIVERSAL_CHAT_URL_HIGH", "http://127.0.0.1:8000")
    _ps    = urllib.parse.urlparse(_url)
    _bmap  = {"universal-nemotron": (_ps.hostname or "127.0.0.1", _ps.port or 8000)}

BACKENDS: dict[str, tuple[str, int]] = _bmap
# Unique backend addresses in insertion order (for /v1/models fan-out)
ALL_BACKENDS: list[tuple[str, int]] = list(dict.fromkeys(BACKENDS.values()))

# Primary backend: explicit override or first entry
_PRIMARY_RAW = os.environ.get("VLLM_PRIMARY", "")
if _PRIMARY_RAW:
    _ph, _pp      = _PRIMARY_RAW.rsplit(":", 1)
    PRIMARY_BACKEND: tuple[str, int] = (_ph, int(_pp))
else:
    PRIMARY_BACKEND = next(iter(BACKENDS.values()))

_REWRITES_RAW = os.environ.get("MODEL_REWRITES", "")
MODEL_REWRITES: dict[str, str] = json.loads(_REWRITES_RAW) if _REWRITES_RAW else {}

_IMAGE_BACKEND_RAW = os.environ.get("IMAGE_BACKEND_URL", "").strip()
if _IMAGE_BACKEND_RAW:
    _image_parts = urllib.parse.urlparse(_IMAGE_BACKEND_RAW)
    IMAGE_BACKEND: tuple[str, int, str] | None = (
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

def _backend_for_model(model_name: str | None) -> tuple[str, int]:
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


def _fetch_models(host: str, port: int, timeout: float = 5.0) -> list:
    """Fetch /v1/models from a backend, return the data list (empty on error)."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
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

    discovered: dict[str, tuple[str, int]] = {}
    for host, port in ALL_BACKENDS:
        for entry in _fetch_models(host, port):
            model_id = _normalize_model_name(entry.get("id"))
            if model_id:
                discovered.setdefault(model_id, (host, port))

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

    def _fwd_headers(self, host: str, port: int, body: bytes) -> dict[str, str]:
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        fwd["Host"] = f"{host}:{port}"
        if "Expect" in fwd:
            del fwd["Expect"]
        if body:
            fwd["Content-Length"] = str(len(body))
        elif "Content-Length" in fwd:
            del fwd["Content-Length"]
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

    def _handle_models(self):
        """Expose only the four visible fast/thinking variants for Gemma and Qwen 3.6."""
        merged: list = []
        base_entries: dict[str, dict] = {}
        for host, port in ALL_BACKENDS:
            for entry in _fetch_models(host, port):
                mid = _normalize_model_name(entry.get("id", ""))
                if mid in VISIBLE_BASE_MODELS and mid not in base_entries:
                    base_entries[mid] = entry
        # Add only virtual thinking/fast variants.
        for virtual_id, (base_id, enable_thinking) in MODEL_VARIANTS.items():
            if virtual_id not in VISIBLE_VARIANT_IDS:
                continue
            base = base_entries.get(base_id, {"id": base_id, "object": "model", "owned_by": "llamacpp"})
            entry = dict(base)
            entry["id"] = virtual_id
            merged.append(entry)
        self._send_json(200, {"object": "list", "data": merged})

    def _proxy_image_request(self):
        if IMAGE_BACKEND is None:
            self._send_json(404, {"error": "not_found", "message": "Image backend is not configured"})
            return

        host, port, base_path = IMAGE_BACKEND
        raw_path = self.path.rstrip("/")
        if raw_path == "/zimage/health":
            upstream_path = f"{base_path}/health"
        elif raw_path == "/zimage/generate":
            if not self._authorized():
                self._send_json(401, {
                    "error": "unauthorized",
                    "message": "Invalid or missing Bearer token",
                })
                return
            upstream_path = f"{base_path}/v1/generate"
        elif raw_path == "/v1/images/generations":
            if not self._authorized():
                self._send_json(401, {
                    "error": "unauthorized",
                    "message": "Invalid or missing Bearer token",
                })
                return
            upstream_path = f"{base_path}/v1/images/generations"
        elif self.path.startswith("/zimage/images/"):
            upstream_path = f"{base_path}{self.path[len('/zimage'):] }"
        else:
            self._send_json(404, {"error": "not_found", "message": "Unknown zimage route"})
            return

        body = self._read_request_body()
        if raw_path == "/v1/images/generations":
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
        if IMAGE_BACKEND_TOKEN and raw_path in {"/zimage/generate", "/v1/images/generations"}:
            headers["Authorization"] = f"Bearer {IMAGE_BACKEND_TOKEN}"

        conn = http.client.HTTPConnection(host, port, timeout=3600)
        try:
            conn.request(self.command, upstream_path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            self._send_json(502, {"error": "image_backend_unreachable", "message": str(exc)})
            conn.close()
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
        self.close_connection = True

    # ---- Core proxy logic -----------------------------------------------

    def _proxy(self):
        if self.path.startswith("/zimage/") or self.path.rstrip("/") == "/v1/images/generations":
            self._proxy_image_request()
            return

        # 1. Token auth
        if not self._authorized():
            self._send_json(401, {
                "error": "unauthorized",
                "message": "Invalid or missing Bearer token",
            })
            return

        # 2. Special case: GET /v1/models — merge from all backends
        if self.command == "GET" and self.path.rstrip("/") == "/v1/models":
            self._handle_models()
            return

        # 3. Read request body (if any)
        body = self._read_request_body()

        # 4. Pick backend by model name from body
        model_name = _extract_model(body)
        host, port = _backend_for_model(model_name)

        # Log detailed request info for analysis
        if self.path.rstrip("/").endswith("chat/completions"):
            self.log_request_detail(model_name, port, body)

        # 4a. Rewrite legacy/external alias model names if needed.
        body = _rewrite_model_in_body(body, model_name)

        # 4b. Inject thinking kwargs and handle virtual variants
        # Virtual variants (-thinking / -fast): rewrite model field + inject enable_thinking.
        # THINKING_BUDGET == -1 → no injection for non-variant requests
        # THINKING_BUDGET ==  0 → disable thinking
        # THINKING_BUDGET  > 0 → set thinking_budget=N
        effective_model = _effective_model_name(body, model_name)
        needs_think_embed = False  # whether to rewrite reasoning_content into content
        if (
            body
            and self.path.rstrip("/").endswith("chat/completions")
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
        conn = http.client.HTTPConnection(host, port, timeout=3600)
        fwd = self._fwd_headers(host, port, body)
        try:
            conn.request(self.command, self.path, body=body or None, headers=fwd)
            resp = conn.getresponse()
        except Exception as exc:
            self._send_json(502, {"error": "backend_unreachable", "message": str(exc)})
            conn.close()
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
    summary = ", ".join(f"{n}→{h}:{p}" for n, (h, p) in BACKENDS.items())
    server  = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), GatewayHandler)
    print(
        f"[gateway] {LISTEN_HOST}:{LISTEN_PORT}  "
        f"backends=[{summary}]  "
        f"primary={PRIMARY_BACKEND[0]}:{PRIMARY_BACKEND[1]}  "
        f"auth={'token' if TOKEN else 'DISABLED'}",
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

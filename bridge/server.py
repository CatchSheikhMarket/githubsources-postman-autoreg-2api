#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""postman2api — мост Postman AI (Agent Mode) → OpenAI / Anthropic API.

Чистый Python: без Go, без сборки exe. Запуск — `python3 server.py`.

Что делает:
  * держит пул веб-сессий Postman (`cookie:<postman.sid>`) из sessions.txt;
  * ротирует их по кругу, отбитые 401 исключает до перезапуска;
  * пересобирает SSE апстрима в OpenAI- и Anthropic-совместимые ответы;
  * отдаёт квоту аккаунта в `_postman_usage` каждого ответа;
  * поддерживает tools (clientTools.thirdParty) и стриминг.

Особенности апстрима, которые ломают наивный мост:
  * авторизация — `x-access-token` (не Bearer) либо кука `postman.sid`;
  * ответ всегда SSE, даже когда стрим клиенту не нужен;
  * `input.query` ограничен ~10 000 символов — это лимит поля, а не контекст;
    system уходит отдельно в `backgroundContext.AGENTS_MD`, история обрезается;
  * платформа, product и хэши инструментов — часть контракта и привязаны к
    версии Postman.

Порт по умолчанию 12049, слушает 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("нужен requests: pip install requests")

HERE = Path(__file__).resolve().parent


# ---------- конфиг ----------

def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


load_env(HERE / ".env")
_cfg_json = load_json(HERE / "config.json")


def cfg(key: str, default: str) -> str:
    return os.environ.get(key) or str(_cfg_json.get(key) or default)


HOST = cfg("HOST", "127.0.0.1")
PORT = int(cfg("PORT", "12049"))
AUTH_TOKEN = cfg("AUTH_TOKEN", "postman2api")
BASE_URL = cfg("POSTMAN_API_BASE", "https://gateway.postman.com").rstrip("/")
SESSIONS_FILE = HERE / cfg("SESSIONS_FILE", "sessions.txt")
DEFAULT_MODEL = cfg("DEFAULT_MODEL", "gpt-6.1-sol")
TIMEOUT = int(cfg("TIMEOUT", "300"))
APP_VERSION = cfg("POSTMAN_APP_VERSION", "12.26.4")
PLATFORM = cfg("POSTMAN_PLATFORM", "DESKTOP_LINUX")
PRODUCT = cfg("POSTMAN_PRODUCT", "workspace_localmode_v12")
PRODUCT_TOOLS = cfg("POSTMAN_PRODUCT_TOOLS", "workspace_tools_sdk_localmode")
WORKSPACE_ID = cfg("POSTMAN_WORKSPACE_ID", "proxy-workspace")
MAX_QUERY = int(cfg("MAX_QUERY_BYTES", "9800"))

# Хэши выдаются сервером Postman и привязаны к версии приложения.
# При TOOL_VALIDATION_ERROR перехвати свежие значения и задай в .env.
TOOLS_HASH = cfg("POSTMAN_TOOLS_HASH",
                 "clienttools-workspace_localmode_v12-desktop-darwin-12.23.1-ui-260811-0231-828d6b3ed37b")
TERMS_HASH = cfg("POSTMAN_TERMS_HASH",
                 "kbterms-workspace_localmode_v12-desktop-darwin-12.23.1-ui-260811-0231-dbc2c7575e92")
TOOLS_HASH_SDK = cfg("POSTMAN_TOOLS_HASH_SDK",
                     "clienttools-workspace_tools_sdk_localmode-desktop-darwin-12.23.1-ui-260811-0231-e23d250ee33a")
TERMS_HASH_SDK = cfg("POSTMAN_TERMS_HASH_SDK",
                     "kbterms-workspace_tools_sdk_localmode-desktop-darwin-12.23.1-ui-260811-0231-dbc2c7575e92")


# ---------- каталог моделей ----------

# alias → (enum-ключ Postman, человекочитаемое имя)
MODELS: list[tuple[str, str, str]] = [
    ("gpt-6.1-sol", "GPT_61_SOL", "GPT-6.1 Sol"),
    ("gpt-5.6-sol", "GPT_56_SOL", "GPT-5.6 Sol"),
    ("gpt-5.6-terra", "GPT_56_TERRA", "GPT-5.6 Terra"),
    ("gpt-5.6-luna", "GPT_56_LUNA", "GPT-5.6 Luna"),
    ("gpt-5.5", "GPT_55", "GPT-5.5"),
    ("gpt-5.4", "GPT_54", "GPT-5.4"),
    ("claude-opus-4.8", "CLAUDE_OPUS_48_BEDROCK", "Claude Opus 4.8"),
    ("claude-opus-4.7", "CLAUDE_OPUS_47_BEDROCK", "Claude Opus 4.7"),
    ("claude-opus-4.5", "CLAUDE_OPUS_45_BEDROCK", "Claude Opus 4.5"),
    ("claude-sonnet-4.6", "CLAUDE_46_SONNET_BEDROCK", "Claude Sonnet 4.6"),
    ("claude-sonnet-4.5", "CLAUDE_45_SONNET_BEDROCK", "Claude Sonnet 4.5"),
    ("claude-haiku-4.5", "CLAUDE_45_HAIKU_BEDROCK", "Claude Haiku 4.5"),
]

SHORT_ALIASES = {
    "gpt-6.1": "GPT_61_SOL",
    "gpt-5.6": "GPT_56_SOL",
    "gpt-5": "GPT_54",
    "opus-4.8": "CLAUDE_OPUS_48_BEDROCK",
    "opus-4.7": "CLAUDE_OPUS_47_BEDROCK",
    "opus-4.5": "CLAUDE_OPUS_45_BEDROCK",
    "sonnet-4.6": "CLAUDE_46_SONNET_BEDROCK",
    "sonnet-4.5": "CLAUDE_45_SONNET_BEDROCK",
    "haiku-4.5": "CLAUDE_45_HAIKU_BEDROCK",
}


def resolve_model(name: str) -> str:
    n = (name or "").strip()
    if not n:
        n = DEFAULT_MODEL
    low = n.lower()
    for alias, key, _ in MODELS:
        if low == alias or n == key:
            return key
    if low in SHORT_ALIASES:
        return SHORT_ALIASES[low]
    if n.isupper() and 4 <= len(n) <= 44 and n.replace("_", "").isalnum():
        return n
    return MODELS[0][1]


def display_name(key: str) -> str:
    for _, k, name in MODELS:
        if k == key:
            return name
    return key


# ---------- пул сессий ----------

class SessionPool:
    """Сессии Postman: `cookie:<sid>` — веб-сессия, иначе access_token."""

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._sessions: list[tuple[str, bool]] = []   # (value, is_cookie)
        self._invalid: set[str] = set()
        self._next = 0
        self._load(path)

    def _load(self, path: Path) -> None:
        if not path.is_file():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            self.add(line)

    def add(self, line: str) -> None:
        line = (line or "").strip()
        if not line or line.startswith("#"):
            return
        if line.lower().startswith("cookie:"):
            item = (line[7:].strip(), True)
        else:
            item = (line, False)
        if item[0] and item not in self._sessions:
            self._sessions.append(item)

    def next(self) -> tuple[str, bool] | None:
        with self._lock:
            n = len(self._sessions)
            for _ in range(n):
                value, cookie = self._sessions[self._next % n]
                self._next = (self._next + 1) % n
                if value not in self._invalid:
                    return value, cookie
            return None

    def retire(self, value: str) -> None:
        with self._lock:
            self._invalid.add(value)

    def stats(self) -> tuple[int, int]:
        with self._lock:
            alive = sum(1 for v, _ in self._sessions if v not in self._invalid)
            return alive, len(self._sessions)


POOL = SessionPool(SESSIONS_FILE)


# ---------- запрос к Postman ----------

def build_query(turns: list[tuple[str, str]], max_bytes: int) -> str:
    """Собирает input.query: свежая история + последний user-ход, с обрезкой."""
    last_user = ""
    hist_end = len(turns)
    for i in range(len(turns) - 1, -1, -1):
        if turns[i][0] == "user":
            last_user = turns[i][1].strip()
            hist_end = i
            break

    budget = max_bytes - len(last_user) - 100
    if budget < 200:
        budget = 200

    lines = [f"{role}: {text.strip()}" for role, text in turns[:hist_end] if text.strip()]
    parts: list[str] = []
    used = 0
    omitted = False
    for line in reversed(lines):
        need = len(line) + 2
        if used + need > budget:
            omitted = True
            break
        parts.insert(0, line)
        used += need

    out = ""
    if parts:
        if omitted:
            out += "[...earlier messages omitted...]\n\n"
        out += "\n\n".join(parts) + "\n\n"

    if last_user:
        remain = max_bytes - len(out)
        if remain < 500:
            remain = 500
        out += truncate(last_user, remain)

    return out.strip() or "Hello"


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    while cut and not cut.encode("utf-8", "ignore").decode("utf-8", "ignore") == cut:
        cut = cut[:-1]
    return cut + "…"


def encode_tool_name(name: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)[:48]
    return "client__" + safe


def decode_tool_name(encoded: str, names: dict[str, str]) -> str:
    if encoded.startswith("client__"):
        return names.get(encoded, encoded[len("client__"):])
    return names.get(encoded, encoded)


def build_body(model_key: str, query: str, system: str, conv_id: str | None,
               tools: list[dict] | None, tool_results: list[dict] | None,
               tool_group: str | None) -> dict:
    use_tools = bool(tools) or bool(tool_results)
    product = PRODUCT_TOOLS if use_tools else PRODUCT
    tools_hash = TOOLS_HASH_SDK if use_tools else TOOLS_HASH
    terms_hash = TERMS_HASH_SDK if use_tools else TERMS_HASH

    background: list[dict] = [
        {"type": "ACTIVE_ENVIRONMENT", "value": None},
        {"type": "ACTIVE_WORKSPACE", "value": {"name": "Proxy Workspace", "id": WORKSPACE_ID}},
    ]
    if system:
        background.append({"type": "AGENTS_MD", "value": {"agentsMdFileContent": system}})

    inp: dict = {
        "chatType": "USER_QUERY",
        "query": query,
        "toolResponse": "",
        "useCase": None,
        "conversationId": conv_id,
        "agent": None,
        "product": product,
        "startedFrom": "CHAT_INPUT",
    }

    if tool_results:
        inp.pop("startedFrom", None)
        inp["chatType"] = "TOOL_RESPONSE"
        inp["query"] = ""
        if tool_group:
            responses = []
            for r in tool_results:
                item = {
                    "toolCallId": r["call_id"],
                    "content": r.get("content") or "",
                    "toolResponseSummary": (r.get("summary") or r.get("content") or "")[:200],
                    "toolResponseStatus": r.get("status") or "SUCCESS",
                }
                if item["toolResponseStatus"] == "REJECTED":
                    item["toolResponseRejectionType"] = "EXPLICIT"
                responses.append(item)
            inp["toolCallGroupId"] = tool_group
            inp["toolResponses"] = responses
        else:
            r = tool_results[0]
            inp["toolCallId"] = r["call_id"]
            inp["toolResponse"] = r.get("content") or ""
            inp["toolResponseSummary"] = (r.get("summary") or r.get("content") or "")[:200]
            inp["toolResponseStatus"] = r.get("status") or "SUCCESS"

    client_tools: dict = {
        "native": [],
        "excludedTools": ["askUser"],
        "nativeToolsHash": tools_hash,
    }
    if tools:
        defs = []
        for t in tools:
            defs.append({
                "name": encode_tool_name(t["name"]),
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            })
        client_tools["thirdParty"] = {"external-client": {"tools": defs}}

    return {
        "input": inp,
        "platform": PLATFORM,
        "clientTools": client_tools,
        "clientKBTerms": {"nativeTermsHash": terms_hash, "excludedKBTerms": []},
        "mandatoryContext": {"workspaceId": WORKSPACE_ID},
        "selectedContext": [],
        "backgroundContext": background,
        "devModeOptions": {
            "selectedModel": model_key,
            "isParallelToolCallingSupported": True,
            "autoRun": False,
            "supportsAskUser": False,
            "supportsActionRecommendations": True,
            "useThinkingModeIfAvailable": True,
            "thinkingLevel": "medium",
            "isAgentModeEnabled": True,
        },
    }


def upstream_headers(value: str, cookie: bool) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-pstmn-req-service": "agent-mode-service",
        "x-app-version": APP_VERSION,
        "Origin": "https://desktop.postman.com",
        "Referer": "https://desktop.postman.com/",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Postman/" + APP_VERSION +
                       " Electron/37.10.3 Safari/537.36"),
    }
    if cookie:
        h["Cookie"] = "postman.sid=" + value
    else:
        h["x-access-token"] = value
    return h


class UpstreamError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"upstream {status}: {body[:200]}")


def sse_events(resp) -> list[dict]:
    """Читает поток и отдаёт события Postman (`eventType` + `data`)."""
    out = []
    buf = ""
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw == "":
            payload = buf.strip()
            buf = ""
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("eventType"):
                out.append(ev)
            continue
        if raw.startswith("data:"):
            if buf:
                buf += "\n"
            buf += raw[5:].strip()
    payload = buf.strip()
    if payload and payload != "[DONE]":
        try:
            ev = json.loads(payload)
            if isinstance(ev, dict) and ev.get("eventType"):
                out.append(ev)
        except ValueError:
            pass
    return out


def call_upstream(body: dict, stream_cb=None) -> dict:
    """Отправляет запрос, перебирая сессии. Возвращает накопленный результат."""
    total = POOL.stats()[1]
    if total == 0:
        raise UpstreamError(503, "нет сессий Postman: заполни sessions.txt")

    last_err: UpstreamError | None = None
    for _ in range(total):
        sess = POOL.next()
        if not sess:
            break
        value, cookie = sess
        try:
            resp = requests.post(BASE_URL + "/chat", json=body,
                                 headers=upstream_headers(value, cookie),
                                 stream=True, timeout=(15, TIMEOUT))
        except requests.RequestException as exc:
            last_err = UpstreamError(502, f"сеть: {exc}")
            continue

        if resp.status_code == 401:
            POOL.retire(value)
            resp.close()
            continue
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            last_err = UpstreamError(resp.status_code, resp.text[:1000])
            resp.close()
            continue
        if resp.status_code != 200:
            last_err = UpstreamError(resp.status_code, resp.text[:1000])
            resp.close()
            break

        return collect_events(resp, stream_cb)

    raise last_err or UpstreamError(502, "все аккаунты отбиты")


def collect_events(resp, stream_cb=None) -> dict:
    acc = {
        "text": "",
        "thinking": "",
        "conv_id": "",
        "model_key": "",
        "usage": None,
        "failure": "",
        "tool_calls": [],
    }
    names = {}
    for ev in sse_events(resp):
        etype = ev.get("eventType")
        data = ev.get("data") or {}
        if etype == "textChunk":
            chunk = data.get("textContent") or ""
            if chunk and not data.get("switchAgentSeparator"):
                acc["text"] += chunk
                if stream_cb and not stream_cb({"type": "text", "value": chunk}):
                    break
        elif etype == "thinkingChunk":
            chunk = data.get("thinkingContent") or data.get("thinking") or ""
            if chunk:
                acc["thinking"] += chunk
                if stream_cb:
                    stream_cb({"type": "thinking", "value": chunk})
        elif etype == "conversation":
            if data.get("id"):
                acc["conv_id"] = data["id"]
            if data.get("modelKey"):
                acc["model_key"] = data["modelKey"]
        elif etype in ("toolCall", "toolCallChunk"):
            for tc in (data.get("toolCalls") or []):
                _merge_tool_call(acc["tool_calls"], tc, etype == "toolCallChunk", names)
        elif etype == "usage":
            acc["usage"] = data
        elif etype == "failure":
            acc["failure"] = (data.get("userMessage") or data.get("message")
                              or data.get("errorType") or "unknown failure")
            break
    for tc in acc["tool_calls"]:
        tc["name"] = decode_tool_name(tc["name"], names)
    resp.close()
    return acc


def _merge_tool_call(acc: list[dict], tc: dict, chunk: bool, names: dict) -> None:
    call_id = tc.get("id") or tc.get("toolCallId") or ""
    fn = tc.get("function") or {}
    name = fn.get("name") or tc.get("name") or ""
    args = fn.get("arguments", tc.get("arguments", ""))
    if isinstance(args, dict):
        args = json.dumps(args, ensure_ascii=False)
    if not isinstance(args, str):
        args = ""

    existing = next((x for x in acc if x.get("id") and x["id"] == call_id), None)
    if existing is None:
        existing = {"id": call_id or ("call_" + uuid.uuid4().hex[:12]),
                    "name": name, "arguments": ""}
        acc.append(existing)
    if name and not existing["name"]:
        existing["name"] = name
    if name:
        names[encode_tool_name(_plain(name))] = _plain(name)
    existing["arguments"] += args if chunk else (args or "")
    if not chunk and not existing["arguments"]:
        existing["arguments"] = args


def _plain(name: str) -> str:
    return name[len("client__"):] if name.startswith("client__") else name


# ---------- разбор запроса клиента ----------

def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def parse_openai_tools(tools) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict):
            fn = t if isinstance(t, dict) else {}
        name = fn.get("name") or (t.get("name") if isinstance(t, dict) else "")
        if not name:
            continue
        out.append({
            "name": name,
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def closing_tool_results(messages: list[dict]) -> list[dict]:
    """Собирает хвостовой блок role=tool — результаты вызовов."""
    results = []
    for m in reversed(messages):
        if (m.get("role") or "").lower() == "tool":
            results.insert(0, {
                "call_id": m.get("tool_call_id") or "",
                "content": text_of(m.get("content")),
                "status": "SUCCESS",
            })
        elif results:
            break
    return [r for r in results if r["call_id"]]


def to_postman_request(req: dict) -> dict:
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    messages = req.get("messages") or []
    results = closing_tool_results(messages)
    closing_ids = {r["call_id"] for r in results}

    for m in messages:
        role = (m.get("role") or "").lower()
        text = text_of(m.get("content"))
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif role == "tool":
            if m.get("tool_call_id") in closing_ids:
                continue
            if text:
                turns.append(("tool", text))
        else:
            if text:
                turns.append((role or "user", text))

    system = "\n\n".join(system_parts)
    query = build_query(turns, MAX_QUERY)
    model_key = resolve_model(req.get("model") or "")

    body = build_body(model_key, query, system,
                      req.get("_postman_conversation_id") or req.get("conversation_id"),
                      parse_openai_tools(req.get("tools")),
                      results or None, None)
    return {"body": body, "query": query, "model_key": model_key, "tool_results": results}


def oai_tool_calls(calls: list[dict]) -> list[dict]:
    return [{
        "id": c["id"],
        "type": "function",
        "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
    } for c in calls]


def postman_usage(data: dict | None) -> dict | None:
    if not data:
        return None
    return {
        "usage": data.get("usage", 0),
        "limit": data.get("limit", 0),
        "usage_state": data.get("usageState", ""),
        "user_type": data.get("userType", ""),
    }


# ---------- HTTP-сервер ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "postman2api-py"

    def log_message(self, fmt, *args):  # тише в консоли
        pass

    # -- helpers --
    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": {"message": msg, "type": "invalid_request_error"}})

    def _auth_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            token = self.headers.get("x-api-key", "").strip()
        return token == AUTH_TOKEN

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 32 << 20:
                return None
            return json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return None

    # -- routes --
    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            return self._json(200, {"ok": True})
        if self.path == "/status":
            alive, total = POOL.stats()
            return self._json(200, {"sessions_alive": alive, "sessions_total": total,
                                    "default_model": DEFAULT_MODEL})
        if self.path == "/v1/models":
            if not self._auth_ok():
                return self._err(401, "invalid auth token")
            created = int(time.time())
            default_key = resolve_model("")
            data = []
            for alias, key, name in MODELS:
                data.append({
                    "id": alias, "object": "model", "created": created,
                    "owned_by": "postman", "name": name, "postman_key": key,
                    "default": key == default_key,
                })
            return self._json(200, {"object": "list", "data": data})
        if self.path == "/":
            alive, total = POOL.stats()
            html = (f"<html><body style='font-family:sans-serif;background:#111;color:#eee;padding:20px'>"
                    f"<h2>postman2api</h2><p>Порт {PORT}</p>"
                    f"<p>Сессий живых: {alive} из {total}</p>"
                    f"<p>Модель по умолчанию: <code>{DEFAULT_MODEL}</code></p>"
                    f"</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        return self._err(404, "not found")

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/v1/messages"):
            return self._err(404, "not found")
        if not self._auth_ok():
            return self._err(401, "invalid auth token (нужен Bearer или x-api-key)")

        req = self._body()
        if not isinstance(req, dict):
            return self._err(400, "cannot parse request")
        if not req.get("messages"):
            return self._err(400, "messages is required")

        if self.path == "/v1/messages":
            return self._anthropic(req)

        pm = to_postman_request(req)
        stream = bool(req.get("stream"))
        model = req.get("model") or DEFAULT_MODEL

        try:
            if not stream:
                acc = call_upstream(pm["body"])
                if acc["failure"]:
                    return self._err(502, "Postman отказал: " + acc["failure"])
                msg = {"role": "assistant", "content": acc["text"]}
                finish = "stop"
                if acc["tool_calls"] and not acc["text"]:
                    msg["content"] = None
                if acc["thinking"]:
                    msg["reasoning_content"] = acc["thinking"]
                if acc["tool_calls"]:
                    finish = "tool_calls"
                    msg["tool_calls"] = oai_tool_calls(acc["tool_calls"])
                resp = {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": len(pm["query"]) // 4,
                        "completion_tokens": len(acc["text"]) // 4,
                        "total_tokens": (len(pm["query"]) + len(acc["text"])) // 4,
                    },
                }
                if acc["conv_id"]:
                    resp["_postman_conversation_id"] = acc["conv_id"]
                if pu := postman_usage(acc["usage"]):
                    resp["_postman_usage"] = pu
                if acc["model_key"]:
                    resp["_postman_model"] = acc["model_key"]
                return self._json(200, resp)

            return self._stream_chat(pm, model)
        except UpstreamError as exc:
            return self._err(502, f"upstream {exc.status}: {exc.body[:300]}")
        except Exception as exc:  # noqa: BLE001
            return self._err(500, f"{type(exc).__name__}: {exc}")

    def _stream_chat(self, pm: dict, model: str):
        chat_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(obj: dict) -> None:
            obj.setdefault("id", chat_id)
            obj.setdefault("object", "chat.completion.chunk")
            obj.setdefault("created", created)
            obj.setdefault("model", model)
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        def cb(ev: dict) -> bool:
            try:
                if ev["type"] == "text":
                    send({"choices": [{"index": 0, "delta": {"content": ev["value"]},
                                       "finish_reason": None}]})
                elif ev["type"] == "thinking":
                    send({"choices": [{"index": 0, "delta": {"reasoning_content": ev["value"]},
                                       "finish_reason": None}]})
                return True
            except (BrokenPipeError, ValueError):
                return False

        send({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        try:
            acc = call_upstream(pm["body"], cb)
        except UpstreamError as exc:
            send({"error": {"message": str(exc)}})
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if acc["tool_calls"]:
            send({"choices": [{"index": 0,
                               "delta": {"tool_calls": oai_tool_calls(acc["tool_calls"])},
                               "finish_reason": None}]})
        send({"choices": [{"index": 0, "delta": {}, "finish_reason":
                           "tool_calls" if acc["tool_calls"] else "stop"}],
              "_postman_conversation_id": acc["conv_id"] or None,
              "_postman_usage": postman_usage(acc["usage"])})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _anthropic(self, req: dict):
        """Минимальный Anthropic-адаптер: тот же upstream, формат /v1/messages."""
        oai = {
            "model": req.get("model"),
            "messages": [],
            "max_tokens": req.get("max_tokens"),
        }
        if req.get("system"):
            oai["messages"].append({"role": "system", "content": req["system"]})
        for m in req.get("messages") or []:
            oai["messages"].append({"role": m.get("role"), "content": m.get("content")})
        pm = to_postman_request(oai)
        try:
            acc = call_upstream(pm["body"])
        except UpstreamError as exc:
            return self._json(502, {"type": "error", "error": {
                "type": "upstream_error", "message": f"{exc.status}: {exc.body[:200]}"}})
        if acc["failure"]:
            return self._json(502, {"type": "error", "error": {
                "type": "upstream_error", "message": acc["failure"]}})

        content = []
        if acc["thinking"]:
            content.append({"type": "thinking", "thinking": acc["thinking"]})
        if acc["text"]:
            content.append({"type": "text", "text": acc["text"]})
        return self._json(200, {
            "id": "msg_" + uuid.uuid4().hex[:24],
            "type": "message",
            "role": "assistant",
            "model": req.get("model") or DEFAULT_MODEL,
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": len(pm["query"]) // 4,
                      "output_tokens": len(acc["text"]) // 4},
        })


def main() -> None:
    if not SESSIONS_FILE.is_file():
        SESSIONS_FILE.write_text("# по одной сессии на строку: cookie:<postman.sid>\n",
                                 encoding="utf-8")
        print(f"[!] {SESSIONS_FILE.name} создан пустым — добавь сессию")
    alive, total = POOL.stats()
    print(f"postman2api (python) · http://{HOST}:{PORT}")
    print(f"  модель по умолчанию: {DEFAULT_MODEL}")
    print(f"  сессий: {alive} живых из {total}")
    print(f"  ключ для клиентов: {AUTH_TOKEN}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлен")
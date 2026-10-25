# -*- coding: utf-8 -*-
"""limits — квота Postman AI по каждому аккаунту.

Источник истины — сам gateway: первый SSE-ивент `usage` на любой запрос
`POST https://gateway.postman.com/chat`.

Поля, которые он отдаёт:
    userType    FREE_USER | PAID_USER
    limit       100000 (free) | 400000 (Solo Trial / paid)
    usage       сколько потрачено в текущем цикле
    usageState  AVAILABLE | EXHAUSTED
    usageCycle  {start, end}

Проверка живости — ОТДЕЛЬНЫЙ шаг: 401 = сессия протухла, а не «200 = успех».
"""
from __future__ import annotations

import json
import os
import threading
import time

import requests

from . import config

LIMITS_FILE = os.path.join(config.DATA_DIR, "limits.json")
_lock = threading.Lock()
CACHE_TTL = int(getattr(config, "LIMITS_CACHE_TTL", 600))

GATEWAY = "https://gateway.postman.com/chat"


def _headers(sid: str) -> dict:
    return {
        "Cookie": f"postman.sid={sid}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-pstmn-req-service": "agent-mode-service",
        "x-app-version": getattr(config, "APP_VERSION", "12.26.4"),
        "Origin": "https://desktop.postman.com",
        "Referer": "https://desktop.postman.com/",
        "User-Agent": getattr(config, "UA", "Mozilla/5.0"),
    }


def _load_cache() -> dict:
    try:
        with open(LIMITS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"accounts": {}, "checked_at": None}


def _save_cache(data: dict) -> None:
    tmp = f"{LIMITS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LIMITS_FILE)


def probe(sid: str, timeout: int = 60) -> dict:
    """Один запрос → состояние квоты.

    Возвращает:
      {alive, user_type, limit, usage, remaining, state, cycle_end, error}
    """
    out = {"alive": False, "user_type": "", "limit": 0, "usage": 0,
           "remaining": 0, "state": "", "cycle_end": "", "error": ""}
    if not sid:
        out["error"] = "нет sid"
        return out
    try:
        r = requests.post(GATEWAY, headers=_headers(sid),
                          json={"input": {"query": "say OK"}, "metadata": {}},
                          timeout=timeout, stream=True)
        if r.status_code == 401:
            out["error"] = "401 — сессия протухла"
            return out
        if r.status_code == 403:
            out["error"] = "403 — доступ запрещён (бан?)"
            return out

        for line in r.iter_lines():
            if not line:
                continue
            s = line.decode("utf-8", "replace")
            if not s.startswith("data:"):
                continue
            try:
                ev = json.loads(s[5:].strip())
            except ValueError:
                continue
            if ev.get("eventType") == "usage":
                d = ev.get("data") or {}
                out.update({
                    "alive": True,
                    "user_type": d.get("userType", ""),
                    "limit": int(d.get("limit") or 0),
                    "usage": int(d.get("usage") or 0),
                    "state": d.get("usageState", ""),
                    "cycle_end": (d.get("usageCycle") or {}).get("end", ""),
                })
                out["remaining"] = max(0, out["limit"] - out["usage"])
                return out
            if ev.get("eventType") == "failure":
                msg = (ev.get("data") or {}).get("userMessage", "")
                out["error"] = f"failure: {msg}"
                return out
        out["error"] = "usage-ивент не пришёл"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def refresh(accounts: list[dict], force: bool = False) -> dict:
    """Обновить квоты по списку аккаунтов (из data/accounts.jsonl).

    Кэш держит результат CACHE_TTL секунд, чтобы панель не дёргала gateway
    на каждый запрос.
    """
    with _lock:
        cache = _load_cache()
        now = time.time()
        if (not force and cache.get("checked_at")
                and now - cache["checked_at"] < CACHE_TTL):
            return cache

        results = {}
        for a in accounts:
            sid = a.get("sid") or ""
            email = a.get("email") or ""
            if not sid:
                continue
            st = probe(sid)
            results[email or sid[:12]] = st
            time.sleep(0.5)   # не долбить gateway

        cache = {"accounts": results, "checked_at": now}
        _save_cache(cache)
        return cache


def summary(cache: dict) -> dict:
    """Сводка по кэшу: сколько живых, сколько исчерпано, суммарный остаток."""
    accs = (cache or {}).get("accounts") or {}
    alive = [k for k, v in accs.items() if v.get("alive")]
    exhausted = [k for k, v in accs.items()
                 if v.get("alive") and v.get("state") == "EXHAUSTED"]
    total_left = sum(v.get("remaining", 0) for v in accs.values() if v.get("alive"))
    return {"total": len(accs), "alive": len(alive), "exhausted": len(exhausted),
            "remaining_total": total_left, "checked_at": cache.get("checked_at")}
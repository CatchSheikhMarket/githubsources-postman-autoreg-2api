#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prune — уборка пула postman2api.

Мёртвые (401), исчерпанные (usage >= limit) и забаненные (403) сессии
уезжают в data/archive/ с датой, пул остаётся чистым.

Почему нужен: мост ротирует всё подряд и не знает про исчерпание. Аккаунт
с usage == limit остаётся в круге и каждый свой ход отдаёт пустой content —
это «сгнивший» пул. Prune вычищает такие до того, как они начнут мешать.

Запуск:
    python3 prune.py            # проверить и почистить
    python3 prune.py --dry      # показать, не трогать
    python3 prune.py --archive  # сложить убразанные в data/archive/

Cron (раз в сутки, после refresh реггера):
    30 4 * * * cd <путь>/postman2api && python3 prune.py --archive >> data/prune.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SESSIONS = os.path.join(HERE, "sessions.txt")
ARCHIVE = os.path.join(HERE, "data", "archive")
GATEWAY = "https://gateway.postman.com/chat"
APP_VERSION = "12.26.4"

HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "x-pstmn-req-service": "agent-mode-service",
    "x-app-version": APP_VERSION,
    "Origin": "https://desktop.postman.com",
    "Referer": "https://desktop.postman.com/",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Postman/12.26.4 Electron/37.10.3 Safari/537.36"),
}

BODY = {
    "input": {"chatType": "USER_QUERY", "query": "say OK", "toolResponse": "",
              "useCase": None, "conversationId": None, "agent": None,
              "product": "workspace_localmode_v12", "startedFrom": "CHAT_INPUT"},
    "platform": "DESKTOP_LINUX",
    "clientTools": {"native": [], "excludedTools": ["askUser"],
                    "nativeToolsHash": "clienttools-workspace_localmode_v12-desktop-darwin-12.23.1-ui-260811-0231-828d6b3ed37b"},
    "clientKBTerms": {"nativeTermsHash": "kbterms-workspace_localmode_v12-desktop-darwin-12.23.1-ui-260811-0231-dbc2c7575e92",
                      "excludedKBTerms": []},
    "mandatoryContext": {"workspaceId": "proxy-workspace"},
    "selectedContext": [],
    "backgroundContext": [{"type": "ACTIVE_ENVIRONMENT", "value": None},
                          {"type": "ACTIVE_WORKSPACE",
                           "value": {"name": "Proxy Workspace", "id": "proxy-workspace"}}],
    "devModeOptions": {"selectedModel": "CLAUDE_45_HAIKU_BEDROCK",
                       "isParallelToolCallingSupported": True, "autoRun": False,
                       "supportsAskUser": False, "supportsActionRecommendations": True,
                       "useThinkingModeIfAvailable": False, "thinkingLevel": "medium",
                       "isAgentModeEnabled": True},
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def probe(sid: str, timeout: int = 90) -> dict:
    """Вердикт по сессии: alive | exhausted | dead | banned | error."""
    out = {"verdict": "error", "user_type": "", "usage": 0, "limit": 0, "error": ""}
    try:
        r = requests.post(GATEWAY, headers={**HEADERS_BASE, "Cookie": f"postman.sid={sid}"},
                          json=BODY, timeout=timeout, stream=True)
        if r.status_code == 401:
            out["verdict"] = "dead"
            out["error"] = "401"
            return out
        if r.status_code == 403:
            out["verdict"] = "banned"
            out["error"] = "403"
            return out

        got_usage = False
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
            et = ev.get("eventType")
            if et == "usage":
                d = ev.get("data") or {}
                out["user_type"] = d.get("userType", "")
                out["limit"] = int(d.get("limit") or 0)
                out["usage"] = int(d.get("usage") or 0)
                got_usage = True
                if out["limit"] and out["usage"] >= out["limit"]:
                    out["verdict"] = "exhausted"
                    return out
                if d.get("usageState") == "EXHAUSTED":
                    out["verdict"] = "exhausted"
                    return out
            if et == "failure":
                msg = (ev.get("data") or {}).get("userMessage", "")
                if "Forbidden" in msg:
                    out["verdict"] = "banned"
                    out["error"] = "Forbidden"
                    return out
                out["error"] = msg[:60]
                continue
            if et == "textChunk":
                out["verdict"] = "alive"
                return out
            if et == "done":
                break
        if got_usage and out["verdict"] == "error":
            out["verdict"] = "alive"
        elif not got_usage:
            out["error"] = out["error"] or "нет usage-ивента"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def load_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def desktop_session() -> tuple[str, str]:
    """(email, access_token) из установленного Postman Desktop.

    Мост подхватывает Desktop-сессию сам (userPartitionData.json), поэтому
    она участвует в ротации, но в sessions.txt её нет — prune должен
    проверять её отдельно.
    """
    import glob
    import json as _json
    cands = [
        os.path.expanduser("~/.config/Postman/storage/userPartitionData.json"),
        os.path.expanduser("~/Library/Application Support/Postman/storage/userPartitionData.json"),
        os.path.expandvars(r"%APPDATA%\Postman\storage\userPartitionData.json"),
    ]
    for p in cands:
        if not os.path.exists(p):
            continue
        try:
            data = _json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for part in (data.get("v8Partitions") or {}).values():
            raw = (part.get("meta") or {}).get("raw")
            if not raw:
                continue
            try:
                j = _json.loads(raw)
            except Exception:
                continue
            tok = (j.get("auth") or {}).get("access_token")
            if tok:
                return j.get("email", ""), tok
    return "", ""


def probe_token(token: str, timeout: int = 90) -> dict:
    """То же, что probe, но по access_token (Desktop), а не по cookie."""
    out = {"verdict": "error", "user_type": "", "usage": 0, "limit": 0, "error": ""}
    try:
        r = requests.post(GATEWAY, headers={**HEADERS_BASE, "x-access-token": token},
                          json=BODY, timeout=timeout, stream=True)
        if r.status_code == 401:
            out["verdict"] = "dead"
            out["error"] = "401"
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
            et = ev.get("eventType")
            if et == "usage":
                d = ev.get("data") or {}
                out.update(user_type=d.get("userType", ""),
                           limit=int(d.get("limit") or 0),
                           usage=int(d.get("usage") or 0))
                if out["limit"] and out["usage"] >= out["limit"]:
                    out["verdict"] = "exhausted"
                    return out
                if d.get("usageState") == "EXHAUSTED":
                    out["verdict"] = "exhausted"
                    return out
            if et == "textChunk":
                out["verdict"] = "alive"
                return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Уборка пула postman2api")
    ap.add_argument("--dry", action="store_true", help="только показать")
    ap.add_argument("--archive", action="store_true",
                    help="сложить убранные в data/archive/")
    args = ap.parse_args()

    lines = load_lines(SESSIONS)
    if not lines:
        log(f"пул пуст или не найден: {SESSIONS}")
        return 0

    keep, drop = [], []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            keep.append(ln)
            continue
        sid = s[len("cookie:"):] if s.startswith("cookie:") else s
        st = probe(sid)
        v = st["verdict"]
        extra = ""
        if v in ("alive", "exhausted"):
            extra = f"{st['user_type']} {st['usage']}/{st['limit']}"
        elif st["error"]:
            extra = st["error"]
        log(f"  {sid[:16]}... {v:<10} {extra}")

        if v == "alive":
            keep.append(ln)
        else:
            drop.append({"sid": sid, "line": s, "verdict": v,
                         "user_type": st["user_type"], "usage": st["usage"],
                         "limit": st["limit"], "error": st["error"],
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        time.sleep(0.5)

    log(f"оставить: {len([l for l in keep if not l.strip().startswith('#')])}, "
        f"убрать: {len(drop)}")

    # Desktop-сессия: мост подхватывает её сам, в sessions.txt её нет.
    # Проверяем отдельно — если исчерпана, подсказываем, что делать.
    demail, dtok = desktop_session()
    if dtok:
        st = probe_token(dtok)
        extra = (f"{st['user_type']} {st['usage']}/{st['limit']}"
                 if st["user_type"] else st["error"])
        log(f"  Desktop ({demail or '?'}) {st['verdict']:<10} {extra}")
        if st["verdict"] == "exhausted":
            log("  ⚠ Desktop-сессия исчерпана и участвует в ротации. "
                "Перелогинься в Postman Desktop или смени аккаунт — "
                "из sessions.txt её не убрать.")

    if args.dry:
        log("--dry: ничего не изменено")
        return 0

    if drop and args.archive:
        os.makedirs(ARCHIVE, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(ARCHIVE, f"pruned_{stamp}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for d in drop:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        log(f"архив: {path}")

    if drop:
        tmp = SESSIONS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, SESSIONS)
        log(f"пул обновлён: {SESSIONS}")
        log("мост подхватит изменения при следующем запросе (перезапуск не нужен)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
# -*- coding: utf-8 -*-
"""Атомарная запись результатов: JSONL (полные данные) + combo txt + пул моста.

Пишем СРАЗУ после каждого аккаунта — падение на 90-м не теряет 89.

Форматы:
  data/accounts.jsonl   — полный JSON по аккаунту
  data/accounts.txt     — combo: email|password|session
  <bridge>/sessions.txt — строка `cookie:<sid>` для моста postman2api

Мост читает sessions.txt построчно: `access_token` или `cookie:<postman.sid>`.
"""
import json
import os
import threading

from . import config

_lock = threading.Lock()

CREDS_HEADER = (
    "# Сессии postman2api: access_token или cookie:<postman.sid> (веб-сессия).\n"
    "# Аккаунты добавляет postman-register автоматически.\n"
)


def _atomic_append(path, line):
    tmp = path + ".tmp"
    with _lock:
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(existing + line)
        os.replace(tmp, path)


def save_account(account: dict) -> None:
    """Сохранить аккаунт и добавить его сессию в пул моста.

    account: {email, password, session, sid, user_type, limit, usage, status, ...}
    """
    os.makedirs(os.path.dirname(config.OUTPUT_JSONL), exist_ok=True)
    _atomic_append(config.OUTPUT_JSONL,
                   json.dumps(account, ensure_ascii=False) + "\n")

    combo = "|".join(str(account.get(k, "") or "") for k in
                     ("email", "password", "session"))
    _atomic_append(config.OUTPUT_COMBO, combo + "\n")

    _add_to_bridge(account)


def _bridge_path() -> str:
    target = getattr(config, "BRIDGE_SESSIONS_FILE", "") or ""
    if not target:
        return ""
    return os.path.normpath(os.path.join(config.ROOT, target))


def is_usable(account: dict) -> bool:
    """Годится ли аккаунт в ротацию моста.

    Не пускаем:
      * исчерпанные  — usage >= limit (отдают пустой content и жгут слот);
      * помеченные   — status в dead/exhausted/banned;
      * без сессии   — sid пустой.
    """
    if not (account.get("sid") or ""):
        return False
    status = str(account.get("status") or "").lower()
    if status in ("dead", "exhausted", "banned", "expired"):
        return False
    limit = int(account.get("limit") or 0)
    usage = int(account.get("usage") or 0)
    if limit > 0 and usage >= limit:
        return False
    return True


def _add_to_bridge(account: dict) -> None:
    """Дописать сессию в sessions.txt моста.

    Дубликаты не плодим, исчерпанные/мёртвые не добавляем вовсе —
    иначе мост каждый круг тратит слот на пустой ответ.
    """
    path = _bridge_path()
    if not path:
        return
    sid = account.get("sid") or ""
    if not sid or not is_usable(account):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(CREDS_HEADER)
        with open(path, "r", encoding="utf-8") as f:
            if sid in f.read():
                return
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"cookie:{sid}\n")
    except OSError:
        pass


def remove_from_bridge(sid: str) -> bool:
    """Убрать сессию из пула моста (протухла / исчерпана / забанена)."""
    path = _bridge_path()
    if not path or not sid:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False
    kept = [ln for ln in lines if sid not in ln]
    if len(kept) == len(lines):
        return False
    tmp = path + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, path)
    return True


def sync_bridge(accounts: list[dict] | None = None) -> dict:
    """Привести sessions.txt в соответствие с accounts.jsonl.

    Оставляет в пуле только живые неисчерпанные сессии. Возвращает счётчики.
    """
    path = _bridge_path()
    accounts = accounts if accounts is not None else load_accounts()
    if not path:
        return {"kept": 0, "dropped": 0, "path": ""}

    good = {a["sid"] for a in accounts if is_usable(a)}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return {"kept": 0, "dropped": 0, "path": path}

    kept, dropped = [], 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            kept.append(ln)
            continue
        sid = s[len("cookie:"):] if s.startswith("cookie:") else s
        # сессии, которых нет в accounts.jsonl (добавлены вручную), не трогаем
        known = any(a.get("sid") == sid for a in accounts)
        if not known or sid in good:
            kept.append(ln)
        else:
            dropped += 1

    if dropped:
        tmp = path + ".tmp"
        with _lock:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, path)
    return {"kept": len([l for l in kept if not l.strip().startswith("#")]),
            "dropped": dropped, "path": path}


def load_accounts() -> list[dict]:
    if not os.path.exists(config.OUTPUT_JSONL):
        return []
    out = []
    with open(config.OUTPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def update_account(email: str, **fields) -> bool:
    """Обновить запись по email (перезапись JSONL целиком).

    Нужно для статусов: сессия протухла / квота исчерпана / забанен.
    """
    accounts = load_accounts()
    found = False
    for a in accounts:
        if a.get("email") == email:
            a.update(fields)
            found = True
    if not found:
        return False
    os.makedirs(os.path.dirname(config.OUTPUT_JSONL), exist_ok=True)
    tmp = config.OUTPUT_JSONL + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            for a in accounts:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
        os.replace(tmp, config.OUTPUT_JSONL)
    return True
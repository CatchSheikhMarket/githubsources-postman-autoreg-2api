#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""refresh — ежедневная проверка пула Postman.

Что делает:
  1. Читает data/accounts.jsonl.
  2. По каждой сессии дёргает gateway и читает usage-ивент.
  3. Классифицирует: alive / exhausted / dead(401) / banned(403).
  4. Обновляет accounts.jsonl (status, usage, limit, last_check).
  5. Чистит sessions.txt моста: мёртвые и исчерпанные убираются,
     вернувшиеся в строй — добавляются.

Зачем: postman.sid живёт ~30 дней, Solo Trial — 7. Без обновления пул
постепенно заполняется протухшими сессиями, и мост тратит слоты впустую.

Запуск:
    python3 refresh.py               # проверить всё
    python3 refresh.py --dry         # только показать, не писать
    python3 refresh.py --cron        # тихий режим для cron/systemd

Cron (раз в сутки в 4:00):
    0 4 * * * cd <путь>/postman-register && \
      env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH \
      python3 refresh.py --cron >> data/refresh.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core import config, limits, results  # noqa: E402


def log(msg: str, quiet: bool = False) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if not quiet:
        print(line, flush=True)


def classify(st: dict) -> str:
    """Вердикт по результату limits.probe."""
    if st.get("alive"):
        limit = int(st.get("limit") or 0)
        usage = int(st.get("usage") or 0)
        if limit > 0 and usage >= limit:
            return "exhausted"
        if st.get("state") == "EXHAUSTED":
            return "exhausted"
        return "alive"
    err = str(st.get("error") or "").lower()
    if "401" in err:
        return "dead"
    if "403" in err:
        return "banned"
    return "error"


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка пула Postman")
    ap.add_argument("--dry", action="store_true", help="не писать изменения")
    ap.add_argument("--cron", action="store_true", help="тихий режим")
    args = ap.parse_args()
    quiet = args.cron

    accounts = results.load_accounts()
    if not accounts:
        log("нет аккаунтов в data/accounts.jsonl", quiet)
        return 0

    log(f"проверяю {len(accounts)} аккаунтов", quiet)
    stats = {"alive": 0, "exhausted": 0, "dead": 0, "banned": 0, "error": 0}

    for a in accounts:
        sid = a.get("sid") or ""
        email = a.get("email") or sid[:12]
        if not sid:
            log(f"  {email:<34} НЕТ sid", quiet)
            stats["error"] += 1
            continue

        st = limits.probe(sid)
        verdict = classify(st)
        stats[verdict] = stats.get(verdict, 0) + 1

        detail = ""
        if verdict in ("alive", "exhausted"):
            detail = (f"{st.get('user_type')} "
                      f"{st.get('usage')}/{st.get('limit')}")
        else:
            detail = st.get("error", "")

        log(f"  {email:<34} {verdict:<10} {detail}", quiet)

        if not args.dry:
            results.update_account(
                a.get("email", ""),
                status=verdict,
                user_type=st.get("user_type", a.get("user_type", "")),
                limit=st.get("limit", a.get("limit", 0)),
                usage=st.get("usage", a.get("usage", 0)),
                cycle_end=st.get("cycle_end", ""),
                last_check=time.strftime("%Y-%m-%dT%H:%M:%S"),
                last_error=st.get("error", "") if verdict not in ("alive", "exhausted") else "",
            )
        time.sleep(0.6)

    # чистим пул моста
    if not args.dry:
        synced = results.sync_bridge(results.load_accounts())
        log(f"пул моста: оставлено {synced['kept']}, убрано {synced['dropped']}", quiet)
    else:
        log("пул моста: не тронут (--dry)", quiet)

    summary = (f"итого: alive={stats['alive']} exhausted={stats['exhausted']} "
               f"dead={stats['dead']} banned={stats['banned']} error={stats['error']}")
    log(summary, quiet)
    return 0 if stats["alive"] or stats["exhausted"] else 1


if __name__ == "__main__":
    sys.exit(main())
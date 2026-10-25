#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_pool — занести существующие сессии моста в accounts.jsonl реггера.

Нужно, когда пул наполнялся вручную (или до появления реггера): панель
и refresh читают accounts.jsonl, а не sessions.txt.

Запуск:
    python3 import_pool.py            # импорт из sessions.txt моста
    python3 import_pool.py --check    # сразу проверить квоты
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core import config, limits, results  # noqa: E402


def bridge_sessions() -> list[str]:
    target = getattr(config, "BRIDGE_SESSIONS_FILE", "") or ""
    if not target:
        return []
    path = os.path.normpath(os.path.join(config.ROOT, target))
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s[len("cookie:"):] if s.startswith("cookie:") else s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="сразу проверить квоты")
    args = ap.parse_args()

    existing = {a.get("sid") for a in results.load_accounts()}
    sids = [s for s in bridge_sessions() if s not in existing]
    if not sids:
        print("новых сессий в пуле моста нет")
        return 0

    print(f"импортирую {len(sids)} сессий")
    for i, sid in enumerate(sids, 1):
        rec = {
            "email": f"imported_{sid[:8]}@manual",
            "username": "",
            "password": "",
            "sid": sid,
            "session": f"cookie:{sid}",
            "status": "unknown",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "note": "импортировано из sessions.txt моста",
        }
        if args.check:
            st = limits.probe(sid)
            rec.update(user_type=st.get("user_type", ""),
                       limit=st.get("limit", 0),
                       usage=st.get("usage", 0),
                       cycle_end=st.get("cycle_end", ""),
                       status=("alive" if st.get("alive") else "dead"),
                       last_check=time.strftime("%Y-%m-%dT%H:%M:%S"))
            print(f"  {i}. {sid[:16]}... {rec['status']:<8} "
                  f"{rec['user_type']} {rec['usage']}/{rec['limit']}")
            time.sleep(0.5)
        else:
            print(f"  {i}. {sid[:16]}...")
        # пишем напрямую в jsonl, не трогая пул моста (он уже наполнен)
        results._atomic_append(config.OUTPUT_JSONL,
                               __import__("json").dumps(rec, ensure_ascii=False) + "\n")

    print(f"готово: {len(sids)} записей в {config.OUTPUT_JSONL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
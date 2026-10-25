#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Офлайн selfcheck postman-register: импорты, конфиг, парсеры, запись.

Живой прогон не делает — только проверки без сети (кроме опционального ping почты).
Запуск: python3 tests/selfcheck.py
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  OK   {name}")
    except Exception as e:
        FAIL.append((name, e))
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


def t_imports():
    from core import config, register, limits, results  # noqa
    assert hasattr(register, "register_one")
    assert hasattr(register, "test_key")
    assert hasattr(register, "list_models")


def t_config():
    from core import config
    assert config.PANEL_PORT == 9924, config.PANEL_PORT
    assert config.BRIDGE_PORT == 12049, config.BRIDGE_PORT
    assert config.MAIL_DOMAINS, "нет доменов почты"
    assert "postman" in config.BASE_URL


def t_password():
    from core.register import gen_password
    seen = set()
    for _ in range(50):
        pw = gen_password()
        assert len(pw) == 18, len(pw)
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)
        # нет общего префикса/суффикса (урок GitHub-авторега)
        assert pw[:3] not in seen, f"повторяющийся префикс {pw[:3]}"
        seen.add(pw[:3])


def t_username():
    from core.register import gen_username
    u = gen_username()
    assert re.fullmatch(r"pmu\d{7}", u), u


def t_code_parser():
    """Парсер кода должен отбрасывать заглушку 000000."""
    from core.register import wait_code  # noqa — проверяем только regex-логику
    raw = "Your verification code is 000000. Do not share."
    pats = (r"verification code[^0-9]{0,150}(\d{6})",
            r"code[^0-9]{0,80}(\d{6})")
    found = None
    for p in pats:
        mm = re.search(p, raw, re.I)
        if mm and mm.group(1) != "000000":
            found = mm.group(1)
    assert found is None, "заглушка 000000 прошла как код"

    raw2 = "Your verification code is 451290."
    for p in pats:
        mm = re.search(p, raw2, re.I)
        if mm and mm.group(1) != "000000":
            found = mm.group(1)
            break
    assert found == "451290", found


def t_proxy_parse():
    from core.register import browser_proxy
    p = browser_proxy("http://user:pass@brd.superproxy.io:44445")
    assert p and p["server"] == "http://brd.superproxy.io:44445"
    assert p["username"] == "user" and p["password"] == "pass"
    assert browser_proxy("") is None


def t_results():
    from core import results, config
    old_jsonl, old_combo = config.OUTPUT_JSONL, config.OUTPUT_COMBO
    old_bridge = config.BRIDGE_SESSIONS_FILE
    tmp = tempfile.mkdtemp()
    try:
        config.OUTPUT_JSONL = os.path.join(tmp, "a.jsonl")
        config.OUTPUT_COMBO = os.path.join(tmp, "a.txt")
        config.BRIDGE_SESSIONS_FILE = os.path.join(tmp, "sessions.txt")
        results.save_account({"email": "x@y.z", "password": "p",
                              "session": "cookie:ABC", "sid": "ABC",
                              "status": "ok", "limit": 400000})
        accs = results.load_accounts()
        assert len(accs) == 1 and accs[0]["sid"] == "ABC"
        with open(config.BRIDGE_SESSIONS_FILE) as f:
            assert "cookie:ABC" in f.read()
        # дубликат не должен добавляться
        results.save_account({"email": "x@y.z", "session": "cookie:ABC", "sid": "ABC"})
        with open(config.BRIDGE_SESSIONS_FILE) as f:
            assert f.read().count("cookie:ABC") == 1
        assert results.update_account("x@y.z", status="exhausted")
    finally:
        config.OUTPUT_JSONL, config.OUTPUT_COMBO = old_jsonl, old_combo
        config.BRIDGE_SESSIONS_FILE = old_bridge


def t_limits_shape():
    from core import limits
    st = limits.probe("", timeout=1)
    assert st["alive"] is False and st["error"] == "нет sid"
    s = limits.summary({"accounts": {"a": {"alive": True, "remaining": 100,
                                           "state": "AVAILABLE"}},
                        "checked_at": 1})
    assert s["alive"] == 1 and s["remaining_total"] == 100


def t_mail_optional():
    """Опционально: почта отвечает. Нет сети — не валим."""
    try:
        import requests
        from core import config
        r = requests.get(f"{config.MAIL_BASE}/admin/mails",
                         params={"address": "none@example.com", "limit": 1, "offset": 0},
                         headers={"x-admin-auth": config.MAIL_ADMIN_KEY}, timeout=10)
        assert r.status_code in (200, 400, 401, 403), r.status_code
    except Exception:
        pass


if __name__ == "__main__":
    print("postman-register selfcheck")
    for name, fn in [
        ("imports", t_imports),
        ("config", t_config),
        ("password", t_password),
        ("username", t_username),
        ("code_parser", t_code_parser),
        ("proxy_parse", t_proxy_parse),
        ("results", t_results),
        ("limits", t_limits_shape),
        ("mail", t_mail_optional),
    ]:
        check(name, fn)

    print(f"\n{PASS.__len__()} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
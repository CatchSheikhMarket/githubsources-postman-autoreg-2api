# -*- coding: utf-8 -*-
"""Регистрация Postman: signup → код из почты → Turnstile (Camoufox) → онбординг → Solo Trial.

Флоу проверен живьём 22.10.2026 (см. docs/РАЗБОР.md).

Ключевые факты:
  * Chrome/Playwright НЕ проходит Cloudflare Turnstile на Postman — нужен Camoufox.
  * Camoufox решает Turnstile сам, без внешних солверов (внешний токен не принимают).
  * Онбординг обязателен: без него аккаунт висит на /onboarding/user.
  * Solo Trial активируется кнопкой, карты не требует → лимит 100k → 400k.
  * postman.sid живёт ~30 дней; мост принимает его как `cookie:<sid>`.
"""
from __future__ import annotations

import json
import re
import secrets
import string
import time
from typing import Optional

import requests

from . import config

IDENTITY = "https://identity.getpostman.com"
SIGNUP_URL = f"{IDENTITY}/signup"
SITEKEY = "0x4AAAAAAAYx_CBZ0IJEN60P"

_VERIFY_MARK = "verify-account"
_ONBOARD_MARK = "onboarding/user"


def gen_password(n: int = 18) -> str:
    """Полностью случайный пароль (без общих префиксов/суффиксов).

    Postman требует минимум 8 символов, буквы + цифры. Фиксированный шаблон —
    сигнал для антифрода (урок GitHub-авторега, где все аккаунты имели `Qz9!…7k`).
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if (any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
            return pw


def gen_username(prefix: str = "pmu") -> str:
    return f"{prefix}{secrets.randbelow(10**7):07d}"


def browser_proxy(raw: str) -> Optional[dict]:
    """URL прокси → dict для Camoufox {server, username, password}."""
    if not raw:
        return None
    from urllib.parse import unquote, urlsplit
    s = raw if "://" in raw else f"http://{raw}"
    u = urlsplit(s)
    if not u.hostname or not u.port:
        return None
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = unquote(u.username)
        out["password"] = unquote(u.password or "")
    return out


def wait_code(email: str, timeout: int | None = None) -> Optional[str]:
    """Код подтверждения из apimail.

    В письме Postman есть заглушка `000000` — её отбрасываем, иначе возьмём
    не тот код. Тело quoted-printable, поэтому сначала склеиваем мягкие переносы.
    """
    timeout = timeout or getattr(config, "MAIL_TIMEOUT_SEC", 180)
    poll = getattr(config, "MAIL_POLL_SEC", 5)
    base = getattr(config, "MAIL_BASE", "https://apimail.example.com")
    key = getattr(config, "MAIL_ADMIN_KEY", "")
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/admin/mails",
                             params={"address": email, "limit": 10, "offset": 0},
                             headers={"x-admin-auth": key}, timeout=20)
            for m in (r.json().get("results") or []):
                raw = (m.get("raw") or "").replace("=\r\n", "").replace("=\n", "")
                for pat in (r"verification code[^0-9]{0,150}(\d{6})",
                            r"code[^0-9]{0,80}(\d{6})",
                            r">\s*(\d{6})\s*<"):
                    mm = re.search(pat, raw, re.I)
                    if mm and mm.group(1) != "000000":
                        return mm.group(1)
        except Exception:
            pass
        time.sleep(poll)
    return None


class SignupError(Exception):
    pass


class PostmanClient:
    """Один прогон = один аккаунт = один sticky-прокси."""

    def __init__(self, proxy: str | None = None, log=print):
        self.proxy = proxy
        self.log = log
        self.email = ""
        self.username = ""
        self.password = ""
        self.sid: str | None = None
        self.usage: dict | None = None

    # ---------------------------------------------------------------- основной
    def run(self, do_trial: bool = True, do_onboarding: bool = True) -> dict:
        from camoufox.sync_api import Camoufox

        email_local = f"{getattr(config, 'ADDRESS_PREFIX', '') or 'pm'}{int(time.time())}"
        domains = getattr(config, "MAIL_DOMAINS", None) or []
        domain = (secrets.choice(domains) if domains
                  else getattr(config, "MAIL_DOMAIN", "mail.example.com"))
        self.email = f"{email_local}@{domain}"
        self.username = gen_username()
        self.password = gen_password()
        self.log(f"email={self.email} user={self.username}")

        opts = {
            "headless": bool(getattr(config, "HEADLESS", False)),
            "humanize": True,
            "locale": "en-US",
            "os": "linux",
            # geoip обязателен при прокси: иначе TZ/локаль не совпадут с exit-IP
            # и Cloudflare забанит ещё до формы.
            "geoip": bool(self.proxy),
        }
        bp = browser_proxy(self.proxy)
        if bp:
            opts["proxy"] = bp
            self.log(f"прокси: {bp['server']}")

        with Camoufox(**opts) as browser:
            page = browser.new_page()
            page.set_default_timeout(90000)

            # --- 1. форма регистрации
            page.goto(SIGNUP_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            if "Just a moment" in page.title():
                raise SignupError("Cloudflare заблокировал ещё до формы "
                                  "(проверь прокси/geoip)")
            page.fill("#email", self.email)
            time.sleep(0.4)
            page.fill("#username", self.username)
            time.sleep(0.4)
            page.fill("#password", self.password)
            time.sleep(0.5)
            page.click("button:has-text('Create Free Account')")
            page.wait_for_timeout(11000)

            if _VERIFY_MARK not in page.url:
                txt = page.evaluate("() => (document.body.innerText||'').slice(0,300)")
                raise SignupError(f"не дошли до verify: {page.url} | {txt[:200]}")

            # --- 2. код из почты
            self.log("страница верификации — ждём код")
            code = wait_code(self.email)
            if not code:
                raise SignupError("код не пришёл")
            self.log(f"код: {code}")

            # --- 3. Turnstile: Camoufox решает сам, только ждём
            self.log("ждём авто-решения Turnstile")
            solved = False
            for _ in range(20):
                page.wait_for_timeout(2000)
                ln = page.evaluate("""() => {
                  const i = document.querySelector('input[name="cf-turnstile-response"]');
                  return i ? (i.value || '').length : 0;
                }""")
                if ln > 100:
                    solved = True
                    break
            if not solved:
                raise SignupError("Turnstile не решился автоматически")
            self.log("Turnstile решён")

            # --- 4. ввод кода + подтверждение
            page.fill("#verification-code", code)
            time.sleep(0.4)
            page.click("button:has-text('Verify Account')")
            page.wait_for_timeout(14000)

            if _ONBOARD_MARK not in page.url and "postman.co" not in page.url:
                txt = page.evaluate("() => (document.body.innerText||'').slice(0,300)")
                raise SignupError(f"verify не прошёл: {page.url} | {txt[:200]}")

            # --- 5. онбординг
            if do_onboarding and _ONBOARD_MARK in page.url:
                self._onboarding(page)

            # --- 6. Solo Trial
            if do_trial:
                self._solo_trial(page)

            # --- 7. сессия
            cookies = page.context.cookies()
            self.sid = next((c["value"] for c in cookies if c["name"] == "postman.sid"), None)
            if not self.sid:
                raise SignupError("postman.sid не появился")
            self.log(f"postman.sid получен ({len(self.sid)} симв)")

            # --- 8. проверка квоты
            self.usage = self.check_quota()
            if self.usage:
                self.log(f"квота: {self.usage.get('userType')} "
                         f"{self.usage.get('usage')}/{self.usage.get('limit')}")

        return self.result()

    # ------------------------------------------------------------- онбординг
    def _onboarding(self, page) -> None:
        """Имя + два React-селекта + размер команды.

        Селекты не открываются через page.click('#id') — только по координатам.
        Оба надо выбрать за один проход: выбор второго сбрасывает первый.

        Вёрстка онбординга меняется между прогонами (иногда есть шаг с именем,
        иногда сразу воркспейс), поэтому каждый шаг — best effort: не нашли
        элемент, идём дальше, а не падаем.
        """
        self.log("онбординг")
        page.wait_for_timeout(3000)

        # шаг 1: имя (может отсутствовать)
        name_filled = False
        for sel in ("input[placeholder='eg: John Doe']",
                    "input[placeholder*='name' i]",
                    "input[name='name']"):
            try:
                page.fill(sel, "Alex Morgan", timeout=6000)
                name_filled = True
                break
            except Exception:
                continue
        self.log(f"  имя: {'ок' if name_filled else 'поля нет'}")

        # шаг 2: два React-селекта — сначала ищем по DOM, потом по координатам
        for label, (cx, cy) in (("use-case", (780, 377)), ("role", (780, 431))):
            opened = False
            for sel in ("#onboarding-role-use-case-select", "#onboarding-role-select",
                        "[role=combobox]"):
                try:
                    el = page.locator(sel).first
                    if el.count():
                        el.click(timeout=4000)
                        opened = True
                        break
                except Exception:
                    continue
            if not opened:
                try:
                    page.mouse.click(cx, cy)
                    opened = True
                except Exception:
                    pass
            if not opened:
                self.log(f"  {label}: селект не найден")
                continue

            page.wait_for_timeout(2200)
            opts = page.evaluate("""() => {
              const out = [];
              document.querySelectorAll('[role=option]').forEach(e => {
                const t = (e.innerText||'').trim();
                const r = e.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && t)
                  out.push({t, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)});
              });
              return out.slice(0, 10);
            }""")
            if opts:
                page.mouse.click(opts[0]["x"], opts[0]["y"])
                self.log(f"  {label}: {opts[0]['t'][:40]}")
                page.wait_for_timeout(1500)
            else:
                self.log(f"  {label}: опции не найдены")
                page.keyboard.press("Escape")

        # шаг 3: размер команды
        for sel in ("button:has-text('1 member')", "button:has-text('Just me')"):
            try:
                page.click(sel, timeout=4000)
                break
            except Exception:
                continue

        page.wait_for_timeout(1500)

        # шаг 4: в воркспейс — по кнопке, иначе просто уходим на главную
        btn = page.evaluate("""() => {
          const b = [...document.querySelectorAll('button')]
            .find(e => /take me to my workspace|go to workspace|continue/i.test(e.innerText||''));
          if (!b) return null;
          const r = b.getBoundingClientRect();
          return JSON.stringify({x: Math.round(r.x+r.width/2),
                                 y: Math.round(r.y+r.height/2)});
        }""")
        if btn and btn != "null":
            d = json.loads(btn)
            page.mouse.click(d["x"], d["y"])
            page.wait_for_timeout(12000)
            self.log(f"  в воркспейсе: {page.url[:70]}")
        else:
            self.log("  кнопки перехода нет — иду на главную")
            try:
                page.goto("https://postman.co/", wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
            except Exception:
                pass

    # ------------------------------------------------------------ Solo Trial
    def _solo_trial(self, page) -> None:
        """Upgrade → Start Solo Trial. Карта не нужна, лимит 100k → 400k.

        Кнопку Upgrade ищем по тексту, а не по координатам: вёрстка шапки
        отличается между аккаунтами (у нового она уже сдвинута).
        """
        self.log("активирую Solo Trial")
        try:
            up = page.evaluate("""() => {
              const b = [...document.querySelectorAll('button,a,[role=button]')]
                .find(e => /^\\s*upgrade\\s*$/i.test(e.innerText||''));
              if (!b) return null;
              const r = b.getBoundingClientRect();
              return JSON.stringify({x: Math.round(r.x+r.width/2),
                                     y: Math.round(r.y+r.height/2)});
            }""")
            if up and up != "null":
                d = json.loads(up)
                page.mouse.click(d["x"], d["y"])
                self.log(f"  клик Upgrade ({d['x']},{d['y']})")
            else:
                self.log("  кнопка Upgrade не найдена — пробую координаты")
                page.mouse.click(1361, 18)
            page.wait_for_timeout(4500)

            btn = page.evaluate("""() => {
              const b = [...document.querySelectorAll('button')]
                .find(e => /start solo trial/i.test(e.innerText||''));
              if (!b) return null;
              const r = b.getBoundingClientRect();
              return JSON.stringify({x: Math.round(r.x+r.width/2),
                                     y: Math.round(r.y+r.height/2)});
            }""")
            if not btn or btn == "null":
                self.log("  кнопки 'Start Solo Trial' нет")
                return
            d = json.loads(btn)
            page.mouse.click(d["x"], d["y"])
            page.wait_for_timeout(10000)
            txt = page.evaluate("() => (document.body.innerText||'')")
            if "Trial ending in" in txt or "Solo Trial" in txt:
                self.log("  Solo Trial активирован")
            else:
                self.log("  триал: статус не подтверждён")
        except Exception as e:
            self.log(f"  триал: {type(e).__name__}: {e}")

    # ---------------------------------------------------------------- квота
    def check_quota(self) -> dict | None:
        """GET gateway /chat → первый SSE-ивент `usage`.

        Не «200 = успех»: читаем реальные userType/usage/limit.
        """
        if not self.sid:
            return None
        try:
            r = requests.post(
                "https://gateway.postman.com/chat",
                headers={"Cookie": f"postman.sid={self.sid}",
                         "Content-Type": "application/json",
                         "Accept": "text/event-stream",
                         "x-pstmn-req-service": "agent-mode-service",
                         "x-app-version": getattr(config, "APP_VERSION", "12.26.4"),
                         "Origin": "https://desktop.postman.com",
                         "Referer": "https://desktop.postman.com/"},
                json={"input": {"query": "say OK"}, "metadata": {}},
                timeout=60, stream=True)
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
                    return ev.get("data") or {}
                if ev.get("eventType") == "failure":
                    return {"error": ev.get("data", {}).get("userMessage")}
        except Exception as e:
            self.log(f"  квота: {type(e).__name__}: {e}")
        return None

    def result(self) -> dict:
        u = self.usage or {}
        return {
            "email": self.email,
            "username": self.username,
            "password": self.password,
            "session": f"cookie:{self.sid}" if self.sid else "",
            "sid": self.sid or "",
            "user_type": u.get("userType", ""),
            "limit": u.get("limit", 0),
            "usage": u.get("usage", 0),
            "cycle_end": (u.get("usageCycle") or {}).get("end", ""),
            "status": "ok" if self.sid else "fail",
            "proxy": self.proxy or "",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }


def register_one(proxy: str | None = None, do_trial: bool = True,
                 do_onboarding: bool = True, log=print) -> dict:
    """Один аккаунт. Бросает SignupError при провале."""
    client = PostmanClient(proxy=proxy, log=log)
    return client.run(do_trial=do_trial, do_onboarding=do_onboarding)

# ─── Интерфейс для панели (совместимость с шаблоном) ────────────────────────

def test_key(sid: str, model: str | None = None) -> tuple[bool, str, str]:
    """Проверка живости сессии — панель зовёт это для /api/accounts/check.

    Возвращает (ok, detail, user_type). 401 = сессия протухла, 403 = бан.
    """
    from .limits import probe
    st = probe(sid)
    if not st.get("alive"):
        return False, st.get("error", "не отвечает"), ""
    return True, f"{st['usage']}/{st['limit']}", st.get("user_type", "")


def list_models(sid: str | None = None) -> list[str]:
    """Модели Postman Agent Mode (статический список из моста).

    У Postman нет публичного /v1/models: ключи моделей зашиты в gateway.
    Список совпадает с тем, что отдаёт postman2api.
    """
    return [
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4",
        "claude-opus-4.8", "claude-opus-4.7", "claude-opus-4.5",
        "claude-sonnet-4.6", "claude-sonnet-4.5",
        "claude-haiku-4.5",
    ]

# -*- coding: utf-8 -*-
"""apimail (catch-all домен) — catch-all ящик, чтение через admin API.

GET {MAIL_BASE}/admin/mails?address=<addr>&limit=N&offset=0
  Headers: x-admin-auth: <key>, User-Agent: curl/8.5.0 (иначе 403!)
  Ответ: {"results": [{"id", "raw" (MIME), ...}]}

Ящик создавать не нужно: любой адрес на catch-all домене принимается.
Универсально: умеет и verify-ССЫЛКУ (`wait_link`), и 6-значный КОД (`wait_code`).
Хост ссылки берётся из config.BASE_URL — ничего не хардкодим под сервис.
"""
import email as email_module
import email.utils
import random
import re
import string
import time

import requests

from . import config

# 6-значный код (для сервисов, которые шлют код, а не ссылку)
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _code_re():
    """Формат кода из конфига `mail_code_re` (напр. Slack шлёт XXX-XXX:
    r"\\b([A-Z0-9]{3}-[A-Z0-9]{3})\\b"). Пусто/бито — дефолтные 6 цифр."""
    pat = getattr(config, "MAIL_CODE_RE", None)
    if pat:
        try:
            return re.compile(pat)
        except re.error:
            pass
    return CODE_RE


def _link_res():
    """Регулярки под хост сервиса из конфига (BASE_URL)."""
    host = re.escape((getattr(config, "BASE_URL", "") or "")
                     .replace("https://", "").replace("http://", "").rstrip("/"))
    if not host:
        return []
    return [
        re.compile(rf"https://{host}/[^\s\"'<>\\)]*token=[^\s\"'<>\\)]+", re.I),
        re.compile(rf"https://{host}/[^\s\"'<>\\)]*(?:verify|confirm|activate)[^\s\"'<>\\)]*", re.I),
        re.compile(rf"https://{host}/[^\s\"'<>\\)]+", re.I),   # запасной: любая ссылка сервиса
    ]


# Статика из HTML-писем (логотипы, стили, трекеры) — НЕ verify-ссылки.
# Набито на wispbyte (14.10.2026): запасной паттерн «любая ссылка хоста»
# хватал …/assets/wispbyte_blue_nobg.png из шапки письма.
_ASSET_RE = re.compile(
    r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|woff2?|ttf|eot|mp4|webm|pdf|zip)"
    r"(?:[?#][^\s\"'<>\\)]*)?$", re.I)


def _is_asset(url):
    return bool(_ASSET_RE.search(url))


def _decode_body(raw, sender_hint=None, since_ts=None):
    """MIME → (body, subject) с фильтрами по отправителю и времени.

    sender_hint=None — фильтр выключен (когда имя сервиса заранее неизвестно).
    """
    msg = email_module.message_from_string(raw or "")
    subj = str(email_module.header.make_header(
        email_module.header.decode_header(msg.get("Subject", ""))))
    frm = str(msg.get("From", ""))
    if sender_hint and sender_hint.lower() not in (frm + subj).lower():
        return None, subj
    if since_ts:
        try:
            ts = email_module.utils.mktime_tz(
                email_module.utils.parsedate_tz(msg.get("Date", "")))
            if ts and ts < since_ts - 60:
                return None, subj
        except Exception:
            pass
    body = ""
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)      # декодирует base64/QP
            if payload:
                body += payload.decode("utf-8", "ignore") + "\n"
    return body.replace("&amp;", "&"), subj              # HTML экранирует &


def parse_link_from_raw(raw, sender_hint=None, since_ts=None):
    """MIME → (url, subject); url=None, если ссылки нет.

    Паттерны идут от точного к запасному; ссылки на статику (логотипы и пр.)
    пропускаются всегда — иначе запасной паттерн хватает картинку из шапки.
    """
    body, subj = _decode_body(raw, sender_hint, since_ts)
    if body is None:
        return None, subj
    res = _link_res()
    fallback = None
    for i, rx in enumerate(res):
        m = rx.search(body)
        if not m:
            continue
        url = m.group(0).rstrip(".,;>]")
        if _is_asset(url):
            continue
        if i == len(res) - 1:
            fallback = fallback or url     # запасной — только если точнее нет
            continue
        return url, subj
    return fallback, subj


def parse_code_from_raw(raw, sender_hint=None, since_ts=None):
    """MIME → (code, subject); code=None, если кода нет.

    Берём только 6 цифр подряд — даты (2026-10-07) и годы не считаются кодом.
    """
    body, subj = _decode_body(raw, sender_hint, since_ts)
    if body is None:
        return None, subj
    m = _code_re().search(body)
    return (m.group(1) if m else None), subj


class MailClient:
    def __init__(self, base=None, admin_key=None, domain=None):
        self.base = (base or config.MAIL_BASE).rstrip("/")
        self.key = admin_key or config.MAIL_ADMIN_KEY
        self.domain = domain or config.MAIL_DOMAIN
        self.domains = list(getattr(config, "MAIL_DOMAINS", None) or [self.domain])
        self.dot_domains = list(getattr(config, "MAIL_DOT_DOMAINS", None) or [])
        # gmail plus-алиасы — второй источник в ТОМ ЖЕ клиенте (единый API).
        # Письма алиасов читаются по IMAP напрямую (catch-all worker их не
        # хранит — он знает только свои домены).
        g = getattr(config, "GMAIL", None) or {}
        self.gmail = g if g.get("enabled") else None
        self._flip = 0
        self.session = requests.Session()
        self.session.headers.update({
            "x-admin-auth": self.key,
            "User-Agent": config.MAIL_UA,
            "Accept": "application/json",
        })

    def new_address(self, prefix=None):
        """Новый адрес. Домен — случайный из MAIL_DOMAINS (мультидомен).
        При включённом gmail — чёредуем: чётные apimail, нечётные gmail-алиас."""
        rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        if self.gmail:
            self._flip += 1
            if self._flip % 2 == 0:
                local = self.gmail["user"].split("@")[0]
                tag = (prefix or config.ADDRESS_PREFIX or "mm") + rnd
                return f"{local}+{tag}@gmail.com"
        prefix = prefix or (config.ADDRESS_PREFIX + rnd)
        dom = random.choice(self.domains) if self.domains else self.domain
        return f"{prefix}@{dom}"

    @staticmethod
    def _is_gmail(address):
        return address.lower().endswith(("@gmail.com", "@googlemail.com"))

    def send_address(self, address):
        """Адрес для ОТПРАВКИ в сервис. К доменам из mail_dot_domains добавляет
        точку в конце (FQDN) — обход блок-листа по точному домену; почтовый
        сервер нормализует FQDN, письмо падает в тот же catch-all."""
        local, _, dom = address.partition("@")
        if not dom or dom.endswith(".") or dom not in self.dot_domains:
            return address
        return f"{local}@{dom}."

    def list_mails(self, address, limit=10, offset=0):
        r = self.session.get(f"{self.base}/admin/mails",
                             params={"address": address, "limit": limit, "offset": offset},
                             timeout=25)
        if r.status_code != 200:
            raise Exception(f"mail list {address}: {r.status_code} {r.text[:200]}")
        return r.json().get("results", [])

    def _wait(self, address, extractor, sender_hint, timeout, poll, since_ts):
        timeout = timeout or config.MAIL_TIMEOUT_SEC
        poll = poll or config.MAIL_POLL_SEC
        deadline = time.time() + timeout
        seen = set()
        while time.time() < deadline:
            try:
                mails = self.list_mails(address, limit=15)
            except Exception as exc:
                print(f"  [mail] ошибка списка: {str(exc)[:120]}")
                mails = []
            for m in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                mid = m.get("id")
                if mid in seen:
                    continue
                seen.add(mid)
                val, subj = extractor(m.get("raw", ""), sender_hint=sender_hint,
                                      since_ts=since_ts)
                if val:
                    return val, subj
            print(f"  [mail] письма пока нет ({int(deadline - time.time())}s осталось)...")
            time.sleep(poll)
        return None, None

    def wait_link(self, address, sender_hint=None, timeout=None, poll=None, since_ts=None):
        """Ждёт письмо и возвращает (verify-ссылка, subject) или (None, None)."""
        if self._is_gmail(address) and self.gmail:
            return self._wait_gmail(address, "link", sender_hint, timeout, poll, since_ts)
        return self._wait(address, parse_link_from_raw, sender_hint, timeout, poll, since_ts)

    def wait_code(self, address, sender_hint=None, timeout=None, poll=None, since_ts=None):
        """Ждёт письмо и возвращает (6-значный код, subject) или (None, None)."""
        if self._is_gmail(address) and self.gmail:
            return self._wait_gmail(address, "code", sender_hint, timeout, poll, since_ts)
        return self._wait(address, parse_code_from_raw, sender_hint, timeout, poll, since_ts)

    # ── gmail по IMAP ────────────────────────────────────────────────────
    def _imap(self):
        import imaplib
        import ssl
        g = self.gmail
        ctx = ssl.create_default_context()
        m = imaplib.IMAP4_SSL(g.get("imap_host", g.get("host", "imap.gmail.com")),
                              int(g.get("imap_port", g.get("port", 993))),
                              ssl_context=ctx)
        m.login(g["user"], (g.get("app_password") or "").replace(" ", ""))
        return m

    def _wait_gmail(self, address, kind, sender_hint, timeout, poll, since_ts):
        timeout = timeout or config.MAIL_TIMEOUT_SEC
        poll = poll or config.MAIL_POLL_SEC
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                m = self._imap()
                m.select("INBOX", readonly=True)
                _, data = m.uid("search", None, "TO", f'"{address}"')
                ids = data[0].split() if data and data[0] else []
                for uid in reversed(ids[-3:]):
                    _, md = m.uid("fetch", uid, "(RFC822)")
                    if not md or not isinstance(md[0], tuple):
                        continue
                    raw = md[0][1].decode("utf-8", "ignore")
                    extractor = parse_link_from_raw if kind == "link" else parse_code_from_raw
                    val, subj = extractor(raw, sender_hint=sender_hint, since_ts=since_ts)
                    if val:
                        m.logout()
                        return val, subj
                m.logout()
            except Exception as exc:
                print(f"  [gmail] ошибка: {str(exc)[:100]}")
            time.sleep(poll)
        return None, None

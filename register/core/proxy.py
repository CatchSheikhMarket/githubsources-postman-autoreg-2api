# -*- coding: utf-8 -*-
"""Прокси: разбор строк, маскирование для логов, ПАРАЛЛЕЛЬНАЯ проверка, ротация.

Формат строки: scheme://[user:pass@]host:port (http/https/socks5)
или host:port:user:pass (4 поля — конвертируем).
Источники: config.proxies + config.proxies_file (по строке, # — комментарий).
"""
import os
import random
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

import requests

from . import config


def _brd_customer():
    """Customer BRD из ЕДИНОГО ИСТОЧНИКА (config.brd_customer), не хардкод.

    Customer приходит из proxy/ПРОКСИ.json (core/config.py подтягивает сам).
    Хардкод здесь недопустим: зашитый customer умирает вместе с аккаунтом BRD
    (suspend), и все реггеры разом ловят `407 Account is suspended`, пока
    единый источник уже указывает на рабочий. Пусто → зоны BRD не собираются
    (см. SessionRotator/IspRotator: без customer строки прокси не построить).
    """
    return config.get("brd_customer") or ""


def _brd_host():
    """Хост BRD из единого источника (по умолчанию brd.superproxy.io:44445)."""
    return config.get("brd_host") or "brd.superproxy.io:44445"



def normalize(raw):
    """Любой поддерживаемый формат → канонический URL или None."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s.startswith("#"):
        return None
    if "\t" in s:                      # формат «... \t user:pass@host:port»
        s = s.split("\t")[-1]
    if "://" not in s:
        parts = s.split(":")
        if len(parts) == 4:            # host:port:user:pass
            host, port, user, pw = parts
            s = f"http://{user}:{pw}@{host}:{port}"
        elif len(parts) == 2:
            s = f"http://{s}"
        else:
            return None
    try:
        u = urlsplit(s)
        if not u.hostname or not u.port:
            return None
    except ValueError:
        return None
    return s


def mask(raw):
    """→ «http://user:***@host:port» для логов."""
    if not raw:
        return None
    try:
        u = urlsplit(raw)
        host = (u.hostname or "") + (f":{u.port}" if u.port else "")
        return f"{u.scheme}://{u.username}:***@{host}" if u.username else f"{u.scheme}://{host}"
    except ValueError:
        return raw


def load_all():
    """Все прокси из конфига и файла, нормализованные, без дублей."""
    seen, out = set(), []
    for raw in list(config.PROXIES or []):
        p = normalize(raw)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    path = config.PROXIES_FILE
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                p = normalize(line)
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
    return out


def check_one(proxy, timeout=None):
    """Один прокси жив? Дёргаем /login целевого сервиса."""
    timeout = timeout or config.PROXY_CHECK_TIMEOUT
    try:
        r = requests.get(f"{config.BASE_URL}/login",
                         proxies={"http": proxy, "https": proxy},
                         headers={"User-Agent": config.UA},
                         timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def check_all(proxies=None, workers=None):
    """Параллельная проверка. Возвращает (alive, dead)."""
    proxies = load_all() if proxies is None else proxies
    workers = workers or config.PROXY_CHECK_WORKERS
    if not proxies:
        return [], []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(proxies)))) as ex:
        results = list(ex.map(lambda p: (p, check_one(p)), proxies))
    alive = [p for p, ok in results if ok]
    dead = [p for p, ok in results if not ok]
    return alive, dead


class Rotator:
    """Круговая ротация живых прокси; пустой список → всегда None (напрямую).
    Потокобезопасен — используется параллельными воркерами панели."""

    def __init__(self, alive):
        import threading
        self._lock = threading.Lock()
        self._pool = list(alive or [])
        random.shuffle(self._pool)
        self._i = 0

    def next(self):
        with self._lock:
            if not self._pool:
                return None
            p = self._pool[self._i % len(self._pool)]
            self._i += 1
            return p

    def __len__(self):
        return len(self._pool)


class SessionRotator:
    """Свежий IP на каждый вызов через Bright Data session-ротацию.

    У BRD-зон с поддержкой сессий суффикс `-session-<id>` даёт НОВЫЙ выходной IP
    (проверено 09.10 на datacenter_proxy1/2: 4 сессии → 4 разных IP). То есть это
    не список из N прокси, а фабрика: сколько аккаунтов — столько IP.

    Зоны берутся из config.BRD_ZONES: [{"zone": "...", "password": "..."}].
    """

    def __init__(self, zones=None, session_len=10):
        import random as _r
        import string as _s
        self._zones = list(zones if zones is not None else (config.BRD_ZONES or []))
        self._len = session_len
        self._used = 0
        self._lock = __import__("threading").Lock()
        self._r, self._s = _r, _s

    def _make(self, zone, password):
        sess = "".join(self._r.choices(self._s.ascii_lowercase + self._s.digits, k=self._len))
        return (f"http://brd-customer-{_brd_customer()}-zone-{zone}-session-{sess}"
                f":{password}@{_brd_host()}")

    def next(self):
        if not self._zones:
            return None
        with self._lock:
            z = self._zones[self._used % len(self._zones)]
            self._used += 1
        return self._make(z["zone"], z["password"])

    def __len__(self):
        return len(self._zones)


class IspRotator:
    """ISP-зоны BRD: СТАТИЧЕСКИЕ IP (session-суффикс их НЕ меняет).

    Зачем отдельный класс: у ЧАСТИ сервисов встречается сверка IP минта капчи
    и IP сабмита формы — тогда минт и сабмит обязаны идти с ОДНОГО адреса.
    ISP-зоны статичны, поэтому ротация тут = чередование зон, а не фабрика IP
    (⇒ быстрее выжигаются per-network лимитом; для массового фарма нужна
    residential-зона с ротацией).

    ВАЖНО, это НЕ универсальное правило: сверка IP есть не у всех сервисов,
    и датацентровые прокси годятся для многих из них. Проверяй под свой
    сервис (таблица «минт/сабмит» — GUIDE §2), а не переноси «по умолчанию».
    """

    def __init__(self, zones=None):
        import threading
        self._zones = list(zones if zones is not None else (config.ISP_ZONES or []))
        self._used = 0
        self._lock = threading.Lock()

    def _make(self, zone, password, country=None):
        z = f"{zone}-country-{country}" if country else zone
        return f"http://brd-customer-{_brd_customer()}-zone-{z}:{password}@{_brd_host()}"

    def next(self):
        if not self._zones:
            return None
        with self._lock:
            z = self._zones[self._used % len(self._zones)]
            self._used += 1
        return self._make(z["zone"], z["password"], z.get("country"))

    def __len__(self):
        return len(self._zones)


def make_rotator(alive_static=None):
    """Единая точка: ISP-зоны → сессионные BRD-зоны → статический список.

    ISP идут первыми, потому что они статичны и дают связку «минт+сабмит с
    одного IP» для сервисов со сверкой IP капчи. Сессионные datacenter —
    для сервисов, которым нужна ротация IP и сверки нет. Порядок можно
    поменять под свой сервис: если сверки IP нет, ротирующий DC предпочтительнее
    (свежий IP на аккаунт).
    """
    # Сервис может требовать прямого выхода: например, Postman-Cloudflare
    # отдаёт 403 на все прогретые ISP-IP, а домашний IP проходит.
    if getattr(config, "PROXY_DISABLED", False):
        return Rotator([])
    if config.ISP_ZONES:
        return IspRotator(config.ISP_ZONES)
    if config.BRD_ZONES:
        return SessionRotator(config.BRD_ZONES)
    return Rotator(alive_static or [])


# ── прокси для браузера ─────────────────────────────────────────────────────
# ГРАБЛИ (общее): Chromium ИГНОРИРУЕТ логин/пароль в `--proxy-server` (молча
# вырезает), а BRD-прокси всегда с авторизацией. Значит браузер в BRD напрямую
# не сходит — 407, капча не проходит.
#
# Развилка по движку:
#   * Playwright / Camoufox (Firefox) — принимают прокси как dict
#     {server, username, password}, авторизация работает нативно → релей НЕ нужен.
#     Для них используй browser_proxy_dict().
#   * Голый Chrome с `--proxy-server=<url>` — креды вырезаются → нужен релей
#     `core/proxy_relay.py`; для них используй chrome_proxy_arg().

def browser_proxy_dict(raw):
    """Строка прокси → dict для Playwright/Camoufox {server, username, password}.

    Авторизация поддерживается нативно, релей не нужен.
    """
    if not raw:
        return None
    p = normalize(raw) or (f"http://{raw}" if "://" not in str(raw) else str(raw))
    try:
        u = urlsplit(p)
        if not u.hostname or not u.port:
            return None
    except ValueError:
        return None
    out = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = unquote(u.username)
        out["password"] = unquote(u.password or "")
    return out


def chrome_proxy_arg(raw):
    """Строка прокси → значение `--proxy-server` для голого Chrome.

    Прокси с авторизацией (в т.ч. любой BRD) отдаём через локальный релей
    (`core/proxy_relay.py`): Chrome ходит на 127.0.0.1:<port> без авторизации,
    релей добавляет её и туннелирует. Прокси без авторизации — как есть.
    """
    if not raw:
        return None
    p = normalize(raw) or str(raw)
    u = urlsplit(p)
    if u.username:
        try:
            from . import proxy_relay
            url = proxy_relay.relay_url(p)
            if url:
                return url
        except Exception:
            return None      # без релея прокси с авторизацией в Chrome не сработает
    return f"{u.scheme or 'http'}://{u.hostname}:{u.port}"

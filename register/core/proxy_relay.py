# -*- coding: utf-8 -*-
"""proxy_relay.py — локальный релей авторизации для прокси с паролем в браузере.

Зачем: Chromium НЕ поддерживает логин/пароль в `--proxy-server` (он их молча
вырезает — это не баг, а решение браузера). А прокси Bright Data авторизуются
именем пользователя вида
`brd-customer-<customer>-zone-datacenter_proxy1-session-<id>` + пароль зоны.
Значит, Chrome напрямую в BRD не сходит — вернёт 407 и капча не пройдёт.

Решение: поднимаем на 127.0.0.1 маленький HTTP-релей БЕЗ авторизации для Chrome
и С авторизацией — к BRD. Chrome ходит на `http://127.0.0.1:<port>`, релей
добавляет заголовок `Proxy-Authorization` и туннелирует через BRD. Выходной IP
браузера = exit-IP выбранной BRD-сессии.

На каждую BRD-сессию — свой локальный порт (это и есть ключ к тому, что Chrome
перезапускается при смене аккаунта: `--proxy-server` меняется, значит
запуск браузера видит другой прокси). Релеи живут до выхода
процесса; неиспользованные подчищаются по TTL.

Только stdlib: панель и реггер не тянут сторонние зависимости.
"""
from __future__ import annotations

import base64
import socket
import threading
import time
from urllib.parse import urlsplit

BIND_HOST = "127.0.0.1"
RELAY_TTL_SEC = 1800          # неиспользованный релей живёт полчаса
BUFSIZE = 65536

_relays = {}                  # raw_upstream → Relay
_lock = threading.Lock()


def _upstream_parts(raw):
    """BRD-строка → (host, port, auth_header_value)."""
    u = urlsplit(raw)
    host, port = u.hostname, u.port or 44445
    user = u.username or ""
    password = u.password or ""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return host, port, "Basic " + token


class Relay:
    """Один локальный порт → один BRD-апстрим (с авторизацией)."""

    def __init__(self, raw_upstream):
        self.raw = raw_upstream
        self.up_host, self.up_port, self.auth = _upstream_parts(raw_upstream)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((BIND_HOST, 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self.url = "http://%s:%d" % (BIND_HOST, self.port)
        self.created = time.time()
        self.last_used = time.time()
        self.conns = 0
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    # ── приём от Chrome ────────────────────────────────────────────────
    def _serve(self):
        self._sock.settimeout(1.0)
        while not self._stop:
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.last_used = time.time()
            self.conns += 1
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client):
        try:
            client.settimeout(30)
            head = self._read_head(client)
            if not head:
                return
            upstream = self._connect_upstream()
            if upstream is None:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            try:
                method, target = self._parse_request_line(head)
                if method.upper() == "CONNECT":
                    # HTTPS: просим у BRD туннель до ЦЕЛЕВОГО хоста (не до BRD),
                    # добавив авторизацию; затем гоним байты в обе стороны.
                    if not self._open_tunnel(upstream, target):
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        return
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self._pump_both(client, upstream)
                else:
                    # plain HTTP: абсолютный URI уже в строке запроса — просто
                    # вставляем Proxy-Authorization и отдаём BRD.
                    upstream.sendall(self._rewrite_http(head))
                    self._pump(upstream, client, half=True)
            finally:
                try:
                    upstream.close()
                except OSError:
                    pass
        except Exception:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _open_tunnel(self, upstream, target):
        """CONNECT <target> к BRD с авторизацией. True, если BRD ответил 2xx."""
        req = ("CONNECT %s HTTP/1.1\r\n"
               "Host: %s\r\n"
               "Proxy-Authorization: %s\r\n"
               "Proxy-Connection: keep-alive\r\n\r\n"
               % (target, target, self.auth))
        upstream.sendall(req.encode("latin-1"))
        resp = b""
        while b"\r\n\r\n" not in resp and len(resp) < 8192:
            try:
                chunk = upstream.recv(BUFSIZE)
            except socket.timeout:
                return False
            if not chunk:
                return False
            resp += chunk
        status = resp.split(b" ", 2)[1] if b" " in resp else b""
        return status.startswith(b"2")

    @staticmethod
    def _pump_both(a, b):
        """Двунаправленный туннель между клиентом и апстримом."""
        def one_way(src, dst):
            try:
                while True:
                    data = src.recv(BUFSIZE)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        t = threading.Thread(target=one_way, args=(b, a), daemon=True)
        t.start()
        one_way(a, b)
        t.join(timeout=5)

    def _read_head(self, sock):
        """Прочитать заголовки запроса (до пустой строки)."""
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            try:
                chunk = sock.recv(BUFSIZE)
            except socket.timeout:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _parse_request_line(head):
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = first.split(" ")
        return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")

    def _connect_upstream(self):
        try:
            s = socket.create_connection((self.up_host, self.up_port), timeout=30)
            s.settimeout(60)
            return s
        except OSError:
            return None

    def _authenticate(self, upstream):
        """CONNECT к BRD с авторизацией: ждём 200, иначе — ошибка."""
        req = ("CONNECT %s:%d HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Proxy-Authorization: %s\r\n"
               "Proxy-Connection: keep-alive\r\n\r\n"
               % (self.up_host, self.up_port, self.up_host, self.up_port, self.auth))
        upstream.sendall(req.encode("latin-1"))
        resp = b""
        while b"\r\n\r\n" not in resp and len(resp) < 8192:
            try:
                chunk = upstream.recv(BUFSIZE)
            except socket.timeout:
                return False
            if not chunk:
                return False
            resp += chunk
        # BRD отвечает 200 на успешный CONNECT к себе; далее идёт обычный CONNECT
        return True

    def _rewrite_http(self, head):
        """Убрать возможный Proxy-Authorization клиента и вставить наш."""
        lines = head.split(b"\r\n")
        out = [lines[0]]
        for line in lines[1:]:
            if not line:
                continue
            low = line.lower()
            if low.startswith(b"proxy-authorization:") or low.startswith(b"proxy-connection:"):
                continue
            out.append(line)
        out.append(("Proxy-Authorization: %s" % self.auth).encode("latin-1"))
        out.append(("Proxy-Connection: keep-alive").encode("latin-1"))
        return b"\r\n".join(out) + b"\r\n\r\n"

    @staticmethod
    def _pump(src, dst, half=False):
        try:
            while True:
                data = src.recv(BUFSIZE)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            if not half:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


def relay_for(raw_upstream):
    """Локальный релей для BRD-строки (создаётся один раз на сессию)."""
    if not raw_upstream:
        return None
    with _lock:
        _gc_locked()
        r = _relays.get(raw_upstream)
        if r is None:
            try:
                r = Relay(raw_upstream)
            except OSError:
                return None
            _relays[raw_upstream] = r
        r.last_used = time.time()
        return r


def relay_url(raw_upstream):
    """URL релея для `--proxy-server` (None, если поднять не удалось)."""
    r = relay_for(raw_upstream)
    return r.url if r else None


def _gc_locked():
    now = time.time()
    for raw in list(_relays):
        r = _relays[raw]
        if now - r.last_used > RELAY_TTL_SEC:
            r.close()
            del _relays[raw]


def stats():
    with _lock:
        return {"count": len(_relays),
                "relays": [{"upstream": r.raw.split("@")[-1],
                            "port": r.port, "conns": r.conns} for r in _relays.values()]}


def close_all():
    with _lock:
        for r in _relays.values():
            r.close()
        _relays.clear()


if __name__ == "__main__":  # ручная проверка: python3 core/proxy_relay.py <прокси-строка>
    import sys
    import urllib.request
    if len(sys.argv) < 2:
        print("usage: proxy_relay.py 'http://user:pass@brd.superproxy.io:44445'")
        raise SystemExit(2)
    url = relay_url(sys.argv[1])
    print("релей:", url)
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url}))
    with op.open("https://api.ipify.org", timeout=30) as resp:
        print("exit-IP через релей:", resp.read().decode())
    close_all()

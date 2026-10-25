# -*- coding: utf-8 -*-
"""Двухуровневый лог: обычный + суперподробный.

Обычный лог (``log``) — короткие строки «что случилось», идут в stdout и,
через ``start.sh``, в ``data/<имя>.log``. Читаются глазами: регер сказал
«аккаунт 12/500», мост сказал «chat → 200 (3.6с)».

Подробный лог (``log_debug``) — JSONL в ``data/debug.log``: всё, что нужно,
чтобы разобрать инцидент без воспроизведения. Для моста это полный входящий
payload (system/messages/параметры), каждая попытка с ключом, линией и
прокси, полное тело ответа и — для стрима — собранный текст, reasoning и
usage. Для реггера — каждый шаг флоу с телом запроса/ответа.

Правило: обычный лог не разрастается. Всё «много» уходит в подробный.
Подробный можно выключить конфигом (``verbose_log: false``) или env
(``*_VERBOSE=0``) — тогда ``log_debug`` становится no-op.

Подключение::

    from core.logging import make_logger
    LOG = make_logger("<имя-сервиса>", data_dir=config.DATA_DIR,
                      enabled=config.get("verbose_log", True),
                      max_chars=200000)
    LOG.info("мост поднят на :<порт>")
    rid = LOG.new_req_id()
    LOG.debug("request_in", rid=rid, messages=[...])
    LOG.debug("response", rid=rid, body=...)
"""
import json
import os
import threading
import time


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clip(s, limit):
    """Обрезать длинный текст, пометив факт и размер обрезки."""
    if not isinstance(s, str) or limit is None or len(s) <= limit:
        return s
    return s[:limit] + f"…[+{len(s) - limit} симв.]"


class Logger:
    """Обычный лог в stdout + подробный JSONL в файл. Потокобезопасен."""

    def __init__(self, name, data_dir=None, enabled=True, max_chars=200000,
                 debug_filename="debug.log"):
        self.name = name
        self.enabled = bool(enabled)
        self.max_chars = max_chars
        self.debug_path = (os.path.join(data_dir, debug_filename)
                           if data_dir else None)
        self._lock = threading.Lock()
        self._seq = 0
        self._seq_lock = threading.Lock()

    # ── обычный лог ────────────────────────────────────────────────────
    def info(self, msg):
        print(f"[{_now_iso()}] {msg}", flush=True)

    # алиасы: код реггеров исторически зовёт log()/log_debug()
    def log(self, msg):
        self.info(msg)

    # ── подробный лог ──────────────────────────────────────────────────
    def new_req_id(self, prefix="r"):
        with self._seq_lock:
            self._seq += 1
            n = self._seq
        return "%s%d-%04x" % (prefix, n, int.from_bytes(os.urandom(2), "big"))

    def debug(self, kind, **fields):
        """Строка в подробный лог. Молча no-op, если выключено."""
        if not self.enabled or not self.debug_path:
            return
        row = {"ts": _now_iso(), "kind": kind}
        row.update(fields)
        try:
            line = json.dumps(row, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = json.dumps({"ts": row["ts"], "kind": kind,
                               "error": "не сериализовалось"}, ensure_ascii=False)
        try:
            with self._lock:
                with open(self.debug_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as exc:
            self.info(f"подробный лог не записался: {exc}")

    log_debug = debug  # обратная совместимость с inline-версией в мостах


def make_logger(name, data_dir=None, enabled=True, max_chars=200000,
                debug_filename="debug.log"):
    """Фабрика логгера. ``enabled`` можно взять из конфига: verbose_log."""
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "")
    return Logger(name, data_dir=data_dir, enabled=enabled,
                  max_chars=max_chars, debug_filename=debug_filename)


def sniff_sse(buf, acc):
    """Разобрать SSE-чанки стрима в acc: content / reasoning / usage / finish.

    Возвращает хвост последней незавершённой строки (переносится на следующий
    чанк). Терпимо к мусору: нераспарсенное молча пропускаем — это только лог.
    ``acc`` — словарь с ключами content[], reasoning[], usage, finish.
    """
    text = buf.decode("utf-8", "replace")
    lines = text.split("\n")
    tail = lines.pop()
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if obj.get("usage"):
            acc["usage"] = obj["usage"]
        for ch in (obj.get("choices") or []):
            if ch.get("finish_reason"):
                acc["finish"] = ch["finish_reason"]
            delta = ch.get("delta") or ch.get("message") or {}
            c = delta.get("content")
            if c:
                acc["content"].append(c if isinstance(c, str) else str(c))
            for k in ("reasoning_content", "reasoning", "thinking"):
                v = delta.get(k)
                if v:
                    acc["reasoning"].append(v if isinstance(v, str) else str(v))
                    break
    return tail.encode("utf-8")

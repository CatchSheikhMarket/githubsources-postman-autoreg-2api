#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Панель реггера — PANEL-SPEC (stdlib http.server + threading).

Запуск: bash start.sh  (порт и имя сервиса — свои; реестр autoreg/docs/PANEL-PORTS.md)
  → http://127.0.0.1:<порт>

Эндпоинты (все JSON, слушаем только 127.0.0.1):
  GET  /                     index.html
  GET  /api/summary          сводка: counts, капча, последний прогон, мост <порт>
  GET  /api/accounts         аккаунты (results.load_accounts) + статус ключа из
                             data/keys_state.json (alive→ok, cooldown→unknown,
                             dead→failed, нет записи→unknown)
  POST /api/accounts/check   перепроверка ключей (test_key; ≤5 синхронно, иначе фон)
  POST /api/register         {count} → фоновый прогон {job_id}
  GET  /api/job              прогресс прогона + хвост лога
  POST /api/job/stop         мягкий стоп прогона
  GET  /api/captcha          провайдер/статус капчи
  GET  /api/models           живой /v1/models по первому ключу; fallback — из конфига
  GET  /api/pool             data/keys_state.json моста + counts
  GET  /api/logs?tail=200    хвост data/panel.log
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests                                       # noqa: E402
from core import config                               # noqa: E402
from core import results                              # noqa: E402
from core.register import register_one, test_key, list_models  # noqa: E402

DATA_DIR = config.DATA_DIR
JOB_FILE = os.path.join(DATA_DIR, "job.json")
LOG_FILE = os.path.join(DATA_DIR, "panel.log")
KEYS_STATE_FILE = os.path.join(DATA_DIR, "keys_state.json")
INDEX_FILE = os.path.join(ROOT, "panel", "static", "index.html")

VERSION = "1.0.0"
# имя сервиса для шапки/логов — из конфига (service_name), иначе домен из base_url
SERVICE_NAME = (config.get("service_name")
                or (config.get("base_url") or "").replace("https://", "").replace("http://", "").rstrip("/")
                or "<сервис>")
# порт моста (OpenAI-шлюз) — свой для каждого реггера, вписан в PORTS.md
BRIDGE_PORT = int(config.get("bridge_port") or 0)
# команда запуска моста для подсказок в панели (путь свой у каждого реггера)
BRIDGE_START = config.get("bridge_start") or f"bash <путь-к-мосту>/start.sh"
PANEL_PORT = int(config.get("panel_port") or 9900)

_log_lock = threading.Lock()


def log(msg):
    """Строка в stdout; start.sh ловит его tee'ом в data/panel.log.
    (Прямая запись в файл убрана: иначе каждая строка дублируется.)"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    return line


def log_tail(lines=200):
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            data = f.readlines()
    except OSError:
        return []
    return [ln.rstrip("\n") for ln in data[-max(1, lines):]]


def _tail_file(path, lines=200):
    """Хвост произвольного файла (для логов моста)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.readlines()
    except OSError:
        return []
    return [ln.rstrip("\n") for ln in data[-max(1, lines):]]


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


_write_lock = threading.Lock()


def write_json_atomic(path, obj):
    """Атомарно, потокобезопасно: уникальный tmp на вызов + лок на replace."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
    with _write_lock:
        os.replace(tmp, path)


def _resolve(path):
    """Путь из конфига может быть относительным — резолвим от корня реггера."""
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(ROOT, path))


# ── мост (OpenAI-шлюз на пул ключей, свой порт) ──────────
def bridge_alive():
    """Быстрый GET /health моста (таймаут 2с); мост выключен — не падаем."""
    try:
        r = requests.get(f"http://127.0.0.1:{BRIDGE_PORT}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def load_keys_state():
    """data/keys_state.json → dict key → state (толерантно к форме файла).

    Ожидаемые формы (мост ещё в разработке, принимаем любую):
      {"keys": {"thk_...": {"status": "alive", ...}}, ...}
      {"thk_...": {"status": "cooldown"}, ...}
      {"thk_...": "dead", ...}
    Возвращает (keys_dict, raw_payload или None если файла нет).
    """
    if not os.path.exists(KEYS_STATE_FILE):
        return {}, None
    raw = read_json(KEYS_STATE_FILE, None)
    if not isinstance(raw, dict):
        return {}, raw
    src = raw.get("keys") if isinstance(raw.get("keys"), dict) else raw
    out = {}
    for k, v in src.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, dict):
            out[k] = v
        elif isinstance(v, str):
            out[k] = {"status": v}
    return out, raw


def key_status(api_key, keys_state):
    """Статус ключа аккаунта по keys_state: alive→ok, cooldown→unknown,
    dead→failed, нет записи→unknown. Матчинг: точный или по префиксу."""
    if not api_key:
        return "unknown"
    entry = keys_state.get(api_key)
    if entry is None:
        for k, v in keys_state.items():
            if api_key.startswith(k) or k.startswith(api_key[:20]):
                entry = v
                break
    if entry is None:
        return "unknown"
    st = str(entry.get("status") or "").lower()
    return {"alive": "ok", "dead": "failed", "cooldown": "unknown",
            "exhausted": "exhausted"}.get(st, "unknown")


def public_account(row, keys_state):
    """Карточка аккаунта для панели.

    У Postman нет api_key — статус берём из last_check (его пишет refresh.py
    и кнопка «Обновить квоты»), а квоту из полей limit/usage.
    """
    email = row.get("email") or "?"
    checked = _key_checks.get(email) or {}
    status = row.get("status") or "unknown"
    if checked.get("status") in ("ok", "failed"):
        status = checked["status"]
    elif status == "unknown":
        status = "unknown"
    return {
        "email": email,
        "username": row.get("username") or "",
        "status": status,
        "sid": (row.get("sid") or "")[:16],
        "user_type": row.get("user_type") or "",
        "limit": int(row.get("limit") or 0),
        "usage": int(row.get("usage") or 0),
        "remaining": max(0, int(row.get("limit") or 0) - int(row.get("usage") or 0)),
        "cycle_end": row.get("cycle_end") or "",
        "created_at": row.get("created_at"),
        "last_check": row.get("last_check") or "",
        "note": row.get("note") or "",
        "check_note": checked.get("note") or "",
    }


# ── фон-воркер прогона ───────────────────────────────────
class StopRequested(Exception):
    pass


class JobRunner:
    """Один фон-прогон за раз: прогресс в data/job.json, лог — в panel.log."""

    IDLE = {"job_id": None, "status": "idle", "started_at": None, "finished_at": None,
            "done": 0, "total": 0, "ok": 0, "last_error": None, "results": [],
            "current": None,   # {"index":N, "step":k, "steps_total":6, "label":...}
            "workers": 1, "currents": []}

    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state = dict(self.IDLE)
        saved = read_json(JOB_FILE, None)
        if isinstance(saved, dict) and saved.get("status") != "running":
            self.state = {**self.IDLE, **saved}     # история прошлого прогона
        self.thread = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def is_running(self):
        with self.lock:
            return self.state["status"] == "running"

    def _update(self, **kw):
        with self.lock:
            self.state.update(kw)
            snap = dict(self.state)
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                write_json_atomic(JOB_FILE, snap)
            except OSError:
                pass

    def start(self, count, workers=1):
        with self.lock:
            if self.state["status"] == "running":
                return None
            job_id = "job-" + uuid.uuid4().hex[:8]
            self.state = {**self.IDLE, "job_id": job_id, "status": "running",
                          "started_at": time.time(), "total": count,
                          "workers": workers, "currents": []}
            snap = dict(self.state)
        self.stop_event.clear()
        write_json_atomic(JOB_FILE, snap)
        self.thread = threading.Thread(target=self._run, args=(count, workers),
                                       name="reg-job", daemon=True)
        self.thread.start()
        return job_id

    def stop(self):
        if not self.is_running():
            return False
        self.stop_event.set()
        log("  ⏹ запрошен стоп прогона")
        return True

    def _setup_proxies(self):
        """Ротатор прокси: сессионные BRD-зоны (свежий IP на акк) либо статический
        список. None → напрямую."""
        try:
            from core import proxy as proxy_mod
            rot = proxy_mod.make_rotator()
            if len(rot) > 0:
                if type(rot).__name__ == "SessionRotator":
                    log(f"прокси: BRD session-ротация, зон {len(rot)} "
                        f"(свежий IP на каждый аккаунт)")
                else:
                    log(f"прокси: статический пул, {len(rot)} шт")
                return rot
            log("прокси не заданы — прямые подключения")
        except Exception as exc:
            log(f"прокси: ошибка ({exc}) — прямые подключения")
        return None

    def _register_once(self, rot, on_step):
        """Один аккаунт с одним ретраем на следующем прокси (капча/сеть).
        Возвращает acc-словарь (с error при фейле) или None при стопе."""
        for proxy_try in range(2):
            try:
                # Postman-реггер не принимает on_step (шаги не детализируются);
                # прогресс по аккаунтам панель ведёт сама через _account_result.
                acc = register_one(proxy=rot.next() if rot else None)
            except StopRequested:
                return None
            except Exception as exc:          # один упавший акк не роняет прогон
                acc = {"email": "?", "error": f"{type(exc).__name__}: {exc}"}
                log(f"   ❌ {type(exc).__name__}: {exc}")
            err_low = str(acc.get("error", "")).lower()
            if not acc.get("error") or proxy_try == 1:
                return acc
            # свежий IP на ретрай: капча/сеть/рейт-лимит «fast»
            if any(w in err_low for w in ("turnstile", "timed out", "connection",
                                          "fast", "human check", "too many")):
                log("   ↻ повтор на следующем прокси (капча/сеть/лимит)")
                continue
            return acc
        return {"email": "?", "error": "unknown"}

    def _account_result(self, acc, ok, fails_in_row, last_error):
        """Учесть результат аккаунта (потокобезопасно). Возвращает (ok, fails, last_error)."""
        with self.lock:
            if acc.get("error"):
                last_error = str(acc["error"])
                status = "failed"
                fails_in_row += 1
            else:
                results.save_account(acc)
                ok += 1
                fails_in_row = 0
                status = "ok"
            self.state["done"] += 1
            self.state["ok"] = ok
            self.state["results"] = list(self.state["results"]) + [
                {"email": acc.get("email"), "status": status}]
            snap = dict(self.state)
            try:
                write_json_atomic(JOB_FILE, snap)
            except OSError:
                pass
        if acc.get("error"):
            log(f"   ✗ {last_error}")
        else:
            log(f"   ✓ {acc.get('email')} (key …{str(acc.get('api_key'))[-8:]})")
        return ok, fails_in_row, last_error

    def _finish(self, stopped, ok, last_error):
        snap = self.snapshot()
        if stopped or self.stop_event.is_set():
            status = "stopped"
        elif ok == 0 and snap["total"] > 0:
            status = "failed"
        else:
            status = "done"
        self._update(status=status, ok=ok, finished_at=time.time(),
                     last_error=last_error, current=None, currents=[])
        log(f"■ прогон {snap['job_id']}: {status}, ok={ok}/{snap['total']}"
            + (f", ошибка: {last_error}" if last_error else ""))

    # ── последовательный режим ──────────────────────────────────────────
    def _run(self, count, workers=1):
        if workers > 1:
            return self._run_parallel(count, workers)

        log(f"▶ прогон {self.snapshot()['job_id']}: {count} акк "
            f"(последовательно, интервал {config.BATCH_INTERVAL_SEC}s)")
        rot = self._setup_proxies()
        ok, fails_in_row, last_error, stopped = 0, 0, None, False
        try:
            for i in range(count):
                if self.stop_event.is_set():
                    stopped = True
                    break
                log(f"── аккаунт {i + 1}/{count}")
                self._update(current={"index": i + 1, "step": 0,
                                      "steps_total": 6, "label": "старт"})

                def on_step(msg, idx=i + 1):
                    # лог шагов + точка остановки: флаг проверяется между шагами
                    m = re.match(r"step (\d+)/(\d+):\s*(.*)", str(msg))
                    if m:
                        self._update(current={"index": idx, "step": int(m.group(1)),
                                              "steps_total": int(m.group(2)),
                                              "label": m.group(3)[:140]})
                    log("   " + str(msg))
                    if self.stop_event.is_set():
                        raise StopRequested()

                acc = self._register_once(rot, on_step)
                if acc is None:
                    stopped = True
                    break
                ok, fails_in_row, last_error = self._account_result(
                    acc, ok, fails_in_row, last_error)

                # circuit breaker: N фейлов подряд (сгоревший IP и т.п.) —
                # стоп, чтобы не молотить впустую оставшиеся аккаунты
                if fails_in_row >= getattr(config, "CIRCUIT_BREAKER_FAILS", 5):
                    last_error = (f"circuit breaker: {fails_in_row} фейлов подряд "
                                  f"(последняя: {last_error}) — вероятно, IP в часовом "
                                  f"бане; подождите час или дайте residential-прокси")
                    log(f"⛔ {last_error}")
                    break

                if i < count - 1 and not self.stop_event.is_set():
                    # с прокси у каждого акка свой IP — длинная пауза не нужна;
                    # напрямую (один IP) держим полный интервал против рейт-лимита
                    has_proxies = bool(rot) and len(rot) > 0
                    delay = 3 if has_proxies else config.BATCH_INTERVAL_SEC
                    log(f"   пауза {delay}s...")
                    if self.stop_event.wait(delay):
                        stopped = True
                        break
        except Exception as exc:              # фатально (почта недоступна и т.п.)
            last_error = f"{type(exc).__name__}: {exc}"
            log(f"✖ прогон упал: {last_error}")
            self._update(status="failed", last_error=last_error,
                         finished_at=time.time(), current=None, currents=[])
            return

        self._finish(stopped, ok, last_error)

    # ── параллельный режим: W воркеров, каждый на своём прокси ──────────
    def _run_parallel(self, count, workers):
        from concurrent.futures import ThreadPoolExecutor
        log(f"▶ прогон {self.snapshot()['job_id']}: {count} акк "
            f"ПАРАЛЛЕЛЬНО ×{workers} воркеров")
        rot = self._setup_proxies()
        # статический пул ограничивает число воркеров; session-ротация — нет
        if rot and type(rot).__name__ != "SessionRotator" and len(rot) < workers:
            workers = max(1, len(rot))
            log(f"воркеров урезано до живых прокси: {workers}")

        tasks = list(range(1, count + 1))
        queue_lock = threading.Lock()
        shared = {"ok": 0, "fails": 0, "last_error": None, "stopped": False}
        CB = getattr(config, "CIRCUIT_BREAKER_FAILS", 5)

        def slot_update(slot, **kw):
            with self.lock:
                cur = list(self.state.get("currents") or [None] * workers)
                row = dict(cur[slot] or {})
                row.update(kw)
                cur[slot] = row
                self.state["currents"] = cur
                snap = dict(self.state)
                try:
                    write_json_atomic(JOB_FILE, snap)
                except OSError:
                    pass

        def worker(slot):
            while not shared["stopped"] and not self.stop_event.is_set():
                with queue_lock:
                    if not tasks:
                        return
                    idx = tasks.pop(0)
                log(f"[w{slot + 1}] ── аккаунт {idx}/{count}")
                slot_update(slot, index=idx, step=0, steps_total=6, label="старт")

                def on_step(msg, s=slot, i=idx):
                    m = re.match(r"step (\d+)/(\d+):\s*(.*)", str(msg))
                    if m:
                        slot_update(s, index=i, step=int(m.group(1)),
                                    steps_total=int(m.group(2)),
                                    label=m.group(3)[:140])
                    log(f"   [w{s + 1} #{i}] {msg}")
                    if self.stop_event.is_set():
                        raise StopRequested()

                acc = self._register_once(rot, on_step)
                pause_sec = 0
                with queue_lock:
                    if shared["stopped"] or acc is None:
                        return
                    shared["ok"], shared["fails"], shared["last_error"] = \
                        self._account_result(acc if acc is not None else {},
                                             shared["ok"], shared["fails"],
                                             shared["last_error"])
                    slot_update(slot, done=True)
                    # мягкий breaker: длинный прогон не стопаем, а притормаживаем
                    if shared["fails"] >= CB:
                        if shared["fails"] >= 3 * CB:
                            shared["last_error"] = (f"circuit breaker: {shared['fails']} "
                                                    f"фейлов подряд — стоп")
                            shared["stopped"] = True
                        else:
                            log(f"⏸ {shared['fails']} фейлов подряд — пауза 60с, продолжаю")
                            shared["fails"] = 0
                            pause_sec = 60
                if pause_sec and not self.stop_event.is_set():
                    if self.stop_event.wait(pause_sec):
                        return
                if not shared["stopped"]:
                    time.sleep(3)

        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(worker, range(workers)))
        except Exception as exc:              # фатально (почта недоступна и т.п.)
            shared["last_error"] = f"{type(exc).__name__}: {exc}"
            log(f"✖ прогон упал: {shared['last_error']}")
            self._update(status="failed", last_error=shared["last_error"],
                         finished_at=time.time(), currents=[])
            return

        self._finish(shared["stopped"], shared["ok"], shared["last_error"])


JOB = JobRunner()

# ── фон-сбор лимитов (квоты по аккаунтам) ────────────────
# Квоты меняются только после login-обхода (дорого: рейт-лимит), поэтому кэш
# data/limits.json держим свежим ФОНОМ: раз в интервал добираем протухшие
# (limits.CACHE_TTL) аккаунты. /api/limits читает limits.summary() → кэш, так
# что обновление кэша фоном = актуальные данные в панели без ручной кнопки.
LIMITS_STATE = {"running": False, "done": 0, "total": 0,
                "started_at": None, "finished_at": None, "error": None,
                "next_run_at": None, "interval_sec": None, "skipped": None}
_limits_lock = threading.Lock()
_bg_stop = threading.Event()


def _limits_bg_interval():
    """Интервал фонового опроса СЕРВЕРА за квотами, сек.

    ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН (0): расход считаем сами из своего леджера
    (data/usage.jsonl) в реальном времени, а к серверу ходить незачем —
    это логины по всем аккаунтам (медленно, рейт-лимит 429).

    Включается явно: config.json → `limits_bg_interval_sec` (>0, секунды)
    или env `REG_LIMITS_BG_MIN` (минуты). Нужен только для сверки
    серверного `used_pct`/`exhausted`/`reset_at`."""
    try:
        v = int(os.environ.get("REG_LIMITS_BG_MIN") or 0)
        if v > 0:
            return v * 60
    except ValueError:
        pass
    try:
        v = int(config.get("limits_bg_interval_sec", 0) or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else 0


def _limits_job():
    """Один проход сбора квот. running уже выставлен вызывающим (limits_start)."""
    from core import limits
    try:
        accounts = results.load_accounts()
        LIMITS_STATE["total"] = len(accounts)
        log(f"▶ сбор лимитов по {len(accounts)} аккаунтам…")

        def prog(done, total):
            LIMITS_STATE["done"] = done
            LIMITS_STATE["total"] = total

        # only_missing=True: трогаем только тех, у кого кэш протух (TTL в limits)
        limits.collect(accounts, only_missing=True, max_workers=3, on_progress=prog)
        LIMITS_STATE["done"] = LIMITS_STATE["total"]
        log(f"■ лимиты собраны ({LIMITS_STATE['total']} акк)")
    except Exception as exc:                    # noqa: BLE001
        LIMITS_STATE["error"] = f"{type(exc).__name__}: {exc}"
        log(f"✖ сбор лимитов упал: {LIMITS_STATE['error']}")
    finally:
        LIMITS_STATE["running"] = False
        LIMITS_STATE["finished_at"] = time.time()


def limits_start():
    """Атомарно стартует фоновый сбор квот. False — сбор уже идёт."""
    with _limits_lock:
        if LIMITS_STATE["running"]:
            return False
        LIMITS_STATE.update(running=True, done=0, total=0, error=None,
                            started_at=time.time(), finished_at=None)
    threading.Thread(target=_limits_job, name="limits-job", daemon=True).start()
    return True


def limits_bg_loop():
    """Демон: раз в интервал добирает протухшие квоты, если не идёт другой сбор
    или прогон регистрации (иначе 429 и лишняя нагрузка). Первый сбор — через
    ~60с после старта панели."""
    interval = _limits_bg_interval()
    LIMITS_STATE["interval_sec"] = interval
    if interval <= 0:
        LIMITS_STATE["skipped"] = "выключен (расход считаем локально)"
        log("фон-опрос квот выключен: расход считаем локально из usage.jsonl; "
            "сверка с сервером — кнопкой «Обновить квоты»")
        return
    first = min(60, interval)
    log(f"фон-опрос квот сервера: интервал {interval}s, первый через {first}s")
    if _bg_stop.wait(first):
        return
    while not _bg_stop.is_set():
        if LIMITS_STATE["running"]:
            LIMITS_STATE["skipped"] = "сбор уже идёт"
        elif JOB.is_running():
            LIMITS_STATE["skipped"] = "идёт прогон регистрации"
            log("фон-сбор квот: пропуск — идёт прогон регистрации")
        else:
            LIMITS_STATE["skipped"] = None
            limits_start()
        LIMITS_STATE["next_run_at"] = time.time() + interval
        if _bg_stop.wait(interval):
            return


# ── фон-перепроверка ключей (когда акков > 5) ────────────
_check_lock = threading.Lock()
_check_state = {"running": False, "checked": 0, "total": 0, "ok": 0,
                "started_at": None, "finished_at": None}

# статусы из последней перепроверки: email → status (поверх keys_state,
# пока мост не запущен это единственный источник живых статусов)
_key_checks = {}
_key_checks_lock = threading.Lock()


def _check_one(acc):
    # Postman авторизуется веб-сессией (postman.sid), а не api_key —
    # шаблонная проверка по api_key здесь не применима.
    sid = acc.get("sid") or ""
    if not sid:
        return acc.get("email"), "unknown", "нет sid"
    try:
        from core.limits import probe
        st = probe(sid)
    except Exception as exc:
        return acc.get("email"), "failed", str(exc)[:120]
    if not st.get("alive"):
        err = str(st.get("error") or "не отвечает")
        status = "failed" if ("401" in err or "403" in err) else "unknown"
        return acc.get("email"), status, err[:100]
    limit = int(st.get("limit") or 0)
    usage = int(st.get("usage") or 0)
    note = f"{st.get('user_type', '')} {usage}/{limit}"
    if limit and usage >= limit:
        return acc.get("email"), "failed", f"исчерпан: {note}"
    return acc.get("email"), "ok", note


def _apply_check(email, status, note):
    with _key_checks_lock:
        _key_checks[email] = {"status": status, "note": note, "ts": time.time()}
    mark = "✓" if status == "ok" else "✗"
    log(f"   {mark} {email}: {note}")


def accounts_check(count_hint=None):
    """Перепроверка ключей: ≤5 — синхронно, иначе фоном. → dict для ответа."""
    accs = results.load_accounts()
    total = len(accs)
    if not accs:
        return {"checked": 0}
    if total <= 5:
        log(f"▶ перепроверка ключей: {total} акк (синхронно)")
        checked = ok = 0
        for acc in accs:
            email, status, note = _check_one(acc)
            _apply_check(email, status, note)
            checked += 1
            ok += status == "ok"
        log(f"■ перепроверка: {checked} проверено, ok={ok}")
        return {"checked": checked, "ok": ok}
    with _check_lock:
        if _check_state["running"]:
            return {"checked": 0, "background": True, "note": "перепроверка уже идёт",
                    **_check_state}
        _check_state.update(running=True, checked=0, total=total, ok=0,
                            started_at=time.time(), finished_at=None)
    threading.Thread(target=_check_worker, args=(accs,), name="reg-check",
                     daemon=True).start()
    return {"checked": 0, "background": True, "total": total}


def _check_worker(accs):
    log(f"▶ перепроверка ключей: {len(accs)} акк (фон)")
    ok = 0
    try:
        for acc in accs:
            email, status, note = _check_one(acc)
            _apply_check(email, status, note)
            ok += status == "ok"
            with _check_lock:
                _check_state["checked"] += 1
                _check_state["ok"] = ok
    finally:
        with _check_lock:
            _check_state.update(running=False, finished_at=time.time())
        log(f"■ перепроверка: {_check_state['checked']} проверено, ok={ok}")


# ── капча / модели ───────────────────────────────────────
def captcha_info():
    camoufox = _resolve(config.CAMOUFOX_PYTHON)
    return {
        "provider": "auto (camoufox → capsolver)",
        "configured": bool(camoufox and os.path.exists(camoufox)),
        "capsolver_configured": bool(config.CAPSOLVER_API_KEY),
        "camoufox_python": camoufox,
        "balance": None,
        "note": "Решалка подключается только если сервер реально потребовал капчу "
                "(не превентивно). Тип капчи и решалку под свой сервис настрой "
                "в core/register.py (см. docs/СТАНДАРТ.md §4).",
    }


def models_payload():
    """Живой /v1/models по первому ключу + пометка, что реально доступно.

    /v1/models перечисляет ВСЕ модели сервиса, но free-аккаунту доступны только
    те, что в free-tier (см. кэш лимитов) — остальные требуют баланс.
    """
    accs = results.load_accounts()
    key = next((a.get("api_key") for a in accs if a.get("api_key")), None)
    ids, live, err = [], False, None
    if key:
        try:
            ids = list_models(key)
            live = bool(ids)
        except Exception as exc:
            err = str(exc)[:120]
    else:
        err = "нет аккаунтов с api_key"

    # реальный фри-набор — из кэша лимитов (авторитетно), иначе по суффиксу
    free_set = set()
    try:
        from core import limits as limits_mod
        cache = limits_mod._load_cache().get("accounts", {})
        for q in cache.values():
            for m in (q.get("model_ids") or []):
                free_set.add(m)
                free_set.add(m + ":free")
    except Exception:
        pass

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    models = []
    if live:
        for mid in ids:
            is_free_id = mid.endswith(":free")
            base = mid[:-5] if is_free_id else mid
            on_free_tier = mid in free_set or base in free_set or is_free_id
            if on_free_tier:
                status, note = "available", "free tier — доступна без баланса"
            else:
                status, note = "paid", "требует баланс (в free-tier нет)"
            models.append({"id": mid, "name": base, "status": status,
                           "note": note, "last_checked": now})
        source = f"живой /v1/models (ключ …{key[-8:]})"
    else:
        for mid in config.FALLBACK_FREE_MODELS:
            models.append({
                "id": mid, "name": mid[:-5] if mid.endswith(":free") else mid,
                "status": "unknown",
                "note": ("free tier; fallback — живой список недоступен"
                         + (f": {err}" if err else "")),
                "last_checked": None,
            })
        source = "fallback из config.json" + (f" ({err})" if err else "")

    free_models = [m for m in models if m["status"] == "available"]
    return {"models": models, "source": source, "live": live,
            "free_count": len(free_models), "total_count": len(models),
            "free_ids": [m["id"] for m in free_models]}


# ── HTTP ─────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "reg-panel/" + VERSION

    def log_message(self, *args):
        pass                                   # доступ-лог не нужен, события — в panel.log

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ── GET ──────────────────────────────────────────────
    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html")
            except OSError:
                return self._err(500, "index.html не найден")
        if path == "/api/summary":
            return self._send(200, self._summary())
        if path == "/api/accounts":
            keys_state, _ = load_keys_state()
            with _key_checks_lock:
                checks = dict(_key_checks)
            # счётчик запросов по ключу из леджера моста (для сортировки
            # «использованные / неиспользованные» и колонки «Заметка»)
            try:
                from core import limits as limits_mod
                usage = limits_mod.usage_detailed()
            except Exception:
                usage = {}
            # локальная детекция исчерпания (лимит $ минус наш учёт)
            try:
                lim = limits_mod.summary()
                lim_by_email = {r.get("email"): r for r in (lim.get("accounts") or [])}
            except Exception:
                lim_by_email = {}
            out = []
            for row in results.load_accounts():
                pub = public_account(row, keys_state)
                u = usage.get((pub.get("api_key") or "")[:24]) or {}
                pub["requests"] = u.get("requests", 0)
                pub["total_tokens"] = (u.get("prompt_tokens") or 0) + \
                                      (u.get("completion_tokens") or 0)
                pub["usd"] = round(u.get("usd") or 0.0, 6)
                lr = lim_by_email.get(pub.get("email")) or {}
                pub["exhausted"] = bool(lr.get("exhausted")) or pub["status"] == "exhausted"
                pub["reset_at"] = lr.get("reset_at_est") or lr.get("reset_at")
                pub["remaining_usd"] = lr.get("local_remaining_usd")
                chk = checks.get(pub["email"])
                # ручная перепроверка свежее/точнее, если мост ключ не знает
                if chk and pub["status"] == "unknown":
                    pub["status"] = chk["status"]
                if chk:
                    pub["note"] = chk["note"]
                    pub["last_check"] = chk["ts"]
                out.append(pub)
            return self._send(200, out)
        if path == "/api/job":
            snap = JOB.snapshot()
            snap["log_tail"] = log_tail(40)
            return self._send(200, snap)
        if path == "/api/captcha":
            return self._send(200, captcha_info())
        if path == "/api/models":
            return self._send(200, models_payload())
        if path == "/api/limits":
            from core import limits
            return self._send(200, limits.summary())
        if path == "/api/limits/models":
            from core import limits
            return self._send(200, limits.model_costs())
        if path == "/api/limits/status":
            return self._send(200, dict(LIMITS_STATE))
        if path == "/api/pool":
            return self._send(200, self._pool())
        if path == "/api/bridge/requests":
            try:
                tail = int((query.get("tail") or ["50"])[0])
            except ValueError:
                tail = 50
            return self._send(200, self._bridge_requests(tail))
        if path == "/api/bridge/logs":
            try:
                tail = int((query.get("tail") or ["200"])[0])
            except ValueError:
                tail = 200
            return self._send(200, {"lines": _tail_file(
                os.path.join(config.DATA_DIR, "bridge.log"), tail)})
        if path == "/api/logs":
            try:
                tail = int((query.get("tail") or ["200"])[0])
            except ValueError:
                tail = 200
            lines = log_tail(min(max(tail, 1), 2000))
            return self._send(200, {"count": len(lines), "lines": lines})
        self._err(404, f"нет такого пути: {path}")

    def _summary(self):
        keys_state, _ = load_keys_state()
        accs = results.load_accounts()
        counts = {"total": len(accs), "ok": 0, "failed": 0, "unknown": 0}
        for row in accs:
            st = key_status(row.get("api_key"), keys_state)
            counts[st] = counts.get(st, 0) + 1
        job = JOB.snapshot()
        return {
            "service": SERVICE_NAME,
            "version": VERSION,
            "running": True,                       # панель отвечает = сервис жив
            "job_running": job["status"] == "running",
            "port": self.server.server_port,
            "counts": counts,
            "captcha": {
                "provider": "auto (camoufox→capsolver)",
                "configured": bool(config.CAPSOLVER_API_KEY),
                "balance": None,
            },
            "last_run": {"started_at": job["started_at"], "ok": job["ok"],
                         "total": job["total"], "status": job["status"]},
            "bridge": {"port": BRIDGE_PORT, "alive": bridge_alive(),
                       "start": BRIDGE_START},
        }

    def _bridge_requests(self, tail=50):
        """Живой фид запросов моста к апстриму (из data/usage.jsonl) + сводка.

        Показывает то, что важно при проблемах: статус, латентность, ошибку.
        """
        path = os.path.join(config.DATA_DIR, "usage.jsonl")
        rows = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-max(1, tail):]
        except OSError:
            lines = []
        lat = []
        errs = 0
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            st = r.get("status")
            if isinstance(st, int) and 200 <= st < 300:
                if r.get("latency_ms"):
                    lat.append(r["latency_ms"])
            else:
                errs += 1
            rows.append({
                "ts": r.get("ts"),
                "model": r.get("model"),
                "key": (r.get("key_prefix") or "")[:16],
                "status": st,
                "latency_ms": r.get("latency_ms"),
                "tokens": (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0),
                "usd": r.get("usd"),
            })
        rows.reverse()   # свежие сверху
        lat.sort()
        stats = {
            "shown": len(rows),
            "errors": errs,
            "avg_ms": int(sum(lat) / len(lat)) if lat else None,
            "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
            "slowest_ms": lat[-1] if lat else None,
        }
        return {"requests": rows, "stats": stats,
                "bridge": {"port": BRIDGE_PORT, "alive": bridge_alive(),
                           "start": BRIDGE_START}}

    def _pool(self):
        keys, raw = load_keys_state()
        if raw is None:
            return {"keys": {}, "counts": None, "note": "мост не запущен",
                    "bridge": {"port": BRIDGE_PORT, "alive": bridge_alive(),
                               "start": BRIDGE_START}}
        counts = {"alive": 0, "cooldown": 0, "dead": 0, "other": 0, "total": len(keys)}
        for v in keys.values():
            st = str(v.get("status") or "").lower()
            counts[st if st in ("alive", "cooldown", "dead") else "other"] += 1
        return {"keys": keys, "counts": counts,
                "refill": raw.get("refill") if isinstance(raw, dict) else None,
                "note": None,
                "bridge": {"port": BRIDGE_PORT, "alive": bridge_alive(),
                           "start": BRIDGE_START}}

    # ── POST ─────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body_json()
        if body is None:
            return self._err(400, "тело должно быть JSON-объектом")

        if path == "/api/limits/check":
            # фоновый сбор квот: инкрементально, только у кого кэш протух
            if not limits_start():
                return self._err(409, "сбор уже идёт")
            return self._send(200, {"started": True})

        if path == "/api/register":
            count = body.get("count", 1)
            try:
                count = int(count)
            except (TypeError, ValueError):
                return self._err(400, "count должен быть числом")
            if not 1 <= count <= 500:
                return self._err(400, "count вне диапазона 1..500")
            try:
                workers = int(body.get("workers", 1))
            except (TypeError, ValueError):
                return self._err(400, "workers должен быть числом")
            if not 1 <= workers <= 12:
                return self._err(400, "workers вне диапазона 1..12")
            job_id = JOB.start(count, workers=workers)
            if job_id is None:
                return self._err(409, "прогон уже идёт — сначала Стоп")
            return self._send(200, {"job_id": job_id, "count": count, "workers": workers})

        if path == "/api/job/stop":
            if not JOB.stop():
                return self._err(409, "нечего останавливать: прогон не идёт")
            return self._send(200, {"ok": True, "stopped": True})

        if path == "/api/accounts/check":
            return self._send(200, accounts_check())

        self._err(404, f"нет такого пути: {path}")


def main():
    ap = argparse.ArgumentParser(description="Панель реггера (PANEL-SPEC)")
    ap.add_argument("--port", type=int, default=PANEL_PORT,
                    help=f"порт (по умолчанию {PANEL_PORT}, зарезервирован за реггером)")
    ap.add_argument("--host", default="127.0.0.1", help="адрес (только локальный)")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    if args.host not in ("127.0.0.1", "localhost"):
        print("[!] панель слушает только 127.0.0.1 — host принудительно локальный")
        args.host = "127.0.0.1"

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    # фоновый сбор квот: первый проход ~через 60с, дальше по интервалу
    threading.Thread(target=limits_bg_loop, name="limits-bg", daemon=True).start()
    log(f"панель запущена: http://{args.host}:{args.port} (данные: {DATA_DIR})")
    print(f"панель {SERVICE_NAME}: http://{args.host}:{args.port}  (Ctrl+C — стоп)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        _bg_stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()

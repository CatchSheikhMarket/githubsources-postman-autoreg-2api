# -*- coding: utf-8 -*-
"""Ретраи флакающих шагов (капча, сетевые ошибки)."""
import time


def retry_call(fn, attempts=3, delay=3.0, on_error=None, sleep=time.sleep):
    """fn() до attempts раз → (value, last_error). Пустой/None результат — фейл."""
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if on_error:
                on_error(attempt, exc)
            if attempt < attempts:
                sleep(delay)
            continue
        if value:
            return value, None
        last_error = None
        if attempt < attempts:
            sleep(delay)
    return None, last_error

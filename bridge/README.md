# postman2api — мост Postman AI → OpenAI / Anthropic API

Пул веб-сессий Postman (`postman.sid`), ротация, квота в каждом ответе.
**Чистый Python** — без Go, без сборки exe.

Порт по умолчанию **12049**.

Наполняется реггером [`../register`](../register/README.md): после регистрации
он пишет `cookie:<sid>` в `sessions.txt`.

---

## Запуск

```bash
cp config.example.json config.json
bash start.sh          # сам поставит requests и поднимет мост
```

Windows: двойной клик по `start.bat` — создаст venv, поставит зависимости,
скопирует конфиг и запустит.

Проверка:

```bash
curl http://127.0.0.1:12049/v1/models -H "Authorization: Bearer postman2api"
```

---

## Клиенты

| Клиент | Настройка |
|---|---|
| OpenAI-совместимые | Base URL `http://127.0.0.1:12049/v1`, ключ `postman2api` |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:12049`, `ANTHROPIC_AUTH_TOKEN=postman2api` |

---

## Модели

`gpt-6.1-sol` (дефолт), `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`,
`gpt-5.5`, `gpt-5.4`, `claude-opus-4.8`, `claude-opus-4.7`, `claude-opus-4.5`,
`claude-sonnet-4.6`, `claude-sonnet-4.5`, `claude-haiku-4.5`.

Список живой — `GET /v1/models`.

---

## Эндпоинты

| Метод | Путь | Что |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-совместимый чат (stream и non-stream) |
| POST | `/v1/messages` | Anthropic-совместимый |
| GET | `/v1/models` | каталог моделей |
| GET | `/` | простая страница со статусом пула |
| GET | `/status`, `/health` | состояние моста |

---

## Квота в ответе

Каждый ответ содержит `_postman_usage`:

```json
{
  "_postman_usage": {
    "user_type": "PAID_USER",
    "usage": 1234,
    "limit": 800000,
    "usage_state": "AVAILABLE"
  }
}
```

Когда `usage >= limit`, сессия исключается из ротации и берётся следующая.
Отдельный `prune.py` чистит пул заранее:

```bash
python3 prune.py --dry      # показать
python3 prune.py --archive  # убрать мёртвые в data/archive/
```

---

## Сессии

`sessions.txt` — по одной сессии на строку:

```
cookie:<postman.sid>
<access_token>
# строки с # игнорируются
```

Порядок обхода — по кругу. Сессия, отбитая 401, исключается до перезапуска.
`postman.sid` можно взять в DevTools → Application → Cookies → `postman.co`
или дать реггеру — он сделает это сам.

---

## Настройка

`config.json` (или переменные окружения):

| Параметр | Что |
|---|---|
| `port` | порт (12049) |
| `auth_token` | ключ, который мост спрашивает у клиентов |
| `sessions_file` | файл пула (`sessions.txt`) |
| `default_model` | модель по умолчанию |
| `app_version` | версия клиента для `x-app-version` |
| `MAX_QUERY_BYTES` | лимит поля `input.query` (~9800) |

Хэши инструментов привязаны к версии Postman — при `TOOL_VALIDATION_ERROR`
перехвати свежие и задай `POSTMAN_TOOLS_HASH` / `POSTMAN_TERMS_HASH`
(и `_SDK`-варианты) в `.env`.

---

## Ограничения

- **Неофициальный контракт.** Мост подставляет заголовки десктопного клиента к
  `gateway.postman.com`. Схему могут поменять.
- **`input.query` ~10 000 символов** — лимит поля, не контекст. System уходит
  отдельно (`backgroundContext.AGENTS_MD`), история обрезается.
- **System нельзя дублировать в query** — safety-фильтр отвечает
  «This message got flagged by our safety checks».
- **Инструменты** требуют дочерний product `workspace_tools_sdk_localmode`
  со своими хэшами.
- **Только веб-сессия.** API-ключа «просто так» нет: нужен `postman.sid`.
- **Метрик токенов у Postman нет** — в `usage` отдаётся оценка (символы / 4),
  реальная квота лежит в `_postman_usage`.
- **Tool-цикл упрощён:** результаты инструментов уходят в запрос текстом.
  Полное продолжение диалога (TOOL_RESPONSE) — в планах.
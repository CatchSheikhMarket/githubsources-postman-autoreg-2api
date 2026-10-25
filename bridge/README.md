# postman2api — мост Postman AI → OpenAI / Anthropic API

Пул веб-сессий Postman (`postman.sid`), ротация, квота в каждом ответе.
Порт по умолчанию **12049**.

Наполняется реггером [`../register`](../register/README.md): после регистрации
он пишет `cookie:<sid>` в `sessions.txt`.

---

## Запуск

```bash
cp config.example.json config.json
bash start.sh          # соберёт бинарник (если есть Go) и поднимет мост
```

Windows: двойной клик по `start.bat` — соберёт `postman2api.exe` и запустит.
Флаги: `--rebuild` (пересобрать), `--skip-build` (запустить готовый exe).

Проверка:

```bash
curl http://127.0.0.1:12049/v1/models -H "Authorization: Bearer postman2api"
```

Сборка вручную:

```bash
CGO_ENABLED=0 go build -o postman2api ./cmd/server
```

---

## Клиенты

| Клиент | Настройка |
|---|---|
| OpenAI-совместимые | Base URL `http://127.0.0.1:12049/v1`, ключ `postman2api` |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:12049`, `ANTHROPIC_AUTH_TOKEN=postman2api` |

---

## Модели

`gpt-6.1-sol` (дефолт), `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`,
`claude-opus-4.8`, `claude-opus-4.7`, `claude-opus-4.5`,
`claude-sonnet-4.6`, `claude-sonnet-4.5`, `claude-haiku-4.5`.

Список живой — `GET /v1/models`.

---

## Эндпоинты

| Метод | Путь | Что |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-совместимый чат (stream и non-stream) |
| POST | `/v1/messages` | Anthropic-совместимый |
| GET | `/v1/models` | каталог моделей |
| GET | `/` | дашборд: пул, квоты, расход |
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

Когда `usage >= limit` (`usage_state: EXHAUSTED`), мост исключает сессию из
ротации и переходит к следующей.

---

## Сессии

`sessions.txt` — по одной сессии на строку:

```
cookie:<postman.sid>
<access_token>
# строки с # игнорируются
```

Порядок обхода — по кругу. Сессия, отбитая 401/403, исключается до перезапуска.

Взять `postman.sid` вручную: DevTools → Application → Cookies → `postman.sid`
на домене `postman.co`. Либо дать реггеру — он сделает это сам.

---

## Настройка (.env)

Основное — в `.env.example`. Ключевые параметры:

| Переменная | Что |
|---|---|
| `AUTH_TOKEN` | ключ, который мост спрашивает у клиентов (`postman2api`) |
| `SESSIONS_FILE` | файл пула (по умолчанию `sessions.txt`) |
| `PORT` | порт (12049) |
| `DEFAULT_MODEL` | модель по умолчанию |
| `MAX_QUERY_BYTES` | лимит поля `input.query` (~9800) |
| `POSTMAN_APP_VERSION` | версия клиента для заголовка `x-app-version` |

Хэши инструментов привязаны к версии Postman — при `TOOL_VALIDATION_ERROR`
перехвати свежие и задай `POSTMAN_TOOLS_HASH` / `POSTMAN_TERMS_HASH`.

---

## Ограничения

- **Неофициальный контракт.** Мост подставляет заголовки десктопного клиента к
  `gateway.postman.com`. Postman может поменять схему в любой момент.
- **`input.query` ~10 000 символов** — лимит поля, не контекст. System уходит
  отдельно (`backgroundContext.AGENTS_MD`), история обрезается.
- **System нельзя дублировать в query** — safety-фильтр отвечает
  «This message got flagged by our safety checks».
- **Инструменты** требуют дочерний product `workspace_tools_sdk_localmode`
  со своими хэшами; без него `tools` игнорируются.
- **Только веб-сессия.** API-ключа «просто так» нет: нужен `postman.sid`.

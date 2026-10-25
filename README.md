# postman-autoreg-2api

> **Одна строка:** авторег аккаунтов Postman + мост к их AI-моделям — в одном
> репозитории, связанные в цепочку.

Postman раздаёт **Enterprise Trial на 800 000 AI-кредитов** без привязки карты.
Внутри десктопного клиента живёт полноценная AI-панель: GPT-6.1 Sol, GPT-5.6
Terra/Luna, Claude Opus 4.8, Sonnet 4.6, Haiku 4.5 — 11 моделей.

```
register/   — авторегер: регистрирует аккаунты, проходит Turnstile, активирует триал,
              достаёт cookie postman.sid и пишет в пул
bridge/     — мост (Python): держит пул сессий, ротирует, отдаёт OpenAI- и
              Anthropic-совместимый API
```

Цепочка: `register` создаёт аккаунт → активирует Enterprise Trial → достаёт
`postman.sid` → пишет `cookie:<sid>` в `sessions.txt` → клиент ходит на
`http://127.0.0.1:12049` и не знает про аккаунты.

Подробности — в README каждой папки: [`register/README.md`](register/README.md),
[`bridge/README.md`](bridge/README.md).

---

## Что даёт Postman

| Тариф | AI-кредитов |
|---|---|
| Enterprise Trial | **800 000** |
| Solo Trial | 400 000 |
| Free | 100 000 |

Кредиты общие на все модели, по моделям не делятся. Обычный чат стоит единицы
кредитов, агентные цепочки с инструментами расходуют заметно быстрее.

### Модели (11)

| Семейство | Модели |
|---|---|
| GPT | `gpt-6.1-sol` |
| GPT | `gpt-5.6-terra`, `gpt-5.6-luna` |
| GPT | `gpt-5.5`, `gpt-5.4` |
| Claude Opus | `claude-opus-4.8`, `claude-opus-4.7`, `claude-opus-4.5` |
| Claude Sonnet | `claude-sonnet-4.6`, `claude-sonnet-4.5` |
| Claude Haiku | `claude-haiku-4.5` |

Картинки поддерживают все. Ризонинг есть везде, кроме Opus 4.5, Sonnet 4.5 и
Haiku 4.5.

---

## Как работает регистрация

```
1. signup на identity.getpostman.com  (email + username + password)
2. код из почты (6 цифр) — читается из catch-all ящика по admin API
3. Turnstile на /verify-account       — Camoufox проходит сам, $0
4. онбординг (обязателен)             — имя, роль, размер команды
5. Upgrade → Start Enterprise Trial   — карта не нужна
6. postman.sid из cookies             → sessions.txt моста
```

### Что пришлось преодолеть

| Препятствие | Решение |
|---|---|
| Cloudflare на `identity.getpostman.com` | нужен реальный браузер, не curl |
| Turnstile на шаге verify | **Camoufox (Firefox) проходит сам** за ~2 с |
| Chrome + Playwright палится | Camoufox с `humanize=True` проходит |
| Внешний солвер (capsolver и пр.) | **не работает**: токен привязан к IP и сессии виджета |
| React-селекты в онбординге | клик по координатам + выбор `[role=option]` |
| Прокси только мешает | Bright Data ISP → Cloudflare банит до формы; напрямую проходит |

Полный разбор с граблями и замерами — [`register/docs/РАЗБОР.md`](register/docs/РАЗБОР.md).

---

## Установка

### Что нужно

- **Python 3.10+** и `camoufox` — для реггера
- **Python 3.10+** и `requests` — для моста (Go не нужен, exe не собирается)
- **Catch-all почта** с admin API — чтобы читать коды подтверждения
  (как поднять с нуля — [`register/docs/ПОЧТА.md`](register/docs/ПОЧТА.md))

Прокси **не нужен**: регистрация идёт напрямую с домашнего IP. Bright Data
здесь только вредит — с ним Cloudflare блокирует ещё до формы.

### Windows (одним файлом)

Оба компонента запускаются двойным кликом, ничего дописывать не нужно:

| Файл | Что делает |
|---|---|
| `register\start.bat` | создаёт venv, ставит зависимости, качает camoufox, копирует конфиг, поднимает панель |
| `bridge\start.bat` | создаёт venv, ставит `requests`, копирует конфиг и сессии, поднимает мост |

Флаги: `--reinstall` / `--rebuild` — пересоздать окружение или бинарник,
`--skip-install` / `--skip-build` — запустить без установки.

### Linux / macOS

```bash
cd register
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 -m camoufox fetch          # скачать браузер

cp config.example.json config.json # заполни почту
bash start.sh                      # панель на http://127.0.0.1:9924
```

```bash
cd bridge
cp config.example.json config.json
bash start.sh                      # соберёт бинарник и поднимет на :12049
```

Проверка:

```bash
curl http://127.0.0.1:12049/v1/models -H "Authorization: Bearer postman2api"
```

---

## Использование

Любой OpenAI-совместимый клиент:

| Параметр | Значение |
|---|---|
| Base URL | `http://127.0.0.1:12049/v1` |
| API Key | `postman2api` |
| Модель | `gpt-6.1-sol`, `claude-opus-4.8`, … |

Claude Code:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:12049
export ANTHROPIC_AUTH_TOKEN=postman2api
```

```bash
curl http://127.0.0.1:12049/v1/chat/completions \
  -H "Authorization: Bearer postman2api" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-6.1-sol","messages":[{"role":"user","content":"Say OK"}]}'
```

Каждый ответ содержит `_postman_usage` — сколько кредитов осталось на аккаунте.
Когда `usage >= limit`, мост исключает сессию и берёт следующую.

---

## Ограничения

- **Нет официального API.** Мост разговаривает с тем же шлюзом, что и десктопный
  клиент (`gateway.postman.com`), подставляя заголовки клиента. Это не
  документированный контракт: Postman может его поменять.
- **Хэши инструментов привязаны к версии Postman.** При `TOOL_VALIDATION_ERROR`
  нужно перехватить свежие значения и положить в `.env`.
- **`input.query` ограничен ~10 000 символов** — это лимит поля, а не контекст
  модели. Мост обрезает историю и шлёт system отдельно.
- **Триал живёт 7 дней.** После — либо новый аккаунт, либо падение до Free.
- **Модели только внутри клиента.** Прямого роута «просто ключ» нет: нужна
  именно веб-сессия (`postman.sid`).
- **Turnstile нельзя решить солвером.** Только живой браузер Camoufox.

---

## Структура

```
register/
├── core/            логика регистрации, почта, лимиты
├── panel/           веб-панель (:9924) — запуск прогонов, статистика
├── docs/РАЗБОР.md   полный разбор флоу и грабель
├── docs/ПОЧТА.md    как поднять catch-all почту для авторега
├── tests/           selfcheck без сети
├── config.example.json
└── start.sh

bridge/
├── server.py        мост целиком: чат, Anthropic-адаптер, пул сессий, tools
├── prune.py         уборка мёртвых сессий
├── requirements.txt
└── config.example.json
```

---

## Лицензия

MIT + условие добросовестного использования. Полный текст — [`LICENSE`](LICENSE).

Используй только на своих аккаунтах и в рамках правил сервиса.

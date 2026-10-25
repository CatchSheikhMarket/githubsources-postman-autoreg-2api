# postman-register — авторег Postman AI

Регистрирует аккаунты Postman, активирует **Solo Trial** (лимит 100k → 400k, без карты)
и кладёт веб-сессию (`postman.sid`) в пул моста **postman2api** (`../bridge`, порт 12049).

Панель: **http://127.0.0.1:9924**

---

## Что делает

```
signup (email+username+password)
  → код из apimail
  → Turnstile (Camoufox решает САМ, $0)
  → онбординг (обязателен)
  → Solo Trial (кнопка, карты не требует)
  → postman.sid → sessions.txt моста
```

Полный разбор флоу и грабли: **[docs/РАЗБОР.md](docs/РАЗБОР.md)**

Как поднять почту для авторега: **[docs/ПОЧТА.md](docs/ПОЧТА.md)**

---

## Быстрый старт

Windows: двойной клик по `start.bat` — сам создаст venv, поставит зависимости,
скачает camoufox и поднимет панель.

```bash
cd register

# 1) проверки без сети
python3 tests/selfcheck.py

# 2) панель
bash start.sh --no-browser      # http://127.0.0.1:9924

# 3) один аккаунт из CLI
python3 -c "
import sys; sys.path.insert(0,'.')
from core.register import register_one
from core.results import save_account
r = register_one(proxy=None)
save_account(r)
print(r['user_type'], r['limit'], r['status'])
"
```

---

## Почему Camoufox, а не Chrome

| Движок | Turnstile на Postman |
|---|---|
| Chrome + Playwright (`--disable-blink-features`) | ❌ не проходит |
| Chrome + внешний солвер (peak.fo) | ❌ токен не принимают |
| **Camoufox (Firefox)** | ✅ решает сам за ~2 сек |

Внешний солвер выдаёт валидный токен, но Postman его отвергает: токен привязан
к конкретному виджету и IP-сессии. Поэтому Camoufox — единственный рабочий путь,
и он же бесплатный.

---

## Конфиг

`config.json` (в `.gitignore`, секреты только тут):

| Ключ | Что |
|---|---|
| `panel_port` | 9924 |
| `bridge_port` | 12049 |
| `mail_domains` | домены apimail для регистрации |
| `camoufox_python` | python с установленным camoufox |
| `do_onboarding` | пройти онбординг (обязательно `true`) |
| `do_trial` | активировать Solo Trial (`true` → 400k) |
| `bridge_sessions_file` | куда писать `cookie:<sid>` для моста |
| `headless` | `false` (headful нужен для Turnstile) |

Прокси задаются в `config.json`: `isp_zones` (статичные ISP-IP) или `brd_zones`
(ротация по сессиям). Пусто — реггер ходит напрямую.
Sticky-сессия на аккаунт: браузер и проверка квоты ходят с одного exit-IP.

---

## Данные

```
data/
  accounts.jsonl     полный JSON по аккаунту (email, sid, user_type, limit, usage)
  accounts.txt       combo: email|password|session
  limits.json        кэш квот (обновляется кнопкой в панели)
  panel.log          лог панели
  bridge.log         лог моста
```

Сессии дублируются в `../bridge/sessions.txt` —
мост подхватывает их при следующем запросе.

---

## Квоты

Источник истины — сам gateway: первый SSE-ивент `usage` на любой запрос.

| Состояние | `userType` | `limit` |
|---|---|---|
| свежий аккаунт | `FREE_USER` | 100 000 |
| после Solo Trial | `PAID_USER` | 400 000 |
| триал истёк | `FREE_USER` | 100 000 |

Цикл — 30 дней для free, 7 дней для триала.

Проверка живости отдельным шагом: **401 = сессия протухла**, **403 = бан**,
`usageState: EXHAUSTED` = квота кончилась.

---

## Мост

Живёт рядом: `../bridge/` (порт 12049).

```bash
cd ../bridge && bash start.sh
curl http://127.0.0.1:12049/v1/models -H "Authorization: Bearer postman2api"
```

Мост читает `sessions.txt` построчно (`cookie:<sid>`), ротирует аккаунты,
исключает отбитые 401 и отдаёт квоту в каждом ответе (`_postman_usage`).


---

## Agent Mode — обязателен

Gateway отвечает `Forbidden`, если в теле нет `devModeOptions.isAgentModeEnabled: true`
(плюс `mandatoryContext.workspaceId` и хэши `clientTools`). Квота при этом
показывается нормально — легко принять за «сломанный аккаунт».

Мост `postman2api` собирает тело сам, поэтому через него всё работает:

```bash
curl http://127.0.0.1:12049/v1/chat/completions \
  -H "Authorization: Bearer postman2api" -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4.5","messages":[{"role":"user","content":"say OK"}]}'
```

Подробности и полное тело запроса — `docs/РАЗБОР.md` §10.


---

## Обслуживание пула

### refresh.py — ежедневная проверка

```bash
python3 refresh.py           # проверить всё и почистить пул
python3 refresh.py --dry     # показать без изменений
python3 refresh.py --cron    # тихий режим (для cron)
```

Классифицирует каждую сессию: `alive` / `exhausted` / `dead` (401) /
`banned` (403). Обновляет `accounts.jsonl`, чистит `sessions.txt` моста.

Cron (раз в сутки):
```
0 4 * * * cd <путь>/postman-register && \
  python3 refresh.py --cron >> data/refresh.log 2>&1
```

**Зачем:** `postman.sid` живёт ~30 дней, Solo Trial — 7. Без проверки пул
заполняется протухшими сессиями, и мост тратит слоты впустую.

### import_pool.py — подхватить готовые сессии

```bash
python3 import_pool.py --check   # импорт из sessions.txt + проверка квот
```

Нужен, когда пул наполнялся вручную: панель и refresh читают `accounts.jsonl`,
а не `sessions.txt`.

### prune.py (в папке моста)

```bash
cd ../bridge
python3 prune.py --archive
```

Убирает мёртвые и исчерпанные из пула, складывает в `data/archive/`.

---

## Известные особенности

### Desktop-сессия

Мост подхватывает `access_token` из установленного Postman Desktop **сверх**
`sessions.txt`. Если там залогинен исчерпанный аккаунт — каждый круг даёт
пустой ответ, и из пула его не убрать.

Решение: в `.env` моста `POSTMAN_DATA_DIR=./data/empty_desktop` (пустой
каталог) — Desktop считается незалогиненным.

### Прокси

Наши ISP-IP отдают `403` от Cloudflare Postman (прогреты). В `config.json`
стоит `"proxy_disabled": true`, реггер ходит напрямую. При появлении чистого
residential — поставить `false`.

---

## Что осталось

- [ ] Enterprise trial (второй триал) — путь не найден, вероятно через sales
- [ ] Авто-ротация при `usageState: EXHAUSTED` → пометить и взять следующий
- [ ] Детект бана (403) с записью в `accounts.jsonl`
- [ ] Обновление сессии (postman.sid ~30 дней)
- [ ] Параллельные прогоны (свой прокси на воркер)

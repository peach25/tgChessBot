# Chess.com → Telegram: разбор партий через Stockfish

Telegram-бот, который следит за новыми партиями игроков Chess.com и присылает каждому
подписчику разбор его партий (Stockfish).

## Команды

| Команда | Что делает |
|---|---|
| `/start` | Регистрация: бот спрашивает username на Chess.com |
| `/setuser` | Сменить привязанный username (можно сразу: `/setuser hikaru`) |
| `/status` | Текущий username и дата регистрации |
| `/cancel` | Отменить ввод username |

Несколько Telegram-пользователей могут указать один и тот же chess.com username — это не ограничивается.

### Админские команды

Доступны только пользователю, чей telegram user id совпадает с `ADMIN_TELEGRAM_ID` из `.env`
(в коде id не прописан). Для остальных этих команд нет — бот на них просто не отвечает,
и в меню команд они видны только в личном чате админа.

| Команда | Что делает |
|---|---|
| `/stats` | Число пользователей и chess-аккаунтов; сколько партий обработано: всего отправок, уникальных партий, за 24 часа |
| `/users` | Все записи: telegram_user_id → chess username, дата регистрации, сколько партий отправлено (длинный список режется на несколько сообщений) |

«Отправки» и «уникальные партии» различаются, когда за одним игроком следят несколько человек:
одна партия hikaru, отправленная двум подписчикам, — это 2 отправки и 1 уникальная партия.

## Как работает проверка новых партий

- Раз в `CHECK_INTERVAL_SECONDS` (5 минут) в том же процессе запускается проверка
  (`JobQueue`; первая — через 15 секунд после старта). Если предыдущая ещё идёт, следующая пропускается.
- Архив месяца на Chess.com (`/pub/player/{username}/games/{YYYY}/{MM}`) скачивается **один раз
  на chess-аккаунт** за проверку, даже если за ним следят несколько человек.
- «Новые» партии определяются **отдельно для каждой Telegram-записи**: партия закончилась после её
  `updated_at` (регистрации или последней смены username) и ещё не записана в `processed_games`
  для этой записи. Поэтому после регистрации или `/setuser` старые партии не присылаются,
  а подписчики одного игрока получают партии независимо друг от друга.
- На каждую партию — отдельное сообщение с отчётом. Партия помечается обработанной только после
  успешной отправки. Если пользователь заблокировал бота или Telegram недоступен, партии придут
  при следующей проверке.
- За одну проверку одному пользователю уходит не больше `MAX_GAMES_PER_CHECK` партий (старые первыми),
  остальные — в следующие проверки. Это же ограничивает время работы Stockfish за один цикл.
- Смотрится текущий месяц и, если отслеживание началось раньше, предыдущий — не дальше.

Ошибки Chess.com (недоступен, игрок удалён, пустой профиль) только логируются и не останавливают
проверку остальных. Известная особенность API: у очень активных игроков архив текущего месяца
иногда отвечает `404 "An internal error has occurred"` — это считается временным сбоем, а не
«игрок не найден», проверка повторится через 5 минут.

## Анализ партии

Отчёт по партии: результат и причина (resigned, timeout, …), рейтинги, контроль времени, цвет,
ссылка на партию, а также:

- число **зевков / ошибок / неточностей** и средняя потеря в centipawn — только по ходам отслеживаемого игрока (ходы соперника не разбираются);
- **3 худших хода** игрока: потеря в cp, оценка до и после хода, лучший ход по Stockfish.

Как считается:

- Каждая позиция партии оценивается Stockfish на глубину `STOCKFISH_DEPTH` (по умолчанию 14;
  партия из ~80 полуходов — около 7–10 секунд на одном потоке).
- **Centipawn loss** хода — насколько ухудшилась оценка для сходившего; оценки обрезаются до ±10 пешек,
  чтобы «мат в N» не давал потерь в тысячи cp.
- **Классификация** — по падению вероятности выигрыша (формула Lichess): ≥10% — неточность (`?!`),
  ≥20% — ошибка (`?`), ≥30% — зевок (`??`). Поэтому ход «+8 → +5» в выигранной позиции не считается
  зевком, хотя потеря в cp большая.
- Оценки в отчёте — с точки зрения отслеживаемого игрока (`+` — в его пользу), за какой бы цвет он ни играл.
- Партия, которую получают несколько подписчиков одного игрока, анализируется один раз за проверку.
- Варианты (chess960 и т.п.) не анализируются — отчёт уходит без анализа.

Сбои Stockfish (не установлен, не запустился, завис дольше `STOCKFISH_GAME_TIMEOUT`, упал) и
битый PGN не ломают бота: отчёт уходит с пометкой «анализ недоступен», движок перезапускается
для следующей партии. Процесс Stockfish живёт только во время проверки и закрывается после неё.

## Почему python-telegram-bot

Выбран `python-telegram-bot` (v21+), а не aiogram:

- **Встроенный `JobQueue`** (APScheduler) — периодическая проверка партий раз в 5 минут живёт
  в том же процессе и event loop, что и бот, без отдельного планировщика и межпроцессного обмена.
- **`ConversationHandler`** из коробки — диалог «спроси username → проверь → сохрани» пишется
  декларативно, с таймаутом и `/cancel`.
- Стабильный API, подробная документация; для бота такого размера роутеры/middleware aiogram не нужны.

## Структура

```
bot.py                 # хендлеры команд, точка входа (long polling)
background_checker.py  # фоновая проверка новых партий всех пользователей, формирование отчёта
analyzer.py            # анализ партии через Stockfish (UCI)
chess_api.py           # клиент API Chess.com
db.py                  # SQLite: users, processed_games
config.py              # чтение .env, настройка логирования
chess-bot.service      # systemd unit (установка без Docker)
Dockerfile, docker-compose.yml, install.sh  # установка через Docker
```

## Быстрая установка через Docker (рекомендуется)

Нужны только Docker и плагин `docker compose` ([установка](https://docs.docker.com/engine/install/)).
Stockfish, Python и зависимости уже внутри образа.

```bash
git clone https://github.com/peach25/tgChessBot.git chess-tgbot
cd chess-tgbot
./install.sh          # создаст .env из шаблона и попросит его заполнить
nano .env             # вписать TELEGRAM_BOT_TOKEN и ADMIN_TELEGRAM_ID
./install.sh          # соберёт образ и запустит бота
```

`install.sh` создаёт `.env` только если его ещё нет (с правами `600`) и не запускает бота, пока
токен и id админа не заполнены. Контейнер перезапускается автоматически при падении и после
перезагрузки сервера (`restart: unless-stopped`).

Полезные команды:

```bash
docker compose logs -f                 # логи
docker compose restart                 # перезапуск (например, после правки .env)
git pull && ./install.sh               # обновление до новой версии
docker compose down                    # остановить (данные сохраняются)
```

Данные лежат в именованных томах docker: БД — `bot-data` (`/app/data/bot.db` в контейнере),
файловые логи — `bot-logs`. Резервная копия БД:

```bash
docker compose cp bot:/app/data/bot.db ./bot-backup.db
```

`docker compose down -v` удалит и тома — **вместе с базой пользователей**.

## Настройка `.env`

| Переменная | Обязательна | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | да | Токен от [@BotFather](https://t.me/BotFather) (`/newbot`) |
| `ADMIN_TELEGRAM_ID` | да | Ваш числовой telegram user id |
| `DB_PATH` | нет | Путь к SQLite, по умолчанию `data/bot.db` |
| `LOG_FILE` | нет | Файл лога, по умолчанию `logs/bot.log` (ротация 5 × 5 МБ) |
| `LOG_LEVEL` | нет | `INFO` по умолчанию |
| `CHESSCOM_USER_AGENT` | нет | Chess.com просит указывать контакт в User-Agent |
| `HTTP_TIMEOUT` | нет | Таймаут запросов к Chess.com, сек |
| `CHECK_INTERVAL_SECONDS` | нет | Период проверки новых партий, по умолчанию `300` |
| `MAX_GAMES_PER_CHECK` | нет | Максимум партий одному пользователю за проверку, по умолчанию `5` |
| `STOCKFISH_PATH` | нет | Путь к Stockfish; по умолчанию `stockfish` из `PATH`, иначе `/usr/games/stockfish` |
| `STOCKFISH_DEPTH` | нет | Глубина анализа, по умолчанию `14` |
| `STOCKFISH_THREADS` / `STOCKFISH_HASH_MB` | нет | Потоки и хэш движка, по умолчанию `1` / `64` |
| `STOCKFISH_MOVE_TIME_LIMIT` | нет | Предел на одну позицию, сек (`3`) |
| `STOCKFISH_GAME_TIMEOUT` | нет | Предел на всю партию, сек (`300`) |
| `STOCKFISH_START_TIMEOUT` | нет | Предел на запуск движка, сек (`15`) |

### Как получить свой ADMIN_TELEGRAM_ID

Любой из способов:

- написать боту [@userinfobot](https://t.me/userinfobot) — он ответит вашим `Id`;
- или запустить своего бота, написать ему что-нибудь и открыть
  `https://api.telegram.org/bot<TOKEN>/getUpdates` — значение `message.from.id`.

Это число (не `@username`). После изменения `ADMIN_TELEGRAM_ID` перезапустите бота.
Чтобы админские команды появились в меню, напишите боту хотя бы раз и перезапустите его
(сами команды работают и без этого).

## Установка без Docker (systemd)

Требуется Python 3.10+.

### 1. Stockfish

```bash
sudo apt update && sudo apt install -y stockfish
which stockfish        # обычно /usr/games/stockfish
echo "quit" | /usr/games/stockfish    # должна напечататься версия
```

Если `stockfish` не в `PATH` пользователя сервиса — укажите `STOCKFISH_PATH` в `.env`.

Если в репозитории дистрибутива старая версия — скачайте бинарник с https://stockfishchess.org/download/
и положите, например, в `/usr/local/bin/stockfish`.

### 2. Код и зависимости

```bash
sudo useradd --system --create-home --home-dir /opt/chess-tgbot chessbot
sudo -u chessbot -H bash
cd /opt/chess-tgbot
# скопируйте сюда файлы проекта
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Настройка `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env      # см. «Настройка .env» выше
```

### 4. Первый запуск

Сначала вручную, чтобы увидеть ошибки конфигурации:

```bash
cd /opt/chess-tgbot
.venv/bin/python bot.py
```

В Telegram: `/start` → пришлите username → `/status`. Сыграйте партию на Chess.com — в течение
~5 минут (плюс задержка обновления архива на стороне Chess.com) бот пришлёт её. Для отладки можно
временно поставить `CHECK_INTERVAL_SECONDS=60`. Остановить — `Ctrl+C`.

### 5. Установка как systemd service

```bash
sudo cp /opt/chess-tgbot/chess-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chess-bot
systemctl status chess-bot
journalctl -u chess-bot -f          # логи (также в /opt/chess-tgbot/logs/bot.log)
```

Unit настроен на `Restart=always` — при падении бот перезапустится через 10 секунд.
Если путь установки или пользователь другие — поправьте `User`, `WorkingDirectory`, `ExecStart`,
`ReadWritePaths` в unit-файле.

## Какие данные собираются и хранятся

Бот пока не публичный, но объём данных фиксируется заранее. Всё хранится локально в SQLite
(`DB_PATH`), никуда, кроме Telegram и Chess.com, не передаётся.

**Таблица `users`** — по одной записи на Telegram-пользователя:

| Поле | Что это | Зачем |
|---|---|---|
| `telegram_user_id` | Числовой id пользователя Telegram | Идентификация, привязка username; виден админу в `/users` |
| `chat_id` | Id чата, куда писать | Отправка отчётов |
| `chess_username` | Username на Chess.com (публичный) | Получение партий через публичный API |
| `registered_at` | Дата регистрации (UTC) | `/status` |
| `updated_at` | Дата последней смены username (UTC) | `/status`; от этого момента отслеживаются новые партии |

**Таблица `processed_games`** — история обработанных партий для каждой записи `users`:

| Поле | Что это |
|---|---|
| `user_id` | Ссылка на запись в `users` |
| `game_url` | Ссылка на партию на Chess.com |
| `end_time` | Время окончания партии |
| `processed_at` | Когда бот обработал и отправил партию |

**Не сохраняется:** имя, фамилия, `@username` Telegram, номер телефона, текст сообщений
(кроме введённого chess.com username), сами PGN и результаты анализа — партии каждый раз
скачиваются с Chess.com заново.

**Логи** (`logs/bot.log`, journald или `docker compose logs`) содержат telegram user id и chess.com username при
регистрации/смене username и при ошибках.

**Удаление данных пользователя** вручную:

```bash
# Docker:
docker compose exec bot sqlite3 /app/data/bot.db "PRAGMA foreign_keys = ON; DELETE FROM users WHERE telegram_user_id = <ID>;"
# Без Docker:
# PRAGMA нужна, чтобы каскадно удалились и записи processed_games
sqlite3 data/bot.db "PRAGMA foreign_keys = ON; DELETE FROM users WHERE telegram_user_id = <ID>;"
```

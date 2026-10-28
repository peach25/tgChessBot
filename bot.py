"""Telegram-бот: регистрация chess.com username и команды пользователя.

Запуск: python bot.py (long polling, постоянный процесс).
"""
import html
import logging

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import background_checker
import chess_api
import config
from db import Database

log = logging.getLogger(__name__)

ASK_USERNAME = 1

db: Database | None = None  # создаётся в main()


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _profile_link(username: str) -> str:
    u = html.escape(username)
    return f'<a href="https://www.chess.com/member/{u}">{u}</a>'


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = db.get_user(update.effective_user.id)
    if user:
        await update.message.reply_text(
            f"Ты уже зарегистрирован, слежу за {_profile_link(user.chess_username)}.\n"
            "Сменить: /setuser, подробности: /status",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Привет! Я слежу за новыми партиями на Chess.com и присылаю их разбор.\n\n"
        "Пришли свой username на Chess.com (или ссылку на профиль).\n"
        "/cancel — отмена"
    )
    return ASK_USERNAME


async def setuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Можно сразу: /setuser hikaru
    if context.args:
        return await _save_username(update, context.args[0])
    await update.message.reply_text("Пришли новый username на Chess.com.\n/cancel — отмена")
    return ASK_USERNAME


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _save_username(update, update.message.text)


async def _save_username(update: Update, raw: str) -> int:
    username = chess_api.normalize_username(raw)
    if username is None:
        await update.message.reply_text(
            "Это не похоже на username Chess.com (3–25 символов: латиница, цифры, _ и -). Попробуй ещё раз."
        )
        return ASK_USERNAME

    warning = ""
    try:
        await chess_api.get_player(username)
    except chess_api.PlayerNotFound:
        await update.message.reply_text(
            f"Игрок «{username}» на Chess.com не найден. Проверь написание и пришли ещё раз."
        )
        return ASK_USERNAME
    except chess_api.ChessComError as e:
        # Chess.com лежит — не блокируем регистрацию, просто предупреждаем
        log.warning("Не удалось проверить игрока %s: %s", username, e)
        warning = "\n\n⚠️ Chess.com сейчас не отвечает, проверить существование игрока не удалось."

    tg_user = update.effective_user
    user, previous = db.save_user(tg_user.id, update.effective_chat.id, username)

    if previous is None:
        text = f"Готово! Слежу за партиями {_profile_link(username)}."
        log.info("Новый пользователь tg=%s -> %s", tg_user.id, username)
    elif previous != username:
        text = f"Username изменён: {html.escape(previous)} → {_profile_link(username)}."
        log.info("Пользователь tg=%s сменил username %s -> %s", tg_user.id, previous, username)
    else:
        text = f"Ты и так привязан к {_profile_link(username)}, ничего не изменилось."

    await update.message.reply_text(
        text + "\n/status — текущие настройки" + warning,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отменено.")
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Ты ещё не зарегистрирован. Нажми /start")
        return

    lines = [
        f"Chess.com: {_profile_link(user.chess_username)}",
        f"Зарегистрирован: {_fmt_dt(user.registered_at)}",
    ]
    if user.updated_at != user.registered_at:
        lines.append(f"Username изменён: {_fmt_dt(user.updated_at)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# --- Админские команды. Доступ ограничен фильтром filters.User(ADMIN_TELEGRAM_ID) при регистрации
# хендлеров: для остальных пользователей эти команды просто не существуют (бот молчит).

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = db.stats()
    await update.message.reply_text(
        "📊 <b>Статистика</b>\n"
        f"Пользователей: {s['users']}\n"
        f"Chess.com аккаунтов: {s['chess_accounts']}\n"
        f"Партий обработано (отправок): {s['deliveries']}\n"
        f"  уникальных партий: {s['unique_games']}\n"
        f"  за последние 24 ч: {s['deliveries_24h']}",
        parse_mode=ParseMode.HTML,
    )


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = db.list_users()
    if not users:
        await update.message.reply_text("Зарегистрированных пользователей нет.")
        return

    counts = db.processed_counts()
    lines = [f"👥 <b>Пользователи: {len(users)}</b>", ""]
    for i, u in enumerate(users, 1):
        lines.append(
            f"{i}. <code>{u.telegram_user_id}</code> → {html.escape(u.chess_username)} · "
            f"с {u.registered_at:%Y-%m-%d} · партий: {counts.get(u.id, 0)}"
        )

    # Лимит Telegram — 4096 символов на сообщение
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое необработанное исключение в хендлере логируется, бот продолжает работать."""
    log.error("Ошибка при обработке апдейта %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Что-то пошло не так, попробуй позже.")
        except Exception:
            log.exception("Не удалось отправить сообщение об ошибке")


USER_COMMANDS = [
    BotCommand("start", "Регистрация"),
    BotCommand("setuser", "Сменить username Chess.com"),
    BotCommand("status", "Текущий username"),
    BotCommand("cancel", "Отменить ввод"),
]
ADMIN_COMMANDS = [
    BotCommand("stats", "Статистика (админ)"),
    BotCommand("users", "Список пользователей (админ)"),
]


async def post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(USER_COMMANDS)
        # Админские команды видны в меню только в личном чате админа
        await app.bot.set_my_commands(
            USER_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=config.ADMIN_TELEGRAM_ID)
        )
    except Exception:
        # Например, админ ещё ни разу не писал боту — тогда чат для scope не найден
        log.warning("Не удалось установить меню команд", exc_info=True)


def build_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start), CommandHandler("setuser", setuser)],
            states={ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)]},
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=600,
        )
    )
    app.add_handler(CommandHandler("status", status))

    admin_only = filters.User(user_id=config.ADMIN_TELEGRAM_ID)
    app.add_handler(CommandHandler("stats", admin_stats, filters=admin_only))
    app.add_handler(CommandHandler("users", admin_users, filters=admin_only))
    app.add_error_handler(on_error)

    # Фоновая проверка в том же процессе; если прошлая ещё идёт — следующая пропускается
    app.job_queue.run_repeating(
        background_checker.job,
        interval=config.CHECK_INTERVAL_SECONDS,
        first=15,
        data=db,
        name="check_new_games",
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    return app


def main() -> None:
    global db
    config.setup_logging()
    db = Database(config.DB_PATH)
    log.info("БД: %s", config.DB_PATH)

    app = build_application()
    log.info("Бот запущен (long polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Установка/обновление бота в Docker.
#   Первый запуск: создаёт .env из шаблона и просит вписать токен и id админа.
#   Повторный запуск (после заполнения .env или git pull): собирает образ и (пере)запускает контейнер.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Нужны Docker и плагин docker compose: https://docs.docker.com/engine/install/" >&2
    exit 1
fi

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "Создан .env из шаблона."
fi

# Значение переменной из .env (пусто, если не задана)
env_value() {
    grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | tr -d '[:space:]"'"'"
}

missing=()
[ -n "$(env_value TELEGRAM_BOT_TOKEN)" ] || missing+=(TELEGRAM_BOT_TOKEN)
[[ "$(env_value ADMIN_TELEGRAM_ID)" =~ ^[0-9]+$ ]] || missing+=(ADMIN_TELEGRAM_ID)

if [ ${#missing[@]} -gt 0 ]; then
    echo
    echo "Заполните в $(pwd)/.env: ${missing[*]}"
    echo "  TELEGRAM_BOT_TOKEN — токен от @BotFather"
    echo "  ADMIN_TELEGRAM_ID  — ваш числовой id (узнать: написать @userinfobot)"
    echo
    echo "Например: nano .env   — затем снова запустите ./install.sh"
    exit 1
fi

docker compose up -d --build
echo
echo "Бот запущен. Логи: docker compose logs -f"

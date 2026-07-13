"""
Telegram-бот для уведомлений об изменении цены топ-200 криптовалют.

Версия для GitHub Actions: скрипт делает ОДНУ проверку цен и завершается.
За расписание (как часто запускать) отвечает файл
.github/workflows/check-prices.yml - GitHub сам запускает этот скрипт
по таймеру, скрипту не нужно "спать" в бесконечном цикле.

Токен и chat_id берутся из переменных окружения (GitHub Secrets), а не
хранятся в коде - это безопаснее, особенно если репозиторий публичный.

Логика по тирам и анти-флуд - без изменений:
- TOP-2 (BTC/ETH): суточное падение/рост, только жёстче реагируем
- TOP 3-50: свои пороги
- TOP 51-200: пороги пошире, чтобы не спамить
- Топ-50 дополнительно проверяется на резкий часовой импульс
- После алерта по монете этот тип алерта "спит" ALERT_COOLDOWN_HOURS часов
  (состояние хранится в alert_state.json, который workflow сам коммитит
  обратно в репозиторий после каждого запуска)
"""

import json
import os
import sys
from datetime import datetime, timedelta

import requests

# ============ НАСТРОЙКИ ============

# Токен и chat_id читаются из переменных окружения (GitHub Secrets).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TOP_N_COINS = 200
VS_CURRENCY = "usd"
STORAGE_FILE = "alert_state.json"
ALERT_COOLDOWN_HOURS = 3

TIERS = [
    {
        "name": "TOP-2 (BTC/ETH)",
        "rank_max": 2,
        "daily_drop": 0.1,
        "daily_rise": 0.1,
        "hourly_impulse_drop": 0.1,
        "check_hourly_impulse": True,
    },
    {
        "name": "TOP 3-50 (крупные альты)",
        "rank_max": 50,
        "daily_drop": 0.1,
        "daily_rise": 0.1,
        "hourly_impulse_drop": 0.1,
        "check_hourly_impulse": True,
    },
    {
        "name": "TOP 51-200 (средние альты)",
        "rank_max": 200,
        "daily_drop": 17.0,
        "daily_rise": 20.0,
        "hourly_impulse_drop": None,
        "check_hourly_impulse": False,
    },
]

# ====================================

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def get_tier_for_rank(rank):
    for tier in TIERS:
        if rank <= tier["rank_max"]:
            return tier
    return TIERS[-1]


def load_state():
    if os.path.exists(STORAGE_FILE):
        with open(STORAGE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STORAGE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_on_cooldown(state, coin_id, alert_type):
    key = f"{coin_id}:{alert_type}"
    last_ts = state.get(key)
    if last_ts is None:
        return False
    last_time = datetime.fromisoformat(last_ts)
    return datetime.now() - last_time < timedelta(hours=ALERT_COOLDOWN_HOURS)


def mark_alerted(state, coin_id, alert_type):
    key = f"{coin_id}:{alert_type}"
    state[key] = datetime.now().isoformat()


def fetch_top_coins():
    params = {
        "vs_currency": VS_CURRENCY,
        "order": "market_cap_desc",
        "per_page": min(TOP_N_COINS, 250),
        "page": 1,
        "price_change_percentage": "1h,24h",
        "sparkline": "false",
    }
    resp = requests.get(COINGECKO_URL, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def send_telegram_message(text):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(TELEGRAM_API_URL, data=payload, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[{datetime.now()}] Ошибка отправки в Telegram: {e}")
        try:
            print(f"[{datetime.now()}] Тело ответа Telegram: {resp.text}")
        except NameError:
            pass


def format_message(emoji, title, coin, rank, tier_name, price, change, period_label):
    symbol = coin["symbol"].upper()
    name = coin["name"]
    price_str = f"${price:,.4f}" if price < 1 else f"${price:,.2f}"
    return (
        f"{emoji} <b>{title}</b>\n"
        f"{name} ({symbol}) — ранг #{rank}, категория: {tier_name}\n"
        f"Изменение за {period_label}: {change:+.2f}%\n"
        f"Текущая цена: {price_str}"
    )


def process_coin(coin, state):
    rank = coin.get("market_cap_rank")
    if rank is None:
        return

    tier = get_tier_for_rank(rank)
    coin_id = coin["id"]
    price = coin["current_price"]
    change_24h = coin.get("price_change_percentage_24h_in_currency")
    change_1h = coin.get("price_change_percentage_1h_in_currency")

    if change_24h is not None and change_24h <= -tier["daily_drop"]:
        alert_type = "daily_drop"
        if not is_on_cooldown(state, coin_id, alert_type):
            msg = format_message("🔻", "Суточное падение", coin, rank, tier["name"], price, change_24h, "24ч")
            send_telegram_message(msg)
            mark_alerted(state, coin_id, alert_type)
            print(f"[{datetime.now()}] ALERT daily_drop: {coin_id} {change_24h:.2f}%")

    if change_24h is not None and change_24h >= tier["daily_rise"]:
        alert_type = "daily_rise"
        if not is_on_cooldown(state, coin_id, alert_type):
            msg = format_message("🚀", "Суточный рост", coin, rank, tier["name"], price, change_24h, "24ч")
            send_telegram_message(msg)
            mark_alerted(state, coin_id, alert_type)
            print(f"[{datetime.now()}] ALERT daily_rise: {coin_id} {change_24h:.2f}%")

    if tier["check_hourly_impulse"] and change_1h is not None:
        threshold = tier["hourly_impulse_drop"]
        if threshold is not None and change_1h <= -threshold:
            alert_type = "hourly_impulse"
            if not is_on_cooldown(state, coin_id, alert_type):
                msg = format_message("⚠️", "Резкий импульс (1 час)", coin, rank, tier["name"], price, change_1h, "1ч")
                send_telegram_message(msg)
                mark_alerted(state, coin_id, alert_type)
                print(f"[{datetime.now()}] ALERT hourly_impulse: {coin_id} {change_1h:.2f}%")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Ошибка: TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID не заданы "
              "(проверь GitHub Secrets в настройках репозитория).")
        sys.exit(1)

    state = load_state()
    print(f"[{datetime.now()}] Запуск проверки топ-{TOP_N_COINS} монет.")

    try:
        coins = fetch_top_coins()
    except requests.RequestException as e:
        print(f"[{datetime.now()}] Ошибка получения данных с CoinGecko: {e}")
        sys.exit(1)

    for coin in coins:
        try:
            process_coin(coin, state)
        except Exception as e:
            print(f"[{datetime.now()}] Ошибка обработки монеты {coin.get('id')}: {e}")

    save_state(state)
    print(f"[{datetime.now()}] Проверка завершена.")


if __name__ == "__main__":
    main()

"""
One-shot test: run monitoring and send morning report to the daily topic.
Prints the report locally too so you can verify format without Telegram.
"""
import asyncio
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()

from telegram import Bot, InlineKeyboardMarkup
from main import get_all_accounts, run_monitoring_all, format_report, send_daily_message


async def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN not set")

    print("=== Fetching accounts from OnlyMonster... ===")
    accounts = get_all_accounts()
    print(f"Found {len(accounts)} account(s): {[(a['id'], a['platform']) for a in accounts]}")

    if accounts:
        print("\n=== Running monitoring... ===")
        platforms_status = run_monitoring_all(accounts)
        for p, s in platforms_status.items():
            print(f"  [{p}] active={s['active_count']}  "
                  f"hot={len(s['hot'])}  watching={len(s['watching'])}  special={len(s['special'])}")

        text, keyboard = format_report(platforms_status)
        markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    else:
        from datetime import datetime
        text = (
            f"☀️ Доброе утро! ({datetime.now().strftime('%d.%m.%Y')})\n\n"
            "⚠️ Аккаунты OnlyMonster не найдены."
        )
        markup = None

    print("\n=== REPORT PREVIEW ===")
    print(text)
    print("=== END PREVIEW ===\n")

    chat_id  = os.getenv("TELEGRAM_CHAT_ID")
    topic_id = os.getenv("TELEGRAM_TOPIC_DAILY_ID")
    print(f"Sending to chat_id={chat_id}  topic_id={topic_id}...")

    async with Bot(token=token) as bot:
        try:
            await send_daily_message(bot, text, markup)
            print("Sent to Telegram!")
        except Exception as e:
            print(f"Telegram send failed: {e}")
            print("(Report content was printed above)")


asyncio.run(main())

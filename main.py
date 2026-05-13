import asyncio
import hashlib
import hmac
import html as _html
import json
import logging
import os
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

import pytz

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

from onlymonster import OnlyMonsterClient
from anthropic_client import AnthropicClient

load_dotenv()

# ─── CONSTANTS ────────────────────────────────────────────────────────────────
DAYS_WITHOUT_PURCHASE_ALERT = 3
DAYS_WITHOUT_REPLY_ALERT    = 2
HOT_LIST_MAX_SILENT_DAYS    = 7
SUBSCRIPTION_MILESTONE_30   = 30
SUBSCRIPTION_MILESTONE_365  = 365
SCHEDULE_HOUR_MSK           = 13   # 13:00 МСК — daily report and Sunday analyze
SCHEDULE_WEEKDAY_WEEKLY     = 6    # 0=Пн … 6=Вс
TZ_MSK = pytz.timezone("Europe/Moscow")
# ──────────────────────────────────────────────────────────────────────────────

FANS_DATA_FILE         = Path(__file__).parent / "fans_data.json"
WEEKLY_REPORTS_FILE    = Path(__file__).parent / "weekly_reports.json"
STYLE_GUIDE_FILE       = Path(__file__).parent / "style_guide.json"
SENT_LOG_FILE          = Path(__file__).parent / "sent_log.json"
BOT_STATUS_FILE        = Path(__file__).parent / "bot_status.json"
LOG_FILE               = Path(__file__).parent / "bot.log"

PLATFORM_LABELS = {
    "onlyfans": "OnlyFans",
}
PLATFORM_COMPETITORS = {
    "onlyfans": "Fansly",
}
PLATFORM_ORDER = ["onlyfans"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

om_client     = OnlyMonsterClient()
claude_client = AnthropicClient()


# ─── STORAGE ──────────────────────────────────────────────────────────────────

def load_fans() -> dict:
    if not FANS_DATA_FILE.exists():
        return {}
    with open(FANS_DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_fans(fans: dict) -> None:
    with open(FANS_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(fans, f, ensure_ascii=False, indent=2)


def load_weekly_reports() -> list[dict]:
    if not WEEKLY_REPORTS_FILE.exists():
        return []
    with open(WEEKLY_REPORTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("reports", [])


def save_weekly_report(entry: dict) -> None:
    reports = load_weekly_reports()
    reports.append(entry)
    reports = reports[-52:]  # keep last 52 weeks (1 year)
    with open(WEEKLY_REPORTS_FILE, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)


def load_style_guide() -> dict:
    if not STYLE_GUIDE_FILE.exists():
        return {}
    with open(STYLE_GUIDE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_style_guide(data: dict) -> None:
    with open(STYLE_GUIDE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _style_prefix() -> str:
    """Returns style guide instruction to prepend to fan-facing message prompts."""
    guide = load_style_guide().get("guide", "")
    if not guide:
        return ""
    return (
        f"Пиши строго в стиле модели согласно этому style guide:\n{guide}\n\n"
        "Копируй манеру речи, длину сообщений, использование эмодзи точь-в-точь.\n\n"
    )


def load_bot_status() -> dict:
    if not BOT_STATUS_FILE.exists():
        return {}
    with open(BOT_STATUS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_bot_status(data: dict) -> None:
    with open(BOT_STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def is_stopped() -> bool:
    return bool(load_bot_status().get("stop_all"))


def load_sent_log() -> list[dict]:
    if not SENT_LOG_FILE.exists():
        return []
    with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def append_sent_log(entry: dict) -> None:
    log = load_sent_log()
    log.append(entry)
    with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log[-500:], f, ensure_ascii=False, indent=2)



# ─── HELPERS ──────────────────────────────────────────────────────────────────

def unwrap_list(data, key: str = "data") -> list:
    return data if isinstance(data, list) else data.get(key, [])


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def clean_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text  = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def get_all_accounts() -> list[dict]:
    """Return all OM accounts with their platform field."""
    try:
        items  = om_client.get_accounts()   # already unwrapped by client
        result = []
        for acct in items:
            aid      = str(acct.get("id") or acct.get("_id") or "")
            platform = (acct.get("platform") or acct.get("type") or "unknown").lower().strip()
            plat_aid = str(acct.get("platform_account_id") or acct.get("platformAccountId") or "")
            if aid:
                result.append({
                    "id":                  aid,
                    "platform":            platform,
                    "name":                acct.get("name", ""),
                    "platform_account_id": plat_aid,
                })
        return result
    except Exception as e:
        logger.error("get_all_accounts: %s", e)
        return []


def days_to_birthday(bday_str: str | None) -> int | None:
    if not bday_str:
        return None
    today = datetime.now().date()
    for fmt in ("%B %d", "%d %B", "%d.%m", "%m/%d"):
        try:
            bday = datetime.strptime(bday_str, fmt).replace(year=today.year).date()
            diff = (bday - today).days
            if diff < 0:
                bday = bday.replace(year=today.year + 1)
                diff = (bday - today).days
            return diff
        except ValueError:
            continue
    return None


_TG_MAX_LEN = 4000  # Telegram hard limit is 4096; leave buffer for safety


def _split_message(text: str, max_len: int = _TG_MAX_LEN) -> list[str]:
    """Split text into chunks ≤ max_len, preferring paragraph breaks."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while len(text) > max_len:
        pos = text.rfind("\n\n", 0, max_len)
        if pos < max_len // 3:
            pos = text.rfind("\n", 0, max_len)
        if pos < max_len // 3:
            pos = max_len
        chunks.append(text[:pos].rstrip())
        text = text[pos:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def send_to_topic(bot, text: str, topic_id: str | int | None,
                        reply_markup=None, parse_mode: str | None = None):
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        logger.error("TELEGRAM_CHAT_ID not set")
        return None
    chunks = _split_message(text or "—")
    last_msg = None
    for i, chunk in enumerate(chunks):
        kwargs: dict = {}
        if topic_id:
            kwargs["message_thread_id"] = int(topic_id)
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if reply_markup and i == len(chunks) - 1:
            kwargs["reply_markup"] = reply_markup
        last_msg = await bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
    return last_msg


async def send_daily_message(bot, text: str, reply_markup=None,
                              parse_mode: str = "HTML") -> None:
    """Send to the daily topic (Дневная инфа). Defaults to HTML parse mode."""
    await send_to_topic(bot, text, os.getenv("TELEGRAM_TOPIC_DAILY_ID"), reply_markup, parse_mode)


async def send_misc_message(bot, text: str, reply_markup=None,
                            parse_mode: str | None = None) -> None:
    """Send to the misc topic (Всякое, thread_id=TELEGRAM_TOPIC_MISC_ID)."""
    await send_to_topic(bot, text, os.getenv("TELEGRAM_TOPIC_MISC_ID"), reply_markup, parse_mode)


async def send_chatters_message(bot, text: str, reply_markup=None) -> None:
    """Send to the chatters topic (Чаттерс, thread_id=TELEGRAM_TOPIC_CHATTERS_ID)."""
    await send_to_topic(bot, text, os.getenv("TELEGRAM_TOPIC_CHATTERS_ID"), reply_markup)


async def send_alerts_message(bot, text: str, reply_markup=None) -> None:
    """Send to the alerts topic (Оповещения, thread_id=TELEGRAM_TOPIC_ALERTS_ID)."""
    await send_to_topic(bot, text, os.getenv("TELEGRAM_TOPIC_ALERTS_ID"), reply_markup)


async def send_nightdrive_message(bot, text: str, reply_markup=None):
    """Send to the nightdrive topic (thread_id=TELEGRAM_TOPIC_NIGHTDRIVE_ID). Returns Message."""
    return await send_to_topic(bot, text, os.getenv("TELEGRAM_TOPIC_NIGHTDRIVE_ID", "52"), reply_markup)


async def send_broadcasts_message(bot, text: str, reply_markup=None):
    """Send to the broadcasts topic (thread_id=TELEGRAM_TOPIC_BROADCASTS_ID). Returns Message."""
    return await send_to_topic(bot, text, os.getenv("TELEGRAM_TOPIC_BROADCASTS_ID", "94"), reply_markup)


# ─── /analyze ─────────────────────────────────────────────────────────────────

import re as _re
_TAG = _re.compile(r"<[^>]+>")


def _clean(text) -> str:
    """Strip HTML tags from a value so it's safe inside parse_mode=HTML messages."""
    return _TAG.sub("", str(text)).strip() if text else ""


ANALYZE_PROMPT = """\
Ты — аналитик OnlyFans чата. Изучи переписку и составь досье фана.

Переписка ({msg_count} сообщений):
{conversation}

Верни ТОЛЬКО JSON без markdown:
{{
  "name": "имя или ник как представился, иначе null",
  "birthday": "возраст или дата рождения если упоминал, иначе null",
  "job": "работа и финансовое положение по контексту, иначе null",
  "hobbies": ["хобби", "интересы"],
  "personal_life": "личная жизнь, характер, ситуация в жизни",
  "location": "город/страна/часовой пояс если упоминал, иначе null",
  "warmth": <1-5: 1=холодный/формальный, 5=очень влюблён/эмоционален>,
  "engagement": <1-5: 1=редко отвечает, 5=пишет сам и активно реагирует>,
  "fetishes": ["что упоминал или на что реагировал"],
  "chat_style": "короткая подсказка чаттеру как общаться с этим фаном",
  "notes": "другие важные наблюдения или null"
}}\
"""

UPDATE_PROMPT = """\
Обнови досье фана. Текущие данные:
{existing_profile}

Новые сообщения за последние 7 дней ({msg_count} сообщений):
{conversation}

Верни ТОЛЬКО JSON без markdown. Обновляй поле только если появились новые данные, иначе null:
{{
  "warmth": <1-5 или null если без изменений>,
  "engagement": <1-5 или null если без изменений>,
  "fetishes": ["только новые фетиши которых ещё не было в досье"],
  "chat_style": "обновлённая подсказка или null если без изменений"
}}\
"""

DEEP_ANALYZE_PROMPT = """\
Ты — аналитик OnlyFans. Сделай МАКСИМАЛЬНО ПОДРОБНОЕ досье VIP фана по ВСЕЙ его переписке.

Переписка ({msg_count} сообщений — полная история):
{conversation}

ОСОБЫЕ ЗАДАЧИ:
1. Тщательно ищи ВСЕ фетиши и предпочтения по всей переписке. Примеры: ведьмы/магия, \
ноги/стопы, косплей, ролевые игры, BDSM, конкретные персонажи, темы фантазий.
   Если фан хоть раз упомянул что-то интересное — включи в fetishes.
2. Определи точный стиль общения: что его заводит, какие слова/темы он любит.
3. Найди личные детали: имя, возраст, работа, семья, город.

Верни ТОЛЬКО JSON без markdown:
{{
  "name": "как представился, null если нет",
  "birthday": "возраст или дата, null если нет",
  "job": "работа, финансовое положение, null если нет",
  "hobbies": ["все хобби и интересы которые упоминал"],
  "personal_life": "личная жизнь, характер, жизненная ситуация",
  "location": "город/страна/часовой пояс, null если нет",
  "warmth": <1-5: 1=холодный/деловой, 5=очень влюблён/эмоционален>,
  "engagement": <1-5: 1=редко пишет, 5=пишет сам часто и активно>,
  "fetishes": ["ПОЛНЫЙ список — фетиши, предпочтения, темы, косплеи, всё что нашёл"],
  "chat_style": "подробная инструкция чаттеру: как общаться, что говорить, какие темы поднимать",
  "notes": "все остальные важные детали"
}}\
"""


_AUTO_USERNAME = _re.compile(r"^u\d+$", _re.IGNORECASE)


def fan_display_name(fan_id: str, fan_data: dict) -> str:
    """Priority: custom_name → @realusername → display_name/profile.name → fan_id.
    Always appends (fan_id) so the ID is visible even when the name is wrong."""
    custom = (fan_data.get("custom_name") or "").strip()
    if custom:
        return f"{custom} ({fan_id})"
    uname = (fan_data.get("username") or "").strip()
    name  = (fan_data.get("display_name") or
             (fan_data.get("profile") or {}).get("name") or "").strip()
    if uname and not _AUTO_USERNAME.match(uname):
        return f"@{uname} ({fan_id})"
    if name:
        return f"{name} ({fan_id})"
    return str(fan_id)


def _stars(n, max_n: int = 5) -> str:
    if n is None:
        return "—"
    n = max(1, min(int(n), max_n))
    return "⭐" * n + "☆" * (max_n - n)


def _truncate_sentence(text: str, max_len: int = 120) -> str:
    """Truncate at the last sentence boundary before max_len chars."""
    if len(text) <= max_len:
        return text
    for sep in (".", "!", "?", ";"):
        pos = text.rfind(sep, 0, max_len)
        if pos >= 15:
            return text[:pos + 1]
    pos = text.rfind(" ", 0, max_len)
    return (text[:pos] + "…") if pos > 0 else text[:max_len] + "…"


def _money_stars(total: float | None) -> str:
    """Dollar amount → star rating with amount label. Used in /fan profile cards."""
    if total is None or total <= 0:
        return "—"
    if total < 10:   n = 1
    elif total < 50:  n = 2
    elif total < 100: n = 3
    elif total < 600: n = 4
    else:             n = 5
    return "⭐" * n + "☆" * (5 - n) + f"  (${total:.0f})"


def _money_stars_compact(total: float | None) -> str:
    """Stars only — no dollar amount. Used in compact report entries."""
    if not total or total <= 0: n = 0
    elif total < 10:  n = 1
    elif total < 50:  n = 2
    elif total < 100: n = 3
    elif total < 600: n = 4
    else:             n = 5
    return ("⭐" * n + "☆" * (5 - n)) if n else "—"


# Transactions cache: platform_account_id → (list_of_txns, fetched_at)
# Avoids re-downloading 1000+ records for every fan during analyze_all runs.
_txn_cache: dict[str, tuple[list[dict], datetime]] = {}
_TXN_CACHE_TTL_SEC = 600  # 10 minutes


async def _all_transactions_cached(platform_account_id: str) -> list[dict]:
    """Return full transaction list for a platform account, using a 10-min in-memory cache."""
    cached = _txn_cache.get(platform_account_id)
    if cached:
        txns, fetched_at = cached
        if (datetime.now() - fetched_at).total_seconds() < _TXN_CACHE_TTL_SEC:
            logger.debug("_all_transactions_cached: cache hit for %s (%d txns)", platform_account_id, len(txns))
            return txns
    txns = await asyncio.to_thread(om_client.get_all_transactions_paged, platform_account_id)
    _txn_cache[platform_account_id] = (txns, datetime.now())
    logger.info("_all_transactions_cached: fetched %d txns for %s", len(txns), platform_account_id)
    return txns


def _compute_fan_payments(txns: list[dict], fan_id: str) -> tuple[float, float]:
    """Filter txns by fan.id == fan_id and return (total_all_time, total_last_7_days)."""
    week_ago = datetime.now() - timedelta(days=7)
    total = 0.0
    week  = 0.0
    for txn in txns:
        if str((txn.get("fan") or {}).get("id") or "") != str(fan_id):
            continue
        amount = float(txn.get("amount") or txn.get("price") or txn.get("net_amount") or 0)
        if amount <= 0:
            continue
        total += amount
        raw_dt = txn.get("timestamp") or txn.get("created_at") or txn.get("date")
        dt = parse_dt(str(raw_dt)) if raw_dt else None
        if dt and dt >= week_ago:
            week += amount
    return total, week


def _compute_fan_payment_stats(fan_id: str, all_txns: list[dict], now: datetime) -> dict:
    """Compute rich payment stats for a single fan from the full account transaction list."""
    fan_txns = [
        t for t in all_txns
        if str((t.get("fan") or {}).get("id") or "") == str(fan_id)
    ]
    if not fan_txns:
        return {}

    week_ago  = now - timedelta(days=7)
    purchases: list[tuple[datetime, float, str]] = []
    for t in fan_txns:
        raw    = t.get("timestamp") or t.get("created_at") or t.get("date")
        dt     = parse_dt(str(raw)) if raw else None
        amount = float(t.get("amount") or 0)
        if dt and amount > 0:
            purchases.append((dt, amount, str(t.get("type") or "")))

    if not purchases:
        return {}

    purchases.sort(key=lambda x: x[0])

    total_spent     = sum(a for _, a, _ in purchases)
    spent_this_week = sum(a for dt, a, _ in purchases if dt >= week_ago)
    purchase_dates  = [d.isoformat() for d, _, _ in purchases[-10:]]
    last_purchase   = purchases[-1][0].isoformat()

    if len(purchases) >= 2:
        intervals = [(purchases[i][0] - purchases[i-1][0]).days for i in range(1, len(purchases))]
        purchase_interval_avg: float | None = round(sum(intervals) / len(intervals), 1)
    else:
        purchase_interval_avg = None

    sub_purchases = [(dt, tp) for dt, _, tp in purchases if "subscription" in tp.lower()]
    if sub_purchases:
        last_sub_dt = max(dt for dt, _ in sub_purchases)
        subscription_expires: str | None = (last_sub_dt + timedelta(days=30)).isoformat()
    else:
        subscription_expires = None

    return {
        "total_spent":            total_spent,
        "spent_this_week":        spent_this_week,
        "purchase_dates":         purchase_dates,
        "purchase_interval_avg":  purchase_interval_avg,
        "subscription_expires":   subscription_expires,
        "last_purchase_date":     last_purchase,
        # keep compat fields used elsewhere
        "payment_total":          total_spent,
        "payment_week":           spent_this_week,
    }


async def fetch_fan_payments(platform_account_id: str, fan_id: str) -> tuple[float, float]:
    """Returns (total_usd, week_usd) for a specific fan, filtered client-side."""
    if not platform_account_id:
        return 0.0, 0.0
    try:
        txns = await _all_transactions_cached(platform_account_id)
    except Exception:
        logger.debug("fetch_fan_payments: failed to load txns for fan %s (plat_acc=%s)",
                     fan_id, platform_account_id)
        return 0.0, 0.0
    return _compute_fan_payments(txns, fan_id)


def format_profile_card(fan_id: str, fan_data: dict, profile: dict, platform: str) -> str:
    p      = profile
    label  = PLATFORM_LABELS.get(platform, platform)
    dname  = _html.escape(_clean(fan_display_name(fan_id, fan_data)))
    lines  = [f"📋 <b>{dname}</b>  [{label}]  <code>{fan_id}</code>", ""]

    for val, emoji in [
        (p.get("birthday"),                                                    "🎂"),
        (p.get("job"),                                                         "💼"),
        (", ".join(str(h) for h in p["hobbies"]) if p.get("hobbies") else None, "🎯"),
        (p.get("personal_life"),                                               "❤️"),
        (p.get("location"),                                                    "📍"),
    ]:
        if val:
            lines.append(f"{emoji} {_html.escape(_clean(val))}")

    lines.append("")
    lines.append(f"💰 За всё время:   {_money_stars(fan_data.get('payment_total'))}")
    lines.append(f"📅 За неделю:      {_money_stars(fan_data.get('payment_week'))}")
    lines.append(f"🔥 Прогретость:    {_stars(p.get('warmth'))}")
    lines.append(f"👁 Заинтересован.: {_stars(p.get('engagement'))}")

    if p.get("fetishes"):
        lines.append("")
        lines.append(f"🌶 {_html.escape(_clean(', '.join(str(f) for f in p['fetishes'])))}")

    if p.get("chat_style"):
        lines.append("")
        lines.append(f"💬 {_html.escape(_clean(p['chat_style']))}")

    if p.get("notes"):
        lines.append("")
        lines.append(f"📝 {_html.escape(_clean(p['notes']))}")

    return "\n".join(lines)


def _extract_fan_identity(fan_id: str, messages: list[dict], fans: dict) -> None:
    """
    Pull username / display_name out of the from_user field on fan messages
    and persist them in the fans dict (caller must save_fans afterwards).
    """
    for msg in messages:
        if msg.get("is_sent_by_me"):
            continue
        fu = msg.get("from_user")
        if not isinstance(fu, dict):
            continue
        uname = (fu.get("username") or "").strip()
        dname = (fu.get("name") or fu.get("display_name") or "").strip()
        if uname:
            fans[fan_id]["username"] = uname
            logger.debug("identity %s → username=%s dname=%s", fan_id, uname, dname)
        if dname:
            fans[fan_id]["display_name"] = dname
        if uname or dname:
            break


async def analyze_fan(account_id: str, fan_id: str, platform: str,
                      platform_account_id: str = "") -> dict | None:
    """Core analyze logic. Returns saved profile or None on failure."""
    try:
        messages = await asyncio.to_thread(om_client.get_messages, account_id, fan_id)
        if not messages:
            logger.warning("analyze_fan %s: no messages", fan_id)
            return None

        lines: list[str] = []
        fan_dates: list[datetime] = []

        for msg in messages:
            is_fan = not msg.get("is_sent_by_me")
            text   = _TAG.sub("", msg.get("text") or "").strip()
            if text:
                lines.append(f"{'Fan' if is_fan else 'Model'}: {text}")
            dt = parse_dt(msg.get("created_at"))
            if is_fan and dt:
                fan_dates.append(dt)

        if not lines:
            return None

        prompt = ANALYZE_PROMPT.format(
            msg_count    = len(lines),
            conversation = "\n".join(lines),
        )

        raw_json = await asyncio.to_thread(claude_client.chat, prompt)
        profile  = json.loads(clean_json(raw_json))

        pay_total, pay_week = await fetch_fan_payments(platform_account_id, fan_id)

        fans = load_fans()
        fans.setdefault(fan_id, {"id": fan_id})
        fans[fan_id].update({"account_id": account_id, "platform": platform,
                              "platform_account_id": platform_account_id})
        fans[fan_id]["profile"]         = profile
        fans[fan_id]["last_analyzed"]   = datetime.now().isoformat()
        fans[fan_id]["payment_total"]   = pay_total
        fans[fan_id]["payment_week"]    = pay_week
        fans[fan_id]["total_spent"]     = pay_total
        fans[fan_id]["spent_this_week"] = pay_week

        _extract_fan_identity(fan_id, messages, fans)

        if fan_dates:
            fans[fan_id]["last_message_date"] = max(fan_dates).isoformat()

        save_fans(fans)
        logger.info("analyze_fan %s (%s) saved — %d msgs, pay_total=%.0f",
                    fan_id, platform, len(lines), pay_total)
        return profile

    except json.JSONDecodeError:
        logger.exception("analyze_fan %s: JSON parse error", fan_id)
        return None
    except Exception:
        logger.exception("analyze_fan %s: unexpected error", fan_id)
        return None


async def update_fan(account_id: str, fan_id: str, platform: str,
                     platform_account_id: str = "") -> dict | None:
    """Light update: only last-7-days messages, only updates stars/fetishes/chat_style."""
    try:
        messages = await asyncio.to_thread(om_client.get_messages, account_id, fan_id)
        week_ago = datetime.now() - timedelta(days=7)

        lines: list[str] = []
        fan_dates: list[datetime] = []
        purchase_count = 0
        purchase_total = 0.0

        for msg in messages:
            dt     = parse_dt(msg.get("created_at"))
            is_fan = not msg.get("is_sent_by_me")
            price  = float(msg.get("price") or 0)

            if dt and dt < week_ago:
                continue
            text = _TAG.sub("", msg.get("text") or "").strip()
            if text:
                lines.append(f"{'Fan' if is_fan else 'Model'}: {text}")
            if is_fan and dt:
                fan_dates.append(dt)
            if is_fan and price > 0:
                purchase_count += 1
                purchase_total += price

        fans = load_fans()
        fans.setdefault(fan_id, {"id": fan_id})
        entry = fans[fan_id]
        entry.update({"account_id": account_id, "platform": platform,
                      "platform_account_id": platform_account_id})
        # Prefer stored platform_account_id if caller didn't supply one
        plat_acc = platform_account_id or entry.get("platform_account_id", "")

        if fan_dates:
            entry["last_message_date"] = max(fan_dates).isoformat()

        if not lines:
            # No new messages — refresh payment data, mark analyzed
            pay_total, pay_week = await fetch_fan_payments(plat_acc, fan_id)
            entry["last_analyzed"] = datetime.now().isoformat()
            entry["payment_total"] = pay_total
            entry["payment_week"]  = pay_week
            fans[fan_id] = entry
            save_fans(fans)
            return entry.get("profile")

        existing_profile = json.dumps(entry.get("profile", {}), ensure_ascii=False, indent=2)

        prompt = UPDATE_PROMPT.format(
            existing_profile = existing_profile,
            msg_count        = len(lines),
            conversation     = "\n".join(lines),
        )

        raw_json = await asyncio.to_thread(claude_client.chat, prompt)
        updates  = json.loads(clean_json(raw_json))

        profile = entry.get("profile") or {}
        for field in ("warmth", "engagement", "chat_style"):
            if updates.get(field) is not None:
                profile[field] = updates[field]

        new_fetishes = [f for f in (updates.get("fetishes") or []) if f]
        if new_fetishes:
            existing = profile.get("fetishes") or []
            merged = list(existing)
            for f in new_fetishes:
                if f not in merged:
                    merged.append(f)
            profile["fetishes"] = merged

        pay_total, pay_week = await fetch_fan_payments(plat_acc, fan_id)

        entry["profile"]         = profile
        entry["last_analyzed"]   = datetime.now().isoformat()
        entry["payment_total"]   = pay_total
        entry["payment_week"]    = pay_week
        entry["total_spent"]     = pay_total
        entry["spent_this_week"] = pay_week

        _extract_fan_identity(fan_id, messages, fans)

        fans[fan_id] = entry
        save_fans(fans)
        logger.info("update_fan %s (%s) — %d new msgs, %d new purchases",
                    fan_id, platform, len(lines), purchase_count)
        return profile

    except json.JSONDecodeError:
        logger.exception("update_fan %s: JSON parse error", fan_id)
        return None
    except Exception:
        logger.exception("update_fan %s: unexpected error", fan_id)
        return None


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /analyze <fan_id>")
        return

    fan_id = context.args[0]
    await update.message.reply_text(f"Анализирую фана {fan_id}...")
    logger.info("cmd_analyze: fan %s", fan_id)

    fans       = load_fans()
    fan_data   = fans.get(fan_id, {})
    account_id = fan_data.get("account_id")
    platform   = fan_data.get("platform", "unknown")
    plat_acc   = fan_data.get("platform_account_id", "")

    if not account_id:
        accounts = await asyncio.to_thread(get_all_accounts)
        if not accounts:
            await update.message.reply_text("Нет аккаунтов OnlyMonster.")
            return
        account_id = accounts[0]["id"]
        platform   = accounts[0]["platform"]
        plat_acc   = accounts[0].get("platform_account_id", "")

    profile = await analyze_fan(account_id, fan_id, platform, plat_acc)
    if profile is None:
        await update.message.reply_text("Нет сообщений или ошибка Claude.")
        return

    fans = load_fans()
    card = format_profile_card(fan_id, fans.get(fan_id, {}), profile, platform)
    await update.message.reply_text(card, parse_mode="HTML")


# ─── /analyze_all ─────────────────────────────────────────────────────────────

TOP_FANS_COUNT    = 20   # how many fans to analyze
ANALYZE_PAUSE_SEC = 3    # seconds between Claude calls (rate limit guard)
ACTIVITY_SCAN_MSG = 5    # messages fetched per fan during activity scan
REANALYZE_DAYS    = 7    # skip fan if last_analyzed is fresher than this


async def cmd_analyze_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("Сканирую активность фанов...")
    logger.info("analyze_all started")

    accounts = await asyncio.to_thread(get_all_accounts)
    if not accounts:
        await status.edit_text("Нет аккаунтов OnlyMonster.")
        return

    # acc_id → platform_account_id lookup for transactions API
    plat_acc_map = {a["id"]: a.get("platform_account_id", "") for a in accounts}

    # Collect (fan_id, account_id, platform) from ALL accounts, skip blocked
    fans_db_pre = load_fans()
    all_fans: list[tuple[str, str, str]] = []
    for acct in accounts:
        acc_id   = acct["id"]
        platform = acct["platform"]
        try:
            ids = await asyncio.to_thread(om_client.get_fan_ids, acc_id)
            for fid in ids:
                if not fans_db_pre.get(fid, {}).get("blocked"):
                    all_fans.append((fid, acc_id, platform))
        except Exception as e:
            logger.error("get_fan_ids failed for %s (%s): %s", platform, acc_id, e)

    if not all_fans:
        await status.edit_text("Список фанов пуст.")
        return

    await status.edit_text(
        f"Найдено {len(all_fans)} фанов по всем платформам. Сканирую активность..."
    )

    # ── Phase 1: quick activity scan (limit=5 per fan) ──────────────────────
    week_ago = datetime.now() - timedelta(days=7)
    activity: list[tuple[str, str, str, datetime | None]] = []

    for i, (fid, acc_id, platform) in enumerate(all_fans):
        try:
            msgs = await asyncio.to_thread(om_client.get_messages, acc_id, fid, ACTIVITY_SCAN_MSG)
            fan_dates = [parse_dt(m.get("created_at")) for m in msgs if not m.get("is_sent_by_me")]
            fan_dates = [d for d in fan_dates if d]
            activity.append((fid, acc_id, platform, max(fan_dates) if fan_dates else None))
        except Exception:
            activity.append((fid, acc_id, platform, None))

        if (i + 1) % 10 == 0:
            await status.edit_text(f"Сканирую... {i + 1}/{len(all_fans)} фанов проверено")

    # Sort: most recently active this week first, then rest
    active_week = sorted(
        [(fid, acc, plat, dt) for fid, acc, plat, dt in activity if dt and dt >= week_ago],
        key=lambda x: x[3], reverse=True,
    )
    the_rest = [(fid, acc, plat, dt) for fid, acc, plat, dt in activity
                if not (dt and dt >= week_ago)]
    top_fans = [(fid, acc, plat) for fid, acc, plat, _ in (active_week + the_rest)][:TOP_FANS_COUNT]

    await status.edit_text(
        f"Активных за неделю: {len(active_week)} из {len(all_fans)}.\n"
        f"Начинаю анализ топ-{len(top_fans)} через Claude..."
    )
    logger.info("analyze_all: %d active this week, analyzing %d fans", len(active_week), len(top_fans))

    # ── Phase 2: smart analyze (new / update / skip) ────────────────────────
    reanalyze_cutoff = datetime.now() - timedelta(days=REANALYZE_DAYS)
    created  = 0
    updated  = 0
    skipped  = 0
    failed   = 0
    log_lines: list[str] = []

    def _progress_text() -> str:
        head = (
            f"Анализирую топ-{len(top_fans)} фанов...\n"
            f"🆕 {created}  🔄 {updated}  ✅ {skipped}  ❌ {failed}\n\n"
        )
        return head + "\n".join(log_lines[-8:])

    for i, (fan_id, acc_id, platform) in enumerate(top_fans, 1):
        fans_db       = load_fans()
        fan_entry     = fans_db.get(fan_id, {})
        dname         = fan_display_name(fan_id, fan_entry)
        last_analyzed = parse_dt(fan_entry.get("last_analyzed"))
        has_profile   = bool(fan_entry.get("profile"))
        plat_acc      = plat_acc_map.get(acc_id, "")

        if has_profile and last_analyzed and last_analyzed >= reanalyze_cutoff:
            skipped += 1
            log_lines.append(f"✅ {dname} — досье актуально, пропускаем")
            await status.edit_text(_progress_text())
            continue

        if not has_profile:
            log_lines.append(f"🆕 {dname} — создаю новое досье...")
            await status.edit_text(_progress_text())
            profile = await analyze_fan(acc_id, fan_id, platform, plat_acc)
            if profile:
                created += 1
                fans_db = load_fans()
                card = format_profile_card(fan_id, fans_db.get(fan_id, {}), profile, platform)
                await update.message.reply_text(card, parse_mode="HTML")
                log_lines[-1] = f"🆕 {dname} — досье создано ✓"
            else:
                failed += 1
                log_lines[-1] = f"🆕 {dname} — ошибка ✗"
        else:
            log_lines.append(f"🔄 {dname} — обновляю досье...")
            await status.edit_text(_progress_text())
            profile = await update_fan(acc_id, fan_id, platform, plat_acc)
            if profile:
                updated += 1
                fans_db = load_fans()
                card = format_profile_card(fan_id, fans_db.get(fan_id, {}), profile, platform)
                await update.message.reply_text(card, parse_mode="HTML")
                log_lines[-1] = f"🔄 {dname} — досье обновлено ✓"
            else:
                failed += 1
                log_lines[-1] = f"🔄 {dname} — ошибка ✗"

        await status.edit_text(_progress_text())
        logger.info("analyze_all %d/%d fan=%s (%s) new=%d upd=%d skip=%d fail=%d",
                    i, len(top_fans), fan_id, platform, created, updated, skipped, failed)

        if i < len(top_fans):
            await asyncio.sleep(ANALYZE_PAUSE_SEC)

    await status.edit_text(
        f"Анализ завершён!\n\n"
        f"🆕 Создано новых досье: {created}\n"
        f"🔄 Обновлено: {updated}\n"
        f"✅ Актуальных (пропущено): {skipped}\n"
        f"❌ Ошибок: {failed}"
    )
    logger.info("analyze_all done: created=%d updated=%d skipped=%d failed=%d",
                created, updated, skipped, failed)


# ─── DAILY MONITORING ─────────────────────────────────────────────────────────

_REASON_PRIORITY = {"financial": 1, "temporal": 2, "behavioral": 3, "content": 4}


def _best_reason(fan_id: str, fan_data: dict, profile: dict, now: datetime) -> tuple[str, str] | None:
    """
    Return (reason_text, priority_category) for the single strongest actionable
    reason to contact this fan, or None if there is no signal.
    Priority order: financial → temporal → behavioral → content.
    """
    total_spent    = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
    purchase_dates = fan_data.get("purchase_dates") or []
    interval_avg   = fan_data.get("purchase_interval_avg")
    sub_expires_dt = parse_dt(fan_data.get("subscription_expires"))
    last_msg_dt    = parse_dt(fan_data.get("last_message_date"))
    days_silent    = int((now - last_msg_dt).days) if last_msg_dt else None

    # ── 1. FINANCIAL ──────────────────────────────────────────────────────────
    if total_spent >= 500 and days_silent is not None and days_silent >= 3:
        return (f"VIP молчит {days_silent} дн — срочная реактивация", "financial")

    if purchase_dates:
        last_purchase_dt = parse_dt(purchase_dates[-1])
        if last_purchase_dt and (now - last_purchase_dt).days <= 1:
            return ("Горячий! Только что купил — предложи следующий шаг", "financial")

    if interval_avg and purchase_dates:
        last_purchase_dt = parse_dt(purchase_dates[-1])
        if last_purchase_dt:
            days_since_last = (now - last_purchase_dt).days
            if days_since_last >= interval_avg * 2:
                return (
                    f"Сломался паттерн покупок (пауза {days_since_last} дн, среднее {int(interval_avg)} дн)",
                    "financial",
                )

    notes = _clean(profile.get("notes") or "")
    if any(w in notes.lower() for w in ["кастом", "custom", "купит", "обсуждал", "хочет купить"]):
        return ("Тянет с покупкой — дожать", "financial")

    # ── 2. TEMPORAL ───────────────────────────────────────────────────────────
    if sub_expires_dt:
        days_to_exp = (sub_expires_dt - now).days
        if 0 <= days_to_exp <= 3:
            return (f"Подписка истекает через {days_to_exp} дн — удержать", "temporal")

    sub_since = parse_dt(fan_data.get("subscribed_since"))
    if sub_since:
        days_sub = (now - sub_since).days
        for milestone in (365, 90, 60, 30):
            if days_sub == milestone:
                return (f"Юбилей {milestone} дней подписки — поздравить и монетизировать", "temporal")

    dtb = days_to_birthday(profile.get("birthday"))
    if dtb is not None and 0 <= dtb <= 3:
        return (f"День рождения через {dtb} дн — поздравить", "temporal")

    # ── 3. BEHAVIORAL ─────────────────────────────────────────────────────────
    engagement = int(profile.get("engagement") or 0)
    pay_tier   = (5 if total_spent >= 600 else 4 if total_spent >= 100 else
                  3 if total_spent >= 50  else 2 if total_spent >= 10  else
                  1 if total_spent > 0    else 0)
    if engagement >= 4 and pay_tier <= 2:
        return ("Много болтает — конвертировать в деньги", "behavioral")

    if days_silent is not None:
        if interval_avg and days_silent >= interval_avg * 2 and days_silent > DAYS_WITHOUT_REPLY_ALERT:
            return (f"Резкое молчание {days_silent} дн — что-то случилось", "behavioral")
        if days_silent >= DAYS_WITHOUT_REPLY_ALERT:
            return (f"Молчит {days_silent} дн — написать", "behavioral")

    # ── 4. CONTENT ────────────────────────────────────────────────────────────
    fetishes = profile.get("fetishes") or []
    if fetishes:
        first = _clean(str(fetishes[0]))
        return (f"Фетиш: {first} — персональный питч", "content")

    return None


def run_monitoring_account(account_id: str, platform: str) -> dict:
    now    = datetime.now()
    result = {"account_id": account_id, "special": [], "actionable": [], "active_count": 0}
    fans   = load_fans()

    try:
        fan_ids = om_client.get_fan_ids(account_id)
    except Exception as e:
        logger.error("get_fan_ids failed for %s: %s", platform, e)
        return result

    result["active_count"] = len(fan_ids)

    for fid in fan_ids:
        fans.setdefault(fid, {"id": fid})
        fans[fid].update({"account_id": account_id, "platform": platform})
        fan_data = fans[fid]
        if fan_data.get("blocked") or fan_data.get("auto_stopped"):
            continue
        profile = fan_data.get("profile") or {}

        # Subscription milestone special events
        sub_since = parse_dt(fan_data.get("subscribed_since"))
        if sub_since:
            days_sub = (now - sub_since).days
            uname  = fan_data.get("username") or fid
            dname  = fan_data.get("display_name") or profile.get("name") or fid
            custom = fan_data.get("custom_name") or ""
            if days_sub == SUBSCRIPTION_MILESTONE_365:
                result["special"].append(
                    {"fan_id": fid, "username": uname, "display_name": dname, "custom_name": custom, "reason": "year"})
            elif days_sub == SUBSCRIPTION_MILESTONE_30:
                result["special"].append(
                    {"fan_id": fid, "username": uname, "display_name": dname, "custom_name": custom, "reason": "month"})

        # Resubscription
        sub_hist = fan_data.get("subscription_history", [])
        if len(sub_hist) >= 2:
            last, prev = sub_hist[-1], sub_hist[-2]
            if last.get("action") == "subscribed" and prev.get("action") == "unsubscribed":
                last_dt = parse_dt(last.get("date"))
                if last_dt and (now - last_dt).days <= 1:
                    uname  = fan_data.get("username") or fid
                    dname  = fan_data.get("display_name") or profile.get("name") or fid
                    custom = fan_data.get("custom_name") or ""
                    result["special"].append(
                        {"fan_id": fid, "username": uname, "display_name": dname, "custom_name": custom,
                         "reason": "resubscribed"})

        # Update _computed helper for get_text button
        last_msg_dt    = parse_dt(fan_data.get("last_message_date"))
        last_buy_dt    = parse_dt(fan_data.get("last_purchase_date"))
        days_silent    = int((now - last_msg_dt).days) if last_msg_dt else None
        days_since_buy = int((now - last_buy_dt).days) if last_buy_dt else None
        fans[fid]["_computed"] = {
            "days_since_purchase": days_since_buy,
            "days_silent":         days_silent,
            "days_to_birthday":    days_to_birthday(profile.get("birthday")),
        }

        # Only fans with profiles get smart reasons
        if not profile:
            continue

        reason_result = _best_reason(fid, fan_data, profile, now)
        if reason_result:
            reason_text, priority = reason_result
            result["actionable"].append({
                "fan_id":      fid,
                "fan_data":    fan_data,
                "profile":     profile,
                "platform":    platform,
                "reason":      reason_text,
                "priority":    priority,
            })

    # Sort: by priority tier, then by total_spent descending within tier
    result["actionable"].sort(key=lambda x: (
        _REASON_PRIORITY.get(x["priority"], 9),
        -(float(x["fan_data"].get("total_spent") or x["fan_data"].get("payment_total") or 0)),
    ))

    save_fans(fans)
    return result


def run_monitoring_all(accounts: list[dict]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for acct in accounts:
        platform   = (acct.get("platform") or "unknown").lower().strip()
        account_id = acct.get("id", "")
        if not account_id:
            continue
        try:
            status = run_monitoring_account(account_id, platform)
            if platform not in results:
                results[platform] = status
            else:
                for key in ("special", "actionable"):
                    results[platform][key].extend(status[key])
                results[platform]["active_count"] += status["active_count"]
        except Exception as e:
            logger.error("Monitoring failed for %s (%s): %s", platform, account_id, e)
    return results


# ─── REPORT FORMATTING ────────────────────────────────────────────────────────

_SEP = "━━━━━━━━━━━━━━━━"


def _format_fan_entry(
    idx: int,
    entry: dict,
    buttons: list[InlineKeyboardButton],
) -> list[str]:
    fan_id   = entry["fan_id"]
    fan_data = entry["fan_data"]
    profile  = entry["profile"]
    reason   = entry.get("reason", "")

    dname      = _html.escape(_clean(fan_display_name(fan_id, fan_data)))
    total      = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
    week       = float(fan_data.get("spent_this_week") or fan_data.get("payment_week") or 0)
    warmth     = profile.get("warmth")
    engage     = profile.get("engagement")
    chat_style = _clean(str(profile.get("chat_style") or ""))

    total_str = f"${total:.0f}" if total > 0 else "$0"
    week_str  = f"${week:.0f}" if week > 0 else "$0"

    lines: list[str] = [
        _SEP,
        "",
        f"<b>{idx}. {dname}</b>",
        "",
    ]

    if reason:
        lines.append(f"🎯 {_html.escape(reason)}")
        lines.append("")

    lines.append(f"💰 За всё время: {_money_stars_compact(total)}  {total_str}")
    lines.append(f"📅 {week_str} за неделю")
    lines.append(f"🔥 Прогрет: {_stars(warmth)}")
    lines.append(f"👁 Интерес: {_stars(engage)}")

    if chat_style:
        lines.append("")
        lines.append(f"💬 {_html.escape(_truncate_sentence(chat_style))}")

    lines.append(f"👤 /fan {fan_id}")
    lines.append("")

    buttons.append(InlineKeyboardButton(
        f"📝 {_clean(fan_display_name(fan_id, fan_data))[:18]}",
        callback_data=f"get_text:{fan_id}",
    ))
    return lines


def format_report(platforms_status: dict[str, dict]) -> tuple[str, list[list]]:
    now   = datetime.now()
    lines = [f"☀️ Список на сегодня ({now.strftime('%d.%m.%Y')}):\n"]

    buttons: list[InlineKeyboardButton] = []
    fan_idx = 1

    for platform in PLATFORM_ORDER:
        if platform not in platforms_status:
            continue
        status = platforms_status[platform]

        if status["special"]:
            lines.append("🎯 ОСОБЫЕ ПОВОДЫ:")
            for s in status["special"]:
                name = _html.escape(_clean(fan_display_name(s["fan_id"], s)))
                if s["reason"] == "year":
                    lines.append(f"- {name} — подписан 1 год сегодня! 🎉")
                elif s["reason"] == "month":
                    lines.append(f"- {name} — подписан 30 дней 🌟")
                else:
                    lines.append(f"- {name} — переподписался 🔄")
            lines.append("")

        if status["actionable"]:
            lines.append("🔥 НАПИСАТЬ СЕЙЧАС:")
            lines.append("")
            for entry in status["actionable"][:12]:
                lines.extend(_format_fan_entry(fan_idx, entry, buttons))
                fan_idx += 1
            lines.append(_SEP)

    total = sum(s["active_count"] for s in platforms_status.values())
    lines.append(f"\n📊 Активных фанов: {total}")

    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return "\n".join(lines), keyboard


# ─── SCHEDULED JOBS ───────────────────────────────────────────────────────────

async def daily_morning_report(app: Application) -> None:
    logger.info("Running daily morning report")
    try:
        accounts = await asyncio.wait_for(asyncio.to_thread(get_all_accounts), timeout=25)
    except asyncio.TimeoutError:
        logger.error("daily_morning_report: get_all_accounts timed out")
        await send_daily_message(app.bot, "❌ Отчёт не отправлен: таймаут при получении аккаунтов.")
        return
    except Exception as e:
        logger.exception("daily_morning_report: get_all_accounts failed")
        await send_daily_message(app.bot, f"❌ Отчёт не отправлен: {e}")
        return

    if not accounts:
        logger.error("No accounts found — skipping report")
        return

    try:
        platforms_status = await asyncio.wait_for(
            asyncio.to_thread(run_monitoring_all, accounts), timeout=90
        )
        text, keyboard = format_report(platforms_status)
        markup         = InlineKeyboardMarkup(keyboard) if keyboard else None
        await send_daily_message(app.bot, text, markup)
        logger.info("Morning report sent")
    except asyncio.TimeoutError:
        logger.error("daily_morning_report: monitoring timed out")
        await send_daily_message(app.bot, "❌ Отчёт не отправлен: таймаут мониторинга (>90 сек).")
    except Exception as e:
        logger.exception("Morning report failed")
        await send_daily_message(app.bot, f"❌ Ошибка отчёта: {e}")


async def run_weekly_analyze(app: Application) -> None:
    """Sunday 13:00 MSK: smart analyze_all for top fans, summary → topic Всякое."""
    logger.info("run_weekly_analyze started")
    try:
        accounts = await asyncio.wait_for(asyncio.to_thread(get_all_accounts), timeout=25)
    except Exception as e:
        await send_misc_message(app.bot, f"❌ Еженедельный анализ: ошибка аккаунтов: {e}")
        return

    if not accounts:
        await send_misc_message(app.bot, "❌ Еженедельный анализ: аккаунты не найдены.")
        return

    plat_acc_map = {a["id"]: a.get("platform_account_id", "") for a in accounts}
    fans_db_pre  = load_fans()
    all_fans: list[tuple[str, str, str]] = []
    for acct in accounts:
        acc_id, platform = acct["id"], acct["platform"]
        try:
            ids = await asyncio.to_thread(om_client.get_fan_ids, acc_id)
            for fid in ids:
                if not fans_db_pre.get(fid, {}).get("blocked"):
                    all_fans.append((fid, acc_id, platform))
        except Exception as e:
            logger.error("weekly_analyze get_fan_ids %s: %s", platform, e)

    if not all_fans:
        await send_misc_message(app.bot, "Еженедельный анализ: список фанов пуст.")
        return

    # Activity scan → top N
    week_ago = datetime.now() - timedelta(days=7)
    activity: list[tuple[str, str, str, datetime | None]] = []
    for fid, acc_id, platform in all_fans:
        try:
            msgs = await asyncio.to_thread(om_client.get_messages, acc_id, fid, ACTIVITY_SCAN_MSG)
            dts  = [parse_dt(m.get("created_at")) for m in msgs if not m.get("is_sent_by_me")]
            dts  = [d for d in dts if d]
            activity.append((fid, acc_id, platform, max(dts) if dts else None))
        except Exception:
            activity.append((fid, acc_id, platform, None))

    active_week = sorted(
        [(fid, acc, plat, dt) for fid, acc, plat, dt in activity if dt and dt >= week_ago],
        key=lambda x: x[3], reverse=True,
    )
    the_rest = [(fid, acc, plat, dt) for fid, acc, plat, dt in activity if not (dt and dt >= week_ago)]
    top_fans = [(fid, acc, plat) for fid, acc, plat, _ in (active_week + the_rest)][:TOP_FANS_COUNT]

    reanalyze_cutoff = datetime.now() - timedelta(days=REANALYZE_DAYS)
    created = updated = skipped = failed = 0

    for fan_id, acc_id, platform in top_fans:
        fans_db   = load_fans()
        fan_entry = fans_db.get(fan_id, {})
        if fan_entry.get("blocked"):
            continue
        last_analyzed = parse_dt(fan_entry.get("last_analyzed"))
        has_profile   = bool(fan_entry.get("profile"))

        if has_profile and last_analyzed and last_analyzed >= reanalyze_cutoff:
            skipped += 1
            continue

        plat_acc = plat_acc_map.get(acc_id, "")
        try:
            if not has_profile:
                profile = await analyze_fan(acc_id, fan_id, platform, plat_acc)
                created += 1 if profile else 0
                failed  += 0 if profile else 1
            else:
                profile = await update_fan(acc_id, fan_id, platform, plat_acc)
                updated += 1 if profile else 0
                failed  += 0 if profile else 1
        except Exception:
            logger.exception("weekly_analyze: fan %s", fan_id)
            failed += 1

        await asyncio.sleep(ANALYZE_PAUSE_SEC)

    summary = (
        f"📊 Еженедельный анализ завершён!\n\n"
        f"🆕 Создано: {created}  🔄 Обновлено: {updated}\n"
        f"✅ Актуальных: {skipped}  ❌ Ошибок: {failed}\n"
        f"Всего фанов в системе: {len(all_fans)}"
    )
    await send_misc_message(app.bot, summary)
    logger.info("run_weekly_analyze done: new=%d upd=%d skip=%d fail=%d", created, updated, skipped, failed)


# ─── STYLE GUIDE ─────────────────────────────────────────────────────────────

_STYLE_LEARN_PROMPT = """\
Проанализируй эти исходящие сообщения от модели OnlyFans и составь детальный style guide.

Сообщений для анализа: {count}

{messages}

Составь детальный style guide на русском:

1. ДЛИНА СООБЩЕНИЙ — типичная (количество слов/предложений, примеры коротких и длинных)
2. ТОН И МАНЕРА — формальный/неформальный, тёплый/холодный, игривый/серьёзный
3. ТИПИЧНЫЕ ФРАЗЫ И ОБОРОТЫ — конкретные выражения которые часто повторяются
4. КАК НАЧИНАЮТ СООБЩЕНИЯ — типичные зачины, приветствия
5. КАК ЗАКАНЧИВАЮТ — типичные концовки, прощания
6. ЭМОДЗИ — какие используют, как часто, в каких ситуациях (или не используют вообще)
7. ФЛИРТ — как флиртуют, какие слова/образы используют
8. ПРЕДЛОЖЕНИЕ КОНТЕНТА И PPV — как предлагают, какие формулировки, как называют цену
9. ЧТО НИКОГДА НЕ ПИШУТ — табу, слова и фразы которых нет в переписке
10. ПРИМЕРЫ — 5-7 типичных сообщений разных типов (флирт, ответ на вопрос, предложение контента, реактивация, благодарность)
"""


async def _collect_outgoing_messages(limit: int = 200) -> list[str]:
    """Collect up to `limit` recent outgoing messages from active fan chats."""
    fans     = load_fans()
    accounts = await asyncio.to_thread(get_all_accounts)

    # Sort fans by last_message_date — most recent first
    sorted_fans = sorted(
        [(fid, fd) for fid, fd in fans.items() if fd.get("last_message_date")],
        key=lambda x: x[1].get("last_message_date", ""),
        reverse=True,
    )

    outgoing: list[str] = []
    acc_map  = {acct["id"]: acct for acct in accounts}

    for fid, fdata in sorted_fans:
        if len(outgoing) >= limit:
            break
        acc_id = fdata.get("account_id") or (accounts[0]["id"] if accounts else "")
        if not acc_id:
            continue
        try:
            msgs = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_messages, acc_id, fid, 50),
                timeout=10,
            )
        except Exception:
            continue
        for m in msgs:
            if m.get("is_sent_by_me"):
                txt = _clean(_TAG.sub("", str(m.get("text") or ""))).strip()
                if txt and len(txt) > 3:
                    outgoing.append(txt)
                    if len(outgoing) >= limit:
                        break

    return outgoing


async def _do_learn_style() -> tuple[str, int]:
    """Collect outgoing messages, analyze with Claude, save style guide. Returns (guide, count)."""
    outgoing = await _collect_outgoing_messages(200)
    if not outgoing:
        raise ValueError("Нет исходящих сообщений для анализа")

    # Deduplicate and truncate each message for the prompt
    seen: set[str] = set()
    unique: list[str] = []
    for txt in outgoing:
        key = txt[:50]
        if key not in seen:
            seen.add(key)
            unique.append(txt[:300])

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(unique[:200]))
    prompt   = _STYLE_LEARN_PROMPT.format(count=len(unique), messages=numbered)
    guide    = await asyncio.to_thread(claude_client.chat, prompt)

    save_style_guide({
        "created_at":        datetime.now().isoformat(),
        "messages_analyzed": len(unique),
        "guide":             guide,
    })
    return guide, len(unique)


async def cmd_learn_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("⏳ Собираю исходящие сообщения из чатов...")
    try:
        guide, count = await _do_learn_style()
        preview = guide[:800] + ("…" if len(guide) > 800 else "")
        await msg.edit_text(
            f"✅ Style guide создан на основе {count} сообщений!\n\n"
            f"Превью:\n{preview}\n\n"
            f"Полный гайд: /show_style"
        )
        logger.info("learn_style: guide created from %d messages", count)
    except Exception as e:
        logger.exception("cmd_learn_style failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_update_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same as /learn_style — refreshes the style guide with fresh messages."""
    await cmd_learn_style(update, context)


async def cmd_show_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    guide_data = load_style_guide()
    if not guide_data:
        await update.message.reply_text("❌ Style guide ещё не создан. Запусти /learn_style")
        return
    guide    = guide_data.get("guide", "")
    count    = guide_data.get("messages_analyzed", "?")
    created  = guide_data.get("created_at", "?")[:10]
    header   = f"📖 Style guide (создан {created}, на основе {count} сообщений):\n\n"

    # Split into chunks ≤ 4000 chars for Telegram
    full = header + guide
    for i in range(0, len(full), 4000):
        await update.message.reply_text(full[i:i+4000])


async def _auto_update_style_if_needed(app: Application) -> None:
    guide = load_style_guide()
    if guide:
        last = parse_dt(guide.get("created_at"))
        if last and (datetime.now() - last).days < 14:
            return  # fresh enough
    logger.info("Auto-updating style guide (14+ days old or missing)")
    try:
        guide_text, count = await _do_learn_style()
        logger.info("Auto style update done: %d messages", count)
    except Exception:
        logger.exception("_auto_update_style_if_needed failed")


# ─── WEEKLY AI REPORT ────────────────────────────────────────────────────────

_WEEKLY_AI_PROMPT = """\
Ты — аналитик OnlyFans агентства. Напиши еженедельный отчёт на русском по предоставленным данным.

ДАННЫЕ ЗА НЕДЕЛЮ ({week_start} — {week_end}):

💰 Финансы:
- Итого: ${total_earned:.0f} (PPV: ${ppv_total:.0f} | Типы: ${tips_total:.0f} | Посты: ${posts_total:.0f})
- Транзакций: {txn_count}, средний чек: ${avg_check:.0f}
- Прошлая неделя: {prev_total_str}
- Топ плательщики: {top3_str}

👥 Фаны:
- Подписались за неделю: {new_fans_count} ({new_fans_names})
- Молчат 7+ дней и потратили 100$+: {at_risk_str}

💬 Чаттеры:
{chatters_str}

📜 История (последние недели):
{history_str}
{extra_data}
Напиши отчёт СТРОГО в таком формате (без лишнего текста до/после):

📊 Еженедельный анализ {week_start} — {week_end}

💰 ФИНАНСЫ:
- Итого: ${total_earned:.0f} [больше/меньше/столько же что прошлая неделя, %]
- PPV (сообщения): ${ppv_total:.0f} | Типы: ${tips_total:.0f} | Посты: ${posts_total:.0f}
- Средний чек: ${avg_check:.0f}
- Топ плательщики: [Имя $X, Имя $X, Имя $X]

👥 ФАНЫ:
- Новых подписчиков: {new_fans_count} (с подпиской на этой неделе)
- Реактивированных: [оцени по данным]
- Молчат и богатые (риск потери): [список или "нет"]

💬 ЧАТТЕРЫ:
[по каждому: имя, продажи, время ответа]

🔍 ПРОБЛЕМЫ:
[2-3 конкретных проблемы с цифрами из данных]

🎯 РЕКОМЕНДАЦИИ НА СЛЕДУЮЩУЮ НЕДЕЛЮ:
[2-3 конкретных действия]

📈 ДИНАМИКА:
[сравнение с прошлыми неделями, тренд, вывод]
{extra_instructions}"""


async def _collect_weekly_data(now_msk: datetime) -> dict:
    """Collect all raw data for weekly AI report. No Claude involved."""
    week_start_msk = now_msk - timedelta(days=7)
    start_iso = (week_start_msk - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso   = (now_msk       - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Load history first — used in multiple sections below
    prev_reports = load_weekly_reports()

    # 1. Transactions for the week
    accounts = await asyncio.to_thread(get_all_accounts)
    all_txns: list[dict] = []
    for acct in accounts:
        plat_acc = acct.get("platform_account_id", "")
        if not plat_acc:
            continue
        try:
            batch = await asyncio.wait_for(
                asyncio.to_thread(
                    om_client.get_all_transactions_paged, plat_acc, 7, start_iso, end_iso
                ),
                timeout=90,
            )
            all_txns.extend(batch)
        except Exception as e:
            logger.warning("_collect_weekly_data txns for %s: %s", plat_acc, e)

    ppv_total   = 0.0
    tips_total  = 0.0
    posts_total = 0.0
    txn_count   = 0
    for txn in all_txns:
        amt = float(txn.get("amount") or 0)
        if amt <= 0:
            continue
        txn_count += 1
        t = str(txn.get("type") or "").lower()
        if "payment for message" in t:
            ppv_total += amt
        elif "tip" in t:
            tips_total += amt
        elif "post purchase" in t:
            posts_total += amt
        # other types also count toward total
    total_earned = ppv_total + tips_total + posts_total
    avg_check    = total_earned / txn_count if txn_count else 0.0

    # 2. Top-3 fans by week spending
    fan_week: dict[str, float] = {}
    for txn in all_txns:
        fid = str((txn.get("fan") or {}).get("id") or "")
        amt = float(txn.get("amount") or 0)
        if fid and amt > 0:
            fan_week[fid] = fan_week.get(fid, 0.0) + amt
    fans   = load_fans()
    top3   = sorted(fan_week.items(), key=lambda x: x[1], reverse=True)[:3]
    top3_named = [(fan_display_name(fid, fans.get(fid, {})), amt) for fid, amt in top3]

    # 3. New subscribers this week — compare current fan list vs previous week's snapshot
    current_fan_ids: set[str] = set()
    for acct in accounts:
        try:
            ids = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_fan_ids, acct["id"]),
                timeout=30,
            )
            current_fan_ids.update(str(fid) for fid in ids)
        except Exception as e:
            logger.warning("get_fan_ids for acc %s: %s", acct["id"], e)

    prev_fan_ids  = set(prev_reports[-1].get("fan_ids", [])) if prev_reports else set()
    new_sub_ids   = current_fan_ids - prev_fan_ids
    new_fans_count_real = len(new_sub_ids)
    new_fans = [fan_display_name(fid, fans.get(fid, {})) for fid in list(new_sub_ids)[:15]]

    # 4. At-risk fans (7+ days silent, 100$+ spent)
    at_risk: list[tuple[str, float, int]] = []
    for fid, fdata in fans.items():
        if fdata.get("blocked"):
            continue
        total = float(fdata.get("total_spent") or fdata.get("payment_total") or 0)
        if total < 100:
            continue
        last_dt = parse_dt(fdata.get("last_message_date"))
        if last_dt:
            days_s = (now_msk - last_dt).days
            if days_s >= 7:
                at_risk.append((fan_display_name(fid, fdata), total, days_s))
    at_risk.sort(key=lambda x: x[1], reverse=True)

    # 5. Chatter metrics + shift-based sales
    chatters = load_chatters()
    user_ids = [ch["user_id"] for ch in chatters if ch.get("user_id")]
    metrics_by_uid: dict[int, dict] = {}
    if user_ids:
        try:
            ml = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_users_metrics, user_ids, start_iso, end_iso),
                timeout=20,
            )
            for m in ml:
                uid = m.get("user_id") or m.get("id")
                if uid is not None:
                    metrics_by_uid[int(uid)] = m
        except Exception as e:
            logger.warning("_collect_weekly_data metrics: %s", e)

    chatter_sales: dict[str, float] = {ch["name"]: 0.0 for ch in chatters}
    for txn in all_txns:
        raw_ts = txn.get("timestamp") or txn.get("created_at")
        dt_utc = parse_dt(str(raw_ts)) if raw_ts else None
        if not dt_utc:
            continue
        ch = _chatter_for_hour((dt_utc + timedelta(hours=3)).hour, chatters)
        if ch:
            chatter_sales[ch["name"]] += float(txn.get("amount") or 0)

    chatter_stats: list[dict] = []
    for ch in chatters:
        uid = ch.get("user_id")
        m   = metrics_by_uid.get(uid, {}) if uid else {}
        chatter_stats.append({
            "name":           ch["name"],
            "sales":          chatter_sales[ch["name"]],
            "messages":       int(m.get("messages_count") or m.get("message_count") or 0),
            "reply_time_avg": float(m.get("reply_time_avg") or 0),
        })

    # 6. Previous reports for comparison
    prev_total = prev_reports[-1].get("total_earned") if prev_reports else None

    # 7. Broadcast stats — auto-detected via detect_broadcasts()
    broadcast_stats: dict = {"count": 0, "recipients": 0, "replied": 0, "revenue": 0.0, "top_text": ""}
    try:
        detected_bc = await detect_broadcasts(days=7, prefetched_txns=all_txns)
        if detected_bc:
            broadcast_stats["count"]      = len(detected_bc)
            broadcast_stats["recipients"] = sum(b["recipients"] for b in detected_bc)
            broadcast_stats["replied"]    = sum(b["replied"]    for b in detected_bc)
            broadcast_stats["revenue"]    = sum(b["revenue"]    for b in detected_bc)
            broadcast_stats["top_text"]   = detected_bc[0]["text"][:80]
    except Exception as e:
        logger.warning("_collect_weekly_data broadcasts: %s", e)

    # 8. Pricing analysis — top price points by revenue
    pricing_top: list[tuple[float, int, float]] = []
    try:
        price_counts: dict[float, int] = {}
        price_rev: dict[float, float] = {}
        for txn in all_txns:
            amt = round(float(txn.get("amount") or 0), 2)
            if amt > 0:
                price_counts[amt] = price_counts.get(amt, 0) + 1
                price_rev[amt] = price_rev.get(amt, 0.0) + amt
        pricing_top = sorted(
            [(amt, price_counts[amt], price_rev[amt]) for amt in price_counts],
            key=lambda x: x[2], reverse=True,
        )[:5]
    except Exception as e:
        logger.warning("_collect_weekly_data pricing: %s", e)

    # 9. Refusal stats — PPVs viewed but not purchased (top fans by week spend)
    refusal_list: list[tuple[str, float]] = []
    try:
        top5_week = sorted(fan_week.items(), key=lambda x: x[1], reverse=True)[:5]
        for fan_id_r, _ in top5_week:
            fdata_r = fans.get(fan_id_r, {})
            acc_id_r = fdata_r.get("account_id") or (accounts[0]["id"] if accounts else "")
            if not acc_id_r:
                continue
            try:
                msgs_r = await asyncio.wait_for(
                    asyncio.to_thread(om_client.get_messages, acc_id_r, fan_id_r, 30),
                    timeout=10,
                )
                for m in msgs_r:
                    if m.get("is_opened") and m.get("can_purchase") and not m.get("is_purchased"):
                        price = float(m.get("price") or 0)
                        refusal_list.append((fan_display_name(fan_id_r, fdata_r), price))
            except Exception as e_r:
                logger.warning("refusal check fan %s: %s", fan_id_r, e_r)
    except Exception as e:
        logger.warning("_collect_weekly_data refusals: %s", e)

    return {
        "week_start":    week_start_msk.strftime("%d.%m.%Y"),
        "week_end":      now_msk.strftime("%d.%m.%Y"),
        "start_iso":     start_iso,
        "end_iso":       end_iso,
        "total_earned":  total_earned,
        "txn_count":        txn_count,
        "avg_check":        avg_check,
        "ppv_total":        ppv_total,
        "tips_total":       tips_total,
        "posts_total":      posts_total,
        "top3":             top3_named,
        "new_fans":         new_fans,
        "new_fans_count":   new_fans_count_real,
        "current_fan_ids":  list(current_fan_ids),
        "at_risk":          at_risk,
        "chatter_stats":    chatter_stats,
        "prev_total":       prev_total,
        "prev_reports":     prev_reports[-4:],
        "broadcast_stats":  broadcast_stats,
        "pricing_top":      pricing_top,
        "refusal_list":     refusal_list,
    }


def _build_weekly_prompt(d: dict) -> str:
    prev_total_str = f"${d['prev_total']:.0f}" if d["prev_total"] is not None else "нет данных"
    top3_str = ", ".join(f"{n} ${a:.0f}" for n, a in d["top3"]) or "нет данных"
    new_fans_count = d.get("new_fans_count", len(d["new_fans"]))
    new_names = ", ".join(d["new_fans"][:10]) if d["new_fans"] else ("нет данных (первый запуск)" if not d.get("new_fans_count") else "нет")
    at_risk_str = (
        "; ".join(f"{n} (${a:.0f}, {ds} дн)" for n, a, ds in d["at_risk"][:5])
        or "нет"
    )
    chatters_lines = []
    for cs in d["chatter_stats"]:
        rtime = f"{int(cs['reply_time_avg'] // 60)} мин" if cs["reply_time_avg"] else "N/A"
        chatters_lines.append(
            f"  {cs['name']}: ${cs['sales']:.0f} продаж, "
            f"{cs['messages'] or 'N/A'} сообщ., ответ {rtime}"
        )
    history_lines = []
    for r in d["prev_reports"]:
        history_lines.append(
            f"  {r.get('week_start','?')}–{r.get('week_end','?')}: "
            f"${r.get('total_earned', 0):.0f}, "
            f"чеков {r.get('txn_count', 0)}, "
            f"avg ${r.get('avg_check', 0):.0f}"
        )
    # ── extra data sections (broadcast / pricing / refusal) ──────────────────
    bs = d.get("broadcast_stats", {})
    extra_data = ""
    if bs.get("count", 0) > 0:
        n   = bs["recipients"]
        r   = bs["replied"]
        rate = f"{r/n*100:.0f}%" if n else "0%"
        extra_data += (
            f"\n📢 Рассылки за неделю (авто-обнаружены):\n"
            f"- Рассылок: {bs['count']}, охват: {n} фанов\n"
            f"- Ответили: {r} ({rate}), доход после: ${bs['revenue']:.0f}\n"
            f"- Топ текст: «{bs['top_text']}»\n"
        )
    pricing_top = d.get("pricing_top", [])
    if len(pricing_top) >= 2:
        lines_p = [f"  ${amt:.0f}: {cnt} продаж = ${rev:.0f}" for amt, cnt, rev in pricing_top[:5]]
        extra_data += "\n💸 Чеки по размеру:\n" + "\n".join(lines_p) + "\n"
    refusal_list = d.get("refusal_list", [])
    if refusal_list:
        lines_r = [f"  {name}: ${price:.0f}" for name, price in refusal_list[:5]]
        extra_data += "\n📉 Открыли PPV но не купили:\n" + "\n".join(lines_r) + "\n"

    # ── extra output format instructions ─────────────────────────────────────
    extra_instructions = ""
    if bs.get("total_sent", 0) > 0:
        extra_instructions += (
            "\n📢 АНАЛИЗ РАССЫЛОК:\n"
            "- Конверсия [%] и доход $X\n"
            "- Вывод: был ли смысл? Что улучшить в тексте?\n"
        )
    if len(pricing_top) >= 2:
        extra_instructions += (
            "\n💸 ЦЕНООБРАЗОВАНИЕ:\n"
            "- Самый прибыльный чек: $X (N продаж)\n"
            "- Рекомендация по ценообразованию на следующую неделю\n"
        )
    if refusal_list:
        extra_instructions += (
            "\n📉 АНАЛИЗ ОТКАЗОВ:\n"
            "- Сколько фанов открыли но не купили, на какую сумму\n"
            "- Причина и как дожать\n"
        )
    extra_instructions += (
        "\n💡 ИДЕИ ДЛЯ РАССЫЛОК НА СЛЕДУЮЩУЮ НЕДЕЛЮ:\n"
        "1. [конкретная идея с примером текста, на основе данных о лучших фанах]\n"
        "2. [вторая идея с примером текста]\n"
        "3. [третья идея с примером текста]\n"
    )

    return _WEEKLY_AI_PROMPT.format(
        week_start    = d["week_start"],
        week_end      = d["week_end"],
        total_earned  = d["total_earned"],
        ppv_total     = d.get("ppv_total", 0),
        tips_total    = d.get("tips_total", 0),
        posts_total   = d.get("posts_total", 0),
        txn_count     = d["txn_count"],
        avg_check     = d["avg_check"],
        prev_total_str= prev_total_str,
        top3_str      = top3_str,
        new_fans_count= new_fans_count,
        new_fans_names= new_names,
        at_risk_str   = at_risk_str,
        chatters_str  = "\n".join(chatters_lines) or "  нет данных",
        history_str   = "\n".join(history_lines) or "  нет истории",
        extra_data    = extra_data,
        extra_instructions = extra_instructions,
    )


def _extract_broadcast_ideas(report_text: str) -> str | None:
    """Extract the 💡 ИДЕИ section from the weekly report, or None if absent."""
    idx = report_text.find("💡")
    return report_text[idx:].strip() if idx != -1 else None


_BROADCAST_AI_PROMPT = """\
Ты — эксперт по монетизации OnlyFans. Проанализируй рассылки чаттеров за прошедшие 7 дней и дай конкретные рекомендации по улучшению.

ДАННЫЕ О РАССЫЛКАХ:
{broadcasts_text}

Напиши анализ СТРОГО в этом формате (без вступлений):

📢 АНАЛИЗ РАССЫЛОК

📊 РЕЗУЛЬТАТЫ:
[по каждой рассылке: текст → ответов → доход → оценка 1-5]

🏆 ЧТО СРАБОТАЛО:
[конкретно что в топ-рассылках зацепило фанов]

❌ ЧТО НЕ СРАБОТАЛО:
[конкретно почему слабые рассылки не дали результат]

💡 3 УЛУЧШЕННЫХ ВАРИАНТА ДЛЯ СЛЕДУЮЩЕЙ НЕДЕЛИ:
1. «[текст]» — [почему лучше]
2. «[текст]» — [почему лучше]
3. «[текст]» — [почему лучше]

🎯 РЕКОМЕНДАЦИЯ:
[конкретный план по рассылкам на следующую неделю, 2-3 пункта]
"""


async def _run_broadcast_ai_report(app: Application) -> None:
    """Weekly AI analysis of chatters' broadcasts → sends to topic 94."""
    logger.info("Broadcast AI report: detecting broadcasts")
    try:
        results = await detect_broadcasts(days=7)
        if not results:
            logger.info("Broadcast AI report: no broadcasts found, skipping")
            return

        broadcasts_text = ""
        for i, b in enumerate(results[:10], 1):
            n    = b["recipients"]
            r    = b["replied"]
            rate = f"{r/n*100:.0f}%" if n else "0%"
            dt   = b["sent_at"][:16].replace("T", " ")
            broadcasts_text += (
                f"{i}. Текст: «{b['text'][:200]}»\n"
                f"   Дата: {dt} | Фанов: {n} | Ответили: {r} ({rate}) | Доход: ${b['revenue']:.0f}\n\n"
            )

        prompt = _BROADCAST_AI_PROMPT.format(broadcasts_text=broadcasts_text)
        logger.info("Broadcast AI report: calling Claude")
        report = await asyncio.to_thread(claude_client.chat, prompt)

        await send_broadcasts_message(app.bot, report)
        logger.info("Broadcast AI report: sent to topic 94")
    except Exception:
        logger.exception("_run_broadcast_ai_report failed")


async def _run_weekly_ai_report(app: Application) -> None:
    logger.info("Weekly AI report: collecting data")
    try:
        now_msk = datetime.now(TZ_MSK).replace(tzinfo=None)
        data    = await _collect_weekly_data(now_msk)
        prompt  = _build_weekly_prompt(data)

        logger.info("Weekly AI report: calling Claude")
        report_text = await asyncio.to_thread(claude_client.chat, prompt)

        await send_daily_message(app.bot, report_text)
        logger.info("Weekly AI report: sent to Дневная инфа")

        ideas = _extract_broadcast_ideas(report_text)
        if ideas:
            await send_broadcasts_message(app.bot, ideas)
            logger.info("Weekly AI report: broadcast ideas sent to topic 94")

        # Save to history
        entry = {
            "created_at":    now_msk.isoformat(),
            "week_start":    data["week_start"],
            "week_end":      data["week_end"],
            "total_earned":  data["total_earned"],
            "txn_count":     data["txn_count"],
            "avg_check":     data["avg_check"],
            "new_subs":      data.get("new_fans_count", 0),
            "fan_ids":       data.get("current_fan_ids", []),
            "ppv_total":     data.get("ppv_total", 0),
            "tips_total":    data.get("tips_total", 0),
            "posts_total":   data.get("posts_total", 0),
            "chatters":      {
                cs["name"]: {
                    "sales":          cs["sales"],
                    "messages":       cs["messages"],
                    "reply_time_avg": cs["reply_time_avg"],
                }
                for cs in data["chatter_stats"]
            },
            "summary": report_text[:500],
        }
        save_weekly_report(entry)
        logger.info("Weekly AI report: saved to weekly_reports.json")

    except Exception:
        logger.exception("_run_weekly_ai_report failed")


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the weekly AI report."""
    msg = await update.message.reply_text("⏳ Собираю данные за неделю...")
    try:
        now_msk = datetime.now(TZ_MSK).replace(tzinfo=None)
        data    = await _collect_weekly_data(now_msk)
        await msg.edit_text("🤖 Данные собраны, генерирую AI анализ...")
        prompt  = _build_weekly_prompt(data)
        report_text = await asyncio.to_thread(claude_client.chat, prompt)

        await send_daily_message(context.bot, report_text)

        ideas = _extract_broadcast_ideas(report_text)
        if ideas:
            await send_broadcasts_message(context.bot, ideas)

        entry = {
            "created_at":   now_msk.isoformat(),
            "week_start":   data["week_start"],
            "week_end":     data["week_end"],
            "total_earned": data["total_earned"],
            "txn_count":    data["txn_count"],
            "avg_check":    data["avg_check"],
            "new_subs":     data.get("new_fans_count", 0),
            "fan_ids":      data.get("current_fan_ids", []),
            "chatters": {
                cs["name"]: {
                    "sales":          cs["sales"],
                    "messages":       cs["messages"],
                    "reply_time_avg": cs["reply_time_avg"],
                }
                for cs in data["chatter_stats"]
            },
            "summary": report_text[:500],
        }
        save_weekly_report(entry)
        await msg.edit_text("✅ Еженедельный отчёт отправлен в «Дневная инфа»")
    except Exception as e:
        logger.exception("cmd_weekly failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── SCHEDULER LOOP ───────────────────────────────────────────────────────────

async def scheduler_loop(app: Application) -> None:
    reported_today            : set[str] = set()
    analyzed_this_week        : set[str] = set()
    synced_today              : set[str] = set()
    chatter_reported_today    : set[str] = set()
    reactivation_done_week    : set[str] = set()
    weekly_ai_done_week       : set[str] = set()
    broadcast_ai_done_week    : set[str] = set()
    nightdrive_done_today     : set[str] = set()

    while True:
        now  = datetime.now()
        wait = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(max(wait, 1))

        msk   = datetime.now(TZ_MSK)
        today = msk.date().isoformat()
        week  = f"{msk.year}-W{msk.isocalendar()[1]}"

        # Daily report at 13:00 MSK → topic "Дневная инфа"
        if msk.hour == SCHEDULE_HOUR_MSK and msk.minute == 0 and today not in reported_today:
            reported_today.add(today)
            asyncio.create_task(daily_morning_report(app))
            logger.info("Scheduler: daily report triggered")

        # Sunday 13:00 MSK → analyze_all + weekly summary → topic "Всякое"
        if (msk.weekday() == SCHEDULE_WEEKDAY_WEEKLY and msk.hour == SCHEDULE_HOUR_MSK
                and msk.minute == 0 and week not in analyzed_this_week):
            analyzed_this_week.add(week)
            asyncio.create_task(run_weekly_analyze(app))
            logger.info("Scheduler: weekly analyze triggered")

        # Nightly 03:00 MSK → sync payment stats for all fans
        if msk.hour == 3 and msk.minute == 0 and today not in synced_today:
            synced_today.add(today)
            asyncio.create_task(_scheduled_sync_payments(app))
            logger.info("Scheduler: nightly payment sync triggered")

        # Daily 13:00 MSK → chatter report → topic "Чаттерс"
        if (msk.hour == SCHEDULE_HOUR_MSK and msk.minute == 0
                and today not in chatter_reported_today):
            chatter_reported_today.add(today)
            asyncio.create_task(_run_chatter_report_scheduled(app))
            logger.info("Scheduler: chatter report triggered")

        # Sunday 13:00 MSK → reactivation report → topic "Всякое"
        if (msk.weekday() == SCHEDULE_WEEKDAY_WEEKLY and msk.hour == SCHEDULE_HOUR_MSK
                and msk.minute == 0 and week not in reactivation_done_week):
            reactivation_done_week.add(week)
            asyncio.create_task(_run_reactivation_report(app))
            logger.info("Scheduler: reactivation report triggered")

        # Sunday 14:00 MSK → weekly AI report → topic "Дневная инфа"
        if (msk.weekday() == SCHEDULE_WEEKDAY_WEEKLY and msk.hour == 14
                and msk.minute == 0 and week not in weekly_ai_done_week):
            weekly_ai_done_week.add(week)
            asyncio.create_task(_run_weekly_ai_report(app))
            logger.info("Scheduler: weekly AI report triggered")

        # Sunday 15:00 MSK → broadcast AI analysis → topic 94
        if (msk.weekday() == SCHEDULE_WEEKDAY_WEEKLY and msk.hour == 15
                and msk.minute == 0 and week not in broadcast_ai_done_week):
            broadcast_ai_done_week.add(week)
            asyncio.create_task(_run_broadcast_ai_report(app))
            logger.info("Scheduler: broadcast AI report triggered")

        # Sunday 03:00 MSK → auto-refresh style guide if 14+ days old
        if (msk.weekday() == SCHEDULE_WEEKDAY_WEEKLY and msk.hour == 3
                and msk.minute == 0):
            asyncio.create_task(_auto_update_style_if_needed(app))
            logger.info("Scheduler: style guide auto-update check triggered")

        # 00:00 MSK daily → reset daily counters + run nightdrive
        if msk.hour == 0 and msk.minute == 0 and today not in nightdrive_done_today:
            nightdrive_done_today.add(today)
            asyncio.create_task(_reset_daily_message_counts())
            asyncio.create_task(_run_nightdrive(app))
            logger.info("Scheduler: nightdrive triggered")


# ─── REACTIVATION ─────────────────────────────────────────────────────────────

_REACTIVATION_PROMPT = """\
{style_prefix}You are a chatter working for a creator on OnlyFans. Never mention competing platforms.
Write a personal reactivation message to a fan who has gone quiet, on behalf of the creator.

Fan profile:
{profile_text}

Spent ${total_spent:.0f} total. Last purchase: {last_purchase}. Silent for {days_silent} days.

Requirements:
- 2-4 sentences in English
- Warm and genuinely personal — use their name or nickname if known
- Reference something specific from their profile (fetish, hobby, interest, or past conversation)
- Make them feel missed and special, like the creator was thinking of them specifically
- Hint at something they would love (without being explicit or mentioning prices)
- No pressure, no template phrases like "I noticed you've been quiet"
- Should read as a real spontaneous message from the creator, not a sales pitch
"""


async def _run_reactivation_report(app: Application) -> None:
    logger.info("Reactivation report started")
    try:
        fans = load_fans()
        now  = datetime.now()

        candidates: list[tuple[str, dict, int, float]] = []
        for fan_id, fan_data in fans.items():
            if fan_data.get("blocked"):
                continue
            total_spent = float(
                fan_data.get("total_spent") or fan_data.get("payment_total") or 0
            )
            if total_spent <= 0:
                continue
            last_msg_dt = parse_dt(fan_data.get("last_message_date"))
            if not last_msg_dt:
                continue
            days_silent = (now - last_msg_dt).days
            if days_silent < 7:
                continue
            candidates.append((fan_id, fan_data, days_silent, total_spent))

        candidates.sort(key=lambda x: x[3], reverse=True)

        if not candidates:
            await send_misc_message(app.bot, "😴 Нет молчащих фанов для реактивации (все активны или нет платящих)")
            return

        await send_misc_message(app.bot, f"😴 <b>Реактивация молчунов — {len(candidates)} фанов</b>", parse_mode="HTML")

        for fan_id, fan_data, days_silent, total_spent in candidates:
            profile = fan_data.get("profile", {})
            dname   = _clean(fan_display_name(fan_id, fan_data))
            warmth  = profile.get("warmth")

            desc_parts = []
            if profile.get("fetishes"):
                desc_parts.append("фетиш: " + _clean(", ".join(str(f) for f in profile["fetishes"][:3])))
            if profile.get("chat_style"):
                desc_parts.append(_clean(_truncate_sentence(str(profile["chat_style"]), 80)))

            lines = [
                f"😴 Молчит {days_silent} дн — {dname}",
                f"💰 ${total_spent:.0f} за всё время",
                f"🔥 Прогрет: {_stars(warmth)}",
            ]
            if desc_parts:
                lines.append(f"💬 {desc_parts[0]}")

            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✍️ Сгенерировать сообщение", callback_data=f"reactivate:{fan_id}")
            ]])
            await send_misc_message(app.bot, "\n".join(lines), reply_markup=markup)
            await asyncio.sleep(0.3)

        logger.info("Reactivation report sent: %d fans", len(candidates))
    except Exception:
        logger.exception("_run_reactivation_report failed")


async def handle_reactivate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()

    fan_id   = query.data.split(":", 1)[1]
    fans     = load_fans()
    fan_data = fans.get(fan_id, {})
    profile  = fan_data.get("profile", {})
    dname    = fan_display_name(fan_id, fan_data)

    now         = datetime.now()
    total_spent = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
    last_msg_dt = parse_dt(fan_data.get("last_message_date"))
    days_silent = (now - last_msg_dt).days if last_msg_dt else "?"
    last_pur_dt = parse_dt(fan_data.get("last_purchase_date"))
    last_purchase = f"{(now - last_pur_dt).days} days ago" if last_pur_dt else "unknown"

    profile_lines: list[str] = []
    if profile.get("name"):
        profile_lines.append(f"Name: {_clean(profile['name'])}")
    if profile.get("birthday"):
        profile_lines.append(f"Age/Birthday: {_clean(str(profile['birthday']))}")
    if profile.get("job"):
        profile_lines.append(f"Job: {_clean(str(profile['job']))}")
    if profile.get("hobbies"):
        profile_lines.append(f"Hobbies: {_clean(', '.join(str(h) for h in profile['hobbies']))}")
    if profile.get("personal_life"):
        profile_lines.append(f"Personal life: {_clean(str(profile['personal_life']))}")
    if profile.get("location"):
        profile_lines.append(f"Location: {_clean(str(profile['location']))}")
    if profile.get("fetishes"):
        profile_lines.append(f"Fetishes/interests: {_clean(', '.join(str(f) for f in profile['fetishes']))}")
    if profile.get("chat_style"):
        profile_lines.append(f"How to talk to him: {_clean(str(profile['chat_style']))}")
    if profile.get("notes"):
        profile_lines.append(f"Notes: {_clean(str(profile['notes']))}")
    profile_text = "\n".join(profile_lines) or "No profile data available"

    prompt = _REACTIVATION_PROMPT.format(
        style_prefix=_style_prefix(),
        profile_text=profile_text,
        total_spent=total_spent,
        last_purchase=last_purchase,
        days_silent=days_silent,
    )

    await query.message.reply_text(f"✍️ Генерирую сообщение для {dname}...")
    try:
        text = await asyncio.to_thread(claude_client.chat, prompt)
        clean_dname = _clean(dname)
        await query.message.reply_text(
            f'✍️ Текст для {clean_dname}:\n\n"{text}"\n\n📋 Скопируй и отправь в OnlyMonster'
        )
        logger.info("Reactivation message generated for fan %s", fan_id)
    except Exception as e:
        logger.exception("handle_reactivate: generation failed for %s", fan_id)
        await query.message.reply_text(f"❌ Ошибка генерации: {e}")


async def cmd_reactivation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the reactivation report."""
    msg = await update.message.reply_text("⏳ Ищу молчунов...")
    try:
        fans = load_fans()
        now  = datetime.now()
        candidates: list[tuple[str, dict, int, float]] = []
        for fan_id, fan_data in fans.items():
            if fan_data.get("blocked") or fan_data.get("auto_stopped"):
                continue
            total_spent = float(
                fan_data.get("total_spent") or fan_data.get("payment_total") or 0
            )
            if total_spent <= 0:
                continue
            last_msg_dt = parse_dt(fan_data.get("last_message_date"))
            if not last_msg_dt:
                continue
            days_silent = (now - last_msg_dt).days
            if days_silent < 7:
                continue
            candidates.append((fan_id, fan_data, days_silent, total_spent))

        candidates.sort(key=lambda x: x[3], reverse=True)
        await msg.edit_text(f"✅ Найдено {len(candidates)} молчащих фанов. Отправляю в топик «Всякое»...")

        if not candidates:
            await msg.edit_text("😴 Нет молчащих фанов (все активны или нет платящих).")
            return

        await send_misc_message(
            context.bot,
            f"😴 <b>Реактивация молчунов — {len(candidates)} фанов</b>",
            parse_mode="HTML",
        )
        for fan_id, fan_data, days_silent, total_spent in candidates:
            profile = fan_data.get("profile", {})
            dname   = _clean(fan_display_name(fan_id, fan_data))
            warmth  = profile.get("warmth")

            desc_parts = []
            if profile.get("fetishes"):
                desc_parts.append("фетиш: " + _clean(", ".join(str(f) for f in profile["fetishes"][:3])))
            if profile.get("chat_style"):
                desc_parts.append(_clean(_truncate_sentence(str(profile["chat_style"]), 80)))

            lines = [
                f"😴 Молчит {days_silent} дн — {dname}",
                f"💰 ${total_spent:.0f} за всё время",
                f"🔥 Прогрет: {_stars(warmth)}",
            ]
            if desc_parts:
                lines.append(f"💬 {desc_parts[0]}")

            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✍️ Сгенерировать сообщение", callback_data=f"reactivate:{fan_id}")
            ]])
            await send_misc_message(context.bot, "\n".join(lines), reply_markup=markup)
            await asyncio.sleep(0.3)

        await msg.edit_text(f"✅ Отчёт реактивации отправлен: {len(candidates)} фанов")
    except Exception as e:
        logger.exception("cmd_reactivation failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── NIGHTDRIVE ────────────────────────────────────────────────────────────────

# Module-level state
_nd_texts       : dict[str, dict] = {}  # fan_id → {text, account_id, dname}
_nd_edit_pending: dict[int, dict] = {}  # prompt_msg_id → {fan_id, account_id, dname, orig_msg}
_nd_sent_tonight: int             = 0
_nd_last_sent   : datetime | None = None

_NIGHTDRIVE_PROMPT = """\
{style_prefix}You are a chatter for an OnlyFans creator. Write a short, personal re-engagement message \
to a fan who has gone quiet.

Fan profile:
{profile_text}

Spent ${total_spent:.0f} total. Silent for {days_silent} days.

Requirements:
- MAXIMUM 2 sentences — short and natural
- Write as the creator herself, first person
- Match the style guide exactly: same tone, emoji usage, phrasing
- Warm and personal — make them feel the creator thought of them specifically
- Do NOT mention they've been quiet or away
- No pressure, no sales pitch — just a genuine, flirty check-in
- Output ONLY the message text, no quotes, no commentary
"""


async def _check_unanswered_streak(account_id: str, fan_id: str) -> int:
    """Count consecutive trailing outgoing messages with no fan reply.
    Returns the streak only when consecutive messages are ≥24h apart (separate sessions).
    Returns 0 if the gap is under 24h — multiple messages sent in one session don't count."""
    try:
        msgs = await asyncio.wait_for(
            asyncio.to_thread(om_client.get_messages, account_id, fan_id, 20),
            timeout=10,
        )
    except Exception:
        return 0

    # Collect trailing outgoing messages (newest→oldest via reversed)
    streak_msgs: list[dict] = []
    for m in reversed(msgs):
        if m.get("is_sent_by_me"):
            streak_msgs.append(m)
        else:
            break

    streak = len(streak_msgs)
    if streak < 2:
        return streak

    # Require at least 24h between the oldest and newest outgoing message
    times = [parse_dt(m.get("created_at")) for m in streak_msgs]
    times = [t for t in times if t]
    if len(times) >= 2:
        gap_hours = (max(times) - min(times)).total_seconds() / 3600
        if gap_hours < 24:
            return 0  # all sent in the same session — no auto-stop

    return streak


async def _do_nd_send(
    app: Application,
    fan_id: str,
    account_id: str,
    text: str,
    dname: str,
    orig_msg,
    delay_mins: int,
    edited: bool = False,
) -> None:
    global _nd_sent_tonight, _nd_last_sent

    logger.info(
        "nd_send START: fan_id=%s account_id=%s delay=%dm edited=%s text=%r",
        fan_id, account_id, delay_mins, edited, text[:100],
    )

    await asyncio.sleep(delay_mins * 60)

    if _nd_sent_tonight >= 5:
        logger.info("nd_send: limit reached during delay, aborting fan=%s", fan_id)
        try:
            await orig_msg.edit_text(f"🚫 Лимит 5 сообщений — отмена для {dname}")
        except Exception:
            pass
        return

    if is_stopped():
        logger.info("nd_send: aborted — stop_all active (fan=%s)", fan_id)
        try:
            await orig_msg.edit_text(f"🛑 Остановлено — {dname}")
        except Exception:
            pass
        return

    logger.info("nd_send: calling OM API send_message fan=%s acc=%s", fan_id, account_id)
    try:
        result = await asyncio.to_thread(om_client.send_message, account_id, fan_id, text)
        logger.info("nd_send: OM API response: %s", result)
    except Exception as e:
        logger.error("nd_send: OM API FAILED fan=%s: %s", fan_id, e, exc_info=True)
        try:
            await orig_msg.edit_text(f"❌ Ошибка отправки — {dname}: {e}")
        except Exception:
            pass
        return

    # Update fans_data
    fans = load_fans()
    if fan_id in fans:
        fans[fan_id]["last_reply_date"]     = datetime.now().strftime("%Y-%m-%d")
        fans[fan_id]["daily_message_count"] = int(fans[fan_id].get("daily_message_count", 0)) + 1
    save_fans(fans)

    append_sent_log({
        "date":       datetime.now().isoformat(),
        "fan_id":     fan_id,
        "fan_name":   dname,
        "account_id": account_id,
        "text":       text,
        "edited":     edited,
    })

    _nd_sent_tonight += 1
    _nd_last_sent     = datetime.now()
    logger.info("nd_send: SUCCESS fan=%s sent_tonight=%d", fan_id, _nd_sent_tonight)

    label = "✅ Отправлено (отредактировано)" if edited else "✅ Отправлено"

    # Update the original card
    try:
        await orig_msg.edit_text(
            f"{label} — {dname} (через {delay_mins} мин)\n\n"
            f'✍️ "{text[:200]}"'
        )
    except Exception as ex:
        logger.warning("nd_send: could not edit orig_msg: %s", ex)

    # Always send a visible confirmation message in the topic
    try:
        await send_nightdrive_message(
            app.bot,
            f"{label} — {dname}\n✍️ \"{text[:150]}\""
        )
    except Exception as ex:
        logger.warning("nd_send: confirmation message failed: %s", ex)

    # Auto-stop if 2 consecutive unanswered messages (each sent on a different day)
    streak = await _check_unanswered_streak(account_id, fan_id)
    logger.info("nd_send: unanswered streak for fan=%s: %d", fan_id, streak)
    if streak >= 2:
        fans = load_fans()
        if fan_id in fans:
            fans[fan_id]["auto_stopped"]        = True
            fans[fan_id]["auto_stopped_at"]     = datetime.now().isoformat()
            fans[fan_id]["auto_stopped_streak"] = streak
            save_fans(fans)
            logger.info("nd_send: auto_stopped fan=%s (streak=%d)", fan_id, streak)
        try:
            await send_nightdrive_message(
                app.bot,
                f"⚠️ {dname} добавлен в стоп-лист: {streak} сообщения подряд без ответа"
            )
        except Exception:
            pass


async def _run_nightdrive(app: Application) -> None:
    global _nd_sent_tonight
    _nd_sent_tonight = 0
    _nd_texts.clear()
    _nd_edit_pending.clear()

    if is_stopped():
        logger.info("nightdrive: stop_all active, skipping")
        return

    fans     = load_fans()
    accounts = await asyncio.to_thread(get_all_accounts)
    default_acc = accounts[0]["id"] if accounts else ""

    now     = datetime.now()
    today_s = now.strftime("%Y-%m-%d")

    candidates: list[tuple[str, dict, int, float]] = []
    for fan_id, fan_data in fans.items():
        if fan_data.get("blocked") or fan_data.get("auto_stopped"):
            continue
        total_spent = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
        if total_spent <= 0:
            continue
        if int(fan_data.get("daily_message_count", 0)) > 0:
            continue
        if fan_data.get("last_reply_date") == today_s:
            continue
        last_msg_dt = parse_dt(fan_data.get("last_message_date"))
        if not last_msg_dt:
            continue
        days_silent = (now - last_msg_dt).days
        if days_silent < 3:
            continue
        candidates.append((fan_id, fan_data, days_silent, total_spent))

    candidates.sort(key=lambda x: x[3], reverse=True)
    top5 = candidates[:5]

    if not top5:
        logger.info("nightdrive: no candidates tonight")
        return

    await send_nightdrive_message(app.bot, f"🌙 Ночной дожим — {len(top5)} фанов")

    for fan_id, fan_data, days_silent, total_spent in top5:
        profile  = fan_data.get("profile", {})
        dname    = _clean(fan_display_name(fan_id, fan_data))
        acc_id   = fan_data.get("account_id") or default_acc

        profile_lines: list[str] = []
        if profile.get("name"):
            profile_lines.append(f"Name: {_clean(profile['name'])}")
        if profile.get("fetishes"):
            profile_lines.append(f"Fetishes: {_clean(', '.join(str(f) for f in profile['fetishes']))}")
        if profile.get("chat_style"):
            profile_lines.append(f"Chat style: {_clean(str(profile['chat_style']))}")
        if profile.get("notes"):
            profile_lines.append(f"Notes: {_clean(str(profile['notes']))}")
        profile_text = "\n".join(profile_lines) or "No profile data"

        prompt = _NIGHTDRIVE_PROMPT.format(
            style_prefix=_style_prefix(),
            profile_text=profile_text,
            total_spent=total_spent,
            days_silent=days_silent,
        )

        try:
            text = await asyncio.to_thread(claude_client.chat, prompt)
            text = text.strip().strip('"')
        except Exception as e:
            logger.warning("nightdrive: Claude failed for %s: %s", fan_id, e)
            text = "Hey, was just thinking about you 💕"

        reason = f"Молчит {days_silent} дн, потратил ${total_spent:.0f}"

        msg_text = (
            f"🌙 Ночной дожим — {dname}\n"
            f"💰 ${total_spent:.0f} за всё время • Молчит {days_silent} дней\n"
            f"🎯 Причина: {reason}\n\n"
            f'✍️ Предлагаемое сообщение:\n"{text}"'
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить",     callback_data=f"nd_send:{fan_id}"),
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"nd_edit:{fan_id}"),
        ]])

        _nd_texts[fan_id] = {"text": text, "account_id": acc_id, "dname": dname}

        await send_nightdrive_message(app.bot, msg_text, reply_markup=markup)
        await asyncio.sleep(0.5)

    logger.info("nightdrive: posted %d cards", len(top5))


async def handle_nd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _nd_sent_tonight, _nd_last_sent

    query  = update.callback_query
    fan_id = query.data.split(":", 1)[1]
    logger.info("nd_send button: fan_id=%s", fan_id)

    # Wrap query.answer() — it throws BadRequest if the query is expired (>60s old)
    async def _safe_answer(text: str = "", show_alert: bool = False) -> bool:
        try:
            await query.answer(text, show_alert=show_alert)
            return True
        except Exception as ex:
            logger.warning("nd_send: query.answer failed (expired?): %s", ex)
            return False

    state = _nd_texts.get(fan_id)
    if not state:
        logger.warning("nd_send: no state for fan_id=%s (keys: %s)", fan_id, list(_nd_texts.keys()))
        answered = await _safe_answer("❌ Данные устарели, запусти /nightdrive заново", show_alert=True)
        if not answered:
            try:
                await query.message.reply_text("❌ Данные устарели, запусти /nightdrive заново")
            except Exception:
                pass
        return

    if is_stopped():
        await _safe_answer("🛑 Отправки остановлены (/resume_all)", show_alert=True)
        return

    if _nd_sent_tonight >= 5:
        await _safe_answer("🚫 Лимит 5 сообщений за ночь исчерпан", show_alert=True)
        return

    now = datetime.now()
    if _nd_last_sent and (now - _nd_last_sent).total_seconds() < 600:
        remaining = int(600 - (now - _nd_last_sent).total_seconds())
        await _safe_answer(f"⏱ Подожди ещё {remaining // 60}м {remaining % 60}с", show_alert=True)
        return

    # All checks passed — dismiss spinner
    await _safe_answer()

    delay      = random.randint(3, 15)
    text       = state["text"]
    account_id = state["account_id"]
    dname      = state["dname"]

    logger.info("nd_send: fan=%s acc=%s delay=%dm text=%r", fan_id, account_id, delay, text[:80])

    # Immediately edit card to show pending status (removes buttons)
    try:
        await query.message.edit_text(
            f"🌙 Ночной дожим — {dname}\n"
            f"⏳ Отправляю через {delay} мин...\n\n"
            f'✍️ "{text[:200]}"'
        )
    except Exception as ex:
        logger.warning("nd_send: edit_text failed: %s", ex)
        try:
            await query.message.reply_text(f"⏳ Отправляю для {dname} через {delay} мин...")
        except Exception:
            pass

    asyncio.create_task(_do_nd_send(
        app=context.application,
        fan_id=fan_id,
        account_id=account_id,
        text=text,
        dname=dname,
        orig_msg=query.message,
        delay_mins=delay,
        edited=False,
    ))


async def handle_nd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    fan_id = query.data.split(":", 1)[1]
    logger.info("nd_edit button: fan_id=%s", fan_id)

    state = _nd_texts.get(fan_id)
    if not state:
        try:
            await query.answer("❌ Данные устарели, запусти /nightdrive заново", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception as ex:
        logger.warning("nd_edit: query.answer failed (expired?): %s", ex)

    dname      = state["dname"]
    account_id = state["account_id"]

    # Send prompt and key pending by the prompt message ID
    # (bots always receive replies to their own messages — works even with privacy mode)
    prompt_msg = await send_nightdrive_message(
        context.bot,
        f"✏️ Ответь на это сообщение своим текстом для {dname} — отправлю с задержкой 3–15 мин"
    )
    if not prompt_msg:
        await query.message.reply_text("❌ Не могу отправить сообщение-подсказку")
        return

    logger.info("nd_edit: prompt_msg_id=%d fan=%s acc=%s", prompt_msg.message_id, fan_id, account_id)
    _nd_edit_pending[prompt_msg.message_id] = {
        "fan_id":     fan_id,
        "account_id": account_id,
        "dname":      dname,
        "orig_msg":   query.message,
    }


async def handle_nd_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch a reply to the nightdrive edit-prompt message and use it as custom send text."""
    msg = update.message
    if not msg or not msg.text or not msg.reply_to_message:
        return

    reply_to_id = msg.reply_to_message.message_id
    logger.info(
        "nd_custom_text: reply_to=%d pending_ids=%s text=%r",
        reply_to_id, list(_nd_edit_pending.keys()), msg.text[:60],
    )

    pending = _nd_edit_pending.pop(reply_to_id, None)
    if not pending:
        return

    fan_id     = pending["fan_id"]
    account_id = pending["account_id"]
    dname      = pending["dname"]
    orig_msg   = pending["orig_msg"]
    text       = msg.text.strip()

    logger.info("nd_custom_text: using custom text for fan=%s: %r", fan_id, text[:80])

    if is_stopped():
        await msg.reply_text("🛑 Отправки остановлены (/resume_all)")
        return

    if _nd_sent_tonight >= 5:
        await msg.reply_text("🚫 Лимит 5 сообщений за ночь исчерпан")
        return

    delay = random.randint(3, 15)
    await msg.reply_text(f"⏳ Отправлю для {dname} через {delay} мин...")

    asyncio.create_task(_do_nd_send(
        app=context.application,
        fan_id=fan_id,
        account_id=account_id,
        text=text,
        dname=dname,
        orig_msg=orig_msg,
        delay_mins=delay,
        edited=True,
    ))


async def _reset_daily_message_counts() -> None:
    fans = load_fans()
    for fid in fans:
        fans[fid]["daily_message_count"] = 0
    save_fans(fans)
    logger.info("nightdrive: reset daily_message_count for %d fans", len(fans))


async def cmd_nightdrive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual nightdrive trigger for testing."""
    msg = await update.message.reply_text("🌙 Запускаю ночной дожим...")
    try:
        await _run_nightdrive(context.application)
        await msg.edit_text("🌙 Ночной дожим запущен — карточки отправлены в топик")
    except Exception as e:
        logger.exception("cmd_nightdrive failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_bot_status({"stop_all": True})
    await update.message.reply_text("🛑 Все авто-отправки остановлены")


async def cmd_resume_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_bot_status({"stop_all": False})
    await update.message.reply_text("✅ Авто-отправки возобновлены")


async def cmd_sent_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log = load_sent_log()
    if not log:
        await update.message.reply_text("📭 Лог пуст — ещё ничего не отправлено")
        return
    last10 = log[-10:]
    lines = ["📋 Последние 10 отправленных:\n"]
    for e in reversed(last10):
        date  = e.get("date", "")[:16].replace("T", " ")
        name  = e.get("fan_name", e.get("fan_id", "?"))
        text  = e.get("text", "")[:60]
        edited = " (ред.)" if e.get("edited") else ""
        lines.append(f"• {date}{edited} — {name}\n  {text}")
    await update.message.reply_text("\n".join(lines))


async def cmd_stoplist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fans = load_fans()
    stopped = [
        (fid, fd) for fid, fd in fans.items() if fd.get("auto_stopped")
    ]
    if not stopped:
        await update.message.reply_text("✅ Стоп-лист пуст")
        return
    now   = datetime.now()
    lines = [f"🚫 Стоп-лист ({len(stopped)} фанов):\n"]
    for fid, fd in stopped:
        name    = fan_display_name(fid, fd)
        spent   = float(fd.get("total_spent") or fd.get("payment_total") or 0)
        streak  = int(fd.get("auto_stopped_streak") or 2)
        last_dt = parse_dt(fd.get("last_message_date"))
        days_silent = (now - last_dt).days if last_dt else "?"
        lines.append(
            f"• {name} — {streak} сообщ. без ответа • "
            f"молчит {days_silent} дн • ${spent:.0f} всего"
        )
    await update.message.reply_text("\n".join(lines))


# ─── /ask ─────────────────────────────────────────────────────────────────────

_ASK_PROMPT = """\
Ты — эксперт по монетизации OnlyFans. Отвечай строго на русском, конкретно, 3-5 предложений.

ДОСЬЕ ФАНА ({dname}):
{profile_text}

ПОСЛЕДНИЕ СООБЩЕНИЯ (от новых к старым):
{messages_text}

ВОПРОС МЕНЕДЖЕРА:
{question}

Дай конкретный совет основанный на данных выше. Без воды, без общих слов.
"""


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Использование: /ask [fan_id] [вопрос]\n"
            "Пример: /ask 12345 как лучше предложить ему PPV сейчас?"
        )
        return

    fan_id   = args[0]
    question = " ".join(args[1:])

    fans     = load_fans()
    fan_data = fans.get(fan_id, {})
    if not fan_data:
        await update.message.reply_text(f"❌ Фан {fan_id} не найден в базе")
        return

    dname    = fan_display_name(fan_id, fan_data)
    msg      = await update.message.reply_text(f"🤖 Читаю чат с {dname}...")

    # Get last 20 messages
    accounts = await asyncio.to_thread(get_all_accounts)
    acc_id   = fan_data.get("account_id") or (accounts[0]["id"] if accounts else "")
    chat_lines: list[str] = []
    if acc_id:
        try:
            msgs = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_messages, acc_id, fan_id, 20),
                timeout=15,
            )
            for m in msgs:
                who  = "Модель" if m.get("is_sent_by_me") else "Фан"
                text = _clean(_TAG.sub("", str(m.get("text") or ""))).strip()
                if text:
                    chat_lines.append(f"{who}: {text[:200]}")
        except Exception as e:
            logger.warning("cmd_ask: get_messages: %s", e)

    # Build profile text
    profile = fan_data.get("profile", {})
    prof_parts: list[str] = []
    if profile.get("name"):
        prof_parts.append(f"Имя: {_clean(profile['name'])}")
    total_spent = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
    prof_parts.append(f"Потратил: ${total_spent:.0f}")
    last_msg = fan_data.get("last_message_date", "")
    if last_msg:
        days = (datetime.now() - (parse_dt(last_msg) or datetime.now())).days
        prof_parts.append(f"Молчит: {days} дн")
    if profile.get("fetishes"):
        prof_parts.append(f"Фетиши: {_clean(', '.join(str(f) for f in profile['fetishes']))}")
    if profile.get("chat_style"):
        prof_parts.append(f"Стиль общения: {_clean(str(profile['chat_style']))}")
    if profile.get("notes"):
        prof_parts.append(f"Заметки: {_clean(str(profile['notes']))}")

    prompt = _ASK_PROMPT.format(
        dname         = dname,
        profile_text  = "\n".join(prof_parts) or "нет данных",
        messages_text = "\n".join(chat_lines) or "нет сообщений",
        question      = question,
    )

    try:
        await msg.edit_text(f"🤖 Анализирую...")
        answer = await asyncio.to_thread(claude_client.chat, prompt)
        await msg.edit_text(f"🤖 Анализ {dname}:\n\n{answer}")
    except Exception as e:
        logger.exception("cmd_ask failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── BROADCAST ANALYTICS ──────────────────────────────────────────────────────

async def detect_broadcasts(days: int = 7,
                            prefetched_txns: list[dict] | None = None) -> list[dict]:
    """
    Scan outgoing messages for all fans over the last `days` days.
    Groups messages by text — groups with 3+ recipients = broadcast.
    Returns list sorted by most recent first, with reply + revenue stats.
    Pass prefetched_txns to skip the transaction API call (avoids double fetch in weekly report).
    """
    fans     = load_fans()
    accounts = await asyncio.to_thread(get_all_accounts)
    default_acc = accounts[0]["id"] if accounts else ""
    since = datetime.now() - timedelta(days=days)

    # Fetch messages for all fans (max 5 concurrent to avoid hammering API)
    fan_messages: dict[str, list[dict]] = {}
    sem = asyncio.Semaphore(5)

    async def _fetch(fan_id: str, fdata: dict) -> None:
        async with sem:
            acc_id = fdata.get("account_id") or default_acc
            if not acc_id:
                return
            try:
                msgs = await asyncio.wait_for(
                    asyncio.to_thread(om_client.get_messages, acc_id, fan_id, 100),
                    timeout=12,
                )
                fan_messages[fan_id] = msgs if isinstance(msgs, list) else []
            except Exception as e:
                logger.warning("detect_broadcasts: fan %s: %s", fan_id, e)

    await asyncio.gather(*[_fetch(fid, fd) for fid, fd in fans.items()])

    # Use prefetched transactions or fetch fresh
    if prefetched_txns is not None:
        all_txns = prefetched_txns
    else:
        all_txns = []
        for acct in accounts:
            plat_acc = acct.get("platform_account_id", "")
            if not plat_acc:
                continue
            try:
                batch = await asyncio.wait_for(
                    asyncio.to_thread(om_client.get_all_transactions_paged, plat_acc, days),
                    timeout=60,
                )
                all_txns.extend(batch)
            except Exception as e:
                logger.warning("detect_broadcasts: txns %s: %s", plat_acc, e)

    # Build txn lookup: fan_id → [(datetime, amount)]
    txn_lookup: dict[str, list[tuple]] = {}
    for txn in all_txns:
        fid = str((txn.get("fan") or {}).get("id") or "")
        amt = float(txn.get("amount") or 0)
        ts_str = txn.get("timestamp") or txn.get("created_at")
        if fid and amt > 0 and ts_str:
            dt = parse_dt(str(ts_str))
            if dt:
                txn_lookup.setdefault(fid, []).append((dt, amt))

    # Group outgoing messages by text (first 120 chars, lowercase)
    text_groups: dict[str, dict] = {}
    for fan_id, msgs in fan_messages.items():
        for m in msgs:
            if not m.get("is_sent_by_me"):
                continue
            text = (m.get("text") or "").strip()
            if not text:
                continue
            ts = parse_dt(m.get("created_at") or m.get("timestamp") or "")
            if not ts or ts < since:
                continue
            key = text[:120].lower().strip()
            if key not in text_groups:
                text_groups[key] = {"text": text, "fans": {}, "earliest": ts}
            grp = text_groups[key]
            if fan_id not in grp["fans"] or ts < grp["fans"][fan_id]:
                grp["fans"][fan_id] = ts
            if ts < grp["earliest"]:
                grp["earliest"] = ts

    # Build results for groups with 3+ recipients
    results = []
    for grp in text_groups.values():
        n = len(grp["fans"])
        if n < 3:
            continue

        replied = 0
        revenue = 0.0
        for fan_id, sent_ts in grp["fans"].items():
            # Reply: any incoming message after broadcast was sent
            msgs_all = fan_messages.get(fan_id, [])
            if any(
                not m.get("is_sent_by_me") and
                (parse_dt(m.get("created_at") or m.get("timestamp") or "") or datetime.min) > sent_ts
                for m in msgs_all
            ):
                replied += 1
            # Revenue: any purchase after broadcast
            for txn_ts, amt in txn_lookup.get(fan_id, []):
                if txn_ts > sent_ts:
                    revenue += amt

        results.append({
            "text":       grp["text"][:300],
            "sent_at":    grp["earliest"].isoformat(),
            "recipients": n,
            "replied":    replied,
            "revenue":    revenue,
        })

    results.sort(key=lambda x: x["sent_at"], reverse=True)
    logger.info("detect_broadcasts: %d broadcasts detected across %d fans", len(results), len(fans))
    return results


async def cmd_broadcasts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect and show broadcasts sent by chatters over the last 7 days."""
    days = 7
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            pass

    msg = await update.message.reply_text(f"🔍 Сканирую рассылки за {days} дней...")
    try:
        results = await detect_broadcasts(days=days)
        if not results:
            await msg.edit_text(
                f"📭 Рассылок не найдено за {days} дней\n"
                "(нет одинакового текста у 3+ фанов)"
            )
            return

        lines = [f"📢 Рассылки за {days} дней — {len(results)} шт.\n"]
        for i, b in enumerate(results[:10], 1):
            n    = b["recipients"]
            r    = b["replied"]
            rate = f"{r/n*100:.0f}%" if n else "0%"
            dt   = b["sent_at"][:16].replace("T", " ")
            lines.append(
                f"{i}. «{b['text'][:80]}»\n"
                f"   📅 {dt}  👥 {n} фанов  💬 {r} ответов ({rate})  💰 ${b['revenue']:.0f}\n"
            )

        result_text = "\n".join(lines)
        await msg.edit_text(result_text)
        await send_broadcasts_message(context.bot, result_text)
    except Exception as e:
        logger.exception("cmd_broadcasts failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── BUTTON HANDLER ───────────────────────────────────────────────────────────

async def handle_get_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()

    fan_id   = query.data.split(":", 1)[1]
    fans     = load_fans()
    fan      = fans.get(fan_id, {})
    profile  = fan.get("profile", {})
    dname    = fan_display_name(fan_id, fan)
    platform = fan.get("platform", "onlyfans")
    plabel   = PLATFORM_LABELS.get(platform, platform)
    comp     = PLATFORM_COMPETITORS.get(platform, "")

    await query.message.reply_text(f"Генерирую текст для {dname}...")
    logger.info("Generating message for fan %s (%s)", fan_id, platform)

    computed = fan.get("_computed", {})

    profile_lines = []
    for key, label in [("name","Имя"),("birthday","ДР"),("job","Работа"),
                        ("personal_life","Личное"),("notes","Заметки")]:
        if profile.get(key):
            profile_lines.append(f"{label}: {profile[key]}")
    for key, label in [("hobbies","Хобби"),("reactions","Реагирует на"),("likes","Любит")]:
        if profile.get(key):
            profile_lines.append(f"{label}: {', '.join(profile[key])}")
    profile_text = "\n".join(profile_lines) or "Досье пустое"

    ctx_parts = []
    if computed.get("days_since_purchase"):
        ctx_parts.append(f"hasn't purchased in {computed['days_since_purchase']} days")
    if computed.get("days_silent"):
        ctx_parts.append(f"silent for {computed['days_silent']} days")
    dtb = computed.get("days_to_birthday")
    if dtb is not None and dtb <= 3:
        ctx_parts.append(f"birthday in {dtb} days")
    situation = ", ".join(ctx_parts) or "no special signals"

    no_comp = f" Never mention {comp} or any competing platform." if comp else ""

    prompt = (
        f"{_style_prefix()}"
        f"You are a chatter working for a creator on {plabel}.{no_comp}\n"
        f"Write a personal message to a fan on behalf of the creator.\n\n"
        f"Fan profile:\n{profile_text}\n\n"
        f"Situation: {situation}\n\n"
        "Requirements: 2-4 sentences, warm and personal, "
        "hint at exclusive content or personal connection, no direct money talk."
    )

    try:
        text = await asyncio.to_thread(claude_client.chat, prompt)
        await query.message.reply_text(
            f"💬 Текст для {dname} [{plabel}]:\n\n{text}"
        )
        logger.info("Message generated for fan %s", fan_id)
    except Exception as e:
        logger.exception("Text generation failed for fan %s", fan_id)
        await query.message.reply_text(f"Ошибка генерации: {e}")


# ─── COMMANDS ─────────────────────────────────────────────────────────────────

async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show raw API response from GET /api/v0/accounts."""
    try:
        raw = await asyncio.to_thread(om_client._get, "/api/v0/accounts")
        logger.info("GET /api/v0/accounts raw: %s", raw)
        raw_text = json.dumps(raw, ensure_ascii=False, indent=2)
        chunk = _html.escape(raw_text[:3800])
        await update.message.reply_text(
            f"<b>Raw /api/v0/accounts:</b>\n<pre>{chunk}</pre>",
            parse_mode="HTML",
        )
        accounts = get_all_accounts()
        lines = [f"<b>Parsed ({len(accounts)} аккаунтов):</b>"]
        for a in accounts:
            lines.append(f"  id=<code>{a['id']}</code>  platform=<code>{a['platform']}</code>  name={a.get('name','—')}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_accounts failed")
        await update.message.reply_text(f"Ошибка: {e}")


async def cmd_faninfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show raw API data for a fan: stored entry + from_user from messages + probe endpoints."""
    if not context.args:
        await update.message.reply_text("Использование: /faninfo <fan_id>")
        return

    fan_id = context.args[0]

    # 1. What we have stored
    fans      = load_fans()
    fan_entry = fans.get(fan_id, {})
    stored    = _html.escape(json.dumps(fan_entry, ensure_ascii=False, indent=2)[:1500])
    await update.message.reply_text(
        f"<b>fans_data.json [{fan_id}]:</b>\n<pre>{stored}</pre>",
        parse_mode="HTML",
    )

    accounts = await asyncio.to_thread(get_all_accounts)
    if not accounts:
        await update.message.reply_text("Аккаунты не найдены.")
        return

    for acct in accounts:
        acc_id   = acct["id"]
        platform = acct["platform"]
        hdr      = f"acc {acc_id} ({platform})"

        # 2. Probe fan-detail endpoints
        for path in [
            f"/api/v0/accounts/{acc_id}/fans/{fan_id}",
            f"/api/v0/accounts/{acc_id}/chats/{fan_id}",
        ]:
            try:
                data = await asyncio.to_thread(om_client._get, path)
                txt  = _html.escape(json.dumps(data, ensure_ascii=False, indent=2)[:800])
                await update.message.reply_text(
                    f"<b>GET {path}:</b>\n<pre>{txt}</pre>", parse_mode="HTML"
                )
            except Exception as e:
                await update.message.reply_text(f"GET {path} → {e}")

        # 3. First message — full structure to see from_user shape
        try:
            msgs = await asyncio.to_thread(om_client.get_messages, acc_id, fan_id, 5)
            if msgs:
                txt = _html.escape(json.dumps(msgs[0], ensure_ascii=False, indent=2)[:1000])
                await update.message.reply_text(
                    f"<b>messages[0] ({hdr}):</b>\n<pre>{txt}</pre>", parse_mode="HTML"
                )
                fu = msgs[0].get("from_user")
                logger.info("faninfo %s acc=%s from_user=%s", fan_id, acc_id, fu)

                identities = {}
                for m in msgs:
                    fu2 = m.get("from_user")
                    if isinstance(fu2, dict):
                        for k, v in fu2.items():
                            identities.setdefault(k, v)
                id_txt = _html.escape(json.dumps(identities, ensure_ascii=False, indent=2))
                await update.message.reply_text(
                    f"<b>from_user merged fields ({hdr}):</b>\n<pre>{id_txt}</pre>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(f"Сообщений нет ({hdr})")
        except Exception as e:
            await update.message.reply_text(f"messages ({hdr}) → {e}")


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /block <fan_id> [reason]  — blacklist a fan from all reports and analysis."""
    if not context.args:
        await update.message.reply_text("Использование: /block <fan_id> [причина]")
        return

    fan_id = context.args[0]
    reason = " ".join(context.args[1:]).strip() or "нет причины"

    fans = load_fans()
    old  = fans.get(fan_id, {})
    # Keep only identity fields, wipe everything else
    fans[fan_id] = {
        "id":          fan_id,
        "username":    old.get("username", ""),
        "custom_name": old.get("custom_name", ""),
        "blocked":     True,
        "block_reason": reason,
        "blocked_at":  datetime.now().isoformat(),
    }
    save_fans(fans)

    dname = fans[fan_id].get("custom_name") or fans[fan_id].get("username") or fan_id
    logger.info("cmd_block: fan %s blocked — %s", fan_id, reason)
    await update.message.reply_text(
        f"🚫 Фан <b>{dname}</b> (<code>{fan_id}</code>) заблокирован.\n"
        f"Причина: {reason}\n"
        "Досье удалено. Фан исключён из всех отчётов и анализа.",
        parse_mode="HTML",
    )


async def cmd_reanalyze_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch all transactions, find 600$+ fans, deep-analyze full message history."""
    msg = await update.message.reply_text("⏳ Получаю транзакции для поиска VIP фанов (600$+)...")

    accounts = await asyncio.to_thread(get_all_accounts)
    if not accounts:
        await msg.edit_text("❌ Аккаунты не найдены.")
        return

    # acc_id → platform_account_id for transactions API
    plat_acc_map = {a["id"]: a.get("platform_account_id", "") for a in accounts}

    # Collect payment totals using platform_account_id
    payment_totals: dict[str, float] = {}
    for acct in accounts:
        acc_id   = acct["id"]
        plat_acc = acct.get("platform_account_id", "")
        if not plat_acc:
            await update.message.reply_text(
                f"⚠️ Аккаунт {acc_id}: platform_account_id не найден. "
                "Используйте /accounts для диагностики."
            )
            continue
        try:
            txns = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_all_transactions_paged, plat_acc), timeout=120
            )
            for txn in txns:
                fid = str((txn.get("fan") or {}).get("id") or "")
                if not fid:
                    continue
                amount = float(txn.get("amount") or txn.get("price") or txn.get("net_amount") or 0)
                if amount > 0:
                    payment_totals[fid] = payment_totals.get(fid, 0.0) + amount
        except asyncio.TimeoutError:
            await update.message.reply_text(f"⚠️ Таймаут транзакций (plat_acc={plat_acc})")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Транзакции plat_acc={plat_acc}: {e}")

    vip_fans = sorted(
        [(fid, total) for fid, total in payment_totals.items() if total >= 600],
        key=lambda x: x[1], reverse=True,
    )

    if not vip_fans:
        await msg.edit_text(
            "Нет фанов с оплатой 600$+.\n"
            "Возможно API транзакций возвращает другую структуру — используйте /faninfo для диагностики."
        )
        return

    fans_db = load_fans()
    await msg.edit_text(
        f"💎 Найдено {len(vip_fans)} VIP фанов (600$+).\n"
        f"Запускаю глубокий анализ полной переписки..."
    )

    done = failed = 0
    for fan_id, total in vip_fans:
        fan_entry = fans_db.get(fan_id, {})
        if fan_entry.get("blocked"):
            continue

        acc_id   = fan_entry.get("account_id") or accounts[0]["id"]
        platform = fan_entry.get("platform", "onlyfans")
        dname    = fan_display_name(fan_id, fan_entry)

        await update.message.reply_text(
            f"🔍 Анализирую {dname} — ${total:.0f} за всё время..."
        )
        try:
            all_messages = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_all_messages_paged, acc_id, fan_id), timeout=120
            )
            if not all_messages:
                await update.message.reply_text(f"⚠️ {dname} — сообщений не найдено.")
                failed += 1
                continue

            lines: list[str] = []
            fan_dates: list[datetime] = []
            for m in all_messages:
                is_fan = not m.get("is_sent_by_me")
                text   = _TAG.sub("", m.get("text") or "").strip()
                if text:
                    lines.append(f"{'Fan' if is_fan else 'Model'}: {text}")
                dt = parse_dt(m.get("created_at"))
                if is_fan and dt:
                    fan_dates.append(dt)

            if not lines:
                failed += 1
                continue

            prompt   = DEEP_ANALYZE_PROMPT.format(
                msg_count    = len(lines),
                conversation = "\n".join(lines),
            )
            raw_json = await asyncio.to_thread(claude_client.chat, prompt)
            profile  = json.loads(clean_json(raw_json))

            fans_db = load_fans()
            fans_db.setdefault(fan_id, {"id": fan_id})
            fans_db[fan_id].update({"account_id": acc_id, "platform": platform})
            fans_db[fan_id]["profile"]       = profile
            fans_db[fan_id]["last_analyzed"] = datetime.now().isoformat()
            fans_db[fan_id]["payment_total"] = total
            if fan_dates:
                fans_db[fan_id]["last_message_date"] = max(fan_dates).isoformat()
            _extract_fan_identity(fan_id, all_messages, fans_db)
            save_fans(fans_db)

            fans_db = load_fans()
            card = format_profile_card(fan_id, fans_db.get(fan_id, {}), profile, platform)
            await update.message.reply_text(card, parse_mode="HTML")
            done += 1

        except asyncio.TimeoutError:
            await update.message.reply_text(f"⚠️ {dname} — таймаут загрузки сообщений.")
            failed += 1
        except Exception as e:
            logger.exception("reanalyze_top: fan %s failed", fan_id)
            await update.message.reply_text(f"❌ {dname}: {e}")
            failed += 1

        await asyncio.sleep(ANALYZE_PAUSE_SEC)

    await msg.edit_text(
        f"✅ Анализ VIP фанов завершён!\n"
        f"💎 Обработано: {done} | ❌ Ошибок: {failed}"
    )


async def sync_payments_for_all() -> dict:
    """
    Fetch all transactions once per account, compute rich payment stats
    (total_spent, spent_this_week, purchase_dates, purchase_interval_avg,
    subscription_expires, last_purchase_date) for every fan in fans_data.json.
    Updates the cache so fetch_fan_payments calls benefit too.
    Returns a summary dict.
    """
    accounts = await asyncio.to_thread(get_all_accounts)
    if not accounts:
        return {"error": "no accounts"}

    now = datetime.now()
    all_txns: list[dict] = []

    for acct in accounts:
        plat_acc = acct.get("platform_account_id", "")
        if not plat_acc:
            continue
        try:
            txns = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_all_transactions_paged, plat_acc),
                timeout=120,
            )
            _txn_cache[plat_acc] = (txns, now)  # warm the cache
            all_txns.extend(txns)
            logger.info("sync_payments: fetched %d txns from plat_acc=%s", len(txns), plat_acc)
        except Exception as e:
            logger.error("sync_payments: failed to fetch %s: %s", plat_acc, e)

    if not all_txns:
        return {"error": "no transactions fetched"}

    fans    = load_fans()
    updated = 0
    for fan_id in fans:
        stats = _compute_fan_payment_stats(fan_id, all_txns, now)
        if stats:
            fans[fan_id].update(stats)
            updated += 1

    save_fans(fans)
    logger.info("sync_payments_for_all: updated %d fans from %d txns", updated, len(all_txns))
    return {"updated": updated, "total_txns": len(all_txns)}


async def _scheduled_sync_payments(app: Application) -> None:
    logger.info("Nightly payment sync started")
    try:
        result = await asyncio.wait_for(sync_payments_for_all(), timeout=180)
        logger.info("Nightly payment sync done: %s", result)
    except Exception:
        logger.exception("Nightly payment sync failed")


async def cmd_sync_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch all transactions and recompute rich payment stats for every fan."""
    msg = await update.message.reply_text("⏳ Синхронизирую транзакции...")
    try:
        result = await asyncio.wait_for(sync_payments_for_all(), timeout=150)
        if "error" in result:
            await msg.edit_text(f"❌ {result['error']}")
            return
        fans = load_fans()
        top10 = sorted(
            [(fid, d.get("total_spent") or d.get("payment_total") or 0)
             for fid, d in fans.items() if d.get("total_spent") or d.get("payment_total")],
            key=lambda x: x[1], reverse=True,
        )[:10]
        lines = [
            f"✅ Синхронизация завершена!",
            f"📊 Транзакций: {result['total_txns']}  |  Фанов обновлено: {result['updated']}",
            "",
            "🏆 Топ-10 плательщиков (за всё время):",
        ]
        for fid, total in top10:
            d     = fans[fid]
            dname = fan_display_name(fid, d)
            week  = d.get("spent_this_week") or d.get("payment_week") or 0
            week_str = f"  (+${week:.0f} нед)" if week > 0 else ""
            lines.append(f"  {_html.escape(_clean(dname))}: ${total:.0f}{week_str}")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")
    except asyncio.TimeoutError:
        await msg.edit_text("❌ Таймаут (>150 сек)")
    except Exception as e:
        logger.exception("cmd_sync_payments failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_recalc_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch all transactions ONCE, then update payment_total/payment_week for every fan in DB."""
    msg = await update.message.reply_text("⏳ Загружаю все транзакции аккаунта...")

    accounts = await asyncio.to_thread(get_all_accounts)
    if not accounts:
        await msg.edit_text("❌ Аккаунты не найдены.")
        return

    # Build per-fan totals from all accounts
    # fan_id → (total_all_time, total_last_7_days)
    totals_map: dict[str, tuple[float, float]] = {}
    week_ago = datetime.now() - timedelta(days=7)

    for acct in accounts:
        plat_acc = acct.get("platform_account_id", "")
        if not plat_acc:
            continue
        try:
            txns = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_all_transactions_paged, plat_acc),
                timeout=120,
            )
            # Invalidate cache so fetch_fan_payments will also see fresh data
            _txn_cache[plat_acc] = (txns, datetime.now())
        except asyncio.TimeoutError:
            await update.message.reply_text(f"⚠️ Таймаут транзакций (plat_acc={plat_acc})")
            continue
        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка транзакций: {e}")
            continue

        await msg.edit_text(f"⏳ Считаю суммы по фанам ({len(txns)} транзакций)...")

        for txn in txns:
            fid = str((txn.get("fan") or {}).get("id") or "")
            if not fid:
                continue
            amount = float(txn.get("amount") or txn.get("price") or txn.get("net_amount") or 0)
            if amount <= 0:
                continue
            prev_total, prev_week = totals_map.get(fid, (0.0, 0.0))
            raw_dt = txn.get("timestamp") or txn.get("created_at") or txn.get("date")
            dt = parse_dt(str(raw_dt)) if raw_dt else None
            week_amount = amount if (dt and dt >= week_ago) else 0.0
            totals_map[fid] = (prev_total + amount, prev_week + week_amount)

    if not totals_map:
        await msg.edit_text("❌ Транзакции не получены или пусты.")
        return

    # Update fans_data.json
    fans = load_fans()
    updated = 0
    for fan_id, (total, week) in totals_map.items():
        if fan_id in fans:
            fans[fan_id]["payment_total"] = total
            fans[fan_id]["payment_week"]  = week
            updated += 1
        # Fans in transactions but not yet in DB are skipped — no profile to update

    save_fans(fans)

    # Build summary of top payers
    top10 = sorted(
        [(fid, t, w) for fid, (t, w) in totals_map.items() if fid in fans],
        key=lambda x: x[1], reverse=True,
    )[:10]

    lines = [
        f"✅ Пересчёт завершён!",
        f"",
        f"📊 Транзакций в базе: {sum(1 for _ in totals_map)}",
        f"👥 Обновлено фанов в DB: {updated}",
        f"",
        f"🏆 Топ-10 плательщиков (за всё время):",
    ]
    for fid, total, week in top10:
        fan_data = fans.get(fid, {})
        dname    = fan_display_name(fid, fan_data)
        week_str = f"  +${week:.0f} за неделю" if week > 0 else ""
        lines.append(f"  {dname}: ${total:.0f}{week_str}")

    await msg.edit_text("\n".join(lines))
    logger.info("recalc_payments: updated %d fans from %d unique payers", updated, len(totals_map))


async def cmd_analyze_paying(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Find all fans who paid in last 30 days and analyze those without a profile."""
    msg = await update.message.reply_text("⏳ Получаю транзакции за последние 30 дней...")

    accounts = await asyncio.to_thread(get_all_accounts)
    if not accounts:
        await msg.edit_text("❌ Аккаунты не найдены.")
        return

    plat_acc_map = {a["id"]: a.get("platform_account_id", "") for a in accounts}

    # fan_id → (total_paid, acc_id, platform)
    fan_payments: dict[str, tuple[float, str, str]] = {}

    for acct in accounts:
        acc_id   = acct["id"]
        platform = acct["platform"]
        plat_acc = acct.get("platform_account_id", "")
        if not plat_acc:
            continue
        try:
            txns = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_all_transactions_paged, plat_acc, 30),
                timeout=90,
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(f"⚠️ Таймаут транзакций (plat_acc={plat_acc})")
            continue
        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка транзакций: {e}")
            continue

        for txn in txns:
            fid    = str((txn.get("fan") or {}).get("id") or "")
            amount = float(txn.get("amount") or 0)
            if not fid or amount <= 0:
                continue
            prev_total, _, _ = fan_payments.get(fid, (0.0, acc_id, platform))
            fan_payments[fid] = (prev_total + amount, acc_id, platform)

    if not fan_payments:
        await msg.edit_text("❌ Нет платящих фанов за последние 30 дней.")
        return

    total_month = sum(t for t, _, _ in fan_payments.values())

    fans_db = load_fans()
    # Sort by payment desc, skip those with existing profile or blocked
    to_analyze: list[tuple[str, float, str, str]] = [
        (fid, total, acc_id, platform)
        for fid, (total, acc_id, platform) in sorted(
            fan_payments.items(), key=lambda x: x[1][0], reverse=True
        )
        if not fans_db.get(fid, {}).get("profile") and not fans_db.get(fid, {}).get("blocked")
    ]

    already_have = len(fan_payments) - len(to_analyze)

    if not to_analyze:
        await msg.edit_text(
            f"📊 За 30 дней платили: {len(fan_payments)} фанов  (${total_month:.0f} итого)\n"
            f"✅ У всех уже есть досье — нечего анализировать."
        )
        return

    await msg.edit_text(
        f"📊 За 30 дней платили: {len(fan_payments)} фанов  (${total_month:.0f} итого)\n"
        f"✅ С досье: {already_have} — пропускаем\n"
        f"🆕 Без досье: {len(to_analyze)} — начинаю анализ..."
    )

    created = 0
    failed  = 0

    for i, (fan_id, total, acc_id, platform) in enumerate(to_analyze, 1):
        fans_db  = load_fans()
        fan_data = fans_db.get(fan_id, {})
        dname    = fan_display_name(fan_id, fan_data)
        plat_acc = plat_acc_map.get(acc_id, "")

        await msg.edit_text(
            f"🔍 Анализирую фана {i}/{len(to_analyze)}: {dname}  (${total:.0f} за месяц)\n"
            f"🆕 Создано: {created}  ❌ Ошибок: {failed}"
        )

        profile = await analyze_fan(acc_id, fan_id, platform, plat_acc)
        if profile:
            created += 1
            fans_db = load_fans()
            card = format_profile_card(fan_id, fans_db.get(fan_id, {}), profile, platform)
            await update.message.reply_text(card, parse_mode="HTML")
        else:
            failed += 1

        if i < len(to_analyze):
            await asyncio.sleep(ANALYZE_PAUSE_SEC)

    await msg.edit_text(
        f"✅ Анализ платящих фанов завершён!\n\n"
        f"💰 Итого за 30 дней: ${total_month:.0f}\n"
        f"👥 Платили: {len(fan_payments)} фанов\n"
        f"🆕 Создано новых досье: {created}\n"
        f"✅ Уже было досье: {already_have}\n"
        f"❌ Ошибок: {failed}"
    )


async def cmd_transaction_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch last 100 transactions and show all unique type values."""
    msg = await update.message.reply_text("⏳ Запрашиваю транзакции...")
    try:
        accounts = await asyncio.to_thread(get_all_accounts)
        type_counts: dict[str, int] = {}
        type_examples: dict[str, float] = {}

        for acct in accounts:
            plat_acc = acct.get("platform_account_id", "")
            if not plat_acc:
                continue
            resp = await asyncio.wait_for(
                asyncio.to_thread(om_client.get_transactions, plat_acc, limit=100),
                timeout=30,
            )
            txns = resp.get("items") or resp.get("transactions") or resp.get("data") or []
            for txn in txns:
                t = str(txn.get("type") or "(пусто)")
                amt = float(txn.get("amount") or 0)
                type_counts[t] = type_counts.get(t, 0) + 1
                if t not in type_examples:
                    type_examples[t] = amt

        if not type_counts:
            await msg.edit_text("❌ Транзакций не найдено")
            return

        lines = ["<b>Уникальные типы транзакций (последние 100):</b>\n"]
        for t, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"• <code>{_html.escape(t)}</code> — {cnt} шт, пример: ${type_examples[t]:.2f}")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.exception("cmd_transaction_types failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_transactions_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dump raw transactions for a platform_account_id and show top-5 payers."""
    plat_acc = context.args[0] if context.args else "435481099"
    msg = await update.message.reply_text(f"⏳ Запрашиваю транзакции (plat_acc={plat_acc}, limit=10)...")

    try:
        # First batch — show raw structure
        resp = await asyncio.wait_for(
            asyncio.to_thread(om_client.get_transactions, plat_acc, None, 10),
            timeout=30,
        )
        raw = _html.escape(json.dumps(resp, ensure_ascii=False, indent=2)[:3500])
        await update.message.reply_text(
            f"<b>Raw response (limit=10):</b>\n<pre>{raw}</pre>",
            parse_mode="HTML",
        )

        items = resp.get("items") or resp.get("transactions") or resp.get("data") or []
        if items:
            first = _html.escape(json.dumps(items[0], ensure_ascii=False, indent=2)[:2000])
            await update.message.reply_text(
                f"<b>Первая транзакция — все поля:</b>\n<pre>{first}</pre>",
                parse_mode="HTML",
            )

        # Load all pages and compute totals
        await msg.edit_text("⏳ Загружаю все страницы транзакций...")
        all_txns = await asyncio.wait_for(
            asyncio.to_thread(om_client.get_all_transactions_paged, plat_acc),
            timeout=120,
        )

        totals: dict[str, float] = {}
        no_fid = 0
        for txn in all_txns:
            fid = str((txn.get("fan") or {}).get("id") or "")
            if not fid:
                no_fid += 1
                continue
            amount = float(txn.get("amount") or txn.get("price") or txn.get("net_amount") or 0)
            if amount > 0:
                totals[fid] = totals.get(fid, 0.0) + amount

        top5 = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:5]
        vip_count = sum(1 for _, t in totals.items() if t >= 600)

        lines = [
            f"📊 Всего транзакций: {len(all_txns)}",
            f"👥 Уникальных fan_id: {len(totals)}",
            f"❓ Без fan_id: {no_fid}",
            f"💎 Фанов с 600$+: {vip_count}",
            "",
            "🏆 Топ-5 плательщиков:",
        ]
        for fid, total in top5:
            lines.append(f"  <code>{fid}</code>: ${total:.2f}")

        await msg.edit_text("\n".join(lines), parse_mode="HTML")

    except asyncio.TimeoutError:
        await msg.edit_text("❌ Таймаут при запросе транзакций.")
    except Exception as e:
        logger.exception("cmd_transactions_debug failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /nick <fan_id> <custom name>"""
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /nick <fan_id> <ник>\nПример: /nick 85391040 Майк")
        return

    fan_id      = context.args[0]
    custom_name = " ".join(context.args[1:]).strip()

    fans = load_fans()
    fans.setdefault(fan_id, {"id": fan_id})
    fans[fan_id]["custom_name"] = custom_name
    save_fans(fans)

    logger.info("cmd_nick: fan %s → custom_name=%s", fan_id, custom_name)
    await update.message.reply_text(f"✅ Фан {fan_id} теперь называется <b>{custom_name}</b>", parse_mode="HTML")


# ─── WEBHOOK / ALERTS ─────────────────────────────────────────────────────────

# fan_id → datetime of last unanswered fan message
_unanswered: dict[str, datetime] = {}
# fan_id → already alerted for this unanswered stretch (cleared on reply)
_alerted_unanswered: set[str] = set()

_wh_events_total: int = 0   # events received since start

_HOT_WORDS = frozenset({
    "how much", "price", "cost", "buy", "want", "purchase",
    "how can i", "can i get",
})


def _wh_verify(raw_body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    msg = f"{timestamp}.{raw_body.decode('utf-8', errors='replace')}"
    expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _wh_extract(payload: dict) -> tuple[str, str, dict]:
    """Return (fan_id, account_id, message_dict) from any webhook shape."""
    data    = payload.get("data") or payload
    fan_id  = str(
        data.get("fan_id") or
        (data.get("fan") or {}).get("id") or
        payload.get("fan_id") or ""
    )
    acc_id  = str(
        data.get("account_id") or payload.get("account_id") or ""
    )
    msg     = data.get("message") or data
    return fan_id, acc_id, msg


async def _alert_level2_vip(bot, fan_id: str, fan_data: dict, acc_id: str, text: str) -> None:
    profile = fan_data.get("profile", {})
    dname   = _clean(fan_display_name(fan_id, fan_data))
    total   = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)

    try:
        msgs = await asyncio.wait_for(
            asyncio.to_thread(om_client.get_messages, acc_id, fan_id, 10),
            timeout=15,
        )
    except Exception:
        msgs = []

    conv = "\n".join(
        f"{'Фан' if not m.get('is_sent_by_me') else 'Модель'}: "
        f"{_clean(_TAG.sub('', str(m.get('text') or ''))).strip()[:120]}"
        for m in msgs if (m.get("text") or "").strip()
    ) or "нет диалога"

    style = _style_prefix()
    prompt = (
        f"{style}"
        f"VIP фан написал (потратил ${total:.0f}).\n"
        f"Фетиши: {', '.join(str(f) for f in profile.get('fetishes', [])) or 'неизвестно'}\n"
        f"Стиль: {profile.get('chat_style') or 'нет данных'}\n\n"
        f"Последние сообщения:\n{conv}\n\n"
        f"Его сообщение: {text or '(без текста)'}\n\n"
        "Дай 1-2 предложения на русском: что предложить этому VIP прямо сейчас? "
        "Если есть style guide — подскажи конкретную фразу в стиле модели."
    )
    try:
        advice = await asyncio.to_thread(claude_client.chat, prompt)
        await send_alerts_message(
            bot,
            f"⚡️ VIP написал — {dname}\n"
            f"💰 ${total:.0f} за всё время • 🔥 {_stars(profile.get('warmth'))} • "
            f"👁 {_stars(profile.get('engagement'))}\n"
            f"🤖 Совет: {advice}\n"
            f"👤 /fan {fan_id}",
        )
    except Exception:
        logger.exception("_alert_level2_vip Claude failed fan=%s", fan_id)


async def _alert_level2_purchase(bot, fan_id: str, fan_data: dict, amount: float) -> None:
    profile = fan_data.get("profile", {})
    dname   = _clean(fan_display_name(fan_id, fan_data))
    total   = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)

    style = _style_prefix()
    prompt = (
        f"{style}"
        f"Фан только что купил контент на ${amount:.0f} (итого потратил ${total:.0f}).\n"
        f"Фетиши: {', '.join(str(f) for f in profile.get('fetishes', [])) or 'неизвестно'}\n"
        f"Стиль: {profile.get('chat_style') or 'нет данных'}\n\n"
        "Дай 1 предложение на русском: что предложить следующим шагом? "
        "Если есть style guide — используй конкретную фразу в стиле модели."
    )
    try:
        advice = await asyncio.to_thread(claude_client.chat, prompt)
        await send_alerts_message(
            bot,
            f"💸 Купил! — {dname}\n"
            f"💰 Покупка: ${amount:.0f} • Всего: ${total:.0f}\n"
            f"🤖 Следующий шаг: {advice}\n"
            f"👤 /fan {fan_id}",
        )
    except Exception:
        logger.exception("_alert_level2_purchase Claude failed fan=%s", fan_id)


async def _alert_level2_hot_words(bot, fan_id: str, fan_data: dict, text: str) -> None:
    profile = fan_data.get("profile", {})
    dname   = _clean(fan_display_name(fan_id, fan_data))

    style = _style_prefix()
    prompt = (
        f"{style}"
        f"Фан написал: \"{text}\"\n"
        f"Фетиши: {', '.join(str(f) for f in profile.get('fetishes', [])) or 'неизвестно'}\n"
        f"Стиль: {profile.get('chat_style') or 'нет данных'}\n\n"
        "Дай 1 предложение на русском: что конкретно предложить и по какой цене? "
        "Если есть style guide — приведи пример фразы в стиле модели."
    )
    try:
        advice = await asyncio.to_thread(claude_client.chat, prompt)
        await send_alerts_message(
            bot,
            f"🎯 Готов покупать — {dname}\n"
            f"💬 Написал: \"{_clean(text[:120])}\"\n"
            f"🤖 Предложи: {advice}\n"
            f"👤 /fan {fan_id}",
        )
    except Exception:
        logger.exception("_alert_level2_hot_words Claude failed fan=%s", fan_id)


async def _process_webhook(app: "Application", payload: dict) -> None:
    try:
        event          = str(payload.get("event") or payload.get("type") or "")
        fan_id, acc_id, msg = _wh_extract(payload)

        logger.info("Webhook event=%r fan=%s acc=%s", event, fan_id, acc_id)

        is_purchase_event = event in ("transaction", "purchase", "chat.purchase")
        is_message_event  = not is_purchase_event  # treat anything else as potential message

        fans     = load_fans()
        fan_data = fans.get(fan_id, {})

        # ── Purchase event ──────────────────────────────────────────────────
        if is_purchase_event:
            amount = float(
                payload.get("amount") or msg.get("price") or msg.get("amount") or 0
            )
            if amount > 0 and fan_id:
                asyncio.create_task(_alert_level2_purchase(app.bot, fan_id, fan_data, amount))
            return

        # ── Message event ───────────────────────────────────────────────────
        if not fan_id:
            return

        is_from_fan = not bool(msg.get("is_sent_by_me", False))
        text        = _clean(_TAG.sub("", str(msg.get("text") or ""))).strip()
        price       = float(msg.get("price") or 0)
        is_free     = bool(msg.get("is_free", True))
        now         = datetime.now()

        if is_from_fan:
            # Auto-restore from stop-list if fan writes on their own
            if fan_data.get("auto_stopped"):
                last_dt     = parse_dt(fan_data.get("last_message_date"))
                days_silent = (now - last_dt).days if last_dt else 0
                total       = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
                dname_alert = _clean(fan_display_name(fan_id, fan_data))
                fans[fan_id].pop("auto_stopped",        None)
                fans[fan_id].pop("auto_stopped_at",     None)
                fans[fan_id].pop("auto_stopped_streak", None)
                save_fans(fans)
                fan_data = fans[fan_id]
                logger.info("Stoplist: fan %s returned by own message, removed auto_stopped", fan_id)
                await send_alerts_message(
                    app.bot,
                    f"🔄 Вернулся из стоп-листа — {dname_alert}\n"
                    f"💰 ${total:.0f} за всё время • Молчал {days_silent} дн\n"
                    f"👤 /profile {fan_id}",
                )

            # Level 1 — new fan
            if not fan_data:
                await send_alerts_message(
                    app.bot,
                    f"🆕 Новый фан написал!\nfan_id: {fan_id}\n👤 /analyze {fan_id}",
                )
                logger.info("Alert: new fan %s", fan_id)

            # Level 1 — returned after 5+ days
            last_msg_dt = parse_dt(fan_data.get("last_message_date"))
            if last_msg_dt:
                days_silent = (now - last_msg_dt).days
                if days_silent >= 5:
                    dname = _clean(fan_display_name(fan_id, fan_data))
                    total = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
                    await send_alerts_message(
                        app.bot,
                        f"🔄 Вернулся после {days_silent} дн — {dname}\n"
                        f"💰 ${total:.0f} за всё время\n"
                        f"👤 /fan {fan_id}",
                    )
                    logger.info("Alert: fan %s returned after %d days", fan_id, days_silent)

            # Track unanswered
            _unanswered[fan_id] = now
            _alerted_unanswered.discard(fan_id)

            # Level 2 — VIP
            total = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
            if total >= 300 and fan_data.get("profile"):
                asyncio.create_task(_alert_level2_vip(app.bot, fan_id, fan_data, acc_id, text))

            # Level 2 — hot words
            if text and any(hw in text.lower() for hw in _HOT_WORDS):
                asyncio.create_task(_alert_level2_hot_words(app.bot, fan_id, fan_data, text))

            # Level 2 — paid message received (fan bought PPV)
            if price > 0 and not is_free:
                asyncio.create_task(_alert_level2_purchase(app.bot, fan_id, fan_data, price))

            # Update last_message_date
            fan_data["last_message_date"] = now.isoformat()
            fans[fan_id] = fan_data
            save_fans(fans)

        else:
            # Model replied → clear unanswered state
            _unanswered.pop(fan_id, None)
            _alerted_unanswered.discard(fan_id)

    except Exception:
        logger.exception("_process_webhook error, payload: %s", payload)


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(status=200, text="healthy")


async def webhook_handler(request: web.Request) -> web.Response:
    raw = await request.read()
    secret = os.getenv("WEBHOOK_SECRET", "")
    if secret and secret != "СЮДА_ВСТАВИШЬ_СЕКРЕТ":
        sig = request.headers.get("x-om-webhook-signature", "")
        ts  = request.headers.get("x-om-webhook-timestamp", "")
        if not sig or not _wh_verify(raw, ts, sig, secret):
            logger.warning("Webhook: bad signature from %s", request.remote)
            return web.Response(status=401, text="Unauthorized")

    try:
        payload = json.loads(raw)
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    global _wh_events_total
    _wh_events_total += 1

    app = request.app["tg_app"]
    asyncio.create_task(_process_webhook(app, payload))
    return web.Response(status=200, text="OK")


async def _poll_unanswered_loop(app: "Application") -> None:
    """Every 30 min: alert if a fan's message has gone unanswered for 45+ minutes."""
    while True:
        await asyncio.sleep(30 * 60)
        now  = datetime.now()
        fans = load_fans()
        for fan_id, since in list(_unanswered.items()):
            waited_min = (now - since).total_seconds() / 60
            if waited_min >= 45 and fan_id not in _alerted_unanswered:
                _alerted_unanswered.add(fan_id)
                fan_data = fans.get(fan_id, {})
                dname    = _clean(fan_display_name(fan_id, fan_data))
                total    = float(fan_data.get("total_spent") or fan_data.get("payment_total") or 0)
                await send_alerts_message(
                    app.bot,
                    f"⏰ Ждёт ответа {int(waited_min)} мин\n"
                    f"{dname} • 💰 ${total:.0f} за всё время\n"
                    f"👤 /fan {fan_id}",
                )
                logger.info("Alert: fan %s unanswered %.0f min", fan_id, waited_min)


# ─── CHATTERS ──────────────────────────────────────────────────────────────────

CHATTERS_FILE = Path(__file__).parent / "chatters_data.json"

_DEFAULT_CHATTERS: list[dict] = [
    {"name": "gofyaa gg",  "start_hour": 17, "end_hour": 1,  "emoji": "🌙", "user_id": 168106},
    {"name": "АяНами рей", "start_hour": 1,  "end_hour": 17, "emoji": "🌅", "user_id": 46038},
]

_DEFAULTS_BY_NAME = {ch["name"].lower(): ch for ch in _DEFAULT_CHATTERS}


def load_chatters() -> list[dict]:
    if not CHATTERS_FILE.exists():
        save_chatters(_DEFAULT_CHATTERS)
        return list(_DEFAULT_CHATTERS)
    with open(CHATTERS_FILE, "r", encoding="utf-8") as f:
        chatters = json.load(f).get("chatters", _DEFAULT_CHATTERS)
    # Backfill user_id if missing (migration from old format)
    changed = False
    for ch in chatters:
        if "user_id" not in ch:
            default = _DEFAULTS_BY_NAME.get(ch["name"].lower())
            if default:
                ch["user_id"] = default["user_id"]
                changed = True
    if changed:
        save_chatters(chatters)
    return chatters


def save_chatters(chatters: list[dict]) -> None:
    with open(CHATTERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"chatters": chatters}, f, ensure_ascii=False, indent=2)


def _chatter_for_hour(hour_msk: int, chatters: list[dict]) -> dict | None:
    """Return the chatter whose shift covers the given MSK hour (0-23)."""
    for ch in chatters:
        s, e = ch["start_hour"], ch["end_hour"]
        if s < e:               # normal range e.g. 01-17
            if s <= hour_msk < e:
                return ch
        else:                   # wraps midnight e.g. 17-01
            if hour_msk >= s or hour_msk < e:
                return ch
    return None


def _chatter_stars(total: float, msg_count: int) -> str:
    if total <= 0 and msg_count >= 50: n = 1
    elif total <= 0:                   n = 0
    elif total < 10:                   n = 1
    elif total < 30:                   n = 2
    elif total < 100:                  n = 3
    elif total < 300:                  n = 4
    else:                              n = 5
    return "⭐" * n + "☆" * (5 - n)


async def build_chatter_report(
    msk_start: datetime,   # naive MSK start of period
    msk_end:   datetime,   # naive MSK end of period
    chatters:  list[dict],
    accounts:  list[dict],
    label:     str,
) -> str:
    """Build chatter performance report for a period. All datetimes are naive MSK."""
    utc_start = msk_start - timedelta(hours=3)
    utc_end   = msk_end   - timedelta(hours=3)
    start_iso = utc_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso   = utc_end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Per-chatter accumulators
    stats: dict[str, dict] = {
        ch["name"]: {"sales": 0.0, "tips": 0.0, "messages": 0, "reply_secs": []}
        for ch in chatters
    }

    # ── Metrics: one call for all chatters via user_ids ───────────────────────
    user_ids = [ch["user_id"] for ch in chatters if ch.get("user_id")]
    metrics_by_uid: dict[int, dict] = {}
    if user_ids:
        try:
            metrics_list = await asyncio.wait_for(
                asyncio.to_thread(
                    om_client.get_users_metrics, user_ids, start_iso, end_iso
                ),
                timeout=20,
            )
            for m in metrics_list:
                uid = m.get("user_id") or m.get("id")
                if uid is not None:
                    metrics_by_uid[int(uid)] = m
            logger.info("build_chatter_report: metrics received for %d users", len(metrics_by_uid))
        except Exception as e:
            logger.warning("build_chatter_report: metrics failed: %s", e)

    # Populate messages/reply_time from metrics API (financial data comes from transactions)
    for ch in chatters:
        uid = ch.get("user_id")
        m   = metrics_by_uid.get(uid, {}) if uid else {}
        if m:
            msgs  = int(m.get("messages_count") or m.get("message_count") or 0)
            rtime = (m.get("reply_time_avg") or m.get("avg_reply_time") or
                     m.get("reply_time") or None)
            stats[ch["name"]]["messages"] = msgs
            if rtime is not None:
                stats[ch["name"]]["reply_secs"].append(float(rtime))

    # Financial attribution: transactions by shift time
    for acct in accounts:
        plat_acc = acct.get("platform_account_id", "")
        if not plat_acc:
            continue
        try:
            txns = await asyncio.wait_for(
                asyncio.to_thread(
                    om_client.get_all_transactions_paged, plat_acc,
                    730, start_iso, end_iso,
                ),
                timeout=90,
            )
        except Exception as e:
            logger.error("build_chatter_report txns: %s", e)
            txns = []

        for txn in txns:
            raw_ts = txn.get("timestamp") or txn.get("created_at")
            dt_utc = parse_dt(str(raw_ts)) if raw_ts else None
            if not dt_utc:
                continue
            dt_msk  = dt_utc + timedelta(hours=3)
            ch      = _chatter_for_hour(dt_msk.hour, chatters)
            if not ch:
                continue
            amount   = float(txn.get("amount") or 0)
            txn_type = str(txn.get("type") or "").lower()
            cname    = ch["name"]
            if "tip" in txn_type:
                stats[cname]["tips"]  += amount
            else:
                stats[cname]["sales"] += amount

    # ── Format ────────────────────────────────────────────────────────────────
    lines = [f"📊 {label}:\n"]
    for ch in chatters:
        cname    = ch["name"]
        emoji    = ch.get("emoji", "👤")
        s_h, e_h = ch["start_hour"], ch["end_hour"]
        st       = stats[cname]
        total    = st["sales"] + st["tips"]
        msgs     = st["messages"]
        rating   = _chatter_stars(total, msgs)

        if st["reply_secs"]:
            avg_secs  = sum(st["reply_secs"]) / len(st["reply_secs"])
            reply_str = f"{int(avg_secs // 60)} мин"
        else:
            reply_str = "N/A"

        lines.append(f"{emoji} {cname} ({s_h:02d}:00 — {e_h:02d}:00)")
        lines.append(f"💰 Продаж: ${st['sales']:.0f}")
        lines.append(f"🎁 Типов: ${st['tips']:.0f}")
        lines.append(f"💬 Сообщений: {msgs if msgs else 'N/A'}")
        lines.append(f"⚡️ Ответ: {reply_str}")
        lines.append(f"{rating} (${total:.0f} итого)")
        lines.append("")

    return "\n".join(lines).rstrip()


async def _run_chatter_report_scheduled(app: Application) -> None:
    logger.info("Scheduled chatter report started")
    try:
        now_msk   = datetime.now(TZ_MSK).replace(tzinfo=None)
        msk_end   = now_msk
        msk_start = msk_end - timedelta(hours=24)
        chatters  = load_chatters()
        accounts  = await asyncio.to_thread(get_all_accounts)
        label     = (f"Отчёт за 24ч "
                     f"({msk_start.strftime('%d.%m %H:%M')} — {msk_end.strftime('%d.%m %H:%M')} МСК)")
        text = await build_chatter_report(msk_start, msk_end, chatters, accounts, label)
        await send_chatters_message(app.bot, text)
        logger.info("Scheduled chatter report sent")
    except Exception:
        logger.exception("_run_chatter_report_scheduled failed")


async def cmd_chatter_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("⏳ Формирую отчёт за последние 24ч...")
    try:
        now_msk   = datetime.now(TZ_MSK).replace(tzinfo=None)
        msk_end   = now_msk
        msk_start = msk_end - timedelta(hours=24)
        chatters  = load_chatters()
        accounts  = await asyncio.to_thread(get_all_accounts)
        label     = (f"Отчёт за 24ч "
                     f"({msk_start.strftime('%d.%m %H:%M')} — {msk_end.strftime('%d.%m %H:%M')} МСК)")
        text = await build_chatter_report(msk_start, msk_end, chatters, accounts, label)
        await msg.edit_text(text)
    except Exception as e:
        logger.exception("cmd_chatter_report failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_chatter_report_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("⏳ Формирую отчёт за 7 дней...")
    try:
        now_msk   = datetime.now(TZ_MSK).replace(tzinfo=None)
        msk_end   = now_msk
        msk_start = msk_end - timedelta(days=7)
        chatters  = load_chatters()
        accounts  = await asyncio.to_thread(get_all_accounts)
        label = (f"Отчёт за 7 дней "
                 f"({msk_start.strftime('%d.%m %H:%M')} — {msk_end.strftime('%d.%m %H:%M')} МСК)")
        text = await build_chatter_report(msk_start, msk_end, chatters, accounts, label)
        await msg.edit_text(text)
    except Exception as e:
        logger.exception("cmd_chatter_report_week failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_chatter_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /chatter_edit <name> <HH:MM> <HH:MM>  — change shift hours
    /chatter_edit <old_name> <new_name>    — rename chatter
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "/chatter_edit <имя> <начало HH:MM> <конец HH:MM>\n"
            "/chatter_edit <старое_имя> <новое_имя>"
        )
        return

    args     = context.args
    time_re  = _re.compile(r"^\d{1,2}:\d{2}$")
    chatters = load_chatters()

    if len(args) >= 3 and time_re.match(args[-1]) and time_re.match(args[-2]):
        # Change shift hours: name ... HH:MM HH:MM
        name      = " ".join(args[:-2]).strip().strip('"\'')
        start_h   = int(args[-2].split(":")[0])
        end_h     = int(args[-1].split(":")[0])
        for ch in chatters:
            if ch["name"].lower() == name.lower():
                ch["start_hour"] = start_h
                ch["end_hour"]   = end_h
                save_chatters(chatters)
                await update.message.reply_text(
                    f"✅ Смена <b>{_html.escape(name)}</b>: "
                    f"{start_h:02d}:00 — {end_h:02d}:00",
                    parse_mode="HTML",
                )
                return
        await update.message.reply_text(f"❌ Чаттер «{name}» не найден.")
    else:
        # Rename: first token = old name, rest = new name
        old_name = args[0].strip('"\'')
        new_name = " ".join(args[1:]).strip().strip('"\'')
        for ch in chatters:
            if ch["name"].lower() == old_name.lower():
                ch["name"] = new_name
                save_chatters(chatters)
                await update.message.reply_text(
                    f"✅ <b>{_html.escape(old_name)}</b> → <b>{_html.escape(new_name)}</b>",
                    parse_mode="HTML",
                )
                return
        await update.message.reply_text(f"❌ Чаттер «{old_name}» не найден.")


async def cmd_fan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /fan <fan_id> — show stored profile card."""
    if not context.args:
        await update.message.reply_text("Использование: /fan <fan_id>")
        return

    fan_id   = context.args[0]
    fans     = load_fans()
    fan_data = fans.get(fan_id)

    if not fan_data:
        await update.message.reply_text(f"Фан {fan_id} не найден в базе.")
        return

    profile = fan_data.get("profile")
    if not profile:
        dname = fan_display_name(fan_id, fan_data)
        await update.message.reply_text(
            f"Фан {dname} есть в базе, но досье ещё не создано.\n"
            f"Запусти /analyze {fan_id}"
        )
        return

    platform = fan_data.get("platform", "onlyfans")
    card = format_profile_card(fan_id, fan_data, profile, platform)
    await update.message.reply_text(card, parse_mode="HTML")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["Бот работает ✅"]
    for name, check in [("OnlyMonster", om_client.ping), ("Claude", claude_client.ping)]:
        try:
            await asyncio.to_thread(check)
            lines.append(f"{name} ✅")
        except Exception as e:
            logger.error("%s ping failed: %s", name, e)
            lines.append(f"{name} ❌")
    await update.message.reply_text(" ".join(lines))


async def cmd_webhook_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import aiohttp as _aiohttp
    lines: list[str] = ["<b>Webhook Status</b>"]

    # 1. WEBHOOK_SECRET
    secret = os.getenv("WEBHOOK_SECRET", "")
    if secret and secret != "СЮДА_ВСТАВИШЬ_СЕКРЕТ":
        lines.append("🔑 WEBHOOK_SECRET: задан ✅")
    else:
        lines.append("🔑 WEBHOOK_SECRET: не задан ⚠️")

    # 2. HTTP connectivity + 3. test request
    wh_port = int(os.environ.get("PORT", 8080))
    url = f"http://localhost:{wh_port}/webhook"
    test_payload = json.dumps({"event": "status_check", "data": {}}).encode()
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=test_payload,
                headers={"Content-Type": "application/json"},
                timeout=_aiohttp.ClientTimeout(total=5),
            ) as resp:
                status = resp.status
        lines.append(f"🌐 HTTP сервер {wh_port}: отвечает ✅")
        if status == 200:
            lines.append(f"📨 Тест /webhook: {status} OK ✅")
        elif status == 401:
            lines.append(f"📨 Тест /webhook: {status} (секрет настроен — подпись не передана) ⚠️")
        else:
            lines.append(f"📨 Тест /webhook: {status} ⚠️")
    except _aiohttp.ClientConnectorError:
        lines.append(f"🌐 HTTP сервер {wh_port}: не отвечает ❌")
        lines.append("📨 Тест /webhook: недоступен ❌")
    except Exception as e:
        lines.append(f"🌐 HTTP сервер {wh_port}: ошибка — {e} ❌")
        lines.append("📨 Тест /webhook: недоступен ❌")

    # 4. Last 5 webhook-related log lines
    try:
        log_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        wh_lines = [l for l in log_lines if "webhook" in l.lower() or "Webhook" in l][-5:]
        if wh_lines:
            formatted = "\n".join(f"  {l}" for l in wh_lines)
            lines.append(f"\n📋 Последние строки лога (webhook):\n<pre>{_html.escape(formatted)}</pre>")
        else:
            lines.append("\n📋 Лог: webhook-строк не найдено")
    except Exception as e:
        lines.append(f"\n📋 Лог: не удалось прочитать — {e}")

    # 5. Events counter
    lines.append(f"\n📊 Webhook событий с запуска: <b>{_wh_events_total}</b>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("⏳ Получаю список аккаунтов...")

    try:
        accounts = await asyncio.wait_for(asyncio.to_thread(get_all_accounts), timeout=25)
    except asyncio.TimeoutError:
        await msg.edit_text("❌ Таймаут: API не ответил за 25 сек при получении аккаунтов.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка получения аккаунтов: {e}")
        return

    if not accounts:
        await msg.edit_text("❌ Аккаунты не найдены.")
        return

    await msg.edit_text("⏳ Получаю список фанов и анализирую активность...")

    try:
        platforms_status = await asyncio.wait_for(
            asyncio.to_thread(run_monitoring_all, accounts), timeout=90
        )
    except asyncio.TimeoutError:
        await msg.edit_text(
            "❌ Таймаут: мониторинг не завершился за 90 сек.\n"
            "Скорее всего API OnlyMonster недоступен или отвечает очень медленно."
        )
        return
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка мониторинга: {e}")
        return

    await msg.edit_text("⏳ Формирую отчёт...")

    text, keyboard = format_report(platforms_status)
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    try:
        await send_daily_message(context.application.bot, text, markup)
        await msg.edit_text("✅ Отчёт отправлен в топик!")
    except Exception as e:
        await msg.edit_text(f"❌ Не удалось отправить в топик: {e}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def run() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("fan",                  cmd_fan))
    app.add_handler(CommandHandler("chatter_report",       cmd_chatter_report))
    app.add_handler(CommandHandler("chatter_report_week",  cmd_chatter_report_week))
    app.add_handler(CommandHandler("chatter_edit",         cmd_chatter_edit))
    app.add_handler(CommandHandler("ping",                 cmd_ping))
    app.add_handler(CommandHandler("webhook_status",       cmd_webhook_status))
    app.add_handler(CommandHandler("nick",                 cmd_nick))
    app.add_handler(CommandHandler("block",                cmd_block))
    app.add_handler(CommandHandler("accounts",             cmd_accounts))
    app.add_handler(CommandHandler("faninfo",              cmd_faninfo))
    app.add_handler(CommandHandler("analyze",              cmd_analyze))
    app.add_handler(CommandHandler("analyze_all",          cmd_analyze_all))
    app.add_handler(CommandHandler("analyze_paying",       cmd_analyze_paying))
    app.add_handler(CommandHandler("sync_payments",        cmd_sync_payments))
    app.add_handler(CommandHandler("recalc_payments",      cmd_recalc_payments))
    app.add_handler(CommandHandler("reanalyze_top",        cmd_reanalyze_top))
    app.add_handler(CommandHandler("transactions_debug",   cmd_transactions_debug))
    app.add_handler(CommandHandler("transaction_types",    cmd_transaction_types))
    app.add_handler(CommandHandler("report",               cmd_report))
    app.add_handler(CommandHandler("reactivation",         cmd_reactivation))
    app.add_handler(CommandHandler("weekly",               cmd_weekly))
    app.add_handler(CommandHandler("learn_style",          cmd_learn_style))
    app.add_handler(CommandHandler("update_style",         cmd_update_style))
    app.add_handler(CommandHandler("show_style",           cmd_show_style))
    app.add_handler(CommandHandler("nightdrive",           cmd_nightdrive))
    app.add_handler(CommandHandler("stop_all",             cmd_stop_all))
    app.add_handler(CommandHandler("resume_all",           cmd_resume_all))
    app.add_handler(CommandHandler("sent_log",             cmd_sent_log))
    app.add_handler(CommandHandler("stoplist",             cmd_stoplist))
    app.add_handler(CommandHandler("ask",                  cmd_ask))
    app.add_handler(CommandHandler("broadcasts",           cmd_broadcasts))
    app.add_handler(CallbackQueryHandler(handle_get_text,  pattern=r"^get_text:"))
    app.add_handler(CallbackQueryHandler(handle_reactivate, pattern=r"^reactivate:"))
    app.add_handler(CallbackQueryHandler(handle_nd_send,   pattern=r"^nd_send:"))
    app.add_handler(CallbackQueryHandler(handle_nd_edit,   pattern=r"^nd_edit:"))
    # MessageHandler for nightdrive edit replies — catches replies to bot's prompt messages
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.REPLY,
        handle_nd_custom_text,
    ))

    logger.info(
        "Bot started — daily report %02d:00 MSK, weekly analyze Sun %02d:00 MSK",
        SCHEDULE_HOUR_MSK, SCHEDULE_HOUR_MSK,
    )

    # Start aiohttp webhook server — use $PORT if set (bothost.ru proxy), else 8080
    wh_port = int(os.environ.get("PORT", 8080))
    wh_app = web.Application()
    wh_app["tg_app"] = app
    wh_app.router.add_get("/health", health_handler)
    wh_app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(wh_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", wh_port).start()
    logger.info("Webhook server listening on 0.0.0.0:%d", wh_port)

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
        asyncio.create_task(scheduler_loop(app))
        asyncio.create_task(_poll_unanswered_loop(app))
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())

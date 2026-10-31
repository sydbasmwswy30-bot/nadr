import logging
import asyncio
from copy import deepcopy
import sqlite3
import time
import ipaddress
from urllib.parse import urlsplit, parse_qsl
import httpx
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    BotCommand,
)
try:
    from telegram import LabeledPrice
except ImportError:
    LabeledPrice = None
try:
    from telegram import CopyTextButton
except ImportError:
    CopyTextButton = None
try:
    from telegram.ext import PreCheckoutQueryHandler
except ImportError:
    PreCheckoutQueryHandler = None
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    TypeHandler,
)

# ==========================================
# SEYED PREMIUM - تنظیمات
# ==========================================

ADMIN_ID = 8762160338

def toman(value):
    """Format a toman amount without letting malformed stored prices crash a menu."""
    try:
        number = int(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return "—"
    return f"{number:,}".replace(",", "٬")


def normalize_ascii_digits(value):
    digits = "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩"
    ascii_digits = "01234567890123456789"
    return str(value).translate(str.maketrans(digits, ascii_digits))


DB_FILE = Path(__file__).with_name("shop.db")
IRAN_TZ = ZoneInfo("Asia/Tehran")


def utc_now():
    """Current real server time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_expiry(value):
    """Parse stored expiry. New values are UTC-aware; legacy naive values are treated as Iran local time."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IRAN_TZ)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def expiry_is_valid(value, now=None):
    """A coupon expires at the exact instant now >= expires_at."""
    if not value:
        return True
    expiry = parse_expiry(value)
    if expiry is None:
        return False
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) < expiry


def format_expiry_iran(value):
    if not value:
        return "بدون انقضا"
    expiry = parse_expiry(value)
    if expiry is None:
        return f"زمان نامعتبر: {value}"
    return expiry.astimezone(IRAN_TZ).strftime("%Y-%m-%d %H:%M:%S (Asia/Tehran)")


def format_coupon_expiry(value, label=None):
    """Display the selected Jalali date or duration while retaining the real stored deadline."""
    if not value:
        return "بدون انقضا"
    label = str(label or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", label):
        return f"{label.replace('-', '/')} (شمسی)"
    actual = format_expiry_iran(value)
    if label:
        return f"{label} | زمان دقیق انقضا: {actual}"
    return actual


def jalali_to_gregorian(jy, jm, jd):
    """Convert a Jalali date to Gregorian date without external dependencies."""
    jy = int(jy); jm = int(jm); jd = int(jd)
    if not (1 <= jm <= 12 and 1 <= jd <= (31 if jm <= 6 else 30)):
        raise ValueError("invalid Jalali date")
    jy -= 979
    days = 365 * jy + (jy // 33) * 8 + ((jy % 33 + 3) // 4)
    days += sum(31 if i < 6 else 30 for i in range(jm - 1))
    days += jd - 1 + 79
    gy = 1600 + 400 * (days // 146097)
    days %= 146097
    leap = True
    if days >= 36525:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
        else:
            leap = False
    gy += 4 * (days // 1461)
    days %= 1461
    if days >= 366:
        leap = False
        days -= 1
        gy += days // 365
        days %= 365
    month_days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 1
    while gm <= 12 and days >= month_days[gm - 1]:
        days -= month_days[gm - 1]
        gm += 1
    converted = datetime(gy, gm, days + 1, tzinfo=IRAN_TZ)
    if gregorian_to_jalali(converted.year, converted.month, converted.day) != (int(jy + 979), int(jm), int(jd)):
        raise ValueError("invalid Jalali calendar date")
    return converted


def gregorian_to_jalali(gy, gm, gd):
    """Inverse for validation/display round-trips using the same Jalali civil calendar."""
    g_days_in_month = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_days_in_month[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return int(jy), int(jm), int(jd)

# EDITOR_PANEL_INSTALLED
import json
import hashlib
EDITABLE_TEXTS = {
    "welcome": ("متن خوشامدگویی", "💎 SEYED PREMIUM 👑\n\nبه دنیای خدمات پریمیوم خوش آمدید. ✨\n\n━━━━━━━━━━━━━━\n\n💎 خدمات ویژه | ⚡ خرید آسان\n🛡️ پشتیبانی و پیگیری سفارش\n\n━━━━━━━━━━━━━━\n\n👑 خاص انتخاب کنید. متفاوت تجربه کنید.\n\nبرای شروع از منوی زیر انتخاب کنید. 👇"),
    "main_menu": ("متن منوی اصلی", "💎 منوی اصلی SEYED PREMIUM\n\nیک گزینه انتخاب کن:"),
    "admin_title": ("عنوان پنل مدیریت", "⚙️ پنل مدیریت SEYED PREMIUM"),
    "support_title": ("عنوان پشتیبانی", "🎧 پشتیبانی SEYED PREMIUM"),
    "back_button": ("دکمه بازگشت", "🏠 بازگشت به منو"),
    "stars_button": ("دکمه استارز", "⭐️ استارز"),
    "premium_button": ("دکمه پریمیوم", "💎 پریمیوم"),
    "boost_button": ("دکمه بوست", "🚀 بوست"),
    "gifts_button": ("دکمه گیفت", "🎁 گیفت‌های تلگرام"),
    "numbers_button": ("دکمه شماره مجازی", "📱 شماره مجازی"),
    "config_button": ("دکمه کانفیگ", "🌐 خرید کانفیگ"),
    "orders_button": ("دکمه سفارش‌ها", "📦 سفارش‌های من"),
    "account_button": ("دکمه حساب کاربری", "👤 حساب کاربری"),
    "invite_button": ("دکمه دعوت دوستان", "👥 دعوت دوستان"),
    "topup_button": ("دکمه افزایش موجودی", "💳 افزایش موجودی"),
    "support_button": ("دکمه پشتیبانی", "🎧 پشتیبانی"),
    "buy_service_button": ("دکمه خرید سرویس", "🛍 خرید سرویس"),
    "wallet_button": ("دکمه کیف پول", "👛 کیف پول"),
    "orders_button": ("دکمه سفارش‌ها", "📚 سفارش‌های من"),
    "topup_button": ("دکمه شارژ کیف پول", "💳 شارژ کیف پول"),
    "profile_button": ("دکمه پروفایل", "👤 پروفایل من"),
    "free_stars_button": ("دکمه استارز رایگان", "⭐ استارز رایگان"),
    "faq_button": ("دکمه سوالات متداول", "❓ سوالات متداول"),
    "members_button": ("دکمه ممبر تلگرام", "👥 ممبر تلگرام"),
    "reactions_button": ("دکمه ری‌اکشن تلگرام", "💙 ری‌اکشن تلگرام"),
    "views_button": ("دکمه ویو تلگرام", "👀 ویو تلگرام"),
    "instagram_followers_button": ("دکمه فالوور اینستاگرام", "📸 فالوور اینستاگرام"),
    "instagram_likes_button": ("دکمه لایک اینستاگرام", "💙 لایک اینستاگرام"),
    "payment_card_number": ("شماره کارت پرداخت", "هنوز تنظیم نشده"),
    "payment_card_owner": ("نام صاحب کارت", "هنوز تنظیم نشده"),
    "payment_instructions": ("راهنمای پرداخت و ارسال رسید", "پس از پرداخت، تصویر واضح رسید را همین‌جا ارسال کنید. سفارش فقط پس از بررسی و تأیید مدیر قابل تحویل است."),
    "stars_menu_description": ("متن راهنمای منوی استارز", "🎁 استارز - انتخاب بسته\n\nبسته موردنظرت رو انتخاب کن 👇\n\n🔐 بدون نیاز به ورود به اکانت\n⚡️ تحویل طبق روند ثبت سفارش\n🍦 امکان سفارش با مقدار دلخواه\n💰 امکان بررسی موجودی و تخفیف\n💬 پشتیبانی در تمام مراحل"),

}

_BOT_SETTINGS_CACHE = None

# These defaults are embedded in bot.py so the bot does not require ui_config.py,
# service_menu.py, stars_menu.py, or a separately-created JSON settings file.
_PREMIUM_MAIN_MENU_ITEMS = [
    # The approved 9-row luxury layout. Icons are monochrome Unicode glyphs
    # because Telegram keyboard labels cannot embed arbitrary SVG/Lucide assets.
    {"id": "buy_service", "text": "◈ خـریـد سـرویـس", "callback": "cat_stars", "enabled": True, "row": 1},
    {"id": "wallet", "text": "▣ کـیـف پـول", "callback": "wallet", "enabled": True, "row": 2},
    {"id": "topup", "text": "＋ شـارژ کـیـف پـول", "callback": "topup", "enabled": True, "row": 2},
    {"id": "my_orders", "text": "▤ سـفـارش‌هـای مـن", "callback": "orders", "enabled": True, "row": 3},
    {"id": "account", "text": "◉ پـروفـایـل مـن", "callback": "account", "enabled": True, "row": 4},
    {"id": "support", "text": "☏ پـشـتـیـبـانـی", "callback": "support", "enabled": True, "row": 4},
    {"id": "free_stars", "text": "✦ اسـتـارز رایـگـان", "callback": "free_stars", "enabled": True, "row": 5},
    {"id": "numbers", "text": "▯ شـمـاره مـجـازی", "callback": "cat_numbers", "enabled": True, "row": 5},
    {"id": "mediator_request", "text": "✓ درخـواسـت واسـطـه", "callback": "mediator_request", "enabled": True, "row": 6},
    {"id": "config", "text": "⌘ خـریـد کـانـفـیـگ", "callback": "config_menu", "enabled": True, "row": 7},
    {"id": "faq", "text": "ⓘ سـؤالات مـتـداول", "callback": "faq", "enabled": True, "row": 7},
    {"id": "ton_buy", "text": "◇ خـریـد تـون", "callback": "ton_buy", "enabled": True, "row": 8},
    {"id": "track_order", "text": "⌕ پـیـگـیـری سـفـارش", "callback": "orders", "enabled": True, "row": 9},
    {"id": "ticket", "text": "▱ تـیـکـت", "callback": "support", "enabled": True, "row": 9},
]

_DEFAULT_UI_MENUS = {
    "main": deepcopy(_PREMIUM_MAIN_MENU_ITEMS),
    "services": [
        {"id": "stars", "text": "✦ اسـتـارز تـلـگـرام", "callback": "service_stars", "enabled": True, "row": 1, "button_style": "success"},
        {"id": "premium", "text": "◇ پـریـمـیـوم تـلـگـرام", "callback": "service_premium", "enabled": True, "row": 1, "button_style": "success"},
        {"id": "gifts", "text": "❖ گـیـفـت تـلـگـرام", "callback": "service_gifts", "enabled": True, "row": 2, "button_style": "primary"},
        {"id": "boost", "text": "➤ بـوسـت تـلـگـرام", "callback": "service_boost", "enabled": True, "row": 3, "button_style": "danger"},
        {"id": "members", "text": "♙ مـمـبـر تـلـگـرام", "callback": "service_members", "enabled": True, "row": 4, "button_style": "primary"},
        {"id": "reactions", "text": "ϟ ری‌اکـشـن تـلـگـرام", "callback": "service_reactions", "enabled": True, "row": 5, "button_style": "success"},
        {"id": "views", "text": "◎ ویـو تـلـگـرام", "callback": "service_views", "enabled": True, "row": 6, "button_style": "danger"},
        {"id": "instagram_followers", "text": "▣ فـالـوور ایـنـسـتـاگـرام", "callback": "service_instagram_followers", "enabled": True, "row": 7, "button_style": "primary"},
        {"id": "instagram_likes", "text": "♡ لایـک ایـنـسـتـاگـرام", "callback": "service_instagram_likes", "enabled": True, "row": 8, "button_style": "success"},
        {"id": "main_menu", "text": "⌂ مـنـوی اصـلـی", "callback": "home", "enabled": True, "row": 9, "button_style": "danger"},
    ],
    "stars": [
        {"id": "custom_amount", "text": "✎ مقدار دلخواه", "callback": "stars_custom", "enabled": True, "row": 13, "button_style": "primary"},
        {"id": "back_services", "text": "‹ بازگشت", "callback": "back_to_services", "enabled": True, "row": 14, "button_style": "danger"},
    ],
}
_LEGACY_SETTINGS_FILE = Path(__file__).with_name("bot_settings.json")  # read-only migration source
_SETTINGS_DB_KEY = "bot_settings_json"


def _normalize_bot_settings(data):
    if not isinstance(data, dict):
        data = {}
    menus = data.get("menus")
    if not isinstance(menus, dict):
        menus = {}
    for name, defaults in _DEFAULT_UI_MENUS.items():
        # Initialize missing menus only; do not silently restore buttons the owner
        # intentionally removed or reordered in the management panel.
        if not isinstance(menus.get(name), list):
            menus[name] = deepcopy(defaults)
    data["menus"] = menus
    if not isinstance(data.get("texts"), dict):
        data["texts"] = {}
    return data


def load_bot_settings():
    """Load editable text/menu settings from SQLite; import legacy JSON once if present."""
    global _BOT_SETTINGS_CACHE
    if _BOT_SETTINGS_CACHE is not None:
        return _BOT_SETTINGS_CACHE

    data = None
    try:
        with db() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (_SETTINGS_DB_KEY,)).fetchone()
        if row:
            data = json.loads(row["value"])
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        logger.debug("Could not read persistent UI settings from database", exc_info=True)

    if data is None:
        # Existing settings are imported, not overwritten or deleted. No JSON file is created.
        try:
            if _LEGACY_SETTINGS_FILE.exists():
                candidate = json.loads(_LEGACY_SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    data = candidate
        except (OSError, ValueError):
            logger.warning("Legacy UI settings could not be imported; defaults will be used.")

    data = _normalize_bot_settings(data or {})
    try:
        with db() as con:
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (_SETTINGS_DB_KEY, json.dumps(data, ensure_ascii=False)),
            )
            # If another process initialized the key first, use its persisted value.
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (_SETTINGS_DB_KEY,)).fetchone()
            if row:
                data = _normalize_bot_settings(json.loads(row["value"]))
    except (sqlite3.Error, ValueError, TypeError):
        logger.debug("UI settings could not be initialized in SQLite", exc_info=True)

    _BOT_SETTINGS_CACHE = data
    return _BOT_SETTINGS_CACHE


def save_bot_settings(data):
    """Persist editable UI texts and menus inside the existing shop database."""
    global _BOT_SETTINGS_CACHE
    normalized = _normalize_bot_settings(data)
    try:
        with db() as con:
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_SETTINGS_DB_KEY, json.dumps(normalized, ensure_ascii=False)),
            )
        _BOT_SETTINGS_CACHE = normalized
    except sqlite3.Error:
        logger.exception("Could not save persistent UI settings")
        raise


def ui_load():
    return load_bot_settings()


def ui_save(data):
    save_bot_settings(data)


def configured_buttons(menu):
    items = load_bot_settings().get("menus", {}).get(str(menu), [])
    return [dict(item) for item in items
            if isinstance(item, dict) and item.get("enabled", True)]


def apply_main_menu_redesign_once():
    """One-time additive UI migration: preserve custom features and apply the approved menu layout."""
    try:
        with db() as con:
            marker = con.execute(
                "SELECT value FROM app_settings WHERE key='main_menu_redesign_v2'"
            ).fetchone()
        if marker and str(marker["value"] or "") == "done":
            return

        settings = load_bot_settings()
        old_items = settings.get("menus", {}).get("main", [])
        layout = deepcopy(_PREMIUM_MAIN_MENU_ITEMS)
        target_callbacks = {str(item["callback"]) for item in layout}
        # Preserve legacy optional buttons' customized text/enable flags.
        optional_callbacks = {"config_menu", "faq", "invite"}
        for old in old_items if isinstance(old_items, list) else []:
            if not isinstance(old, dict):
                continue
            callback = str(old.get("callback", "")).strip()
            if callback in optional_callbacks:
                for item in layout:
                    if item["callback"] == callback:
                        if str(old.get("text", "")).strip():
                            item["text"] = str(old["text"]).strip()
                        item["enabled"] = bool(old.get("enabled", True))
                        break

        # Keep truly custom legacy buttons at the bottom. Standard old buttons are
        # already represented by the new layout, and are not duplicated.
        replaced_legacy_callbacks = {
            "cat_stars", "cat_numbers", "wallet", "topup", "orders", "account",
            "free_stars", "support", "buy_service", "numbers", "profile",
            "config", "invite", "faq", "service_stars", "service_premium",
            "service_gifts", "service_boost", "service_members", "service_reactions",
            "service_views", "service_instagram_followers", "service_instagram_likes",
        }
        known_ids = {str(item.get("id", "")) for item in layout}
        known_callbacks = {str(item.get("callback", "")) for item in layout}
        known_texts = {str(item.get("text", "")).strip() for item in layout}
        next_row = max(int(item.get("row", 0) or 0) for item in layout) + 1
        for old in old_items if isinstance(old_items, list) else []:
            if not isinstance(old, dict):
                continue
            callback = str(old.get("callback", "")).strip()
            text_value = str(old.get("text", "")).strip()
            item_id = str(old.get("id", "")).strip()
            if not callback or not text_value:
                continue
            if callback in known_callbacks or callback in replaced_legacy_callbacks:
                continue
            if item_id in known_ids or text_value in known_texts:
                continue
            preserved = dict(old)
            preserved["row"] = next_row
            layout.append(preserved)
            next_row += 1
            known_callbacks.add(callback)
            known_ids.add(item_id)
            known_texts.add(text_value)

        settings.setdefault("menus", {})["main"] = layout
        save_bot_settings(settings)
        with db() as con:
            # Preserve the owner's current menu mode. The current Bot API allows
            # native color styles for both reply and inline keyboard buttons.
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES ('main_menu_type', 'keyboard') "
                "ON CONFLICT(key) DO UPDATE SET value='keyboard'"
            )
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES ('main_menu_redesign_v2', 'done') "
                "ON CONFLICT(key) DO UPDATE SET value='done'"
            )
    except Exception:
        logger.exception("Could not apply one-time main-menu redesign; stored data was not reset")



def apply_main_menu_redesign_v3_once():
    """Install the owner's approved 14-button menu without resetting shop data."""
    try:
        with db() as con:
            marker = con.execute(
                "SELECT value FROM app_settings WHERE key='main_menu_redesign_v3'"
            ).fetchone()
        if marker and str(marker["value"] or "") == "done":
            return

        settings = load_bot_settings()
        # Replace only the main-menu definition. Other menu definitions, text edits,
        # product prices, orders, receipts, balances and the chosen menu mode remain intact.
        settings.setdefault("menus", {})["main"] = deepcopy(_PREMIUM_MAIN_MENU_ITEMS)
        save_bot_settings(settings)
        with db() as con:
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES ('main_menu_redesign_v3', 'done') "
                "ON CONFLICT(key) DO UPDATE SET value='done'"
            )
    except Exception:
        logger.exception("Could not apply main-menu redesign v3; stored shop data was not reset")


def apply_main_menu_redesign_v4_once():
    """Apply the approved luxury icon labels to the main menu without touching shop data."""
    try:
        with db() as con:
            marker = con.execute(
                "SELECT value FROM app_settings WHERE key='main_menu_redesign_v4'"
            ).fetchone()
        if marker and str(marker["value"] or "") == "done":
            return

        settings = load_bot_settings()
        # Only the main menu is replaced. Other menus, custom texts, the selected
        # menu mode, prices, inventory, receipts, wallet balances and order history persist.
        settings.setdefault("menus", {})["main"] = deepcopy(_PREMIUM_MAIN_MENU_ITEMS)
        save_bot_settings(settings)
        with db() as con:
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES ('main_menu_redesign_v4', 'done') "
                "ON CONFLICT(key) DO UPDATE SET value='done'"
            )
    except Exception:
        logger.exception("Could not apply luxury icon menu v4; stored shop data was not reset")


def apply_services_menu_design_once():
    """One-time migration to the exact owner-approved services screen.

    Only the services-menu configuration is replaced. Products, prices, orders,
    users, receipts, wallet balances, payment settings, other menus and text edits
    remain untouched. After the migration, the service screen is fully editable
    from the existing admin UI.
    """
    marker_key = "services_menu_design_v2"
    try:
        with db() as con:
            marker = con.execute("SELECT value FROM app_settings WHERE key=?", (marker_key,)).fetchone()
        if marker and str(marker["value"] or "") == "done":
            return

        settings = load_bot_settings()
        # Install the exact approved order, labels, symbols, colors and row widths.
        # Admin can subsequently change any of these items from the management UI.
        settings.setdefault("menus", {})["services"] = deepcopy(_DEFAULT_UI_MENUS["services"])
        save_bot_settings(settings)
        with db() as con:
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, 'done') "
                "ON CONFLICT(key) DO UPDATE SET value='done'",
                (marker_key,),
            )
    except Exception:
        logger.exception("Could not apply approved services menu design; existing shop data was retained")


def bot_text(key):
    defaults = dict(EDITABLE_TEXTS)
    item = defaults.get(key)
    if not item:
        return key
    settings = load_bot_settings()
    return settings.get(key, settings.get("texts", {}).get(key, item[1]))


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PRODUCTS = {
    "premium": ("💎 تلگرام پرمیوم", "اشتراک تلگرام پرمیوم"),
    "stars": ("⭐ استارز تلگرام", "خرید استارز تلگرام"),
    "gifts": ("🎁 گیفت تلگرام", "خرید گیفت تلگرام"),
    "boost": ("🚀 بوست کانال / گروه", "افزایش بوست کانال یا گروه"),
    "numbers": ("📱 شماره مجازی", "شماره مجازی"),
    "config": ("🌐 کانفیگ", "خرید کانفیگ"),
    "configs": ("🌐 کانفیگ", "خرید کانفیگ"),
    "members": ("👥 ممبر تلگرام", "ممبر تلگرام"),
    "reactions": ("💙 ری‌اکشن تلگرام", "ری‌اکشن تلگرام"),
    "views": ("👀 ویو تلگرام", "ویو تلگرام"),
    "instagram_followers": ("📸 فالوور اینستاگرام", "فالوور اینستاگرام"),
    "instagram_likes": ("💙 لایک اینستاگرام", "لایک اینستاگرام"),
    "instagram_views": ("🎬 ویو اینستاگرام", "ویو اینستاگرام"),
}


DEFAULT_COUPON_CATEGORIES = {
    "stars": "⭐ استارز تلگرام",
    "gifts": "🎁 گیفت تلگرام",
    "premium": "👑 پرمیوم تلگرام",
    "boost": "🚀 بوست تلگرام",
    "numbers": "📱 شماره مجازی",
    "config": "🔐 خرید کانفیگ",
    "configs": "🔐 خرید کانفیگ",
    "members": "👥 خدمات ممبر تلگرام",
    "reactions": "💟 ری‌اکشن تلگرام",
    "views": "👁️ ویو تلگرام",
    "instagram_followers": "📈 فالوور اینستاگرام",
    "instagram_likes": "💙 لایک اینستاگرام",
    "instagram_views": "🎬 ویو اینستاگرام",
}


def coupon_category_keys(category):
    """Return selected coupon category keys while retaining compatibility with old single-key rows."""
    if category is None or not str(category).strip() or str(category).strip().casefold() == "all":
        return []
    raw = str(category).strip()
    if raw.startswith("["):
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                return list(dict.fromkeys(str(x).strip() for x in value if str(x).strip() and str(x).casefold() != "all"))
        except (ValueError, TypeError):
            pass
    return [raw]


def coupon_category_label(category, con=None):
    """Human-readable legacy or multi-category coupon scope."""
    keys = coupon_category_keys(category)
    if not keys:
        return "🌐 همه محصولات"
    labels = []
    for key in keys:
        label = DEFAULT_COUPON_CATEGORIES.get(key)
        if not label:
            label = PRODUCTS.get(key, (f"🛍 {key}", str(key)))[0]
        if con is not None:
            try:
                row = con.execute("SELECT display_name FROM coupon_categories WHERE category_key=? AND active=1", (key,)).fetchone()
                if row and row["display_name"]:
                    label = str(row["display_name"])
            except sqlite3.Error:
                pass
        labels.append(label)
    return "، ".join(labels)


def coupon_selected_categories(stored_value):
    return set(coupon_category_keys(stored_value))


def list_coupon_categories(con):
    """Return the persisted category catalog plus any categories discovered on products."""
    defaults = dict(DEFAULT_COUPON_CATEGORIES)
    for key, label in PRODUCTS.items():
        defaults.setdefault(key, label[0])
    for order, (key, label) in enumerate(defaults.items(), start=1):
        # `config` and legacy `configs` are aliases for the same service; show only one coupon scope.
        if key == "configs":
            continue
        con.execute(
            "INSERT OR IGNORE INTO coupon_categories(category_key, display_name, active, sort_order, created_at) VALUES (?, ?, 1, ?, ?)",
            (key, label, order, utc_now().isoformat(timespec="seconds")),
        )
    try:
        product_rows = con.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND TRIM(category)!=''").fetchall()
    except sqlite3.Error:
        product_rows = []
    for row in product_rows:
        key = str(row[0]).strip()
        if key.casefold() in {"ton", "tons"}:
            continue
        if key == "configs":
            key = "config"
        if key:
            label = defaults.get(key, PRODUCTS.get(key, (f"🛍 {key}", key))[0])
            con.execute(
                "INSERT OR IGNORE INTO coupon_categories(category_key, display_name, active, sort_order, created_at) VALUES (?, ?, 1, 500, ?)",
                (key, label, utc_now().isoformat(timespec="seconds")),
            )
    return [dict(row) for row in con.execute(
        "SELECT category_key, display_name FROM coupon_categories WHERE active=1 AND lower(category_key) NOT IN ('ton', 'tons') AND lower(display_name) NOT LIKE '%ton (ton)%' ORDER BY sort_order, display_name COLLATE NOCASE"
    ).fetchall()]


def coupon_scope_keyboard(code, categories, selected):
    buttons = []
    for index, item in enumerate(categories):
        key = str(item["category_key"])
        label = str(item["display_name"])
        marker = "✅" if key in selected else "⬜"
        buttons.append([InlineKeyboardButton(f"{label} {marker}", callback_data=f"admin_coupon_category_toggle_{index}")])
    buttons.extend([
        [InlineKeyboardButton("🌐 همه محصولات (پاک کردن انتخاب)", callback_data="admin_coupon_category_all")],
        [InlineKeyboardButton("➕ افزودن دسته جدید", callback_data="admin_coupon_category_add")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_coupon_category_back")],
    ])
    return InlineKeyboardMarkup(buttons)


def coupon_expiry_display(row):
    """Format expiry using stored Jalali date and independently stored local clock time."""
    try:
        date_value = str(row["expires_jalali_date"] or "").strip()
    except (KeyError, IndexError):
        date_value = ""
    try:
        clock_value = str(row["expires_local_time"] or "").strip()
    except (KeyError, IndexError):
        clock_value = ""
    label = str(row["expiry_label"] or "").strip() if "expiry_label" in row.keys() else ""
    if not date_value and re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", label):
        date_value = label.replace("/", "-")
    if date_value:
        if clock_value:
            return f"{date_value} ساعت {clock_value} (شمسی)"
        return f"{date_value} تا پایان روز (شمسی)"
    if clock_value and not row["expires_at"]:
        return f"ساعت {clock_value} (تاریخ هنوز تعیین نشده)"
    return format_coupon_expiry(row["expires_at"], label)


def coupon_management_detail(con, code):
    row = con.execute("""
        SELECT discount_codes.*,
               MAX(COALESCE(total_uses, 0), (
                   SELECT COUNT(*) FROM coupon_usage u
                   WHERE u.code=discount_codes.code AND u.status IN ('used','pending')
               )) AS actual_total_uses
        FROM discount_codes WHERE code=?
    """, (code,)).fetchone()
    if not row:
        return None, None
    expired = not expiry_is_valid(row["expires_at"])
    status = "⌛ منقضی‌شده" if expired else ("🟢 فعال" if row["active"] else "🔴 غیرفعال")
    amount_text = f"{int(row['discount_amount'])}%" if row["discount_type"] == "percent" else f"{int(row['discount_amount']):,} تومان (مبلغ ثابت)"
    max_uses = "نامحدود" if row["max_uses"] is None or int(row["max_uses"] or 0) <= 0 else str(int(row["max_uses"]))
    per_user = "نامحدود" if row["per_user_limit"] is None or int(row["per_user_limit"] or 0) <= 0 else str(int(row["per_user_limit"]))
    recipient = "همه کاربران" if row["recipient_type"] == "all" else (row["recipient_ids"] or "کاربر انتخاب‌شده")
    detail = (
        f"🏷️ کد تخفیف: {code}\n\n"
        f"وضعیت: {status}\n"
        f"مقدار تخفیف: {amount_text}\n"
        f"تعداد استفاده: {int(row['actual_total_uses'] or 0)} از {max_uses}\n"
        f"سقف استفادهٔ هر کاربر: {per_user}\n"
        f"انقضا: {coupon_expiry_display(row)}\n"
        f"اختصاص به کاربر: {recipient}\n"
        f"حداقل مبلغ خرید: {int(row['min_order_amount'] or 0):,} تومان\n"
        f"📦 دسته‌های محصول: {coupon_category_label(row['category'], con=con)}\n"
        f"محصول مشخص: {row['product_name'] or 'همه محصولات محدوده انتخاب‌شده'}\n\n"
        "از دکمه‌های زیر ویرایش کن:"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ تغییر کد", callback_data="admin_coupon_edit_code_" + code), InlineKeyboardButton("💯 تغییر مقدار تخفیف", callback_data="admin_coupon_edit_amount_" + code)],
        [InlineKeyboardButton("🔢 تعداد مجاز استفاده", callback_data="admin_coupon_edit_userlimit_" + code), InlineKeyboardButton("📅 تاریخ انقضا", callback_data="admin_coupon_edit_expiry_" + code)],
        [InlineKeyboardButton("👤 اختصاص به یک کاربر خاص", callback_data="admin_coupon_edit_recipient_" + code)],
        [InlineKeyboardButton("🧾 حداقل مبلغ خرید", callback_data="admin_coupon_edit_min_" + code)],
        [InlineKeyboardButton("📦 دسته‌های محصول", callback_data="admin_coupon_edit_scope_" + code)],
        [InlineKeyboardButton("🔄 ریست تعداد استفاده", callback_data="admin_coupon_reset_" + code)],
        [InlineKeyboardButton("🗑️ حذف این کد", callback_data="admin_coupon_delete_" + code)],
        [InlineKeyboardButton("⬅️ بازگشت به لیست", callback_data="admin_coupons")],
    ]
    return detail, InlineKeyboardMarkup(keyboard)


def coupon_scope_text(con, code, categories, selected):
    if not selected:
        current = "همه محصولات"
    else:
        current = "، ".join(str(item["display_name"]) for item in categories if item["category_key"] in selected)
    return (
        f"📦 دسته‌های محصول کد {code}\n\n"
        f"الان معتبر برای: {current}\n\n"
        "هر دسته‌ای را که تیک بزنی، کد فقط روی همان دسته‌ها کار می‌کند. "
        "اگر هیچ دسته‌ای انتخاب نشده باشد، کد روی همه محصولات کار می‌کند.\n\n"
        "ℹ️ دسته‌بندی انتخابی باید با دستهٔ واقعی محصول در سفارش مطابقت داشته باشد."
    )


def product_details_prompt(product):
    category = str(product["category"] or "").lower()
    if category == "boost":
        return "🚀 لطفاً لینک گروه یا کانالی را که می‌خواهید بوست شود ارسال کنید.\n\nلینک عمومی یا لینک دعوت خصوصی قابل قبول است."
    if category == "gifts":
        return (
            "🎁 اطلاعات گیرندهٔ گیفت را در یک پیام بفرست:\n\n"
            "ID عددی گیرنده: 123456789\n"
            "آیدی تلگرام گیرنده: @username یا ندارم\n\n"
            "اگر برای خودت خرید می‌کنی، ID عددی و آیدی خودت را در قسمت گیرنده هم وارد کن.\n"
            "ID عددی برای بررسی و ارسال رسمی لازم است؛ آیدی @ اختیاری است."
        )
    if category == "premium":
        return (
            "👑 اطلاعات گیرندهٔ تلگرام پرمیوم را در یک پیام بفرست:\n\n"
            "ID عددی گیرنده: 123456789\n"
            "آیدی تلگرام گیرنده: @username یا ندارم\n\n"
            "برای تحویل خودکار، ID عددی گیرنده لازم است. اگر فقط آیدی @ را داری، آن را هم بنویس؛ در صورت شناخته‌شدن گیرنده در ربات، ID از سوابق واقعی پیدا می‌شود.\n"
            "برای تحویل دستی، آیدی @ گیرنده کافی است."
        )
    if category == "stars":
        return "👈 لطفاً آیدی اکانتی را که می‌خواهی استارز برای آن تهیه کنی ارسال کن.\nمثال: @SeyedPro_AIA"
    return "لطفاً اطلاعات موردنیاز سفارش را برای پشتیبانی ارسال کنید."


# ==========================================
# دیتابیس
# ==========================================

def db():
    connection = sqlite3.connect(
        DB_FILE,
        timeout=10,
        cached_statements=256,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def ensure_discount_schema(con):
    """Apply additive, non-destructive migrations for the discount system."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS discount_codes (
            code TEXT PRIMARY KEY,
            discount_amount INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT 'stars',
            product_name TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(discount_codes)").fetchall()}
    migrations = {
        "expires_at": "TEXT",
        "expiry_label": "TEXT",
        "expires_jalali_date": "TEXT",
        "expires_local_time": "TEXT",
        "min_order_amount": "INTEGER NOT NULL DEFAULT 0",
        "discount_type": "TEXT NOT NULL DEFAULT 'fixed'",
        "max_uses": "INTEGER",
        "per_user_limit": "INTEGER",
        "total_uses": "INTEGER NOT NULL DEFAULT 0",
        "recipient_type": "TEXT NOT NULL DEFAULT 'all'",
        "recipient_ids": "TEXT",
        "created_by": "INTEGER",
        "created_at": "TEXT",
        "used_by": "INTEGER",
        "used_order_id": "INTEGER",
        "used_at": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            con.execute(f"ALTER TABLE discount_codes ADD COLUMN {column} {definition}")
    con.execute("""
        CREATE TABLE IF NOT EXISTS coupon_categories (
            category_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 100,
            created_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS coupon_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            discount_amount INTEGER NOT NULL,
            used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'used'
        )
    """)
    usage_columns = {row[1] for row in con.execute("PRAGMA table_info(coupon_usage)").fetchall()}
    if "status" not in usage_columns:
        con.execute("ALTER TABLE coupon_usage ADD COLUMN status TEXT NOT NULL DEFAULT 'used'")
    con.execute("CREATE INDEX IF NOT EXISTS idx_coupon_usage_code_user ON coupon_usage(code, user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_coupon_usage_order ON coupon_usage(order_id)")

    # Older installs do not have these optional order fields. Add them without rebuilding orders.
    order_columns = {row[1] for row in con.execute("PRAGMA table_info(orders)").fetchall()}
    if order_columns:
        if "coupon_code" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN coupon_code TEXT")
        if "coupon_discount" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN coupon_discount INTEGER NOT NULL DEFAULT 0")


def coupon_usage_totals(con, code, user_id=None):
    """Count committed and pending reservations; tolerate legacy aggregate-only records."""
    if user_id is None:
        usage_count = int(con.execute(
            "SELECT COUNT(*) FROM coupon_usage WHERE code=? AND status IN ('used','pending')", (code,)
        ).fetchone()[0])
        coupon = con.execute("SELECT total_uses FROM discount_codes WHERE code=?", (code,)).fetchone()
        legacy_count = int(coupon["total_uses"] or 0) if coupon else 0
        return max(usage_count, legacy_count)
    return int(con.execute(
        "SELECT COUNT(*) FROM coupon_usage WHERE code=? AND user_id=? AND status IN ('used','pending')", (code, user_id)
    ).fetchone()[0])


def validate_coupon_for_order(con, code, user_id, price, category, product_name=None, username=None):
    """Authoritative coupon validation shared by preview and order-creation/payment paths."""
    ensure_discount_schema(con)
    clean_code = str(code or "").strip().upper()
    row = con.execute("SELECT * FROM discount_codes WHERE UPPER(code)=? LIMIT 1", (clean_code,)).fetchone()
    if not row:
        return {"ok": False, "reason": "❌ این کد تخفیف در سیستم ثبت نشده است."}
    if not int(row["active"] or 0):
        return {"ok": False, "reason": "⛔ این کد تخفیف غیرفعال است."}
    if not expiry_is_valid(row["expires_at"]):
        return {"ok": False, "reason": "⌛ اعتبار زمانی این کد تخفیف به پایان رسیده است."}
    # Preserve the old single-use behavior only for untouched legacy records.
    # Once explicit per-user or global limits are configured, those limits are authoritative.
    if (row["used_by"] is not None
            and row["per_user_limit"] is None
            and row["max_uses"] is None):
        return {"ok": False, "reason": "⛔ این کد قدیمی قبلاً مصرف شده و دیگر قابل استفاده نیست. برای فعال‌سازی مجدد، محدودیت استفاده را در مدیریت تنظیم کن."}

    stored_categories = coupon_category_keys(row["category"])
    wanted_category = str(category or "").strip().casefold()
    aliases = {"config": "configs", "configs": "config", "instagram_follower": "instagram_followers"}
    category_matches = not stored_categories or any(
        key.casefold() == wanted_category
        or aliases.get(key.casefold()) == wanted_category
        or aliases.get(wanted_category) == key.casefold()
        for key in stored_categories
    )
    if not category_matches:
        expected = coupon_category_label(row["category"], con=con)
        return {"ok": False, "reason": f"⛔ این کد مخصوص دسته‌های «{expected}» است و برای این محصول معتبر نیست."}
    if row["product_name"]:
        actual_names = {str(product_name or "").strip().casefold()}
        # Stars bundles have a stable label even in purchase flows that resolve them from the amount.
        if not actual_names.intersection({str(row["product_name"]).strip().casefold()}):
            return {"ok": False, "reason": f"⛔ این کد مخصوص محصول «{row['product_name']}» است، نه محصول انتخاب‌شده."}

    total_uses = coupon_usage_totals(con, clean_code)
    if row["max_uses"] is not None and int(row["max_uses"]) > 0 and total_uses >= int(row["max_uses"]):
        return {"ok": False, "reason": "⛔ سقف مصرف کل این کد تکمیل شده است."}
    if row["per_user_limit"] is not None and int(row["per_user_limit"]) > 0:
        user_uses = coupon_usage_totals(con, clean_code, int(user_id))
        if row["used_by"] is not None and int(row["used_by"]) == int(user_id):
            user_uses = max(user_uses, 1)
        if user_uses >= int(row["per_user_limit"]):
            return {"ok": False, "reason": "⛔ سقف استفادهٔ شما از این کد تکمیل شده است."}

    if str(row["recipient_type"] or "all") != "all":
        try:
            allowed = json.loads(row["recipient_ids"] or "[]")
            allowed_ids = {str(x).strip() for x in allowed if str(x).strip().isdigit()}
            allowed_names = {str(x).strip().lstrip("@").casefold() for x in allowed if str(x).strip().startswith("@")}
            current_username = str(username or "").strip().lstrip("@").casefold()
            if str(user_id) not in allowed_ids and (not current_username or current_username not in allowed_names):
                return {"ok": False, "reason": "⛔ این کد برای حساب کاربری شما مجاز نیست."}
        except (ValueError, TypeError):
            return {"ok": False, "reason": "⚠️ تنظیم کاربران مجاز این کد خراب است؛ با پشتیبانی تماس بگیرید."}

    original_price = int(price or 0)
    if original_price <= 0:
        return {"ok": False, "reason": "❌ قیمت محصول هنوز مشخص نیست؛ فعلاً امکان اعمال کد تخفیف وجود ندارد."}
    minimum = int(row["min_order_amount"] or 0)
    if minimum > original_price:
        return {"ok": False, "reason": f"❌ حداقل مبلغ خرید این کد {minimum:,} تومان است."}

    raw_discount = int(row["discount_amount"] or 0)
    discount_type = str(row["discount_type"] or "fixed")
    if discount_type == "percent":
        if not 1 <= raw_discount <= 100:
            return {"ok": False, "reason": "❌ درصد تخفیف این کد معتبر نیست."}
        discount = (original_price * raw_discount) // 100
    else:
        discount = raw_discount
    if discount <= 0:
        return {"ok": False, "reason": "❌ مقدار تخفیف این کد برای این سفارش معتبر نیست."}
    if discount > original_price:
        return {"ok": False, "reason": "❌ مبلغ ثابت تخفیف از مبلغ سفارش بیشتر است."}
    return {"ok": True, "row": row, "code": clean_code, "discount": discount, "final_price": original_price - discount}


def save_coupon_from_admin_state(state, admin_user_id):
    """Persist a complete new global coupon; timed expiry starts at successful insert time."""
    if not state.get("code") or state.get("discount_type") not in ("percent", "fixed"):
        raise ValueError("incomplete coupon state")
    amount = int(state.get("discount_amount", 0))
    if amount <= 0 or (state["discount_type"] == "percent" and amount > 100):
        raise ValueError("invalid discount amount")
    max_uses = state.get("max_uses")
    per_user_limit = state.get("per_user_limit")
    if max_uses is not None and int(max_uses) < 1:
        max_uses = None
    if per_user_limit is not None and int(per_user_limit) < 1:
        per_user_limit = None

    with db() as con:
        ensure_discount_schema(con)
        con.execute("BEGIN IMMEDIATE")
        created_at = utc_now()
        duration = state.get("expiry_duration_seconds")
        expires_at = ((created_at + timedelta(seconds=int(duration))).isoformat(timespec="seconds")
                      if duration is not None else state.get("expires_at"))
        expiry_label = state.get("expiry_label")
        con.execute("""
            INSERT INTO discount_codes
            (code, discount_amount, category, product_name, active, discount_type,
             expires_at, expiry_label, max_uses, per_user_limit, total_uses,
             recipient_type, recipient_ids, min_order_amount, created_by, created_at)
            VALUES (?, ?, 'all', NULL, 1, ?, ?, ?, ?, ?, 0, 'all', '[]', 0, ?, ?)
        """, (str(state["code"]).upper(), amount, state["discount_type"], expires_at,
              expiry_label, max_uses, per_user_limit, int(admin_user_id),
              created_at.isoformat(timespec="seconds")))
        return {
            "code": str(state["code"]).upper(), "discount_amount": amount,
            "discount_type": state["discount_type"], "expires_at": expires_at,
            "expiry_label": expiry_label, "max_uses": max_uses,
            "per_user_limit": per_user_limit, "created_at": created_at.isoformat(timespec="seconds"),
            "category": "all", "recipient_type": "all", "total_uses": 0,
        }


def coupon_created_message(record):
    amount = (f"{int(record['discount_amount'])}%" if record["discount_type"] == "percent"
              else f"{int(record['discount_amount']):,} تومان")
    total_cap = "نامحدود" if record.get("max_uses") is None else str(int(record["max_uses"]))
    user_cap = "نامحدود" if record.get("per_user_limit") is None else str(int(record["per_user_limit"]))
    return (
        "✅ کد تخفیف ساخته شد!\n\n"
        f"🏷️ کد: {record['code']}\n"
        f"💰 تخفیف: {amount}\n"
        f"👤 حداکثر استفادهٔ هر کاربر: {user_cap}\n"
        f"🔢 سقف مصرف کل: {total_cap}\n"
        f"📅 انقضا: {format_coupon_expiry(record.get('expires_at'), record.get('expiry_label'))}\n"
        "👥 کاربران: همه\n"
        "📦 محدوده: همهٔ محصولات فعلی و آینده"
    )


def init_db():
    with sqlite3.connect(DB_FILE, timeout=10) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT,
                referred_by INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                price INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        # Additive catalog metadata: do not replace or reset the existing shop.db.
        product_columns = {row["name"] for row in con.execute("PRAGMA table_info(products)").fetchall()}
        additive_product_columns = {
            "button_text": "TEXT NOT NULL DEFAULT ''",
            "icon": "TEXT NOT NULL DEFAULT ''",
            "button_style": "TEXT NOT NULL DEFAULT 'success'",
            "sort_order": "INTEGER NOT NULL DEFAULT 0",
            "description": "TEXT NOT NULL DEFAULT ''",
            "image_file_id": "TEXT NOT NULL DEFAULT ''",
            "star_amount": "INTEGER",
            "currency": "TEXT NOT NULL DEFAULT 'تومان'",
            "old_price": "INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, column_ddl in additive_product_columns.items():
            if column_name not in product_columns:
                con.execute(f"ALTER TABLE products ADD COLUMN {column_name} {column_ddl}")
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'در انتظار بررسی',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        order_columns = {row["name"] for row in con.execute("PRAGMA table_info(orders)").fetchall()}
        additive_order_columns = {
            "currency": "TEXT NOT NULL DEFAULT 'تومان'",
            "order_details": "TEXT NOT NULL DEFAULT ''",
            "receipt_file_id": "TEXT NOT NULL DEFAULT ''",
            "receipt_submitted_at": "TEXT",
            "payment_method": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, column_ddl in additive_order_columns.items():
            if column_name not in order_columns:
                con.execute(f"ALTER TABLE orders ADD COLUMN {column_name} {column_ddl}")
        # Additive, non-destructive storage for persistent global bot settings.
        con.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.execute(
            "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('main_menu_type', 'keyboard')"
        )
        # Preserve the administrator's selected menu mode across restarts.
        # Persist visible button -> callback routing so keyboard menus continue to work after restart.
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_menu_routes (
                user_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                action_type TEXT NOT NULL,
                action_value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, label)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_menu_state (
                user_id INTEGER PRIMARY KEY,
                menu_mode TEXT NOT NULL DEFAULT 'inline'
            )
        """)

        user_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()
        }
        if "joined_at" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN joined_at TEXT")
        if "referred_by" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        if "menu_type_seen" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN menu_type_seen TEXT")
        con.execute(
            "UPDATE users SET joined_at = CURRENT_TIMESTAMP "
            "WHERE joined_at IS NULL OR joined_at = ''"
        )

        count = con.execute(
            "SELECT COUNT(*) AS n FROM products"
        ).fetchone()["n"]

        if count == 0:
            defaults = [
                ("premium", "💎 پرمیوم تلگرام - ۱ ماه", 0),
                ("stars", "⭐ استارز تلگرام", 0),
                ("gifts", "🎁 گیفت تلگرام", 0),
                ("boost", "🚀 بوست کانال", 0),
                ("numbers", bot_text("numbers_button"), 0),
            ]
            con.executemany(
                "INSERT INTO products(category, name, price) VALUES (?, ?, ?)",
                defaults,
            )

        # محصول آزمایشی ناخواستهٔ «1 views» از خرید و فهرست محصولات فعال خارج شود؛
        # ردیف حذف نمی‌شود تا سابقه و ارجاع‌های احتمالی دیتابیس حفظ شوند.
        con.execute("UPDATE products SET active=0 WHERE LOWER(TRIM(name)) IN ('1 views', '1 view')")

        # Parse existing package amounts once before seeding. This prevents legacy names such
        # as "⭐ 500 استارز" from being duplicated by the fixed default catalog.
        amount_marker = con.execute(
            "SELECT value FROM app_settings WHERE key='stars_amount_backfill_v1'"
        ).fetchone()
        if not amount_marker:
            for old_star in con.execute(
                "SELECT id, name FROM products WHERE category='stars' AND star_amount IS NULL ORDER BY id"
            ).fetchall():
                match = re.search(r"(?<!\d)(\d[\d,٬، ]*)", normalize_ascii_digits(str(old_star["name"])))
                if not match:
                    continue
                try:
                    detected_amount = int(re.sub(r"[,٬، ]", "", match.group(1)))
                except ValueError:
                    continue
                if detected_amount > 0:
                    # Preserve the first row for a repeated amount; later duplicates stay hidden.
                    already = con.execute(
                        "SELECT id FROM products WHERE category='stars' AND star_amount=? LIMIT 1",
                        (detected_amount,),
                    ).fetchone()
                    if not already:
                        con.execute("UPDATE products SET star_amount=? WHERE id=?", (detected_amount, old_star["id"]))
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES ('stars_amount_backfill_v1', 'done') "
                "ON CONFLICT(key) DO UPDATE SET value='done'"
            )

        # هر بستهٔ ثابت استارز یک محصول مستقل دارد؛ قیمت‌های موجود دست‌نخورده می‌مانند.
        fixed_stars = (50, 75, 100, 150, 250, 350, 500, 1000, 2500, 5000, 10000, 50000)
        for position, amount in enumerate(fixed_stars, start=1):
            name = f"استارز {amount} عدد"
            style = {50: "success", 75: "primary", 100: "danger", 150: "success",
                     250: "primary", 350: "danger", 500: "success", 1000: "primary",
                     2500: "danger", 5000: "success", 10000: "primary", 50000: "danger"}[amount]
            con.execute(
                "INSERT INTO products(category, name, price, active, icon, button_style, sort_order, star_amount) "
                "SELECT 'stars', ?, 0, 1, '✧', ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM products WHERE category='stars' AND (name=? OR star_amount=?))",
                (name, style, position * 10, amount, name, amount),
            )

        premium_marker = con.execute("SELECT value FROM app_settings WHERE key='premium_auto_packages_v1'").fetchone()
        if not premium_marker:
            premium_rows = con.execute("SELECT id, name, sort_order FROM products WHERE lower(category)='premium'").fetchall()
            present_months = set()
            for premium_row in premium_rows:
                found = re.search(r"(?<!\d)(3|6|12)\s*(?:ماه(?:ه)?|months?|mo\b)", normalize_ascii_digits(str(premium_row["name"] or "")), re.IGNORECASE)
                if found:
                    present_months.add(int(found.group(1)))
            next_order = max((int(row["sort_order"] or 0) for row in premium_rows), default=0) + 10
            for months in (3, 6, 12):
                if months in present_months:
                    continue
                con.execute(
                    "INSERT INTO products(category, name, price, active, icon, button_style, sort_order, description, currency) "
                    "VALUES ('premium', ?, 0, 1, '◇', 'primary', ?, ?, 'تومان')",
                    (f"💎 تلگرام پرمیوم - {months} ماه", next_order, f"اشتراک تلگرام پرمیوم {months} ماهه؛ قیمت فقط با منبع زنده نمایش داده می‌شود."),
                )
                next_order += 10
            con.execute("INSERT INTO app_settings(key, value) VALUES ('premium_auto_packages_v1', 'done') ON CONFLICT(key) DO UPDATE SET value='done'")

        # Keep only one active product for each Telegram-supported Premium duration.
        # Legacy/duplicate products are deactivated, never deleted, so old order history remains intact.
        premium_cleanup_marker = con.execute(
            "SELECT value FROM app_settings WHERE key='premium_supported_plans_cleanup_v1'"
        ).fetchone()
        if not premium_cleanup_marker:
            active_premium_rows = con.execute(
                "SELECT id, name FROM products WHERE lower(category)='premium' AND active=1 ORDER BY id"
            ).fetchall()
            kept_premium_months = set()
            for premium_row in active_premium_rows:
                found = re.search(
                    r"(?<!\d)(3|6|12)\s*(?:ماه(?:ه)?|months?|mo\b)",
                    normalize_ascii_digits(str(premium_row["name"] or "")), re.IGNORECASE,
                )
                months = int(found.group(1)) if found else None
                if months not in (3, 6, 12) or months in kept_premium_months:
                    con.execute("UPDATE products SET active=0 WHERE id=?", (int(premium_row["id"]),))
                else:
                    kept_premium_months.add(months)
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES ('premium_supported_plans_cleanup_v1', 'done') "
                "ON CONFLICT(key) DO UPDATE SET value='done'"
            )

        # One-time, additive migration for existing Stars products. This marker prevents
        # later restarts from overwriting an administrator's chosen icon, color or order.
        # The package migration fixes the requested glyph/order but keeps every saved button color.
        marker = con.execute(
            "SELECT value FROM app_settings WHERE key='stars_icons_colors_v1'"
        ).fetchone()
        if not marker:
            rows = con.execute(
                "SELECT id, name, star_amount, sort_order FROM products WHERE category='stars' ORDER BY id"
            ).fetchall()
            known_order = {amount: (index + 1) * 10 for index, amount in enumerate(fixed_stars)}
            extra_order = max((int(row['sort_order'] or 0) for row in rows), default=0) + 10
            for row in rows:
                amount = row["star_amount"]
                if amount is None:
                    match = re.search(r"(?<!\d)(\d[\d,٬، ]*)", normalize_ascii_digits(str(row["name"])))
                    if match:
                        try:
                            candidate = int(re.sub(r"[,٬، ]", "", match.group(1)))
                            amount = candidate if candidate > 0 else None
                        except ValueError:
                            amount = None
                if amount is None:
                    continue
                amount = int(amount)
                order = known_order.get(amount, extra_order)
                if amount not in known_order:
                    extra_order += 10
                con.execute(
                    "UPDATE products SET star_amount=?, icon='✧', sort_order=? "
                    "WHERE id=? AND category='stars'",
                    (amount, order, int(row["id"])),
                )
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES ('stars_icons_colors_v1', 'done') "
                "ON CONFLICT(key) DO UPDATE SET value='done'"
            )

        # Backfill legacy ordering once. A manager may later intentionally set sort_order=0.
        sort_marker = con.execute("SELECT value FROM app_settings WHERE key='product_sort_order_backfill_v1'").fetchone()
        if not sort_marker:
            con.execute("UPDATE products SET sort_order=id WHERE sort_order=0")
            con.execute("INSERT INTO app_settings(key, value) VALUES ('product_sort_order_backfill_v1', 'done') ON CONFLICT(key) DO UPDATE SET value='done'")

    with db() as con:
        ensure_gift_support_schema(con)
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_products_category_active ON products(category, active)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)")
        # New request flows use additive tables in the existing shop.db. Existing
        # products, orders, balances and payment history are never rewritten here.
        con.execute("""
            CREATE TABLE IF NOT EXISTS mediator_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'در انتظار بررسی مدیر',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS ton_purchase_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                amount_ton TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'در انتظار بررسی مدیر',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
    apply_main_menu_redesign_once()
    apply_main_menu_redesign_v3_once()
    apply_main_menu_redesign_v4_once()
    apply_services_menu_design_once()


# ==========================================
# Gift catalog, gift orders and persistent support tickets
# Additive module embedded in bot.py (keeps the existing shop.db intact).
# ==========================================

GIFT_SETTINGS_KEY = "gift_management_settings_v1"
GIFT_DEFAULT_SETTINGS = {
    "base_star_rate_toman": 1000,
    "profit_percent": 20,
    "auto_rate_enabled": False,
    "usd_per_star_override": "",
    "usd_toman_manual": 0,
    "manual_delivery_all": False,
    "customer_delivery_choice": False,
    "support_first": False,
    "support_text": "📩 برای هماهنگی تحویل سفارش، لطفاً یک پیام کوتاه برای پشتیبانی بفرست. این پیام به‌تنهایی تأیید پرداخت یا تحویل سفارش نیست.",
    "catalog_last_sync": "",
    "catalog_last_error": "",
}


def ensure_gift_support_schema(con):
    """Safe, additive schema migrations; never drop or recreate existing shop data."""
    product_columns = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
    product_migrations = {
        "source_type": "TEXT NOT NULL DEFAULT 'manual'",
        "telegram_gift_id": "TEXT NOT NULL DEFAULT ''",
        "star_count": "INTEGER",
        "catalog_available": "INTEGER NOT NULL DEFAULT 0",
        "custom_link": "TEXT NOT NULL DEFAULT ''",
        "delivery_mode": "TEXT NOT NULL DEFAULT 'manual'",
    }
    for name, ddl in product_migrations.items():
        if name not in product_columns:
            con.execute(f"ALTER TABLE products ADD COLUMN {name} {ddl}")

    order_columns = {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
    order_migrations = {
        "buyer_username": "TEXT NOT NULL DEFAULT ''",
        "buyer_full_name": "TEXT NOT NULL DEFAULT ''",
        "recipient_user_id": "INTEGER",
        "recipient_username": "TEXT NOT NULL DEFAULT ''",
        "product_name_snapshot": "TEXT NOT NULL DEFAULT ''",
        "gift_id": "TEXT NOT NULL DEFAULT ''",
        "delivery_mode": "TEXT NOT NULL DEFAULT 'manual'",
        "order_code": "TEXT NOT NULL DEFAULT ''",
        "payment_verified_at": "TEXT",
        "delivered_at": "TEXT",
        "delivery_error": "TEXT NOT NULL DEFAULT ''",
        "delivery_started_at": "TEXT",
    }
    for name, ddl in order_migrations.items():
        if name not in order_columns:
            con.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")

    con.execute("CREATE INDEX IF NOT EXISTS idx_orders_recipient_id ON orders(recipient_user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_code ON orders(order_code)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_products_gift_id ON products(telegram_gift_id)")

    con.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_code TEXT NOT NULL UNIQUE,
            sender_user_id INTEGER NOT NULL,
            sender_username TEXT NOT NULL DEFAULT '',
            sender_full_name TEXT NOT NULL DEFAULT '',
            buyer_user_id INTEGER NOT NULL,
            buyer_username TEXT NOT NULL DEFAULT '',
            recipient_user_id INTEGER,
            recipient_username TEXT NOT NULL DEFAULT '',
            product_name TEXT NOT NULL DEFAULT '',
            order_code TEXT NOT NULL DEFAULT '',
            order_id INTEGER,
            message_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'جدید',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            admin_replied_at TEXT,
            last_admin_id INTEGER,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS support_ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            sender_user_id INTEGER NOT NULL,
            sender_role TEXT NOT NULL,
            message_text TEXT NOT NULL DEFAULT '',
            telegram_file_id TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES support_tickets(id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS support_form_sessions (
            user_id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_support_tickets_sender ON support_tickets(sender_user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_support_tickets_buyer ON support_tickets(buyer_user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_support_tickets_recipient ON support_tickets(recipient_user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_support_messages_ticket ON support_ticket_messages(ticket_id, id)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS support_active_tickets (
            user_id INTEGER PRIMARY KEY,
            ticket_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES support_tickets(id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS order_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            actor_user_id INTEGER,
            event_key TEXT NOT NULL,
            status_text TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_order_history_order ON order_status_history(order_id,id)")

    con.execute("""
        CREATE TABLE IF NOT EXISTS gift_star_topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT NOT NULL UNIQUE,
            admin_user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'XTR',
            status TEXT NOT NULL DEFAULT 'pending',
            telegram_charge_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            paid_at TEXT
        )
    """)
    con.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)",
                (GIFT_SETTINGS_KEY, json.dumps(GIFT_DEFAULT_SETTINGS, ensure_ascii=False)))


def load_gift_settings():
    cfg = dict(GIFT_DEFAULT_SETTINGS)
    try:
        with db() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (GIFT_SETTINGS_KEY,)).fetchone()
        if row:
            saved = json.loads(row["value"] or "{}")
            if isinstance(saved, dict):
                cfg.update(saved)
    except Exception:
        logger.exception("Could not load gift management settings")
    try:
        cfg["base_star_rate_toman"] = max(1, int(cfg.get("base_star_rate_toman", 1000)))
    except (TypeError, ValueError):
        cfg["base_star_rate_toman"] = 1000
    try:
        cfg["profit_percent"] = max(0, min(1000, int(cfg.get("profit_percent", 20))))
    except (TypeError, ValueError):
        cfg["profit_percent"] = 20
    try:
        cfg["usd_toman_manual"] = max(0, int(cfg.get("usd_toman_manual", 0)))
    except (TypeError, ValueError):
        cfg["usd_toman_manual"] = 0
    for key in ("manual_delivery_all", "customer_delivery_choice", "support_first"):
        cfg[key] = bool(cfg.get(key))
    # Gift auto-pricing is now controlled only by the central pricing center.
    # Keep the old database fields for compatibility, but never let them override it.
    try:
        cfg["auto_rate_enabled"] = bool(load_auto_pricing_config().get("enabled"))
    except Exception:
        cfg["auto_rate_enabled"] = False
    cfg["usd_per_star_override"] = str(cfg.get("usd_per_star_override") or "").strip()
    cfg["support_text"] = str(cfg.get("support_text") or GIFT_DEFAULT_SETTINGS["support_text"])
    return cfg


def save_gift_settings(cfg):
    current = load_gift_settings()
    current.update(cfg or {})
    with db() as con:
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (GIFT_SETTINGS_KEY, json.dumps(current, ensure_ascii=False)),
        )
    return load_gift_settings()


def iran_timestamp_now():
    return utc_now().isoformat(timespec="seconds")


def log_order_event(con, order_id, event_key, status_text, details="", actor_user_id=None):
    con.execute(
        "INSERT INTO order_status_history(order_id,actor_user_id,event_key,status_text,details,created_at) VALUES(?,?,?,?,?,?)",
        (int(order_id), actor_user_id, str(event_key), str(status_text), str(details or ""), iran_timestamp_now()),
    )


def format_iran_jalali_datetime(value):
    """Display stored UTC/ISO or SQLite timestamps in Jalali date + Asia/Tehran clock."""
    if not value:
        return "ثبت نشده"
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(IRAN_TZ)
        jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
        return f"{persian_digits(f'{jy:04d}/{jm:02d}/{jd:02d}')} | {persian_digits(local.strftime('%H:%M:%S'))} ایران"
    except Exception:
        logger.warning("Could not format stored timestamp: %r", value, exc_info=True)
        return "زمان نامشخص"


def normalize_optional_username(value):
    value = str(value or "").strip()
    if value.lower() in {"ندارم", "ندارد", "ثبت نشده", "خالی", "-", "none", "no", "ندارم."}:
        return ""
    if value.startswith("https://t.me/"):
        value = "@" + value.rsplit("/", 1)[-1]
    if value and not value.startswith("@"):
        value = "@" + value
    if value and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
        return None
    return value


def _central_stars_quote_for_quantity(star_count, auto_cfg=None):
    """Gift/internal Stars quote using the exact central FX, fee and margin rules."""
    cfg = auto_cfg or load_auto_pricing_config()
    try:
        amount = int(star_count)
    except (TypeError, ValueError):
        return None
    if not 1 <= amount <= 1_000_000 or not auto_pricing_ready(cfg):
        return None
    prices = cfg.get("catalog_prices") or {}
    unit_usd = _pricing_decimal(prices.get("stars_price_per_star_usd"))
    fx = _pricing_decimal(cfg.get("fx_rate_toman"))
    if unit_usd is None or fx is None:
        return None
    try:
        margin = Decimal(str(cfg.get("margin_percent", "40")))
        fee_percent = Decimal(str(cfg.get("fee_percent", "0")))
        if (not margin.is_finite() or not fee_percent.is_finite()
                or margin < 0 or margin > 500 or fee_percent < 0 or fee_percent > 100):
            return None
        cost_usd = unit_usd * Decimal(amount)
        cost_toman = _ceil_toman(cost_usd * fx)
        fee_toman = _ceil_toman(Decimal(cost_toman) * fee_percent / Decimal(100)) if fee_percent else 0
        sale_toman = _ceil_toman(Decimal(cost_toman + fee_toman) * (Decimal(100) + margin) / Decimal(100))
        return {
            "quantity": amount, "cost_toman": cost_toman, "fee_toman": fee_toman,
            "sale_toman": sale_toman, "margin_percent": str(margin),
            "fee_percent": str(fee_percent),
        }
    except (InvalidOperation, TypeError, ValueError):
        return None


def gift_base_unit_price(settings=None):
    """Current central Stars cost per unit; legacy gift-specific rates are not used."""
    cfg = load_auto_pricing_config()
    if not auto_pricing_ready(cfg):
        return None
    prices = cfg.get("catalog_prices") or {}
    usd = _pricing_decimal(prices.get("stars_price_per_star_usd"))
    fx = _pricing_decimal(cfg.get("fx_rate_toman"))
    return usd * fx if usd is not None and fx is not None else None


def gift_unit_sale_price(settings=None):
    quote = _central_stars_quote_for_quantity(1)
    return Decimal(quote["sale_toman"]) if quote else None


def gift_price_for_stars(star_count, settings=None):
    quote = _central_stars_quote_for_quantity(star_count)
    return int(quote["sale_toman"]) if quote else None

def sync_gift_prices_in_db():
    if not load_auto_pricing_config().get("enabled"):
        return 0
    settings = load_gift_settings()
    unit_price = gift_unit_sale_price(settings)
    if unit_price is None:
        return 0
    changed = 0
    with db() as con:
        rows = con.execute("SELECT id, star_count FROM products WHERE lower(category)='gifts' AND source_type='official' AND star_count>0").fetchall()
        for row in rows:
            price = gift_price_for_stars(row["star_count"], settings)
            if price is not None:
                con.execute("UPDATE products SET price=?, currency='تومان' WHERE id=?", (price, row["id"]))
                changed += 1
    return changed


def _extract_api_object_value(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def telegram_bot_api_call(bot, method_name, **params):
    """Prefer PTB's typed methods; use its authenticated API transport as fallback."""
    method_map = {
        "getAvailableGifts": "get_available_gifts",
        "getMyStarBalance": "get_my_star_balance",
        "sendGift": "send_gift",
        "giftPremiumSubscription": "gift_premium_subscription",
    }
    method = getattr(bot, method_map.get(method_name, ""), None)
    if callable(method):
        return await method(**params)
    post = getattr(bot, "_post", None)
    if callable(post):
        return await post(method_name, data=params or {})
    raise RuntimeError(f"روش {method_name} در نسخهٔ کتابخانهٔ تلگرام در دسترس نیست.")


async def get_real_bot_star_balance(bot):
    result = await telegram_bot_api_call(bot, "getMyStarBalance")
    amount = _extract_api_object_value(result, "amount")
    if amount is None and isinstance(result, dict):
        amount = result.get("amount")
    if amount is None:
        raise RuntimeError("پاسخ تلگرام فیلد موجودی Stars ندارد.")
    return int(amount)


def gift_admin_home_text(catalog_count, active_count, balance_text, note=""):
    settings = load_gift_settings()
    auto_cfg = load_auto_pricing_config()
    unit = gift_unit_sale_price(settings)
    unit_text = f"{toman(unit)} تومان" if unit is not None else "در دسترس نیست؛ منبع مرکزی را بررسی کن"
    manual_text = "روشن" if settings["manual_delivery_all"] else "خاموش"
    customer_text = "روشن" if settings["customer_delivery_choice"] else "خاموش"
    support_text = "روشن" if settings["support_first"] else "خاموش"
    updated = format_iran_jalali_datetime(settings.get("catalog_last_sync")) if settings.get("catalog_last_sync") else "هنوز همگام‌سازی نشده"
    fx_ok = bool(auto_cfg.get("fx_valid") and _pricing_timestamp_fresh(auto_cfg.get("fx_source_updated_at"), _PRICING_FX_MAX_AGE_SECONDS))
    fx_fresh = bool(fx_ok and _pricing_timestamp_fresh(auto_cfg.get("fx_source_updated_at"), _PRICING_MAX_AGE_SECONDS))
    fx_label = "✅ تازه" if fx_fresh else ("⚠️ آخرین نرخ تأییدشده (پشتیبان)" if fx_ok else "❌ نرخ معتبر نیست")
    fx_rate_value = _pricing_decimal(auto_cfg.get("fx_rate_toman"))
    fx_rate_display = f"{toman(fx_rate_value)} تومان" if fx_rate_value is not None else "ثبت نشده/نامعتبر"
    lines = [
        "🎁 مدیریت گیفت تلگرام | SEYED PREMIUM",
        "━━━━━━━━━━━━━━━━━━",
        f"📚 تعداد گیفت‌های شناسایی‌شده: {catalog_count}",
        f"✅ گیفت‌های فعال برای فروش: {active_count}",
        f"💱 نرخ تتر مرکزی: {fx_rate_display} | {fx_label}",
        f"📈 سود مرکزی: {auto_cfg.get('margin_percent')}٪ | کارمزد: {auto_cfg.get('fee_percent')}٪",
        f"💵 قیمت نهایی محاسباتی هر ⭐: {unit_text}",
        f"🌐 منبع نرخ: {auto_cfg.get('fx_source_name') or 'تأیید نشده'}",
        f"🕒 آخرین نرخ: {_pricing_time_text(auto_cfg.get('fx_source_updated_at'))}",
        f"✋ تحویل دستی همه: {manual_text}",
        f"🧑‍💻 انتخاب روش ارسال توسط مشتری: {customer_text}",
        f"📩 پیام به پشتیبانی قبل از تحویل: {support_text}",
        f"⭐ موجودی واقعی Stars ربات: {balance_text}",
        f"🕒 آخرین همگام‌سازی کاتالوگ: {updated}",
        "",
        "نرخ، هزینه، سود و کارمزد گیفت‌های رسمی از مرکز یکپارچهٔ قیمت‌گذاری خوانده می‌شود.",
    ]
    if settings.get("catalog_last_error"):
        lines.extend(["", "⚠️ آخرین خطای کاتالوگ: " + str(settings["catalog_last_error"])[:250]])
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def gift_admin_home_keyboard():
    settings = load_gift_settings()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 به‌روزرسانی فهرست از تلگرام", callback_data="gift_sync", style="success")],
        [InlineKeyboardButton("⚙️ مدیریت گیفت‌ها (فعال/غیرفعال)", callback_data="gift_catalog_page_1", style="primary")],
        [InlineKeyboardButton(f"🔵 حالت تحویل دستی همه: {'روشن' if settings['manual_delivery_all'] else 'خاموش'}", callback_data="gift_global_manual", style="primary")],
        [InlineKeyboardButton("🟢 افزودن گیفت سفارشی (با لینک)", callback_data="gift_custom_add", style="success")],
        [InlineKeyboardButton("🟢 افزایش موجودی استارز ربات (مشترک خدمات)", callback_data="gift_star_topup", style="success")],
        [InlineKeyboardButton("🔵 انتخاب روش ارسال توسط مشتری: " + ("روشن" if settings['customer_delivery_choice'] else "خاموش"), callback_data="gift_customer_choice", style="primary")],
        [InlineKeyboardButton("✋ مدیریت روش تحویل تک‌تک گیفت‌ها", callback_data="gift_delivery_page_1", style="primary")],
        [InlineKeyboardButton("💹 مدیریت نرخ، تتر، سود و کارمزد در مرکز اصلی", callback_data="admin_auto_pricing", style="success")],
        [InlineKeyboardButton("📩 «اول به پشتیبانی پیام بده»: " + ("روشن" if settings['support_first'] else "خاموش"), callback_data="gift_support_first", style="primary")],
        [InlineKeyboardButton("📝 متن پیام «اول به پشتیبانی پیام بده»", callback_data="gift_support_text", style="primary")],
        [InlineKeyboardButton("📋 سفارش‌های گیفت", callback_data="gift_orders", style="primary")],
        [InlineKeyboardButton("🔙 بازگشت به مدیریت قیمت‌گذاری", callback_data="admin_auto_prices", style="danger")],
    ])


def _gift_list_query(con):
    return con.execute(
        "SELECT id, name, price, active, currency, telegram_gift_id, star_count, source_type, delivery_mode, custom_link, catalog_available "
        "FROM products WHERE lower(category)='gifts' AND (source_type='custom' OR catalog_available=1 OR telegram_gift_id!='' OR star_count IS NOT NULL OR (source_type='manual' AND name NOT IN ('🎁 گیفت تلگرام','گیفت تلگرام'))) ORDER BY COALESCE(sort_order,0), id"
    ).fetchall()


def gift_catalog_page_text_and_markup(page=1, delivery=False):
    page = max(1, int(page))
    with db() as con:
        rows = _gift_list_query(con)
    per_page = 8
    total_pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, total_pages)
    visible = rows[(page - 1) * per_page: page * per_page]
    title = "✋ روش تحویل هر گیفت" if delivery else "⚙️ مدیریت گیفت‌ها"
    lines = [f"{title} | صفحه {page} از {total_pages}", "━━━━━━━━━━━━━━━━━━"]
    buttons = []
    for row in visible:
        stars = int(row["star_count"] or 0)
        price = int(row["price"] or 0)
        state = "✅ فعال" if int(row["active"] or 0) else "❌ غیرفعال"
        name = str(row["name"] or "گیفت")
        delivery_mode = str(row["delivery_mode"] or "manual")
        delivery_label = "✋ دستی" if delivery_mode != "auto" else "⚡ خودکار"
        source = "NFT/سفارشی" if row["source_type"] == "custom" else ("کاتالوگ تلگرام" if row["source_type"] == "official" else "محصول دستی")
        lines.append(f"\n{state} | {name}\n⭐ {stars if stars else '—'} | 💰 {toman(price)} {row['currency'] or 'تومان'}\nمنبع: {source} | روش تحویل: {delivery_label}")
        if delivery:
            allow_auto = row["source_type"] == "official" and bool(row["telegram_gift_id"])
            if allow_auto:
                buttons.append([InlineKeyboardButton(f"{name[:28]} | {'⚡ خودکار' if delivery_mode != 'auto' else '✋ دستی'}", callback_data=f"gift_delivery_toggle_{row['id']}")])
            else:
                buttons.append([InlineKeyboardButton(f"{name[:28]} | فقط تحویل دستی", callback_data=f"gift_delivery_manual_{row['id']}")])
        else:
            buttons.append([InlineKeyboardButton(f"{name[:30]} | {'فعال ✅' if row['active'] else 'فعال‌سازی 🟢'}", callback_data=f"gift_toggle_{row['id']}")])
    if not rows:
        lines.append("\nهنوز گیفتی در کاتالوگ ثبت نشده است. ابتدا فهرست را از تلگرام به‌روزرسانی کن.")
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"{'gift_delivery_page' if delivery else 'gift_catalog_page'}_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"{'gift_delivery_page' if delivery else 'gift_catalog_page'}_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 بازگشت به مدیریت گیفت", callback_data="gift_admin_home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def sync_official_gift_catalog(bot):
    """Pull the official Telegram catalogue; preserve admin settings for existing gift rows."""
    result = await telegram_bot_api_call(bot, "getAvailableGifts")
    gifts = _extract_api_object_value(result, "gifts", []) or []
    if not isinstance(gifts, (list, tuple)):
        raise RuntimeError("ساختار کاتالوگ تلگرام معتبر نیست.")
    parsed = []
    for gift in gifts:
        gift_id = str(_extract_api_object_value(gift, "id", "") or "").strip()
        star_count = _extract_api_object_value(gift, "star_count")
        sticker = _extract_api_object_value(gift, "sticker")
        emoji = str(_extract_api_object_value(sticker, "emoji", "🎁") or "🎁")
        if not gift_id or not str(star_count or "").isdigit() or int(star_count) <= 0:
            continue
        name = f"{emoji} گیفت رسمی تلگرام — {int(star_count)} ⭐"
        parsed.append((gift_id, int(star_count), emoji, name))
    if not parsed:
        raise RuntimeError("تلگرام کاتالوگ خالی یا فاقد شناسه/قیمت معتبر برگرداند؛ فهرست قبلی حفظ شد.")
    settings = load_gift_settings()
    with db() as con:
        existing_ids = set(str(r[0]) for r in con.execute("SELECT telegram_gift_id FROM products WHERE category='gifts' AND telegram_gift_id!=''").fetchall())
        for gift_id, star_count, emoji, name in parsed:
            old = con.execute("SELECT id FROM products WHERE category='gifts' AND telegram_gift_id=? LIMIT 1", (gift_id,)).fetchone()
            price = gift_price_for_stars(star_count, settings) or 0
            if old:
                # Name and Star count track Telegram; admin activation, delivery mode and ordering survive refresh.
                con.execute("UPDATE products SET name=?, star_count=?, price=?, currency='تومان', catalog_available=1, source_type='official', icon=? WHERE id=?",
                            (name, star_count, price, emoji, old["id"]))
            else:
                next_sort = con.execute("SELECT COALESCE(MAX(sort_order),0)+10 FROM products WHERE category='gifts'").fetchone()[0]
                con.execute(
                    "INSERT INTO products(category,name,price,active,button_text,icon,button_style,sort_order,description,currency,source_type,telegram_gift_id,star_count,catalog_available,delivery_mode) "
                    "VALUES('gifts',?,?,0,?,?,?,?,?,'تومان','official',?,?,1,'manual')",
                    (name, price, name, emoji, "success", next_sort, f"گیفت رسمی تلگرام؛ هزینهٔ ارسال {star_count} Stars.", gift_id, star_count),
                )
        current_ids = [item[0] for item in parsed]
        if current_ids:
            placeholders = ",".join("?" for _ in current_ids)
            con.execute(f"UPDATE products SET catalog_available=0 WHERE category='gifts' AND source_type='official' AND telegram_gift_id NOT IN ({placeholders})", current_ids)
    settings["catalog_last_sync"] = iran_timestamp_now()
    settings["catalog_last_error"] = ""
    save_gift_settings(settings)
    return len(parsed)


def parse_support_form(text_value):
    """Parse the one-click copied Persian support template, tolerating Persian digits."""
    raw = str(text_value or "").strip()
    norm = normalize_ascii_digits(raw)
    def field(*labels):
        for line in norm.splitlines():
            line_clean = line.strip()
            for label in labels:
                if line_clean.lower().startswith(label.lower()):
                    return line_clean[len(label):].lstrip(" :：\t").strip()
        return ""
    buyer_id = field("ID عددی خریدار", "آیدی عددی خریدار", "ID خریدار")
    recipient_id = field("ID عددی گیرنده", "آیدی عددی گیرنده", "ID گیرنده")
    buyer_username = field("آیدی تلگرام خریدار", "آیدی خریدار")
    recipient_username = field("آیدی تلگرام گیرنده", "آیدی گیرنده")
    product_name = field("نام کامل محصول", "نام محصول", "محصول")
    order_code = field("کد سفارش", "شماره سفارش")
    message_lines = []
    active = False
    for line in norm.splitlines():
        clean = line.strip()
        if ("متن پیام شما به پشتیبانی" in clean) or ("پیام شما به پشتیبانی" in clean) or clean.startswith("پیام شما"):
            active = True
            tail = clean.split(":", 1)[1].strip() if ":" in clean else ""
            if tail and tail.lower() not in {"وارد کنید", "اینجا بنویسید", "سلام"}:
                message_lines.append(tail)
            continue
        if active:
            if clean.startswith("👇 پیام خودت را در خط بعد بنویس"):
                continue
            message_lines.append(line.rstrip())
    message_text = "\n".join(message_lines).strip()
    placeholders = {"وارد کنید", "نام دقیق محصول، نوع و مقدار", "نام دقیق محصول، نوع و مقدار را بنویس", "@username", "@username یا ندارم", "@username/ندارم", "سلام، متن پیام خود را اینجا بنویسید.", "سلام، متن پیام را اینجا بنویس", "متن پیام خود را اینجا بنویسید", "ندارم"}
    if buyer_username in placeholders:
        buyer_username = ""
    if recipient_username in placeholders:
        recipient_username = ""
    if product_name in placeholders:
        product_name = ""
    if order_code in {"وارد کنید", "وارد کنید / ندارم", "ندارم", ""} or order_code.startswith("وارد کنید"):
        order_code = ""
    return {
        "buyer_id_entered": buyer_id,
        "buyer_username_entered": buyer_username,
        "recipient_id": recipient_id,
        "recipient_username": recipient_username,
        "product_name": product_name,
        "order_code": order_code,
        "message_text": message_text,
    }


def support_form_text(user):
    username = f"@{user.username}" if getattr(user, "username", None) else "ندارم"
    return (
        "💎 پشتیبانی SEYED PREMIUM\n\n"
        "برای بررسی سریع‌تر، متن زیر را کپی کن، قسمت‌های خالی را تکمیل کن و همین‌جا بفرست.\n\n"
        "👤 اطلاعات خریدار\n"
        f"ID عددی خریدار {user.id}\n"
        f"آیدی تلگرام خریدار {username}\n\n"
        "🎁 اطلاعات گیرنده\n"
        "ID عددی گیرنده وارد کنید\n"
        "آیدی تلگرام گیرنده @username یا ندارم\n\n"
        "📦 اطلاعات سفارش\n"
        "نام کامل محصول نام دقیق محصول، نوع و مقدار را بنویس\n"
        "کد سفارش وارد کنید / ندارم\n\n"
        "💬 پیام شما به پشتیبانی\n"
        "👇 پیام خودت را در خط بعد بنویس\n"
        "سلام، متن پیام خود را اینجا بنویس\n\n"
        "نکته: اگر خریدار و گیرنده یک نفر هستند، ID و آیدی همان حساب را در بخش گیرنده هم وارد کن."
    )


def support_form_copy_text(user):
    username = f"@{user.username}" if getattr(user, "username", None) else "ندارم"
    return (
        f"ID عددی خریدار {user.id}\n"
        f"آیدی تلگرام خریدار {username}\n"
        "ID عددی گیرنده وارد کنید\n"
        "آیدی تلگرام گیرنده @username/ندارم\n"
        "نام کامل محصول وارد کنید\n"
        "کد سفارش وارد کنید / ندارم\n"
        "متن پیام شما به پشتیبانی\n"
        "سلام، متن پیام را اینجا بنویس"
    )


def support_form_markup(copy_text):
    buttons = []
    try:
        if CopyTextButton is not None and len(copy_text) <= 256:
            buttons.append([InlineKeyboardButton("📋 کپی سریع فرم", copy_text=CopyTextButton(text=copy_text))])
    except Exception:
        logger.warning("Copy-text button not available in this PTB version", exc_info=True)
    buttons.append([InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="danger")])
    buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")])
    return InlineKeyboardMarkup(buttons)


async def create_support_ticket(update, context, parsed):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False
    buyer_entered = normalize_ascii_digits(parsed.get("buyer_id_entered", ""))
    # The actual Telegram sender ID is authoritative and cannot be overwritten by copied text.
    recipient_id_raw = normalize_ascii_digits(parsed.get("recipient_id", "")).strip()
    recipient_id = int(recipient_id_raw) if re.fullmatch(r"\d{5,20}", recipient_id_raw) else None
    recipient_username = normalize_optional_username(parsed.get("recipient_username", ""))
    if recipient_username is None:
        await message.reply_text("❌ آیدی تلگرام گیرنده معتبر نیست. آن را به شکل @username وارد کن یا بنویس ندارم.")
        return True
    product_name = str(parsed.get("product_name") or "").strip()
    order_code = str(parsed.get("order_code") or "").strip()
    message_text = str(parsed.get("message_text") or "").strip()
    if not re.fullmatch(r"\d{5,20}", buyer_entered):
        await message.reply_text("❌ ID عددی خریدار باید عددی باشد. برای شروع دوباره دکمه پشتیبانی را بزن.")
        return True
    if int(buyer_entered) != int(user.id):
        await message.reply_text("⚠️ ID خریدار باید همان ID عددی حسابی باشد که اکنون با آن به ربات پیام می‌دهی. فرم را با ID واقعی خودت تکمیل کن.")
        return True
    if not recipient_id:
        await message.reply_text("❌ ID عددی گیرنده را درست وارد کن. اگر خرید برای خودت است، ID عددی خودت را در بخش گیرنده هم وارد کن.")
        return True
    if not product_name or len(product_name) < 2 or product_name.lower() in {"وارد کنید", "نام دقیق محصول، نوع و مقدار"}:
        await message.reply_text("❌ نام کامل محصول را وارد کن؛ مثلاً «گیفت رسمی تلگرام — 25 Stars».")
        return True
    if (not message_text or len(message_text) < 2
            or "متن پیام خود را اینجا بنویسید" in message_text
            or "متن پیام را اینجا بنویسید" in message_text):
        await message.reply_text("❌ متن پیام خودت را در قسمت «پیام شما به پشتیبانی» بنویس و فرم را دوباره بفرست.")
        return True
    if len(order_code) > 64:
        await message.reply_text("❌ کد سفارش بیش از حد طولانی است.")
        return True

    order_id = None
    order_row = None
    with db() as con:
        if order_code:
            clean_code = order_code.strip().upper().replace("#", "")
            order_row = con.execute(
                "SELECT o.*, p.name AS joined_product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                "WHERE UPPER(o.order_code)=? OR CAST(o.id AS TEXT)=? ORDER BY o.id DESC LIMIT 1",
                (clean_code, clean_code),
            ).fetchone()
            if order_row and int(order_row["user_id"]) != int(user.id):
                order_row = None
        if not order_row:
            order_row = con.execute(
                "SELECT o.*, p.name AS joined_product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                "WHERE (o.user_id=? OR o.recipient_user_id=?) AND (lower(COALESCE(p.name,'')) LIKE lower(?) OR lower(COALESCE(o.product_name_snapshot,'')) LIKE lower(?)) "
                "ORDER BY o.id DESC LIMIT 1",
                (user.id, user.id, f"%{product_name[:40]}%", f"%{product_name[:40]}%"),
            ).fetchone()
        if order_row:
            order_id = int(order_row["id"])
            if not order_code:
                order_code = str(order_row["order_code"] or f"#{order_id}")
        created = iran_timestamp_now()
        cur = con.execute(
            "INSERT INTO support_tickets(ticket_code,sender_user_id,sender_username,sender_full_name,buyer_user_id,buyer_username,recipient_user_id,recipient_username,product_name,order_code,order_id,message_text,status,created_at,updated_at) "
            "VALUES('PENDING',?,?,?,?,?,?,?,?,?,?,?,'جدید',?,?)",
            (user.id, user.username or "", user.full_name or "", user.id, user.username or "", recipient_id,
             recipient_username or "", product_name, order_code, order_id, message_text, created, created),
        )
        ticket_id = int(cur.lastrowid)
        ticket_code = f"SP-T{ticket_id:06d}"
        con.execute("UPDATE support_tickets SET ticket_code=? WHERE id=?", (ticket_code, ticket_id))
        con.execute(
            "INSERT INTO support_ticket_messages(ticket_id,sender_user_id,sender_role,message_text,created_at) VALUES(?,?, 'customer', ?, ?)",
            (ticket_id, user.id, message_text, created),
        )
        con.execute(
            "INSERT INTO support_active_tickets(user_id,ticket_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET ticket_id=excluded.ticket_id,updated_at=excluded.updated_at",
            (user.id, ticket_id, created),
        )
    order_info = "سفارش مرتبط پیدا نشد؛ مدیر می‌تواند با ID یا کد سفارش جست‌وجو کند."
    if order_row:
        order_info = (
            f"کد سفارش: {order_row['order_code'] or ('#' + str(order_id))}\n"
            f"نام محصول ثبت‌شده: {order_row['joined_product_name'] or order_row['product_name_snapshot'] or 'ثبت نشده'}\n"
            f"زمان واقعی سفارش: {format_iran_jalali_datetime(order_row['created_at'])}\n"
            f"وضعیت سفارش: {order_row['status']}\n"
            f"مبلغ ثبت‌شده: {toman(order_row['price'])} {order_row['currency'] or 'تومان'}"
        )
    admin_text = (
        f"📩 تیکت جدید پشتیبانی | {ticket_code}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 فرستندهٔ واقعی (تلگرام): {user.full_name}\n"
        f"ID عددی فرستنده: {user.id}\n"
        f"آیدی تلگرام فرستنده: {'@' + user.username if user.username else 'تنظیم نشده'}\n\n"
        "👤 خریدار\n"
        f"ID عددی خریدار: {user.id}\n"
        f"آیدی تلگرام خریدار: {'@' + user.username if user.username else 'تنظیم نشده'}\n\n"
        "🎁 گیرنده\n"
        f"ID عددی گیرنده: {recipient_id}\n"
        f"آیدی تلگرام گیرنده: {recipient_username or 'ثبت نشده'}\n\n"
        "📦 سفارش\n"
        f"نام کامل محصول اعلام‌شده: {product_name}\n"
        f"کد سفارش اعلام‌شده: {order_code or 'ندارد'}\n"
        f"زمان دریافت تیکت: {format_iran_jalali_datetime(created)}\n"
        f"{order_info}\n\n"
        "💬 پیام مشتری\n" + message_text
    )
    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 مشاهده پرونده تیکت", callback_data=f"admin_ticket_view_{ticket_id}")],
        [InlineKeyboardButton("↩️ پاسخ به مشتری", callback_data=f"admin_ticket_reply_{ticket_id}")],
        [InlineKeyboardButton("📦 بررسی سفارش", callback_data=f"admin_ticket_order_{ticket_id}")],
    ])
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text[:3900], reply_markup=admin_keyboard)
    except Exception:
        logger.exception("Support ticket %s saved but admin notification failed", ticket_code)
        context.user_data.pop("support_form_active", None)
        with db() as con:
            con.execute("DELETE FROM support_form_sessions WHERE user_id=?", (user.id,))
        await message.reply_text(f"⚠️ تیکت {ticket_code} ذخیره شد، اما ارسال اعلان به مدیریت ناموفق بود. مدیر می‌تواند از پنل تیکت‌ها آن را ببیند.")
        return True
    await message.reply_text(
        f"✅ پیام شما با موفقیت ثبت و برای مدیریت ارسال شد.\nشماره پیگیری: {ticket_code}\nوضعیت: در انتظار بررسی",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 ادامه ارسال پیام در همین تیکت", callback_data=f"support_ticket_continue_{ticket_id}")],
            [InlineKeyboardButton("✅ پایان گفت‌وگو", callback_data="support_ticket_close")],
            [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
        ])
    )
    context.user_data.pop("support_form_active", None)
    with db() as con:
        con.execute("DELETE FROM support_form_sessions WHERE user_id=?", (user.id,))
    return True


def support_message_attachment(message):
    """Return a Telegram file id and Bot API media type for supported support attachments."""
    if getattr(message, "photo", None):
        return message.photo[-1].file_id, "photo"
    for attr, kind in (("document", "document"), ("video", "video"), ("animation", "animation"),
                       ("audio", "audio"), ("voice", "voice"), ("video_note", "video_note"),
                       ("sticker", "sticker")):
        obj = getattr(message, attr, None)
        if obj is not None:
            return obj.file_id, kind
    return "", ""


async def deliver_support_attachment_to_admin(bot, file_id, media_type, caption):
    """Send a stored customer attachment to the admin chat using its real Telegram file_id."""
    method = getattr(bot, f"send_{media_type}", None)
    if not callable(method):
        raise RuntimeError(f"ارسال نوع پیوست {media_type} در این نسخه در دسترس نیست.")
    argument_name = {"photo":"photo", "document":"document", "video":"video", "animation":"animation",
                     "audio":"audio", "voice":"voice", "video_note":"video_note", "sticker":"sticker"}.get(media_type)
    if not argument_name:
        raise RuntimeError("نوع پیوست پشتیبانی نمی‌شود.")
    kwargs = {"chat_id": ADMIN_ID, argument_name: file_id}
    if media_type not in {"video_note", "sticker"} and caption:
        kwargs["caption"] = caption[:1000]
    await method(**kwargs)
    if media_type in {"video_note", "sticker"} and caption:
        await bot.send_message(chat_id=ADMIN_ID, text=caption[:3500])


async def append_support_customer_message(update, context, ticket_id):
    """Persist and route a follow-up text/media message to the open customer ticket."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False
    attachment_id, media_type = support_message_attachment(message)
    text_value = (message.text or message.caption or "").strip()
    if not attachment_id and not text_value:
        await message.reply_text("این نوع پیام در تیکت پشتیبانی پشتیبانی نمی‌شود. متن، عکس، ویدئو یا فایل بفرست.")
        return True
    now = iran_timestamp_now()
    with db() as con:
        ticket = con.execute("SELECT ticket_code,status FROM support_tickets WHERE id=? AND sender_user_id=?", (ticket_id, user.id)).fetchone()
        if not ticket or ticket["status"] == "بسته‌شده":
            con.execute("DELETE FROM support_active_tickets WHERE user_id=?", (user.id,))
            await message.reply_text("این تیکت بسته شده است. برای درخواست تازه، دوباره دکمه پشتیبانی را بزن.")
            return True
        con.execute(
            "INSERT INTO support_ticket_messages(ticket_id,sender_user_id,sender_role,message_text,telegram_file_id,media_type,created_at) VALUES(?,?,'customer',?,?,?,?)",
            (ticket_id, user.id, text_value, attachment_id, media_type, now),
        )
        con.execute("UPDATE support_tickets SET updated_at=? WHERE id=?", (now, ticket_id))
    admin_caption = (
        f"💬 پیام تازه مشتری | {ticket['ticket_code']}\n"
        f"نام: {user.full_name}\nID فرستنده: {user.id}\n"
        f"آیدی: {'@'+user.username if user.username else 'تنظیم نشده'}\n"
        f"زمان ایران: {format_iran_jalali_datetime(now)}\n\n"
        + (text_value or ("📎 پیوست مشتری" if attachment_id else "پیام بدون متن"))
    )
    try:
        if attachment_id:
            await deliver_support_attachment_to_admin(context.bot, attachment_id, media_type, admin_caption)
        else:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_caption[:3900])
        await message.reply_text(f"✅ پیام به تیکت {ticket['ticket_code']} اضافه و برای مدیریت ارسال شد.")
    except Exception as exc:
        logger.exception("Support follow-up saved but admin delivery failed: ticket=%s", ticket["ticket_code"])
        await message.reply_text("⚠️ پیام در سوابق تیکت ذخیره شد، اما ارسال اعلان به مدیریت ناموفق بود. مدیر می‌تواند از داخل تیکت، پیوست‌ها را دوباره دریافت کند.\n" + str(exc)[:150])
    return True


def support_ticket_detail(con, ticket_id):
    ticket = con.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
    if not ticket:
        return None
    order = None
    if ticket["order_id"]:
        order = con.execute(
            "SELECT o.*, p.name AS product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id=?",
            (ticket["order_id"],),
        ).fetchone()
    messages = con.execute("SELECT * FROM support_ticket_messages WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()
    lines = [
        f"📩 پرونده پشتیبانی {ticket['ticket_code']}",
        f"وضعیت تیکت: {ticket['status']}",
        f"تاریخ و ساعت دریافت: {format_iran_jalali_datetime(ticket['created_at'])}",
        "━━━━━━━━━━━━━━━━━━",
        "👤 خریدار",
        f"نام نمایشی: {ticket['sender_full_name'] or 'ثبت نشده'}",
        f"ID عددی: {ticket['buyer_user_id']}",
        f"آیدی تلگرام: @{ticket['buyer_username']}" if ticket['buyer_username'] else "آیدی تلگرام: تنظیم نشده",
        "🎁 گیرنده",
        f"ID عددی: {ticket['recipient_user_id'] or 'ثبت نشده'}",
        f"آیدی تلگرام: {ticket['recipient_username'] or 'ثبت نشده'}",
        "📦 سفارش اعلام‌شده",
        f"نام کامل محصول: {ticket['product_name'] or 'ثبت نشده'}",
        f"کد سفارش: {ticket['order_code'] or 'ثبت نشده'}",
    ]
    if order:
        lines.extend([
            "", "📄 سفارش واقعی مرتبط",
            f"کد سفارش: {order['order_code'] or ('#' + str(order['id']))}",
            f"نام محصول ثبت‌شده: {order['product_name'] or 'ثبت نشده'}",
            f"مبلغ: {toman(order['price'])} {order['currency'] or 'تومان'}",
            f"وضعیت: {order['status']}",
            f"زمان ثبت واقعی: {format_iran_jalali_datetime(order['created_at'])}",
            f"زمان ثبت رسید: {format_iran_jalali_datetime(order['receipt_submitted_at'])}",
            f"زمان تأیید پرداخت: {format_iran_jalali_datetime(order['payment_verified_at'])}",
            f"زمان تحویل: {format_iran_jalali_datetime(order['delivered_at'])}",
        ])
    lines.extend(["", "💬 تاریخچه گفتگو"])
    for msg in messages[-12:]:
        role = "مشتری" if msg["sender_role"] == "customer" else "مدیریت"
        lines.append(f"\n{role} | {format_iran_jalali_datetime(msg['created_at'])}")
        lines.append(str(msg["message_text"] or ("رسانه پیوست شده" if msg["telegram_file_id"] else "پیام بدون متن"))[:700])
        if msg["telegram_file_id"]:
            lines.append("📎 پیوست: " + str(msg["media_type"] or "رسانه"))
    return "\n".join(lines)


def create_support_ticket_keyboard(ticket_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ پاسخ به مشتری", callback_data=f"admin_ticket_reply_{ticket_id}"),
         InlineKeyboardButton("📦 سفارش", callback_data=f"admin_ticket_order_{ticket_id}")],
        [InlineKeyboardButton("🟡 در حال بررسی", callback_data=f"admin_ticket_status_review_{ticket_id}")],
        [InlineKeyboardButton("✅ پاسخ داده شد", callback_data=f"admin_ticket_status_replied_{ticket_id}"),
         InlineKeyboardButton("📁 بستن تیکت", callback_data=f"admin_ticket_status_closed_{ticket_id}")],
        [InlineKeyboardButton("📎 ارسال دوباره پیوست‌ها به مدیریت", callback_data=f"admin_ticket_attachments_{ticket_id}")],
        [InlineKeyboardButton("🔙 فهرست تیکت‌ها", callback_data="admin_support_tickets")],
    ])


async def _show_gift_admin_home(query, bot, note=""):
    with db() as con:
        catalog_count = int(con.execute("SELECT COUNT(*) FROM products WHERE category='gifts' AND source_type='official' AND catalog_available=1").fetchone()[0])
        active_count = int(con.execute("SELECT COUNT(*) FROM products WHERE category='gifts' AND active=1 AND (source_type='custom' OR catalog_available=1 OR telegram_gift_id!='')").fetchone()[0])
    try:
        balance_text = f"⭐ {toman(await get_real_bot_star_balance(bot))}"
    except Exception as exc:
        logger.warning("Could not query real bot Stars balance", exc_info=True)
        balance_text = "دریافت نشد (" + str(exc)[:90] + ")"
    await query.edit_message_text(gift_admin_home_text(catalog_count, active_count, balance_text, note), reply_markup=gift_admin_home_keyboard())


async def _create_owner_star_invoice(update_or_query, context, amount):
    """Owner-only XTR invoice. Stars are recorded only after Telegram's successful_payment update."""
    actor = update_or_query.effective_user if hasattr(update_or_query, "effective_user") else update_or_query.from_user
    message = update_or_query.effective_message if hasattr(update_or_query, "effective_message") else update_or_query.message
    if not actor or actor.id != ADMIN_ID:
        if hasattr(update_or_query, "answer"):
            await update_or_query.answer("فقط مدیر اصلی می‌تواند موجودی Stars ربات را شارژ کند.", show_alert=True)
        return
    if not isinstance(amount, int) or not 1 <= amount <= 100000:
        await message.reply_text("تعداد Stars باید بین ۱ تا ۱۰۰٬۰۰۰ باشد.")
        return
    payload = f"SP-GIFT-TOPUP-{actor.id}-{int(time.time())}-{amount}"
    created = iran_timestamp_now()
    with db() as con:
        con.execute("INSERT INTO gift_star_topups(payload,admin_user_id,amount,status,created_at) VALUES(?,?,?,'pending',?)",
                    (payload, actor.id, amount, created))
    if LabeledPrice is None or PreCheckoutQueryHandler is None:
        await message.reply_text("❌ نسخهٔ کتابخانهٔ تلگرام برای پرداخت امن Stars آماده نیست. ابتدا python-telegram-bot را به نسخهٔ سازگار با Bot API به‌روزرسانی کن.")
        with db() as con:
            con.execute("UPDATE gift_star_topups SET status='unsupported' WHERE payload=?", (payload,))
        return
    await context.bot.send_invoice(
        chat_id=actor.id,
        title="شارژ موجودی استارز ربات",
        description=f"افزودن {amount} ⭐ به موجودی ربات برای ارسال گیفت‌های رسمی و خدمات پشتیبانی‌شده.",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} Telegram Stars", amount=amount)],
    )


async def handle_owner_star_precheckout(update, context):
    query = update.pre_checkout_query
    try:
        with db() as con:
            row = con.execute("SELECT admin_user_id,amount,status FROM gift_star_topups WHERE payload=?", (query.invoice_payload,)).fetchone()
        valid = bool(row and int(row["admin_user_id"]) == int(query.from_user.id) == int(ADMIN_ID)
                     and int(row["amount"]) == int(query.total_amount) and query.currency == "XTR"
                     and row["status"] == "pending")
        await query.answer(ok=valid, error_message=None if valid else "این فاکتور معتبر یا قابل استفاده نیست. دوباره از پنل مدیریت شروع کن.")
    except Exception:
        logger.exception("Owner Stars top-up pre-checkout validation failed")
        await query.answer(ok=False, error_message="بررسی فاکتور با خطا روبه‌رو شد؛ لطفاً دوباره تلاش کن.")


async def handle_owner_star_success(update, context):
    message = update.effective_message
    user = update.effective_user
    payment = getattr(message, "successful_payment", None) if message else None
    if not payment or not user:
        return
    payload = str(payment.invoice_payload or "")
    with db() as con:
        row = con.execute("SELECT * FROM gift_star_topups WHERE payload=?", (payload,)).fetchone()
        if not row or int(row["admin_user_id"]) != int(user.id) or int(user.id) != int(ADMIN_ID):
            await message.reply_text("⚠️ پرداخت دریافت شد اما فاکتور شارژ موجودی در سیستم پیدا نشد؛ لطفاً با مدیریت بررسی کن.")
            return
        if payment.currency != "XTR" or int(payment.total_amount) != int(row["amount"]):
            con.execute("UPDATE gift_star_topups SET status='mismatch' WHERE payload=?", (payload,))
            await message.reply_text("⚠️ مبلغ یا واحد پرداخت با فاکتور مطابقت ندارد؛ موجودی محلی تغییر نکرد.")
            return
        if row["status"] == "paid":
            await message.reply_text("این پرداخت قبلاً ثبت شده است؛ دوباره محاسبه نشد.")
            return
        con.execute("UPDATE gift_star_topups SET status='paid',telegram_charge_id=?,paid_at=? WHERE payload=?",
                    (payment.telegram_payment_charge_id or "", iran_timestamp_now(), payload))
    try:
        balance = await get_real_bot_star_balance(context.bot)
        balance_text = f"⭐ {toman(balance)}"
    except Exception as exc:
        balance_text = "پرداخت با موفقیت ثبت شد؛ دریافت موجودی فعلی از API ناموفق بود: " + str(exc)[:100]
    await message.reply_text(
        f"✅ پرداخت {int(payment.total_amount)} ⭐ با تأیید تلگرام ثبت شد.\n{balance_text}\n\nاین موجودی برای روش‌های رسمی ارسال پشتیبانی‌شده استفاده می‌شود؛ پرداخت Stars به‌تنهایی به معنی موجودی قابل‌خرج برای هر نوع محصول نیست."
    )


async def try_deliver_official_gift(context, order_id):
    """Attempt only an actual official sendGift call; keep order pending on every failure."""
    with db() as con:
        order = con.execute(
            "SELECT o.*,p.name AS product_name,p.category,p.source_type,p.telegram_gift_id,p.delivery_mode AS product_delivery_mode,p.custom_link "
            "FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id=?",
            (order_id,),
        ).fetchone()
    if not order:
        return {"ok": False, "manual": True, "reason": "سفارش پیدا نشد."}
    if not order or str(order["category"] or "").lower() != "gifts":
        return {"ok": False, "manual": True, "reason": "محصول از نوع گیفت نیست."}
    if order["delivered_at"]:
        return {"ok": True, "manual": False, "already_delivered": True, "delivered_at": order["delivered_at"]}
    if not order["payment_verified_at"]:
        return {"ok": False, "manual": True, "reason": "پرداخت این سفارش هنوز تأیید نشده است."}
    settings = load_gift_settings()
    mode = str(order["delivery_mode"] or order["product_delivery_mode"] or "manual")
    if settings["manual_delivery_all"] or mode != "auto":
        return {"ok": False, "manual": True, "reason": "روش تحویل این سفارش دستی است."}
    if order["source_type"] != "official" or not order["telegram_gift_id"]:
        return {"ok": False, "manual": True, "reason": "این محصول گیفت رسمی قابل ارسال خودکار نیست."}
    recipient_id = order["recipient_user_id"]
    if not recipient_id:
        return {"ok": False, "manual": True, "reason": "ID عددی گیرنده ثبت نشده است."}
    try:
        await telegram_bot_api_call(context.bot, "sendGift", user_id=int(recipient_id), gift_id=str(order["telegram_gift_id"]))
    except Exception as exc:
        logger.exception("Official Telegram gift delivery failed for order #%s", order_id)
        reason = str(exc)[:400]
        with db() as con:
            con.execute("UPDATE orders SET status='پرداخت تأیید شد - در انتظار تحویل',delivery_error=? WHERE id=?", (reason, order_id))
        return {"ok": False, "manual": False, "reason": reason}
    delivered_at = iran_timestamp_now()
    with db() as con:
        con.execute("UPDATE orders SET status='تحویل‌شده',delivered_at=?,delivery_error='' WHERE id=?", (delivered_at, order_id))
    return {"ok": True, "manual": False, "delivered_at": delivered_at}


async def try_deliver_official_premium(context, order_id):
    """Gift Premium using Telegram's official Bot API, only once after verified payment."""
    with db() as con:
        order = con.execute(
            "SELECT o.*,p.name AS product_name,p.category FROM orders o "
            "LEFT JOIN products p ON p.id=o.product_id WHERE o.id=?", (order_id,)
        ).fetchone()
    if not order:
        return {"ok": False, "manual": True, "reason": "سفارش پیدا نشد."}
    if str(order["category"] or "").lower() != "premium":
        return {"ok": False, "manual": True, "reason": "این سفارش از نوع پرمیوم نیست."}
    if order["delivered_at"]:
        return {"ok": True, "manual": False, "already_delivered": True, "delivered_at": order["delivered_at"]}
    if not order["payment_verified_at"]:
        return {"ok": False, "manual": True, "reason": "پرداخت هنوز توسط مدیر تأیید نشده است."}
    if str(order["delivery_mode"] or "manual") != "auto":
        return {"ok": False, "manual": True, "reason": "روش تحویل این سفارش دستی است."}
    if not order["recipient_user_id"]:
        return {"ok": False, "manual": True, "reason": "ID عددی گیرنده ثبت نشده است؛ تحویل خودکار ممکن نیست."}
    months = premium_product_months(order["product_name"])
    if months not in PREMIUM_PLAN_STARS:
        return {"ok": False, "manual": True, "reason": "مدت پلن از نام محصول قابل تشخیص نیست."}
    required_stars = PREMIUM_PLAN_STARS[months]
    try:
        # Check real balance before reserving the one-shot send attempt. A low balance
        # must not permanently block the order after the admin tops up later.
        balance = await get_real_bot_star_balance(context.bot)
        if balance < required_stars:
            reason = f"موجودی واقعی ربات کافی نیست؛ موجودی {balance} ⭐ و نیاز این پلن {required_stars} ⭐ است."
            with db() as con:
                con.execute("UPDATE orders SET status='پرداخت تأیید شد - در انتظار تحویل',delivery_error=? WHERE id=?", (reason, order_id))
            return {"ok": False, "manual": False, "reason": reason}
        # Reserve immediately before the network call. If the outcome is ambiguous
        # (timeout/crash), no automatic duplicate send is attempted.
        with db() as con:
            latest = con.execute("SELECT delivery_started_at,delivered_at FROM orders WHERE id=?", (order_id,)).fetchone()
            if not latest or latest["delivered_at"]:
                return {"ok": bool(latest and latest["delivered_at"]), "manual": False, "already_delivered": bool(latest and latest["delivered_at"])}
            if latest["delivery_started_at"]:
                return {"ok": False, "manual": False, "reason": "برای جلوگیری از ارسال تکراری، تلاش قبلی نیازمند بررسی دستی است."}
            cur = con.execute("UPDATE orders SET delivery_started_at=? WHERE id=? AND delivery_started_at IS NULL", (iran_timestamp_now(), order_id))
            if cur.rowcount != 1:
                return {"ok": False, "manual": False, "reason": "سفارش هم‌زمان در حال ارسال است؛ برای جلوگیری از ارسال تکراری بررسی دستی لازم است."}
        result = await telegram_bot_api_call(
            context.bot, "giftPremiumSubscription", user_id=int(order["recipient_user_id"]),
            month_count=int(months), star_count=int(required_stars),
        )
        if result is not True and not bool(_extract_api_object_value(result, "ok", False)):
            raise RuntimeError("Telegram Bot API ارسال پرمیوم را تأیید نکرد.")
    except Exception as exc:
        logger.exception("Official Telegram Premium delivery failed for order #%s", order_id)
        reason = str(exc)[:400]
        with db() as con:
            con.execute("UPDATE orders SET status='پرداخت تأیید شد - نیازمند بررسی تحویل',delivery_error=? WHERE id=?", (reason, order_id))
        return {"ok": False, "manual": False, "reason": reason}
    delivered_at = iran_timestamp_now()
    with db() as con:
        con.execute("UPDATE orders SET status='تحویل‌شده',delivered_at=?,delivery_error='' WHERE id=?", (delivered_at, order_id))
    return {"ok": True, "manual": False, "delivered_at": delivered_at}


def gift_orders_page(page=1):
    page = max(1, int(page))
    with db() as con:
        total = int(con.execute("SELECT COUNT(*) FROM orders o JOIN products p ON p.id=o.product_id WHERE lower(p.category)='gifts'").fetchone()[0])
        if total == 0:
            return "🧾 سفارش‌هایِ گیفت\n\nهنوز سفارشی ثبت نشده.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="gift_admin_home", style="danger")]])
        per_page = 5
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        rows = con.execute(
            "SELECT o.*,p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id WHERE lower(p.category)='gifts' ORDER BY o.id DESC LIMIT ? OFFSET ?",
            (per_page, (page-1)*per_page),
        ).fetchall()
    lines = [f"🧾 سفارش‌هایِ گیفت | صفحه {page} از {total_pages}", "━━━━━━━━━━━━━━━━━━"]
    buttons = []
    for row in rows:
        buyer_at = "@" + row["buyer_username"] if row["buyer_username"] else "تنظیم نشده"
        recipient_at = row["recipient_username"] or "ثبت نشده"
        code = row["order_code"] or f"#{row['id']}"
        lines.extend([
            f"\n🧾 کد سفارش: {code}",
            f"🎁 نام کامل محصول: {row['product_name_snapshot'] or row['product_name']}",
            f"💰 مبلغ: {toman(row['price'])} {row['currency'] or 'تومان'}",
            f"👤 خریدار — ID: {row['user_id']} | {buyer_at}",
            f"🎯 گیرنده — ID: {row['recipient_user_id'] or 'ثبت نشده'} | {recipient_at}",
            f"📅 زمان ثبت سفارش: {format_iran_jalali_datetime(row['created_at'])}",
            f"📌 وضعیت: {row['status']}",
        ])
        buttons.append([InlineKeyboardButton(f"🔎 جزئیات {code}", callback_data=f"gift_order_view_{row['id']}")])
    nav=[]
    if page > 1: nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"gift_orders_page_{page-1}"))
    if page < total_pages: nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"gift_orders_page_{page+1}"))
    if nav: buttons.append(nav)
    buttons.extend([[InlineKeyboardButton("🔎 جست‌وجوی سفارش", callback_data="gift_order_search")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data="gift_admin_home", style="danger")]])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(buttons)


def gift_order_detail_text(row):
    buyer_at = "@" + row["buyer_username"] if row["buyer_username"] else "تنظیم نشده"
    recipient_at = row["recipient_username"] or "ثبت نشده"
    with db() as con:
        history = con.execute(
            "SELECT event_key,status_text,details,created_at FROM order_status_history WHERE order_id=? ORDER BY id DESC LIMIT 12",
            (int(row["id"]),),
        ).fetchall()
    history_lines = []
    for event in reversed(history):
        detail = (" — " + str(event["details"])[:120]) if event["details"] else ""
        history_lines.append(
            f"• {format_iran_jalali_datetime(event['created_at'])}: {event['status_text']}{detail}"
        )
    history_text = "\n".join(history_lines) if history_lines else "هنوز رویداد دیگری ثبت نشده است."
    text_value = (
        f"🎁 جزئیات سفارش گیفت {row['order_code'] or ('#'+str(row['id']))}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"نام کامل محصول: {row['product_name_snapshot'] or row['product_name'] or 'ثبت نشده'}\n"
        f"مبلغ ثبت‌شده: {toman(row['price'])} {row['currency'] or 'تومان'}\n"
        f"وضعیت پرداخت/سفارش: {row['status']}\n"
        f"روش تحویل سفارش: {'خودکار' if row['delivery_mode']=='auto' else 'دستی'}\n"
        f"تاریخ و ساعت سفارش: {format_iran_jalali_datetime(row['created_at'])}\n"
        f"زمان ارسال رسید: {format_iran_jalali_datetime(row['receipt_submitted_at'])}\n"
        f"زمان تأیید پرداخت: {format_iran_jalali_datetime(row['payment_verified_at'])}\n"
        f"زمان تحویل واقعی: {format_iran_jalali_datetime(row['delivered_at'])}\n\n"
        "👤 اطلاعات خریدار\n"
        f"ID عددی خریدار: {row['user_id']}\n"
        f"آیدی تلگرام خریدار: {buyer_at}\n\n"
        "🎯 اطلاعات گیرنده\n"
        f"ID عددی گیرنده: {row['recipient_user_id'] or 'ثبت نشده'}\n"
        f"آیدی تلگرام گیرنده: {recipient_at}\n\n"
        f"📝 جزئیات واردشده: {row['order_details'] or 'ثبت نشده'}\n"
        f"🧾 رسید: {'ثبت شده' if row['receipt_file_id'] else 'ثبت نشده'}\n"
        f"⚠️ خطای تحویل: {row['delivery_error'] or 'ندارد'}\n\n"
        "🕒 تاریخچه رویدادهای سفارش\n" + history_text
    )
    return text_value[:3900]


def register_user(user):
    with db() as con:
        con.execute(
            """
            INSERT INTO users(user_id, username, joined_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id)
            DO UPDATE SET username=excluded.username
            """,
            (user.id, user.username or ""),
        )


# ==========================================
# قیمت‌گذاری زندهٔ استارز و پرمیوم
# ==========================================

FIXED_STAR_AMOUNTS = (50, 75, 100, 150, 250, 350, 500, 1000, 2500, 5000, 10000, 50000)
_AUTO_PRICING_KEY = "auto_pricing_v1"
_AUTO_PRICING_CACHE = None
_AUTO_PRICING_REFRESH_LOCK = None
_PROVIDER_PRODUCTS_URL = "https://pay.tgsuperstars.com/api/partner/v1/products"
# Free, read-only public price quote endpoint; no API key and no purchase requests are made.
_PUBLIC_PRICE_API_BASE = "https://api-stars.duckdns.org/api/v1/prices/quote"
_PROVIDER_REFRESH_COOLDOWN_SECONDS = 1800  # Stars/Premium reference catalog: every 30 minutes.
_PROVIDER_FORCE_REFRESH_COOLDOWN_SECONDS = 5
_FX_REFRESH_COOLDOWN_SECONDS = 30 * 60  # Official Nobitex public FX API: refresh every 30 minutes.
_PRICING_MAX_AGE_SECONDS = 35 * 60  # A scheduled 30-minute quote remains fresh until the next cycle.
# A brief exchange/API outage must not erase the last good value, but never use
# an old FX quote for new orders after this short safety grace window.
_PRICING_FX_MAX_AGE_SECONDS = 40 * 60
_PRICING_CATALOG_MAX_AGE_SECONDS = 24 * 3600


def _auto_pricing_defaults():
    return {
        "enabled": True,
        "provider_api_key": "",
        "margin_percent": "40",
        "fee_percent": "0",
        "refresh_interval_minutes": 30,
        "catalog_prices": {"stars_price_per_star_usd": "", "stars_max_per_order": 50000, "premium_usd": {}},
        "catalog_valid": False,
        "catalog_updated_at": "",
        "premium_catalog_valid": None,
        "premium_catalog_updated_at": "",
        "premium_catalog_error": "قیمت‌های مرجع پرمیوم هنوز دریافت نشده‌اند.",
        "catalog_attempted_at": "",
        "catalog_error": "قیمت عمومی هنوز دریافت نشده است.",
        "fx_rate_toman": "",
        "fx_valid": False,
        "fx_source_updated_at": "",
        "fx_source_name": "",
        "fx_source_symbol": "",
        "fx_fetched_at": "",
        "fx_attempted_at": "",
        "fx_error": "نرخ تتر هنوز دریافت نشده است.",
        "usd_market_rate_toman": "",
        "usd_market_valid": False,
        "usd_market_source_updated_at": "",
        "usd_market_source_name": "",
        "usd_market_error": "نرخ دلار بازار آزاد هنوز دریافت نشده است.",
    }


def load_auto_pricing_config():
    """Keep pricing preferences and last quotes inside the existing shop.db only."""
    global _AUTO_PRICING_CACHE
    if _AUTO_PRICING_CACHE is not None:
        return deepcopy(_AUTO_PRICING_CACHE)
    defaults = _auto_pricing_defaults()
    stored = None
    try:
        with db() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (_AUTO_PRICING_KEY,)).fetchone()
        if row:
            stored = json.loads(row["value"])
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        logger.warning("Auto-pricing settings could not be loaded", exc_info=True)
    cfg = dict(defaults)
    if isinstance(stored, dict):
        cfg.update(stored)
    # Discard obsolete signed Nobitex credentials from older bot versions.
    for obsolete_key in (
        "nobitex_api_public_key", "nobitex_api_private_key", "nobitex_api_verified_at",
        "fx_custom_api_url", "fx_custom_json_path", "fx_custom_unit", "fx_custom_api_key",
    ):
        cfg.pop(obsolete_key, None)
    price_defaults = defaults["catalog_prices"]
    price_stored = cfg.get("catalog_prices")
    cfg["catalog_prices"] = dict(price_defaults, **(price_stored if isinstance(price_stored, dict) else {}))
    # Sanitize values used by the admin pricing screens. Older provider responses
    # may store a decimal/text max-order field; never let that crash a menu.
    try:
        raw_max = Decimal(str(cfg["catalog_prices"].get("stars_max_per_order") or 50000))
        max_order = int(raw_max) if raw_max.is_finite() else 50000
        if not 50 <= max_order <= 1_000_000:
            max_order = 50000
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        max_order = 50000
    cfg["catalog_prices"]["stars_max_per_order"] = max_order
    # A malformed cached FX value is not a usable rate. Keep it out of price math
    # and show an unavailable marker instead of raising ValueError in the gift menu.
    normalized_fx = _pricing_decimal(cfg.get("fx_rate_toman"))
    cfg["fx_rate_toman"] = str(normalized_fx) if normalized_fx is not None else ""
    # The product owner requested a fixed 30-minute refresh; normalize legacy values.
    cfg["refresh_interval_minutes"] = 30
    # The store's USDT rate is API-only; older manual-rate settings are ignored.
    legacy_pricing_keys = {
        "nobitex_api_public_key", "nobitex_api_private_key", "nobitex_api_verified_at",
        "fx_custom_api_url", "fx_custom_json_path", "fx_custom_unit", "fx_custom_api_key",
    }
    legacy_was_present = isinstance(stored, dict) and any(key in stored for key in legacy_pricing_keys)
    for name, lower, upper in (("margin_percent", Decimal("0"), Decimal("500")), ("fee_percent", Decimal("0"), Decimal("100"))):
        try:
            value = Decimal(str(cfg.get(name, "0")))
            if not value.is_finite() or value < lower or value > upper:
                raise InvalidOperation
            cfg[name] = str(value)
        except (InvalidOperation, ValueError, TypeError):
            cfg[name] = defaults[name]
    _AUTO_PRICING_CACHE = cfg
    if stored is None or legacy_was_present:
        save_auto_pricing_config(cfg)
    return deepcopy(cfg)


def save_auto_pricing_config(cfg):
    global _AUTO_PRICING_CACHE
    defaults = _auto_pricing_defaults()
    normalized = dict(defaults)
    if isinstance(cfg, dict):
        normalized.update(cfg)
    catalog_prices = normalized.get("catalog_prices")
    normalized["catalog_prices"] = dict(defaults["catalog_prices"], **(catalog_prices if isinstance(catalog_prices, dict) else {}))
    normalized["provider_api_key"] = str(normalized.get("provider_api_key") or "").strip()
    normalized["refresh_interval_minutes"] = 30
    for obsolete_key in (
        "nobitex_api_public_key", "nobitex_api_private_key", "nobitex_api_verified_at",
        "fx_custom_api_url", "fx_custom_json_path", "fx_custom_unit", "fx_custom_api_key",
    ):
        normalized.pop(obsolete_key, None)
    for name, lower, upper in (("margin_percent", Decimal("0"), Decimal("500")), ("fee_percent", Decimal("0"), Decimal("100"))):
        try:
            value = Decimal(str(normalized.get(name, "0")))
            if not value.is_finite() or value < lower or value > upper:
                raise InvalidOperation
            normalized[name] = str(value)
        except (InvalidOperation, TypeError, ValueError):
            normalized[name] = defaults[name]
    with db() as con:
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_AUTO_PRICING_KEY, json.dumps(normalized, ensure_ascii=False)),
        )
    _AUTO_PRICING_CACHE = normalized
    return deepcopy(normalized)


def _pricing_decimal(value):
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None


def _pricing_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _pricing_timestamp_fresh(value, max_age_seconds=_PRICING_MAX_AGE_SECONDS):
    parsed = _pricing_time(value)
    if parsed is None:
        return False
    age = (utc_now() - parsed).total_seconds()
    return -60 <= age <= max_age_seconds


def _pricing_timestamp_age(value):
    parsed = _pricing_time(value)
    if parsed is None:
        return None
    age = (utc_now() - parsed).total_seconds()
    return age if age >= 0 else 0


def _effective_fx_rate(cfg=None):
    cfg = cfg or load_auto_pricing_config()
    return _pricing_decimal(cfg.get("fx_rate_toman"))


def _effective_fx_is_usable(cfg=None):
    cfg = cfg or load_auto_pricing_config()
    return bool(
        cfg.get("fx_valid")
        and _pricing_timestamp_fresh(cfg.get("fx_source_updated_at"), _PRICING_FX_MAX_AGE_SECONDS)
        and _effective_fx_rate(cfg)
    )


def auto_pricing_ready(cfg=None):
    cfg = cfg or load_auto_pricing_config()
    return bool(
        cfg.get("enabled")
        and cfg.get("catalog_valid")
        and _effective_fx_is_usable(cfg)
        and _pricing_timestamp_fresh(cfg.get("catalog_updated_at"), _PRICING_CATALOG_MAX_AGE_SECONDS)
        and _pricing_decimal((cfg.get("catalog_prices") or {}).get("stars_price_per_star_usd"))
    )


def _ceil_toman(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_CEILING))


def auto_pricing_quote(product_type, quantity, cfg=None, allow_stale=False):
    """Return a quote; allow_stale is only for clearly labelled display previews, never orders."""
    cfg = cfg or load_auto_pricing_config()
    fx_rate = _effective_fx_rate(cfg)
    fx_is_fresh = _effective_fx_is_usable(cfg)
    fx_ready = bool(cfg.get("enabled") and fx_rate and (fx_is_fresh or allow_stale))
    if not fx_ready:
        return None
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return None
    prices = cfg.get("catalog_prices") or {}
    if product_type == "stars":
        if not 50 <= quantity <= 1_000_000:
            return None
        catalog_is_fresh = bool(cfg.get("catalog_valid") and _pricing_timestamp_fresh(cfg.get("catalog_updated_at"), _PRICING_CATALOG_MAX_AGE_SECONDS))
        if not (catalog_is_fresh or allow_stale):
            return None
        unit_usd = _pricing_decimal(prices.get("stars_price_per_star_usd"))
        if unit_usd is None:
            return None
        cost_usd = unit_usd * Decimal(quantity)
        try:
            raw_upstream_max = Decimal(str(prices.get("stars_max_per_order") or 50000))
            upstream_max = int(raw_upstream_max) if raw_upstream_max.is_finite() else 50000
            if not 50 <= upstream_max <= 1_000_000:
                upstream_max = 50000
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            upstream_max = 50000
    elif product_type == "premium":
        if quantity not in (3, 6, 12):
            return None
        premium_catalog_fresh = _premium_catalog_is_fresh(cfg)
        if not (premium_catalog_fresh or allow_stale):
            return None
        cost_usd = _pricing_decimal((prices.get("premium_usd") or {}).get(str(quantity)))
        if cost_usd is None:
            return None
        upstream_max = None
    else:
        return None

    fx_rate = _effective_fx_rate(cfg)
    if fx_rate is None:
        return None
    try:
        margin_percent = Decimal(str(cfg.get("margin_percent", "40")))
        fee_percent = Decimal(str(cfg.get("fee_percent", "0")))
        if not margin_percent.is_finite() or not fee_percent.is_finite():
            return None
    except (InvalidOperation, ValueError, TypeError):
        return None

    cost_toman = _ceil_toman(cost_usd * fx_rate)
    fee_toman = _ceil_toman(Decimal(cost_toman) * fee_percent / Decimal(100)) if fee_percent else 0
    sale_toman = _ceil_toman(Decimal(cost_toman + fee_toman) * (Decimal(100) + margin_percent) / Decimal(100))
    profit_toman = sale_toman - cost_toman - fee_toman
    return {
        "product_type": product_type,
        "quantity": quantity,
        "cost_usd": str(cost_usd),
        "fx_rate_toman": str(fx_rate),
        "cost_toman": cost_toman,
        "fee_toman": fee_toman,
        "sale_toman": sale_toman,
        "profit_toman": profit_toman,
        "margin_percent": str(margin_percent),
        "fee_percent": str(fee_percent),
        "provider_max_per_order": upstream_max,
        "provider_over_limit": bool(product_type == "stars" and upstream_max and quantity > upstream_max),
        "catalog_updated_at": cfg.get("catalog_updated_at", ""),
        "fx_source_updated_at": cfg.get("fx_source_updated_at", ""),
        "is_stale": bool(allow_stale and (not fx_is_fresh or (product_type == "stars" and not catalog_is_fresh) or (product_type == "premium" and not premium_catalog_fresh))),
    }


def auto_pricing_unit_sale(cfg=None):
    """Approximate per-Star retail price to two decimals; totals are rounded separately."""
    cfg = cfg or load_auto_pricing_config()
    if not auto_pricing_ready(cfg):
        return None
    prices = cfg.get("catalog_prices") or {}
    unit = _pricing_decimal(prices.get("stars_price_per_star_usd"))
    fx = _effective_fx_rate(cfg)
    if unit is None or fx is None:
        return None
    try:
        fee = Decimal(str(cfg.get("fee_percent", "0")))
        margin = Decimal(str(cfg.get("margin_percent", "40")))
        value = unit * fx * (Decimal(100) + fee) / Decimal(100) * (Decimal(100) + margin) / Decimal(100)
        return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    except (InvalidOperation, ValueError, TypeError):
        return None


def premium_product_months(product):
    try:
        text = normalize_ascii_digits(str(product["name"] or ""))
    except (KeyError, TypeError, IndexError):
        text = normalize_ascii_digits(str(product or ""))
    match = re.search(r"(?<!\d)(3|6|12)\s*(?:ماه(?:ه)?|months?|mo\b)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


# ==========================================
# مدیریت اختصاصی قیمت و تحویل پرمیوم
# Independent from Gift and general Stars pricing settings.
# ==========================================

PREMIUM_MANAGER_KEY = "premium_manager_settings_v1"
PREMIUM_PLAN_STARS = {3: 1000, 6: 1500, 12: 2500}
PREMIUM_MANAGER_DEFAULTS = {
    "delivery_modes": {"3": "manual", "6": "manual", "12": "manual"},
    "star_rate_toman": 1000,
    "auto_star_rate_enabled": False,
    "auto_fragment_enabled": False,
    "fragment_manual_usd": {},
    "base_star_usd_manual": "",
    "usd_toman_manual": 0,
    "profit_percent": "20",
    "plan_price_toman_manual": {},
}


def load_premium_manager_settings():
    defaults = deepcopy(PREMIUM_MANAGER_DEFAULTS)
    stored = None
    try:
        with db() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (PREMIUM_MANAGER_KEY,)).fetchone()
        if row:
            stored = json.loads(row["value"])
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        logger.warning("Premium manager settings could not be loaded", exc_info=True)
    cfg = deepcopy(defaults)
    if isinstance(stored, dict):
        cfg.update(stored)
    for key in ("delivery_modes", "fragment_manual_usd", "plan_price_toman_manual"):
        val = cfg.get(key)
        cfg[key] = dict(defaults[key], **(val if isinstance(val, dict) else {}))
    for months in ("3", "6", "12"):
        if cfg["delivery_modes"].get(months) not in {"manual", "auto"}:
            cfg["delivery_modes"][months] = "manual"
    try:
        rate = int(normalize_ascii_digits(str(cfg.get("star_rate_toman", 1000))).replace(",", "").replace("٬", ""))
        cfg["star_rate_toman"] = rate if 1 <= rate <= 100_000_000 else 1000
    except (TypeError, ValueError):
        cfg["star_rate_toman"] = 1000
    try:
        cfg["usd_toman_manual"] = int(normalize_ascii_digits(str(cfg.get("usd_toman_manual", 0))).replace(",", "").replace("٬", ""))
        if not 0 <= cfg["usd_toman_manual"] <= 100_000_000:
            cfg["usd_toman_manual"] = 0
    except (TypeError, ValueError):
        cfg["usd_toman_manual"] = 0
    for key, min_v, max_v in (("profit_percent", Decimal("0"), Decimal("1000")),):
        try:
            value = Decimal(str(cfg.get(key, defaults[key])))
            if not value.is_finite() or value < min_v or value > max_v:
                raise InvalidOperation
            cfg[key] = str(value)
        except (InvalidOperation, TypeError, ValueError):
            cfg[key] = defaults[key]
    for key in ("auto_star_rate_enabled", "auto_fragment_enabled"):
        cfg[key] = bool(cfg.get(key))
    cfg["base_star_usd_manual"] = str(cfg.get("base_star_usd_manual") or "").strip()
    if cfg["base_star_usd_manual"] and _pricing_decimal(cfg["base_star_usd_manual"]) is None:
        cfg["base_star_usd_manual"] = ""
    for group in ("fragment_manual_usd", "plan_price_toman_manual"):
        clean = {}
        for months in ("3", "6", "12"):
            value = cfg[group].get(months)
            if value in (None, "", 0, "0"):
                clean[months] = ""
                continue
            try:
                d = Decimal(str(normalize_ascii_digits(str(value)).replace(",", "").replace("٬", "")))
                clean[months] = str(d) if d.is_finite() and d > 0 else ""
            except (InvalidOperation, TypeError, ValueError):
                clean[months] = ""
        cfg[group] = clean
    return cfg


def save_premium_manager_settings(cfg):
    normalized = load_premium_manager_settings()
    if isinstance(cfg, dict):
        normalized.update(cfg)
    for key in ("delivery_modes", "fragment_manual_usd", "plan_price_toman_manual"):
        incoming = cfg.get(key) if isinstance(cfg, dict) else None
        if isinstance(incoming, dict):
            normalized[key] = dict(normalized[key], **incoming)
    with db() as con:
        con.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (PREMIUM_MANAGER_KEY, json.dumps(normalized, ensure_ascii=False)),
        )
    return normalized


def _premium_feed_parts(auto_cfg=None):
    cfg = auto_cfg or load_auto_pricing_config()
    prices = cfg.get("catalog_prices") or {}
    catalog_ok = bool(cfg.get("catalog_valid") and _pricing_timestamp_fresh(cfg.get("catalog_updated_at"), _PRICING_CATALOG_MAX_AGE_SECONDS))
    fx_ok = _effective_fx_is_usable(cfg)
    return cfg, prices, catalog_ok, fx_ok


def _premium_catalog_is_fresh(cfg):
    """Premium prices have their own validity clock, independent from Stars prices."""
    explicit_flag = cfg.get("premium_catalog_valid")
    if explicit_flag is None:  # compatibility with databases created before this split
        return bool(cfg.get("catalog_valid") and _pricing_timestamp_fresh(cfg.get("catalog_updated_at")))
    timestamp = cfg.get("premium_catalog_updated_at")
    return bool(explicit_flag and _pricing_timestamp_fresh(timestamp, _PRICING_CATALOG_MAX_AGE_SECONDS))


def premium_effective_star_rate_toman(settings=None, auto_cfg=None):
    settings = settings or load_premium_manager_settings()
    auto_cfg, prices, catalog_ok, fx_ok = _premium_feed_parts(auto_cfg)
    if settings.get("auto_star_rate_enabled"):
        star_usd = _pricing_decimal(settings.get("base_star_usd_manual")) if settings.get("base_star_usd_manual") else None
        if star_usd is None and catalog_ok:
            star_usd = _pricing_decimal(prices.get("stars_price_per_star_usd"))
        fx = _pricing_decimal(settings.get("usd_toman_manual")) or (_effective_fx_rate(auto_cfg) if fx_ok else None)
        if star_usd is None or fx is None:
            return None
        return (star_usd * fx).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if settings.get("base_star_usd_manual"):
        star_usd = _pricing_decimal(settings.get("base_star_usd_manual"))
        fx = _pricing_decimal(settings.get("usd_toman_manual")) or (_effective_fx_rate(auto_cfg) if fx_ok else None)
        if star_usd is not None and fx is not None:
            return (star_usd * fx).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    try:
        value = Decimal(str(settings.get("star_rate_toman", 1000)))
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def premium_price_quote(months, settings=None, auto_cfg=None):
    """Compute a Premium sale quote from one selected cost source; never fabricate stale rates."""
    try:
        months = int(months)
    except (TypeError, ValueError):
        return None
    if months not in PREMIUM_PLAN_STARS:
        return None
    settings = settings or load_premium_manager_settings()
    auto_cfg, prices, catalog_ok, fx_ok = _premium_feed_parts(auto_cfg)
    manual_final = _pricing_decimal(settings["plan_price_toman_manual"].get(str(months)))
    if manual_final is not None:
        amount = _ceil_toman(manual_final)
        return {"product_type": "premium", "quantity": months, "cost_toman": amount, "sale_toman": amount,
                "profit_toman": 0, "margin_percent": "0", "source": "قیمت اختصاصی مدیر", "manual_price": True}

    # The unified automatic-pricing center is the primary source for Premium too.
    # Existing legacy per-Premium settings remain stored and are used only as a
    # fallback when the central catalog/FX quote is not active or not fresh.
    central_quote = auto_pricing_quote("premium", months, auto_cfg)
    if central_quote is not None:
        central_quote["source"] = "مرکز مدیریت قیمت‌گذاری خودکار (کاتالوگ تأمین‌کننده × نرخ تتر)"
        central_quote["manual_price"] = False
        return central_quote
    if auto_cfg.get("enabled"):
        # Do not fall back to a legacy/default 1,000-toman Star rate when the
        # central engine is on but its current supplier/FX sources are invalid.
        return None

    fx = _pricing_decimal(settings.get("usd_toman_manual")) or (_effective_fx_rate(auto_cfg) if fx_ok else None)
    cost_toman = None
    source = ""
    if settings.get("auto_fragment_enabled"):
        if not (_premium_catalog_is_fresh(auto_cfg) and fx):
            return None
        fragment_usd = _pricing_decimal((prices.get("premium_usd") or {}).get(str(months)))
        if fragment_usd is None:
            return None
        cost_toman = fragment_usd * fx
        source = "قیمت زندهٔ مرجع Fragment از API واسط"
    else:
        manual_fragment = _pricing_decimal(settings["fragment_manual_usd"].get(str(months)))
        if manual_fragment is not None:
            if fx is None:
                return None
            cost_toman = manual_fragment * fx
            source = "قیمت دلاری دستی فرگمنت"
        else:
            unit_toman = premium_effective_star_rate_toman(settings, auto_cfg)
            if unit_toman is None:
                return None
            cost_toman = unit_toman * Decimal(PREMIUM_PLAN_STARS[months])
            source = "نرخ هر استارز × تعداد استارز پلن"

    try:
        margin = Decimal(str(settings.get("profit_percent", "20")))
        if not margin.is_finite() or margin < 0 or margin > 1000:
            return None
        cost = _ceil_toman(cost_toman)
        sale = _ceil_toman(Decimal(cost) * (Decimal(100) + margin) / Decimal(100))
        return {"product_type": "premium", "quantity": months, "cost_toman": cost,
                "sale_toman": sale, "profit_toman": sale - cost, "margin_percent": str(margin),
                "source": source, "manual_price": False,
                "catalog_updated_at": auto_cfg.get("catalog_updated_at", ""),
                "fx_source_updated_at": auto_cfg.get("fx_source_updated_at", "")}
    except (InvalidOperation, TypeError, ValueError):
        return None


def sync_premium_prices_in_db():
    if not load_auto_pricing_config().get("enabled"):
        return 0
    settings = load_premium_manager_settings()
    auto_cfg = load_auto_pricing_config()
    changed = 0
    with db() as con:
        rows = con.execute("SELECT id,name FROM products WHERE lower(category)='premium'").fetchall()
        for row in rows:
            months = premium_product_months(row["name"])
            quote = premium_price_quote(months, settings, auto_cfg) if months else None
            if quote:
                con.execute("UPDATE products SET price=?,currency='تومان' WHERE id=?", (int(quote["sale_toman"]), int(row["id"])))
                changed += 1
    return changed


def premium_delivery_label(months, settings=None):
    settings = settings or load_premium_manager_settings()
    mode = settings["delivery_modes"].get(str(months), "manual")
    return ("🔵 خودکار" if mode == "auto" else "🟢 دستی"), mode


def premium_admin_text(note=""):
    settings = load_premium_manager_settings()
    auto_cfg = load_auto_pricing_config()
    fx_ok = bool(auto_cfg.get("fx_valid") and _pricing_timestamp_fresh(auto_cfg.get("fx_source_updated_at"), _PRICING_FX_MAX_AGE_SECONDS))
    fx_fresh = bool(fx_ok and _pricing_timestamp_fresh(auto_cfg.get("fx_source_updated_at"), _PRICING_MAX_AGE_SECONDS))
    fx_label = "✅ تازه" if fx_fresh else ("⚠️ آخرین نرخ تأییدشده (پشتیبان)" if fx_ok else "❌ نرخ معتبر نیست")
    lines = [
        "🌟 مدیریت تلگرام پرمیوم | SEYED PREMIUM",
        "━━━━━━━━━━━━━━━━━━",
        "قیمت پایه، نرخ تتر، منبع قیمت و درصد سود از مرکز یکپارچهٔ قیمت‌گذاری مدیریت می‌شود.",
        f"💱 نرخ تتر: {toman(auto_cfg.get('fx_rate_toman') or 0)} تومان | {fx_label}",
        f"🌐 منبع نرخ: {auto_cfg.get('fx_source_name') or 'تأیید نشده'} ({auto_cfg.get('fx_source_symbol') or '—'})",
        f"🕒 آخرین نرخ: {_pricing_time_text(auto_cfg.get('fx_source_updated_at'))}",
        f"📈 سود مرکزی: {auto_cfg.get('margin_percent')}٪ | کارمزد مرکزی: {auto_cfg.get('fee_percent')}٪",
        "",
    ]
    for months, stars in PREMIUM_PLAN_STARS.items():
        quote = premium_price_quote(months, settings, auto_cfg)
        stale_preview = False
        if quote is None:
            quote = auto_pricing_quote("premium", months, auto_cfg, allow_stale=True)
            stale_preview = quote is not None
        mode_label, _ = premium_delivery_label(months, settings)
        manual_price = settings["plan_price_toman_manual"].get(str(months))
        if quote:
            price_text = f"{toman(quote['sale_toman'])} تومان" + (" ⚠️ آخرین نرخ ذخیره‌شده" if stale_preview else "")
        else:
            price_text = "قیمت هنوز ثبت نشده است"
        lines.append(f"• پرمیوم {months} ماهه ({toman(stars)} ⭐): {price_text} | {mode_label}")
        if manual_price:
            lines.append("  └ قیمت اختصاصی مدیر (اولویت بالاتر از محاسبهٔ خودکار)")
    lines.extend([
        "",
        "برای تغییر نرخ ارز، منبع کاتالوگ، سود و کارمزد از دکمهٔ «مدیریت نرخ و قیمت‌گذاری خودکار» استفاده کن.",
        "قیمت اختصاصی هر پلن همچنان از همین بخش قابل تنظیم است.",
        "تحویل خودکار فقط پس از تأیید پرداخت و کنترل موجودی واقعی Stars انجام می‌شود.",
    ])
    if not fx_ok and auto_cfg.get("fx_error"):
        lines.append("⚠️ وضعیت نرخ تتر: " + str(auto_cfg.get("fx_error"))[:180])
    if note:
        lines.extend(["", note])
    return "\n".join(lines)[:3900]


def premium_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ افزایش موجودی استارز ربات", callback_data="gift_star_topup", style="success")],
        [InlineKeyboardButton("⚙️ مدیریت تحویل پلن‌های ۳، ۶ و ۱۲ ماهه", callback_data="premium_delivery_menu", style="primary")],
        [InlineKeyboardButton("💹 مدیریت نرخ، قیمت مرجع و سود در مرکز قیمت‌گذاری", callback_data="admin_auto_pricing", style="success")],
        [InlineKeyboardButton("💰 قیمت شخصی برای ۳ ماهه", callback_data="premium_plan_price_3", style="primary")],
        [InlineKeyboardButton("💰 قیمت شخصی برای ۶ ماهه", callback_data="premium_plan_price_6", style="primary")],
        [InlineKeyboardButton("💰 قیمت شخصی برای ۱۲ ماهه", callback_data="premium_plan_price_12", style="primary")],
        [InlineKeyboardButton("🔙 بازگشت به مدیریت قیمت‌گذاری", callback_data="admin_auto_prices", style="danger")],
    ])


def premium_delivery_keyboard():
    settings = load_premium_manager_settings()
    buttons = []
    for months in (3, 6, 12):
        label, mode = premium_delivery_label(months, settings)
        color = "primary" if mode == "auto" else "success"
        buttons.append([InlineKeyboardButton(f"{label} | پرمیوم {months} ماهه", callback_data=f"premium_delivery_toggle_{months}", style=color)])
    buttons.extend([
        [InlineKeyboardButton("🟢 همه دستی", callback_data="premium_delivery_all_manual", style="success"),
         InlineKeyboardButton("🔵 همه خودکار", callback_data="premium_delivery_all_auto", style="primary")],
        [InlineKeyboardButton("🔙 بازگشت به مدیریت پرمیوم", callback_data="premium_admin_home", style="danger")],
    ])
    return InlineKeyboardMarkup(buttons)


def premium_delivery_text(note=""):
    settings = load_premium_manager_settings()
    lines = ["⚙️ مدیریت روش تحویل پرمیوم", "━━━━━━━━━━━━━━━━━━", "آبی = خودکار 🔵 | سبز = دستی 🟢", ""]
    for months, stars in PREMIUM_PLAN_STARS.items():
        label, mode = premium_delivery_label(months, settings)
        lines.append(f"{label} — پلن {months} ماهه ({toman(stars)} ⭐)")
    lines.extend(["", "انتخاب هر پلن فقط همان پلن را تغییر می‌دهد. دکمه‌های گروهی هر سه پلن را یکسان می‌کنند.",
                  "⚠️ تحویل خودکار فقط با موجودی کافی Stars و ID عددی معتبر گیرنده انجام می‌شود."])
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def _saved_stars_pack_quote(amount):
    """Fallback to the administrator-entered, persisted price for a fixed Stars pack."""
    try:
        with db() as con:
            row = con.execute("SELECT price FROM products WHERE lower(category)='stars' AND star_amount=? AND active=1 ORDER BY id LIMIT 1", (int(amount),)).fetchone()
        price = int(row["price"] or 0) if row else 0
        if price > 0:
            return {"product_type": "stars", "quantity": int(amount), "cost_toman": price,
                    "sale_toman": price, "profit_toman": 0, "margin_percent": "0",
                    "source": "قیمت دستی/ذخیره‌شدهٔ مدیر", "manual_price": True}
    except Exception:
        logger.debug("Could not load saved Stars pack price", exc_info=True)
    return None


def dynamic_product_quote(product, cfg=None):
    try:
        category = str(product["category"] or "").strip().lower()
    except (KeyError, TypeError, IndexError):
        return None
    quote = None
    if category == "stars":
        try:
            amount = product["star_amount"]
            if amount is not None:
                quote = auto_pricing_quote("stars", int(amount), cfg)
        except (KeyError, TypeError, ValueError, IndexError):
            quote = None
    elif category == "premium":
        months = premium_product_months(product)
        quote = premium_price_quote(months, auto_cfg=cfg) if months else None
    if quote is not None:
        return quote
    try:
        saved_price = int(product["price"] or 0)
        if saved_price > 0:
            return {"product_type": category, "quantity": 1, "cost_toman": saved_price,
                    "sale_toman": saved_price, "profit_toman": 0, "margin_percent": "0",
                    "source": "قیمت دستی/ذخیره‌شدهٔ مدیر", "manual_price": True}
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return None


def apply_auto_prices_to_products(cfg=None):
    """Sync only prices with a currently valid quote; Premium uses its isolated manager settings."""
    cfg = cfg or load_auto_pricing_config()
    updated = 0
    with db() as con:
        rows = con.execute(
            "SELECT id, category, name, star_amount FROM products "
            "WHERE active=1 AND lower(category) IN ('stars','premium')"
        ).fetchall()
        for row in rows:
            category = str(row["category"] or "").lower()
            if category == "stars" and not auto_pricing_ready(cfg):
                continue
            quote = dynamic_product_quote(row, cfg)
            if quote:
                con.execute("UPDATE products SET price=?, currency='تومان' WHERE id=?", (int(quote["sale_toman"]), int(row["id"])))
                updated += 1
    return updated


def _pricing_time_text(value):
    parsed = _pricing_time(value)
    return format_iran_jalali_datetime(parsed.isoformat(timespec="seconds")) if parsed else "ثبت نشده"


def pricing_customer_status_text(cfg=None):
    """Keep provider diagnostics private; customer menus show product prices only."""
    return ""


def _catalog_source_names(value):
    """Return a compact, de-duplicated list of catalogue source labels."""
    raw_parts = str(value or "").split(" + ")
    labels = []
    seen = set()
    for raw in raw_parts:
        part = str(raw or "").strip()
        if not part:
            continue
        low = part.casefold()
        if "tgsuperstars" in low:
            label = "TgSuperstars"
        elif "fragment" in low:
            label = "Fragment (مرجع عمومی)"
        else:
            label = part.split("؛", 1)[0][:48]
        key = label.casefold()
        if key not in seen:
            seen.add(key)
            labels.append(label)
    return " + ".join(labels[:3]) or "منبع عمومی مرجع"


def _fx_source_short(value):
    """Keep long upstream source descriptions out of the admin status message."""
    raw = str(value or "")
    low = raw.casefold()
    if "میانه" in raw or "median" in low:
        return "میانهٔ بازارهای معتبر"
    for token, label in (
        ("wallex", "Wallex"), ("tabdeal", "Tabdeal"), ("nobitex", "Nobitex"),
        ("exir", "Exir"), ("nerkhsanj", "نرخ‌سنج"), ("arzdigital", "ارزدیجیتال"),
        ("iran market", "Iran Market"), ("custom fx", "API مدیر"),
    ):
        if token in low:
            return label
    return (raw.split("؛", 1)[0][:52] if raw else "منبع عمومی بازار")


def pricing_panel_text(cfg=None, note=""):
    cfg = cfg or load_auto_pricing_config()
    ready = auto_pricing_ready(cfg)
    catalog_state = "✅ تازه/معتبر" if cfg.get("catalog_valid") and _pricing_timestamp_fresh(cfg.get("catalog_updated_at"), _PRICING_CATALOG_MAX_AGE_SECONDS) else "⚠️ نامعتبر/منقضی"
    fx_fresh = bool(cfg.get("fx_valid") and _pricing_timestamp_fresh(cfg.get("fx_source_updated_at"), _PRICING_MAX_AGE_SECONDS))
    fx_grace = bool(cfg.get("fx_valid") and _pricing_timestamp_fresh(cfg.get("fx_source_updated_at"), _PRICING_FX_MAX_AGE_SECONDS))
    fx_state = "✅ تازه" if fx_fresh else ("⚠️ آخرین نرخ تأییدشده" if fx_grace else "❌ نامعتبر")
    stored_fx = _pricing_decimal(cfg.get("fx_rate_toman"))
    fx_text = f"{toman(stored_fx)} تومان" if stored_fx is not None else "دریافت نشده"
    fx_ready = bool(cfg.get("enabled") and fx_grace and stored_fx is not None)
    stars_ready = bool(ready)
    premium_terms = (cfg.get("catalog_prices") or {}).get("premium_usd") or {}
    premium_terms_text = "، ".join(f"{term}ماهه" for term in ("3", "6", "12") if _pricing_decimal(premium_terms.get(term))) or "دریافت نشده"
    premium_ready = bool(fx_ready and _premium_catalog_is_fresh(cfg) and all(_pricing_decimal(premium_terms.get(str(m))) is not None for m in (3, 6, 12)))
    catalog_source = _catalog_source_names((cfg.get("catalog_prices") or {}).get("catalog_source"))
    fx_source = _fx_source_short(cfg.get("fx_source_name"))
    lines = [
        "💹 قیمت‌گذاری خودکار | SEYED PREMIUM",
        "━━━━━━━━━━━━━━━━━━",
        f"⭐ استارز: {'✅ آماده' if stars_ready else '⚠️ نیازمند بررسی'}  |  💎 پرمیوم: {'✅ آماده' if premium_ready else '⚠️ نیازمند بررسی'}",
        f"⚙️ سیستم: {'روشن' if cfg.get('enabled') else 'خاموش'}",
        "🔗 اتصال نرخ: API عمومی رسمی نوبیتکس | بدون API Key و Private Key",
        "━━━━━━━━━━━━━━━━━━",
        f"💱 نرخ تتر: {fx_text}  |  {fx_state}",
        f"🌐 منبع نرخ: {fx_source}",
        f"📊 مسیرهای API نوبیتکس: {cfg.get('fx_sources_ok', '—')} موفق / {cfg.get('fx_sources_failed', '—')} ناموفق",
        "💵 مبنای محاسبهٔ قیمت محصولات: نرخ تتر/تومان نوبیتکس",
        "━━━━━━━━━━━━━━━━━━",
        f"📦 کاتالوگ: {catalog_source}  |  {catalog_state}",
        f"🕒 آخرین دریافت کاتالوگ: {_pricing_time_text(cfg.get('catalog_updated_at'))}",
        f"💎 پلن‌های پرمیوم: {premium_terms_text}",
        f"📈 سود: {cfg.get('margin_percent')}٪  |  🧾 کارمزد: {cfg.get('fee_percent')}٪",
        "⏱️ بروزرسانی: نرخ تتر از API نوبیتکس هر ۳۰ دقیقه؛ کاتالوگ قیمت هر ۳۰ دقیقه",
        "ℹ️ منبع نرخ فقط API عمومی نوبیتکس است؛ هیچ کلید خصوصی یا دسترسی حساب استفاده نمی‌شود.",
    ]
    if fx_grace and not fx_fresh:
        lines.append("⚠️ نرخ تازه موقتاً در دسترس نیست؛ آخرین نرخ تأییدشده تا پایان مهلت اعتبار نگه داشته شده است.")
    if not ready:
        errors = []
        if cfg.get("catalog_error"):
            errors.append("کاتالوگ: " + str(cfg["catalog_error"])[:140])
        if cfg.get("fx_error"):
            errors.append("ارز: " + str(cfg["fx_error"])[:140])
        if errors:
            lines.append("\n⚠️ " + " | ".join(errors))
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def pricing_panel_keyboard(cfg=None):
    cfg = cfg or load_auto_pricing_config()
    enabled_label = "⏸ توقف قیمت‌گذاری" if cfg.get("enabled") else "▶️ فعال‌کردن قیمت‌گذاری"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💱 دریافت نرخ تتر از API عمومی نوبیتکس", callback_data="admin_pricing_refresh", style="success")],
        [InlineKeyboardButton("⭐ شارژ موجودی واقعی Stars ربات (مشترک خدمات پشتیبانی‌شده)", callback_data="gift_star_topup", style="success")],
        [InlineKeyboardButton("📈 تنظیم درصد سود", callback_data="admin_pricing_margin", style="primary"),
         InlineKeyboardButton("🧾 تنظیم کارمزد", callback_data="admin_pricing_fee", style="primary")],
        [InlineKeyboardButton("💰 ورود/ویرایش دستی قیمت همهٔ خدمات", callback_data="admin_products", style="primary")],
        [InlineKeyboardButton(enabled_label, callback_data="admin_pricing_toggle", style="danger" if cfg.get("enabled") else "success")],
        [InlineKeyboardButton("⬅️ بازگشت به مدیریت قیمت‌گذاری", callback_data="admin_auto_prices", style="primary")],
    ])


async def _finish_admin_pricing_refresh(query, status_prefix="", context=None):
    """Complete a potentially slow live refresh outside the button callback."""
    try:
        ready = await refresh_auto_pricing(force=True)
        cfg = load_auto_pricing_config()
        if not cfg.get("enabled") and _effective_fx_is_usable(cfg):
            refresh_note = "✅ نرخ عمومی تتر از API نوبیتکس دریافت و ذخیره شد؛ چون محاسبهٔ خودکار خاموش است، قیمت محصولات تغییر نکرد."
        else:
            refresh_note = (
                "✅ داده‌های تازه دریافت و اعتبارسنجی شدند." if ready
                else "⚠️ همهٔ نرخ‌های لازم تازه و معتبر نیستند؛ فروش با قیمت قدیمی مجاز نمی‌شود."
            )
        note = (status_prefix + "\n" if status_prefix else "") + refresh_note
        await safe_callback_edit_or_reply(query, pricing_panel_text(cfg, note), pricing_panel_keyboard(cfg))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Background admin pricing refresh failed")
        cfg = load_auto_pricing_config()
        await safe_callback_edit_or_reply(
            query, pricing_panel_text(cfg, f"❌ دریافت نرخ کامل نشد ({type(exc).__name__}). نرخ معتبر قبلی حفظ شد."),
            pricing_panel_keyboard(cfg),
        )
    finally:
        if context is not None:
            context.user_data.pop("_pricing_refresh_pending", None)


async def _fetch_public_price_catalog(client):
    """Read public, no-credential Fragment reference quotes; never creates a purchase."""
    async def get_quote(product, amount):
        response = await client.get(
            _PUBLIC_PRICE_API_BASE,
            params={"product": product, "amount": int(amount)},
            headers={"Accept": "application/json", "User-Agent": "SEYED-PREMIUM-price-check/2.0"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("پاسخ سرویس قیمت ساختار JSON معتبر ندارد.")
        quote = payload
        for key in ("data", "result", "quote"):
            candidate = quote.get(key) if isinstance(quote, dict) else None
            if isinstance(candidate, dict):
                quote = candidate
        stale = quote.get("stale", payload.get("stale", False))
        if stale is True or quote.get("unavailable") is True or payload.get("unavailable") is True:
            raise ValueError("منبع قیمت، دادهٔ قدیمی یا ناموجود اعلام کرد.")
        prices = quote.get("prices")
        if not isinstance(prices, dict):
            prices = quote
        value = None
        for key in ("usdt", "price_usdt", "unit_usdt", "priceUsdt", "usd"):
            value = _pricing_decimal(prices.get(key))
            if value is not None:
                break
        if value is None or value <= 0:
            raise ValueError("قیمت USDT در پاسخ مرجع موجود یا معتبر نیست.")
        # If the API provides a quote age, reject old prices even when stale is omitted.
        age = quote.get("age_sec", payload.get("age_sec"))
        if age is not None:
            try:
                age_seconds = float(age)
            except (TypeError, ValueError):
                raise ValueError("سن قیمت مرجع قابل‌تشخیص نیست.")
            if age_seconds < 0 or age_seconds > 3600:
                raise ValueError("قیمت مرجع بیش از یک ساعت عمر دارد.")
        return value

    requests = [("stars", 50), ("premium", 3), ("premium", 6), ("premium", 12)]
    results = await asyncio.gather(
        *(get_quote(product, amount) for product, amount in requests),
        return_exceptions=True,
    )
    stars_result = results[0]
    stars_error = ""
    if isinstance(stars_result, Exception):
        stars_error = f"{type(stars_result).__name__}: {str(stars_result)[:180]}"
        logger.warning("Public Fragment Stars quote unavailable: %s", stars_error)
        stars_price_per_star = ""
    else:
        stars_price_per_star = str(stars_result / Decimal(50))

    premium_prices = {}
    premium_errors = {}
    for months, result in zip((3, 6, 12), results[1:]):
        if isinstance(result, Exception):
            premium_errors[str(months)] = f"{type(result).__name__}: {str(result)[:180]}"
            logger.warning("Public Fragment Premium quote unavailable for %s months: %s", months, premium_errors[str(months)])
        else:
            premium_prices[str(months)] = str(result)
    return {
        "stars_price_per_star_usd": stars_price_per_star,
        "stars_max_per_order": 1000000,
        "premium_usd": premium_prices,
        "stars_quote_error": stars_error,
        "premium_quote_errors": premium_errors,
        "catalog_source": "سرویس مرجع غیررسمی قیمت Fragment (خواندن قیمت عمومی؛ بدون کلید و بدون خرید)",
    }


async def _fetch_tgsuperstars_price_catalog(client, api_key):
    """Optional second quote source for operators who configured a supplier key."""
    response = await client.get(
        _PROVIDER_PRODUCTS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("پاسخ تأمین‌کننده ساختار JSON معتبر ندارد.")
    catalog = payload.get("data", payload)
    payload_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    catalog_meta = catalog.get("meta") if isinstance(catalog, dict) and isinstance(catalog.get("meta"), dict) else {}
    stale = any((
        payload.get("unavailable") is True,
        isinstance(catalog, dict) and catalog.get("unavailable") is True,
        payload.get("stale") is True,
        isinstance(catalog, dict) and catalog.get("stale") is True,
        payload_meta.get("stale") is True,
        catalog_meta.get("stale") is True,
        str(payload.get("source_status", "")).lower() in {"stale", "unavailable"},
        isinstance(catalog, dict) and str(catalog.get("source_status", "")).lower() in {"stale", "unavailable"},
    ))
    if not isinstance(catalog, dict) or stale:
        raise ValueError("منبع تأمین‌کننده قیمت تازه ارائه نکرد.")
    stars_info = catalog.get("stars") or {}
    stars_currency = str(stars_info.get("currency") or catalog.get("currency") or "USD").upper()
    unit_price = _pricing_decimal(stars_info.get("price_per_star"))
    if stars_currency not in {"USD", "USDT"} or unit_price is None:
        raise ValueError("قیمت یا واحد پول استارز در منبع تأمین‌کننده معتبر نیست.")
    premium_raw = catalog.get("premium") or {}
    premium_prices = {}
    for months in (3, 6, 12):
        item = premium_raw.get(str(months)) or premium_raw.get(months)
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or catalog.get("currency") or "USD").upper()
        value = _pricing_decimal(item.get("partner_price"))
        if currency in {"USD", "USDT"} and value is not None:
            premium_prices[str(months)] = str(value)
    try:
        upstream_max = int(stars_info.get("max_per_order") or 50000)
    except (TypeError, ValueError):
        upstream_max = 50000
    return {
        "stars_price_per_star_usd": str(unit_price),
        "stars_max_per_order": max(50, upstream_max),
        "premium_usd": premium_prices,
        "catalog_source": "TgSuperstars (کلید API مدیر)",
    }


def _merge_quote_catalog(primary, fallback):
    """Use primary fields where present and safely fill missing fields from fallback."""
    result = dict(primary or {})
    fallback = fallback or {}
    if _pricing_decimal(result.get("stars_price_per_star_usd")) is None:
        result["stars_price_per_star_usd"] = fallback.get("stars_price_per_star_usd", "")
        result["stars_max_per_order"] = fallback.get("stars_max_per_order", result.get("stars_max_per_order", 1000000))
    premium = result.get("premium_usd") if isinstance(result.get("premium_usd"), dict) else {}
    fallback_premium = fallback.get("premium_usd") if isinstance(fallback.get("premium_usd"), dict) else {}
    premium = dict(premium)
    for month in ("3", "6", "12"):
        if _pricing_decimal(premium.get(month)) is None and _pricing_decimal(fallback_premium.get(month)) is not None:
            premium[month] = fallback_premium[month]
    result["premium_usd"] = premium
    result["catalog_source"] = _catalog_source_names(" + ".join(x for x in (result.get("catalog_source"), fallback.get("catalog_source")) if x))
    return result


async def _fetch_live_fx_bundle(client):
    """Get the USDT/Toman rate only from Nobitex's documented public REST API.

    The order book is primary; the public market-stats endpoint is a fallback.
    Neither endpoint requires an API token, API Key, or Private Key.
    """
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "SEYED-PREMIUM-FX/4.0",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    }

    async def get_json(url, label, params=None, extra_headers=None, follow_redirects=None):
        headers = dict(request_headers)
        if isinstance(extra_headers, dict):
            headers.update(extra_headers)
        request_options = {"params": params, "headers": headers}
        if follow_redirects is not None:
            request_options["follow_redirects"] = bool(follow_redirects)
        response = await client.get(url, **request_options)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ValueError(f"پاسخ {label} ساختار JSON ندارد.")
        if isinstance(payload, dict):
            payload["_observed_at"] = utc_now().isoformat(timespec="seconds")
        return payload

    def quote_record(rate, source, symbol, source_time=None, note=""):
        value = _pricing_decimal(rate)
        if value is None or value < Decimal("1000") or value > Decimal("100000000"):
            raise ValueError(f"نرخ {source} خارج از محدودهٔ معتبر تومان است.")
        observed = str(source_time or utc_now().isoformat(timespec="seconds"))
        if not _pricing_timestamp_fresh(observed, 15 * 60):
            raise ValueError(f"نرخ {source} بیش از ۱۵ دقیقه قدیمی است.")
        return {
            "rate": value,
            "updated_at": observed,
            "symbol": str(symbol),
            "source": str(source) + (f"؛ {note}" if note else ""),
        }

    async def fetch_nobitex_orderbook():
        # Official Nobitex public orderbook endpoint; no token or signature is required.
        payload = await get_json("https://apiv2.nobitex.ir/v3/orderbook/USDTIRT", "Nobitex orderbook")
        if not isinstance(payload, dict) or str(payload.get("status", "")).lower() != "ok":
            raise ValueError("پاسخ دفتر سفارش نوبیتکس معتبر نیست.")
        try:
            source_time = datetime.fromtimestamp(float(payload.get("lastUpdate")) / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError, OSError):
            raise ValueError("زمان به‌روزرسانی دفتر سفارش نوبیتکس موجود نیست.")
        if not _pricing_timestamp_fresh(source_time, 5 * 60):
            raise ValueError("دفتر سفارش نوبیتکس بیش از ۵ دقیقه قدیمی است.")
        bids, asks = payload.get("bids") or [], payload.get("asks") or []
        bid = _pricing_decimal(bids[0][0]) if bids and isinstance(bids[0], (list, tuple)) and bids[0] else None
        ask = _pricing_decimal(asks[0][0]) if asks and isinstance(asks[0], (list, tuple)) and asks[0] else None
        if bid is None or ask is None or ask < bid:
            raise ValueError("قیمت خرید/فروش دفتر سفارش نوبیتکس معتبر نیست.")
        spread = (ask - bid) / ((ask + bid) / Decimal(2))
        if spread > Decimal("0.08"):
            raise ValueError("فاصلهٔ دفتر سفارش نوبیتکس غیرعادی است.")
        # Nobitex API prices for IRT/RLS markets are returned in Rials.
        # Normalize to Toman before the central pricing engine consumes the quote.
        return quote_record(((bid + ask) / Decimal(2)) / Decimal(10), "Nobitex (دفتر سفارش USDTIRT؛ ریال ÷ ۱۰ = تومان)", "USDTIRT", source_time)

    async def fetch_nobitex_stats():
        payload = await get_json(
            "https://apiv2.nobitex.ir/market/stats", "Nobitex stats",
            params={"srcCurrency": "usdt", "dstCurrency": "rls"},
        )
        if not isinstance(payload, dict) or str(payload.get("status", "")).lower() != "ok":
            raise ValueError("پاسخ آمار بازار نوبیتکس معتبر نیست.")
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        row = stats.get("usdt-rls") or stats.get("USDT-RLS") or stats.get("usdt-irt") or {}
        if not isinstance(row, dict) or row.get("isClosed") is True:
            raise ValueError("بازار USDT نوبیتکس بسته یا در دسترس نیست.")
        buy = _pricing_decimal(row.get("bestBuy"))
        sell = _pricing_decimal(row.get("bestSell"))
        last = _pricing_decimal(row.get("latest"))
        if buy is not None and sell is not None and sell >= buy:
            rial_rate = (buy + sell) / Decimal(2)
            note = "میانگین سفارش‌ها؛ واحد خام ریال و تبدیل به تومان ÷۱۰"
        elif last is not None:
            rial_rate = last
            note = "آخرین معامله؛ واحد خام ریال و تبدیل به تومان ÷۱۰"
        else:
            raise ValueError("قیمت خرید/فروش یا آخرین معاملهٔ نوبیتکس موجود نیست.")
        return quote_record(rial_rate / Decimal(10), "Nobitex (آمار بازار USDT/RLS)", "USDT-RLS", payload.get("_observed_at"), note)

    # Only official Nobitex public REST endpoints are permitted to supply the FX rate.
    # Call the market-stats endpoint only when the primary order book is unavailable.
    errors = []
    selected = None
    try:
        selected = await fetch_nobitex_orderbook()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        errors.append(f"Nobitex orderbook API: {type(exc).__name__}: {str(exc)[:130]}")
        logger.warning("Nobitex public orderbook unavailable; trying official public stats fallback: %s", exc)

    if selected is None:
        try:
            selected = await fetch_nobitex_stats()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(f"Nobitex market-stats API: {type(exc).__name__}: {str(exc)[:130]}")
            logger.warning("Nobitex public market-stats fallback unavailable: %s", exc)

    if selected is None:
        raise RuntimeError("API عمومی نوبیتکس نرخ معتبر تتر برنگرداند. " + " | ".join(errors[:4]))

    tether = {
        "rate": selected["rate"],
        "updated_at": selected["updated_at"],
        "symbol": selected["symbol"],
        "source": selected["source"] + " (API عمومی نوبیتکس؛ تومان)",
        "sources_ok": 1,
        "sources_failed": len(errors),
    }

    # The shop uses the verified USDT/IRT quote as its single FX base.
    # A separate free-market USD quote is not mixed into this API-only mode.
    usd_market = None
    usd_error = "نرخ مستقل دلار آزاد در مسیر API انتخاب‌شده استفاده نمی‌شود؛ مبنای قیمت‌گذاری نرخ USDT/IRT نوبیتکس است."
    return tether, usd_market, usd_error


async def _fetch_live_fx_toman(client):
    """Backward-compatible wrapper for callers needing only the USDT/Toman rate."""
    tether, _usd_market, _usd_error = await _fetch_live_fx_bundle(client)
    return tether["rate"], tether["updated_at"], tether["symbol"], tether["source"]


async def refresh_auto_pricing(force=False, background=False):
    """Refresh the Nobitex public FX API and product reference catalog."""
    global _AUTO_PRICING_REFRESH_LOCK
    if not force and not background:
        # Customer/admin navigation and checkout callbacks only read the last
        # validated snapshot. They never wait for the shared network-refresh lock.
        return auto_pricing_ready(load_auto_pricing_config())
    if _AUTO_PRICING_REFRESH_LOCK is None:
        _AUTO_PRICING_REFRESH_LOCK = asyncio.Lock()
    async with _AUTO_PRICING_REFRESH_LOCK:
        cfg = load_auto_pricing_config()
        # A manual admin refresh may still fetch and display the public exchange
        # rate while automatic price calculation is paused. Background polling
        # remains disabled until the admin re-enables automatic pricing.
        if not cfg.get("enabled") and not force:
            return False
        now_iso = utc_now().isoformat(timespec="seconds")
        api_key = str(cfg.get("provider_api_key") or "").strip()

        catalog_age = _pricing_timestamp_age(cfg.get("catalog_attempted_at"))
        fx_age = _pricing_timestamp_age(cfg.get("fx_attempted_at"))
        force_allowed = bool(force) and (catalog_age is None or catalog_age >= _PROVIDER_FORCE_REFRESH_COOLDOWN_SECONDS
                                         or fx_age is None or fx_age >= _PROVIDER_FORCE_REFRESH_COOLDOWN_SECONDS)
        catalog_due = catalog_age is None or catalog_age >= _PROVIDER_REFRESH_COOLDOWN_SECONDS or force_allowed
        fx_due = fx_age is None or fx_age >= _FX_REFRESH_COOLDOWN_SECONDS or force_allowed

        if catalog_due:
            cfg["catalog_attempted_at"] = now_iso
        if fx_due:
            cfg["fx_attempted_at"] = now_iso
        if catalog_due or fx_due:
            if not background and not force:
                # Never make a customer-facing button wait on a rate provider.
                # The 30-minute scheduler refreshes these sources independently.
                return auto_pricing_ready(cfg)
            save_auto_pricing_config(cfg)
        else:
            # Most button presses land here: use the verified in-memory/SQLite snapshot
            # and avoid opening an HTTP client or re-syncing product tables.
            return auto_pricing_ready(cfg)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=3.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=True,
        ) as client:
            async def fetch_catalog():
                # Fetch independent sources concurrently. If a supplier key exists, its
                # wholesale/partner amounts take precedence; the public quote only fills gaps.
                supplier_task = (
                    asyncio.create_task(_fetch_tgsuperstars_price_catalog(client, api_key))
                    if api_key else None
                )
                public_task = asyncio.create_task(_fetch_public_price_catalog(client))
                supplier_result = None
                if supplier_task is not None:
                    supplier_result = (await asyncio.gather(supplier_task, return_exceptions=True))[0]
                public_result = (await asyncio.gather(public_task, return_exceptions=True))[0]
                supplier_catalog = supplier_result if isinstance(supplier_result, dict) else None
                public_catalog = public_result if isinstance(public_result, dict) else None
                if isinstance(supplier_result, asyncio.CancelledError):
                    raise supplier_result
                if isinstance(public_result, asyncio.CancelledError):
                    raise public_result
                if isinstance(supplier_result, BaseException):
                    logger.warning("Supplier catalog unavailable; public reference may be used: %s", supplier_result)
                if isinstance(public_result, BaseException):
                    logger.warning("Public reference catalog unavailable: %s", public_result)
                if supplier_catalog and public_catalog:
                    return _merge_quote_catalog(supplier_catalog, public_catalog)
                if supplier_catalog:
                    return supplier_catalog
                if public_catalog:
                    return public_catalog
                raise RuntimeError("کاتالوگ تأمین‌کننده و منبع عمومی هیچ قیمت معتبری ارائه نکردند.")

            tasks = []
            if catalog_due:
                tasks.append(("catalog", asyncio.create_task(fetch_catalog())))
            if fx_due:
                tasks.append(("fx", asyncio.create_task(_fetch_live_fx_bundle(client))))
            results = await asyncio.gather(*(task for _name, task in tasks), return_exceptions=True) if tasks else []

        for (name, _task), result in zip(tasks, results):
            if name == "catalog":
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    logger.error("Live pricing catalog refresh failed; keeping last successful catalog", exc_info=(type(result), result, result.__traceback__))
                    # Do not erase a previously good Stars/Premium catalog on a transient outage.
                    if not (cfg.get("catalog_valid") and _pricing_timestamp_fresh(cfg.get("catalog_updated_at"), _PRICING_CATALOG_MAX_AGE_SECONDS)):
                        cfg["catalog_valid"] = False
                    if not _premium_catalog_is_fresh(cfg):
                        cfg["premium_catalog_valid"] = False
                    cfg["catalog_error"] = f"آخرین دریافت کاتالوگ ناموفق بود؛ دادهٔ موفق قبلی حفظ شد ({type(result).__name__}: {str(result)[:160]})."
                    cfg["premium_catalog_error"] = cfg["catalog_error"]
                    # Failed catalog sources retry in five minutes, independently of FX polling.
                    cfg["catalog_attempted_at"] = (utc_now() - timedelta(minutes=25)).isoformat(timespec="seconds")
                else:
                    if not isinstance(result.get("premium_usd"), dict):
                        result["premium_usd"] = {}
                    old_prices = cfg.get("catalog_prices") if isinstance(cfg.get("catalog_prices"), dict) else {}
                    merged_prices = _merge_quote_catalog(result, old_prices)
                    new_stars_price = _pricing_decimal(result.get("stars_price_per_star_usd"))
                    retained_stars_price = _pricing_decimal(merged_prices.get("stars_price_per_star_usd"))
                    if new_stars_price is not None:
                        cfg["catalog_valid"] = True
                        cfg["catalog_updated_at"] = utc_now().isoformat(timespec="seconds")
                        cfg["catalog_error"] = ""
                    elif retained_stars_price is not None and cfg.get("catalog_valid") and _pricing_timestamp_fresh(cfg.get("catalog_updated_at"), _PRICING_CATALOG_MAX_AGE_SECONDS):
                        cfg["catalog_error"] = str(result.get("stars_quote_error") or "قیمت تازهٔ استارز نیامد؛ نرخ موفق قبلی حفظ شد.")[:250]
                    else:
                        cfg["catalog_valid"] = False
                        cfg["catalog_error"] = str(result.get("stars_quote_error") or "منبع قیمت تازهٔ هر استارز ارائه نکرد.")[:250]

                    new_premium = result.get("premium_usd") or {}
                    merged_premium = merged_prices.get("premium_usd") or {}
                    new_premium_complete = all(_pricing_decimal(new_premium.get(str(m))) is not None for m in (3, 6, 12))
                    merged_premium_complete = all(_pricing_decimal(merged_premium.get(str(m))) is not None for m in (3, 6, 12))
                    if new_premium_complete:
                        cfg["premium_catalog_valid"] = True
                        cfg["premium_catalog_updated_at"] = utc_now().isoformat(timespec="seconds")
                        cfg["premium_catalog_error"] = ""
                    elif merged_premium_complete and _premium_catalog_is_fresh(cfg):
                        cfg["premium_catalog_error"] = "دریافت تازهٔ بعضی پلن‌ها ناموفق بود؛ کاتالوگ موفق قبلی حفظ شد."
                    elif merged_premium_complete and cfg.get("premium_catalog_valid") and _pricing_timestamp_fresh(cfg.get("premium_catalog_updated_at"), _PRICING_CATALOG_MAX_AGE_SECONDS):
                        cfg["premium_catalog_error"] = "کاتالوگ تازه کامل نبود؛ دادهٔ قبلی حفظ شد."
                    else:
                        cfg["premium_catalog_valid"] = False
                        missing = [str(m) for m in (3, 6, 12) if _pricing_decimal(merged_premium.get(str(m))) is None]
                        cfg["premium_catalog_error"] = "قیمت مرجع پلن‌های " + "، ".join(missing) + " ماهه هنوز موجود نیست."
                    cfg["catalog_prices"] = merged_prices
            elif name == "fx":
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    logger.error("Nobitex public FX API failed", exc_info=(type(result), result, result.__traceback__))
                    # Keep the last confirmed quote during a bounded outage grace period.
                    # auto_pricing_ready() independently expires it after the grace window.
                    if not (cfg.get("fx_valid") and _pricing_timestamp_fresh(cfg.get("fx_source_updated_at"), _PRICING_FX_MAX_AGE_SECONDS) and _pricing_decimal(cfg.get("fx_rate_toman"))):
                        cfg["fx_valid"] = False
                    cfg["fx_error"] = str(result)[:500] or f"دریافت نرخ تتر ناموفق بود ({type(result).__name__})."
                    # Retry a failed FX fetch in about five minutes, without hammering the API.
                    cfg["fx_attempted_at"] = (utc_now() - timedelta(minutes=25)).isoformat(timespec="seconds")
                else:
                    tether, usd_market, usd_error = result
                    cfg["fx_rate_toman"] = str(tether["rate"])
                    cfg["fx_source_updated_at"] = str(tether["updated_at"])
                    cfg["fx_source_symbol"] = str(tether["symbol"])
                    cfg["fx_source_name"] = str(tether["source"])
                    cfg["fx_fetched_at"] = utc_now().isoformat(timespec="seconds")
                    cfg["fx_valid"] = True
                    cfg["fx_error"] = ""
                    cfg["fx_sources_ok"] = int(tether.get("sources_ok") or 1)
                    cfg["fx_sources_failed"] = int(tether.get("sources_failed") or 0)
                    if usd_market:
                        cfg["usd_market_rate_toman"] = str(usd_market["rate"])
                        cfg["usd_market_source_updated_at"] = str(usd_market["updated_at"])
                        cfg["usd_market_source_name"] = str(usd_market["source"])
                        cfg["usd_market_valid"] = True
                        cfg["usd_market_error"] = ""
                    else:
                        cfg["usd_market_valid"] = False
                        cfg["usd_market_error"] = usd_error or "نرخ مستقل دلار بازار آزاد فعلاً دریافت نشد."

        # Merge only feed fields into the latest settings snapshot. This prevents a
        # concurrent admin change (enable/disable, margin, fee or API key) from being
        # overwritten by a slower network refresh that started earlier.
        latest_cfg = load_auto_pricing_config()
        catalog_fields = (
            "catalog_prices", "catalog_valid", "catalog_updated_at", "premium_catalog_valid",
            "premium_catalog_updated_at", "premium_catalog_error", "catalog_attempted_at", "catalog_error",
        )
        fx_fields = (
            "fx_rate_toman", "fx_valid", "fx_source_updated_at", "fx_source_name", "fx_source_symbol",
            "fx_fetched_at", "fx_attempted_at", "fx_error", "usd_market_rate_toman",
            "usd_market_valid", "usd_market_source_updated_at", "usd_market_source_name", "usd_market_error",
            "fx_sources_ok", "fx_sources_failed",
        )
        if str(latest_cfg.get("provider_api_key") or "").strip() == api_key:
            for key in catalog_fields:
                latest_cfg[key] = cfg.get(key)
        for key in fx_fields:
            latest_cfg[key] = cfg.get(key)
        latest_cfg["refresh_interval_minutes"] = 30
        save_auto_pricing_config(latest_cfg)
        cfg = load_auto_pricing_config()
        if cfg.get("enabled") and cfg.get("fx_valid") and _pricing_timestamp_fresh(cfg.get("fx_source_updated_at"), _PRICING_FX_MAX_AGE_SECONDS):
            try:
                apply_auto_prices_to_products(cfg)
            except Exception:
                logger.exception("Could not update cached automatic Stars/Premium product prices")
        # Keep all dependent catalogs in sync with the same validated snapshot.
        try:
            sync_gift_prices_in_db()
            sync_premium_prices_in_db()
        except Exception:
            logger.exception("Dependent gift/premium catalog synchronization failed")
        return auto_pricing_ready(cfg)


async def auto_pricing_refresh_loop(application):
    """Refresh the Nobitex public FX API every 30m and the product catalog every 30m."""
    while True:
        try:
            await refresh_auto_pricing(force=False, background=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic pricing refresh task failed")
        cfg = load_auto_pricing_config()
        catalog_age = _pricing_timestamp_age(cfg.get("catalog_attempted_at"))
        fx_age = _pricing_timestamp_age(cfg.get("fx_attempted_at"))
        catalog_interval = float(_PROVIDER_REFRESH_COOLDOWN_SECONDS)
        fx_interval = float(_FX_REFRESH_COOLDOWN_SECONDS)
        remaining = [
            5.0 if catalog_age is None else max(5.0, catalog_interval - catalog_age),
            5.0 if fx_age is None else max(5.0, fx_interval - fx_age),
        ]
        await asyncio.sleep(min(remaining) if remaining else fx_interval)


def quote_breakdown_text(quote):
    text_value = (
        f"برآورد هزینه بر پایه کاتالوگ تأمین‌کننده: {quote['cost_usd']} USDT × نرخ {toman(quote['fx_rate_toman'])} تومان\n"
        "⚠️ این مبلغ برآورد مرجع است؛ کسر دقیق تأمین‌کننده فقط هنگام ثبت سفارش نزد همان تأمین‌کننده مشخص می‌شود. این ربات سفارش خودکار نزد تأمین‌کننده نمی‌سازد.\n"
        f"💰 هزینهٔ خرید برآوردی: {toman(quote['cost_toman'])} تومان\n"
        f"🧾 کارمزد/هزینهٔ اضافی تنظیم‌شده: {toman(quote['fee_toman'])} تومان\n"
        f"📈 سود محاسبه‌شده: {toman(quote['profit_toman'])} تومان\n"
        f"💵 قیمت نهایی: {toman(quote['sale_toman'])} تومان"
    )
    if quote.get("provider_over_limit"):
        text_value += "\n⚠️ این مقدار از سقف سفارش یک‌بارهٔ API تأمین‌کننده بیشتر است؛ ثبت در فروشگاه دستی است و تأمین این مقدار باید جداگانه بررسی شود."
    return text_value


# ==========================================
# منوها
# ==========================================

import re


def service_menu(text_fn=None):
    """Build the exact approved service screen from editable central settings."""
    all_configured = load_bot_settings().get("menus", {}).get("services", [])
    configured = configured_buttons("services")
    valid_styles = {"primary", "success", "danger"}
    if not configured:
        configured = deepcopy(_DEFAULT_UI_MENUS["services"])
    grouped = {}
    ordered_row_numbers = []
    for index, item in enumerate(configured):
        if not isinstance(item, dict):
            continue
        label = str(item.get("text", "")).strip()
        callback = str(item.get("callback", "")).strip()
        if not label or not callback:
            continue
        try:
            row_no = int(item.get("row", index + 1) or index + 1)
        except (TypeError, ValueError):
            row_no = index + 1
        if row_no not in grouped:
            grouped[row_no] = []
            ordered_row_numbers.append(row_no)
        style = str(item.get("button_style", "") or "").strip().lower()
        if style not in valid_styles:
            style = _button_style_for(label, callback) or "primary"
        grouped[row_no].append(InlineKeyboardButton(label[:64], callback_data=callback, style=style))
    rows = []
    for row_no in ordered_row_numbers:
        row = grouped[row_no]
        # Row 1 contains the two green buttons; every following service and
        # the main-menu button occupies its own full-width row.
        for offset in range(0, min(len(row), 8), 2):
            rows.append(row[offset:offset + 2])

    # Backward compatibility: if an older settings record has no main-menu item,
    # append the approved one. When it exists in the central settings, its text,
    # color, order and active state are all controlled by the admin panel.
    has_home_configured = any(
        isinstance(item, dict) and str(item.get("callback", "")).strip() == "home"
        for item in all_configured
    )
    if not has_home_configured:
        rows.append([InlineKeyboardButton("⌂ مـنـوی اصـلـی", callback_data="home", style="danger")])
    return InlineKeyboardMarkup(rows)


def stars_menu():
    """Compatibility alias: the active Stars menu is built from the database catalog."""
    return build_stars_menu()


def stars_detail_menu(amount):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 برای خودم", callback_data=f"stars_recipient_self_{amount}", style="primary")],
        [InlineKeyboardButton("🎁 برای شخص دیگر", callback_data=f"stars_recipient_other_{amount}", style="primary")],
        [InlineKeyboardButton("🏷️ وارد کردن کد تخفیف", callback_data=f"stars_discount_{amount}", style="primary")],
        [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="wallet", style="success")],
        [InlineKeyboardButton("↩️ بازگشت", callback_data="stars_detail_back", style="danger")],
    ])


def stars_text():
    """Stars intro plus a truthful snapshot of the live price feed."""
    cfg = load_auto_pricing_config()
    base = bot_text("stars_menu_description")
    if auto_pricing_ready(cfg):
        unit = auto_pricing_unit_sale(cfg)
        unit_text = f"{persian_digits(format(unit, ',.2f').replace(',', '٬'))} تومان" if unit is not None else "در حال محاسبه"
        return base + f"\n\n💹 قیمت هر استارز: {unit_text}"
    return base


MENU_TYPE_DEFAULT = "keyboard"
MENU_TYPE_KEYBOARD = "keyboard"
# Kept only for compatibility with old saved settings and callback routes.
MENU_TYPE_INLINE = "inline"


def get_main_menu_type():
    """Read the global menu mode from the existing settings table."""
    try:
        with db() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key='main_menu_type'").fetchone()
        value = str(row[0] if row else MENU_TYPE_DEFAULT).strip().lower()
        return value if value in (MENU_TYPE_KEYBOARD, MENU_TYPE_INLINE) else MENU_TYPE_KEYBOARD
    except (sqlite3.Error, TypeError, ValueError):
        return MENU_TYPE_KEYBOARD


def set_main_menu_type(value=MENU_TYPE_KEYBOARD):
    """Persist either ordinary reply-keyboard mode or inline-button mode."""
    value = str(value or MENU_TYPE_KEYBOARD).strip().lower()
    if value not in (MENU_TYPE_KEYBOARD, MENU_TYPE_INLINE):
        value = MENU_TYPE_KEYBOARD
    with db() as con:
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES ('main_menu_type', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )


def _main_menu_items():
    """Return enabled main-menu items in a consistent, premium order."""
    items = [
        item for item in configured_buttons("main")
        if isinstance(item, dict) and item.get("enabled", True)
        and str(item.get("text", "")).strip()
        and str(item.get("callback", "")).strip()
    ]
    # Keep the configured order authoritative so the owner's move-up/move-down
    # controls actually change the visible order instead of being silently undone.
    return items


def _button_style_for(label, callback=None):
    """Assign Telegram's native button colors to match the approved menu design."""
    original_label = str(label or "").strip()
    label = original_label.casefold()
    callback = str(callback or "").strip().casefold()
    joined = f"{label} {callback}"

    # Resolve the specific main-menu label first; some actions intentionally share
    # a handler (for example, "سفارش‌های من" and "پیگیری سفارش").
    label_styles = {
        "◈ خـریـد سـرویـس": "danger",
        "▣ کـیـف پـول": "success",
        "＋ شـارژ کـیـف پـول": "success",
        "▤ سـفـارش‌هـای مـن": "primary",
        "◉ پـروفـایـل مـن": "danger",
        "☏ پـشـتـیـبـانـی": "danger",
        "✦ اسـتـارز رایـگـان": "success",
        "▯ شـمـاره مـجـازی": "success",
        "✓ درخـواسـت واسـطـه": "primary",
        "⌘ خـریـد کـانـفـیـگ": "danger",
        "ⓘ سـؤالات مـتـداول": "danger",
        "◇ خـریـد تـون": "success",
        "⌕ پـیـگـیـری سـفـارش": "primary",
        "▱ تـیـکـت": "primary",
    }
    if original_label in label_styles:
        return label_styles[original_label]

    main_palette = {
        "check_membership": "success",
        "cat_stars": "danger",
        "back_to_services": "danger",
        "cat_numbers": "success",
        "mediator_request": "primary",
        "ton_gram": "primary",
        # Approved services-menu palette: green, blue, red in the exact row order.
        "service_stars": "success",
        "service_premium": "success",
        "service_gifts": "primary",
        "service_boost": "danger",
        "service_members": "primary",
        "service_reactions": "success",
        "service_views": "danger",
        "service_instagram_followers": "primary",
        "service_instagram_likes": "success",
        "home": "danger",
        "account": "danger",
        "support": "primary",
        "wallet": "success",
        "topup": "success",
        "free_stars": "success",
        "mini_app": "primary",
        "nft_market": "primary",
        "orders": "primary",
        "ton_buy": "success",
        "config_menu": "danger",
        "faq": "danger",
        "invite": "primary",
    }
    if callback in main_palette:
        return main_palette[callback]

    # Destructive or cancellation actions always take priority over positive words.
    danger_terms = (
        "حذف", "پاک کردن", "لغو", "رد سفارش", "رد پرداخت", "رد رسید",
        "مسدود", "غیرفعال", "delete", "remove", "cancel", "reject",
        "decline", "ban", "disable", "danger",
    )
    if any(term in joined for term in danger_terms):
        return "danger"

    # FAQ and explanatory screens are neutral, using Telegram's default style.
    if callback == "faq" or "سؤالات متداول" in label or "سوالات متداول" in label:
        return None

    if callback in {"admin_menu_set_keyboard", "admin_menu_set_inline"}:
        return "success" if "✅" in label else "primary"

    # Informational/neutral options use Telegram's default background.
    neutral_terms = (
        "مشاهده", "جزئیات", "اطلاعات", "راهنما", "وضعیت فعلی", "نمایش اطلاعات",
        "view", "details", "information", "help", "status",
    )
    if any(term in joined for term in neutral_terms):
        return None

    # Positive/commit actions are green; ordinary navigation and choices stay blue.
    success_terms = (
        "تأیید", "تایید", "ثبت نهایی", "پرداخت", "شارژ کیف پول", "خرید نهایی",
        "خرید سرویس", "شماره مجازی", "افزودن به سبد", "ثبت سفارش", "استارز رایگان",
        "ذخیره", "افزودن", "فعال‌سازی", "تکمیل سفارش", "approve", "confirm",
        "submit", "activate", "save", "pay", "topup", "buy_", "purchase",
    )
    if any(term in joined for term in success_terms):
        return "success"

    # Product/service selection is a positive step and must use the same green
    # styling across the catalogue, product cards and nested service screens.
    positive_callbacks = (
        "service_", "stars_pack_", "buy_", "vip_plan_", "config_plan_",
        "premium_plan_", "gift_plan_", "boost_plan_",
    )
    if any(callback.startswith(prefix) for prefix in positive_callbacks):
        return "success"

    return "primary"


def customer_order_cancel_keyboard():
    """Reply-keyboard-only cancellation button for customer order input steps."""
    return ReplyKeyboardMarkup(
        [[_styled_keyboard_button("❌ لغو سفارش", style_override="danger")]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="برای لغو سفارش، دکمهٔ لغو سفارش را بزن…",
    )


def _is_customer_cancel_text(value):
    text = str(value or "").strip().casefold()
    compact = re.sub(r"[\s‌]+", "", text)
    return (
        text in {"لغو", "cancel", "/cancel", "لغو الان", "❌ لغو", "❌ لغو سفارش", "🏠 لغو سفارش"}
        or compact.startswith("لغو")
        or "لغوسفارش" in compact
    )


def _styled_keyboard_button(label, callback=None, style_override=None):
    """Build a colored ReplyKeyboardButton, using an editable style when supplied."""
    style = style_override if style_override in {"primary", "success", "danger"} else _button_style_for(label, callback)
    try:
        if style:
            return KeyboardButton(str(label), style=style)
        return KeyboardButton(str(label))
    except TypeError:
        # Some older library releases can forward newly-added Bot API fields via api_kwargs.
        if style:
            try:
                return KeyboardButton(str(label), api_kwargs={"style": style})
            except TypeError:
                pass
        logger.warning(
            "This python-telegram-bot version cannot forward KeyboardButton styles; using unstyled button for %r",
            label,
        )
        return KeyboardButton(str(label))


def _button_is_navigation(button):
    label = str(getattr(button, "text", "") or "").strip()
    callback = str(getattr(button, "callback_data", "") or "").strip().lower()
    return (
        callback in {"home", "nav_back", "back_to_services", "admin_menu_settings"}
        or "بازگشت" in label
        or "منوی اصلی" in label
        or label in {"خانه", "🏠 خانه"}
    )


def _button_is_sensitive(button):
    label = str(getattr(button, "text", "") or "").strip().casefold()
    callback = str(getattr(button, "callback_data", "") or "").strip().casefold()
    markers = ("حذف", "لغو", "رد ", "مسدود", "delete", "cancel", "reject", "ban", "danger")
    return any(marker in f"{label} {callback}" for marker in markers)


def _restyle_inline_button(button):
    """Return a copy of an ordinary inline button with a semantic Telegram style."""
    label = str(getattr(button, "text", "") or "")
    callback = getattr(button, "callback_data", None)
    existing_style = getattr(button, "style", None)
    style = existing_style if existing_style in {"primary", "success", "danger"} else _button_style_for(label, callback)
    try:
        data = button.to_dict()
        data["style"] = style
        # de_json retains Telegram object fields while applying the supported style.
        return InlineKeyboardButton.de_json(data, bot=None)
    except Exception:
        # All common project buttons are callback or URL buttons. Keep a safe fallback
        # if a future Telegram button type cannot be reconstructed on this library.
        try:
            kwargs = {"text": label, "style": style}
            for attr in (
                "url", "callback_data", "switch_inline_query",
                "switch_inline_query_current_chat", "callback_game", "pay",
                "login_url", "web_app", "switch_inline_query_chosen_chat", "copy_text",
            ):
                value = getattr(button, attr, None)
                if value is not None:
                    kwargs[attr] = value
            api_kwargs = getattr(button, "api_kwargs", None)
            if api_kwargs:
                kwargs["api_kwargs"] = api_kwargs
            return InlineKeyboardButton(**kwargs)
        except Exception:
            logger.debug("Could not apply button style for %r", label, exc_info=True)
            return button


def _is_services_menu_markup(markup):
    """Identify the approved services menu so its full-width rows are never auto-paired."""
    source_rows = getattr(markup, "inline_keyboard", None) or []
    buttons = [button for row in source_rows for button in (row or [])]
    callbacks = [str(getattr(button, "callback_data", "") or "").strip() for button in buttons]
    service_callbacks = [value for value in callbacks if value.startswith("service_")]
    return bool(service_callbacks) and "home" in callbacks and all(
        value == "home" or value.startswith("service_") for value in callbacks
    )


def _is_stars_menu_markup(markup):
    """Keep the one-package-per-row Stars catalog intact in both inline and reply modes."""
    source_rows = getattr(markup, "inline_keyboard", None) or []
    callbacks = [str(getattr(button, "callback_data", "") or "").strip()
                 for row in source_rows for button in (row or [])]
    return sum(value.startswith("stars_pack_") for value in callbacks) >= 6


def _premium_inline_rows(markup):
    """Standardize inline rows; keep the approved services layout exactly as authored."""
    source_rows = getattr(markup, "inline_keyboard", None) or []
    if _is_services_menu_markup(markup) or _is_stars_menu_markup(markup):
        # Do not pair one-button service or Stars rows; these layouts are user-approved.
        return [
            [_restyle_inline_button(button) for button in row]
            for row in source_rows if row
        ]
    content_rows = []
    navigation = []
    pending = []

    def flush_pending():
        nonlocal pending
        if pending:
            content_rows.append(pending[:])
            pending = []

    for source_row in source_rows:
        row = list(source_row or [])
        if not row:
            continue
        # Preserve rows that intentionally contain multiple related buttons, while
        # reducing any row wider than two buttons to a mobile-friendly width.
        if len(row) >= 2:
            flush_pending()
            for offset in range(0, len(row), 2):
                chunk = row[offset:offset + 2]
                kept = []
                for button in chunk:
                    if _button_is_navigation(button):
                        navigation.append(button)
                    else:
                        kept.append(button)
                if len(kept) == 2:
                    content_rows.append(kept)
                elif len(kept) == 1:
                    if _button_is_sensitive(kept[0]):
                        content_rows.append(kept)
                    else:
                        pending.append(kept[0])
                        if len(pending) == 2:
                            content_rows.append(pending)
                            pending = []
            continue

        button = row[0]
        if _button_is_navigation(button):
            flush_pending()
            navigation.append(button)
        elif _button_is_sensitive(button):
            flush_pending()
            content_rows.append([button])
        else:
            pending.append(button)
            if len(pending) == 2:
                content_rows.append(pending)
                pending = []

    flush_pending()

    styled_rows = []
    for row in content_rows:
        styled_rows.append([_restyle_inline_button(button) for button in row[:2]])
    # Navigation stays at the end; pair a matching back/home pair where possible.
    styled_navigation = [_restyle_inline_button(button) for button in navigation]
    if styled_navigation:
        for offset in range(0, len(styled_navigation), 2):
            styled_rows.append(styled_navigation[offset:offset + 2])
    return styled_rows


def main_menu(user_id=None):
    """Render the approved SEYED PREMIUM layout with colored reply or inline buttons."""
    items = _main_menu_items()
    item_rows = []
    if items and all(item.get("row") is not None for item in items):
        grouped = {}
        row_order = []
        for item in items:
            row_no = int(item.get("row", 999) or 999)
            if row_no not in grouped:
                grouped[row_no] = []
                row_order.append(row_no)
            grouped[row_no].append(item)
        item_rows = [grouped[row_no] for row_no in row_order]
    else:
        normal_items = [item for item in items if str(item.get("callback", "")) != "faq"]
        faq_items = [item for item in items if str(item.get("callback", "")) == "faq"]
        item_rows = [normal_items[i:i + 2] for i in range(0, len(normal_items), 2)]
        if faq_items:
            item_rows.append(faq_items)

    if get_main_menu_type() == MENU_TYPE_INLINE:
        rows = []
        for item_row in item_rows:
            row = []
            for item in item_row[:2]:
                label = str(item.get("text", "")).strip()
                callback = str(item.get("callback", "")).strip()
                if label and callback:
                    chosen_style = item.get("button_style")
                    if chosen_style not in {"primary", "success", "danger"}:
                        chosen_style = _button_style_for(label, callback)
                    row.append(InlineKeyboardButton(
                        label, callback_data=callback, style=chosen_style,
                    ))
            if row:
                rows.append(row)
        if user_id is not None and int(user_id) == ADMIN_ID:
            rows.append([InlineKeyboardButton("👑 مدیریت کل ربات", callback_data="admin", style="primary")])
        return InlineKeyboardMarkup(rows)

    rows = []
    for item_row in item_rows:
        row = []
        for item in item_row[:2]:
            label = str(item.get("text", "")).strip()
            callback = str(item.get("callback", "")).strip()
            if label and callback:
                row.append(_styled_keyboard_button(label, callback, item.get("button_style")))
        if row:
            rows.append(row)
    if user_id is not None and int(user_id) == ADMIN_ID:
        rows.append([_styled_keyboard_button("👑 مدیریت کل ربات", "admin")])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="پیامت رو بنویس یا از منو انتخاب کن…",
    )

def menu_settings_keyboard():
    mode = get_main_menu_type()
    keyboard_label = "⌨️ کیبورد معمولی: فعال ✅" if mode == MENU_TYPE_KEYBOARD else "⌨️ فعال‌کردن کیبورد معمولی"
    inline_label = "💎 شیشه‌ای: فعال ✅" if mode == MENU_TYPE_INLINE else "💎 فعال‌کردن منوی شیشه‌ای"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(keyboard_label, callback_data="admin_menu_set_keyboard")],
        [InlineKeyboardButton(inline_label, callback_data="admin_menu_set_inline")],
        [InlineKeyboardButton("⬅️ بازگشت به مدیریت", callback_data="admin")],
    ])


def menu_settings_text(prefix=None):
    mode = get_main_menu_type()
    active = "⌨️ کیبورد معمولی" if mode == MENU_TYPE_KEYBOARD else "💎 دکمه‌های شیشه‌ای"
    details = ("منوها با کیبورد پایین صفحه نمایش داده می‌شوند و کاربر می‌تواند آن را مخفی کند."
               if mode == MENU_TYPE_KEYBOARD else "منوها با دکمه‌های شیشه‌ای زیر پیام‌ها نمایش داده می‌شوند.")
    return ((str(prefix).strip() + "\n\n") if prefix else "") + (
        "⚙️ تنظیمات منوی SEYED PREMIUM\n\n"
        f"🔹 حالت فعال: {active}\n{details}\n\n"
        "برای تغییر حالت، یکی از گزینه‌های زیر را انتخاب کن."
    )


# Every outgoing inline menu is registered per user. In keyboard mode the same
# menu is rendered as ReplyKeyboardMarkup and each text button routes back into
# the existing callback handler. Routes are saved additively in the shop DB.
_USER_MENU_ROUTES = {}
_USER_LAST_MENU_MESSAGE_IDS = {}
_ORIGINAL_BOT_SEND_MESSAGE = Bot.send_message
_ORIGINAL_BOT_EDIT_MESSAGE_TEXT = Bot.edit_message_text
_ORIGINAL_BOT_EDIT_MESSAGE_REPLY_MARKUP = Bot.edit_message_reply_markup

def _route_from_button(button):
    label = str(getattr(button, "text", "") or "").strip()
    if not label:
        return None, None, None
    callback = getattr(button, "callback_data", None)
    url = getattr(button, "url", None)
    if callback is not None:
        return label, "callback", str(callback)
    if url:
        return label, "url", str(url)
    # Preserve the label in the keyboard, but do not invent an action for
    # Telegram button types that cannot be routed as a regular message.
    return label, "noop", ""

def _set_user_seen_mode(user_id, mode):
    try:
        user_id = int(user_id)
        if mode not in (MENU_TYPE_KEYBOARD, MENU_TYPE_INLINE):
            return
        with db() as con:
            con.execute(
                "INSERT INTO user_menu_state(user_id,menu_mode) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET menu_mode=excluded.menu_mode",
                (user_id, mode),
            )
            con.execute("UPDATE users SET menu_type_seen=? WHERE user_id=?", (mode, user_id))
    except (sqlite3.Error, TypeError, ValueError):
        logger.debug("Could not update user menu mode", exc_info=True)

def _save_user_menu_routes(chat_id, routes, mode=None):
    try:
        user_id = int(chat_id)
    except (TypeError, ValueError):
        return
    clean = {str(k): (str(v[0]), str(v[1])) for k, v in routes.items() if k and v}
    _USER_MENU_ROUTES[user_id] = clean
    try:
        with db() as con:
            con.execute("DELETE FROM user_menu_routes WHERE user_id=?", (user_id,))
            con.executemany(
                "INSERT OR REPLACE INTO user_menu_routes(user_id,label,action_type,action_value) VALUES (?,?,?,?)",
                [(user_id, label, action[0], action[1]) for label, action in clean.items()],
            )
            if mode in (MENU_TYPE_KEYBOARD, MENU_TYPE_INLINE):
                con.execute(
                    "INSERT INTO user_menu_state(user_id,menu_mode) VALUES (?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET menu_mode=excluded.menu_mode",
                    (user_id, mode),
                )
                con.execute("UPDATE users SET menu_type_seen=? WHERE user_id=?", (mode, user_id))
    except sqlite3.Error:
        # Route conversion should not crash a menu send; in-memory routes remain available.
        logger.exception("Could not persist menu routes for chat_id=%s", user_id)

def _load_user_menu_routes(user_id):
    routes = _USER_MENU_ROUTES.get(int(user_id))
    if routes is not None:
        return routes
    try:
        with db() as con:
            rows = con.execute(
                "SELECT label, action_type, action_value FROM user_menu_routes WHERE user_id=?",
                (int(user_id),),
            ).fetchall()
        routes = {str(r["label"]): (str(r["action_type"]), str(r["action_value"] or "")) for r in rows}
        _USER_MENU_ROUTES[int(user_id)] = routes
        return routes
    except (sqlite3.Error, ValueError, TypeError):
        logger.exception("Could not load menu routes for user_id=%s", user_id)
        return {}

def _routes_from_inline_markup(markup, keyboard_labels=False):
    routes = {}
    rows = getattr(markup, "inline_keyboard", None) or []
    for row in rows:
        for button in row:
            label, action_type, action_value = _route_from_button(button)
            if label and keyboard_labels:
                label = label[:64]
            if label:
                routes[label] = (action_type, action_value)
    return routes

def _routes_from_main_reply_keyboard(markup, user_id=None):
    available = {str(item["text"]).strip(): str(item["callback"]) for item in _main_menu_items()}
    available["🏠 منوی اصلی"] = "home"
    available["🔄 بررسی عضویت"] = "check_membership"
    if user_id is not None and int(user_id) == ADMIN_ID:
        available["👑 مدیریت کل ربات"] = "admin"
    labels = []
    for row in getattr(markup, "keyboard", None) or []:
        for button in row:
            label = str(getattr(button, "text", "") or "").strip()
            if label:
                labels.append(label)
    return {label: ("callback", available[label]) for label in labels if label in available}

def _is_custom_stars_markup(markup):
    rows = getattr(markup, "inline_keyboard", None) or []
    return any(
        str(getattr(button, "callback_data", "") or "").startswith("customstars_")
        for row in rows for button in (row or [])
    )


def _prepare_outgoing_markup(chat_id, markup):
    """Render outgoing keyboards with semantic colors while preserving main-menu rows."""
    if isinstance(markup, ReplyKeyboardRemove):
        return markup
    if _is_custom_stars_markup(markup):
        return markup

    mode = get_main_menu_type()
    if mode == MENU_TYPE_INLINE:
        if isinstance(markup, InlineKeyboardMarkup):
            return InlineKeyboardMarkup(_premium_inline_rows(markup))
        if isinstance(markup, ReplyKeyboardMarkup):
            return None
        return markup

    if isinstance(markup, InlineKeyboardMarkup):
        routes = _routes_from_inline_markup(markup, keyboard_labels=True)
        if _is_services_menu_markup(markup) or _is_stars_menu_markup(markup):
            # The services menu has one paired row and the Stars catalog has one package per row.
            # Preserve those exact row boundaries in reply-keyboard mode too.
            rows = []
            for source_row in (getattr(markup, "inline_keyboard", None) or []):
                row = []
                for button in (source_row or [])[:2]:
                    label = str(getattr(button, "text", "") or "").strip()[:64]
                    callback = getattr(button, "callback_data", None)
                    if label and callback and not getattr(button, "url", None):
                        row.append(_styled_keyboard_button(label, callback, getattr(button, "style", None)))
                if row:
                    rows.append(row)
            labels = {str(getattr(button, "text", "") or "").strip() for row in rows for button in row}
            home_label = "⌂ مـنـوی اصـلـی"
            if _is_services_menu_markup(markup):
                if not any(str(getattr(button, "callback_data", "") or "") == "home"
                           for source_row in (getattr(markup, "inline_keyboard", None) or [])
                           for button in (source_row or [])):
                    rows.append([_styled_keyboard_button(home_label, "home")])
                routes[home_label] = ("callback", "home")
            # Do not append an admin-only button to approved catalog/service screens;
            # their row count and appearance must match the owner's exact design.
            # Admin access remains available through the dedicated /admin command/menu.
            _save_user_menu_routes(chat_id, routes, MENU_TYPE_KEYBOARD)
            return ReplyKeyboardMarkup(
                rows,
                resize_keyboard=True,
                one_time_keyboard=False,
                input_field_placeholder="پیامت رو بنویس یا از منو انتخاب کن…",
            )
        rows = []
        for source_row in _premium_inline_rows(markup):
            pair = []
            for button in source_row:
                label = str(getattr(button, "text", "") or "").strip()[:64]
                if not label or getattr(button, "url", None):
                    continue
                pair.append(_styled_keyboard_button(label, getattr(button, "callback_data", None), getattr(button, "style", None)))
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
            if pair:
                rows.append(pair)
        labels = {str(getattr(button, "text", "") or "").strip() for row in rows for button in row}
        if "🏠 منوی اصلی" not in labels:
            rows.append([_styled_keyboard_button("🏠 منوی اصلی", "home")])
        try:
            routes["🏠 منوی اصلی"] = ("callback", "home")
            if chat_id is not None and int(chat_id) == ADMIN_ID:
                if "👑 مدیریت کل ربات" not in labels:
                    rows.append([_styled_keyboard_button("👑 مدیریت کل ربات", "admin")])
                routes["👑 مدیریت کل ربات"] = ("callback", "admin")
            _save_user_menu_routes(chat_id, routes, MENU_TYPE_KEYBOARD)
        except Exception:
            logger.exception("Could not register converted keyboard routes")
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder="پیامت رو بنویس یا از منو انتخاب کن…",
        )

    if isinstance(markup, ReplyKeyboardMarkup):
        original_rows = getattr(markup, "keyboard", None) or []
        raw_labels = [
            str(getattr(button, "text", "") or "").strip()
            for row in original_rows for button in row
            if str(getattr(button, "text", "") or "").strip()
        ]
        main_labels = {str(item["text"]).strip() for item in _main_menu_items()}
        is_main_menu = bool(raw_labels) and all(
            label in main_labels or label in {"🏠 منوی اصلی", "👑 مدیریت کل ربات"}
            for label in raw_labels
        )

        if is_main_menu:
            # Keep every row exactly as defined in the redesign, including full-width rows.
            rows = []
            for source_row in original_rows:
                row = []
                for button in source_row[:2]:
                    label = str(getattr(button, "text", "") or "").strip()
                    if label:
                        callback = _route_for_main_label(label)
                        if label == "👑 مدیریت کل ربات":
                            callback = "admin"
                        row.append(_styled_keyboard_button(label, callback, getattr(button, "style", None)))
                if row:
                    rows.append(row)
            labels = {str(getattr(button, "text", "") or "").strip() for row in rows for button in row}
            if chat_id is not None and int(chat_id) == ADMIN_ID and "👑 مدیریت کل ربات" not in labels:
                rows.append([_styled_keyboard_button("👑 مدیریت کل ربات", "admin")])
            try:
                routes = _routes_from_main_reply_keyboard(ReplyKeyboardMarkup(rows), user_id=chat_id)
                routes["🏠 منوی اصلی"] = ("callback", "home")
                if chat_id is not None and int(chat_id) == ADMIN_ID:
                    routes["👑 مدیریت کل ربات"] = ("callback", "admin")
                _save_user_menu_routes(chat_id, routes, MENU_TYPE_KEYBOARD)
            except Exception:
                logger.exception("Could not register styled main reply keyboard routes")
            return ReplyKeyboardMarkup(
                rows,
                resize_keyboard=True,
                one_time_keyboard=False,
                input_field_placeholder="پیامت رو بنویس یا از منو انتخاب کن…",
            )

        # Keep older submenu reply keyboards compatible while applying styles where possible.
        labels = []
        for label in raw_labels:
            if label not in labels:
                labels.append(label)
        labels = [label for label in labels if label != "👑 مدیریت کل ربات"]
        rows = []
        for offset in range(0, len(labels), 2):
            rows.append([
                _styled_keyboard_button(label, _route_for_main_label(label))
                for label in labels[offset:offset + 2]
            ])
        if "🏠 منوی اصلی" not in labels:
            rows.append([_styled_keyboard_button("🏠 منوی اصلی", "home")])
        if chat_id is not None and int(chat_id) == ADMIN_ID:
            rows.append([_styled_keyboard_button("👑 مدیریت کل ربات", "admin")])
        routes = _routes_from_main_reply_keyboard(ReplyKeyboardMarkup(rows), user_id=chat_id)
        routes["🏠 منوی اصلی"] = ("callback", "home")
        if chat_id is not None and int(chat_id) == ADMIN_ID:
            routes["👑 مدیریت کل ربات"] = ("callback", "admin")
        _save_user_menu_routes(chat_id, routes, MENU_TYPE_KEYBOARD)
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder="پیامت رو بنویس یا از منو انتخاب کن…",
        )

    return markup

def _route_for_main_label(label):
    for item in _main_menu_items():
        if str(item.get("text", "")).strip() == str(label).strip():
            return str(item.get("callback", ""))
    if label == "🏠 منوی اصلی":
        return "home"
    if label == "👑 مدیریت کل ربات":
        return "admin"
    if label == "🔄 بررسی عضویت":
        return "check_membership"
    return None

def _bot_argument(args, kwargs, name, positional_index, default=None):
    if name in kwargs:
        return kwargs[name]
    if len(args) > positional_index:
        return args[positional_index]
    return default

def _seen_user_menu_mode(chat_id):
    try:
        user_id = int(chat_id)
        with db() as con:
            row = con.execute("SELECT menu_mode FROM user_menu_state WHERE user_id=?", (user_id,)).fetchone()
            if row:
                return str(row["menu_mode"] or "")
            row = con.execute("SELECT menu_type_seen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return str(row["menu_type_seen"] or "") if row else ""
    except (sqlite3.Error, TypeError, ValueError):
        return ""

async def _remove_stale_reply_keyboard(self, chat_id):
    if chat_id is None or _seen_user_menu_mode(chat_id) != MENU_TYPE_KEYBOARD:
        return
    try:
        await _ORIGINAL_BOT_SEND_MESSAGE(
            self, chat_id=chat_id,
            text="\u2063",
            reply_markup=ReplyKeyboardRemove(),
        )
        _set_user_seen_mode(chat_id, MENU_TYPE_INLINE)
    except Exception:
        logger.debug("Could not remove stale reply keyboard", exc_info=True)

async def _mode_aware_send_message(self, *args, **kwargs):
    markup = kwargs.get("reply_markup")
    chat_id = _bot_argument(args, kwargs, "chat_id", 0)
    mode = get_main_menu_type()
    if mode == MENU_TYPE_INLINE and not isinstance(markup, ReplyKeyboardRemove):
        await _remove_stale_reply_keyboard(self, chat_id)
    if isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
        kwargs = dict(kwargs)
        kwargs["reply_markup"] = _prepare_outgoing_markup(chat_id, markup)
    result = await _ORIGINAL_BOT_SEND_MESSAGE(self, *args, **kwargs)
    if isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)) and getattr(result, "message_id", None) is not None:
        try:
            _USER_LAST_MENU_MESSAGE_IDS[int(chat_id)] = int(result.message_id)
        except (TypeError, ValueError):
            pass
    return result

async def _mode_aware_edit_message_text(self, *args, **kwargs):
    markup = kwargs.get("reply_markup")
    chat_id = _bot_argument(args, kwargs, "chat_id", 1)
    text_value = _bot_argument(args, kwargs, "text", 0)
    message_id = _bot_argument(args, kwargs, "message_id", 2)
    mode = get_main_menu_type()
    if mode == MENU_TYPE_INLINE and not isinstance(markup, ReplyKeyboardRemove):
        await _remove_stale_reply_keyboard(self, chat_id)
    if mode == MENU_TYPE_KEYBOARD and isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)) and chat_id is not None and not _is_custom_stars_markup(markup):
        converted = _prepare_outgoing_markup(chat_id, markup)
        # Telegram cannot edit an inline message into a ReplyKeyboard. Send the
        # same screen as a new message with the ReplyKeyboard, then clear old inline buttons.
        send_options = {}
        allowed = {
            "parse_mode", "entities", "link_preview_options", "business_connection_id",
            "message_thread_id", "disable_notification", "protect_content",
            "message_effect_id", "allow_paid_broadcast", "direct_messages_topic_id",
            "read_timeout", "write_timeout", "connect_timeout", "pool_timeout",
        }
        for key in allowed:
            if key in kwargs:
                send_options[key] = kwargs[key]
        sent = await _ORIGINAL_BOT_SEND_MESSAGE(
            self, chat_id=chat_id, text=text_value, reply_markup=converted, **send_options
        )
        if getattr(sent, "message_id", None) is not None:
            try:
                _USER_LAST_MENU_MESSAGE_IDS[int(chat_id)] = int(sent.message_id)
            except (TypeError, ValueError):
                pass
        if message_id is not None:
            try:
                await _ORIGINAL_BOT_EDIT_MESSAGE_REPLY_MARKUP(
                    self, chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except Exception:
                logger.debug("Could not clear old inline menu after keyboard-mode switch", exc_info=True)
        return sent
    if mode == MENU_TYPE_KEYBOARD and markup is None and chat_id is not None:
        result = await _ORIGINAL_BOT_EDIT_MESSAGE_TEXT(self, *args, **kwargs)
        if message_id is not None:
            try:
                await _ORIGINAL_BOT_EDIT_MESSAGE_REPLY_MARKUP(
                    self, chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except Exception:
                logger.debug("Could not clear legacy inline markup", exc_info=True)
        return result
    if isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
        if mode == MENU_TYPE_INLINE and isinstance(markup, ReplyKeyboardMarkup):
            kwargs = dict(kwargs)
            kwargs["reply_markup"] = None
        else:
            kwargs = dict(kwargs)
            kwargs["reply_markup"] = _prepare_outgoing_markup(chat_id, markup)
    result = await _ORIGINAL_BOT_EDIT_MESSAGE_TEXT(self, *args, **kwargs)
    if isinstance(markup, InlineKeyboardMarkup) and getattr(result, "message_id", None) is not None:
        try:
            _USER_LAST_MENU_MESSAGE_IDS[int(chat_id)] = int(result.message_id)
        except (TypeError, ValueError):
            pass
    return result

# Install once during import. This keeps the existing handlers intact and applies
# the global mode to every Bot.send_message / Bot.edit_message_text call site.
Bot.send_message = _mode_aware_send_message
Bot.edit_message_text = _mode_aware_edit_message_text


class _KeyboardMenuQuery:
    """Route ReplyKeyboard taps through existing callback logic."""
    def __init__(self, update, context, data):
        from types import SimpleNamespace
        self._update = update
        self._context = context
        self.data = data
        self.from_user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
        message_id = _USER_LAST_MENU_MESSAGE_IDS.get(int(update.effective_user.id))
        if message_id is None and update.effective_message is not None:
            message_id = update.effective_message.message_id
        # Existing callback paths that store the menu message_id keep working
        # even though a ReplyKeyboard tap arrives as a new user text message.
        self.message = SimpleNamespace(chat_id=chat_id, message_id=message_id)

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text, **kwargs):
        return await self._context.bot.send_message(
            chat_id=self._update.effective_chat.id, text=text, **kwargs
        )

    async def edit_message_reply_markup(self, *args, **kwargs):
        return None


def _user_expecting_text_input(context):
    """Avoid treating ordinary answers as menu taps while a form is waiting for text."""
    state = getattr(context, "user_data", {}) or {}
    if state.get("editor_wait_key"):
        return True
    if state.get("pricing_admin_state") or state.get("stars_custom_wait_recipient") or state.get("stars_fixed_wait_recipient"):
        return True
    if state.get("coupon_category_add_state"):
        return True
    if state.get("pending_product_id") is not None:
        return True
    if state.get("pending_product_coupon_input") is not None:
        return True
    if state.get("pending_stars_coupon"):
        return True
    if state.get("pending_product_coupon"):
        return True
    if state.get("config_wait_volume") or state.get("config_wait_duration"):
        return True
    if state.get("vip_wait_volume") or state.get("vip_wait_duration"):
        return True
    if state.get("admin_coupon_state"):
        step = (state.get("admin_coupon_state") or {}).get("step")
        if step in {"code", "amount", "recipients", "expiry_hours", "expiry_date"}:
            return True
    coupon_edit = state.get("coupon_edit_state")
    if coupon_edit:
        field = str(coupon_edit.get("field", ""))
        if field in {"code", "amount", "userlimit", "expiry_date", "expiry_time", "recipient", "min"}:
            return True
    product_state = state.get("product_admin_state")
    if product_state:
        action = str(product_state.get("action", ""))
        if action in {"add_name", "edit_name", "add_price", "edit_price", "price", "name"}:
            return True
    ui_state = state.get("uiadm_state")
    if ui_state and str(ui_state.get("step", "")) in {"id", "text", "label"}:
        return True
    return False


async def dispatch_keyboard_menu_text(update, context):
    """Route menu taps, while leaving active free-text input to its own workflow."""
    current_mode = get_main_menu_type()
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return False

    label = message.text.strip()
    routes = _load_user_menu_routes(user.id)
    if current_mode == MENU_TYPE_KEYBOARD:
        # During a text-entry step, ordinary answers take precedence over menu labels.
        # Navigation remains available so users can escape the current step.
        navigation_labels = {
            "🏠 منوی اصلی", "🔙 بازگشت", "⬅️ بازگشت", "↩️ بازگشت",
            "⬅️ بازگشت به مدیریت", "❌ لغو", "لغو",
        }
        is_navigation = (
            label in navigation_labels
            or "بازگشت" in label
            or "منوی اصلی" in label
            or label.startswith("❌ لغو")
        )
        if _user_expecting_text_input(context) and not is_navigation:
            return False
        # Only route labels belonging to the user's most recently shown menu.
        action = routes.get(label)
        if action is None:
            return False
        action_type, action_value = action
        _set_user_seen_mode(user.id, current_mode)

        if action_type == "url":
            await message.reply_text(f"🔗 برای ادامه، این لینک را باز کن:\n{action_value}")
            return True
        if action_type != "callback" or not action_value:
            await message.reply_text("این گزینه در حالت فعلی قابل اجرا نیست. از منوی فعلی گزینه دیگری انتخاب کن.")
            return True

        from types import SimpleNamespace
        query = _KeyboardMenuQuery(update, context, action_value)
        synthetic_update = SimpleNamespace(
            callback_query=query,
            effective_user=update.effective_user,
            effective_chat=update.effective_chat,
            effective_message=update.effective_message,
            update_id=getattr(update, "update_id", None),
        )
        await _callbacks_impl(synthetic_update, context)
        return True

    # If the global mode changed from keyboard to inline, only intercept a label
    # that belonged to the old keyboard. Arbitrary text/IDs/links must continue
    # into the current input workflow so in-progress orders are not discarded.
    if label not in routes:
        return False
    seen_mode = _seen_user_menu_mode(user.id)
    if seen_mode == MENU_TYPE_KEYBOARD:
        await message.reply_text("\u2063", reply_markup=ReplyKeyboardRemove())
        _set_user_seen_mode(user.id, MENU_TYPE_INLINE)
        await message.reply_text(bot_text("main_menu"), reply_markup=main_menu())
        return True
    return False


def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bot_text("back_button"), callback_data="nav_back", style="primary")]
    ])


# ==========================================
# /start
# ==========================================



# قفل عضویت اجباری کانال
CHANNEL_USERNAME = '@SeyedPro_FQA'

def membership_keyboard():
    if get_main_menu_type() == MENU_TYPE_KEYBOARD:
        return ReplyKeyboardMarkup(
            [[KeyboardButton("🔄 بررسی عضویت")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder="برای بررسی عضویت، دکمه را بزن…",
        )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/SeyedPro_FQA", style="primary")],
        [InlineKeyboardButton("🔄 بررسی عضویت", callback_data="check_membership", style="success")],
    ])

_MEMBERSHIP_CACHE = {}
_MEMBERSHIP_CACHE_TTL = 300
_MEMBERSHIP_NEGATIVE_CACHE_TTL = 15
_MEMBERSHIP_FAILURE_CACHE_TTL = 5


async def is_channel_member(update, context):
    user = update.effective_user
    if user is None:
        return False

    user_id = user.id
    query = update.callback_query
    force_refresh = bool(query and query.data == "check_membership")
    now = time.monotonic()

    cached = _MEMBERSHIP_CACHE.get(user_id)
    if not force_refresh and cached:
        result, checked_at = cached
        cache_ttl = (
            _MEMBERSHIP_CACHE_TTL if result is True
            else _MEMBERSHIP_NEGATIVE_CACHE_TTL if result is False
            else _MEMBERSHIP_FAILURE_CACHE_TTL
        )
        if now - checked_at < cache_ttl:
            return result

    try:
        m = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        result = (
            m.status in ("creator", "administrator", "member")
            or (
                m.status == "restricted"
                and getattr(m, "is_member", False)
            )
        )
        _MEMBERSHIP_CACHE[user_id] = (result, time.monotonic())
        return result
    except Exception:
        logger.warning("Channel membership check failed", exc_info=True)
        _MEMBERSHIP_CACHE[user_id] = (None, time.monotonic())
        return None


async def require_channel_membership(update, context):
    result = await is_channel_member(update, context)

    if result is True:
        return True

    message = update.effective_message
    if message:
        if result is None:
            await message.reply_text(
                "⚠️ در حال حاضر بررسی عضویت کانال ممکن نیست. "
                "چند لحظه دیگر دوباره تلاش کن."
            )
        else:
            await message.reply_text(
                "🔒 برای استفاده از خدمات ربات، ابتدا عضو کانال شو:\nhttps://t.me/SeyedPro_FQA\n\nپس از عضویت، «بررسی عضویت» را بزن.",
                reply_markup=membership_keyboard(),
            )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _start_t = time.perf_counter()
    _last_t = _start_t

    def _start_mark(label):
        nonlocal _last_t
        _now = time.perf_counter()
        logger.warning(
            "START PERF | %s: %.3fs | total: %.3fs",
            label, _now - _last_t, _now - _start_t
        )
        _last_t = _now

    if not await require_channel_membership(update, context):
        _start_mark("membership denied/failed")
        return
    _start_mark("membership check")

    user = update.effective_user
    register_user(user)
    _start_mark("register_user")

    current_menu_type = get_main_menu_type()
    _set_user_seen_mode(user.id, current_menu_type)

    # ثبت معرف فقط یک بار؛ دعوت کردن خود کاربر مجاز نیست.
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0][4:])
        except ValueError:
            referrer_id = 0
        if referrer_id and referrer_id != user.id:
            with db() as con:
                exists = con.execute(
                    "SELECT 1 FROM users WHERE user_id = ?", (referrer_id,)
                ).fetchone()
                if exists:
                    con.execute(
                        "UPDATE users SET referred_by = ? "
                        "WHERE user_id = ? AND referred_by IS NULL",
                        (referrer_id, user.id),
                    )
    _start_mark("referral handling")

    text = bot_text("welcome")
    _start_mark("welcome text")

    markup = main_menu(update.effective_user.id)
    _start_mark("main_menu build")

    await update.effective_message.reply_text(text, reply_markup=markup)
    _start_mark("reply_text completed")

# ==========================================
# مرکز مدیریت قیمت‌گذاری محصولات
# ==========================================

AUTO_PRICE_CATEGORY_DEFS = (
    ("numbers", "📱 شماره مجازی", ("numbers", "virtual_numbers", "virtual_number", "phone_numbers")),
    ("configs", "🛒 خرید کانفیگ", ("configs", "config", "vpn", "v2ray", "wireguard", "vpn_configs")),
    ("ton", "💎 خرید تون", ("ton", "tons")),
    ("premium", "💎 پرمیوم تلگرام", ("premium", "telegram_premium")),
    ("stars", "⭐ استارز تلگرام", ("stars", "telegram_stars")),
    ("gifts", "🎁 گیفت تلگرام", ("gifts", "telegram_gifts")),
    ("boost", "🚀 بوست تلگرام", ("boost", "telegram_boost", "boosts")),
    ("members", "👥 ممبر تلگرام", ("members", "member", "telegram_members", "telegram_member")),
    ("reactions", "💙 ری‌اکشن تلگرام", ("reactions", "reaction", "telegram_reactions")),
    ("views", "👀 ویو تلگرام", ("views", "view", "telegram_views")),
    ("instagram_followers", "📸 فالوور اینستاگرام", ("instagram_followers", "instagram_follower", "followers")),
    ("instagram_likes", "❤️ لایک اینستاگرام", ("instagram_likes", "instagram_like", "likes")),
)


def _auto_price_category_map(context):
    """Build the fixed 12 storefront groups plus any new product categories saved later."""
    with db() as con:
        rows = con.execute(
            "SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND TRIM(category) != '' ORDER BY category COLLATE NOCASE"
        ).fetchall()
    existing = [str(row[0]).strip() for row in rows if str(row[0] or '').strip()]
    result = []
    known = set()
    token_map = {}
    for key, label, aliases in AUTO_PRICE_CATEGORY_DEFS:
        alias_set = {str(x).casefold() for x in aliases}
        matched = [cat for cat in existing if cat.casefold() in alias_set]
        for cat in matched:
            known.add(cat.casefold())
        token = key
        token_map[token] = {"label": label, "categories": tuple(matched or (key,))}
        result.append((token, label))
    # Any category introduced in the product manager is included without code changes.
    import hashlib
    for cat in existing:
        if cat.casefold() in known:
            continue
        token = "x" + hashlib.sha1(cat.encode("utf-8")).hexdigest()[:10]
        token_map[token] = {"label": f"📦 {cat}", "categories": (cat,)}
        result.append((token, f"📦 {cat}"))
    context.user_data["auto_price_category_tokens"] = token_map
    return result


def auto_price_categories_markup(context):
    categories = _auto_price_category_map(context)
    buttons = [[InlineKeyboardButton("💹 مدیریت نرخ ارز، تتر و محاسبهٔ قیمت", callback_data="admin_auto_pricing", style="success")]]
    buttons.extend(
        [InlineKeyboardButton(label, callback_data=f"admin_auto_cat|{token}|1", style="primary")]
        for token, label in categories
    )
    buttons.append([InlineKeyboardButton("🔙 بازگشت به مدیریت", callback_data="admin")])
    return InlineKeyboardMarkup(buttons)


def auto_price_category_screen(context, token, page=1):
    token_map = context.user_data.get("auto_price_category_tokens") or {}
    entry = token_map.get(token)
    if not entry:
        # Fixed categories can be reconstructed after a restart.
        _auto_price_category_map(context)
        token_map = context.user_data.get("auto_price_category_tokens") or {}
        entry = token_map.get(token)
    if not entry:
        return ("⚠️ این دسته‌بندی دیگر در دسترس نیست. از مرکز مدیریت قیمت‌گذاری دوباره وارد شو.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 مرکز قیمت‌گذاری", callback_data="admin_auto_prices")]]))
    if token == "gifts":
        return (None, None)

    categories = tuple(entry["categories"])
    placeholders = ",".join("?" for _ in categories)
    with db() as con:
        rows = con.execute(
            f"SELECT id, name, price, currency, active FROM products WHERE category IN ({placeholders}) ORDER BY sort_order, id",
            categories,
        ).fetchall()
    per_page = 8
    total_pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(max(1, int(page)), total_pages)
    visible = rows[(page - 1) * per_page: page * per_page]
    lines = [f"⚙️ مدیریت قیمت‌گذاری | {entry['label']}", "━━━━━━━━━━━━━━━━━━"]
    buttons = []
    if not rows:
        lines.append("\nهنوز محصولی در این گروه ثبت نشده است.")
        lines.append("\nبا افزودن محصول در بخش «محصولات و قیمت‌ها»، اینجا به‌صورت خودکار نمایش داده می‌شود.")
    else:
        lines.append(f"\n📦 تعداد محصولات: {len(rows)} | صفحه {page} از {total_pages}\n")
        for row in visible:
            price = int(row["price"] or 0)
            currency = str(row["currency"] or "تومان")
            price_text = f"{toman(price)} {currency}" if price > 0 else "تنظیم نشده"
            status = "🟢 فعال" if int(row["active"] or 0) else "🔴 غیرفعال"
            lines.append(f"• {row['name']} | {price_text} | {status}")
            buttons.append([InlineKeyboardButton(f"{str(row['name'])[:42]} | مدیریت", callback_data=f"admin_auto_product|{row['id']}|{token}|{page}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"admin_auto_cat|{token}|{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"admin_auto_cat|{token}|{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("➕ افزودن / مدیریت محصولات", callback_data="admin_products")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت به فهرست محصولات", callback_data="admin_auto_prices")])
    return ("\n".join(lines)[:3900], InlineKeyboardMarkup(buttons))


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_channel_membership(update, context):
        return
    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("⛔ دسترسی مجاز نیست.")
        return

    current_mode = get_main_menu_type()
    mode_label = "⌨️ کیبورد معمولی" if current_mode == MENU_TYPE_KEYBOARD else "💎 دکمه‌های شیشه‌ای"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار فروشگاه", callback_data="admin_stats", style="primary"),
         InlineKeyboardButton("🛍 محصولات و قیمت‌ها", callback_data="admin_products", style="primary")],
        [InlineKeyboardButton("📦 آخرین سفارش‌ها", callback_data="admin_orders", style="primary"),
         InlineKeyboardButton("🏷️ مدیریت کدهای تخفیف", callback_data="admin_coupons", style="primary")],
        [InlineKeyboardButton("⚙️ مدیریت قیمت‌گذاری خودکار", callback_data="admin_auto_prices", style="primary"),
         InlineKeyboardButton("💬 پیام‌های پشتیبانی", callback_data="admin_support_tickets", style="success")],
        [InlineKeyboardButton(f"⌨️ تغییر به کیبورد معمولی{' ✅' if current_mode == MENU_TYPE_KEYBOARD else ''}", callback_data="admin_menu_set_keyboard", style="success"),
         InlineKeyboardButton(f"💎 تغییر به شیشه‌ای{' ✅' if current_mode == MENU_TYPE_INLINE else ''}", callback_data="admin_menu_set_inline", style="success")],
        [InlineKeyboardButton("📝 ویرایش متن‌ها و دکمه‌ها", callback_data="uiadm_home", style="primary")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
    ])
    await update.effective_message.reply_text(
        f"{bot_text('admin_title')}\n\n🔹 حالت منوی کل ربات: {mode_label}",
        reply_markup=keyboard,
    )


# ==========================================
# دکمه‌ها
# ==========================================

def persian_digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def build_stars_menu():
    """Show only the twelve approved amounts, one per row, with current prices when valid."""
    with db() as con:
        rows = con.execute(
            "SELECT id, name, star_amount, price, currency, button_text, icon, button_style, sort_order, active "
            "FROM products WHERE category='stars' AND star_amount IS NOT NULL ORDER BY id"
        ).fetchall()
    by_amount = {}
    for row in rows:
        try:
            amount = int(row["star_amount"] or 0)
        except (TypeError, ValueError):
            continue
        if amount in FIXED_STAR_AMOUNTS and amount not in by_amount and int(row["active"] or 0):
            by_amount[amount] = row

    cfg = load_auto_pricing_config()
    buttons = []
    for amount in FIXED_STAR_AMOUNTS:
        row = by_amount.get(amount)
        if row is None:
            continue
        live_quote = auto_pricing_quote("stars", amount, cfg)
        display_quote = live_quote or auto_pricing_quote("stars", amount, cfg, allow_stale=True)
        amount_text = persian_digits(f"{amount:,}".replace(",", "٬"))
        if live_quote:
            price_text = f"{persian_digits(toman(live_quote['sale_toman']))} تومان"
        elif int(row["price"] or 0) > 0:
            price_text = f"{persian_digits(toman(row['price']))} تومان"
        elif display_quote:
            price_text = f"{persian_digits(toman(display_quote['sale_toman']))} تومان"
        else:
            price_text = "تماس با پشتیبانی"
        label = f"✧ {amount_text} استارز | {price_text}"
        style = str(row["button_style"] or "primary").strip().lower()
        if style not in {"primary", "success", "danger"}:
            style = "primary"
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"stars_pack_{amount}", style=style)])

    # Keep the configured labels and semantic colors of the two requested bottom buttons.
    special_by_callback = {
        str(item.get("callback", "")): item
        for item in load_bot_settings().get("menus", {}).get("stars", [])
        if isinstance(item, dict)
    }
    defaults = {
        "stars_custom": {"text": "✎ مقدار دلخواه", "button_style": "primary"},
        "back_to_services": {"text": "‹ بازگشت", "button_style": "danger"},
    }
    for callback in ("stars_custom", "back_to_services"):
        item = special_by_callback.get(callback, {})
        fallback = defaults[callback]
        label = str(item.get("text") or fallback["text"]).strip()
        style = str(item.get("button_style") or fallback["button_style"]).strip().lower()
        if style not in {"primary", "success", "danger"}:
            style = fallback["button_style"]
        buttons.append([InlineKeyboardButton(label[:64], callback_data=callback, style=style)])
    return InlineKeyboardMarkup(buttons)


def custom_stars_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="customstars_digit_1", style="primary"), InlineKeyboardButton("2", callback_data="customstars_digit_2", style="primary"), InlineKeyboardButton("3", callback_data="customstars_digit_3", style="primary")],
        [InlineKeyboardButton("4", callback_data="customstars_digit_4", style="primary"), InlineKeyboardButton("5", callback_data="customstars_digit_5", style="primary"), InlineKeyboardButton("6", callback_data="customstars_digit_6", style="primary")],
        [InlineKeyboardButton("7", callback_data="customstars_digit_7", style="primary"), InlineKeyboardButton("8", callback_data="customstars_digit_8", style="primary"), InlineKeyboardButton("9", callback_data="customstars_digit_9", style="primary")],
        [InlineKeyboardButton("0", callback_data="customstars_digit_0", style="primary"), InlineKeyboardButton("⌫ پاک", callback_data="customstars_clear", style="danger"), InlineKeyboardButton("✅ تأیید", callback_data="customstars_confirm", style="success")],
        [InlineKeyboardButton("‹ بازگشت", callback_data="customstars_back", style="danger")],
    ])


def _manual_stars_unit_toman():
    """Use the administrator's saved per-Star rate, then derive from a saved fixed pack."""
    try:
        settings = load_premium_manager_settings()
        rate = _pricing_decimal(settings.get("star_rate_toman"))
        if rate is not None:
            return rate
    except Exception:
        logger.debug("Could not load manual Stars unit rate", exc_info=True)
    try:
        with db() as con:
            rows = con.execute("SELECT price, star_amount FROM products WHERE lower(category)='stars' AND active=1 AND price>0 AND star_amount>0 ORDER BY star_amount").fetchall()
        if rows:
            row = rows[0]
            return Decimal(str(row["price"])) / Decimal(str(row["star_amount"]))
    except Exception:
        logger.debug("Could not derive manual Stars unit rate", exc_info=True)
    return None


def _manual_stars_quote(amount):
    unit = _manual_stars_unit_toman()
    if unit is None:
        return None
    try:
        total = _ceil_toman(unit * Decimal(int(amount)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return {"product_type": "stars", "quantity": int(amount), "cost_toman": total,
            "sale_toman": total, "profit_toman": 0, "margin_percent": "0",
            "source": "قیمت دستی مدیر", "manual_price": True}


def custom_stars_display_text(raw_amount="", cfg=None):
    cfg = cfg or load_auto_pricing_config()
    raw_amount = normalize_ascii_digits(str(raw_amount or ""))
    raw_amount = re.sub(r"\D", "", raw_amount)[:7]
    unit = auto_pricing_unit_sale(cfg)
    if unit is None:
        cached_quote = auto_pricing_quote("stars", 50, cfg, allow_stale=True)
        unit = (Decimal(cached_quote["sale_toman"]) / Decimal(50)) if cached_quote else None
    if unit is None:
        unit = _manual_stars_unit_toman()
    unit_text = f"{persian_digits(format(unit, ',.2f').replace(',', '٬'))} تومان" if unit is not None else "—"
    if raw_amount:
        try:
            amount = int(raw_amount)
        except ValueError:
            amount = 0
    else:
        amount = 0
    amount_text = persian_digits(f"{amount:,}".replace(",", "٬")) if raw_amount else "—"
    if amount < 50:
        total_text = "برای محاسبه، حداقل ۵۰ استارز وارد کن."
    elif amount > 1_000_000:
        total_text = "❌ تعداد بیشتر از سقف ۱٬۰۰۰٬۰۰۰ استارز است."
    else:
        quote = auto_pricing_quote("stars", amount, cfg)
        display_quote = quote or auto_pricing_quote("stars", amount, cfg, allow_stale=True)
        if quote:
            total_text = f"{persian_digits(toman(quote['sale_toman']))} تومان"
        elif display_quote:
            total_text = f"{persian_digits(toman(display_quote['sale_toman']))} تومان"
        elif unit is not None:
            total_text = f"{persian_digits(toman(_ceil_toman(unit * Decimal(amount))))} تومان"
        else:
            total_text = "برای ثبت قیمت این مقدار با پشتیبانی تماس بگیر."
    return (
        "✎ مقدار دلخواه استارز\n\n"
        f"✧ هر استارز: {unit_text}\n"
        "▤ محدوده: 50 تا 1,000,000 استارز\n\n"
        "برای انتخاب تعداد 👇\n"
        f"🔢 تعداد انتخاب‌شده: {amount_text}\n"
        f"💰 مبلغ کل: {total_text}\n\n"
        f"{pricing_customer_status_text(cfg)}"
    )


def ensure_custom_stars_product():
    """Use one hidden product row for custom Stars orders; never add one row per amount."""
    with db() as con:
        row = con.execute("SELECT id FROM products WHERE category='stars' AND name='استارز مقدار دلخواه' ORDER BY id LIMIT 1").fetchone()
        if row:
            return int(row["id"])
        max_order = con.execute("SELECT COALESCE(MAX(sort_order), 0) FROM products WHERE category='stars'").fetchone()[0]
        cur = con.execute(
            "INSERT INTO products(category, name, price, active, icon, button_style, sort_order, description, currency) "
            "VALUES ('stars', 'استارز مقدار دلخواه', 0, 0, '✧', 'primary', ?, 'محصول داخلی برای ثبت سفارش مقدار دلخواه؛ در منوی بسته‌ها نمایش داده نمی‌شود.', 'تومان')",
            (int(max_order or 0) + 10,),
        )
        return int(cur.lastrowid)


def custom_stars_payment_keyboard(amount):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 کارت‌به‌کارت", callback_data=f"customstars_pay_card_{amount}", style="primary")],
        [InlineKeyboardButton("💰 پرداخت با کیف پول", callback_data=f"customstars_pay_wallet_{amount}", style="success")],
        [InlineKeyboardButton("❌ بازگشت به انتخاب گیرنده", callback_data="customstars_back_recipient", style="danger")],
        [InlineKeyboardButton("⌂ منوی اصلی", callback_data="home", style="primary")],
    ])


def custom_stars_customer_text(order, quote=None, warning=""):
    amount = int(order.get("amount") or 0)
    recipient = str(order.get("recipient") or "ثبت نشده")
    lines = [
        "🧾 تأیید سفارش استارز دلخواه | SEYED PREMIUM",
        "━━━━━━━━━━━━━━━━━━",
        f"⭐ تعداد: {persian_digits(f'{amount:,}'.replace(',', '٬'))} استارز",
        f"👤 گیرنده: {recipient}",
    ]
    if quote:
        lines.append(f"💰 قیمت نهایی: {toman(quote['sale_toman'])} تومان")
        unit = auto_pricing_unit_sale()
        if unit is not None:
            lines.append(f"✧ نرخ محاسبه‌شدهٔ تقریبی هر استارز: {format(unit, ',.2f').replace(',', '٬')} تومان")
    else:
        lines.append("💰 قیمت نهایی: فعلاً به‌دلیل نبود قیمت زنده ناموجود است.")
    if warning:
        lines.extend(["", warning])
    lines.extend(["", "روش پرداخت را انتخاب کن. تحویل فقط پس از بررسی رسید/پرداخت و تأیید دستی مدیر انجام می‌شود."])
    return "\n".join(lines)


def custom_stars_order_details(order, quote):
    amount = int(order.get("amount") or 0)
    recipient = str(order.get("recipient") or "ثبت نشده")
    return (
        f"مقدار: {amount} استارز؛ گیرنده: {recipient}; "
        + quote_breakdown_text(quote).replace("\n", "؛ ")
        + ("؛ بیش از سقف یک سفارش API تأمین‌کننده؛ تأمین دستی نیازمند بررسی است" if quote.get("provider_over_limit") else "")
    )


def jalali_date(value):
    import jdatetime
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    if not value:
        return "نامشخص"
    try:
        # زمان CURRENT_TIMESTAMP در SQLite بر اساس UTC ذخیره می‌شود.
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo("Asia/Tehran")
        )
        jd = jdatetime.datetime.fromgregorian(datetime=dt.replace(tzinfo=None))
        # اعداد انگلیسی و نمایش ساعت با دقت ثانیه
        return jd.strftime("%Y/%m/%d — %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


async def _safe_callback_answer(query):
    """Acknowledge Telegram callback once; never recursively schedule itself."""
    try:
        await query.answer()
    except Exception:
        logger.debug("Callback answer could not be sent", exc_info=True)


GLOBAL_CANCEL_STATE_KEYS = (
    # Admin forms and editors
    "gift_admin_input", "premium_admin_input", "gift_custom_state",
    "support_admin_reply_state", "support_admin_search_state", "gift_order_search_state",
    "pricing_admin_state", "product_admin_state", "uiadm_state", "editor_wait_key",
    "coupon_edit_state", "coupon_edit_categories", "coupon_category_add_state",
    "coupon_scope_state", "admin_coupon_state",
    # Customer order forms that have not yet created an order
    "mediator_request_state", "ton_purchase_state",
    "stars_custom_amount", "stars_custom_order", "stars_custom_wait_recipient",
    "stars_fixed_wait_recipient", "stars_custom_chat_id", "stars_custom_message_id",
    "stars_recipient", "stars_coupon", "pending_stars_coupon",
    "pending_product_id", "pending_dynamic_quote", "pending_gift_delivery_mode",
    "pending_gift_delivery_product_id", "pending_gift_price_quote",
    "pending_product_coupon", "pending_product_coupon_input",
    # VPN/VIP configuration input states
    "config_duration", "config_invoice_amount", "config_invoice_expires_at",
    "config_invoice_open", "config_plan_type", "config_price", "config_volume",
    "config_wait_duration", "config_wait_volume", "vip_duration", "vip_invoice_amount",
    "vip_payment_open", "vip_plan_type", "vip_price", "vip_volume",
    "vip_wait_duration", "vip_wait_volume",
)


def clear_active_operation_state(context):
    """Clear only transient forms; never delete saved orders, payment receipts or settings."""
    cleared = []
    for key in GLOBAL_CANCEL_STATE_KEYS:
        if key in context.user_data:
            context.user_data.pop(key, None)
            cleared.append(key)
    if context.user_data.pop("support_form_active", None):
        cleared.append("support_form_active")
    return cleared


def safe_premium_admin_keyboard():
    try:
        return premium_admin_keyboard()
    except Exception:
        logger.exception("Could not build premium admin keyboard; using fallback")
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin")]])


async def safe_callback_edit_or_reply(query, text, reply_markup=None):
    """Render a callback result without allowing an edit failure to become a handler crash."""
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
        return True
    except Exception as edit_exc:
        logger.warning("Callback message edit failed; falling back to a new message: %s", edit_exc, exc_info=True)
        try:
            source_message = getattr(query, "message", None)
            if source_message is not None:
                await source_message.reply_text(text=text, reply_markup=reply_markup)
                return True
        except Exception:
            logger.exception("Callback fallback reply failed")
        try:
            await query.answer(str(text).replace("\n", " ")[:190], show_alert=True)
        except Exception:
            logger.exception("Callback alert fallback failed")
        return False


async def handle_global_cancel_callback(update, context):
    """Global cancel for every button visibly labelled '❌ لغو عملیات'."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    await _safe_callback_answer(query)
    cleared = clear_active_operation_state(context)
    if "support_form_active" in cleared:
        try:
            with db() as con:
                con.execute("DELETE FROM support_form_sessions WHERE user_id=?", (user.id,))
        except Exception:
            logger.exception("Could not remove cancelled support form session")

    if user.id == ADMIN_ID:
        if "premium_admin_input" in cleared:
            destination = "premium_admin_home"
            title = "مدیریت پرمیوم"
        elif "gift_admin_input" in cleared or "gift_custom_state" in cleared or "gift_order_search_state" in cleared:
            destination = "gift_admin_home"
            title = "مدیریت گیفت"
        elif "pricing_admin_state" in cleared:
            destination = "admin_auto_pricing"
            title = "قیمت‌گذاری خودکار"
        elif "product_admin_state" in cleared:
            destination = "admin_products"
            title = "مدیریت محصولات"
        else:
            destination = "admin"
            title = "پنل مدیریت"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔙 بازگشت به {title}", callback_data=destination)]])
    else:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]])

    if cleared:
        message = "❌ عملیات جاری لغو شد؛ هیچ مقدارِ ذخیره‌نشده‌ای ثبت نشد."
        if context.user_data.get("pending_receipt_order_id"):
            message += "\nسفارشِ از قبل ثبت‌شده و منتظر رسید تغییر نکرده است؛ برای لغو آن از دکمهٔ «لغو سفارش» استفاده کن."
    else:
        message = "عملیات ورودیِ فعالی برای لغو پیدا نشد."
    await safe_callback_edit_or_reply(query, message, reply_markup=markup)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _handler_started = time.perf_counter()
    try:
        await _callbacks_impl(update, context)
    except Exception as exc:
        logger.exception("Unhandled callback failure; update_id=%s", getattr(update, "update_id", None))
        query = update.callback_query
        if query:
            # For the two live-rate buttons, return a useful diagnostic instead of
            # leaving the spinner/progress text on screen.
            if query.data in {"premium_auto_star_rate_toggle", "premium_auto_fragment_toggle"}:
                try:
                    await safe_callback_edit_or_reply(
                        query,
                        f"⚠️ عملیات نرخ خودکار کامل نشد ({type(exc).__name__}: {str(exc)[:350]}). حالت دستی/قبلی حفظ شد؛ جزئیات کامل در لاگ ثبت شد.",
                        safe_premium_admin_keyboard(),
                    )
                    return
                except Exception:
                    logger.exception("Could not render premium pricing callback diagnostic")
            try:
                await query.answer(f"خطا: {type(exc).__name__}؛ جزئیات در لاگ ثبت شد.", show_alert=True)
            except Exception:
                logger.exception("Could not notify user about callback failure")
    finally:
        _query = update.callback_query
        _button = _query.data if _query else "unknown"
        logger.info(
            "PERF callback=%s duration=%.3fs",
            _button,
            time.perf_counter() - _handler_started,
        )


async def _show_product_category(query, category, user_id):
    """Render any database-backed product category without a hard-coded category whitelist."""
    if str(category or "").lower() in {"stars", "premium"}:
        await refresh_auto_pricing(force=False)
    pricing_cfg = load_auto_pricing_config()
    with db() as con:
        rows = con.execute(
            "SELECT id, name, price, button_text, icon, button_style, sort_order, currency, old_price FROM products "
            "WHERE category=? AND active=1 ORDER BY sort_order, id",
            (category,),
        ).fetchall()
    labels = {
        "premium": "💎 تلگرام پرمیوم", "stars": "⭐ تلگرام استارز",
        "gifts": "🎁 گیفت تلگرام", "boost": "🚀 بوست تلگرام",
        "numbers": "📱 شماره مجازی", "members": "👥 ممبر تلگرام",
        "reactions": "💙 ری‌اکشن تلگرام", "views": "👀 ویو تلگرام",
        "instagram_followers": "📸 فالوور اینستاگرام",
        "instagram_likes": "💙 لایک اینستاگرام",
    }
    title = labels.get(category, f"🛍 {category}")
    price_status = ""
    if not rows:
        await query.edit_message_text(
            f"{title}\n\nفعلاً محصول فعالی در این دسته ثبت نشده است.{price_status}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ بازگشت به خدمات", callback_data="cat_stars")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
            ]),
        )
        return
    keyboard = []
    for row in rows:
        price = int(row["price"] or 0)
        currency = str(row["currency"] or "تومان")
        if str(category or "").lower() in {"stars", "premium"}:
            live_quote = dynamic_product_quote(row, pricing_cfg)
            if live_quote:
                price_text = f"{toman(live_quote['sale_toman'])} تومان"
            elif int(row["price"] or 0) > 0:
                price_text = f"{toman(row['price'])} تومان ⚠️"
            else:
                price_text = "نرخ در حال بازیابی"
        else:
            price_text = f"{price:,} {currency}" if price > 0 else "استعلام قیمت"
            old_price = int(row["old_price"] or 0)
            if old_price > price > 0:
                price_text = f"{old_price:,} ← {price:,} {currency}"
        label = str(row["button_text"] or "").strip() or str(row["name"])
        icon = str(row["icon"] or "").strip()
        style = str(row["button_style"] or "success").strip().lower()
        if style not in {"primary", "success", "danger"}:
            style = "success"
        keyboard.append([InlineKeyboardButton(
            f"{icon + ' ' if icon else ''}{label} | {price_text}"[:64],
            callback_data=f"buy_{row['id']}", style=style
        )])
    keyboard.extend([
        [InlineKeyboardButton("⬅️ بازگشت به خدمات", callback_data="cat_stars")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
    ])
    await query.edit_message_text(
        f"{title}\n\nمحصول موردنظر را انتخاب کن.\nقیمت صفر یعنی قیمت هنوز توسط مدیر تنظیم نشده است.{price_status}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )



async def handle_gift_support_callback(query, context):
    """Return True when a gift/support management callback has been handled."""
    data = str(query.data or "")
    user_id = int(query.from_user.id)

    if data == "support":
        # Save the active form state so a customer can finish after a restart.
        with db() as con:
            con.execute(
                "INSERT INTO support_form_sessions(user_id,started_at) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET started_at=excluded.started_at",
                (user_id, iran_timestamp_now()),
            )
        context.user_data["support_form_active"] = True
        form = support_form_text(query.from_user)
        copy_form = support_form_copy_text(query.from_user)
        await query.edit_message_text(
            form + "\n\n📋 روی دکمه کپی بزن، فرم کوتاه را تکمیل کن و همین‌جا برای ربات بفرست.",
            reply_markup=support_form_markup(copy_form),
        )
        return True

    if data == "support_cancel":
        context.user_data.pop("support_form_active", None)
        with db() as con:
            con.execute("DELETE FROM support_form_sessions WHERE user_id=?", (user_id,))
        await query.edit_message_text("❌ فرم پشتیبانی لغو شد؛ هیچ تیکتی ثبت نشد.", reply_markup=back_menu())
        return True

    if data.startswith("support_ticket_continue_"):
        ticket_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            con.execute("INSERT INTO support_active_tickets(user_id,ticket_id,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET ticket_id=excluded.ticket_id,updated_at=excluded.updated_at", (user_id, ticket_id, iran_timestamp_now()))
        await query.edit_message_text("💬 گفت‌وگوی پشتیبانی فعال است. پیام متنی، عکس، ویدئو یا فایل خود را همین‌جا بفرست. برای پایان، دکمه زیر یا /cancel را بزن.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ پایان گفت‌وگو", callback_data="support_ticket_close")]]))
        return True

    if data == "support_ticket_close":
        with db() as con:
            con.execute("DELETE FROM support_active_tickets WHERE user_id=?", (user_id,))
        await query.edit_message_text("✅ گفت‌وگوی ارسال پیام پایان یافت. تیکت و سوابق آن برای پیگیری مدیر محفوظ است.", reply_markup=back_menu())
        return True

    # Customer chooses only between a supported automatic send or manual handling.
    if data.startswith("gift_customer_delivery_"):
        if data == "gift_customer_delivery_cancel":
            for key in ("pending_product_id", "pending_gift_delivery_product_id", "pending_gift_delivery_mode", "pending_dynamic_quote"):
                context.user_data.pop(key, None)
            await query.edit_message_text("❌ سفارش لغو شد؛ سفارشی ثبت نشد.", reply_markup=back_menu())
            return True
        try:
            product_id = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await query.answer("شناسه محصول نامعتبر است.", show_alert=True)
            return True
        if context.user_data.get("pending_gift_delivery_product_id") != product_id or context.user_data.get("pending_product_id") != product_id:
            await query.answer("این انتخاب منقضی شده است؛ محصول را دوباره انتخاب کن.", show_alert=True)
            return True
        with db() as con:
            product = con.execute("SELECT * FROM products WHERE id=? AND category='gifts' AND active=1", (product_id,)).fetchone()
        cfg = load_gift_settings()
        if not product:
            await query.answer("محصول دیگر فعال نیست.", show_alert=True)
            return True
        if data.startswith("gift_customer_delivery_auto_"):
            if cfg["manual_delivery_all"] or product["source_type"] != "official" or not product["telegram_gift_id"] or product["delivery_mode"] != "auto":
                await query.answer("ارسال خودکار برای این محصول در حال حاضر در دسترس نیست.", show_alert=True)
                return True
            mode = "auto"
        else:
            mode = "manual"
        context.user_data["pending_gift_delivery_mode"] = mode
        context.user_data.pop("pending_gift_delivery_product_id", None)
        prompt = product_details_prompt(product)
        await query.edit_message_text(
            f"✅ روش تحویل انتخاب شد: {'ارسال خودکار توسط ربات' if mode == 'auto' else 'تحویل دستی توسط پشتیبانی'}\n\n"
            f"🛍 محصول: {product['name']}\n\n{prompt}\n\nبرای لغو، دکمهٔ لغو سفارش را بزن.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو سفارش", callback_data="gift_customer_delivery_cancel", style="danger")]])
        )
        return True

    # Admin-only routes below this point.
    admin_prefixes = (
        "gift_", "admin_support_tickets", "admin_ticket_", "admin_support_search",
        "admin_ticket_search", "gift_order_search", "gift_order_view_",
    )
    if not (data == "gift_admin_home" or data.startswith(admin_prefixes)):
        return False
    if user_id != ADMIN_ID:
        await query.answer("دسترسی فقط برای مدیر اصلی است.", show_alert=True)
        return True

    if data == "gift_admin_home":
        await _show_gift_admin_home(query, context.bot)
        return True

    if data == "gift_sync":
        await query.edit_message_text("🔄 در حال دریافت فهرست رسمی گیفت‌ها از تلگرام...")
        try:
            count = await sync_official_gift_catalog(context.bot)
            await _show_gift_admin_home(query, context.bot, f"✅ فهرست رسمی با موفقیت به‌روزرسانی شد. تعداد گیفت‌های دریافت‌شده: {count}")
        except Exception as exc:
            logger.exception("Official Telegram gift catalogue sync failed")
            settings = load_gift_settings()
            settings["catalog_last_error"] = str(exc)[:500]
            save_gift_settings(settings)
            await _show_gift_admin_home(query, context.bot, "❌ دریافت فهرست رسمی ناموفق بود. فهرست قبلی حفظ شد.\n" + str(exc)[:350])
        return True

    if data.startswith("gift_catalog_page_"):
        page = int(data.rsplit("_", 1)[1])
        text_value, markup = gift_catalog_page_text_and_markup(page, delivery=False)
        await query.edit_message_text(text_value, reply_markup=markup)
        return True

    if data.startswith("gift_delivery_page_"):
        page = int(data.rsplit("_", 1)[1])
        text_value, markup = gift_catalog_page_text_and_markup(page, delivery=True)
        await query.edit_message_text(text_value, reply_markup=markup)
        return True

    if data.startswith("gift_toggle_"):
        product_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            row = con.execute("SELECT id,active,price,star_count,source_type FROM products WHERE id=? AND category='gifts'", (product_id,)).fetchone()
            if not row:
                await query.answer("این گیفت پیدا نشد.", show_alert=True)
                return True
            new_active = 0 if int(row["active"] or 0) else 1
            if new_active and int(row["price"] or 0) <= 0:
                await query.answer("قیمت معتبر ندارد؛ ابتدا نرخ پایه یا منبع قیمت خودکار را تنظیم کن.", show_alert=True)
                return True
            con.execute("UPDATE products SET active=? WHERE id=?", (new_active, product_id))
        text_value, markup = gift_catalog_page_text_and_markup(int(context.user_data.get("gift_catalog_page", 1)), delivery=False)
        await query.answer("گیفت فعال شد." if new_active else "گیفت غیرفعال شد.")
        await query.edit_message_text(text_value, reply_markup=markup)
        return True

    if data == "gift_global_manual":
        cfg = load_gift_settings()
        cfg["manual_delivery_all"] = not cfg["manual_delivery_all"]
        save_gift_settings(cfg)
        await query.answer("تحویل دستی همه روشن شد." if cfg["manual_delivery_all"] else "تحویل دستی همه خاموش شد.")
        await _show_gift_admin_home(query, context.bot)
        return True

    if data == "gift_customer_choice":
        cfg = load_gift_settings()
        cfg["customer_delivery_choice"] = not cfg["customer_delivery_choice"]
        save_gift_settings(cfg)
        await query.answer("انتخاب روش ارسال توسط مشتری روشن شد." if cfg["customer_delivery_choice"] else "انتخاب روش ارسال توسط مشتری خاموش شد.")
        await _show_gift_admin_home(query, context.bot)
        return True

    if data == "gift_delivery_page_1":
        text_value, markup = gift_catalog_page_text_and_markup(1, delivery=True)
        await query.edit_message_text(text_value, reply_markup=markup)
        return True

    if data.startswith("gift_delivery_manual_"):
        await query.answer("این گیفت فقط به‌صورت دستی تحویل داده می‌شود.", show_alert=True)
        return True

    if data.startswith("gift_delivery_toggle_"):
        product_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            row = con.execute("SELECT source_type,telegram_gift_id,delivery_mode FROM products WHERE id=? AND category='gifts'", (product_id,)).fetchone()
            if not row:
                await query.answer("گیفت پیدا نشد.", show_alert=True)
                return True
            if row["source_type"] != "official" or not row["telegram_gift_id"]:
                await query.answer("تحویل خودکار فقط برای گیفت رسمیِ دارای شناسه تلگرام فعال می‌شود.", show_alert=True)
                return True
            new_mode = "manual" if str(row["delivery_mode"] or "manual") == "auto" else "auto"
            con.execute("UPDATE products SET delivery_mode=? WHERE id=?", (new_mode, product_id))
        text_value, markup = gift_catalog_page_text_and_markup(1, delivery=True)
        await query.answer("تحویل خودکار انتخاب شد؛ ارسال فقط پس از تأیید پرداخت و موفقیت API انجام می‌شود." if new_mode == "auto" else "تحویل دستی انتخاب شد.")
        await query.edit_message_text(text_value, reply_markup=markup)
        return True

    if data in {"gift_base_rate", "gift_auto_rate", "gift_profit", "gift_usd_settings"}:
        await safe_callback_edit_or_reply(
            query,
            "💹 نرخ و سود همهٔ قیمت‌های خودکار، از جمله گیفت‌های رسمی، یکپارچه شده است.\n"
            "برای تغییر آن‌ها وارد مرکز مدیریت قیمت‌گذاری خودکار شو:",
            pricing_panel_keyboard(),
        )
        return True

    if data == "gift_support_first":
        cfg = load_gift_settings()
        cfg["support_first"] = not cfg["support_first"]
        save_gift_settings(cfg)
        await query.answer("پیام پشتیبانی قبل از تحویل روشن شد." if cfg["support_first"] else "پیام پشتیبانی قبل از تحویل خاموش شد.")
        await _show_gift_admin_home(query, context.bot)
        return True

    if data == "gift_support_text":
        context.user_data["gift_admin_input"] = {"field": "support_text"}
        await query.edit_message_text(
            "📝 متن پیام پشتیبانی را بفرست. این متن فقط در صورت نیاز به هماهنگی تحویل برای مشتری ارسال می‌شود.\n\nمتن فعلی:\n" + load_gift_settings()["support_text"],
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="danger")], [InlineKeyboardButton("🔙 بازگشت", callback_data="gift_admin_home")]])
        )
        return True

    if data == "gift_input_cancel":
        context.user_data.pop("gift_admin_input", None)
        await query.edit_message_text("❌ عملیات لغو شد؛ تنظیمی ذخیره نشد.", reply_markup=gift_admin_home_keyboard())
        return True

    if data == "gift_custom_add":
        context.user_data["gift_custom_state"] = {"step": "link"}
        await query.edit_message_text(
            "🔗 افزودن گیفت سفارشی\n\n"
            "این بخش برای گیفت یا NFT سفارشی است که ربات آن را از کاتالوگ رسمی ارسال نمی‌کند؛ تحویل آن دستی خواهد بود.\n\n"
            "ابتدا لینک کامل گیفت را بفرست؛ نمونه: https://t.me/nft/MoodPack-134232\n\nبرای لغو /cancel را بزن.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="danger")]])
        )
        return True

    if data == "gift_custom_cancel":
        context.user_data.pop("gift_custom_state", None)
        await query.edit_message_text("❌ افزودن گیفت سفارشی لغو شد؛ چیزی ذخیره نشد.", reply_markup=gift_admin_home_keyboard())
        return True

    if data == "gift_star_topup":
        try:
            balance = await get_real_bot_star_balance(context.bot)
            balance_text = f"⭐ {toman(balance)}"
        except Exception as exc:
            balance_text = "دریافت موجودی ناموفق: " + str(exc)[:120]
        await query.edit_message_text(
            "⭐ شارژ موجودی واقعی Stars ربات\n\n"
            f"موجودی فعلی ربات: {balance_text}\n\n"
            "پرداخت فقط با Stars و از طریق فاکتور رسمی تلگرام انجام می‌شود. پس از پرداخت موفق، نتیجه از پیام پرداخت تلگرام ثبت می‌شود.\n\n"
            "این موجودی برای روش‌های رسمی ارسال پشتیبانی‌شده استفاده می‌شود؛ ارسال مستقیم Stars به کاربر با ارسال گیفت یکی نیست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("100 ⭐", callback_data="gift_topup_100"), InlineKeyboardButton("250 ⭐", callback_data="gift_topup_250"), InlineKeyboardButton("500 ⭐", callback_data="gift_topup_500")],
                [InlineKeyboardButton("1,000 ⭐", callback_data="gift_topup_1000"), InlineKeyboardButton("2,500 ⭐", callback_data="gift_topup_2500"), InlineKeyboardButton("5,000 ⭐", callback_data="gift_topup_5000")],
                [InlineKeyboardButton("✍️ تعداد دلخواه", callback_data="gift_topup_custom")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="gift_admin_home")],
            ])
        )
        return True

    if data.startswith("gift_topup_") and data != "gift_topup_custom":
        amount_raw = data.rsplit("_", 1)[1]
        if amount_raw.isdigit():
            await _create_owner_star_invoice(query, context, int(amount_raw))
        return True

    if data == "gift_topup_custom":
        context.user_data["gift_admin_input"] = {"field": "topup_amount"}
        await query.edit_message_text("✍️ تعداد Stars موردنظر را فقط با عدد بفرست (۱ تا ۱۰۰٬۰۰۰). برای لغو /cancel را بزن.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel")]]))
        return True

    if data == "gift_orders":
        text_value, markup = gift_orders_page(1)
        await query.edit_message_text(text_value, reply_markup=markup)
        return True

    if data.startswith("gift_orders_page_"):
        text_value, markup = gift_orders_page(int(data.rsplit("_", 1)[1]))
        await query.edit_message_text(text_value, reply_markup=markup)
        return True

    if data.startswith("gift_order_view_"):
        order_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            row = con.execute(
                "SELECT o.*,p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=? AND lower(p.category)='gifts'",
                (order_id,),
            ).fetchone()
        if not row:
            await query.edit_message_text("سفارش گیفت پیدا نشد.", reply_markup=gift_admin_home_keyboard())
            return True
        buttons = [
            [InlineKeyboardButton("👤 پرونده خریدار / سفارش‌ها", callback_data=f"admin_support_search_id_{row['user_id']}")],
            [InlineKeyboardButton("🎯 جست‌وجو با ID گیرنده", callback_data=f"admin_support_search_id_{row['recipient_user_id'] or 0}")],
        ]
        if row["receipt_file_id"]:
            buttons.append([InlineKeyboardButton("🧾 مشاهده رسید", callback_data=f"gift_order_receipt_{order_id}")])
        if row["status"] in {"پرداخت تأیید شد - در انتظار تحویل", "تأیید شده"} and row["delivered_at"] is None:
            buttons.append([InlineKeyboardButton("✅ ثبت تحویل دستی انجام‌شده", callback_data=f"gift_manual_delivered_{order_id}", style="success")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت به سفارش‌های گیفت", callback_data="gift_orders")])
        await query.edit_message_text(gift_order_detail_text(row), reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if data.startswith("gift_order_receipt_"):
        order_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            row = con.execute("SELECT receipt_file_id FROM orders WHERE id=?", (order_id,)).fetchone()
        if row and row["receipt_file_id"]:
            try:
                await context.bot.send_photo(chat_id=ADMIN_ID, photo=row["receipt_file_id"], caption=f"🧾 رسید سفارش گیفت #{order_id}")
                await query.answer("رسید به گفت‌وگوی مدیریت ارسال شد.")
            except Exception as exc:
                await query.answer("نمایش رسید ناموفق بود: " + str(exc)[:150], show_alert=True)
        else:
            await query.answer("برای این سفارش رسیدی ثبت نشده است.", show_alert=True)
        return True

    if data.startswith("gift_manual_delivered_yes_"):
        order_id = int(data.rsplit("_", 1)[1])
        now = iran_timestamp_now()
        with db() as con:
            row = con.execute("SELECT user_id,status,delivered_at,payment_verified_at,order_code FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                await query.answer("سفارش پیدا نشد.", show_alert=True)
                return True
            if row["delivered_at"]:
                await query.answer("این سفارش قبلاً تحویل‌شده ثبت شده است.", show_alert=True)
                return True
            if row["status"] not in {"پرداخت تأیید شد - در انتظار تحویل", "تأیید شده"}:
                await query.answer("ابتدا باید پرداخت سفارش تأیید شده باشد.", show_alert=True)
                return True
            updated = con.execute("UPDATE orders SET status='تحویل‌شده',delivered_at=?,delivery_error='' WHERE id=? AND delivered_at IS NULL", (now, order_id))
            if updated.rowcount != 1:
                await query.answer("وضعیت سفارش هم‌زمان تغییر کرده؛ دوباره جزئیات را باز کن.", show_alert=True)
                return True
            log_order_event(con, order_id, "gift_delivered_manual", "تحویل‌شده", "مدیر تحویل دستی را تأیید کرد.", ADMIN_ID)
            customer_id = int(row["user_id"])
            order_code = row["order_code"] or f"#{order_id}"
        try:
            await context.bot.send_message(
                chat_id=customer_id,
                text=f"✅ تحویل سفارش گیفت شما توسط مدیریت ثبت شد.\nکد سفارش: {order_code}\nزمان تحویل: {format_iran_jalali_datetime(now)}",
            )
            notify = "وضعیت تحویل ثبت و مشتری مطلع شد."
        except Exception as exc:
            logger.exception("Manual delivery confirmation saved but customer notification failed")
            notify = "تحویل در دیتابیس ثبت شد، اما اطلاع‌رسانی به مشتری ناموفق بود: " + str(exc)[:120]
        await query.edit_message_text(f"✅ سفارش {order_code} به‌عنوان تحویل‌شده ثبت شد.\n{notify}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 سفارش‌های گیفت", callback_data="gift_orders")]]))
        return True

    if data.startswith("gift_manual_delivered_"):
        order_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            row = con.execute("SELECT status,delivered_at,order_code FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            await query.answer("سفارش پیدا نشد.", show_alert=True)
            return True
        if row["delivered_at"]:
            await query.answer("این سفارش قبلاً تحویل‌شده ثبت شده است.", show_alert=True)
            return True
        if row["status"] not in {"پرداخت تأیید شد - در انتظار تحویل", "تأیید شده"}:
            await query.answer("ابتدا باید پرداخت سفارش تأیید شده باشد.", show_alert=True)
            return True
        await query.edit_message_text(
            f"⚠️ تأیید تحویل دستی\n\nآیا واقعاً گیفت سفارش {row['order_code'] or ('#'+str(order_id))} را خارج از ربات برای گیرنده ارسال کرده‌ای؟\n\nفقط پس از ارسال واقعی، تأیید کن. این کار زمان تحویل را ثبت می‌کند و به مشتری اطلاع می‌دهد.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، واقعاً تحویل داده‌ام", callback_data=f"gift_manual_delivered_yes_{order_id}", style="success")],
                [InlineKeyboardButton("❌ نه، بازگشت", callback_data=f"gift_order_view_{order_id}", style="danger")],
            ]),
        )
        return True

    if data == "gift_order_search":
        context.user_data["gift_order_search_state"] = True
        await query.edit_message_text("🔎 کد سفارش، ID عددی خریدار/گیرنده یا آیدی تلگرام (@username) را بفرست.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو جست‌وجو", callback_data="gift_search_cancel")]]))
        return True

    if data == "gift_search_cancel":
        context.user_data.pop("gift_order_search_state", None)
        await query.edit_message_text("جست‌وجوی سفارش لغو شد.", reply_markup=gift_admin_home_keyboard())
        return True

    if data.startswith("admin_support_search_id_"):
        search_value = data.rsplit("_", 1)[1]
        rows = find_orders_for_support(search_value)
        await query.edit_message_text(render_order_search_results(rows, search_value), reply_markup=order_search_results_keyboard(rows))
        return True

    if data == "admin_support_tickets":
        with db() as con:
            rows = con.execute("SELECT id,ticket_code,status,buyer_user_id,product_name,created_at FROM support_tickets ORDER BY id DESC LIMIT 12").fetchall()
            total = int(con.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0])
        lines = [f"💬 مدیریت پیام‌های پشتیبانی | کل تیکت‌ها: {total}", "━━━━━━━━━━━━━━━━━━"]
        buttons=[]
        for row in rows:
            lines.append(f"\n{row['ticket_code']} | {row['status']}\nID خریدار: {row['buyer_user_id']} | {row['product_name']}\n{format_iran_jalali_datetime(row['created_at'])}")
            buttons.append([InlineKeyboardButton(f"📩 {row['ticket_code']} | {row['status']}", callback_data=f"admin_ticket_view_{row['id']}")])
        if not rows:
            lines.append("\nهنوز تیکتی ثبت نشده است.")
        buttons.extend([[InlineKeyboardButton("🔎 جست‌وجو با ID / @ / کد سفارش", callback_data="admin_support_search")],
                        [InlineKeyboardButton("🔙 بازگشت به مدیریت", callback_data="admin")]])
        await query.edit_message_text("\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if data == "admin_support_search":
        context.user_data["support_admin_search_state"] = True
        await query.edit_message_text("🔎 ID عددی خریدار، ID گیرنده، @username، کد سفارش یا شماره تیکت را بفرست.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو جست‌وجو", callback_data="admin_support_search_cancel")]]))
        return True

    if data == "admin_support_search_cancel":
        context.user_data.pop("support_admin_search_state", None)
        await query.edit_message_text("جست‌وجو لغو شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 مدیریت تیکت‌ها", callback_data="admin_support_tickets")]]))
        return True

    if data.startswith("admin_ticket_view_"):
        ticket_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            detail = support_ticket_detail(con, ticket_id)
        if not detail:
            await query.edit_message_text("تیکت پیدا نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تیکت‌ها", callback_data="admin_support_tickets")]]))
        else:
            await query.edit_message_text(detail[:3900], reply_markup=create_support_ticket_keyboard(ticket_id))
        return True

    if data.startswith("admin_ticket_attachments_"):
        ticket_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            ticket = con.execute("SELECT ticket_code FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
            files = con.execute("SELECT telegram_file_id,media_type,message_text,created_at FROM support_ticket_messages WHERE ticket_id=? AND telegram_file_id!='' ORDER BY id", (ticket_id,)).fetchall()
        if not ticket:
            await query.answer("تیکت پیدا نشد.", show_alert=True)
            return True
        sent = 0
        for item in files:
            caption = f"📎 پیوست تیکت {ticket['ticket_code']}\nزمان: {format_iran_jalali_datetime(item['created_at'])}\n{str(item['message_text'] or '')}"
            try:
                await deliver_support_attachment_to_admin(context.bot, item["telegram_file_id"], item["media_type"], caption)
                sent += 1
            except Exception:
                logger.exception("Could not resend support attachment for ticket=%s", ticket["ticket_code"])
        await query.answer(f"{sent} پیوست از {len(files)} پیوست دوباره ارسال شد.", show_alert=True)
        return True

    if data == "admin_ticket_reply_cancel":
        context.user_data.pop("support_admin_reply_state", None)
        await query.edit_message_text("❌ پاسخ‌گویی لغو شد؛ پیامی برای مشتری ارسال نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تیکت‌ها", callback_data="admin_support_tickets")]]))
        return True

    if data.startswith("admin_ticket_reply_"):
        ticket_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            row = con.execute("SELECT ticket_code,sender_user_id FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            await query.answer("تیکت پیدا نشد.", show_alert=True)
            return True
        context.user_data["support_admin_reply_state"] = {"ticket_id": ticket_id, "chat_id": query.message.chat_id, "message_id": query.message.message_id}
        await query.edit_message_text(f"↩️ پاسخ به مشتری | {row['ticket_code']}\n\nمتن پاسخ خود را بفرست. پاسخ از طریق ربات برای مشتری ارسال می‌شود.\nبرای لغو /cancel را بزن.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو پاسخ", callback_data="admin_ticket_reply_cancel")]]))
        return True

    if data.startswith("admin_ticket_status_"):
        parts = data.split("_")
        new_status = {"review": "در حال بررسی", "replied": "پاسخ داده‌شده", "closed": "بسته‌شده"}.get(parts[-2])
        ticket_id = int(parts[-1])
        if not new_status:
            await query.answer("وضعیت نامعتبر است.", show_alert=True)
            return True
        with db() as con:
            con.execute("UPDATE support_tickets SET status=?,updated_at=? WHERE id=?", (new_status, iran_timestamp_now(), ticket_id))
            if new_status == "بسته‌شده":
                con.execute("DELETE FROM support_active_tickets WHERE ticket_id=?", (ticket_id,))
        with db() as con:
            detail = support_ticket_detail(con, ticket_id)
        await query.edit_message_text(detail[:3900] if detail else "تیکت پیدا نشد.", reply_markup=create_support_ticket_keyboard(ticket_id))
        return True

    if data.startswith("admin_ticket_order_"):
        ticket_id = int(data.rsplit("_", 1)[1])
        with db() as con:
            ticket = con.execute("SELECT order_id,order_code,buyer_user_id,recipient_user_id FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
        if not ticket:
            await query.answer("تیکت پیدا نشد.", show_alert=True)
            return True
        if ticket["order_id"]:
            # Re-use the existing order detail screen.
            from types import SimpleNamespace
            q = query
            q.data = f"admin_order_view_{ticket['order_id']}"
            await _callbacks_impl(update_from_query(query), context)
        else:
            rows = find_orders_for_support(str(ticket["buyer_user_id"]))
            await query.edit_message_text("سفارش دقیق به تیکت متصل نبود. سفارش‌های این ID:", reply_markup=order_search_results_keyboard(rows))
        return True

    return False


def update_from_query(query):
    """Build a minimal Update wrapper for the existing order-detail callback route."""
    from types import SimpleNamespace
    return SimpleNamespace(callback_query=query, effective_user=query.from_user, effective_chat=query.message.chat, effective_message=query.message)


def find_orders_for_support(search_value):
    value = str(search_value or "").strip()
    normalized = normalize_ascii_digits(value)
    with db() as con:
        if normalized.upper().startswith("SP-T"):
            ticket = con.execute("SELECT order_id FROM support_tickets WHERE upper(ticket_code)=?", (normalized.upper(),)).fetchone()
            if ticket and ticket["order_id"]:
                return con.execute("SELECT o.id,o.user_id,o.product_id,o.price,o.status,o.order_code,p.name AS product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id=?", (ticket["order_id"],)).fetchall()
        if normalized.upper().startswith("SP-"):
            return con.execute("SELECT o.id,o.user_id,o.product_id,o.price,o.status,o.order_code,p.name AS product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE upper(o.order_code)=?", (normalized.upper(),)).fetchall()
        if normalized.startswith("#"):
            normalized = normalized[1:]
        if normalized.isdigit():
            return con.execute(
                "SELECT o.id,o.user_id,o.product_id,o.price,o.status,o.order_code,p.name AS product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id=? OR o.user_id=? OR o.recipient_user_id=? ORDER BY o.id DESC LIMIT 20",
                (int(normalized), int(normalized), int(normalized)),
            ).fetchall()
        username = normalized if normalized.startswith("@") else "@" + normalized
        username = username.lstrip("@").lower()
        return con.execute(
            "SELECT o.id,o.user_id,o.product_id,o.price,o.status,o.order_code,p.name AS product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE lower(o.buyer_username)=? OR lower(o.recipient_username)=? ORDER BY o.id DESC LIMIT 20",
            (username, "@" + username),
        ).fetchall()


def render_order_search_results(rows, query_value):
    if not rows:
        return f"🔎 نتیجه‌ای برای «{query_value}» پیدا نشد.\n\nمی‌توانی ID عددی، @username یا کد سفارش را دوباره بررسی کنی."
    lines = [f"🔎 نتایج جست‌وجو: {query_value}", "━━━━━━━━━━━━━━━━━━"]
    for row in rows:
        lines.append(f"\n🧾 {row['order_code'] or ('#'+str(row['id']))}\nمحصول: {row['product_name'] or 'ثبت نشده'}\nID خریدار: {row['user_id']}\nمبلغ: {toman(row['price'])}\nوضعیت: {row['status']}")
    return "\n".join(lines)[:3900]


def order_search_results_keyboard(rows):
    buttons=[]
    for row in rows[:10]:
        buttons.append([InlineKeyboardButton(f"🔎 سفارش {row['order_code'] or ('#'+str(row['id']))}", callback_data=f"admin_order_view_{row['id']}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت به تیکت‌ها", callback_data="admin_support_tickets")])
    return InlineKeyboardMarkup(buttons)


async def _callbacks_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query and query.data == "global_cancel":
        await handle_global_cancel_callback(update, context)
        return
    if query.data == "check_membership":
        context.application.create_task(_safe_callback_answer(query))
        result = await is_channel_member(update, context)
        if result is True:
            await query.edit_message_text(
                "✅ عالیه! عضویتت تایید شد. /start رو بزن 🎉",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 ورود به فروشگاه", callback_data="home")
                ]]),
            )
        elif result is False:
            await query.edit_message_text(
                "🔒 برای استفاده از خدمات ربات، ابتدا عضو کانال شو:\nhttps://t.me/SeyedPro_FQA\n\nپس از عضویت، «بررسی عضویت» را بزن.",
                reply_markup=membership_keyboard(),
            )
        else:
            await query.edit_message_text(
                "⚠️ در حال حاضر بررسی عضویت کانال ممکن نیست. چند لحظه دیگر دوباره تلاش کن.",
                reply_markup=membership_keyboard(),
            )
        return
    context.application.create_task(_safe_callback_answer(query))
    if not await require_channel_membership(update, context):
        return
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if (data.startswith("premium_") or data == "admin_auto_prices" or data.startswith("admin_auto_cat|")) and user_id != ADMIN_ID:
        await query.answer("این بخش فقط برای مدیر ربات در دسترس است.", show_alert=True)
        return

    if await handle_gift_support_callback(query, context):
        return

    # Premium manager callbacks are intentionally isolated from Gift settings.
    if data == "premium_admin_home":
        await query.edit_message_text(premium_admin_text(), reply_markup=premium_admin_keyboard())
        return
    if data == "premium_delivery_menu":
        await query.edit_message_text(premium_delivery_text(), reply_markup=premium_delivery_keyboard())
        return
    if data.startswith("premium_delivery_toggle_"):
        months = int(data.rsplit("_", 1)[1])
        if months not in PREMIUM_PLAN_STARS:
            await query.answer("پلن معتبر نیست.", show_alert=True); return
        cfg = load_premium_manager_settings()
        cfg["delivery_modes"][str(months)] = "manual" if cfg["delivery_modes"].get(str(months)) == "auto" else "auto"
        # Never activate auto for a plan unless Bot API delivery and current balance can be checked.
        if cfg["delivery_modes"][str(months)] == "auto":
            try:
                balance = await get_real_bot_star_balance(context.bot)
                if balance < PREMIUM_PLAN_STARS[months]:
                    cfg["delivery_modes"][str(months)] = "manual"
                    save_premium_manager_settings(cfg)
                    await query.answer(f"موجودی فعلی {balance} ⭐ است؛ برای این پلن {PREMIUM_PLAN_STARS[months]} ⭐ لازم است. حالت دستی حفظ شد.", show_alert=True)
                    await query.edit_message_text(premium_delivery_text(), reply_markup=premium_delivery_keyboard())
                    return
            except Exception as exc:
                cfg["delivery_modes"][str(months)] = "manual"
                save_premium_manager_settings(cfg)
                await query.answer("موجودی واقعی تلگرام دریافت نشد؛ حالت دستی حفظ شد.", show_alert=True)
                await query.edit_message_text(premium_delivery_text(), reply_markup=premium_delivery_keyboard())
                return
        save_premium_manager_settings(cfg)
        await query.answer("روش تحویل پلن ذخیره شد.")
        await query.edit_message_text(premium_delivery_text(), reply_markup=premium_delivery_keyboard())
        return
    if data in {"premium_delivery_all_manual", "premium_delivery_all_auto"}:
        mode = "manual" if data.endswith("manual") else "auto"
        cfg = load_premium_manager_settings()
        if mode == "auto":
            try:
                balance = await get_real_bot_star_balance(context.bot)
            except Exception:
                await query.answer("موجودی واقعی تلگرام قابل دریافت نیست؛ هیچ پلنی خودکار نشد.", show_alert=True)
                return
            available = [m for m, required in PREMIUM_PLAN_STARS.items() if balance >= required]
            if len(available) != 3:
                await query.answer(f"موجودی {balance} ⭐ برای همه پلن‌ها کافی نیست؛ همه خودکار نشدند.", show_alert=True)
                return
        cfg["delivery_modes"] = {"3": mode, "6": mode, "12": mode}
        save_premium_manager_settings(cfg)
        await query.answer("روش تحویل هر سه پلن به‌روزرسانی شد.")
        await query.edit_message_text(premium_delivery_text(), reply_markup=premium_delivery_keyboard())
        return
    if data in {
        "premium_auto_star_rate_toggle", "premium_auto_fragment_toggle",
        "premium_rate_toman", "premium_fragment_usd", "premium_base_usd_fx", "premium_margin",
    }:
        # Backward compatibility for an old Telegram message still showing the retired
        # controls. All rate, source, margin and fee changes now live in one central menu.
        await safe_callback_edit_or_reply(
            query,
            "💹 تنظیمات نرخ و قیمت‌گذاری یکپارچه شده است.\n"
            "برای جلوگیری از اختلاف قیمت، این تنظیم قدیمی دیگر جداگانه تغییر نمی‌کند.\n\n"
            "از مرکز مدیریت قیمت‌گذاری خودکار استفاده کن:",
            pricing_panel_keyboard(),
        )
        return
    if data in {"premium_plan_price_3", "premium_plan_price_6", "premium_plan_price_12"}:
        cfg = load_premium_manager_settings()
        months = int(data.rsplit("_", 1)[1])
        field = f"plan_price_{months}"
        current = cfg["plan_price_toman_manual"].get(str(months))
        current_text = f"{toman(current)} تومان" if current else "تنظیم‌نشده (محاسبهٔ خودکار)"
        prompt = (f"💰 قیمت شخصی فعلی پلن {months} ماهه: {current_text}\n\n"
                  "قیمت تومانی جدید را فقط به‌صورت عدد بفرست.\n"
                  "برای حذف قیمت شخصی و بازگشت به محاسبهٔ خودکار، عدد 0 را بفرست.")
        context.user_data["premium_admin_input"] = {"field": field}
        await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="danger")],
            [InlineKeyboardButton("🔙 بازگشت به مدیریت پرمیوم", callback_data="premium_admin_home")],
        ]))
        return
    if data == "premium_input_cancel":
        context.user_data.pop("premium_admin_input", None)
        await query.edit_message_text("❌ عملیات لغو شد؛ تنظیم قبلی حفظ شد.", reply_markup=premium_admin_keyboard())
        return

    if data == "cancel_public_request":
        context.user_data.pop("mediator_request_state", None)
        context.user_data.pop("ton_purchase_state", None)
        await query.edit_message_text(
            "درخواست لغو شد؛ هیچ پرداخت یا سفارشی ایجاد نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]]),
        )
        return

    if data == "mediator_request":
        context.user_data.pop("ton_purchase_state", None)
        context.user_data["mediator_request_state"] = {"step": "details"}
        await query.edit_message_text(
            "🤝 درخواست واسطه | SEYED PREMIUM\n\n"
            "در یک پیام توضیح بده معامله درباره چیست، طرف مقابل چه آیدی/راه ارتباطی دارد و چه توافقی انجام شده است.\n"
            "اطلاعات حساس مثل رمز عبور یا کد ورود را ارسال نکن.\n\n"
            "درخواست برای بررسی دستی مدیر ثبت می‌شود؛ ثبت آن به معنی تضمین یا تأیید معامله نیست. برای لغو دکمه زیر را بزن.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو درخواست", callback_data="cancel_public_request", style="danger")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
            ]),
        )
        return

    if data == "ton_gram":
        await query.edit_message_text(
            "💎 تون (TON) | SEYED PREMIUM\n\n"
            "از این بخش می‌توانی درخواست خرید TON ثبت کنی. در حال حاضر قیمت زنده یا پرداخت و انتقال خودکار به ربات متصل نیست.\n"
            "درخواست ثبت‌شده فقط پس از بررسی و تأیید دستی مدیر قابل انجام است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🪙 ثبت درخواست خرید تون", callback_data="ton_buy", style="success")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
            ]),
        )
        return

    if data == "ton_buy":
        context.user_data.pop("mediator_request_state", None)
        context.user_data["ton_purchase_state"] = {"step": "amount"}
        await query.edit_message_text(
            "🪙 درخواست خرید تون (TON)\n\n"
            "مقدار TON موردنظرت را با عدد بفرست؛ مثلاً 2 یا 0.5.\n"
            "قیمت زنده، پرداخت و انتقال خودکار فعال نیست و درخواست پیش از هر اقدامی توسط مدیر بررسی می‌شود.\n"
            "برای لغو، دکمه زیر یا /cancel را بزن.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو درخواست", callback_data="cancel_public_request", style="danger")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
            ]),
        )
        return

    if data == "mini_app":
        await query.edit_message_text(
            "▤ مینی اپ\n\nمینی‌اپ وب فعلاً به این ربات متصل نشده است؛ برای جلوگیری از نمایش قابلیت غیرواقعی، این دکمه فعلاً وضعیت را شفاف اعلام می‌کند.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]]),
        )
        return

    if data == "nft_market":
        await query.edit_message_text(
            "▦ مارکت NFT\n\nبازارچه NFT هنوز به سرویس یا کاتالوگ واقعی متصل نشده است. هیچ قیمت یا موجودی ساختگی نمایش داده نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]]),
        )
        return

    # بازگشت به صفحه قبلی
    history = context.user_data.setdefault("screen_history", [])
    if data == "nav_back":
        if history:
            previous = history.pop()
            context.user_data["current_screen"] = previous
            context.user_data["_replaying_screen"] = True
            from types import SimpleNamespace
            replay_query = SimpleNamespace(
                data=previous,
                from_user=query.from_user,
                message=query.message,
                answer=query.answer,
                edit_message_text=query.edit_message_text,
                edit_message_reply_markup=query.edit_message_reply_markup,
            )
            replay_update = SimpleNamespace(
                callback_query=replay_query,
                effective_user=update.effective_user,
                effective_chat=update.effective_chat,
                effective_message=update.effective_message,
            )
            await _callbacks_impl(replay_update, context)
        else:
            context.user_data["current_screen"] = "home"
            if get_main_menu_type() == MENU_TYPE_KEYBOARD:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    logger.debug("Could not remove previous inline keyboard", exc_info=True)
                await context.bot.send_message(
                    chat_id=user_id, text=bot_text("main_menu"), reply_markup=main_menu(user_id)
                )
            else:
                await query.edit_message_text(
                    "🏠 منوی اصلی", reply_markup=main_menu(user_id)
                )
        return

    screen_routes = (
        "home", "cat_", "service_", "stars_", "back_to_services",
        "config_", "vip_", "account", "wallet", "orders",
        "support", "invite", "topup"
    )
    replaying = context.user_data.pop("_replaying_screen", False)
    is_screen = any(
        data == route or data.startswith(route) for route in screen_routes
    )
    if is_screen and not replaying:
        current = context.user_data.get("current_screen")
        if current and current != data:
            history.append(current)
            del history[:-30]
        context.user_data["current_screen"] = data

    # UI_ADMIN_PATCH_V1
    if data.startswith("uiadm_"):
        if user_id != ADMIN_ID:
            await query.answer("دسترسی مجاز نیست.", show_alert=True)
            return

        state = context.user_data
        parts = data.split("_")
        action = data

        if data == "uiadm_home":
            state.pop("uiadm_state", None)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("منوی اصلی", callback_data="uiadm_menu_main")],
                [InlineKeyboardButton("منوی خدمات", callback_data="uiadm_menu_services")],
                [InlineKeyboardButton("منوی انتخاب استارز", callback_data="uiadm_menu_stars")],
                [InlineKeyboardButton("ویرایش متن‌ها", callback_data="uiadm_texts")],
                [InlineKeyboardButton("بازگشت به مدیریت", callback_data="admin")],
            ])
            await query.edit_message_text("⚙️ مدیریت یکپارچه دکمه‌ها و متن‌ها", reply_markup=kb)
            return

        if data == "uiadm_texts":
            keys = list(EDITABLE_TEXTS.items())
            kb = [[InlineKeyboardButton(label, callback_data=f"uiadm_text_{key}")]
                  for key, (label, default) in keys]
            kb.append([InlineKeyboardButton("↩️ بازگشت", callback_data="uiadm_home")])
            await query.edit_message_text("📝 متن موردنظر را انتخاب کن:", reply_markup=InlineKeyboardMarkup(kb))
            return

        if data.startswith("uiadm_text_"):
            key = data[len("uiadm_text_"):]
            if key not in EDITABLE_TEXTS:
                await query.answer("متن معتبر نیست.", show_alert=True)
                return
            state["uiadm_state"] = {"kind": "text", "key": key}
            await query.edit_message_text(
                f"✏️ متن فعلی:\n\n{bot_text(key)}\n\nمتن جدید را در یک پیام بفرست.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("لغو", callback_data="uiadm_home")]])
            )
            return

        if data.startswith("uiadm_menu_"):
            menu = data[len("uiadm_menu_"):]
            cfg = ui_load()
            if menu not in {"main", "services", "stars"}:
                await query.answer("این منو پشتیبانی نمی‌شود.", show_alert=True)
                return
            items = cfg.get("menus", {}).get(menu, [])
            kb = []
            for item in items:
                label = ("🟢 " if item.get("enabled", True) else "🔴 ") + item.get("text", item.get("id", "?"))
                kb.append([InlineKeyboardButton(label[:60], callback_data=f"uiadm_item_{menu}_{item.get('id','')}")])
            kb.extend([
                [InlineKeyboardButton("➕ افزودن دکمه", callback_data=f"uiadm_add_{menu}")],
                [InlineKeyboardButton("↩️ بازگشت", callback_data="uiadm_home")],
            ])
            await query.edit_message_text(f"مدیریت منوی {menu}:", reply_markup=InlineKeyboardMarkup(kb))
            return

        if data.startswith("uiadm_item_"):
            payload = data[len("uiadm_item_"):]
            menu = next(
                (m for m in ("services", "main", "stars")
                 if payload.startswith(m + "_")),
                None
            )
            if menu is None:
                await query.answer("شناسه منو نامعتبر است.", show_alert=True)
                return
            button_id = payload[len(menu) + 1:]
            if not button_id:
                await query.answer("شناسه دکمه نامعتبر است.", show_alert=True)
                return
            state["uiadm_state"] = {"kind": "button", "menu": menu, "id": button_id}
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ تغییر متن دکمه", callback_data="uiadm_rename")],
                [InlineKeyboardButton("🎨 تغییر رنگ دکمه", callback_data="uiadm_style")],
                [InlineKeyboardButton("🧭 تغییر عملکرد/مقصد", callback_data="uiadm_callback")],
                [InlineKeyboardButton("🔁 فعال/غیرفعال", callback_data="uiadm_toggle")],
                [InlineKeyboardButton("⬆️ جابه‌جایی به بالا", callback_data="uiadm_up")],
                [InlineKeyboardButton("⬇️ جابه‌جایی به پایین", callback_data="uiadm_down")],
                [InlineKeyboardButton("🗑 غیرفعال‌کردن دکمه", callback_data="uiadm_delete")],
                [InlineKeyboardButton("↩️ بازگشت", callback_data=f"uiadm_menu_{menu}")],
            ])
            await query.edit_message_text(f"مدیریت دکمه: {button_id}", reply_markup=kb)
            return

        if data == "uiadm_style":
            old = state.get("uiadm_state", {})
            if old.get("kind") != "button":
                await query.answer("ابتدا دکمه را انتخاب کن.", show_alert=True)
                return
            menu_name, item_id = old.get("menu"), old.get("id")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 سبز", callback_data="uiadm_style_set_success", style="success")],
                [InlineKeyboardButton("🔵 آبی", callback_data="uiadm_style_set_primary", style="primary")],
                [InlineKeyboardButton("🔴 قرمز", callback_data="uiadm_style_set_danger", style="danger")],
                [InlineKeyboardButton("↩️ بازگشت", callback_data=f"uiadm_item_{menu_name}_{item_id}")],
            ])
            await query.edit_message_text("رنگ واقعی دکمه را انتخاب کن:", reply_markup=kb)
            return

        if data.startswith("uiadm_style_set_"):
            style = data.removeprefix("uiadm_style_set_")
            old = state.get("uiadm_state", {})
            if old.get("kind") != "button" or style not in {"primary", "success", "danger"}:
                await query.answer("درخواست یا رنگ نامعتبر است.", show_alert=True)
                return
            cfg = ui_load()
            items = cfg.get("menus", {}).get(old["menu"], [])
            item = next((x for x in items if str(x.get("id")) == str(old["id"])), None)
            if not item:
                await query.answer("دکمه پیدا نشد.", show_alert=True)
                return
            item["button_style"] = style
            ui_save(cfg)
            await query.edit_message_text("✅ رنگ دکمه ذخیره شد.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بازگشت به ویرایش دکمه", callback_data=f"uiadm_item_{old['menu']}_{old['id']}")],
                [InlineKeyboardButton("مدیریت منو", callback_data=f"uiadm_menu_{old['menu']}")],
            ]))
            return

        if data == "uiadm_callback":
            old = state.get("uiadm_state", {})
            if old.get("kind") != "button":
                await query.answer("ابتدا دکمه را انتخاب کن.", show_alert=True)
                return
            cfg = ui_load()
            item = next((x for x in cfg.get("menus", {}).get(old["menu"], []) if str(x.get("id")) == str(old["id"])), None)
            if not item:
                await query.answer("دکمه پیدا نشد.", show_alert=True)
                return
            state["uiadm_state"] = {**old, "kind": "callback"}
            await query.edit_message_text(
                "شناسهٔ عملکرد موجود را بفرست. فقط عملکردهای پیاده‌سازی‌شده پذیرفته می‌شوند.\n\n"
                f"مقدار فعلی: {item.get('callback', '')}\nبرای لغو /cancel را بفرست."
            )
            return

        if data in ("uiadm_rename", "uiadm_add_main", "uiadm_add_services", "uiadm_add_stars"):
            if data == "uiadm_rename":
                old = state.get("uiadm_state", {})
                if old.get("kind") != "button":
                    await query.answer("ابتدا دکمه را انتخاب کن.", show_alert=True)
                    return
                state["uiadm_state"] = {**old, "kind": "rename"}
                await query.edit_message_text("نام جدید دکمه را بفرست:")
            else:
                menu = data[len("uiadm_add_"):]
                state["uiadm_state"] = {"kind": "add", "menu": menu, "step": "id"}
                await query.edit_message_text("شناسه انگلیسی یکتا بفرست؛ مثال: my_button")
            return

        if data in ("uiadm_toggle", "uiadm_up", "uiadm_down", "uiadm_delete"):
            st = state.get("uiadm_state", {})
            if st.get("kind") != "button":
                await query.answer("دکمه‌ای انتخاب نشده.", show_alert=True)
                return
            cfg = ui_load()
            items = cfg.get("menus", {}).get(st["menu"], [])
            idx = next((i for i, x in enumerate(items) if x.get("id") == st["id"]), None)
            if idx is None:
                await query.answer("دکمه پیدا نشد.", show_alert=True)
                return
            if data == "uiadm_toggle":
                items[idx]["enabled"] = not items[idx].get("enabled", True)
            elif data == "uiadm_up" and idx > 0:
                items[idx-1], items[idx] = items[idx], items[idx-1]
                if "row" in items[idx-1] or "row" in items[idx]:
                    items[idx-1]["row"], items[idx]["row"] = items[idx].get("row"), items[idx-1].get("row")
            elif data == "uiadm_down" and idx < len(items)-1:
                items[idx+1], items[idx] = items[idx], items[idx+1]
                if "row" in items[idx+1] or "row" in items[idx]:
                    items[idx+1]["row"], items[idx]["row"] = items[idx].get("row"), items[idx+1].get("row")
            elif data == "uiadm_delete":
                items[idx]["enabled"] = False
            ui_save(cfg)
            result_text = "✅ دکمه غیرفعال شد؛ برای بازیابی از گزینهٔ فعال/غیرفعال استفاده کن." if data == "uiadm_delete" else "✅ تغییر ذخیره شد."
            await query.edit_message_text(result_text, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بازگشت به منو", callback_data=f"uiadm_menu_{st['menu']}")],
                [InlineKeyboardButton("پنل مدیریت", callback_data="uiadm_home")],
            ]))
            return

    if data == "cat_stars":
        text = (
            "🛒 خرید سرویس | SEYED PREMIUM 💎\n\n"
            "سرویس موردنظرت رو انتخاب کن 👇🏻\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🎁 تا ۵۰۰ هزار تومان تخفیف برای استارز و پرمیوم\n\n"
            "💰 کش‌بک روی تمام سفارشات\n\n"
            "⚡️ تحویل فوق‌سریع\n\n"
            "🏷️ تخفیف و مزایای اختصاصی برای همه\n\n"
            "👑 هر کاربر متناسب با خریدهاش، تخفیف‌ها و مزایای اختصاصی خودش رو دریافت می‌کنه. ✨"
        )
        await query.edit_message_text(text, reply_markup=service_menu(bot_text))
        return

    if data == "service_stars":
        context.user_data.pop("stars_fixed_wait_recipient", None)
        await refresh_auto_pricing(force=False)
        await query.edit_message_text(
            stars_text(),
            reply_markup=build_stars_menu()
        )
        return

    if data.startswith("stars_pack_"):
        raw_amount = data.removeprefix("stars_pack_")
        try:
            amount = int(raw_amount)
        except ValueError:
            await query.answer("بسته انتخاب‌شده معتبر نیست.", show_alert=True)
            return

        if amount not in FIXED_STAR_AMOUNTS:
            await query.answer("این بسته در فهرست مجاز استارز نیست.", show_alert=True)
            return
        await refresh_auto_pricing(force=False)
        live_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount)
        persian_amount = persian_digits(f"{amount:,}".replace(",", "٬"))
        tier = "بسته استارز"
        conn = db()
        try:
            product = conn.execute(
                "SELECT id, price, active FROM products "
                "WHERE category='stars' AND star_amount=? LIMIT 1",
                (amount,)
            ).fetchone()
            user = conn.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (query.from_user.id,)
            ).fetchone()
        finally:
            conn.close()

        if not product or not product["active"]:
            await query.answer("این بسته فعلاً در دسترس نیست.", show_alert=True)
            return

        price = int(live_quote["sale_toman"]) if live_quote else int(product["price"] or 0)
        balance = int(user["balance"] or 0) if user else 0

        if price <= 0:
            await query.answer("این بسته هنوز قیمت ثبت‌شده ندارد؛ لطفاً با پشتیبانی تماس بگیر.", show_alert=True)
            return
        else:
            shortage = max(0, price - balance)
            payment_status = (
                "✅ موجودی کیف پول کافی است."
                if shortage == 0
                else f"⚠️ موجودی کافی نیست؛ کسری: {toman(shortage)} تومان"
            )
            detail = (
                "🛒 جزئیات بسته انتخابی\n\n"
                f"✅ استارز - {persian_amount} استارز\n"
                f"📝 {tier}\n"
                f"💸 قیمت: {toman(price)} تومان\n"
                f"💰 موجودی کیف پول: {toman(balance)} تومان\n"
                f"📉 کسری مبلغ: {toman(shortage)} تومان\n\n"
                f"{payment_status}\n\n"
                "انتخاب این بسته به‌تنهایی پرداخت یا سفارش را تأیید نمی‌کند."
            )

        await query.edit_message_text(
            detail,
            reply_markup=stars_detail_menu(amount)
        )
        return

    if data == "stars_detail_back":
        context.user_data.pop("stars_fixed_wait_recipient", None)
        await query.edit_message_text(
            stars_text(),
            reply_markup=build_stars_menu()
        )
        return

    # SEYED_STARS_DISCOUNT_V1
    if data.startswith("stars_discount_") and not data.startswith("stars_discount_skip_"):
        try:
            amount = int(data.rsplit("_", 1)[-1])
            if amount not in FIXED_STAR_AMOUNTS:
                await query.answer("بسته معتبر نیست.", show_alert=True)
                return
            await refresh_auto_pricing(force=False)
            live_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount)
            with db() as con:
                product = con.execute(
                    "SELECT price, active FROM products WHERE category=? AND (star_amount=? OR name=?) LIMIT 1",
                    ("stars", str(amount), f"استارز {amount} عدد")
                ).fetchone()
            if not product or not product["active"] or live_quote is None:
                await query.answer("قیمت این بسته ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.", show_alert=True)
                return
            context.user_data["pending_stars_coupon"] = {
                "amount": amount, "price": int(live_quote["sale_toman"])
            }
            await query.edit_message_text(
                "💎 کد تخفیف ویژه\n\n━━━━━━━━━━━━━━━━━━\n🏷️ کد تخفیفت رو اینجا وارد کن:\n\n✨ اگر کد معتبر باشه، تخفیف از مبلغ سفارش کم می‌شه.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("رد شدن", callback_data=f"stars_discount_skip_{amount}")
                ]])
            )
        except Exception:
            logger.exception("Stars discount prompt failed")
            await query.answer("خطا در باز کردن کد تخفیف.", show_alert=True)
        return

    if data.startswith("stars_discount_skip_"):
        try:
            amount = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return
        pending = context.user_data.get("pending_stars_coupon", {})
        if pending.get("amount") == amount:
            context.user_data.pop("pending_stars_coupon", None)
        coupon = context.user_data.get("stars_coupon", {})
        if coupon.get("amount") == amount:
            context.user_data.pop("stars_coupon", None)
        await query.answer("ورود کد تخفیف رد شد.")
        await query.edit_message_text(
            "⭐ بسته استارز انتخاب شد. برای ادامه، گیرنده را انتخاب کن.",
            reply_markup=stars_detail_menu(amount)
        )
        return

    if data.startswith("stars_recipient_self_") or data.startswith("stars_recipient_other_"):
        is_self = data.startswith("stars_recipient_self_")
        try:
            amount = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return
        if amount not in FIXED_STAR_AMOUNTS:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return
        await refresh_auto_pricing(force=False)
        live_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount)
        if live_quote is None:
            await query.answer("قیمت این بسته ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.", show_alert=True)
            return

        user_id = query.from_user.id
        username = query.from_user.username
        if not is_self:
            # The recipient must be explicitly collected before showing payment options.
            # Keep the bot's existing message so the next step can edit it in place.
            context.user_data.pop("stars_recipient", None)
            context.user_data["stars_fixed_wait_recipient"] = {
                "amount": int(amount),
                "chat_id": int(query.message.chat_id) if query.message else int(user_id),
                "message_id": int(query.message.message_id) if query.message else None,
            }
            await query.edit_message_text(
                "🎁 استارز برای شخص دیگر\n\n"
                "لطفاً آیدی عمومی دریافت‌کننده را با @ ارسال کن؛ مثال: @SeyedPro_AIA\n"
                "🔐 رمز عبور یا کد ورود را ارسال نکن.\n\n"
                "پس از ثبت آیدی، قیمت دوباره بررسی می‌شود."
            )
            await context.bot.send_message(
                chat_id=int(query.message.chat_id),
                text="برای لغو این سفارش، فقط دکمهٔ «❌ لغو سفارش» در کیبورد پایین را بزن.",
                reply_markup=customer_order_cancel_keyboard(),
            )
            return

        recipient = f"@{username}" if username else "خودم"
        recipient_id = str(user_id) if username else "نام کاربری عمومی ثبت نشده"
        context.user_data["stars_recipient"] = {
            "amount": int(amount), "recipient": recipient, "recipient_id": recipient_id,
            "quote": live_quote,
        }

        try:
            conn = db()
            product = conn.execute(
                "SELECT price FROM products WHERE category = ? AND (star_amount = ? OR name = ?) LIMIT 1",
                ("stars", amount, f"استارز {amount} عدد")
            ).fetchone()
            user = conn.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            conn.close()

            price = int(live_quote["sale_toman"])
            balance = int(user["balance"] or 0) if user else 0
        except Exception:
            price = 0
            balance = 0

        coupon = context.user_data.get("stars_coupon", {})
        if coupon.get("amount") == int(amount) and coupon.get("final_price", 0) > 0:
            discount = int(coupon.get("discount", 0))
            price = int(coupon["final_price"])
            price_text = f"{toman(price)} تومان (با تخفیف {toman(discount)} تومان)"
        else:
            price_text = f"{toman(price)} تومان" if price > 0 else "قیمت هنوز تنظیم نشده"
        balance_text = f"{toman(balance)} تومان"
        recipient_quote_state = context.user_data.get("stars_recipient") or {}
        recipient_quote_state["shown_price_toman"] = int(price)
        recipient_quote_state["quote"] = live_quote
        context.user_data["stars_recipient"] = recipient_quote_state

        detail = (
            "🧾 تأیید سفارش | SEYED PREMIUM\n\n"
            f"⭐ محصول: {amount} استارز\n"
            f"👤 گیرنده: {recipient}\n"
            f"🆔 آیدی عددی: {recipient_id}\n\n"
            f"💰 مبلغ: {price_text}\n"
            f"👛 موجودی کیف پول: {balance_text}\n\n"
            "━━━━━━━━━━━━━━\n"
            "لطفاً روش پرداخت را انتخاب کنید:"
        )

        payment_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "💳 کارت به کارت",
                callback_data=f"stars_pay_card_{amount}",
                style="primary"
            )],
            [InlineKeyboardButton(
                "💰 پرداخت با کیف پول",
                callback_data=f"stars_pay_wallet_{amount}",
                style="success"
            )],
            [InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="stars_detail_back",
                style="danger"
            )],
        ])

        await query.edit_message_text(detail, reply_markup=payment_keyboard)
        return

    if data.startswith("stars_pay_card_"):
        try:
            amount = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return

        if amount not in FIXED_STAR_AMOUNTS:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return
        await refresh_auto_pricing(force=False)
        live_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount)
        if live_quote is None:
            await query.answer("قیمت این بسته ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.", show_alert=True)
            return
        with db() as con:
            ensure_discount_schema(con)
            product = con.execute(
                "SELECT id, name, price, active, currency FROM products WHERE category='stars' AND (star_amount=? OR name=?) LIMIT 1",
                (amount, f"استارز {amount} عدد")
            ).fetchone()
            if not product or not product["active"]:
                await query.answer("این بسته فعلاً در دسترس نیست.", show_alert=True)
                return
            price = int(live_quote["sale_toman"])
            discount = 0
            coupon = context.user_data.get("stars_coupon", {})
            if coupon.get("amount") == amount and coupon.get("code"):
                validation = validate_coupon_for_order(
                    con, coupon["code"], query.from_user.id, price, "stars", str(product["name"]), query.from_user.username
                )
                if not validation.get("ok"):
                    context.user_data.pop("stars_coupon", None)
                    await query.answer(validation.get("reason", "کد تخفیف دیگر معتبر نیست."), show_alert=True)
                    return
                price = int(validation["final_price"])
                discount = int(validation["discount"])
                context.user_data["stars_coupon"] = {
                    "amount": amount, "code": validation["code"], "discount": discount,
                    "final_price": price,
                }
        recipient_quote_state = context.user_data.get("stars_recipient") or {}
        if int(recipient_quote_state.get("amount", -1) or -1) != amount:
            await query.answer("گیرندهٔ سفارش پیدا نشد؛ بسته را دوباره انتخاب کن.", show_alert=True)
            return
        recipient_quote_state["shown_price_toman"] = int(price)
        recipient_quote_state["quote"] = live_quote
        context.user_data["stars_recipient"] = recipient_quote_state

        card_number = str(bot_text("payment_card_number") or "").strip()
        card_owner = str(bot_text("payment_card_owner") or "").strip()
        payment_ready = bool(card_number and card_number.casefold() not in {"هنوز تنظیم نشده", "تنظیم نشده", "ثبت نشده"})
        buttons = []
        if payment_ready:
            buttons.append([InlineKeyboardButton(
                "✅ پرداخت کردم؛ ثبت سفارش و ارسال رسید",
                callback_data=f"stars_receipt_start_{amount}", style="success"
            )])
        buttons.append([InlineKeyboardButton("❌ لغو سفارش", callback_data="stars_detail_back", style="danger")])
        payment_text = (
            "💳 پرداخت کارت‌به‌کارت | SEYED PREMIUM\n\n"
            f"⭐ محصول: {amount} استارز\n"
            f"💰 مبلغ: {toman(price)} {str(product['currency'] or 'تومان')}\n\n"
            f"🏦 شماره کارت: {card_number}\n"
            f"👤 نام صاحب کارت: {card_owner or 'در تنظیمات وارد نشده'}\n\n"
            + (bot_text("payment_instructions") if payment_ready else
               "⚠️ اطلاعات کارت هنوز توسط مدیریت تنظیم نشده است؛ فعلاً پرداخت انجام نده. از پشتیبانی پیگیری کن.")
        )
        await query.edit_message_text(payment_text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("stars_receipt_start_"):
        try:
            amount = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return
        if amount not in FIXED_STAR_AMOUNTS:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return
        await refresh_auto_pricing(force=False)
        live_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount)
        if live_quote is None:
            await query.answer("قیمت این بسته ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.", show_alert=True)
            return
        card_number = str(bot_text("payment_card_number") or "").strip()
        if not card_number or card_number.casefold() in {"هنوز تنظیم نشده", "تنظیم نشده", "ثبت نشده"}:
            await query.answer("اطلاعات پرداخت هنوز توسط مدیریت تنظیم نشده است.", show_alert=True)
            return
        user_id = query.from_user.id
        recipient_info = context.user_data.get("stars_recipient") or {}
        if int(recipient_info.get("amount", -1) or -1) != amount or not recipient_info.get("recipient"):
            await query.answer("گیرندهٔ سفارش پیدا نشد؛ برای جلوگیری از ثبت ناقص، دوباره انتخاب کن.", show_alert=True)
            return
        recipient_label = str(recipient_info.get("recipient", "گیرنده مشخص نشده"))
        recipient_id = str(recipient_info.get("recipient_id", ""))
        try:
            with db() as con:
                ensure_discount_schema(con)
                con.execute("BEGIN IMMEDIATE")
                product = con.execute(
                    "SELECT id, name, price, currency, active FROM products WHERE category='stars' AND star_amount=? LIMIT 1",
                    (amount,),
                ).fetchone()
                if not product or not product["active"]:
                    con.rollback()
                    await query.answer("این بسته فعلاً در دسترس نیست.", show_alert=True)
                    return
                price = int(live_quote["sale_toman"])
                coupon = context.user_data.get("stars_coupon", {})
                coupon_code = None
                coupon_discount = 0
                if int(coupon.get("amount", 0) or 0) == amount and coupon.get("code"):
                    validation = validate_coupon_for_order(
                        con, str(coupon["code"]), user_id, price, "stars", str(product["name"]), query.from_user.username
                    )
                    if not validation.get("ok"):
                        con.rollback()
                        await query.answer(validation.get("reason", "کد تخفیف معتبر نیست."), show_alert=True)
                        return
                    coupon_code = validation["code"]
                    coupon_discount = int(validation["discount"])
                    price = int(validation["final_price"])
                expected_price = recipient_info.get("shown_price_toman")
                if expected_price is None or int(expected_price) != int(price):
                    con.rollback()
                    context.user_data.pop("stars_recipient", None)
                    await query.answer("قیمت تغییر کرده؛ هیچ سفارشی ثبت نشد.", show_alert=True)
                    await query.edit_message_text(
                        "⚠️ قیمت استارز پس از بررسی نهایی تغییر کرده است؛ سفارش ثبت نشد.\n\n"
                        "اگر قبلاً مبلغ قبلی را پرداخت کرده‌ای، برای هماهنگی با پشتیبانی تماس بگیر. تا تأیید مبلغ، رسیدی برای این سفارش نفرست.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🎧 پشتیبانی", callback_data="support", style="primary")],
                            [InlineKeyboardButton("‹ بازگشت به بسته‌ها", callback_data="service_stars", style="danger")],
                        ]),
                    )
                    return
                order_details = (
                    f"مقدار: {amount} استارز؛ گیرنده: {recipient_label}؛ شناسه گیرنده: {recipient_id or 'ثبت نشده'}\n\n"
                    + quote_breakdown_text(live_quote)
                )
                existing = con.execute(
                    "SELECT id FROM orders WHERE user_id=? AND product_id=? AND status='در انتظار ارسال رسید' AND order_details=? ORDER BY id DESC LIMIT 1",
                    (user_id, int(product["id"]), order_details),
                ).fetchone()
                if existing:
                    order_id = int(existing["id"])
                else:
                    cur = con.execute(
                        "INSERT INTO orders (user_id, product_id, price, status, currency, order_details, payment_method, coupon_code, coupon_discount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (user_id, int(product["id"]), price, "در انتظار ارسال رسید", str(product["currency"] or "تومان"), order_details, "کارت‌به‌کارت", coupon_code, coupon_discount),
                    )
                    order_id = int(cur.lastrowid)
                    if coupon_code:
                        con.execute(
                            "INSERT INTO coupon_usage (code, user_id, order_id, discount_amount, status) VALUES (?, ?, ?, ?, 'pending')",
                            (coupon_code, user_id, order_id, coupon_discount),
                        )
                        con.execute("UPDATE discount_codes SET total_uses=COALESCE(total_uses,0)+1 WHERE code=?", (coupon_code,))
            context.user_data["pending_receipt_order_id"] = order_id
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(f"🧾 سفارش استارز #{order_id} ساخته شد و منتظر رسید است.\n"
                          f"کاربر: {user_id}\nبسته: {amount} استارز\nمبلغ: {price:,} {str(product['currency'] or 'تومان')}\n"
                          f"جزئیات: {order_details}\nوضعیت: در انتظار ارسال رسید"),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("مشاهده سفارش", callback_data=f"admin_order_view_{order_id}")]]),
                )
            except Exception:
                logger.warning("Could not notify admin about Stars order #%s", order_id, exc_info=True)
            await query.edit_message_text(
                f"✅ سفارش استارز #{order_id} ثبت شد.\n\n"
                f"💰 مبلغ: {price:,} {str(product['currency'] or 'تومان')}\n"
                "حالا تصویر واضح رسید پرداخت را در همین گفت‌وگو ارسال کن. پس از بررسی و تأیید مدیر، سفارش برای تحویل آماده می‌شود.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو سفارش", callback_data=f"cancel_receipt_order_{order_id}", style="danger")]]),
            )
        except Exception:
            logger.exception("Could not create Stars payment order")
            await query.answer("ثبت سفارش انجام نشد؛ دوباره تلاش کن.", show_alert=True)
        return

    if data.startswith("cancel_receipt_order_"):
        try:
            order_id = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("شماره سفارش معتبر نیست.", show_alert=True)
            return
        with db() as con:
            cur = con.execute(
                "UPDATE orders SET status='لغو شده' WHERE id=? AND user_id=? AND status='در انتظار ارسال رسید'",
                (order_id, query.from_user.id),
            )
        context.user_data.pop("pending_receipt_order_id", None)
        if cur.rowcount:
            await query.edit_message_text(f"سفارش #{order_id} لغو شد؛ سوابق برای پیگیری محفوظ می‌ماند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")]]))
        else:
            await query.answer("سفارش در وضعیت قابل لغو نیست.", show_alert=True)
        return

    if data.startswith("stars_pay_wallet_"):
        try:
            amount = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("بسته استارز معتبر نیست.", show_alert=True)
            return

        if amount not in FIXED_STAR_AMOUNTS:
            await query.answer("بسته معتبر نیست.", show_alert=True)
            return
        await refresh_auto_pricing(force=False)
        live_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount)
        if live_quote is None:
            await query.answer("قیمت این بسته ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.", show_alert=True)
            return
        user_id = query.from_user.id

        try:
            with db() as con:
                con.execute("BEGIN IMMEDIATE")
                ensure_discount_schema(con)

                con.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_payment_claims (
                        user_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL,
                        PRIMARY KEY (user_id, message_id)
                    )
                """)

                claim = con.execute(
                    "INSERT OR IGNORE INTO wallet_payment_claims (user_id, message_id, amount) VALUES (?, ?, ?)",
                    (user_id, query.message.message_id, amount)
                )

                if claim.rowcount != 1:
                    con.rollback()
                    await query.answer(
                        "این پیام پرداخت قبلاً پردازش شده است.",
                        show_alert=True
                    )
                    return

                product = con.execute(
                    "SELECT id, price, active FROM products WHERE category=? AND (star_amount=? OR name=?) LIMIT 1",
                    ("stars", amount, f"استارز {amount} عدد")
                ).fetchone()

                user_row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (user_id,)
                ).fetchone()

                if not product or not product["active"]:
                    con.rollback()
                    await query.answer("این بسته فعلاً در دسترس نیست.", show_alert=True)
                    return

                if not user_row:
                    con.rollback()
                    await query.answer("حساب کاربری پیدا نشد.", show_alert=True)
                    return

                price = int(live_quote["sale_toman"])
                coupon = context.user_data.get("stars_coupon", {})
                discount = 0
                coupon_code = None

                # Revalidate with the shared rules while BEGIN IMMEDIATE holds the SQLite write lock.
                if coupon.get("amount") == amount and coupon.get("code"):
                    coupon_code = str(coupon["code"]).strip().upper()
                    validation = validate_coupon_for_order(
                        con, coupon_code, user_id, price, "stars", f"استارز {amount} عدد",
                        query.from_user.username
                    )
                    if not validation.get("ok"):
                        con.rollback()
                        await query.answer(validation.get("reason", "کد تخفیف دیگر معتبر نیست."), show_alert=True)
                        return
                    discount = int(validation["discount"])
                    price = int(validation["final_price"])

                recipient_info = context.user_data.get("stars_recipient") or {}
                expected_price = recipient_info.get("shown_price_toman")
                if (int(recipient_info.get("amount", -1) or -1) != amount
                        or expected_price is None or int(expected_price) != int(price)):
                    con.rollback()
                    context.user_data.pop("stars_recipient", None)
                    await query.answer("قیمت تغییر کرده؛ از کیف پول مبلغی کسر نشد.", show_alert=True)
                    await query.edit_message_text(
                        "⚠️ قیمت سفارش تغییر کرده است. هیچ مبلغی از کیف پول کسر نشد؛ بسته را دوباره انتخاب کن و مبلغ جدید را بررسی کن.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ انتخاب بسته‌ها", callback_data="service_stars", style="primary")]]),
                    )
                    return

                balance = int(user_row["balance"] or 0)
                if balance < price:
                    con.rollback()
                    await query.answer(
                        f"موجودی کافی نیست. مبلغ سفارش: {price:,} تومان؛ موجودی: {balance:,} تومان.",
                        show_alert=True
                    )
                    return

                result = con.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (price, user_id, price)
                )
                if result.rowcount != 1:
                    con.rollback()
                    await query.answer("موجودی تغییر کرده است؛ دوباره بررسی کن.", show_alert=True)
                    return

                wallet_order_details = (
                    f"مقدار: {amount} استارز\n\n" + quote_breakdown_text(live_quote)
                )
                cur = con.execute(
                    "INSERT INTO orders (user_id, product_id, price, status, currency, order_details, payment_method) VALUES (?, ?, ?, ?, 'تومان', ?, 'کیف پول')",
                    (user_id, int(product["id"]), price, "پرداخت‌شده - در انتظار تحویل", wallet_order_details)
                )
                order_id = cur.lastrowid

                if coupon_code:
                    con.execute("""
                        INSERT INTO coupon_usage
                        (code, user_id, order_id, discount_amount, status)
                        VALUES (?, ?, ?, ?, 'used')
                    """, (coupon_code, user_id, order_id, discount))
                    con.execute("""
                        UPDATE discount_codes
                        SET total_uses=COALESCE(total_uses, 0)+1
                        WHERE code=?
                    """, (coupon_code,))

                new_balance = balance - price
                con.commit()

        except Exception:
            logger.exception("Stars wallet payment failed")
            await query.answer("پرداخت انجام نشد؛ موجودی کسر نشده است.", show_alert=True)
            return

        await query.edit_message_text(
            "💎 SEYED PREMIUM\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "✅ پرداخت با موفقیت انجام شد\n\n"
            f"🧾 شماره سفارش: #{order_id}\n"
            f"⭐ محصول: {amount} استارز\n"
            f"💵 مبلغ پرداخت‌شده: {price:,} تومان\n"
            f"👛 موجودی باقی‌مانده: {new_balance:,} تومان\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📦 وضعیت: در انتظار تحویل توسط مدیر\n"
            "━━━━━━━━━━━━━━━━━━",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")
            ]])
        )

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "💎 SEYED PREMIUM\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "💰 پرداخت جدید با کیف پول\n\n"
                    f"🧾 شماره سفارش: #{order_id}\n\n"
                    f"👤 نام خریدار: {query.from_user.full_name}\n"
                    f"🔗 نام کاربری: @{query.from_user.username if query.from_user.username else 'تنظیم نشده'}\n"
                    f"🆔 آیدی عددی: {query.from_user.id}\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"⭐ محصول: {amount} استارز\n"
                    f"💵 مبلغ پرداخت‌شده: {price:,} تومان\n"
                    "📦 وضعیت: در انتظار تحویل\n"
                    "━━━━━━━━━━━━━━━━━━"
                )
            )
        except Exception:
            logger.exception("Admin notification failed for Stars wallet order")

        return

    if data == "stars_custom":
        await refresh_auto_pricing(force=False)
        context.user_data["stars_custom_amount"] = ""
        context.user_data.pop("stars_custom_order", None)
        context.user_data.pop("stars_custom_wait_recipient", None)
        context.user_data["stars_custom_chat_id"] = query.message.chat_id
        context.user_data["stars_custom_message_id"] = query.message.message_id
        await query.edit_message_text(custom_stars_display_text(""), reply_markup=custom_stars_keyboard())
        return

    if data.startswith("customstars_digit_"):
        digit = data.removeprefix("customstars_digit_")
        if digit not in set("0123456789"):
            await query.answer("عدد معتبر نیست.", show_alert=True)
            return
        current = normalize_ascii_digits(str(context.user_data.get("stars_custom_amount", "")))
        if len(current) >= 7:
            await query.answer("حداکثر مقدار ۱٬۰۰۰٬۰۰۰ استارز است.", show_alert=True)
            return
        current = (current + digit).lstrip("0")
        context.user_data["stars_custom_amount"] = current
        await query.edit_message_text(custom_stars_display_text(current), reply_markup=custom_stars_keyboard())
        return

    if data == "customstars_clear":
        context.user_data["stars_custom_amount"] = ""
        await query.edit_message_text(custom_stars_display_text(""), reply_markup=custom_stars_keyboard())
        return

    if data == "customstars_confirm":
        raw_amount = normalize_ascii_digits(str(context.user_data.get("stars_custom_amount", "")))
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError):
            amount = 0
        if not 50 <= amount <= 1_000_000:
            await query.answer("تعداد باید بین ۵۰ تا ۱٬۰۰۰٬۰۰۰ استارز باشد.", show_alert=True)
            return
        await refresh_auto_pricing(force=False)
        quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount) or _manual_stars_quote(amount)
        if quote is None:
            await query.answer("برای این مقدار هنوز قیمت ثبت نشده است.", show_alert=True)
            return
        order = {"amount": amount, "quote": quote, "recipient": "", "recipient_user_id": ""}
        context.user_data["stars_custom_order"] = order
        context.user_data.pop("stars_custom_wait_recipient", None)
        await query.edit_message_text(
            custom_stars_customer_text(order, quote) + "\n\nگیرندهٔ استارز را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 برای خودم", callback_data="customstars_recipient_self", style="primary")],
                [InlineKeyboardButton("🎁 برای شخص دیگر", callback_data="customstars_recipient_other", style="primary")],
                [InlineKeyboardButton("‹ بازگشت به مقدار دلخواه", callback_data="customstars_back", style="danger")],
            ]),
        )
        return

    if data == "customstars_recipient_self":
        order = context.user_data.get("stars_custom_order")
        if not isinstance(order, dict):
            await query.answer("ابتدا مقدار استارز را تأیید کن.", show_alert=True)
            return
        username = str(query.from_user.username or "").strip()
        if not username:
            context.user_data["stars_custom_wait_recipient"] = {"role": "self"}
            await query.edit_message_text(
                "برای این سفارش، آیدی عمومی تلگرام را ارسال کن؛ مثلاً @SeyedPro_AIA.\n"
                "حساب شما نام کاربری عمومی ندارد، بنابراین برای تحویل باید آیدی دریافت‌کننده ثبت شود."
            )
            await context.bot.send_message(
                chat_id=int(query.message.chat_id),
                text="برای لغو این سفارش، فقط دکمهٔ «❌ لغو سفارش» در کیبورد پایین را بزن.",
                reply_markup=customer_order_cancel_keyboard(),
            )
            return
        order["recipient"] = "@" + username.lstrip("@")
        order["recipient_user_id"] = str(query.from_user.id)
        context.user_data["stars_custom_order"] = order
        context.user_data.pop("stars_custom_wait_recipient", None)
        await query.edit_message_text(custom_stars_customer_text(order, order.get("quote")), reply_markup=custom_stars_payment_keyboard(order["amount"]))
        return

    if data == "customstars_recipient_other":
        order = context.user_data.get("stars_custom_order")
        if not isinstance(order, dict):
            await query.answer("ابتدا مقدار استارز را تأیید کن.", show_alert=True)
            return
        context.user_data["stars_custom_wait_recipient"] = {"role": "other"}
        await query.edit_message_text(
            "آیدی عمومی دریافت‌کننده را با @ ارسال کن؛ مثال: @SeyedPro_AIA\n"
            "اطلاعات ورود یا کد تأیید را ارسال نکن."
        )
        await context.bot.send_message(
            chat_id=int(query.message.chat_id),
            text="برای لغو این سفارش، فقط دکمهٔ «❌ لغو سفارش» در کیبورد پایین را بزن.",
            reply_markup=customer_order_cancel_keyboard(),
        )
        return

    if data == "customstars_back_recipient":
        order = context.user_data.get("stars_custom_order")
        if not isinstance(order, dict):
            await query.answer("اطلاعات انتخاب استارز پیدا نشد.", show_alert=True)
            return
        order["recipient"] = ""
        order["recipient_user_id"] = ""
        context.user_data["stars_custom_order"] = order
        await query.edit_message_text(
            custom_stars_customer_text(order, order.get("quote")) + "\n\nگیرندهٔ استارز را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 برای خودم", callback_data="customstars_recipient_self", style="primary")],
                [InlineKeyboardButton("🎁 برای شخص دیگر", callback_data="customstars_recipient_other", style="primary")],
                [InlineKeyboardButton("‹ بازگشت به مقدار دلخواه", callback_data="customstars_back", style="danger")],
            ]),
        )
        return

    if data.startswith("customstars_pay_card_") or data.startswith("customstars_pay_wallet_"):
        is_card = data.startswith("customstars_pay_card_")
        try:
            amount = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("مقدار استارز معتبر نیست.", show_alert=True)
            return
        order = context.user_data.get("stars_custom_order")
        if not isinstance(order, dict) or int(order.get("amount") or 0) != amount or not order.get("recipient"):
            await query.answer("اطلاعات سفارش یا گیرنده پیدا نشد؛ انتخاب را دوباره انجام بده.", show_alert=True)
            return

        await refresh_auto_pricing(force=False)
        fresh_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount) or _manual_stars_quote(amount)
        if fresh_quote is None:
            await query.answer("قیمت این مقدار ثبت نشده است.", show_alert=True)
            return

        previous_quote = order.get("quote") or {}
        if previous_quote.get("sale_toman") is None or int(previous_quote.get("sale_toman")) != int(fresh_quote["sale_toman"]):
            order["quote"] = fresh_quote
            context.user_data["stars_custom_order"] = order
            await query.answer("قیمت به‌روز شد؛ مبلغ تازه را بررسی کن و دوباره روش پرداخت را بزن.", show_alert=True)
            await query.edit_message_text(
                custom_stars_customer_text(order, fresh_quote, "قیمت پیش از ثبت سفارش دوباره بررسی شد و تغییر داشت؛ ابتدا مبلغ تازه را بررسی کن."),
                reply_markup=custom_stars_payment_keyboard(amount),
            )
            return
        order["quote"] = fresh_quote

        if is_card:
            card_number = str(bot_text("payment_card_number") or "").strip()
            if not card_number or card_number.casefold() in {"هنوز تنظیم نشده", "تنظیم نشده", "ثبت نشده"}:
                await query.answer("اطلاعات کارت هنوز توسط مدیریت تنظیم نشده است.", show_alert=True)
                return
            product_id = ensure_custom_stars_product()
            details = custom_stars_order_details(order, fresh_quote)
            try:
                with db() as con:
                    cur = con.execute(
                        "INSERT INTO orders (user_id, product_id, price, status, currency, order_details, payment_method) "
                        "VALUES (?, ?, ?, 'در انتظار ارسال رسید', 'تومان', ?, 'کارت‌به‌کارت')",
                        (query.from_user.id, product_id, int(fresh_quote["sale_toman"]), details),
                    )
                    order_id = int(cur.lastrowid)
            except Exception:
                logger.exception("Custom Stars card order creation failed")
                await query.answer("ثبت سفارش انجام نشد؛ دوباره تلاش کن.", show_alert=True)
                return
            context.user_data["pending_receipt_order_id"] = order_id
            context.user_data.pop("stars_custom_order", None)
            context.user_data.pop("stars_custom_wait_recipient", None)
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(f"🧾 سفارش مقدار دلخواه استارز #{order_id}\n"
                          f"کاربر: {query.from_user.full_name} (@{query.from_user.username or 'بدون‌نام'}) — {query.from_user.id}\n"
                          f"تعداد: {amount:,} استارز\nگیرنده: {order['recipient']}\n"
                          f"مبلغ فروش: {toman(fresh_quote['sale_toman'])} تومان\n"
                          f"جزئیات هزینه: {quote_breakdown_text(fresh_quote)}\n"
                          "وضعیت: در انتظار ارسال رسید؛ تحویل فقط با تأیید دستی مدیر."),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 جزئیات سفارش", callback_data=f"admin_order_view_{order_id}")]]),
                )
            except Exception:
                logger.warning("Could not notify admin about custom Stars order #%s", order_id, exc_info=True)
            await query.edit_message_text(
                f"✅ سفارش استارز دلخواه #{order_id} ثبت شد.\n\n"
                f"⭐ تعداد: {amount:,} استارز\n💰 مبلغ قابل پرداخت: {toman(fresh_quote['sale_toman'])} تومان\n"
                f"🏦 شماره کارت: {card_number}\n👤 صاحب کارت: {bot_text('payment_card_owner')}\n\n"
                f"{bot_text('payment_instructions')}\n\nپس از پرداخت، تصویر واضح رسید را ارسال کن. سفارش فقط پس از تأیید دستی مدیر قابل تحویل است.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو سفارش", callback_data=f"cancel_receipt_order_{order_id}", style="danger")]]),
            )
            return

        # Wallet payment is atomic, but fulfillment remains a separate manual admin action.
        product_id = ensure_custom_stars_product()
        details = custom_stars_order_details(order, fresh_quote)
        try:
            with db() as con:
                con.execute("BEGIN IMMEDIATE")
                user_row = con.execute("SELECT balance FROM users WHERE user_id=?", (query.from_user.id,)).fetchone()
                if not user_row:
                    con.rollback()
                    await query.answer("حساب کاربری پیدا نشد.", show_alert=True)
                    return
                balance = int(user_row["balance"] or 0)
                sale_price = int(fresh_quote["sale_toman"])
                if balance < sale_price:
                    con.rollback()
                    await query.answer(f"موجودی کافی نیست. مبلغ: {toman(sale_price)} تومان؛ موجودی: {toman(balance)} تومان.", show_alert=True)
                    return
                update = con.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (sale_price, query.from_user.id, sale_price))
                if update.rowcount != 1:
                    con.rollback()
                    await query.answer("موجودی تغییر کرده است؛ دوباره بررسی کن.", show_alert=True)
                    return
                cur = con.execute(
                    "INSERT INTO orders (user_id, product_id, price, status, currency, order_details, payment_method) "
                    "VALUES (?, ?, ?, 'پرداخت‌شده - در انتظار تحویل', 'تومان', ?, 'کیف پول')",
                    (query.from_user.id, product_id, sale_price, details),
                )
                order_id = int(cur.lastrowid)
                new_balance = balance - sale_price
                con.commit()
        except Exception:
            logger.exception("Custom Stars wallet order creation failed")
            await query.answer("پرداخت از کیف پول انجام نشد.", show_alert=True)
            return
        context.user_data.pop("stars_custom_order", None)
        context.user_data.pop("stars_custom_wait_recipient", None)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(f"💰 سفارش مقدار دلخواه استارز #{order_id} با کیف پول پرداخت شد.\n"
                      f"کاربر: {query.from_user.full_name} — {query.from_user.id}\n"
                      f"تعداد: {amount:,} استارز؛ گیرنده: {order['recipient']}\n"
                      f"قیمت فروش: {toman(sale_price)} تومان\nجزئیات هزینه: {quote_breakdown_text(fresh_quote)}\n"
                      "وضعیت: در انتظار تحویل دستی مدیر."),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 جزئیات سفارش", callback_data=f"admin_order_view_{order_id}")]]),
            )
        except Exception:
            logger.warning("Could not notify admin about custom Stars wallet order #%s", order_id, exc_info=True)
        await query.edit_message_text(
            f"✅ پرداخت سفارش #{order_id} انجام شد.\n⭐ تعداد: {amount:,} استارز\n"
            f"💰 پرداخت‌شده: {toman(sale_price)} تومان\n👛 موجودی باقی‌مانده: {toman(new_balance)} تومان\n\n"
            "📦 وضعیت: در انتظار تحویل دستی مدیر.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⌂ منوی اصلی", callback_data="home", style="primary")]]),
        )
        return

    if data == "customstars_back":
        context.user_data.pop("stars_custom_amount", None)
        context.user_data.pop("stars_custom_order", None)
        context.user_data.pop("stars_custom_wait_recipient", None)
        await query.edit_message_text(stars_text(), reply_markup=build_stars_menu())
        return

    if data == "stars_custom":
        await refresh_auto_pricing(force=False)
        context.user_data["stars_custom_amount"] = ""
        context.user_data.pop("stars_custom_order", None)
        context.user_data.pop("stars_custom_wait_recipient", None)
        context.user_data["stars_custom_chat_id"] = query.message.chat_id
        context.user_data["stars_custom_message_id"] = query.message.message_id
        await query.edit_message_text(custom_stars_display_text(""), reply_markup=custom_stars_keyboard())
        return

    if data == "back_to_services":
        await query.edit_message_text(
            "💎✨ خرید سرویس | SEYED PREMIUM 👑\n\n"
            "🛍️ سرویس موردنظرت رو انتخاب کن 👇🏻",
            reply_markup=service_menu(bot_text)
        )
        return

    if data == "home":
        # Explicit main-menu destination: do not reuse wallet or previous submenu renderers.
        # Returning home cancels only transient in-memory forms/drafts; no database
        # customers, products, order, receipt, wallet or payment history is deleted.
        for key in (
            "pending_product_id", "pending_stars_coupon", "stars_coupon", "stars_fixed_wait_recipient",
            "pending_product_coupon", "pending_product_coupon_input",
            "admin_coupon_state", "coupon_category_add_state", "coupon_edit_categories",
            "coupon_edit_state", "coupon_scope_state", "product_admin_state",
            "editor_wait_key", "uiadm_state", "config_wait_volume",
            "config_wait_duration", "vip_wait_volume", "vip_wait_duration",
            "stars_custom_amount", "stars_custom_order", "stars_custom_wait_recipient",
            "pricing_admin_state", "pending_dynamic_quote",
        ):
            context.user_data.pop(key, None)
        context.user_data["current_screen"] = "home"
        context.user_data["screen_history"] = []
        if get_main_menu_type() == MENU_TYPE_KEYBOARD:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                logger.debug("Could not remove previous inline keyboard", exc_info=True)
            await context.bot.send_message(
                chat_id=user_id,
                text=bot_text("main_menu"),
                reply_markup=main_menu(user_id),
            )
        else:
            await query.edit_message_text(
                bot_text("main_menu"),
                reply_markup=main_menu(),
            )
        return

    if data == "config_menu":
        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            balance = row["balance"] if row else 0
        except Exception:
            logger.exception("Wallet balance lookup failed")
            balance = 0

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💸 کانفیگ اقتصادی",
                    callback_data="config_economic",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    "🇮🇷 کانفیگ تانل VIP",
                    callback_data="config_vip",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎁 تست رایگان ۱ گیگابایت",
                    callback_data="config_test",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    bot_text("topup_button"),
                    callback_data="topup",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    bot_text("back_button"),
                    callback_data="home",
                    style="primary",
                )
            ],
        ])

        await query.edit_message_text(
            text=(
                "💎 <b>انتخاب سرویس</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"💳 موجودی کیف پول: <b>{balance:,} تومان</b>"
            ),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    if data == "config_wallet":
        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            balance = row["balance"] if row else 0
            await query.edit_message_text(
                f"💰 موجودی کیف پول شما: {balance:,} تومان\n\n"
                "برای افزایش موجودی از دکمه «افزایش موجودی» در منوی اصلی استفاده کن.",
                reply_markup=back_menu(),
            )
        except Exception:
            logger.exception("Wallet balance lookup failed")
            await query.edit_message_text(
                "💰 در حال حاضر امکان خواندن موجودی وجود ندارد.",
                reply_markup=back_menu(),
            )
        return

    if data == "config_economic":
        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            balance = row["balance"] if row else 0
        except Exception:
            logger.exception("Wallet balance lookup failed")
            balance = 0

        fa_digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
        balance_text = f"{balance:,}".translate(fa_digits)

        text = (
            "🏷 💸 اقتصادی\n\n"
            "▫️ متصل و کیفیت متناسب در اکثر مواقع\n"
            "▪️ دارای سرویس تانل با ضریب مصرف ۵ برابر\n"
            "▫️ قیمت مناسب نسبت به قیمت\n"
            "▪️ پشتیبانی و بروزرسانی\n\n"
            "🛒 خرید سرویس\n\n"
            f"💵 موجودی : {balance_text} تومان\n\n"
            "🏷 پلن مد نظرت رو انتخاب کن :"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 پلن ثابت — ۱۴۹٬۰۰۰ تومان", callback_data="config_fixed", style="success")],
            [InlineKeyboardButton("🧮 پلن دلخواه — هر گیگ ۲٬۰۰۰ تومان", callback_data="config_custom", style="success")],
            [InlineKeyboardButton(bot_text("topup_button"), callback_data="topup", style="primary")],
            [InlineKeyboardButton("⬅️ بازگشت به سرویس‌ها", callback_data="config_menu", style="primary")],
        ])

        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data == "config_fixed":
        price = 149000
        context.user_data["config_plan_type"] = "fixed"
        context.user_data["config_price"] = price
        context.user_data["config_volume"] = 30
        context.user_data["config_duration"] = 30
        context.user_data["config_invoice_open"] = False
        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ).fetchone()
            balance = row["balance"] if row else 0
        except Exception:
            logger.exception("Wallet balance lookup failed")
            balance = 0
        shortage = max(0, price - balance)
        await query.edit_message_text(
            "📋 جزئیات پلن اقتصادی ثابت\n\n"
            "⏳ مدت: ۳۰ روز\n"
            "📊 حجم: ۳۰ گیگابایت با سهمیه هفتگی\n"
            f"💰 قیمت: {price:,} تومان\n\n"
            f"👛 موجودی شما: {balance:,} تومان\n"
            + (f"❌ کسری موجودی: {shortage:,} تومان\n\n" if shortage else "✅ موجودی کافی است.\n\n")
            + "می‌تونی همین الان فقط کسری رو پرداخت کنی؛ بعد از تأیید، خرید خودکار انجام می‌شه.✅\n\n"
"⚠️ توجه: این قابلیت هنوز فعال نیست؛ درگاه واقعی متصل نشده و پرداخت یا تحویل خودکار انجام نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت / محاسبه کسری", callback_data="config_open_payment", style="success")],
                [InlineKeyboardButton(bot_text("topup_button"), callback_data="topup", style="primary")],
                [InlineKeyboardButton("⬅️ بازگشت به پلن‌ها", callback_data="config_economic", style="primary")],
            ]),
        )
        return

    if data == "config_custom":
        context.user_data["config_wait_volume"] = True
        context.user_data.pop("config_wait_duration", None)
        context.user_data.pop("config_volume", None)
        await query.edit_message_text(
            "🧮 پلن دلخواه — انتخاب حجم\n\n"
            "چند گیگ می‌خواهی؟ عددی بین ۱۰ تا ۵۰۰ بفرست.\n\n"
            "📊 هر گیگ: ۲٬۰۰۰ تومان"
        )
        return

    if data == "config_change_volume":
        context.user_data["config_wait_volume"] = True
        context.user_data.pop("config_wait_duration", None)
        await query.edit_message_text("📊 حجم جدید را به گیگابایت بفرست (۱۰ تا ۵۰۰).")
        return

    if data == "config_change_duration":
        context.user_data["config_wait_duration"] = True
        await query.edit_message_text("⏳ مدت جدید را به روز بفرست (۳۰ تا ۳۶۵).")
        return

    if data == "config_open_payment":
        price = int(context.user_data.get("config_price", 0))
        if price <= 0:
            await query.answer("اول پلن اقتصادی را انتخاب کن.", show_alert=True)
            return

        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ).fetchone()
            balance = int(row["balance"]) if row else 0
        except Exception:
            logger.exception("Wallet balance lookup failed")
            balance = 0

        shortage = max(0, price - balance)
        if shortage == 0:
            await query.answer(
                "موجودی کافی است؛ اما تحویل سرویس هنوز فعال نیست.",
                show_alert=True,
            )
            return

        context.user_data["config_invoice_amount"] = shortage
        context.user_data["config_invoice_expires_at"] = time.time() + 900
        context.user_data["config_invoice_open"] = True
        amount_text = f"{shortage:,}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

        await query.edit_message_text(
            "✅ پرداخت از طریق درگاه امن\n\n"
            f"🧾 مبلغ قابل پرداخت : {amount_text} تومان\n"
            "⏱ مهلت پرداخت فاکتور : ۱۵ دقیقه\n\n"
            "⚠️ این صفحه فعلاً آزمایشی است و درگاه واقعی متصل نیست؛ پرداختی انجام نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت آزمایشی", callback_data="config_gateway_demo", style="success")],
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")],
                [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="topup", style="primary")],
                [InlineKeyboardButton("⬅️ بازگشت به جزئیات پلن", callback_data="config_back_details", style="primary")],
            ]),
        )
        return

    if data == "config_gateway_demo":
        expires_at = float(context.user_data.get("config_invoice_expires_at", 0))
        if not context.user_data.get("config_invoice_open") or not expires_at:
            await query.answer("فاکتور فعالی پیدا نشد.", show_alert=True)
            return
        if time.time() >= expires_at:
            context.user_data["config_invoice_open"] = False
            await query.answer("فاکتور منقضی شده است.", show_alert=True)
            await query.edit_message_text(
                "⌛ مهلت فاکتور تمام شده است. برای محاسبه دوبارهٔ کسری، به پلن‌ها برگرد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ بازگشت به پلن‌ها", callback_data="config_economic", style="primary")],
                ]),
            )
            return
        await query.answer(
            "درگاه واقعی متصل نیست؛ پرداختی انجام نشده و مبلغی کسر نشده است.",
            show_alert=True,
        )
        return

    if data == "config_invoice_cancel":
        context.user_data["config_invoice_open"] = False
        await query.edit_message_text(
            "❌ فاکتور لغو شد؛ هیچ مبلغی کسر نشده است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ بازگشت به پلن‌ها", callback_data="config_economic", style="primary")],
            ]),
        )
        return

    if data == "config_back_details":
        await query.answer(
            "برای محاسبه دوبارهٔ کسری، پلن اقتصادی را از نو انتخاب کن.",
            show_alert=True,
        )
        context.user_data["config_invoice_open"] = False
        return

    if data == "config_payment_unavailable":
        await query.answer("پرداخت هنوز متصل نیست؛ مبلغی کسر نشده است.", show_alert=True)
        return

    if data == "config_vip":
        context.user_data.pop("vip_wait_volume", None)
        context.user_data.pop("vip_wait_duration", None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 10 گیگ یکماهه — 49 هزار تومان", callback_data="vip_plan_10", style="success")],
            [InlineKeyboardButton("📦 50 گیگ یکماهه — 249 هزار تومان", callback_data="vip_plan_50", style="success")],
            [InlineKeyboardButton("📦 100 گیگ یکماهه — 489 هزار تومان", callback_data="vip_plan_100", style="success")],
            [InlineKeyboardButton("🧮 پلن دلخواه — انتخاب حجم و زمان", callback_data="vip_custom", style="primary")],
            [InlineKeyboardButton("💳 شارژ کیف پول", callback_data="topup", style="primary")],
            [InlineKeyboardButton("⬅️ بازگشت به کانفیگ‌ها", callback_data="config_menu", style="primary")],
        ])
        await query.edit_message_text(
            "🏷 ⭐️ 𝗩𝗜𝗣 تانل 🇮🇷\n"
            "▫️ تضمین اتصال با کیفیت✅\n"
            "▪️پشتیبانی و آپدیت و پیگیری VIP\n"
            "▫️سرویس تانل شده بدون ضریب\n"
            "▪️مولتی لوکیشن و متصل\n\n"
            "🛒 خرید سرویس\n\n"
            "💵 موجودی از کیف پول واقعی شما خوانده می‌شود.\n\n"
            "🏷 پلن مد نظرت رو انتخاب کن :",
            reply_markup=keyboard,
        )
        return

    if data.startswith("vip_plan_"):
        try:
            volume = int(data.rsplit("_", 1)[1])
        except ValueError:
            await query.answer("پلن نامعتبر است.", show_alert=True)
            return

        fixed = {
            10: (49000, 49535),
            50: (249000, 249386),
            100: (489000, 488878),
        }
        if volume not in fixed:
            await query.answer("پلن پیدا نشد.", show_alert=True)
            return

        price, invoice_amount = fixed[volume]
        context.user_data["vip_price"] = price
        context.user_data["vip_invoice_amount"] = invoice_amount
        context.user_data["vip_volume"] = volume
        context.user_data["vip_duration"] = 30
        context.user_data["vip_plan_type"] = "fixed"

        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ).fetchone()
            balance = row["balance"] if row else 0
        except Exception:
            logger.exception("VIP wallet lookup failed")
            balance = 0

        shortage = max(0, price - balance)
        shortage_line = (
            f"💸 کسری موجودی : {shortage:,} تومان\n"
            "می‌تونی همین الان فقط کسری رو پرداخت کنی؛ بعد از تأیید، خرید خودکار انجام می‌شه.✅"
            if shortage else "✅ موجودی کیف پول برای این پلن کافی است."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 پرداخت / محاسبه کسری", callback_data="vip_open_payment", style="success")],
            [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="topup", style="primary")],
            [InlineKeyboardButton("⬅️ بازگشت به پلن‌های VIP", callback_data="config_vip", style="primary")],
            [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")],
        ])
        await query.edit_message_text(
            "📋 جزئیات پلن :\n\n"
            "⏰️ مدت : 30 روز\n"
            f"📊 حجم : {volume} گیگابایت\n"
            f"🪙 قیمت : {price:,} تومان\n"
            "ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ\n"
            f"🏷 موجودی شما : {balance:,} تومان"
            + (" ❌ (ناکافی)\n\n" if shortage else " ✅\n\n")
            + shortage_line,
            reply_markup=keyboard,
        )
        return

    if data == "vip_custom":
        context.user_data.pop("vip_wait_duration", None)
        context.user_data["vip_wait_volume"] = True
        context.user_data.pop("vip_volume", None)
        await query.edit_message_text(
            "🧮 پلن دلخواه — حجم\n\n"
            "چند گیگ می‌خوای؟ عددی بین 10 تا 1000 گیگ بفرست.\n\n"
            "📊 هر گیگ: ۵,۰۰۰ تومان\n"
            "⏳ هر روز: ۰ تومان\n\n"
            "برای لغو، دکمهٔ لغو عملیات را بزن.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")]
            ]),
        )
        return

    if data == "vip_open_payment":
        price = int(context.user_data.get("vip_price", 0))
        invoice_amount = int(context.user_data.get("vip_invoice_amount", price))
        if price <= 0:
            await query.answer("اول یک پلن انتخاب کن.", show_alert=True)
            return
        context.user_data["vip_payment_open"] = True
        await query.edit_message_text(
            "✅ پرداخت از طریق درگاه امن\n\n"
            f"🧾 مبلغ قابل پرداخت : {invoice_amount:,} تومان\n"
            "⏱ مهلت پرداخت فاکتور : 15 دقیقه\n\n"
            "⚠️ این صفحه فعلاً آزمایشی است و درگاه واقعی متصل نیست؛ پرداختی انجام نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت در درگاه", callback_data="vip_gateway_demo", style="success")],
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")],
                [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="topup", style="primary")],
                [InlineKeyboardButton("⬅️ بازگشت به جزئیات پلن", callback_data="vip_back_details", style="primary")],
            ]),
        )
        return

    if data == "vip_gateway_demo":
        await query.answer(
            "درگاه واقعی هنوز متصل نیست؛ هیچ پرداختی انجام نشده است.",
            show_alert=True,
        )
        return

    if data == "vip_back_details":
        volume = context.user_data.get("vip_volume")
        if volume in (10, 50, 100):
            fake_data = f"vip_plan_{volume}"
            # اجرای دوبارهٔ callback برای نمایش جزئیات پلن انتخاب‌شده
            context.application.create_task(_safe_callback_answer(query))
            context.user_data.pop("vip_payment_open", None)
            price = int(context.user_data.get("vip_price", 0))
            try:
                with db() as con:
                    row = con.execute(
                        "SELECT balance FROM users WHERE user_id=?", (user_id,)
                    ).fetchone()
                balance = row["balance"] if row else 0
            except Exception:
                balance = 0
            shortage = max(0, price - balance)
            await query.edit_message_text(
                "📋 جزئیات پلن :\n\n"
                "⏰️ مدت : 30 روز\n"
                f"📊 حجم : {volume} گیگابایت\n"
                f"🪙 قیمت : {price:,} تومان\n"
                "ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ ـ\n"
                f"🏷 موجودی شما : {balance:,} تومان"
                + (" ❌ (ناکافی)\n\n" if shortage else " ✅\n\n")
                + (f"💸 کسری موجودی : {shortage:,} تومان\n"
                   "می‌تونی همین الان فقط کسری رو پرداخت کنی؛ بعد از تأیید، خرید خودکار انجام می‌شه.✅"
                   if shortage else "✅ موجودی کیف پول برای این پلن کافی است."),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 پرداخت / محاسبه کسری", callback_data="vip_open_payment", style="success")],
                    [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="topup", style="primary")],
                    [InlineKeyboardButton("⬅️ بازگشت به پلن‌های VIP", callback_data="config_vip", style="primary")],
                    [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")],
                ]),
            )
            return

        price = int(context.user_data.get("vip_price", 0))
        context.application.create_task(_safe_callback_answer(query))
        context.user_data.pop("vip_payment_open", None)
        await query.edit_message_text(
            "🧮 خلاصهٔ پلن دلخواه\n\n"
            f"📊 حجم: {context.user_data.get('vip_volume', 0)} گیگابایت\n"
            f"⏳ مدت: {context.user_data.get('vip_duration', 0)} روز\n"
            f"💰 قیمت: {price:,} تومان\n\n"
            "برای ادامه، پلن دلخواه را دوباره انتخاب کن.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧮 پلن دلخواه", callback_data="vip_custom", style="primary")],
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")],
            ]),
        )
        return

    if data == "vip_change_volume":
        context.user_data["vip_wait_volume"] = True
        context.user_data["vip_wait_duration"] = False
        context.user_data["vip_plan_type"] = "custom"
        await query.edit_message_text(
            "🧮 پلن دلخواه — تغییر حجم\n\n"
            "چند گیگ می‌خوای؟ عددی بین 10 تا 1000 گیگ بفرست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")]
            ]),
        )
        return

    if data == "vip_change_duration":
        volume = context.user_data.get("vip_volume")
        if volume is None or not 10 <= int(volume) <= 1000:
            await query.answer("اول حجم پلن را انتخاب کن.", show_alert=True)
            return
        context.user_data["vip_wait_volume"] = False
        context.user_data["vip_wait_duration"] = True
        context.user_data["vip_plan_type"] = "custom"
        await query.edit_message_text(
            "🧮 پلن دلخواه — تغییر مدت\n\n"
            f"📊 حجم انتخابی: {volume} گیگ\n\n"
            "چند روز می‌خوای؟ عددی بین 30 تا 90 روز بفرست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")]
            ]),
        )
        return

    if data == "vip_cancel":
        for key in (
            "vip_wait_volume", "vip_wait_duration", "vip_payment_open",
            "vip_volume", "vip_duration", "vip_price",
            "vip_invoice_amount", "vip_plan_type",
        ):
            context.user_data.pop(key, None)
        await query.edit_message_text(
            "❌ عملیات لغو شد. هیچ مبلغی کسر نشده است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏷 بازگشت به VIP تانل", callback_data="config_vip", style="primary")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
            ]),
        )
        return

    if data == "config_vip":
        context.application.create_task(_safe_callback_answer(query))
        return

    if data == "config_test":
        await query.edit_message_text(
            "🎁 تست رایگان ۱ گیگابایت\n\n"
            "بعد از اتصال پنل واقعی، این قسمت برای ساخت سرویس آزمایشی فعال می‌شود.\n"
            "⚠️ در حال حاضر کانفیگ آزمایشی ساخته نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ بازگشت به کانفیگ‌ها", callback_data="config_menu", style="primary")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
            ]),
        )
        return

    # Configurable service buttons must have a route, even when a new category is added.
    if data.startswith("service_") and data != "service_stars":
        service_category = data[len("service_"):]
        await _show_product_category(query, service_category, user_id)
        return

    if data == "free_stars":
        await query.edit_message_text(
            "⭐ استارز رایگان | SEYED PREMIUM\n\n"
            "این قابلیت هنوز به منبع واقعی دریافت استارز متصل نشده است؛ بنابراین در حال حاضر استارز رایگان ثبت یا پرداخت نمی‌شود.\n\n"
            "برای دیدن خدمات فروشگاه از منوی اصلی استفاده کن.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 خرید سرویس", callback_data="cat_stars")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
            ]),
        )
        return

    if data == "faq":
        await query.edit_message_text(
            "❓ سؤالات متداول | SEYED PREMIUM\n\n"
            "📦 سفارش پس از ثبت برای بررسی مدیر ارسال می‌شود.\n"
            "💳 پرداخت و تحویل فقط پس از بررسی و تأیید مدیر انجام می‌شود.\n"
            "🎧 برای پیگیری سفارش با پشتیبانی تماس بگیر.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎧 پشتیبانی", callback_data="support")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
            ]),
        )
        return

    if data.startswith("cat_"):
        category = data[4:]
        if category == "premium":
            await _show_product_category(query, category, user_id)
            return
        if category not in PRODUCTS:
            # Categories added in the database/configuration remain navigable.
            await _show_product_category(query, category, user_id)
            return
        if category not in PRODUCTS:
            await query.edit_message_text("❌ دسته‌بندی پیدا نشد.", reply_markup=back_menu())
            return

        with db() as con:
            rows = con.execute(
                """
                SELECT id, name, price, button_text, icon, button_style, sort_order, currency, old_price FROM products
                WHERE category=? AND active=1
                ORDER BY sort_order, id
                """,
                (category,),
            ).fetchall()

        if not rows:
            await query.edit_message_text(
                "فعلاً محصولی در این دسته موجود نیست.",
                reply_markup=back_menu(),
            )
            return

        buttons = []
        if category == "gifts":
            # Dynamic product list: official gifts and custom gifts are shown from DB.
            # No hard-coded emoji catalogue; new products appear automatically.
            row_buttons = []
            gift_cfg_for_menu = load_gift_settings()
            for product in rows[:90]:
                price_value = int(product["price"] or 0)
                if gift_cfg_for_menu.get("auto_rate_enabled") and product["source_type"] == "official":
                    live_value = gift_price_for_stars(product["star_count"], gift_cfg_for_menu)
                    price_value = int(live_value or 0)
                currency = str(product["currency"] or "تومان")
                price_text = f"{price_value:,} {currency}" if price_value > 0 else ("قیمت زنده ناموجود" if gift_cfg_for_menu.get("auto_rate_enabled") else "قیمت تنظیم نشده")
                icon = str(product["icon"] or "🎁").strip() or "🎁"
                label = str(product["button_text"] or product["name"]).strip()
                row_buttons.append(InlineKeyboardButton(
                    f"{icon} {label[:26]} | {price_text}"[:64],
                    callback_data=f"buy_{product['id']}",
                    style=str(product["button_style"] or "success") if str(product["button_style"] or "success") in {"primary", "success", "danger"} else "success",
                ))
                if len(row_buttons) == 2:
                    buttons.append(row_buttons)
                    row_buttons = []
            if row_buttons:
                buttons.append(row_buttons)
        else:
            for product in rows:
                price_value = int(product["price"] or 0)
                currency = str(product["currency"] or "تومان")
                price_text = f"{price_value:,} {currency}" if price_value else "استعلام قیمت"
                old_price = int(product["old_price"] or 0)
                if old_price > price_value > 0:
                    price_text = f"{old_price:,} ← {price_value:,} {currency}"
                label = str(product["button_text"] or "").strip() or str(product["name"])
                icon = str(product["icon"] or "").strip()
                button_text = f"{icon + ' ' if icon else ''}{label} | {price_text}"
                style = str(product["button_style"] or "success").strip().lower()
                if style not in {"primary", "success", "danger"}:
                    style = "success"
                buttons.append([
                    InlineKeyboardButton(
                        button_text[:64], callback_data=f"buy_{product['id']}", style=style,
                    )
                ])
        buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="home", style="primary")])

        await query.edit_message_text(
            f"🛍 {PRODUCTS[category][0]}\n\n"
            "محصول موردنظر را انتخاب کن.\n"
            "قیمت صفر یعنی هنوز قیمت توسط مدیر تعیین نشده است.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("product_coupon_enter_"):
        try:
            product_id = int(data[len("product_coupon_enter_"):])
        except ValueError:
            await query.answer("شناسهٔ محصول نامعتبر است.", show_alert=True)
            return
        if context.user_data.get("pending_product_id") != product_id:
            await query.answer("این مرحلهٔ سفارش منقضی شده است؛ محصول را دوباره انتخاب کن.", show_alert=True)
            return
        with db() as con:
            product = con.execute("SELECT id, name, price, active FROM products WHERE id=?", (product_id,)).fetchone()
        if not product or not product["active"]:
            await query.answer("این محصول دیگر فعال نیست.", show_alert=True)
            return
        if int(product["price"] or 0) <= 0:
            await query.answer("قیمت این محصول هنوز مشخص نیست؛ کد تخفیف فعلاً قابل محاسبه نیست.", show_alert=True)
            return
        context.user_data["pending_product_coupon_input"] = product_id
        await query.edit_message_text(
            "🏷️ کد تخفیف را وارد کن (حروف انگلیسی/عدد، بدون فاصله):\n"
            "مثال: SUMMER30\nبرای لغو /cancel را بفرست.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو ورود کد", callback_data="product_coupon_cancel")]])
        )
        return

    if data == "product_coupon_cancel":
        context.user_data.pop("pending_product_coupon_input", None)
        context.user_data.pop("pending_product_coupon", None)
        await query.answer("ورود کد لغو شد.")
        await query.edit_message_text("❌ ورود کد تخفیف لغو شد. اگر می‌خواهی سفارش بدهی، اطلاعات محصول را در یک پیام بفرست. برای لغو کامل، /start را بزن.")
        return

    if data.startswith("buy_"):
        try:
            product_id = int(data[4:])
        except ValueError:
            await query.edit_message_text("شناسه محصول نامعتبر است.", reply_markup=back_menu())
            return

        with db() as con:
            product = con.execute(
                "SELECT * FROM products WHERE id=? AND active=1",
                (product_id,),
            ).fetchone()

        if product is None:
            await query.edit_message_text("این محصول موجود نیست.", reply_markup=back_menu())
            return

        product_category = str(product["category"] or "").strip().lower()
        dynamic_quote = None
        if product_category == "premium":
            months_for_order = premium_product_months(product["name"])
            premium_settings_for_order = load_premium_manager_settings()
            if months_for_order and premium_settings_for_order["delivery_modes"].get(str(months_for_order)) == "auto":
                try:
                    current_balance = await get_real_bot_star_balance(context.bot)
                except Exception:
                    await query.answer("موجودی واقعی Stars ربات قابل دریافت نیست؛ سفارش خودکار فعلاً پذیرفته نمی‌شود.", show_alert=True)
                    return
                required_balance = PREMIUM_PLAN_STARS.get(months_for_order, 10**9)
                if current_balance < required_balance:
                    await query.answer(f"موجودی ربات {current_balance} ⭐ است و برای پلن {months_for_order} ماهه {required_balance} ⭐ لازم است. فعلاً قابل سفارش نیست.", show_alert=True)
                    return
        if product_category in {"stars", "premium"}:
            await refresh_auto_pricing(force=False)
            with db() as con:
                product = con.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
            if product is None:
                await query.answer("این محصول دیگر فعال نیست.", show_alert=True)
                return
            dynamic_quote = dynamic_product_quote(product)
            if dynamic_quote is None:
                await query.answer("قیمت این محصول ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.", show_alert=True)
                return
            context.user_data["pending_dynamic_quote"] = {
                "product_id": product_id,
                "sale_toman": int(dynamic_quote["sale_toman"]),
                "quote": dynamic_quote,
            }
        else:
            context.user_data.pop("pending_dynamic_quote", None)

        context.user_data["pending_product_id"] = product_id
        context.user_data.pop("pending_product_coupon", None)
        if product_category == "gifts":
            gift_cfg = load_gift_settings()
            can_offer_auto = (
                product["source_type"] == "official"
                and bool(product["telegram_gift_id"])
                and str(product["delivery_mode"] or "manual") == "auto"
                and not gift_cfg.get("manual_delivery_all")
            )
            if gift_cfg.get("customer_delivery_choice") and can_offer_auto:
                context.user_data["pending_gift_delivery_product_id"] = product_id
                await query.edit_message_text(
                    f"🎁 روش تحویل «{product['name']}» را انتخاب کن.\n\n"
                    "⚡ ارسال خودکار توسط ربات فقط پس از تأیید پرداخت و در صورت موفقیت واقعی تلگرام انجام می‌شود.\n"
                    "👤 تحویل دستی توسط پشتیبانی نیز در دسترس است.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⚡ ارسال خودکار با ربات", callback_data=f"gift_customer_delivery_auto_{product_id}", style="success")],
                        [InlineKeyboardButton("👤 تحویل دستی توسط پشتیبانی", callback_data=f"gift_customer_delivery_manual_{product_id}", style="primary")],
                        [InlineKeyboardButton("❌ لغو سفارش", callback_data="gift_customer_delivery_cancel", style="danger")],
                    ]),
                )
                return
            context.user_data["pending_gift_delivery_mode"] = "manual" if gift_cfg.get("manual_delivery_all") else ("auto" if can_offer_auto else "manual")
        context.user_data.pop("pending_product_coupon_input", None)
        prompt = product_details_prompt(product)
        description = str(product["description"] or "").strip() if "description" in product.keys() else ""
        if description:
            prompt = description + "\n\n" + prompt
        display_title = str(product["button_text"] or "").strip() or str(product["name"])
        currency = str(product["currency"] or "تومان") if "currency" in product.keys() else "تومان"
        current_price = int(dynamic_quote["sale_toman"]) if dynamic_quote else int(product["price"] or 0)
        if product_category == "gifts" and product["source_type"] == "official" and load_gift_settings().get("auto_rate_enabled") and load_auto_pricing_config().get("enabled"):
            gift_live = gift_price_for_stars(product["star_count"], load_gift_settings())
            if gift_live is not None:
                current_price = int(gift_live)
                context.user_data["pending_gift_price_quote"] = {"product_id": product_id, "sale_toman": current_price}
                currency = "تومان"
        if dynamic_quote:
            currency = "تومان"
        old_price = int(product["old_price"] or 0) if (not dynamic_quote and "old_price" in product.keys()) else 0
        price_line = (f"💰 قیمت: {current_price:,} {currency}" if current_price > 0 else "💰 قیمت: استعلام قیمت")
        if old_price > current_price > 0:
            price_line = f"💰 قیمت: {old_price:,} ← {current_price:,} {currency}"
        prompt = price_line + "\n\n" + prompt
        product_keyboard = []
        if current_price > 0:
            product_keyboard.append([InlineKeyboardButton("🏷️ وارد کردن کد تخفیف", callback_data=f"product_coupon_enter_{product_id}")])
        product_message = (
            f"🛍 محصول انتخاب‌شده: {display_title}\n\n{prompt}\n\n"
            "برای لغو سفارش از دکمهٔ «❌ لغو سفارش» در کیبورد پایین استفاده کن."
        )
        product_markup = InlineKeyboardMarkup(product_keyboard)
        image_file_id = str(product["image_file_id"] or "").strip() if "image_file_id" in product.keys() else ""
        if image_file_id:
            # Photos are optional catalog metadata. If Telegram rejects the file_id or caption,
            # fall back to a normal text screen without losing the customer's current state.
            try:
                # Keep the photo as presentation only. The actionable menu is sent
                # through edit_message_text so the global inline/reply-keyboard mode
                # and the same button-style handling apply consistently.
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=image_file_id,
                    caption=product_message[:1024],
                )
                await query.edit_message_text(
                    "✅ برای ادامهٔ سفارش، گزینه‌های زیر را انتخاب کن.",
                    reply_markup=product_markup,
                )
            except Exception:
                logger.warning("Could not show product image for product_id=%s", product_id, exc_info=True)
                await query.edit_message_text(product_message, reply_markup=product_markup)
        else:
            await query.edit_message_text(product_message, reply_markup=product_markup)
        await context.bot.send_message(
            chat_id=int(query.message.chat_id),
            text="اگر نمی‌خواهی ادامه بدهی، دکمهٔ «❌ لغو سفارش» را از کیبورد پایین بزن.",
            reply_markup=customer_order_cancel_keyboard(),
        )
        return


    if data == "account":
        try:
            with db() as con:
                user = con.execute(
                    "SELECT user_id, username, balance, joined_at "
                    "FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()

                total_orders = con.execute(
                    "SELECT COUNT(*) FROM orders WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]

                confirmed = con.execute(
                    "SELECT COUNT(*), COALESCE(SUM(price), 0) "
                    "FROM orders WHERE user_id = ? AND status = 'تأیید شده'",
                    (user_id,),
                ).fetchone()

            username = (
                "@" + str(user["username"])
                if user and user["username"] else "تنظیم نشده"
            )
            balance = user["balance"] if user and user["balance"] is not None else 0

            try:
                joined = jalali_date(user["joined_at"] if user else None)
            except Exception:
                joined = str(user["joined_at"]) if user and user["joined_at"] else "نامشخص"

            message = (
                "💎 حساب کاربری | SEYED PREMIUM\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🆔 شناسه تلگرام: {user_id}\n"
                f"👤 نام کاربری: {username}\n"
                f"📅 تاریخ عضویت: {joined}\n\n"
                f"👛 موجودی حساب: {balance:,} تومان\n"
                f"📦 کل سفارش‌ها: {total_orders}\n"
                f"✅ سفارش‌های تأییدشده: {confirmed[0]}\n"
                f"💰 مجموع سفارش‌های تأییدشده: {confirmed[1]:,} تومان\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            await query.edit_message_text(
                message,
                reply_markup=back_menu(),
            )
        except Exception:
            import logging
            logging.exception("SEYED PREMIUM account page error")
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="💎 حساب کاربری\n"
                         "در نمایش صفحه مشکلی پیش آمد. لطفاً دوباره تلاش کن."
                )
            except Exception:
                logging.exception("Could not send account fallback message")
        return

    if data == "topup":
        await query.edit_message_text(
            "💳 افزایش موجودی\n\n"
            "روش شارژ هنوز تنظیم نشده است.\n"
            "موجودی حساب شما در این صفحه تغییر نمی‌کند.\n\n"
            "وقتی روش شارژ را مشخص کنی، این بخش را فعال می‌کنیم.",
            reply_markup=back_menu(),
        )
        return

    if data == "invite":
        me = await context.bot.get_me()
        if not me.username:
            await query.edit_message_text(
                "❌ نام کاربری ربات تنظیم نشده است.",
                reply_markup=back_menu(),
            )
            return
        link = f"https://t.me/{me.username}?start=ref_{user_id}"
        with db() as con:
            invited = con.execute(
                "SELECT COUNT(*) AS n FROM users WHERE referred_by = ?", (user_id,)
            ).fetchone()["n"]
        await query.edit_message_text(
            "👥 دعوت دوستان\n\n"
            "لینک اختصاصی شما:\n"
            f"{link}\n\n"
            f"👤 کاربران ثبت‌شده از طریق لینک شما: {invited}\n\n"
            "لینک را برای دوستانت بفرست تا از طریق آن وارد ربات شوند.",
            reply_markup=back_menu(),
            disable_web_page_preview=True,
        )
        return

    if data == "wallet":
        with db() as con:
            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
        balance = row["balance"] if row else 0
        await query.edit_message_text(
            f"👛 کیف پول شما\n\n💰 موجودی: {balance:,} تومان\n\n"
            "شارژ خودکار در این نسخه فعال نیست. برای شارژ با پشتیبانی هماهنگ کن.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎧 درخواست شارژ / پشتیبانی", callback_data="support", style="success")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
            ]),
        )
        return

    if data == "orders":
        with db() as con:
            rows = con.execute(
                """
                SELECT orders.id, products.name, orders.price,
                       orders.status, orders.created_at
                FROM orders
                JOIN products ON products.id=orders.product_id
                WHERE orders.user_id=?
                ORDER BY orders.id DESC LIMIT 10
                """,
                (user_id,),
            ).fetchall()

        if not rows:
            text = "📦 هنوز سفارشی ثبت نکرده‌ای."
        else:
            text = "📦 آخرین سفارش‌های شما:\n\n"
            for row in rows:
                text += (
                    f"#{row['id']} — {row['name']}\n"
                    f"💰 {row['price']:,} تومان\n"
                    f"📌 {row['status']}\n\n"
                )

        await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")
                ]])
            )
        return

    if data == "support":
        await query.edit_message_text(
            "🎧 پشتیبانی SEYED PREMIUM\n\n"
            f"شناسه تلگرام شما: {user_id}\n\n"
            "برای پیگیری سفارش، این شناسه و شماره سفارش را برای مدیر فروشگاه بفرست.\n"
            "مدیر می‌تواند از طریق تلگرام با شما هماهنگ کند.",
            reply_markup=back_menu(),
        )
        return


    if data == "edit_content" or data.startswith("edit_item:") or data == "edit_cancel" or data == "edit_reset":
        if user_id != ADMIN_ID:
            await query.edit_message_text("⛔ دسترسی مجاز نیست.")
            return

        if data == "edit_content":
            keyboard = [
                [InlineKeyboardButton(label, callback_data=f"edit_item:{key}")]
                for key, (label, default) in EDITABLE_TEXTS.items()
            ]
            keyboard.append([InlineKeyboardButton("🏠 بازگشت به مدیریت", callback_data="edit_cancel")])
            await query.edit_message_text(
            "💎 مدیریت محتوا\n\n"
            "━━━━━━━━━━━━\n\n"
            "ویرایش متن‌ها و دکمه‌ها\n\n"
            "لطفاً گزینهٔ موردنظر را انتخاب کنید.\n\n"
            "✦ SEYED PREMIUM",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
            return

        if data == "edit_cancel":
            context.user_data.pop("editor_wait_key", None)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 ویرایش متن‌ها و دکمه‌ها", callback_data="edit_content")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")]
            ])
            await query.edit_message_text("⚙️ مدیریت متن‌ها", reply_markup=keyboard)
            return

        if data == "edit_reset":
            key = context.user_data.pop("editor_wait_key", None)
            if key in EDITABLE_TEXTS:
                settings = load_bot_settings()
                settings.pop(key, None)
                save_bot_settings(settings)
            await query.edit_message_text(
                "✅ متن به مقدار پیش‌فرض برگشت.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 بازگشت به ویرایش", callback_data="edit_content")]
                ])
            )
            return

        key = data.split(":", 1)[1]
        if key not in EDITABLE_TEXTS:
            await query.edit_message_text("این گزینه معتبر نیست.")
            return
        context.user_data["editor_wait_key"] = key
        label, default = EDITABLE_TEXTS[key]
        await query.edit_message_text(
            f"✏️ ویرایش: {label}\n\n"
            f"متن فعلی:\n{bot_text(key)}\n\n"
            "حالا متن جدید را به‌صورت یک پیام متنی بفرست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")]
            ])
        )
        return

    if data == "admin" or data.startswith("admin_"):
        if user_id != ADMIN_ID:
            await query.edit_message_text("⛔ دسترسی مجاز نیست.")
            return

        if data == "admin":
            current_mode = get_main_menu_type()
            mode_label = "⌨️ کیبورد معمولی" if current_mode == MENU_TYPE_KEYBOARD else "💎 دکمه‌های شیشه‌ای"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 آمار فروشگاه", callback_data="admin_stats"),
                 InlineKeyboardButton("🛍 محصولات و قیمت‌ها", callback_data="admin_products")],
                [InlineKeyboardButton("📦 آخرین سفارش‌ها", callback_data="admin_orders"),
                 InlineKeyboardButton("🏷️ مدیریت کدهای تخفیف", callback_data="admin_coupons")],
                [InlineKeyboardButton("⚙️ مدیریت قیمت‌گذاری خودکار", callback_data="admin_auto_prices", style="primary"),
                 InlineKeyboardButton("💬 پیام‌های پشتیبانی", callback_data="admin_support_tickets", style="success")],
                [InlineKeyboardButton(f"⌨️ تغییر به کیبورد معمولی{' ✅' if current_mode == MENU_TYPE_KEYBOARD else ''}", callback_data="admin_menu_set_keyboard"),
                 InlineKeyboardButton(f"💎 تغییر به شیشه‌ای{' ✅' if current_mode == MENU_TYPE_INLINE else ''}", callback_data="admin_menu_set_inline")],
                        [InlineKeyboardButton("📝 ویرایش متن‌ها و دکمه‌ها", callback_data="uiadm_home")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
            ])
            await query.edit_message_text(
                f"⚙️ پنل مدیریت SEYED PREMIUM\n\n🔹 حالت منوی کل ربات: {mode_label}",
                reply_markup=keyboard
            )
            return

        if data == "admin_auto_prices":
            context.user_data.pop("auto_pricing_return", None)
            await query.edit_message_text(
                "⚙️ مدیریت قیمت‌گذاری خودکار | SEYED PREMIUM\n━━━━━━━━━━━━━━━━━━\n\nمرکز یکپارچهٔ قیمت‌های فروشگاه: از دکمهٔ اول برای نرخ تتر/دلار، منابع قیمت، سود و کارمزد استفاده کن؛ سپس با انتخاب هر گروه، محصولات و قیمت‌های همان گروه را مدیریت کن.\n\nمحصولات و دسته‌بندی‌های جدید نیز به‌صورت خودکار به این فهرست اضافه می‌شوند:",
                reply_markup=auto_price_categories_markup(context),
            )
            return

        if data.startswith("admin_auto_cat|"):
            try:
                _, token, page_text = data.split("|", 2)
                page = max(1, int(page_text))
            except (ValueError, TypeError):
                await query.answer("درخواست دسته‌بندی معتبر نیست.", show_alert=True)
                return
            if token == "gifts":
                await _show_gift_admin_home(query, context.bot)
                return
            if token == "premium":
                context.application.create_task(refresh_auto_pricing(force=False))
                sync_premium_prices_in_db()
                await query.edit_message_text(premium_admin_text(), reply_markup=premium_admin_keyboard())
                return
            text_value, markup = auto_price_category_screen(context, token, page)
            await query.edit_message_text(text_value, reply_markup=markup)
            return

        if data == "admin_auto_pricing":
            context.user_data.pop("pricing_admin_state", None)
            await query.edit_message_text(pricing_panel_text(), reply_markup=pricing_panel_keyboard())
            return

        if data in {"admin_nobitex_api_setup", "admin_nobitex_api_clear", "admin_pricing_api_key"}:
            context.user_data.pop("pricing_admin_state", None)
            await query.edit_message_text(
                "✅ نرخ تتر از API عمومی رسمی نوبیتکس دریافت می‌شود و برای این کار هیچ API Key یا Private Key لازم نیست.\n\n"
                "از دکمهٔ «دریافت نرخ تتر از API عمومی نوبیتکس» برای دریافت و همگام‌سازی نرخ استفاده کن.",
                reply_markup=pricing_panel_keyboard(),
            )
            return


        if data in {"admin_fx_api_menu", "admin_fx_api_setup", "admin_fx_api_clear"}:
            context.user_data.pop("pricing_admin_state", None)
            await query.edit_message_text(
                "در این نسخه، منبع نرخ تتر فقط API عمومی نوبیتکس است. تنظیم API سفارشی یا کلید جداگانه استفاده نمی‌شود.",
                reply_markup=pricing_panel_keyboard(),
            )
            return

        if data == "admin_pricing_refresh":
            if context.user_data.get("_pricing_refresh_pending"):
                await query.answer("به‌روزرسانی در حال انجام است؛ درخواست تکراری لازم نیست.")
                return
            context.user_data["_pricing_refresh_pending"] = True
            cfg = load_auto_pricing_config()
            await safe_callback_edit_or_reply(
                query, pricing_panel_text(cfg, "🔄 درخواست ثبت شد؛ دریافت نرخ‌ها در پس‌زمینه انجام می‌شود و دکمه‌های دیگر ربات منتظر شبکه نمی‌مانند."),
                pricing_panel_keyboard(cfg),
            )
            context.application.create_task(_finish_admin_pricing_refresh(query, context=context))
            return

        if data == "admin_pricing_interval":
            cfg = load_auto_pricing_config()
            cfg["refresh_interval_minutes"] = 30
            save_auto_pricing_config(cfg)
            await query.edit_message_text(
                pricing_panel_text(cfg, "✅ چرخهٔ به‌روزرسانی طبق تنظیم ثابت، هر ۳۰ دقیقه است."),
                reply_markup=pricing_panel_keyboard(cfg),
            )
            return

        if data in {"admin_pricing_margin", "admin_pricing_fee"}:
            state_map = {
                "admin_pricing_margin": ("margin_percent", "درصد سود را بین ۰ تا ۵۰۰ وارد کن؛ نمونه: ۴۰"),
                "admin_pricing_fee": ("fee_percent", "درصد کارمزد/هزینهٔ اضافی را بین ۰ تا ۱۰۰ وارد کن؛ نمونه: ۲ یا ۰"),
            }
            field, prompt = state_map[data]
            context.user_data["pricing_admin_state"] = {"field": field}
            await query.edit_message_text(
                "⚙️ تنظیم قیمت‌گذاری خودکار\n\n" + prompt,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="admin_auto_pricing", style="danger")]]),
            )
            return

        if data == "admin_pricing_clear_key":
            cfg = load_auto_pricing_config()
            cfg["provider_api_key"] = ""
            cfg["catalog_valid"] = False
            cfg["catalog_error"] = "کلید API حذف شده است."
            save_auto_pricing_config(cfg)
            await query.edit_message_text(pricing_panel_text(cfg, "کلید حذف شد؛ قیمت زندهٔ استارز/پرمیوم تا ثبت کلید فعال نمی‌شود."), reply_markup=pricing_panel_keyboard(cfg))
            return

        if data == "admin_pricing_toggle":
            cfg = load_auto_pricing_config()
            cfg["enabled"] = not bool(cfg.get("enabled"))
            save_auto_pricing_config(cfg)
            if cfg["enabled"]:
                if context.user_data.get("_pricing_refresh_pending"):
                    await query.answer("درخواست بررسی نرخ‌ها از قبل در حال انجام است.")
                    return
                context.user_data["_pricing_refresh_pending"] = True
                await safe_callback_edit_or_reply(
                    query, pricing_panel_text(cfg, "🔄 قیمت‌گذاری فعال شد؛ اعتبارسنجی نرخ‌ها در پس‌زمینه انجام می‌شود."),
                    pricing_panel_keyboard(cfg),
                )
                context.application.create_task(_finish_admin_pricing_refresh(query, "قیمت‌گذاری خودکار فعال شد.", context))
            else:
                # Pause the FX/catalog calculation only. Never delete or blank any product price.
                await query.edit_message_text(
                    pricing_panel_text(cfg, "محاسبه و به‌روزرسانی نرخ متوقف شد؛ تمام قیمت‌های ثبت‌شدهٔ دستی و قیمت‌های ذخیره‌شدهٔ محصولات بدون تغییر باقی می‌مانند."),
                    reply_markup=pricing_panel_keyboard(cfg),
                )
            return

        if data == "admin_menu_settings":
            await query.edit_message_text(
                menu_settings_text(),
                reply_markup=menu_settings_keyboard(),
            )
            return

        if data in ("admin_menu_set_keyboard", "admin_menu_set_inline"):
            selected_mode = MENU_TYPE_KEYBOARD if data == "admin_menu_set_keyboard" else MENU_TYPE_INLINE
            set_main_menu_type(selected_mode)
            selected_label = "کیبورد معمولی" if selected_mode == MENU_TYPE_KEYBOARD else "منوی شیشه‌ای"
            await query.edit_message_text(
                menu_settings_text(f"✅ حالت {selected_label} فعال شد."),
                reply_markup=menu_settings_keyboard(),
            )
            return

        if data == "admin_coupons":
            # Coupon management home: render every saved coupon from the live database.
            with db() as con:
                ensure_discount_schema(con)
                rows = con.execute("""
                    SELECT discount_codes.code, discount_amount, discount_type, category, product_name, active,
                           expires_at, expiry_label, expires_jalali_date, expires_local_time, max_uses, per_user_limit, total_uses, recipient_type,
                           min_order_amount,
                           MAX(COALESCE(total_uses, 0), (SELECT COUNT(*) FROM coupon_usage u WHERE u.code=discount_codes.code AND u.status IN ('used','pending'))) AS actual_total_uses
                    FROM discount_codes ORDER BY created_at DESC, code
                """).fetchall()
            lines = ["🏷️ کدهای تخفیف", ""]
            buttons = []
            for r in rows:
                code = str(r["code"])
                expired = not expiry_is_valid(r["expires_at"])
                state_label = "⌛ منقضی‌شده" if expired else ("🟢 فعال" if r["active"] else "🔴 غیرفعال")
                amount_text = f"{int(r['discount_amount'])}%" if r["discount_type"] == "percent" else f"{int(r['discount_amount']):,} تومان"
                total = "نامحدود" if r["max_uses"] is None or int(r["max_uses"] or 0) <= 0 else str(int(r["max_uses"]))
                used = int(r["actual_total_uses"] or 0)
                lines.append(
                    f"⌛ {code} — {amount_text}\n"
                    f"استفاده: {used}/{total} | انقضا: {coupon_expiry_display(r)}\n"
                )
                buttons.append([
                    InlineKeyboardButton(f"✏️ {code}", callback_data="admin_coupon_view_" + code),
                    InlineKeyboardButton(state_label if expired else ("🔴 غیرفعال" if r["active"] else "🟢 فعال‌سازی"), callback_data="admin_coupon_toggle_" + code),
                    InlineKeyboardButton("🗑️", callback_data="admin_coupon_delete_" + code),
                ])
            if not rows:
                lines.append("هنوز کدی تعریف نشده.")
            buttons.extend([
                [InlineKeyboardButton("➕ افزودن کد تخفیف جدید", callback_data="admin_coupon_create")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="admin")],
            ])
            await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("admin_coupon_view_"):
            code = data[len("admin_coupon_view_"):]
            with db() as con:
                ensure_discount_schema(con)
                detail, markup = coupon_management_detail(con, code)
            if not detail:
                await query.answer("این کد در دیتابیس وجود ندارد.", show_alert=True)
                return
            await query.edit_message_text(detail, reply_markup=markup)
            return

        # The amount editor first asks for the discount kind, as requested.
        if data.startswith("admin_coupon_edit_amount_"):
            code = data[len("admin_coupon_edit_amount_"):]
            with db() as con:
                ensure_discount_schema(con)
                exists = con.execute("SELECT 1 FROM discount_codes WHERE code=?", (code,)).fetchone()
            if not exists:
                await query.answer("کد تخفیف پیدا نشد.", show_alert=True)
                return
            await query.edit_message_text(
                "نوع تخفیف را انتخاب کن:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💯 درصدی (مثل 20٪)", callback_data="admin_coupon_amount_type_percent_" + code)],
                    [InlineKeyboardButton("💵 مبلغ ثابت (تومان)", callback_data="admin_coupon_amount_type_fixed_" + code)],
                    [InlineKeyboardButton("❌ لغو", callback_data="admin_coupon_amount_type_cancel_" + code)],
                ])
            )
            return

        if data.startswith("admin_coupon_amount_type_cancel_"):
            code = data[len("admin_coupon_amount_type_cancel_"):]
            context.user_data.pop("coupon_edit_state", None)
            with db() as con:
                detail, markup = coupon_management_detail(con, code)
            if not detail:
                await query.answer("کد تخفیف پیدا نشد.", show_alert=True)
                return
            await query.edit_message_text(detail, reply_markup=markup)
            return

        if data.startswith("admin_coupon_amount_type_percent_") or data.startswith("admin_coupon_amount_type_fixed_"):
            is_percent = data.startswith("admin_coupon_amount_type_percent_")
            prefix = "admin_coupon_amount_type_percent_" if is_percent else "admin_coupon_amount_type_fixed_"
            code = data[len(prefix):]
            context.user_data["coupon_edit_state"] = {
                "code": code, "field": "amount_type_input", "discount_type": "percent" if is_percent else "fixed"
            }
            prompt = "💯 درصد تخفیف جدید را وارد کن (۱ تا ۱۰۰):" if is_percent else "💵 مبلغ ثابت تخفیف را به تومان وارد کن: مثال: 60000"
            await query.edit_message_text(prompt + "\nبرای لغو /cancel را بفرست.")
            return

        # Expiry date and local clock are separate edit actions.
        if data.startswith("admin_coupon_edit_expiry_"):
            code = data[len("admin_coupon_edit_expiry_"):]
            await query.edit_message_text(
                "📅 تاریخ و ساعت انقضا را جداگانه مدیریت کن:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📅 تغییر تاریخ انقضا", callback_data="admin_coupon_expiry_date_" + code)],
                    [InlineKeyboardButton("🕒 تغییر ساعت انقضا", callback_data="admin_coupon_expiry_time_" + code)],
                    [InlineKeyboardButton("♾️ بدون انقضا (حذف انقضا)", callback_data="admin_coupon_expiry_none_" + code)],
                    [InlineKeyboardButton("❌ لغو", callback_data="admin_coupon_expiry_cancel_" + code)],
                ])
            )
            return

        if data.startswith("admin_coupon_expiry_cancel_"):
            code = data[len("admin_coupon_expiry_cancel_"):]
            context.user_data.pop("coupon_edit_state", None)
            with db() as con:
                detail, markup = coupon_management_detail(con, code)
            if not detail:
                await query.answer("کد تخفیف پیدا نشد.", show_alert=True)
                return
            await query.edit_message_text(detail, reply_markup=markup)
            return

        if data.startswith("admin_coupon_expiry_none_"):
            code = data[len("admin_coupon_expiry_none_"):]
            with db() as con:
                ensure_discount_schema(con)
                cur = con.execute("UPDATE discount_codes SET expires_at=NULL, expiry_label=NULL, expires_jalali_date=NULL, expires_local_time=NULL WHERE code=?", (code,))
                if cur.rowcount != 1:
                    await query.answer("کد تخفیف پیدا نشد.", show_alert=True)
                    return
            context.user_data.pop("coupon_edit_state", None)
            await query.edit_message_text(
                "✅ تاریخ انقضای کد حذف شد و کد بدون محدودیت زمانی فعال است.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + code)]])
            )
            return

        if data.startswith("admin_coupon_expiry_date_") or data.startswith("admin_coupon_expiry_time_"):
            is_date = data.startswith("admin_coupon_expiry_date_")
            prefix = "admin_coupon_expiry_date_" if is_date else "admin_coupon_expiry_time_"
            code = data[len(prefix):]
            context.user_data["coupon_edit_state"] = {"code": code, "field": "expiry_date" if is_date else "expiry_time"}
            prompt = (
                "📅 تاریخ انقضای جدید را به شمسی وارد کن:\nفرمت: YYYY-MM-DD\nمثال: 1405-08-15"
                if is_date else
                "🕒 ساعت انقضای جدید را وارد کن:\nفرمت: HH:MM\nمثال: 20:30\nاگر هنوز تاریخ تعیین نشده باشد، ساعت ذخیره می‌شود و با تعیین تاریخ اعمال خواهد شد."
            )
            await query.edit_message_text(prompt + "\nبرای لغو /cancel را بفرست.")
            return

        # Dynamic, multi-select category editor.
        if data.startswith("admin_coupon_category_toggle_"):
            state = context.user_data.get("coupon_scope_state")
            if not state:
                await query.answer("صفحه دسته‌ها منقضی شده؛ دوباره بازش کن.", show_alert=True)
                return
            try:
                index = int(data[len("admin_coupon_category_toggle_"):])
                item = state["categories"][index]
                category_key = str(item["category_key"])
            except (ValueError, IndexError, KeyError, TypeError):
                await query.answer("دسته انتخاب‌شده معتبر نیست.", show_alert=True)
                return
            code = state["code"]
            with db() as con:
                ensure_discount_schema(con)
                current_row = con.execute("SELECT category FROM discount_codes WHERE code=?", (code,)).fetchone()
                if not current_row:
                    await query.answer("کد تخفیف پیدا نشد.", show_alert=True)
                    return
                selected = coupon_selected_categories(current_row["category"])
                if category_key in selected:
                    selected.remove(category_key)
                else:
                    selected.add(category_key)
                stored = json.dumps(sorted(selected), ensure_ascii=False) if selected else "all"
                con.execute("UPDATE discount_codes SET category=?, product_name=NULL WHERE code=?", (stored, code))
                categories = list_coupon_categories(con)
            state["categories"] = categories
            state["selected"] = selected
            await query.answer("انتخاب دسته ذخیره شد.")
            await query.edit_message_text(
                coupon_scope_text(None, code, categories, selected),
                reply_markup=coupon_scope_keyboard(code, categories, selected)
            )
            return

        if data == "admin_coupon_category_all":
            state = context.user_data.get("coupon_scope_state")
            if not state:
                await query.answer("صفحه دسته‌ها منقضی شده؛ دوباره بازش کن.", show_alert=True)
                return
            code = state["code"]
            with db() as con:
                ensure_discount_schema(con)
                con.execute("UPDATE discount_codes SET category='all', product_name=NULL WHERE code=?", (code,))
                categories = list_coupon_categories(con)
            selected = set()
            state.update(categories=categories, selected=selected)
            await query.answer("کد برای همه محصولات تنظیم شد.")
            await query.edit_message_text(
                coupon_scope_text(None, code, categories, selected),
                reply_markup=coupon_scope_keyboard(code, categories, selected)
            )
            return

        if data == "admin_coupon_category_add":
            state = context.user_data.get("coupon_scope_state")
            if not state:
                await query.answer("صفحه دسته‌ها منقضی شده؛ دوباره بازش کن.", show_alert=True)
                return
            context.user_data["coupon_category_add_state"] = {
                "code": state["code"], "chat_id": query.message.chat_id,
                "message_id": query.message.message_id,
            }
            await query.edit_message_text(
                "➕ نام دسته جدید را بفرست. مثال: 🎮 سرویس جدید\nنام تکراری پذیرفته نمی‌شود.\nبرای لغو /cancel را بفرست."
            )
            return

        if data == "admin_coupon_category_back":
            state = context.user_data.pop("coupon_scope_state", None)
            if not state:
                await query.answer("صفحه دسته‌ها منقضی شده است.", show_alert=True)
                return
            context.user_data.pop("coupon_category_add_state", None)
            with db() as con:
                ensure_discount_schema(con)
                detail, markup = coupon_management_detail(con, state["code"])
            if not detail:
                await query.answer("کد تخفیف پیدا نشد.", show_alert=True)
                return
            await query.edit_message_text(detail, reply_markup=markup)
            return

        if data.startswith("admin_coupon_clear_expiry_"):
            code = data[len("admin_coupon_clear_expiry_"):]
            with db() as con:
                ensure_discount_schema(con)
                cur = con.execute("UPDATE discount_codes SET expires_at=NULL, expiry_label=NULL, expires_jalali_date=NULL, expires_local_time=NULL WHERE code=?", (code,))
            if not cur.rowcount:
                await query.answer("کد پیدا نشد.", show_alert=True)
                return
            await query.answer("انقضا حذف شد.")
            await query.edit_message_text("✅ تاریخ انقضای کد حذف شد و کد بدون محدودیت زمانی فعال است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + code)]]))
            return

        if data.startswith("admin_coupon_reset_"):
            code = data[len("admin_coupon_reset_"):]
            with db() as con:
                ensure_discount_schema(con)
                con.execute("BEGIN IMMEDIATE")
                pending = con.execute("SELECT COUNT(*) FROM coupon_usage WHERE code=? AND status='pending'", (code,)).fetchone()[0]
                if pending:
                    await query.answer("این کد سفارش در انتظار بررسی دارد؛ برای حفظ سقف مصرف، ابتدا سفارش‌های معلق را تعیین تکلیف کن.", show_alert=True)
                    return
                con.execute("UPDATE discount_codes SET total_uses=0, used_by=NULL, used_order_id=NULL, used_at=NULL WHERE code=?", (code,))
                con.execute("UPDATE coupon_usage SET status='reset' WHERE code=? AND status IN ('used','reset')", (code,))
            await query.answer("تعداد استفاده ریست شد.")
            await query.edit_message_text("✅ تعداد استفاده ریست شد", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + code)]]))
            return

        if data.startswith("admin_coupon_edit_"):
            parts = data.split("_", 4)
            if len(parts) < 5:
                await query.answer("درخواست ویرایش نامعتبر است.", show_alert=True)
                return
            field, code = parts[3], parts[4]
            if field == "scope":
                with db() as con:
                    ensure_discount_schema(con)
                    categories = list_coupon_categories(con)
                    row = con.execute("SELECT category FROM discount_codes WHERE code=?", (code,)).fetchone()
                if not row:
                    await query.answer("کد تخفیف پیدا نشد.", show_alert=True)
                    return
                selected = coupon_selected_categories(row["category"])
                context.user_data["coupon_scope_state"] = {
                    "code": code, "categories": categories, "selected": selected,
                    "chat_id": query.message.chat_id, "message_id": query.message.message_id,
                }
                await query.edit_message_text(
                    coupon_scope_text(None, code, categories, selected),
                    reply_markup=coupon_scope_keyboard(code, categories, selected)
                )
                return
            if field == "expiry":
                await query.edit_message_text(
                    "📅 تاریخ و ساعت انقضا را جداگانه مدیریت کن:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📅 تغییر تاریخ انقضا", callback_data="admin_coupon_expiry_date_" + code)],
                        [InlineKeyboardButton("🕒 تغییر ساعت انقضا", callback_data="admin_coupon_expiry_time_" + code)],
                        [InlineKeyboardButton("♾️ بدون انقضا (حذف انقضا)", callback_data="admin_coupon_expiry_none_" + code)],
                        [InlineKeyboardButton("❌ لغو", callback_data="admin_coupon_expiry_cancel_" + code)],
                    ])
                )
                return
            prompts = {
                "code": "✏️ کد جدید را وارد کن (حروف انگلیسی و عدد):\nمثال: SEYED18",
                "userlimit": "🔢 حداکثر تعداد استفاده را وارد کن (۰ = نامحدود):",
                "recipient": "👤 اختصاص کد به یک کاربر\n\nآی‌دیِ عددیِ تلگرامِ کاربر را بفرست؛ از آن پس فقط همان کاربر می‌تواند این کد را استفاده کند.\n\nبرای برداشتنِ محدودیت عدد 0 را بفرست.",
                "min": "🧾 حداقلِ مبلغ خرید\n\nمبلغ را به تومان بفرست؛ کد فقط روی سفارش‌هایی با مبلغِ برابر یا بیشتر از این اعمال می‌شود.\n\nبرای برداشتنِ محدودیت عدد 0 را بفرست.",
            }
            if field not in prompts:
                await query.answer("این گزینه پشتیبانی نمی‌شود.", show_alert=True)
                return
            context.user_data["coupon_edit_state"] = {"code": code, "field": field}
            await query.edit_message_text(prompts[field] + "\nبرای لغو /cancel بفرست.")
            return

        if data.startswith("admin_coupon_setscope_all_"):
            code = data[len("admin_coupon_setscope_all_"):]
            with db() as con:
                ensure_discount_schema(con)
                con.execute("UPDATE discount_codes SET category='all', product_name=NULL WHERE code=?", (code,))
            await query.answer("محدوده روی همه خدمات تنظیم شد.")
            await query.edit_message_text("🌐 محدوده کد روی همه خدمات و محصولات تنظیم شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + code)]]))
            return

        if data.startswith("admin_coupon_setscope_"):
            rest = data[len("admin_coupon_setscope_"):]
            try:
                idx_text, code = rest.split("_", 1)
                idx = int(idx_text)
                categories = context.user_data.get("coupon_edit_categories", {}).get(code, [])
                category = categories[idx]
            except (ValueError, IndexError, TypeError):
                await query.answer("دسته انتخاب‌شده معتبر نیست؛ صفحه را دوباره باز کن.", show_alert=True)
                return
            with db() as con:
                ensure_discount_schema(con)
                con.execute("UPDATE discount_codes SET category=?, product_name=NULL WHERE code=?", (category, code))
            await query.answer("محدوده کد تغییر کرد.")
            await query.edit_message_text(f"📦 محدودهٔ کد تغییر کرد: {coupon_category_label(category)}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + code)]]))
            return

        if data.startswith("admin_coupon_toggle_"):
            code = data[len("admin_coupon_toggle_"):]
            with db() as con:
                ensure_discount_schema(con)
                row = con.execute(
                    "SELECT active, expires_at FROM discount_codes WHERE code=?", (code,)
                ).fetchone()
                if not row:
                    await query.answer("این کد وجود ندارد یا حذف شده است.", show_alert=True)
                    return
                if not expiry_is_valid(row["expires_at"]):
                    await query.answer("این کد منقضی شده است؛ ابتدا تاریخ انقضا را ویرایش کن.", show_alert=True)
                    return
                new_active = 0 if int(row["active"] or 0) else 1
                con.execute(
                    "UPDATE discount_codes SET active=? WHERE code=?",
                    (new_active, code),
                )
            await query.answer("کد تخفیف فعال شد." if new_active else "کد تخفیف غیرفعال شد.")
            # Re-render the same management list layout after the state change.
            with db() as con:
                rows = con.execute("""
                    SELECT discount_codes.code, discount_amount, discount_type, category, active,
                           expires_at, expiry_label, expires_jalali_date, expires_local_time, max_uses, total_uses,
                           MAX(COALESCE(total_uses, 0), (SELECT COUNT(*) FROM coupon_usage u WHERE u.code=discount_codes.code AND u.status IN ('used','pending'))) AS actual_total_uses
                    FROM discount_codes ORDER BY created_at DESC, code
                """).fetchall()
            lines = ["🏷️ کدهای تخفیف", ""]
            buttons = []
            for r in rows:
                code2 = str(r["code"])
                expired = not expiry_is_valid(r["expires_at"])
                state_text = "⌛ منقضی‌شده" if expired else ("🟢 فعال" if r["active"] else "🔴 غیرفعال")
                amount_text = f"{int(r['discount_amount'])}%" if r["discount_type"] == "percent" else f"{int(r['discount_amount']):,} تومان"
                uses = "نامحدود" if r["max_uses"] is None or int(r["max_uses"] or 0) <= 0 else str(int(r["max_uses"]))
                lines.append(f"⌛ {code2} — {amount_text}\nاستفاده: {int(r['actual_total_uses'] or 0)}/{uses} | انقضا: {coupon_expiry_display(r)}\n")
                buttons.append([
                    InlineKeyboardButton(f"✏️ {code2}", callback_data="admin_coupon_view_" + code2),
                    InlineKeyboardButton(state_text if expired else ("🔴 غیرفعال" if r["active"] else "🟢 فعال‌سازی"), callback_data="admin_coupon_toggle_" + code2),
                    InlineKeyboardButton("🗑️", callback_data="admin_coupon_delete_" + code2),
                ])
            if not rows:
                lines.append("هنوز کدی تعریف نشده.")
            buttons.extend([
                [InlineKeyboardButton("➕ افزودن کد تخفیف جدید", callback_data="admin_coupon_create")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="admin")],
            ])
            await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("admin_coupon_delete_"):
            code = data[len("admin_coupon_delete_"):]
            with db() as con:
                ensure_discount_schema(con)
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT code FROM discount_codes WHERE code=?", (code,)
                ).fetchone()
                if not row:
                    await query.answer("این کد قبلاً حذف شده یا وجود ندارد.", show_alert=True)
                    return
                pending = con.execute("SELECT COUNT(*) FROM coupon_usage WHERE code=? AND status='pending'", (code,)).fetchone()[0]
                if pending:
                    await query.answer("این کد سفارش در انتظار بررسی دارد؛ ابتدا سفارش‌های معلق را تأیید یا رد کن.", show_alert=True)
                    return
                # Keep historical order and usage records, while excluding them from future usage counts.
                con.execute("UPDATE coupon_usage SET status='archived' WHERE code=?", (code,))
                con.execute("DELETE FROM discount_codes WHERE code=?", (code,))

            await query.answer("✅️کد با موفقیت حذف شد.")
            with db() as con:
                rows = con.execute("""
                    SELECT discount_codes.code, discount_amount, discount_type, category, active,
                           expires_at, expiry_label, expires_jalali_date, expires_local_time, max_uses, total_uses,
                           MAX(COALESCE(total_uses, 0), (SELECT COUNT(*) FROM coupon_usage u WHERE u.code=discount_codes.code AND u.status IN ('used','pending'))) AS actual_total_uses
                    FROM discount_codes ORDER BY created_at DESC, code
                """).fetchall()
            lines = ["🏷️ کدهای تخفیف", "", "✅️کد با موفقیت حذف شد.", ""]
            buttons = []
            for r in rows:
                code2 = str(r["code"])
                expired = not expiry_is_valid(r["expires_at"])
                state_text = "⌛ منقضی‌شده" if expired else ("🟢 فعال" if r["active"] else "🔴 غیرفعال")
                amount_text = f"{int(r['discount_amount'])}%" if r["discount_type"] == "percent" else f"{int(r['discount_amount']):,} تومان"
                uses = "نامحدود" if r["max_uses"] is None or int(r["max_uses"] or 0) <= 0 else str(int(r["max_uses"]))
                lines.append(f"⌛ {code2} — {amount_text}\nاستفاده: {int(r['actual_total_uses'] or 0)}/{uses} | انقضا: {coupon_expiry_display(r)}\n")
                buttons.append([
                    InlineKeyboardButton(f"✏️ {code2}", callback_data="admin_coupon_view_" + code2),
                    InlineKeyboardButton(state_text if expired else ("🔴 غیرفعال" if r["active"] else "🟢 فعال‌سازی"), callback_data="admin_coupon_toggle_" + code2),
                    InlineKeyboardButton("🗑️", callback_data="admin_coupon_delete_" + code2),
                ])
            if not rows:
                lines.append("هنوز کدی تعریف نشده.")
            buttons.extend([
                [InlineKeyboardButton("➕ افزودن کد تخفیف جدید", callback_data="admin_coupon_create")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="admin")],
            ])
            await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data == "admin_coupon_create":
            context.user_data.pop("coupon_edit_state", None)
            context.user_data.pop("coupon_scope_state", None)
            context.user_data.pop("coupon_category_add_state", None)
            context.user_data["admin_coupon_state"] = {"step": "code"}
            await query.edit_message_text(
                "➕ افزودن کد تخفیف\n\n"
                "کد تخفیف را وارد کن (حروف انگلیسی/عدد، بدون فاصله):\n"
                "مثال: `SEYED18`\n\n"
                "برای لغو، دکمهٔ زیر یا /cancel را بزن.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="admin_coupon_creation_cancel")]])
            )
            return

        if data == "admin_coupon_creation_cancel":
            context.user_data.pop("admin_coupon_state", None)
            await query.answer("ساخت کد لغو شد.")
            await query.edit_message_text("❌ ساخت کد تخفیف لغو شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏷️ مدیریت تخفیف‌ها", callback_data="admin_coupons")]]))
            return

        if data in ("admin_coupon_type_percent", "admin_coupon_type_fixed"):
            state = context.user_data.get("admin_coupon_state")
            if not state or state.get("step") != "discount_type":
                await query.answer("مرحلهٔ ساخت کوپن پیدا نشد؛ دوباره شروع کن.", show_alert=True)
                return
            state["discount_type"] = "percent" if data.endswith("percent") else "fixed"
            state["step"] = "amount"
            if state["discount_type"] == "percent":
                prompt = "💯 درصد تخفیف را وارد کن (عدد 1 تا 100). مثال: 20"
            else:
                prompt = "💵 مبلغ ثابت تخفیف را به تومان وارد کن. مثال: 50000"
            await query.edit_message_text(prompt + "\nبرای لغو /cancel را بفرست.")
            return

        if data == "admin_coupon_scope_all":
            state = context.user_data.get("admin_coupon_state")
            if not state or state.get("step") != "scope_choice":
                await query.answer("مرحله ساخت کوپن پیدا نشد؛ دوباره شروع کن.", show_alert=True)
                return
            state.update(category="all", product_name=None, step="recipients")
            await query.edit_message_text("🎯 محدوده: همه خدمات و محصولات فعلی و آینده\n\n👥 برای همه کاربران، ALL بفرست؛ یا آیدی عددی/@username را وارد کن. چند نفر را با کاما جدا کن.\nبرای لغو /cancel")
            return

        if data.startswith("admin_coupon_scope_cat_"):
            state = context.user_data.get("admin_coupon_state")
            if not state or state.get("step") != "scope_choice":
                await query.answer("مرحله ساخت کوپن پیدا نشد؛ دوباره شروع کن.", show_alert=True)
                return
            try:
                idx = int(data.rsplit("_", 1)[1])
                category = state.get("scope_categories", [])[idx]
            except (ValueError, IndexError, TypeError):
                await query.answer("دسته‌بندی انتخاب‌شده معتبر نیست.", show_alert=True)
                return
            with db() as con:
                products = con.execute("SELECT id, name FROM products WHERE category=? AND active=1 ORDER BY name", (category,)).fetchall()
            state.update(category=category, product_name=None, step="scope_product_choice")
            state["scope_products"] = [{"id": int(r["id"]), "name": str(r["name"])} for r in products]
            label = coupon_category_label(category)
            buttons = [[InlineKeyboardButton("📂 تمام محصولات این دسته", callback_data="admin_coupon_scope_category_all")]]
            for idx, product in enumerate(state["scope_products"]):
                # Use index-based callback data to respect Telegram's callback length limit.
                buttons.append([InlineKeyboardButton(product["name"][:60], callback_data=f"admin_coupon_scope_product_{idx}")])
            buttons.append([InlineKeyboardButton("↩️ انتخاب دسته دیگر", callback_data="admin_coupon_scope_back")])
            await query.edit_message_text(f"🎯 دسته انتخاب‌شده: {label}\nمی‌توانی کل دسته یا فقط یک محصول مشخص را انتخاب کنی:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data == "admin_coupon_scope_back":
            state = context.user_data.get("admin_coupon_state")
            if not state:
                await query.answer("فرایند ساخت کوپن فعال نیست.", show_alert=True)
                return
            with db() as con:
                rows = con.execute("SELECT category, COUNT(*) AS n FROM products WHERE active=1 GROUP BY category ORDER BY category").fetchall()
            categories = [(str(r["category"]), int(r["n"] or 0)) for r in rows if str(r["category"] or "").strip()]
            state["scope_categories"] = [c for c, _ in categories]
            state["step"] = "scope_choice"
            buttons = [[InlineKeyboardButton("🌐 همه خدمات", callback_data="admin_coupon_scope_all")]]
            for idx, (category, count) in enumerate(categories):
                label = coupon_category_label(category)
                buttons.append([InlineKeyboardButton(f"{label} ({count} محصول)", callback_data=f"admin_coupon_scope_cat_{idx}")])
            buttons.append([InlineKeyboardButton("❌ لغو ساخت", callback_data="admin_coupon_creation_cancel")])
            await query.edit_message_text("🎯 دسته‌بندی را انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data == "admin_coupon_scope_category_all" or data.startswith("admin_coupon_scope_product_"):
            state = context.user_data.get("admin_coupon_state")
            if not state or state.get("step") != "scope_product_choice":
                await query.answer("مرحله انتخاب محصول معتبر نیست.", show_alert=True)
                return
            if data == "admin_coupon_scope_category_all":
                state["product_name"] = None
            else:
                try:
                    idx = int(data.rsplit("_", 1)[1])
                    state["product_name"] = state["scope_products"][idx]["name"]
                except (ValueError, IndexError, TypeError):
                    await query.answer("محصول انتخاب‌شده معتبر نیست.", show_alert=True)
                    return
            state["step"] = "recipients"
            scope_label = coupon_category_label(state["category"])
            product_label = state.get("product_name")
            await query.edit_message_text(
                f"✅ محدوده: {scope_label}" + (f"\n📦 محصول دقیق: {product_label}" if product_label else "\n📦 تمام محصولات این دسته") +
                "\n\n👥 برای همه کاربران، ALL بفرست؛ یا آیدی عددی/@username را وارد کن. چند نفر را با کاما جدا کن.\nبرای لغو /cancel"
            )
            return

        if data in ("admin_coupon_expiry_hours", "admin_coupon_expiry_date", "admin_coupon_expiry_never"):
            state = context.user_data.get("admin_coupon_state")
            if not state or state.get("step") != "expiry_choice":
                await query.answer("این مرحله منقضی شده؛ ساخت کد را دوباره شروع کن.", show_alert=True)
                return
            if data == "admin_coupon_expiry_never":
                state["expires_at"] = None
                state.pop("expiry_duration_seconds", None)
                state["expiry_label"] = None
                try:
                    record = save_coupon_from_admin_state(state, query.from_user.id)
                except sqlite3.IntegrityError:
                    logger.exception("Coupon insert conflict for no-expiry coupon")
                    context.user_data.pop("admin_coupon_state", None)
                    await query.edit_message_text("❌ این کد قبلاً ثبت شده است. دوباره یک کد جدید بساز.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏷️ بازگشت به لیست کدها", callback_data="admin_coupons")]]))
                    return
                except Exception:
                    logger.exception("Coupon save failed for no-expiry coupon")
                    await query.answer("ذخیره انجام نشد؛ جزئیات در لاگ ثبت شد.", show_alert=True)
                    return
                context.user_data.pop("admin_coupon_state", None)
                await query.edit_message_text(
                    coupon_created_message(record),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 بازگشت به لیست کدها", callback_data="admin_coupons")]])
                )
            elif data == "admin_coupon_expiry_hours":
                state["step"] = "expiry_hours"
                await query.edit_message_text(
                    "⏱️ تعداد ساعت اعتبار را وارد کن:\n"
                    "مثال: 6 یعنی کد دقیقاً ۶ ساعت پس از ذخیره منقضی شود.\n"
                    "از 1 تا 876000 ساعت. برای لغو /cancel را بفرست."
                )
            elif data == "admin_coupon_expiry_date":
                state["step"] = "expiry_date"
                await query.edit_message_text(
                    "📅 تاریخ انقضا را به شمسی وارد کن\n"
                    "فرمت: YYYY-MM-DD\n"
                    "مثال: 1405-08-07\n"
                    "کد تا پایان همان روز به وقت تهران معتبر خواهد بود.\n"
                    "برای لغو /cancel را بفرست."
                )
            return

        if data == "admin_coupon_toggle":
            context.user_data["admin_coupon_state"] = {"step": "toggle"}
            await query.edit_message_text(
                "کد تخفیفی را که می‌خواهی فعال یا غیرفعال شود بفرست.\nبرای لغو /cancel بفرست."
            )
            return

        if data == "admin_stats":
            with db() as con:
                users = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
                orders = con.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
                total = con.execute("SELECT COALESCE(SUM(price),0) AS n FROM orders").fetchone()["n"]
            text = (
                "📊 آمار فروشگاه\n\n"
                f"👥 کاربران: {users}\n"
                f"📦 تعداد سفارش‌ها: {orders}\n"
                f"💰 مجموع مبلغ سفارش‌های ثبت‌شده: {total:,} تومان\n\n"
                "این مبلغ فروش قطعی نیست؛ پرداخت‌ها در این نسخه تأیید نمی‌شوند."
            )
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ بازگشت به مدیریت", callback_data="admin")]])
            )
            return

        if data.startswith("admin_auto_product|"):
            try:
                _, pid_text, return_token, return_page = data.split("|", 3)
                pid = int(pid_text)
                context.user_data["auto_pricing_return"] = f"admin_auto_cat|{return_token}|{max(1, int(return_page))}"
            except (ValueError, TypeError):
                await query.answer("درخواست محصول معتبر نیست.", show_alert=True)
                return
            data = f"admin_p_edit_{pid}"

        if data == "admin_products" or data.startswith("admin_products_page_") or data.startswith("admin_p_"):
            if user_id != ADMIN_ID:
                await query.answer("دسترسی مجاز نیست.", show_alert=True)
                return

            # صفحه‌بندی قبل از پردازش سایر callbackها تشخیص داده می‌شود.
            if data == "admin_products":
                context.user_data.pop("auto_pricing_return", None)
            products_page = 1
            if data.startswith("admin_products_page_"):
                try:
                    products_page = max(1, int(data.rsplit("_", 1)[1]))
                except (ValueError, IndexError):
                    products_page = 1
                data = "admin_products"
            products_per_page = 8

            categories = {
                "stars": "⭐ استارز",
                "premium": "💎 پرمیوم",
                "boost": "🚀 بوست",
                "gifts": "🎁 گیفت",
                "numbers": "📱 شماره مجازی",
                "configs": "🛒 خرید کانفیگ",
                "ton": "💎 خرید تون",
                "members": "👥 ممبر تلگرام",
                "reactions": "💙 ری‌اکشن تلگرام",
                "views": "👀 ویو تلگرام",
                "instagram_followers": "📸 فالوور اینستاگرام",
                "instagram_likes": "💙 لایک اینستاگرام",
                "instagram_views": "🎬 ویو اینستاگرام",
            }

            # دسته‌های موجود در محصولات و فهرست دسته‌های مدیریتی نیز به‌صورت پویا اضافه می‌شوند.
            with db() as con:
                ensure_discount_schema(con)
                existing_categories = [r[0] for r in con.execute(
                    "SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category != '' ORDER BY category"
                ).fetchall()]
                saved_categories = con.execute(
                    "SELECT category_key, display_name FROM coupon_categories WHERE active=1 ORDER BY sort_order, display_name"
                ).fetchall()
            for existing_category in existing_categories:
                if str(existing_category).strip().casefold() not in {"ton", "tons"}:
                    categories.setdefault(existing_category, existing_category)
            for saved_category in saved_categories:
                if str(saved_category["category_key"]).strip().casefold() not in {"ton", "tons"}:
                    categories[saved_category["category_key"]] = saved_category["display_name"]

            if data == "admin_p_add":
                keyboard = [
                    [InlineKeyboardButton(label, callback_data=f"admin_p_addcat_{key}")]
                    for key, label in categories.items()
                ]
                keyboard.append([InlineKeyboardButton("↩️ بازگشت", callback_data="admin_products")])
                await query.edit_message_text("➕ دسته‌بندی محصول جدید را انتخاب کن:", reply_markup=InlineKeyboardMarkup(keyboard))
                return

            if data.startswith("admin_p_addcat_"):
                category = data[len("admin_p_addcat_"):]
                if category not in categories:
                    await query.answer("دسته‌بندی نامعتبر است.", show_alert=True)
                    return
                context.user_data["product_admin_state"] = {"action": "add_name", "category": category}
                await query.edit_message_text(
                    f"دسته: {categories[category]}\n\nنام محصول جدید را در یک پیام بفرست.\nبرای لغو دستور /cancel را بزن."
                )
                return

            if data.startswith("admin_p_toggle_"):
                pid = int(data.rsplit("_", 1)[1])
                with db() as con:
                    row = con.execute("SELECT active FROM products WHERE id=?", (pid,)).fetchone()
                    if not row:
                        await query.answer("محصول پیدا نشد.", show_alert=True)
                        return
                    active = 0 if int(row["active"]) else 1
                    con.execute("UPDATE products SET active=? WHERE id=?", (active, pid))
                await query.answer("محصول فعال شد." if active else "محصول غیرفعال شد.", show_alert=True)

            elif data.startswith("admin_p_edit_"):
                pid = int(data.rsplit("_", 1)[1])
                with db() as con:
                    row = con.execute(
                        "SELECT id, name, price, category, button_text, icon, button_style, sort_order, "
                        "description, star_amount, currency, old_price, image_file_id FROM products WHERE id=?",
                        (pid,),
                    ).fetchone()
                if not row:
                    await query.answer("محصول پیدا نشد.", show_alert=True)
                    return
                product_back_callback = context.user_data.get("auto_pricing_return") or "admin_products"
                product_back_label = "↩️ بازگشت به گروه قیمت‌گذاری" if context.user_data.get("auto_pricing_return") else "↩️ بازگشت به محصولات"
                keyboard = [
                    [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"admin_p_name_{pid}"),
                     InlineKeyboardButton("💰 ویرایش قیمت", callback_data=f"admin_p_price_{pid}")],
                    [InlineKeyboardButton("🏷️ متن دکمه", callback_data=f"admin_p_button_text_{pid}"),
                     InlineKeyboardButton("✧ آیکون", callback_data=f"admin_p_icon_{pid}")],
                    [InlineKeyboardButton("🎨 رنگ دکمه", callback_data=f"admin_p_style_{pid}"),
                     InlineKeyboardButton("↕️ ترتیب نمایش", callback_data=f"admin_p_sort_order_{pid}")],
                    [InlineKeyboardButton("📝 توضیحات محصول", callback_data=f"admin_p_description_{pid}"),
                     InlineKeyboardButton("🖼️ تصویر محصول", callback_data=f"admin_p_image_{pid}")],
                    [InlineKeyboardButton("💱 واحد پول", callback_data=f"admin_p_currency_{pid}"),
                     InlineKeyboardButton("🏷️ قیمت قبلی", callback_data=f"admin_p_old_price_{pid}")],
                    [InlineKeyboardButton("📂 ویرایش دسته‌بندی", callback_data=f"admin_p_cat_{pid}")],
                    [InlineKeyboardButton("⭐ مقدار بستهٔ استارز", callback_data=f"admin_p_star_amount_{pid}")],
                    [InlineKeyboardButton(product_back_label, callback_data=product_back_callback)],
                ]
                amount_text = str(row["star_amount"] or "تنظیم نشده")
                text = (
                    f"🛠 ویرایش محصول #{pid}\n\nنام: {row['name']}\n"
                    f"متن دکمه: {row['button_text'] or 'همان نام محصول'}\n"
                    f"آیکون: {row['icon'] or 'ندارد'} | رنگ: {row['button_style']}\n"
                    f"قیمت: {int(row['price'] or 0):,} {row['currency'] or 'تومان'}\n"
                    f"قیمت قبلی: {int(row['old_price'] or 0):,}\nدسته: {row['category']}\n"
                    f"ترتیب: {int(row['sort_order'] or 0)} | مقدار استارز: {amount_text}\n"
                    f"تصویر: {'ثبت‌شده' if row['image_file_id'] else 'ندارد'}\n"
                    f"توضیحات: {str(row['description'] or '')[:300] or 'ثبت نشده'}"
                )
                await query.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup(keyboard))
                return

            elif data.startswith("admin_p_style_set_"):
                payload = data[len("admin_p_style_set_"):]
                try:
                    pid_text, style = payload.split("_", 1)
                    pid = int(pid_text)
                except (ValueError, IndexError):
                    await query.answer("درخواست نامعتبر است.", show_alert=True)
                    return
                if style not in {"primary", "success", "danger"}:
                    await query.answer("رنگ نامعتبر است.", show_alert=True)
                    return
                with db() as con:
                    cur = con.execute("UPDATE products SET button_style=? WHERE id=?", (style, pid))
                if cur.rowcount != 1:
                    await query.answer("محصول پیدا نشد.", show_alert=True)
                    return
                await query.answer("رنگ دکمه ذخیره شد.", show_alert=True)
                data = f"admin_p_edit_{pid}"
                # Reuse the regular edit screen below by rebuilding it directly.
                with db() as con:
                    row = con.execute("SELECT id, name, price, category, button_text, icon, button_style, sort_order, description, star_amount, currency, old_price, image_file_id FROM products WHERE id=?", (pid,)).fetchone()
                product_back_callback = context.user_data.get("auto_pricing_return") or "admin_products"
                product_back_label = "↩️ بازگشت به گروه قیمت‌گذاری" if context.user_data.get("auto_pricing_return") else "↩️ بازگشت به محصولات"
                keyboard = [
                    [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"admin_p_name_{pid}"), InlineKeyboardButton("💰 ویرایش قیمت", callback_data=f"admin_p_price_{pid}")],
                    [InlineKeyboardButton("🏷️ متن دکمه", callback_data=f"admin_p_button_text_{pid}"), InlineKeyboardButton("✧ آیکون", callback_data=f"admin_p_icon_{pid}")],
                    [InlineKeyboardButton("🎨 رنگ دکمه", callback_data=f"admin_p_style_{pid}"), InlineKeyboardButton("↕️ ترتیب نمایش", callback_data=f"admin_p_sort_order_{pid}")],
                    [InlineKeyboardButton("📝 توضیحات محصول", callback_data=f"admin_p_description_{pid}"), InlineKeyboardButton("🖼️ تصویر محصول", callback_data=f"admin_p_image_{pid}")],
                    [InlineKeyboardButton("💱 واحد پول", callback_data=f"admin_p_currency_{pid}"), InlineKeyboardButton("🏷️ قیمت قبلی", callback_data=f"admin_p_old_price_{pid}")],
                    [InlineKeyboardButton("📂 ویرایش دسته‌بندی", callback_data=f"admin_p_cat_{pid}"), InlineKeyboardButton("⭐ مقدار بستهٔ استارز", callback_data=f"admin_p_star_amount_{pid}")],
                    [InlineKeyboardButton(product_back_label, callback_data=product_back_callback)],
                ]
                await query.edit_message_text(
                    f"🛠 ویرایش محصول #{pid}\n\nنام: {row['name']}\nمتن دکمه: {row['button_text'] or 'همان نام محصول'}\n"
                    f"آیکون: {row['icon'] or 'ندارد'} | رنگ: {row['button_style']}\n"
                    f"قیمت: {int(row['price'] or 0):,} {row['currency'] or 'تومان'}\nدسته: {row['category']}\n"
                    f"ترتیب: {int(row['sort_order'] or 0)} | مقدار استارز: {row['star_amount'] or 'تنظیم نشده'}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return

            elif data.startswith("admin_p_style_"):
                try:
                    pid = int(data.rsplit("_", 1)[1])
                except ValueError:
                    await query.answer("شناسه محصول نامعتبر است.", show_alert=True)
                    return
                await query.edit_message_text(
                    "رنگ موردنظر دکمه را انتخاب کن:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🟢 سبز", callback_data=f"admin_p_style_set_{pid}_success", style="success")],
                        [InlineKeyboardButton("🔵 آبی", callback_data=f"admin_p_style_set_{pid}_primary", style="primary")],
                        [InlineKeyboardButton("🔴 قرمز", callback_data=f"admin_p_style_set_{pid}_danger", style="danger")],
                        [InlineKeyboardButton("↩️ بازگشت", callback_data=f"admin_p_edit_{pid}")],
                    ])
                )
                return

            elif data.startswith(("admin_p_button_text_", "admin_p_icon_", "admin_p_sort_order_", "admin_p_description_", "admin_p_star_amount_", "admin_p_currency_", "admin_p_old_price_", "admin_p_image_")):
                routes = (
                    ("admin_p_button_text_", "edit_button_text", "متن جدید دکمه را بفرست (حداکثر ۶۴ نویسه؛ برای استفاده از نام محصول، کلمهٔ پاک را بفرست):"),
                    ("admin_p_icon_", "edit_icon", "نماد متنی جدید را بفرست؛ برای حذف آیکون، کلمهٔ پاک را بفرست:"),
                    ("admin_p_sort_order_", "edit_sort_order", "عدد ترتیب نمایش را بفرست؛ عدد کمتر زودتر نمایش داده می‌شود:"),
                    ("admin_p_description_", "edit_description", "توضیحات محصول را بفرست (حداکثر ۱۲۰۰ نویسه؛ برای پاک‌کردن، کلمهٔ پاک):"),
                    ("admin_p_star_amount_", "edit_star_amount", "مقدار بستهٔ استارز را به عدد بفرست؛ برای حذف این بسته از منوی استارز عدد ۰ را بفرست:"),
                    ("admin_p_currency_", "edit_currency", "واحد پول را بفرست؛ مثال: تومان یا ریال:"),
                    ("admin_p_old_price_", "edit_old_price", "قیمت قبلی را به عدد بفرست؛ برای نداشتن قیمت قبلی ۰ را بفرست:"),
                    ("admin_p_image_", "edit_image", "حالا تصویر محصول را به‌صورت عکس در تلگرام بفرست. برای لغو /cancel را بزن."),
                )
                for prefix, action, prompt in routes:
                    if data.startswith(prefix):
                        pid = int(data[len(prefix):])
                        with db() as con:
                            product = con.execute("SELECT id, category FROM products WHERE id=?", (pid,)).fetchone()
                        if not product:
                            await query.answer("محصول پیدا نشد.", show_alert=True)
                            return
                        context.user_data["product_admin_state"] = {"action": action, "product_id": pid, "category": product["category"]}
                        await query.edit_message_text(prompt + ("\nبرای لغو /cancel را بزن." if action != "edit_image" else ""))
                        return

            elif data.startswith("admin_p_name_") or data.startswith("admin_p_price_"):
                pid = int(data.rsplit("_", 1)[1])
                action = "edit_name" if data.startswith("admin_p_name_") else "edit_price"
                with db() as con:
                    row = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
                if not row:
                    await query.answer("محصول پیدا نشد.", show_alert=True)
                    return
                context.user_data["product_admin_state"] = {"action": action, "product_id": pid}
                prompt = "نام جدید محصول را بفرست:" if action == "edit_name" else "قیمت جدید را فقط به عدد و بر اساس واحد پول محصول وارد کن. برای قیمت نامشخص عدد 0 وارد کن:"
                await query.edit_message_text(prompt + "\n\nبرای لغو /cancel را بزن.")
                return

            elif data.startswith("admin_p_cat_"):
                pid = int(data.rsplit("_", 1)[1])
                with db() as con:
                    row = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
                if not row:
                    await query.answer("محصول پیدا نشد.", show_alert=True)
                    return
                keyboard = [
                    [InlineKeyboardButton(label, callback_data=f"admin_p_setcat_{pid}_{key}")]
                    for key, label in categories.items()
                ]
                keyboard.append([InlineKeyboardButton("↩️ بازگشت", callback_data=f"admin_p_edit_{pid}")])
                await query.edit_message_text("دسته‌بندی جدید را انتخاب کن:", reply_markup=InlineKeyboardMarkup(keyboard))
                return

            elif data.startswith("admin_p_setcat_"):
                # فرمت: admin_p_setcat_<product_id>_<category_key>؛ کلید دسته ممکن است underscore داشته باشد.
                prefix = "admin_p_setcat_"
                payload = data[len(prefix):]
                try:
                    pid_text, category = payload.split("_", 1)
                    pid = int(pid_text)
                except (ValueError, IndexError):
                    await query.answer("درخواست نامعتبر است.", show_alert=True)
                    return
                if category not in categories:
                    await query.answer("دسته‌بندی نامعتبر است.", show_alert=True)
                    return
                with db() as con:
                    cur = con.execute("UPDATE products SET category=? WHERE id=?", (category, pid))
                if cur.rowcount != 1:
                    await query.answer("محصول پیدا نشد.", show_alert=True)
                    return
                await query.answer("دسته‌بندی تغییر کرد.")
                data = "admin_products"

            elif data.startswith("admin_p_del_"):
                pid = int(data.rsplit("_", 1)[1])
                with db() as con:
                    row = con.execute("SELECT id, name FROM products WHERE id=?", (pid,)).fetchone()
                if not row:
                    await query.answer("محصول پیدا نشد.", show_alert=True)
                    return
                keyboard = [[
                    InlineKeyboardButton("🗑 تأیید حذف", callback_data=f"admin_p_delconfirm_{pid}"),
                    InlineKeyboardButton("❌ لغو", callback_data="admin_products"),
                ]]
                await query.edit_message_text(
                    f"⚠️ حذف محصول #{pid}\n\n{row['name']}\n\nاگر سفارش قبلی داشته باشد، حذف فیزیکی نمی‌شود و فقط غیرفعال خواهد شد.",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return

            elif data.startswith("admin_p_delconfirm_"):
                pid = int(data.rsplit("_", 1)[1])
                with db() as con:
                    row = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
                    if not row:
                        await query.answer("محصول پیدا نشد.", show_alert=True)
                        return
                    old_orders = con.execute("SELECT COUNT(*) FROM orders WHERE product_id=?", (pid,)).fetchone()[0]
                    if old_orders:
                        con.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
                        result_message = "این محصول سفارش قبلی دارد؛ برای حفظ تاریخچه فقط غیرفعال شد."
                    else:
                        con.execute("DELETE FROM products WHERE id=?", (pid,))
                        result_message = "محصول حذف شد."
                await query.answer(result_message, show_alert=True)

            with db() as con:
                total_products = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                total_pages = max(1, (int(total_products) + products_per_page - 1) // products_per_page)
                products_page = min(products_page, total_pages)
                rows = con.execute(
                    "SELECT id, category, name, price, active, currency, button_text, icon, button_style, sort_order, star_amount, old_price FROM products ORDER BY sort_order, id LIMIT ? OFFSET ?",
                    (products_per_page, (products_page - 1) * products_per_page),
                ).fetchall()

            text = "🛍 مدیریت محصولات و قیمت‌ها | SEYED PREMIUM\n━━━━━━━━━━━━━━━━━━\n"
            text += f"📦 کل محصولات: {total_products}\n"
            text += f"📄 صفحه {products_page} از {total_pages}\n"
            text += "محصولات جدیدی که اضافه شوند، خودکار در همین فهرست نمایش داده می‌شوند.\n\n"
            keyboard = []

            for r in rows:
                price_value = int(r["price"] or 0)
                currency_value = str(r["currency"] or "تومان")
                price_text = f"{price_value:,} {currency_value}" if price_value > 0 else "تنظیم نشده"
                status = "🟢 فعال" if r["active"] else "🔴 غیرفعال"
                text += (f"#{r['id']} | {r['name']}\n📂 {r['category']} | 💰 {price_text}\n"
                         f"🔘 {r['icon'] or '—'} {r['button_text'] or r['name']} | رنگ: {r['button_style']}\n"
                         f"↕️ ترتیب: {r['sort_order']} | مقدار استارز: {r['star_amount'] or '—'}\nوضعیت: {status}\n\n")
                keyboard.append([
                    InlineKeyboardButton(
                        "🔴 غیرفعال" if r["active"] else "🟢 فعال",
                        callback_data=f"admin_p_toggle_{r['id']}"
                    ),
                    InlineKeyboardButton("✏️ ویرایش", callback_data=f"admin_p_edit_{r['id']}"),
                    InlineKeyboardButton("🗑 حذف", callback_data=f"admin_p_del_{r['id']}"),
                ])

            page_buttons = []
            if products_page > 1:
                page_buttons.append(InlineKeyboardButton("⬅️ صفحه قبل", callback_data=f"admin_products_page_{products_page - 1}"))
            if products_page < total_pages:
                page_buttons.append(InlineKeyboardButton("صفحه بعد ➡️", callback_data=f"admin_products_page_{products_page + 1}"))
            if page_buttons:
                keyboard.append(page_buttons)

            keyboard.append([InlineKeyboardButton("➕ افزودن محصول", callback_data="admin_p_add")])
            keyboard.append([InlineKeyboardButton("🔄 تازه‌سازی", callback_data=f"admin_products_page_{products_page}")])
            keyboard.append([InlineKeyboardButton("🏠 بازگشت به مدیریت", callback_data="admin")])
            await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(keyboard))
            return

        if data.startswith("admin_order_view_"):
            order_id = int(data.rsplit("_", 1)[1])
            with db() as con:
                order = con.execute(
                    """
                    SELECT orders.id, orders.user_id, orders.product_id,
                           orders.price, orders.status, orders.created_at,
                           orders.currency, orders.order_details, orders.receipt_file_id, orders.payment_method,
                           orders.buyer_username, orders.buyer_full_name, orders.recipient_user_id, orders.recipient_username,
                           orders.product_name_snapshot, orders.order_code, orders.payment_verified_at, orders.delivered_at,
                           orders.delivery_error, orders.delivery_mode, products.name, products.category
                    FROM orders
                    LEFT JOIN products ON products.id = orders.product_id
                    WHERE orders.id = ?
                    """,
                    (order_id,)
                ).fetchone()

            if not order:
                await query.edit_message_text(
                    "این سفارش پیدا نشد.",
                    reply_markup=back_menu()
                )
                return

            keyboard = []
            if not order["payment_verified_at"] and not order["delivered_at"]:
                keyboard.extend([
                    [InlineKeyboardButton("✅ تأیید سفارش", callback_data=f"admin_order_approve_{order_id}", style="success")],
                    [InlineKeyboardButton("❌ رد سفارش", callback_data=f"admin_order_reject_{order_id}", style="danger")],
                ])
            elif str(order["category"] or "").lower() == "gifts" and not order["delivered_at"]:
                keyboard.append([InlineKeyboardButton("🎁 جزئیات / ثبت تحویل گیفت", callback_data=f"gift_order_view_{order_id}", style="success")])
            keyboard.extend([
                [InlineKeyboardButton("🗑 حذف سفارش", callback_data=f"admin_order_delete_confirm_{order_id}")],
                [InlineKeyboardButton("⬅️ فهرست سفارش‌ها", callback_data="admin_orders")],
            ])

            await query.edit_message_text(
                f"📦 سفارش #{order['id']}\n\n"
                f"👤 اطلاعات خریدار\nID عددی خریدار: {order['user_id']}\nآیدی تلگرام خریدار: {'@'+order['buyer_username'] if order['buyer_username'] else 'تنظیم نشده'}\n"
                + (f"\n🎯 اطلاعات گیرنده\nID عددی گیرنده: {order['recipient_user_id'] or 'ثبت نشده'}\nآیدی تلگرام گیرنده: {order['recipient_username'] or 'ثبت نشده'}\n" if str(order['category'] or '').lower() in {'gifts', 'premium'} else "")
                + f"\n🛍 محصول: {order['product_name_snapshot'] or order['name'] or ('محصول #' + str(order['product_id']))}\n"
                + f"🧾 کد سفارش: {order['order_code'] or ('#'+str(order['id']))}\n"
                + f"💰 مبلغ: {order['price']:,} {order['currency'] or 'تومان'}\n"
                + f"📝 جزئیات: {order['order_details'] or 'ثبت نشده'}\n"
                + f"💳 روش پرداخت: {order['payment_method'] or 'نامشخص'}\n"
                + f"🧾 رسید: {'ثبت شده' if order['receipt_file_id'] else 'ثبت نشده'}\n"
                + f"📌 وضعیت: {order['status']}\n"
                + f"📅 زمان ثبت: {format_iran_jalali_datetime(order['created_at'])}\n"
                + f"💳 زمان تأیید پرداخت: {format_iran_jalali_datetime(order['payment_verified_at'])}\n"
                + f"📦 زمان تحویل: {format_iran_jalali_datetime(order['delivered_at'])}\n"
                + f"⚠️ خطای ارسال: {order['delivery_error'] or 'ندارد'}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        if data.startswith("admin_order_delete_confirm_"):
            order_id = int(data.rsplit("_", 1)[1])
            with db() as con:
                order = con.execute(
                    "SELECT id FROM orders WHERE id = ?",
                    (order_id,)
                ).fetchone()

            if not order:
                await query.edit_message_text(
                    "این سفارش قبلاً حذف شده یا وجود ندارد.",
                    reply_markup=back_menu()
                )
                return

            await query.edit_message_text(
                f"⚠️ تأیید حذف سفارش #{order_id}\n\n"
                "با تأیید، سفارش برای همیشه از دیتابیس حذف می‌شود. ادامه می‌دهی؟",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🗑 بله، حذف شود",
                        callback_data=f"admin_order_delete_yes_{order_id}"
                    )],
                    [InlineKeyboardButton(
                        "لغو و بازگشت",
                        callback_data="admin_orders"
                    )]
                ])
            )
            return

        if data.startswith("admin_order_delete_yes_"):
            order_id = int(data.rsplit("_", 1)[1])

            with db() as con:
                ensure_discount_schema(con)
                con.execute("BEGIN IMMEDIATE")
                order = con.execute(
                    "SELECT user_id, status FROM orders WHERE id = ?",
                    (order_id,)
                ).fetchone()

                if not order:
                    await query.edit_message_text(
                        "این سفارش پیدا نشد یا قبلاً حذف شده است.",
                        reply_markup=back_menu()
                    )
                    return

                customer_id = order["user_id"]
                pending_use = con.execute(
                    "SELECT code FROM coupon_usage WHERE order_id=? AND status='pending' LIMIT 1",
                    (order_id,)
                ).fetchone()
                if pending_use:
                    con.execute("DELETE FROM coupon_usage WHERE order_id=? AND status='pending'", (order_id,))
                    con.execute("UPDATE discount_codes SET total_uses=MAX(0, COALESCE(total_uses, 0)-1) WHERE code=?", (pending_use["code"],))
                con.execute("UPDATE orders SET status='لغو شده' WHERE id = ?", (order_id,))

            try:
                await context.bot.send_message(
                    chat_id=customer_id,
                    text=f"اطلاع‌رسانی SEYED PREMIUM: سفارش شماره #{order_id} توسط مدیریت لغو و بایگانی شد. برای پیگیری با پشتیبانی تماس بگیر."
                )
            except Exception:
                pass

            await query.edit_message_text(
                f"✅ سفارش #{order_id} لغو و بایگانی شد؛ تاریخچه حذف نشد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "📦 بازگشت به سفارش‌ها",
                        callback_data="admin_orders"
                    )],
                    [InlineKeyboardButton(
                        "🏠 منوی اصلی",
                        callback_data="home"
                    )]
                ])
            )
            return

        if data.startswith("admin_order_approve_") or data.startswith("admin_order_reject_"):
            approve = data.startswith("admin_order_approve_")
            order_id = int(data.rsplit("_", 1)[1])
            with db() as con:
                ensure_discount_schema(con)
                con.execute("BEGIN IMMEDIATE")
                order = con.execute(
                    "SELECT o.user_id,o.status,o.delivery_mode,o.recipient_user_id,o.payment_verified_at,o.delivered_at,p.category,p.name AS product_name "
                    "FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id = ?",
                    (order_id,),
                ).fetchone()
                if not order:
                    await query.edit_message_text("این سفارش پیدا نشد.", reply_markup=back_menu())
                    return
                if approve and str(order["status"] or "") == "رد شده":
                    await query.answer("این سفارش قبلاً رد شده؛ سفارش جدید ثبت شود.", show_alert=True)
                    return
                if approve and order["payment_verified_at"]:
                    already = " و قبلاً تحویل شده" if order["delivered_at"] else " و در انتظار تحویل است"
                    await query.answer("پرداخت این سفارش قبلاً تأیید شده" + already + "؛ برای جلوگیری از پرداخت/ارسال تکراری، دوباره تأیید نشد.", show_alert=True)
                    return
                if approve and str(order["status"] or "") == "در انتظار ارسال رسید":
                    await query.answer("هنوز رسیدی ثبت نشده است؛ پیش از تأیید باید رسید را بررسی کنید.", show_alert=True)
                    return
                customer_id = int(order["user_id"])
                category = str(order["category"] or "").lower()
                if approve:
                    new_status = "پرداخت تأیید شد - در انتظار تحویل" if category in {"gifts", "premium"} else "تأیید شده"
                    con.execute("UPDATE orders SET status=?,payment_verified_at=?,delivery_error='' WHERE id=?",
                                (new_status, iran_timestamp_now(), order_id))
                    con.execute("UPDATE coupon_usage SET status='used' WHERE order_id=? AND status='pending'", (order_id,))
                    log_order_event(con, order_id, "payment_approved", new_status, "پرداخت توسط مدیر تأیید شد.", ADMIN_ID)
                else:
                    new_status = "رد شده"
                    pending_use = con.execute(
                        "SELECT code FROM coupon_usage WHERE order_id=? AND status='pending' LIMIT 1", (order_id,)
                    ).fetchone()
                    if pending_use:
                        con.execute("DELETE FROM coupon_usage WHERE order_id=? AND status='pending'", (order_id,))
                        con.execute("UPDATE discount_codes SET total_uses=MAX(0, COALESCE(total_uses,0)-1) WHERE code=?", (pending_use["code"],))
                    con.execute("UPDATE orders SET status=? WHERE id=?", (new_status, order_id))
                    log_order_event(con, order_id, "payment_rejected", new_status, "پرداخت/رسید توسط مدیر رد شد.", ADMIN_ID)

            delivery_note = ""
            if approve and category == "premium":
                result = await try_deliver_official_premium(context, order_id)
                if result.get("ok"):
                    delivery_note = "\n✅ تلگرام ارسال اشتراک پرمیوم را با موفقیت تأیید کرد؛ زمان تحویل ثبت شد."
                    with db() as con:
                        log_order_event(con, order_id, "premium_delivered_auto", "تحویل‌شده", "ارسال اشتراک از طریق giftPremiumSubscription در Telegram Bot API موفق بود.", ADMIN_ID)
                elif result.get("manual"):
                    delivery_note = "\n✋ پرداخت تأیید شد؛ تحویل این سفارش دستی است و هنوز تحویل‌شده ثبت نشده."
                    with db() as con:
                        log_order_event(con, order_id, "awaiting_manual_delivery", "پرداخت تأیید شد - در انتظار تحویل", result.get("reason", ""), ADMIN_ID)
                else:
                    delivery_note = "\n⚠️ ارسال خودکار پرمیوم تأیید نشد؛ سفارش تحویل‌شده اعلام نشد و برای بررسی مدیر باقی ماند.\nخطا: " + str(result.get("reason", "نامشخص"))[:300]
                    with db() as con:
                        log_order_event(con, order_id, "premium_auto_delivery_failed", "پرداخت تأیید شد - نیازمند بررسی تحویل", result.get("reason", ""), ADMIN_ID)

            if approve and category == "gifts":
                result = await try_deliver_official_gift(context, order_id)
                if result.get("ok"):
                    delivery_note = "\n✅ گیفت با موفقیت از طریق API رسمی تلگرام ارسال شد؛ زمان تحویل ثبت شد."
                    with db() as con:
                        log_order_event(con, order_id, "gift_delivered_auto", "تحویل‌شده", "ارسال رسمی گیفت توسط Telegram Bot API موفق بود.", ADMIN_ID)
                elif result.get("manual"):
                    delivery_note = "\n✋ پرداخت تأیید شد؛ تحویل این سفارش دستی است و هنوز تحویل‌شده ثبت نشده."
                    with db() as con:
                        log_order_event(con, order_id, "awaiting_manual_delivery", "پرداخت تأیید شد - در انتظار تحویل", result.get("reason", ""), ADMIN_ID)
                else:
                    delivery_note = "\n⚠️ ارسال خودکار ناموفق بود؛ سفارش تحویل‌شده اعلام نشد و در انتظار تحویل باقی ماند.\nخطا: " + str(result.get("reason", "نامشخص"))[:300]
                    with db() as con:
                        log_order_event(con, order_id, "gift_auto_delivery_failed", "پرداخت تأیید شد - در انتظار تحویل", result.get("reason", ""), ADMIN_ID)

            try:
                customer_text = f"📦 وضعیت سفارش شما در SEYED PREMIUM تغییر کرد.\nکد سفارش: SP-{order_id:06d}\nوضعیت: {new_status}"
                if approve and category in {"gifts", "premium"}:
                    with db() as con:
                        delivered = con.execute("SELECT delivered_at,order_code,delivery_mode,status,delivery_error FROM orders WHERE id=?", (order_id,)).fetchone()
                    if delivered and delivered["delivered_at"]:
                        product_label = "اشتراک پرمیوم" if category == "premium" else "گیفت"
                        customer_text = f"✅ {product_label} سفارش شما با موفقیت ارسال شد.\nکد سفارش: {delivered['order_code'] or ('#'+str(order_id))}\nزمان تحویل: {format_iran_jalali_datetime(delivered['delivered_at'])}"
                    else:
                        order_code = delivered["order_code"] if delivered else ""
                        product_label = "پرمیوم" if category == "premium" else "گیفت"
                        customer_text = f"✅ پرداخت سفارش {product_label} شما تأیید شد.\nکد سفارش: {order_code or ('#'+str(order_id))}\nوضعیت: در انتظار تحویل/بررسی مدیریت."
                        if delivered and delivered["delivery_error"]:
                            customer_text += "\nدر صورت نیاز، پشتیبانی وضعیت تحویل را پیگیری می‌کند."
                        if category == "gifts":
                            cfg = load_gift_settings()
                            if cfg.get("support_first"):
                                customer_text += "\n\n" + cfg["support_text"]
                await context.bot.send_message(
                    chat_id=customer_id,
                    text=customer_text,
                    reply_markup=(InlineKeyboardMarkup([[InlineKeyboardButton("💬 ارسال پیام به پشتیبانی", callback_data="support")]])
                                  if approve and category == "gifts" and load_gift_settings().get("support_first") else None),
                )
            except Exception:
                logger.exception("Could not notify customer about order #%s", order_id)

            admin_text = f"✅ پرداخت سفارش #{order_id} تأیید شد." if approve else f"❌ سفارش #{order_id} رد شد."
            await query.edit_message_text(
                admin_text + delivery_note,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 جزئیات سفارش", callback_data=f"admin_order_view_{order_id}")],
                    [InlineKeyboardButton("🎁 سفارش‌های گیفت", callback_data="gift_orders")],
                    [InlineKeyboardButton("📦 فهرست سفارش‌ها", callback_data="admin_orders")],
                ])
            )
            return

        if data == "admin_orders":
            with db() as con:
                rows = con.execute(
                    """
                    SELECT id, user_id, product_id, price, status, created_at
                    FROM orders ORDER BY id DESC LIMIT 10
                    """
                ).fetchall()

            if not rows:
                await query.edit_message_text(
                    "📦 هنوز سفارشی ثبت نشده است.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ بازگشت به مدیریت", callback_data="admin")]])
                )
                return

            keyboard = []
            for row in rows:
                keyboard.append([
                    InlineKeyboardButton(
                        f"📄 سفارش #{row['id']} - {row['status']}",
                        callback_data=f"admin_order_view_{row['id']}"
                    ),
                    InlineKeyboardButton(
                        f"🗑 حذف #{row['id']}",
                        callback_data=f"admin_order_delete_confirm_{row['id']}"
                    )
                ])

            keyboard.append([
                InlineKeyboardButton("↩️ بازگشت به مدیریت", callback_data="admin")
            ])

            await query.edit_message_text(
                "📦 مدیریت سفارش‌ها\n\n"
                "برای حذف سفارش، دکمه 🗑 کنار آن را بزن. "
                "قبل از حذف، تأیید نهایی نمایش داده می‌شود.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

    await query.edit_message_text("گزینه ناشناخته است.", reply_markup=back_menu())


# ==========================================
# پیام‌های متنی
# ==========================================

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _handler_started = time.perf_counter()
    try:
        await _text_message_impl(update, context)
    except Exception as exc:
        state = getattr(context, "user_data", None)
        active_states = sorted(k for k, v in state.items() if v is not None) if isinstance(state, dict) else []
        logger.exception(
            "Unhandled text-message failure; update_id=%s; active_state_keys=%s",
            getattr(update, "update_id", None), active_states,
        )
        message = update.effective_message
        if message and update.effective_user and update.effective_user.id == ADMIN_ID:
            try:
                await message.reply_text(
                    f"❌ خطایی هنگام پردازش پیام رخ داد ({type(exc).__name__})؛ جزئیات کامل در لاگ ثبت شد.\n"
                    "تنظیم قبلی تغییر نکرده است. برای لغو ورودی معلق /cancel را بزن."
                )
            except Exception:
                logger.exception("Could not notify admin about text-message failure")
    finally:
        logger.info("PERF text_message: %.3fs",
                    time.perf_counter() - _handler_started)


async def cancel_coupon_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data if isinstance(getattr(context, "user_data", None), dict) else {}
    user = update.effective_user
    message = update.effective_message
    if user and user.id == ADMIN_ID:
        cleared = False
        for key in ("gift_admin_input", "premium_admin_input", "gift_custom_state", "support_admin_reply_state", "support_admin_search_state", "gift_order_search_state"):
            if user_data.pop(key, None) is not None:
                cleared = True
        if cleared and message:
            await message.reply_text("❌ عملیات مدیریتی لغو شد؛ مرحلهٔ ناقص ذخیره نشد.")
            return
    if user and user.id != ADMIN_ID:
        with db() as con:
            con.execute("DELETE FROM support_form_sessions WHERE user_id=?", (user.id,))
            active_ticket = con.execute("SELECT ticket_id FROM support_active_tickets WHERE user_id=?", (user.id,)).fetchone()
            if active_ticket:
                con.execute("DELETE FROM support_active_tickets WHERE user_id=?", (user.id,))
        user_data.pop("support_form_active", None)
        if active_ticket and message:
            await message.reply_text("✅ گفت‌وگوی پشتیبانی پایان یافت؛ تیکت و سوابق آن برای مدیریت محفوظ است.", reply_markup=main_menu())
            return
    # /cancel exits transient Stars quantity/recipient forms without creating an order.
    if user and user.id != ADMIN_ID and (user_data.get("stars_custom_amount") is not None or user_data.get("stars_custom_order") or user_data.get("stars_custom_wait_recipient") or user_data.get("stars_fixed_wait_recipient")):
        was_waiting_fixed_recipient = bool(user_data.get("stars_fixed_wait_recipient"))
        user_data.pop("stars_custom_amount", None)
        user_data.pop("stars_custom_order", None)
        user_data.pop("stars_custom_wait_recipient", None)
        user_data.pop("stars_fixed_wait_recipient", None)
        if message:
            cancel_text = "✅ انتخاب گیرنده لغو شد؛ سفارشی ثبت نشد." if was_waiting_fixed_recipient else "✅ مرحلهٔ مقدار دلخواه لغو شد؛ سفارشی ثبت نشد."
            await message.reply_text(cancel_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ بازگشت به استارز", callback_data="service_stars", style="primary")]]))
        return
    # Customer can cancel an order that is still awaiting a receipt; preserve its history.
    if user and user.id != ADMIN_ID and user_data.get("pending_receipt_order_id"):
        order_id = int(user_data.pop("pending_receipt_order_id"))
        with db() as con:
            con.execute(
                "UPDATE orders SET status='لغو شده' WHERE id=? AND user_id=? AND status='در انتظار ارسال رسید'",
                (order_id, user.id),
            )
        if message:
            await message.reply_text(f"سفارش #{order_id} لغو شد؛ سوابق برای پیگیری محفوظ می‌ماند.")
        return
    # The shared /cancel command also exits customer-side mediator and TON forms.
    if user_data.get("mediator_request_state") or user_data.get("ton_purchase_state"):
        user_data.pop("mediator_request_state", None)
        user_data.pop("ton_purchase_state", None)
        if message:
            await message.reply_text(
                "درخواست لغو شد؛ هیچ پرداخت یا سفارشی ایجاد نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]]),
            )
        return
    if not user or user.id != ADMIN_ID:
        return

    pricing_admin_state = user_data.pop("pricing_admin_state", None)
    product_state = user_data.pop("product_admin_state", None)
    ui_state = user_data.pop("uiadm_state", None)
    editor_wait_key = user_data.pop("editor_wait_key", None)
    if pricing_admin_state:
        if message:
            await message.reply_text("❌ تنظیم قیمت‌گذاری لغو شد؛ مقدار قبلی حفظ شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💹 بازگشت به قیمت‌گذاری", callback_data="admin_auto_pricing", style="primary")]]))
        return
    if product_state or ui_state or editor_wait_key:
        if message:
            await message.reply_text("✅ عملیات ویرایش لغو شد؛ اطلاعات قبلی حفظ شدند.")
        return

    edit_state = user_data.pop("coupon_edit_state", None)
    if edit_state:
        if message:
            await message.reply_text(
                "❌ ویرایش لغو شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + str(edit_state["code"]))]])
            )
        return

    category_state = user_data.pop("coupon_category_add_state", None)
    if category_state:
        scope_state = user_data.get("coupon_scope_state")
        if scope_state:
            try:
                with db() as con:
                    ensure_discount_schema(con)
                    categories = list_coupon_categories(con)
                    row = con.execute("SELECT category FROM discount_codes WHERE code=?", (scope_state["code"],)).fetchone()
                if row:
                    selected = coupon_selected_categories(row["category"])
                    scope_state.update(categories=categories, selected=selected)
                    await context.bot.edit_message_text(
                        chat_id=category_state["chat_id"], message_id=category_state["message_id"],
                        text=coupon_scope_text(None, scope_state["code"], categories, selected),
                        reply_markup=coupon_scope_keyboard(scope_state["code"], categories, selected),
                    )
            except Exception:
                logger.exception("Could not restore category editor after /cancel")
        if message:
            await message.reply_text("❌ افزودن دسته لغو شد.")
        return

    if user_data.get("admin_coupon_state"):
        user_data.pop("admin_coupon_state", None)
        if message:
            await message.reply_text("❌ ساخت کد تخفیف لغو شد. هیچ کدی ساخته نشد.")
        return

    remaining = clear_active_operation_state(context)
    if "support_form_active" in remaining and user:
        try:
            with db() as con:
                con.execute("DELETE FROM support_form_sessions WHERE user_id=?", (user.id,))
        except Exception:
            logger.exception("Could not remove support session during /cancel")
    if remaining and message:
        await message.reply_text("❌ عملیات جاری لغو شد؛ هیچ مقدار ذخیره‌نشده‌ای ثبت نشد.")
    elif message:
        await message.reply_text("عملیات در حال انجامی برای لغو وجود ندارد.")


async def _handle_public_request_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect and save mediator / TON requests without automatic payment or fulfillment."""
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return False
    text_value = message.text.strip()

    mediator_state = context.user_data.get("mediator_request_state")
    ton_state = context.user_data.get("ton_purchase_state")
    if not mediator_state and not ton_state:
        return False

    if text_value.casefold() in {"/cancel", "لغو", "❌ لغو عملیات", "❌ لغو"}:
        context.user_data.pop("mediator_request_state", None)
        context.user_data.pop("ton_purchase_state", None)
        await message.reply_text(
            "درخواست لغو شد؛ هیچ پرداخت یا سفارشی ایجاد نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]]),
        )
        return True

    if mediator_state:
        if len(text_value) < 5 or len(text_value) > 1800:
            await message.reply_text("لطفاً توضیحات درخواست واسطه را بین ۵ تا ۱۸۰۰ نویسه بفرست. برای لغو /cancel را ارسال کن.")
            return True
        with db() as con:
            cur = con.execute(
                "INSERT INTO mediator_requests(user_id, username, full_name, details) VALUES (?, ?, ?, ?)",
                (user.id, user.username or "", user.full_name or "", text_value),
            )
            request_id = cur.lastrowid
        context.user_data.pop("mediator_request_state", None)
        admin_notified = False
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🤝 درخواست جدید واسطه\n"
                    f"شماره درخواست: #{request_id}\n"
                    f"کاربر: {user.full_name or 'بدون نام'} (@{user.username or 'بدون نام کاربری'})\n"
                    f"آیدی: {user.id}\n\n"
                    f"توضیحات:\n{text_value}\n\n"
                    "وضعیت: در انتظار بررسی دستی مدیر"
                ),
            )
            admin_notified = True
        except Exception:
            logger.exception("Could not notify administrator about mediator request #%s", request_id)
        notification_text = (
            f"✅ درخواست واسطه با شماره #{request_id} ثبت شد و برای بررسی دستی مدیر ارسال شد.\n"
            if admin_notified else
            f"✅ درخواست واسطه با شماره #{request_id} در سیستم ذخیره شد، اما اعلان مدیر ارسال نشد. از پشتیبانی پیگیری کن.\n"
        )
        await message.reply_text(
            notification_text + "ثبت درخواست به معنی تأیید معامله یا تضمین انجام آن نیست.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]]),
        )
        return True

    if ton_state and ton_state.get("step") == "amount":
        normalized = normalize_ascii_digits(text_value).replace(",", ".")
        if not re.fullmatch(r"\d{1,10}(?:\.\d{1,8})?", normalized):
            await message.reply_text("مقدار تون را فقط با عدد مثبت بفرست؛ مثلاً 2 یا 0.5. برای لغو /cancel را بزن.")
            return True
        try:
            amount = float(normalized)
        except ValueError:
            amount = 0
        if amount <= 0:
            await message.reply_text("مقدار باید بیشتر از صفر باشد. دوباره بفرست:")
            return True
        ton_state["amount_ton"] = normalized
        ton_state["step"] = "wallet"
        context.user_data["ton_purchase_state"] = ton_state
        await message.reply_text(
            "✅ مقدار ثبت شد. حالا آدرس کیف پول TON مقصد را بفرست.\n\n"
            "⚠️ آدرس را با دقت بررسی کن. این فقط درخواست خرید است؛ قیمت زنده، پرداخت و ارسال خودکار فعال نیست. برای لغو /cancel را بفرست."
        )
        return True

    if ton_state and ton_state.get("step") == "wallet":
        if len(text_value) < 10 or len(text_value) > 200 or any(ch.isspace() for ch in text_value):
            await message.reply_text("آدرس کیف پول باید بین ۱۰ تا ۲۰۰ نویسه و بدون فاصله باشد. دوباره بفرست یا /cancel را بزن.")
            return True
        amount = str(ton_state.get("amount_ton", ""))
        with db() as con:
            cur = con.execute(
                "INSERT INTO ton_purchase_requests(user_id, username, amount_ton, wallet_address) VALUES (?, ?, ?, ?)",
                (user.id, user.username or "", amount, text_value),
            )
            request_id = cur.lastrowid
        context.user_data.pop("ton_purchase_state", None)
        admin_notified = False
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🪙 درخواست جدید خرید تون (TON)\n"
                    f"شماره درخواست: #{request_id}\n"
                    f"کاربر: {user.full_name or 'بدون نام'} (@{user.username or 'بدون نام کاربری'})\n"
                    f"آیدی: {user.id}\n"
                    f"مقدار درخواستی: {amount} TON\n"
                    f"آدرس مقصد: {text_value}\n"
                    "وضعیت: در انتظار بررسی دستی؛ قیمت یا پرداخت خودکار انجام نشده است."
                ),
            )
            admin_notified = True
        except Exception:
            logger.exception("Could not notify administrator about TON request #%s", request_id)
        notification_text = (
            f"✅ درخواست خرید تون با شماره #{request_id} ثبت شد و برای بررسی مدیر ارسال شد.\n"
            if admin_notified else
            f"✅ درخواست خرید تون با شماره #{request_id} ذخیره شد، اما اعلان مدیر ارسال نشد. از پشتیبانی پیگیری کن.\n"
        )
        await message.reply_text(
            notification_text +
            "قیمت نهایی، روش پرداخت و امکان انجام درخواست باید توسط مدیر تأیید شود؛ هیچ پرداخت یا انتقالی خودکار انجام نشده است.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")]]),
        )
        return True

    return False


async def _text_message_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Telegram may deliver updates without an effective user (e.g. channel/system updates).
    # In that case PTB exposes user_data as None; use a safe local mapping.
    user_data = context.user_data if isinstance(getattr(context, "user_data", None), dict) else {}
    user = update.effective_user
    message = update.effective_message
    if user:
        register_user(user)

    # Persistent support form: a copied form is parsed only while this user has opened support.
    if message and user and user.id != ADMIN_ID:
        with db() as con:
            support_session = con.execute("SELECT user_id FROM support_form_sessions WHERE user_id=?", (user.id,)).fetchone()
        if support_session:
            if not message.text:
                await message.reply_text("ابتدا فرم متنی پشتیبانی را کپی و تکمیل کن و همین‌جا بفرست. پس از ثبت تیکت می‌توانی عکس و فایل هم ارسال کنی.")
                return
            if _is_customer_cancel_text(message.text):
                with db() as con:
                    con.execute("DELETE FROM support_form_sessions WHERE user_id=?", (user.id,))
                user_data.pop("support_form_active", None)
                await message.reply_text("❌ ارسال پیام به پشتیبانی لغو شد؛ تیکتی ثبت نشد.", reply_markup=main_menu())
                return
            parsed_support = parse_support_form(message.text)
            await create_support_ticket(update, context, parsed_support)
            return
        with db() as con:
            active_ticket = con.execute("SELECT ticket_id FROM support_active_tickets WHERE user_id=?", (user.id,)).fetchone()
        if active_ticket:
            if message.text and _is_customer_cancel_text(message.text):
                with db() as con:
                    con.execute("DELETE FROM support_active_tickets WHERE user_id=?", (user.id,))
                await message.reply_text("✅ گفت‌وگوی پشتیبانی پایان یافت؛ سوابق تیکت محفوظ است.", reply_markup=main_menu())
                return
            await append_support_customer_message(update, context, int(active_ticket["ticket_id"]))
            return

    # Admin reply to a ticket is sent first; only a successful Telegram send is saved as delivered.
    if message and message.text and user and user.id == ADMIN_ID and user_data.get("support_admin_reply_state"):
        state = user_data.get("support_admin_reply_state") or {}
        reply_text = message.text.strip()
        if _is_customer_cancel_text(reply_text) or reply_text.lower() == "/cancel":
            user_data.pop("support_admin_reply_state", None)
            await message.reply_text("❌ پاسخ‌گویی لغو شد؛ پیامی ارسال نشد.")
            return
        if not reply_text or len(reply_text) > 3500:
            await message.reply_text("متن پاسخ باید بین ۱ تا ۳۵۰۰ نویسه باشد. دوباره بفرست یا /cancel بزن.")
            return
        ticket_id = int(state.get("ticket_id") or 0)
        with db() as con:
            ticket = con.execute("SELECT sender_user_id,ticket_code FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
        if not ticket:
            user_data.pop("support_admin_reply_state", None)
            await message.reply_text("تیکت پیدا نشد؛ پاسخ ارسال نشد.")
            return
        try:
            await context.bot.send_message(
                chat_id=int(ticket["sender_user_id"]),
                text=f"💎 پاسخ پشتیبانی SEYED PREMIUM\nشماره پیگیری: {ticket['ticket_code']}\n━━━━━━━━━━━━━━━━━━\n{reply_text}",
            )
        except Exception as exc:
            logger.exception("Support reply delivery failed for ticket_id=%s", ticket_id)
            await message.reply_text("❌ ارسال پاسخ به مشتری ناموفق بود؛ پاسخ به‌عنوان ارسال‌شده ذخیره نشد.\n" + str(exc)[:250])
            return
        now = iran_timestamp_now()
        with db() as con:
            con.execute("INSERT INTO support_ticket_messages(ticket_id,sender_user_id,sender_role,message_text,created_at) VALUES(?,?,'admin',?,?)",
                        (ticket_id, ADMIN_ID, reply_text, now))
            con.execute("UPDATE support_tickets SET status='پاسخ داده‌شده',updated_at=?,admin_replied_at=?,last_admin_id=? WHERE id=?",
                        (now, now, ADMIN_ID, ticket_id))
        user_data.pop("support_admin_reply_state", None)
        await message.reply_text(f"✅ پاسخ با موفقیت برای مشتری ارسال و در سوابق تیکت {ticket['ticket_code']} ثبت شد.")
        return

    # Admin searches tickets/orders by numeric ID, username, ticket code or order code.
    if message and message.text and user and user.id == ADMIN_ID and user_data.get("support_admin_search_state"):
        value = message.text.strip()
        if _is_customer_cancel_text(value) or value.lower() == "/cancel":
            user_data.pop("support_admin_search_state", None)
            await message.reply_text("❌ جست‌وجو لغو شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 مدیریت تیکت‌ها", callback_data="admin_support_tickets")]]))
            return
        user_data.pop("support_admin_search_state", None)
        orders_found = find_orders_for_support(value)
        norm = normalize_ascii_digits(value).strip().lstrip("#")
        with db() as con:
            if norm.upper().startswith("SP-T"):
                tickets_found = con.execute("SELECT id,ticket_code,status,buyer_user_id,product_name,created_at FROM support_tickets WHERE upper(ticket_code)=?", (norm.upper(),)).fetchall()
            elif norm.isdigit():
                tickets_found = con.execute("SELECT id,ticket_code,status,buyer_user_id,product_name,created_at FROM support_tickets WHERE buyer_user_id=? OR recipient_user_id=? OR sender_user_id=? ORDER BY id DESC LIMIT 10", (int(norm), int(norm), int(norm))).fetchall()
            else:
                uname = (norm if norm.startswith("@") else "@" + norm).lstrip("@").lower()
                tickets_found = con.execute("SELECT id,ticket_code,status,buyer_user_id,product_name,created_at FROM support_tickets WHERE lower(sender_username)=? OR lower(buyer_username)=? OR lower(recipient_username)=? ORDER BY id DESC LIMIT 10", (uname, uname, "@"+uname)).fetchall()
        lines = [f"🔎 نتیجه جست‌وجو برای: {value}", "━━━━━━━━━━━━━━━━━━"]
        buttons = []
        if tickets_found:
            lines.append("\n💬 تیکت‌ها:")
            for row in tickets_found:
                lines.append(f"{row['ticket_code']} | {row['status']} | ID {row['buyer_user_id']} | {format_iran_jalali_datetime(row['created_at'])}")
                buttons.append([InlineKeyboardButton(f"📩 {row['ticket_code']}", callback_data=f"admin_ticket_view_{row['id']}")])
        if orders_found:
            lines.append("\n📦 سفارش‌ها:")
            for row in orders_found[:10]:
                lines.append(f"{row['order_code'] or ('#'+str(row['id']))} | {row['product_name'] or 'محصول'} | {row['status']}")
                buttons.append([InlineKeyboardButton(f"🔎 سفارش {row['order_code'] or ('#'+str(row['id']))}", callback_data=f"admin_order_view_{row['id']}")])
        if not tickets_found and not orders_found:
            lines.append("\nنتیجه‌ای پیدا نشد. ID، @username یا کد را بررسی کن.")
        buttons.append([InlineKeyboardButton("🔙 مدیریت تیکت‌ها", callback_data="admin_support_tickets")])
        await message.reply_text("\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(buttons))
        return

    if message and message.text and user and user.id == ADMIN_ID and user_data.get("gift_order_search_state"):
        value = message.text.strip()
        if _is_customer_cancel_text(value) or value.lower() == "/cancel":
            user_data.pop("gift_order_search_state", None)
            await message.reply_text("❌ جست‌وجو لغو شد.", reply_markup=gift_admin_home_keyboard())
            return
        user_data.pop("gift_order_search_state", None)
        found = find_orders_for_support(value)
        gift_rows = []
        if found:
            ids = [int(r["id"]) for r in found]
            with db() as con:
                placeholders = ",".join("?" for _ in ids)
                gift_rows = con.execute(
                    f"SELECT o.id,o.order_code,o.status,p.name AS product_name,o.user_id,o.recipient_user_id,o.price FROM orders o JOIN products p ON p.id=o.product_id WHERE lower(p.category)='gifts' AND o.id IN ({placeholders}) ORDER BY o.id DESC",
                    ids,
                ).fetchall()
        if gift_rows:
            lines = [f"🔎 سفارش‌های گیفت برای: {value}"]
            buttons=[]
            for row in gift_rows:
                code = row["order_code"] or f"#{row['id']}"
                lines.append(f"\n{code} | {row['product_name']}\nخریدار ID: {row['user_id']} | گیرنده ID: {row['recipient_user_id'] or 'ثبت نشده'}\nمبلغ: {toman(row['price'])} | وضعیت: {row['status']}")
                buttons.append([InlineKeyboardButton(f"🔎 جزئیات {code}", callback_data=f"gift_order_view_{row['id']}")])
            buttons.append([InlineKeyboardButton("🔙 سفارش‌های گیفت", callback_data="gift_orders")])
            await message.reply_text("\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await message.reply_text("برای این جست‌وجو سفارش گیفتی پیدا نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 سفارش‌های گیفت", callback_data="gift_orders")]]))
        return

    # Premium manager inputs; all values are isolated in premium_manager_settings_v1.
    premium_input = user_data.get("premium_admin_input") if user and user.id == ADMIN_ID else None
    if message and message.text and premium_input:
        raw = message.text.strip()
        if _is_customer_cancel_text(raw) or raw.lower() == "/cancel":
            user_data.pop("premium_admin_input", None)
            await message.reply_text("❌ عملیات لغو شد؛ تنظیم قبلی حفظ شد.", reply_markup=premium_admin_keyboard())
            return
        field = str(premium_input.get("field") or "")
        cfg = load_premium_manager_settings()
        clean = normalize_ascii_digits(raw).replace("٬", "").replace(",", "").replace("،", "").strip()
        note = ""
        if field == "star_rate_toman":
            if not clean.isdigit() or not 1 <= int(clean) <= 100_000_000:
                await message.reply_text("❌ نرخ باید عددی بین ۱ تا ۱۰۰٬۰۰۰٬۰۰۰ تومان باشد. دوباره بفرست یا /cancel را بزن.")
                return
            cfg["star_rate_toman"] = int(clean)
            note = "✅ نرخ دستی هر استارز پرمیوم ذخیره شد."
        elif field == "fragment_prices":
            if clean == "0":
                cfg["fragment_manual_usd"] = {"3": "", "6": "", "12": ""}
                note = "✅ قیمت‌های دلاری دستی پاک شدند؛ قیمت خودکار/پیش‌فرض استفاده می‌شود."
            else:
                parts = clean.split()
                if len(parts) != 3:
                    await message.reply_text("❌ دقیقاً سه قیمت به ترتیب ۳، ۶ و ۱۲ ماهه بفرست. نمونه: 11.99 15.99 28.99")
                    return
                parsed = []
                try:
                    for part in parts:
                        value = Decimal(part)
                        if not value.is_finite() or value <= 0 or value > Decimal("10000"):
                            raise InvalidOperation
                        parsed.append(str(value))
                except (InvalidOperation, ValueError):
                    await message.reply_text("❌ هر سه قیمت باید عدد دلاری معتبر و مثبت باشند. نمونه: 11.99 15.99 28.99")
                    return
                cfg["fragment_manual_usd"] = dict(zip(("3", "6", "12"), parsed))
                note = "✅ قیمت دلاری دستی هر سه پلن ذخیره شد."
        elif field == "base_usd_fx":
            normalized = normalize_ascii_digits(raw).strip()
            price_match = re.fullmatch(r"قیمت\s+([0-9]+(?:\.[0-9]+)?)", normalized, re.IGNORECASE)
            dollar_match = re.fullmatch(r"دلار\s+([0-9,٬،]+)", normalized, re.IGNORECASE)
            if price_match:
                value = _pricing_decimal(price_match.group(1))
                if value is None or value > Decimal("100"):
                    await message.reply_text("❌ قیمت دلاری معتبر نیست. نمونه: قیمت 0.0155")
                    return
                cfg["base_star_usd_manual"] = str(value)
                note = "✅ قیمت پایهٔ دلاری پرمیوم ذخیره شد."
            elif dollar_match:
                amount = int(re.sub(r"[,٬،]", "", dollar_match.group(1)) or "0")
                if amount == 0:
                    cfg["usd_toman_manual"] = 0
                    note = "✅ نرخ دلار/تتر به حالت خودکار برگشت."
                elif 1000 <= amount <= 100_000_000:
                    cfg["usd_toman_manual"] = amount
                    note = "✅ نرخ دستی دلار/تتر ویژهٔ پرمیوم ذخیره شد."
                else:
                    await message.reply_text("❌ نرخ دلار باید ۰ یا عددی بین ۱٬۰۰۰ تا ۱۰۰٬۰۰۰٬۰۰۰ تومان باشد.")
                    return
            else:
                await message.reply_text("❌ دستور معتبر نیست. نمونه‌ها:\nقیمت 0.0155\nدلار 165000\nدلار 0")
                return
        elif field == "profit_percent":
            try:
                value = Decimal(clean)
                if not value.is_finite() or value < 0 or value > 1000:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await message.reply_text("❌ درصد سود باید عددی بین ۰ تا ۱۰۰۰ باشد.")
                return
            cfg["profit_percent"] = str(value)
            note = "✅ درصد سود پرمیوم ذخیره شد."
        elif field.startswith("plan_price_"):
            months = field.rsplit("_", 1)[1]
            if months not in {"3", "6", "12"}:
                user_data.pop("premium_admin_input", None)
                await message.reply_text("❌ پلن معتبر نیست.", reply_markup=premium_admin_keyboard())
                return
            if not clean.isdigit():
                await message.reply_text("❌ قیمت را فقط به‌صورت عدد صحیح تومان بفرست؛ برای حذف، 0 وارد کن.")
                return
            amount = int(clean)
            if amount < 0 or amount > 10_000_000_000:
                await message.reply_text("❌ مبلغ خارج از محدودهٔ مجاز است.")
                return
            cfg["plan_price_toman_manual"][months] = "" if amount == 0 else str(amount)
            note = (f"✅ قیمت اختصاصی پلن {months} ماهه حذف شد و محاسبهٔ خودکار فعال شد." if amount == 0
                    else f"✅ قیمت اختصاصی پلن {months} ماهه ذخیره شد.")
        else:
            user_data.pop("premium_admin_input", None)
            await message.reply_text("❌ نوع تنظیم مشخص نیست؛ چیزی تغییر نکرد.", reply_markup=premium_admin_keyboard())
            return
        save_premium_manager_settings(cfg)
        sync_premium_prices_in_db()
        user_data.pop("premium_admin_input", None)
        await message.reply_text(note + "\n\n" + premium_admin_text(), reply_markup=premium_admin_keyboard())
        return

    # Gift pricing/settings editor and owner top-up custom amount.
    gift_input = user_data.get("gift_admin_input") if user and user.id == ADMIN_ID else None
    if message and message.text and gift_input:
        raw = message.text.strip()
        if _is_customer_cancel_text(raw) or raw.lower() == "/cancel":
            user_data.pop("gift_admin_input", None)
            await message.reply_text("❌ عملیات لغو شد؛ هیچ تغییری ذخیره نشد.", reply_markup=gift_admin_home_keyboard())
            return
        field = gift_input.get("field")
        if field == "base_star_rate":
            clean = normalize_ascii_digits(raw).replace(",", "").replace("٬", "").replace("،", "").strip()
            if not clean.isdigit() or not 1 <= int(clean) <= 100000000:
                await message.reply_text("❌ نرخ باید عددی بین ۱ تا ۱۰۰٬۰۰۰٬۰۰۰ تومان باشد. دوباره بفرست یا /cancel را بزن.")
                return
            cfg = load_gift_settings(); cfg["base_star_rate_toman"] = int(clean); save_gift_settings(cfg)
            sync_gift_prices_in_db()
            user_data.pop("gift_admin_input", None)
            await message.reply_text("✅ نرخ پایه ذخیره شد و قیمت گیفت‌های رسمی بر اساس نرخ جدید محاسبه شد.", reply_markup=gift_admin_home_keyboard())
            return
        if field == "profit_percent":
            clean = normalize_ascii_digits(raw).replace("٪", "").replace("%", "").strip()
            if not clean.isdigit() or not 0 <= int(clean) <= 1000:
                await message.reply_text("❌ درصد سود باید عددی بین ۰ تا ۱۰۰۰ باشد.")
                return
            cfg = load_gift_settings(); cfg["profit_percent"] = int(clean); save_gift_settings(cfg)
            sync_gift_prices_in_db()
            user_data.pop("gift_admin_input", None)
            await message.reply_text("✅ درصد سود ذخیره شد و قیمت‌های وابسته به Stars به‌روزرسانی شدند.", reply_markup=gift_admin_home_keyboard())
            return
        if field == "usd_settings":
            normalized = normalize_ascii_digits(raw).strip()
            price_match = re.fullmatch(r"قیمت\s+([0-9]+(?:\.[0-9]+)?)", normalized, re.IGNORECASE)
            dollar_match = re.fullmatch(r"دلار\s+([0-9,٬،]+)", normalized, re.IGNORECASE)
            if price_match:
                try:
                    value = Decimal(price_match.group(1))
                    if not value.is_finite() or value <= 0 or value > Decimal("100"):
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    await message.reply_text("❌ قیمت دلاری معتبر نیست. نمونه: قیمت 0.0155")
                    return
                cfg = load_gift_settings(); cfg["usd_per_star_override"] = str(value); save_gift_settings(cfg)
                note = "✅ قیمت پایه دلاری هر ⭐ ذخیره شد."
            elif dollar_match:
                amount = int(re.sub(r"[,٬،]", "", dollar_match.group(1)) or "0")
                if amount == 0:
                    cfg = load_gift_settings(); cfg["usd_toman_manual"] = 0; save_gift_settings(cfg)
                    note = "✅ نرخ دلار به حالت خودکار برگشت."
                elif 1000 <= amount <= 100000000:
                    cfg = load_gift_settings(); cfg["usd_toman_manual"] = amount; save_gift_settings(cfg)
                    note = "✅ نرخ دستی دلار ذخیره شد."
                else:
                    await message.reply_text("❌ نرخ دلار باید ۰ یا عددی بین ۱٬۰۰۰ تا ۱۰۰٬۰۰۰٬۰۰۰ تومان باشد.")
                    return
            else:
                await message.reply_text("❌ دستور معتبر نیست. نمونه‌ها:\nقیمت 0.0155\nدلار 165000\nدلار 0")
                return
            sync_gift_prices_in_db()
            user_data.pop("gift_admin_input", None)
            await message.reply_text(note, reply_markup=gift_admin_home_keyboard())
            return
        if field == "support_text":
            if not raw or len(raw) > 1500:
                await message.reply_text("متن پیام باید بین ۱ تا ۱۵۰۰ نویسه باشد.")
                return
            cfg = load_gift_settings(); cfg["support_text"] = raw; save_gift_settings(cfg)
            user_data.pop("gift_admin_input", None)
            await message.reply_text("✅ متن پیام پشتیبانی ذخیره شد.", reply_markup=gift_admin_home_keyboard())
            return
        if field == "topup_amount":
            clean = normalize_ascii_digits(raw).replace(",", "").replace("٬", "").strip()
            if not clean.isdigit() or not 1 <= int(clean) <= 100000:
                await message.reply_text("❌ تعداد باید بین ۱ تا ۱۰۰٬۰۰۰ باشد.")
                return
            user_data.pop("gift_admin_input", None)
            await _create_owner_star_invoice(update, context, int(clean))
            return

    # Custom gift creation: link -> display name -> fixed Toman price. All new custom items stay manual.
    custom_state = user_data.get("gift_custom_state") if user and user.id == ADMIN_ID else None
    if message and message.text and custom_state:
        raw = message.text.strip()
        if _is_customer_cancel_text(raw) or raw.lower() == "/cancel":
            user_data.pop("gift_custom_state", None)
            await message.reply_text("❌ افزودن گیفت سفارشی لغو شد؛ چیزی ذخیره نشد.", reply_markup=gift_admin_home_keyboard())
            return
        step = custom_state.get("step")
        if step == "link":
            clean_link = raw if raw.startswith("http://") or raw.startswith("https://") else "https://" + raw
            if not re.fullmatch(r"https?://t\.me/[A-Za-z0-9_./-]{3,180}/?", clean_link):
                await message.reply_text("❌ لینک معتبر تلگرام بفرست؛ نمونه: https://t.me/nft/MoodPack-134232")
                return
            custom_state["link"] = clean_link
            custom_state["step"] = "name"
            user_data["gift_custom_state"] = custom_state
            await message.reply_text("✏️ نام کامل این گیفت را بنویس؛ همان نامی که مشتری در فروشگاه می‌بیند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel")]]))
            return
        if step == "name":
            if len(raw) < 2 or len(raw) > 100:
                await message.reply_text("نام گیفت باید بین ۲ تا ۱۰۰ نویسه باشد.")
                return
            custom_state["name"] = raw
            custom_state["step"] = "price"
            user_data["gift_custom_state"] = custom_state
            await message.reply_text("💵 قیمت فروش این گیفت چند تومان است؟ فقط عدد مثبت بفرست.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel")]]))
            return
        if step == "price":
            clean = normalize_ascii_digits(raw).replace(",", "").replace("٬", "").replace("،", "").strip()
            if not clean.isdigit() or not 1 <= int(clean) <= 100000000000:
                await message.reply_text("❌ قیمت باید عدد مثبت و معتبر باشد.")
                return
            with db() as con:
                next_sort = con.execute("SELECT COALESCE(MAX(sort_order),0)+10 FROM products WHERE category='gifts'").fetchone()[0]
                cur = con.execute(
                    "INSERT INTO products(category,name,price,active,button_text,icon,button_style,sort_order,description,currency,source_type,custom_link,delivery_mode,catalog_available) "
                    "VALUES('gifts',?,?,1,?,'🎁','success',?,?,'تومان','custom',?,'manual',0)",
                    (custom_state["name"], int(clean), custom_state["name"], next_sort,
                     "گیفت سفارشی؛ تحویل توسط مدیریت انجام می‌شود.", custom_state["link"]),
                )
                product_id = int(cur.lastrowid)
            user_data.pop("gift_custom_state", None)
            await message.reply_text(
                f"✅ گیفت سفارشی «{custom_state['name']}» با قیمت {toman(int(clean))} تومان اضافه شد.\n"
                "روش تحویل: دستی\nلینک و محصول در دیتابیس ذخیره شدند و در فروشگاه و مدیریت محصولات نمایش داده می‌شوند.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎁 مدیریت گیفت‌ها", callback_data="gift_catalog_page_1")], [InlineKeyboardButton("🔙 مدیریت گیفت", callback_data="gift_admin_home")]]),
            )
            return

    pricing_state = user_data.get("pricing_admin_state")
    if message and message.text and pricing_state:
        if not user or user.id != ADMIN_ID:
            user_data.pop("pricing_admin_state", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return
        raw_value = message.text.strip()
        field = str(pricing_state.get("field") or "")
        if raw_value.casefold() in {"لغو", "cancel"}:
            user_data.pop("pricing_admin_state", None)
            await message.reply_text(pricing_panel_text(), reply_markup=pricing_panel_keyboard())
            return
        cfg = load_auto_pricing_config()
        if field == "nobitex_api_setup":
            user_data.pop("pricing_admin_state", None)
            await message.reply_text("نرخ عمومی از API نوبیتکس بدون API Key و Private Key دریافت می‌شود؛ هیچ کلیدی لازم نیست.", reply_markup=pricing_panel_keyboard())
            return

        if field == "provider_api_key":
            try:
                await message.delete()
            except Exception:
                logger.debug("Could not delete pricing API key message", exc_info=True)
            if len(raw_value) < 12 or len(raw_value) > 512 or any(ch.isspace() for ch in raw_value):
                await message.reply_text("❌ کلید API از نظر قالب معتبر نیست. کلید درست را دوباره بفرست یا /cancel را بزن.")
                return
            cfg["provider_api_key"] = raw_value
            cfg["catalog_valid"] = False
            cfg["catalog_error"] = "کلید جدید ثبت شد؛ دریافت کاتالوگ در حال اعتبارسنجی است."
            cfg["catalog_attempted_at"] = ""
            save_auto_pricing_config(cfg)
            user_data.pop("pricing_admin_state", None)
            ready = await refresh_auto_pricing(force=True)
            cfg = load_auto_pricing_config()
            note = "کلید ذخیره شد و قیمت‌گذاری معتبر شد." if ready else "کلید ذخیره شد، اما منبع هنوز قیمت معتبر برنگردانده است؛ فروش با قیمت قبلی فعال نمی‌شود."
            await message.reply_text(pricing_panel_text(cfg, note), reply_markup=pricing_panel_keyboard(cfg))
            return

        if field == "fx_custom_api_setup":
            user_data.pop("pricing_admin_state", None)
            await message.reply_text("در این نسخه نرخ تتر فقط از API عمومی نوبیتکس دریافت می‌شود؛ تنظیم API سفارشی غیرفعال است.", reply_markup=pricing_panel_keyboard())
            return

        normalized_value = normalize_ascii_digits(raw_value).replace("٪", "").replace("%", "").replace(",", "").replace("٬", "").replace("،", "").strip()
        if field == "fx_manual_rate_toman":
            user_data.pop("pricing_admin_state", None)
            await message.reply_text("نرخ تتر فقط از API عمومی نوبیتکس دریافت می‌شود و ورود نرخ دستی غیرفعال است.", reply_markup=pricing_panel_keyboard())
            return

        if field == "refresh_interval_minutes":
            cfg["refresh_interval_minutes"] = 30
            user_data.pop("pricing_admin_state", None)
            save_auto_pricing_config(cfg)
            await message.reply_text("✅ فاصلهٔ به‌روزرسانی ثابت است و هر ۳۰ دقیقه انجام می‌شود.", reply_markup=pricing_panel_keyboard(cfg))
            return
        elif field in {"margin_percent", "fee_percent"}:
            try:
                value = Decimal(normalized_value)
                max_value = Decimal("500") if field == "margin_percent" else Decimal("100")
                if not value.is_finite() or value < 0 or value > max_value:
                    raise InvalidOperation
            except (InvalidOperation, TypeError, ValueError):
                cap = "۵۰۰" if field == "margin_percent" else "۱۰۰"
                await message.reply_text(f"❌ مقدار باید عددی بین ۰ تا {cap} باشد. دوباره وارد کن یا /cancel را بزن.")
                return
            cfg[field] = str(value)
        else:
            user_data.pop("pricing_admin_state", None)
            await message.reply_text("گزینهٔ تنظیمات معتبر نیست.")
            return
        save_auto_pricing_config(cfg)
        user_data.pop("pricing_admin_state", None)
        await message.reply_text(pricing_panel_text(cfg, "✅ تنظیمات ذخیره شد."), reply_markup=pricing_panel_keyboard(cfg))
        return

    # Customer-facing cancel button is handled before recipient/product text validation,
    # so it cannot fall through and ask for the Telegram ID again.
    if message and message.text and user and user.id != ADMIN_ID and _is_customer_cancel_text(message.text):
        customer_flow_keys = (
            "pending_product_id", "pending_dynamic_quote", "pending_product_coupon",
            "pending_product_coupon_input", "pending_stars_coupon", "stars_coupon",
            "stars_recipient", "stars_custom_amount", "stars_custom_order",
            "stars_custom_wait_recipient", "stars_fixed_wait_recipient",
        )
        active_customer_flow = any(user_data.get(key) is not None for key in customer_flow_keys)
        if active_customer_flow:
            for key in customer_flow_keys:
                user_data.pop(key, None)
            await message.reply_text(
                "✅ سفارش لغو شد؛ هیچ سفارش جدیدی ثبت نشد و مرحلهٔ دریافت آیدی متوقف شد.",
                reply_markup=main_menu(),
            )
            return

    fixed_recipient_state = user_data.get("stars_fixed_wait_recipient")
    if message and message.text and fixed_recipient_state and user and user.id != ADMIN_ID:
        raw_recipient = message.text.strip()
        if _is_customer_cancel_text(raw_recipient):
            user_data.pop("stars_fixed_wait_recipient", None)
            user_data.pop("stars_recipient", None)
            try:
                await context.bot.edit_message_text(
                    chat_id=int(fixed_recipient_state.get("chat_id") or user.id),
                    message_id=int(fixed_recipient_state.get("message_id")),
                    text=stars_text(), reply_markup=build_stars_menu(),
                )
            except Exception:
                logger.debug("Could not restore Stars menu after recipient cancellation", exc_info=True)
                await message.reply_text("مرحلهٔ انتخاب گیرنده لغو شد؛ از منوی استارز دوباره انتخاب کن.")
            return
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw_recipient):
            await message.reply_text("❌ آیدی معتبر نیست. یک نام کاربری عمومی با @ و ۵ تا ۳۲ نویسه بفرست؛ مثال: @SeyedPro_AIA")
            return

        try:
            amount = int(fixed_recipient_state.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount not in FIXED_STAR_AMOUNTS:
            user_data.pop("stars_fixed_wait_recipient", None)
            await message.reply_text("بستهٔ استارز پیدا نشد؛ از منوی استارز دوباره شروع کن.")
            return

        await refresh_auto_pricing(force=False)
        live_quote = auto_pricing_quote("stars", amount) or _saved_stars_pack_quote(amount)
        if live_quote is None:
            user_data.pop("stars_fixed_wait_recipient", None)
            await message.reply_text("قیمت این بسته ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.")
            try:
                await context.bot.edit_message_text(
                    chat_id=int(fixed_recipient_state.get("chat_id") or user.id),
                    message_id=int(fixed_recipient_state.get("message_id")),
                    text="برای دریافت اطلاعات این بسته با پشتیبانی تماس بگیر.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("‹ بازگشت به بسته‌ها", callback_data="service_stars", style="danger")]]),
                )
            except Exception:
                logger.debug("Could not update unavailable-quote Stars screen", exc_info=True)
            return

        try:
            with db() as con:
                ensure_discount_schema(con)
                product = con.execute(
                    "SELECT id, name, currency, active FROM products WHERE category='stars' AND star_amount=? ORDER BY id LIMIT 1",
                    (amount,),
                ).fetchone()
                balance_row = con.execute("SELECT balance FROM users WHERE user_id=?", (user.id,)).fetchone()
                if not product or not int(product["active"] or 0):
                    user_data.pop("stars_fixed_wait_recipient", None)
                    await message.reply_text("این بسته فعلاً در فروشگاه فعال نیست.")
                    return
                base_price = int(live_quote["sale_toman"])
                final_price = base_price
                coupon = user_data.get("stars_coupon", {})
                if coupon.get("amount") == amount and coupon.get("code"):
                    validation = validate_coupon_for_order(
                        con, str(coupon["code"]), user.id, base_price, "stars", str(product["name"]), user.username
                    )
                    if validation.get("ok"):
                        final_price = int(validation["final_price"])
                        user_data["stars_coupon"] = {
                            "amount": amount, "code": validation["code"],
                            "discount": int(validation["discount"]), "final_price": final_price,
                        }
                    else:
                        user_data.pop("stars_coupon", None)
                        await message.reply_text("⚠️ کد تخفیف به دلیل تغییر شرایط اعمال نشد: " + str(validation.get("reason", "کد معتبر نیست")))
                balance = int(balance_row["balance"] or 0) if balance_row else 0
                currency = str(product["currency"] or "تومان")
        except Exception:
            logger.exception("Could not validate fixed Stars recipient order")
            await message.reply_text("بررسی سفارش انجام نشد؛ لطفاً دوباره تلاش کن.")
            return

        user_data.pop("stars_fixed_wait_recipient", None)
        user_data["stars_recipient"] = {
            "amount": amount, "recipient": raw_recipient,
            "recipient_id": raw_recipient, "quote": live_quote,
            "shown_price_toman": int(final_price),
        }
        discount = max(0, base_price - final_price)
        price_text = f"{toman(final_price)} تومان" + (f" (با تخفیف {toman(discount)} تومان)" if discount else "")
        detail = (
            "🧾 تأیید سفارش | SEYED PREMIUM\n\n"
            f"⭐ محصول: {persian_digits(f'{amount:,}'.replace(',', '٬'))} استارز\n"
            f"👤 گیرنده: {raw_recipient}\n"
            f"🆔 آیدی گیرنده: {raw_recipient}\n\n"
            f"💰 مبلغ: {persian_digits(price_text)}\n"
            f"👛 موجودی کیف پول: {persian_digits(toman(balance))} تومان\n\n"
            "━━━━━━━━━━━━━━\n"
            "لطفاً روش پرداخت را انتخاب کنید:"
        )
        payment_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 کارت به کارت", callback_data=f"stars_pay_card_{amount}", style="primary")],
            [InlineKeyboardButton("💰 پرداخت با کیف پول", callback_data=f"stars_pay_wallet_{amount}", style="success")],
            [InlineKeyboardButton("❌ لغو سفارش", callback_data="stars_detail_back", style="danger")],
        ])
        try:
            await context.bot.edit_message_text(
                chat_id=int(fixed_recipient_state.get("chat_id") or user.id),
                message_id=int(fixed_recipient_state.get("message_id")),
                text=detail, reply_markup=payment_keyboard,
            )
        except Exception:
            logger.warning("Could not edit fixed Stars payment screen after recipient entry", exc_info=True)
            await message.reply_text(detail, reply_markup=payment_keyboard)
        return

    recipient_state = user_data.get("stars_custom_wait_recipient")
    if message and message.text and recipient_state:
        raw_recipient = normalize_ascii_digits(message.text.strip())
        if _is_customer_cancel_text(raw_recipient):
            user_data.pop("stars_custom_wait_recipient", None)
            user_data.pop("stars_custom_order", None)
            await message.reply_text("✅ سفارش مقدار دلخواه لغو شد؛ سفارشی ساخته نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ بازگشت به استارز", callback_data="service_stars", style="primary")]]))
            return
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw_recipient):
            await message.reply_text("❌ آیدی معتبر نیست. یک نام کاربری عمومی با @ و ۵ تا ۳۲ نویسه بفرست؛ مثال: @SeyedPro_AIA")
            return
        order = user_data.get("stars_custom_order")
        if not isinstance(order, dict) or not 50 <= int(order.get("amount") or 0) <= 1_000_000:
            user_data.pop("stars_custom_wait_recipient", None)
            await message.reply_text("اطلاعات انتخاب استارز پیدا نشد؛ از منوی استارز دوباره شروع کن.")
            return
        order["recipient"] = raw_recipient
        role = str((recipient_state or {}).get("role", "other"))
        order["recipient_user_id"] = str(user.id) if role == "self" and user else ""
        await refresh_auto_pricing(force=False)
        latest_quote = auto_pricing_quote("stars", int(order["amount"]))
        if latest_quote is not None:
            order["quote"] = latest_quote
        user_data["stars_custom_order"] = order
        user_data.pop("stars_custom_wait_recipient", None)
        chat_id = user_data.get("stars_custom_chat_id") or (update.effective_chat.id if update.effective_chat else user.id)
        message_id = user_data.get("stars_custom_message_id")
        screen_text = custom_stars_customer_text(order, order.get("quote"))
        try:
            if message_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=int(message_id), text=screen_text,
                    reply_markup=custom_stars_payment_keyboard(int(order["amount"])),
                )
            else:
                await message.reply_text(screen_text, reply_markup=custom_stars_payment_keyboard(int(order["amount"])))
        except Exception:
            logger.warning("Could not edit the original custom Stars screen after recipient entry", exc_info=True)
            await message.reply_text(screen_text, reply_markup=custom_stars_payment_keyboard(int(order["amount"])))
        return

    product_state = user_data.get("product_admin_state") or {}
    if message and message.photo and product_state.get("action") == "edit_image":
        if not user or user.id != ADMIN_ID:
            user_data.pop("product_admin_state", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return
        file_id = message.photo[-1].file_id
        with db() as con:
            cur = con.execute("UPDATE products SET image_file_id=? WHERE id=?", (file_id, int(product_state.get("product_id", 0))))
        user_data.pop("product_admin_state", None)
        await message.reply_text("✅ تصویر محصول در دیتابیس ذخیره شد." if cur.rowcount else "محصول پیدا نشد.")
        return

    if message and message.photo and product_state.get("action"):
        await message.reply_text("این مرحله منتظر متن است. عکس دریافت نشد؛ برای لغو /cancel را بزن.")
        return

    # Customer receipts: match the photo to exactly one pending order; never infer payment from a text claim.
    if message and message.photo and user and user.id != ADMIN_ID:
        pending_id = user_data.get("pending_receipt_order_id")
        with db() as con:
            if pending_id:
                pending_rows = con.execute(
                    "SELECT o.id, o.user_id, o.product_id, o.price, o.currency, o.order_details, p.name "
                    "FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                    "WHERE o.id=? AND o.user_id=? AND o.status='در انتظار ارسال رسید' LIMIT 1",
                    (int(pending_id), user.id),
                ).fetchall()
            else:
                pending_rows = con.execute(
                    "SELECT o.id, o.user_id, o.product_id, o.price, o.currency, o.order_details, p.name "
                    "FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                    "WHERE o.user_id=? AND o.status='در انتظار ارسال رسید' ORDER BY o.id DESC LIMIT 2",
                    (user.id,),
                ).fetchall()
        if len(pending_rows) != 1:
            if len(pending_rows) > 1:
                await message.reply_text("چند سفارش منتظر رسید دارید. برای جلوگیری از ثبت رسید روی سفارش اشتباه، با پشتیبانی تماس بگیرید و شماره سفارش را مشخص کنید.")
            else:
                await message.reply_text("سفارش فعالی که منتظر رسید باشد پیدا نشد. ابتدا سفارش را از فروشگاه ثبت کنید.")
            return
        order = pending_rows[0]
        file_id = message.photo[-1].file_id
        with db() as con:
            cur = con.execute(
                "UPDATE orders SET receipt_file_id=?, receipt_submitted_at=CURRENT_TIMESTAMP, status='در انتظار بررسی رسید' "
                "WHERE id=? AND user_id=? AND status='در انتظار ارسال رسید'",
                (file_id, int(order["id"]), user.id),
            )
            if cur.rowcount == 1:
                log_order_event(con, int(order["id"]), "receipt_uploaded", "در انتظار بررسی رسید", "تصویر رسید توسط مشتری ارسال شد.", user.id)
        if cur.rowcount != 1:
            await message.reply_text("وضعیت سفارش تغییر کرده است؛ رسید ثبت نشد. لطفاً از پشتیبانی پیگیری کن.")
            return
        user_data.pop("pending_receipt_order_id", None)
        admin_caption = (
            f"🧾 رسید جدید برای سفارش #{order['id']}\n"
            f"👤 کاربر: {user.full_name} (@{user.username or 'بدون‌نام'})\n"
            f"🆔 شناسه: {user.id}\n"
            f"📦 محصول: {order['name'] or ('محصول #' + str(order['product_id']))}\n"
            f"💰 مبلغ ادعاشده: {int(order['price'] or 0):,} {order['currency'] or 'تومان'}\n"
            f"📝 مشخصات سفارش: {order['order_details'] or 'ثبت نشده'}\n"
            "⚠️ رسید را دستی بررسی کنید؛ ارسال تصویر به معنی تأیید پرداخت نیست."
        )
        admin_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 جزئیات سفارش", callback_data=f"admin_order_view_{order['id']}")],
            [InlineKeyboardButton("✅ تأیید سفارش", callback_data=f"admin_order_approve_{order['id']}", style="success"),
             InlineKeyboardButton("❌ رد سفارش", callback_data=f"admin_order_reject_{order['id']}", style="danger")],
        ])
        try:
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=file_id, caption=admin_caption[:1024], reply_markup=admin_markup)
        except Exception:
            logger.exception("Could not forward receipt photo to admin for order #%s", order["id"])
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_caption, reply_markup=admin_markup)
            except Exception:
                logger.exception("Could not send receipt notification for order #%s", order["id"])
        await message.reply_text(
            f"✅ رسید سفارش #{order['id']} دریافت و برای بررسی مدیر ارسال شد.\n"
            "سفارش هنوز تأیید نشده است؛ پس از بررسی مدیر وضعیت جدید به شما اعلام می‌شود."
        )
        return

    if await _handle_public_request_input(update, context):
        return

    # Route exact menu taps before membership/input workflows consume the text.
    # Protected callbacks still enforce channel membership, and this lets non-members
    # activate the visible membership-check button in keyboard mode.
    if message and message.text and await dispatch_keyboard_menu_text(update, context):
        return

    # Admin workflows (including coupon expiry numeric replies) must not be blocked by channel checks.
    if not user or user.id != ADMIN_ID:
        if not await require_channel_membership(update, context):
            return

    # Admin coupon panel: create / activate / deactivate.
    # لغو ساخت کد تخفیف از داخل پیام متنی
    if message and message.text and message.text.strip().lower() == "/cancel":
        if user and user.id == ADMIN_ID and user_data.get("admin_coupon_state"):
            user_data.pop("admin_coupon_state", None)
            await message.reply_text("❌ ساخت کد تخفیف لغو شد.")
            return

    # افزودن دستهٔ پویا از داخل صفحهٔ دسته‌های همین کد تخفیف
    category_add_state = user_data.get("coupon_category_add_state")
    if message and message.text and category_add_state and user and user.id == ADMIN_ID:
        answer = message.text.strip()
        if answer.lower() == "/cancel":
            user_data.pop("coupon_category_add_state", None)
            scope_state = user_data.get("coupon_scope_state")
            if scope_state:
                try:
                    with db() as con:
                        ensure_discount_schema(con)
                        categories = list_coupon_categories(con)
                        row = con.execute("SELECT category FROM discount_codes WHERE code=?", (scope_state["code"],)).fetchone()
                    if row:
                        selected = coupon_selected_categories(row["category"])
                        scope_state.update(categories=categories, selected=selected)
                        await context.bot.edit_message_text(
                            chat_id=category_add_state["chat_id"],
                            message_id=category_add_state["message_id"],
                            text=coupon_scope_text(None, scope_state["code"], categories, selected),
                            reply_markup=coupon_scope_keyboard(scope_state["code"], categories, selected),
                        )
                except Exception:
                    logger.exception("Could not restore coupon category editor after cancel")
            await message.reply_text("❌ افزودن دسته لغو شد.")
            return
        if not answer or len(answer) > 60:
            await message.reply_text("❌ نام دسته باید بین ۱ تا ۶۰ نویسه باشد. دوباره بفرست یا /cancel بزن.")
            return
        try:
            with db() as con:
                ensure_discount_schema(con)
                existing = con.execute(
                    "SELECT category_key FROM coupon_categories WHERE LOWER(TRIM(display_name))=LOWER(TRIM(?)) AND active=1 LIMIT 1",
                    (answer,),
                ).fetchone()
                if existing:
                    await message.reply_text("❌ این نام دسته از قبل وجود دارد. نام دیگری وارد کن.")
                    return
                category_key = "custom_" + hashlib.sha1(answer.casefold().encode("utf-8")).hexdigest()[:14]
                con.execute(
                    "INSERT INTO coupon_categories(category_key, display_name, active, sort_order, created_at) VALUES (?, ?, 1, 999, ?)",
                    (category_key, answer, utc_now().isoformat(timespec="seconds")),
                )
                categories = list_coupon_categories(con)
                scope_state = user_data.get("coupon_scope_state")
                if scope_state:
                    row = con.execute("SELECT category FROM discount_codes WHERE code=?", (scope_state["code"],)).fetchone()
                    selected = coupon_selected_categories(row["category"]) if row else set()
                else:
                    selected = set()
            user_data.pop("coupon_category_add_state", None)
            if scope_state:
                scope_state.update(categories=categories, selected=selected)
                await context.bot.edit_message_text(
                    chat_id=category_add_state["chat_id"],
                    message_id=category_add_state["message_id"],
                    text=coupon_scope_text(None, scope_state["code"], categories, selected),
                    reply_markup=coupon_scope_keyboard(scope_state["code"], categories, selected),
                )
            await message.reply_text("✅ دستهٔ جدید اضافه شد و در فهرست دسته‌های تخفیف قرار گرفت.")
        except Exception:
            logger.exception("Adding dynamic coupon category failed")
            await message.reply_text("❌ افزودن دسته انجام نشد. اطلاعات قبلی حفظ شد؛ دوباره تلاش کن یا /cancel بزن.")
        return

    coupon_edit_state = user_data.get("coupon_edit_state")
    if message and message.text and coupon_edit_state and user and user.id == ADMIN_ID:
        answer = normalize_ascii_digits(message.text.strip())
        code = str(coupon_edit_state["code"])
        field = coupon_edit_state["field"]
        if answer.lower() == "/cancel":
            user_data.pop("coupon_edit_state", None)
            await message.reply_text(
                "❌ ویرایش لغو شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + code)]])
            )
            return

        success_text = None
        try:
            with db() as con:
                ensure_discount_schema(con)
                row = con.execute("SELECT * FROM discount_codes WHERE code=?", (code,)).fetchone()
                if not row:
                    raise LookupError("coupon not found")

                if field == "code":
                    new_code = answer.upper()
                    if not re.fullmatch(r"[A-Z0-9]{1,36}", new_code):
                        await message.reply_text("❌ کد باید فقط از حروف انگلیسی و اعداد تشکیل شود (حداکثر ۳۶ نویسه).")
                        return
                    if con.execute("SELECT 1 FROM discount_codes WHERE UPPER(code)=? AND code<>?", (new_code, code)).fetchone():
                        await message.reply_text("❌ این کد قبلاً ثبت شده است. کد دیگری وارد کن.")
                        return
                    con.execute("UPDATE discount_codes SET code=? WHERE code=?", (new_code, code))
                    con.execute("UPDATE coupon_usage SET code=? WHERE code=?", (new_code, code))
                    # Keep the original code text on historical orders for audit; usage rows follow the renamed coupon.
                    code = new_code
                    success_text = f"✅ کد به {new_code} تغییر یافت."

                elif field in ("amount_type_input", "amount"):
                    discount_type = coupon_edit_state.get("discount_type", row["discount_type"] or "fixed")
                    amount_text = answer.replace(",", "").replace("٬", "").replace("،", "")
                    if not re.fullmatch(r"[0-9]+", amount_text):
                        await message.reply_text("❌ فقط عدد صحیح وارد کن.")
                        return
                    value = int(amount_text)
                    if value <= 0 or (discount_type == "percent" and value > 100):
                        await message.reply_text("❌ درصد باید بین ۱ تا ۱۰۰ یا مبلغ ثابت باید عددی مثبت باشد.")
                        return
                    con.execute("UPDATE discount_codes SET discount_amount=?, discount_type=? WHERE code=?", (value, discount_type, code))
                    success_text = f"✅ تخفیف به {value}% تغییر یافت." if discount_type == "percent" else f"✅ تخفیف به {value:,} تومان تغییر یافت."

                elif field in ("userlimit", "per_user_limit"):
                    if not re.fullmatch(r"[0-9]+", answer):
                        await message.reply_text("❌ عدد صفر یا یک عدد صحیح مثبت وارد کن.")
                        return
                    value = int(answer)
                    if value > 2_147_483_647:
                        await message.reply_text("❌ عدد واردشده بیش از حد بزرگ است.")
                        return
                    con.execute("UPDATE discount_codes SET per_user_limit=?, used_by=NULL WHERE code=?", (value or None, code))
                    success_text = f"✅ حداکثر تعداد استفادهٔ هر کاربر به {'نامحدود' if value == 0 else value} تغییر یافت."

                elif field == "max":
                    # Compatibility with an old open admin keyboard: this remains the global cap.
                    if not re.fullmatch(r"[0-9]+", answer):
                        await message.reply_text("❌ عدد صفر یا یک عدد صحیح مثبت وارد کن.")
                        return
                    value = int(answer)
                    if value > 2_147_483_647:
                        await message.reply_text("❌ عدد واردشده بیش از حد بزرگ است.")
                        return
                    con.execute("UPDATE discount_codes SET max_uses=? WHERE code=?", (value or None, code))
                    success_text = f"✅ سقف مصرف کل به {'نامحدود' if value == 0 else value} تغییر یافت."

                elif field == "min":
                    amount_text = answer.replace(",", "").replace("٬", "").replace("،", "")
                    if not re.fullmatch(r"[0-9]+", amount_text):
                        await message.reply_text("❌ مبلغ را فقط با عدد صحیح و به تومان وارد کن.")
                        return
                    value = int(amount_text)
                    con.execute("UPDATE discount_codes SET min_order_amount=? WHERE code=?", (value, code))
                    success_text = "✅ محدودیت حداقل مبلغ خرید برداشته شد." if value == 0 else f"✅ حداقل مبلغ خرید به {value:,} تومان تغییر یافت."

                elif field == "recipient":
                    if not re.fullmatch(r"[0-9]{1,20}", answer):
                        await message.reply_text("❌ آی‌دی عددی تلگرام را وارد کن؛ برای برداشتن محدودیت عدد 0 را بفرست.")
                        return
                    value = int(answer)
                    if value == 0:
                        con.execute("UPDATE discount_codes SET recipient_type='all', recipient_ids='[]' WHERE code=?", (code,))
                        success_text = "✅ محدودیت اختصاص کد به کاربر برداشته شد."
                    else:
                        con.execute("UPDATE discount_codes SET recipient_type='selected', recipient_ids=? WHERE code=?", (json.dumps([str(value)]), code))
                        success_text = f"✅ کد تخفیف فقط به کاربر با آیدی {value} اختصاص یافت."

                elif field == "expiry_date":
                    match = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", answer)
                    if not match:
                        await message.reply_text("❌ تاریخ شمسی معتبر وارد کن. فرمت: YYYY-MM-DD؛ مثال: 1405-08-15")
                        return
                    try:
                        date_start = jalali_to_gregorian(*map(int, match.groups()))
                    except (ValueError, OverflowError):
                        await message.reply_text("❌ تاریخ شمسی واقعی و معتبر وارد کن؛ مثال: 1405-08-15")
                        return
                    clock_value = str(row["expires_local_time"] or "").strip()
                    if clock_value:
                        hour, minute = map(int, clock_value.split(":"))
                        expiry_local = date_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    else:
                        expiry_local = date_start + timedelta(days=1)
                    expiry_utc = expiry_local.astimezone(timezone.utc)
                    if expiry_utc <= utc_now():
                        await message.reply_text("❌ تاریخ/ساعت انقضا باید در آینده باشد. تاریخ جدیدی وارد کن.")
                        return
                    con.execute(
                        "UPDATE discount_codes SET expires_at=?, expiry_label=?, expires_jalali_date=?, expires_local_time=? WHERE code=?",
                        (expiry_utc.isoformat(timespec="seconds"), answer, answer, clock_value or None, code),
                    )
                    success_text = f"✅ تاریخ انقضای کد به {answer} تغییر یافت."

                elif field == "expiry_time":
                    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", answer):
                        await message.reply_text("❌ ساعت معتبر وارد کن. فرمت: HH:MM؛ مثال: 20:30")
                        return
                    jalali_date = str(row["expires_jalali_date"] or "").strip()
                    expiry_label = str(row["expiry_label"] or "").strip()
                    if not jalali_date and re.fullmatch(r"[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}", expiry_label):
                        jalali_date = expiry_label.replace("/", "-")
                    if not jalali_date and row["expires_at"]:
                        parsed_expiry = parse_expiry(row["expires_at"])
                        if parsed_expiry:
                            local_date = parsed_expiry.astimezone(IRAN_TZ)
                            jy, jm, jd = gregorian_to_jalali(local_date.year, local_date.month, local_date.day)
                            jalali_date = f"{jy:04d}-{jm:02d}-{jd:02d}"
                    if jalali_date:
                        expiry_date_match = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", jalali_date)
                        if not expiry_date_match:
                            raise ValueError("stored Jalali date invalid")
                        date_start = jalali_to_gregorian(*map(int, expiry_date_match.groups()))
                        hour, minute = map(int, answer.split(":"))
                        expiry_local = date_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        expiry_utc = expiry_local.astimezone(timezone.utc)
                        if expiry_utc <= utc_now():
                            await message.reply_text("❌ این ساعت برای تاریخ انقضای ذخیره‌شده در گذشته است. ابتدا تاریخ آینده‌ای تعیین کن یا ساعت دیگری بفرست.")
                            return
                        con.execute(
                            "UPDATE discount_codes SET expires_at=?, expiry_label=?, expires_jalali_date=?, expires_local_time=? WHERE code=?",
                            (expiry_utc.isoformat(timespec="seconds"), jalali_date, jalali_date, answer, code),
                        )
                    else:
                        # Store the time independently; the actual expiry begins once a date is selected.
                        con.execute("UPDATE discount_codes SET expires_at=NULL, expiry_label=NULL, expires_local_time=? WHERE code=?", (answer, code))
                    success_text = f"✅ ساعت انقضای کد به {answer} تغییر یافت."

                else:
                    raise ValueError("unsupported field")

            user_data.pop("coupon_edit_state", None)
            await message.reply_text(
                success_text or "✅ تغییرات ذخیره شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 مشاهده کد", callback_data="admin_coupon_view_" + code)]])
            )
        except LookupError:
            user_data.pop("coupon_edit_state", None)
            await message.reply_text("❌ کد موردنظر پیدا نشد.")
        except Exception:
            logger.exception("Coupon edit failed: code=%s field=%s", code, field)
            await message.reply_text("❌ تغییر ذخیره نشد. مقدار را بررسی کن و دوباره تلاش کن؛ جزئیات در لاگ ثبت شد.")
        return

    admin_state = user_data.get("admin_coupon_state")
    if message and message.text and admin_state and user and user.id == ADMIN_ID:
        answer = normalize_ascii_digits(message.text.strip())
        if answer.lower() == "/cancel":
            user_data.pop("admin_coupon_state", None)
            await message.reply_text("❌ عملیات لغو شد.")
            return

        step = admin_state.get("step")

        if step == "code":
            code = answer.upper()
            if not re.fullmatch(r"[A-Z0-9]{1,36}", code):
                await message.reply_text(
                    "❌ کد معتبر نیست. فقط حروف انگلیسی و عدد، بدون فاصله یا نماد، وارد کن (حداکثر ۳۶ نویسه).\n"
                    "مثال: SEYED500\nبرای لغو /cancel را بفرست."
                )
                return
            try:
                with db() as con:
                    ensure_discount_schema(con)
                    duplicate = con.execute(
                        "SELECT 1 FROM discount_codes WHERE UPPER(code)=? LIMIT 1", (code,)
                    ).fetchone()
            except Exception:
                logger.exception("Could not check duplicate coupon code")
                await message.reply_text("❌ بررسی کد در دیتابیس انجام نشد؛ خطا ثبت شد. دوباره تلاش کن یا /cancel بزن.")
                return
            if duplicate:
                await message.reply_text("❌ این کد قبلاً ثبت شده است. کد دیگری بفرست. برای لغو /cancel را بفرست.")
                return
            admin_state.update(code=code, step="discount_type")
            await message.reply_text(
                f"✅ کد: {code}\n\nنوع تخفیف را انتخاب کن:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💯 درصدی (مثلاً 20٪)", callback_data="admin_coupon_type_percent")],
                    [InlineKeyboardButton("💵 مبلغ ثابت (تومان)", callback_data="admin_coupon_type_fixed")],
                    [InlineKeyboardButton("❌ لغو", callback_data="admin_coupon_creation_cancel")],
                ])
            )
            return

        if step == "amount":
            try:
                if not re.fullmatch(r"[0-9][0-9,٬]*", answer):
                    raise ValueError
                amount = int(answer.replace(",", "").replace("٬", ""))
                if amount <= 0:
                    raise ValueError
                if admin_state.get("discount_type") == "percent" and amount > 100:
                    raise ValueError
            except ValueError:
                if admin_state.get("discount_type") == "percent":
                    await message.reply_text("❌ درصد تخفیف باید عدد صحیح بین ۱ تا ۱۰۰ باشد. مثال: 20")
                else:
                    await message.reply_text("❌ مبلغ تخفیف باید عدد صحیح مثبت به تومان باشد. مثال: 50000")
                return
            admin_state["discount_amount"] = amount
            admin_state["step"] = "per_user"
            await message.reply_text(
                "🔢 حداکثر تعداد استفادهٔ هر کاربر را وارد کن:\n"
                "مثال: 1 یعنی هر کاربر فقط یک‌بار؛ 0 = نامحدود\n"
                "برای لغو /cancel را بفرست."
            )
            return

        if step == "per_user":
            try:
                if not re.fullmatch(r"[0-9]+", answer):
                    raise ValueError
                n = int(answer)
                if n < 0 or n > 2_147_483_647:
                    raise ValueError
            except ValueError:
                await message.reply_text("❌ صفر یا عدد صحیح مثبت وارد کن. مثال: 1؛ برای نامحدود 0")
                return
            admin_state["per_user_limit"] = n or None
            admin_state["step"] = "max_uses"
            await message.reply_text(
                "🔢 سقف مصرف کل را بفرست. 0 = نامحدود\n"
                "مثال: 10 یعنی مجموعاً فقط ۱۰ بار استفاده\n"
                "برای لغو /cancel را بفرست."
            )
            return

        if step == "max_uses":
            try:
                if not re.fullmatch(r"[0-9]+", answer):
                    raise ValueError
                n = int(answer)
                if n < 0 or n > 2_147_483_647:
                    raise ValueError
            except ValueError:
                await message.reply_text("❌ صفر یا عدد صحیح مثبت وارد کن. مثال: 10؛ برای نامحدود 0")
                return
            admin_state["max_uses"] = n or None
            admin_state["step"] = "expiry_choice"
            await message.reply_text(
                "📅 تاریخ انقضا (اختیاری) را به شمسی وارد کن\n"
                "فرمت: YYYY-MM-DD\n"
                "مثال: 1405-08-07\n\n"
                "⏱️ برای انقضای ساعتی، گزینهٔ مدت اعتبار بر اساس ساعت را بزن و تعداد ساعت را وارد کن.\n"
                "مثال: 6 یعنی دقیقاً ۶ ساعت پس از ذخیرهٔ کد.\n"
                "♾️ بدون تاریخ انقضا یعنی بدون محدودیت زمانی.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏱️ مدت اعتبار بر اساس ساعت", callback_data="admin_coupon_expiry_hours")],
                    [InlineKeyboardButton("📅 تاریخ شمسی", callback_data="admin_coupon_expiry_date")],
                    [InlineKeyboardButton("♾️ بدون تاریخ انقضا", callback_data="admin_coupon_expiry_never")],
                    [InlineKeyboardButton("❌ لغو", callback_data="admin_coupon_creation_cancel")],
                ])
            )
            return

        if step == "expiry_date":
            try:
                match = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", answer)
                if not match:
                    raise ValueError("format")
                jy, jm, jd = map(int, match.groups())
                expiry_start_iran = jalali_to_gregorian(jy, jm, jd)
                expiry_end_iran = expiry_start_iran + timedelta(days=1)
                expiry_utc = expiry_end_iran.astimezone(timezone.utc)
                if expiry_utc <= utc_now():
                    raise ValueError("past")
                admin_state["expires_at"] = expiry_utc.isoformat(timespec="seconds")
                admin_state.pop("expiry_duration_seconds", None)
                admin_state["expiry_label"] = answer
                record = save_coupon_from_admin_state(admin_state, user.id)
            except sqlite3.IntegrityError:
                logger.exception("Coupon insert conflict during date expiry save")
                await message.reply_text("❌ این کد قبلاً ثبت شده است. عملیات لغو شد؛ کد دیگری بساز.")
                user_data.pop("admin_coupon_state", None)
                return
            except (ValueError, OverflowError):
                await message.reply_text("❌ تاریخ شمسی معتبر و آینده وارد کن. فرمت: YYYY-MM-DD؛ مثال: 1405-08-07")
                return
            except Exception:
                logger.exception("Coupon save failed after Jalali expiry selection")
                await message.reply_text("❌ ذخیرهٔ کد انجام نشد؛ جزئیات خطا در لاگ ثبت شد. اطلاعات مرحله حفظ شده است؛ دوباره تلاش کن یا /cancel بزن.")
                return
            user_data.pop("admin_coupon_state", None)
            await message.reply_text(
                coupon_created_message(record),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 بازگشت به لیست کدها", callback_data="admin_coupons")]])
            )
            return

        if step == "expiry_hours":
            try:
                if not re.fullmatch(r"[0-9]+", answer):
                    raise ValueError
                hours = int(answer)
                if not 1 <= hours <= 876000:
                    raise ValueError
                admin_state["expiry_duration_seconds"] = hours * 3600
                admin_state["expires_at"] = None
                admin_state["expiry_label"] = f"{hours} ساعت"
                record = save_coupon_from_admin_state(admin_state, user.id)
            except sqlite3.IntegrityError:
                logger.exception("Coupon insert conflict during hours expiry save")
                await message.reply_text("❌ این کد قبلاً ثبت شده است. عملیات لغو شد؛ کد دیگری بساز.")
                user_data.pop("admin_coupon_state", None)
                return
            except ValueError:
                await message.reply_text("❌ تعداد ساعت باید عدد صحیح بین ۱ تا ۸۷۶۰۰۰ باشد. مثال: 6؛ برای لغو /cancel را بفرست.")
                return
            except Exception:
                logger.exception("Coupon save failed after hourly expiry selection")
                await message.reply_text("❌ ذخیرهٔ کد انجام نشد؛ جزئیات خطا در لاگ ثبت شد. اطلاعات مرحله حفظ شده است؛ دوباره تلاش کن یا /cancel بزن.")
                return
            user_data.pop("admin_coupon_state", None)
            await message.reply_text(
                coupon_created_message(record),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 بازگشت به لیست کدها", callback_data="admin_coupons")]])
            )
            return

    # Generic product coupon input: applies to catalog products beyond Stars.
    pending_product_coupon_input = user_data.get("pending_product_coupon_input")
    if message and message.text and pending_product_coupon_input is not None and user:
        answer_code = message.text.strip().upper()
        if answer_code.lower() == "/cancel":
            user_data.pop("pending_product_coupon_input", None)
            user_data.pop("pending_product_coupon", None)
            await message.reply_text("❌ ورود کد تخفیف لغو شد. اطلاعات محصول را برای ثبت درخواست ارسال کن.")
            return
        product_id_for_coupon = int(pending_product_coupon_input)
        try:
            with db() as con:
                ensure_discount_schema(con)
                product_for_coupon = con.execute(
                    "SELECT id, name, category, price, active FROM products WHERE id=?",
                    (product_id_for_coupon,)
                ).fetchone()
                if not product_for_coupon or not int(product_for_coupon["active"] or 0):
                    validation = {"ok": False, "reason": "❌ این محصول دیگر فعال نیست."}
                else:
                    validation = validate_coupon_for_order(
                        con, answer_code, user.id, int(product_for_coupon["price"] or 0),
                        product_for_coupon["category"], product_for_coupon["name"], user.username
                    )
        except Exception:
            logger.exception("Generic product coupon preview failed: user_id=%s product_id=%s", user.id, product_id_for_coupon)
            await message.reply_text("⚠️ بررسی کد تخفیف انجام نشد؛ جزئیات در لاگ ثبت شد. دوباره تلاش کن.")
            return
        if not validation.get("ok"):
            await message.reply_text(
                validation.get("reason", "❌ کد تخفیف معتبر نیست.") + "\n\nکد دیگری بفرست یا /cancel را برای لغو بزن.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو کد تخفیف", callback_data="product_coupon_cancel")]])
            )
            return
        user_data["pending_product_coupon"] = {
            "product_id": product_id_for_coupon, "code": validation["code"]
        }
        user_data.pop("pending_product_coupon_input", None)
        final_price = int(validation["final_price"])
        original_price = int(product_for_coupon["price"] or 0)
        discount = int(validation["discount"])
        await message.reply_text(
            "✅ کد تخفیف پذیرفته شد. هنگام ثبت سفارش دوباره بررسی می‌شود.\n\n"
            f"🏷️ کد: {validation['code']}\n"
            f"💰 قیمت اصلی: {original_price:,} تومان\n"
            f"🎁 میزان تخفیف: {discount:,} تومان\n"
            f"💵 مبلغ پس از تخفیف: {final_price:,} تومان\n\n"
            "حالا اطلاعات موردنیاز محصول را بفرست تا درخواست سفارش ثبت شود."
        )
        return

    # SEYED_STARS_DISCOUNT_V1: preview is advisory; payment re-validates inside an immediate transaction.
    pending_coupon = user_data.get("pending_stars_coupon")
    if message and message.text and pending_coupon and user:
        code = message.text.strip().upper()
        amount = int(pending_coupon.get("amount", 0))
        original_price = int(pending_coupon.get("price", 0))
        try:
            with db() as con:
                validation = validate_coupon_for_order(
                    con, code, user.id, original_price, "stars", f"استارز {amount} عدد", user.username
                )
            if not validation.get("ok"):
                await message.reply_text(
                    validation.get("reason", "❌ کد تخفیف نامعتبر است.") + "\n\nدوباره تلاش کنید یا روی «رد شدن» بزنید:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔁 تلاش دوباره", callback_data=f"stars_discount_{amount}"),
                        InlineKeyboardButton("رد شدن", callback_data=f"stars_discount_skip_{amount}")
                    ]])
                )
                return
            discount = int(validation["discount"])
            user_data["stars_coupon"] = {
                "amount": amount, "code": validation["code"], "discount": discount,
                "final_price": int(validation["final_price"])
            }
            user_data.pop("pending_stars_coupon", None)
            await message.reply_text(
                "🎉 کد تخفیف با موفقیت اعمال شد!\n\n━━━━━━━━━━━━━━━━━━\n"
                f"💰 قیمت اصلی: {original_price:,} تومان\n"
                f"🏷️ تخفیف: {discount:,} تومان\n"
                f"💵 مبلغ نهایی: {int(validation['final_price']):,} تومان\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "✅ تخفیف برای همین بسته ثبت شد. هنگام پرداخت دوباره اعتبارسنجی می‌شود.\n\n"
                "👇 این استارز رو برای چه کسی می‌خوای؟",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 برای خودم", callback_data=f"stars_recipient_self_{amount}", style="success")],
                    [InlineKeyboardButton("🎁 برای شخص دیگر", callback_data=f"stars_recipient_other_{amount}", style="primary")],
                    [InlineKeyboardButton("❌ لغو خرید", callback_data="stars_detail_back", style="danger")]
                ])
            )
        except Exception:
            logger.exception("Stars coupon validation failed: user_id=%s", user.id)
            await message.reply_text("⚠️ بررسی کد تخفیف انجام نشد؛ جزئیات خطا در لاگ ثبت شد. دوباره تلاش کن.")
        return

    # UI_ADMIN_PATCH_V1 message input
    ui_st = user_data.get("uiadm_state")
    if message and message.text and ui_st:
        if not user or user.id != ADMIN_ID:
            user_data.pop("uiadm_state", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return
        value = message.text.strip()
        if value == "/cancel":
            user_data.pop("uiadm_state", None)
            await message.reply_text("عملیات لغو شد.")
            return
        kind = ui_st.get("kind")
        if kind == "text":
            key = ui_st["key"]
            settings = load_bot_settings()
            settings[key] = value
            save_bot_settings(settings)
            user_data.pop("uiadm_state", None)
            await message.reply_text("✅ متن ذخیره شد.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("مدیریت متن‌ها و دکمه‌ها", callback_data="uiadm_home")]
            ]))
            return
        if kind == "callback":
            allowed_exact = {
                "home", "nav_back", "back_to_services", "service_stars", "service_premium",
                "service_gifts", "service_boost", "service_members", "service_reactions",
                "service_views", "service_instagram_followers", "service_instagram_likes",
                "cat_stars", "cat_numbers", "wallet", "topup", "orders", "account",
                "support", "free_stars", "config_menu", "mediator_request", "ton_buy",
                "ton_gram", "faq", "invite", "stars_custom", "stars_detail_back",
                "check_membership", "admin", "service_menu", "nft_market", "mini_app",
            }
            valid = value in allowed_exact
            if not valid and value.startswith("buy_") and value[4:].isdigit():
                with db() as con:
                    valid = bool(con.execute("SELECT 1 FROM products WHERE id=?", (int(value[4:]),)).fetchone())
            if not valid and value.startswith(("cat_", "service_")):
                target = value.split("_", 1)[1]
                if target in {"stars", "premium", "gifts", "boost", "members", "reactions", "views", "instagram_followers", "instagram_likes", "numbers", "config", "configs"}:
                    valid = True
                else:
                    with db() as con:
                        valid = bool(con.execute("SELECT 1 FROM products WHERE category=? LIMIT 1", (target,)).fetchone())
            if not valid:
                await message.reply_text("❌ این عملکرد در ربات تعریف نشده یا مقصد آن معتبر نیست. برای جلوگیری از دکمه خراب، یک عملکرد موجود وارد کن؛ برای لغو /cancel.")
                return
            cfg = ui_load()
            items = cfg.get("menus", {}).get(ui_st["menu"], [])
            item = next((x for x in items if str(x.get("id")) == str(ui_st["id"])), None)
            if not item:
                user_data.pop("uiadm_state", None)
                await message.reply_text("دکمه پیدا نشد؛ عملیات بسته شد.")
                return
            item["callback"] = value
            ui_save(cfg)
            user_data["uiadm_state"] = {"kind": "button", "menu": ui_st["menu"], "id": ui_st["id"]}
            await message.reply_text("✅ عملکرد ذخیره شد. فقط مقصدهایی که در کد پشتیبانی می‌شوند فعال خواهند بود.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بازگشت به ویرایش دکمه", callback_data=f"uiadm_item_{ui_st['menu']}_{ui_st['id']}")],
                [InlineKeyboardButton("مدیریت منو", callback_data=f"uiadm_menu_{ui_st['menu']}")],
            ]))
            return

        if kind == "rename":
            cfg = ui_load()
            items = cfg.get("menus", {}).get(ui_st["menu"], [])
            item = next((x for x in items if x.get("id") == ui_st["id"]), None)
            if item:
                item["text"] = value[:64]
                ui_save(cfg)
            user_data["uiadm_state"] = {"kind": "button", "menu": ui_st["menu"], "id": ui_st["id"]}
            await message.reply_text("✅ نام ذخیره شد.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بازگشت به منو", callback_data=f"uiadm_menu_{ui_st['menu']}")]
            ]))
            return
        if kind == "add":
            step = ui_st.get("step")
            if step == "id":
                ui_st["id"] = value
                ui_st["step"] = "text"
                user_data["uiadm_state"] = ui_st
                await message.reply_text("متن نمایشی دکمه را بفرست:")
                return
            if step == "text":
                ui_st["text"] = value[:64]
                ui_st["step"] = "callback"
                user_data["uiadm_state"] = ui_st
                await message.reply_text("شناسهٔ عملکرد موجود را دقیق بفرست؛ مثلاً home یا service_stars:")
                return
            if step == "callback":
                allowed_exact = {
                    "home", "nav_back", "back_to_services", "service_stars", "service_premium",
                    "service_gifts", "service_boost", "service_members", "service_reactions",
                    "service_views", "service_instagram_followers", "service_instagram_likes",
                    "cat_stars", "cat_numbers", "wallet", "topup", "orders", "account",
                    "support", "free_stars", "config_menu", "mediator_request", "ton_buy",
                    "ton_gram", "faq", "invite", "stars_custom", "stars_detail_back",
                    "check_membership", "admin", "nft_market", "mini_app",
                }
                valid_callback = value in allowed_exact
                if not valid_callback and value.startswith("buy_") and value[4:].isdigit():
                    with db() as con:
                        valid_callback = bool(con.execute("SELECT 1 FROM products WHERE id=?", (int(value[4:]),)).fetchone())
                if not valid_callback and value.startswith(("cat_", "service_")):
                    target = value.split("_", 1)[1]
                    if target in {"stars", "premium", "gifts", "boost", "members", "reactions", "views", "instagram_followers", "instagram_likes", "numbers", "config", "configs"}:
                        valid_callback = True
                    else:
                        with db() as con:
                            valid_callback = bool(con.execute("SELECT 1 FROM products WHERE category=? LIMIT 1", (target,)).fetchone())
                if not valid_callback:
                    await message.reply_text("❌ عملکرد در کد ربات تعریف نشده یا مقصد معتبر نیست. یک عملکرد موجود وارد کن؛ برای لغو /cancel را بفرست.")
                    return
                cfg = ui_load()
                menu = ui_st["menu"]
                items = cfg.setdefault("menus", {}).setdefault(menu, [])
                if not ui_st.get("id") or any(x.get("id") == ui_st["id"] for x in items):
                    await message.reply_text("❌ شناسه خالی است یا تکراری است. /cancel برای لغو")
                    return
                try:
                    new_row = max((int(item.get("row", 0) or 0) for item in items), default=0) + 1
                except (TypeError, ValueError):
                    new_row = len(items) + 1
                items.append({
                    "id": ui_st["id"], "text": ui_st["text"], "callback": value,
                    "enabled": True, "row": new_row,
                    "button_style": _button_style_for(ui_st["text"], value) or "primary",
                })
                ui_save(cfg)
                user_data.pop("uiadm_state", None)
                await message.reply_text("✅ دکمه ذخیره شد و در منو فعال است. رنگ و ترتیب آن هم از همین پنل قابل ویرایش است.")
                return


    if message and message.text and user_data.get("product_admin_state"):
        if not user or user.id != ADMIN_ID:
            user_data.pop("product_admin_state", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return

        value = message.text.strip()
        if value == "/cancel":
            user_data.pop("product_admin_state", None)
            back = user_data.get("auto_pricing_return") or "admin_products"
            await message.reply_text("عملیات لغو شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ بازگشت به مدیریت محصول", callback_data=back)]]))
            return

        state = user_data.get("product_admin_state", {})
        action = state.get("action")

        if action == "edit_image":
            await message.reply_text("برای این مرحله باید عکس محصول را ارسال کنی؛ یا /cancel را بزن.")
            return

        if action == "add_name":
            if not value or len(value) > 150:
                await message.reply_text("نام محصول باید بین 1 تا 150 نویسه باشد. دوباره بفرست:")
                return
            state["name"] = value
            if str(state.get("category", "")).lower() == "stars":
                state["action"] = "add_star_amount"
                user_data["product_admin_state"] = state
                await message.reply_text("مقدار دقیق این بسته را به عدد بفرست؛ مثال 750 یا 1500:")
            else:
                state["action"] = "add_price"
                user_data["product_admin_state"] = state
                await message.reply_text("قیمت محصول را فقط به عدد و به تومان بفرست. برای قیمت نامشخص عدد 0 وارد کن:")
            return

        if action == "add_star_amount":
            try:
                amount = int(normalize_ascii_digits(value).replace(",", "").replace("٬", "").replace("،", "").replace(" ", ""))
                if amount <= 0 or amount > 1_000_000_000:
                    raise ValueError
            except ValueError:
                await message.reply_text("مقدار نامعتبر است. عدد صحیح مثبت بفرست:")
                return
            with db() as con:
                exists = con.execute("SELECT 1 FROM products WHERE category='stars' AND star_amount=?", (amount,)).fetchone()
            if exists:
                await message.reply_text("این مقدار استارز از قبل وجود دارد. مقدار دیگری وارد کن یا /cancel بزن:")
                return
            state["star_amount"] = amount
            state["action"] = "add_price"
            user_data["product_admin_state"] = state
            await message.reply_text("حالا قیمت بسته را به تومان وارد کن؛ برای قیمت نامشخص ۰:")
            return

        if action == "add_price":
            try:
                price = int(value.replace(",", "").replace("٬", "").replace(" ", ""))
                if price < 0 or price > 2_000_000_000:
                    raise ValueError
            except ValueError:
                await message.reply_text("قیمت نامعتبر است. یک عدد صحیح بین 0 تا 2,000,000,000 وارد کن:")
                return
            with db() as con:
                category = str(state["category"])
                star_amount = state.get("star_amount") if category.lower() == "stars" else None
                max_order = int(con.execute("SELECT COALESCE(MAX(sort_order), 0) FROM products WHERE category=?", (category,)).fetchone()[0] or 0)
                star_styles = {50: "success", 75: "primary", 100: "danger", 150: "success", 250: "primary", 350: "danger", 500: "success", 1000: "primary", 2500: "danger", 5000: "success", 10000: "primary", 50000: "danger"}
                style = star_styles.get(int(star_amount), "primary") if star_amount else "success"
                cur = con.execute(
                    "INSERT INTO products (category, name, price, active, icon, button_style, sort_order, star_amount) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                    (category, state["name"], price, "✧" if category.lower() == "stars" else "", style, max_order + 10, star_amount)
                )
                new_id = cur.lastrowid
            user_data.pop("product_admin_state", None)
            await message.reply_text(f"✅ محصول با شناسه #{new_id} ثبت شد.\nنام: {state['name']}\nقیمت: {price:,} تومان\nدسته: {state['category']}\n\nبرای مدیریت، دستور /admin را بزن.")
            return

        if action == "edit_name":
            if not value or len(value) > 150:
                await message.reply_text("نام محصول باید بین 1 تا 150 نویسه باشد. دوباره بفرست:")
                return
            with db() as con:
                cur = con.execute("UPDATE products SET name=? WHERE id=?", (value, int(state["product_id"])))
            user_data.pop("product_admin_state", None)
            back = user_data.get("auto_pricing_return") or "admin_products"
            await message.reply_text("✅ نام محصول ویرایش شد." if cur.rowcount else "محصول پیدا نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ بازگشت به مدیریت محصول", callback_data=back)]]))
            return

        if action == "edit_price":
            try:
                price = int(value.replace(",", "").replace("٬", "").replace(" ", ""))
                if price < 0 or price > 2_000_000_000:
                    raise ValueError
            except ValueError:
                await message.reply_text("قیمت نامعتبر است. یک عدد صحیح بین 0 تا 2,000,000,000 وارد کن:")
                return
            with db() as con:
                cur = con.execute("UPDATE products SET price=? WHERE id=?", (price, int(state["product_id"])))
            user_data.pop("product_admin_state", None)
            back = user_data.get("auto_pricing_return") or "admin_products"
            await message.reply_text("✅ قیمت محصول ویرایش شد." if cur.rowcount else "محصول پیدا نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ بازگشت به مدیریت محصول", callback_data=back)]]))
            return

        pid = int(state.get("product_id", 0) or 0)
        clear = value.strip().casefold() in {"/clear", "پاک", "پاک کردن", "حذف"}
        if action == "edit_button_text":
            if not clear and len(value) > 64:
                await message.reply_text("متن دکمه حداکثر ۶۴ نویسه است:")
                return
            new_value = "" if clear else value
            with db() as con:
                cur = con.execute("UPDATE products SET button_text=? WHERE id=?", (new_value, pid))
        elif action == "edit_icon":
            if not clear and len(value) > 16:
                await message.reply_text("نماد باید حداکثر ۱۶ نویسه باشد:")
                return
            new_value = "" if clear else value
            with db() as con:
                cur = con.execute("UPDATE products SET icon=? WHERE id=?", (new_value, pid))
        elif action == "edit_sort_order":
            try:
                number = int(normalize_ascii_digits(value.replace(",", "").replace("٬", "").strip()))
                if not -1_000_000 <= number <= 1_000_000:
                    raise ValueError
            except ValueError:
                await message.reply_text("ترتیب باید عدد صحیح بین ‎-۱۰۰۰۰۰۰ تا ۱۰۰۰۰۰۰ باشد:")
                return
            with db() as con:
                cur = con.execute("UPDATE products SET sort_order=? WHERE id=?", (number, pid))
        elif action == "edit_description":
            if not clear and len(value) > 1200:
                await message.reply_text("توضیحات حداکثر ۱۲۰۰ نویسه است:")
                return
            with db() as con:
                cur = con.execute("UPDATE products SET description=? WHERE id=?", ("" if clear else value, pid))
        elif action == "edit_star_amount":
            try:
                number = int(normalize_ascii_digits(value.replace(",", "").replace("٬", "").replace("،", "").strip()))
                if number < 0 or number > 1_000_000_000:
                    raise ValueError
            except ValueError:
                await message.reply_text("مقدار باید عدد صحیح از ۰ تا ۱٬۰۰۰٬۰۰۰٬۰۰۰ باشد:")
                return
            with db() as con:
                if number > 0:
                    clash = con.execute("SELECT 1 FROM products WHERE category='stars' AND star_amount=? AND id<>?", (number, pid)).fetchone()
                    if clash:
                        await message.reply_text("این مقدار استارز برای محصول دیگری ثبت شده است. مقدار دیگری وارد کن:")
                        return
                cur = con.execute("UPDATE products SET star_amount=? WHERE id=?", (number or None, pid))
        elif action == "edit_currency":
            if not value or len(value) > 16:
                await message.reply_text("واحد پول باید بین ۱ تا ۱۶ نویسه باشد:")
                return
            with db() as con:
                cur = con.execute("UPDATE products SET currency=? WHERE id=?", (value, pid))
        elif action == "edit_old_price":
            try:
                number = int(normalize_ascii_digits(value.replace(",", "").replace("٬", "").replace(" ", "")))
                if number < 0 or number > 2_000_000_000:
                    raise ValueError
            except ValueError:
                await message.reply_text("قیمت قبلی باید عدد صحیح بین ۰ تا ۲٬۰۰۰٬۰۰۰٬۰۰۰ باشد:")
                return
            with db() as con:
                cur = con.execute("UPDATE products SET old_price=? WHERE id=?", (number, pid))
        else:
            cur = None

        if action in {"edit_button_text", "edit_icon", "edit_sort_order", "edit_description", "edit_star_amount", "edit_currency", "edit_old_price"}:
            user_data.pop("product_admin_state", None)
            back = user_data.get("auto_pricing_return") or "admin_products"
            await message.reply_text("✅ تنظیم محصول ذخیره شد و در منوی مربوطه از اطلاعات جدید خوانده می‌شود." if cur and cur.rowcount else "محصول پیدا نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ بازگشت به مدیریت محصول", callback_data=back)]]))
            return

    if message and message.text and user_data.get("editor_wait_key"):
        if user.id != ADMIN_ID:
            user_data.pop("editor_wait_key", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return
        key = user_data.pop("editor_wait_key")
        new_text = message.text.strip()
        if key not in EDITABLE_TEXTS or not new_text:
            await message.reply_text("❌ متن خالی یا گزینه نامعتبر است.")
            return
        if len(new_text) > 3500:
            user_data["editor_wait_key"] = key
            await message.reply_text("❌ متن خیلی طولانی است؛ حداکثر ۳۵۰۰ نویسه بفرست.")
            return
        settings = load_bot_settings()
        settings[key] = new_text
        save_bot_settings(settings)
        await message.reply_text(
            f"✅ تغییرات با موفقیت ذخیره شد\n\n"
            f"📝 بخش: {EDITABLE_TEXTS[key][0]}\n\n"
            "💎 تنظیمات جدید ثبت شدند.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 ادامه ویرایش", callback_data="edit_content")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")]
            ])
        )
        return

    if message and message.text and user_data.get("vip_wait_volume"):
        raw = message.text.strip()
        if not raw.isdigit() or not 10 <= int(raw) <= 1000:
            await message.reply_text("❌ حجم باید عددی بین 10 تا 1000 گیگ باشد.")
            return
        volume = int(raw)
        user_data["vip_volume"] = volume
        user_data["vip_wait_volume"] = False
        user_data["vip_wait_duration"] = True
        await message.reply_text(
            "🧮 پلن دلخواه — انتخاب مدت\n\n"
            f"📊 حجم انتخابی: {volume} گیگ\n\n"
            "چند روز می‌خوای؟ یک عدد بین 30 تا 90 روز بفرست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")]
            ]),
        )
        return

    if message and message.text and user_data.get("vip_wait_duration"):
        raw = message.text.strip()
        if not raw.isdigit() or not 30 <= int(raw) <= 90:
            await message.reply_text("❌ مدت باید عددی بین 30 تا 90 روز باشد.")
            return
        duration = int(raw)
        volume = user_data.get("vip_volume")
        if volume is None or not 10 <= int(volume) <= 1000:
            user_data.pop("vip_wait_duration", None)
            await message.reply_text("اطلاعات حجم پیدا نشد؛ پلن دلخواه را دوباره انتخاب کن.")
            return

        price = int(volume) * 5000
        user_data["vip_duration"] = duration
        user_data["vip_price"] = price
        user_data["vip_invoice_amount"] = price
        user_data["vip_plan_type"] = "custom"
        user_data["vip_wait_duration"] = False
        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user.id,)
                ).fetchone()
            balance = row["balance"] if row else 0
        except Exception:
            logger.exception("VIP wallet lookup failed")
            balance = 0

        shortage = max(0, price - balance)
        text = (
            "🧮 خلاصهٔ پلن دلخواه\n\n"
            "💎 پلن: 📊 دلخواه\n"
            f"📊 حجم: {volume} گیگابایت\n"
            f"⏳ مدت: {duration} روز\n"
            f"💰 قیمت: {price:,} تومان\n\n"
            f"👛 موجودی شما: {balance:,} تومان"
            + (" ❌ (ناکافی) " if shortage else " ✅ ")
            + (f"💸 کسری موجودی : {shortage:,} تومان\n"
               "می‌تونی همین الان فقط کسری رو پرداخت کنی؛ بعد از تأیید، خرید خودکار انجام می‌شه.✅"
               if shortage else "موجودی کیف پول کافی است.")
        )
        await message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت / محاسبه کسری", callback_data="vip_open_payment", style="success")],
                [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="topup", style="primary")],
                [
                    InlineKeyboardButton("📊 تغییر حجم", callback_data="vip_change_volume", style="primary"),
                    InlineKeyboardButton("⏳ تغییر مدت", callback_data="vip_change_duration", style="primary"),
                ],
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="global_cancel", style="primary")],
                [InlineKeyboardButton("⬅️ بازگشت به VIP", callback_data="config_vip", style="primary")],
            ]),
        )
        return

    if message and message.text and user_data.get("config_wait_volume"):
        raw = message.text.strip()
        if not raw.isdigit() or not 10 <= int(raw) <= 500:
            await message.reply_text("❌ حجم باید عددی بین ۱۰ تا ۵۰۰ گیگ باشد.")
            return
        user_data["config_volume"] = int(raw)
        user_data.pop("config_wait_volume", None)
        user_data["config_wait_duration"] = True
        await message.reply_text(
            f"🧮 پلن دلخواه — انتخاب مدت\n\n"
            f"📊 حجم انتخابی: {int(raw)} گیگ\n\n"
            "چند روز می‌خواهی؟ عددی بین ۳۰ تا ۳۶۵ بفرست."
        )
        return

    if message and message.text and user_data.get("config_wait_duration"):
        raw = message.text.strip()
        if not raw.isdigit() or not 30 <= int(raw) <= 365:
            await message.reply_text("❌ مدت باید عددی بین ۳۰ تا ۳۶۵ روز باشد.")
            return
        duration = int(raw)
        volume = user_data.get("config_volume")
        if volume is None or not 10 <= volume <= 500:
            user_data.pop("config_wait_duration", None)
            await message.reply_text("اطلاعات حجم پیدا نشد؛ پلن دلخواه را دوباره انتخاب کن.")
            return

        price = volume * 2000
        user_data.pop("config_wait_duration", None)
        user_data["config_plan_type"] = "custom"
        user_data["config_price"] = price
        user_data["config_volume"] = volume
        user_data["config_duration"] = duration
        user_data["config_invoice_open"] = False
        try:
            with db() as con:
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user.id,)
                ).fetchone()
            balance = row["balance"] if row else 0
        except Exception:
            logger.exception("Wallet balance lookup failed")
            balance = 0

        shortage = max(0, price - balance)
        await message.reply_text(
            "🧮 خلاصهٔ پلن دلخواه\n\n"
            f"📊 حجم: {volume} گیگابایت\n"
            f"⏳ مدت: {duration} روز\n"
            f"💰 قیمت: {price:,} تومان\n\n"
            f"👛 موجودی شما: {balance:,} تومان\n"
            + (f"❌ کسری موجودی: {shortage:,} تومان\n\n" if shortage else "✅ موجودی کافی است.\n\n")
            + "می‌تونی همین الان فقط کسری رو پرداخت کنی؛ بعد از تأیید، خرید خودکار انجام می‌شه.✅\n\n"
"⚠️ توجه: این قابلیت هنوز فعال نیست؛ درگاه واقعی متصل نشده و پرداخت یا تحویل خودکار انجام نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 پرداخت / محاسبه کسری", callback_data="config_open_payment", style="success")],
                [InlineKeyboardButton(bot_text("topup_button"), callback_data="topup", style="primary")],
                [
                    InlineKeyboardButton("📊 تغییر حجم", callback_data="config_change_volume", style="primary"),
                    InlineKeyboardButton("⏳ تغییر مدت", callback_data="config_change_duration", style="primary"),
                ],
                [InlineKeyboardButton("⬅️ بازگشت به پلن‌ها", callback_data="config_economic", style="primary")],
            ]),
        )
        return

    product_id = user_data.get("pending_product_id")
    if product_id is not None and message and message.text:
        details = message.text.strip()
        if not details:
            await message.reply_text("لطفاً اطلاعات سفارش را ارسال کنید.")
            return

        pending_coupon = user_data.get("pending_product_coupon") or {}
        preflight_product = None
        with db() as con:
            preflight_product = con.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if preflight_product is None:
            user_data.pop("pending_product_id", None)
            user_data.pop("pending_dynamic_quote", None)
            user_data.pop("pending_product_coupon", None)
            await message.reply_text("این محصول دیگر فعال نیست؛ از منو دوباره انتخاب کن.")
            return
        preflight_category = str(preflight_product["category"] or "").strip().lower()
        order_live_quote = None
        gift_order_live_price = None
        if preflight_category == "gifts" and preflight_product["source_type"] == "official" and load_gift_settings().get("auto_rate_enabled"):
            await refresh_auto_pricing(force=False)
            gift_cfg_now = load_gift_settings()
            gift_order_live_price = gift_price_for_stars(preflight_product["star_count"], gift_cfg_now)
            expected_gift = user_data.get("pending_gift_price_quote") or {}
            if gift_order_live_price is None:
                user_data.pop("pending_product_id", None)
                user_data.pop("pending_gift_price_quote", None)
                await message.reply_text("قیمت این گیفت ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.")
                return
            if int(expected_gift.get("sale_toman", -1)) != int(gift_order_live_price):
                user_data.pop("pending_product_id", None)
                user_data.pop("pending_gift_price_quote", None)
                await message.reply_text(f"قیمت گیفت تغییر کرده است. قیمت تازه: {toman(gift_order_live_price)} تومان. محصول را دوباره انتخاب کن تا قیمت جدید را ببینی.")
                return
        if preflight_category in {"stars", "premium"}:
            await refresh_auto_pricing(force=False)
            order_live_quote = dynamic_product_quote(preflight_product)
            expected_quote = user_data.get("pending_dynamic_quote") or {}
            if order_live_quote is None:
                user_data.pop("pending_product_id", None)
                user_data.pop("pending_dynamic_quote", None)
                user_data.pop("pending_product_coupon", None)
                user_data.pop("pending_product_coupon_input", None)
                await message.reply_text("قیمت این محصول ثبت نشده است؛ لطفاً با پشتیبانی تماس بگیر.")
                return
            if (int(expected_quote.get("product_id", -1)) != int(product_id)
                    or int(expected_quote.get("sale_toman", -1)) != int(order_live_quote["sale_toman"])):
                user_data.pop("pending_product_id", None)
                user_data.pop("pending_dynamic_quote", None)
                user_data.pop("pending_product_coupon", None)
                user_data.pop("pending_product_coupon_input", None)
                await message.reply_text(
                    f"قیمت این محصول تغییر کرده است. قیمت تازه: {toman(order_live_quote['sale_toman'])} تومان.\n"
                    "برای جلوگیری از ثبت با مبلغ اشتباه، سفارش ثبت نشد؛ محصول را دوباره انتخاب کن و سپس اطلاعات سفارش را بفرست."
                )
                return
        coupon_code = None
        coupon_discount = 0
        original_price = 0
        order_price = 0
        try:
            with db() as con:
                ensure_discount_schema(con)
                con.execute("BEGIN IMMEDIATE")
                product = con.execute(
                    "SELECT * FROM products WHERE id=? AND active=1",
                    (product_id,),
                ).fetchone()
                if product is None:
                    user_data.pop("pending_product_id", None)
                    user_data.pop("pending_product_coupon", None)
                    await message.reply_text(
                        "این محصول دیگر فعال نیست. دوباره از منو انتخاب کنید.",
                        reply_markup=main_menu(),
                    )
                    return

                category = (product["category"] or "").lower()
                recipient_user_id = None
                recipient_username = ""
                if category == "gifts":
                    gift_id_match = re.search(r"(?im)^\s*ID\s*عددی\s*گیرنده\s*[:：]\s*(\d{5,20})\s*$", normalize_ascii_digits(details))
                    username_match = re.search(r"(?im)^\s*آیدی\s*تلگرام\s*گیرنده\s*[:：]\s*(.*)$", details)
                    if not gift_id_match:
                        await message.reply_text("❌ برای ثبت سفارش گیفت، ID عددی گیرنده را جداگانه وارد کن.\nنمونه:\nID عددی گیرنده: 123456789\nآیدی تلگرام گیرنده: @username یا ندارم")
                        return
                    recipient_user_id = int(gift_id_match.group(1))
                    if username_match:
                        recipient_username = normalize_optional_username(username_match.group(1))
                        if recipient_username is None:
                            await message.reply_text("❌ آیدی تلگرام گیرنده معتبر نیست. آن را با @ وارد کن یا بنویس ندارم.")
                            return
                    gift_cfg_now = load_gift_settings()
                    requested_mode = str(user_data.get("pending_gift_delivery_mode") or ("manual" if gift_cfg_now["manual_delivery_all"] else product["delivery_mode"] or "manual"))
                    if gift_cfg_now["manual_delivery_all"]:
                        requested_mode = "manual"
                    if requested_mode == "auto" and (product["source_type"] != "official" or not product["telegram_gift_id"]):
                        requested_mode = "manual"
                    user_data["pending_gift_delivery_mode"] = requested_mode
                elif category == "premium":
                    premium_months = premium_product_months(product["name"])
                    premium_cfg_now = load_premium_manager_settings()
                    premium_mode_now = premium_cfg_now["delivery_modes"].get(str(premium_months), "manual") if premium_months else "manual"
                    id_match = re.search(r"(?im)^\s*ID\s*عددی\s*گیرنده\s*[:：]\s*(\d{5,20})\s*$", normalize_ascii_digits(details))
                    uname_match = re.search(r"(?im)^\s*آیدی\s*تلگرام\s*گیرنده\s*[:：]\s*(.*)$", details)
                    raw_detail = details.strip()
                    if id_match:
                        recipient_user_id = int(id_match.group(1))
                    elif raw_detail.isdigit() and 5 <= len(raw_detail) <= 20:
                        recipient_user_id = int(raw_detail)
                    if uname_match:
                        recipient_username = normalize_optional_username(uname_match.group(1)) or ""
                    elif raw_detail.startswith("@"):
                        recipient_username = normalize_optional_username(raw_detail) or ""
                    if raw_detail.startswith("@") and not recipient_username:
                        await message.reply_text("❌ آیدی تلگرام معتبر نیست. نمونه: @SeyedPro_AIA")
                        return
                    if recipient_user_id is None and recipient_username:
                        if user.username and user.username.casefold() == recipient_username.casefold():
                            recipient_user_id = int(user.id)
                        else:
                            with db() as lookup_con:
                                known_recipient = lookup_con.execute("SELECT user_id FROM users WHERE lower(username)=? LIMIT 1", (recipient_username.casefold(),)).fetchone()
                            if known_recipient:
                                recipient_user_id = int(known_recipient["user_id"])
                    if not recipient_username and not recipient_user_id:
                        await message.reply_text("❌ اطلاعات گیرنده کامل نیست. ID عددی یا آیدی @ گیرنده را بفرست.")
                        return
                    if premium_mode_now == "auto" and not recipient_user_id:
                        await message.reply_text("⚠️ این پلن روی تحویل خودکار تنظیم شده است. برای ارسال رسمی تلگرام، ID عددی گیرنده لازم است. لطفاً پیام را با قالب زیر دوباره بفرست:\nID عددی گیرنده: 123456789\nآیدی تلگرام گیرنده: @username")
                        return
                elif category == "stars":
                    if not details.startswith("@") or len(details) < 2:
                        await message.reply_text("لطفاً آیدی تلگرام را با @ ارسال کنید.\nمثال: @SeyedPro_AIA")
                        return
                elif category == "boost":
                    if "t.me/" not in details.lower() and "telegram.me/" not in details.lower():
                        await message.reply_text("لطفاً لینک گروه یا کانال تلگرام را ارسال کنید؛ لینک عمومی یا دعوت خصوصی.")
                        return

                original_price = (int(order_live_quote["sale_toman"]) if order_live_quote else
                                  int(gift_order_live_price) if gift_order_live_price is not None else int(product["price"] or 0))
                order_price = original_price
                if pending_coupon and int(pending_coupon.get("product_id", -1)) == int(product_id):
                    validation = validate_coupon_for_order(
                        con, pending_coupon.get("code"), user.id, original_price,
                        product["category"], product["name"], user.username
                    )
                    if not validation.get("ok"):
                        await message.reply_text(
                            validation.get("reason", "❌ کد تخفیف دیگر معتبر نیست.")
                            + "\nدرخواست سفارش ثبت نشد. کد را دوباره بررسی کن یا بدون کد اطلاعات سفارش را ارسال کن."
                        )
                        return
                    coupon_code = validation["code"]
                    coupon_discount = int(validation["discount"])
                    order_price = int(validation["final_price"])

                order_currency = "تومان" if (order_live_quote or gift_order_live_price is not None) else str(product["currency"] or "تومان")
                stored_details = details
                if order_live_quote:
                    stored_details += "\n\n[ریز محاسبه قیمت برای بررسی مدیر]\n" + quote_breakdown_text(order_live_quote)
                if gift_order_live_price is not None:
                    gift_unit = gift_unit_sale_price(load_gift_settings())
                    stored_details += f"\n\n[محاسبه قیمت گیفت]\nقیمت هر ⭐ با سود: {toman(gift_unit)} تومان\nتعداد Stars گیفت: {int(product['star_count'] or 0)}\nقیمت نهایی: {toman(gift_order_live_price)} تومان"
                card_number = str(bot_text("payment_card_number") or "").strip()
                payment_ready = bool(card_number and card_number.casefold() not in {"هنوز تنظیم نشده", "تنظیم نشده", "ثبت نشده"})
                order_status = "در انتظار ارسال رسید" if order_price > 0 and payment_ready else "در انتظار بررسی مدیر"
                payment_method = "کارت‌به‌کارت" if order_price > 0 and payment_ready else "نیازمند هماهنگی مدیر"
                chosen_delivery_mode = "manual"
                saved_gift_id = ""
                if category == "premium":
                    premium_months = premium_product_months(product["name"])
                    premium_cfg_now = load_premium_manager_settings()
                    chosen_delivery_mode = premium_cfg_now["delivery_modes"].get(str(premium_months), "manual") if premium_months else "manual"
                if category == "gifts":
                    gift_cfg_now = load_gift_settings()
                    chosen_delivery_mode = str(user_data.get("pending_gift_delivery_mode") or product["delivery_mode"] or "manual")
                    if gift_cfg_now["manual_delivery_all"] or product["source_type"] != "official" or not product["telegram_gift_id"]:
                        chosen_delivery_mode = "manual"
                    saved_gift_id = str(product["telegram_gift_id"] or "")
                cur = con.execute(
                    "INSERT INTO orders (user_id, product_id, price, status, coupon_code, coupon_discount, currency, order_details, payment_method, buyer_username, buyer_full_name, recipient_user_id, recipient_username, product_name_snapshot, gift_id, delivery_mode) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user.id, int(product_id), order_price, order_status, coupon_code, coupon_discount, order_currency, stored_details,
                     payment_method, user.username or "", user.full_name or "", recipient_user_id,
                     recipient_username or "", str(product["name"] or ""), saved_gift_id, chosen_delivery_mode),
                )
                order_id = cur.lastrowid
                order_code = f"SP-G{int(order_id):06d}" if category == "gifts" else f"SP-{int(order_id):06d}"
                con.execute("UPDATE orders SET order_code=? WHERE id=?", (order_code, order_id))
                log_order_event(con, order_id, "created", order_status, f"محصول: {product['name']} | مبلغ: {order_price} {order_currency}", user.id)
                if coupon_code:
                    # Pending order reserves a use atomically. Rejection/deletion releases it.
                    con.execute(
                        "INSERT INTO coupon_usage (code, user_id, order_id, discount_amount, status) VALUES (?, ?, ?, ?, 'pending')",
                        (coupon_code, user.id, order_id, coupon_discount)
                    )
                    con.execute(
                        "UPDATE discount_codes SET total_uses=COALESCE(total_uses, 0)+1 WHERE code=?",
                        (coupon_code,)
                    )
        except Exception:
            logger.exception("Product order creation failed; user_id=%s product_id=%s", getattr(user, "id", None), product_id)
            await message.reply_text("❌ ثبت درخواست انجام نشد؛ جزئیات خطا در لاگ ثبت شد. اطلاعات سفارش و تخفیف دوباره بررسی شود.")
            return

        user_data.pop("pending_product_id", None)
        user_data.pop("pending_dynamic_quote", None)
        user_data.pop("pending_product_coupon", None)
        user_data.pop("pending_product_coupon_input", None)
        user_data.pop("pending_gift_delivery_mode", None)
        user_data.pop("pending_gift_delivery_product_id", None)
        user_data.pop("pending_gift_price_quote", None)
        price_text = f"{order_price:,} {order_currency}" if original_price > 0 else "نیازمند استعلام"
        coupon_summary = (f"\nکد تخفیف: {coupon_code}\nمیزان تخفیف: {coupon_discount:,} {order_currency}\nقیمت اصلی: {original_price:,} {order_currency}"
                          if coupon_code else "")

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"📦 سفارش جدید #{order_id}\n\n"
                    f"کاربر: {user.full_name}\n"
                    f"شناسه کاربر: {user.id}\n"
                    f"محصول: {product['name']}\n"
                    f"کد سفارش: {order_code}\n"
                    f"ID عددی خریدار: {user.id} | آیدی: @{user.username or 'تنظیم نشده'}\n"
                    + (f"ID عددی گیرنده: {recipient_user_id or 'ثبت نشده'} | آیدی: {recipient_username or 'ثبت نشده'}\n" if category == "gifts" else "")
                    + f"زمان ثبت: {format_iran_jalali_datetime(utc_now().isoformat(timespec='seconds'))}\n"
                    + f"اطلاعات سفارش: {details}\n"
                    + f"قیمت نهایی: {price_text}{coupon_summary}\n"
                    f"روش پرداخت: {payment_method}\n"
                    f"وضعیت: {order_status}"
                ),
            )
        except Exception:
            logger.exception("Could not notify admin about order")

        if order_status == "در انتظار ارسال رسید":
            user_data["pending_receipt_order_id"] = int(order_id)
            card_owner = str(bot_text("payment_card_owner") or "").strip()
            customer_message = (
                f"✅ سفارش شما ثبت شد. کد سفارش: {order_code}\n\n"
                f"🛍 محصول: {product['name']}\n📨 اطلاعات سفارش: {details}\n"
                f"💰 مبلغ قابل پرداخت: {price_text}"
                + (f"\n🏷️ کد تخفیف: {coupon_code}\n🎁 میزان تخفیف: {coupon_discount:,} {order_currency}" if coupon_code else "")
                + f"\n\n💳 شماره کارت: {bot_text('payment_card_number')}\n👤 صاحب کارت: {card_owner or 'در تنظیمات وارد نشده'}\n\n"
                + bot_text("payment_instructions")
                + "\n\nپس از پرداخت، عکس واضح رسید را همین‌جا ارسال کن. تا پیش از تأیید مدیر، سفارش تأییدشده محسوب نمی‌شود."
            )
        else:
            customer_message = (
                f"✅ درخواست شما ثبت شد.\n\n📦 کد سفارش: {order_code}\n"
                f"🛍 محصول: {product['name']}\n📨 اطلاعات ارسالی: {details}\n💰 مبلغ: {price_text}\n\n"
                "اطلاعات پرداخت هنوز تنظیم نشده یا قیمت نیازمند بررسی است. فعلاً پرداخت انجام نده؛ مدیر درخواست را بررسی می‌کند و اطلاع می‌دهد."
            )
        if order_status == "در انتظار ارسال رسید":
            await message.reply_text(
                customer_message,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو سفارش", callback_data=f"cancel_receipt_order_{order_id}", style="danger")]]),
            )
        else:
            await message.reply_text(customer_message, reply_markup=main_menu())
        return

    await message.reply_text(
        "برای سفارش، ابتدا محصول را از منوی فروشگاه انتخاب کنید.",
        reply_markup=main_menu(),
    )


# ==========================================
# اجرای ربات
# ==========================================

def main():
    token = input("توکن ربات را وارد کن (در چت ارسال نکن): ").strip()

    if not token or ":" not in token:
        print("توکن معتبر نیست. از BotFather توکن صحیح بگیر.")
        return

    init_db()

    async def set_commands(application):
        await application.bot.set_my_commands([
            BotCommand("start", "نمایش منوی فروشگاه"),
            BotCommand("admin", "پنل مدیریت"),
        ])

    async def post_init(application):
        application.create_task(set_commands(application))
        application.create_task(auto_pricing_refresh_loop(application))
    app = (
        Application.builder()
        .token(token)
        .connection_pool_size(16)
        .connect_timeout(10)
        .read_timeout(15)
        .write_timeout(15)
        .pool_timeout(5)
        .post_init(post_init)
        .build()
    )
    async def trace_update(update, context):
        if update.effective_message and update.effective_message.text:
            if update.effective_message.text.split()[0].startswith("/start"):
                logger.warning(
                    "START RECEIVED | update_id=%s | monotonic=%.6f",
                    update.update_id,
                    time.perf_counter(),
                )

    app.add_handler(TypeHandler(Update, trace_update), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("cancel", cancel_coupon_creation))
    app.add_handler(CallbackQueryHandler(callbacks))
    if PreCheckoutQueryHandler is not None:
        app.add_handler(PreCheckoutQueryHandler(handle_owner_star_precheckout))
    successful_payment_filter = getattr(filters, "SUCCESSFUL_PAYMENT", None)
    if successful_payment_filter is not None:
        app.add_handler(MessageHandler(successful_payment_filter, handle_owner_star_success))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    support_media_filters = (
        filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO |
        filters.VOICE | filters.ANIMATION | filters.Sticker.ALL | filters.VIDEO_NOTE
    )
    app.add_handler(MessageHandler(support_media_filters, text_message))



    print("SEYED PREMIUM روشن شد. برای توقف Ctrl+C را بزن.")
    # Keep polling resilient to transient Telegram Bad Gateway / network errors.
    # Pending updates are preserved so orders and receipts are not silently dropped.
    # Timeouts belong to Application.builder() above, not run_polling().
    # Passing timeout kwargs here raises TypeError and causes the host crash loop.
    app.run_polling(
        poll_interval=0.5,
        timeout=25,
        bootstrap_retries=-1,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()


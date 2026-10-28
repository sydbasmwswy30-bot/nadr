from ui_config import buttons as configured_buttons
from service_menu import service_menu

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    BotCommand,
)
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
    return f"{int(value or 0):,}".replace(",", "٬")


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
SETTINGS_FILE = Path(__file__).with_name("bot_settings.json")

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
    "reactions_button": ("دکمه ری‌اکشن تلگرام", "🔥 ری‌اکشن تلگرام"),
    "views_button": ("دکمه ویو تلگرام", "👀 ویو تلگرام"),
    "instagram_followers_button": ("دکمه فالوور اینستاگرام", "📸 فالوور اینستاگرام"),
    "instagram_likes_button": ("دکمه لایک اینستاگرام", "❤️ لایک اینستاگرام"),


}

_BOT_SETTINGS_CACHE = None

def load_bot_settings():
    global _BOT_SETTINGS_CACHE
    if _BOT_SETTINGS_CACHE is not None:
        return _BOT_SETTINGS_CACHE
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        _BOT_SETTINGS_CACHE = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _BOT_SETTINGS_CACHE = {}
    return _BOT_SETTINGS_CACHE


def save_bot_settings(data):
    global _BOT_SETTINGS_CACHE
    SETTINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    _BOT_SETTINGS_CACHE = data if isinstance(data, dict) else {}


def bot_text(key):
    defaults = dict(EDITABLE_TEXTS)
    item = defaults.get(key)
    if not item:
        return key
    return load_bot_settings().get(key, item[1])


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
    "reactions": ("🔥 ری‌اکشن تلگرام", "ری‌اکشن تلگرام"),
    "views": ("👀 ویو تلگرام", "ویو تلگرام"),
    "instagram_followers": ("📸 فالوور اینستاگرام", "فالوور اینستاگرام"),
    "instagram_likes": ("❤️ لایک اینستاگرام", "لایک اینستاگرام"),
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
    "instagram_likes": "❤️ لایک اینستاگرام",
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
    if category in ("premium", "stars", "gifts"):
        title = {"premium": "پرمیوم", "stars": "استارز", "gifts": "گیفت"}[category]
        return f"👈 لطفاً آیدی اکانتی که می‌خواهید {title} برای آن تهیه کنید را ارسال کنید.\n• مثال: @SeyedPro_AIA"
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
        # Additive, non-destructive storage for persistent global bot settings.
        con.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.execute(
            "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('main_menu_type', 'inline')"
        )

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

        # هر بستهٔ ثابت استارز یک محصول مستقل دارد؛ قیمت‌های موجود دست‌نخورده می‌مانند.
        for amount in (50, 75, 100, 150, 250, 350, 500, 1000, 2500, 5000, 10000, 50000):
            name = f"استارز {amount} عدد"
            con.execute(
                "INSERT INTO products(category, name, price, active) "
                "SELECT 'stars', ?, 0, 1 WHERE NOT EXISTS "
                "(SELECT 1 FROM products WHERE category='stars' AND name=?)",
                (name, name),
            )



    with db() as con:
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_products_category_active ON products(category, active)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)")

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
# منوها
# ==========================================

from stars_menu import stars_menu, stars_text, stars_detail_menu
import re


MENU_TYPE_DEFAULT = "inline"
MENU_TYPE_KEYBOARD = "keyboard"
MENU_TYPE_INLINE = "inline"


def get_main_menu_type():
    """Read the saved main-menu mode from the existing shop database."""
    try:
        with db() as con:
            row = con.execute(
                "SELECT value FROM app_settings WHERE key='main_menu_type'"
            ).fetchone()
        value = str(row["value"] if row else MENU_TYPE_DEFAULT).strip().lower()
        return value if value in (MENU_TYPE_KEYBOARD, MENU_TYPE_INLINE) else MENU_TYPE_DEFAULT
    except sqlite3.Error:
        logger.exception("Could not read main menu type; falling back to inline")
        return MENU_TYPE_DEFAULT


def set_main_menu_type(value):
    value = str(value or "").strip().lower()
    if value not in (MENU_TYPE_KEYBOARD, MENU_TYPE_INLINE):
        raise ValueError("نوع منو نامعتبر است")
    with db() as con:
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES ('main_menu_type', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )


def _main_menu_items():
    return [
        item for item in configured_buttons("main")
        if isinstance(item, dict) and item.get("enabled", True)
        and str(item.get("text", "")).strip()
        and str(item.get("callback", "")).strip()
    ]


def main_menu():
    items = _main_menu_items()
    if get_main_menu_type() == MENU_TYPE_KEYBOARD:
        rows = []
        pair = []
        for item in items:
            label = str(item.get("text", "")).strip()
            if "سؤالات متداول" in label or "سوالات متداول" in label or "خرید کانفیگ" in label:
                if pair:
                    rows.append(pair)
                    pair = []
                rows.append([KeyboardButton(label)])
            else:
                pair.append(KeyboardButton(label))
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
        if pair:
            rows.append(pair)
        rows.append([KeyboardButton("🏠 منوی اصلی")])
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="از منوی SEYED PREMIUM انتخاب کن…",
        )

    rows = []
    pair = []
    for item in items:
        label = str(item.get("text", ""))
        callback = str(item.get("callback", ""))
        button = InlineKeyboardButton(
            label,
            callback_data=callback,
            style="danger" if "سؤالات متداول" in label or "سوالات متداول" in label else "primary",
        )
        if "سؤالات متداول" in label or "سوالات متداول" in label or "خرید کانفیگ" in label:
            if pair:
                rows.append(pair)
                pair = []
            rows.append([button])
        else:
            pair.append(button)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(rows)


def menu_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⌨️ نوع منو: کیبورد", callback_data="admin_menu_set_keyboard")],
        [InlineKeyboardButton("💎 نوع منو: شیشه‌ای", callback_data="admin_menu_set_inline")],
        [InlineKeyboardButton("⬅️ بازگشت به مدیریت", callback_data="admin")],
    ])


def menu_settings_text(prefix=None):
    mode = get_main_menu_type()
    current = "⌨️ کیبورد معمولی" if mode == MENU_TYPE_KEYBOARD else "💎 منوی شیشه‌ای (Inline)"
    return ((str(prefix).strip() + "\n\n") if prefix else "") + f"⚙️ تنظیم نوع منوی اصلی\n\nحالت فعلی: {current}\n\nنوع دلخواه را انتخاب کن:"


class _KeyboardMenuQuery:
    """Route ReplyKeyboard taps through existing callback logic."""
    def __init__(self, update, context, data):
        self._update = update
        self._context = context
        self.data = data
        self.from_user = update.effective_user
        self.message = update.effective_message

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text, **kwargs):
        return await self._context.bot.send_message(
            chat_id=self._update.effective_chat.id, text=text, **kwargs
        )

    async def edit_message_reply_markup(self, *args, **kwargs):
        return None


async def dispatch_keyboard_menu_text(update, context):
    """Route exact main-menu keyboard labels into the existing callback handlers."""
    current_mode = get_main_menu_type()
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return False
    routes = {str(item["text"]).strip(): str(item["callback"]) for item in _main_menu_items()}
    routes["🏠 منوی اصلی"] = "home"
    callback_data = routes.get(message.text.strip())
    if not callback_data:
        return False

    seen_mode = ""
    try:
        with db() as con:
            row = con.execute(
                "SELECT menu_type_seen FROM users WHERE user_id=?", (user.id,)
            ).fetchone()
        seen_mode = str(row["menu_type_seen"] or "") if row else ""
    except sqlite3.Error:
        logger.exception("Could not determine stale reply-keyboard state")
    stale_keyboard = current_mode == MENU_TYPE_INLINE and seen_mode == MENU_TYPE_KEYBOARD
    if current_mode != MENU_TYPE_KEYBOARD and not stale_keyboard:
        return False
    if stale_keyboard:
        await message.reply_text(
            "🔄 منوی قبلی به‌روزرسانی شد.", reply_markup=ReplyKeyboardRemove()
        )
    try:
        with db() as con:
            con.execute("UPDATE users SET menu_type_seen=? WHERE user_id=?", (current_mode, user.id))
    except sqlite3.Error:
        logger.exception("Could not update the user's seen menu type")

    from types import SimpleNamespace
    query = _KeyboardMenuQuery(update, context, callback_data)
    synthetic_update = SimpleNamespace(
        callback_query=query,
        effective_user=update.effective_user,
        effective_chat=update.effective_chat,
        effective_message=update.effective_message,
        update_id=getattr(update, "update_id", None),
    )
    await _callbacks_impl(synthetic_update, context)
    return True


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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/SeyedPro_FQA")],
        [InlineKeyboardButton("🔄 بررسی عضویت", callback_data="check_membership")],
    ])

_MEMBERSHIP_CACHE = {}
_MEMBERSHIP_CACHE_TTL = 300
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
        if result is True and now - checked_at < _MEMBERSHIP_CACHE_TTL:
            return True

    try:
        m = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        result = (
            m.status in ("creator", "administrator", "member")
            or (
                m.status == "restricted"
                and getattr(m, "is_member", False)
            )
        )
        if result is True:
            _MEMBERSHIP_CACHE[user_id] = (True, time.monotonic())
        else:
            _MEMBERSHIP_CACHE.pop(user_id, None)
        return result
    except Exception:
        logger.warning("Channel membership check failed", exc_info=True)
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
                "🔒 برای استفاده از خدمات ربات تلگرام، ابتدا عضو کانال زیر شو و سپس «بررسی عضویت» را بزن.",
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
    with db() as con:
        seen_row = con.execute(
            "SELECT menu_type_seen FROM users WHERE user_id=?", (user.id,)
        ).fetchone()
        previous_menu_type = str(seen_row["menu_type_seen"] or "") if seen_row else ""
    if current_menu_type == MENU_TYPE_INLINE and previous_menu_type == MENU_TYPE_KEYBOARD:
        await update.effective_message.reply_text(
            "🔄 منوی قبلی به‌روزرسانی شد.", reply_markup=ReplyKeyboardRemove()
        )
    with db() as con:
        con.execute("UPDATE users SET menu_type_seen=? WHERE user_id=?", (current_menu_type, user.id))

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

    markup = main_menu()
    _start_mark("main_menu build")

    await update.effective_message.reply_text(text, reply_markup=markup)
    _start_mark("reply_text completed")

# ==========================================
# پنل مدیریت
# ==========================================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_channel_membership(update, context):
        return
    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("⛔ دسترسی مجاز نیست.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار فروشگاه", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton("🛍 محصولات و قیمت‌ها", callback_data="admin_products", style="primary")],
        [InlineKeyboardButton("📦 آخرین سفارش‌ها", callback_data="admin_orders", style="primary")],
        [InlineKeyboardButton("🏷️ مدیریت کدهای تخفیف", callback_data="admin_coupons", style="primary")],
        [InlineKeyboardButton("⌨️ نوع منو: کیبورد", callback_data="admin_menu_set_keyboard", style="primary")],
        [InlineKeyboardButton("💎 نوع منو: شیشه‌ای", callback_data="admin_menu_set_inline", style="primary")],
        [InlineKeyboardButton("📝 ویرایش متن‌ها و دکمه‌ها", callback_data="uiadm_home", style="primary")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home", style="primary")],
    ])
    await update.effective_message.reply_text(
        bot_text("admin_title"),
        reply_markup=keyboard,
    )


# ==========================================
# دکمه‌ها
# ==========================================

def persian_digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def build_stars_menu():
    """ساخت منوی استارز از همان کاتالوگ محصولات مدیریت؛ محصولات آینده هم خودکار دیده می‌شوند."""
    with db() as con:
        rows = con.execute(
            "SELECT id, name FROM products WHERE category='stars' AND active=1 ORDER BY id"
        ).fetchall()
    buttons = []
    for row in rows:
        # دکمه‌های فعلی و بسته‌های جدیدی که مدیر با نام «استارز N عدد» اضافه کند.
        import re
        match = re.search(r"(\d[\d,٬]*)", row["name"].replace("،", ","))
        if not match:
            continue
        try:
            amount = int(match.group(1).replace(",", "").replace("٬", ""))
        except ValueError:
            continue
        buttons.append([InlineKeyboardButton(
            f"🎁 {persian_digits(amount)} استارز",
            callback_data=f"stars_pack_{amount}",
            style="primary" if len(buttons) % 2 == 0 else "success",
        )])
    buttons.append([InlineKeyboardButton(
        "✏️ استارز به مقدار دلخواه", callback_data="stars_custom", style="success"
    )])
    buttons.append([InlineKeyboardButton(
        "↩️ بازگشت", callback_data="back_to_services", style="danger"
    )])
    return InlineKeyboardMarkup(buttons)


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


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _handler_started = time.perf_counter()
    try:
        await _callbacks_impl(update, context)
    except Exception:
        logger.exception("Unhandled callback failure; update_id=%s", getattr(update, "update_id", None))
        query = update.callback_query
        if query:
            try:
                await query.answer("خطایی رخ داد؛ خطا ثبت شد. دوباره تلاش کن.", show_alert=True)
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
    with db() as con:
        rows = con.execute(
            "SELECT id, name, price FROM products WHERE category=? AND active=1 ORDER BY id",
            (category,),
        ).fetchall()
    labels = {
        "premium": "💎 تلگرام پرمیوم", "stars": "⭐ تلگرام استارز",
        "gifts": "🎁 گیفت تلگرام", "boost": "🚀 بوست تلگرام",
        "numbers": "📱 شماره مجازی", "members": "👥 ممبر تلگرام",
        "reactions": "🔥 ری‌اکشن تلگرام", "views": "👀 ویو تلگرام",
        "instagram_followers": "📸 فالوور اینستاگرام",
        "instagram_likes": "❤️ لایک اینستاگرام",
    }
    title = labels.get(category, f"🛍 {category}")
    if not rows:
        await query.edit_message_text(
            f"{title}\n\nفعلاً محصول فعالی در این دسته ثبت نشده است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ بازگشت به خدمات", callback_data="cat_stars")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
            ]),
        )
        return
    keyboard = []
    for row in rows:
        price = int(row["price"] or 0)
        price_text = f"{price:,} تومان" if price > 0 else "استعلام قیمت"
        keyboard.append([InlineKeyboardButton(
            f"{row['name']} | {price_text}", callback_data=f"buy_{row['id']}", style="success"
        )])
    keyboard.extend([
        [InlineKeyboardButton("⬅️ بازگشت به خدمات", callback_data="cat_stars")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
    ])
    await query.edit_message_text(
        f"{title}\n\nمحصول موردنظر را انتخاب کن.\nقیمت صفر یعنی قیمت هنوز توسط مدیر تنظیم نشده است.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _callbacks_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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
                "🔒 برای استفاده از خدمات ربات تلگرام، ابتدا عضو کانال زیر شو و سپس «بررسی عضویت» را بزن.",
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
                    chat_id=user_id, text=bot_text("main_menu"), reply_markup=main_menu()
                )
            else:
                await query.edit_message_text(
                    "🏠 منوی اصلی", reply_markup=main_menu()
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

        from ui_config import load as ui_load, save as ui_save
        state = context.user_data
        parts = data.split("_")
        action = data

        if data == "uiadm_home":
            state.pop("uiadm_state", None)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("منوی اصلی", callback_data="uiadm_menu_main")],
                [InlineKeyboardButton("منوی خدمات", callback_data="uiadm_menu_services")],
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
                (m for m in ("services", "main")
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
                [InlineKeyboardButton("✏️ تغییر نام", callback_data="uiadm_rename")],
                [InlineKeyboardButton("🔁 فعال/غیرفعال", callback_data="uiadm_toggle")],
                [InlineKeyboardButton("⬆️ جابه‌جایی به بالا", callback_data="uiadm_up")],
                [InlineKeyboardButton("⬇️ جابه‌جایی به پایین", callback_data="uiadm_down")],
                [InlineKeyboardButton("🗑 حذف دکمه", callback_data="uiadm_delete")],
                [InlineKeyboardButton("↩️ بازگشت", callback_data=f"uiadm_menu_{menu}")],
            ])
            await query.edit_message_text(f"مدیریت دکمه: {button_id}", reply_markup=kb)
            return

        if data in ("uiadm_rename", "uiadm_add_main", "uiadm_add_services"):
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
            elif data == "uiadm_down" and idx < len(items)-1:
                items[idx+1], items[idx] = items[idx], items[idx+1]
            elif data == "uiadm_delete":
                items.pop(idx)
            ui_save(cfg)
            await query.edit_message_text("✅ تغییر ذخیره شد.", reply_markup=InlineKeyboardMarkup([
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

        if amount <= 0:
            await query.answer("مقدار بسته معتبر نیست.", show_alert=True)
            return
        persian_amount = persian_digits(f"{amount:,}".replace(",", "٬"))
        tier = "بسته استارز"
        conn = db()
        try:
            product = conn.execute(
                "SELECT id, price, active FROM products "
                "WHERE category = 'stars' AND name = ? LIMIT 1",
                (f"استارز {amount} عدد",)
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

        price = int(product["price"] or 0)
        balance = int(user["balance"] or 0) if user else 0

        def toman(value):
            return f"{value:,}".replace(",", "٬")

        if price <= 0:
            detail = (
                "🛒 جزئیات بسته انتخابی\n\n"
                f"✅ استارز - {persian_amount} استارز\n"
                f"📝 {tier}\n"
                "💸 قیمت: هنوز تنظیم نشده است\n"
                f"💰 موجودی کیف پول: {toman(balance)} تومان\n\n"
                "⚠️ قیمت این بسته هنوز در تنظیمات فروشگاه ثبت نشده؛ "
                "فعلاً امکان ادامه خرید وجود ندارد."
            )
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
        await query.edit_message_text(
            stars_text(),
            reply_markup=build_stars_menu()
        )
        return

    # SEYED_STARS_DISCOUNT_V1
    if data.startswith("stars_discount_") and not data.startswith("stars_discount_skip_"):
        try:
            amount = int(data.rsplit("_", 1)[-1])
            with db() as con:
                product = con.execute(
                    "SELECT price, active FROM products WHERE category=? AND name=? LIMIT 1",
                    ("stars", f"استارز {amount} عدد")
                ).fetchone()
            if not product or not product["active"] or int(product["price"] or 0) <= 0:
                await query.answer("قیمت این بسته تنظیم نشده است.", show_alert=True)
                return
            context.user_data["pending_stars_coupon"] = {
                "amount": amount, "price": int(product["price"])
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
        def toman(value):
            return f"{int(value):,}".replace(",", "٬")
        is_self = data.startswith("stars_recipient_self_")
        amount = data.rsplit("_", 1)[-1]

        user_id = query.from_user.id
        username = query.from_user.username
        recipient = f"@{username}" if is_self and username else ("خودم" if is_self else "پس از انتخاب گیرنده")
        recipient_id = str(user_id) if is_self else "پس از انتخاب گیرنده"

        try:
            conn = db()
            product = conn.execute(
                "SELECT price FROM products WHERE category = ? AND name = ? LIMIT 1",
                ("stars", f"استارز {amount} عدد")
            ).fetchone()
            user = conn.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            conn.close()

            price = int(product["price"] or 0) if product else 0
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

        with db() as con:
            product = con.execute(
                "SELECT price, active FROM products WHERE category='stars' AND name=? LIMIT 1",
                (f"استارز {amount} عدد",)
            ).fetchone()

        if not product or not product["active"] or int(product["price"] or 0) <= 0:
            await query.answer("محصول یا قیمت آن تنظیم نشده است.", show_alert=True)
            return

        price = int(product["price"])
        coupon = context.user_data.get("stars_coupon", {})
        if coupon.get("amount") == amount and coupon.get("final_price", 0) > 0:
            price = int(coupon["final_price"])
            discount = int(coupon.get("discount", 0))
        else:
            discount = 0
        await query.edit_message_text(
            "💳 پرداخت کارت‌به‌کارت | SEYED PREMIUM\n\n"
            f"⭐ محصول: {amount} استارز\n"
            f"💰 مبلغ: {toman(price)} تومان\n\n"
            "🏦 شماره کارت: هنوز تنظیم نشده است\n"
            "👤 نام صاحب کارت: هنوز تنظیم نشده است\n\n"
            "⚠️ تا زمان تنظیم اطلاعات کارت، پرداخت انجام نده.\n"
            "پس از تنظیم کارت، ارسال رسید و تأیید مدیر باید انجام شود.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ لغو سفارش", callback_data="stars_detail_back", style="danger")
            ]])
        )
        return

    if data.startswith("stars_pay_wallet_"):
        try:
            amount = int(data.rsplit("_", 1)[-1])
        except ValueError:
            await query.answer("بسته استارز معتبر نیست.", show_alert=True)
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
                    "SELECT id, price, active FROM products WHERE category=? AND name=? LIMIT 1",
                    ("stars", f"استارز {amount} عدد")
                ).fetchone()

                user_row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (user_id,)
                ).fetchone()

                if not product or not product["active"] or int(product["price"] or 0) <= 0:
                    con.rollback()
                    await query.answer("محصول یا قیمت آن تنظیم نشده است.", show_alert=True)
                    return

                if not user_row:
                    con.rollback()
                    await query.answer("حساب کاربری پیدا نشد.", show_alert=True)
                    return

                price = int(product["price"])
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

                cur = con.execute(
                    "INSERT INTO orders (user_id, product_id, price, status) VALUES (?, ?, ?, ?)",
                    (user_id, int(product["id"]), price, "پرداخت‌شده - در انتظار تحویل")
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
        await query.edit_message_text(
            "✏️ استارز به مقدار دلخواه | SEYED PREMIUM\n\n"
            "برای این گزینه هنوز روش قیمت‌گذاری و ثبت سفارش امن تعریف نشده است؛ بنابراین فعلاً سفارش مقدار دلخواه ثبت نمی‌شود.\n"
            "می‌توانی یکی از بسته‌های موجود را انتخاب کنی یا به منوی خدمات برگردی.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ انتخاب بسته‌های استارز", callback_data="service_stars")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
            ]),
        )
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
        for key in ("pending_product_id", "pending_stars_coupon", "stars_coupon", "pending_product_coupon", "pending_product_coupon_input"):
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
                reply_markup=main_menu(),
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
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="config_invoice_cancel", style="primary")],
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
            [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")],
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
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")]
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
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")],
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
                    [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")],
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
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")],
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
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")]
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
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")]
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
                SELECT id, name, price FROM products
                WHERE category=? AND active=1
                ORDER BY id
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
            gift_order = ["💝", "🧸", "🎁", "🌹", "🎂", "💐", "🚀", "🍾", "🏆", "💎", "💍"]
            gift_products = {}
            for product in rows:
                for emoji in gift_order:
                    if emoji in product["name"]:
                        gift_products[emoji] = product
                        break

            for i in range(0, len(gift_order), 3):
                row_buttons = []
                for emoji in gift_order[i:i + 3]:
                    product = gift_products.get(emoji)
                    if product:
                        button_text = f"{emoji} {product['price']:,}"
                        row_buttons.append(
                            InlineKeyboardButton(
                                button_text,
                                callback_data=f"buy_{product['id']}",
                                style={
                                    "💝": "danger",
                                    "🧸": "primary",
                                    "🎁": "success",
                                    "🌹": "danger",
                                    "🎂": "primary",
                                    "💐": "success",
                                    "🚀": "primary",
                                    "🍾": "success",
                                    "🏆": "success",
                                    "💎": "primary",
                                    "💍": "danger",
                                }.get(emoji, "success"),
                            )
                        )
                if row_buttons:
                    buttons.append(row_buttons)
        else:
            for product in rows:
                price_text = f"{product['price']:,} تومان" if product["price"] else "استعلام قیمت"
                button_text = (
                    product["name"]
                    if not product["price"] and "استعلام قیمت" in product["name"]
                    else f"{product['name']} | {price_text}"
                )
                buttons.append([
                    InlineKeyboardButton(
                        button_text,
                        callback_data=f"buy_{product['id']}",
                        style="success",
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

        context.user_data["pending_product_id"] = product_id
        context.user_data.pop("pending_product_coupon", None)
        context.user_data.pop("pending_product_coupon_input", None)
        prompt = product_details_prompt(product)
        product_keyboard = []
        if int(product["price"] or 0) > 0:
            product_keyboard.append([InlineKeyboardButton("🏷️ وارد کردن کد تخفیف", callback_data=f"product_coupon_enter_{product_id}")])
        product_keyboard.append([InlineKeyboardButton("🏠 لغو سفارش", callback_data="home")])
        await query.edit_message_text(
            f"🛍 محصول انتخاب‌شده: {product['name']}\n\n{prompt}\n\n"
            "برای لغو، /start را بزنید.",
            reply_markup=InlineKeyboardMarkup(product_keyboard)
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
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 آمار فروشگاه", callback_data="admin_stats")],
                [InlineKeyboardButton("🛍 محصولات و قیمت‌ها", callback_data="admin_products")],
                [InlineKeyboardButton("📦 آخرین سفارش‌ها", callback_data="admin_orders")],
                [InlineKeyboardButton("🏷️ مدیریت کدهای تخفیف", callback_data="admin_coupons")],
                [InlineKeyboardButton("⌨️ نوع منو: کیبورد", callback_data="admin_menu_set_keyboard")],
                [InlineKeyboardButton("💎 نوع منو: شیشه‌ای", callback_data="admin_menu_set_inline")],
                [InlineKeyboardButton("📝 ویرایش متن‌ها و دکمه‌ها", callback_data="uiadm_home")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
            ])
            await query.edit_message_text(
                "⚙️ پنل مدیریت SEYED PREMIUM",
                reply_markup=keyboard
            )
            return

        if data == "admin_menu_settings":
            await query.edit_message_text(
                menu_settings_text(),
                reply_markup=menu_settings_keyboard(),
            )
            return

        if data in ("admin_menu_set_keyboard", "admin_menu_set_inline"):
            selected = MENU_TYPE_KEYBOARD if data == "admin_menu_set_keyboard" else MENU_TYPE_INLINE
            set_main_menu_type(selected)
            confirmation = (
                "✅ نوع منو با موفقیت روی کیبورد معمولی تنظیم شد."
                if selected == MENU_TYPE_KEYBOARD
                else "✅ نوع منو با موفقیت روی منوی شیشه‌ای تنظیم شد."
            )
            await query.edit_message_text(
                menu_settings_text(confirmation),
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
                [InlineKeyboardButton("🎁 تخفیف پلکانی زیرمجموعه‌گیری", callback_data="admin_coupon_referral")],
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

        if data == "admin_coupon_referral":
            await query.answer("مدیریت تخفیف پلکانی زیرمجموعه‌گیری در نسخهٔ فعلی به جدول/تنظیمات جداگانه متصل نیست.", show_alert=True)
            await query.edit_message_text(
                "🎁 تخفیف پلکانی زیرمجموعه‌گیری\n\n"
                "برای اینکه این بخش واقعی و امن باشد، باید قواعد فعلی پاداش دعوت و ساختار ذخیره‌سازی آن مشخص شود. "
                "هیچ تنظیم ساختگی یا نمایشی اعمال نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_coupons")]])
            )
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
                [InlineKeyboardButton("🎁 تخفیف پلکانی زیرمجموعه‌گیری", callback_data="admin_coupon_referral")],
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
                [InlineKeyboardButton("🎁 تخفیف پلکانی زیرمجموعه‌گیری", callback_data="admin_coupon_referral")],
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

        if data == "admin_products" or data.startswith("admin_products_page_") or data.startswith("admin_p_"):
            if user_id != ADMIN_ID:
                await query.answer("دسترسی مجاز نیست.", show_alert=True)
                return

            # صفحه‌بندی قبل از پردازش سایر callbackها تشخیص داده می‌شود.
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
                "members": "👥 ممبر تلگرام",
                "reactions": "🔥 ری‌اکشن تلگرام",
                "views": "👀 ویو تلگرام",
                "instagram_followers": "📸 فالوور اینستاگرام",
                "instagram_likes": "❤️ لایک اینستاگرام",
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
                    row = con.execute("SELECT id, name, price, category FROM products WHERE id=?", (pid,)).fetchone()
                if not row:
                    await query.answer("محصول پیدا نشد.", show_alert=True)
                    return
                keyboard = [
                    [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"admin_p_name_{pid}")],
                    [InlineKeyboardButton("💰 ویرایش قیمت", callback_data=f"admin_p_price_{pid}")],
                    [InlineKeyboardButton("📂 ویرایش دسته‌بندی", callback_data=f"admin_p_cat_{pid}")],
                    [InlineKeyboardButton("↩️ بازگشت به محصولات", callback_data="admin_products")],
                ]
                await query.edit_message_text(
                    f"🛠 ویرایش محصول #{pid}\n\n"
                    f"نام: {row['name']}\nقیمت: {int(row['price'] or 0):,} تومان\nدسته: {row['category']}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
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
                prompt = "نام جدید محصول را بفرست:" if action == "edit_name" else "قیمت جدید را فقط به عدد و به تومان بفرست. برای قیمت نامشخص عدد 0 وارد کن:"
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
                    "SELECT id, category, name, price, active FROM products ORDER BY id LIMIT ? OFFSET ?",
                    (products_per_page, (products_page - 1) * products_per_page),
                ).fetchall()

            text = "🛍 مدیریت محصولات و قیمت‌ها | SEYED PREMIUM\n━━━━━━━━━━━━━━━━━━\n"
            text += f"📦 کل محصولات: {total_products}\n"
            text += f"📄 صفحه {products_page} از {total_pages}\n"
            text += "محصولات جدیدی که اضافه شوند، خودکار در همین فهرست نمایش داده می‌شوند.\n\n"
            keyboard = []

            for r in rows:
                price_value = int(r["price"] or 0)
                price_text = f"{price_value:,} تومان" if price_value > 0 else "تنظیم نشده"
                status = "🟢 فعال" if r["active"] else "🔴 غیرفعال"
                text += f"#{r['id']} | {r['name']}\n📂 {r['category']} | 💰 {price_text}\nوضعیت: {status}\n\n"
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
                           products.name
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

            keyboard = [
                [InlineKeyboardButton(
                    "✅ تأیید سفارش",
                    callback_data=f"admin_order_approve_{order_id}"
                )],
                [InlineKeyboardButton(
                    "❌ رد سفارش",
                    callback_data=f"admin_order_reject_{order_id}"
                )],
                [InlineKeyboardButton(
                    "🗑 حذف سفارش",
                    callback_data=f"admin_order_delete_confirm_{order_id}"
                )],
                [InlineKeyboardButton(
                    "⬅️ فهرست سفارش‌ها",
                    callback_data="admin_orders"
                )]
            ]

            await query.edit_message_text(
                f"📦 سفارش #{order['id']}\n\n"
                f"👤 مشتری: {order['user_id']}\n"
                f"🛍 محصول: {order['name'] or ('محصول #' + str(order['product_id']))}\n"
                f"💰 مبلغ: {order['price']:,} تومان\n"
                f"📌 وضعیت: {order['status']}\n"
                f"🕒 تاریخ: {order['created_at']}",
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
                con.execute("DELETE FROM orders WHERE id = ?", (order_id,))

            try:
                await context.bot.send_message(
                    chat_id=customer_id,
                    text=f"اطلاع‌رسانی SEYED PREMIUM: سفارش شماره #{order_id} توسط مدیریت حذف شد. برای پیگیری با پشتیبانی تماس بگیر."
                )
            except Exception:
                pass

            await query.edit_message_text(
                f"✅ سفارش #{order_id} حذف شد.",
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
            status = "تأیید شده" if approve else "رد شده"

            with db() as con:
                ensure_discount_schema(con)
                con.execute("BEGIN IMMEDIATE")
                order = con.execute(
                    "SELECT user_id, status FROM orders WHERE id = ?",
                    (order_id,)
                ).fetchone()

                if not order:
                    await query.edit_message_text(
                        "این سفارش پیدا نشد.",
                        reply_markup=back_menu()
                    )
                    return

                if approve and str(order["status"] or "") == "رد شده":
                    await query.answer("این سفارش قبلاً رد شده؛ درخواست جدید ثبت شود.", show_alert=True)
                    return
                customer_id = order["user_id"]
                if approve:
                    con.execute("UPDATE coupon_usage SET status='used' WHERE order_id=? AND status='pending'", (order_id,))
                elif str(order["status"] or "") not in ("رد شده", "تأیید شده"):
                    pending_use = con.execute(
                        "SELECT code FROM coupon_usage WHERE order_id=? AND status='pending' LIMIT 1",
                        (order_id,)
                    ).fetchone()
                    if pending_use:
                        con.execute("DELETE FROM coupon_usage WHERE order_id=? AND status='pending'", (order_id,))
                        con.execute("UPDATE discount_codes SET total_uses=MAX(0, COALESCE(total_uses, 0)-1) WHERE code=?", (pending_use["code"],))
                con.execute(
                    "UPDATE orders SET status = ? WHERE id = ?",
                    (status, order_id)
                )

            try:
                await context.bot.send_message(
                    chat_id=customer_id,
                    text=f"📦 وضعیت سفارش شما در SEYED PREMIUM تغییر کرد.\nشماره سفارش: #{order_id}\nوضعیت: {status}"
                )
            except Exception:
                pass

            await query.edit_message_text(
                f"✅ وضعیت سفارش #{order_id}: {status}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "📦 فهرست سفارش‌ها",
                        callback_data="admin_orders"
                    )]
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
    except Exception:
        logger.exception("Unhandled text-message failure; update_id=%s", getattr(update, "update_id", None))
        message = update.effective_message
        if message and update.effective_user and update.effective_user.id == ADMIN_ID:
            try:
                await message.reply_text("❌ خطایی هنگام پردازش پیام رخ داد؛ جزئیات در لاگ ثبت شد. برای ادامه دوباره تلاش کن یا /cancel بزن.")
            except Exception:
                logger.exception("Could not notify admin about text-message failure")
    finally:
        logger.info("PERF text_message: %.3fs",
                    time.perf_counter() - _handler_started)


async def cancel_coupon_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    message = update.effective_message

    edit_state = context.user_data.pop("coupon_edit_state", None)
    if edit_state:
        if message:
            await message.reply_text(
                "❌ ویرایش لغو شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به کد", callback_data="admin_coupon_view_" + str(edit_state["code"]))]])
            )
        return

    category_state = context.user_data.pop("coupon_category_add_state", None)
    if category_state:
        scope_state = context.user_data.get("coupon_scope_state")
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

    if context.user_data.get("admin_coupon_state"):
        context.user_data.pop("admin_coupon_state", None)
        if message:
            await message.reply_text("❌ ساخت کد تخفیف لغو شد. هیچ کدی ساخته نشد.")
    elif message:
        await message.reply_text("عملیات در حال انجامی برای لغو وجود ندارد.")


async def _text_message_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Admin workflows (including coupon expiry numeric replies) must not be blocked by channel checks.
    if not user or user.id != ADMIN_ID:
        if not await require_channel_membership(update, context):
            return
    message = update.effective_message
    if user:
        register_user(user)

    # Route menu taps before free-text order/coupon workflows can consume them.
    if message and message.text and await dispatch_keyboard_menu_text(update, context):
        return

    # Admin coupon panel: create / activate / deactivate.
    # لغو ساخت کد تخفیف از داخل پیام متنی
    if message and message.text and message.text.strip().lower() == "/cancel":
        if user and user.id == ADMIN_ID and context.user_data.get("admin_coupon_state"):
            context.user_data.pop("admin_coupon_state", None)
            await message.reply_text("❌ ساخت کد تخفیف لغو شد.")
            return

    # افزودن دستهٔ پویا از داخل صفحهٔ دسته‌های همین کد تخفیف
    category_add_state = context.user_data.get("coupon_category_add_state")
    if message and message.text and category_add_state and user and user.id == ADMIN_ID:
        answer = message.text.strip()
        if answer.lower() == "/cancel":
            context.user_data.pop("coupon_category_add_state", None)
            scope_state = context.user_data.get("coupon_scope_state")
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
                scope_state = context.user_data.get("coupon_scope_state")
                if scope_state:
                    row = con.execute("SELECT category FROM discount_codes WHERE code=?", (scope_state["code"],)).fetchone()
                    selected = coupon_selected_categories(row["category"]) if row else set()
                else:
                    selected = set()
            context.user_data.pop("coupon_category_add_state", None)
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

    coupon_edit_state = context.user_data.get("coupon_edit_state")
    if message and message.text and coupon_edit_state and user and user.id == ADMIN_ID:
        answer = normalize_ascii_digits(message.text.strip())
        code = str(coupon_edit_state["code"])
        field = coupon_edit_state["field"]
        if answer.lower() == "/cancel":
            context.user_data.pop("coupon_edit_state", None)
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

            context.user_data.pop("coupon_edit_state", None)
            await message.reply_text(
                success_text or "✅ تغییرات ذخیره شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 مشاهده کد", callback_data="admin_coupon_view_" + code)]])
            )
        except LookupError:
            context.user_data.pop("coupon_edit_state", None)
            await message.reply_text("❌ کد موردنظر پیدا نشد.")
        except Exception:
            logger.exception("Coupon edit failed: code=%s field=%s", code, field)
            await message.reply_text("❌ تغییر ذخیره نشد. مقدار را بررسی کن و دوباره تلاش کن؛ جزئیات در لاگ ثبت شد.")
        return

    admin_state = context.user_data.get("admin_coupon_state")
    if message and message.text and admin_state and user and user.id == ADMIN_ID:
        answer = normalize_ascii_digits(message.text.strip())
        if answer.lower() == "/cancel":
            context.user_data.pop("admin_coupon_state", None)
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
                context.user_data.pop("admin_coupon_state", None)
                return
            except (ValueError, OverflowError):
                await message.reply_text("❌ تاریخ شمسی معتبر و آینده وارد کن. فرمت: YYYY-MM-DD؛ مثال: 1405-08-07")
                return
            except Exception:
                logger.exception("Coupon save failed after Jalali expiry selection")
                await message.reply_text("❌ ذخیرهٔ کد انجام نشد؛ جزئیات خطا در لاگ ثبت شد. اطلاعات مرحله حفظ شده است؛ دوباره تلاش کن یا /cancel بزن.")
                return
            context.user_data.pop("admin_coupon_state", None)
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
                context.user_data.pop("admin_coupon_state", None)
                return
            except ValueError:
                await message.reply_text("❌ تعداد ساعت باید عدد صحیح بین ۱ تا ۸۷۶۰۰۰ باشد. مثال: 6؛ برای لغو /cancel را بفرست.")
                return
            except Exception:
                logger.exception("Coupon save failed after hourly expiry selection")
                await message.reply_text("❌ ذخیرهٔ کد انجام نشد؛ جزئیات خطا در لاگ ثبت شد. اطلاعات مرحله حفظ شده است؛ دوباره تلاش کن یا /cancel بزن.")
                return
            context.user_data.pop("admin_coupon_state", None)
            await message.reply_text(
                coupon_created_message(record),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 بازگشت به لیست کدها", callback_data="admin_coupons")]])
            )
            return

    # Generic product coupon input: applies to catalog products beyond Stars.
    pending_product_coupon_input = context.user_data.get("pending_product_coupon_input")
    if message and message.text and pending_product_coupon_input is not None and user:
        answer_code = message.text.strip().upper()
        if answer_code.lower() == "/cancel":
            context.user_data.pop("pending_product_coupon_input", None)
            context.user_data.pop("pending_product_coupon", None)
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
        context.user_data["pending_product_coupon"] = {
            "product_id": product_id_for_coupon, "code": validation["code"]
        }
        context.user_data.pop("pending_product_coupon_input", None)
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
    pending_coupon = context.user_data.get("pending_stars_coupon")
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
            context.user_data["stars_coupon"] = {
                "amount": amount, "code": validation["code"], "discount": discount,
                "final_price": int(validation["final_price"])
            }
            context.user_data.pop("pending_stars_coupon", None)
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
    ui_st = context.user_data.get("uiadm_state")
    if message and message.text and ui_st:
        if not user or user.id != ADMIN_ID:
            context.user_data.pop("uiadm_state", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return
        value = message.text.strip()
        if value == "/cancel":
            context.user_data.pop("uiadm_state", None)
            await message.reply_text("عملیات لغو شد.")
            return
        from ui_config import load as ui_load, save as ui_save
        kind = ui_st.get("kind")
        if kind == "text":
            key = ui_st["key"]
            settings = load_bot_settings()
            settings[key] = value
            save_bot_settings(settings)
            context.user_data.pop("uiadm_state", None)
            await message.reply_text("✅ متن ذخیره شد.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("مدیریت متن‌ها و دکمه‌ها", callback_data="uiadm_home")]
            ]))
            return
        if kind == "rename":
            cfg = ui_load()
            items = cfg.get("menus", {}).get(ui_st["menu"], [])
            item = next((x for x in items if x.get("id") == ui_st["id"]), None)
            if item:
                item["text"] = value[:64]
                ui_save(cfg)
            context.user_data["uiadm_state"] = {"kind": "button", "menu": ui_st["menu"], "id": ui_st["id"]}
            await message.reply_text("✅ نام ذخیره شد.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بازگشت به منو", callback_data=f"uiadm_menu_{ui_st['menu']}")]
            ]))
            return
        if kind == "add":
            step = ui_st.get("step")
            if step == "id":
                ui_st["id"] = value
                ui_st["step"] = "text"
                context.user_data["uiadm_state"] = ui_st
                await message.reply_text("متن نمایشی دکمه را بفرست:")
                return
            if step == "text":
                ui_st["text"] = value[:64]
                ui_st["step"] = "callback"
                context.user_data["uiadm_state"] = ui_st
                await message.reply_text("شناسهٔ عملکرد موجود را دقیق بفرست؛ مثلاً home یا service_stars:")
                return
            if step == "callback":
                cfg = ui_load()
                menu = ui_st["menu"]
                items = cfg.setdefault("menus", {}).setdefault(menu, [])
                if not ui_st.get("id") or any(x.get("id") == ui_st["id"] for x in items):
                    await message.reply_text("❌ شناسه خالی است یا تکراری است. /cancel برای لغو")
                    return
                items.append({"id": ui_st["id"], "text": ui_st["text"], "callback": value, "enabled": True})
                ui_save(cfg)
                context.user_data.pop("uiadm_state", None)
                await message.reply_text("✅ دکمه ذخیره شد. فقط اگر عملکردش در کد موجود باشد کار می‌کند.")
                return


    if message and message.text and context.user_data.get("product_admin_state"):
        if not user or user.id != ADMIN_ID:
            context.user_data.pop("product_admin_state", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return

        value = message.text.strip()
        if value == "/cancel":
            context.user_data.pop("product_admin_state", None)
            await message.reply_text("عملیات لغو شد.")
            return

        state = context.user_data.get("product_admin_state", {})
        action = state.get("action")

        if action == "add_name":
            if not value or len(value) > 150:
                await message.reply_text("نام محصول باید بین 1 تا 150 نویسه باشد. دوباره بفرست:")
                return
            state["name"] = value
            state["action"] = "add_price"
            context.user_data["product_admin_state"] = state
            await message.reply_text("قیمت محصول را فقط به عدد و به تومان بفرست. برای قیمت نامشخص عدد 0 وارد کن:")
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
                cur = con.execute(
                    "INSERT INTO products (category, name, price, active) VALUES (?, ?, ?, 1)",
                    (state["category"], state["name"], price)
                )
                new_id = cur.lastrowid
            context.user_data.pop("product_admin_state", None)
            await message.reply_text(f"✅ محصول با شناسه #{new_id} ثبت شد.\nنام: {state['name']}\nقیمت: {price:,} تومان\nدسته: {state['category']}\n\nبرای مدیریت، دستور /admin را بزن.")
            return

        if action == "edit_name":
            if not value or len(value) > 150:
                await message.reply_text("نام محصول باید بین 1 تا 150 نویسه باشد. دوباره بفرست:")
                return
            with db() as con:
                cur = con.execute("UPDATE products SET name=? WHERE id=?", (value, int(state["product_id"])))
            context.user_data.pop("product_admin_state", None)
            await message.reply_text("✅ نام محصول ویرایش شد." if cur.rowcount else "محصول پیدا نشد.")
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
            context.user_data.pop("product_admin_state", None)
            await message.reply_text("✅ قیمت محصول ویرایش شد." if cur.rowcount else "محصول پیدا نشد.")
            return

    if message and message.text and context.user_data.get("editor_wait_key"):
        if user.id != ADMIN_ID:
            context.user_data.pop("editor_wait_key", None)
            await message.reply_text("⛔ دسترسی مجاز نیست.")
            return
        key = context.user_data.pop("editor_wait_key")
        new_text = message.text.strip()
        if key not in EDITABLE_TEXTS or not new_text:
            await message.reply_text("❌ متن خالی یا گزینه نامعتبر است.")
            return
        if len(new_text) > 3500:
            context.user_data["editor_wait_key"] = key
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

    if message and message.text and context.user_data.get("vip_wait_volume"):
        raw = message.text.strip()
        if not raw.isdigit() or not 10 <= int(raw) <= 1000:
            await message.reply_text("❌ حجم باید عددی بین 10 تا 1000 گیگ باشد.")
            return
        volume = int(raw)
        context.user_data["vip_volume"] = volume
        context.user_data["vip_wait_volume"] = False
        context.user_data["vip_wait_duration"] = True
        await message.reply_text(
            "🧮 پلن دلخواه — انتخاب مدت\n\n"
            f"📊 حجم انتخابی: {volume} گیگ\n\n"
            "چند روز می‌خوای؟ یک عدد بین 30 تا 90 روز بفرست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")]
            ]),
        )
        return

    if message and message.text and context.user_data.get("vip_wait_duration"):
        raw = message.text.strip()
        if not raw.isdigit() or not 30 <= int(raw) <= 90:
            await message.reply_text("❌ مدت باید عددی بین 30 تا 90 روز باشد.")
            return
        duration = int(raw)
        volume = context.user_data.get("vip_volume")
        if volume is None or not 10 <= int(volume) <= 1000:
            context.user_data.pop("vip_wait_duration", None)
            await message.reply_text("اطلاعات حجم پیدا نشد؛ پلن دلخواه را دوباره انتخاب کن.")
            return

        price = int(volume) * 5000
        context.user_data["vip_duration"] = duration
        context.user_data["vip_price"] = price
        context.user_data["vip_invoice_amount"] = price
        context.user_data["vip_plan_type"] = "custom"
        context.user_data["vip_wait_duration"] = False
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
                [InlineKeyboardButton("❌ لغو عملیات", callback_data="vip_cancel", style="primary")],
                [InlineKeyboardButton("⬅️ بازگشت به VIP", callback_data="config_vip", style="primary")],
            ]),
        )
        return

    if message and message.text and context.user_data.get("config_wait_volume"):
        raw = message.text.strip()
        if not raw.isdigit() or not 10 <= int(raw) <= 500:
            await message.reply_text("❌ حجم باید عددی بین ۱۰ تا ۵۰۰ گیگ باشد.")
            return
        context.user_data["config_volume"] = int(raw)
        context.user_data.pop("config_wait_volume", None)
        context.user_data["config_wait_duration"] = True
        await message.reply_text(
            f"🧮 پلن دلخواه — انتخاب مدت\n\n"
            f"📊 حجم انتخابی: {int(raw)} گیگ\n\n"
            "چند روز می‌خواهی؟ عددی بین ۳۰ تا ۳۶۵ بفرست."
        )
        return

    if message and message.text and context.user_data.get("config_wait_duration"):
        raw = message.text.strip()
        if not raw.isdigit() or not 30 <= int(raw) <= 365:
            await message.reply_text("❌ مدت باید عددی بین ۳۰ تا ۳۶۵ روز باشد.")
            return
        duration = int(raw)
        volume = context.user_data.get("config_volume")
        if volume is None or not 10 <= volume <= 500:
            context.user_data.pop("config_wait_duration", None)
            await message.reply_text("اطلاعات حجم پیدا نشد؛ پلن دلخواه را دوباره انتخاب کن.")
            return

        price = volume * 2000
        context.user_data.pop("config_wait_duration", None)
        context.user_data["config_plan_type"] = "custom"
        context.user_data["config_price"] = price
        context.user_data["config_volume"] = volume
        context.user_data["config_duration"] = duration
        context.user_data["config_invoice_open"] = False
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

    product_id = context.user_data.get("pending_product_id")
    if product_id is not None and message and message.text:
        details = message.text.strip()
        if not details:
            await message.reply_text("لطفاً اطلاعات سفارش را ارسال کنید.")
            return

        pending_coupon = context.user_data.get("pending_product_coupon") or {}
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
                    context.user_data.pop("pending_product_id", None)
                    context.user_data.pop("pending_product_coupon", None)
                    await message.reply_text(
                        "این محصول دیگر فعال نیست. دوباره از منو انتخاب کنید.",
                        reply_markup=main_menu(),
                    )
                    return

                category = (product["category"] or "").lower()
                if category in ("premium", "stars", "gifts"):
                    if not details.startswith("@") or len(details) < 2:
                        await message.reply_text("لطفاً آیدی تلگرام را با @ ارسال کنید.\nمثال: @SeyedPro_AIA")
                        return
                elif category == "boost":
                    if "t.me/" not in details.lower() and "telegram.me/" not in details.lower():
                        await message.reply_text("لطفاً لینک گروه یا کانال تلگرام را ارسال کنید؛ لینک عمومی یا دعوت خصوصی.")
                        return

                original_price = int(product["price"] or 0)
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

                cur = con.execute(
                    "INSERT INTO orders (user_id, product_id, price, status, coupon_code, coupon_discount) VALUES (?, ?, ?, ?, ?, ?)",
                    (user.id, int(product_id), order_price, "در انتظار بررسی", coupon_code, coupon_discount),
                )
                order_id = cur.lastrowid
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

        context.user_data.pop("pending_product_id", None)
        context.user_data.pop("pending_product_coupon", None)
        context.user_data.pop("pending_product_coupon_input", None)
        price_text = f"{order_price:,} تومان" if original_price > 0 else "نیازمند استعلام"
        coupon_summary = (f"\nکد تخفیف: {coupon_code}\nمیزان تخفیف: {coupon_discount:,} تومان\nقیمت اصلی: {original_price:,} تومان"
                          if coupon_code else "")

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"📦 سفارش جدید #{order_id}\n\n"
                    f"کاربر: {user.full_name}\n"
                    f"شناسه کاربر: {user.id}\n"
                    f"محصول: {product['name']}\n"
                    f"اطلاعات سفارش: {details}\n"
                    f"قیمت نهایی: {price_text}{coupon_summary}\n"
                    "وضعیت: در انتظار بررسی مدیر"
                ),
            )
        except Exception:
            logger.exception("Could not notify admin about order")

        await message.reply_text(
            f"✅ درخواست شما ثبت شد.\n\n"
            f"📦 شماره سفارش: #{order_id}\n"
            f"🛍 محصول: {product['name']}\n"
            f"📨 اطلاعات ارسالی: {details}\n"
            f"💰 مبلغ پس از تخفیف: {price_text}"
            + (f"\n🏷️ کد: {coupon_code}\n🎁 تخفیف: {coupon_discount:,} تومان" if coupon_code else "")
            + "\n\nمدیر سفارش را بررسی می‌کند و برای هماهنگی پرداخت اطلاع می‌دهد.",
            reply_markup=main_menu(),
        )
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))



    print("SEYED PREMIUM روشن شد. برای توقف Ctrl+C را بزن.")
    app.run_polling()


if __name__ == "__main__":
    main()


from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def stars_menu():
    packages = [
        (50, "۵۰", "primary"),
        (75, "۷۵", None),
        (100, "۱۰۰", "primary"),
        (150, "۱۵۰", None),
        (250, "۲۵۰", "primary"),
        (350, "۳۵۰", None),
        (500, "۵۰۰", "primary"),
        (1000, "۱۰۰۰", None),
        (2500, "۲۵۰۰", "primary"),
        (5000, "۵۰۰۰", None),
        (10000, "۱۰ هزار", "primary"),
        (50000, "۵۰ هزار", None),
    ]

    buttons = []
    for amount, label, color in packages:
        kwargs = {"callback_data": f"stars_pack_{amount}"}
        if color:
            kwargs["style"] = color
        buttons.append([
            InlineKeyboardButton(f"🎁 {label} استارز", **kwargs)
        ])

    buttons.append([
        InlineKeyboardButton(
            "✏️ استارز به مقدار دلخواه",
            callback_data="stars_custom",
            style="success"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "↩️ بازگشت",
            callback_data="back_to_services",
            style="danger"
        )
    ])

    return InlineKeyboardMarkup(buttons)


def stars_detail_menu(amount):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "💎 برای خودم",
            callback_data=f"stars_recipient_self_{amount}",
            style="primary"
        )],
        [InlineKeyboardButton(
            "🎁 برای شخص دیگر",
            callback_data=f"stars_recipient_other_{amount}",
            style="primary"
        )],
        [InlineKeyboardButton(
            "🏷️ وارد کردن کد تخفیف",
            callback_data=f"stars_discount_{amount}",
            style="primary"
        )],
        [InlineKeyboardButton(
            "💰 شارژ کیف پول",
            callback_data="wallet",
            style="success"
        )],
        [InlineKeyboardButton(
            "↩️ بازگشت",
            callback_data="stars_detail_back",
            style="danger"
        )],
    ])

def stars_text():
    return (
        "🎁 استارز - انتخاب بسته\n\n"
        "بسته موردنظرت رو انتخاب کن 👇\n\n"
        "🔐 بدون نیاز به ورود به اکانت\n"
        "⚡️ تحویل سریع و اتوماتیک\n"
        "🍦 امکان سفارش با مقدار دلخواه\n"
        "🔘 دارای کش‌بک\n"
        "💬 پشتیبانی در تمام مراحل"
    )

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from ui_config import buttons as configured_buttons


def service_menu(text_fn=None):
    items = configured_buttons("services")

    stars = None
    premium = None
    others = []

    for item in items:
        text = str(item.get("text", ""))
        callback = str(item.get("callback", "")).lower()
        key = f"{text} {callback}".lower()

        button = InlineKeyboardButton(
            text,
            callback_data=item["callback"],
            style="success" if (
                "stars" in key or "استارز" in key or "تلگرام پرمیوم" in key
                or "premium" in key
            ) else "primary",
        )

        if "stars" in key or "استارز" in key:
            stars = button
        elif "premium" in key or "پرمیوم" in key:
            premium = button
        else:
            others.append(button)

    rows = []

    if stars and premium:
        rows.append([stars, premium])
    else:
        if stars:
            rows.append([stars])
        if premium:
            rows.append([premium])

    for button in others:
        rows.append([button])

    back_text = text_fn("back_button") if text_fn else "↩️ بازگشت به منو"
    rows.append([
        InlineKeyboardButton(
            back_text,
            callback_data="home",
            style="danger",
        )
    ])

    return InlineKeyboardMarkup(rows)

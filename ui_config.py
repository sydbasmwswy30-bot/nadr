import json
from pathlib import Path
from copy import deepcopy

FILE = Path(__file__).with_name("bot_settings.json")

DEFAULTS = {
    "menus": {
        "main": [
            {"id": "buy_service", "text": "🛍 خرید سرویس", "callback": "cat_stars", "enabled": True},
            {"id": "wallet", "text": "👛 کیف پول", "callback": "wallet", "enabled": True},
            {"id": "orders", "text": "📚 سفارش‌های من", "callback": "orders", "enabled": True},
            {"id": "topup", "text": "💳 شارژ کیف پول", "callback": "topup", "enabled": True},
            {"id": "profile", "text": "👤 پروفایل من", "callback": "account", "enabled": True},
            {"id": "free_stars", "text": "⭐ استارز رایگان", "callback": "free_stars", "enabled": True},
            {"id": "numbers", "text": "📱 شماره مجازی", "callback": "cat_numbers", "enabled": True},
            {"id": "support", "text": "🎧 پشتیبانی", "callback": "support", "enabled": True},
            {"id": "config", "text": "🌐 خرید کانفیگ", "callback": "config_menu", "enabled": True},
            {"id": "faq", "text": "❓ سوالات متداول", "callback": "faq", "enabled": True}
        ],
        "services": [
            {"id": "stars", "text": "⭐ استارز تلگرام", "callback": "service_stars", "enabled": True},
            {"id": "premium", "text": "💎 پرمیوم تلگرام", "callback": "service_premium", "enabled": True},
            {"id": "gifts", "text": "🎁 گیفت تلگرام", "callback": "service_gifts", "enabled": True},
            {"id": "boost", "text": "🚀 بوست تلگرام", "callback": "service_boost", "enabled": True},
            {"id": "members", "text": "👥 ممبر تلگرام", "callback": "service_members", "enabled": True},
            {"id": "reactions", "text": "🔥 ری‌اکشن تلگرام", "callback": "service_reactions", "enabled": True},
            {"id": "views", "text": "👀 ویو تلگرام", "callback": "service_views", "enabled": True},
            {"id": "instagram_followers", "text": "📸 فالوور اینستاگرام", "callback": "service_instagram_followers", "enabled": True},
            {"id": "instagram_likes", "text": "❤️ لایک اینستاگرام", "callback": "service_instagram_likes", "enabled": True}
        ]
    },
    "texts": {}
}

def load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}

    changed = False
    if not isinstance(data.get("menus"), dict):
        data["menus"] = deepcopy(DEFAULTS["menus"])
        changed = True
    else:
        for name, buttons in DEFAULTS["menus"].items():
            if name not in data["menus"]:
                data["menus"][name] = deepcopy(buttons)
                changed = True

    if not isinstance(data.get("texts"), dict):
        data["texts"] = {}
        changed = True

    if changed:
        save(data)
    return data

def save(data):
    temp = FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp.replace(FILE)

def text(key, default=None):
    data = load()
    if key in data.get("texts", {}):
        return str(data["texts"][key])
    return default if default is not None else key

def buttons(menu):
    data = load()
    items = data.get("menus", {}).get(menu, [])
    return [
        dict(item) for item in items
        if isinstance(item, dict) and item.get("enabled", True)
    ]

def edit_text(key, value):
    data = load()
    data.setdefault("texts", {})[key] = str(value)
    save(data)

def add_button(menu, button_id, label, callback):
    if not all(isinstance(x, str) and x.strip()
               for x in (menu, button_id, label, callback)):
        raise ValueError("تمام فیلدها باید پر باشند.")

    data = load()
    items = data.setdefault("menus", {}).setdefault(menu, [])

    if any(x.get("id") == button_id for x in items):
        raise ValueError("این شناسه قبلاً ثبت شده است.")

    items.append({
        "id": button_id,
        "text": label,
        "callback": callback,
        "enabled": True
    })
    save(data)

def remove_button(menu, button_id):
    data = load()
    items = data.get("menus", {}).get(menu, [])
    before = len(items)
    data["menus"][menu] = [
        x for x in items if x.get("id") != button_id
    ]
    save(data)
    return len(data["menus"][menu]) < before

def move_button(menu, button_id, position):
    data = load()
    items = data.get("menus", {}).get(menu, [])
    index = next((i for i, x in enumerate(items)
                  if x.get("id") == button_id), None)
    if index is None:
        return False
    item = items.pop(index)
    position = max(0, min(int(position), len(items)))
    items.insert(position, item)
    save(data)
    return True

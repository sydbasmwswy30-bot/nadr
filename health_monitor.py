"""Read-only operational diagnostics for SEYED PREMIUM.

No secrets, API keys, bot tokens, receipt contents, usernames or customer
identifiers are read or returned by this module.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pricing_engine
from backup_manager import latest_backup


REQUIRED_TABLES = ("users", "products", "orders", "app_settings")


def inspect_database(db_path: str | Path, backup_dir: str | Path | None = None) -> dict[str, Any]:
    path = Path(db_path).expanduser()
    result: dict[str, Any] = {
        "exists": path.is_file(),
        "path": str(path),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "integrity": "not_checked",
        "missing_tables": list(REQUIRED_TABLES),
        "counts": {},
        "journal_mode": "unknown",
        "disk_free_bytes": None,
        "latest_backup": None,
        "backup_age_seconds": None,
        "error": "",
    }
    try:
        if not path.is_file():
            result["error"] = "فایل دیتابیس پیدا نشد."
            return result

        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as con:
            con.execute("PRAGMA query_only=ON")
            integrity = con.execute("PRAGMA integrity_check").fetchone()
            result["integrity"] = str(integrity[0]) if integrity else "unknown"
            tables = {
                str(row[0]) for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            result["missing_tables"] = [name for name in REQUIRED_TABLES if name not in tables]
            for table in ("users", "products", "orders"):
                if table in tables:
                    # Table identifiers are selected only from the fixed allow-list above.
                    result["counts"][table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            mode = con.execute("PRAGMA journal_mode").fetchone()
            result["journal_mode"] = str(mode[0]) if mode else "unknown"

        try:
            result["disk_free_bytes"] = int(shutil.disk_usage(path.parent).free)
        except OSError:
            pass

        if backup_dir is not None:
            backup = latest_backup(backup_dir)
            result["latest_backup"] = backup
            if backup:
                age = max(0.0, datetime.now(timezone.utc).timestamp() - float(backup["modified_at"]))
                result["backup_age_seconds"] = age
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return result


def inspect_pricing(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    try:
        fx_ok = pricing_engine.fx_feed_usable(cfg)
    except Exception:
        fx_ok = False
    try:
        stars_ok = pricing_engine.stars_feed_usable(cfg)
    except Exception:
        stars_ok = False
    premium = {}
    for months in pricing_engine.PREMIUM_PLANS:
        try:
            premium[str(months)] = pricing_engine.premium_feed_usable(cfg, months)
        except Exception:
            premium[str(months)] = False

    prices = cfg.get("catalog_prices") if isinstance(cfg.get("catalog_prices"), dict) else {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "fx_ok": bool(fx_ok),
        "stars_ok": bool(stars_ok),
        "premium_ok": premium,
        "fx_source": str(cfg.get("fx_source_name") or "نامشخص")[:80],
        "catalog_source": str(prices.get("catalog_source") or "نامشخص")[:100],
        "margin_percent": str(cfg.get("margin_percent", pricing_engine.DEFAULT_MARGIN_PERCENT)),
        "fee_percent": str(cfg.get("fee_percent", pricing_engine.DEFAULT_FEE_PERCENT)),
        "refresh_interval_minutes": int(cfg.get("refresh_interval_minutes") or pricing_engine.REFRESH_INTERVAL_MINUTES),
        "fx_age_seconds": pricing_engine.timestamp_age(cfg.get("fx_source_updated_at")),
        "stars_age_seconds": pricing_engine.timestamp_age(cfg.get("catalog_updated_at")),
        "premium_age_seconds": pricing_engine.timestamp_age(cfg.get("premium_catalog_updated_at")),
        "fx_error": str(cfg.get("fx_error") or "")[:180],
        "catalog_error": str(cfg.get("catalog_error") or "")[:180],
        "premium_error": str(cfg.get("premium_catalog_error") or "")[:180],
    }


def _age_label(seconds: float | None) -> str:
    if seconds is None:
        return "زمان دریافت ثبت نشده"
    minutes = int(seconds // 60)
    return f"{minutes} دقیقه قبل"


def format_health_report(db_path: str | Path, backup_dir: str | Path,
                         pricing_config: dict[str, Any] | None) -> str:
    db = inspect_database(db_path, backup_dir)
    pricing = inspect_pricing(pricing_config)
    lines = [
        "🩺 گزارش سلامت SEYED PREMIUM",
        "",
        "💾 دیتابیس:",
        f"وضعیت فایل: {'✅ موجود' if db['exists'] else '❌ پیدا نشد'}",
    ]
    if db["exists"]:
        lines.extend([
            f"حجم: {db['size_bytes']:,} بایت",
            f"سلامت SQLite: {'✅ سالم' if db['integrity'] == 'ok' else '⚠️ ' + str(db['integrity'])}",
            f"حالت ژورنال: {db['journal_mode']}",
        ])
        if db["missing_tables"]:
            lines.append("جدول‌های لازمِ پیدا نشده: " + "، ".join(db["missing_tables"]))
        counts = db["counts"]
        if counts:
            lines.append(
                "تعداد رکوردها: "
                + " | ".join(f"{name}: {count:,}" for name, count in counts.items())
            )
    if db["disk_free_bytes"] is not None:
        lines.append(f"فضای آزاد دیسک: {db['disk_free_bytes']:,} بایت")
    backup = db["latest_backup"]
    if backup:
        lines.append(
            f"آخرین بکاپ محلی: ✅ {_age_label(db['backup_age_seconds'])} "
            f"({backup['size_bytes']:,} بایت)"
        )
    else:
        lines.append("آخرین بکاپ محلی: ⚠️ هنوز بکاپی پیدا نشد")

    lines.extend([
        "",
        "💱 قیمت‌گذاری:",
        f"محاسبه خودکار: {'روشن' if pricing['enabled'] else 'خاموش'}",
        f"نرخ تتر: {'✅ تازه و معتبر' if pricing['fx_ok'] else '❌ نامعتبر/منقضی'}"
        f" | منبع: {pricing['fx_source']} | {_age_label(pricing['fx_age_seconds'])}",
        f"کاتالوگ استارز: {'✅ معتبر' if pricing['stars_ok'] else '❌ تأیید نشده/منقضی'}"
        f" | منبع: {pricing['catalog_source']} | {_age_label(pricing['stars_age_seconds'])}",
    ])
    for months in pricing_engine.PREMIUM_PLANS:
        status = "✅ معتبر" if pricing["premium_ok"][str(months)] else "❌ نامعتبر/منقضی"
        lines.append(f"پرمیوم {months} ماهه: {status}")
    lines.extend([
        f"سود: {pricing['margin_percent']}٪ | کارمزد: {pricing['fee_percent']}٪",
        f"فاصله به‌روزرسانی: {pricing['refresh_interval_minutes']} دقیقه",
    ])

    warnings = []
    if not db["exists"] or db["integrity"] != "ok" or db["missing_tables"]:
        warnings.append("دیتابیس نیازمند بررسی است.")
    if db["error"]:
        warnings.append("خطای دیتابیس: " + db["error"])
    if not backup:
        warnings.append("بکاپی ثبت نشده است.")
    if not pricing["fx_ok"]:
        warnings.append("نرخ تتر برای سفارش جدید قابل استفاده نیست.")
    if not pricing["stars_ok"]:
        warnings.append("قیمت استارز برای سفارش جدید قابل استفاده نیست.")
    if not all(pricing["premium_ok"].values()):
        warnings.append("حداقل یکی از پلن‌های پرمیوم قیمت معتبر ندارد.")
    if warnings:
        lines.extend(["", "⚠️ موارد نیازمند رسیدگی:"])
        lines.extend("• " + item for item in warnings)
    else:
        lines.extend(["", "✅ بررسی‌های پایه بدون هشدار بودند."])
    lines.extend([
        "",
        "این گزارش وضعیت محلی را بررسی می‌کند؛ موفقیت پرداخت، API زنده یا تحویل واقعی را تضمین نمی‌کند.",
    ])
    return "\n".join(lines)

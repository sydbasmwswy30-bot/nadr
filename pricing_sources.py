"""Supplier catalog adapters and trust-aware catalog merging.

Network I/O stays in bot.py; this module validates the supplier payload before
any value is allowed into the central checkout pricing engine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pricing_engine


def extract_supplier_catalog_timestamp(payload: dict[str, Any], catalog: dict[str, Any],
                                       *, now: datetime | None = None) -> str:
    """Require a verifiable source time or age; never substitute HTTP receipt time."""
    containers: list[dict[str, Any]] = [payload, catalog]
    for obj in (payload, catalog):
        for key in ("meta", "metadata", "source", "data_source"):
            nested = obj.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    for obj in containers:
        age = obj.get("age_sec", obj.get("age_seconds"))
        if age is None:
            continue
        try:
            seconds = float(age)
        except (TypeError, ValueError):
            raise ValueError("سن کاتالوگ تأمین‌کننده نامعتبر است.")
        if not 0 <= seconds <= pricing_engine.SOURCE_MAX_AGE_SECONDS:
            raise ValueError("کاتالوگ تأمین‌کننده بیش از ۶۰ دقیقه عمر دارد.")
        return (current - timedelta(seconds=seconds)).isoformat(timespec="seconds")

    for obj in containers:
        for key in ("updated_at", "timestamp", "last_updated", "generated_at", "fetched_at", "as_of", "updatedAt"):
            raw = obj.get(key)
            if raw in (None, ""):
                continue
            parsed = pricing_engine.parse_utc_timestamp(raw)
            if parsed is None:
                raise ValueError("زمان کاتالوگ تأمین‌کننده قابل‌تشخیص نیست.")
            age = (current - parsed).total_seconds()
            if age < -60 or age > pricing_engine.SOURCE_MAX_AGE_SECONDS:
                raise ValueError("کاتالوگ تأمین‌کننده قدیمی یا دارای زمان نامعتبر است.")
            return parsed.isoformat(timespec="seconds")
    raise ValueError("API تأمین‌کننده زمان واقعی کاتالوگ را اعلام نکرد؛ قیمت برای فروش تأیید نمی‌شود.")


def parse_tgsuperstars_catalog(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Validate authenticated supplier data, accepting only explicit USDT costs."""
    if not isinstance(payload, dict):
        raise ValueError("پاسخ تأمین‌کننده ساختار JSON معتبر ندارد.")
    catalog = payload.get("data", payload)
    if not isinstance(catalog, dict):
        raise ValueError("بخش data کاتالوگ تأمین‌کننده معتبر نیست.")
    payload_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    catalog_meta = catalog.get("meta") if isinstance(catalog.get("meta"), dict) else {}
    stale = any((
        payload.get("unavailable") is True, catalog.get("unavailable") is True,
        payload.get("stale") is True, catalog.get("stale") is True,
        payload_meta.get("stale") is True, catalog_meta.get("stale") is True,
        str(payload.get("source_status", "")).lower() in {"stale", "unavailable"},
        str(catalog.get("source_status", "")).lower() in {"stale", "unavailable"},
    ))
    if stale:
        raise ValueError("تأمین‌کننده دادهٔ قدیمی یا ناموجود اعلام کرد.")
    source_updated_at = extract_supplier_catalog_timestamp(payload, catalog, now=now)

    stars_info = catalog.get("stars") or {}
    if not isinstance(stars_info, dict):
        raise ValueError("ساختار قیمت استارز در کاتالوگ معتبر نیست.")
    currency = str(stars_info.get("currency") or catalog.get("currency") or "").upper()
    unit_price = pricing_engine.positive_decimal(stars_info.get("price_per_star"))
    if currency != "USDT" or unit_price is None:
        raise ValueError("قیمت استارز تأمین‌کننده باید مبلغ مثبت با واحد صریح USDT باشد.")

    premium_raw = catalog.get("premium") or {}
    if not isinstance(premium_raw, dict):
        raise ValueError("ساختار پلن‌های پرمیوم معتبر نیست.")
    premium_prices: dict[str, str] = {}
    premium_trusted = {"3": False, "6": False, "12": False}
    for months in (3, 6, 12):
        item = premium_raw.get(str(months)) or premium_raw.get(months)
        if not isinstance(item, dict):
            continue
        plan_currency = str(item.get("currency") or catalog.get("currency") or "").upper()
        amount = pricing_engine.positive_decimal(item.get("partner_price"))
        if plan_currency == "USDT" and amount is not None:
            premium_prices[str(months)] = str(amount)
            premium_trusted[str(months)] = True

    upstream_max = None
    raw_max = stars_info.get("max_per_order")
    try:
        max_decimal = Decimal(str(raw_max)) if raw_max not in (None, "") else None
        if (max_decimal is not None and max_decimal.is_finite()
                and max_decimal == max_decimal.to_integral_value()
                and 50 <= int(max_decimal) <= pricing_engine.STARS_MAX_QUANTITY):
            upstream_max = int(max_decimal)
    except (TypeError, ValueError, InvalidOperation, OverflowError):
        upstream_max = None

    return {
        "stars_price_per_star_usd": str(unit_price),
        "stars_price_trusted": True,
        "stars_max_per_order": upstream_max,
        "stars_max_per_order_source": "TgSuperstars" if upstream_max else "",
        "premium_usd": premium_prices,
        "premium_prices_trusted": premium_trusted,
        "source_updated_at": source_updated_at,
        "premium_source_updated_at": source_updated_at,
        "catalog_source": "TgSuperstars (API تأمین‌کننده احراز‌شده)",
    }


def merge_quote_catalog(primary: Any, fallback: Any) -> dict[str, Any]:
    """Merge while preferring trusted supplier values over unverified reference data."""
    primary = primary if isinstance(primary, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    result = dict(primary)
    primary_star_trusted = primary.get("stars_price_trusted") is True
    fallback_star_trusted = fallback.get("stars_price_trusted") is True
    if not primary_star_trusted and fallback_star_trusted:
        for key in ("stars_price_per_star_usd", "stars_max_per_order", "stars_max_per_order_source"):
            result[key] = fallback.get(key)
        result["stars_price_trusted"] = True
        result["source_updated_at"] = fallback.get("source_updated_at")
    else:
        result["stars_price_trusted"] = primary_star_trusted

    primary_premium = primary.get("premium_usd") if isinstance(primary.get("premium_usd"), dict) else {}
    fallback_premium = fallback.get("premium_usd") if isinstance(fallback.get("premium_usd"), dict) else {}
    primary_trust = primary.get("premium_prices_trusted") if isinstance(primary.get("premium_prices_trusted"), dict) else {}
    fallback_trust = fallback.get("premium_prices_trusted") if isinstance(fallback.get("premium_prices_trusted"), dict) else {}
    merged_prices = dict(primary_premium)
    merged_trust: dict[str, bool] = {}
    for month in ("3", "6", "12"):
        if primary_trust.get(month) is True and pricing_engine.positive_decimal(primary_premium.get(month)) is not None:
            merged_trust[month] = True
        elif fallback_trust.get(month) is True and pricing_engine.positive_decimal(fallback_premium.get(month)) is not None:
            merged_prices[month] = fallback_premium[month]
            merged_trust[month] = True
        else:
            merged_trust[month] = False
            if (pricing_engine.positive_decimal(merged_prices.get(month)) is None
                    and pricing_engine.positive_decimal(fallback_premium.get(month)) is not None):
                merged_prices[month] = fallback_premium[month]
    result["premium_usd"] = merged_prices
    result["premium_prices_trusted"] = merged_trust
    if all(merged_trust.get(month) is True for month in ("3", "6", "12")):
        result["premium_source_updated_at"] = (
            primary.get("premium_source_updated_at") or fallback.get("premium_source_updated_at")
        )
    return result

"""Central, deterministic pricing engine for SEYED PREMIUM.

This module contains no Telegram or database dependency. It deliberately refuses
unverified supplier prices and stale FX/catalog snapshots for checkout quotes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

REFRESH_INTERVAL_MINUTES = 60
REFRESH_INTERVAL_SECONDS = REFRESH_INTERVAL_MINUTES * 60
# Checkout prices expire after one full hourly cycle. Cached values remain saved
# for diagnostics, but are never silently presented as fresh or sold after expiry.
FEED_MAX_AGE_SECONDS = 60 * 60
SOURCE_MAX_AGE_SECONDS = 60 * 60
DEFAULT_MARGIN_PERCENT = "30"
DEFAULT_FEE_PERCENT = "0"
STARS_MIN_QUANTITY = 50
STARS_MAX_QUANTITY = 1_000_000
PREMIUM_PLANS = (3, 6, 12)


def positive_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None


def percentage(value: Any, *, maximum: Decimal) -> Decimal | None:
    try:
        result = Decimal(str(value).strip())
        if result.is_finite() and Decimal("0") <= result <= maximum:
            return result
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        pass
    return None


def parse_utc_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        raw = str(value).strip()
        # Accept Unix seconds/milliseconds as well as ISO-8601 timestamps.
        if isinstance(value, (int, float)) or raw.isdigit():
            stamp = float(raw)
            if stamp > 100_000_000_000:
                stamp /= 1000.0
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def timestamp_is_fresh(value: Any, max_age_seconds: int = FEED_MAX_AGE_SECONDS,
                       *, now: datetime | None = None) -> bool:
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current.astimezone(timezone.utc) - parsed).total_seconds()
    return -60 <= age <= max_age_seconds


def timestamp_age(value: Any, *, now: datetime | None = None) -> float | None:
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - parsed).total_seconds())


def fx_feed_usable(cfg: dict[str, Any], *, allow_stale: bool = False,
                   now: datetime | None = None) -> bool:
    if not cfg or not cfg.get("fx_valid") or positive_decimal(cfg.get("fx_rate_toman")) is None:
        return False
    return allow_stale or timestamp_is_fresh(
        cfg.get("fx_source_updated_at"), FEED_MAX_AGE_SECONDS, now=now
    )


def stars_feed_usable(cfg: dict[str, Any], *, allow_stale: bool = False,
                       now: datetime | None = None) -> bool:
    prices = (cfg or {}).get("catalog_prices") or {}
    # Legacy cached numbers without an explicit trusted-source marker are not safe
    # for new orders. They remain stored for audit/compatibility, but are not used.
    if not cfg.get("catalog_valid") or prices.get("stars_price_trusted") is not True:
        return False
    if positive_decimal(prices.get("stars_price_per_star_usd")) is None:
        return False
    return allow_stale or timestamp_is_fresh(
        cfg.get("catalog_updated_at"), FEED_MAX_AGE_SECONDS, now=now
    )


def premium_feed_usable(cfg: dict[str, Any], months: int, *, allow_stale: bool = False,
                         now: datetime | None = None) -> bool:
    if months not in PREMIUM_PLANS:
        return False
    prices = (cfg or {}).get("catalog_prices") or {}
    trusted = prices.get("premium_prices_trusted")
    if not isinstance(trusted, dict) or trusted.get(str(months)) is not True:
        return False
    premium_prices = prices.get("premium_usd") or {}
    if positive_decimal(premium_prices.get(str(months))) is None:
        return False
    if cfg.get("premium_catalog_valid") is not True:
        return False
    return allow_stale or timestamp_is_fresh(
        cfg.get("premium_catalog_updated_at"), FEED_MAX_AGE_SECONDS, now=now
    )


def pricing_ready(cfg: dict[str, Any], *, now: datetime | None = None) -> bool:
    return bool(cfg and cfg.get("enabled") and fx_feed_usable(cfg, now=now)
                and stars_feed_usable(cfg, now=now))


def ceil_toman(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_CEILING))


def sale_components(cost_usdt: Any, fx_rate_toman: Any, margin_percent: Any,
                    fee_percent: Any) -> dict[str, Any] | None:
    """Convert a USDT cost to Toman and apply fee first, then margin exactly once."""
    cost = positive_decimal(cost_usdt)
    fx = positive_decimal(fx_rate_toman)
    margin = percentage(margin_percent, maximum=Decimal("500"))
    fee = percentage(fee_percent, maximum=Decimal("100"))
    if cost is None or fx is None or margin is None or fee is None:
        return None
    cost_toman = ceil_toman(cost * fx)
    fee_toman = ceil_toman(Decimal(cost_toman) * fee / Decimal(100)) if fee else 0
    sale_toman = ceil_toman(
        Decimal(cost_toman + fee_toman) * (Decimal(100) + margin) / Decimal(100)
    )
    return {
        "cost_toman": cost_toman,
        "fee_toman": fee_toman,
        "sale_toman": sale_toman,
        "profit_toman": sale_toman - cost_toman - fee_toman,
        "margin_percent": str(margin),
        "fee_percent": str(fee),
    }


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite() or number != number.to_integral_value():
            return None
        return int(number)
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None


def quote(product_type: str, quantity: Any, cfg: dict[str, Any], *,
          allow_stale: bool = False, now: datetime | None = None) -> dict[str, Any] | None:
    """Create a trusted quote. `allow_stale` is for admin preview only, never checkout."""
    if not cfg or not cfg.get("enabled") or not fx_feed_usable(cfg, allow_stale=allow_stale, now=now):
        return None
    count = _strict_int(quantity)
    if count is None:
        return None
    prices = cfg.get("catalog_prices") or {}
    if product_type == "stars":
        if not STARS_MIN_QUANTITY <= count <= STARS_MAX_QUANTITY:
            return None
        if not stars_feed_usable(cfg, allow_stale=allow_stale, now=now):
            return None
        unit = positive_decimal(prices.get("stars_price_per_star_usd"))
        cost_usdt = unit * Decimal(count) if unit is not None else None
        is_fresh = stars_feed_usable(cfg, now=now) and fx_feed_usable(cfg, now=now)
        upstream_max = _strict_int(prices.get("stars_max_per_order"))
        if str(prices.get("stars_max_per_order_source") or "") != "TgSuperstars":
            upstream_max = None
        if upstream_max is not None and not STARS_MIN_QUANTITY <= upstream_max <= STARS_MAX_QUANTITY:
            upstream_max = None
    elif product_type == "premium":
        if count not in PREMIUM_PLANS:
            return None
        if not premium_feed_usable(cfg, count, allow_stale=allow_stale, now=now):
            return None
        cost_usdt = positive_decimal((prices.get("premium_usd") or {}).get(str(count)))
        is_fresh = premium_feed_usable(cfg, count, now=now) and fx_feed_usable(cfg, now=now)
        upstream_max = None
    else:
        return None
    if cost_usdt is None:
        return None
    components = sale_components(
        cost_usdt, cfg.get("fx_rate_toman"),
        cfg.get("margin_percent", DEFAULT_MARGIN_PERCENT),
        cfg.get("fee_percent", DEFAULT_FEE_PERCENT),
    )
    if components is None:
        return None
    return {
        "product_type": product_type,
        "quantity": count,
        "cost_usd": str(cost_usdt),
        "cost_usdt": str(cost_usdt),
        "fx_rate_toman": str(positive_decimal(cfg.get("fx_rate_toman"))),
        **components,
        "provider_max_per_order": upstream_max,
        "provider_over_limit": bool(product_type == "stars" and upstream_max and count > upstream_max),
        "catalog_updated_at": cfg.get("catalog_updated_at", ""),
        "premium_catalog_updated_at": cfg.get("premium_catalog_updated_at", ""),
        "fx_source_updated_at": cfg.get("fx_source_updated_at", ""),
        "is_stale": not is_fresh,
    }


def unit_stars_sale(cfg: dict[str, Any], *, now: datetime | None = None) -> Decimal | None:
    if not pricing_ready(cfg, now=now):
        return None
    unit = positive_decimal((cfg.get("catalog_prices") or {}).get("stars_price_per_star_usd"))
    fx = positive_decimal(cfg.get("fx_rate_toman"))
    margin = percentage(cfg.get("margin_percent", DEFAULT_MARGIN_PERCENT), maximum=Decimal("500"))
    fee = percentage(cfg.get("fee_percent", DEFAULT_FEE_PERCENT), maximum=Decimal("100"))
    if None in (unit, fx, margin, fee):
        return None
    result = unit * fx * (Decimal(100) + fee) / Decimal(100) * (Decimal(100) + margin) / Decimal(100)
    return result.quantize(Decimal("0.01"), rounding=ROUND_CEILING)

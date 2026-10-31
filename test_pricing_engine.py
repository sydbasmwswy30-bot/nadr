import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pricing_engine as pe


class PricingEngineTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.now = now
        self.cfg = {
            "enabled": True,
            "margin_percent": "30",
            "fee_percent": "0",
            "fx_valid": True,
            "fx_rate_toman": "100000",
            "fx_source_updated_at": now.isoformat(),
            "catalog_valid": True,
            "catalog_updated_at": now.isoformat(),
            "premium_catalog_valid": True,
            "premium_catalog_updated_at": now.isoformat(),
            "catalog_prices": {
                "stars_price_per_star_usd": "0.01",
                "stars_price_trusted": True,
                "premium_usd": {"3": "9", "6": "15", "12": "25"},
                "premium_prices_trusted": {"3": True, "6": True, "12": True},
                "stars_max_per_order": 50000,
                "stars_max_per_order_source": "TgSuperstars",
            },
        }

    def test_hourly_refresh_settings(self):
        self.assertEqual(pe.REFRESH_INTERVAL_MINUTES, 60)
        self.assertEqual(pe.REFRESH_INTERVAL_SECONDS, 3600)

    def test_default_margin_and_fee(self):
        self.assertEqual(pe.DEFAULT_MARGIN_PERCENT, "30")
        self.assertEqual(pe.DEFAULT_FEE_PERCENT, "0")

    def test_sale_formula_zero_fee_30_margin(self):
        result = pe.sale_components("1", "100000", "30", "0")
        self.assertEqual(result["cost_toman"], 100000)
        self.assertEqual(result["fee_toman"], 0)
        self.assertEqual(result["sale_toman"], 130000)
        self.assertEqual(result["profit_toman"], 30000)

    def test_stars_quote(self):
        quote = pe.quote("stars", 50, self.cfg, now=self.now)
        self.assertEqual(quote["cost_usdt"], "0.50")
        self.assertEqual(quote["sale_toman"], 65000)

    def test_premium_plans_have_independent_prices(self):
        prices = {m: pe.quote("premium", m, self.cfg, now=self.now)["cost_usdt"] for m in (3, 6, 12)}
        self.assertEqual(prices, {3: "9", 6: "15", 12: "25"})

    def test_unverified_public_price_never_becomes_checkout_quote(self):
        self.cfg["catalog_prices"]["stars_price_trusted"] = False
        self.assertIsNone(pe.quote("stars", 50, self.cfg, now=self.now))

    def test_unverified_premium_plan_blocked(self):
        self.cfg["catalog_prices"]["premium_prices_trusted"]["6"] = False
        self.assertIsNone(pe.quote("premium", 6, self.cfg, now=self.now))

    def test_stale_fx_blocks_checkout(self):
        self.cfg["fx_source_updated_at"] = (self.now - timedelta(minutes=71)).isoformat()
        self.assertIsNone(pe.quote("stars", 50, self.cfg, now=self.now))

    def test_stale_catalog_blocks_checkout(self):
        self.cfg["catalog_updated_at"] = (self.now - timedelta(minutes=71)).isoformat()
        self.assertIsNone(pe.quote("stars", 50, self.cfg, now=self.now))

    def test_stale_admin_preview_can_be_labelled_but_not_fresh(self):
        self.cfg["catalog_updated_at"] = (self.now - timedelta(minutes=71)).isoformat()
        self.assertIsNone(pe.quote("stars", 50, self.cfg, now=self.now))
        preview = pe.quote("stars", 50, self.cfg, allow_stale=True, now=self.now)
        self.assertIsNotNone(preview)
        self.assertTrue(preview["is_stale"])

    def test_non_integer_quantity_rejected(self):
        self.assertIsNone(pe.quote("stars", 50.5, self.cfg, now=self.now))

    def test_usd_toman_formula_invalid_percent_rejected(self):
        self.assertIsNone(pe.sale_components("1", "100000", "-1", "0"))
        self.assertIsNone(pe.sale_components("1", "100000", "30", "101"))

    def test_missing_source_timestamp_is_stale(self):
        self.cfg["fx_source_updated_at"] = ""
        self.assertIsNone(pe.quote("stars", 50, self.cfg, now=self.now))


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timedelta, timezone

import pricing_sources as ps


class SupplierCatalogTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.payload = {
            "meta": {"updated_at": self.now.isoformat(), "stale": False},
            "data": {
                "currency": "USDT",
                "stars": {"currency": "USDT", "price_per_star": "0.012", "max_per_order": 50000},
                "premium": {
                    "3": {"currency": "USDT", "partner_price": "9"},
                    "6": {"currency": "USDT", "partner_price": "15"},
                    "12": {"currency": "USDT", "partner_price": "25"},
                },
            },
        }

    def test_valid_supplier_catalog_is_marked_trusted(self):
        result = ps.parse_tgsuperstars_catalog(self.payload, now=self.now)
        self.assertTrue(result["stars_price_trusted"])
        self.assertTrue(all(result["premium_prices_trusted"].values()))
        self.assertEqual(result["stars_max_per_order"], 50000)
        self.assertEqual(result["premium_usd"]["6"], "15")

    def test_missing_source_timestamp_rejected(self):
        payload = {"data": dict(self.payload["data"])}
        with self.assertRaisesRegex(ValueError, "زمان واقعی"):
            ps.parse_tgsuperstars_catalog(payload, now=self.now)

    def test_old_source_timestamp_rejected(self):
        payload = dict(self.payload)
        payload["meta"] = {"updated_at": (self.now - timedelta(minutes=61)).isoformat()}
        with self.assertRaisesRegex(ValueError, "قدیمی"):
            ps.parse_tgsuperstars_catalog(payload, now=self.now)

    def test_usd_currency_rejected(self):
        payload = dict(self.payload)
        payload["data"] = dict(self.payload["data"])
        payload["data"]["stars"] = {"currency": "USD", "price_per_star": "0.012"}
        with self.assertRaisesRegex(ValueError, "USDT"):
            ps.parse_tgsuperstars_catalog(payload, now=self.now)

    def test_stale_supplier_response_rejected(self):
        payload = dict(self.payload)
        payload["meta"] = {"updated_at": self.now.isoformat(), "stale": True}
        with self.assertRaisesRegex(ValueError, "قدیمی یا ناموجود"):
            ps.parse_tgsuperstars_catalog(payload, now=self.now)

    def test_missing_premium_plan_is_not_marked_trusted(self):
        payload = dict(self.payload)
        payload["data"] = dict(self.payload["data"])
        payload["data"]["premium"] = {"3": {"currency": "USDT", "partner_price": "9"}}
        result = ps.parse_tgsuperstars_catalog(payload, now=self.now)
        self.assertTrue(result["premium_prices_trusted"]["3"])
        self.assertFalse(result["premium_prices_trusted"]["6"])
        self.assertFalse(result["premium_prices_trusted"]["12"])

    def test_verified_fallback_wins_over_unverified_public_quote(self):
        verified = ps.parse_tgsuperstars_catalog(self.payload, now=self.now)
        public = {
            "stars_price_per_star_usd": "0.02",
            "stars_price_trusted": False,
            "premium_usd": {"3": "12", "6": "22", "12": "35"},
            "premium_prices_trusted": {"3": False, "6": False, "12": False},
        }
        merged = ps.merge_quote_catalog(public, verified)
        self.assertEqual(merged["stars_price_per_star_usd"], "0.012")
        self.assertTrue(merged["stars_price_trusted"])
        self.assertEqual(merged["premium_usd"]["6"], "15")
        self.assertTrue(all(merged["premium_prices_trusted"].values()))


if __name__ == "__main__":
    unittest.main()

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import health_monitor


class HealthMonitorTests(unittest.TestCase):
    def _pricing(self):
        now = datetime.now(timezone.utc).isoformat()
        return {
            "enabled": True,
            "fx_valid": True,
            "fx_rate_toman": "700000",
            "fx_source_updated_at": now,
            "fx_source_name": "test-exchange",
            "catalog_valid": True,
            "catalog_updated_at": now,
            "premium_catalog_valid": True,
            "premium_catalog_updated_at": now,
            "margin_percent": "30",
            "fee_percent": "0",
            "refresh_interval_minutes": 60,
            "catalog_prices": {
                "stars_price_per_star_usd": "0.02",
                "stars_price_trusted": True,
                "premium_usd": {"3": "4", "6": "7", "12": "12"},
                "premium_prices_trusted": {"3": True, "6": True, "12": True},
                "catalog_source": "test-supplier",
            },
        }

    def test_database_integrity_and_required_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "shop.db"
            with sqlite3.connect(db) as con:
                for table in ("users", "products", "orders", "app_settings"):
                    con.execute(f"CREATE TABLE {table}(id INTEGER)")
            result = health_monitor.inspect_database(db)
            self.assertTrue(result["exists"])
            self.assertEqual(result["integrity"], "ok")
            self.assertEqual(result["missing_tables"], [])

    def test_missing_database_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = health_monitor.inspect_database(Path(tmp) / "missing.db")
            self.assertFalse(result["exists"])
            self.assertIn("پیدا نشد", result["error"])

    def test_valid_price_feeds_are_reported(self):
        result = health_monitor.inspect_pricing(self._pricing())
        self.assertTrue(result["fx_ok"])
        self.assertTrue(result["stars_ok"])
        self.assertTrue(all(result["premium_ok"].values()))

    def test_stale_prices_are_reported_unusable(self):
        cfg = self._pricing()
        cfg["fx_source_updated_at"] = "2000-01-01T00:00:00+00:00"
        result = health_monitor.inspect_pricing(cfg)
        self.assertFalse(result["fx_ok"])
        self.assertTrue(result["stars_ok"])  # Catalog is fresh; FX is independently stale.

    def test_report_does_not_expose_provider_key(self):
        cfg = self._pricing()
        cfg["provider_api_key"] = "VERY_SECRET"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "shop.db"
            with sqlite3.connect(db) as con:
                for table in ("users", "products", "orders", "app_settings"):
                    con.execute(f"CREATE TABLE {table}(id INTEGER)")
            report = health_monitor.format_health_report(db, Path(tmp) / "backups", cfg)
            self.assertNotIn("VERY_SECRET", report)
            self.assertIn("گزارش سلامت", report)


if __name__ == "__main__":
    unittest.main()

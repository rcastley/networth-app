"""Focused regression tests for multi-currency storage and aggregation."""
from datetime import date, datetime
from decimal import Decimal
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import fx, services
from app.db import Base
from app.models import (
    Account,
    AppSettings,
    Balance,
    Category,
    Snapshot,
    SnapshotFxRate,
)


class CurrencyUnitTests(unittest.TestCase):
    def test_cross_rate_uses_units_per_eur(self):
        converted = fx.convert(
            Decimal("120"),
            "USD",
            "GBP",
            {"EUR": Decimal("1"), "USD": Decimal("1.2"), "GBP": Decimal("0.8")},
        )
        self.assertEqual(converted, Decimal("80.00"))

    def test_manual_rate_set_converts_native_to_reporting(self):
        rate_set = fx.manual_rate_set("GBP", {"USD": Decimal("0.75")})
        converted = fx.convert(
            Decimal("100"),
            "USD",
            "GBP",
            rate_set.rates_per_eur,
        )
        self.assertEqual(converted, Decimal("75.00"))

    def test_currency_formatting_is_explicit(self):
        self.assertEqual(fx.format_currency(Decimal("-12.5"), "GBP"), "(£12.50)")
        self.assertEqual(fx.format_currency(Decimal("12.5"), "USD"), "$12.50")
        self.assertEqual(fx.format_currency(Decimal("12.5"), "CHF"), "CHF 12.50")

    def test_non_finite_values_are_rejected(self):
        with self.assertRaises(fx.FxError):
            fx.convert(
                Decimal("NaN"),
                "USD",
                "GBP",
                {"USD": Decimal("1.2"), "GBP": Decimal("0.8")},
            )

    def test_required_currency_check_detects_sparse_response(self):
        sparse = fx.RateSet(date(2026, 7, 29), {"EUR": Decimal("1")})
        self.assertFalse(fx.rate_set_supports(sparse, {"EUR", "USD"}))

    def test_unrepresentable_rate_is_rejected(self):
        with self.assertRaises(fx.FxError):
            fx.normalize_rate(Decimal("10000000000"))


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add(AppSettings(id=1, reporting_currency="GBP"))
        category = Category(name="Investments", in_net_worth=True, in_liquid=True)
        gbp = Account(name="UK cash", category=category, currency_code="GBP")
        usd = Account(name="US shares", category=category, currency_code="USD")
        snapshot = Snapshot(snapshot_date=datetime(2026, 7, 29, 17, 0))
        snapshot.fx_rates.extend([
            SnapshotFxRate(
                currency_code="EUR",
                rate_per_eur=Decimal("1"),
                effective_date=date(2026, 7, 29),
                source="ecb",
            ),
            SnapshotFxRate(
                currency_code="GBP",
                rate_per_eur=Decimal("0.8"),
                effective_date=date(2026, 7, 29),
                source="ecb",
            ),
            SnapshotFxRate(
                currency_code="USD",
                rate_per_eur=Decimal("1.2"),
                effective_date=date(2026, 7, 29),
                source="ecb",
            ),
        ])
        snapshot.balances.extend([
            Balance(account=gbp, amount=Decimal("100"), currency_code="GBP"),
            Balance(account=usd, amount=Decimal("120"), currency_code="USD"),
        ])
        self.db.add(snapshot)
        self.db.commit()
        self.snapshot = services.latest_snapshot(self.db)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_mixed_currency_totals_convert_before_sum(self):
        totals = services.snapshot_totals(self.snapshot, "GBP")
        self.assertEqual(totals["net_worth"], Decimal("180.00"))
        self.assertEqual(totals["liquid"], Decimal("180.00"))
        self.assertEqual(totals["currency"], "GBP")

    def test_refresh_persisted_rates_updates_adds_and_removes_rows(self):
        original_ids = {row.currency_code: row.id for row in self.snapshot.fx_rates}
        refreshed = fx.RateSet(
            date(2026, 8, 1),
            {"GBP": Decimal("0.9"), "USD": Decimal("1.3"), "CHF": Decimal("0.95")},
            "manual",
        )
        services.store_rate_set(self.snapshot, refreshed)
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(services.snapshot_rate_map(self.snapshot), refreshed.rates_per_eur)
        for row in self.snapshot.fx_rates:
            self.assertEqual(row.effective_date, refreshed.effective_date)
            self.assertEqual(row.source, "manual")
            if row.currency_code in original_ids:
                self.assertEqual(row.id, original_ids[row.currency_code])

    def test_same_snapshot_can_be_reexpressed(self):
        totals = services.snapshot_totals(self.snapshot, "EUR")
        self.assertEqual(totals["net_worth"], Decimal("225.00"))

    def test_new_reporting_rate_preserves_frozen_cross_rates(self):
        before = services.snapshot_totals(self.snapshot, "GBP")["net_worth"]
        fetched = fx.RateSet(
            date(2026, 7, 29),
            {"EUR": Decimal("1"), "GBP": Decimal("0.85")},
        )
        services.merge_reporting_rate(self.snapshot, "GBP", "EUR", fetched)
        self.db.commit()
        after = services.snapshot_totals(self.snapshot, "GBP")["net_worth"]
        self.assertEqual(after, before)
        self.assertEqual(
            services.snapshot_totals(self.snapshot, "EUR")["net_worth"],
            Decimal("211.77"),
        )

    def test_manual_reporting_rate_merge_preserves_existing_rates(self):
        before = services.snapshot_totals(self.snapshot, "GBP")["net_worth"]
        services.merge_manual_reporting_rate(
            self.snapshot,
            "EUR",
            {"USD": Decimal("0.9")},
            date(2026, 7, 29),
        )
        self.db.commit()
        self.assertEqual(
            services.snapshot_totals(self.snapshot, "GBP")["net_worth"],
            before,
        )
        self.assertEqual(
            services.snapshot_rate_map(self.snapshot)["EUR"],
            Decimal("1.0800000000"),
        )

    def test_balance_map_can_return_native_or_reporting_values(self):
        native = services.balance_map(self.snapshot)
        converted = services.balance_map(self.snapshot, "GBP")
        self.assertEqual(sorted(native.values()), [Decimal("100.00"), Decimal("120.00")])
        self.assertEqual(sorted(converted.values()), [Decimal("80.00"), Decimal("100.00")])

    def test_legacy_gbp_snapshot_needs_no_rate_rows(self):
        account = self.db.scalar(select(Account).where(Account.name == "UK cash"))
        legacy = Snapshot(snapshot_date=datetime(2020, 1, 1))
        legacy.balances.append(
            Balance(account=account, amount=Decimal("50"), currency_code="GBP")
        )
        self.db.add(legacy)
        self.db.commit()
        self.assertEqual(
            services.snapshot_totals(legacy, "GBP")["net_worth"],
            Decimal("50"),
        )


if __name__ == "__main__":
    unittest.main()

"""Route regression tests with isolated databases and mocked rate retrieval."""
import importlib
import io
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request

from app import fx, services
from app.db import Base
from app.models import Account, AppSettings, Balance, Category, Snapshot


def request(form=None):
    req = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [], "query_string": b"",
    })
    req.state.csrf_token = "test"
    req._form = FormData(form or {})
    return req


class CurrencyRouteTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # main runs migrations on import; never let those touch the user's DB.
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{directory}/startup.db")
            try:
                with patch("app.db.engine", engine), patch(
                    "app.db.SessionLocal", sessionmaker(bind=engine)
                ):
                    cls.main = importlib.import_module("app.main")
            finally:
                engine.dispose()

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.db.add(AppSettings(id=1, reporting_currency="GBP"))
        self.category = Category(name="Cash")
        self.db.add(self.category)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def account(self, name, currency="GBP"):
        account = Account(name=name, category=self.category, currency_code=currency)
        self.db.add(account)
        self.db.flush()
        return account

    def rated_snapshot(self):
        usd = self.account("US", "USD")
        eur = self.account("EU", "EUR")
        snap = Snapshot(snapshot_date=datetime(2020, 1, 1))
        snap.balances.append(Balance(account=usd, amount=100, currency_code="USD"))
        services.store_rate_set(snap, fx.manual_rate_set(
            "GBP", {"USD": Decimal("0.75")}, date(2020, 1, 1)
        ))
        self.db.add(snap)
        self.db.commit()
        return snap, usd, eur

    async def check_added_currency(self, offline):
        snap, usd, eur = self.rated_snapshot()
        original = {
            row.currency_code: (row.rate_per_eur, row.effective_date, row.source)
            for row in snap.fx_rates
        }
        latest = fx.RateSet(date(2026, 10, 1), {
            "EUR": Decimal("1"), "USD": Decimal("1.2"), "GBP": Decimal("0.8"),
        })
        form = {
            "snapshot_date": "2020-01-01", f"acc_{usd.id}": "100",
            f"currency_{usd.id}": "USD", f"acc_{eur.id}": "100",
            f"currency_{eur.id}": "EUR", f"manual_rate_{eur.id}": "0.8",
        }
        with patch.object(fx, "fetch_rate_set", return_value=latest,
                          side_effect=fx.FxError("offline") if offline else None):
            await self.main.snapshot_update(snap.id, request(form), None, self.db)
        self.db.expire_all()
        for row in snap.fx_rates:
            if row.currency_code in original:
                self.assertEqual(
                    (row.rate_per_eur, row.effective_date, row.source),
                    original[row.currency_code],
                )
        rates = services.snapshot_rate_map(snap)
        self.assertEqual(fx.convert(100, "USD", "GBP", rates), Decimal("75.00"))
        self.assertEqual(fx.convert(100, "EUR", "GBP", rates), Decimal("80.00"))
        self.assertEqual(services.snapshot_totals(snap)["net_worth"], Decimal("155.00"))

    async def test_add_currency_preserves_frozen_rates(self):
        await self.check_added_currency(offline=False)

    async def test_add_currency_offline_only_requires_new_manual_rate(self):
        await self.check_added_currency(offline=True)

    async def test_explicit_refresh_replaces_persisted_rates(self):
        snap, usd, _ = self.rated_snapshot()
        latest = fx.RateSet(date(2026, 10, 1), {
            "EUR": Decimal("1"), "USD": Decimal("1.2"), "GBP": Decimal("0.8"),
        })
        with patch.object(fx, "fetch_rate_set", return_value=latest):
            await self.main.snapshot_update(snap.id, request({
                "snapshot_date": "2020-01-01", "refresh_rates": "on",
                f"acc_{usd.id}": "100", f"currency_{usd.id}": "USD",
            }), None, self.db)
        self.db.expire_all()
        self.assertEqual(services.snapshot_rate_map(snap), latest.rates_per_eur)
        self.assertEqual(services.snapshot_totals(snap)["net_worth"], Decimal("66.67"))

    async def test_existing_currency_edit_does_not_fetch_rates(self):
        snap, usd, _ = self.rated_snapshot()
        with patch.object(fx, "fetch_rate_set") as fetch:
            await self.main.snapshot_update(snap.id, request({
                "snapshot_date": "2020-01-01", f"acc_{usd.id}": "200",
                f"currency_{usd.id}": "USD",
            }), None, self.db)
            fetch.assert_not_called()
        self.assertEqual(services.snapshot_totals(snap)["net_worth"], Decimal("150.00"))

    async def test_csv_roundtrip_keeps_accounts_with_metadata_suffixes(self):
        snap = Snapshot(snapshot_date=datetime(2020, 1, 1))
        self.db.add(snap)
        expected = {"Cash": Decimal("100"), "Foreign Currency": Decimal("200"),
                    "Bank FX Rate": Decimal("300")}
        for name, amount in expected.items():
            snap.balances.append(Balance(
                account=self.account(name), amount=amount, currency_code="GBP",
            ))
        self.db.commit()
        exported = self.main.export_csv(self.db)
        response = await self.main.import_csv(request(), UploadFile(
            filename="export.csv", file=io.BytesIO(exported.body),
        ), "overwrite", None, self.db)
        self.assertEqual(response.status_code, 200)
        self.db.expire_all()
        self.assertEqual({b.account.name: b.amount for b in self.db.scalars(select(Balance))}, expected)

    async def test_duplicate_csv_headers_rejected_before_overwrite(self):
        snap = Snapshot(snapshot_date=datetime(2020, 1, 1))
        account = self.account("Cash")
        snap.balances.append(Balance(account=account, amount=100, currency_code="GBP"))
        self.db.add(snap)
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            await self.main.import_csv(request(), UploadFile(
                filename="ambiguous.csv",
                file=io.BytesIO(b"Date,Cash,Cash Currency,Cash Currency\n2020-01-01,50,GBP,200\n"),
            ), "overwrite", None, self.db)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.db.scalar(select(Balance.amount)), Decimal("100"))

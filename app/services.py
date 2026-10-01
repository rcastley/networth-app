"""Business logic: totals, aggregations, deltas."""
from decimal import Decimal
from collections import defaultdict
from typing import Optional
from sqlalchemy.orm import Session, joinedload, selectinload  # type: ignore
from sqlalchemy import and_, or_, select  # type: ignore
from . import fx
from .models import (
    Account,
    AppSettings,
    Balance,
    Category,
    Snapshot,
    SnapshotFxRate,
)


def _snapshot_eager_options():
    """Eager-load every attribute snapshot_totals touches, in one selectin + joined chain."""
    return (
        selectinload(Snapshot.balances)
        .joinedload(Balance.account)
        .options(
            joinedload(Account.category),
            joinedload(Account.parent),
        ),
        selectinload(Snapshot.fx_rates),
    )


# ---------- Settings and exchange rates ----------

def get_settings(db: Session) -> AppSettings:
    settings = db.get(AppSettings, 1)
    if settings is None:
        settings = AppSettings(id=1, reporting_currency="GBP")
        db.add(settings)
        db.flush()
    return settings


def reporting_currency(db: Session) -> str:
    return fx.normalize_currency(get_settings(db).reporting_currency)


def snapshot_rate_map(snap: Snapshot) -> dict[str, Decimal]:
    return {
        row.currency_code: Decimal(row.rate_per_eur)
        for row in snap.fx_rates
    }


def store_rate_set(snap: Snapshot, rate_set: fx.RateSet) -> None:
    # Reuse persisted rows: inserting replacements before orphan deletion would
    # violate the snapshot/currency unique constraint during the flush.
    existing = {row.currency_code: row for row in snap.fx_rates}
    for row in list(snap.fx_rates):
        if row.currency_code not in rate_set.rates_per_eur:
            snap.fx_rates.remove(row)
    for code, rate in sorted(rate_set.rates_per_eur.items()):
        row = existing.get(code)
        if row is None:
            row = SnapshotFxRate(currency_code=code)
            snap.fx_rates.append(row)
        row.rate_per_eur = rate
        row.effective_date = rate_set.effective_date
        row.source = rate_set.source


def merge_reporting_rate(
    snap: Snapshot,
    current_reporting: str,
    new_reporting: str,
    fetched: fx.RateSet,
) -> None:
    """Add a new reporting-currency rate without changing frozen cross-rates."""
    saved = snapshot_rate_map(snap)
    if not saved:
        store_rate_set(snap, fetched)
        return
    if current_reporting not in saved:
        raise fx.FxError(
            f"The snapshot has no saved {current_reporting} reference rate."
        )
    required = {current_reporting, new_reporting}
    if not fx.rate_set_supports(fetched, required):
        raise fx.FxError(
            f"Historical {current_reporting}/{new_reporting} rates are unavailable."
        )
    new_rate = fx.normalize_rate(
        saved[current_reporting]
        * fx.direct_rate(current_reporting, new_reporting, fetched.rates_per_eur)
    )
    existing = next(
        (row for row in snap.fx_rates if row.currency_code == new_reporting),
        None,
    )
    if existing:
        existing.rate_per_eur = new_rate
        existing.effective_date = fetched.effective_date
        existing.source = fetched.source
    else:
        snap.fx_rates.append(
            SnapshotFxRate(
                currency_code=new_reporting,
                rate_per_eur=new_rate,
                effective_date=fetched.effective_date,
                source=fetched.source,
            )
        )


def merge_manual_reporting_rate(
    snap: Snapshot,
    new_reporting: str,
    direct_rates: dict[str, Decimal],
    effective_date,
) -> None:
    """Merge explicit native-to-new-reporting rates into a frozen rate basis."""
    saved = snapshot_rate_map(snap)
    if not saved:
        store_rate_set(
            snap,
            fx.manual_rate_set(new_reporting, direct_rates, effective_date),
        )
        return
    candidates: list[Decimal] = []
    for source, direct in direct_rates.items():
        if source not in saved:
            raise fx.FxError(f"The snapshot has no saved {source} reference rate.")
        candidates.append(
            fx.normalize_rate(saved[source] * fx.normalize_rate(direct))
        )
    if not candidates:
        raise fx.FxError("At least one manual exchange rate is required.")
    target_rate = candidates[0]
    tolerance = Decimal("0.00000001")
    if any(abs(candidate - target_rate) > tolerance for candidate in candidates[1:]):
        raise fx.FxError(
            "Manual rates for this snapshot are inconsistent with one another."
        )
    existing = next(
        (row for row in snap.fx_rates if row.currency_code == new_reporting),
        None,
    )
    if existing:
        existing.rate_per_eur = target_rate
        existing.effective_date = effective_date
        existing.source = "manual"
    else:
        snap.fx_rates.append(
            SnapshotFxRate(
                currency_code=new_reporting,
                rate_per_eur=target_rate,
                effective_date=effective_date,
                source="manual",
            )
        )


def snapshot_rate_summary(snap: Snapshot) -> Optional[dict]:
    if not snap.fx_rates:
        return None
    first = snap.fx_rates[0]
    sources = sorted({row.source for row in snap.fx_rates})
    return {
        "effective_date": first.effective_date,
        "source": ", ".join(sources),
    }


def converted_balance_amount(
    balance: Balance,
    reporting: str,
    rates: Optional[dict[str, Decimal]] = None,
) -> Decimal:
    return fx.convert(
        balance.amount,
        balance.currency_code,
        reporting,
        rates if rates is not None else snapshot_rate_map(balance.snapshot),
    )


# ---------- Per-snapshot totals ----------

def snapshot_totals(snap: Snapshot, reporting: str = "GBP") -> dict:
    """
    Returns category-level rollup with hierarchical grouping.

    Shape:
        {
          "by_category": [
              {
                "category": Category,
                "total": Decimal,
                "rows": [
                  # Either a leaf row:
                  {"type": "leaf",  "account": Account, "amount": Decimal},
                  # Or a parent-group row:
                  {"type": "group", "parent": Account, "total": Decimal,
                   "children": [{"account": Account, "amount": Decimal}, ...]},
                ],
                "accounts": [{"account": Account, "amount": Decimal}, ...],  # flat for charts
              }, ...
          ],
          "category_map": {category_name: Decimal},
          "net_worth": Decimal,         # in_net_worth=True categories
          "net_worth_plus_aux": Decimal,
          "liquid": Decimal,            # in_liquid=True categories
        }
    """
    # Bucket per (category_id, group_key) where group_key is parent_id or ("leaf", account_id)
    cat_buckets: dict[int, dict] = {}

    reporting = fx.normalize_currency(reporting)
    rates = snapshot_rate_map(snap)
    for b in snap.balances:
        acc = b.account
        cat = acc.category
        amount = converted_balance_amount(b, reporting, rates)
        cb = cat_buckets.setdefault(cat.id, {
            "category": cat,
            "total": Decimal("0"),
            "_groups": {},   # parent_id -> {"parent": acc, "total": d, "children": [], "_sort": int}
            "_leaves": [],   # list of leaf entries
            "_flat":   [],   # flat list for charts/exports
        })
        cb["total"] += amount
        cb["_flat"].append({
            "account": acc,
            "amount": amount,
            "native_amount": b.amount,
            "currency_code": b.currency_code,
        })

        if acc.parent_id:
            pg = cb["_groups"].setdefault(acc.parent_id, {
                "type":     "group",
                "parent":   acc.parent,
                "total":    Decimal("0"),
                "children": [],
                "_sort":    (acc.parent.sort_order, acc.parent.id),
            })
            pg["total"] += amount
            pg["children"].append({
                "account": acc,
                "amount": amount,
                "native_amount": b.amount,
                "currency_code": b.currency_code,
            })
        else:
            cb["_leaves"].append({
                "type":    "leaf",
                "account": acc,
                "amount":  amount,
                "native_amount": b.amount,
                "currency_code": b.currency_code,
                "_sort":   (acc.sort_order, acc.id),
            })

    # Build sorted output
    rows = []
    for cb in cat_buckets.values():
        items = list(cb["_groups"].values()) + cb["_leaves"]
        items.sort(key=lambda x: x["_sort"])
        for item in items:
            if item["type"] == "group":
                item["children"].sort(key=lambda c: (c["account"].sort_order, c["account"].id))
        rows.append({
            "category": cb["category"],
            "total":    cb["total"],
            "rows":     items,
            "accounts": sorted(cb["_flat"], key=lambda e: (e["account"].sort_order, e["account"].id)),
        })

    rows.sort(key=lambda r: (r["category"].sort_order, r["category"].id))

    cat_map: dict[str, Decimal] = {r["category"].name: r["total"] for r in rows}
    net_worth = sum((r["total"] for r in rows if r["category"].in_net_worth), Decimal("0"))
    plus_aux  = sum((r["total"] for r in rows),                              Decimal("0"))
    liquid    = sum((r["total"] for r in rows if r["category"].in_liquid),   Decimal("0"))

    return {
        "by_category":        rows,
        "category_map":       cat_map,
        "net_worth":          net_worth,
        "net_worth_plus_aux": plus_aux,
        "liquid":             liquid,
        "currency":           reporting,
    }


# ---------- Snapshot retrieval helpers ----------

def latest_snapshot(db: Session) -> Optional[Snapshot]:
    return db.scalar(
        select(Snapshot)
        .options(*_snapshot_eager_options())
        .order_by(Snapshot.snapshot_date.desc(), Snapshot.id.desc())
        .limit(1)
    )


def previous_snapshot(db: Session, current: Snapshot) -> Optional[Snapshot]:
    """Return the snapshot immediately before ``current`` in deterministic order."""
    return db.scalar(
        select(Snapshot)
        .options(*_snapshot_eager_options())
        .where(
            or_(
                Snapshot.snapshot_date < current.snapshot_date,
                and_(
                    Snapshot.snapshot_date == current.snapshot_date,
                    Snapshot.id < current.id,
                ),
            )
        )
        .order_by(Snapshot.snapshot_date.desc(), Snapshot.id.desc())
        .limit(1)
    )


def all_snapshots(db: Session, ascending: bool = False) -> list[Snapshot]:
    order = (
        (Snapshot.snapshot_date.asc(), Snapshot.id.asc())
        if ascending
        else (Snapshot.snapshot_date.desc(), Snapshot.id.desc())
    )
    return list(db.scalars(
        select(Snapshot)
        .options(*_snapshot_eager_options())
        .order_by(*order)
    ))


# ---------- Deltas ----------

def delta(current, previous) -> Optional[dict]:
    """Returns {abs, pct, direction} or None if either input is None."""
    if current is None or previous is None:
        return None
    cur = Decimal(current)
    prv = Decimal(previous)
    diff = cur - prv
    pct  = (diff / abs(prv) * 100) if prv != 0 else None
    direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
    return {"abs": diff, "pct": pct, "direction": direction}


# ---------- Time series for charts ----------

def time_series(db: Session, period: str = "weekly") -> dict:
    """
    Returns {"labels": [...], "net_worth": [...], "liquid": [...], "categories": {name: [...]}}
    period: "weekly" (all snapshots), "monthly" (last per month), "quarterly" (last per quarter).
    """
    snaps = all_snapshots(db, ascending=True)
    reporting = reporting_currency(db)

    if period == "monthly":
        snaps = _resample(snaps, key=lambda s: (s.snapshot_date.year, s.snapshot_date.month))
    elif period == "quarterly":
        snaps = _resample(snaps, key=lambda s: (s.snapshot_date.year, (s.snapshot_date.month - 1) // 3))

    labels: list[str] = []
    nw: list[float] = []
    liq: list[float] = []
    by_cat: dict[str, list[float]] = {}

    all_categories = [c.name for c in db.scalars(select(Category).order_by(Category.sort_order, Category.id)).all()]
    for c in all_categories:
        by_cat[c] = []

    for s in snaps:
        t = snapshot_totals(s, reporting)
        labels.append(s.snapshot_date.isoformat())
        nw.append(float(t["net_worth"]))
        liq.append(float(t["liquid"]))
        for cname in all_categories:
            by_cat[cname].append(float(t["category_map"].get(cname, Decimal("0"))))

    return {
        "labels": labels,
        "net_worth": nw,
        "liquid": liq,
        "categories": by_cat,
        "currency": reporting,
    }


def _resample(snaps: list[Snapshot], key) -> list[Snapshot]:
    bucket: dict = {}
    for s in snaps:
        bucket[key(s)] = s
    return list(bucket.values())


# ---------- Convenience ----------

def active_accounts(db: Session) -> list[Account]:
    """All active accounts (groups + leaves)."""
    return list(db.scalars(
        select(Account)
        .options(joinedload(Account.category), joinedload(Account.parent))
        .where(Account.is_active == True)
        .order_by(Account.sort_order, Account.id)
    ))


def active_leaf_accounts(db: Session) -> list[Account]:
    """Only leaf accounts (suitable for snapshot input forms)."""
    return list(db.scalars(
        select(Account)
        .where(Account.is_active == True, Account.is_group == False)
        .order_by(Account.sort_order, Account.id)
    ))


def all_categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.sort_order, Category.id)))


def assert_account_hierarchy_integrity(db: Session) -> None:
    """Refuse startup when stored accounts exceed the supported single group level."""
    accounts = list(db.scalars(select(Account)))
    by_id = {account.id: account for account in accounts}
    violations: list[str] = []

    for account in accounts:
        if account.parent_id is None:
            continue
        parent = by_id.get(account.parent_id)
        if parent is None:
            violations.append(f"account {account.id} has a missing parent")
        elif account.is_group:
            violations.append(f"group {account.id} is nested")
        elif not parent.is_group:
            violations.append(f"account {account.id} has a non-group parent")
        elif parent.parent_id is not None:
            violations.append(f"account {account.id} is below a nested group")

    if violations:
        examples = ", ".join(violations[:5])
        remainder = len(violations) - 5
        if remainder > 0:
            examples += f", and {remainder} more"
        raise RuntimeError(
            "Account hierarchy check failed; no data was changed. "
            "Only top-level groups with leaf sub-accounts are supported: "
            f"{examples}."
        )


def balance_map(
    snap: Optional[Snapshot],
    reporting: Optional[str] = None,
) -> dict[int, Decimal]:
    """Account amounts for a snapshot, native by default or converted when requested."""
    if snap is None:
        return {}
    if reporting is None:
        return {b.account_id: b.amount for b in snap.balances}
    rates = snapshot_rate_map(snap)
    return {
        b.account_id: converted_balance_amount(b, reporting, rates)
        for b in snap.balances
    }


def balance_currency_map(snap: Optional[Snapshot]) -> dict[int, str]:
    if snap is None:
        return {}
    return {b.account_id: b.currency_code for b in snap.balances}


# ---------- Per-account history (for the detail page) ----------

def account_history(
    db: Session,
    account: Account,
    reporting: Optional[str] = None,
) -> list[tuple]:
    """
    Returns a list of (snapshot_date, amount) tuples sorted ascending.
    For a group account, sums balances across all of its leaf children per snapshot.
    Snapshots with no balances for the account (or its children) are omitted.
    """
    target = reporting or reporting_currency(db)
    account_ids = (
        {c.id for c in account.children if not c.is_group}
        if account.is_group
        else {account.id}
    )
    if not account_ids:
        return []
    history: list[tuple] = []
    for snap in all_snapshots(db, ascending=True):
        rates = snapshot_rate_map(snap)
        amounts = [
            converted_balance_amount(balance, target, rates)
            for balance in snap.balances
            if balance.account_id in account_ids
        ]
        if amounts:
            history.append((snap.snapshot_date, sum(amounts, Decimal("0"))))
    return history


def account_histories(
    db: Session,
    accounts: list[Account],
    reporting: Optional[str] = None,
) -> dict[int, list[tuple]]:
    """Return converted snapshot histories for multiple accounts."""
    result: dict[int, list[tuple]] = {account.id: [] for account in accounts}
    target = reporting or reporting_currency(db)
    by_id = {account.id: account for account in accounts}
    snaps = all_snapshots(db, ascending=True)
    for snap in snaps:
        rates = snapshot_rate_map(snap)
        grouped: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        for balance in snap.balances:
            if balance.account_id in by_id:
                grouped[balance.account_id] += converted_balance_amount(
                    balance, target, rates
                )
        for account_id, amount in grouped.items():
            result[account_id].append((snap.snapshot_date, amount))
    for account in accounts:
        if account.is_group:
            result[account.id] = account_history(db, account, target)
    return result


def latest_balance_for(
    db: Session,
    account: Account,
    reporting: Optional[str] = None,
) -> Optional[Decimal]:
    """Quick lookup of the most recent balance for an account (or group total)."""
    hist = account_history(db, account, reporting)
    return hist[-1][1] if hist else None


def latest_balances_for_accounts(
    db: Session,
    accounts: list[Account],
    reporting: Optional[str] = None,
) -> dict[int, Optional[Decimal]]:
    histories = account_histories(db, accounts, reporting)
    return {
        account.id: histories[account.id][-1][1] if histories[account.id] else None
        for account in accounts
    }

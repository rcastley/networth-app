"""Business logic: totals, aggregations, deltas."""
from decimal import Decimal
from collections import defaultdict
from datetime import date
from typing import Optional
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import select, func
from .models import Snapshot, Balance, Account, Category


def _snapshot_eager_options():
    """Eager-load every attribute snapshot_totals touches, in one selectin + joined chain."""
    return (
        selectinload(Snapshot.balances)
        .joinedload(Balance.account)
        .options(
            joinedload(Account.category),
            joinedload(Account.parent),
        ),
    )


# ---------- Per-snapshot totals ----------

def snapshot_totals(snap: Snapshot) -> dict:
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

    for b in snap.balances:
        acc = b.account
        cat = acc.category
        cb = cat_buckets.setdefault(cat.id, {
            "category": cat,
            "total": Decimal("0"),
            "_groups": {},   # parent_id -> {"parent": acc, "total": d, "children": [], "_sort": int}
            "_leaves": [],   # list of leaf entries
            "_flat":   [],   # flat list for charts/exports
        })
        cb["total"] += b.amount
        cb["_flat"].append({"account": acc, "amount": b.amount})

        if acc.parent_id:
            pg = cb["_groups"].setdefault(acc.parent_id, {
                "type":     "group",
                "parent":   acc.parent,
                "total":    Decimal("0"),
                "children": [],
                "_sort":    acc.parent.sort_order,
            })
            pg["total"] += b.amount
            pg["children"].append({"account": acc, "amount": b.amount})
        else:
            cb["_leaves"].append({
                "type":    "leaf",
                "account": acc,
                "amount":  b.amount,
                "_sort":   acc.sort_order,
            })

    # Build sorted output
    rows = []
    for cb in cat_buckets.values():
        items = list(cb["_groups"].values()) + cb["_leaves"]
        items.sort(key=lambda x: x["_sort"])
        for item in items:
            if item["type"] == "group":
                item["children"].sort(key=lambda c: c["account"].sort_order)
        rows.append({
            "category": cb["category"],
            "total":    cb["total"],
            "rows":     items,
            "accounts": sorted(cb["_flat"], key=lambda e: e["account"].sort_order),
        })

    rows.sort(key=lambda r: r["category"].sort_order)

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
    }


# ---------- Snapshot retrieval helpers ----------

def latest_snapshot(db: Session) -> Optional[Snapshot]:
    return db.scalar(
        select(Snapshot)
        .options(*_snapshot_eager_options())
        .order_by(Snapshot.snapshot_date.desc())
        .limit(1)
    )


def previous_snapshot(db: Session, before_date: date) -> Optional[Snapshot]:
    return db.scalar(
        select(Snapshot)
        .options(*_snapshot_eager_options())
        .where(Snapshot.snapshot_date < before_date)
        .order_by(Snapshot.snapshot_date.desc())
        .limit(1)
    )


def all_snapshots(db: Session, ascending: bool = False) -> list[Snapshot]:
    order = Snapshot.snapshot_date.asc() if ascending else Snapshot.snapshot_date.desc()
    return list(db.scalars(
        select(Snapshot)
        .options(*_snapshot_eager_options())
        .order_by(order)
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

    if period == "monthly":
        snaps = _resample(snaps, key=lambda s: (s.snapshot_date.year, s.snapshot_date.month))
    elif period == "quarterly":
        snaps = _resample(snaps, key=lambda s: (s.snapshot_date.year, (s.snapshot_date.month - 1) // 3))

    labels: list[str] = []
    nw: list[float] = []
    liq: list[float] = []
    by_cat: dict[str, list[float]] = {}

    all_categories = [c.name for c in db.scalars(select(Category).order_by(Category.sort_order)).all()]
    for c in all_categories:
        by_cat[c] = []

    for s in snaps:
        t = snapshot_totals(s)
        labels.append(s.snapshot_date.isoformat())
        nw.append(float(t["net_worth"]))
        liq.append(float(t["liquid"]))
        for cname in all_categories:
            by_cat[cname].append(float(t["category_map"].get(cname, Decimal("0"))))

    return {"labels": labels, "net_worth": nw, "liquid": liq, "categories": by_cat}


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
        .order_by(Account.sort_order)
    ))


def active_leaf_accounts(db: Session) -> list[Account]:
    """Only leaf accounts (suitable for snapshot input forms)."""
    return list(db.scalars(
        select(Account)
        .where(Account.is_active == True, Account.is_group == False)
        .order_by(Account.sort_order)
    ))


def all_categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.sort_order)))


def balance_map(snap: Optional[Snapshot]) -> dict[int, Decimal]:
    """account_id -> amount for the given snapshot (empty if None)."""
    if snap is None:
        return {}
    return {b.account_id: b.amount for b in snap.balances}


# ---------- Per-account history (for the detail page) ----------

def account_history(db: Session, account: Account) -> list[tuple]:
    """
    Returns a list of (snapshot_date, amount) tuples sorted ascending.
    For a group account, sums balances across all of its leaf children per snapshot.
    Snapshots with no balances for the account (or its children) are omitted.
    """
    if account.is_group:
        child_ids = [c.id for c in account.children if not c.is_group]
        if not child_ids:
            return []
        rows = db.execute(
            select(Snapshot.snapshot_date, func.sum(Balance.amount))
            .join(Balance, Balance.snapshot_id == Snapshot.id)
            .where(Balance.account_id.in_(child_ids))
            .group_by(Snapshot.id, Snapshot.snapshot_date)
            .order_by(Snapshot.snapshot_date.asc())
        ).all()
        return [(r[0], r[1]) for r in rows]
    else:
        rows = db.execute(
            select(Snapshot.snapshot_date, Balance.amount)
            .join(Balance, Balance.snapshot_id == Snapshot.id)
            .where(Balance.account_id == account.id)
            .order_by(Snapshot.snapshot_date.asc())
        ).all()
        return [(r[0], r[1]) for r in rows]


def latest_balance_for(db: Session, account: Account) -> Optional[Decimal]:
    """Quick lookup of the most recent balance for an account (or group total)."""
    hist = account_history(db, account)
    return hist[-1][1] if hist else None


def latest_balances_for_accounts(
    db: Session, accounts: list[Account]
) -> dict[int, Optional[Decimal]]:
    """Batched version of latest_balance_for() over multiple accounts.

    Leaves are resolved in a single window-function query. Groups (rare here, only
    when nested under another group) fall back to per-account history.
    """
    result: dict[int, Optional[Decimal]] = {a.id: None for a in accounts}
    leaves = [a for a in accounts if not a.is_group]
    if leaves:
        ids = [a.id for a in leaves]
        ranked = (
            select(
                Balance.account_id.label("account_id"),
                Balance.amount.label("amount"),
                func.row_number().over(
                    partition_by=Balance.account_id,
                    order_by=[Snapshot.snapshot_date.desc(), Snapshot.id.desc()],
                ).label("rn"),
            )
            .join(Snapshot, Snapshot.id == Balance.snapshot_id)
            .where(Balance.account_id.in_(ids))
            .subquery()
        )
        rows = db.execute(
            select(ranked.c.account_id, ranked.c.amount).where(ranked.c.rn == 1)
        ).all()
        for acc_id, amount in rows:
            result[acc_id] = amount

    for a in accounts:
        if a.is_group:
            result[a.id] = latest_balance_for(db, a)
    return result

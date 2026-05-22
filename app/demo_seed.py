"""Populate a SEPARATE demo SQLite database with realistic accounts and
snapshots so a new user can see what a populated home page looks like.

The seed deliberately ignores the app's DATABASE_URL — it always writes
to a dedicated demo file so it cannot clobber real data. Default path:

    /data/demo.db        (inside the Docker container, alongside the
                          mounted networth.db)
    <repo>/data/demo.db  (on a local checkout)

Pass --db /some/other/path.db to override.

Run inside Docker:
    docker compose exec networth python -m app.demo_seed

Or locally:
    python -m app.demo_seed

By default refuses to run if the demo DB already contains data — pass
--reset to wipe and re-seed.

What gets created:
  - 10 leaf accounts under 2 groups, spanning Cash, Investments,
    Pension, Property, Liability, and Life Insurance.
  - 5 weekly snapshots over ~28 days with realistic month-over-month
    moves: salary deposit, mortgage payment, market dip and recovery,
    credit card paid off.

To view the demo, point the app at the demo DB. Locally:
    DATABASE_URL='sqlite:///./data/demo.db' uvicorn app.main:app

In Docker (the /data mount is shared with the host's ./data):
    DATABASE_URL=sqlite:////data/demo.db docker compose up
"""
import argparse
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select, delete, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Category, Account, Snapshot, Balance
from .seed import CATEGORIES


# (name, category, notes, parent_marker, is_group, domain)
# parent_marker links a child to its parent group in the same pass.
ACCOUNTS = [
    ("Main Bank",      "Cash",           "Day-to-day banking",                None,   True,  "barclays.com"),
    ("Checking",       "Cash",           "Current account",                   "bank", False, None),
    ("Savings",        "Cash",           "Instant-access savings",            "bank", False, None),
    ("Emergency Fund", "Cash",           "Three months of expenses",          None,   False, "monzo.com"),
    ("Vanguard",       "Investments",    "Brokerage (parent group)",          None,   True,  "vanguard.co.uk"),
    ("Stocks ISA",     "Investments",    "Tax-wrapped index funds",           "vg",   False, None),
    ("Brokerage",      "Investments",    "General investing account",         "vg",   False, None),
    ("Workplace Pension", "Pension",     "Employer + personal contributions", None,   False, "aviva.com"),
    ("House",          "Property",       "Estimated market value",            None,   False, None),
    ("Mortgage",       "Liability",      "Outstanding principal",             None,   False, "santander.co.uk"),
    ("Credit Card",    "Liability",      "Statement balance",                 None,   False, "amex.com"),
    ("Life Insurance", "Life Insurance", "Death in Service benefit",          None,   False, "standardlife.co.uk"),
]

PARENT_MARKERS = {"bank": "Main Bank", "vg": "Vanguard"}

# Five weekly snapshots. Amounts tell a one-month story: salary deposit on
# week 2, mortgage paid + small market dip on week 3, credit card cleared
# and markets recovering on week 4, end-of-month wrap on week 5.
SNAPSHOTS = [
    {
        "days_ago": 28,
        "notes": "Start-of-month baseline",
        "balances": {
            "Checking": 1850, "Savings": 8000, "Emergency Fund": 5000,
            "Stocks ISA": 22000, "Brokerage": 6500,
            "Workplace Pension": 44850,
            "House": 325000, "Mortgage": -245000, "Credit Card": -450,
            "Life Insurance": 400000,
        },
    },
    {
        "days_ago": 21,
        "notes": "Salary paid; markets up slightly",
        "balances": {
            "Checking": 4250, "Savings": 8000, "Emergency Fund": 5000,
            "Stocks ISA": 22180, "Brokerage": 6540,
            "Workplace Pension": 44950,
            "House": 325000, "Mortgage": -245000, "Credit Card": -680,
            "Life Insurance": 400000,
        },
    },
    {
        "days_ago": 14,
        "notes": "Mortgage paid; markets dipped",
        "balances": {
            "Checking": 2100, "Savings": 8200, "Emergency Fund": 5000,
            "Stocks ISA": 21450, "Brokerage": 6320,
            "Workplace Pension": 44950,
            "House": 325000, "Mortgage": -244500, "Credit Card": -680,
            "Life Insurance": 400000,
        },
    },
    {
        "days_ago": 7,
        "notes": "Credit card cleared; markets recovering",
        "balances": {
            "Checking": 1450, "Savings": 8500, "Emergency Fund": 5100,
            "Stocks ISA": 22300, "Brokerage": 6580,
            "Workplace Pension": 44950,
            "House": 325000, "Mortgage": -244500, "Credit Card": 0,
            "Life Insurance": 400000,
        },
    },
    {
        "days_ago": 0,
        "notes": "End-of-month wrap-up",
        "balances": {
            "Checking": 1680, "Savings": 8500, "Emergency Fund": 5100,
            "Stocks ISA": 22580, "Brokerage": 6610,
            "Workplace Pension": 45050,
            "House": 325000, "Mortgage": -244500, "Credit Card": -180,
            "Life Insurance": 400000,
        },
    },
]


def _default_db_path() -> Path:
    """Place demo.db alongside the real DB when running in Docker (/data is
    the mount point); fall back to <repo>/data/demo.db on a local checkout."""
    if Path("/data").is_dir():
        return Path("/data/demo.db")
    return (Path(__file__).resolve().parent.parent / "data" / "demo.db").resolve()


def _ensure_schema(engine) -> None:
    """Run Alembic migrations so tables exist when invoked on a fresh DB."""
    from alembic.config import Config
    from alembic import command

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))

    if "accounts" in existing and "alembic_version" not in existing:
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")


def _ensure_categories(db: Session) -> None:
    """Add any of the standard categories that don't already exist."""
    existing = {c.name for c in db.scalars(select(Category))}
    for name, sort, in_nw, in_liq, is_liab, color in CATEGORIES:
        if name in existing:
            continue
        db.add(Category(
            name=name, sort_order=sort,
            in_net_worth=in_nw, in_liquid=in_liq,
            is_liability=is_liab, color=color,
        ))
    db.commit()


def _wipe(db: Session) -> None:
    """Remove all accounts, snapshots, and balances. Preserves categories."""
    db.execute(delete(Balance))
    db.execute(delete(Snapshot))
    db.execute(delete(Account))
    db.commit()


def _seed(db: Session, base_dt: datetime) -> None:
    cats = {c.name: c for c in db.scalars(select(Category))}

    accounts: dict[str, Account] = {}
    sort = 10
    # Pass 1: parent groups so children can FK to them.
    for name, cat_name, notes, marker, is_group, domain in ACCOUNTS:
        if not is_group:
            continue
        a = Account(
            name=name, category_id=cats[cat_name].id,
            notes=notes, sort_order=sort, is_active=True,
            is_group=True, parent_id=None,
            institution_domain=domain,
        )
        db.add(a)
        accounts[name] = a
        sort += 10
    db.flush()

    # Pass 2: leaf accounts (some parented to a group).
    for name, cat_name, notes, marker, is_group, domain in ACCOUNTS:
        if is_group:
            continue
        parent_id = accounts[PARENT_MARKERS[marker]].id if marker else None
        a = Account(
            name=name, category_id=cats[cat_name].id,
            notes=notes, sort_order=sort, is_active=True,
            is_group=False, parent_id=parent_id,
            institution_domain=domain,
        )
        db.add(a)
        accounts[name] = a
        sort += 10
    db.flush()

    for snap_def in SNAPSHOTS:
        snap_dt = base_dt - timedelta(days=snap_def["days_ago"])
        snap = Snapshot(snapshot_date=snap_dt, notes=snap_def["notes"])
        db.add(snap)
        db.flush()
        for acc_name, amount in snap_def["balances"].items():
            db.add(Balance(
                snapshot_id=snap.id,
                account_id=accounts[acc_name].id,
                amount=Decimal(str(amount)),
            ))
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed a separate demo SQLite database with realistic demo data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="Path to the demo SQLite file (default: /data/demo.db in Docker, "
             "<repo>/data/demo.db locally). Never reads DATABASE_URL — by design.",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Wipe existing demo data first (categories preserved).",
    )
    args = parser.parse_args()

    db_path = (args.db or _default_db_path()).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{db_path}"

    engine = create_engine(
        db_url, connect_args={"check_same_thread": False}, future=True,
    )
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    _ensure_schema(engine)

    with Session() as db:
        has_data = bool(
            db.scalar(select(Account.id).limit(1))
            or db.scalar(select(Snapshot.id).limit(1))
        )
        if has_data and not args.reset:
            print(f"Refusing to seed: {db_path} already has demo data.")
            print("Re-run with --reset to wipe it, or pass --db <path> to use a different file.")
            sys.exit(1)
        if args.reset:
            _wipe(db)
        _ensure_categories(db)
        _seed(db, base_dt=datetime.now().replace(microsecond=0))

    group_count = sum(1 for row in ACCOUNTS if row[4])
    leaf_count = len(ACCOUNTS) - group_count
    print(
        f"Seeded {leaf_count} leaf accounts under {group_count} groups "
        f"and {len(SNAPSHOTS)} snapshots into {db_path}."
    )
    print("Point the app at the demo DB to view it, e.g.:")
    print(f"  DATABASE_URL='sqlite:///{db_path}' uvicorn app.main:app")


if __name__ == "__main__":
    main()

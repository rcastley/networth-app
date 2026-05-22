"""First-boot seed (categories + accounts) and idempotent data upgrades."""
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from .models import Category, Account


CATEGORIES = [
    # name,            sort, in_nw, in_liq, is_liab, color
    ("Cash",            10,  True,  True,   False,   "#3B82F6"),
    ("Investments",     20,  True,  True,   False,   "#10B981"),
    ("Restricted",      30,  True,  False,  False,   "#8B5CF6"),
    ("Pension",         40,  True,  False,  False,   "#F59E0B"),
    ("Property",        50,  True,  False,  False,   "#6366F1"),
    ("Liability",       60,  True,  False,  True,    "#EF4444"),
    ("Life Insurance",  70,  False, False,  False,   "#14B8A6"),
]

# (name, category, notes, sort, parent_marker)
# parent_marker is a string used to link children to their parent in the same seed pass.
# None = top-level account. A string = "I'm a child of the account marked with this string".
# Parent group rows are defined by `is_group=True` with parent_marker matching their children.
ACCOUNTS = [
    # name,                  category,         notes,                            sort, parent_marker, is_group
    ("NS&I",                 "Cash",           "Premium Bonds / Direct Saver",    10, None,    False),
    ("Monzo",                "Cash",           "Monzo (parent group)",            20, None,    True),
    ("Monzo Personal",       "Cash",           "Main current account",            30, "monzo", False),
    ("Monzo Flex",           "Cash",           "Credit card balance (negative)",  40, "monzo", False),
    ("Monzo Christmas",      "Cash",           "Pot",                             50, "monzo", False),
    ("Monzo Joint",          "Cash",           "Joint pot",                       60, "monzo", False),
    ("Monzo Savings",        "Cash",           "Pot",                             70, "monzo", False),
    ("Monzo Cruise",         "Cash",           "Pot",                             80, "monzo", False),
    ("Monzo Wedding",        "Cash",           "Pot",                             90, "monzo", False),
    ("Monzo Investment",     "Cash",           "Pot",                            100, "monzo", False),
    ("Monzo Cashback",       "Cash",           "Pot",                            110, "monzo", False),
    ("Monzo 1p Saving",      "Cash",           "Pot",                            120, "monzo", False),
    ("Monzo Charlie Loan",   "Cash",           "Pot",                            130, "monzo", False),
    ("Monzo Alfie Loan",     "Cash",           "Pot",                            140, "monzo", False),
    ("RBS",                  "Cash",           "Current account",                150, None,    False),
    ("Trading 212",          "Investments",   "Brokerage (parent group)",       155, None,    True),
    ("Trading 212 (1)",      "Investments",   "Stocks ISA",                      160, "t212",  False),
    ("Trading 212 (2)",      "Investments",   "Invest account",                  170, "t212",  False),
    ("Raisin",               "Cash",           "Savings marketplace",            180, None,    False),
    ("ESPP",                 "Investments",   "Employee Share Purchase Plan",   190, None,    False),
    ("Restricted Holdings",  "Restricted",    "Unvested RSUs / locked equity",  200, None,    False),
    ("Pension",              "Pension",       "All pension pots combined",      210, None,    False),
    ("House",                "Property",      "Estimated market value",         220, None,    False),
    ("Mortgage",             "Liability",     "Outstanding mortgage",           230, None,    False),
    ("DIS",                  "Life Insurance","Death in Service benefit",       240, None,    False),
]

# Maps parent_marker → seed-row name of the parent group
PARENT_MARKERS = {
    "monzo": "Monzo",
    "t212":  "Trading 212",
}

# Default institution_domain for known top-level accounts.
# Logos are fetched at render-time from https://logo.clearbit.com/{domain}.
INSTITUTION_DEFAULTS = {
    "NS&I":         "nsandi.com",
    "Monzo":        "monzo.com",
    "RBS":          "rbs.co.uk",
    "Trading 212":  "trading212.com",
    "Raisin":       "raisin.co.uk",
}


def seed_if_empty(db: Session) -> None:
    """Populate categories + accounts on first boot."""
    if db.scalar(select(func.count(Category.id))):
        return

    cat_by_name: dict[str, Category] = {}
    for name, sort, in_nw, in_liq, is_liab, color in CATEGORIES:
        c = Category(
            name=name, sort_order=sort,
            in_net_worth=in_nw, in_liquid=in_liq,
            is_liability=is_liab, color=color,
        )
        db.add(c)
        cat_by_name[name] = c
    db.flush()

    # Two passes: first create parent groups, then children referencing them
    parent_by_name: dict[str, Account] = {}
    for name, cat_name, notes, sort, marker, is_group in ACCOUNTS:
        if is_group:
            a = Account(
                name=name, category_id=cat_by_name[cat_name].id,
                notes=notes, sort_order=sort, is_active=True,
                is_group=True, parent_id=None,
            )
            db.add(a)
            parent_by_name[name] = a
    db.flush()

    for name, cat_name, notes, sort, marker, is_group in ACCOUNTS:
        if is_group:
            continue
        parent_id = None
        if marker and marker in PARENT_MARKERS:
            parent_id = parent_by_name[PARENT_MARKERS[marker]].id
        db.add(Account(
            name=name, category_id=cat_by_name[cat_name].id,
            notes=notes, sort_order=sort, is_active=True,
            is_group=False, parent_id=parent_id,
        ))
    db.flush()

    # Apply institution domain defaults
    for name, domain in INSTITUTION_DEFAULTS.items():
        acc = db.scalar(select(Account).where(Account.name == name))
        if acc and not acc.institution_domain:
            acc.institution_domain = domain

    db.commit()


def upgrade_existing_data(db: Session) -> None:
    """Idempotent: convert flat Monzo / Trading 212 layouts from v1 into grouped layouts.
    Safe to call on every startup."""
    # --- Monzo group ---
    monzo = db.scalar(select(Account).where(Account.name == "Monzo", Account.is_group == True))
    if monzo is None:
        # Are there any Monzo* accounts that aren't already parented?
        monzo_children = list(db.scalars(
            select(Account).where(
                Account.name.like("Monzo %"),
                Account.parent_id.is_(None),
                Account.is_group == False,
            )
        ))
        if monzo_children:
            cash_cat = db.scalar(select(Category).where(Category.name == "Cash"))
            if cash_cat:
                monzo = Account(
                    name="Monzo", category_id=cash_cat.id,
                    notes="Monzo (parent group)", sort_order=20,
                    is_active=True, is_group=True, parent_id=None,
                )
                db.add(monzo)
                db.flush()
                for child in monzo_children:
                    child.parent_id = monzo.id

    # --- Trading 212 group ---
    t212 = db.scalar(select(Account).where(Account.name == "Trading 212", Account.is_group == True))
    if t212 is None:
        t212_children = list(db.scalars(
            select(Account).where(
                Account.name.like("Trading 212 %"),
                Account.parent_id.is_(None),
                Account.is_group == False,
            )
        ))
        if t212_children:
            inv_cat = db.scalar(select(Category).where(Category.name == "Investments"))
            if inv_cat:
                t212 = Account(
                    name="Trading 212", category_id=inv_cat.id,
                    notes="Brokerage (parent group)", sort_order=155,
                    is_active=True, is_group=True, parent_id=None,
                )
                db.add(t212)
                db.flush()
                for child in t212_children:
                    child.parent_id = t212.id

    # --- Institution domain defaults: fill blanks only ---
    for name, domain in INSTITUTION_DEFAULTS.items():
        acc = db.scalar(select(Account).where(Account.name == name))
        if acc and not acc.institution_domain:
            acc.institution_domain = domain

    db.commit()

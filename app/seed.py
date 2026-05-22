"""First-boot seed: populate categories. Accounts are added by the user via the UI."""
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from .models import Category


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


def seed_if_empty(db: Session) -> None:
    """Populate categories on first boot. The category list is a starting point —
    edit, add, or remove on the Categories page."""
    if db.scalar(select(func.count(Category.id))):
        return

    for name, sort, in_nw, in_liq, is_liab, color in CATEGORIES:
        db.add(Category(
            name=name, sort_order=sort,
            in_net_worth=in_nw, in_liquid=in_liq,
            is_liability=is_liab, color=color,
        ))
    db.commit()

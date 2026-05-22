"""SQLAlchemy ORM models."""
from datetime import datetime, date, timezone
from sqlalchemy import (
    Column, Integer, String, Boolean, Date, DateTime, Numeric,
    ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from .db import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Category(Base):
    __tablename__ = "categories"
    id           = Column(Integer, primary_key=True)
    name         = Column(String, unique=True, nullable=False)
    sort_order   = Column(Integer, default=100, nullable=False)
    in_net_worth = Column(Boolean, default=True,  nullable=False)
    in_liquid    = Column(Boolean, default=False, nullable=False)
    is_liability = Column(Boolean, default=False, nullable=False)
    color        = Column(String, default="#3B82F6", nullable=False)

    accounts = relationship("Account", back_populates="category")


class Account(Base):
    __tablename__ = "accounts"
    id          = Column(Integer, primary_key=True)
    name        = Column(String, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False)
    notes       = Column(String, default="", nullable=False)
    sort_order  = Column(Integer, default=100, nullable=False)
    is_active   = Column(Boolean, default=True, nullable=False)
    parent_id   = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    is_group    = Column(Boolean, default=False, nullable=False)
    institution_domain = Column(String, nullable=True)  # e.g. "monzo.com"
    logo_url           = Column(String, nullable=True)  # full URL override

    category = relationship("Category", back_populates="accounts")
    balances = relationship("Balance", back_populates="account", cascade="all, delete-orphan")
    parent   = relationship("Account", remote_side=[id], backref="children")


class Snapshot(Base):
    __tablename__ = "snapshots"
    id            = Column(Integer, primary_key=True)
    snapshot_date = Column(DateTime, nullable=False, index=True)  # multiple per day allowed
    notes         = Column(String, default="", nullable=False)
    created_at    = Column(DateTime, default=_utcnow_naive, nullable=False)

    balances = relationship(
        "Balance",
        back_populates="snapshot",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Balance(Base):
    __tablename__ = "balances"
    id          = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False)
    account_id  = Column(Integer, ForeignKey("accounts.id",  ondelete="CASCADE"), nullable=False, index=True)
    amount      = Column(Numeric(14, 2), nullable=False, default=0)

    snapshot = relationship("Snapshot", back_populates="balances")
    account  = relationship("Account",  back_populates="balances")

    __table_args__ = (UniqueConstraint("snapshot_id", "account_id", name="uq_snap_acc"),)

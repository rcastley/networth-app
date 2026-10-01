"""FastAPI app for the Net Worth Tracker."""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import yaml
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import FastAPI, Depends, Form, Request, HTTPException, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db import Base, engine, get_db, SessionLocal, assert_foreign_key_integrity
from .models import Account, Balance, Category, Snapshot, SnapshotFxRate
from .seed import seed_if_empty
from . import fx, services


logger = logging.getLogger("networth")


# Server-side dictionary of toast messages. base.html resolves the key sent in
# the URL — unknown keys render nothing, so the toast text isn't attacker-controllable.
TOAST_MESSAGES = {
    "snapshot-created": "Snapshot created",
    "snapshot-updated": "Snapshot updated",
    "snapshot-deleted": "Snapshot deleted",
    "account-created":  "Account created",
    "account-saved":    "Account saved",
    "account-deleted":  "Account deleted",
    "category-created": "Category created",
    "category-saved":   "Category saved",
    "category-deleted": "Category deleted",
    "settings-saved": "Reporting currency updated",
}


def _redirect_with_toast(url: str, key: str, kind: str = "success") -> RedirectResponse:
    """303 redirect carrying a one-shot toast keyed to TOAST_MESSAGES.
    base.html looks the key up; unknown keys render nothing."""
    if kind not in {"success", "error"}:
        kind = "success"
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["toast"] = key
    query["kind"] = kind
    new_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return RedirectResponse(url=new_url, status_code=303)

# ---------- App setup ----------
def _run_migrations() -> None:
    """Apply Alembic migrations on startup. Handles upgrade from pre-Alembic v1 installs."""
    from sqlalchemy import inspect
    from alembic.config import Config
    from alembic import command

    with SessionLocal() as db:
        assert_foreign_key_integrity(db)

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))

    # Upgrade path for v1 installs that were created via Base.metadata.create_all
    # and therefore have the tables but no alembic_version row.
    if "accounts" in existing and "alembic_version" not in existing:
        command.stamp(cfg, "0001")

    command.upgrade(cfg, "head")


_run_migrations()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    with SessionLocal() as db:
        services.assert_account_hierarchy_integrity(db)
        seed_if_empty(db)
    yield


app = FastAPI(title="Net Worth Tracker", lifespan=_lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

CSRF_COOKIE_NAME = "csrf"
CSRF_SECRET = os.environ.get("CSRF_SECRET", "").encode() or secrets.token_bytes(32)
CSRF_COOKIE_SECURE = os.environ.get("CSRF_COOKIE_SECURE", "").lower() in {
    "1", "true", "yes", "on",
}


def _new_csrf_token() -> str:
    value = secrets.token_urlsafe(32)
    signature = hmac.new(CSRF_SECRET, value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{signature}"


def _valid_csrf_token(token: Optional[str]) -> bool:
    if not token or len(token) > 256 or "." not in token:
        return False
    value, signature = token.rsplit(".", 1)
    expected = hmac.new(CSRF_SECRET, value.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _require_csrf(request: Request, csrf_token: str = Form("")) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not _valid_csrf_token(cookie_token)
        or not hmac.compare_digest(cookie_token, csrf_token)
    ):
        logger.warning(
            "CSRF validation failed",
            extra={
                "event": "csrf_validation_failed",
                "method": request.method,
                "path": request.url.path,
            },
        )
        raise HTTPException(403, "Security token invalid or expired. Refresh the page and try again.")


@app.middleware("http")
async def csrf_cookie_middleware(request: Request, call_next):
    token = request.cookies.get(CSRF_COOKIE_NAME)
    set_cookie = not _valid_csrf_token(token)
    if set_cookie:
        token = _new_csrf_token()
    request.state.csrf_token = token
    response = await call_next(request)
    if set_cookie:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            httponly=True,
            secure=CSRF_COOKIE_SECURE,
            samesite="strict",
            path="/",
        )
    return response


def _dt_display(value) -> str:
    """Friendly date/datetime: show only date when time is midnight, else date + HH:MM."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _dt_local(value) -> str:
    """ISO format suitable for <input type='datetime-local' value=...>."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M")
    return str(value)


def _money(value, currency_code: str = "GBP") -> str:
    """Format money using an explicit ISO currency code."""
    try:
        return fx.format_currency(value, currency_code)
    except ValueError:
        return "–"


def _pct(value, total) -> str:
    try:
        if total in (None, 0) or Decimal(total) == 0:
            return "–"
        return f"{(Decimal(value) / Decimal(total)) * 100:.1f}%"
    except (InvalidOperation, TypeError, ZeroDivisionError):
        return "–"


def _account_logo(acc) -> dict:
    """Resolve an account's logo by walking up the parent chain.
    Returns {"primary": url, "fallback": url} — either may be None.

    - If the account (or an ancestor) has logo_url, that's the primary, no fallback.
    - Else if it has institution_domain, primary=clearbit, fallback=google favicons.
    - Else returns Nones.
    """
    cur = acc
    while cur is not None:
        if cur.logo_url:
            return {"primary": cur.logo_url, "fallback": None}
        if cur.institution_domain:
            d = cur.institution_domain
            return {
                "primary":  f"https://logo.clearbit.com/{d}",
                "fallback": f"https://www.google.com/s2/favicons?domain={d}&sz=128",
            }
        cur = cur.parent
    return {"primary": None, "fallback": None}


templates.env.filters["money"] = _money
templates.env.filters["gbp"] = _money  # Backward-compatible for third-party templates.
templates.env.filters["pct"] = _pct
templates.env.filters["dt"]  = _dt_display
templates.env.filters["dtlocal"] = _dt_local
templates.env.globals["delta"] = services.delta
templates.env.globals["account_logo"] = _account_logo
templates.env.globals["currency_options"] = fx.currency_options()

# Help content (loaded once at startup; restart to pick up edits).
# YAML structure:
#   views: { <view_id>: { title, sections: [{id, title, body}, ...] }, ... }
#   global: [{id, title, body}, ...]   (appended to every view)
_help_path = BASE_DIR / "help.yaml"
try:
    _help_doc = yaml.safe_load(_help_path.read_text()) or {}
except (FileNotFoundError, OSError, yaml.YAMLError):
    _help_doc = {}
templates.env.globals["help_views"]  = _help_doc.get("views", {})
templates.env.globals["help_global"] = _help_doc.get("global", [])
templates.env.globals["toast_messages"] = TOAST_MESSAGES


# ---------- Home (accounts + insights) ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    reporting = services.reporting_currency(db)
    latest = services.latest_snapshot(db)
    previous = services.previous_snapshot(db, latest) if latest else None
    totals = services.snapshot_totals(latest, reporting) if latest else None
    prev_totals = services.snapshot_totals(previous, reporting) if previous else None
    snaps_count = db.scalar(select(func.count(Snapshot.id))) or 0

    latest_balances = services.balance_map(latest, reporting) if latest else {}
    previous_balances = services.balance_map(previous, reporting) if previous else None
    grid = _build_card_grid(db, prefill=latest_balances, previous=previous_balances)
    account_count = sum(len(cb["cards"]) for cb in grid["categories"])

    return templates.TemplateResponse(
        "home.html",
        {
            "request":       request,
            "view_id":       "home",
            "snapshot":      latest,
            "totals":        totals,
            "previous":      previous,
            "prev_totals":   prev_totals,
            "snaps_count":   snaps_count,
            "grid":          grid,
            "account_count": account_count,
            "latest_date":   latest.snapshot_date if latest else None,
            "now":           date.today(),
            "reporting_currency": reporting,
        },
    )


@app.get("/api/chart-data")
def chart_data(period: str = "weekly", db: Session = Depends(get_db)):
    return JSONResponse(services.time_series(db, period=period))


# ---------- Snapshots ----------

@app.get("/snapshots", response_class=HTMLResponse)
def snapshots_list(request: Request, db: Session = Depends(get_db)):
    reporting = services.reporting_currency(db)
    snaps = services.all_snapshots(db)
    return templates.TemplateResponse(
        "snapshots_list.html",
        {
            "request": request,
            "view_id": "snapshots",
            "snapshots": snaps,
            "totals_for": lambda snap: services.snapshot_totals(snap, reporting),
            "reporting_currency": reporting,
        },
    )


def _build_card_grid(
    db: Session,
    prefill: Optional[dict] = None,
    previous: Optional[dict] = None,
) -> dict:
    """Group active accounts into a card-friendly structure.

    Returns:
        {
          "categories": [
            {"category": Category,
             "cards": [
                {"account": Account (top-level / group),
                 "children": [Account, ...],  # empty for non-group leaves
                 "current_total": Decimal,    # sum of prefill across the card's leaves
                 "previous_total": Decimal,   # same against `previous` (None if no prior snapshot)
                }, ...
             ]}, ...
          ]
        }
    """
    if prefill is None:
        latest = services.latest_snapshot(db)
        prefill = services.balance_map(latest) if latest else {}
    all_active = services.active_accounts(db)
    by_parent: dict[int, list] = {}
    for a in all_active:
        if a.parent_id is not None:
            by_parent.setdefault(a.parent_id, []).append(a)

    cat_buckets: dict[int, dict] = {}
    for a in all_active:
        if a.parent_id is not None:
            continue  # only top-level accounts become cards
        cb = cat_buckets.setdefault(a.category_id, {
            "category": a.category, "cards": [], "_sort": (a.category.sort_order, a.category.id)
        })
        children = sorted(by_parent.get(a.id, []), key=lambda c: (c.sort_order, c.id))
        if a.is_group:
            current = sum((prefill[c.id] for c in children if c.id in prefill), Decimal("0"))
            has_any = any(c.id in prefill for c in children)
        else:
            current = prefill.get(a.id)
            has_any = a.id in prefill
        previous_total = None
        if previous is not None:
            if a.is_group:
                if any(c.id in previous for c in children):
                    previous_total = sum(
                        (previous[c.id] for c in children if c.id in previous),
                        Decimal("0"),
                    )
            else:
                previous_total = previous.get(a.id)
        cb["cards"].append({
            "account":        a,
            "children":       children,
            "current_total":  current if has_any else None,
            "previous_total": previous_total,
        })

    categories = sorted(cat_buckets.values(), key=lambda b: b["_sort"])
    for cb in categories:
        cb["cards"].sort(key=lambda c: (c["account"].sort_order, c["account"].id))
    return {"categories": categories}


def _snapshot_balance_inputs(form, accounts: list[Account]) -> list[tuple[Account, Decimal, str]]:
    entries: list[tuple[Account, Decimal, str]] = []
    for account in accounts:
        amount = _parse_decimal(form.get(f"acc_{account.id}"))
        if amount is None:
            continue
        try:
            currency = fx.normalize_currency(
                form.get(f"currency_{account.id}") or account.currency_code
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        entries.append((account, amount, currency))
    return entries


def _manual_rate_set_from_form(form, entries, reporting: str) -> fx.RateSet:
    direct_rates: dict[str, Decimal] = {}
    for account, _amount, currency in entries:
        if currency == reporting:
            continue
        raw = form.get(f"manual_rate_{account.id}")
        rate = _parse_decimal(raw)
        if rate is None:
            raise HTTPException(
                503,
                f"Current rates are unavailable. Go back and enter the manual "
                f"{currency} to {reporting} rate for {account.name}.",
            )
        if currency in direct_rates and direct_rates[currency] != rate:
            raise HTTPException(
                400,
                f"Use the same manual {currency}/{reporting} rate for every account.",
            )
        direct_rates[currency] = rate
    try:
        return fx.manual_rate_set(reporting, direct_rates)
    except (fx.FxError, InvalidOperation) as exc:
        raise HTTPException(400, str(exc)) from exc


async def _resolve_snapshot_rates(
    form,
    entries,
    reporting: str,
    existing: Optional[Snapshot] = None,
) -> Optional[fx.RateSet]:
    foreign = {currency for _account, _amount, currency in entries if currency != reporting}
    if not foreign:
        return None
    saved = services.snapshot_rate_map(existing) if existing else {}
    refresh = form.get("refresh_rates") == "on"
    preserve_saved = bool(saved) and not refresh
    needed = foreign
    if preserve_saved:
        if reporting not in saved:
            raise HTTPException(
                400,
                "The saved reporting-currency rate is missing. "
                "Select refresh to replace the saved rates.",
            )
        needed = foreign - saved.keys()
        if not needed:
            return None
    try:
        rate_set = await asyncio.to_thread(fx.fetch_rate_set)
        if not fx.rate_set_supports(rate_set, needed | {reporting}):
            raise fx.FxError("The daily-rate service omitted a selected currency.")
    except fx.FxError:
        rate_set = _manual_rate_set_from_form(
            form, [entry for entry in entries if entry[2] in needed], reporting
        )
    if preserve_saved:
        # Anchor new currencies to the saved reporting rate, preserving both the
        # existing cross-rates and their individual date/source metadata.
        try:
            for code in sorted(needed):
                services.merge_reporting_rate(existing, reporting, code, rate_set)
        except fx.FxError as exc:
            raise HTTPException(400, str(exc)) from exc
        return None
    return rate_set


def _snapshot_display_rates(
    snap: Optional[Snapshot],
    reporting: str,
) -> dict[str, dict[str, str]]:
    """Build exact native-to-reporting rates for snapshot-form display."""
    if snap is None or not snap.fx_rates:
        return {}
    rates = services.snapshot_rate_map(snap)
    rows = {row.currency_code: row for row in snap.fx_rates}
    display_rates: dict[str, dict[str, str]] = {}
    for code in fx.SUPPORTED_CURRENCIES:
        if code == reporting or code not in rates or reporting not in rates:
            continue
        relevant_rows = (rows[code], rows[reporting])
        display_rates[code] = {
            "rate": str(fx.direct_rate(code, reporting, rates)),
            "effective_date": max(
                row.effective_date for row in relevant_rows
            ).isoformat(),
            "source": ", ".join(sorted({
                row.source.upper() for row in relevant_rows
            })),
        }
    return display_rates


@app.get("/snapshots/new", response_class=HTMLResponse)
def snapshot_new_form(request: Request, db: Session = Depends(get_db)):
    if not services.active_leaf_accounts(db):
        return templates.TemplateResponse(
            "snapshot_form.html",
            {
                "request": request,
                "view_id": "snapshot_form",
                "no_accounts": True,
                "form_title": "New snapshot",
            },
        )
    reporting = services.reporting_currency(db)
    latest = services.latest_snapshot(db)
    prefill: dict[int, Decimal] = {}
    currencies: dict[int, str] = {}
    conversion_warnings: list[str] = []
    if latest:
        latest_rates = services.snapshot_rate_map(latest)
        for balance in latest.balances:
            target = balance.account.currency_code
            try:
                prefill[balance.account_id] = fx.convert(
                    balance.amount,
                    balance.currency_code,
                    target,
                    latest_rates,
                )
                currencies[balance.account_id] = target
            except fx.FxError:
                # Do not mislabel the previous native amount as the new default.
                # The user must enter a fresh value in the selected currency.
                currencies[balance.account_id] = target
                conversion_warnings.append(balance.account.name)
    converted = services.balance_map(latest, reporting)
    grid = _build_card_grid(db, converted)
    return templates.TemplateResponse(
        "snapshot_form.html",
        {
            "request": request,
            "view_id": "snapshot_form",
            "grid": grid,
            "prefill": prefill,
            "balance_currencies": currencies,
            "snapshot": None,
            "latest_date": latest.snapshot_date if latest else None,
            "default_date": _dt_local(datetime.now()),
            "form_title": "New snapshot",
            "submit_label": "Create snapshot",
            "form_action": "/snapshots",
            "reporting_currency": reporting,
            "rate_summary": None,
            "display_rates": {},
            "conversion_warnings": conversion_warnings,
        },
    )


@app.post("/snapshots")
async def snapshot_create(
    request: Request,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    if not services.active_leaf_accounts(db):
        raise HTTPException(400, "Add at least one account before creating a snapshot.")
    form = await request.form()
    snap_dt = _parse_dt(form.get("snapshot_date"))
    notes = (form.get("notes") or "").strip()

    accounts = services.active_leaf_accounts(db)
    entries = _snapshot_balance_inputs(form, accounts)
    reporting = services.reporting_currency(db)
    rate_set = await _resolve_snapshot_rates(form, entries, reporting)

    snap = Snapshot(snapshot_date=snap_dt, notes=notes)
    db.add(snap)
    db.flush()
    if rate_set:
        services.store_rate_set(snap, rate_set)
    for acc, amt, currency in entries:
        db.add(
            Balance(
                snapshot_id=snap.id,
                account_id=acc.id,
                amount=amt,
                currency_code=currency,
            )
        )
    db.commit()
    return _redirect_with_toast("/", "snapshot-created")


@app.get("/snapshots/{snap_id}/edit", response_class=HTMLResponse)
def snapshot_edit_form(snap_id: int, request: Request, db: Session = Depends(get_db)):
    snap = db.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    reporting = services.reporting_currency(db)
    prefill = services.balance_map(snap)
    currencies = services.balance_currency_map(snap)
    converted = services.balance_map(snap, reporting)
    grid = _build_card_grid(db, converted)
    display_rates = _snapshot_display_rates(snap, reporting)
    return templates.TemplateResponse(
        "snapshot_form.html",
        {
            "request": request,
            "view_id": "snapshot_form",
            "grid": grid,
            "prefill": prefill,
            "balance_currencies": currencies,
            "snapshot": snap,
            "latest_date": snap.snapshot_date,
            "default_date": _dt_local(snap.snapshot_date),
            "form_title": f"Edit snapshot {_dt_display(snap.snapshot_date)}",
            "submit_label": "Save changes",
            "form_action": f"/snapshots/{snap.id}",
            "reporting_currency": reporting,
            "rate_summary": services.snapshot_rate_summary(snap),
            "display_rates": display_rates,
        },
    )


@app.post("/snapshots/{snap_id}")
async def snapshot_update(
    snap_id: int,
    request: Request,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    snap = db.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    form = await request.form()
    snap.snapshot_date = _parse_dt(form.get("snapshot_date"))
    snap.notes = (form.get("notes") or "").strip()

    accounts = services.active_leaf_accounts(db)
    entries = _snapshot_balance_inputs(form, accounts)
    reporting = services.reporting_currency(db)
    rate_set = await _resolve_snapshot_rates(form, entries, reporting, snap)
    if rate_set:
        services.store_rate_set(snap, rate_set)

    # Replace balances
    existing = {b.account_id: b for b in snap.balances}
    submitted = {account.id: (amount, currency) for account, amount, currency in entries}
    for acc in accounts:
        amount_currency = submitted.get(acc.id)
        if acc.id in existing:
            if amount_currency is None:
                db.delete(existing[acc.id])
            else:
                existing[acc.id].amount = amount_currency[0]
                existing[acc.id].currency_code = amount_currency[1]
        elif amount_currency is not None:
            db.add(
                Balance(
                    snapshot_id=snap.id,
                    account_id=acc.id,
                    amount=amount_currency[0],
                    currency_code=amount_currency[1],
                )
            )
    db.commit()
    return _redirect_with_toast("/snapshots", "snapshot-updated")


@app.post("/snapshots/{snap_id}/delete")
def snapshot_delete(
    snap_id: int,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    snap = db.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    db.delete(snap)
    db.commit()
    return _redirect_with_toast("/snapshots", "snapshot-deleted")


# ---------- Accounts ----------

@app.get("/accounts/new", response_class=HTMLResponse)
def account_new_form(request: Request, db: Session = Depends(get_db)):
    categories = services.all_categories(db)
    all_accounts = list(db.scalars(select(Account).order_by(Account.sort_order, Account.id)))
    groups = [a for a in all_accounts if a.is_group and a.parent_id is None]
    return templates.TemplateResponse(
        "account_new.html",
        {
            "request": request,
            "view_id": "account_detail",
            "categories": categories,
            "groups": groups,
            "reporting_currency": services.reporting_currency(db),
        },
    )


@app.get("/accounts/{acc_id}", response_class=HTMLResponse)
def account_detail(acc_id: int, request: Request, db: Session = Depends(get_db)):
    acc = db.get(Account, acc_id)
    if not acc:
        raise HTTPException(404, f"Account {acc_id} not found.")
    categories = services.all_categories(db)
    all_accounts = list(db.scalars(select(Account).order_by(Account.sort_order, Account.id)))
    groups = [
        a for a in all_accounts
        if a.is_group and a.parent_id is None and a.id != acc.id
    ]

    # Per-account history for sparkline + recent values.
    # Recent values is a scrollable list in the UI; cap at 500 so a hyperactive
    # snapshotter doesn't ship a pathological payload to the client.
    reporting = services.reporting_currency(db)
    history = services.account_history(db, acc, reporting)
    recent = []
    for i in range(len(history) - 1, -1, -1):
        d, amt = history[i]
        prev = history[i - 1][1] if i > 0 else None
        recent.append({"date": d, "amount": amt, "previous": prev})
        if len(recent) >= 500:
            break

    children = sorted(acc.children, key=lambda c: (c.sort_order, c.id)) if acc.is_group else []
    children_latest = services.latest_balances_for_accounts(db, children, reporting)
    children_history = services.account_histories(db, children, reporting)

    return templates.TemplateResponse(
        "account_detail.html",
        {
            "request":         request,
            "view_id":         "account_detail",
            "account":         acc,
            "categories":      categories,
            "groups":          groups,
            "history":         history,
            "recent":          recent,
            "children":        children,
            "children_latest": children_latest,
            "children_history": children_history,
            "reporting_currency": reporting,
        },
    )


@app.post("/accounts")
async def account_create(
    request: Request,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    name = (form.get("name") or "").strip()
    category_id = int(form.get("category_id"))
    _ensure_category(db, category_id)
    notes = (form.get("notes") or "").strip()
    is_group = form.get("is_group") == "on"
    parent_raw = (form.get("parent_id") or "").strip()
    parent_id = int(parent_raw) if parent_raw else None
    institution_domain = (form.get("institution_domain") or "").strip() or None
    logo_url           = _validated_logo_url(form.get("logo_url"))
    try:
        currency_code = fx.normalize_currency(
            form.get("currency_code") or services.reporting_currency(db)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not name:
        raise HTTPException(400, "Name is required")
    _validate_account_hierarchy(
        db,
        account_id=None,
        is_group=is_group,
        parent_id=parent_id,
        has_children=False,
    )
    acc = Account(
        name=name, category_id=category_id, notes=notes,
        is_active=True,
        is_group=is_group, parent_id=parent_id,
        institution_domain=institution_domain, logo_url=logo_url,
        currency_code=currency_code,
    )
    db.add(acc)
    db.commit()
    return _redirect_with_toast(f"/accounts/{acc.id}", "account-created")


@app.post("/accounts/{acc_id}")
async def account_update(
    acc_id: int,
    request: Request,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    acc = db.get(Account, acc_id)
    if not acc:
        raise HTTPException(404)
    form = await request.form()
    acc.name = (form.get("name") or acc.name).strip()
    if form.get("category_id"):
        new_cat = int(form["category_id"])
        _ensure_category(db, new_cat)
        acc.category_id = new_cat
    acc.notes = (form.get("notes") or "").strip()
    new_is_group = form.get("is_group") == "on"
    parent_raw = (form.get("parent_id") or "").strip()
    new_parent = int(parent_raw) if parent_raw else None
    _validate_account_hierarchy(
        db,
        account_id=acc.id,
        is_group=new_is_group,
        parent_id=new_parent,
        has_children=bool(acc.children),
    )
    acc.is_active = form.get("is_active") == "on"
    acc.is_group = new_is_group
    acc.parent_id = new_parent
    acc.institution_domain = (form.get("institution_domain") or "").strip() or None
    acc.logo_url           = _validated_logo_url(form.get("logo_url"))
    if not new_is_group:
        try:
            acc.currency_code = fx.normalize_currency(
                form.get("currency_code") or acc.currency_code
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    # is_modified() compares loaded state vs current — same-value assignments don't count.
    # Must be called before commit (which clears the session's attribute history).
    changed = db.is_modified(acc, include_collections=False)
    db.commit()
    if changed:
        return _redirect_with_toast(f"/accounts/{acc.id}", "account-saved")
    return RedirectResponse(url=f"/accounts/{acc.id}", status_code=303)


@app.post("/accounts/{acc_id}/delete")
def account_delete(
    acc_id: int,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    acc = db.get(Account, acc_id)
    if not acc:
        raise HTTPException(404)
    if acc.children:
        raise HTTPException(
            409,
            "Move or delete this group's sub-accounts before deleting the group.",
        )
    db.delete(acc)
    db.commit()
    return _redirect_with_toast("/", "account-deleted")


# ---------- Categories ----------

@app.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request, db: Session = Depends(get_db)):
    categories = services.all_categories(db)
    return templates.TemplateResponse(
        "categories.html",
        {"request": request, "view_id": "categories", "categories": categories},
    )


@app.post("/categories")
async def category_create(
    request: Request,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    cat = Category(
        name=name,
        in_net_worth=form.get("in_net_worth") == "on",
        in_liquid=form.get("in_liquid") == "on",
        is_liability=form.get("is_liability") == "on",
        color=(form.get("color") or "#3B82F6").strip(),
    )
    db.add(cat)
    db.commit()
    return _redirect_with_toast("/categories", "category-created")


@app.post("/categories/{cat_id}")
async def category_update(
    cat_id: int,
    request: Request,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    cat = db.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    form = await request.form()
    cat.name = (form.get("name") or cat.name).strip()
    cat.in_net_worth = form.get("in_net_worth") == "on"
    cat.in_liquid = form.get("in_liquid") == "on"
    cat.is_liability = form.get("is_liability") == "on"
    cat.color = (form.get("color") or cat.color).strip()
    # is_modified() compares loaded state vs current — same-value assignments don't count.
    # Must be called before commit (which clears the session's attribute history).
    changed = db.is_modified(cat, include_collections=False)
    db.commit()
    if changed:
        return _redirect_with_toast("/categories", "category-saved")
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{cat_id}/delete")
def category_delete(
    cat_id: int,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    cat = db.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    if cat.accounts:
        raise HTTPException(400, f"Category in use by {len(cat.accounts)} account(s); reassign first.")
    db.delete(cat)
    db.commit()
    return _redirect_with_toast("/categories", "category-deleted")


# ---------- Settings ----------

def _currency_change_preview(db: Session, target: str) -> list[dict]:
    affected: list[dict] = []
    current = services.reporting_currency(db)
    for snap in services.all_snapshots(db, ascending=True):
        rates = services.snapshot_rate_map(snap)
        source_currencies = sorted({
            balance.currency_code
            for balance in snap.balances
            if balance.currency_code != target
        })
        missing = [
            code
            for code in source_currencies
            if code not in rates or current not in rates or target not in rates
        ]
        if missing:
            affected.append({"snapshot": snap, "currencies": missing})
    return affected


def _settings_context(
    request: Request,
    db: Session,
    *,
    selected: Optional[str] = None,
    preview: Optional[list[dict]] = None,
    error: Optional[str] = None,
) -> dict:
    current = services.reporting_currency(db)
    return {
        "request": request,
        "view_id": "settings",
        "current_currency": current,
        "selected_currency": selected or current,
        "preview": preview,
        "error": error,
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "settings.html",
        _settings_context(request, db),
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_update(
    request: Request,
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        target = fx.normalize_currency(form.get("reporting_currency"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    current = services.reporting_currency(db)
    if target == current:
        return RedirectResponse("/settings", status_code=303)

    preview = _currency_change_preview(db, target)
    if form.get("confirmed") != "yes":
        return templates.TemplateResponse(
            "settings.html",
            _settings_context(request, db, selected=target, preview=preview),
        )

    rate_dates = sorted({item["snapshot"].snapshot_date.date() for item in preview})
    max_settings_rate_dates = 366
    if len(rate_dates) > max_settings_rate_dates:
        raise HTTPException(
            400,
            f"Changing currency requires more than {max_settings_rate_dates} "
            "historical rate dates, which exceeds the safe per-request limit.",
        )
    semaphore = asyncio.Semaphore(4)

    async def fetch_historical(rate_date: date):
        async with semaphore:
            try:
                return rate_date, await asyncio.to_thread(
                    fx.fetch_rate_set,
                    rate_date,
                )
            except fx.FxError as exc:
                return rate_date, exc

    settings_rate_cache = dict(
        await asyncio.gather(*(fetch_historical(rate_date) for rate_date in rate_dates))
    )

    for item in preview:
        snap = item["snapshot"]
        manual: Optional[dict[str, Decimal]] = None
        rate_set: Optional[fx.RateSet] = None
        try:
            cached_rate = settings_rate_cache[snap.snapshot_date.date()]
            if isinstance(cached_rate, fx.FxError):
                raise cached_rate
            rate_set = cached_rate
            saved = services.snapshot_rate_map(snap)
            required = (
                {current, target}
                if saved
                else set(item["currencies"]) | {target}
            )
            if not fx.rate_set_supports(rate_set, required):
                raise fx.FxError("A required historical currency is unavailable.")
        except fx.FxError:
            manual = {}
            for code in item["currencies"]:
                rate = _parse_decimal(form.get(f"manual_{snap.id}_{code}"))
                if rate is None:
                    return templates.TemplateResponse(
                        "settings.html",
                        _settings_context(
                            request,
                            db,
                            selected=target,
                            preview=preview,
                            error=(
                                "Some historical rates are unavailable. Enter each "
                                "requested manual rate and confirm again."
                            ),
                        ),
                        status_code=503,
                    )
                manual[code] = rate
        saved = services.snapshot_rate_map(snap)
        if manual is not None and saved:
            try:
                services.merge_manual_reporting_rate(
                    snap,
                    target,
                    manual,
                    snap.snapshot_date.date(),
                )
            except fx.FxError as exc:
                raise HTTPException(400, str(exc)) from exc
        elif manual is not None:
            services.store_rate_set(
                snap,
                fx.manual_rate_set(
                    target,
                    manual,
                    snap.snapshot_date.date(),
                ),
            )
        elif saved and rate_set is not None:
            services.merge_reporting_rate(snap, current, target, rate_set)
        elif rate_set is not None:
            services.store_rate_set(snap, rate_set)

    settings = services.get_settings(db)
    settings.reporting_currency = target
    db.commit()
    return _redirect_with_toast("/settings", "settings-saved")


# ---------- Helpers ----------

def _parse_dt(value) -> datetime:
    """Parse a date/datetime string from form input. Accepts:
       - 'YYYY-MM-DDTHH:MM'        (HTML datetime-local)
       - 'YYYY-MM-DDTHH:MM:SS'
       - 'YYYY-MM-DD HH:MM[:SS]'   (ISO with space separator)
       - 'YYYY-MM-DD'              (date only → midnight of that day)
    """
    if not value:
        raise HTTPException(400, "Date and time are required.")
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise HTTPException(400, f"Couldn't understand date/time '{s}'. Use YYYY-MM-DD HH:MM.")


def _validated_logo_url(value: Optional[str]) -> Optional[str]:
    """Return a normalised http(s) URL, or None when empty. Reject other schemes."""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    if not (v.startswith("https://") or v.startswith("http://")):
        raise HTTPException(400, "logo_url must start with https:// or http://")
    return v


def _ensure_category(db: Session, category_id: int) -> None:
    if db.get(Category, category_id) is None:
        raise HTTPException(400, f"Category {category_id} does not exist.")


def _validate_account_hierarchy(
    db: Session,
    *,
    account_id: Optional[int],
    is_group: bool,
    parent_id: Optional[int],
    has_children: bool,
) -> None:
    """Enforce the supported top-level-group → leaf-account hierarchy."""
    if is_group and parent_id is not None:
        raise HTTPException(400, "Groups must be top-level accounts.")
    if has_children and not is_group:
        raise HTTPException(400, "Move or delete sub-accounts before converting this group.")
    if parent_id is None:
        return

    parent = db.get(Account, parent_id)
    if parent is None:
        raise HTTPException(400, f"Parent account {parent_id} does not exist.")
    if not parent.is_group:
        raise HTTPException(400, "The parent account must be a group.")
    if parent.parent_id is not None:
        raise HTTPException(400, "Nested groups are not supported.")
    if account_id is not None and _would_cycle(db, account_id, parent_id):
        raise HTTPException(400, "Setting that parent would create a cycle.")


def _would_cycle(db: Session, acc_id: int, new_parent_id: int) -> bool:
    """True if setting acc.parent_id = new_parent_id would form a cycle."""
    seen: set[int] = set()
    cur_id: Optional[int] = new_parent_id
    while cur_id is not None:
        if cur_id == acc_id or cur_id in seen:
            return True
        seen.add(cur_id)
        parent = db.get(Account, cur_id)
        if parent is None:
            return False
        cur_id = parent.parent_id
    return False


def _parse_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    for symbol in ("£", "$", "€", "¥"):
        s = s.replace(symbol, "")
    if s == "":
        return None
    try:
        amount = Decimal(s)
    except InvalidOperation as exc:
        raise HTTPException(400, f"Invalid monetary value: {value!s}") from exc
    if not amount.is_finite():
        raise HTTPException(400, "Monetary values must be finite numbers.")
    if abs(amount) > Decimal("999999999999.99"):
        raise HTTPException(400, "Monetary value is outside the supported range.")
    return amount


# ---------- CSV import / export ----------

def _parse_exported_fx_rates(value: object) -> list[dict]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if len(raw) > 64 * 1024:
        raise ValueError("FX Rates metadata is too large.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("FX Rates metadata is not valid JSON.") from exc
    if not isinstance(payload, list) or len(payload) > len(fx.SUPPORTED_CURRENCIES):
        raise ValueError("FX Rates metadata has an invalid structure.")
    parsed: list[dict] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("FX Rates metadata has an invalid entry.")
        code = fx.normalize_currency(item.get("currency"))
        if code in seen:
            raise ValueError(f"FX Rates metadata repeats {code}.")
        seen.add(code)
        try:
            rate = fx.normalize_rate(item.get("rate_per_eur"))
            effective_date = datetime.strptime(
                str(item.get("effective_date")),
                "%Y-%m-%d",
            ).date()
        except (fx.FxError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"FX Rates metadata has an invalid {code} rate.") from exc
        source = str(item.get("source") or "")
        if source not in {"ecb", "manual"}:
            raise ValueError(f"FX Rates metadata has an invalid {code} rate.")
        parsed.append({
            "currency_code": code,
            "rate_per_eur": rate,
            "effective_date": effective_date,
            "source": source,
        })
    return parsed

@app.get("/export")
def export_csv(db: Session = Depends(get_db)):
    """Wide-format CSV with native account amounts and companion currency columns."""
    leaves = services.active_leaf_accounts(db)
    snaps = services.all_snapshots(db, ascending=True)
    reporting = services.reporting_currency(db)

    sio = io.StringIO()
    w = csv.writer(sio)
    account_headers: list[str] = []
    for account in leaves:
        account_headers.extend([account.name, f"{account.name} Currency"])
    w.writerow(
        ["Date", "Notes", "FX Rates"]
        + account_headers
        + ["Liquid", "Net Worth", "Net Worth + Aux", "Reporting Currency"]
    )

    for snap in snaps:
        bm = services.balance_map(snap)
        currencies = services.balance_currency_map(snap)
        t = services.snapshot_totals(snap, reporting)
        frozen_rates = json.dumps(
            [
                {
                    "currency": rate.currency_code,
                    "rate_per_eur": str(rate.rate_per_eur),
                    "effective_date": rate.effective_date.isoformat(),
                    "source": rate.source,
                }
                for rate in sorted(snap.fx_rates, key=lambda item: item.currency_code)
            ],
            separators=(",", ":"),
        )
        row = [snap.snapshot_date.isoformat(), snap.notes, frozen_rates]
        for a in leaves:
            row.append(str(bm.get(a.id, "")) if a.id in bm else "")
            row.append(currencies.get(a.id, a.currency_code) if a.id in bm else "")
        row.extend([
            str(t["liquid"]),
            str(t["net_worth"]),
            str(t["net_worth_plus_aux"]),
            reporting,
        ])
        w.writerow(row)

    fname = f"networth-export-{date.today().isoformat()}.csv"
    return Response(
        content=sio.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    return templates.TemplateResponse("import.html", {"request": request, "view_id": "import_export", "result": None})


@app.post("/import", response_class=HTMLResponse)
async def import_csv(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("skip"),
    _csrf: None = Depends(_require_csrf),
    db: Session = Depends(get_db),
):
    """Import snapshots from a wide-format CSV.

    Required column: Date. Optional: Notes. Other columns are matched (case-insensitive)
    to existing account names; unmatched columns are reported but ignored.
    mode = 'skip' to leave existing-date snapshots untouched; 'overwrite' to replace them.
    """
    if not services.active_leaf_accounts(db):
        raise HTTPException(400, "Add at least one account before importing snapshots.")
    max_csv_bytes = 5 * 1024 * 1024
    raw = await file.read(max_csv_bytes + 1)
    if len(raw) > max_csv_bytes:
        raise HTTPException(413, "CSV files are limited to 5 MB.")
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if "Date" not in headers:
        raise HTTPException(400, "CSV must include a 'Date' column.")
    if len(headers) != len(set(headers)):
        raise HTTPException(
            400,
            "CSV column names must be unique; an account name may conflict "
            "with a currency column.",
        )

    accounts_by_name = {a.name.lower().strip(): a for a in services.active_leaf_accounts(db)}
    matched: dict[str, Account] = {}
    unmatched: list[str] = []
    for h in headers:
        if h in ("Date", "Notes", "FX Rates"):
            continue
        if h in ("Liquid", "Net Worth", "Net Worth + Aux", "Reporting Currency"):
            continue  # derived columns from the export — ignore on import
        key = h.lower().strip()
        if key in accounts_by_name:
            matched[h] = accounts_by_name[key]
            continue
        if any(
            h.endswith(suffix)
            and h[:-len(suffix)] in headers
            and h[:-len(suffix)].lower().strip() in accounts_by_name
            for suffix in (" Currency", " FX Rate")
        ):
            continue
        unmatched.append(h)

    created, skipped, overwritten = 0, 0, 0
    errors: list[str] = []
    rate_cache: dict[date, fx.RateSet | fx.FxError] = {}
    max_rows = 5000
    max_rate_dates = 366
    for i, row in enumerate(reader, start=2):
        if i > max_rows + 1:
            errors.append(f"Import stopped after {max_rows} data rows.")
            break
        date_str = (row.get("Date") or "").strip()
        if not date_str:
            continue
        try:
            snap_dt = _parse_dt(date_str)
        except HTTPException:
            errors.append(f"Row {i}: invalid date '{date_str}' (expected YYYY-MM-DD or YYYY-MM-DDTHH:MM).")
            continue

        # In skip/overwrite modes, match on exact timestamp.
        existing = db.scalar(select(Snapshot).where(Snapshot.snapshot_date == snap_dt))
        if existing:
            if mode == "skip":
                skipped += 1
                continue

        parsed_entries: list[tuple[Account, Decimal, str]] = []
        row_invalid = False
        for col, acc in matched.items():
            try:
                amt = _parse_decimal(row.get(col))
                currency = fx.normalize_currency(
                    row.get(f"{col} Currency") or acc.currency_code
                )
            except (HTTPException, ValueError) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                errors.append(f"Row {i}: {detail}")
                row_invalid = True
                break
            if amt is not None:
                parsed_entries.append((acc, amt, currency))
        if row_invalid:
            continue
        try:
            frozen_rate_rows = _parse_exported_fx_rates(row.get("FX Rates"))
        except ValueError as exc:
            errors.append(f"Row {i}: {exc}")
            continue

        reporting = services.reporting_currency(db)
        foreign = {currency for _acc, _amt, currency in parsed_entries if currency != reporting}
        rate_set = None
        frozen_rate_map = {
            item["currency_code"]: item["rate_per_eur"]
            for item in frozen_rate_rows
        }
        if foreign and frozen_rate_rows:
            if not foreign.issubset(frozen_rate_map) or reporting not in frozen_rate_map:
                errors.append(
                    f"Row {i}: frozen FX metadata omits a balance or reporting currency."
                )
                continue
        elif foreign:
            try:
                rate_date = snap_dt.date()
                if rate_date not in rate_cache:
                    if len(rate_cache) >= max_rate_dates:
                        errors.append(
                            f"Row {i}: import exceeds the {max_rate_dates}-date "
                            "foreign-rate lookup limit."
                        )
                        continue
                    try:
                        rate_cache[rate_date] = await asyncio.to_thread(
                            fx.fetch_rate_set,
                            rate_date,
                        )
                    except fx.FxError as exc:
                        rate_cache[rate_date] = exc
                cached_rate = rate_cache[rate_date]
                if isinstance(cached_rate, fx.FxError):
                    raise cached_rate
                rate_set = cached_rate
                if not fx.rate_set_supports(rate_set, foreign | {reporting}):
                    raise fx.FxError("A selected currency has no historical rate.")
            except fx.FxError as exc:
                errors.append(f"Row {i}: {exc}")
                continue
        if existing:
            db.delete(existing)
            db.flush()
            overwritten += 1
        snap = Snapshot(snapshot_date=snap_dt, notes=(row.get("Notes") or "").strip())
        db.add(snap)
        db.flush()
        if frozen_rate_rows:
            snap.fx_rates.extend(
                SnapshotFxRate(**item)
                for item in frozen_rate_rows
            )
        elif rate_set:
            services.store_rate_set(snap, rate_set)
        for acc, amt, currency in parsed_entries:
            db.add(
                Balance(
                    snapshot_id=snap.id,
                    account_id=acc.id,
                    amount=amt,
                    currency_code=currency,
                )
            )
        created += 1
    db.commit()

    return templates.TemplateResponse("import.html", {
        "request": request,
        "view_id": "import_export",
        "result": {
            "created": created,
            "skipped": skipped,
            "overwritten": overwritten,
            "matched":   [(h, a.name) for h, a in matched.items()],
            "unmatched": unmatched,
            "errors":    errors,
        },
    })


# ---------- Error handling ----------

_STATUS_TITLES = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Page not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Upload too large",
    422: "Invalid input",
    500: "Something went wrong on our end",
}


def _wants_html(request: Request) -> bool:
    """Content negotiation: JSON for /api/*, /healthz, and JSON-only Accept headers; HTML otherwise."""
    p = request.url.path
    if p.startswith("/api/") or p == "/healthz":
        return False
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return False
    return True


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if not _wants_html(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        "error.html",
        {
            "request":     request,
            "view_id":     "error",
            "status_code": exc.status_code,
            "title":       _STATUS_TITLES.get(exc.status_code, "Error"),
            "detail":      str(exc.detail) if exc.detail else None,
        },
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if not _wants_html(request):
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    return templates.TemplateResponse(
        "error.html",
        {
            "request":     request,
            "view_id":     "error",
            "status_code": 422,
            "title":       _STATUS_TITLES.get(422, "Error"),
            "detail":      "Some fields are missing or in the wrong format.",
            "hint":        "Use the Back button to return to the form — your data should still be there.",
            "errors":      exc.errors(),
        },
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled request error",
        extra={
            "event": "unhandled_request_error",
            "method": request.method,
            "path": request.url.path,
        },
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    if not _wants_html(request):
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "view_id": "error",
            "status_code": 500,
            "title": _STATUS_TITLES[500],
            "detail": "The request could not be completed.",
            "hint": "Check the application logs for the error reference and try again.",
        },
        status_code=500,
    )


# ---------- Health check ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}

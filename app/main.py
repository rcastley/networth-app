"""FastAPI app for the Net Worth Tracker."""
import csv
import io
import yaml
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, Form, Request, HTTPException, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db import Base, engine, get_db, SessionLocal
from .models import Category, Account, Snapshot, Balance
from .seed import seed_if_empty
from . import services

# ---------- App setup ----------
def _run_migrations() -> None:
    """Apply Alembic migrations on startup. Handles upgrade from pre-Alembic v1 installs."""
    from sqlalchemy import inspect
    from alembic.config import Config
    from alembic import command

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
        seed_if_empty(db)
    yield


app = FastAPI(title="Net Worth Tracker", lifespan=_lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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


def _gbp(value) -> str:
    """Format a Decimal/float/None as a GBP string."""
    if value is None:
        return "–"
    try:
        v = Decimal(value)
    except (InvalidOperation, TypeError):
        return "–"
    sign = "-" if v < 0 else ""
    abs_v = abs(v)
    formatted = f"£{abs_v:,.2f}"
    return f"({formatted})" if sign else formatted


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


templates.env.filters["gbp"] = _gbp
templates.env.filters["pct"] = _pct
templates.env.filters["dt"]  = _dt_display
templates.env.filters["dtlocal"] = _dt_local
templates.env.globals["delta"] = services.delta
templates.env.globals["account_logo"] = _account_logo

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


# ---------- Dashboard ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    latest = services.latest_snapshot(db)
    totals = services.snapshot_totals(latest) if latest else None
    previous = services.previous_snapshot(db, latest.snapshot_date) if latest else None
    prev_totals = services.snapshot_totals(previous) if previous else None
    snaps_count = db.scalar(select(func.count(Snapshot.id))) or 0
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request":     request,
            "view_id":     "dashboard",
            "snapshot":    latest,
            "totals":      totals,
            "previous":    previous,
            "prev_totals": prev_totals,
            "snaps_count": snaps_count,
            "now":         date.today(),
        },
    )


@app.get("/api/chart-data")
def chart_data(period: str = "weekly", db: Session = Depends(get_db)):
    return JSONResponse(services.time_series(db, period=period))


# ---------- Snapshots ----------

@app.get("/snapshots", response_class=HTMLResponse)
def snapshots_list(request: Request, db: Session = Depends(get_db)):
    snaps = services.all_snapshots(db)
    return templates.TemplateResponse(
        "snapshots_list.html",
        {"request": request, "view_id": "snapshots", "snapshots": snaps, "totals_for": services.snapshot_totals},
    )


def _build_card_grid(db: Session, prefill: Optional[dict] = None) -> dict:
    """Group active accounts into a card-friendly structure.
    Returns:
        {
          "categories": [
            {"category": Category,
             "cards": [
                {"account": Account (top-level / group),
                 "children": [Account, ...],  # empty for non-group leaves
                 "current_total": Decimal,    # sum of prefill across the card's leaves
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
            "category": a.category, "cards": [], "_sort": a.category.sort_order
        })
        children = sorted(by_parent.get(a.id, []), key=lambda c: c.sort_order)
        if a.is_group:
            current = sum((prefill[c.id] for c in children if c.id in prefill), Decimal("0"))
            has_any = any(c.id in prefill for c in children)
        else:
            current = prefill.get(a.id)
            has_any = a.id in prefill
        cb["cards"].append({
            "account":       a,
            "children":      children,
            "current_total": current if has_any else None,
        })

    categories = sorted(cat_buckets.values(), key=lambda b: b["_sort"])
    for cb in categories:
        cb["cards"].sort(key=lambda c: c["account"].sort_order)
    return {"categories": categories}


@app.get("/snapshots/new", response_class=HTMLResponse)
def snapshot_new_form(request: Request, db: Session = Depends(get_db)):
    latest = services.latest_snapshot(db)
    prefill = services.balance_map(latest)
    grid = _build_card_grid(db, prefill)
    return templates.TemplateResponse(
        "snapshot_form.html",
        {
            "request": request,
            "view_id": "snapshot_form",
            "grid": grid,
            "prefill": prefill,
            "snapshot": None,
            "latest_date": latest.snapshot_date if latest else None,
            "default_date": _dt_local(datetime.now()),
            "form_title": "New snapshot",
            "submit_label": "Create snapshot",
            "form_action": "/snapshots",
        },
    )


@app.post("/snapshots")
async def snapshot_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    snap_dt = _parse_dt(form.get("snapshot_date"))
    notes = (form.get("notes") or "").strip()

    snap = Snapshot(snapshot_date=snap_dt, notes=notes)
    db.add(snap)
    db.flush()

    for acc in services.active_leaf_accounts(db):
        raw = form.get(f"acc_{acc.id}")
        amt = _parse_decimal(raw)
        if amt is None:
            continue
        db.add(Balance(snapshot_id=snap.id, account_id=acc.id, amount=amt))
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/snapshots/{snap_id}/edit", response_class=HTMLResponse)
def snapshot_edit_form(snap_id: int, request: Request, db: Session = Depends(get_db)):
    snap = db.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    prefill = services.balance_map(snap)
    grid = _build_card_grid(db, prefill)
    return templates.TemplateResponse(
        "snapshot_form.html",
        {
            "request": request,
            "view_id": "snapshot_form",
            "grid": grid,
            "prefill": prefill,
            "snapshot": snap,
            "latest_date": snap.snapshot_date,
            "default_date": _dt_local(snap.snapshot_date),
            "form_title": f"Edit snapshot {_dt_display(snap.snapshot_date)}",
            "submit_label": "Save changes",
            "form_action": f"/snapshots/{snap.id}",
        },
    )


@app.post("/snapshots/{snap_id}")
async def snapshot_update(snap_id: int, request: Request, db: Session = Depends(get_db)):
    snap = db.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    form = await request.form()
    snap.snapshot_date = _parse_dt(form.get("snapshot_date"))
    snap.notes = (form.get("notes") or "").strip()

    # Replace balances
    existing = {b.account_id: b for b in snap.balances}
    for acc in services.active_leaf_accounts(db):
        raw = form.get(f"acc_{acc.id}")
        amt = _parse_decimal(raw)
        if acc.id in existing:
            if amt is None:
                db.delete(existing[acc.id])
            else:
                existing[acc.id].amount = amt
        elif amt is not None:
            db.add(Balance(snapshot_id=snap.id, account_id=acc.id, amount=amt))
    db.commit()
    return RedirectResponse(url="/snapshots", status_code=303)


@app.post("/snapshots/{snap_id}/delete")
def snapshot_delete(snap_id: int, db: Session = Depends(get_db)):
    snap = db.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    db.delete(snap)
    db.commit()
    return RedirectResponse(url="/snapshots", status_code=303)


# ---------- Accounts ----------

@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, db: Session = Depends(get_db)):
    latest        = services.latest_snapshot(db)
    grid          = _build_card_grid(db)
    totals        = services.snapshot_totals(latest) if latest else None
    account_count = sum(len(cb["cards"]) for cb in grid["categories"])
    return templates.TemplateResponse(
        "accounts.html",
        {
            "request":       request,
            "view_id":       "accounts",
            "grid":          grid,
            "totals":        totals,
            "latest_date":   latest.snapshot_date if latest else None,
            "account_count": account_count,
        },
    )


@app.get("/accounts/new", response_class=HTMLResponse)
def account_new_form(request: Request, db: Session = Depends(get_db)):
    categories = services.all_categories(db)
    all_accounts = list(db.scalars(select(Account).order_by(Account.sort_order)))
    groups = [a for a in all_accounts if a.is_group]
    return templates.TemplateResponse(
        "account_new.html",
        {
            "request": request,
            "view_id": "account_detail",
            "categories": categories,
            "groups": groups,
        },
    )


@app.get("/accounts/{acc_id}", response_class=HTMLResponse)
def account_detail(acc_id: int, request: Request, db: Session = Depends(get_db)):
    acc = db.get(Account, acc_id)
    if not acc:
        raise HTTPException(404, f"Account {acc_id} not found.")
    categories = services.all_categories(db)
    all_accounts = list(db.scalars(select(Account).order_by(Account.sort_order)))
    groups = [a for a in all_accounts if a.is_group and a.id != acc.id]

    # Per-account history for sparkline + recent values
    history = services.account_history(db, acc)
    # Last 10 values, newest first, with delta from previous
    recent = []
    for i in range(len(history) - 1, -1, -1):
        d, amt = history[i]
        prev = history[i - 1][1] if i > 0 else None
        recent.append({"date": d, "amount": amt, "previous": prev})
        if len(recent) >= 10:
            break

    children = sorted(acc.children, key=lambda c: c.sort_order) if acc.is_group else []
    children_latest = services.latest_balances_for_accounts(db, children)

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
        },
    )


@app.post("/accounts")
async def account_create(request: Request, db: Session = Depends(get_db)):
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
    if not name:
        raise HTTPException(400, "Name is required")
    max_sort = db.scalar(select(Account.sort_order).order_by(Account.sort_order.desc()).limit(1)) or 0
    acc = Account(
        name=name, category_id=category_id, notes=notes,
        sort_order=max_sort + 10, is_active=True,
        is_group=is_group, parent_id=parent_id,
        institution_domain=institution_domain, logo_url=logo_url,
    )
    db.add(acc)
    db.commit()
    return RedirectResponse(url=f"/accounts/{acc.id}", status_code=303)


@app.post("/accounts/{acc_id}")
async def account_update(acc_id: int, request: Request, db: Session = Depends(get_db)):
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
    acc.is_active = form.get("is_active") == "on"
    acc.is_group  = form.get("is_group")  == "on"
    parent_raw = (form.get("parent_id") or "").strip()
    new_parent = int(parent_raw) if parent_raw else None
    if new_parent is not None and _would_cycle(db, acc.id, new_parent):
        raise HTTPException(400, "Setting that parent would create a cycle.")
    acc.parent_id = new_parent
    acc.institution_domain = (form.get("institution_domain") or "").strip() or None
    acc.logo_url           = _validated_logo_url(form.get("logo_url"))
    sort_raw = (form.get("sort_order") or "").strip()
    if sort_raw:
        try:
            acc.sort_order = int(sort_raw)
        except ValueError:
            pass
    db.commit()
    return RedirectResponse(url=f"/accounts/{acc.id}", status_code=303)


@app.post("/accounts/{acc_id}/delete")
def account_delete(acc_id: int, db: Session = Depends(get_db)):
    acc = db.get(Account, acc_id)
    if not acc:
        raise HTTPException(404)
    db.delete(acc)
    db.commit()
    return RedirectResponse(url="/accounts", status_code=303)


# ---------- Categories ----------

@app.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request, db: Session = Depends(get_db)):
    categories = services.all_categories(db)
    return templates.TemplateResponse(
        "categories.html",
        {"request": request, "view_id": "categories", "categories": categories},
    )


@app.post("/categories")
async def category_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    max_sort = db.scalar(select(Category.sort_order).order_by(Category.sort_order.desc()).limit(1)) or 0
    cat = Category(
        name=name,
        sort_order=max_sort + 10,
        in_net_worth=form.get("in_net_worth") == "on",
        in_liquid=form.get("in_liquid") == "on",
        is_liability=form.get("is_liability") == "on",
        color=(form.get("color") or "#3B82F6").strip(),
    )
    db.add(cat)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{cat_id}")
async def category_update(cat_id: int, request: Request, db: Session = Depends(get_db)):
    cat = db.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    form = await request.form()
    cat.name = (form.get("name") or cat.name).strip()
    cat.in_net_worth = form.get("in_net_worth") == "on"
    cat.in_liquid = form.get("in_liquid") == "on"
    cat.is_liability = form.get("is_liability") == "on"
    cat.color = (form.get("color") or cat.color).strip()
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{cat_id}/delete")
def category_delete(cat_id: int, db: Session = Depends(get_db)):
    cat = db.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    if cat.accounts:
        raise HTTPException(400, f"Category in use by {len(cat.accounts)} account(s); reassign first.")
    db.delete(cat)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


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
    s = str(value).strip().replace(",", "").replace("£", "")
    if s == "":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# ---------- CSV import / export ----------

@app.get("/export")
def export_csv(db: Session = Depends(get_db)):
    """Wide-format CSV: Date, Notes, [per-leaf-account columns], Liquid, Net Worth, Net Worth + Aux."""
    leaves = services.active_leaf_accounts(db)
    snaps = services.all_snapshots(db, ascending=True)

    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerow(["Date", "Notes"] + [a.name for a in leaves]
               + ["Liquid", "Net Worth", "Net Worth + Aux"])

    for snap in snaps:
        bm = services.balance_map(snap)
        t = services.snapshot_totals(snap)
        row = [snap.snapshot_date.isoformat(), snap.notes]
        for a in leaves:
            row.append(str(bm.get(a.id, "")) if a.id in bm else "")
        row.extend([str(t["liquid"]), str(t["net_worth"]), str(t["net_worth_plus_aux"])])
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
    db: Session = Depends(get_db),
):
    """Import snapshots from a wide-format CSV.

    Required column: Date. Optional: Notes. Other columns are matched (case-insensitive)
    to existing account names; unmatched columns are reported but ignored.
    mode = 'skip' to leave existing-date snapshots untouched; 'overwrite' to replace them.
    """
    raw = await file.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if "Date" not in headers:
        raise HTTPException(400, "CSV must include a 'Date' column.")

    accounts_by_name = {a.name.lower().strip(): a for a in services.active_leaf_accounts(db)}
    matched: dict[str, Account] = {}
    unmatched: list[str] = []
    for h in headers:
        if h in ("Date", "Notes"):
            continue
        if h in ("Liquid", "Net Worth", "Net Worth + Aux"):
            continue  # derived columns from the export — ignore on import
        key = h.lower().strip()
        if key in accounts_by_name:
            matched[h] = accounts_by_name[key]
        else:
            unmatched.append(h)

    created, skipped, overwritten = 0, 0, 0
    errors: list[str] = []
    for i, row in enumerate(reader, start=2):
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
            else:
                db.delete(existing)
                db.flush()
                overwritten += 1

        snap = Snapshot(snapshot_date=snap_dt, notes=(row.get("Notes") or "").strip())
        db.add(snap)
        db.flush()
        for col, acc in matched.items():
            amt = _parse_decimal(row.get(col))
            if amt is None:
                continue
            db.add(Balance(snapshot_id=snap.id, account_id=acc.id, amount=amt))
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


# ---------- Health check ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}

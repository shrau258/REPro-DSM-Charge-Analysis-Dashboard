"""Download RE.Pro Historical Insights reports to disk and PostgreSQL.

The authenticated portal DOM was inspected. Locators remain strict and fail with
a screenshot/HTML snapshot rather than guessing an export.
"""
import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
# Prefer the project-local .env so stale process-level variables from a prior
# terminal session cannot silently override the credentials the user edited.
load_dotenv(ROOT / ".env", override=True)
URL = "https://grid.reconnectenergy.com/refs/index.php/C_gridconnect_ipp_performance/forecast_vs_actual"
SITES = ["zaheerabad", "mandrup", "turmamidi", "tuljapur_Amp", "kumbhari_solapur",
         "bhokar", "PGCIL_Nagda", "Fortune Metals Ltd", "umri", "karajagi"]
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
DEFAULT_MONTHS = ["April", "May", "June", "July", "August", "September", "October", "November"]
DEFAULT_YEAR = 2026
DB_PATH = Path(os.getenv("REPRO_DB_PATH", "reports.sqlite3"))
SQLITE_PATH = Path(os.getenv("REPRO_SQLITE_PATH", str(DB_PATH)))
DB_BACKEND = os.getenv("REPRO_DB_BACKEND", "postgres").strip().lower()
DOWNLOAD_DIR = Path(os.getenv("REPRO_DOWNLOAD_DIR", "downloads"))
ERROR_LOG = Path(os.getenv("REPRO_ERROR_LOG", "download_errors.log"))
HEADLESS = os.getenv("REPRO_HEADLESS", "false").lower() == "true"
LOG = logging.getLogger("repro")


class DatabaseConnection:
    """Small compatibility layer for sqlite3 and psycopg connections."""
    def __init__(self, raw, backend):
        self.raw = raw
        self.backend = backend

    def execute(self, query, params=()):
        if self.backend == "postgres":
            if params:
                query = query.replace("%", "%%").replace("?", "%s")
                return self.raw.execute(query, params)
            return self.raw.execute(query)
        return self.raw.execute(query, params) if params else self.raw.execute(query)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def database():
    if DB_BACKEND == "sqlite":
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(SQLITE_PATH)
        raw.execute("""CREATE TABLE IF NOT EXISTS excel_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT NOT NULL, year INTEGER NOT NULL,
            month INTEGER NOT NULL, original_filename TEXT NOT NULL, local_path TEXT NOT NULL,
            sha256 TEXT NOT NULL, content BLOB NOT NULL, downloaded_at TEXT NOT NULL,
            period_type TEXT NOT NULL DEFAULT 'monthly', start_date TEXT, end_date TEXT,
            UNIQUE(site, year, month))""")
        report_columns = {row[1] for row in raw.execute("PRAGMA table_info(excel_reports)")}
        if "period_type" not in report_columns:
            raw.execute("ALTER TABLE excel_reports ADD COLUMN period_type TEXT NOT NULL DEFAULT 'monthly'")
        if "start_date" not in report_columns:
            raw.execute("ALTER TABLE excel_reports ADD COLUMN start_date TEXT")
        if "end_date" not in report_columns:
            raw.execute("ALTER TABLE excel_reports ADD COLUMN end_date TEXT")
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("""CREATE TABLE IF NOT EXISTS excel_report_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES excel_reports(id) ON DELETE CASCADE,
            site TEXT NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL, row_number INTEGER NOT NULL,
            report_date TEXT, ac_mw NUMERIC, dc_mw NUMERIC, energy_type TEXT, report_type TEXT,
            scheduled_generation_mwh NUMERIC, actual_generation_mwh NUMERIC, gross_revenue_inr NUMERIC,
            dsm_charges_inr NUMERIC, dsm_charges_percent NUMERIC, net_revenue_inr NUMERIC,
            firstband_percent NUMERIC, gross_rev_actual_inr NUMERIC, gross_rev_scheduled_inr NUMERIC,
            final_charges_inr NUMERIC, net_rev_scheduled_inr NUMERIC, revenue_loss_inr NUMERIC,
            revenue_loss_percent NUMERIC, firstband TEXT, additional_data TEXT NOT NULL,
            UNIQUE(report_id, row_number))""")
        raw.execute("CREATE INDEX IF NOT EXISTS idx_excel_report_rows_site_month ON excel_report_rows(site, year, month)")
        raw.execute("CREATE INDEX IF NOT EXISTS idx_excel_report_rows_date ON excel_report_rows(report_date)")
        raw.commit()
        return DatabaseConnection(raw, "sqlite")
    if DB_BACKEND != "postgres":
        raise RuntimeError("REPRO_DB_BACKEND must be 'postgres' or 'sqlite'")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL driver missing. Activate .venv and run: pip install -r requirements.txt") from exc
    try:
        raw = psycopg.connect(
            host=os.getenv("REPRO_PG_HOST", "localhost"),
            port=int(os.getenv("REPRO_PG_PORT", "5432")),
            dbname=os.getenv("REPRO_PG_DATABASE", "repro_reports"),
            user=os.getenv("REPRO_PG_USER", "postgres"),
            password=os.getenv("REPRO_PG_PASSWORD", ""),
            connect_timeout=10,
        )
    except Exception as exc:
        detail = str(exc).lower()
        if "no password supplied" in detail or "password authentication failed" in detail:
            raise RuntimeError(
                "PostgreSQL is reachable, but authentication failed. Set REPRO_PG_PASSWORD in the project .env "
                "to the password for REPRO_PG_USER; do not paste it into the terminal command or chat."
            ) from exc
        raise RuntimeError(
            "Could not connect to PostgreSQL. Confirm the PostgreSQL service is running and "
            "REPRO_PG_HOST, REPRO_PG_PORT, REPRO_PG_DATABASE, REPRO_PG_USER, and "
            "REPRO_PG_PASSWORD in the project .env are correct."
        ) from exc
    con = DatabaseConnection(raw, "postgres")
    con.execute("""CREATE TABLE IF NOT EXISTS excel_reports (
        id BIGSERIAL PRIMARY KEY, site TEXT NOT NULL, year INTEGER NOT NULL,
        month INTEGER, period_type TEXT NOT NULL DEFAULT 'monthly', start_date DATE, end_date DATE,
        original_filename TEXT NOT NULL, local_path TEXT NOT NULL,
        sha256 TEXT NOT NULL, content BYTEA NOT NULL, downloaded_at TIMESTAMPTZ NOT NULL,
        UNIQUE(site, year, month))""")
    # Migrate existing monthly-only databases in place. Existing rows default to
    # period_type='monthly'; custom ranges get their own date-keyed uniqueness.
    con.execute("ALTER TABLE excel_reports ADD COLUMN IF NOT EXISTS period_type TEXT NOT NULL DEFAULT 'monthly'")
    con.execute("ALTER TABLE excel_reports ADD COLUMN IF NOT EXISTS start_date DATE")
    con.execute("ALTER TABLE excel_reports ADD COLUMN IF NOT EXISTS end_date DATE")
    con.execute("ALTER TABLE excel_reports ALTER COLUMN month DROP NOT NULL")
    con.execute("ALTER TABLE excel_reports DROP CONSTRAINT IF EXISTS excel_reports_site_year_month_key")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_excel_reports_month_identity ON excel_reports(site,year,month) WHERE period_type='monthly'")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_excel_reports_custom_identity ON excel_reports(site,start_date,end_date) WHERE period_type='custom'")
    con.execute("""CREATE TABLE IF NOT EXISTS excel_report_rows (
        id BIGSERIAL PRIMARY KEY, report_id BIGINT NOT NULL REFERENCES excel_reports(id) ON DELETE CASCADE,
        site TEXT NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL, row_number INTEGER NOT NULL,
        report_date DATE, ac_mw NUMERIC(16,4), dc_mw NUMERIC(16,4), energy_type TEXT, report_type TEXT,
        scheduled_generation_mwh NUMERIC(18,6), actual_generation_mwh NUMERIC(18,6), gross_revenue_inr NUMERIC(20,4),
        dsm_charges_inr NUMERIC(20,4), dsm_charges_percent NUMERIC(10,4), net_revenue_inr NUMERIC(20,4),
        firstband_percent NUMERIC(10,4), gross_rev_actual_inr NUMERIC(20,4), gross_rev_scheduled_inr NUMERIC(20,4),
        final_charges_inr NUMERIC(20,4), net_rev_scheduled_inr NUMERIC(20,4), revenue_loss_inr NUMERIC(20,4),
        revenue_loss_percent NUMERIC(10,4), firstband TEXT, additional_data JSONB NOT NULL,
        UNIQUE(report_id, row_number))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_excel_report_rows_site_month ON excel_report_rows(site, year, month)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_excel_report_rows_date ON excel_report_rows(report_date)")
    con.commit()
    return con


def migrate_sqlite_to_postgres(con):
    """Copy existing SQLite reports to PostgreSQL and verify each content hash."""
    if DB_BACKEND != "postgres":
        raise RuntimeError("Migration requires REPRO_DB_BACKEND=postgres")
    if not SQLITE_PATH.is_file():
        raise RuntimeError(f"SQLite migration source not found: {SQLITE_PATH}")
    source = sqlite3.connect(SQLITE_PATH)
    source.row_factory = sqlite3.Row
    rows = source.execute("SELECT id,site,year,month,original_filename,local_path,sha256,content,downloaded_at FROM excel_reports ORDER BY id").fetchall()
    source.close()
    inserted = 0
    for row in rows:
        content = bytes(row["content"])
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise RuntimeError(f"SQLite source hash mismatch for {row['site']} {row['year']}-{row['month']:02d}")
        cur = con.execute("""INSERT INTO excel_reports
            (id,site,year,month,original_filename,local_path,sha256,content,downloaded_at)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (row["id"], row["site"], row["year"], row["month"], row["original_filename"],
             row["local_path"], row["sha256"], content, row["downloaded_at"]))
        inserted += cur.rowcount
        target = con.execute("SELECT sha256,content FROM excel_reports WHERE site=? AND year=? AND month=?",
                             (row["site"], row["year"], row["month"])).fetchone()
        if not target or target[0] != row["sha256"] or bytes(target[1]) != content:
            raise RuntimeError(f"PostgreSQL verification mismatch for {row['site']} {row['year']}-{row['month']:02d}")
        report_id = con.execute("SELECT id FROM excel_reports WHERE site=? AND year=? AND month=?",
                                (row["site"], row["year"], row["month"])).fetchone()[0]
        replace_report_rows(con, report_id, row["site"], row["year"], row["month"], content)
    if rows:
        con.execute("SELECT setval(pg_get_serial_sequence('excel_reports','id'), (SELECT COALESCE(MAX(id),1) FROM excel_reports), true)")
    con.commit()
    return inserted, len(rows)


def parse_report_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized report date: {value!r}")


def parse_custom_date(value, label):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format") from exc


def parse_report_number(value):
    value = (value or "").strip()
    if not value or value.casefold() in {"n/a", "na", "-", "dna"}:
        return None
    cleaned = value.replace(",", "").replace(chr(0x20B9), "").replace("%", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def normalize_header(value):
    """Normalize CSV headers across UTF-8/legacy rupee symbol encodings."""
    value = (value or "").strip().replace(chr(0x20B9), "INR")
    value = (value or "").strip().replace("₹", "INR").replace("â‚¹", "INR")
    return re.sub(r"\s+", " ", value).casefold()


def csv_value(row, *names):
    normalized = {normalize_header(k): (v or "").strip() for k, v in row.items() if k is not None}
    for name in names:
        if normalize_header(name) in normalized:
            return normalized[normalize_header(name)]
    return ""


RUPEE_SYMBOL = chr(0x20B9)
REPORT_HEADERS = (
    "Date", "AC [MW]", "DC [MW]", "Energy Type", "Type",
    "Scheduled Generation [MWh]", "Actual Generation [MWh]",
    f"Gross Revenue [{RUPEE_SYMBOL}]", f"DSM Charges [{RUPEE_SYMBOL}]",
    "DSM Charges [%]", f"Net Revenue [{RUPEE_SYMBOL}]", "Firstband [%]",
)
REPORT_TYPES = (
    "DATE", "NUMERIC(16,4)", "NUMERIC(16,4)", "TEXT", "TEXT",
    "NUMERIC(18,6)", "NUMERIC(18,6)", "NUMERIC(20,4)",
    "NUMERIC(20,4)", "NUMERIC(10,4)", "NUMERIC(20,4)", "NUMERIC(10,4)",
)
SHEET_COLUMNS = tuple(f'"{name}" {dtype}' for name, dtype in zip(REPORT_HEADERS, REPORT_TYPES))
SHEET_COLUMN_NAMES = tuple(f'"{name}"' for name in REPORT_HEADERS)


def site_month_table(site, year, month):
    """Return a safe, readable Postgres/SQLite identifier for a report table."""
    slug = re.sub(r"[^a-z0-9]+", "_", site.casefold()).strip("_")
    name = f"report_{slug}_{year}_{month:02d}"
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError("Generated unsafe site/month table identifier")
    return name


def site_custom_table(site, start_date, end_date):
    """Return a unique safe identifier for a custom-date report table."""
    slug = re.sub(r"[^a-z0-9]+", "_", site.casefold()).strip("_")
    name = f"report_{slug}_custom_{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError("Generated unsafe custom-date table identifier")
    return name


def report_table_name(site, period_type, year, month, start_date, end_date):
    if period_type == "monthly":
        return site_month_table(site, year, month)
    return site_custom_table(site, start_date, end_date)


def replace_site_report_table(con, site, period_type, year, month, start_date, end_date, rows):
    """Create a per-report table with the exact CSV headings and order."""
    table_name = report_table_name(site, period_type, year, month, start_date, end_date)
    con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    con.execute(f'CREATE TABLE "{table_name}" ({", ".join(SHEET_COLUMNS)})')
    sql = (f'INSERT INTO "{table_name}" ({", ".join(SHEET_COLUMN_NAMES)}) '
           f'VALUES ({",".join("?" for _ in SHEET_COLUMN_NAMES)})')
    for values in rows:
        con.execute(sql, values)


def replace_report_rows(con, report_id, site, year, month, content,
                        period_type="monthly", start_date=None, end_date=None):
    """Replace normalized daily rows for one stored CSV report."""
    raw_content = bytes(content)
    try:
        text = raw_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_content.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError(f"Report CSV has no header: {site} {year}")
    actual_headers = [normalize_header(h) for h in reader.fieldnames]
    expected_headers = [normalize_header(h) for h in REPORT_HEADERS]
    missing = [REPORT_HEADERS[i] for i, name in enumerate(expected_headers) if name not in actual_headers]
    if actual_headers != expected_headers:
        extras = [header for header in reader.fieldnames if normalize_header(header) not in expected_headers]
        raise RuntimeError(
            f"CSV headers/order do not exactly match the required sheet layout for {site}; "
            f"missing={missing}, extra={extras}"
        )
    if con.backend == "postgres":
        from psycopg.types.json import Jsonb
    con.execute("DELETE FROM excel_report_rows WHERE report_id=?", (report_id,))
    site_month_rows = []
    inserted = 0
    insert_sql = """INSERT INTO excel_report_rows (
        report_id,site,year,month,row_number,report_date,ac_mw,dc_mw,energy_type,report_type,
        scheduled_generation_mwh,actual_generation_mwh,gross_revenue_inr,dsm_charges_inr,
        dsm_charges_percent,net_revenue_inr,firstband_percent,gross_rev_actual_inr,
        gross_rev_scheduled_inr,final_charges_inr,net_rev_scheduled_inr,revenue_loss_inr,
        revenue_loss_percent,firstband,additional_data)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    for row_number, row in enumerate(reader, start=1):
        if not any((v or "").strip() for v in row.values() if v is not None):
            continue
        raw_data = {str(k).strip(): (v or "").strip() for k, v in row.items() if k is not None}
        json_data = Jsonb(raw_data) if con.backend == "postgres" else json.dumps(raw_data, ensure_ascii=False)
        values = (
            report_id, site, parse_report_date(csv_value(row, "Date")).year,
            parse_report_date(csv_value(row, "Date")).month, row_number,
            parse_report_date(csv_value(row, "Date")),
            parse_report_number(csv_value(row, "AC [MW]")),
            parse_report_number(csv_value(row, "DC [MW]")),
            csv_value(row, "Energy Type") or None,
            csv_value(row, "Type") or None,
            parse_report_number(csv_value(row, "Scheduled Generation [MWh]")),
            parse_report_number(csv_value(row, "Actual Generation [MWh]")),
            parse_report_number(csv_value(row, f"Gross Revenue [{RUPEE_SYMBOL}]")),
            parse_report_number(csv_value(row, f"DSM Charges [{RUPEE_SYMBOL}]")),
            parse_report_number(csv_value(row, "DSM Charges [%]")),
            parse_report_number(csv_value(row, f"Net Revenue [{RUPEE_SYMBOL}]")),
            parse_report_number(csv_value(row, "Firstband [%]")),
            parse_report_number(csv_value(row, "Gross REV - Actual [₹]")),
            parse_report_number(csv_value(row, "Gross REV - Sch. [₹]")),
            parse_report_number(csv_value(row, "Final Charges [₹]")),
            parse_report_number(csv_value(row, "Net REV - Schedule [₹]")),
            parse_report_number(csv_value(row, "Revenue Loss [₹]")),
            parse_report_number(csv_value(row, "Revenue Loss %")),
            csv_value(row, "Firstband") or None,
            json_data,
        )
        con.execute(insert_sql, values)
        site_month_rows.append((
            values[5], values[6], values[7], values[8], values[9],
            values[10], values[11], values[12], values[13], values[14],
            values[15], values[16],
        ))
        inserted += 1
    replace_site_report_table(con, site, period_type, year, month, start_date, end_date, site_month_rows)
    return inserted


def restore_report_file(row):
    """Restore a missing on-disk export from a verified database BLOB."""
    local_path, expected_hash, content = row
    path = Path(local_path)
    data = bytes(content)
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError(f"Stored database content hash mismatch for report: {path.name}")
    if path.exists():
        disk_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if disk_hash == expected_hash:
            return "skipped"
        raise RuntimeError(f"Existing file does not match its database record; left unchanged: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"Restored file verification failed: {path}")
    return "restored"


async def inspect_failure(page, label):
    """Keep useful diagnostics without exposing form values/credentials."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    Path("artifacts").mkdir(exist_ok=True)
    await page.screenshot(path=f"artifacts/{stamp}_{label}.png", full_page=True)
    # Strip input values from the captured markup before writing it.
    html = await page.content()
    html = re.sub(r'(?i)(value|placeholder)=("|\').*?\2', r'\1="[redacted]"', html)
    Path(f"artifacts/{stamp}_{label}.html").write_text(html, encoding="utf-8")


async def exact_visible(locator, description, timeout=10000):
    count = await locator.count()
    if count != 1:
        raise RuntimeError(f"Expected one {description}; found {count}")
    await locator.wait_for(state="visible", timeout=timeout)
    return locator


async def login_and_navigate(page):
    user, password = os.getenv("REPRO_USERNAME"), os.getenv("REPRO_PASSWORD")
    if not user or not password:
        raise RuntimeError("Set REPRO_USERNAME and REPRO_PASSWORD in repro_automation/.env")
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    # The portal can serve an intermediate page and redirect to /c_login only
    # after DOMContentLoaded. Wait for the actual login/report state before
    # counting fields, otherwise the code can miss the form and misreport the
    # login page's pre-existing "Unauthorised Access!" banner as a POST result.
    await page.wait_for_function(
        """() => {
            const loginForm = document.querySelector('form[action*="Verifylogin/check"]');
            const password = document.querySelector('input[type="password"]');
            const body = document.body?.innerText || '';
            return (loginForm && password) || body.includes('Day-wise DSM & Static Info');
        }""",
        timeout=30000,
    )
    pw = page.locator('input[type="password"]')
    login_status = None
    login_action = None
    if await pw.count():
        # Use the login form and field names observed in the sanitized portal
        # HTML: form[action*="Verifylogin/check"], username, password, btn_login.
        form = page.locator('form[action*="Verifylogin/check"]')
        if await form.count() != 1:
            await inspect_failure(page, "login_form_not_found")
            raise RuntimeError(
                f"Expected exactly one portal login form targeting Verifylogin/check; found {await form.count()}. "
                "See sanitized artifacts/login_form_not_found.html."
            )
        u = form.locator('input[name="username"]')
        username_candidates = u
        if await username_candidates.count() == 1:
            u = username_candidates
        else:
            await inspect_failure(page, "login_username_field_not_found")
            raise RuntimeError("Could not find exactly one input[name=username] in the portal login form")
        pw = form.locator('input[name="password"][type="password"]')
        await exact_visible(u, "username field")
        await exact_visible(pw, "password field")
        await u.fill(user)
        await pw.fill(password)
        # Compare locally in memory only. Never print or log either value.
        if await u.input_value() != user or await pw.input_value() != password:
            raise RuntimeError("The login fields did not retain the values loaded from the project .env file")
        login_action = await form.get_attribute("action")
        method = (await form.get_attribute("method") or "get").upper()
        submit = form.locator('#btn_login[name="btn_login"][type="submit"]')
        await exact_visible(submit, "portal login submit control")
        # Observe the actual request URL/status, independent of redirect URL or
        # same-document navigation behavior.
        login_path = re.compile(r"/Verifylogin/check(?:[/?#]|$)", re.I)
        async with page.expect_response(lambda response: login_path.search(response.url) is not None,
                                        timeout=30000) as login_response:
            await submit.click()
        response = await login_response.value
        login_status = response.status
        LOG.info("Login form submitted method=%s action=%s response_status=%s response_url=%s current_url=%s",
                 method, login_action, login_status, response.url, page.url)

    body_text = (await page.locator("body").inner_text()).lower()
    if "unauthorised access" in body_text or "unauthorized access" in body_text:
        await inspect_failure(page, "login_unauthorised")
        raise RuntimeError(
            "Portal returned its login page with 'Unauthorised Access!' after the login POST. "
            f"Form response status={login_status}; form action={login_action!r}; current URL={page.url!r}. "
            "Confirm the working credentials are in the project .env (the script does not read .env.example). "
            "See artifacts/*login_unauthorised.html for sanitized form markup."
        )
    # Manual CAPTCHA/MFA is allowed; do not continue as though login succeeded.
    if await page.locator('input[type="password"]').count():
        await inspect_failure(page, "login_still_pending")
        raise RuntimeError("Login is still pending. Complete any CAPTCHA/MFA manually, then rerun.")
    # The authenticated landing page's actual RE.Pro card is #pro. Its click
    # handler uses window.open(...redirectProd/REPro, '_blank'); the visible
    # product heading is commented out in the portal HTML, so text lookup fails.
    app = page.locator("#pro")
    if await app.count() == 1:
        async with page.expect_popup(timeout=15000) as popup_info:
            await app.click()
        app_page = await popup_info.value
        await app_page.wait_for_url(re.compile(r"^https?://", re.I), timeout=30000)
        await app_page.wait_for_load_state("domcontentloaded", timeout=30000)
        page = app_page
    # The real sidebar uses a collapsed Performance menu and an anchor with
    # an exact Historical Insights label (verified in authenticated HTML).
    # Locate the menu by the captured Historical Insights URL within the
    # sidebar; its parent menu anchor is the immediately preceding sibling li.
    historical = page.locator('a.label-nav[href*="C_gridconnect_ipp_performance/forecast_vs_actual"]')
    try:
        await historical.wait_for(state="attached", timeout=15000)
    except Exception as exc:
        await inspect_failure(page, "historical_insights_link_not_found")
        raise RuntimeError("Historical Insights link was not present in the authenticated sidebar") from exc
    performance_label = historical.locator("xpath=../../../a//span[contains(@class,'nav-label')]")
    try:
        await performance_label.wait_for(state="attached", timeout=10000)
    except Exception as exc:
        await inspect_failure(page, "performance_menu_not_found")
        raise RuntimeError("Could not find Performance menu containing Historical Insights") from exc
    await performance_label.click()
    await historical.wait_for(state="visible", timeout=10000)
    await historical.click()
    await page.wait_for_url("**/C_gridconnect_ipp_performance/forecast_vs_actual", timeout=30000)
    heading = page.get_by_text("Day-wise DSM & Static Info", exact=True)
    try:
        await heading.wait_for(state="visible", timeout=20000)
    except Exception as exc:
        await inspect_failure(page, "post_login_navigation")
        current = page.url
        body_text = (await page.locator("body").inner_text())[:1000]
        raise RuntimeError(
            f"Login completed or left the login page, but Historical Insights was not reached. "
            f"Current URL: {current}. Visible page text excerpt: {body_text!r}. "
            "See sanitized artifacts/post_login_navigation snapshot."
        ) from exc
    return page


async def choose(page, label, value):
    """Select by associated label, supporting native and common searchable selects."""
    control_ids = {"Site": "#uss_dss_id", "Period": "#select-calendar", "Source": "#sch_mode"}
    field = page.locator(control_ids[label])
    await field.wait_for(state="attached", timeout=12000)
    tag = await field.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        options = await field.locator("option").all()
        matches = [o for o in options if (await o.inner_text()).strip().casefold() == value.casefold()]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one {label} option matching {value!r}; found {len(matches)}")
        await field.select_option(value=await matches[0].get_attribute("value"))
        return
    await field.click()
    # Searchable widgets often expose a textbox after opening.
    search = page.get_by_role("textbox").filter(visible=True)
    if await search.count() == 1:
        try:
            await search.fill(value, timeout=1000)
        except Exception:
            pass
    option = page.get_by_role("option", name=value, exact=True)
    if await option.count() == 1:
        await option.click()
        return
    text = page.get_by_text(value, exact=True)
    if await text.count() == 1:
        await text.click()
        return
    raise RuntimeError(f"Could not uniquely select {value!r} in {label!r}")


async def choose_month(page, month, year):
    field = page.locator("#onlymonth")
    await field.wait_for(state="attached", timeout=12000)
    # The observed portal uses Flatpickr with dateFormat "F Y". Its public
    # setDate API fires the same onChange hook as selecting a month in the UI.
    await field.evaluate(
        "(el, args) => { if (!el._flatpickr) throw new Error('Flatpickr is not initialized'); "
        "el._flatpickr.setDate(new Date(args.year, args.month, 1), true); }",
        {"year": year, "month": MONTHS.index(month)},
    )
    expected = f"{month} {year}"
    await page.wait_for_function(
        "([selector, expected]) => document.querySelector(selector)?.value === expected",
        arg=["#onlymonth", expected], timeout=10000,
    )
    # The observed portal refreshes available source modes through this page
    # function. Flatpickr's month-change handler usually calls it, but setDate
    # does not consistently fire that handler when the selected day is unchanged.
    async with page.expect_response(lambda response: "actual_data_mode" in response.url, timeout=30000):
        await page.evaluate("() => { if (typeof window.sch_mode !== 'function') throw new Error('sch_mode unavailable'); window.sch_mode(); }")


async def choose_custom_dates(page, start_date, end_date):
    """Set the portal's observed Flatpickr FROM/TO controls and refresh source modes."""
    start_field, end_field = page.locator("#datepicker1"), page.locator("#datepicker2")
    await start_field.wait_for(state="visible", timeout=12000)
    await end_field.wait_for(state="visible", timeout=12000)

    async def set_date(locator, selected):
        await locator.evaluate(
            """(el, parts) => {
                if (!el._flatpickr) throw new Error('Custom date Flatpickr is not initialized');
                el._flatpickr.setDate(new Date(parts[0], parts[1] - 1, parts[2]), true);
            }""",
            [selected.year, selected.month, selected.day],
        )

    await set_date(start_field, start_date)
    async with page.expect_response(lambda response: "actual_data_mode" in response.url, timeout=30000):
        await set_date(end_field, end_date)
    expected_start = start_date.strftime("%d/%m/%Y")
    expected_end = end_date.strftime("%d/%m/%Y")
    if (await start_field.input_value()) != expected_start or (await end_field.input_value()) != expected_end:
        raise RuntimeError("Portal custom-date fields did not retain the requested FROM/TO dates")


def validate_csv_layout(data, site):
    """Reject exports whose columns would not match the required spreadsheet schema."""
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        text = bytes(data).decode("cp1252")
    reader = csv.DictReader(io.StringIO(text))
    actual = [normalize_header(header) for header in (reader.fieldnames or [])]
    expected = [normalize_header(header) for header in REPORT_HEADERS]
    missing = [REPORT_HEADERS[i] for i, header in enumerate(expected) if header not in actual]
    if actual != expected:
        extras = [header for header in (reader.fieldnames or []) if normalize_header(header) not in expected]
        raise RuntimeError(f"CSV columns/order do not exactly match the sheet schema for {site}; missing={missing}, extra={extras}")
    return list(reader)


async def report_table(page):
    heading = page.get_by_text("Day-wise DSM & Static Info", exact=True)
    await heading.wait_for(state="visible", timeout=30000)
    wrapper = page.locator("#table_metrices_wrapper")
    table = wrapper.locator("table#table_metrices")
    if await table.count() != 1:
        raise RuntimeError("Could not uniquely identify Day-wise DSM & Static Info table #table_metrices")
    await table.wait_for(state="visible")
    # Wait for loader to disappear when the page exposes one.
    try:
        await wrapper.locator(".dataTables_processing, .loading, [aria-busy='true']").wait_for(state="hidden", timeout=30000)
    except Exception:
        pass
    rows = table.locator("tbody tr")
    await rows.first.wait_for(state="visible", timeout=30000)
    return table


async def download_table_csv(page, site, file_key, date_check):
    table = await report_table(page)
    await table.scroll_into_view_if_needed()
    exports = page.locator('#table_metrices_wrapper a.buttons-csv[aria-controls="table_metrices"]')
    if await exports.count() != 1:
        raise RuntimeError(f"Expected one CSV export control for {site}; found {await exports.count()}")
    async with page.expect_download(timeout=30000) as info:
        await exports.click()
    download = await info.value
    original_filename = download.suggested_filename
    if Path(original_filename).suffix.lower() != ".csv":
        raise RuntimeError(f"Portal export is not CSV: {original_filename}")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", site).strip("_")
    path = DOWNLOAD_DIR / safe / f"{safe}_{file_key}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    await download.save_as(path)
    data = path.read_bytes()
    try:
        if not data:
            raise ValueError("Downloaded report is empty")
        parsed_rows = validate_csv_layout(data, site)
        dates = [parse_report_date(csv_value(row, "Date")) for row in parsed_rows
                 if csv_value(row, "Date")]
        if not dates:
            raise ValueError("CSV contains no dated data rows")
        if any(not date_check(value) for value in dates):
            raise ValueError("CSV data dates fall outside the requested report period")
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded CSV failed layout/date validation: {exc}") from exc
    return original_filename, path, data


def save_report(con, site, year, month, period_type, start_date, end_date,
                original_filename, path, data):
    digest = hashlib.sha256(data).hexdigest()
    if period_type == "monthly":
        con.execute("""INSERT INTO excel_reports
            (site,year,month,period_type,original_filename,local_path,sha256,content,downloaded_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (site, year, month, "monthly", original_filename, str(path.resolve()), digest,
             data, datetime.now(timezone.utc).isoformat()))
        report = con.execute("SELECT id FROM excel_reports WHERE site=? AND year=? AND month=? AND period_type='monthly'",
                             (site, year, month)).fetchone()
    else:
        if DB_BACKEND != "postgres":
            raise RuntimeError("Custom date reports require PostgreSQL; switch REPRO_DB_BACKEND to postgres")
        con.execute("""INSERT INTO excel_reports
            (site,year,month,period_type,start_date,end_date,original_filename,local_path,sha256,content,downloaded_at)
            VALUES (?,?,NULL,'custom',?,?,?,?,?,?,?)""",
            (site, year, start_date, end_date, original_filename, str(path.resolve()), digest,
             data, datetime.now(timezone.utc).isoformat()))
        report = con.execute("SELECT id FROM excel_reports WHERE site=? AND start_date=? AND end_date=? AND period_type='custom'",
                             (site, start_date, end_date)).fetchone()
    report_id = report[0]
    report_month = month if period_type == "monthly" else start_date.month
    replace_report_rows(con, report_id, site, year, report_month, data,
                        period_type=period_type, start_date=start_date, end_date=end_date)
    con.commit()


async def select_site(page, site):
    await choose(page, "Site", site)


async def select_source(page, site):
    try:
        await page.wait_for_function(
            "() => [...document.querySelectorAll('#sch_mode option')].some(o => o.textContent.trim() === 'PSS_SCADA')",
            timeout=10000,
        )
    except Exception as exc:
        available = [item.strip() for item in await page.locator("#sch_mode option").all_inner_texts() if item.strip()]
        raise RuntimeError(f"PSS_SCADA is unavailable for {site}; portal source options: {available}") from exc
    await choose(page, "Source", "PSS_SCADA")
    selected_site = (await page.locator("#uss_dss_id option:checked").inner_text()).strip()
    selected_source = (await page.locator("#sch_mode option:checked").inner_text()).strip()
    if selected_site.casefold() != site.casefold() or selected_source != "PSS_SCADA":
        raise RuntimeError(f"Selection verification failed: site={selected_site!r}, source={selected_source!r}")


async def export_one(page, con, site, year, number, month):
    existing = con.execute("SELECT local_path,sha256,content FROM excel_reports WHERE site=? AND year=? AND month=? AND period_type='monthly'",
                           (site, year, number)).fetchone()
    if existing:
        status = restore_report_file(existing)
        return "skipped" if status == "skipped" else "restored"
    await select_site(page, site)
    await choose(page, "Period", "Monthly")
    await choose_month(page, month, year)
    await select_source(page, site)
    selected_month = (await page.locator("#onlymonth").input_value()).strip()
    if selected_month != f"{month} {year}":
        raise RuntimeError(f"Selected month mismatch: {selected_month!r}")
    submit = page.locator("#submitmonth")
    await exact_visible(submit, "monthly submit control")
    async with page.expect_response(lambda response: "forecast_vs_actual_update_copy" in response.url, timeout=30000):
        await submit.click()
    original_filename, path, data = await download_table_csv(
        page, site, f"{year}_{number:02d}", lambda value: value.year == year and value.month == number)
    print(f"DOWNLOADED {site} {month} {year}", flush=True)
    print(f"IMPORTING {site} {month} {year}", flush=True)
    save_report(con, site, year, number, "monthly", None, None, original_filename, path, data)
    print(f"IMPORTED {site} {month} {year}", flush=True)
    return "successful"


async def export_custom_one(page, con, site, start_date, end_date):
    existing = con.execute("""SELECT local_path,sha256,content FROM excel_reports
        WHERE site=? AND period_type='custom' AND start_date=? AND end_date=?""",
        (site, start_date, end_date)).fetchone()
    if existing:
        status = restore_report_file(existing)
        return "skipped" if status == "skipped" else "restored"
    await select_site(page, site)
    await choose(page, "Period", "Custom Dates")
    await choose_custom_dates(page, start_date, end_date)
    await select_source(page, site)
    selected_start = (await page.locator("#datepicker1").input_value()).strip()
    selected_end = (await page.locator("#datepicker2").input_value()).strip()
    if selected_start != start_date.strftime("%d/%m/%Y") or selected_end != end_date.strftime("%d/%m/%Y"):
        raise RuntimeError(f"Selected custom-date range mismatch: {selected_start!r} to {selected_end!r}")
    submit = page.locator("#submit")
    await exact_visible(submit, "custom-date submit control")
    async with page.expect_response(lambda response: "forecast_vs_actual_update_copy" in response.url, timeout=30000):
        await submit.click()
    key = f"custom_{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    original_filename, path, data = await download_table_csv(
        page, site, key, lambda value: start_date <= value <= end_date)
    print(f"DOWNLOADED {site} {start_date} through {end_date}", flush=True)
    print(f"IMPORTING {site} {start_date} through {end_date}", flush=True)
    save_report(con, site, start_date.year, None, "custom", start_date, end_date,
                original_filename, path, data)
    print(f"IMPORTED {site} {start_date} through {end_date}", flush=True)
    return "successful"


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", choices=SITES, help="One site")
    parser.add_argument("--sites", help="Comma-separated selected sites, for example zaheerabad,mandrup")
    parser.add_argument("--months", nargs="+", choices=MONTHS, help="One or more calendar months; default is April-November")
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR, help=f"Calendar year to download (default: {DEFAULT_YEAR})")
    parser.add_argument("--from-date", help="Custom range start date, YYYY-MM-DD")
    parser.add_argument("--to-date", help="Custom range end date, YYYY-MM-DD; use same date for one day")
    parser.add_argument("--all", action="store_true", help="Run all configured sites")
    parser.add_argument("--headless", action="store_true", help="Run Chromium without opening a browser window")
    parser.add_argument("--restore-files-only", action="store_true", help="Restore missing CSVs from the configured database without opening the portal")
    db_commands = parser.add_mutually_exclusive_group()
    db_commands.add_argument("--db-check", action="store_true", help="Check the configured database connection and record count")
    db_commands.add_argument("--db-list", action="store_true", help="List stored report metadata")
    db_commands.add_argument("--migrate-sqlite", action="store_true", help="Copy and verify reports from REPRO_SQLITE_PATH into PostgreSQL")
    db_commands.add_argument("--sync-report-rows", action="store_true", help="Import daily CSV rows into excel_report_rows")
    db_commands.add_argument("--delete-all-reports", action="store_true", help="Delete all report records (requires --confirm-delete)")
    db_commands.add_argument("--delete-site", action="store_true", help="Delete all report records for --site (requires --confirm-delete)")
    db_commands.add_argument("--delete-report", action="store_true", help="Delete one site/month or custom-date report (requires --confirm-delete)")
    parser.add_argument("--confirm-delete", action="store_true", help="Confirm a requested database deletion")
    args = parser.parse_args()
    if args.site and args.sites:
        raise SystemExit("Use either --site or --sites, not both")
    if args.all and (args.site or args.sites):
        raise SystemExit("Use --all or select sites with --site/--sites, not both")
    selected_sites = [args.site] if args.site else (
        [site.strip() for site in args.sites.split(",") if site.strip()] if args.sites else [])
    invalid_sites = [site for site in selected_sites if site not in SITES]
    if invalid_sites:
        raise SystemExit(f"Unknown site name(s): {invalid_sites}. Use the exact configured names.")
    if len(set(selected_sites)) != len(selected_sites):
        raise SystemExit("The selected site list contains duplicates")
    custom_requested = bool(args.from_date or args.to_date)
    if bool(args.from_date) != bool(args.to_date):
        raise SystemExit("Custom-date mode requires both --from-date and --to-date")
    if custom_requested and args.months:
        raise SystemExit("Choose either monthly mode (--months) or custom-date mode (--from-date/--to-date)")
    if custom_requested:
        try:
            custom_start = parse_custom_date(args.from_date, "--from-date")
            custom_end = parse_custom_date(args.to_date, "--to-date")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if custom_start > custom_end:
            raise SystemExit("--from-date must be on or before --to-date")
        if custom_end > date.today():
            raise SystemExit("The portal custom-date calendar does not allow future dates")
        if DB_BACKEND != "postgres":
            raise SystemExit("Custom-date reports require PostgreSQL; set REPRO_DB_BACKEND=postgres in .env")
    delete_action = args.delete_all_reports or args.delete_site or args.delete_report
    if args.confirm_delete and not delete_action:
        raise SystemExit("--confirm-delete requires a delete command")
    if delete_action and not args.confirm_delete:
        raise SystemExit("Deletion is protected. Add --confirm-delete after selecting the deletion scope.")
    if args.delete_site and not args.site:
        raise SystemExit("--delete-site requires an explicit --site")
    if args.delete_site and (args.months or custom_requested):
        raise SystemExit("--delete-site removes all monthly and custom reports for that site; omit period filters")
    if args.delete_report and (not args.site or (custom_requested == bool(args.months)) or (args.months and len(args.months) != 1)):
        raise SystemExit("--delete-report requires --site and either one --months value or both custom dates")
    if args.delete_all_reports and (args.site or args.sites or args.months or custom_requested):
        raise SystemExit("Do not combine --delete-all-reports with --site or --months")
    if args.restore_files_only and (args.all or args.site or args.sites or custom_requested or args.db_check or args.db_list or args.migrate_sqlite or args.sync_report_rows or delete_action):
        raise SystemExit("Use --restore-files-only by itself")
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ERROR_LOG, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.db_check or args.db_list or args.migrate_sqlite or args.sync_report_rows or delete_action:
        con = database()
        try:
            if args.db_check:
                version = con.execute("SELECT version()").fetchone()[0]
                count = con.execute("SELECT COUNT(*) FROM excel_reports").fetchone()[0]
                row_count = con.execute("SELECT COUNT(*) FROM excel_report_rows").fetchone()[0]
                monthly_count = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND left(table_name,7)='report_'").fetchone()[0] if con.backend == "postgres" else con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND substr(name,1,7)='report_'").fetchone()[0]
                print("PostgreSQL connection: OK")
                print(f"Database: {os.getenv('REPRO_PG_DATABASE', 'repro_reports')}")
                print(f"Reports in excel_reports: {count}")
                print(f"Daily rows in excel_report_rows: {row_count}")
                print(f"Per-report sheet tables: {monthly_count}")
                print(f"Server: {version.split(',')[0]}")
            elif args.db_list:
                rows = con.execute("SELECT site,period_type,year,month,start_date,end_date,original_filename,local_path,sha256,downloaded_at FROM excel_reports ORDER BY site,year,month,start_date").fetchall()
                for row in rows:
                    print(" | ".join(str(value) for value in row))
                print(f"Total records: {len(rows)}")
            elif args.migrate_sqlite:
                inserted, source_count = migrate_sqlite_to_postgres(con)
                target_count = con.execute("SELECT COUNT(*) FROM excel_reports").fetchone()[0]
                print(f"SQLite rows verified: {source_count}; newly inserted: {inserted}; PostgreSQL total: {target_count}")
            elif args.sync_report_rows:
                conditions, params = [], []
                if args.site:
                    conditions.append("site=?")
                    params.append(args.site)
                elif selected_sites:
                    conditions.append("site IN (" + ",".join("?" for _ in selected_sites) + ")")
                    params.extend(selected_sites)
                if args.months:
                    conditions.append("period_type='monthly'")
                    conditions.append("month IN (" + ",".join("?" for _ in args.months) + ")")
                    params.extend(MONTHS.index(m) + 1 for m in args.months)
                if custom_requested:
                    conditions.extend(["period_type='custom'", "start_date=?", "end_date=?"])
                    params.extend([custom_start, custom_end])
                conditions.append("year=?")
                params.append(custom_start.year if custom_requested else args.year)
                where = " WHERE " + " AND ".join(conditions) if conditions else ""
                reports = con.execute(f"SELECT id,site,year,month,period_type,start_date,end_date,content FROM excel_reports{where} ORDER BY site,year,month,start_date", params).fetchall()
                total_rows = 0
                for report_id, site, year, month, period_type, start_date, end_date, content in reports:
                    if isinstance(start_date, str):
                        start_date = date.fromisoformat(start_date)
                    if isinstance(end_date, str):
                        end_date = date.fromisoformat(end_date)
                    report_month = month if month is not None else start_date.month
                    total_rows += replace_report_rows(con, report_id, site, year, report_month,
                                                      bytes(content), period_type, start_date, end_date)
                con.commit()
                print(f"Reports indexed: {len(reports)}; daily rows imported: {total_rows}")
            else:
                if args.delete_all_reports:
                    scope_sql, scope_params, scope_label = "", [], "all sites/months"
                elif args.delete_site:
                    scope_sql, scope_params, scope_label = " WHERE site=?", [args.site], f"site {args.site}"
                elif custom_requested:
                    scope_sql = " WHERE site=? AND period_type='custom' AND start_date=? AND end_date=?"
                    scope_params = [args.site, custom_start, custom_end]
                    scope_label = f"{args.site} {custom_start} through {custom_end}"
                else:
                    month_number = MONTHS.index(args.months[0]) + 1
                    scope_sql, scope_params, scope_label = " WHERE site=? AND year=? AND month=? AND period_type='monthly'", [args.site, args.year, month_number], f"{args.site} {args.months[0]} {args.year}"
                report_total = con.execute("SELECT COUNT(*) FROM excel_reports" + scope_sql, scope_params).fetchone()[0]
                reports_to_drop = con.execute("SELECT id,site,year,month,period_type,start_date,end_date FROM excel_reports" + scope_sql, scope_params).fetchall()
                row_total = 0
                for report_id, report_site, report_year, report_month, period_type, start_date, end_date in reports_to_drop:
                    row_total += con.execute("SELECT COUNT(*) FROM excel_report_rows WHERE report_id=?", (report_id,)).fetchone()[0]
                    if isinstance(start_date, str):
                        start_date = date.fromisoformat(start_date)
                    if isinstance(end_date, str):
                        end_date = date.fromisoformat(end_date)
                    table_name = report_table_name(report_site, period_type, report_year, report_month, start_date, end_date)
                    con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
                con.execute("DELETE FROM excel_reports" + scope_sql, scope_params)
                con.commit()
                print(f"Deleted {report_total} report records and {row_total} daily rows for {scope_label}.")
                print("CSV files on disk were left unchanged.")
        finally:
            con.close()
        return
    if args.restore_files_only:
        con = database()
        totals = {"restored": 0, "already_present": 0, "failed": 0}
        for site, year, month, period_type, start_date, end_date, local_path, digest, content in con.execute(
            "SELECT site,year,month,period_type,start_date,end_date,local_path,sha256,content FROM excel_reports ORDER BY site,year,month,start_date"
        ).fetchall():
            try:
                result = restore_report_file((local_path, digest, content))
                if result == "restored":
                    totals["restored"] += 1
                    period_label = (f"{MONTHS[month - 1]} {year}" if period_type == "monthly"
                                    else f"{start_date} through {end_date}")
                    print(f"RESTORED {site} {period_label}: {local_path}")
                else:
                    totals["already_present"] += 1
            except Exception as exc:
                totals["failed"] += 1
                LOG.exception("Could not restore stored report site=%s month=%s", site, month)
                print(f"FAILED restore {site} {year}-{month:02d}: {exc}")
        con.close()
        print("Restore summary: " + ", ".join(f"{k}={v}" for k, v in totals.items()))
        return
    sites = SITES if args.all else (selected_sites or ["zaheerabad"])
    months = args.months or (DEFAULT_MONTHS if args.all else ["April"])
    con = database()
    counts = {"successful": 0, "restored": 0, "skipped": 0, "failed": 0}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=(HEADLESS or args.headless))
        # The portal certificate is rejected by this machine's Chromium trust
        # store (ERR_CERT_AUTHORITY_INVALID). Ignore certificate validation
        # only for this Playwright context so HTTPS can load; no TLS settings
        # on the machine are changed. Never print credentials or disable auth.
        context = await browser.new_context(accept_downloads=True, ignore_https_errors=True)
        page = await context.new_page()
        try:
            page = await login_and_navigate(page)
            for site in sites:
                if custom_requested:
                    print(f"STARTING {site} {custom_start} through {custom_end}", flush=True)
                    try:
                        outcome = await export_custom_one(page, con, site, custom_start, custom_end)
                        counts[outcome] += 1
                        print(f"{outcome.upper()} {site} {custom_start} through {custom_end}")
                    except Exception as exc:
                        counts["failed"] += 1
                        con.rollback()
                        LOG.exception("Failed custom-date report site=%s from=%s to=%s", site, custom_start, custom_end)
                        print(f"FAILED {site} {custom_start} through {custom_end}: {type(exc).__name__}: {exc}")
                        await inspect_failure(page, f"{site}_custom_{custom_start}_{custom_end}_error")
                else:
                    for month in months:
                        number = MONTHS.index(month) + 1
                        print(f"STARTING {site} {month} {args.year}", flush=True)
                        try:
                            outcome = await export_one(page, con, site, args.year, number, month)
                            counts[outcome] += 1
                            print(f"{outcome.upper()} {site} {month} {args.year}")
                        except Exception as exc:
                            counts["failed"] += 1
                            con.rollback()
                            LOG.exception("Failed report site=%s month=%s", site, month)
                            print(f"FAILED {site} {month} {args.year}: {type(exc).__name__}: {exc}")
                            await inspect_failure(page, f"{site}_{month}_error")
            print("Summary: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        finally:
            await browser.close()
            con.close()


if __name__ == "__main__":
    asyncio.run(main())

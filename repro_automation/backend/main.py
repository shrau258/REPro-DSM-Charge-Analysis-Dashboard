from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
import zipfile
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.background import BackgroundTask
from sqlalchemy import text

from backend.database import PROJECT_ROOT, engine
from backend.calculations import revenue_metrics
from backend.schemas import AutomationRequest, DeleteRequest, DeleteSelection, MasterExcelSettingsRequest
from backend.master_excel import latest_master_workbook, load_customer_site_rows, load_master_customer_rows
from repro_download import REPORT_HEADERS, SITES, MONTHS, report_table_name

app = FastAPI(title="RE.Pro DSM Charge Dashboard API", version="1.0.0")
LOG = logging.getLogger("repro.dashboard")
AUTH_REQUIRED = os.getenv("REPRO_AUTH_REQUIRED", "false").strip().casefold() == "true"
SERVE_FRONTEND = os.getenv("REPRO_SERVE_FRONTEND", "false").strip().casefold() == "true"
FRONTEND_DIST = Path(os.getenv("REPRO_FRONTEND_DIST", str(Path(__file__).resolve().parents[1] / "frontend" / "dist"))).resolve()
try:
    LAN_ALLOWED_NETWORKS = tuple(
        ipaddress.ip_network(value.strip(), strict=False)
        for value in os.getenv("REPRO_LAN_ALLOWED_CIDRS", "").split(",")
        if value.strip()
    )
except ValueError as exc:
    raise RuntimeError("REPRO_LAN_ALLOWED_CIDRS must contain valid IP networks") from exc
AUTH_USER_HEADER = os.getenv("REPRO_AUTH_USER_HEADER", "X-Authenticated-User").strip()
AUTH_ROLES_HEADER = os.getenv("REPRO_AUTH_ROLES_HEADER", "X-Authenticated-Roles").strip()
ADMIN_ROLE = os.getenv("REPRO_ADMIN_ROLE", "DSM-Admin").strip().casefold()
AUTOMATION_ROLE = os.getenv("REPRO_AUTOMATION_ROLE", "DSM-Users").strip().casefold()
if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", AUTH_USER_HEADER):
    raise RuntimeError("REPRO_AUTH_USER_HEADER must be a valid HTTP header name")
if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", AUTH_ROLES_HEADER):
    raise RuntimeError("REPRO_AUTH_ROLES_HEADER must be a valid HTTP header name")
if not ADMIN_ROLE or not AUTOMATION_ROLE:
    raise RuntimeError("REPRO_ADMIN_ROLE and REPRO_AUTOMATION_ROLE must not be empty")
trusted_proxy_setting = os.getenv(
    "REPRO_TRUSTED_PROXY_IPS", "127.0.0.1,::1" if not AUTH_REQUIRED else ""
)
TRUSTED_PROXY_IPS = {
    ipaddress.ip_address(value.strip())
    for value in trusted_proxy_setting.split(",")
    if value.strip()
}
if AUTH_REQUIRED and not TRUSTED_PROXY_IPS:
    raise RuntimeError("Set REPRO_TRUSTED_PROXY_IPS to the actual trusted proxy peer IP before enabling authentication")
cors_origins = [value.strip() for value in os.getenv(
    "REPRO_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",") if value.strip()]
if AUTH_REQUIRED and not cors_origins:
    raise RuntimeError("Set REPRO_CORS_ORIGINS to the exact authenticated dashboard origin before enabling authentication")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def trusted_network_identity(request: Request, call_next):
    """Apply optional LAN allowlisting and trusted-proxy authentication."""
    # Browser CORS preflights carry no authenticated user identity. Let CORS
    # handle the preflight; all actual API requests still pass this gate.
    if request.method == "OPTIONS":
        return await call_next(request)
    if LAN_ALLOWED_NETWORKS:
        client_host = request.client.host if request.client else ""
        try:
            client_ip = ipaddress.ip_address(client_host)
        except ValueError:
            client_ip = None
        if not client_ip or (not client_ip.is_loopback and not any(client_ip in network for network in LAN_ALLOWED_NETWORKS)):
            return JSONResponse(status_code=403, content={"detail": "This dashboard is available only from the configured company LAN."})
    if not AUTH_REQUIRED:
        request.state.principal = "local"
        request.state.roles = {"local"}
        return await call_next(request)
    if request.method == "POST" and request.headers.get("origin") not in cors_origins:
        return JSONResponse(status_code=403, content={"detail": "The request origin is not allowed for this dashboard."})
    client_host = request.client.host if request.client else ""
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        client_ip = None
    # Local Windows health probes are safe because Uvicorn is bound to loopback.
    if request.url.path == "/api/health" and client_ip and client_ip.is_loopback:
        request.state.principal = "service-health-check"
        request.state.roles = set()
        return await call_next(request)
    if client_ip not in TRUSTED_PROXY_IPS:
        return JSONResponse(status_code=403, content={"detail": "Requests must pass through the trusted company proxy."})
    principal = request.headers.get(AUTH_USER_HEADER, "").strip()
    if not principal or len(principal) > 200 or any(ord(char) < 32 for char in principal):
        return JSONResponse(status_code=401, content={"detail": "Company authentication is required."})
    role_header = request.headers.get(AUTH_ROLES_HEADER, "")
    if len(role_header) > 2000 or any(ord(char) < 32 and char not in "\t" for char in role_header):
        return JSONResponse(status_code=400, content={"detail": "Invalid company role information."})
    # Keep SSO identities out of the database; a stable digest scopes jobs.
    request.state.principal = hashlib.sha256(principal.encode("utf-8")).hexdigest()
    request.state.roles = {role.strip().casefold() for role in re.split(r"[,;]", role_header) if role.strip()}
    return await call_next(request)


def require_admin_role(request: Request) -> None:
    if AUTH_REQUIRED and ADMIN_ROLE not in getattr(request.state, "roles", set()):
        raise HTTPException(403, "This action requires the configured dashboard administrator role.")


def require_automation_role(request: Request) -> None:
    roles = getattr(request.state, "roles", set())
    if AUTH_REQUIRED and not {ADMIN_ROLE, AUTOMATION_ROLE}.intersection(roles):
        raise HTTPException(403, "Starting downloads requires the configured dashboard user role.")


@app.exception_handler(StarletteHTTPException)
async def helpful_http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if exc.status_code == 404 and detail == "Not Found":
        detail = (f"No API route is registered for {request.method} {request.url.path} on this server. "
                  "Restart the backend from the repro_automation folder with "
                  "python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000.")
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)


@app.get("/")
def api_root():
    index = FRONTEND_DIST / "index.html"
    if SERVE_FRONTEND and index.is_file():
        return FileResponse(index, media_type="text/html")
    return {"service": "RE.Pro DSM Dashboard API", "docs": "/docs", "health": "/api/health"}

ANALYTIC_ROWS = """(
    SELECT DISTINCT ON (r.site, r.report_date, r.energy_type, r.report_type) r.*
    FROM excel_report_rows r
    JOIN excel_reports e ON e.id = r.report_id
    ORDER BY r.site, r.report_date, r.energy_type, r.report_type,
             CASE WHEN e.period_type = 'monthly' THEN 0 ELSE 1 END,
             e.downloaded_at DESC, r.row_number
) AS report_rows"""
jobs: dict[str, dict[str, Any]] = {}
active_combinations: dict[tuple[str, ...], str] = {}
job_lock = asyncio.Lock()
automation_execution_lock = asyncio.Lock()


def automation_keys(request: AutomationRequest) -> list[tuple[str, ...]]:
    keys = []
    if request.period_type in {"monthly", "both"}:
        keys.extend((site, "monthly", str(request.year), str(month))
                    for site in request.sites for month in request.months)
    if request.period_type in {"custom", "both"}:
        keys.extend((site, "custom", request.start_date.isoformat(), request.end_date.isoformat())
                    for site in request.sites)
    return keys


def json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def filtered_query(sites: list[str], start: date | None, end: date | None) -> tuple[str, dict[str, Any]]:
    parts, params = [], {}
    if sites:
        parts.append("site = ANY(:sites)")
        params["sites"] = sites
    if start:
        parts.append("report_date >= :start_date")
        params["start_date"] = start
    if end:
        parts.append("report_date <= :end_date")
        params["end_date"] = end
    return (" WHERE " + " AND ".join(parts) if parts else ""), params


def parse_filters(sites: list[str], start_date: date | None, end_date: date | None):
    invalid = sorted(set(sites) - set(SITES))
    if invalid:
        raise HTTPException(422, f"Unknown site(s): {', '.join(invalid)}")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(422, "start_date must be on or before end_date")
    return filtered_query(sites, start_date, end_date)


def execute(sql: str, params: dict[str, Any] | None = None):
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).mappings().all()


@app.get("/api/health")
def health():
    try:
        row = execute("SELECT current_database() AS database, version() AS version")[0]
        return {"status": "ok", "database": row["database"], "postgres": row["version"].split(",")[0]}
    except Exception as exc:
        raise HTTPException(503, "PostgreSQL is unavailable; check the local .env connection settings") from exc


@app.get("/api/sites")
def get_sites():
    rows = execute(f"SELECT site, COUNT(*) AS row_count, MIN(report_date) AS first_date, MAX(report_date) AS last_date "
                   f"FROM {ANALYTIC_ROWS} GROUP BY site")
    actual = {r["site"]: r for r in rows}
    return [{"site": site, "row_count": actual[site]["row_count"] if site in actual else 0,
             "first_date": json_safe(actual[site]["first_date"]) if site in actual else None,
             "last_date": json_safe(actual[site]["last_date"]) if site in actual else None}
            for site in SITES]


def ensure_master_excel_tables() -> None:
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE IF NOT EXISTS dashboard_master_excel_config (
            id SMALLINT PRIMARY KEY CHECK (id=1), directory TEXT NOT NULL,
            source_path TEXT, source_mtime_ns BIGINT, source_size BIGINT,
            synced_at TIMESTAMPTZ, mapping_count INTEGER NOT NULL DEFAULT 0,
            unsupported_rows INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            uploaded_filename TEXT, uploaded_at TIMESTAMPTZ, uploaded_status TEXT, uploaded_error TEXT,
            uploaded_mapping_count INTEGER)"""))
        for name, sql_type in (("uploaded_filename", "TEXT"), ("uploaded_at", "TIMESTAMPTZ"),
                               ("uploaded_status", "TEXT"), ("uploaded_error", "TEXT"),
                               ("uploaded_mapping_count", "INTEGER"), ("uploaded_new_count", "INTEGER"),
                               ("uploaded_updated_count", "INTEGER"), ("uploaded_skipped_count", "INTEGER"),
                               ("uploaded_total_unique_count", "INTEGER")):
            connection.execute(text(f"ALTER TABLE dashboard_master_excel_config ADD COLUMN IF NOT EXISTS {name} {sql_type}"))
        connection.execute(text("""CREATE TABLE IF NOT EXISTS customer_site_map (
            customer_name TEXT NOT NULL, master_site TEXT NOT NULL, report_site TEXT NOT NULL,
            PRIMARY KEY (customer_name, master_site, report_site))"""))
        connection.execute(text("""CREATE TABLE IF NOT EXISTS master_customer_sites (
            id BIGSERIAL PRIMARY KEY, customer_key TEXT NOT NULL, site_key TEXT NOT NULL, location_key TEXT NOT NULL,
            customer_name TEXT NOT NULL, location_no TEXT NOT NULL, site_name TEXT NOT NULL,
            name_on_dsm_charges TEXT, group_name TEXT, site_total_capacity_mw_ac NUMERIC(20,6),
            customer_capacity_mw_ac NUMERIC(20,6), report_site TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_master_customer_site_location UNIQUE(customer_key, site_key, location_key))"""))
        connection.execute(text("ALTER TABLE master_customer_sites ADD COLUMN IF NOT EXISTS site_key TEXT"))
        connection.execute(text("UPDATE master_customer_sites SET site_key=lower(btrim(site_name)) WHERE site_key IS NULL OR site_key=''"))
        connection.execute(text("ALTER TABLE master_customer_sites ALTER COLUMN site_key SET NOT NULL"))
        connection.execute(text("ALTER TABLE master_customer_sites DROP CONSTRAINT IF EXISTS uq_master_customer_location"))
        connection.execute(text("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                WHERE conrelid='public.master_customer_sites'::regclass
                AND conname='uq_master_customer_site_location') THEN
                ALTER TABLE public.master_customer_sites
                    ADD CONSTRAINT uq_master_customer_site_location UNIQUE(customer_key,site_key,location_key);
            END IF;
        END $$"""))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_master_customer_sites_report_site ON master_customer_sites(report_site)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_master_customer_sites_customer_key ON master_customer_sites(customer_key)"))


def sync_master_excel_if_changed() -> dict[str, Any]:
    ensure_master_excel_tables()
    with engine.connect() as connection:
        config = connection.execute(text("SELECT * FROM dashboard_master_excel_config WHERE id=1")).mappings().first()
    directory = config["directory"] if config else os.getenv("REPRO_MASTER_EXCEL_DIR", "").strip()
    existing_master_rows = execute("SELECT COUNT(*) AS count FROM master_customer_sites")[0]["count"]
    result: dict[str, Any] = {
        "directory": directory or "", "file_name": Path(config["source_path"]).name if config and config["source_path"] else None,
        "synced_at": json_safe(config["synced_at"]) if config else None,
        "mapping_count": config["mapping_count"] if config else 0,
        "unsupported_rows": config["unsupported_rows"] if config else 0,
        "status": "not_configured", "error": config["last_error"] if config else None,
        "uploaded_filename": config["uploaded_filename"] if config else None,
        "uploaded_at": json_safe(config["uploaded_at"]) if config else None,
        "uploaded_status": config["uploaded_status"] if config else None,
        "uploaded_error": config["uploaded_error"] if config else None,
        "uploaded_mapping_count": config["uploaded_mapping_count"] if config else None,
        "uploaded_new_count": config["uploaded_new_count"] if config else None,
        "uploaded_updated_count": config["uploaded_updated_count"] if config else None,
        "uploaded_skipped_count": config["uploaded_skipped_count"] if config else None,
        "uploaded_total_unique_count": config["uploaded_total_unique_count"] if config else None,
    }
    if not directory:
        return result
    folder = Path(directory).expanduser()
    if not folder.is_dir():
        result.update(status="folder_missing", error="The configured Master Excel folder does not exist or cannot be accessed.")
        return result
    source = latest_master_workbook(folder)
    if source is None:
        result.update(status="file_missing", error=(
            "No valid .xlsx, .xls, or .xlsm Master workbook was found. Each workbook must contain "
            "Sheet1 with Customer Name, Location No., Site, Group, Site Total Capacity (MW-AC), "
            "and Capacity (MW-AC); Name on DSM charges is optional."))
        return result
    stat = source.stat()
    resolved = str(source.resolve())
    if (existing_master_rows and config and config["source_path"] == resolved and config["source_mtime_ns"] == stat.st_mtime_ns
            and config["source_size"] == stat.st_size and not config["last_error"]):
        result.update(status="current", file_name=source.name)
        return result
    try:
        mappings, unsupported, duplicate_rows = load_master_customer_rows(source)
        with engine.begin() as connection:
            sync_summary = persist_master_customer_rows(connection, mappings, duplicate_rows)
            connection.execute(text("""INSERT INTO dashboard_master_excel_config
                (id,directory,source_path,source_mtime_ns,source_size,synced_at,mapping_count,unsupported_rows,last_error)
                VALUES (1,:directory,:source_path,:mtime,:size,CURRENT_TIMESTAMP,:count,:unsupported,NULL)
                ON CONFLICT (id) DO UPDATE SET directory=EXCLUDED.directory,source_path=EXCLUDED.source_path,
                source_mtime_ns=EXCLUDED.source_mtime_ns,source_size=EXCLUDED.source_size,
                synced_at=EXCLUDED.synced_at,mapping_count=EXCLUDED.mapping_count,
                unsupported_rows=EXCLUDED.unsupported_rows,last_error=NULL"""), {
                "directory": str(folder.resolve()), "source_path": resolved, "mtime": stat.st_mtime_ns,
                "size": stat.st_size, "count": len(mappings), "unsupported": unsupported,
            })
        result.update(directory=str(folder.resolve()), file_name=source.name, synced_at=datetime.now().isoformat(),
                      mapping_count=len(mappings), unsupported_rows=unsupported, status="synced", error=None,
                      **sync_summary)
    except Exception as exc:
        message = str(exc)[:500]
        with engine.begin() as connection:
            if config:
                connection.execute(text("UPDATE dashboard_master_excel_config SET last_error=:error WHERE id=1"), {"error": message})
        result.update(status="sync_error", error=message)
    return result


def master_customer_site_data() -> dict[str, Any]:
    status = sync_master_excel_if_changed()
    ensure_master_excel_tables()
    rows = execute("SELECT customer_name,report_site FROM master_customer_sites WHERE report_site IS NOT NULL ORDER BY customer_name,report_site")
    if not rows:
        # Preserve legacy dropdown mappings until the new B:H Master workbook is uploaded/synced.
        rows = execute("SELECT customer_name,report_site FROM customer_site_map ORDER BY customer_name,report_site")
    customer_sites: dict[str, set[str]] = {}
    for row in rows:
        customer_sites.setdefault(row["customer_name"], set()).add(row["report_site"])
    mappings = [{"customer_name": customer, "sites": sorted(mapped_sites)}
                for customer, mapped_sites in sorted(customer_sites.items(), key=lambda item: item[0].casefold())]
    sites = sorted({row["report_site"] for row in rows}, key=lambda value: (SITES.index(value) if value in SITES else len(SITES), value))
    all_customers = execute("SELECT DISTINCT customer_name FROM master_customer_sites WHERE report_site IS NOT NULL ORDER BY customer_name")
    if not all_customers:
        all_customers = execute("SELECT DISTINCT customer_name FROM customer_site_map ORDER BY customer_name")
    return {"customers": [row["customer_name"] for row in all_customers], "mappings": mappings,
            "sites": sites, "status": status}


def persist_master_customer_rows(
    connection, records: list[dict[str, Any]], duplicate_rows_skipped: int = 0
) -> dict[str, int]:
    """Incrementally upsert normalized customer/site/location mappings."""
    connection.execute(text("""CREATE TEMP TABLE incoming_master_customer_sites (
        customer_key TEXT NOT NULL, site_key TEXT NOT NULL, location_key TEXT NOT NULL, customer_name TEXT NOT NULL,
        location_no TEXT NOT NULL, site_name TEXT NOT NULL, name_on_dsm_charges TEXT,
        group_name TEXT, site_total_capacity_mw_ac NUMERIC(20,6), customer_capacity_mw_ac NUMERIC(20,6),
        report_site TEXT) ON COMMIT DROP"""))
    stage_sql = text("""INSERT INTO incoming_master_customer_sites
        (customer_key,site_key,location_key,customer_name,location_no,site_name,name_on_dsm_charges,group_name,
         site_total_capacity_mw_ac,customer_capacity_mw_ac,report_site)
        VALUES (:customer_key,:site_key,:location_key,:customer_name,:location_no,:site_name,:name_on_dsm_charges,
         :group_name,:site_total_capacity_mw_ac,:customer_capacity_mw_ac,:report_site)""")
    connection.execute(stage_sql, records)
    writes = connection.execute(text("""INSERT INTO master_customer_sites
        (customer_key,site_key,location_key,customer_name,location_no,site_name,name_on_dsm_charges,group_name,
         site_total_capacity_mw_ac,customer_capacity_mw_ac,report_site,updated_at)
        SELECT customer_key,site_key,location_key,customer_name,location_no,site_name,name_on_dsm_charges,group_name,
         site_total_capacity_mw_ac,customer_capacity_mw_ac,report_site,CURRENT_TIMESTAMP
        FROM incoming_master_customer_sites
        ON CONFLICT (customer_key,site_key,location_key) DO UPDATE SET customer_name=EXCLUDED.customer_name,
         location_no=EXCLUDED.location_no,site_name=EXCLUDED.site_name,
         name_on_dsm_charges=EXCLUDED.name_on_dsm_charges,group_name=EXCLUDED.group_name,
         site_total_capacity_mw_ac=EXCLUDED.site_total_capacity_mw_ac,
         customer_capacity_mw_ac=EXCLUDED.customer_capacity_mw_ac,report_site=EXCLUDED.report_site,
         updated_at=CURRENT_TIMESTAMP
        WHERE (master_customer_sites.customer_name,master_customer_sites.location_no,
               master_customer_sites.site_name,master_customer_sites.name_on_dsm_charges,
               master_customer_sites.group_name,master_customer_sites.site_total_capacity_mw_ac,
               master_customer_sites.customer_capacity_mw_ac,master_customer_sites.report_site)
          IS DISTINCT FROM
              (EXCLUDED.customer_name,EXCLUDED.location_no,EXCLUDED.site_name,
               EXCLUDED.name_on_dsm_charges,EXCLUDED.group_name,EXCLUDED.site_total_capacity_mw_ac,
               EXCLUDED.customer_capacity_mw_ac,EXCLUDED.report_site)
        RETURNING (xmax=0) AS inserted""")).mappings().all()
    added = sum(1 for row in writes if row["inserted"])
    updated = len(writes) - added
    skipped_existing = len(records) - len(writes)
    mapped = {(row["customer_name"], row["site_name"], row["report_site"])
              for row in records if row["report_site"]}
    if mapped:
        connection.execute(text("""INSERT INTO customer_site_map(customer_name,master_site,report_site)
            VALUES (:customer_name,:master_site,:report_site) ON CONFLICT DO NOTHING"""), [
                {"customer_name": c, "master_site": site, "report_site": report}
                for c, site, report in sorted(mapped)
            ])
    total_unique = connection.execute(text("SELECT COUNT(*) FROM master_customer_sites")).scalar_one()
    return {
        "new_records_added": added,
        "existing_records_updated": updated,
        "duplicate_records_skipped": duplicate_rows_skipped + skipped_existing,
        "total_unique_records": total_unique,
    }


@app.get("/api/settings/master-excel", dependencies=[Depends(require_admin_role)])
def get_master_excel_settings():
    return sync_master_excel_if_changed()


@app.post("/api/settings/master-excel", dependencies=[Depends(require_admin_role)])
def save_master_excel_settings(request: MasterExcelSettingsRequest):
    directory = Path(request.directory.strip()).expanduser()
    if not directory.is_dir():
        raise HTTPException(422, "Choose an existing folder that contains the Master Excel workbook")
    ensure_master_excel_tables()
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO dashboard_master_excel_config(id,directory,last_error)
            VALUES (1,:directory,NULL) ON CONFLICT (id) DO UPDATE
            SET directory=EXCLUDED.directory,source_path=NULL,source_mtime_ns=NULL,source_size=NULL,
            synced_at=NULL,mapping_count=0,unsupported_rows=0,last_error=NULL"""),
            {"directory": str(directory.resolve())})
    return sync_master_excel_if_changed()


@app.post("/api/settings/master-excel/upload", dependencies=[Depends(require_admin_role)])
async def upload_master_excel(file: UploadFile = File(...)):
    """Upload, validate, and atomically replace the customer/site mapping."""
    original_name = Path(file.filename or "").name
    suffix = Path(original_name).suffix.casefold()
    if suffix not in {".xlsx", ".xls"}:
        raise HTTPException(415, "Choose an .xlsx or .xls Excel workbook")
    contents = await file.read(25 * 1024 * 1024 + 1)
    if not contents:
        raise HTTPException(422, "The selected Excel file is empty")
    if len(contents) > 25 * 1024 * 1024:
        raise HTTPException(413, "Excel upload exceeds the 25 MB limit")

    ensure_master_excel_tables()
    upload_dir = PROJECT_ROOT / "uploads" / "master_excel"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original_name).stem).strip("._") or "master"
    destination = upload_dir / f"{safe_stem}_{uuid.uuid4().hex[:10]}{suffix}"
    temp_path = upload_dir / f".{uuid.uuid4().hex}.upload{suffix}"
    try:
        temp_path.write_bytes(contents)
        mappings, unsupported, duplicate_rows = load_master_customer_rows(temp_path)
        temp_path.replace(destination)
        with engine.begin() as connection:
            sync_summary = persist_master_customer_rows(connection, mappings, duplicate_rows)
            connection.execute(text("""INSERT INTO dashboard_master_excel_config
                (id,directory,synced_at,mapping_count,unsupported_rows,last_error,uploaded_filename,
                 uploaded_at,uploaded_status,uploaded_error,uploaded_mapping_count,uploaded_new_count,
                 uploaded_updated_count,uploaded_skipped_count,uploaded_total_unique_count)
                VALUES (1,:directory,CURRENT_TIMESTAMP,:count,:unsupported,NULL,:filename,
                        CURRENT_TIMESTAMP,'success',NULL,:count,:new_count,:updated_count,:skipped_count,:total_unique)
                ON CONFLICT (id) DO UPDATE SET synced_at=EXCLUDED.synced_at,
                mapping_count=EXCLUDED.mapping_count,unsupported_rows=EXCLUDED.unsupported_rows,
                last_error=NULL,uploaded_filename=EXCLUDED.uploaded_filename,
                uploaded_at=EXCLUDED.uploaded_at,uploaded_status='success',uploaded_error=NULL,
                uploaded_mapping_count=EXCLUDED.uploaded_mapping_count,
                uploaded_new_count=EXCLUDED.uploaded_new_count,
                uploaded_updated_count=EXCLUDED.uploaded_updated_count,
                uploaded_skipped_count=EXCLUDED.uploaded_skipped_count,
                uploaded_total_unique_count=EXCLUDED.uploaded_total_unique_count"""), {
                "directory": os.getenv("REPRO_MASTER_EXCEL_DIR", "").strip(), "count": len(mappings),
                "unsupported": unsupported, "filename": original_name,
                "new_count": sync_summary["new_records_added"],
                "updated_count": sync_summary["existing_records_updated"],
                "skipped_count": sync_summary["duplicate_records_skipped"],
                "total_unique": sync_summary["total_unique_records"],
            })
        return {"status": "success", "uploaded_filename": original_name,
                "uploaded_at": datetime.now().isoformat(), "mapping_count": len(mappings),
                "unsupported_rows": unsupported, **sync_summary}
    except HTTPException:
        raise
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        message = str(exc)[:500] or "Could not read the selected Excel workbook"
        with engine.begin() as connection:
            connection.execute(text("""INSERT INTO dashboard_master_excel_config
                (id,directory,uploaded_filename,uploaded_at,uploaded_status,uploaded_error,
                 uploaded_mapping_count,uploaded_new_count,uploaded_updated_count,
                 uploaded_skipped_count,uploaded_total_unique_count)
                VALUES (1,:directory,:filename,CURRENT_TIMESTAMP,'error',:error,NULL,NULL,NULL,NULL,NULL)
                ON CONFLICT (id) DO UPDATE SET uploaded_filename=EXCLUDED.uploaded_filename,
                uploaded_at=EXCLUDED.uploaded_at,uploaded_status='error',uploaded_error=EXCLUDED.uploaded_error,
                uploaded_mapping_count=NULL,uploaded_new_count=NULL,uploaded_updated_count=NULL,
                uploaded_skipped_count=NULL,uploaded_total_unique_count=NULL"""), {
                "directory": os.getenv("REPRO_MASTER_EXCEL_DIR", "").strip(),
                "filename": original_name, "error": message,
            })
        raise HTTPException(422, f"Master Excel was not synchronized: {message}") from exc


@app.get("/api/master/customers")
def get_master_customers():
    return master_customer_site_data()


@app.get("/api/reports/coverage")
def report_coverage():
    rows = execute("SELECT id,site,year,month,period_type,start_date,end_date,original_filename,downloaded_at "
                   "FROM excel_reports ORDER BY downloaded_at DESC")
    return [{k: json_safe(v) for k, v in dict(row).items()} for row in rows]


def delete_filter(selection: DeleteSelection) -> tuple[str, dict[str, Any]]:
    if selection.scope == "all":
        return "", {}
    if selection.scope == "site":
        return " WHERE e.site=:site", {"site": selection.site}
    if selection.scope == "monthly":
        return (" WHERE e.site=:site AND e.period_type='monthly' AND e.year=:year AND e.month=:month",
                {"site": selection.site, "year": selection.year, "month": selection.month})
    return (" WHERE e.site=:site AND e.period_type='custom' AND e.start_date=:start_date AND e.end_date=:end_date",
            {"site": selection.site, "start_date": selection.start_date, "end_date": selection.end_date})


@app.post("/api/reports/delete-preview", dependencies=[Depends(require_admin_role)])
def delete_preview(selection: DeleteSelection):
    where, params = delete_filter(selection)
    rows = execute(f"""SELECT COUNT(DISTINCT e.id) AS report_count, COUNT(r.id) AS daily_row_count
        FROM excel_reports e LEFT JOIN excel_report_rows r ON r.report_id=e.id{where}""", params)
    return {"report_count": rows[0]["report_count"], "daily_row_count": rows[0]["daily_row_count"],
            "scope": selection.scope, "site": selection.site}


def scope_has_running_job(selection: DeleteSelection) -> bool:
    if selection.scope == "all":
        return bool(active_combinations)
    if selection.scope == "site":
        return any(key[0] == selection.site for key in active_combinations)
    if selection.scope == "monthly":
        return any(key[0] == selection.site for key in active_combinations)
    return any(key[0] == selection.site for key in active_combinations)


@app.post("/api/reports/delete", dependencies=[Depends(require_admin_role)])
async def delete_reports(request: DeleteRequest):
    async with job_lock:
        if scope_has_running_job(request):
            raise HTTPException(409, "Cannot delete these records while an automation job is active for the selected site")
        where, params = delete_filter(request)
        try:
            with engine.begin() as connection:
                records = connection.execute(text("""SELECT e.id,e.site,e.year,e.month,e.period_type,e.start_date,e.end_date
                    FROM excel_reports e""" + where), params).mappings().all()
                daily_row_count = 0
                for record in records:
                    report_id = record["id"]
                    daily_row_count += connection.execute(
                        text("SELECT COUNT(*) FROM excel_report_rows WHERE report_id=:id"), {"id": report_id}
                    ).scalar_one()
                    table = report_table_name(record["site"], record["period_type"], record["year"],
                                              record["month"], record["start_date"], record["end_date"])
                    connection.execute(text(f'DROP TABLE IF EXISTS public."{table}"'))
                    connection.execute(text("DELETE FROM excel_reports WHERE id=:id"), {"id": report_id})
        except Exception as exc:
            raise HTTPException(500, "PostgreSQL deletion failed; the transaction was rolled back") from exc
    return {"deleted_reports": len(records), "deleted_daily_rows": daily_row_count,
            "local_files_removed": False, "message": "PostgreSQL records and per-report tables deleted; downloaded files remain on disk."}


@app.get("/api/dsm/charges-trend")
def charges_trend(sites: list[str] = Query(default=[]), start_date: date | None = None,
                  end_date: date | None = None, view: str = "daywise"):
    if view not in {"daywise", "monthwise"}:
        raise HTTPException(422, "view must be daywise or monthwise")
    where, params = parse_filters(sites, start_date, end_date)
    bucket = "date_trunc('month', report_date)::date" if view == "monthwise" else "report_date"
    rows = execute(f"""SELECT {bucket} AS period, SUM(dsm_charges_inr) AS charges,
        SUM(gross_revenue_inr) AS gross, CASE WHEN SUM(gross_revenue_inr)=0 THEN NULL
        ELSE SUM(dsm_charges_inr)/SUM(gross_revenue_inr)*100 END AS charge_percent
        FROM {ANALYTIC_ROWS}{where} GROUP BY period ORDER BY period""", params)
    return [{"date": json_safe(r["period"]), "dsm_charges": json_safe(r["charges"]),
             "gross_revenue": json_safe(r["gross"]), "dsm_charge_percent": json_safe(r["charge_percent"])} for r in rows]


def decimal_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def decimal_text(value: Decimal | None, places: str | None = None) -> str | None:
    if value is None:
        return None
    if places:
        value = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return format(value, "f")


def customer_allocation_data(customer: str | None, sites: list[str], start_date: date | None,
                             end_date: date | None, view: str) -> dict[str, Any]:
    sync_master_excel_if_changed()
    if view not in {"daywise", "monthwise"}:
        raise HTTPException(422, "view must be daywise or monthwise")
    where, params = parse_filters(sites, start_date, end_date)
    bucket = "date_trunc('month', report_date)::date" if view == "monthwise" else "report_date"
    charge_rows = execute(f"""SELECT site,{bucket} AS period,SUM(dsm_charges_inr) AS site_dsm_charge
        FROM {ANALYTIC_ROWS}{where} GROUP BY site,period ORDER BY period,site""", params)
    if sites:
        master_where = "WHERE report_site = ANY(:sites) AND report_site IS NOT NULL"
    else:
        master_where = "WHERE report_site IS NOT NULL"
    master_params: dict[str, Any] = {"sites": sites} if sites else {}
    masters = execute(f"""SELECT customer_name,report_site,site_total_capacity_mw_ac,
        customer_capacity_mw_ac FROM master_customer_sites {master_where}
        ORDER BY report_site,customer_name""", master_params)
    if not masters:
        raise HTTPException(422, "No mapped Master Excel customer/site capacity records match the selected filters")
    if customer and not any(row["customer_name"].casefold() == customer.strip().casefold() for row in masters):
        raise HTTPException(422, f"Customer '{customer}' has no mapped capacity records for the selected site(s)")

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    all_site_caps: dict[str, set[Decimal]] = {}
    for row in masters:
        site = row["report_site"]
        customer_name = row["customer_name"]
        site_cap = decimal_value(row["site_total_capacity_mw_ac"])
        customer_cap = decimal_value(row["customer_capacity_mw_ac"])
        customer_key = customer_name.casefold()
        site_customers = grouped.setdefault(site, {})
        item = site_customers.setdefault(customer_key, {"customer_name": customer_name, "capacity": Decimal(0),
                                                         "capacity_rows": 0, "missing_capacity": False})
        if customer_cap is None:
            item["missing_capacity"] = True
        else:
            item["capacity"] += customer_cap
            item["capacity_rows"] += 1
        if site_cap is not None:
            all_site_caps.setdefault(site, set()).add(site_cap)

    # Identify Master entries that could not be joined to any configured report site.
    unmapped_where = "WHERE report_site IS NULL"
    unmapped_params: dict[str, Any] = {}
    if customer:
        unmapped_where += " AND lower(customer_name)=lower(:customer)"
        unmapped_params["customer"] = customer.strip()
    unmapped = execute(f"SELECT DISTINCT name_on_dsm_charges FROM master_customer_sites {unmapped_where}", unmapped_params)

    charges_by_site_period: dict[tuple[str, str], Decimal | None] = {}
    for row in charge_rows:
        charges_by_site_period[(row["site"], json_safe(row["period"]))] = decimal_value(row["site_dsm_charge"])

    warnings: set[str] = set()
    output: list[dict[str, Any]] = []
    unallocated_total = Decimal(0)
    allocated_total = Decimal(0)
    selected_allocated_total = Decimal(0)
    site_charge_total = Decimal(0)
    for (site, period), site_charge in charges_by_site_period.items():
        if site_charge is None:
            warnings.add(f"DSM charge data is missing for {site} on {period}.")
            continue
        site_charge = site_charge.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        site_charge_total += site_charge
        customers = grouped.get(site)
        if not customers:
            warnings.add(f"No Master Excel customer mapping matches report site {site}; its DSM charge is unallocated.")
            unallocated_total += site_charge
            continue
        caps = all_site_caps.get(site, set())
        if len(caps) > 1:
            warnings.add(f"Master Excel has inconsistent Site Total Capacity values for {site}; allocation was skipped.")
            unallocated_total += site_charge
            for item in customers.values():
                if customer and item["customer_name"].casefold() != customer.strip().casefold():
                    continue
                output.append({"customer_name": item["customer_name"], "site": site, "date": period,
                    "site_dsm_charges": decimal_text(site_charge, "0.01"), "dsm_charges": None,
                    "dsm_per_mw": None, "site_total_capacity_mw_ac": None,
                    "customer_capacity_mw_ac": decimal_text(item["capacity"]),
                    "unallocated_balance": decimal_text(site_charge, "0.01"), "allocation_status": "inconsistent_site_capacity"})
            continue
        site_cap = next(iter(caps), None)
        if site_cap is None or site_cap <= 0:
            warnings.add(f"{site} has missing or zero Site Total Capacity; its DSM charge was not allocated.")
            unallocated_total += site_charge
            for item in customers.values():
                if customer and item["customer_name"].casefold() != customer.strip().casefold():
                    continue
                output.append({"customer_name": item["customer_name"], "site": site, "date": period,
                    "site_dsm_charges": decimal_text(site_charge, "0.01"), "dsm_charges": None,
                    "dsm_per_mw": None, "site_total_capacity_mw_ac": decimal_text(site_cap),
                    "customer_capacity_mw_ac": decimal_text(item["capacity"]),
                    "unallocated_balance": decimal_text(site_charge, "0.01"), "allocation_status": "invalid_site_capacity"})
            continue

        rate = site_charge / site_cap
        calculated: list[tuple[str, dict[str, Any], Decimal | None]] = []
        total_capacity = Decimal(0)
        missing_capacity = False
        for key, item in sorted(customers.items()):
            missing_capacity = missing_capacity or item["missing_capacity"]
            capacity = item["capacity"] if item["capacity_rows"] else None
            if capacity is not None:
                total_capacity += capacity
                charge = (rate * capacity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                charge = None
            calculated.append((key, item, charge))
        valid_allocations = [charge for _, _, charge in calculated if charge is not None]
        allocated_site = sum(valid_allocations, Decimal(0))
        fully_allocated = total_capacity == site_cap and not missing_capacity
        if fully_allocated and valid_allocations:
            rounding_residual = site_charge - allocated_site
            if rounding_residual:
                last_valid = max(i for i, (_, _, value) in enumerate(calculated) if value is not None)
                key, item, value = calculated[last_valid]
                calculated[last_valid] = (key, item, value + rounding_residual)
                allocated_site += rounding_residual
        balance = (site_charge - allocated_site).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        unallocated_total += balance
        allocated_total += allocated_site
        if missing_capacity:
            warnings.add(f"{site} has customer rows with missing Customer Capacity; those rows were not allocated.")
        if total_capacity != site_cap:
            warnings.add(f"{site} allocated capacity is {decimal_text(total_capacity)} MW of {decimal_text(site_cap)} MW; balance is {decimal_text(balance, '0.01')}.")
        status = "fully_allocated" if balance == 0 else "underallocated" if balance > 0 else "overallocated"
        for _, item, allocation in calculated:
            if item["capacity_rows"] and item["capacity"] == 0:
                warnings.add(f"{item['customer_name']} has zero Customer Capacity at {site}; allocated DSM charge is zero.")
            if allocation is not None and (not customer or item["customer_name"].casefold() == customer.strip().casefold()):
                selected_allocated_total += allocation
            if customer and item["customer_name"].casefold() != customer.strip().casefold():
                continue
            output.append({"customer_name": item["customer_name"], "site": site, "date": period,
                "site_dsm_charges": decimal_text(site_charge, "0.01"),
                "dsm_charges": decimal_text(allocation, "0.01"),
                "dsm_per_mw": decimal_text(rate, "0.0001"),
                "site_total_capacity_mw_ac": decimal_text(site_cap),
                "customer_capacity_mw_ac": decimal_text(item["capacity"] if item["capacity_rows"] else None),
                "unallocated_balance": decimal_text(balance, "0.01"), "allocation_status": status})

    for row in unmapped:
        label = row["name_on_dsm_charges"] or "(blank Name on DSM charges)"
        warnings.add(f"Master Excel Name on DSM charges '{label}' does not match a configured report site.")
    if not charge_rows:
        warnings.add("No imported DSM report data matches the selected site and date range.")
    return {"customer": customer, "view": view, "rows": output, "warnings": sorted(warnings),
            "summary": {"site_dsm_charges": decimal_text(site_charge_total, "0.01"),
                        "customer_dsm_charges": decimal_text(selected_allocated_total if customer else allocated_total, "0.01"),
                        "unallocated_balance": decimal_text(unallocated_total, "0.01"),
                        "site_period_count": len(charges_by_site_period)}}


@app.get("/api/master/mappings")
def get_master_mappings(customer: str | None = None, sites: list[str] = Query(default=[])):
    sync_master_excel_if_changed()
    filters, params = [], {}
    if customer:
        filters.append("lower(customer_name)=lower(:customer)")
        params["customer"] = customer
    if sites:
        invalid = sorted(set(sites) - set(SITES))
        if invalid:
            raise HTTPException(422, f"Unknown site(s): {', '.join(invalid)}")
        filters.append("report_site = ANY(:sites)")
        params["sites"] = sites
    where = " WHERE " + " AND ".join(filters) if filters else ""
    rows = execute("""SELECT customer_name,location_no,site_name,name_on_dsm_charges,group_name,
        site_total_capacity_mw_ac,customer_capacity_mw_ac,report_site FROM master_customer_sites""" + where +
        " ORDER BY customer_name,report_site,location_no", params)
    return [{key: json_safe(value) for key, value in dict(row).items()} for row in rows]


@app.get("/api/customer-dsm/trend")
@app.get("/api/customer-dsm/allocation")
def customer_dsm_trend(customer: str | None = None, sites: list[str] = Query(default=[]),
                       start_date: date | None = None, end_date: date | None = None,
                       view: str = "daywise"):
    return customer_allocation_data(customer, sites, start_date, end_date, view)


@app.get("/api/dsm/sitewise-percentage")
def sitewise_percentage(sites: list[str] = Query(default=[]), start_date: date | None = None,
                        end_date: date | None = None, view: str = "daywise"):
    if view not in {"daywise", "monthwise"}:
        raise HTTPException(422, "view must be daywise or monthwise")
    where, params = parse_filters(sites, start_date, end_date)
    bucket = "date_trunc('month', report_date)::date" if view == "monthwise" else "report_date"
    rows = execute(f"""SELECT site,{bucket} AS period,SUM(dsm_charges_inr) AS charges,
        SUM(gross_revenue_inr) AS gross,CASE WHEN SUM(gross_revenue_inr)=0 THEN NULL
        ELSE SUM(dsm_charges_inr)/SUM(gross_revenue_inr)*100 END AS charge_percent
        FROM {ANALYTIC_ROWS}{where} GROUP BY site,period ORDER BY period,site""", params)
    return [{"site": r["site"], "date": json_safe(r["period"]), "dsm_charges": json_safe(r["charges"]),
             "gross_revenue": json_safe(r["gross"]), "dsm_charge_percent": json_safe(r["charge_percent"])} for r in rows]


@app.get("/api/revenue/breakdown")
def revenue_breakdown(site: str, year: int, month: int):
    if site not in SITES or year not in range(2000, 2101) or month not in range(1, 13):
        raise HTTPException(422, "Invalid site, year, or month")
    report = report_record(site, year, month)
    if not report:
        raise HTTPException(404, "No data available for the selected site, month, and year.")
    # Use one report_id from the exact monthly report. Do not query the
    # cross-report analytics CTE, which intentionally combines date ranges.
    rows = execute("""SELECT COUNT(*) AS row_count,SUM(gross_revenue_inr) AS gross,
        SUM(dsm_charges_inr) AS charges FROM excel_report_rows
        WHERE report_id=:report_id AND site=:site AND year=:year AND month=:month""", {
        "report_id": report["id"], "site": site, "year": year, "month": month,
    })
    if not rows[0]["row_count"]:
        raise HTTPException(404, "No data available for the selected site, month, and year.")
    gross, charges = rows[0]["gross"], rows[0]["charges"]
    return {key: json_safe(value) for key, value in revenue_metrics(gross, charges).items()}


def report_record(site: str, year: int, month: int):
    rows = execute("""SELECT id,site,year,month,original_filename,downloaded_at
        FROM excel_reports WHERE site=:site AND year=:year AND month=:month AND period_type='monthly'
        ORDER BY downloaded_at DESC LIMIT 1""", {"site": site, "year": year, "month": month})
    return rows[0] if rows else None


@app.get("/api/reports/daywise")
def daywise_report(site: str, year: int, month: int):
    if site not in SITES or month not in range(1, 13) or year not in range(2000, 2101):
        raise HTTPException(422, "Invalid site, year, or month")
    report = report_record(site, year, month)
    if not report:
        raise HTTPException(404, "No report available for the selected site and month. Please run Data Download.")
    rows = execute("SELECT additional_data,report_date FROM excel_report_rows WHERE report_id=:id ORDER BY row_number",
                   {"id": report["id"]})
    # additional_data preserves the exact source strings and source headings.
    data = []
    for item in rows:
        values = item["additional_data"]
        if isinstance(values, str):
            values = json.loads(values)
        data.append({header: values.get(header) for header in REPORT_HEADERS})
    numeric = {header: sum((Decimal(str(row[header]).replace(",", "")) for row in data
                            if row.get(header) not in (None, "", "-", "N/A", "NA")), Decimal(0))
               for header in ("AC [MW]", "DC [MW]", "Scheduled Generation [MWh]", "Actual Generation [MWh]",
                              "Gross Revenue [₹]", "DSM Charges [₹]", "Net Revenue [₹]")}
    gross, charges = numeric["Gross Revenue [₹]"], numeric["DSM Charges [₹]"]
    numeric["DSM Charges [%]"] = (charges / gross * 100) if gross else None
    numeric_headers = {"AC [MW]", "DC [MW]", "Scheduled Generation [MWh]", "Actual Generation [MWh]",
                       "Gross Revenue [₹]", "DSM Charges [₹]", "DSM Charges [%]", "Net Revenue [₹]", "Firstband [%]"}
    return {"site": site, "year": year, "month": month, "original_filename": report["original_filename"],
            "columns": [{"key": h, "label": h, "type": "date" if h == "Date" else
                         "number" if h in numeric_headers else "string"} for h in REPORT_HEADERS],
            "rows": data, "record_count": len(data), "import_timestamp": json_safe(report["downloaded_at"]),
            "totals": {k: json_safe(v) for k, v in numeric.items()}}


@app.get("/api/reports/daywise/export")
def export_daywise(site: str, year: int, month: int):
    report = report_record(site, year, month)
    if not report:
        raise HTTPException(404, "No report available for the selected site and month")
    row = execute("SELECT content FROM excel_reports WHERE id=:id", {"id": report["id"]})[0]
    filename = Path(report["original_filename"]).name
    return Response(content=bytes(row["content"]), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"})


def ensure_automation_job_report_table() -> None:
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE IF NOT EXISTS dashboard_automation_job_reports (
            job_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            report_id BIGINT NOT NULL REFERENCES excel_reports(id) ON DELETE CASCADE,
            linked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(job_id,report_id))"""))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_job_reports_owner ON dashboard_automation_job_reports(owner_id,job_id)"))


def associate_job_reports(job_id: str, owner_id: str, request: AutomationRequest,
                          job: dict[str, Any]) -> int:
    """Persist links only for reports confirmed successful, skipped, or restored by this job."""
    ensure_automation_job_report_table()
    with engine.begin() as connection:
        for result in job.get("results", []):
            if result.get("status") not in {"successful", "skipped", "restored"}:
                continue
            site = result.get("site")
            period_label = result.get("period_label", "")
            report = None
            custom_period = re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: through \d{4}-\d{2}-\d{2})?", period_label)
            if custom_period and request.period_type in {"custom", "both"}:
                report = connection.execute(text("""SELECT id FROM excel_reports
                    WHERE site=:site AND period_type='custom' AND start_date=:start_date AND end_date=:end_date
                    ORDER BY downloaded_at DESC LIMIT 1"""), {
                    "site": site, "start_date": request.start_date, "end_date": request.end_date,
                }).first()
            elif request.period_type in {"monthly", "both"} and request.year and result.get("month"):
                report = connection.execute(text("""SELECT id FROM excel_reports
                    WHERE site=:site AND period_type='monthly' AND year=:year AND month=:month
                    ORDER BY downloaded_at DESC LIMIT 1"""), {
                    "site": site, "year": request.year, "month": result["month"],
                }).first()
            if report:
                connection.execute(text("""INSERT INTO dashboard_automation_job_reports(job_id,owner_id,report_id)
                    VALUES (:job_id,:owner_id,:report_id) ON CONFLICT (job_id,report_id) DO NOTHING"""), {
                    "job_id": job_id, "owner_id": owner_id, "report_id": report[0],
                })
        return connection.execute(text("SELECT COUNT(*) FROM dashboard_automation_job_reports WHERE job_id=:job_id AND owner_id=:owner_id"), {
            "job_id": job_id, "owner_id": owner_id,
        }).scalar_one()


def authorized_job_report_count(job_id: str, owner_id: str) -> int:
    if not re.fullmatch(r"job-[a-f0-9]{12}", job_id):
        raise HTTPException(404, "Automation job not found")
    ensure_automation_job_report_table()
    count = execute("SELECT COUNT(*) AS count FROM dashboard_automation_job_reports WHERE job_id=:job_id AND owner_id=:owner_id",
                    {"job_id": job_id, "owner_id": owner_id})[0]["count"]
    if not count:
        raise HTTPException(404, "No completed reports are available for this job")
    return count


def safe_archive_filename(site: str, period: str, filename: str) -> str:
    safe_site = re.sub(r"[^A-Za-z0-9._-]+", "_", site).strip("._") or "site"
    source_name = Path(filename.replace("\\", "/")).name
    safe_source = re.sub(r"[\x00-\x1f]", "_", source_name).strip(" .") or "report.csv"
    safe_period = re.sub(r"[^A-Za-z0-9_-]+", "_", period).strip("._") or "report"
    return f"{safe_site}/{safe_period}_{safe_source}"


def delete_job_archive(path: str) -> None:
    Path(path).unlink(missing_ok=True)


@app.get("/api/automation/jobs/{job_id}/files")
def automation_job_files(job_id: str, request: Request):
    owner_id = request.state.principal
    authorized_job_report_count(job_id, owner_id)
    rows = execute("""SELECT e.site,e.year,e.month,e.period_type,e.start_date,e.end_date,
        e.original_filename,e.sha256,e.downloaded_at FROM dashboard_automation_job_reports j
        JOIN excel_reports e ON e.id=j.report_id WHERE j.job_id=:job_id AND j.owner_id=:owner_id
        ORDER BY e.site,e.year,e.month,e.start_date""", {"job_id": job_id, "owner_id": owner_id})
    files = []
    for row in rows:
        period = (f"{row['year']}-{row['month']:02d}" if row["period_type"] == "monthly"
                  else f"{row['start_date']} through {row['end_date']}")
        files.append({"site": row["site"], "period": period,
                      "filename": Path(row["original_filename"].replace("\\", "/")).name,
                      "sha256": row["sha256"], "downloaded_at": json_safe(row["downloaded_at"])})
    return {"job_id": job_id, "file_count": len(files), "files": files}


@app.get("/api/automation/jobs/{job_id}/download")
def download_automation_job_reports(job_id: str, request: Request):
    owner_id = request.state.principal
    authorized_job_report_count(job_id, owner_id)
    export_dir = PROJECT_ROOT / "job_exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    descriptor, archive_path = tempfile.mkstemp(prefix="reports_", suffix=".zip", dir=export_dir)
    os.close(descriptor)
    manifest: list[dict[str, Any]] = []
    try:
        query = text("""SELECT e.id,e.site,e.year,e.month,e.period_type,e.start_date,e.end_date,
            e.original_filename,e.sha256,e.content FROM dashboard_automation_job_reports j
            JOIN excel_reports e ON e.id=j.report_id
            WHERE j.job_id=:job_id AND j.owner_id=:owner_id ORDER BY e.site,e.year,e.month,e.start_date""")
        with engine.connect() as connection, zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            rows = connection.execution_options(stream_results=True).execute(query, {
                "job_id": job_id, "owner_id": owner_id,
            }).mappings()
            for row in rows:
                period = (f"{row['year']}-{row['month']:02d}" if row["period_type"] == "monthly"
                          else f"{row['start_date']} through {row['end_date']}")
                archive_name = safe_archive_filename(row["site"], period, row["original_filename"])
                archive.writestr(archive_name, bytes(row["content"]))
                manifest.append({"site": row["site"], "period": period,
                                 "filename": Path(row["original_filename"].replace("\\", "/")).name,
                                 "archive_path": archive_name, "sha256": row["sha256"]})
            archive.writestr("manifest.json", json.dumps({"job_id": job_id, "reports": manifest}, ensure_ascii=False, indent=2))
        if not manifest:
            raise HTTPException(404, "No completed reports are available for this job")
        return FileResponse(archive_path, media_type="application/zip", filename=f"repro_reports_{job_id}.zip",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
                            background=BackgroundTask(delete_job_archive, archive_path))
    except Exception:
        delete_job_archive(archive_path)
        raise


@app.on_event("startup")
async def remove_abandoned_job_archives():
    export_dir = PROJECT_ROOT / "job_exports"
    if not export_dir.is_dir():
        return
    cutoff = datetime.now().timestamp() - 24 * 60 * 60
    for archive in export_dir.glob("reports_*.zip"):
        try:
            if archive.stat().st_mtime < cutoff:
                archive.unlink(missing_ok=True)
        except OSError:
            LOG.warning("Could not remove an expired temporary job archive")


def sanitized_line(line: str) -> str:
    # Avoid leaking local absolute paths from subprocess output into API responses.
    line = line.replace(str(PROJECT_ROOT), "<project>").replace(str(PROJECT_ROOT).replace("\\", "/"), "<project>")
    return line[:1200]


async def run_downloader_batch(batch: Path, args: list[str], job: dict[str, Any]) -> int:
    """Run the existing BAT from a worker thread and stream its output safely.

    asyncio.create_subprocess_exec raises WinError 5 in some Windows-hosted
    Uvicorn environments. subprocess.Popen works there and avoids blocking the
    API event loop while the downloader runs.
    """
    loop = asyncio.get_running_loop()
    command = "call " + subprocess.list2cmdline([str(batch)]) + " " + subprocess.list2cmdline(args)

    def run_and_stream() -> int:
        process = subprocess.Popen(
            ["cmd.exe", "/d", "/s", "/c", command],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        for raw in process.stdout:
            line = sanitized_line(raw.decode("utf-8", errors="replace").rstrip())
            if line:
                loop.call_soon_threadsafe(record_job_line, job, line)
        return process.wait()

    return await asyncio.to_thread(run_and_stream)


def parse_job_event(line: str):
    match = re.match(r"(STARTING|DOWNLOADED|IMPORTING|IMPORTED|SUCCESSFUL|SKIPPED|RESTORED|FAILED)\s+(.+)$", line)
    if not match:
        return None
    state, details = match.groups()
    custom = re.match(r"(.+?)\s+(\d{4}-\d{2}-\d{2}) through (\d{4}-\d{2}-\d{2})(?:$|:)", details)
    if custom:
        site, start, end = custom.groups()
        month_number = int(start[5:7])
        label = start if start == end else f"{start} through {end}"
        return state, site, month_number, label
    monthly = re.match(r"(.+?)\s+([A-Za-z]+)\s+(\d{4})(?:$|:)", details)
    if monthly and monthly.group(2) in MONTHS:
        site, month_name, year = monthly.groups()
        return state, site, MONTHS.index(month_name) + 1, f"{month_name} {year}"
    return None


def record_job_line(job: dict[str, Any], line: str) -> None:
    job["logs"].append(line)
    job["logs"] = job["logs"][-500:]
    event = parse_job_event(line)
    if not event:
        return
    state, site, month_num, period_label = event
    job["current_site"], job["current_month"], job["current_period"] = site, month_num, period_label
    if state == "DOWNLOADED":
        job["downloaded"] += 1
    elif state == "IMPORTED":
        job["imported"] += 1
    elif state in {"STARTING", "IMPORTING"}:
        return
    else:
        job["completed"] += 1
        job["results"].append({"site": site, "month": month_num, "period_label": period_label,
                               "status": state.lower(), "message": line})
        if state == "FAILED":
            job["failed"] += 1
        elif state == "SKIPPED":
            job["skipped"] += 1
        elif state == "RESTORED":
            job["restored"] += 1
        else:
            job["successful"] += 1


async def execute_job(job_id: str, request: AutomationRequest):
    job = jobs[job_id]
    job["status"] = "Running"
    job["started_at"] = datetime.now().astimezone().isoformat()
    batch = PROJECT_ROOT / "download_reports.bat"
    site_args = ["--all"] if set(request.sites) == set(SITES) else ["--sites", ",".join(request.sites)]
    command_args = []
    if request.period_type in {"monthly", "both"}:
        command_args.append(site_args + ["--year", str(request.year), "--months",
                                         *[MONTHS[m - 1] for m in request.months], "--headless"])
    if request.period_type in {"custom", "both"}:
        command_args.append(site_args + ["--from-date", request.start_date.isoformat(),
                                         "--to-date", request.end_date.isoformat(), "--headless"])
    exit_code = 0
    try:
        for args in command_args:
            # Only the allowlisted BAT path and validated structured arguments are used.
            exit_code = await run_downloader_batch(batch, args, job)
            if exit_code:
                break
        job["exit_code"] = exit_code
        if exit_code or job["completed"] < job["total"]:
            missing = max(0, job["total"] - job["completed"])
            job["failed"] += missing
            job["status"] = "Failed" if job["completed"] == 0 else "Partially Completed"
            if exit_code:
                job["logs"].append(f"Downloader exited with code {exit_code}")
        else:
            job["status"] = "Partially Completed" if job["failed"] else "Completed"
    except Exception as exc:
        job["status"] = "Failed" if job["completed"] == 0 else "Partially Completed"
        job["failed"] += max(0, job["total"] - job["completed"])
        error_detail = f"Windows error {exc.winerror}: {exc.strerror}" if isinstance(exc, OSError) and getattr(exc, "winerror", None) else str(getattr(exc, "strerror", "") or "")
        job["logs"].append(f"Automation could not start: {type(exc).__name__}" + (f" ({error_detail})" if error_detail else ""))
        LOG.exception("Automation could not start for job_id=%s", job_id)
    finally:
        owner_id = job.get("owner_id")
        if owner_id:
            final_status = job["status"]
            job["status"] = "Finalizing"
            try:
                job["files_ready"] = associate_job_reports(job_id, owner_id, request, job)
            except Exception as exc:
                job["files_ready"] = 0
                job["file_export_error"] = "Completed reports could not be prepared for browser download."
                job["logs"].append(f"Report download package preparation failed: {type(exc).__name__}")
                LOG.exception("Could not associate completed reports with job_id=%s", job_id)
            job["status"] = final_status
        job["finished_at"] = datetime.now().astimezone().isoformat()
        async with job_lock:
            for key in list(active_combinations):
                if active_combinations[key] == job_id:
                    del active_combinations[key]


async def run_job(job_id: str, request: AutomationRequest):
    # A single portal session at a time avoids cross-user session collisions;
    # site/period keys are reserved as soon as jobs are accepted.
    async with automation_execution_lock:
        await execute_job(job_id, request)


def schedule_job(job_id: str, request: AutomationRequest):
    return asyncio.create_task(run_job(job_id, request))


@app.post("/api/automation/start", status_code=202, dependencies=[Depends(require_automation_role)])
async def start_automation(request: AutomationRequest, http_request: Request):
    async with job_lock:
        keys = automation_keys(request)
        overlaps = [key for key in keys if key in active_combinations]
        if overlaps:
            raise HTTPException(409, "An automation job is already running for one or more selected site/months")
        job_id = "job-" + uuid.uuid4().hex[:12]
        total = len(keys)
        jobs[job_id] = {"job_id": job_id, "status": "Queued", "total": total, "completed": 0,
                        "successful": 0, "downloaded": 0, "imported": 0, "restored": 0,
                        "skipped": 0, "failed": 0, "current_site": None,
                        "current_month": None, "current_period": None, "results": [], "logs": [],
                        "files_ready": 0, "owner_id": http_request.state.principal,
                        "created_at": datetime.now().astimezone().isoformat()}
        for key in keys:
            active_combinations[key] = job_id
        schedule_job(job_id, request)
    return {"job_id": job_id, "status": "queued", "total_reports": total}


@app.get("/api/automation/status/{job_id}")
def automation_status(job_id: str, request: Request):
    if job_id not in jobs:
        raise HTTPException(404, "Automation job not found")
    if jobs[job_id].get("owner_id", "local") != request.state.principal:
        raise HTTPException(404, "Automation job not found")
    return {k: v for k, v in jobs[job_id].items() if k not in {"logs", "owner_id"}}


@app.get("/api/automation/logs/{job_id}")
def automation_logs(job_id: str, request: Request):
    if job_id not in jobs:
        raise HTTPException(404, "Automation job not found")
    if jobs[job_id].get("owner_id", "local") != request.state.principal:
        raise HTTPException(404, "Automation job not found")
    return {"job_id": job_id, "logs": jobs[job_id]["logs"]}


@app.get("/api/automation/jobs")
def automation_jobs(request: Request):
    return list(reversed([{k: v for k, v in job.items() if k not in {"logs", "owner_id"}}
                          for job in jobs.values() if job.get("owner_id", "local") == request.state.principal]))


@app.get("/{asset_path:path}", include_in_schema=False)
def frontend_asset_or_route(asset_path: str):
    """Serve the built React app and its assets when explicitly enabled."""
    if not SERVE_FRONTEND or asset_path.startswith(("api/", "docs", "redoc", "openapi.json")):
        raise HTTPException(404, "Not Found")
    candidate = (FRONTEND_DIST / asset_path).resolve()
    try:
        candidate.relative_to(FRONTEND_DIST)
    except ValueError:
        raise HTTPException(404, "Not Found") from None
    if candidate.is_file():
        return FileResponse(candidate)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html")
    raise HTTPException(404, "Dashboard build is missing; build frontend/dist first.")

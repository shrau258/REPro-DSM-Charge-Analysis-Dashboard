# RE.Pro DSM Charge Analysis Dashboard

This project keeps the existing Playwright downloader and adds a PostgreSQL-backed FastAPI service and React dashboard. The portal currently exports CSV files that Excel can open; the API preserves and downloads the original CSV bytes from PostgreSQL.

## Project layout

- `repro_download.py` — existing Playwright downloader and PostgreSQL importer.
- `download_reports.bat` — interactive Windows menu; also accepts downloader arguments for the dashboard service.
- `backend/` — FastAPI routes, SQLAlchemy connection and revenue calculations.
- `frontend/` — React + TypeScript + Vite dashboard.
- `tests/` — API and calculation tests.

## Windows setup

Use two PowerShell windows. PostgreSQL must be running and the database set in `.env` must exist.

### 1. Configure the environment

```powershell
cd C:\Users\scadacc1\Downloads\REPro_Python_Backend\repro_automation
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Edit `.env` locally and set `REPRO_USERNAME`, `REPRO_PASSWORD`, `REPRO_PG_HOST`, `REPRO_PG_PORT`, `REPRO_PG_DATABASE`, `REPRO_PG_USER`, and `REPRO_PG_PASSWORD`. Keep `.env` private; do not paste its values into logs or source control. Existing `excel_reports` and `excel_report_rows` data are used directly. The downloader creates/migrates its existing report tables on its normal database startup.

### 2. Start the API (PowerShell window 1)

```powershell
cd C:\Users\scadacc1\Downloads\REPro_Python_Backend\repro_automation
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Check `http://127.0.0.1:8000/api/health` or open the interactive API page at `http://127.0.0.1:8000/docs`.

### 3. Start the dashboard (PowerShell window 2)

```powershell
cd C:\Users\scadacc1\Downloads\REPro_Python_Backend\repro_automation\frontend
npm install
npm run dev
```

Open the Vite URL printed in the terminal (normally `http://127.0.0.1:5173`). Keep both terminals running. In development, Vite proxies `/api` requests to `http://127.0.0.1:8000`. Set `VITE_API_URL` only if the API is hosted elsewhere; restart Vite after changing it.

After setup, you can double-click `start_dashboard.bat` to open both services in separate terminal windows.

## Dashboard features

- Global and independent card site filters; daywise/monthwise DSM charts; inclusive custom date filters.
- Site/month report viewer with the source 12 columns in source order, sorting, 15-row pagination, totals and original CSV download.
- Revenue/DSM donut based on aggregated rupee values. Percentages are recalculated from sums; source percentages remain as-is in report rows.
- Run Data Download modal for selected sites with monthly periods, one date, a custom date range, or both monthly and custom-date reports. Dashboard-triggered Chromium and its command window run hidden; progress stays in the dashboard. A successful run closes the modal and shows a completion toast. Failed/partial runs keep their error details visible.
- Settings provides delete-all, site-wide, site-month, and exact custom-date report deletion. It previews affected report/row counts, requires typing `DELETE`, removes PostgreSQL rows and per-report tables transactionally, and leaves local downloaded files untouched.

## Existing downloader and BAT usage

The dashboard starts the allowlisted `download_reports.bat` with validated CLI parameters. The BAT forwards them to the existing `repro_download.py`; the browser never supplies shell commands. The BAT also retains its interactive menu for manual use:

```powershell
cd C:\Users\scadacc1\Downloads\REPro_Python_Backend\repro_automation
.\download_reports.bat
```

Direct downloader examples:

```powershell
# One site/month
python repro_download.py --site zaheerabad --year 2026 --months April

# A specific site for several months
python repro_download.py --site zaheerabad --year 2026 --months April May June

# All sites for all months (January–December)
python repro_download.py --all --year 2026 --months January February March April May June July August September October November December
```

The automation API accepts monthly, custom-date, or combined monthly-plus-custom requests. A single date is represented by the same start and end date. Custom ranges use the existing downloader's inclusive `--from-date`/`--to-date` flow. Dashboard chart/report filters can use any inclusive dates represented by imported rows.

## API endpoints

- `GET /api/health`, `GET /api/sites`, `GET /api/reports/coverage`
- `GET /api/dsm/charges-trend?sites=zaheerabad&start_date=2026-04-01&end_date=2026-11-30&view=monthwise`
- `GET /api/dsm/sitewise-percentage` (same filters)
- `GET /api/revenue/breakdown` (same filters)
- `GET /api/reports/daywise?site=zaheerabad&year=2026&month=4`
- `GET /api/reports/daywise/export?site=zaheerabad&year=2026&month=4`
- `GET /api/master/customers`, `GET/POST /api/settings/master-excel`
- `POST /api/automation/start`, `GET /api/automation/status/{job_id}`, `GET /api/automation/logs/{job_id}`, `GET /api/automation/jobs`

### Customer and site Master Excel

Open the dashboard Settings, enter the full path to the folder containing the Master Excel workbook, and choose **Save and Sync Master Excel**. The backend uses the same parser and PostgreSQL upsert for folder sync and direct upload. It picks the most recently modified supported workbook and checks for updates while the dashboard is open; after a successful sync the Customer and Site dropdowns refresh automatically. The selected customer narrows the available site choices and dashboard data. A failed or unreadable update leaves the last successful mapping intact.

Settings also supports choosing a local `.xlsx` or `.xls` file with **Choose Excel File** and applying it with **Upload & Sync Master Excel**. The browser sends a `multipart/form-data` request to `POST /api/settings/master-excel/upload` using the field name `file` (the browser supplies the multipart boundary). The parser reads `Sheet1` columns B:G as Customer Name, Location No., Site, Group, Site Total Capacity (MW-AC), and Capacity (MW-AC). An optional Name on DSM charges column may be present in E, shifting Group and capacities to F:H; when absent, the Site column is used to match RE.Pro sites. The parser validates the sheet before transactionally upserting customer/location records in PostgreSQL; a bad upload leaves existing records intact and returns the validation error. Uploaded workbooks are retained under `uploads/master_excel/`. The folder watcher uses the same parser and supports `.xlsx`, `.xls`, and `.xlsm`. If `/api/settings/master-excel` or `/api/master/customers` returns 404, restart the backend from this project's `repro_automation` directory using the command above; an older Uvicorn process may still be serving port 8000.

Customer capacity records are stored in `master_customer_sites` with a unique normalized customer/location key. The `customer_site_map` table continues to hold the dashboard's customer-to-report-site lookup. `GET /api/master/mappings` returns the capacity mapping, while `GET /api/customer-dsm/allocation` and `/api/customer-dsm/trend` return actual report-backed allocations for the requested sites, period, and view. These APIs de-duplicate imported report rows using the same monthly/custom report precedence as the existing analytics. Allocation values use Decimal arithmetic, are rounded to paise, and the response includes capacity warnings and the unallocated site-charge balance.
- `POST /api/reports/delete-preview`, `POST /api/reports/delete`

Month report downloads return the original saved CSV, not a reconstructed workbook. Missing reports return 404 with an explicit message. Automation job state is held in API process memory; restarting the API forgets its progress history, while imported reports remain in PostgreSQL.

## Database tables

- `excel_reports` keeps report identity, original filename, local path, hash, bytes and download timestamp.
- `excel_report_rows` stores typed daily columns and `additional_data` preserves source CSV text values for the report viewer.
- A sheet-shaped `report_<site>_<year>_<month>` table is created per monthly report by the existing downloader.

The dashboard queries `excel_report_rows` for analytics and reads `excel_reports.content` for original-file export. Aggregated DSM percentage is `SUM(DSM charges) / SUM(gross revenue) * 100`; it never averages row percentages. The detailed report viewer continues to show the original percentage values.

## Tests and build

```powershell
cd C:\Users\scadacc1\Downloads\REPro_Python_Backend\repro_automation
.\.venv\Scripts\Activate.ps1
python -m pytest

cd .\frontend
npm run build
```

The automated tests cover money calculations, zero revenue, input validation, site filters, aggregation query shape, report availability and the 12-column order. The API check uses the configured PostgreSQL connection. Live portal automation still depends on portal account access, certificate/network conditions and the portal DOM; prior runs in this project reached the portal dashboard but did not reach Historical Insights, so this project change does not claim that a live download has been verified.

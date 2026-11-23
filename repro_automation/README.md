# RE.Pro DSM Charge Analysis Dashboard

This project keeps the existing Playwright downloader and adds a PostgreSQL-backed FastAPI service and React dashboard. The portal currently exports CSV files that Excel can open; the API preserves and downloads the original CSV bytes from PostgreSQL.

## Project layout

- `repro_download.py` — existing Playwright downloader and PostgreSQL importer.
- `download_reports.bat` — interactive Windows menu; also accepts downloader arguments for the dashboard service.
- `backend/` — FastAPI routes, SQLAlchemy connection and revenue calculations.
- `frontend/` — React + TypeScript + Vite dashboard.
- `tests/` — API and calculation tests.
- `deployment/` — production environment template, Caddy configuration template, Windows service scripts, health check, and PostgreSQL backup/restore scripts.

## Company Windows LAN production deployment

### Direct LAN run on this Windows machine

For a temporary direct-IP run without a login, use `start_lan_dashboard.ps1` from the project root. It serves the existing `frontend/dist` build and the FastAPI endpoints together on the selected machine IP (default `192.168.41.199`) at port `8080`. It derives an allowlisted CIDR from the selected network adapter, also allows the confirmed company subnet `192.168.2.0/24`, and binds only to the selected IP. Keep the PowerShell window open; press Ctrl+C to stop it.

```powershell
Set-Location 'C:\path\to\repro_automation'
.\start_lan_dashboard.ps1
# Or specify a different IPv4 assigned to this machine:
.\start_lan_dashboard.ps1 -ListenAddress '192.168.41.199' -Port 8080
# Add any other IT-approved routed client subnets when required:
.\start_lan_dashboard.ps1 -AdditionalAllowedCidrs @('192.168.2.0/24','192.168.41.0/24')
```

This direct mode has no authentication: every device that can reach the allowed subnet can use dashboard features, including Settings, master uploads, report deletion, and downloads. Use it only on the approved internal LAN. The launcher does not change Windows Firewall; if another computer cannot connect, have IT review the inbound TCP 8080 rule and LAN policy. This temporary run does not install a Windows service and stops when its PowerShell process exits. For persistent hosting, use the authenticated Caddy/service deployment below.

Production uses the compiled Vite site; it does not run the Vite development server. The intended request path is:

```text
LAN user -> company SSO gateway -> Caddy (HTTPS/static files) -> FastAPI on loopback -> private PostgreSQL
```

Caddy serves `frontend/dist` and forwards same-origin `/api` requests to FastAPI at `127.0.0.1:8000`. FastAPI is not bound to the LAN. The service runs one Uvicorn worker because automation queue/duplicate locks are process-local; a second process or second API instance would not share those locks. Portal automation jobs are serialized to avoid concurrent logins using the same portal account. A job is rejected if another job already has the same site/period key.

### IT items required before deployment

IT must provide/configure these; the scripts do not change network settings:

- An approved, stable server install folder (prefer a managed application directory over this developer checkout under Downloads) and a static/reserved LAN IPv4 address. The address in the supplied `ipconfig` screenshot (`192.168.41.199`) is only a candidate; verify it on the intended server and have IT reserve it before use.
- Choose either an IT DNS hostname or direct IP URL. For DNS, IT must configure the name to route to this server. For an IP URL such as `https://<static-ip>`, the company SSO gateway must accept and route that IP host, and IT must issue an HTTPS certificate whose Subject Alternative Name includes that IPv4 address. Browsers reject certificates that do not include the exact IP.
- A company SSO/authentication gateway in front of Caddy. It must authenticate users, strip client-supplied `X-Authenticated-User` and `X-Authenticated-Roles`, and inject those headers only after authentication. It must send requests to Caddy from known source IPs. Caddy's IP allowlist blocks clients that bypass the gateway.
- The exact SSO user and group/role claim mappings. The default required roles are `DSM-Users` to start downloads and `DSM-Admin` for Settings, Master Excel synchronization, and report deletion; IT can set the role names in `.env` to match its identity provider.
- Firewall rules allowing HTTPS only from the approved LAN/SSO gateway path. FastAPI `8000` and PostgreSQL `5432` must not be reachable from client machines. If PostgreSQL is on another host, allow only the dashboard service host to connect to its private database listener.
- Approved Caddy and NSSM Windows binaries, and a least-privilege Windows service identity. Give the API identity read access to application code and `.env`, read access to the shared Playwright browser, certificate only as needed, and the configured Master Excel folder. Grant Modify access only to `downloads`, `uploads`, `artifacts`, `job_exports`, and `logs`. Give Caddy read access to `frontend/dist` and the TLS key and write access to its log file. `REPRO_ERROR_LOG` directs downloader errors into the protected `logs` folder.

Caddy does not provide the company's SSO by itself. Do not expose Caddy to ordinary LAN clients unless the company gateway protects it and the network prevents direct bypass. The Caddy configuration only accepts source IPs supplied as `TrustedGatewayIps`; use exact gateway addresses, not a broad user subnet. The gateway must also enforce authorization. The backend independently checks proxy identity and roles. POST requests require an approved browser origin to reduce cross-site request forgery risk.

### Prepare the production copy

Do not run Windows services from a personal Downloads folder for the final deployment. Have IT stage the source in its managed application path. Do not copy the developer `.env` or local `reports.sqlite3` over production. Keep the existing PostgreSQL database and report rows; point the production `.env` at that database and take a backup before the first production start.

From an elevated PowerShell window in the staged project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create the private `.env` from `deployment/production.env.example` only if one does not already exist (`if (-not (Test-Path .env)) { Copy-Item deployment/production.env.example .env }`), then edit it with the IT-provided database, SSO header/role, allowed origin, proxy peer, and shared `PLAYWRIGHT_BROWSERS_PATH` values. Keep the portal and PostgreSQL passwords only in this file or an IT-approved secret store. The deployment installer refuses to proceed unless PostgreSQL and authentication are enabled and the HTTPS origin is configured.

Install the Chromium build that matches the installed Playwright package into the shared directory, and grant the service identity read/execute permission there:

```powershell
$browserSetting = Select-String -Path .env -Pattern '^PLAYWRIGHT_BROWSERS_PATH=' | Select-Object -Last 1
$env:PLAYWRIGHT_BROWSERS_PATH = ($browserSetting.Line -split '=', 2)[1].Trim().Trim('"').Trim("'")
New-Item -ItemType Directory -Path $env:PLAYWRIGHT_BROWSERS_PATH -Force | Out-Null
.\.venv\Scripts\python.exe -m playwright install chromium
```

The service's Python process loads `.env`, including `PLAYWRIGHT_BROWSERS_PATH`. Installing Chromium under one interactive user's default profile is not sufficient if the service runs as a different identity.

### Build and verify PostgreSQL without replacing records

Install Node.js LTS on the build host, then run:

```powershell
Set-Location .\frontend
npm ci
npm run build
Set-Location ..
.\.venv\Scripts\python.exe repro_download.py --db-check
```

`--db-check` verifies the configured PostgreSQL connection and reports counts; the downloader's existing idempotent schema setup may add missing tables/indexes, but it does not delete report records. Review output and confirm the existing report counts are present before continuing. Do not run SQLite migration against this live database unless IT explicitly approves that separate operation.

### Install Caddy and the Windows services

Download Caddy and NSSM from their official sources using IT's software approval process. In an elevated PowerShell window, set deployment values in environment variables (these are configuration, not credentials) and install services. Use forward-slash or quoted local filesystem paths as PowerShell accepts them:

```powershell
$env:DSM_PROJECT_ROOT = 'SET_TO_THE_MANAGED_PROJECT_PATH'
$env:DSM_NSSM = 'SET_TO_THE_APPROVED_NSSM_EXE'
$env:DSM_CADDY = 'SET_TO_THE_APPROVED_CADDY_EXE'
$env:DSM_HOST = 'SET_TO_THE_IT_DNS_HOSTNAME'
$env:DSM_SERVER_IP = 'SET_TO_THE_IT_STATIC_SERVER_IP'
$env:DSM_GATEWAY_IPS = 'SET_TO_THE_EXACT_SSO_GATEWAY_SOURCE_IPS'
$env:DSM_TLS_CERT = 'SET_TO_THE_IT_CERTIFICATE_FILE'
$env:DSM_TLS_KEY = 'SET_TO_THE_PROTECTED_IT_PRIVATE_KEY_FILE'
.\deployment\Install-ProductionServices.ps1 `
  -ProjectRoot $env:DSM_PROJECT_ROOT `
  -NssmPath $env:DSM_NSSM `
  -CaddyPath $env:DSM_CADDY `
  -DashboardHost $env:DSM_HOST `
  -ServerLanIp $env:DSM_SERVER_IP `
  -TrustedGatewayIps $env:DSM_GATEWAY_IPS `
  -TlsCertificatePath $env:DSM_TLS_CERT `
  -TlsPrivateKeyPath $env:DSM_TLS_KEY
```

The installer validates the Caddy config and creates `EnrichDSMApi` and `EnrichDSMCaddy` with restart-on-exit and rotated logs. It leaves them **disabled** until IT assigns the approved service identity and its file/certificate permissions. Open `services.msc`, open each service's **Properties → Log On**, choose **This account**, enter the IT-provided service account and password in the Windows dialog, and ensure it has **Log on as a service**. Do not put that password in a command or script. Grant the account the folder/key permissions described above. Then run the enable script to set Automatic startup and start services:

```powershell
Set-Location $env:DSM_PROJECT_ROOT
.\deployment\Enable-ProductionServices.ps1
Get-Service EnrichDSMApi, EnrichDSMCaddy
.\deployment\Test-Production.ps1
```

The enable script starts FastAPI first, waits for its PostgreSQL health check, then starts Caddy; both are set to Automatic for startup after reboot. Caddy listens only on the IT-configured IP and HTTPS port 443. The script does not edit Windows Firewall, network interfaces, DNS, PostgreSQL listen settings, or certificates.

### Access and operational commands

Once IT configures the stable IP, matching HTTPS certificate, SSO gateway, and firewall policy, use the IT-provided URL from a LAN computer. This can be `https://<static-ip>` when the gateway routes and authenticates that IP host, or an IT DNS hostname. A direct IP URL does not bypass authentication: Caddy accepts only source IPs of the trusted SSO gateway. Verify login, Customer/Site filters, existing dashboard trends, Settings permissions, and CSV export from a second LAN computer. A user with `DSM-Users` can start automation; administrators with `DSM-Admin` can sync Master Excel or delete reports. Every user downloads the resulting job ZIP through their own browser. The server still stores its copy and PostgreSQL content.

Do not broaden Caddy's gateway allowlist to the whole LAN to make direct access work. If IT cannot route the IP URL through SSO and inject the authenticated user/role headers, use a company DNS hostname with the supported SSO gateway or ask IT to provide an approved identity-aware gateway. Keep FastAPI port 8000 and PostgreSQL port 5432 private.

On the server, elevated PowerShell commands:

```powershell
Get-Service EnrichDSMApi, EnrichDSMCaddy
Restart-Service EnrichDSMApi, EnrichDSMCaddy
Stop-Service EnrichDSMCaddy, EnrichDSMApi
Start-Service EnrichDSMApi
Start-Service EnrichDSMCaddy
Get-Content .\logs\api-error.log -Tail 100
Get-Content .\logs\api-out.log -Tail 100
Get-Content .\logs\caddy-error.log -Tail 100
Get-Content .\logs\caddy-access.jsonl -Tail 50
```

Use the backup script regularly and store the resulting custom-format database dump on an IT-approved protected backup target:

```powershell
Set-Location $env:DSM_PROJECT_ROOT
.\deployment\Backup-Postgres.ps1
```

PostgreSQL prompts securely if no approved password file/credential helper is configured; the scripts never place a password in command arguments or output. For recovery, create a new empty target database first, point a protected recovery `.env` at it, then explicitly confirm restore. The script never uses `--clean`, drops tables, or overwrites a database:

```powershell
.\deployment\Restore-Postgres.ps1 -BackupFile 'SET_TO_THE_PROTECTED_BACKUP.dump' -ConfirmRestore
```

### Production verification status

Local production build, Python syntax/import, API route registration, and an available local PostgreSQL health check can be verified on the build/server host. A real SSO login, role authorization, certificate trust, LAN firewall/DNS route, browser CSV delivery from another computer, and actual portal BAT download require the IT-configured network and must be verified there. Do not report those as passed until a second LAN client completes them. Excel upload writes master mappings, and BAT execution downloads/imports real reports, so perform those acceptance checks with an IT-approved test workbook/site-period after the backup and access controls are in place.

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

Open the Vite URL printed in the terminal (normally `http://127.0.0.1:5173`). Keep both terminals running. In development, Vite proxies relative `/api` requests to `http://127.0.0.1:8000`. The production bundle also uses same-origin relative API URLs and is served by Caddy; do not run `npm run dev` as the LAN production server.

After setup, you can double-click `start_dashboard.bat` to open both services in separate terminal windows.

## Dashboard features

- Global and independent card site filters; daywise/monthwise DSM charts; inclusive custom date filters.
- Site/month report viewer with the source 12 columns in source order, sorting, 15-row pagination, totals and original CSV download.
- Revenue/DSM donut based on aggregated rupee values. Percentages are recalculated from sums; source percentages remain as-is in report rows.
- Run Data Download modal for selected sites with monthly periods, one date, a custom date range, or both monthly and custom-date reports. Dashboard-triggered Chromium and its command window run hidden; progress stays in the dashboard. A successful run closes the modal and shows a completion toast. Failed/partial runs keep their error details visible.
- After a job completes, use **Download reports** in the completion toast to download a ZIP of that job's successfully stored reports to the browser user's machine. The ZIP contains the original CSVs grouped by site and period plus a manifest. The browser's configured download folder/policy controls the final location; on a normal Windows setup this is the user's Downloads folder. The backend continues keeping its own copy and PostgreSQL BLOBs.
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

### Company-network report delivery and authentication

In local single-user mode (`REPRO_AUTH_REQUIRED=false`), the dashboard behaves as before. For company-network hosting, put both the frontend and `/api` behind the company's authenticated reverse proxy/SSO and set `REPRO_AUTH_REQUIRED=true`. Configure `REPRO_TRUSTED_PROXY_IPS` with the source IP address(es) that the backend actually sees for that proxy, and set `REPRO_AUTH_USER_HEADER` to the identity header the proxy injects. The proxy must remove any client-supplied value of that header and set its own value only after successful authentication. Restrict direct backend access at the host firewall to the proxy, and set `REPRO_CORS_ORIGINS` to the exact dashboard origin(s) if the frontend and API use different origins. Same-origin frontend/API routing is preferred.

Job status and report-file routes are scoped to the authenticated identity. Only reports associated with that job are packaged. The ZIP is generated temporarily on the server from PostgreSQL report content, sent as a normal browser download, and removed after the response; abandoned temporary archives older than 24 hours are pruned at API startup. Run Uvicorn with `--no-proxy-headers` so the backend checks the actual connecting proxy IP instead of a forwarded client address. Do not expose the backend directly to the LAN with `REPRO_AUTH_REQUIRED=false`. A user cannot be instructed by the server to write directly to a remote PC's filesystem; the browser-mediated download is the supported secure delivery path. Browser settings or company policy may prompt for a save location or change the default Downloads folder.

## API endpoints

- `GET /api/health`, `GET /api/sites`, `GET /api/reports/coverage`
- `GET /api/dsm/charges-trend?sites=zaheerabad&start_date=2026-04-01&end_date=2026-11-30&view=monthwise`
- `GET /api/dsm/sitewise-percentage` (same filters)
- `GET /api/revenue/breakdown` (same filters)
- `GET /api/reports/daywise?site=zaheerabad&year=2026&month=4`
- `GET /api/reports/daywise/export?site=zaheerabad&year=2026&month=4`
- `GET /api/master/customers`, `GET/POST /api/settings/master-excel`
- `POST /api/automation/start`, `GET /api/automation/status/{job_id}`, `GET /api/automation/logs/{job_id}`, `GET /api/automation/jobs`
- `GET /api/automation/jobs/{job_id}/files` (metadata for this user's available job reports)
- `GET /api/automation/jobs/{job_id}/download` (browser ZIP download of original report files)

### Customer and site Master Excel

Open the dashboard Settings, enter the full path to the folder containing the Master Excel workbook, and choose **Save and Sync Master Excel**. The backend uses the same parser and PostgreSQL upsert for folder sync and direct upload. It picks the most recently modified supported workbook and checks for updates while the dashboard is open; after a successful sync the Customer and Site dropdowns refresh automatically. The selected customer narrows the available site choices and dashboard data. A failed or unreadable update leaves the last successful mapping intact.

Settings also supports choosing a local `.xlsx` or `.xls` file with **Choose Excel File** and applying it with **Upload & Sync Master Excel**. The browser sends a `multipart/form-data` request to `POST /api/settings/master-excel/upload` using the field name `file` (the browser supplies the multipart boundary). The parser reads `Sheet1` columns B:G as Customer Name, Location No., Site, Group, Site Total Capacity (MW-AC), and Capacity (MW-AC). An optional Name on DSM charges column may be present in E, shifting Group and capacities to F:H; when absent, the Site column is used to match RE.Pro sites. Master rows are incrementally upserted using normalized Customer Name + Site + Location No. keys; unchanged rows are skipped, changed fields are updated, and rows absent from a later workbook are retained. Settings reports added, updated, duplicate-skipped, and total unique record counts. Uploaded workbooks are retained under `uploads/master_excel/`. The folder watcher uses the same parser and supports `.xlsx`, `.xls`, and `.xlsm`. If `/api/settings/master-excel` or `/api/master/customers` returns 404, restart the backend from this project's `repro_automation` directory using the command above; an older Uvicorn process may still be serving port 8000.

Customer capacity records are stored in `master_customer_sites` with a database unique constraint on normalized customer/site/location keys. The `customer_site_map` table continues to hold the dashboard's customer-to-report-site lookup. `GET /api/master/mappings` returns the capacity mapping, while `GET /api/customer-dsm/allocation` and `/api/customer-dsm/trend` return actual report-backed allocations for the requested sites, period, and view. These APIs de-duplicate imported report rows using the same monthly/custom report precedence as the existing analytics. Allocation values use Decimal arithmetic, are rounded to paise, and the response includes capacity warnings and the unallocated site-charge balance.
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

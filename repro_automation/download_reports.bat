@echo off
setlocal
cd /d "%~dp0"

echo.
echo RE.Pro report downloader
echo ========================
if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: The project's .venv was not found.
    pause
    exit /b 1
)
if not exist ".env" (
    echo ERROR: .env was not found. Create it from .env.example and set local credentials.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"

rem When called with validated CLI arguments (for example by the dashboard API),
rem forward them to the downloader without opening the interactive menu.
if not "%~1"=="" (
    python repro_download.py %*
    exit /b %ERRORLEVEL%
)

echo Choose report period:
echo   1. Monthly report(s)
echo   2. Custom date or date range
set /p "PERIOD_MODE=Enter 1 or 2: "
if "%PERIOD_MODE%"=="1" goto choose_scope
if "%PERIOD_MODE%"=="2" goto choose_scope
echo Please enter 1 or 2.
pause
exit /b 1

:choose_scope
echo.
echo Choose sites:
echo   1. All configured sites
echo   2. Selected site(s)
set /p "SITE_SCOPE=Enter 1 or 2: "
if "%SITE_SCOPE%"=="1" set "SITE_ARGS=--all"
if "%SITE_SCOPE%"=="2" goto selected_sites
if "%SITE_SCOPE%"=="1" goto period_inputs
echo Please enter 1 or 2.
pause
exit /b 1

:selected_sites
echo Enter exact site names separated by commas.
echo Example: zaheerabad,mandrup,Fortune Metals Ltd
set /p "SITE_NAMES=Site(s): "
if "%SITE_NAMES%"=="" (
    echo You must enter at least one site.
    pause
    exit /b 1
)
set "SITE_ARGS=--sites "%SITE_NAMES%""

:period_inputs
if "%PERIOD_MODE%"=="1" goto monthly_inputs
goto custom_inputs

:monthly_inputs
set /p "REPORT_YEAR=Year (press Enter for 2026): "
if "%REPORT_YEAR%"=="" set "REPORT_YEAR=2026"
set /p "MONTH_LIST=Months separated by spaces (press Enter for April-November): "
if "%MONTH_LIST%"=="" (
    python repro_download.py %SITE_ARGS% --year %REPORT_YEAR%
) else (
    python repro_download.py %SITE_ARGS% --year %REPORT_YEAR% --months %MONTH_LIST%
)
goto finished

:custom_inputs
echo Enter dates using YYYY-MM-DD. Use the same date twice to pull one day.
set /p "DATE_FROM=FROM date: "
set /p "DATE_TO=TO date: "
if "%DATE_FROM%"=="" goto missing_dates
if "%DATE_TO%"=="" goto missing_dates
python repro_download.py %SITE_ARGS% --from-date "%DATE_FROM%" --to-date "%DATE_TO%"
goto finished

:missing_dates
echo Both FROM and TO dates are required.
pause
exit /b 1

:finished
set "RUN_RESULT=%ERRORLEVEL%"
echo.
if "%RUN_RESULT%"=="0" (
    echo Finished. Review the summary above.
) else (
    echo The run returned an error. Check download_errors.log and the artifacts folder.
)
pause
exit /b %RUN_RESULT%

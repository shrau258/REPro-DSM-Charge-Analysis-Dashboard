import asyncio
from decimal import Decimal
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.calculations import revenue_metrics
from backend.schemas import AutomationRequest, DeleteRequest
from repro_download import REPORT_HEADERS, SITES


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "execute", lambda sql, params=None: [])
    return TestClient(main.app)


def test_aggregated_revenue_formula_uses_amounts():
    totals = revenue_metrics(Decimal("245100000"), Decimal("3600000"))
    assert totals["net_revenue"] == Decimal("241500000")
    assert totals["dsm_charge_percent"].quantize(Decimal("0.01")) == Decimal("1.47")
    assert totals["net_revenue_percent"].quantize(Decimal("0.01")) == Decimal("98.53")


def test_zero_gross_revenue_has_no_percentage():
    result = revenue_metrics(Decimal("0"), Decimal("35"))
    assert result["net_revenue"] == Decimal("-35")
    assert result["dsm_charge_percent"] is None
    assert result["net_revenue_percent"] is None


def test_missing_amounts_are_unavailable():
    assert revenue_metrics(None, None)["net_revenue"] is None


def test_exact_report_column_order_is_preserved():
    assert REPORT_HEADERS == (
        "Date", "AC [MW]", "DC [MW]", "Energy Type", "Type",
        "Scheduled Generation [MWh]", "Actual Generation [MWh]",
        "Gross Revenue [₹]", "DSM Charges [₹]", "DSM Charges [%]",
        "Net Revenue [₹]", "Firstband [%]",
    )


def test_download_request_accepts_valid_multi_site_multi_month():
    request = AutomationRequest(sites=[SITES[0], SITES[1]], year=2026, months=[4, 5, 6])
    assert len(request.sites) * len(request.months) == 6


def test_download_request_supports_months_and_custom_dates_together():
    request = AutomationRequest(sites=[SITES[0]], period_type="both", year=2026, months=[4, 5],
                                start_date=date(2026, 4, 12), end_date=date(2026, 4, 12))
    assert len(main.automation_keys(request)) == 3


def test_custom_single_date_progress_event_is_recognized():
    event = main.parse_job_event("SUCCESSFUL zaheerabad 2026-04-12 through 2026-04-12")
    assert event == ("SUCCESSFUL", "zaheerabad", 4, "2026-04-12")


def test_background_both_mode_runs_headless_month_and_custom_commands(monkeypatch):
    calls=[]
    async def fake_run(batch, args, job):
        calls.append((batch, args))
        lines=(
            ["STARTING zaheerabad April 2026", "DOWNLOADED zaheerabad April 2026",
             "IMPORTED zaheerabad April 2026", "SUCCESSFUL zaheerabad April 2026"]
            if len(calls)==1 else
            ["STARTING zaheerabad 2026-04-12 through 2026-04-12",
             "DOWNLOADED zaheerabad 2026-04-12 through 2026-04-12",
             "IMPORTED zaheerabad 2026-04-12 through 2026-04-12",
             "SUCCESSFUL zaheerabad 2026-04-12 through 2026-04-12"]
        )
        for line in lines:
            main.record_job_line(job, line)
        return 0
    monkeypatch.setattr(main, "run_downloader_batch", fake_run)
    request=AutomationRequest(sites=[SITES[0]],period_type="both",year=2026,months=[4],
                              start_date=date(2026,4,12),end_date=date(2026,4,12))
    main.jobs["background-test"]={"job_id":"background-test","total":2,"completed":0,"successful":0,
        "downloaded":0,"imported":0,"restored":0,"skipped":0,"failed":0,"logs":[],"results":[]}
    asyncio.run(main.run_job("background-test",request))
    job=main.jobs.pop("background-test")
    assert len(calls)==2
    assert all("--headless" in args for _, args in calls)
    assert calls[0][1][0:2] == ["--sites", "zaheerabad"]
    assert "--months" in calls[0][1] and "April" in calls[0][1]
    assert "--from-date" in calls[1][1] and "--to-date" in calls[1][1]
    assert job["status"]=="Completed" and job["completed"]==2 and job["imported"]==2


@pytest.mark.parametrize("payload", [
    {"sites": ["not-a-site"], "year": 2026, "months": [4]},
    {"sites": [SITES[0]], "year": 2026, "months": [13]},
    {"sites": [SITES[0], SITES[0]], "year": 2026, "months": [4]},
    {"sites": [SITES[0]], "year": 2026, "months": [4, 4]},
])
def test_download_request_rejects_invalid_or_duplicate_selection(payload):
    with pytest.raises(Exception):
        AutomationRequest(**payload)


def test_sites_endpoint_lists_configured_sites(client):
    response = client.get("/api/sites")
    assert response.status_code == 200
    assert [row["site"] for row in response.json()] == SITES


def test_charges_query_filters_selected_sites_and_aggregates(monkeypatch, client):
    seen = {}
    def fake_execute(sql, params=None):
        seen.update(sql=sql, params=params)
        return [{"period": "2026-04-01", "charges": Decimal(100), "gross": Decimal(10000),
                 "charge_percent": Decimal(1)}]
    monkeypatch.setattr(main, "execute", fake_execute)
    response = client.get("/api/dsm/charges-trend?sites=zaheerabad&sites=mandrup&view=monthwise")
    assert response.status_code == 200
    assert "SUM(dsm_charges_inr)" in seen["sql"]
    assert "date_trunc('month'" in seen["sql"]
    assert seen["params"]["sites"] == ["zaheerabad", "mandrup"]
    assert response.json()[0]["dsm_charge_percent"] == 1


def test_sitewise_aggregation_is_grouped_by_site_and_period(monkeypatch, client):
    seen = {}
    monkeypatch.setattr(main, "execute", lambda sql, params=None: seen.update(sql=sql) or [])
    response = client.get("/api/dsm/sitewise-percentage?view=monthwise")
    assert response.status_code == 200
    assert "GROUP BY site,period" in seen["sql"]
    assert "SUM(gross_revenue_inr)" in seen["sql"]


def test_daywise_query_uses_daily_group_and_inclusive_date_filters(monkeypatch, client):
    seen = {}
    monkeypatch.setattr(main, "execute", lambda sql, params=None: seen.update(sql=sql, params=params) or [])
    response = client.get("/api/dsm/charges-trend?sites=zaheerabad&start_date=2026-04-01&end_date=2026-04-30&view=daywise")
    assert response.status_code == 200
    assert "GROUP BY period" in seen["sql"]
    assert "date_trunc('month'" not in seen["sql"]
    assert seen["params"]["start_date"].isoformat() == "2026-04-01"
    assert seen["params"]["end_date"].isoformat() == "2026-04-30"


def test_revenue_api_aggregates_money_before_percent(monkeypatch, client):
    seen = {}
    monkeypatch.setattr(main, "report_record", lambda *args: {"id": 42})
    def fake_execute(sql, params=None):
        seen.update(sql=sql, params=params)
        return [{"row_count": 2, "gross": Decimal("1000"), "charges": Decimal("25")}]
    monkeypatch.setattr(main, "execute", fake_execute)
    result = client.get("/api/revenue/breakdown?site=zaheerabad&year=2026&month=10").json()
    assert "WHERE report_id=:report_id AND site=:site AND year=:year AND month=:month" in seen["sql"]
    assert seen["params"] == {"report_id": 42, "site": "zaheerabad", "year": 2026, "month": 10}
    assert result["net_revenue"] == 975
    assert result["dsm_charge_percent"] == 2.5
    assert result["net_revenue_percent"] == 97.5


def test_site_filter_rejects_unknown_values(client):
    assert client.get("/api/dsm/charges-trend?sites=unknown").status_code == 422


def test_report_unavailable_is_an_explicit_404(monkeypatch, client):
    monkeypatch.setattr(main, "report_record", lambda *args: None)
    response = client.get("/api/reports/daywise?site=zaheerabad&year=2026&month=4")
    assert response.status_code == 404
    assert "No report available" in response.json()["detail"]


def test_report_api_preserves_source_values_and_recomputes_total_percent(monkeypatch, client):
    rupee = chr(0x20B9)
    source = {header: "0" for header in REPORT_HEADERS}
    source.update({"Date": "01-04-2026", f"Gross Revenue [{rupee}]": "1,000.00",
                   f"DSM Charges [{rupee}]": "20.00", "DSM Charges [%]": "8.75",
                   "Firstband [%]": "91.25"})
    monkeypatch.setattr(main, "report_record", lambda *a: {"id": 23, "original_filename": "report.csv",
                                                           "downloaded_at": datetime(2026, 4, 30, tzinfo=timezone.utc)})
    monkeypatch.setattr(main, "execute", lambda *a, **k: [{"additional_data": source, "report_date": date(2026, 4, 1)}])
    response = client.get("/api/reports/daywise?site=zaheerabad&year=2026&month=4")
    assert response.status_code == 200
    data = response.json()
    assert [column["key"] for column in data["columns"]] == list(REPORT_HEADERS)
    assert data["rows"][0]["Date"] == "01-04-2026"
    assert data["rows"][0]["DSM Charges [%]"] == "8.75"
    assert data["totals"]["DSM Charges [%]"] == 2


def test_report_export_returns_original_bytes(monkeypatch, client):
    monkeypatch.setattr(main, "report_record", lambda *a: {"id": 5, "original_filename": "report.csv"})
    monkeypatch.setattr(main, "execute", lambda *a, **k: [{"content": b"Date,Value\r\n01-04-2026,2\r\n"}])
    response = client.get("/api/reports/daywise/export?site=zaheerabad&year=2026&month=4")
    assert response.status_code == 200
    assert response.content.startswith(b"Date,Value")
    assert "report.csv" in response.headers["content-disposition"]


def test_job_status_and_logs_are_not_exposed_for_unknown_job(client):
    assert client.get("/api/automation/status/missing").status_code == 404
    assert client.get("/api/automation/logs/missing").status_code == 404


def test_delete_preview_returns_record_and_daily_row_counts(monkeypatch, client):
    monkeypatch.setattr(main, "execute", lambda *a, **k: [{"report_count": 2, "daily_row_count": 61}])
    response = client.post("/api/reports/delete-preview", json={"scope":"monthly","site":"zaheerabad","year":2026,"month":4})
    assert response.status_code == 200
    assert response.json()["report_count"] == 2
    assert response.json()["daily_row_count"] == 61


def test_delete_requires_confirmation_keyword():
    request = DeleteRequest(scope="site", site="zaheerabad", confirmation="DELETE")
    assert request.confirmation == "DELETE"
    with pytest.raises(Exception):
        DeleteRequest(scope="all", confirmation="yes")


def test_delete_endpoint_drops_table_and_removes_records_in_transaction(monkeypatch, client):
    class Result:
        def __init__(self, rows=None, scalar=None): self.rows, self.scalar = rows or [], scalar
        def mappings(self): return self
        def all(self): return self.rows
        def scalar_one(self): return self.scalar
    class Connection:
        def __init__(self): self.statements=[]
        def execute(self, statement, params=None):
            sql=str(statement); self.statements.append(sql)
            if sql.startswith("SELECT e.id,e.site"):
                return Result([{"id":7,"site":"zaheerabad","year":2026,"month":4,
                                "period_type":"monthly","start_date":None,"end_date":None}])
            if sql.startswith("SELECT COUNT(*)"):
                return Result(scalar=30)
            return Result()
    class Engine:
        def __init__(self, connection): self.connection=connection
        def begin(self):
            from contextlib import nullcontext
            return nullcontext(self.connection)
    connection=Connection(); monkeypatch.setattr(main,"engine",Engine(connection))
    main.active_combinations.clear()
    response=client.post("/api/reports/delete",json={"scope":"monthly","site":"zaheerabad","year":2026,"month":4,"confirmation":"DELETE"})
    assert response.status_code==200
    assert response.json()["deleted_reports"]==1
    assert response.json()["deleted_daily_rows"]==30
    assert any('DROP TABLE IF EXISTS public."report_zaheerabad_2026_04"' in s for s in connection.statements)
    assert any(s.startswith("DELETE FROM excel_reports") for s in connection.statements)


def test_duplicate_active_site_month_job_is_rejected(monkeypatch, client):
    main.jobs.clear()
    main.active_combinations.clear()
    monkeypatch.setattr(main, "schedule_job", lambda *_: None)
    payload = {"sites": [SITES[0]], "year": 2026, "months": [4]}
    first = client.post("/api/automation/start", json=payload)
    second = client.post("/api/automation/start", json=payload)
    assert first.status_code == 202
    assert first.json()["total_reports"] == 1
    assert second.status_code == 409
    main.jobs.clear()
    main.active_combinations.clear()

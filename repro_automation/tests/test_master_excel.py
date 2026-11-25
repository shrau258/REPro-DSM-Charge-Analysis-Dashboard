import pandas as pd

from backend.master_excel import find_mapping_columns, latest_master_workbook, load_customer_site_rows, report_site_for_master_site


def test_master_sites_map_to_configured_report_site_ids():
    assert report_site_for_master_site("Zaheerabad") == "zaheerabad"
    assert report_site_for_master_site("Tuljapur") == "tuljapur_Amp"
    assert report_site_for_master_site("Kumbhari") == "kumbhari_solapur"
    assert report_site_for_master_site("Bhokar - II") == "bhokar"
    assert report_site_for_master_site("Nagdha") == "PGCIL_Nagda"
    assert report_site_for_master_site("Unknown location") is None


def test_master_columns_accept_newlines_and_case_variation():
    frame = pd.DataFrame(columns=["CUSTOMER NAME", "Site\n"])
    assert find_mapping_columns(frame) == ("CUSTOMER NAME", "Site\n")


def test_latest_workbook_requires_customer_and_site_columns(tmp_path):
    pd.DataFrame({"Date": ["2026-01-01"], "Gross Revenue": [10]}).to_excel(tmp_path / "report.xlsx", index=False)
    master = tmp_path / "Master latest.xlsx"
    pd.DataFrame({
        "Unused": [""], "Customer Name": ["Customer A"], "Location No.": ["L001"],
        "Site": ["Mandrup"], "Group": ["Group A"], "Site Total Capacity (MW-AC)": [50.25],
        "Capacity (MW-AC)": [1.2],
    }).to_excel(master, sheet_name="Sheet1", index=False)
    assert latest_master_workbook(tmp_path) == master
    rows, unsupported = load_customer_site_rows(master)
    assert rows == [{"customer_name": "Customer A", "master_site": "Mandrup", "report_site": "mandrup"}]
    assert unsupported == 0

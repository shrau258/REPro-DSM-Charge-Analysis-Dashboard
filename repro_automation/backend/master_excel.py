from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from decimal import Decimal, InvalidOperation

import pandas as pd

from repro_download import SITES


def normalize_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def report_site_for_master_site(value: Any) -> str | None:
    """Map Master workbook location names to the downloader's exact site ids."""
    normalized = normalize_header(value)
    aliases = {
        "zaheerabad": "zaheerabad",
        "mandrup": "mandrup",
        "turmamidi": "turmamidi",
        "tuljapur": "tuljapur_Amp",
        "tuljapuramp": "tuljapur_Amp",
        "kumbhari": "kumbhari_solapur",
        "kumbharisolapur": "kumbhari_solapur",
        "bhokar": "bhokar",
        "bhokari": "bhokar",
        "bhokarii": "bhokar",
        "nagdha": "PGCIL_Nagda",
        "nagda": "PGCIL_Nagda",
        "pgcilnagda": "PGCIL_Nagda",
        "umri": "umri",
        "karajagi": "karajagi",
        "fortunemetalsltd": "Fortune Metals Ltd",
    }
    if normalized in aliases:
        return aliases[normalized]
    for site in SITES:
        if normalize_header(site) == normalized:
            return site
    # Names on DSM charge sheets may append a group or infrastructure suffix.
    alias_matches = {report_site for alias, report_site in aliases.items()
                     if len(alias) >= 5 and alias in normalized}
    if len(alias_matches) == 1:
        return next(iter(alias_matches))
    return None


def find_mapping_columns(frame: pd.DataFrame) -> tuple[Any, Any] | None:
    columns = {normalize_header(column): column for column in frame.columns}
    customer = next((columns[key] for key in ("customername", "customer", "clientname") if key in columns), None)
    site = next((columns[key] for key in ("site", "location", "plant", "substation") if key in columns), None)
    return (customer, site) if customer is not None and site is not None else None


def latest_master_workbook(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    candidates = sorted(
        (path for path in directory.iterdir()
         if path.is_file() and path.suffix.casefold() in {".xlsx", ".xls", ".xlsm"}
         and not path.name.startswith("~$") and path.name.casefold() != "master_excel_template.xlsx"),
        key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()), reverse=True,
    )
    for path in candidates:
        try:
            # Folder sync accepts precisely the same Sheet1 B:H schema as direct upload.
            # Older Customer/Site-only spreadsheets are ignored rather than selected and
            # then failing later during a dashboard poll.
            load_master_customer_rows(path)
            return path
        except Exception:
            # A malformed or unrelated workbook must not prevent scanning the folder.
            continue
    return None


def load_customer_site_rows(path: Path) -> tuple[list[dict[str, str]], int]:
    workbook = pd.ExcelFile(path)
    try:
        for sheet in workbook.sheet_names:
            frame = pd.read_excel(workbook, sheet_name=sheet, dtype=object)
            columns = find_mapping_columns(frame)
            if not columns:
                continue
            customer_col, site_col = columns
            rows: set[tuple[str, str, str]] = set()
            unsupported = 0
            for customer_value, master_site_value in frame[[customer_col, site_col]].itertuples(index=False, name=None):
                if pd.isna(customer_value) or pd.isna(master_site_value):
                    continue
                customer = str(customer_value).strip()
                master_site = str(master_site_value).strip()
                if not customer or not master_site:
                    continue
                report_site = report_site_for_master_site(master_site)
                if report_site is None:
                    unsupported += 1
                    continue
                rows.add((customer, master_site, report_site))
            if not rows:
                raise ValueError("Workbook contains Customer and Site columns, but no locations match configured dashboard sites")
            return [
                {"customer_name": customer, "master_site": master_site, "report_site": report_site}
                for customer, master_site, report_site in sorted(rows, key=lambda row: (row[0].casefold(), row[1].casefold()))
            ], unsupported
    finally:
        workbook.close()
    raise ValueError("No worksheet contains recognizable Customer Name and Site columns")


def _cell_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _capacity(value: Any, field_name: str) -> Decimal | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {field_name} value: {value}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field_name} must be a non-negative number")
    return number


def load_master_customer_rows(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    """Read the B:G Master layout, with an optional DSM-name column at E."""
    try:
        frame = pd.read_excel(path, sheet_name="Sheet1", dtype=object)
    except ValueError as exc:
        raise ValueError("Master workbook must contain a worksheet named Sheet1") from exc
    if frame.shape[1] < 7:
        raise ValueError("Sheet1 must contain columns A through G, with Customer, Location, Site, Group, and both AC capacity fields in B:G")

    headers = [normalize_header(value) for value in frame.columns]
    customer_headers = {"customername", "customer", "clientname"}
    location_headers = {"locationno", "locationnumber", "locationid"}
    site_headers = {"site", "sitename"}
    dsm_headers = {"nameondsmcharges", "dsmchargename"}
    group_headers = {"group", "groupname"}
    site_capacity_headers = {"sitetotalcapacitymwac", "sitetotalcapacityacmw", "sitetotacapacitymwac"}
    customer_capacity_headers = {
        "capacitymwaccustomercapacity", "customercapacitymwac", "capacitymwaccustomer", "capacitymwac",
    }
    if not (headers[1] in customer_headers and headers[2] in location_headers and headers[3] in site_headers):
        raise ValueError("Sheet1 columns B:D must be Customer Name, Location No., and Site.")

    has_dsm_name = headers[4] in dsm_headers
    if has_dsm_name:
        if frame.shape[1] < 8 or not (
            headers[5] in group_headers and headers[6] in site_capacity_headers
            and headers[7] in customer_capacity_headers
        ):
            raise ValueError(
                "With optional Name on DSM charges in column E, Sheet1 columns F:H must be "
                "Group, Site Total Capacity (MW-AC), and Capacity (MW-AC)."
            )
        dsm_index, group_index, site_capacity_index, customer_capacity_index = 4, 5, 6, 7
    else:
        if not (
            headers[4] in group_headers and headers[5] in site_capacity_headers
            and headers[6] in customer_capacity_headers
        ):
            raise ValueError(
                "Sheet1 columns E:G must be Group, Site Total Capacity (MW-AC), and Capacity (MW-AC). "
                "Name on DSM charges in column E is optional; when present, those fields move to F:H."
            )
        dsm_index, group_index, site_capacity_index, customer_capacity_index = None, 4, 5, 6

    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    unmapped = 0
    duplicate_rows = 0
    for row_number, source_row in enumerate(frame.itertuples(index=False, name=None), start=2):
        customer_value, location_value, site_value = source_row[1], source_row[2], source_row[3]
        dsm_site_value = source_row[dsm_index] if dsm_index is not None else None
        group_value = source_row[group_index]
        site_cap_value = source_row[site_capacity_index]
        customer_cap_value = source_row[customer_capacity_index]
        if all(pd.isna(value) or str(value).strip() == "" for value in
               (customer_value, location_value, site_value, dsm_site_value, group_value, site_cap_value, customer_cap_value)):
            continue
        customer = _cell_text(customer_value)
        location_no = _cell_text(location_value)
        site_name = _cell_text(site_value)
        dsm_site = _cell_text(dsm_site_value)
        if not customer and not site_name:
            # Ignore non-record footer values (for example workbook totals).
            continue
        if not customer or not site_name:
            raise ValueError(f"Master row {row_number} must include Customer Name (B) and Site (D)")
        # Prefer the explicit DSM label when supplied; otherwise map the Site column.
        report_site = report_site_for_master_site(dsm_site or site_name)
        if report_site is None:
            unmapped += 1
        row = {
            # Some non-report rows in the supplied workbook have no Location No.; use
            # that customer's normalized site as a stable identity for those rows.
            "customer_key": customer.casefold(),
            "site_key": site_name.casefold(),
            "location_key": location_no.casefold() if location_no else f"site:{normalize_header(site_name)}",
            "customer_name": customer, "location_no": location_no, "site_name": site_name,
            "name_on_dsm_charges": dsm_site or None, "group_name": _cell_text(group_value) or None,
            "site_total_capacity_mw_ac": _capacity(site_cap_value, "Site Total Capacity (MW-AC)"),
            "customer_capacity_mw_ac": _capacity(customer_cap_value, "Customer Capacity (MW-AC)"),
            "report_site": report_site,
        }
        key = (row["customer_key"], row["site_key"], row["location_key"])
        prior = records.get(key)
        if prior:
            duplicate_rows += 1
            conflicts = [field for field in row if field != "customer_capacity_mw_ac" and prior[field] != row[field]]
            if conflicts:
                raise ValueError(
                    f"Duplicate customer/location record has conflicting {', '.join(conflicts)} "
                    f"at Master row {row_number}: {customer} / {location_no or site_name}"
                )
            previous_capacity = prior["customer_capacity_mw_ac"]
            current_capacity = row["customer_capacity_mw_ac"]
            if previous_capacity is None and current_capacity is not None:
                prior["customer_capacity_mw_ac"] = current_capacity
            elif previous_capacity == 0 and current_capacity not in (None, 0):
                # The workbook repeats some location rows with a zero-capacity line.
                # Keep the populated allocation once instead of counting it twice.
                prior["customer_capacity_mw_ac"] = current_capacity
            elif current_capacity in (None, 0):
                continue
            elif current_capacity != previous_capacity:
                raise ValueError(
                    f"Duplicate customer/location record has conflicting customer capacity "
                    f"at Master row {row_number}: {customer} / {location_no or site_name}"
                )
        else:
            records[key] = row
    if not records:
        raise ValueError("Sheet1 contains no usable customer/location rows")
    return list(records.values()), unmapped, duplicate_rows

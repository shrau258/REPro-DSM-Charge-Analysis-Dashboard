from decimal import Decimal, InvalidOperation


def revenue_metrics(gross, charges):
    """Calculate percentages from aggregated amounts, never from source percentages."""
    if gross is None or charges is None:
        return {"gross_revenue": gross, "dsm_charges": charges, "net_revenue": None,
                "dsm_charge_percent": None, "net_revenue_percent": None}
    try:
        gross = Decimal(str(gross))
        charges = Decimal(str(charges))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Revenue amounts must be decimal numbers") from exc
    if not gross:
        return {"gross_revenue": gross, "dsm_charges": charges, "net_revenue": gross - charges,
                "dsm_charge_percent": None, "net_revenue_percent": None}
    charge_pct = charges / gross * Decimal(100)
    return {"gross_revenue": gross, "dsm_charges": charges, "net_revenue": gross - charges,
            "dsm_charge_percent": charge_pct, "net_revenue_percent": Decimal(100) - charge_pct}

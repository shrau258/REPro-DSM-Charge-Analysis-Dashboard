from __future__ import annotations

from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, model_validator

from repro_download import SITES


class MasterExcelSettingsRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=2048)


class AutomationRequest(BaseModel):
    sites: list[str] = Field(min_length=1, max_length=len(SITES))
    period_type: Literal["monthly", "custom", "both"] = "monthly"
    year: int | None = Field(default=None, ge=2000, le=2100)
    months: list[int] = Field(default_factory=list, max_length=12)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_selection(self):
        if len(set(self.sites)) != len(self.sites) or any(s not in SITES for s in self.sites):
            raise ValueError("Choose unique sites from the configured site list")
        if len(set(self.months)) != len(self.months) or any(m < 1 or m > 12 for m in self.months):
            raise ValueError("Months must be unique integers from 1 through 12")
        has_monthly = self.period_type in {"monthly", "both"}
        has_custom = self.period_type in {"custom", "both"}
        if has_monthly and (self.year is None or not self.months):
            raise ValueError("Monthly downloads require a year and one or more months")
        if not has_monthly and (self.year is not None or self.months):
            raise ValueError("Do not send year/month fields for custom-date-only downloads")
        if has_custom:
            if self.start_date is None or self.end_date is None:
                raise ValueError("Custom downloads require a start and end date")
            if self.start_date > self.end_date:
                raise ValueError("start_date must be on or before end_date")
            if self.end_date > date.today():
                raise ValueError("end_date cannot be in the future")
        elif self.start_date is not None or self.end_date is not None:
            raise ValueError("Do not send custom dates for monthly-only downloads")
        return self


class DeleteSelection(BaseModel):
    scope: Literal["all", "site", "monthly", "custom"]
    site: str | None = None
    year: int | None = Field(default=None, ge=2000, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.scope == "all":
            if any((self.site, self.year, self.month, self.start_date, self.end_date)):
                raise ValueError("Delete all does not accept site or period filters")
        elif self.site not in SITES:
            raise ValueError("Choose an exact configured site")
        elif self.scope == "site":
            if any((self.year, self.month, self.start_date, self.end_date)):
                raise ValueError("Delete site removes all periods for that site")
        elif self.scope == "monthly":
            if self.year is None or self.month is None or self.start_date or self.end_date:
                raise ValueError("Monthly deletion requires site, year, and month")
        else:
            if self.start_date is None or self.end_date is None or self.year or self.month:
                raise ValueError("Custom deletion requires site and exact start/end dates")
            if self.start_date > self.end_date:
                raise ValueError("start_date must be on or before end_date")
        return self


class DeleteRequest(DeleteSelection):
    confirmation: Literal["DELETE"]

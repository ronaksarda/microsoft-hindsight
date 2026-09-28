"""Machine-enforceable grant rules as a pydantic discriminated union."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.jurisdiction import expand_codes

CANONICAL_CATEGORIES = [
    "Travel",
    "Contractor",
    "Compute",
    "Equipment",
    "Personnel",
    "Supplies",
    "Software",
    "Services",
    "Entertainment",
    "Alcohol",
    "General",
]

_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("Travel", ("flight", "airfare", "airline", "hotel", "lodging", "travel", "train", "taxi", "per diem", "uber")),
    ("Contractor", ("contract", "consultant", "freelance", "outsourc", "agency fee", "vendor services")),
    ("Compute", ("gpu", "cloud", "compute", "aws", "azure", "gcp", "instance", "cluster", "hosting")),
    ("Equipment", ("equipment", "hardware", "laptop", "server", "device", "instrument", "microscope")),
    ("Software", ("software", "license", "licence", "saas", "subscription")),
    ("Personnel", ("salary", "payroll", "stipend", "wage", "fringe")),
    ("Supplies", ("supplies", "consumable", "reagent", "materials")),
    ("Entertainment", ("party", "entertainment", "gift")),
    ("Services", ("service", "maintenance", "repair", "calibration", "legal", "accounting", "translation")),
    ("Alcohol", ("alcohol", "wine", "beer", "liquor")),
]


_SYNONYMS = {
    "consultant": "Contractor",
    "consultants": "Contractor",
    "consulting": "Contractor",
    "contractors": "Contractor",
    "subcontract": "Contractor",
    "subcontracts": "Contractor",
    "subcontractor": "Contractor",
    "subcontractors": "Contractor",
    "subcontracting": "Contractor",
    "contractual": "Contractor",
    "salaries": "Personnel",
    "salary": "Personnel",
    "wages": "Personnel",
    "staff": "Personnel",
    "payroll": "Personnel",
    "cloud": "Compute",
    "computing": "Compute",
    "hardware": "Equipment",
    "materials": "Supplies",
    "alcoholic beverages": "Alcohol",
    "beverages": "Alcohol",
    "trips": "Travel",
    "airfare": "Travel",
}


def normalize_category(value: str | None) -> str:
    if not value or not value.strip():
        return "General"
    v = value.strip()
    if v.lower() in _SYNONYMS:
        return _SYNONYMS[v.lower()]
    for c in CANONICAL_CATEGORIES:
        if c.lower() == v.lower():
            return c
    return v[:1].upper() + v[1:]


def infer_category(text: str) -> str:
    """Keyword classifier used only when the caller does not supply a category."""
    t = text.lower()
    for cat, kws in _CATEGORY_KEYWORDS:
        if any(k in t for k in kws):
            return cat
    return "General"


def normalize_vendor(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.casefold())).strip()


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


class RollingWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["rolling"] = "rolling"
    days: int = Field(gt=0, le=3660)


class FiscalWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["fiscal"] = "fiscal"
    period: Literal["month", "quarter", "year"]
    start_month: int = Field(default=1, ge=1, le=12, description="First month of the fiscal year")


Window = Annotated[RollingWindow | FiscalWindow, Field(discriminator="kind")]


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


class _RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    clause: str = Field(default="", max_length=120, description="Human reference, e.g. 'Clause 4.2'")
    description: str = Field(default="", max_length=1000)
    severity: Literal["blocking", "warning"] = "blocking"

    @property
    def label(self) -> str:
        return self.clause or self.id


class _CapMixin(BaseModel):
    cap: float = Field(gt=0)
    window: Window | None = None
    warn_at_pct: float = Field(default=80.0, gt=0, le=100, description="Early-warning threshold (% of cap)")


class CategoryCapRule(_RuleBase, _CapMixin):
    type: Literal["category_cap"] = "category_cap"
    category: str | None = Field(default=None, description="None caps total spend across all categories")

    @field_validator("category")
    @classmethod
    def _norm(cls, v: str | None) -> str | None:
        return normalize_category(v) if v else None


class SpenderCapRule(_RuleBase, _CapMixin):
    type: Literal["spender_cap"] = "spender_cap"
    spender: str | None = Field(default=None, description="Member id or name; None applies to each spender")
    category: str | None = None

    @field_validator("category")
    @classmethod
    def _norm(cls, v: str | None) -> str | None:
        return normalize_category(v) if v else None


class VendorCapRule(_RuleBase, _CapMixin):
    type: Literal["vendor_cap"] = "vendor_cap"
    vendor: str | None = Field(default=None, description="Vendor name; None applies to each vendor")
    category: str | None = None

    @field_validator("category")
    @classmethod
    def _norm(cls, v: str | None) -> str | None:
        return normalize_category(v) if v else None


class JurisdictionRule(_RuleBase):
    type: Literal["jurisdiction"] = "jurisdiction"
    allowed_countries: list[str] | None = None
    blocked_countries: list[str] | None = None
    categories: list[str] | None = Field(default=None, description="Scope; None applies to every category")
    waivable_with_prior_approval: bool = True

    @model_validator(mode="after")
    def _one_list(self) -> JurisdictionRule:
        if bool(self.allowed_countries) == bool(self.blocked_countries):
            raise ValueError("set exactly one of allowed_countries or blocked_countries")
        expand_codes(self.allowed_countries or self.blocked_countries or [])
        if self.categories:
            self.categories = [normalize_category(c) for c in self.categories]
        return self


class PriorApprovalRule(_RuleBase):
    type: Literal["prior_approval"] = "prior_approval"
    threshold: float = Field(ge=0, description="Amounts strictly above this need a prior_approval_ref")
    categories: list[str] | None = None

    @field_validator("categories")
    @classmethod
    def _norm(cls, v: list[str] | None) -> list[str] | None:
        return [normalize_category(c) for c in v] if v else None


class BlockedListRule(_RuleBase):
    type: Literal["blocked_list"] = "blocked_list"
    vendors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _non_empty(self) -> BlockedListRule:
        if not self.vendors and not self.categories:
            raise ValueError("blocked_list needs vendors or categories")
        self.categories = [normalize_category(c) for c in self.categories]
        return self


class AllowedCategoriesRule(_RuleBase):
    type: Literal["allowed_categories"] = "allowed_categories"
    categories: list[str] = Field(min_length=1)

    @field_validator("categories")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return [normalize_category(c) for c in v]


class GrantPeriodRule(_RuleBase):
    type: Literal["grant_period"] = "grant_period"
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self) -> GrantPeriodRule:
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


Rule = Annotated[
    CategoryCapRule
    | SpenderCapRule
    | VendorCapRule
    | JurisdictionRule
    | PriorApprovalRule
    | BlockedListRule
    | AllowedCategoriesRule
    | GrantPeriodRule,
    Field(discriminator="type"),
]

CapRule = Union[CategoryCapRule, SpenderCapRule, VendorCapRule]

RuleListAdapter: TypeAdapter[list[Rule]] = TypeAdapter(list[Rule])
RuleAdapter: TypeAdapter[Rule] = TypeAdapter(Rule)


def parse_rules(raw: list[dict] | None) -> list[Rule]:
    rules = RuleListAdapter.validate_python(raw or [])
    ids = [r.id for r in rules]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate rule ids: {sorted(dupes)}")
    return rules


# --------------------------------------------------------------------------- #
# Text clause compiler (legacy grants that only carry free-text rules)
# --------------------------------------------------------------------------- #

_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(k)?", re.IGNORECASE)


def compile_text_rules(texts: list[str], home_country: str = "US") -> list[Rule]:
    """Best-effort, conservative translation of free-text clauses.

    Only two well-understood shapes are compiled: "<category> ... capped at $X" and
    "no foreign contractor ...". Anything else stays text-only and is left to the
    advisory LLM layer.
    """
    out: list[Rule] = []
    for i, text in enumerate(texts):
        low = text.lower()
        clause_match = re.match(r"\s*((?:clause|article|section)\s+[\w.]+)", text, re.IGNORECASE)
        clause = clause_match.group(1) if clause_match else f"Rule {i + 1}"
        rid = f"compiled_{i + 1}"
        if "foreign" in low and "contractor" in low:
            out.append(
                JurisdictionRule(
                    id=rid,
                    clause=clause,
                    description=text,
                    allowed_countries=[home_country],
                    categories=["Contractor"],
                    waivable_with_prior_approval="approval" in low or "waiver" in low,
                )
            )
            continue
        m = _MONEY.search(text)
        if m and ("cap" in low or "limit" in low or "maximum" in low):
            amount = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
            cat = infer_category(low)
            out.append(
                CategoryCapRule(
                    id=rid,
                    clause=clause,
                    description=text,
                    cap=amount,
                    category=None if cat == "General" else cat,
                )
            )
    return out

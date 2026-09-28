"""Location normalisation: free text such as "Oslo, Norway" to an ISO 3166-1 alpha-2 code.

The resolver never guesses. It returns ``ambiguous`` when the text names no
country, names several, or uses a token with more than one meaning (for
example "Georgia", or "CA" which is both Canada and California).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# ISO2 -> names and aliases (lower case). Not exhaustive; unknown countries resolve as ambiguous.
COUNTRIES: dict[str, list[str]] = {
    "US": ["united states", "united states of america", "usa", "u.s.", "u.s.a.", "us", "america"],
    "CA": ["canada"],
    "MX": ["mexico"],
    "BR": ["brazil", "brasil"],
    "AR": ["argentina"],
    "CL": ["chile"],
    "CO": ["colombia"],
    "PE": ["peru"],
    "GB": ["united kingdom", "uk", "u.k.", "great britain", "britain", "england", "scotland", "wales",
           "northern ireland"],
    "IE": ["ireland", "republic of ireland"],
    "FR": ["france"],
    "DE": ["germany", "deutschland"],
    "NL": ["netherlands", "the netherlands", "holland"],
    "BE": ["belgium"],
    "LU": ["luxembourg"],
    "CH": ["switzerland"],
    "AT": ["austria"],
    "IT": ["italy", "italia"],
    "ES": ["spain", "espana", "españa"],
    "PT": ["portugal"],
    "NO": ["norway", "norge"],
    "SE": ["sweden", "sverige"],
    "DK": ["denmark", "danmark"],
    "FI": ["finland", "suomi"],
    "IS": ["iceland"],
    "PL": ["poland", "polska"],
    "CZ": ["czech republic", "czechia"],
    "SK": ["slovakia"],
    "HU": ["hungary"],
    "RO": ["romania"],
    "BG": ["bulgaria"],
    "GR": ["greece"],
    "HR": ["croatia"],
    "SI": ["slovenia"],
    "EE": ["estonia"],
    "LV": ["latvia"],
    "LT": ["lithuania"],
    "UA": ["ukraine"],
    "RU": ["russia", "russian federation"],
    "BY": ["belarus"],
    "TR": ["turkey", "türkiye", "turkiye"],
    "IL": ["israel"],
    "AE": ["united arab emirates", "uae", "u.a.e."],
    "SA": ["saudi arabia"],
    "QA": ["qatar"],
    "EG": ["egypt"],
    "IR": ["iran"],
    "IQ": ["iraq"],
    "SY": ["syria"],
    "KP": ["north korea", "dprk"],
    "KR": ["south korea", "korea", "republic of korea"],
    "JP": ["japan"],
    "CN": ["china", "people's republic of china", "prc"],
    "HK": ["hong kong"],
    "TW": ["taiwan"],
    "SG": ["singapore"],
    "MY": ["malaysia"],
    "TH": ["thailand"],
    "VN": ["vietnam", "viet nam"],
    "PH": ["philippines"],
    "ID": ["indonesia"],
    "IN": ["india"],
    "PK": ["pakistan"],
    "BD": ["bangladesh"],
    "LK": ["sri lanka"],
    "AU": ["australia"],
    "NZ": ["new zealand"],
    "ZA": ["south africa"],
    "NG": ["nigeria"],
    "KE": ["kenya"],
    "GH": ["ghana"],
    "MA": ["morocco"],
    "GE": ["georgia"],  # also a US state; handled as ambiguous below
    "CU": ["cuba"],
    "VE": ["venezuela"],
}

# Groupings usable in rules ("EU" expands to member states).
REGIONS: dict[str, set[str]] = {
    "EU": {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
           "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"},
}

US_STATES: dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}

_ALIAS: dict[str, set[str]] = {}
for _code, _names in COUNTRIES.items():
    for _n in _names:
        _ALIAS.setdefault(_n, set()).add(_code)
for _abbr, _state in US_STATES.items():
    _ALIAS.setdefault(_state, set()).add("US")
_ALIAS.setdefault("washington dc", set()).add("US")
# "washington" alone is a state and a city; both are in the US, so it stays unambiguous.

_MAX_WORDS = max(len(k.split()) for k in _ALIAS)
_SPLIT = re.compile(r"[,;/|()\[\]]+|\s+-\s+")


@dataclass
class LocationResult:
    raw: str
    status: Literal["resolved", "ambiguous", "unknown", "empty"]
    country: str | None = None
    candidates: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "input": self.raw,
            "status": self.status,
            "country": self.country,
            "candidates": self.candidates,
            "reason": self.reason,
        }


def _two_letter_candidates(token: str) -> set[str]:
    """Upper-case 2-letter tokens: ISO code and/or US state abbreviation."""
    out: set[str] = set()
    if token in COUNTRIES:
        out.add(token)
    if token in US_STATES:
        out.add("US")
    return out


def _segment_matches(segment: str) -> list[set[str]]:
    """Return candidate sets for every country mention inside one segment."""
    matches: list[set[str]] = []
    raw_words = re.findall(r"[A-Za-zÀ-ÿ.'’]+", segment)
    words = [w.strip(".").lower() if w.lower() not in _ALIAS else w.lower() for w in raw_words]
    i = 0
    while i < len(words):
        hit = False
        for n in range(min(_MAX_WORDS, len(words) - i), 0, -1):
            phrase = " ".join(words[i : i + n])
            raw_phrase = " ".join(w.lower() for w in raw_words[i : i + n])
            key = phrase if phrase in _ALIAS else raw_phrase if raw_phrase in _ALIAS else None
            if key is None:
                continue
            tok = raw_words[i].strip(".")
            short = n == 1 and len(tok) == 2
            # Bare 2-letter tokens count when upper case ("US") or when they are the whole segment ("us").
            if short and not tok.isupper() and len(raw_words) != 1:
                continue
            cands = set(_ALIAS[key])
            if short:
                cands |= _two_letter_candidates(tok.upper())
            matches.append(cands)
            i += n
            hit = True
            break
        if not hit:
            token = raw_words[i]
            if len(token) == 2 and token.isupper():
                cands = _two_letter_candidates(token)
                if cands:
                    matches.append(cands)
            i += 1
    return matches


def resolve_location(text: str) -> LocationResult:
    raw = text or ""
    if not raw.strip():
        return LocationResult(raw=raw, status="empty", reason="No location provided")

    mentions: list[set[str]] = []
    for seg in _SPLIT.split(raw):
        if seg.strip():
            mentions.extend(_segment_matches(seg))

    if not mentions:
        return LocationResult(
            raw=raw, status="unknown", reason="No recognisable country. Use the 'City, Country' format."
        )

    definite = {next(iter(m)) for m in mentions if len(m) == 1}
    if len(definite) > 1:
        return LocationResult(
            raw=raw, status="ambiguous", candidates=sorted(definite), reason="Location names more than one country"
        )
    if len(definite) == 1:
        country = next(iter(definite))
        # Every ambiguous mention must be consistent with the definite one.
        if all(country in m for m in mentions):
            return LocationResult(raw=raw, status="resolved", country=country, candidates=[country])
        extra = sorted(set().union(*mentions))
        return LocationResult(raw=raw, status="ambiguous", candidates=extra, reason="Conflicting country mentions")

    union = sorted(set().union(*mentions))
    common = set.intersection(*mentions)
    if len(common) == 1:
        c = next(iter(common))
        return LocationResult(raw=raw, status="resolved", country=c, candidates=[c])
    return LocationResult(
        raw=raw,
        status="ambiguous",
        candidates=union,
        reason=f"'{raw.strip()}' could refer to {', '.join(union)}; specify the country explicitly",
    )


def expand_codes(codes: list[str]) -> set[str]:
    """Expand rule country lists: ISO codes, aliases, or region names such as "EU"."""
    out: set[str] = set()
    for c in codes:
        key = c.strip()
        if key.upper() in REGIONS:
            out |= REGIONS[key.upper()]
        elif key.upper() in COUNTRIES:
            out.add(key.upper())
        elif key.lower() in _ALIAS and len(_ALIAS[key.lower()]) == 1:
            out |= _ALIAS[key.lower()]
        else:
            raise ValueError(f"Unknown country or region code: {c!r}")
    return out

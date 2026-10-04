"""Automatic toll-rate updater.

Pipeline:  fetch sources -> text -> parse tables -> validate -> write overlay file.

* Official NHA pages / notification PDFs are trusted. Secondary (news/guide) pages are
  only used when the official sources yield nothing new (or when 2+ of them agree).
* Every candidate is sanity-checked (price ordering, size of change vs. current rate,
  not older than what we already have). Bad data is rejected and the last good rates stay.
* Results go to `rates_live.json` (overlay) - `tariffs.json` is never modified.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from .calculator import DATA_DIR, TollCalculator

SOURCES_FILE = Path(__file__).parent / "data" / "sources.json"
VEHICLE_ORDER = ["car", "wagon", "coaster", "bus", "truck", "articulated_truck"]
MIN_RATIO, MAX_RATIO = 0.5, 1.75  # allowed new/old per-class rate change

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


# ------------------------------------------------------------------ HTML -> text
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._link_text).strip()))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data.strip())


def html_to_text(html: str) -> tuple[str, list[tuple[str, str]]]:
    p = _TextExtractor()
    p.feed(html)
    text = re.sub(r"[ \t\xa0]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n+", "\n", text), p.links


def pdf_to_text(data: bytes) -> str:
    import io

    from pypdf import PdfReader  # lazy import

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# ------------------------------------------------------------------ parsing
ROAD_TOKENS = re.compile(
    r"(?P<m>\bM[- ]?(?P<mn>\d{1,2})\b)|(?P<e>\bE[- ]?(?P<en>35|75)\b)|(?P<n>\bN[- ]?55\b)"
    r"|(?P<nh>\bnational\s+highways?\b)|(?P<koh>\bkohat\s+tunnel\b)|(?P<imd>\bIMDCW\b)",
    re.I,
)


def road_ids_in(line: str) -> list[str]:
    ids: list[str] = []
    for m in ROAD_TOKENS.finditer(line):
        if m.group("m"):
            rid = f"M-{int(m.group('mn'))}"
        elif m.group("e"):
            rid = f"E-{m.group('en')}"
        elif m.group("n") or m.group("koh"):
            rid = "N-55"
        elif m.group("imd"):
            rid = "E-75"
        else:
            rid = "NH"
        if not ids or ids[-1] != rid:
            ids.append(rid)
    return ids


VEHICLE_ROWS = [
    (re.compile(r"^(articulated|trailer)", re.I), "articulated_truck"),
    (re.compile(r"^(13\b|coaster|mini)", re.I), "coaster"),
    (re.compile(r"^wagon", re.I), "wagon"),
    (re.compile(r"^(2\s*(&|and)\s*3|trucks?\b)", re.I), "truck"),
    (re.compile(r"^bus", re.I), "bus"),
    (re.compile(r"^(cars?\b|jeep)", re.I), "car"),
]


def _vehicle_of(label: str) -> str | None:
    for rx, key in VEHICLE_ROWS:
        if rx.match(label):
            return key
    return None


def parse_effective_date(text: str) -> str | None:
    found: list[date] = []
    for m in re.finditer(
        r"(?:effective|w\.?\s?e\.?\s?f\.?)[^0-9]{0,25}(\d{1,2})(?:st|nd|rd|th)?[\s,.\-]+([A-Za-z]{3,9})[\s,.\-]+(\d{4})",
        text, re.I):
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            try:
                found.append(date(int(m.group(3)), mon, int(m.group(1))))
            except ValueError:
                pass
    for m in re.finditer(r"(?:effective|w\.?\s?e\.?\s?f\.?)[^0-9]{0,25}(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text, re.I):
        try:
            found.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    return max(found).isoformat() if found else None


def parse_rates(text: str) -> dict[str, dict[str, int]]:
    """Extract {road_id: {vehicle: rate}} from table-like text."""
    text = re.sub(r"\bRs\.?\s*", "", text, flags=re.I)
    result: dict[str, dict[str, int]] = defaultdict(dict)
    header: list[str] = []
    pick_last = False

    for raw in text.splitlines():
        line = raw.strip(" \t|·-*#>")
        if not line:
            continue
        veh = _vehicle_of(line)
        if veh is None:
            ids = road_ids_in(line)
            if ids:
                header, pick_last = ids, False
            if re.search(r"\bold\b.*\bnew\b", line, re.I):
                pick_last = True
            continue

        tokens = [t for t in re.split(r"[\s|·]+", line) if t]
        nums: list[int] = []
        for tok in reversed(tokens):
            if re.fullmatch(r"\d[\d,]*", tok):
                nums.append(int(tok.replace(",", "")))
            else:
                break
        nums.reverse()
        if not nums or not header:
            continue
        if pick_last and len(header) == 1:
            result[header[0]].setdefault(veh, nums[-1])
        elif len(nums) == len(header):
            for rid, val in zip(header, nums):
                result[rid].setdefault(veh, val)
    return {k: v for k, v in result.items() if v}


# ------------------------------------------------------------------ optional LLM fallback
def llm_extract(text: str) -> tuple[dict[str, dict[str, int]], str | None]:
    """Used only if the table parser finds nothing and ANTHROPIC_API_KEY is set."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return {}, None
    import httpx

    prompt = (
        "Extract Pakistan NHA toll rates (PKR, M-Tag/standard rate, NOT penalty rates) from the text below. "
        "Reply with ONLY JSON: {\"effective_from\": \"YYYY-MM-DD\"|null, \"roads\": {ROAD_ID: {vehicle: rate}}}. "
        "ROAD_ID in [M-1,M-2,M-3,M-4,M-5,M-9,M-14,E-35,E-75,N-55,NH]. "
        "vehicle in [car,wagon,coaster,bus,truck,articulated_truck]. Omit anything not present.\n\n" + text[:30000]
    )
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": os.getenv("TOLL_LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 2000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    body = "".join(b.get("text", "") for b in r.json()["content"])
    data = json.loads(re.sub(r"^```(?:json)?|```$", "", body.strip(), flags=re.M).strip())
    roads = {rid: {v: int(x) for v, x in rates.items()} for rid, rates in data.get("roads", {}).items()}
    return roads, data.get("effective_from")


# ------------------------------------------------------------------ fetching
def default_fetch(url: str) -> tuple[str, bytes]:
    import httpx

    r = httpx.get(url, timeout=30, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (compatible; PKTollRateBot/1.0)"})
    r.raise_for_status()
    return r.headers.get("content-type", ""), r.content


def to_text(ctype: str, content: bytes, url: str) -> tuple[str, list[tuple[str, str]]]:
    if "pdf" in ctype.lower() or url.lower().endswith(".pdf") or content[:4] == b"%PDF":
        return pdf_to_text(content), []
    return html_to_text(content.decode("utf-8", errors="replace"))


# ------------------------------------------------------------------ candidates & validation
@dataclass
class Candidate:
    road: str
    rates: dict[str, int]
    effective: str | None
    source: str
    trust: str  # "official" | "secondary"


@dataclass
class Report:
    started_at: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    applied: dict[str, Any] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate(road: str, new: dict[str, int], current: dict[str, int]) -> str | None:
    """Return a rejection reason, or None if the candidate looks sane."""
    prev = None
    for v in VEHICLE_ORDER:
        if v not in new:
            continue
        x = new[v]
        if not 10 <= x <= 50000:
            return f"{v}={x} out of range"
        if prev is not None and x < prev[1]:
            return f"{v}={x} is below {prev[0]}={prev[1]} (ordering broken)"
        prev = (v, x)
        old = current.get(v)
        if old and not MIN_RATIO <= x / old <= MAX_RATIO:
            return f"{v}: {old} -> {x} is an implausible change"
    if "car" not in new:
        return "no car rate found"
    return None


def gather_candidates(sources: dict[str, Any], fetch: Callable, report: Report) -> list[Candidate]:
    cands: list[Candidate] = []
    seen: set[str] = set()

    def handle(url: str, trust: str, follow: bool):
        if url in seen:
            return
        seen.add(url)
        try:
            ctype, content = fetch(url)
            text, links = to_text(ctype, content, url)
        except Exception as e:  # network / parsing problems must never crash the app
            report.errors.append(f"{url}: {e.__class__.__name__}: {e}")
            report.sources.append({"url": url, "ok": False})
            return
        roads = parse_rates(text)
        eff = parse_effective_date(text)
        method = "table_parser"
        if not roads:
            try:
                roads, llm_eff = llm_extract(text)
                eff, method = eff or llm_eff, "llm"
            except Exception as e:
                report.errors.append(f"{url}: llm fallback failed: {e}")
        report.sources.append({"url": url, "ok": True, "trust": trust, "roads": sorted(roads), "effective": eff,
                               "method": method if roads else "none"})
        for rid, rates in roads.items():
            cands.append(Candidate(rid, rates, eff, url, trust))
        if follow:
            kws = [k.lower() for k in sources.get("link_keywords", ["toll"])]
            n = 0
            for href, label in links:
                if n >= sources.get("max_documents", 8):
                    break
                if any(k in (label + " " + href).lower() for k in kws):
                    handle(urljoin(url, href), "official", False)
                    n += 1

    for url in sources.get("official_pages", []):
        handle(url, "official", True)
    for url in sources.get("secondary_pages", []):
        handle(url, "secondary", False)
    return cands


def choose(cands: list[Candidate], allow_single_secondary: bool) -> Candidate | None:
    official = [c for c in cands if c.trust == "official"]
    if official:
        return max(official, key=lambda c: c.effective or "")
    groups: dict[tuple, list[Candidate]] = defaultdict(list)
    for c in cands:
        groups[tuple(sorted(c.rates.items()))].append(c)
    best = max(groups.values(), key=lambda g: (len(g), max(c.effective or "" for c in g)), default=None)
    if best and (len(best) >= 2 or allow_single_secondary):
        return max(best, key=lambda c: c.effective or "")
    return None


# ------------------------------------------------------------------ main entry
def run_update(
    calc: TollCalculator,
    sources: dict[str, Any] | None = None,
    fetch: Callable[[str], tuple[str, bytes]] = default_fetch,
    data_dir: Path = DATA_DIR,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    report = Report(started_at=now.isoformat(timespec="seconds"))
    sources = sources or json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    allow_single = os.getenv("TOLL_ALLOW_SINGLE_SOURCE", "true").lower() == "true"

    by_road: dict[str, list[Candidate]] = defaultdict(list)
    for c in gather_candidates(sources, fetch, report):
        if c.road in calc.roads:
            by_road[c.road].append(c)

    overlay_path = Path(data_dir) / "rates_live.json"
    overlay = json.loads(overlay_path.read_text()) if overlay_path.exists() else {"roads": {}}
    changed = False

    for rid, cs in by_road.items():
        pick = choose(cs, allow_single)
        if pick is None:
            continue
        road = calc.roads[rid]
        if pick.effective and pick.effective < road.effective_from:
            continue  # older than what we already have
        merged = {**road.rates, **pick.rates}
        if all(road.rates.get(v) == x for v, x in pick.rates.items()):
            report.unchanged.append(rid)
            continue
        reason = validate(rid, pick.rates, road.rates)
        if reason:
            report.rejected.append({"road": rid, "reason": reason, "source": pick.source, "rates": pick.rates})
            continue
        overlay["roads"][rid] = {
            "rates": merged,
            "effective_from": pick.effective or date.today().isoformat(),
            "verified": pick.trust == "official",
            "source": pick.source,
            "updated_at": report.started_at,
        }
        report.applied[rid] = {"old": road.rates, "new": merged, "source": pick.source, "trust": pick.trust}
        changed = True

    if changed:
        overlay["updated_at"] = report.started_at
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = overlay_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(overlay, indent=2), encoding="utf-8")
        tmp.replace(overlay_path)  # atomic
        with (data_dir / "rates_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": report.started_at, "applied": report.applied}) + "\n")
        calc.load()

    any_ok = any(s.get("ok") for s in report.sources)
    status_path = Path(data_dir) / "update_status.json"
    prev = json.loads(status_path.read_text()) if status_path.exists() else {}
    status = {
        "last_checked": report.started_at,
        "last_success": report.started_at if any_ok else prev.get("last_success"),
        "last_change": report.started_at if changed else prev.get("last_change"),
        "report": report.__dict__,
    }
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status
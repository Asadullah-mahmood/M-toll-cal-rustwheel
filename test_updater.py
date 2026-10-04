"""Offline tests: fake pages shaped like the real NHA / news / guide pages."""
import json
import tempfile
from pathlib import Path

from app.calculator import DATA_FILE, TollCalculator
from app.updater import parse_effective_date, parse_rates, run_update, validate

NHA_NOTICE = """REVISED TOLL RATES effective from 10th January, 2027
| Category | Islamabad - Peshawar (M-1) | Lahore - Abdul Hakeem (M-3) | Pindi Bhattian - Faisalabad - Multan (M-4) |
| Car | 750 | 1050 | 1400 |
| Wagon upto 12 seater | 1150 | 1580 | 2000 |
| 13-24 Seater, Coaster/Mini Bus | 1500 | 2300 | 3000 |
| Bus | 2200 | 3300 | 4200 |
| 2 & 3 Axle Truck | 2800 | 4200 | 5500 |
| Articulated Truck | 3500 | 5200 | 6700 |
| Category | National Highways | Kohat Tunnel (N-55) | IMDCW (E-75) |
| Car | 110 | 260 | 310 |
| Wagon | 210 | 660 | 310 |
| Bus | 310 | 760 | 460 |
"""

M2_PAGE = """M2 LAHORE-ISLAMABAD TOLL TAX RATES w.e.f 05-07-2027
| Vehicle Category | Old Toll Tax (with M-Tag) | New Toll Tax (with M-Tag) |
| Cars / Jeep / Taxis | Rs.1430 | Rs.1530 |
| Wagons | Rs.2390 | Rs.2560 |
| Coasters | Rs.3350 | Rs.3580 |
| Buses | Rs.4770 | Rs.5100 |
| Trucks (2 & 3-Axle) | Rs.6210 | Rs.6640 |
| Articulated Trucks | Rs.7980 | Rs.8540 |
"""


def fresh():
    d = Path(tempfile.mkdtemp())
    return TollCalculator(DATA_FILE, d), d


def fake_fetch(pages):
    def f(url):
        if url not in pages:
            raise OSError("unreachable")
        return "text/html", pages[url].encode()
    return f


def test_parse_multicolumn_and_effective_date():
    r = parse_rates(NHA_NOTICE)
    assert r["M-1"]["wagon"] == 1150 and r["M-4"]["articulated_truck"] == 6700
    assert r["NH"]["car"] == 110 and r["N-55"]["bus"] == 760 and r["E-75"]["car"] == 310
    assert parse_effective_date(NHA_NOTICE) == "2027-01-10"


def test_parse_m2_old_new_picks_new():
    r = parse_rates(M2_PAGE)
    assert r["M-2"]["car"] == 1530 and r["M-2"]["truck"] == 6640
    assert parse_effective_date(M2_PAGE) == "2027-07-05"


def test_parse_single_column_guide_style():
    txt = "Toll Tax List on Islamabad-Peshawar (M-1)\nVehicle Category | Toll Tax Rate\nCars / Jeep / Taxis | Rs.700\nBuses | Rs.2100"
    assert parse_rates(txt)["M-1"] == {"car": 700, "bus": 2100}


def test_auto_update_applies_official_rates():
    calc, d = fresh()
    assert calc.calculate("M-1", "car", "Peshawar", "Islamabad")["total_toll"] == 700
    src = {"official_pages": ["http://nha/list"], "secondary_pages": [], "link_keywords": ["toll"]}
    pages = {
        "http://nha/list": '<a href="/n1.html">Revised Toll Rates</a><a href="/x">Tenders</a>',
        "http://nha/n1.html": NHA_NOTICE,
    }
    st = run_update(calc, src, fake_fetch(pages), d)
    assert set(st["report"]["applied"]) == {"M-1", "M-3", "M-4", "NH", "N-55", "E-75"}
    r = calc.calculate("M-1", "car", "Peshawar", "Islamabad")
    assert r["total_toll"] == 750 and r["rates_verified"] is True and r["rate_effective_from"] == "2027-01-10"
    assert (d / "rates_live.json").exists() and (d / "rates_history.jsonl").exists()
    assert TollCalculator(DATA_FILE, d).calculate("M-3", "car", "Lahore", "Abdul Hakeem")["total_toll"] == 1050


def test_second_run_is_idempotent():
    calc, d = fresh()
    src = {"official_pages": ["http://a"], "secondary_pages": []}
    run_update(calc, src, fake_fetch({"http://a": NHA_NOTICE}), d)
    st = run_update(calc, src, fake_fetch({"http://a": NHA_NOTICE}), d)
    assert st["report"]["applied"] == {} and "M-1" in st["report"]["unchanged"]


def test_garbage_rates_are_rejected_and_old_rates_kept():
    calc, d = fresh()
    bad = NHA_NOTICE.replace("| Car | 750 |", "| Car | 7500 |")  # 10x typo
    run_update(calc, {"official_pages": ["http://a"], "secondary_pages": []}, fake_fetch({"http://a": bad}), d)
    assert calc.calculate("M-1", "car", "Peshawar", "Islamabad")["total_toll"] == 700


def test_older_notice_does_not_downgrade():
    calc, d = fresh()
    old = NHA_NOTICE.replace("10th January, 2027", "10th January, 2020")
    st = run_update(calc, {"official_pages": ["http://a"], "secondary_pages": []}, fake_fetch({"http://a": old}), d)
    assert st["report"]["applied"] == {}


def test_unreachable_sources_keep_working_with_last_rates():
    calc, d = fresh()
    st = run_update(calc, {"official_pages": ["http://down"], "secondary_pages": []}, fake_fetch({}), d)
    assert st["report"]["errors"] and st["last_success"] is None
    assert calc.calculate("M-2", "car", "Lahore", "Islamabad")["total_toll"] == 1430


def test_official_beats_secondary_and_secondary_is_flagged_unverified():
    calc, d = fresh()
    sec = "Toll Tax List on Islamabad-Peshawar (M-1)\nCars / Jeep / Taxis | Rs.760\nBuses | Rs.2250"
    st = run_update(calc, {"official_pages": [], "secondary_pages": ["http://g"]}, fake_fetch({"http://g": sec}), d)
    assert st["report"]["applied"]["M-1"]["trust"] == "secondary"
    assert calc.calculate("M-1", "car", "Peshawar", "Islamabad")["rates_verified"] is False


def test_validate_rules():
    assert validate("M-1", {"car": 700, "wagon": 600}, {}) is not None
    assert validate("M-1", {"car": 700, "wagon": 1100}, {"car": 700}) is None
    assert validate("M-1", {"car": 7000}, {"car": 700}) is not None
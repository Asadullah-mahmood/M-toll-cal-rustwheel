"""Toll calculation engine (no web-framework dependency, easy to unit test)."""
from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).parent / "data" / "tariffs.json"
# Where the auto-updater writes rates_live.json / status (mount a volume here in production)
DATA_DIR = Path(os.getenv("TOLL_DATA_DIR", Path(__file__).parent / "data"))


# ---------------------------------------------------------------- errors
class TollError(Exception):
    """Base error. `status` maps to an HTTP status code in the API layer."""

    status = 400

    def __init__(self, message: str, suggestions: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.suggestions = suggestions or []


class UnknownRoad(TollError):
    status = 404


class UnknownStation(TollError):
    status = 404


class UnknownVehicle(TollError):
    status = 422


class RateNotAvailable(TollError):
    status = 422


class InvalidJourney(TollError):
    status = 422


# ---------------------------------------------------------------- helpers
def _norm(text: str) -> str:
    """'M-2', 'm2', ' M 2 ' -> 'm2' ; 'D.I. Khan' -> 'dikhan'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _round_step(value: Decimal, step: int) -> int:
    return int((value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step)


@dataclass
class Station:
    name: str
    km: float
    aliases: list[str] = field(default_factory=list)

    def keys(self) -> set[str]:
        return {_norm(self.name), *(_norm(a) for a in self.aliases)}


@dataclass
class Road:
    id: str
    name: str
    type: str
    pricing: str  # "distance" | "flat"
    rates: dict[str, int]
    effective_from: str
    verified_rates: bool
    total_km: float | None = None
    valid_until: str | None = None
    rate_note: str | None = None
    surcharge_pct: int | None = None
    rates_source: str = "bundled"
    rates_updated_at: str | None = None
    aliases: list[str] = field(default_factory=list)
    stations: list[Station] = field(default_factory=list)

    def keys(self) -> set[str]:
        return {_norm(self.id), _norm(self.name), *(_norm(a) for a in self.aliases)}


# ---------------------------------------------------------------- engine
class TollCalculator:
    def __init__(self, data_file: Path = DATA_FILE, data_dir: Path = DATA_DIR):
        self.data_file = Path(data_file)
        self.data_dir = Path(data_dir)
        self.load()

    # ---- loading
    def load(self) -> None:
        raw: dict[str, Any] = json.loads(self.data_file.read_text(encoding="utf-8"))
        meta = raw["meta"]
        self.currency: str = meta["currency"]
        self.notice: str = meta.get("notice", "")
        self.last_reviewed: str = meta.get("last_reviewed", "")
        self.default_surcharge: int = int(meta["default_non_mtag_surcharge_pct"])
        self.step: int = int(meta.get("rounding_step", 10))

        self.vehicle_classes: dict[str, dict[str, Any]] = raw["vehicle_classes"]
        self._vehicle_lookup: dict[str, str] = {}
        for key, info in self.vehicle_classes.items():
            for alias in [key, info["label"], *info.get("aliases", [])]:
                self._vehicle_lookup[_norm(alias)] = key

        self.roads: dict[str, Road] = {}
        for rid, r in raw["roads"].items():
            self.roads[rid] = Road(
                id=rid,
                name=r["name"],
                type=r["type"],
                pricing=r["pricing"],
                rates=r["rates"],
                effective_from=r["effective_from"],
                verified_rates=r.get("verified_rates", False),
                total_km=r.get("total_km"),
                valid_until=r.get("valid_until"),
                rate_note=r.get("rate_note"),
                surcharge_pct=r.get("non_mtag_surcharge_pct"),
                aliases=r.get("aliases", []),
                stations=[Station(**s) for s in r.get("stations", [])],
            )

        # live rates written by the auto-updater override the bundled seed rates
        live_file = self.data_dir / "rates_live.json"
        if live_file.exists():
            live = json.loads(live_file.read_text(encoding="utf-8")).get("roads", {})
            for rid, upd in live.items():
                road = self.roads.get(rid)
                if road:
                    road.rates = {**road.rates, **upd["rates"]}
                    road.effective_from = upd.get("effective_from", road.effective_from)
                    road.valid_until = upd.get("valid_until")
                    road.verified_rates = bool(upd.get("verified", False))
                    road.rates_source = upd.get("source", "auto-update")
                    road.rates_updated_at = upd.get("updated_at")
                    road.rate_note = None  # seed caveats no longer apply

        # fixed fares: {"M-2": {"Lahore|Sheikhupura": {"car": 250}}}
        self.fixed_fares: dict[str, dict[frozenset[str], dict[str, int]]] = {}
        for rid, pairs in raw.get("fixed_fares", {}).items():
            if rid.startswith("_") or not isinstance(pairs, dict):
                continue
            table: dict[frozenset[str], dict[str, int]] = {}
            for pair, rates in pairs.items():
                a, b = pair.split("|")
                table[frozenset({_norm(a), _norm(b)})] = rates
            self.fixed_fares[rid] = table

    # ---- lookups
    def find_road(self, text: str) -> Road:
        n = _norm(text)
        for road in self.roads.values():
            if n in road.keys():
                return road
        close = difflib.get_close_matches(n, [k for r in self.roads.values() for k in r.keys()], n=3)
        hints = sorted({r.id for r in self.roads.values() if r.keys() & set(close)})
        raise UnknownRoad(f"Unknown motorway/highway '{text}'.", hints or sorted(self.roads))

    def find_vehicle(self, text: str) -> str:
        n = _norm(text)
        if n in self._vehicle_lookup:
            return self._vehicle_lookup[n]
        raise UnknownVehicle(f"Unknown vehicle type '{text}'.", sorted(self.vehicle_classes))

    def find_station(self, road: Road, text: str) -> Station:
        n = _norm(text)
        for st in road.stations:
            if n in st.keys():
                return st
        names = [s.name for s in road.stations]
        close = difflib.get_close_matches(text, names, n=3, cutoff=0.5)
        raise UnknownStation(
            f"'{text}' is not an interchange on {road.id}.", close or names
        )

    # ---- calculation
    def calculate(
        self,
        motorway: str,
        vehicle_type: str,
        start: str | None = None,
        end: str | None = None,
        has_mtag: bool = True,
    ) -> dict[str, Any]:
        road = self.find_road(motorway)
        vclass = self.find_vehicle(vehicle_type)

        full_rate = road.rates.get(vclass)
        if full_rate is None:
            raise RateNotAvailable(
                f"No {vclass} rate is configured for {road.id}.",
                sorted(road.rates),
            )

        start_name = end_name = None
        distance = None
        method: str

        if road.pricing == "flat":
            base = full_rate
            method = "flat_per_plaza"
        else:
            if not start or not end:
                raise InvalidJourney(
                    f"{road.id} needs both 'start' and 'end' interchanges.",
                    [s.name for s in road.stations],
                )
            s1, s2 = self.find_station(road, start), self.find_station(road, end)
            if s1 is s2:
                raise InvalidJourney("Start and end interchange are the same.")
            start_name, end_name = s1.name, s2.name
            distance = abs(s2.km - s1.km)

            fixed = self.fixed_fares.get(road.id, {}).get(
                frozenset({_norm(s1.name), _norm(s2.name)}), {}
            ).get(vclass)
            if fixed is not None:
                base, method = int(fixed), "fixed_fare_table"
            else:
                raw = Decimal(full_rate) * Decimal(str(distance)) / Decimal(str(road.total_km))
                base = _round_step(raw, self.step)
                method = "distance_prorated"

        pct = road.surcharge_pct if road.surcharge_pct is not None else self.default_surcharge
        surcharge = 0 if has_mtag else _round_step(Decimal(base) * pct / 100, self.step)

        return {
            "motorway": road.id,
            "motorway_name": road.name,
            "start": start_name,
            "end": end_name,
            "vehicle_type": vclass,
            "vehicle_label": self.vehicle_classes[vclass]["label"],
            "distance_km": distance,
            "has_mtag": has_mtag,
            "base_toll": base,
            "non_mtag_surcharge": surcharge,
            "surcharge_pct_applied": 0 if has_mtag else pct,
            "total_toll": base + surcharge,
            "currency": self.currency,
            "pricing_method": method,
            "full_route_rate": full_rate,
            "rate_effective_from": road.effective_from,
            "rate_valid_until": road.valid_until,
            "rates_verified": road.verified_rates,
            "rates_source": road.rates_source,
            "rates_updated_at": road.rates_updated_at,
            "notes": [n for n in [road.rate_note, self.notice] if n],
        }

    def calculate_trip(self, legs: list[dict[str, Any]]) -> dict[str, Any]:
        results = [self.calculate(**leg) for leg in legs]
        return {
            "legs": results,
            "total_toll": sum(r["total_toll"] for r in results),
            "currency": self.currency,
        }

    # ---- catalogue
    def list_roads(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.id,
                "name": r.name,
                "type": r.type,
                "pricing": r.pricing,
                "total_km": r.total_km,
                "station_count": len(r.stations),
                "vehicle_types": sorted(r.rates),
                "rate_effective_from": r.effective_from,
                "rates_verified": r.verified_rates,
            }
            for r in self.roads.values()
        ]
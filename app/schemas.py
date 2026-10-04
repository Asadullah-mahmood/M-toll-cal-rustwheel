from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class TollRequest(BaseModel):
    motorway: str = Field(..., examples=["M-2"], description="Road id or name, e.g. M-2, m2, M-5, NH, E-35")
    vehicle_type: str = Field(..., examples=["car"], description="car, wagon, coaster, bus, truck, articulated_truck (aliases accepted)")
    start: Optional[str] = Field(None, examples=["Lahore"], description="Entry interchange (not needed for flat-rate roads)")
    end: Optional[str] = Field(None, examples=["Islamabad"], description="Exit interchange (not needed for flat-rate roads)")
    has_mtag: bool = Field(True, description="False = no M-Tag / low balance, surcharge applies")

    @field_validator("motorway", "vehicle_type", "start", "end")
    @classmethod
    def _strip(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class TripRequest(BaseModel):
    legs: list[TollRequest] = Field(..., min_length=1, max_length=20)


class TollResponse(BaseModel):
    motorway: str
    motorway_name: str
    start: Optional[str]
    end: Optional[str]
    vehicle_type: str
    vehicle_label: str
    distance_km: Optional[float]
    has_mtag: bool
    base_toll: int
    non_mtag_surcharge: int
    surcharge_pct_applied: int
    total_toll: int
    currency: str
    pricing_method: str
    full_route_rate: int
    rate_effective_from: str
    rate_valid_until: Optional[str]
    rates_verified: bool
    rates_source: str
    rates_updated_at: Optional[str]
    notes: list[str]


class TripResponse(BaseModel):
    legs: list[TollResponse]
    total_toll: int
    currency: str
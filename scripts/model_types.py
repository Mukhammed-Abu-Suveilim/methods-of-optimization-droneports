"""Типы обычных словарей, используемых расчётом сети."""

from typing import TypedDict

Point = TypedDict(
    "Point",
    {
        "id": str,
        "name": str,
        "region": str,
        "lat": float,
        "lon": float,
        "population": float,
        "types": str,
    },
)
InstanceData = TypedDict(
    "InstanceData",
    {
        "label": str,
        "ids": list[str],
        "points": dict[str, Point],
        "distance": dict[tuple[str, str], int],
        "demand": dict[tuple[str, str], tuple[int, int, int]],
        "settings": dict[str, float],
        "hashes": dict[str, str],
    },
)
Plan = TypedDict(
    "Plan",
    {
        "open_year": dict[str, int],
        "fleet": list[int],
        "bases": list[list[str]],
    },
)
Flight = TypedDict(
    "Flight",
    {
        "id": str,
        "drone": int,
        "origin": str,
        "destination": str,
        "departure_s": int,
        "arrival_s": int,
    },
)
Charge = TypedDict(
    "Charge",
    {"drone": int, "port": str, "start_s": int, "end_s": int},
)
Journey = TypedDict(
    "Journey",
    {"origin": str, "destination": str, "count": int, "flights": list[str]},
)
Day = TypedDict(
    "Day",
    {
        "weight": int,
        "bases": list[str],
        "flights": list[Flight],
        "charges": list[Charge],
        "journeys": list[Journey],
    },
)
YearResult = TypedDict(
    "YearResult",
    {
        "year": int,
        "trips": int,
        "ports": int,
        "fleet": int,
        "fixed_cost_rub": float,
        "flight_cost_rub": float,
        "cost_rub": float,
        "cumulative_cost_rub": float,
        "days": list[Day],
    },
)
OperatingResult = TypedDict(
    "OperatingResult",
    {
        "model_version": str,
        "instance": str,
        "plan": Plan,
        "years": list[YearResult],
        "objective_trips": int,
        "cum_spend_rub": float,
        "valid": bool,
        "violations": list[str],
        "input_hashes": dict[str, str],
        "configuration_hash": str,
    },
)
Scenario = TypedDict(
    "Scenario",
    {
        "name": str,
        "demand_multiplier": float,
        "range_km": float,
        "closed_port": str,
        "closed_year": int,
    },
)

SearchCheckpoint = TypedDict(
    "SearchCheckpoint",
    {
        "seed": int,
        "proposals": int,
        "batches": int,
        "accepted": int,
        "stale_batches": int,
        "temperature": float,
        "rng_state": str,
        "wall_s": float,
        "current_plan": Plan,
        "initial_configuration_hash": str,
    },
)

"""
Исторический оценщик v1 для воспроизводимого сравнения с новой
моделью.
"""

import csv
import math
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

from scripts.model_data import read_points

Pair = tuple[str, str]
OpenYears = dict[str, int | None]
Allocation = TypedDict(
    "Allocation",
    {
        "trips": dict[Pair, float],
        "flights": dict[Pair, int],
        "paths": dict[Pair, list[Pair]],
        "cost": float,
    },
)
LegacyYear = TypedDict(
    "LegacyYear",
    {"year": int, "trips": int, "ports": int, "fleet": int, "cost_rub": int},
)
LegacyResult = TypedDict(
    "LegacyResult",
    {"trips": int, "per_year": list[LegacyYear], "cum_spend_rub": int},
)

RHO_EMPTY = 0.10


class Instance:
    """Сохранённый интерфейс v1; новая модель использует словари."""

    def __init__(
        self,
        points_csv: str | Path,
        demand_csv: str | Path,
        params: dict[str, dict[str, float]],
    ) -> None:
        """Загрузить исторические входы с явными типами CSV."""
        # 1. Прочитать идентификаторы, подписи и численность населения.
        points = read_points(Path(points_csv))
        self.ids = list(points)
        self.names = {p: row["name"] for p, row in points.items()}
        self.pop = {p: row["population"] for p, row in points.items()}
        self.dist: dict[Pair, float] = {}
        self.Q: dict[Pair, tuple[int, int, int]] = {}
        with Path(demand_csv).open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                pair = row["origin_id"], row["destination_id"]
                if set(pair) <= points.keys():
                    self.dist[pair] = float(row["distance_km"])
                    self.Q[pair] = (
                        int(row["q1"]),
                        int(row["q2"]),
                        int(row["q3"]),
                    )
        # 2. Сохранить формулы v1, включая известное завышение лимита.
        d = params["drone"]
        op = params["operations"]
        dp = params["droneport"]
        self.seats = d["seats"]
        day_min = (op["day_end_hour"] - op["day_start_hour"]) * 60
        self.window_days = day_min * op["typical_days_per_year"]
        self.tau = {
            a: km / d["cruise_speed_kmh"] * 60
            + d["turnaround_min"]
            + d["recharge_min"]
            for a, km in self.dist.items()
            if km <= d["range_km"]
        }
        self.fh = {
            a: km / d["cruise_speed_kmh"]
            for a, km in self.dist.items()
            if km <= d["range_km"]
        }
        self.adj: dict[str, list[str]] = {i: [] for i in self.ids}
        for a in self.tau:
            self.adj[a[0]].append(a[1])
        self.takeoff_cap = (
            dp["takeoffs_per_hour"] * day_min * op["typical_days_per_year"]
        )
        charges_day = day_min // d["recharge_min"]
        self.slot_cap = {
            i: (
                dp["charge_slots_hub"]
                if self.pop[i] >= dp["hub_if_population_ge"]
                else dp["charge_slots_small"]
            )
            * charges_day
            * op["typical_days_per_year"]
            for i in self.ids
        }
        self.capex = {
            i: (
                dp["capex_hub_rub"]
                if self.pop[i] >= dp["hub_if_population_ge"]
                else dp["capex_small_rub"]
            )
            for i in self.ids
        }
        self.opex = dp["opex_per_year_rub"]
        self.drone_price = d["price_rub"]
        self.cost_fh = op["cost_per_flight_hour_rub"]
        self.tranche = params["budget"]["tranche_rub_per_year"]
        self.OD = [
            (o, d2)
            for (o, d2) in self.Q
            if any(q > 0 for q in self.Q[(o, d2)])
        ]

    def routes(self, open_set: Iterable[str]) -> dict[Pair, list[Pair]]:
        """Воспроизвести исторические маршруты BFS."""
        open_set = set(open_set)
        paths = {}
        for s in open_set:
            parent: dict[str, str | None] = {s: None}
            queue = deque([s])
            while queue:
                u = queue.popleft()
                for v in self.adj[u]:
                    if v in open_set and v not in parent:
                        parent[v] = u
                        queue.append(v)
            for v in open_set:
                if v != s and v in parent:
                    path = []
                    cur = v
                    while parent[cur] is not None:
                        previous = parent[cur]
                        assert previous is not None
                        path.append((previous, cur))
                        cur = previous
                    paths[(s, v)] = path[::-1]
        return paths


def allocate_year(
    inst: Instance,
    open_t: list[str],
    new_ports: list[str],
    fleet_t: int,
    prev_n: int,
    t: int,
    cum: float,
) -> Allocation | None:
    """Воспроизвести распределение v1, не сертифицируя расписание."""
    # 1. Назначить спрос на фиксированные BFS-пути.
    paths = inst.routes(open_t) if open_t else {}
    load: dict[Pair, float] = {}
    trips: dict[Pair, float] = {}
    for od in sorted(inst.OD, key=lambda od: -inst.Q[od][t - 1]):
        q = inst.Q[od][t - 1]
        path = paths.get(od)
        if q <= 0 or path is None:
            continue
        trips[od] = float(q)
        for leg in path:
            load[leg] = load.get(leg, 0.0) + q
    flights = {
        leg: math.ceil(value / inst.seats) for leg, value in load.items()
    }
    minutes = sum(flights[leg] * inst.tau[leg] for leg in flights) * (
        1 + RHO_EMPTY
    )
    dep: dict[str, int] = {}
    arr: dict[str, int] = {}
    for leg, fl in flights.items():
        dep[leg[0]] = dep.get(leg[0], 0) + fl
        arr[leg[1]] = arr.get(leg[1], 0) + fl
    factor = 1.0
    if minutes > 0:
        factor = min(factor, fleet_t * inst.window_days / minutes)
    for k in open_t:
        if dep.get(k, 0) > inst.takeoff_cap:
            factor = min(factor, inst.takeoff_cap / dep[k])
        if arr.get(k, 0) > inst.slot_cap[k]:
            factor = min(factor, inst.slot_cap[k] / arr[k])
    if factor < 1:
        for od in trips:
            trips[od] *= factor
        load = {}
        for od, tr in trips.items():
            for leg in paths[od]:
                load[leg] = load.get(leg, 0.0) + tr
        flights = {
            leg: math.ceil(value / inst.seats) for leg, value in load.items()
        }
    fixed = (
        sum(inst.capex[i] for i in new_ports)
        + inst.drone_price * (fleet_t - prev_n)
        + inst.opex * len(open_t)
    )
    flight_cost = (
        sum(flights[leg] * inst.fh[leg] for leg in flights)
        * (1 + RHO_EMPTY)
        * inst.cost_fh
    )
    headroom = t * inst.tranche - cum - fixed
    if flight_cost > max(0.0, headroom):
        factor_f = headroom / flight_cost if flight_cost > 0 else 0.0
        flights = {leg: int(fl * factor_f) for leg, fl in flights.items()}
        cap = {leg: fl * inst.seats for leg, fl in flights.items()}
        trips = {}
        for od in sorted(inst.OD, key=lambda od: -inst.Q[od][t - 1]):
            q = inst.Q[od][t - 1]
            path = paths.get(od)
            if q <= 0 or path is None:
                continue
            avail = min(cap.get(leg, 0) for leg in path)
            if avail <= 0:
                continue
            tr = min(q, avail)
            trips[od] = tr
            for leg in path:
                cap[leg] -= tr
        flight_cost = (
            sum(flights[leg] * inst.fh[leg] for leg in flights)
            * (1 + RHO_EMPTY)
            * inst.cost_fh
        )
    cost = fixed + flight_cost
    if cum + cost > t * inst.tranche:
        return None
    return {"trips": trips, "flights": flights, "paths": paths, "cost": cost}


def evaluate(
    inst: Instance,
    open_year: OpenYears,
    fleet: list[int],
    years: tuple[int, ...] = (1, 2, 3),
) -> LegacyResult | None:
    """Пересчитать историческую цель для регрессионной проверки."""
    total = 0
    per_year: list[LegacyYear] = []
    cum = 0.0
    prev_n = 0
    for t in years:
        open_t = [i for i in inst.ids if (open_year.get(i) or 4) <= t]
        new_ports = [i for i in open_t if open_year[i] == t]
        alloc = allocate_year(
            inst, open_t, new_ports, fleet[t - 1], prev_n, t, cum
        )
        if alloc is None:
            return None
        trips_t = sum(
            min(int(tr), inst.Q[od][t - 1])
            for od, tr in alloc["trips"].items()
        )
        cost = alloc["cost"]
        prev_n = fleet[t - 1]
        cum += cost
        total += trips_t
        per_year.append(
            {
                "year": t,
                "trips": trips_t,
                "ports": len(open_t),
                "fleet": fleet[t - 1],
                "cost_rub": round(cost),
            }
        )
    return {"trips": total, "per_year": per_year, "cum_spend_rub": round(cum)}

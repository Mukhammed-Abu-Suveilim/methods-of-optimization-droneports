import math
from collections import deque

import pandas as pd

RHO_EMPTY = 0.10


class Instance:
    def __init__(self, points_csv, demand_csv, params):
        pts = pd.read_csv(points_csv)
        self.ids = list(pts["wikidata_id"])
        self.names = dict(zip(pts["wikidata_id"], pts["name"]))
        self.pop = dict(zip(pts["wikidata_id"], pts["population"]))
        dem = pd.read_csv(demand_csv)
        dem = dem[dem["origin_id"].isin(self.ids) & dem["destination_id"].isin(self.ids)]
        self.dist = {(r.origin_id, r.destination_id): r.distance_km for r in dem.itertuples()}
        self.Q = {(r.origin_id, r.destination_id): (r.q1, r.q2, r.q3) for r in dem.itertuples()}
        d = params["drone"]
        op = params["operations"]
        dp = params["droneport"]
        self.seats = d["seats"]
        day_min = (op["day_end_hour"] - op["day_start_hour"]) * 60
        self.window_days = day_min * op["typical_days_per_year"]
        self.tau = {a: km / d["cruise_speed_kmh"] * 60 + d["turnaround_min"] + d["recharge_min"] for a, km in self.dist.items() if km <= d["range_km"]}
        self.fh = {a: km / d["cruise_speed_kmh"] for a, km in self.dist.items() if km <= d["range_km"]}
        self.adj = {i: [] for i in self.ids}
        for a in self.tau:
            self.adj[a[0]].append(a[1])
        self.takeoff_cap = dp["takeoffs_per_hour"] * day_min * op["typical_days_per_year"]
        charges_day = day_min // d["recharge_min"]
        self.slot_cap = {
            i: (dp["charge_slots_hub"] if self.pop[i] >= dp["hub_if_population_ge"] else dp["charge_slots_small"]) * charges_day * op["typical_days_per_year"]
            for i in self.ids
        }
        self.capex = {
            i: (dp["capex_hub_rub"] if self.pop[i] >= dp["hub_if_population_ge"] else dp["capex_small_rub"])
            for i in self.ids
        }
        self.opex = dp["opex_per_year_rub"]
        self.drone_price = d["price_rub"]
        self.cost_fh = op["cost_per_flight_hour_rub"]
        self.tranche = params["budget"]["tranche_rub_per_year"]
        self.OD = [(o, d2) for (o, d2) in self.Q if any(q > 0 for q in self.Q[(o, d2)])]

    def routes(self, open_set):
        open_set = set(open_set)
        paths = {}
        for s in open_set:
            parent = {s: None}
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
                        path.append((parent[cur], cur))
                        cur = parent[cur]
                    paths[(s, v)] = path[::-1]
        return paths


def allocate_year(inst, open_t, new_ports, fleet_t, prev_n, t, cum):
    paths = inst.routes(open_t) if open_t else {}
    load = {}
    trips = {}
    for od in sorted(inst.OD, key=lambda od: -inst.Q[od][t - 1]):
        q = inst.Q[od][t - 1]
        path = paths.get(od)
        if q <= 0 or path is None:
            continue
        trips[od] = float(q)
        for leg in path:
            load[leg] = load.get(leg, 0.0) + q
    flights = {leg: math.ceil(l / inst.seats) for leg, l in load.items()}
    minutes = sum(flights[leg] * inst.tau[leg] for leg in flights) * (1 + RHO_EMPTY)
    dep = {}
    arr = {}
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
        flights = {leg: math.ceil(l / inst.seats) for leg, l in load.items()}
    fixed = (
        sum(inst.capex[i] for i in new_ports)
        + inst.drone_price * (fleet_t - prev_n)
        + inst.opex * len(open_t)
    )
    flight_cost = sum(flights[leg] * inst.fh[leg] for leg in flights) * (1 + RHO_EMPTY) * inst.cost_fh
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
        flight_cost = sum(flights[leg] * inst.fh[leg] for leg in flights) * (1 + RHO_EMPTY) * inst.cost_fh
    cost = fixed + flight_cost
    if cum + cost > t * inst.tranche:
        return None
    return {"trips": trips, "flights": flights, "paths": paths, "cost": cost}


def evaluate(inst, open_year, fleet, years=(1, 2, 3)):
    total = 0
    per_year = []
    cum = 0.0
    prev_n = 0
    for t in years:
        open_t = [i for i in inst.ids if open_year.get(i) is not None and open_year[i] <= t]
        new_ports = [i for i in open_t if open_year[i] == t]
        alloc = allocate_year(inst, open_t, new_ports, fleet[t - 1], prev_n, t, cum)
        if alloc is None:
            return None
        trips_t = sum(min(int(tr), inst.Q[od][t - 1]) for od, tr in alloc["trips"].items())
        cost = alloc["cost"]
        prev_n = fleet[t - 1]
        cum += cost
        total += trips_t
        per_year.append({"year": t, "trips": trips_t, "ports": len(open_t), "fleet": fleet[t - 1], "cost_rub": round(cost)})
    return {"trips": total, "per_year": per_year, "cum_spend_rub": round(cum)}

import json
import time
from pathlib import Path

import pandas as pd
import pulp

TIME_LIMIT = 420

P = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
pts = pd.read_csv(Path("data/processed/points_small.csv"))
dem = pd.read_csv(Path("data/processed/demand_main.csv"))
ids = list(pts["wikidata_id"])
names = dict(zip(pts["wikidata_id"], pts["name"]))
pop = dict(zip(pts["wikidata_id"], pts["population"]))
dem = dem[dem["origin_id"].isin(ids) & dem["destination_id"].isin(ids)]
dist = {(r.origin_id, r.destination_id): r.distance_km for r in dem.itertuples()}
Q = {(r.origin_id, r.destination_id): (r.q1, r.q2, r.q3) for r in dem.itertuples()}

v = P["drone"]["cruise_speed_kmh"]
tau = {}
fh = {}
window = (P["operations"]["day_end_hour"] - P["operations"]["day_start_hour"]) * 60
days = P["operations"]["typical_days_per_year"]
takeoff_cap = P["droneport"]["takeoffs_per_hour"] * window * days
charges_per_slot_day = window // P["drone"]["recharge_min"]
N_MAX = 12
T = [1, 2, 3]

A = [(i, j) for (i, j) in dist if dist[(i, j)] <= P["drone"]["range_km"]]
for a in A:
    tau[a] = dist[a] / v * 60 + P["drone"]["turnaround_min"] + P["drone"]["recharge_min"]
    fh[a] = dist[a] / v
M = {a: min(takeoff_cap, int(N_MAX * window * days / tau[a])) for a in A}
OD = [(o, d) for (o, d) in Q if any(q > 0 for q in Q[(o, d)])]
out_arcs = {k: [a for a in A if a[0] == k] for k in ids}
in_arcs = {k: [a for a in A if a[1] == k] for k in ids}
capex = {
    i: (P["droneport"]["capex_hub_rub"] if pop[i] >= P["droneport"]["hub_if_population_ge"] else P["droneport"]["capex_small_rub"])
    for i in ids
}
slots = {
    i: (P["droneport"]["charge_slots_hub"] if pop[i] >= P["droneport"]["hub_if_population_ge"] else P["droneport"]["charge_slots_small"])
    for i in ids
}

prob = pulp.LpProblem("droneports_small", pulp.LpMaximize)
x = pulp.LpVariable.dicts("x", (ids, T), cat="Binary")
x = {(i, t): x[i][t] for i in ids for t in T}
N = pulp.LpVariable.dicts("N", T, lowBound=0, upBound=N_MAX, cat="Integer")
n = {(a, t): pulp.LpVariable(f"n_{a[0]}_{a[1]}_{t}", lowBound=0, cat="Integer") for a in A for t in T}
e = {(a, t): pulp.LpVariable(f"e_{a[0]}_{a[1]}_{t}", lowBound=0, cat="Integer") for a in A for t in T}
S = {(od, t): pulp.LpVariable(f"S_{od[0]}_{od[1]}_{t}", lowBound=0) for od in OD for t in T}
f = {(od, a, t): pulp.LpVariable(f"f_{od[0]}_{od[1]}_{a[0]}_{a[1]}_{t}", lowBound=0) for od in OD for a in A for t in T}

prob += pulp.lpSum(S[od, t] for od in OD for t in T)

for i in ids:
    for t in T[:-1]:
        prob += x[i, t] <= x[i, t + 1]
for t in T[:-1]:
    prob += N[t] <= N[t + 1]
for a in A:
    for t in T:
        prob += n[a, t] <= M[a] * x[a[0], t]
        prob += n[a, t] <= M[a] * x[a[1], t]
        prob += e[a, t] <= M[a] * x[a[0], t]
        prob += e[a, t] <= M[a] * x[a[1], t]
        prob += pulp.lpSum(f[od, a, t] for od in OD) <= P["drone"]["seats"] * n[a, t]
for od in OD:
    o, d = od
    for t in T:
        prob += S[od, t] <= Q[od][t - 1]
        for k in ids:
            lhs = pulp.lpSum(f[od, a, t] for a in out_arcs[k]) - pulp.lpSum(f[od, a, t] for a in in_arcs[k])
            if k == o:
                prob += lhs == S[od, t]
            elif k == d:
                prob += lhs == -S[od, t]
            else:
                prob += lhs == 0
for k in ids:
    for t in T:
        prob += pulp.lpSum(n[a, t] + e[a, t] for a in out_arcs[k]) == pulp.lpSum(n[a, t] + e[a, t] for a in in_arcs[k])
        prob += pulp.lpSum(n[a, t] + e[a, t] for a in out_arcs[k]) <= takeoff_cap * x[k, t]
        prob += pulp.lpSum(n[a, t] + e[a, t] for a in in_arcs[k]) <= slots[k] * charges_per_slot_day * days * x[k, t]
for t in T:
    prob += pulp.lpSum((n[a, t] + e[a, t]) * tau[a] for a in A) <= N[t] * window * days
for od in OD:
    hops = -(-dist[od] // P["drone"]["range_km"])
    if hops >= 2:
        for t in T:
            prob += pulp.lpSum(f[od, a, t] for a in A) >= hops * S[od, t]

spend = {}
cum = 0.0
for t in T:
    prev_x = {i: (x[i, t - 1] if t > 1 else 0) for i in ids}
    prev_N = N[t - 1] if t > 1 else 0
    spend[t] = (
        pulp.lpSum(capex[i] * (x[i, t] - prev_x[i]) for i in ids)
        + P["drone"]["price_rub"] * (N[t] - prev_N)
        + pulp.lpSum(P["droneport"]["opex_per_year_rub"] * x[i, t] for i in ids)
        + pulp.lpSum(P["operations"]["cost_per_flight_hour_rub"] * (n[a, t] + e[a, t]) * fh[a] for a in A)
    )
    cum = cum + spend[t]
    prob += cum <= t * P["budget"]["tranche_rub_per_year"]

t0 = time.time()
prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=TIME_LIMIT))
wall = time.time() - t0
status = pulp.LpStatus[prob.status]
obj = pulp.value(prob.objective)
xv = {k: (v.value() or 0.0) for k, v in x.items()}
Nv = {t: (N[t].value() or 0) for t in T}
Sv = {k: (v.value() or 0.0) for k, v in S.items()}
spendv = {t: (pulp.value(spend[t]) or 0.0) for t in T}
time_limited = wall >= TIME_LIMIT - 1

for var in prob.variables():
    var.cat = "Continuous"
prob.solve(pulp.PULP_CBC_CMD(msg=0))
ub = pulp.value(prob.objective)
gap = (ub - obj) / ub if ub else 0.0

per_year = []
for t in T:
    trips = sum(Sv[od, t] for od in OD)
    demand_t = sum(Q[od][t - 1] for od in OD)
    open_ports = [names[i] for i in ids if xv[i, t] > 0.5]
    per_year.append(
        {
            "year": t,
            "trips": round(trips),
            "served_share": round(trips / demand_t, 4),
            "fleet": int(round(Nv[t])),
            "open_ports": open_ports,
            "spend_rub": round(spendv[t]),
        }
    )
cum_spend = 0.0
for row in per_year:
    cum_spend += row["spend_rub"]
    row["cum_spend_rub"] = round(cum_spend)

out = Path("data/results")
out.mkdir(parents=True, exist_ok=True)
result = {
    "instance": "small",
    "solver": "PULP_CBC_CMD (CBC)",
    "time_limit_s": TIME_LIMIT,
    "wall_s": round(wall, 1),
    "status": status,
    "time_limited": time_limited,
    "objective_trips": round(obj),
    "lp_upper_bound": round(ub),
    "gap_to_lp_upper_bound": round(gap, 5),
    "gap_note": "разрыв к верхней оценке LP-релаксации; остановка по лимиту времени оптимальность не доказывает",
    "per_year": per_year,
}
(out / "milp_small.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
best = {"milp_small": round(obj)}
(out / "best_objectives.json").write_text(json.dumps(best, indent=2), encoding="utf-8")

print(json.dumps(result, ensure_ascii=False, indent=2))

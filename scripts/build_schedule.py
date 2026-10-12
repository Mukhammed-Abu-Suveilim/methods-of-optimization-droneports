import json
import math
from collections import defaultdict
from pathlib import Path

from evaluator import Instance, allocate_year, evaluate


def decompose_circuits(edges):
    adj = defaultdict(list)
    for (a, b), c in edges.items():
        adj[a].append([b, c])
    circuits = []
    for s in list(adj):
        if not any(c > 0 for _, c in adj[s]):
            continue
        stack = [s]
        path = []
        while stack:
            u = stack[-1]
            moved = False
            for e in adj[u]:
                if e[1] > 0:
                    e[1] -= 1
                    stack.append(e[0])
                    moved = True
                    break
            if not moved:
                path.append(stack.pop())
        rev = path[::-1]
        circuits.append([(rev[i], rev[i + 1]) for i in range(len(rev) - 1)])
    return circuits


def step(t, rem, leg_km, v, turn, recharge, range_km):
    if rem < leg_km:
        t += recharge
        rem = range_km
    land = t + leg_km / v * 60
    t = land + turn
    rem -= leg_km
    return t, rem, land


def return_fits(inst, params, paths, t, rem, from_node, to_node, day_min):
    if from_node == to_node:
        return True
    ret = paths.get((from_node, to_node))
    if ret is None:
        return False
    d = params["drone"]
    for leg in ret:
        t, rem, land = step(t, rem, inst.dist[leg], d["cruise_speed_kmh"], d["turnaround_min"], d["recharge_min"], d["range_km"])
        if land > day_min:
            return False
    return True


def sim_seq(inst, params, paths, core, start_node, day_min):
    d = params["drone"]
    t, rem = 0.0, float(d["range_km"])
    for leg in core:
        t, rem, land = step(t, rem, inst.dist[leg], d["cruise_speed_kmh"], d["turnaround_min"], d["recharge_min"], d["range_km"])
        if land > day_min:
            return None
    end = core[-1][1] if core else start_node
    full = list(core)
    if end != start_node:
        ret = paths.get((end, start_node))
        if ret is None:
            return None
        for leg in ret:
            t, rem, land = step(t, rem, inst.dist[leg], d["cruise_speed_kmh"], d["turnaround_min"], d["recharge_min"], d["range_km"])
            if land > day_min:
                return None
        full += list(ret)
    return {"core": list(core), "full": full, "start": start_node, "end_core": end, "t": t, "rem": rem}


def build_day(inst, params, paths, D, fleet_limit):
    day_min = (params["operations"]["day_end_hour"] - params["operations"]["day_start_hour"]) * 60
    start_min = params["operations"]["day_start_hour"] * 60
    end_min = params["operations"]["day_end_hour"] * 60
    takeoffs_day = params["droneport"]["takeoffs_per_hour"] * day_min // 60
    v = params["drone"]["cruise_speed_kmh"]
    turn = params["drone"]["turnaround_min"]
    recharge = params["drone"]["recharge_min"]
    range_km = params["drone"]["range_km"]
    violations = []
    load = defaultdict(float)
    for od, n in D.items():
        for leg in paths[od]:
            load[leg] += n
    fl = {leg: math.ceil(l / inst.seats) for leg, l in load.items()}
    bal = defaultdict(int)
    for (a, b), f in fl.items():
        bal[a] += f
        bal[b] -= f
    empt = defaultdict(int)
    for k in sorted([n for n in bal if bal[n] > 0]):
        while bal[k] > 0:
            deficits = [n for n in bal if bal[n] < 0 and paths.get((n, k))]
            if not deficits:
                violations.append(f"дисбаланс флота в узле {inst.names[k]}: нет маршрута для порожнего перегона")
                break
            m = min(deficits, key=lambda n: len(paths[(n, k)]))
            push = min(bal[k], -bal[m])
            for leg in paths[(m, k)]:
                empt[leg] += push
            bal[k] -= push
            bal[m] += push
    if violations:
        return {"violations": violations}
    edges = defaultdict(int)
    for leg, f in fl.items():
        edges[leg] += f
    for leg, f in empt.items():
        edges[leg] += f
    raw = []
    for circ in decompose_circuits(edges):
        t, rem = 0.0, float(range_km)
        start_node = circ[0][0]
        cur = []
        for leg in circ:
            t2, rem2, land2 = step(t, rem, inst.dist[leg], v, turn, recharge, range_km)
            fits = land2 <= day_min and return_fits(inst, params, paths, t2, rem2, leg[1], start_node, day_min)
            if not fits and cur:
                raw.append((cur, start_node))
                cur = []
                t, rem = 0.0, float(range_km)
                start_node = leg[0]
                t2, rem2, land2 = step(t, rem, inst.dist[leg], v, turn, recharge, range_km)
            cur.append(leg)
            t, rem = t2, rem2
        if cur:
            raw.append((cur, start_node))
    days = []
    for seq, start_node in raw:
        sim = sim_seq(inst, params, paths, seq, start_node, day_min)
        if sim is None:
            violations.append("маршрут дрона с возвратом длиннее рабочих суток")
            break
        placed = False
        for day in days:
            conn = [] if day["end_core"] == start_node else paths.get((day["end_core"], start_node))
            if conn is None:
                continue
            sim2 = sim_seq(inst, params, paths, day["core"] + list(conn) + seq, day["start"], day_min)
            if sim2:
                day.update(sim2)
                placed = True
                break
        if not placed:
            days.append(sim)
    if violations:
        return {"violations": violations}
    if len(days) > fleet_limit:
        violations.append(f"нужно дронов: {len(days)} > {fleet_limit}")
        return {"violations": violations}
    dep_count = defaultdict(int)
    arr_count = defaultdict(int)
    timeline = []
    for di, day in enumerate(days):
        t, rem = float(start_min), float(range_km)
        seq = []
        for leg in day["full"]:
            if rem < inst.dist[leg]:
                t += recharge
                rem = range_km
            land = t + inst.dist[leg] / v * 60
            if land > end_min:
                violations.append("посадка вне рабочего окна")
                break
            seq.append({"from": inst.names[leg[0]], "to": inst.names[leg[1]], "dep": round(t), "arr": round(land)})
            dep_count[leg[0]] += 1
            arr_count[leg[1]] += 1
            t = land + turn
            rem -= inst.dist[leg]
        timeline.append({"drone": di + 1, "base": inst.names[day["start"]], "flights": seq})
    if violations:
        return {"violations": violations}
    for k in dep_count:
        if dep_count[k] > takeoffs_day:
            violations.append(f"{inst.names[k]}: взлётов {dep_count[k]} > {takeoffs_day}")
        if arr_count[k] > inst.slot_cap[k] / 365:
            violations.append(f"{inst.names[k]}: посадок больше зарядных мест")
    return {
        "violations": violations,
        "drones": len(days),
        "passenger_flights_day": sum(fl.values()),
        "empty_flights_day": sum(empt.values()),
        "timeline": timeline,
    }


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    inst = Instance("data/processed/points_main.csv", "data/processed/demand_main.csv", params)
    plan = json.loads(Path("data/results/sa_main.json").read_text(encoding="utf-8"))
    rev = {v: k for k, v in inst.names.items()}
    oy = {i: None for i in inst.ids}
    for name, y in plan["open_year"].items():
        oy[rev[name]] = y
    fleet = plan["fleet"]
    res = evaluate(inst, oy, fleet)
    cum2 = res["per_year"][0]["cost_rub"] + res["per_year"][1]["cost_rub"]
    open3 = [i for i in inst.ids if oy[i] is not None and oy[i] <= 3]
    new3 = [i for i in open3 if oy[i] == 3]
    alloc = allocate_year(inst, open3, new3, fleet[2], fleet[1], 3, cum2)
    paths = alloc["paths"]
    year3_aggregate = res["per_year"][2]["trips"]
    MAX_LEGS = 24
    legs = set()
    base_D = {}
    for od, tr in sorted(alloc["trips"].items(), key=lambda kv: -kv[1]):
        new = set(paths[od]) - legs
        if base_D and len(legs) + len(new) > MAX_LEGS:
            break
        legs |= set(paths[od])
        base_D[od] = tr
    report = None
    scale = 1.0
    while scale >= 0.3 and report is None:
        D = {od: int(round(tr * scale / 365)) for od, tr in base_D.items()}
        D = {od: n for od, n in D.items() if n > 0}
        day = build_day(inst, params, paths, D, fleet[2])
        if not day["violations"]:
            report = {
                "instance": "main",
                "year": 3,
                "plan_source": "data/results/sa_main.json",
                "day_type": "одни типовые сутки x 365, магистральные коридоры",
                "corridor_od_pairs": len(base_D),
                "active_legs": len(legs),
                "demand_scale": scale,
                "daily_trips": sum(D.values()),
                "annual_trips_converted": 365 * sum(D.values()),
                "year3_aggregate_trips": year3_aggregate,
                "coverage_of_aggregate": round(365 * sum(D.values()) / year3_aggregate, 3),
                "drones": day["drones"],
                "passenger_flights_day": day["passenger_flights_day"],
                "empty_flights_day": day["empty_flights_day"],
                "violations": [],
                "timeline": day["timeline"],
            }
        else:
            last_violations = day["violations"]
            scale = round(scale * 0.95, 3)
    if report is None:
        report = {"instance": "main", "year": 3, "violations": last_violations, "failed_down_to_scale": scale}
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / "schedule_year3.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: v for k, v in report.items() if k != "timeline"}
    if "timeline" in report:
        summary["timeline_preview"] = [
            {"drone": d["drone"], "base": d["base"], "flights_day": len(d["flights"]), "first_flights": d["flights"][:3]}
            for d in report["timeline"]
        ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

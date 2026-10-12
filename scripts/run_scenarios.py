import json
import math
import random
import time
from pathlib import Path

from evaluator import Instance, evaluate
from solve_sa import neighbor


def make_inst(params, q_factor=1.0, range_km=None):
    inst = Instance("data/processed/points_main.csv", "data/processed/demand_main.csv", params)
    if q_factor != 1.0:
        inst.Q = {k: tuple(q * q_factor for q in v) for k, v in inst.Q.items()}
    if range_km is not None:
        inst.tau = {a: t for a, t in inst.tau.items() if inst.dist[a] <= range_km}
        inst.fh = {a: f for a, f in inst.fh.items() if inst.dist[a] <= range_km}
        inst.adj = {i: [] for i in inst.ids}
        for a in inst.tau:
            inst.adj[a[0]].append(a[1])
    return inst


def robust_obj(insts, oy, fleet):
    vals = []
    for inst in insts:
        r = evaluate(inst, oy, fleet)
        vals.append(r["trips"] if r else 0)
    return min(vals)


def worst_failure(inst, oy, fleet):
    worst = None
    for p in [i for i in inst.ids if oy[i] is not None]:
        oy2 = dict(oy)
        oy2[p] = None
        r = evaluate(inst, oy2, fleet)
        v = r["trips"] if r else 0
        if worst is None or v < worst[1]:
            worst = (inst.names[p], v)
    return worst


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    base = make_inst(params)
    plan = json.loads(Path("data/results/sa_main.json").read_text(encoding="utf-8"))
    rev = {v: k for k, v in base.names.items()}
    oy = {i: None for i in base.ids}
    for name, y in plan["open_year"].items():
        oy[rev[name]] = y
    fleet = plan["fleet"]

    scenarios = {
        "base": base,
        "demand_high": make_inst(params, q_factor=1.3),
        "demand_low": make_inst(params, q_factor=0.7),
        "range_160": make_inst(params, range_km=160),
    }
    t0 = time.time()
    base_table = {}
    for name, inst in scenarios.items():
        r = evaluate(inst, oy, fleet)
        base_table[name] = r["trips"] if r else 0
    base_worst = worst_failure(base, oy, fleet)

    insts = list(scenarios.values())
    best = None
    for seed in range(3):
        rng = random.Random(seed)
        cur_oy, cur_fl = dict(oy), list(fleet)
        cur = robust_obj(insts, cur_oy, cur_fl)
        loc = (cur, dict(cur_oy), list(cur_fl))
        T = 3000.0
        for _ in range(300):
            T *= 0.99
            nb = neighbor(cur_oy, cur_fl, rng)
            if nb is None:
                continue
            v = robust_obj(insts, nb[0], nb[1])
            d = v - cur
            if d > 0 or rng.random() < math.exp(d / T):
                cur_oy, cur_fl = nb
                cur = v
                if cur > loc[0]:
                    loc = (cur, dict(cur_oy), list(cur_fl))
        if best is None or loc[0] > best[0]:
            best = loc
    r_oy, r_fl = best[1], best[2]
    robust_table = {}
    for name, inst in scenarios.items():
        r = evaluate(inst, r_oy, r_fl)
        robust_table[name] = r["trips"] if r else 0
    robust_worst = worst_failure(base, r_oy, r_fl)
    wall = time.time() - t0

    out = {
        "base_plan": {"scenarios": base_table, "worst_failure": {"port": base_worst[0], "trips": base_worst[1]}},
        "robust_plan": {
            "scenarios": robust_table,
            "worst_failure": {"port": robust_worst[0], "trips": robust_worst[1]},
            "open_year": {base.names[i]: y for i, y in r_oy.items() if y is not None},
            "fleet": r_fl,
        },
        "price_of_robustness": {
            "loss_in_base_scenario": base_table["base"] - robust_table["base"],
            "gain_in_worst_scenario": min(robust_table.values()) - min(base_table.values()),
            "base_plan_worst": min(base_table.values()),
            "robust_plan_worst": min(robust_table.values()),
        },
        "costs_base_scenario_rub": {
            "base_plan": evaluate(base, oy, fleet)["cum_spend_rub"],
            "robust_plan": evaluate(base, r_oy, r_fl)["cum_spend_rub"],
        },
        "wall_s": round(wall, 1),
        "note": "инфраструктура и флот в каждом сценарии фиксированы; рейсы и маршруты перестраиваются оценщиком",
    }
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / "robustness.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

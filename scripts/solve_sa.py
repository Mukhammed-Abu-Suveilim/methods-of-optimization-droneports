import json
import math
import random
import sys
import time
from pathlib import Path

from evaluator import Instance, evaluate

N_MAX = 12
ITERS = 800
SEEDS = list(range(10))


def neighbor(open_year, fleet, rng):
    oy = dict(open_year)
    fl = list(fleet)
    if rng.random() < 0.6:
        i = rng.choice(list(oy))
        oy[i] = rng.choice([None, 1, 2, 3])
    else:
        t = rng.randrange(3)
        fl[t] += rng.choice([-1, 1])
        if fl[t] < 0:
            return None
        for k in range(1, 3):
            fl[k] = max(fl[k], fl[k - 1])
        if fl[2] > N_MAX:
            return None
    return oy, fl


def sa(inst, init, seed, iters=ITERS):
    rng = random.Random(seed)
    cur_oy, cur_fl = init
    cur = evaluate(inst, cur_oy, cur_fl)
    if cur is None:
        return None
    best = (cur["trips"], dict(cur_oy), list(cur_fl))
    T = 3000.0
    for _ in range(iters):
        T *= 0.995
        nb = neighbor(cur_oy, cur_fl, rng)
        if nb is None:
            continue
        res = evaluate(inst, nb[0], nb[1])
        if res is None:
            continue
        d = res["trips"] - cur["trips"]
        if d > 0 or rng.random() < math.exp(d / T):
            cur_oy, cur_fl = nb
            cur = res
            if cur["trips"] > best[0]:
                best = (cur["trips"], dict(cur_oy), list(cur_fl))
    return best


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "main"
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    inst = Instance(f"data/processed/points_{which}.csv", "data/processed/demand_main.csv", params)
    greedy_plan = json.loads((Path("data/results") / f"greedy_{which}.json").read_text(encoding="utf-8"))
    rev = {v: k for k, v in inst.names.items()}
    init_oy = {i: None for i in inst.ids}
    for name, y in greedy_plan["open_year"].items():
        init_oy[rev[name]] = y
    init = (init_oy, greedy_plan["fleet"])

    runs = []
    t0 = time.time()
    for seed in SEEDS:
        best = sa(inst, init, seed)
        runs.append({"seed": seed, "trips": best[0] if best else None})
    wall = time.time() - t0
    best_overall = max((r for r in runs if r["trips"]), key=lambda r: r["trips"])
    best = sa(inst, init, best_overall["seed"])
    out = {
        "instance": which,
        "method": "simulated_annealing",
        "iters": ITERS,
        "wall_s": round(wall, 1),
        "seed_runs": runs,
        "seed_spread": {
            "min": min(r["trips"] for r in runs),
            "max": max(r["trips"] for r in runs),
            "mean": round(sum(r["trips"] for r in runs) / len(runs), 1),
        },
        "objective_trips": best[0],
        "fleet": best[2],
        "open_year": {inst.names[i]: y for i, y in best[1].items() if y is not None},
        "per_year": evaluate(inst, best[1], best[2])["per_year"],
    }
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / f"sa_{which}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "open_year"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

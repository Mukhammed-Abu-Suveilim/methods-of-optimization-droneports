import json
import sys
import time
from pathlib import Path

from evaluator import Instance, evaluate

N_MAX = 12


def greedy(inst):
    open_year = {i: None for i in inst.ids}
    fleet = [0, 0, 0]
    best_pair = None
    for i, j in inst.tau:
        if i > j:
            continue
        open_year[i] = 1
        open_year[j] = 1
        res = evaluate(inst, open_year, [1, 1, 1])
        open_year[i] = None
        open_year[j] = None
        if res and res["trips"] > 0:
            ratio = res["trips"] / max(1.0, res["cum_spend_rub"])
            if best_pair is None or ratio > best_pair[2]:
                best_pair = (i, j, ratio)
    if best_pair is None:
        return open_year, fleet
    open_year[best_pair[0]] = 1
    open_year[best_pair[1]] = 1
    fleet = [1, 1, 1]
    for t in (1, 2, 3):
        while True:
            base = evaluate(inst, open_year, fleet)
            best = None
            for i in inst.ids:
                if open_year[i] is not None:
                    continue
                open_year[i] = t
                res = evaluate(inst, open_year, fleet)
                open_year[i] = None
                if res and base and res["trips"] > base["trips"]:
                    ratio = (res["trips"] - base["trips"]) / max(1.0, res["cum_spend_rub"] - base["cum_spend_rub"])
                    if best is None or ratio > best[2]:
                        best = ("open", i, ratio)
            for s in (t, t + 1, t + 2):
                cand = list(fleet)
                for k in range(s - 1, 3):
                    cand[k] += 1
                if cand[2] > N_MAX:
                    continue
                res = evaluate(inst, open_year, cand)
                if res and base and res["trips"] > base["trips"]:
                    ratio = (res["trips"] - base["trips"]) / max(1.0, res["cum_spend_rub"] - base["cum_spend_rub"])
                    if best is None or ratio > best[2]:
                        best = ("drone", s, ratio)
            if best is None:
                break
            if best[0] == "open":
                open_year[best[1]] = t
            else:
                for k in range(best[1] - 1, 3):
                    fleet[k] += 1
    return open_year, fleet


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "main"
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    inst = Instance(f"data/processed/points_{which}.csv", "data/processed/demand_main.csv", params)
    t0 = time.time()
    open_year, fleet = greedy(inst)
    wall = time.time() - t0
    res = evaluate(inst, open_year, fleet)
    out = {
        "instance": which,
        "method": "greedy",
        "wall_s": round(wall, 1),
        "objective_trips": res["trips"],
        "fleet": fleet,
        "open_year": {inst.names[i]: y for i, y in open_year.items() if y is not None},
        "per_year": res["per_year"],
        "cum_spend_rub": res["cum_spend_rub"],
    }
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / f"greedy_{which}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

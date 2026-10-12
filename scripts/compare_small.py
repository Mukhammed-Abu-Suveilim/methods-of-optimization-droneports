import json
from pathlib import Path

from evaluator import Instance, evaluate
from solve_greedy import greedy
from solve_sa import sa


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    inst = Instance("data/processed/points_small.csv", "data/processed/demand_main.csv", params)
    rev = {v: k for k, v in inst.names.items()}

    milp = json.loads(Path("data/results/milp_small.json").read_text(encoding="utf-8"))
    oy = {i: None for i in inst.ids}
    for row in milp["per_year"]:
        for name in row["open_ports"]:
            if oy[rev[name]] is None:
                oy[rev[name]] = row["year"]
    fleet = [row["fleet"] for row in milp["per_year"]]
    milp_eval = evaluate(inst, oy, fleet)

    g_oy, g_fl = greedy(inst)
    g_res = evaluate(inst, g_oy, g_fl)

    best = None
    for seed in range(5):
        r = sa(inst, (g_oy, g_fl), seed, iters=400)
        if r and (best is None or r[0] > best[0]):
            best = r

    table = {
        "milp_exact_trips": milp["objective_trips"],
        "milp_plan_under_evaluator": milp_eval["trips"] if milp_eval else None,
        "greedy_evaluator": g_res["trips"],
        "sa_evaluator": best[0],
        "note": "evaluator - общий эвристический критерий для greedy/SA; MILP - точная модель на тех же данных",
    }
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / "compare_small.json").write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(table, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

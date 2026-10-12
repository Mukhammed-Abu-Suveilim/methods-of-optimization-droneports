import json
from pathlib import Path

from evaluator import Instance, allocate_year, evaluate
from solve_greedy import N_MAX, greedy


def year_trips(inst, oy, fleet, t, cum):
    open_t = [i for i in inst.ids if oy.get(i) is not None and oy[i] <= t]
    new_t = [i for i in open_t if oy[i] == t]
    alloc = allocate_year(inst, open_t, new_t, fleet[t - 1], fleet[t - 2] if t > 1 else 0, t, cum)
    if alloc is None:
        return None, None
    trips = sum(min(int(tr), inst.Q[od][t - 1]) for od, tr in alloc["trips"].items())
    return trips, alloc["cost"]


def staged_greedy(inst):
    oy = {i: None for i in inst.ids}
    fleet = [0, 0, 0]
    cum = 0.0
    best_pair = None
    for i, j in inst.tau:
        if i > j:
            continue
        oy[i] = 1
        oy[j] = 1
        tr, _ = year_trips(inst, oy, [1, 1, 1], 1, 0.0)
        r = evaluate(inst, oy, [1, 1, 1])
        oy[i] = None
        oy[j] = None
        if tr and r and tr > 0:
            ratio = tr / max(1.0, r["cum_spend_rub"])
            if best_pair is None or ratio > best_pair[2]:
                best_pair = (i, j, ratio)
    if best_pair is None:
        return oy, fleet
    oy[best_pair[0]] = 1
    oy[best_pair[1]] = 1
    fleet = [1, 1, 1]
    for t in (1, 2, 3):
        while True:
            base_trips, _ = year_trips(inst, oy, fleet, t, cum)
            if base_trips is None:
                break
            r_base = evaluate(inst, oy, fleet)
            best = None
            for i in inst.ids:
                if oy[i] is not None:
                    continue
                oy[i] = t
                tr, _ = year_trips(inst, oy, fleet, t, cum)
                r_cand = evaluate(inst, oy, fleet)
                oy[i] = None
                if tr is not None and r_cand and tr > base_trips:
                    ratio = (tr - base_trips) / max(1.0, r_cand["cum_spend_rub"] - r_base["cum_spend_rub"])
                    if best is None or ratio > best[2]:
                        best = ("open", i, ratio)
            for s in (t, t + 1, t + 2):
                cand = list(fleet)
                for k in range(s - 1, 3):
                    cand[k] += 1
                if cand[2] > N_MAX:
                    continue
                tr, _ = year_trips(inst, oy, cand, t, cum)
                r_cand = evaluate(inst, oy, cand)
                if tr is not None and r_cand and tr > base_trips:
                    ratio = (tr - base_trips) / max(1.0, r_cand["cum_spend_rub"] - r_base["cum_spend_rub"])
                    if best is None or ratio > best[2]:
                        best = ("drone", s, ratio)
            if best is None:
                break
            if best[0] == "open":
                oy[best[1]] = t
            else:
                for k in range(best[1] - 1, 3):
                    fleet[k] += 1
        _, cost = year_trips(inst, oy, fleet, t, cum)
        cum += cost or 0.0
    return oy, fleet


def summary(inst, oy, fleet):
    r = evaluate(inst, oy, fleet)
    return {
        "per_year_trips": [row["trips"] for row in r["per_year"]],
        "total": r["trips"],
        "cum_spend_rub": r["cum_spend_rub"],
        "fleet": fleet,
        "open_year": {inst.names[i]: y for i, y in oy.items() if y is not None},
    }


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    inst = Instance("data/processed/points_main.csv", "data/processed/demand_main.csv", params)
    j_oy, j_fl = greedy(inst)
    joint_greedy = summary(inst, j_oy, j_fl)
    st_oy, st_fl = staged_greedy(inst)
    staged = summary(inst, st_oy, st_fl)
    plan = json.loads(Path("data/results/sa_main.json").read_text(encoding="utf-8"))
    rev = {v: k for k, v in inst.names.items()}
    sa_oy = {i: None for i in inst.ids}
    for name, y in plan["open_year"].items():
        sa_oy[rev[name]] = y
    joint_sa = summary(inst, sa_oy, plan["fleet"])
    out = {
        "joint_greedy": joint_greedy,
        "joint_sa": joint_sa,
        "staged_greedy": staged,
        "myopia_cost_vs_joint_greedy": joint_greedy["total"] - staged["total"],
        "per_year_delta_staged_minus_joint_greedy": [
            staged["per_year_trips"][t] - joint_greedy["per_year_trips"][t] for t in range(3)
        ],
        "note": "оба режима на одном замороженном спросе и правилах финансирования; поэтапный режим оптимизирует только текущий год, решения необратимы",
    }
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / "staged_vs_joint.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "staged_greedy"} | {"staged_greedy": {k: v for k, v in staged.items() if k != "open_year"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

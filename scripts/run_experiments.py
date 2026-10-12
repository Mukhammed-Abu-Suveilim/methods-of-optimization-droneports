import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from build_dataset import WHITELIST, is_settlement
from check_variant import haversine_km
from evaluator import Instance, evaluate
from solve_greedy import greedy
from solve_sa import sa


def gravity_demand(pts, params, out_csv):
    dm = params["demand"]
    recs = pts.to_dict("records")
    rows = []
    for o in recs:
        for d in recs:
            if o["wikidata_id"] != d["wikidata_id"]:
                rows.append((o["wikidata_id"], d["wikidata_id"], round(haversine_km(o, d), 1)))
    od = pd.DataFrame(rows, columns=["origin_id", "destination_id", "distance_km"])
    pop = dict(zip(pts["wikidata_id"], pts["population"]))
    mode_share = od["distance_km"] ** 2 / (od["distance_km"] ** 2 + dm["mode_share_d1_km"] ** 2)
    od["w"] = (od["origin_id"].map(pop) * od["destination_id"].map(pop)) / (od["distance_km"] + dm["d0_km"]) ** dm["gamma"] * mode_share
    row_sum = od.groupby("origin_id")["w"].transform("sum")
    pos_by_origin = od.groupby("origin_id").indices
    for t, growth in enumerate(dm["year_growth"], start=1):
        out = od["origin_id"].map(pop) * dm["trips_per_capita_per_year"] * growth
        q = (out * od["w"] / row_sum).to_numpy()
        base = np.floor(q)
        frac = q - base
        for pos in pos_by_origin.values():
            target = int(round(pop[od.at[pos[0], "origin_id"]] * dm["trips_per_capita_per_year"] * growth))
            need = target - int(base[pos].sum())
            if need > 0:
                pick = pos[np.argsort(-frac[pos], kind="stable")][:need]
                base[pick] += 1
        od[f"q{t}"] = base.astype(int)
    od.drop(columns="w").to_csv(out_csv, index=False)


def select_points(df, out_csv):
    df = df.copy()
    df["letter"] = df["name"].str[0]
    ordered = df.sort_values(["population", "name"], ascending=[False, True], na_position="last")
    main_set = ordered.groupby(["region", "letter"], sort=False).head(5).sort_values(["region", "name"])
    main_set.drop(columns=["letter"]).to_csv(out_csv, index=False)
    return main_set.drop(columns=["letter"])


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    out = {}

    sizes = [12, 25, 50, 75]
    pts_all = pd.read_csv("data/processed/points_main.csv")
    scale_rows = []
    for n in sizes:
        sub = pts_all.sort_values("population", ascending=False).head(n).sort_values(["region", "name"])
        csv = Path(f"data/processed/exp_points_{n}.csv")
        sub.to_csv(csv, index=False)
        inst = Instance(str(csv), "data/processed/demand_main.csv", params)
        t0 = __import__("time").time()
        oy, fl = greedy(inst)
        g = evaluate(inst, oy, fl)
        best = sa(inst, (oy, fl), 0, iters=300)
        wall = __import__("time").time() - t0
        demand_total = sum(sum(q) for q in inst.Q.values())
        scale_rows.append(
            {
                "n": n,
                "greedy_trips": g["trips"],
                "sa_trips": best[0],
                "served_share": round(best[0] / demand_total, 4),
                "wall_s": round(wall, 1),
            }
        )
    out["exp1_scale"] = scale_rows

    fund_rows = []
    for m in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
        p2 = json.loads(json.dumps(params))
        p2["budget"]["tranche_rub_per_year"] = int(params["budget"]["tranche_rub_per_year"] * m)
        inst = Instance("data/processed/points_main.csv", "data/processed/demand_main.csv", p2)
        oy, fl = greedy(inst)
        r = evaluate(inst, oy, fl)
        demand_total = sum(sum(q) for q in inst.Q.values())
        fund_rows.append({"budget_mult": m, "trips": r["trips"], "served_share": round(r["trips"] / demand_total, 4)})
    deltas = [round(fund_rows[i + 1]["served_share"] - fund_rows[i]["served_share"], 4) for i in range(len(fund_rows) - 1)]
    out["exp2_funding"] = {"rows": fund_rows, "share_deltas": deltas, "breakpoint_mult": fund_rows[deltas.index(max(deltas)) + 1]["budget_mult"]}

    raw = pd.read_csv(sorted(Path("data/raw").glob("wikidata_settlements_*.csv"))[-1])
    raw["types_list"] = raw["types"].fillna("").str.split("|")
    raw = raw[raw["types_list"].map(is_settlement)]
    raw["name"] = raw["label"].str.replace(r"^(городской округ|муниципальный округ|городское поселение|сельское поселение)\s+", "", regex=True)
    raw = raw[raw["name"].str[0].isin(["А", "Б", "В", "Ю", "Я"])]
    a = select_points(raw[raw["population"].notna()], "data/processed/exp_dq_A_drop_missing_points.csv")
    first_type = raw["types"].fillna("").str.split("|").str[0]
    medians = raw.assign(ft=first_type).query("population.notna()").groupby("ft")["population"].median()
    filled = raw.assign(ft=first_type)
    filled["population"] = filled["population"].fillna(filled["ft"].map(medians)).fillna(raw["population"].median())
    b = select_points(filled, "data/processed/exp_dq_B_impute_type_median_points.csv")
    dq = {}
    for tag, pts in (("A_drop_missing", a), ("B_impute_type_median", b)):
        gravity_demand(pts, params, f"data/processed/exp_dq_{tag}_demand.csv")
        inst = Instance(f"data/processed/exp_dq_{tag}_points.csv", f"data/processed/exp_dq_{tag}_demand.csv", params)
        oy, fl = greedy(inst)
        r = evaluate(inst, oy, fl)
        dq[tag] = {"points": len(pts), "greedy_trips": r["trips"]}
    dq["overlap_points"] = len(set(a["name"]) & set(b["name"]))
    out["exp3_data_quality"] = dq

    seeds = json.loads(Path("data/results/sa_main.json").read_text(encoding="utf-8"))["seed_runs"]
    greedy_trips = json.loads(Path("data/results/greedy_main.json").read_text(encoding="utf-8"))["objective_trips"]
    vals = [r["trips"] for r in seeds]
    diffs = [v - greedy_trips for v in vals]
    pos = sum(1 for d in diffs if d > 0)
    test = stats.binomtest(pos, len(vals), 0.5, alternative="two-sided")
    out["exp4_randomness"] = {
        "seeds": vals,
        "mean": round(sum(vals) / len(vals), 1),
        "std": round(float(np.std(vals, ddof=1)), 1),
        "greedy": greedy_trips,
        "sign_test_positive": pos,
        "sign_test_pvalue": round(float(test.pvalue), 4),
        "criterion": "знаковый тест для парных различий (SA seed против greedy на той же instance); ANOVA не применяется: нормальность и гомогенность дисперсий не проверялись",
    }

    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / "experiments.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

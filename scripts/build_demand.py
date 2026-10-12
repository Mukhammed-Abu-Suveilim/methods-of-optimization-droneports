import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from check_variant import haversine_km


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    dm = params["demand"]
    pts = pd.read_csv(Path("data/processed/points_main.csv"))
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

    od.drop(columns="w").to_csv(Path("data/processed/demand_main.csv"), index=False)
    Path("data/processed/demand_main.provenance.json").write_text(
        json.dumps(
            {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "parameters_file": "data/parameters.json",
                "parameters_version": params["version"],
                "formula": dm["formula"],
                "gamma": dm["gamma"],
                "d0_km": dm["d0_km"],
                "mode_share_d1_km": dm["mode_share_d1_km"],
                "trips_per_capita_per_year": dm["trips_per_capita_per_year"],
                "year_growth": dm["year_growth"],
                "rounding": dm["rounding"],
                "inputs": ["data/processed/points_main.csv"],
                "status": "спрос зафиксирован до оптимизации",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    names = dict(zip(pts["wikidata_id"], pts["name"]))
    for t in (1, 2, 3):
        col = f"q{t}"
        active = od[od[col] > 0]
        within = active.loc[active["distance_km"] <= 200, col].sum() / active[col].sum()
        print(f"year {t}: trips={active[col].sum()}, active pairs={len(active)}, share within 200 km={within:.3f}")
    for r in od.nlargest(5, "q3").itertuples():
        print(f"  {names[r.origin_id]} -> {names[r.destination_id]}: q3={r.q3}, {r.distance_km} km")


if __name__ == "__main__":
    main()

import json
from pathlib import Path

import pandas as pd

from evaluator import RHO_EMPTY, Instance, allocate_year

params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
pts = pd.read_csv(Path("data/processed/points_main.csv"))
inst = Instance("data/processed/points_main.csv", "data/processed/demand_main.csv", params)
region_of = dict(zip(pts["wikidata_id"], pts["region"]))
plan = json.loads(Path("data/results/sa_main.json").read_text(encoding="utf-8"))
rev = {v: k for k, v in inst.names.items()}
oy = {i: None for i in inst.ids}
for name, y in plan["open_year"].items():
    oy[rev[name]] = y
fleet = plan["fleet"]

out = Path("data/plan")
out.mkdir(parents=True, exist_ok=True)

openings = []
for i in inst.ids:
    if oy[i] is None:
        continue
    hub = inst.pop[i] >= params["droneport"]["hub_if_population_ge"]
    openings.append(
        {
            "пункт": inst.names[i],
            "регион": region_of[i],
            "население": int(inst.pop[i]),
            "тип": "хаб" if hub else "малый",
            "год открытия": oy[i],
            "CAPEX, млн ₽": (params["droneport"]["capex_hub_rub"] if hub else params["droneport"]["capex_small_rub"]) / 1e6,
        }
    )
df_open = pd.DataFrame(openings).sort_values(["год открытия", "пункт"])

cum = 0.0
prev = 0
budget = []
flights_by_leg = {}
trips_by_year = {}
for t in (1, 2, 3):
    open_t = [i for i in inst.ids if oy[i] is not None and oy[i] <= t]
    new_t = [i for i in open_t if oy[i] == t]
    alloc = allocate_year(inst, open_t, new_t, fleet[t - 1], prev, t, cum)
    capex = sum(inst.capex[i] for i in new_t)
    drones = fleet[t - 1] - prev
    drone_cost = drones * params["drone"]["price_rub"]
    opex = params["droneport"]["opex_per_year_rub"] * len(open_t)
    flight_cost = alloc["cost"] - capex - drone_cost - opex
    cum += alloc["cost"]
    budget.append(
        {
            "год": t,
            "открыто портов": len(new_t),
            "CAPEX портов, млн ₽": round(capex / 1e6, 1),
            "куплено дронов": drones,
            "затраты на дроны, млн ₽": round(drone_cost / 1e6, 1),
            "OPEX портов, млн ₽": round(opex / 1e6, 1),
            "рейсовые затраты, млн ₽": round(flight_cost / 1e6, 1),
            "итого за год, млн ₽": round(alloc["cost"] / 1e6, 1),
            "накоплено, млн ₽": round(cum / 1e6, 1),
            "накопленный транш, млн ₽": t * params["budget"]["tranche_rub_per_year"] / 1e6,
            "остаток к переносу, млн ₽": round((t * params["budget"]["tranche_rub_per_year"] - cum) / 1e6, 1),
            "поездок за год": sum(min(int(tr), inst.Q[od][t - 1]) for od, tr in alloc["trips"].items()),
        }
    )
    for leg, f in alloc["flights"].items():
        flights_by_leg.setdefault(leg, [0, 0, 0])[t - 1] = f
    trips_by_year[t] = alloc["trips"]
    prev = fleet[t - 1]
df_budget = pd.DataFrame(budget)

rows = []
for leg, fs in sorted(flights_by_leg.items(), key=lambda kv: -kv[1][2]):
    rows.append(
        {
            "участок": f"{inst.names[leg[0]]} — {inst.names[leg[1]]}",
            "длина, км": inst.dist[leg],
            "рейсов год 1": fs[0],
            "рейсов год 2": fs[1],
            "рейсов год 3": fs[2],
        }
    )
df_flights = pd.DataFrame(rows)

od_rows = []
for od, tr in sorted(trips_by_year[3].items(), key=lambda kv: -kv[1])[:25]:
    od_rows.append(
        {
            "маршрут": f"{inst.names[od[0]]} → {inst.names[od[1]]}",
            "спрос год 3": inst.Q[od][2],
            "обслужено год 3": int(min(tr, inst.Q[od][2])),
        }
    )
df_od = pd.DataFrame(od_rows)

for df, name in ((df_open, "openings.csv"), (df_budget, "budget.csv"), (df_flights, "flights_by_leg.csv"), (df_od, "top_od_year3.csv")):
    df.to_csv(out / name, index=False, encoding="utf-8-sig")


def md_table(df):
    head = "| " + " | ".join(df.columns) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep] + body)


md = "\n\n".join(
    [
        "# Таблицы плана (план SA, main instance)",
        "## Открытия дронопортов\n" + md_table(df_open),
        "## Бюджет по годам (млн ₽, перенос остатка)\n" + md_table(df_budget),
        "## Рейсы по участкам за год\n" + md_table(df_flights),
        "## Топ-25 обслуженных пар год 3\n" + md_table(df_od),
    ]
)
(out / "plan_tables.md").write_text(md, encoding="utf-8")
print(md[:2000])
print("saved tables to", out)

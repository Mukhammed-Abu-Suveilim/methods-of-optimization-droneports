import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from evaluator import Instance

params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
pts = pd.read_csv(Path("data/processed/points_main.csv"))
small = pd.read_csv(Path("data/processed/points_small.csv"))
inst = Instance("data/processed/points_main.csv", "data/processed/demand_main.csv", params)
plan = json.loads(Path("data/results/sa_main.json").read_text(encoding="utf-8"))
rev = {v: k for k, v in inst.names.items()}
oy = {i: None for i in inst.ids}
for name, y in plan["open_year"].items():
    oy[rev[name]] = y
coord = dict(zip(pts["wikidata_id"], zip(pts["lon"], pts["lat"])))
COLORS = {1: "tab:green", 2: "tab:orange", 3: "tab:red"}

out = Path("data/maps")
out.mkdir(parents=True, exist_ok=True)


def draw(year):
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.scatter(pts["lon"], pts["lat"], s=18, c="lightgray", edgecolors="gray", label="допустимые пункты варианта")
    if year is None:
        sm = pts[pts["wikidata_id"].isin(small["wikidata_id"])]
        ax.scatter(sm["lon"], sm["lat"], s=90, facecolors="none", edgecolors="blue", label="малая instance (12)")
        edges = {(a, b) for a, b in inst.tau}
        title = "Допустимые пункты и граф перелётов ≤ 200 км"
    else:
        open_t = [i for i in inst.ids if oy[i] is not None and oy[i] <= year]
        edges = {(a, b) for a, b in inst.tau if a in open_t and b in open_t}
        for a, b in edges:
            if (b, a) in edges and a > b:
                continue
            (x1, y1), (x2, y2) = coord[a], coord[b]
            ax.plot([x1, x2], [y1, y2], c="0.6", lw=0.8, zorder=1)
        for y in (1, 2, 3):
            grp = [i for i in open_t if oy[i] == y]
            if grp:
                ax.scatter([coord[i][0] for i in grp], [coord[i][1] for i in grp], s=70, c=COLORS[y], edgecolors="black", label=f"открыты в год {y}", zorder=3)
        for i in open_t:
            ax.annotate(inst.names[i], coord[i], fontsize=7, xytext=(3, 3), textcoords="offset points")
        title = f"Сеть дронопортов, год {year} (открыто портов: {len(open_t)})"
        edges = None
    if year is None:
        for a, b in edges:
            if a > b:
                continue
            (x1, y1), (x2, y2) = coord[a], coord[b]
            ax.plot([x1, x2], [y1, y2], c="0.85", lw=0.5, zorder=0)
    ax.set_title(title)
    ax.set_xlabel("долгота")
    ax.set_ylabel("широта")
    ax.set_aspect(1.6)
    ax.grid(alpha=0.3)
    ax.legend(loc="lower left", fontsize=8)
    fig.tight_layout()
    name = "map_points.png" if year is None else f"map_year{year}.png"
    fig.savefig(out / name, dpi=150)
    plt.close(fig)
    print("saved", out / name)


draw(None)
for t in (1, 2, 3):
    draw(t)

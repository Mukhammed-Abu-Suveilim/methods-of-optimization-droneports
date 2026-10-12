import sys
from itertools import combinations
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import networkx as nx
import pandas as pd

MAX_LEG_KM = 200.0


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(radians, [a["lat"], a["lon"], b["lat"], b["lon"]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))


def main():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        csvs = sorted(Path("data/raw").glob("wikidata_settlements_*.csv"))
        path = csvs[-1]
    df = pd.read_csv(path)
    g = nx.Graph()
    g.add_nodes_from(df["wikidata_id"])
    recs = df.to_dict("records")
    for a, b in combinations(recs, 2):
        if haversine_km(a, b) <= MAX_LEG_KM:
            g.add_edge(a["wikidata_id"], b["wikidata_id"])
    comps = sorted(nx.connected_components(g), key=len, reverse=True)
    print("points:", len(df))
    print("edges (<=200 km):", g.number_of_edges())
    print("components:", len(comps), "sizes:", [len(c) for c in comps][:10])
    print("isolated points:", sum(1 for c in comps if len(c) == 1))


if __name__ == "__main__":
    main()

from itertools import combinations
from pathlib import Path

import networkx as nx
import pandas as pd

from check_variant import MAX_LEG_KM, haversine_km

QUOTA = 5
SMALL_SIZE = 12

WHITELIST = {
    "город",
    "населённый пункт",
    "село",
    "деревня",
    "посёлок",
    "посёлок городского типа России",
    "посёлок станции",
    "станция как населённый пункт",
    "железнодорожная станция как населённый пункт",
    "железнодорожный разъезд как населённый пункт",
    "хутор",
}


def is_settlement(types):
    return any(t in WHITELIST for t in types)


def graph_of(df):
    g = nx.Graph()
    g.add_nodes_from(df["wikidata_id"])
    for a, b in combinations(df.to_dict("records"), 2):
        if haversine_km(a, b) <= MAX_LEG_KM:
            g.add_edge(a["wikidata_id"], b["wikidata_id"])
    return g


def main():
    raw = sorted(Path("data/raw").glob("wikidata_settlements_*.csv"))[-1]
    df = pd.read_csv(raw)
    df["types_list"] = df["types"].fillna("").str.split("|")
    dropped = df[~df["types_list"].map(is_settlement)]
    df = df[df["types_list"].map(is_settlement)]
    print("candidates after cleaning:", len(df), "(dropped", len(dropped), ")")
    print(dropped["types"].value_counts().head(8))
    df["letter"] = df["name"].str[0]
    ordered = df.sort_values(["population", "name"], ascending=[False, True], na_position="last")
    main_set = ordered.groupby(["region", "letter"], sort=False).head(QUOTA).sort_values(["region", "name"])

    out = Path("data/processed")
    out.mkdir(parents=True, exist_ok=True)
    main_set.drop(columns=["letter", "types_list"]).to_csv(out / "points_main.csv", index=False)

    top = main_set.sort_values("population", ascending=False, na_position="last").head(SMALL_SIZE)
    small = top
    if not nx.is_connected(graph_of(top)):
        g = graph_of(main_set)
        center = max(g.nodes, key=g.degree)
        names = dict(zip(main_set["wikidata_id"], main_set["name"]))
        lengths = nx.single_source_shortest_path_length(g, center)
        order = sorted(lengths.items(), key=lambda kv: (kv[1], names[kv[0]]))
        ids = [k for k, _ in order[:SMALL_SIZE]]
        small = main_set[main_set["wikidata_id"].isin(ids)]
    small.drop(columns=["letter", "types_list"]).to_csv(out / "points_small.csv", index=False)

    print("main points:", len(main_set))
    print(main_set.groupby(["region", "letter"]).size().unstack(fill_value=0))
    print("main connected:", nx.is_connected(graph_of(main_set)))
    print("main missing population share:", round(main_set["population"].isna().mean(), 3))
    print("small points:", len(small), "connected:", nx.is_connected(graph_of(small)))
    print("small:", ", ".join(small["name"]))


if __name__ == "__main__":
    main()

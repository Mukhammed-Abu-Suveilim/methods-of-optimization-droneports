import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

NAME_PREFIX_RE = re.compile(r"^(городской округ|муниципальный округ|городское поселение|сельское поселение)\s+")

ENDPOINT = "https://query.wikidata.org/sparql"
REGIONS = ["Псковская область", "Новгородская область", "Тверская область"]
LETTERS = ["А", "Б", "В", "Ю", "Я"]

QUERY = """
SELECT ?item ?name ?coord (MAX(?popRaw) AS ?pop) ?regionLabel (GROUP_CONCAT(DISTINCT ?typeLabel; SEPARATOR="|") AS ?types) WHERE {
  VALUES ?regionLabel { %s }
  ?region rdfs:label ?regionLabel .
  ?item wdt:P131+ ?region ;
        wdt:P625 ?coord ;
        rdfs:label ?name .
  FILTER(LANG(?name) = "ru")
  FILTER(%s)
  OPTIONAL { ?item wdt:P1082 ?popRaw . }
  OPTIONAL { ?item wdt:P31 ?t . ?t rdfs:label ?typeLabel . FILTER(LANG(?typeLabel) = "ru") }
}
GROUP BY ?item ?name ?coord ?regionLabel
"""


def build_query():
    values = " ".join(f'"{r}"@ru' for r in REGIONS)
    prefixes = "(городской округ |муниципальный округ |городское поселение |сельское поселение )?"
    letter_filter = f'REGEX(?name, "^{prefixes}[{"".join(LETTERS)}]")'
    return QUERY % (values, letter_filter)


def parse_coord(value):
    inner = value[value.index("(") + 1 : value.index(")")]
    lon, lat = inner.split()
    return float(lat), float(lon)


def main():
    query = build_query()
    resp = requests.get(
        ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"User-Agent": "droneports-study-project/0.1 (educational research)"},
        timeout=180,
    )
    resp.raise_for_status()
    bindings = resp.json()["results"]["bindings"]

    rows = []
    for b in bindings:
        lat, lon = parse_coord(b["coord"]["value"])
        rows.append(
            {
                "wikidata_id": b["item"]["value"].rsplit("/", 1)[-1],
                "label": b["name"]["value"],
                "name": NAME_PREFIX_RE.sub("", b["name"]["value"]),
                "region": b["regionLabel"]["value"],
                "lat": lat,
                "lon": lon,
                "population": int(b["pop"]["value"]) if "pop" in b else None,
                "types": b["types"]["value"] if "types" in b else "",
            }
        )

    df = pd.DataFrame(rows).drop_duplicates("wikidata_id").sort_values(["region", "name"])
    out_dir = Path("data/raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    csv_path = out_dir / f"wikidata_settlements_{stamp}.csv"
    df.to_csv(csv_path, index=False)
    (out_dir / f"wikidata_settlements_{stamp}.provenance.json").write_text(
        json.dumps(
            {
                "source": ENDPOINT,
                "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                "license": "CC0 (Wikidata)",
                "query": query,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"rows: {len(df)} -> {csv_path}")
    print(df.groupby(["region", df["name"].str[0]]).size().unstack(fill_value=0))
    print("missing population share:", round(df["population"].isna().mean(), 3))


if __name__ == "__main__":
    main()

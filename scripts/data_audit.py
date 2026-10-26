"""Воспроизведение отбора и спроса без перезаписи замороженного v1."""

from __future__ import annotations

import csv
import io
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import requests

from scripts.model_data import ROOT, input_hashes, load_instance, write_json
from scripts.model_types import InstanceData

SETTLEMENT_TYPES = {
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
REGIONS = {"Псковская область", "Новгородская область", "Тверская область"}
LETTERS = set("АБВЮЯ")


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Вернуть неокруглённое географическое расстояние в км."""
    a, b, c, d = map(math.radians, (lat1, lon1, lat2, lon2))
    value = math.sin((c - a) / 2) ** 2
    value += math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 12742 * math.asin(min(1, math.sqrt(value)))


def graph_of(instance: InstanceData, range_km: float = 200) -> nx.Graph[str]:
    """Сохранить все узлы, включая изолированные."""
    graph: nx.Graph[str] = nx.Graph()
    graph.add_nodes_from(instance["ids"])
    for pair, distance in instance["distance"].items():
        if distance <= range_km * 10:
            graph.add_edge(*pair)
    return graph


def gravity_demand(points: pd.DataFrame) -> pd.DataFrame:
    """Воспроизвести нормированный по отправителю спрос v1."""
    # 1. Сохранить исходный порядок строк для стабильного округления.
    raw = json.loads((ROOT / "data/parameters.json").read_text("utf-8"))
    settings = raw["demand"]
    records = list(points.to_dict("records"))
    rows: list[tuple[str, str, float]] = []
    for origin in records:
        for destination in records:
            if origin["wikidata_id"] != destination["wikidata_id"]:
                distance = haversine(
                    float(origin["lat"]),
                    float(origin["lon"]),
                    float(destination["lat"]),
                    float(destination["lon"]),
                )
                rows.append(
                    (
                        str(origin["wikidata_id"]),
                        str(destination["wikidata_id"]),
                        round(distance, 1),
                    )
                )
    frame = pd.DataFrame(
        rows, columns=["origin_id", "destination_id", "distance_km"]
    )
    population = dict(zip(points["wikidata_id"], points["population"]))
    distance_series = frame["distance_km"]
    mode = distance_series**2 / (
        distance_series**2 + settings["mode_share_d1_km"] ** 2
    )
    weights = frame["origin_id"].map(population) * frame["destination_id"].map(
        population
    )
    weights = (
        weights
        / (distance_series + settings["d0_km"]) ** settings["gamma"]
        * mode
    )
    total = weights.groupby(frame["origin_id"]).transform("sum")
    indices = frame.groupby("origin_id").indices

    # 2. Распределить округлённый годовой итог каждой строки по
    # остаткам.
    for year, growth in enumerate(settings["year_growth"], 1):
        outgoing = frame["origin_id"].map(population)
        outgoing = outgoing * settings["trips_per_capita_per_year"] * growth
        quantity = (outgoing * weights / total).to_numpy(dtype=float)
        base = np.floor(quantity)
        remainder = quantity - base
        for index_values in indices.values():
            positions = np.asarray(index_values, dtype=np.int64)
            point_id = str(frame.at[positions[0], "origin_id"])
            target = round(
                float(population[point_id])
                * settings["trips_per_capita_per_year"]
                * growth
            )
            need = target - int(base[positions].sum())
            chosen = positions[
                np.argsort(-remainder[positions], kind="stable")
            ][: max(0, need)]
            base[chosen] += 1
        frame[f"q{year}"] = base.astype(int)
    return frame


def select_points(frame: pd.DataFrame) -> pd.DataFrame:
    """Применить фиксированные квоты регион × буква."""
    cleaned = frame.copy()
    cleaned["letter"] = cleaned["name"].str[0]
    cleaned = cleaned.sort_values(
        ["population", "name"], ascending=[False, True], na_position="last"
    )
    selected = cleaned.groupby(["region", "letter"], sort=False).head(5)
    return selected.sort_values(["region", "name"]).drop(columns="letter")


def clean_candidates() -> pd.DataFrame:
    """
    Прочитать конкретный архив, не выбирать более поздний файл молча.
    """
    frame = pd.read_csv(ROOT / "data/raw/wikidata_settlements_20261012.csv")
    valid = (
        frame["types"]
        .fillna("")
        .str.split("|")
        .map(lambda values: bool(SETTLEMENT_TYPES.intersection(values)))
    )
    return frame.loc[valid].copy()


def audit_data(output: Path) -> dict[str, object]:
    """
    Проверить вариант и воспроизвести отбор и спрос во временном
    каталоге.
    """
    # 1. Воспроизвести основной набор и спрос без записи в исходные
    # пути.
    output.mkdir(parents=True, exist_ok=True)
    selected = select_points(clean_candidates())
    selected.to_csv(output / "points_main.csv", index=False)
    frozen = pd.read_csv(ROOT / "data/processed/points_main.csv")
    pd.testing.assert_frame_equal(selected.reset_index(drop=True), frozen)
    reproduced = gravity_demand(frozen)
    reproduced.to_csv(output / "demand_main.csv", index=False)
    original = pd.read_csv(ROOT / "data/processed/demand_main.csv")
    pd.testing.assert_frame_equal(reproduced, original)
    small = frozen.sort_values("population", ascending=False).head(12)
    saved_small = pd.read_csv(ROOT / "data/processed/points_small.csv")
    if set(small["wikidata_id"]) != set(saved_small["wikidata_id"]):
        raise ValueError("Малый набор отличается от правила top-12")
    small.to_csv(output / "points_small.csv", index=False)

    # 2. Проверить территорию, связность и распределения без удаления
    # узлов.
    summary: dict[str, object] = {
        "reproduction_passed": True,
        "input_hashes": input_hashes(),
    }
    raw = pd.read_csv(ROOT / "data/raw/wikidata_settlements_20261012.csv")
    candidates = clean_candidates()
    summary["candidate_audit"] = {
        "raw_rows": len(raw),
        "settlement_rows": len(candidates),
        "excluded_non_settlements": len(raw) - len(candidates),
        "missing_population": int(candidates["population"].isna().sum()),
        "duplicate_ids": int(candidates["wikidata_id"].duplicated().sum()),
        "missing_coordinates": int(
            candidates[["lat", "lon"]].isna().any(axis=1).sum()
        ),
    }
    for label in ("main", "small"):
        instance = load_instance(label)
        graph = graph_of(instance)
        for point in instance["points"].values():
            if (
                point["region"] not in REGIONS
                or point["name"][0] not in LETTERS
            ):
                raise ValueError("Нарушен региональный или буквенный фильтр")
            if not (-90 <= point["lat"] <= 90 and -180 <= point["lon"] <= 180):
                raise ValueError("Некорректные координаты")
        names = Counter(p["name"] for p in instance["points"].values())
        population = pd.Series(
            [p["population"] for p in instance["points"].values()]
        )
        q1, q3 = population.quantile([0.25, 0.75])
        outliers = [
            p["id"]
            for p in instance["points"].values()
            if p["population"] > q3 + 1.5 * (q3 - q1)
            or p["population"] < q1 - 1.5 * (q3 - q1)
        ]
        unreachable = [
            pair
            for pair in instance["demand"]
            if not nx.has_path(graph, *pair)
        ]
        summary[label] = {
            "points": len(instance["ids"]),
            "edges": graph.number_of_edges(),
            "components": nx.number_connected_components(graph),
            "isolated": list(nx.isolates(graph)),
            "duplicate_names": {
                n: count for n, count in names.items() if count > 1
            },
            "demand": [
                sum(q[t] for q in instance["demand"].values())
                for t in range(3)
            ],
            "population_quantiles": population.quantile(
                [0, 0.25, 0.5, 0.75, 1]
            ).to_dict(),
            "population_iqr_outliers_retained": outliers,
            "unreachable_od_pairs_retained": len(unreachable),
            "unreachable_demand": [
                sum(instance["demand"][pair][t] for pair in unreachable)
                for t in range(3)
            ],
            "asymmetric_od_pairs": sum(
                q != instance["demand"][b, a]
                for (a, b), q in instance["demand"].items()
            ),
        }
    write_json(output / "audit.json", summary)
    return summary


def population_metadata(instance: InstanceData, output: Path) -> None:
    """
    Восстановить датированные утверждения, совпадающие с населением
    v1.
    """
    # 1. Запросить утверждения P1082, оставляя недоступные годы явными.
    observations: list[dict[str, str | float]] = []
    queries: list[dict[str, str]] = []
    endpoint = "https://query.wikidata.org/sparql"
    for offset in range(0, len(instance["ids"]), 15):
        batch = instance["ids"][offset : offset + 15]
        values = " ".join(
            f"(wd:{p} {instance['points'][p]['population']:.0f})"
            for p in batch
        )
        query = (
            "SELECT ?item ?population ?date WHERE { VALUES (?item ?target) { "
            + values
            + " } ?item p:P1082 ?statement . "
            "?statement ps:P1082 ?population . FILTER(?population=?target) "
            "OPTIONAL { ?statement pq:P585 ?date } }"
        )
        error = ""
        matched = {point_id: set[str]() for point_id in batch}
        try:
            response = requests.get(
                endpoint,
                params={"query": query, "format": "json"},
                timeout=45,
                headers={"User-Agent": "droneports-study/2"},
            )
            response.raise_for_status()
            for row in response.json()["results"]["bindings"]:
                point_id = row["item"]["value"].rsplit("/", 1)[-1]
                matched[point_id].add(row.get("date", {}).get("value", ""))
        except (requests.RequestException, ValueError) as exception:
            error = str(exception)
        queries.append({"query": query, "error": error})

        # 2. Сохранить совпадения и не подставлять произвольный год.
        for point_id in batch:
            dates = matched[point_id] - {""}
            status = (
                "matched"
                if len(dates) == 1
                else ("ambiguous" if len(dates) > 1 else "unavailable")
            )
            observations.append(
                {
                    "wikidata_id": point_id,
                    "population": instance["points"][point_id]["population"],
                    "observation_dates": "|".join(sorted(dates)),
                    "status": status,
                }
            )
    output.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(observations[0]))
    writer.writeheader()
    writer.writerows(observations)
    (output / "population_observations.csv").write_text(
        buffer.getvalue(), encoding="utf-8", newline=""
    )
    write_json(
        output / "population.provenance.json",
        {
            "source": endpoint,
            "retrieved_utc": datetime.now(timezone.utc).isoformat(),
            "input_hashes": instance["hashes"],
            "queries": queries,
            "rule": (
                "match frozen population exactly; no imputed observation year"
            ),
        },
    )

"""Проверка числа точек и связности по географическим расстояниям."""

import csv
import sys
from itertools import combinations
from pathlib import Path

import networkx as nx

MAX_LEG_KM = 200.0


def main() -> None:
    """Прочитать CSV без записи и вывести характеристики графа."""
    # 1. Поддержать прямой запуск и выбрать файл из аргумента.
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.data_audit import haversine

    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else (root / "data/processed/points_main.csv")
    )
    with path.open(encoding="utf-8-sig", newline="") as source:
        points = list(csv.DictReader(source))
    graph: nx.Graph[str] = nx.Graph()
    graph.add_nodes_from(row["wikidata_id"] for row in points)

    # 2. Вычислить рёбра и сохранить изолированные пункты в графе.
    for a, b in combinations(points, 2):
        distance = haversine(
            float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])
        )
        if distance <= MAX_LEG_KM:
            graph.add_edge(a["wikidata_id"], b["wikidata_id"])
    components = sorted(nx.connected_components(graph), key=len, reverse=True)
    print("points:", len(points))
    print("edges (<=200 km):", graph.number_of_edges())
    print("components:", len(components), "sizes:", list(map(len, components)))
    print("isolated points:", len(list(nx.isolates(graph))))


if __name__ == "__main__":
    main()

"""Сопоставимое сравнение BFS, A* и Dijkstra по одинаковому графу."""

from __future__ import annotations

import heapq
import time
from collections import deque

import networkx as nx

from scripts.data_audit import haversine
from scripts.model_data import MODEL_VERSION, flight_seconds
from scripts.model_types import InstanceData, Plan


def breadth_first(
    adjacency: dict[str, list[str]],
    source: str,
    target: str | None = None,
) -> tuple[dict[str, str | None], int]:
    """Выполнить BFS для одной цели либо для всех пунктов."""
    # 1. Обойти слои, считая именно раскрытые, а не найденные узлы.
    parents: dict[str, str | None] = {source: None}
    queue = deque([source])
    expanded = 0
    while queue:
        node = queue.popleft()
        expanded += 1
        if node == target:
            break
        for neighbor in adjacency[node]:
            if neighbor not in parents:
                parents[neighbor] = node
                queue.append(neighbor)
    return parents, expanded


def reconstruct(parents: dict[str, str | None], target: str) -> list[str]:
    """Восстановить найденный путь по предкам."""
    path = [target]
    while parents[path[-1]] is not None:
        previous = parents[path[-1]]
        assert previous is not None
        path.append(previous)
    return path[::-1]


def astar(
    instance: InstanceData,
    graph: nx.DiGraph[str],
    source: str,
    target: str,
    criterion: str,
) -> tuple[list[str], int]:
    """
    Использовать безопасную географическую оценку с учётом округления.
    """
    # 1. Вычесть максимум ошибки расстояния из географической оценки.
    points = instance["points"]
    settings = instance["settings"]

    def heuristic(node: str) -> float:
        """Оценить оставшееся время или стоимость снизу."""
        if node == target:
            return 0.0
        a, b = points[node], points[target]
        geographical = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        safe_distance = max(0, geographical - 0.05 * (len(graph) - 1))
        hours = safe_distance / settings["cruise_speed_kmh"]
        return hours * (
            3600
            if criterion == "time"
            else settings["cost_per_flight_hour_rub"] / settings["seats"]
        )

    # 2. Допустить повторное раскрытие при улучшении g для надёжности.
    queue: list[tuple[float, float, str]] = [(heuristic(source), 0, source)]
    distance = {source: 0.0}
    parents: dict[str, str | None] = {source: None}
    expanded = 0
    while queue:
        _, value, node = heapq.heappop(queue)
        if value > distance[node] + 1e-8:
            continue
        expanded += 1
        if node == target:
            return reconstruct(parents, target), expanded
        for neighbor in graph[node]:
            candidate = value + float(graph[node][neighbor][criterion])
            if candidate + 1e-8 < distance.get(neighbor, float("inf")):
                distance[neighbor] = candidate
                parents[neighbor] = node
                heapq.heappush(
                    queue,
                    (candidate + heuristic(neighbor), candidate, neighbor),
                )
    return [], expanded


def compare_routes(instance: InstanceData, plan: Plan) -> dict[str, object]:
    """Сравнить одиночные запросы и одинаковые пакеты всех OD."""
    # 1. Собрать сеть итогового года с двумя аддитивными критериями.
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(sorted(plan["open_year"]))
    settings = instance["settings"]
    for pair, units in instance["distance"].items():
        if set(pair) <= graph.nodes and units <= settings["range_km"] * 10:
            graph.add_edge(
                *pair,
                time=flight_seconds(instance, pair)
                + settings["turnaround_min"] * 60,
                cost=units
                / 10
                / settings["cruise_speed_kmh"]
                * settings["cost_per_flight_hour_rub"]
                / settings["seats"],
            )
    adjacency = {p: sorted(graph[p]) for p in graph}
    rows: list[dict[str, object]] = []
    timings: dict[str, float] = {
        "bfs_single_s": 0,
        "astar_time_s": 0,
        "astar_cost_s": 0,
        "bfs_batch_s": 0,
    }
    expansions = {
        "bfs_single": 0,
        "bfs_batch": 0,
        "astar_time": 0,
        "astar_cost": 0,
    }

    # 2. Проверить каждый путь A* по оптимуму Dijkstra.
    for source in graph:
        start = time.perf_counter()
        all_parents, expanded = breadth_first(adjacency, source)
        for destination in all_parents:
            if destination != source:
                reconstruct(all_parents, destination)
        timings["bfs_batch_s"] += time.perf_counter() - start
        expansions["bfs_batch"] += expanded
        for target in graph:
            if target == source or not nx.has_path(graph, source, target):
                continue
            start = time.perf_counter()
            parents, expanded = breadth_first(adjacency, source, target)
            bfs_path = reconstruct(parents, target)
            timings["bfs_single_s"] += time.perf_counter() - start
            expansions["bfs_single"] += expanded
            row: dict[str, object] = {
                "origin": source,
                "destination": target,
                "bfs_hops": len(bfs_path) - 1,
            }
            for criterion in ("time", "cost"):
                start = time.perf_counter()
                path, expanded = astar(
                    instance, graph, source, target, criterion
                )
                timings[f"astar_{criterion}_s"] += time.perf_counter() - start
                expansions[f"astar_{criterion}"] += expanded
                optimum = nx.shortest_path_length(
                    graph, source, target, weight=criterion
                )
                actual = nx.path_weight(graph, path, criterion)
                if abs(actual - optimum) > 1e-6:
                    raise ValueError("A* не совпал с Dijkstra")
                row[f"astar_{criterion}"] = actual
                row[f"astar_{criterion}_hops"] = len(path) - 1
                row[f"bfs_{criterion}"] = nx.path_weight(
                    graph, bfs_path, criterion
                )
            rows.append(row)
    return {
        "model_version": MODEL_VERSION,
        "input_hashes": instance["hashes"],
        "source_plan": plan,
        "graph_kind": "potential arcs between all year-3 open ports",
        "batch_workload": "construct every reachable destination path",
        "pairs": len(rows),
        "rows": rows,
        "timings": timings,
        "expansions": expansions,
        "dijkstra_agreement": True,
        "time_unit": "seconds; flight+turnaround; no timetable waiting",
        "heuristic_proof": (
            "A simple remaining path has <=n-1 edges. Each rounded edge "
            "can be 0.05 km shorter than geographical distance. Subtract "
            "0.05*(n-1) before applying speed/cost; ignore service time."
        ),
    }

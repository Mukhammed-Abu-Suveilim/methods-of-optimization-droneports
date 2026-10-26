"""Построение рейсов с остатком дальности и назначение пассажиров."""

import copy
import hashlib
import math
from collections import OrderedDict, defaultdict

import networkx as nx
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from scripts.model_data import (
    DAY_WEIGHTS,
    charging_slots,
    flight_cost,
    flight_seconds,
)
from scripts.model_types import Day, InstanceData, Journey, Scenario

DAY_CACHE: OrderedDict[str, list[Day]] = OrderedDict()
PASSENGER_CACHE: OrderedDict[
    str, tuple[list[list[Journey]], dict[str, str | int | float]]
] = OrderedDict()


def available_time(times: list[int], proposed: int, capacity: int) -> int:
    """Найти безопасное время добавления движения в скользящий час."""
    # 1. Проверить все часовые окна, содержащие добавляемое движение.
    while True:
        combined = sorted(times + [proposed])
        conflicts = [
            start
            for start in combined
            if start <= proposed
            and proposed < start + 3600
            and sum(start <= t < start + 3600 for t in combined) > capacity
        ]
        if not conflicts:
            return proposed
        # 2. Перенести движение за последнее конфликтующее окно.
        proposed = max(start + 3600 for start in conflicts)


def available_charge(
    intervals: list[tuple[int, int]],
    start: int,
    duration: int,
    slots: int,
) -> int:
    """Найти интервал зарядки с ограничением одновременного занятия."""
    # 1. Проверить начало и все изменения занятости внутри интервала.
    while True:
        probes = {start} | {
            a for a, _ in intervals if start <= a < start + duration
        }
        conflicts = [
            t for t in probes if sum(a <= t < b for a, b in intervals) >= slots
        ]
        if not conflicts:
            return start
        # 2. Дождаться освобождения хотя бы одного занятого места.
        first = min(conflicts)
        start = min(b for a, b in intervals if a <= first < b)


def recharge(
    instance: InstanceData,
    day: Day,
    drone: int,
    port: str,
    ready: int,
) -> int:
    """Добавить зарядку после обслуживания и вернуть её окончание."""
    duration = math.ceil(instance["settings"]["recharge_min"] * 60)
    occupied = [
        (c["start_s"], c["end_s"]) for c in day["charges"] if c["port"] == port
    ]
    start = available_charge(
        occupied, ready, duration, charging_slots(instance, port)
    )
    day["charges"].append(
        {
            "drone": drone,
            "port": port,
            "start_s": start,
            "end_s": start + duration,
        }
    )
    return start + duration


def append_route(
    instance: InstanceData,
    day: Day,
    drone: int,
    route: list[str],
    ready: int,
    battery: int,
    range_units: int,
) -> tuple[int, int]:
    """Добавить путь, заряжая дрон только при нехватке остатка."""
    # 1. Последовательно обслужить участки с реальной зарядной ёмкостью.
    settings = instance["settings"]
    capacity = int(settings["takeoffs_per_hour"])
    turnaround = math.ceil(settings["turnaround_min"] * 60)
    for origin, destination in zip(route, route[1:]):
        pair = origin, destination
        distance = instance["distance"][pair]
        if distance > range_units:
            raise ValueError("Участок длиннее доступной дальности")
        if battery < distance:
            ready = recharge(instance, day, drone, origin, ready)
            battery = range_units

        # 2. Согласовать взлёт и посадку с уже построенными рейсами.
        duration = flight_seconds(instance, pair)
        departures = [
            f["departure_s"] for f in day["flights"] if f["origin"] == origin
        ]
        arrivals = [
            f["arrival_s"]
            for f in day["flights"]
            if f["destination"] == destination
        ]
        while True:
            departure = available_time(departures, ready, capacity)
            arrival = available_time(arrivals, departure + duration, capacity)
            if arrival == departure + duration:
                break
            ready = arrival - duration
        day["flights"].append(
            {
                "id": f"f{len(day['flights'])}",
                "drone": drone,
                "origin": origin,
                "destination": destination,
                "departure_s": departure,
                "arrival_s": arrival,
            }
        )
        ready = arrival + turnaround
        battery -= distance
    return ready, battery


def port_paths(
    instance: InstanceData,
    opened: list[str],
    scenario: Scenario,
) -> dict[tuple[str, str], list[str]]:
    """
    Получить пути по действующим портам для конструирования рейсов.
    """
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(opened)
    for pair, distance in instance["distance"].items():
        if pair[0] in graph and pair[1] in graph:
            if distance <= scenario["range_km"] * 10:
                graph.add_edge(*pair, weight=flight_seconds(instance, pair))
    return {
        (origin, destination): path
        for origin, paths in nx.all_pairs_dijkstra_path(graph)
        for destination, path in paths.items()
        if origin != destination
    }


def construct_day(
    instance: InstanceData,
    opened: list[str],
    bases: list[str],
    year: int,
    scenario: Scenario,
    daily_budget: float,
    weight: int,
    residual: bool = False,
) -> Day:
    """
    Составить допустимые круговые задания с общими ресурсами портов.
    """
    # 1. Распределить потенциальный спрос по кратчайшим путям.
    settings = instance["settings"]
    paths = port_paths(instance, opened, scenario)
    demand_load: dict[tuple[str, str], float] = defaultdict(float)
    for pair, quantities in instance["demand"].items():
        quantity = math.floor(
            quantities[year - 1] * scenario["demand_multiplier"]
        )
        target = quantity % 364 if residual else quantity / 365
        path = paths.get(pair, [])
        for leg in zip(path, path[1:]):
            demand_load[leg] += target
    day: Day = {
        "weight": weight,
        "bases": bases,
        "flights": [],
        "charges": [],
        "journeys": [],
    }
    end = int(settings["day_end_hour"] * 3600)
    full = round(scenario["range_km"] * 10)
    spent = 0.0

    # 2. Выбирать полезные круги; каждую попытку завершать проверкой
    # возврата.
    for drone, base in enumerate(bases):
        if base not in opened:
            continue
        ready = int(settings["day_start_hour"] * 3600)
        battery = full
        while True:
            best: tuple[float, Day, int, int, float, list[str]] | None = None
            for destination in opened:
                if (base, destination) not in paths:
                    continue
                route = paths[base, destination] + paths[destination, base][1:]
                benefit = sum(
                    min(settings["seats"], demand_load[leg])
                    for leg in zip(route, route[1:])
                )
                if benefit <= 0:
                    continue
                cost = sum(
                    flight_cost(instance, leg) for leg in zip(route, route[1:])
                )
                if spent + cost > daily_budget + 1e-8:
                    continue
                trial: Day = {
                    "weight": day["weight"],
                    "bases": day["bases"],
                    "flights": list(day["flights"]),
                    "charges": list(day["charges"]),
                    "journeys": [],
                }
                finish, remaining = append_route(
                    instance, trial, drone, route, ready, battery, full
                )
                occupied = [
                    (c["start_s"], c["end_s"])
                    for c in trial["charges"]
                    if c["port"] == base
                ]
                duration = math.ceil(settings["recharge_min"] * 60)
                charged = available_charge(
                    occupied, finish, duration, charging_slots(instance, base)
                )
                charged += duration
                if charged > end:
                    continue
                score = benefit / max(1, finish - ready)
                if best is None or score > best[0]:
                    best = score, trial, finish, remaining, cost, route
            if best is None:
                break
            _, day, ready, battery, cost, selected = best
            spent += cost
            for leg in zip(selected, selected[1:]):
                demand_load[leg] = max(0, demand_load[leg] - settings["seats"])

        # 3. Восстановить полный заряд в базе для повторения суток.
        if battery < full:
            recharge(instance, day, drone, base, ready)
    return day


def itinerary_options(
    instance: InstanceData,
    day: Day,
    year: int,
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Перечислить временные пути без пространственных циклов."""
    # 1. Подготовить рейсы по отправлению для обхода ориентированного
    # времени.
    flights = sorted(day["flights"], key=lambda f: f["departure_s"])
    outgoing: dict[str, list[int]] = defaultdict(list)
    for index, flight in enumerate(flights):
        outgoing[flight["origin"]].append(index)
    transfer = math.ceil(instance["settings"]["turnaround_min"] * 60)
    options: list[tuple[str, str, tuple[str, ...]]] = []

    # 2. Удаление пространственного цикла заменяется ожиданием на месте.
    for origin in outgoing:
        stack: list[tuple[str, int, tuple[str, ...], frozenset[str]]] = [
            (origin, 0, (), frozenset([origin]))
        ]
        while stack:
            port, ready, used, visited = stack.pop()
            for index in outgoing.get(port, []):
                flight = flights[index]
                destination = flight["destination"]
                if flight["departure_s"] < ready or destination in visited:
                    continue
                route = used + (flight["id"],)
                if (
                    instance["demand"].get((origin, destination), (0, 0, 0))[
                        year - 1
                    ]
                    > 0
                ):
                    options.append((origin, destination, route))
                stack.append(
                    (
                        destination,
                        flight["arrival_s"] + transfer,
                        route,
                        visited | {destination},
                    )
                )
    return options


def assign_passengers(
    instance: InstanceData,
    days: list[Day],
    year: int,
    scenario: Scenario,
) -> dict[str, str | int | float]:
    """Решить целочисленное распределение мест восьми типов суток."""
    # 1. Переиспользовать только полностью совпадающую задачу мест.
    key = hashlib.sha256(
        repr(
            (
                instance["demand"],
                year,
                scenario["demand_multiplier"],
                instance["settings"]["seats"],
                instance["settings"]["turnaround_min"],
                [(day["weight"], day["flights"]) for day in days],
            )
        ).encode()
    ).hexdigest()
    for day in days:
        day["journeys"] = []
    if key in PASSENGER_CACHE:
        PASSENGER_CACHE.move_to_end(key)
        assignments, metadata = PASSENGER_CACHE[key]
        for day, journeys in zip(days, assignments):
            day["journeys"] = copy.deepcopy(journeys)
        return dict(metadata)

    # 2. Создать целые количества пассажиров каждого временного пути.
    entries: list[tuple[int, str, str, tuple[str, ...]]] = []
    by_leg: dict[tuple[int, str], list[int]] = defaultdict(list)
    by_od: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for day_index, day in enumerate(days):
        for origin, destination, route in itinerary_options(
            instance, day, year
        ):
            variable = len(entries)
            entries.append((day_index, origin, destination, route))
            by_od[origin, destination].append((day["weight"], variable))
            for flight_id in route:
                by_leg[day_index, flight_id].append(variable)
    if not entries:
        return {"status": "empty", "variables": 0}

    # 3. Ограничить места на рейсах и общий годовой спрос OD.
    row_indices: list[int] = []
    column_indices: list[int] = []
    coefficients: list[float] = []
    limits: list[float] = []
    for variables in by_leg.values():
        row_indices.extend([len(limits)] * len(variables))
        column_indices.extend(variables)
        coefficients.extend([1.0] * len(variables))
        limits.append(instance["settings"]["seats"])
    for pair, weighted_variables in by_od.items():
        for weight, variable in weighted_variables:
            row_indices.append(len(limits))
            column_indices.append(variable)
            coefficients.append(float(weight))
        limits.append(
            float(
                math.floor(
                    instance["demand"][pair][year - 1]
                    * scenario["demand_multiplier"]
                )
            )
        )
    matrix = coo_matrix(
        (
            np.array(coefficients),
            (
                np.array(row_indices, dtype=np.int32),
                np.array(column_indices, dtype=np.int32),
            ),
        ),
        shape=(len(limits), len(entries)),
    ).tocsc()
    objective = np.array(
        [-days[d]["weight"] for d, _, _, _ in entries], dtype=float
    )
    solved = milp(
        objective,
        integrality=np.ones(len(entries), dtype=np.int32),
        bounds=Bounds(0, instance["settings"]["seats"]),
        constraints=LinearConstraint(matrix, -np.inf, limits),
        options={"mip_rel_gap": 0.0},
    )
    if not solved.success or solved.x is None:
        raise RuntimeError("Не найден оптимум распределения пассажиров")

    # 4. Сохранить маршруты для независимого пересчёта целевой.
    for index, (day_index, origin, destination, route) in enumerate(entries):
        count = round(float(solved.x[index]))
        if count:
            journey: Journey = {
                "origin": origin,
                "destination": destination,
                "count": count,
                "flights": list(route),
            }
            days[day_index]["journeys"].append(journey)
    metadata = {
        "status": "optimal",
        "variables": len(entries),
        "objective": round(float(-solved.fun)),
    }
    PASSENGER_CACHE[key] = (
        [copy.deepcopy(day["journeys"]) for day in days],
        metadata,
    )
    if len(PASSENGER_CACHE) > 64:
        PASSENGER_CACHE.popitem(last=False)
    return metadata


def build_days(
    instance: InstanceData,
    opened: list[str],
    bases: list[str],
    year: int,
    scenario: Scenario,
    annual_budget: float,
) -> list[Day]:
    """
    Построить неделю и отдельные остаточные сутки на общей дислокации.
    """
    # 1. Насыщение рабочего времени задаёт безопасный предел рейсовых
    # затрат.
    settings = instance["settings"]
    maximum = (
        len(bases)
        * (settings["day_end_hour"] - settings["day_start_hour"])
        * settings["cost_per_flight_hour_rub"]
        * 365
    )
    annual_budget = min(annual_budget, maximum)
    key = hashlib.sha256(
        repr(
            (
                instance["demand"],
                instance["distance"],
                settings,
                opened,
                bases,
                year,
                scenario["demand_multiplier"],
                scenario["range_km"],
                round(annual_budget, 6),
            )
        ).encode()
    ).hexdigest()
    if key in DAY_CACHE:
        DAY_CACHE.move_to_end(key)
        return DAY_CACHE[key]
    # 1. Повторить регулярное задание семь раз с весом 52.
    regular = construct_day(
        instance,
        opened,
        bases,
        year,
        scenario,
        max(0, annual_budget) / 365,
        52,
    )
    days = [copy.deepcopy(regular) for _ in range(7)]

    # 2. Для последнего дня учесть редкий спрос; общий бюджет не
    # превышается.
    residual = construct_day(
        instance,
        opened,
        bases,
        year,
        scenario,
        max(0, annual_budget) / 365,
        1,
        residual=True,
    )
    days.append(residual)
    assert [d["weight"] for d in days] == DAY_WEIGHTS
    assign_passengers(instance, days, year, scenario)
    DAY_CACHE[key] = days
    if len(DAY_CACHE) > 256:
        DAY_CACHE.popitem(last=False)
    return days

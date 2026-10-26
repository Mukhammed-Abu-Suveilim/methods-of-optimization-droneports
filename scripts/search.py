"""
Жадный поиск и отжиг по одному проверяемому операционному критерию.
"""

import ast
import copy
import math
import random
import time
from collections.abc import Callable

from scripts.evaluation import evaluate_plan, rank
from scripts.model_data import (
    fixed_cost,
    lifetime_bases,
    plan_errors,
    port_cost,
)
from scripts.model_types import (
    InstanceData,
    OperatingResult,
    Plan,
    Scenario,
    SearchCheckpoint,
)

Progress = Callable[[str], None]


def potential(instance: InstanceData, plan: Plan, year: int) -> float:
    """
    Получить направляющую оценку спроса без объявления допустимости.
    """
    opened = {p for p, t in plan["open_year"].items() if t <= year}
    return sum(
        q[year - 1]
        for pair, q in instance["demand"].items()
        if set(pair) <= opened
    )


def seed_plan(
    instance: InstanceData, scenario: Scenario, staged: bool = False
) -> Plan:
    """Выбрать проверенный старт среди восьми перспективных пар."""
    # 1. Отсортировать разрешённые пары по спросу на инвестиции.
    pairs = [
        pair
        for pair, d in instance["distance"].items()
        if pair[0] < pair[1] and d <= scenario["range_km"] * 10
    ]
    pairs.sort(
        key=lambda pair: (
            -sum(
                (
                    instance["demand"][od][0]
                    if staged
                    else sum(instance["demand"][od])
                )
                for od in (pair, pair[::-1])
            )
            / (
                port_cost(instance, pair[0])
                + port_cost(instance, pair[1])
                + instance["settings"]["price_rub"]
            )
        )
    )
    best: OperatingResult | None = None
    best_score = (-1, float("-inf"))
    empty: Plan = {"open_year": {}, "fleet": [0, 0, 0], "bases": [[], [], []]}

    # 2. Сравнить фактические расписания; нулевой план всегда допустим.
    for pair in pairs[:8]:
        plan: Plan = {
            "open_year": dict.fromkeys(pair, 1),
            "fleet": [1, 1, 1],
            "bases": [[], [], []],
        }
        result = evaluate_plan(instance, plan, scenario)
        score = rank(result)
        if staged and result["valid"]:
            score = (
                result["years"][0]["trips"],
                -result["years"][0]["cost_rub"],
            )
        if score > best_score:
            best, best_score = result, score
    return best["plan"] if best and best["valid"] else empty


def candidate_moves(
    instance: InstanceData, plan: Plan, year: int
) -> list[Plan]:
    """Предложить открытия, закупки и совместные инвестиции."""
    # 1. Рассмотреть каждый ещё не построенный порт и одну закупку.
    moves: list[Plan] = []
    for point_id in instance["ids"]:
        if point_id not in plan["open_year"]:
            candidate = copy.deepcopy(plan)
            candidate["open_year"][point_id] = year
            moves.append(candidate)
            combined = copy.deepcopy(candidate)
            combined["fleet"][year - 1 :] = [
                n + 1 for n in plan["fleet"][year - 1 :]
            ]
            moves.append(combined)
    candidate = copy.deepcopy(plan)
    candidate["fleet"][year - 1 :] = [n + 1 for n in plan["fleet"][year - 1 :]]
    moves.append(candidate)

    # 2. Отсеять невозможные инвестиции и явно ограничить скрининг.
    feasible = [p for p in moves if not plan_errors(instance, p)]
    base = potential(instance, plan, year)
    feasible.sort(
        key=lambda p: (
            -(potential(instance, p, year) - base + 1)
            / max(
                1,
                fixed_cost(instance, p, year)
                - fixed_cost(instance, plan, year),
            )
        )
    )
    return feasible[:12]


def greedy_search(
    instance: InstanceData,
    scenario: Scenario,
    staged: bool = False,
    progress: Progress = print,
) -> OperatingResult:
    """Выполнить жадный поиск с явным скринингом и полной проверкой."""
    # 1. Найти допустимую стартовую сеть.
    current = evaluate_plan(
        instance, seed_plan(instance, scenario, staged), scenario
    )
    for year in (1, 2, 3):
        while True:
            best = current
            ratio_best = 0.0
            for plan in candidate_moves(instance, current["plan"], year):
                result = evaluate_plan(instance, plan, scenario)
                if not result["valid"]:
                    continue
                gain = result["objective_trips"] - current["objective_trips"]
                expense = result["cum_spend_rub"] - current["cum_spend_rub"]
                if staged:
                    gain = (
                        result["years"][year - 1]["trips"]
                        - current["years"][year - 1]["trips"]
                    )
                    expense = (
                        result["years"][year - 1]["cost_rub"]
                        - current["years"][year - 1]["cost_rub"]
                    )
                ratio = gain / max(1.0, expense)
                if gain > 0 and ratio > ratio_best:
                    ratio_best, best = ratio, result
            if best is current:
                break
            current = best
            progress(
                f"greedy {instance['label']} year={year} "
                f"trips={current['objective_trips']}"
            )
    return current


def neighbor(instance: InstanceData, plan: Plan, rng: random.Random) -> Plan:
    """Изменить открытие, флот, обмен портов или дислокацию."""
    # 1. Копировать только изменяемые структуры стратегического решения.
    candidate = copy.deepcopy(plan)
    operation = rng.randrange(5)
    year = rng.randrange(3)
    point_id = rng.choice(instance["ids"])
    if operation <= 1:
        opening = rng.choice([0, 1, 2, 3])
        if opening:
            candidate["open_year"][point_id] = opening
        else:
            candidate["open_year"].pop(point_id, None)
    elif operation == 2:
        fleet = candidate["fleet"]
        fleet[year] += rng.choice([-1, 1])
        for t in range(1, 3):
            fleet[t] = max(fleet[t], fleet[t - 1])
    elif operation == 3 and candidate["open_year"]:
        old = rng.choice(list(candidate["open_year"]))
        opening = candidate["open_year"].pop(old)
        candidate["open_year"][point_id] = opening
    else:
        if candidate["fleet"][year] > 0:
            drone = rng.randrange(candidate["fleet"][year])
            purchased = next(
                t for t in range(3) if candidate["fleet"][t] > drone
            )
            opened = [
                p
                for p, t in candidate["open_year"].items()
                if t <= purchased + 1
            ]
            if opened:
                candidate = lifetime_bases(instance, candidate)
                base = rng.choice(opened)
                for t in range(purchased, 3):
                    candidate["bases"][t][drone] = base
    if not plan_errors(instance, candidate):
        candidate = lifetime_bases(instance, candidate)
    return candidate


def anneal(
    instance: InstanceData,
    scenario: Scenario,
    initial: OperatingResult,
    seed: int,
    progress: Progress = print,
    checkpoint: Callable[[OperatingResult, dict[str, object]], None]
    | None = None,
    batch_size: int = 800,
    resume_state: SearchCheckpoint | None = None,
    resume_best: OperatingResult | None = None,
) -> tuple[OperatingResult, dict[str, object]]:
    """Искать до трёх последовательных серий без улучшения."""
    # 1. Зафиксировать независимую случайность и начальную температуру.
    rng = random.Random(seed)
    current = best = initial
    temperature = max(1.0, initial["objective_trips"] * 0.01)
    floor_temperature = temperature * 0.001
    stale = proposals = batches = accepted = 0
    started = time.perf_counter()
    elapsed_before = 0.0
    if resume_state is not None:
        if (
            resume_state["seed"] != seed
            or resume_state["initial_configuration_hash"]
            != initial["configuration_hash"]
        ):
            raise ValueError("Контрольная точка относится к другому старту")
        rng.setstate(ast.literal_eval(resume_state["rng_state"]))
        current = evaluate_plan(
            instance, resume_state["current_plan"], scenario
        )
        temperature = resume_state["temperature"]
        stale, proposals = (
            resume_state["stale_batches"],
            resume_state["proposals"],
        )
        batches, accepted = resume_state["batches"], resume_state["accepted"]
        elapsed_before = resume_state["wall_s"]
        if resume_best is not None:
            best = resume_best

    # 2. Принимать допустимых соседей по изменению завершённых поездок.
    while stale < 3:
        improved = False
        for _ in range(batch_size):
            proposals += 1
            temperature = max(floor_temperature, temperature * 0.995)
            plan = neighbor(instance, current["plan"], rng)
            if plan_errors(instance, plan):
                continue
            result = evaluate_plan(instance, plan, scenario)
            if not result["valid"]:
                continue
            delta = result["objective_trips"] - current["objective_trips"]
            if delta > 0 or rng.random() < math.exp(
                min(0, delta / temperature)
            ):
                current = result
                accepted += 1
            if rank(result) > rank(best):
                best, improved = result, True
        batches += 1
        stale = 0 if improved else stale + 1
        metadata: dict[str, object] = {
            "seed": seed,
            "proposals": proposals,
            "batches": batches,
            "accepted": accepted,
            "stale_batches": stale,
            "temperature": temperature,
            "rng_state": repr(rng.getstate()),
            "wall_s": elapsed_before + time.perf_counter() - started,
            "current_plan": current["plan"],
            "initial_configuration_hash": initial["configuration_hash"],
            "initial_trips": initial["objective_trips"],
            "best_trips": best["objective_trips"],
            "stopping_rule": "3 batches without lexicographic improvement",
        }
        progress(
            f"SA {instance['label']} seed={seed} batch={batches} "
            f"trips={best['objective_trips']} stale={stale}"
        )
        if checkpoint:
            checkpoint(best, metadata)
    return best, metadata

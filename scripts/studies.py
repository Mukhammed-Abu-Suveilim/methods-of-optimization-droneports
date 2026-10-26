"""
Воспроизводимые серии поиска, устойчивость и обязательные
эксперименты.
"""

import copy
import hashlib
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.data_audit import clean_candidates, gravity_demand, select_points
from scripts.evaluation import (
    CACHE,
    evaluate_plan,
    evaluate_recourse,
    rank,
    read_result,
    remove_unused_ports,
    result_metrics,
)
from scripts.model_data import (
    MODEL_VERSION,
    OUTPUT,
    RECOURSE_VERSION,
    ROOT,
    base_scenario,
    load_instance,
    store_instance,
    write_json,
)
from scripts.model_types import (
    InstanceData,
    OperatingResult,
    Plan,
    Scenario,
    SearchCheckpoint,
)
from scripts.operations import DAY_CACHE, PASSENGER_CACHE
from scripts.search import anneal, candidate_moves, greedy_search


def configuration_directory(instance: InstanceData) -> Path:
    """
    Однозначно связать каталог с данными и параметрами эксперимента.
    """
    digest = hashlib.sha256(
        (MODEL_VERSION + repr(instance)).encode()
    ).hexdigest()[:16]
    return OUTPUT / "configurations" / f"{instance['label']}_{digest}"


def run_seed(
    instance: InstanceData,
    initial: OperatingResult,
    seed: int,
    resume: bool,
) -> tuple[OperatingResult, dict[str, object]]:
    """Вычислить seed с точным восстановлением состояния партии."""
    # 1. Проверить результат, исходный старт и состояние генератора.
    directory = configuration_directory(instance)
    path = directory / f"seed_{seed}.json"
    metadata_path = directory / f"seed_{seed}_metadata.json"
    checkpoint_path = directory / f"seed_{seed}_checkpoint.json"
    scenario = base_scenario()
    previous: OperatingResult | None = None
    state: SearchCheckpoint | None = None
    if resume and path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text("utf-8"))
        previous = read_result(path, instance, scenario)
        if (
            metadata.get("complete")
            and metadata.get("initial_configuration_hash")
            == initial["configuration_hash"]
        ):
            return previous, metadata
        if checkpoint_path.exists():
            combined = json.loads(checkpoint_path.read_text("utf-8"))
            metadata = combined["metadata"]
            previous = combined["best_result"]
            from scripts.validation import validate_result

            if validate_result(instance, previous, scenario):
                raise ValueError("Повреждён результат контрольной точки")
        if (
            metadata.get("initial_configuration_hash")
            == initial["configuration_hash"]
            and "current_plan" in metadata
        ):
            state = metadata
            if state["stale_batches"] >= 3:
                metadata["complete"] = True
                write_json(metadata_path, metadata)
                return previous, metadata
    if state is None:
        previous = None

    # 2. Сохранять оба состояния одним атомарным файлом партии.
    def checkpoint(
        result: OperatingResult, metadata: dict[str, object]
    ) -> None:
        """Сохранить лучшую сеть и состояние продолжения поиска."""
        write_json(
            checkpoint_path, {"metadata": metadata, "best_result": result}
        )
        write_json(path, result)
        write_json(metadata_path, metadata)

    result, metadata = anneal(
        instance,
        scenario,
        initial,
        seed,
        checkpoint=checkpoint,
        resume_state=state,
        resume_best=previous,
    )
    metadata["complete"] = True
    checkpoint(result, metadata)
    return result, metadata


def run_configuration(
    instance: InstanceData,
    seeds: list[int] | None = None,
    resume: bool = True,
) -> tuple[OperatingResult, dict[str, object]]:
    """
    Рассчитать greedy, staged и независимые серии SA с контрольными
    файлами.
    """
    # 1. Возобновить только проверенные результаты с теми же входами.
    output = configuration_directory(instance)
    output.mkdir(parents=True, exist_ok=True)
    store_instance(output / "instance.json", instance)
    scenario = base_scenario()
    timing_file = output / "deterministic_timings.json"
    timings: dict[str, float] = {}
    if timing_file.exists():
        timings = json.loads(timing_file.read_text("utf-8"))
    if timings.get("_version") != 2:
        timings = {"_version": 2.0}
    methods: dict[str, OperatingResult] = {}
    for name, staged_mode in (("greedy", False), ("staged", True)):
        path = output / f"{name}.json"
        if resume and path.exists() and name in timings:
            methods[name] = read_result(path, instance, scenario)
        else:
            CACHE.clear()
            DAY_CACHE.clear()
            PASSENGER_CACHE.clear()
            started = time.perf_counter()
            methods[name] = greedy_search(
                instance, scenario, staged=staged_mode
            )
            timings[name] = time.perf_counter() - started
            write_json(path, methods[name])
            write_json(timing_file, timings)
    greedy, staged = methods["greedy"], methods["staged"]
    initial = max((greedy, staged), key=rank)
    best = initial
    records: list[dict[str, object]] = []
    quantities: list[int] = []

    # 2. Сохранить каждый seed отдельно, не перезапуская готовые серии.
    selected_seeds = list(range(10)) if seeds is None else seeds
    if len(selected_seeds) > 1:
        with ProcessPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(run_seed, instance, initial, seed, resume)
                for seed in selected_seeds
            ]
            outputs = [future.result() for future in futures]
    else:
        outputs = [
            run_seed(instance, initial, seed, resume)
            for seed in selected_seeds
        ]
    for result, metadata in outputs:
        records.append(metadata)
        quantities.append(result["objective_trips"])
        if rank(result) > rank(best):
            best = result
    write_json(output / "best.json", best)

    # 3. Одинаково очистить все методы, сохранив исходный ход поиска.
    postprocessing_start = time.perf_counter()
    published = output / "published"
    cleaned_greedy = remove_unused_ports(instance, greedy)
    cleaned_staged = remove_unused_ports(instance, staged)
    write_json(published / "greedy.json", cleaned_greedy)
    write_json(published / "staged.json", cleaned_staged)
    final_best = max((cleaned_greedy, cleaned_staged), key=rank)
    quantities = []
    for seed, (result, metadata) in zip(selected_seeds, outputs):
        cleaned = max(
            (
                remove_unused_ports(instance, result),
                cleaned_greedy,
                cleaned_staged,
            ),
            key=rank,
        )
        write_json(published / f"seed_{seed}.json", cleaned)
        metadata["published_trips"] = cleaned["objective_trips"]
        metadata["published_cost_rub"] = cleaned["cum_spend_rub"]
        quantities.append(cleaned["objective_trips"])
        if rank(cleaned) > rank(final_best):
            final_best = cleaned
    write_json(published / "best.json", final_best)

    # 4. Описать распределение, не тестируя превосходство над своим
    # стартом.
    values = np.array(quantities, dtype=float)
    statistics: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "input_hashes": instance["hashes"],
        "instance": instance["label"],
        "configuration": output.relative_to(ROOT).as_posix(),
        "greedy": cleaned_greedy["objective_trips"],
        "staged": cleaned_staged["objective_trips"],
        "best": final_best["objective_trips"],
        "best_cost_rub": final_best["cum_spend_rub"],
        "raw_best": best["objective_trips"],
        "raw_best_cost_rub": best["cum_spend_rub"],
        "postprocessing": "published/: remove unused ports; retain duties",
        "publication_version": 1,
        "postprocessing_wall_s": time.perf_counter() - postprocessing_start,
        "demand_total": sum(sum(q) for q in instance["demand"].values()),
        "seed_runs": records,
        "seed_objectives": quantities,
        "greedy_wall_s": timings["greedy"],
        "staged_wall_s": timings["staged"],
        "metrics": {
            "greedy": result_metrics(instance, cleaned_greedy),
            "staged": result_metrics(instance, cleaned_staged),
            "best": result_metrics(instance, final_best),
        },
    }
    if quantities:
        differences = values - cleaned_greedy["objective_trips"]
        generator = np.random.default_rng(0)
        boot = generator.choice(differences, (10000, len(values))).mean(axis=1)
        statistics.update(
            {
                "mean": float(values.mean()),
                "median": float(np.median(values)),
                "std": float(values.std(ddof=1)) if len(values) > 1 else 0.0,
                "min": int(values.min()),
                "max": int(values.max()),
                "mean_gain_bootstrap_95": np.quantile(
                    boot, [0.025, 0.975]
                ).tolist(),
                "bootstrap_seed": 0,
                "inference_note": (
                    "Conditional on this instance; no sign-test/ANOVA"
                ),
            }
        )
    write_json(output / "statistics.json", statistics)
    return final_best, statistics


def scenarios(instance: InstanceData) -> list[Scenario]:
    """
    Задать все стрессы, включая отказ каждого допустимого порта в году
    3.
    """
    result = [base_scenario()]
    for name, factor, distance in (
        ("low", 0.7, 200),
        ("high", 1.3, 200),
        ("range160", 1.0, 160),
    ):
        scenario = base_scenario()
        scenario["name"] = name
        scenario["demand_multiplier"] = factor
        scenario["range_km"] = float(distance)
        result.append(scenario)
    for point_id in instance["ids"]:
        scenario = base_scenario()
        scenario["name"] = f"failure_{point_id}"
        scenario["closed_port"] = point_id
        result.append(scenario)
    return result


def stress_values(
    instance: InstanceData,
    result: OperatingResult,
    output: Path | None = None,
) -> tuple[int, list[dict[str, object]], OperatingResult]:
    """
    Перестроить перевозки, сохраняя инвестиции, флот и расходы портов.
    """
    # 1. Пересчитать только затронутые отказы; остальные тождественны
    # базе.
    rows: list[dict[str, object]] = []
    worst = result
    minimum = result["objective_trips"]
    for scenario in scenarios(instance):
        if (
            scenario["closed_port"]
            and scenario["closed_port"] not in result["plan"]["open_year"]
        ):
            operating = result
        else:
            operating = evaluate_recourse(instance, result, scenario)
        trips = operating["objective_trips"] if operating["valid"] else 0
        if output is not None:
            reference = (
                "base"
                if operating is result and scenario["closed_port"]
                else scenario["name"]
            )
            if reference == scenario["name"]:
                write_json(
                    output / f"{reference}.json",
                    {"scenario": scenario, "result": operating},
                )
        rows.append(
            {
                "scenario": scenario["name"],
                "trips": trips,
                "cost_rub": operating["cum_spend_rub"],
                "valid": operating["valid"],
                "per_year": [y["trips"] for y in operating["years"]],
                "schedule_reference": (
                    "base"
                    if operating is result and scenario["closed_port"]
                    else scenario["name"]
                ),
            }
        )
        if trips < minimum:
            worst = operating
            minimum = trips
    return minimum, rows, worst


def robust_candidate(
    instance: InstanceData, plan: Plan
) -> tuple[int, OperatingResult]:
    """Оценить кандидата по полному множеству стрессов."""
    result = evaluate_plan(instance, plan, base_scenario())
    if not result["valid"]:
        return -1, result
    minimum, _, _ = stress_values(instance, result)
    return minimum, result


def robust_moves(
    instance: InstanceData, result: OperatingResult
) -> list[Plan]:
    """Добавить удаления, перенос ввода и обмен портов."""
    # 1. Рассмотреть обычные совместные инвестиции по каждому году.
    plans = [
        plan
        for year in (1, 2, 3)
        for plan in candidate_moves(instance, result["plan"], year)
    ]
    alternative = sorted(
        (p for p in instance["ids"] if p not in result["plan"]["open_year"]),
        key=lambda p: -instance["points"][p]["population"],
    )[:5]

    # 2. Разрешить перераспределение, когда свободного бюджета уже нет.
    for old, opening in result["plan"]["open_year"].items():
        removed = copy.deepcopy(result["plan"])
        del removed["open_year"][old]
        plans.append(removed)
        for year in (1, 2, 3):
            shifted = copy.deepcopy(result["plan"])
            shifted["open_year"][old] = year
            plans.append(shifted)
        for new in alternative:
            exchanged = copy.deepcopy(removed)
            exchanged["open_year"][new] = opening
            plans.append(exchanged)
    return list({json.dumps(p, sort_keys=True): p for p in plans}.values())


def robustness(
    instance: InstanceData,
    baseline: OperatingResult,
    output: Path,
    resume: bool = True,
) -> dict[str, object]:
    """Улучшать max-min детерминированным локальным поиском."""
    # 1. Сравнить базу со всеми уже проверенными кандидатами серии.
    previous = output / "robustness.json"
    baseline_state_hash = hashlib.sha256(
        json.dumps(baseline, sort_keys=True).encode()
    ).hexdigest()
    if resume and previous.exists():
        cached: dict[str, object] = json.loads(previous.read_text("utf-8"))
        if (
            cached.get("baseline_hash") == baseline["configuration_hash"]
            and cached.get("baseline_state_hash") == baseline_state_hash
            and cached.get("recourse_version") == RECOURSE_VERSION
        ):
            read_result(output / "robust_plan.json", instance, base_scenario())
            return cached
    best = baseline
    value, base_rows, base_worst = stress_values(instance, baseline)
    best_value = value
    candidates = [baseline]
    if (output / "robust_plan.json").exists():
        candidates.append(
            read_result(output / "robust_plan.json", instance, base_scenario())
        )
    checkpoint_path = output / "robust_checkpoint.json"
    if resume and checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
        if checkpoint["baseline_hash"] == baseline["configuration_hash"]:
            saved_plan: Plan = checkpoint["best_plan"]
            checked = evaluate_plan(instance, saved_plan, base_scenario())
            if checked["valid"]:
                candidates.append(checked)
    directory = configuration_directory(instance)
    for file in sorted((directory / "published").glob("seed_[0-9].json")):
        candidates.append(read_result(file, instance, base_scenario()))
    staged = directory / "published/staged.json"
    if staged.exists():
        candidates.append(read_result(staged, instance, base_scenario()))
    for candidate in candidates:
        minimum, _, _ = stress_values(instance, candidate)
        if (minimum, rank(candidate)) > (best_value, rank(best)):
            best, best_value = candidate, minimum

    # 2. Искать улучшения среди совместных открытий и закупок до
    # стагнации.
    iterations = 0
    while True:
        improved = False
        with ProcessPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(robust_candidate, instance, plan)
                for plan in robust_moves(instance, best)
            ]
            for future in futures:
                minimum, candidate = future.result()
                if (minimum, rank(candidate)) > (best_value, rank(best)):
                    best, best_value, improved = candidate, minimum, True
        iterations += 1
        write_json(
            checkpoint_path,
            {
                "baseline_hash": baseline["configuration_hash"],
                "best_plan": best["plan"],
                "worst_trips": best_value,
                "iteration": iterations,
            },
        )
        print(f"robust iteration={iterations} worst={best_value}", flush=True)
        if not improved:
            break
    _, base_rows, base_worst = stress_values(
        instance, baseline, output / "stress/base"
    )
    _, robust_rows, robust_worst = stress_values(
        instance, best, output / "stress/robust"
    )
    write_json(output / "robust_plan.json", best)
    write_json(output / "base_worst_schedule.json", base_worst)
    write_json(output / "robust_worst_schedule.json", robust_worst)
    summary: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "input_hashes": instance["hashes"],
        "baseline_hash": baseline["configuration_hash"],
        "recourse_version": RECOURSE_VERSION,
        "baseline_state_hash": baseline_state_hash,
        "base_scenarios": base_rows,
        "robust_scenarios": robust_rows,
        "base_worst": value,
        "robust_worst": best_value,
        "base_case_loss": baseline["objective_trips"]
        - best["objective_trips"],
        "worst_case_gain": best_value - value,
        "cost_change_rub": best["cum_spend_rub"] - baseline["cum_spend_rub"],
        "iterations": iterations,
        "algorithm": "deterministic max-min local search; no global claim",
        "failure_period": "whole year 3; CAPEX and fixed OPEX retained",
        "worst_base_scenario": next(
            r["scenario"] for r in base_rows if r["trips"] == value
        ),
        "worst_robust_scenario": next(
            r["scenario"] for r in robust_rows if r["trips"] == best_value
        ),
    }
    write_json(output / "robustness.json", summary)
    return summary


def game(
    instance: InstanceData,
    actions: list[OperatingResult],
    output: Path,
) -> dict[str, object]:
    """
    Сравнить перебор и alpha-beta на одной конечной матрице выигрышей.
    """
    # 1. Один раз рассчитать перестройку после каждого допустимого
    # отказа.
    start = time.perf_counter()
    unique = {json.dumps(a["plan"], sort_keys=True): a for a in actions}
    table: dict[str, dict[str, int]] = {}
    for index, action in enumerate(unique.values()):
        write_json(
            output / f"game_schedules/network_{index}/base.json",
            {"scenario": base_scenario(), "result": action},
        )
        payoffs: dict[str, int] = {}
        for point_id in sorted(action["plan"]["open_year"]):
            scenario = base_scenario()
            scenario["closed_port"] = point_id
            scenario["name"] = f"failure_{point_id}"
            result = evaluate_recourse(instance, action, scenario)
            write_json(
                output / f"game_schedules/network_{index}/{point_id}.json",
                {"scenario": scenario, "result": result},
            )
            payoffs[point_id] = (
                result["objective_trips"] if result["valid"] else 0
            )
        table[f"network_{index}"] = payoffs
    preprocessing = time.perf_counter() - start
    if not table or any(not row for row in table.values()):
        raise ValueError("Игровая сеть должна содержать построенный порт")

    # 2. Использовать одинаковый lookup листьев для честного сравнения
    # обходов.
    start = time.perf_counter()
    full = max(min(row.values()) for row in table.values())
    full_time = time.perf_counter() - start
    start = time.perf_counter()
    alpha = -1
    leaves = networks = 0
    for row in table.values():
        networks += 1
        worst = 10**18
        for value in row.values():
            leaves += 1
            worst = min(worst, value)
            if worst <= alpha:
                break
        alpha = max(alpha, worst)
    alpha_time = time.perf_counter() - start
    if alpha != full:
        raise ValueError("Alpha-beta изменил значение игры")
    result_game: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "recourse_version": RECOURSE_VERSION,
        "input_hashes": instance["hashes"],
        "action_plans": [a["plan"] for a in unique.values()],
        "payoffs": table,
        "value": full,
        "values_equal": True,
        "preprocessing_s": preprocessing,
        "full_leaves": sum(len(row) for row in table.values()),
        "alpha_beta_leaves": leaves,
        "full_nodes": 1 + len(table) + sum(len(row) for row in table.values()),
        "alpha_beta_nodes": 1 + networks + leaves,
        "full_s": full_time,
        "alpha_beta_s": alpha_time,
        "utility": (
            "validated heuristic recourse; not globally optimal recourse"
        ),
    }
    write_json(output / "game.json", result_game)
    return result_game


def run_experiments(resume: bool = True) -> dict[str, object]:
    """Выполнить масштаб, бюджет, качество данных и чувствительность."""
    # 1. Создать только экспериментальные копии исходных точек и спроса.
    base = load_instance()
    folder = ROOT / "data/processed/experiments_v2"
    folder.mkdir(parents=True, exist_ok=True)
    points = pd.read_csv(ROOT / "data/processed/points_main.csv")
    report: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "input_hashes": base["hashes"],
    }
    scale: list[dict[str, object]] = []
    for size in (12, 25, 50, 75):
        subset = points.sort_values(
            ["population", "wikidata_id"], ascending=[False, True]
        ).head(size)
        path = folder / f"points_{size}.csv"
        subset.to_csv(path, index=False)
        if size == 12:
            instance = load_instance("small")
        elif size == 75:
            instance = copy.deepcopy(base)
        else:
            instance = load_instance(f"size_{size}", points_path=path)
        _, statistics = run_configuration(instance, resume=resume)
        statistics["n"] = size
        scale.append(statistics)
        write_json(OUTPUT / "experiments_progress.json", {"scale": scale})
    report["scale"] = scale

    # 2. Измерить бюджет на равномерной сетке и уточнить наибольший
    # наклон.
    funding: list[dict[str, float]] = []
    multipliers = [0.5 + 0.25 * i for i in range(7)]
    for iteration in range(3):
        for multiplier in multipliers:
            if any(row["multiplier"] == multiplier for row in funding):
                continue
            instance = copy.deepcopy(base)
            instance["settings"]["tranche_rub_per_year"] *= multiplier
            result, _ = run_configuration(instance, resume=resume)
            funding.append(
                {
                    "multiplier": multiplier,
                    "trips": float(result["objective_trips"]),
                    "cost_rub": result["cum_spend_rub"],
                }
            )
        funding.sort(key=lambda row: row["multiplier"])
        if iteration < 2:
            left, right = max(
                zip(funding, funding[1:]),
                key=lambda pair: (
                    (pair[1]["trips"] - pair[0]["trips"])
                    / (pair[1]["multiplier"] - pair[0]["multiplier"])
                ),
            )
            multipliers.append((left["multiplier"] + right["multiplier"]) / 2)
        write_json(OUTPUT / "funding_progress.json", funding)
    report["funding"] = funding

    # 3. Сравнить две стратегии пропусков и сохранить изменившиеся сети.
    candidates = clean_candidates()
    data_quality: list[dict[str, object]] = []
    for label in ("drop_missing", "type_median"):
        frame = candidates.copy()
        if label == "drop_missing":
            frame = frame.loc[frame["population"].notna()].copy()
        else:
            frame["type_group"] = (
                frame["types"].fillna("").str.split("|").str[0]
            )
            medians = frame.groupby("type_group")["population"].transform(
                "median"
            )
            frame["population"] = (
                frame["population"]
                .fillna(medians)
                .fillna(frame["population"].median())
            )
            frame = frame.drop(columns="type_group")
        selected = select_points(frame)
        points_path = folder / f"{label}_points.csv"
        demand_path = folder / f"{label}_demand.csv"
        selected.to_csv(points_path, index=False)
        gravity_demand(selected).to_csv(demand_path, index=False)
        instance = load_instance(label, points_path, demand_path)
        if instance["demand"] == base["demand"] and all(
            instance["points"][p]["population"]
            == base["points"][p]["population"]
            for p in instance["ids"]
        ):
            instance = copy.deepcopy(base)
        result, statistics = run_configuration(instance, resume=resume)
        statistics["strategy"] = label
        statistics["open_year"] = result["plan"]["open_year"]
        statistics["selected_ids"] = instance["ids"]
        data_quality.append(statistics)
    report["data_quality"] = data_quality

    # 4. Проверить технические и экономические учебные допущения
    # отдельно.
    sensitivity: list[dict[str, object]] = []
    scalar_keys = [
        "price_rub",
        "opex_per_year_rub",
        "cost_per_flight_hour_rub",
        "cruise_speed_kmh",
        "recharge_min",
    ]
    cases: list[tuple[str, dict[str, float]]] = []
    for name in scalar_keys + ["port_capex"]:
        for multiplier in (0.8, 1.2):
            changes = {name: base["settings"].get(name, 1) * multiplier}
            if name == "port_capex":
                changes = {
                    key: base["settings"][key] * multiplier
                    for key in ("capex_small_rub", "capex_hub_rub")
                }
            cases.append((f"{name}_{multiplier}", changes))
    cases += [
        (f"movements_{rate}", {"takeoffs_per_hour": float(rate)})
        for rate in (4, 8)
    ]
    cases += [
        (
            f"chargers_{delta}",
            {
                key: max(1, base["settings"][key] + delta)
                for key in ("charge_slots_small", "charge_slots_hub")
            },
        )
        for delta in (-1, 1)
    ]
    for name, changes in cases:
        instance = copy.deepcopy(base)
        instance["settings"].update(changes)
        result, statistics = run_configuration(
            instance, seeds=[0], resume=resume
        )
        sensitivity.append(
            {
                "case": name,
                "changes": changes,
                "trips": result["objective_trips"],
                "cost_rub": result["cum_spend_rub"],
                "statistics": statistics,
            }
        )
        write_json(OUTPUT / "sensitivity_progress.json", sensitivity)
    report["sensitivity"] = sensitivity
    write_json(
        folder / "provenance.json",
        {
            "source": "frozen v1 candidates and parameters",
            "input_hashes": base["hashes"],
            "scale_rule": "top population; tie by wikidata_id",
            "demand_rule": "v1 origin-normalized gravity; experiments only",
        },
    )
    write_json(OUTPUT / "experiments.json", report)
    return report

"""Единый командный запуск расчётов из замороженных входов v1."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.bounds import solve_bound
from scripts.data_audit import audit_data, population_metadata
from scripts.evaluation import read_result
from scripts.model_data import (
    MODEL_VERSION,
    OUTPUT,
    ROOT,
    base_scenario,
    input_hashes,
    load_instance,
    write_json,
)
from scripts.model_types import InstanceData, OperatingResult
from scripts.routing import compare_routes
from scripts.studies import (
    configuration_directory,
    game,
    robustness,
    run_configuration,
    run_experiments,
)


def solver_result(label: str) -> tuple[InstanceData, OperatingResult]:
    """Прочитать актуальное лучшее решение и проверить его целиком."""
    instance = load_instance(label)
    directory = configuration_directory(instance)
    return instance, read_result(
        directory / "published/best.json", instance, base_scenario()
    )


def solve_small(
    resume: bool, seeds: list[int], adaptive: bool = True, limit: int = 420
) -> OperatingResult:
    """Получить проверенное решение и отдельный сертификат границы."""
    # 1. Использовать общий поиск и полный набор малых расписаний.
    instance = load_instance("small")
    result, statistics = run_configuration(instance, seeds, resume)
    output = OUTPUT / "small_bound"
    candidate_path = OUTPUT / "small_milp_plan.json"
    candidate, evidence = solve_bound(
        instance, base_scenario(), result, output, adaptive, limit, resume
    )
    derived = output / "best_milp_derived.json"
    if not derived.exists():
        raise RuntimeError("Нет проверенного плана из MILP incumbent")
    write_json(candidate_path, read_result(derived, instance, base_scenario()))
    write_json(output / "best_scheduled.json", candidate)
    statistics["certificate"] = evidence
    write_json(OUTPUT / "small_comparison.json", statistics)
    return result


def compare_planning() -> dict[str, object]:
    """Сопоставить поэтапные и совместные планы на одном валидаторе."""
    instance, best = solver_result("main")
    directory = configuration_directory(instance)
    staged = read_result(
        directory / "published/staged.json", instance, base_scenario()
    )
    greedy = read_result(
        directory / "published/greedy.json", instance, base_scenario()
    )
    results: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "input_hashes": instance["hashes"],
        "best_hash": best["configuration_hash"],
    }
    for name, result in (
        ("joint_sa", best),
        ("joint_greedy", greedy),
        ("staged_greedy", staged),
    ):
        results[name] = {
            "trips": result["objective_trips"],
            "per_year": [y["trips"] for y in result["years"]],
            "cost_rub": result["cum_spend_rub"],
            "plan": result["plan"],
        }
    results["myopia_difference_heuristics"] = (
        best["objective_trips"] - staged["objective_trips"]
    )
    results["note"] = (
        "Heuristic comparison; no theorem inferred from differences"
    )
    write_json(OUTPUT / "planning_comparison.json", results)
    return results


def run_all(resume: bool, seeds: list[int]) -> None:
    """Пройти все этапы с сохранением результатов каждой стадии."""
    # 1. Подтвердить данные, затем выполнить малую и основную задачи.
    started = time.perf_counter()
    frozen = input_hashes()
    run_quality_checks()
    audit_data(ROOT / "output/repro-check")
    metadata_path = ROOT / "data/metadata/population_observations.csv"
    if not resume or not metadata_path.exists():
        population_metadata(load_instance(), metadata_path.parent)
    solve_small(resume, seeds)
    main = load_instance()
    best, _ = run_configuration(main, seeds, resume)

    # 2. Выполнить сравнения, устойчивость, игру и вычислительные серии.
    write_json(OUTPUT / "routing.json", compare_routes(main, best["plan"]))
    compare_planning()
    robustness(main, best, OUTPUT, resume)
    small, small_best = solver_result("small")
    directory = configuration_directory(small)
    actions = [
        small_best,
        read_result(
            directory / "published/greedy.json", small, base_scenario()
        ),
        read_result(OUTPUT / "small_milp_plan.json", small, base_scenario()),
    ]
    game(small, actions, OUTPUT)
    run_experiments(resume)

    # 3. Собрать отчёт только при неизменных замороженных входах.
    if frozen != input_hashes():
        raise RuntimeError("Изменились замороженные файлы")
    from scripts.reporting import build_report
    from scripts.submission import verify_submission

    build_report()
    verification = verify_submission()
    write_json(
        OUTPUT / "completion.json",
        {
            "all_computations_completed": True,
            "model_version": MODEL_VERSION,
            "input_hashes": frozen,
            "wall_s": time.perf_counter() - started,
            "verification": verification,
        },
    )


def run_quality_checks() -> None:
    """Выполнить обязательные проверки тем же locked интерпретатором."""
    # 1. Запустить четыре независимых проверки и сохранить их вывод.
    commands = {
        "lint": ["ruff", "check", "."],
        "format": ["ruff", "format", "--check", "."],
        "types": ["mypy"],
        "tests": ["unittest", "discover", "-s", "tests"],
    }
    folder = ROOT / "output/quality"
    folder.mkdir(parents=True, exist_ok=True)
    for name, arguments in commands.items():
        completed = subprocess.run(
            [sys.executable, "-m", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = completed.stdout + completed.stderr
        (folder / f"model_v2_{name}.txt").write_text(output, encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"Проверка {name} не пройдена:\n{output}")
        print(f"quality {name}: passed", flush=True)


def main(arguments: list[str] | None = None) -> None:
    """
    Разобрать команду и выполнить предметную функцию без скрытой
    работы.
    """
    # 1. Задать явные команды и настройки без изменения параметров v1.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "check-data",
            "metadata",
            "solve-small",
            "solve-main",
            "routing",
            "planning",
            "robustness",
            "game",
            "experiments",
            "report",
            "schedule",
            "all",
            "parameter-change",
            "validate",
            "check-submission",
        ],
    )
    parser.add_argument(
        "--instance", choices=["main", "small"], default="main"
    )
    parser.add_argument("--seeds", default="0,1,2,3,4,5,6,7,8,9")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--non-adaptive-bound", action="store_true")
    parser.add_argument("--limit", type=int, default=420)
    parser.add_argument("--budget-multiplier", type=float, default=1.0)
    parser.add_argument("--demand-multiplier", type=float, default=1.0)
    parser.add_argument("--range-km", type=float, default=200)
    parser.add_argument("--closed-port-id", default="")
    parser.add_argument("--result", type=Path)
    parser.add_argument(
        "--available-results",
        action="store_true",
        help="Собрать документы по завершённым сериям, указав ограничения",
    )
    options = parser.parse_args(arguments)
    seeds = [int(value) for value in options.seeds.split(",") if value.strip()]
    if (
        len(seeds) != len(set(seeds))
        or any(seed < 0 for seed in seeds)
        or options.limit <= 0
    ):
        parser.error("Seed должны быть уникальны и >=0; лимит >0")
    if (
        options.budget_multiplier <= 0
        or options.demand_multiplier < 0
        or options.range_km <= 0
    ):
        parser.error("Бюджет и дальность >0; множитель спроса >=0")
    resume = not options.no_resume
    command = options.command

    # 2. Отправить управление простой функции соответствующего расчёта.
    if command == "check-data":
        print(
            json.dumps(
                audit_data(ROOT / "output/repro-check"),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif command == "metadata":
        population_metadata(load_instance(), ROOT / "data/metadata")
    elif command == "solve-small":
        solve_small(
            resume, seeds, not options.non_adaptive_bound, options.limit
        )
    elif command == "solve-main":
        run_configuration(load_instance(options.instance), seeds, resume)
    elif command == "routing":
        instance, best = solver_result(options.instance)
        write_json(
            OUTPUT / "routing.json", compare_routes(instance, best["plan"])
        )
    elif command == "planning":
        compare_planning()
    elif command == "robustness":
        instance, best = solver_result(options.instance)
        robustness(instance, best, OUTPUT, resume)
    elif command == "game":
        instance, best = solver_result("small")
        directory = configuration_directory(instance)
        game(
            instance,
            [
                best,
                read_result(
                    directory / "published/greedy.json",
                    instance,
                    base_scenario(),
                ),
                read_result(
                    OUTPUT / "small_milp_plan.json", instance, base_scenario()
                ),
            ],
            OUTPUT,
        )
    elif command == "experiments":
        run_experiments(resume)
    elif command == "all":
        run_all(resume, seeds)
    elif command == "report":
        from scripts.reporting import build_report

        build_report(available_only=options.available_results)
    elif command == "check-submission":
        from scripts.submission import verify_submission

        print(
            json.dumps(
                verify_submission(available_only=options.available_results),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif command == "schedule":
        instance, result = solver_result(options.instance)
        write_json(OUTPUT / f"{options.instance}_schedule.json", result)
        print(f"valid={result['valid']} trips={result['objective_trips']}")
    elif command == "validate":
        instance = load_instance(options.instance)
        if options.result is None:
            parser.error("Для validate нужен --result")
        result = read_result(options.result, instance, base_scenario())
        print(f"valid=True trips={result['objective_trips']}")
    else:
        instance, previous = solver_result(options.instance)
        instance["settings"]["tranche_rub_per_year"] *= (
            options.budget_multiplier
        )
        scenario = base_scenario()
        scenario["demand_multiplier"] = options.demand_multiplier
        scenario["range_km"] = options.range_km
        scenario["closed_port"] = options.closed_port_id
        if (
            scenario["closed_port"]
            and scenario["closed_port"] not in instance["points"]
        ):
            parser.error("Неизвестный ID закрываемого порта")
        from scripts.evaluation import evaluate_recourse

        changed = evaluate_recourse(instance, previous, scenario)
        write_json(
            OUTPUT / "parameter_change.json",
            {
                "scenario": scenario,
                "result": changed,
                "base_trips": previous["objective_trips"],
                "delta": changed["objective_trips"]
                - previous["objective_trips"],
            },
        )
        print(
            f"base={previous['objective_trips']} "
            f"changed={changed['objective_trips']} valid={changed['valid']}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])

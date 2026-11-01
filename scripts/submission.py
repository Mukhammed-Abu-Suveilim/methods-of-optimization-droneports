"""Проверка комплекта по сохранённым событиям и свидетельствам."""

import copy
import csv
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader

from scripts.bounds import certified_gap
from scripts.evaluation import fingerprint, read_result
from scripts.model_data import (
    FROZEN_HASHES,
    MODEL_VERSION,
    OUTPUT,
    RECOURSE_VERSION,
    ROOT,
    base_scenario,
    input_hashes,
    load_instance,
    read_instance,
    write_json,
)
from scripts.model_types import OperatingResult, Scenario
from scripts.studies import configuration_directory
from scripts.validation import validate_result


def verify_plan_exports(result: OperatingResult) -> None:
    """Сопоставить таблицы инвестиций и поездок с проверенным планом."""
    # 1. Независимо суммировать завершённые поездки по весам шаблонов.
    directory = OUTPUT / "plan"
    with (directory / "passenger_itineraries.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        journeys = list(csv.DictReader(source))
    totals = {year: 0 for year in (1, 2, 3)}
    for row in journeys:
        totals[int(row["year"])] += int(row["weight"]) * int(row["count"])
    if totals != {year["year"]: year["trips"] for year in result["years"]}:
        raise ValueError("CSV поездок не совпадает с расписанием")

    # 2. Проверить годовые и накопленные расходы до копейки.
    with (directory / "budget.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        budgets = list(csv.DictReader(source))
    if len(budgets) != 3:
        raise ValueError("Ожидаются три строки годового бюджета")
    for row, year in zip(budgets, result["years"]):
        for key in (
            "year",
            "trips",
            "ports",
            "fleet",
            "cost_rub",
            "cumulative_cost_rub",
            "fixed_cost_rub",
            "flight_cost_rub",
        ):
            if abs(float(row[key]) - float(year[key])) > 0.005:
                raise ValueError(f"CSV бюджета: неверное поле {key}")

    # 3. Проверить открытия по ID и записи всех полётных событий.
    with (directory / "openings.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        openings = list(csv.DictReader(source))
    if {
        row["wikidata_id"]: int(row["opening_year"]) for row in openings
    } != result["plan"]["open_year"]:
        raise ValueError("CSV открытий не совпадает с планом")
    with (directory / "flights.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        flights = list(csv.DictReader(source))
    expected = [
        (year["year"], template, day["weight"], flight)
        for year in result["years"]
        for template, day in enumerate(year["days"])
        for flight in day["flights"]
    ]
    if len(flights) != len(expected):
        raise ValueError("CSV рейсов содержит иной набор событий")
    for row, (year_id, template, weight, flight) in zip(flights, expected):
        values = {
            "year": year_id,
            "template": template,
            "weight": weight,
            **flight,
        }
        if any(row[key] != str(value) for key, value in values.items()):
            raise ValueError("CSV рейсов не совпадает с расписанием")


def verify_submission(available_only: bool = False) -> dict[str, object]:
    """Повторно проверить конфигурации и восемь элементов сдачи."""
    # 1. Защитить основу и проверить полные сохранённые конфигурации.
    if input_hashes() != FROZEN_HASHES:
        raise ValueError("Изменились входы v1")
    count = configurations = 0
    series: dict[str, list[int]] = {}
    for snapshot in sorted(
        (OUTPUT / "configurations").glob("*/instance.json")
    ):
        instance = read_instance(snapshot)
        folder = snapshot.parent
        if configuration_directory(instance) != folder:
            continue
        if not (folder / "published/best.json").exists():
            continue
        statistics = json.loads(
            (folder / "statistics.json").read_text("utf-8")
        )
        seeds = [int(row["seed"]) for row in statistics["seed_runs"]]
        series[folder.name] = sorted(seeds)
        if any(not row.get("complete") for row in statistics["seed_runs"]):
            raise ValueError(f"Не завершена серия: {folder.name}")
        files = [
            folder / f"{name}.json" for name in ("best", "greedy", "staged")
        ]
        files += [folder / f"seed_{seed}.json" for seed in seeds]
        files += [folder / "published" / path.name for path in files]
        for path in files:
            read_result(path, instance, base_scenario())
            count += 1
        best = read_result(
            folder / "published/best.json", instance, base_scenario()
        )
        if statistics["best"] != best["objective_trips"]:
            raise ValueError("Статистика и итоговый план расходятся")
        configurations += 1

    # 2. Подтвердить десять seed каждой основной конфигурации.
    experiment_file = (
        "experiments_available.json" if available_only else "experiments.json"
    )
    experiments = json.loads((OUTPUT / experiment_file).read_text("utf-8"))
    expected = list(range(10))
    primary = experiments["scale"] + experiments["data_quality"]
    for row in primary:
        if series[Path(row["configuration"]).name] != expected:
            raise ValueError("Основной эксперимент требует десять seed")
    if {row["n"] for row in experiments["scale"]} != {12, 25, 50, 75}:
        raise ValueError("Не выполнены четыре размера задачи")
    base = load_instance()
    main_result = read_result(
        configuration_directory(base) / "published/best.json",
        base,
        base_scenario(),
    )
    verify_plan_exports(main_result)
    for row in experiments["funding"]:
        instance = copy.deepcopy(base)
        instance["settings"]["tranche_rub_per_year"] *= row["multiplier"]
        if experiments.get("profile") == "bounded":
            result = read_result(
                Path(row["result"]), instance, base_scenario()
            )
            if (
                result["objective_trips"] != row["trips"]
                or abs(result["cum_spend_rub"] - row["cost_rub"]) > 0.005
            ):
                raise ValueError("Бюджетная точка не совпадает с планом")
            continue
        if series[configuration_directory(instance).name] != expected:
            raise ValueError("Бюджетный эксперимент требует десять seed")
    funding = {row["multiplier"] for row in experiments["funding"]}
    if not available_only and (
        not {0.5 + 0.25 * i for i in range(7)} <= funding or len(funding) < 9
    ):
        raise ValueError("Неполная сетка финансирования или уточнения")
    if not available_only and len(experiments["sensitivity"]) != 16:
        raise ValueError("Неполная чувствительность учебных параметров")
    if experiments.get("profile") == "bounded":
        evidence = experiments["bounded_evidence"]
        if (
            evidence["wall_s"] > evidence["max_wall_seconds"]
            or len(evidence["sensitivity"]) != 16
            or not evidence["data_quality_equivalent"]
            or not {0.5 + i * 0.25 for i in range(7)} <= funding
            or len(funding) != 9
        ):
            raise ValueError("Не выполнен ограниченный протокол")
        folder = OUTPUT / "bounded" / evidence["identity"]
        for label in ("drop_missing", "type_median"):
            variant = load_instance(
                label,
                folder / f"{label}_points.csv",
                folder / f"{label}_demand.csv",
            )
            if variant["points"] != base["points"] or (
                variant["demand"] != base["demand"]
            ):
                raise ValueError("Не подтверждена эквивалентность входов")
        for index, row in enumerate(evidence["sensitivity"]):
            if row["result"] is None:
                if row["status"] != "not_established_within_allowance":
                    raise ValueError("Неверный статус отсутствия решения")
                continue
            variant = read_instance(
                folder / f"sensitivity_{index}_instance.json"
            )
            result = read_result(Path(row["result"]), variant, base_scenario())
            if (
                result["objective_trips"] != row["trips"]
                or abs(result["cum_spend_rub"] - row["cost_rub"]) > 0.005
            ):
                raise ValueError("Не совпадает чувствительность")

    # 3. Восстановить все сохранённые сценарии и игровые ответы.
    robustness = json.loads((OUTPUT / "robustness.json").read_text("utf-8"))
    if robustness.get("recourse_version") != RECOURSE_VERSION:
        raise ValueError("Устаревшая эксплуатационная реакция на стрессы")
    case_files = []
    for label in ("base", "robust"):
        references = {
            row["schedule_reference"]
            for row in robustness[f"{label}_scenarios"]
        }
        case_files += [
            OUTPUT / f"stress/{label}/{reference}.json"
            for reference in sorted(references)
        ]
    game_record = json.loads((OUTPUT / "game.json").read_text("utf-8"))
    case_files += [
        OUTPUT / f"game_schedules/{network}/{port}.json"
        for network, payoffs in game_record["payoffs"].items()
        for port in payoffs
    ]
    case_files += [
        OUTPUT / f"game_schedules/{network}/base.json"
        for network in game_record["payoffs"]
    ]
    if not case_files:
        raise ValueError("Нет сценарных расписаний")
    cases = 0
    for path in case_files:
        payload = json.loads(path.read_text("utf-8"))
        result = payload["result"]
        scenario: Scenario = payload["scenario"]
        instance = load_instance(result["instance"])
        if (
            result["model_version"] != MODEL_VERSION
            or result["configuration_hash"]
            != fingerprint(instance, result["plan"], scenario)
            or validate_result(instance, result, scenario)
        ):
            raise ValueError(f"Неверное сценарное расписание: {path}")
        if scenario["closed_port"]:
            baseline_case = json.loads(
                (path.parent / "base.json").read_text("utf-8")
            )
            stop = scenario["closed_year"] - 1
            if (
                result["years"][:stop]
                != baseline_case["result"]["years"][:stop]
            ):
                raise ValueError("Отказ изменил предшествующие годы")
        cases += 1
    read_result(OUTPUT / "robust_plan.json", base, base_scenario())

    # 4. Проверить границу small и равенства алгоритмических сравнений.
    certificate = json.loads(
        (OUTPUT / "small_bound/certificate.json").read_text("utf-8")
    )
    small = load_instance("small")
    lower = read_result(
        OUTPUT / "small_bound/best_scheduled.json", small, base_scenario()
    )["objective_trips"]
    gap = certified_gap(lower, certificate["upper_bound"])
    if (
        (not available_only and not certificate.get("complete"))
        or (
            not available_only
            and not certificate.get("adaptive")
            and certificate["attempts"][-1]["termination"]
            != "optimal_relaxation"
        )
        or certificate["lower_bound"] != lower
        or abs(certificate["gap_percent"] - gap) > 1e-8
    ):
        raise ValueError("Не завершена или противоречива сертификация small")
    for file, key in (
        ("routing.json", "dijkstra_agreement"),
        ("game.json", "values_equal"),
    ):
        record = json.loads((OUTPUT / file).read_text("utf-8"))
        if not record[key] or record["model_version"] != MODEL_VERSION:
            raise ValueError(f"Не подтверждено сравнение: {file}")
        if file == "game.json" and (
            record.get("recourse_version") != RECOURSE_VERSION
        ):
            raise ValueError("Игра использует устаревшую реакцию на отказ")

    # 5. Проверить наличие артефактов, страницы PDF и записать их хеши.
    required = [
        ROOT / "output/reports/report.md",
        ROOT / "output/reports/management_memo.md",
        ROOT / "output/pdf/report.pdf",
        ROOT / "output/pdf/management_memo.pdf",
        OUTPUT / "plan/budget.csv",
        OUTPUT / "plan/openings.csv",
        OUTPUT / "plan/fleet.csv",
        OUTPUT / "plan/flights.csv",
        OUTPUT / "plan/charging.csv",
        OUTPUT / "plan/passenger_itineraries.csv",
        *[OUTPUT / f"figures/map_{year}.png" for year in range(4)],
    ]
    hashes: dict[str, str] = {}
    pages: dict[str, int] = {}
    for path in required:
        data = path.read_bytes()
        if not data:
            raise ValueError(f"Пустой артефакт: {path}")
        relative = str(path.relative_to(ROOT))
        hashes[relative] = hashlib.sha256(data).hexdigest()
        if path.suffix == ".pdf":
            document = PdfReader(path)
            if any(not page.extract_text().strip() for page in document.pages):
                raise ValueError(f"Пустая страница PDF: {path}")
            pages[relative] = len(document.pages)
    verification: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "verified": True,
        "report_mode": "available_results" if available_only else "full",
        "experiment_profile": experiments.get("profile", "full"),
        "revised_protocol_completed": bool(
            experiments.get("bounded_evidence", {}).get(
                "revised_protocol_completed", False
            )
        ),
        "all_experiments_completed": not available_only,
        "input_hashes": input_hashes(),
        "configurations": configurations,
        "validated_base_results": count,
        "validated_scenario_results": cases,
        "small_gap_percent": gap,
        "plan_tables_agree": True,
        "artifact_hashes": hashes,
        "pdf_pages": pages,
        "visual_review": "separate manual page review required",
    }
    write_json(OUTPUT / "verification.json", verification)
    return verification

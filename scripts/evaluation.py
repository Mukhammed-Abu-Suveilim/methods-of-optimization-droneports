"""Общий расчёт стратегического плана через проверяемые расписания."""

import copy
import hashlib
import json
from pathlib import Path

from scripts.model_data import (
    MODEL_VERSION,
    base_scenario,
    fixed_cost,
    flight_cost,
    lifetime_bases,
    plan_errors,
)
from scripts.model_types import (
    InstanceData,
    OperatingResult,
    Plan,
    Scenario,
    YearResult,
)
from scripts.operations import assign_passengers, build_days
from scripts.validation import validate_day, validate_result

CACHE: dict[str, OperatingResult] = {}


def fingerprint(instance: InstanceData, plan: Plan, scenario: Scenario) -> str:
    """Получить ключ кэша, зависящий от всех фактических входов."""
    payload = {
        "model": MODEL_VERSION,
        "instance": instance,
        "plan": plan,
        "scenario": scenario,
    }
    # Матрицы с ключами-парами преобразуются до JSON отдельно.
    serial = json.dumps(
        {k: v for k, v in payload.items() if k != "instance"}, sort_keys=True
    )
    serial += repr(instance)
    return hashlib.sha256(serial.encode()).hexdigest()


def evaluate_plan(
    instance: InstanceData,
    plan: Plan,
    scenario: Scenario,
) -> OperatingResult:
    """Построить и независимо проверить полное трёхлетнее решение."""
    # 1. Отклонить финансово невозможную стратегию до построения рейсов.
    if not plan_errors(instance, plan):
        plan = lifetime_bases(instance, plan)
    key = fingerprint(instance, plan, scenario)
    if key in CACHE:
        return CACHE[key]
    result: OperatingResult = {
        "model_version": MODEL_VERSION,
        "instance": instance["label"],
        "plan": copy.deepcopy(plan),
        "years": [],
        "objective_trips": 0,
        "cum_spend_rub": 0.0,
        "valid": False,
        "violations": plan_errors(instance, plan),
        "input_hashes": instance["hashes"],
        "configuration_hash": key,
    }
    if result["violations"]:
        CACHE[key] = result
        return result

    # 2. Рассчитать годы с реальным переносом остатка и содержанием
    # объектов.
    cumulative = 0.0
    for year in (1, 2, 3):
        opened = sorted(
            p
            for p, t in plan["open_year"].items()
            if t <= year
            and not (
                year == scenario["closed_year"]
                and p == scenario["closed_port"]
            )
        )
        requested = (
            plan["bases"][year - 1] if len(plan["bases"]) >= year else []
        )
        bases = requested
        fixed = fixed_cost(instance, plan, year)
        available = year * instance["settings"]["tranche_rub_per_year"]
        available -= cumulative + fixed
        if available < -0.01:
            result["violations"].append(f"Недостаточный бюджет года {year}")
            CACHE[key] = result
            return result
        days = build_days(instance, opened, bases, year, scenario, available)
        expense = sum(
            day["weight"]
            * sum(
                flight_cost(instance, (f["origin"], f["destination"]))
                for f in day["flights"]
            )
            for day in days
        )
        trips = sum(
            day["weight"] * sum(j["count"] for j in day["journeys"])
            for day in days
        )
        cumulative += fixed + expense
        row: YearResult = {
            "year": year,
            "trips": trips,
            "ports": sum(t <= year for t in plan["open_year"].values()),
            "fleet": plan["fleet"][year - 1],
            "fixed_cost_rub": fixed,
            "flight_cost_rub": expense,
            "cost_rub": fixed + expense,
            "cumulative_cost_rub": cumulative,
            "days": days,
        }
        result["years"].append(row)
        result["objective_trips"] += trips
    result["cum_spend_rub"] = cumulative

    # 3. Сертифицировать сохранённые события независимым
    # восстановлением.
    result["violations"] = validate_result(instance, result, scenario)
    result["valid"] = not result["violations"]
    CACHE[key] = result
    if len(CACHE) > 128:
        del CACHE[next(iter(CACHE))]
    return result


def read_result(
    path: Path,
    instance: InstanceData,
    scenario: Scenario,
) -> OperatingResult:
    """
    Загрузить JSON и разрешить resume только после полной проверки.
    """
    # JSON — внешняя граница; все суммы и события проверяются ниже.
    result: OperatingResult = json.loads(path.read_text(encoding="utf-8"))
    if result["model_version"] != MODEL_VERSION:
        raise ValueError("Результат относится к другой модели")
    if result["configuration_hash"] != fingerprint(
        instance, result["plan"], scenario
    ):
        raise ValueError("Изменились настройки или условия сценария")
    errors = validate_result(instance, result, scenario)
    if errors:
        raise ValueError("; ".join(errors))
    return result


def rank(result: OperatingResult) -> tuple[int, float]:
    """Сравнить допустимость, поездки и затраты лексикографически."""
    if not result["valid"]:
        return -1, float("-inf")
    return result["objective_trips"], -result["cum_spend_rub"]


def result_metrics(
    instance: InstanceData,
    result: OperatingResult,
    upper_bound: float | None = None,
) -> dict[str, object]:
    """Собрать сравнимые показатели из полных проверенных событий."""
    # 1. Пересчитать пассажирские и пустые рейсы с весами суток.
    loaded = empty = 0
    for year in result["years"]:
        for day in year["days"]:
            occupied = {
                flight_id
                for journey in day["journeys"]
                for flight_id in journey["flights"]
            }
            loaded += day["weight"] * len(occupied)
            empty += day["weight"] * (len(day["flights"]) - len(occupied))

    # 2. Сохранить одинаковые метрики для каждого алгоритма.
    demand = sum(sum(q) for q in instance["demand"].values())
    upper = float(demand) if upper_bound is None else upper_bound
    if upper < result["objective_trips"]:
        raise ValueError("Граница метрик меньше допустимого результата")
    return {
        "trips": result["objective_trips"],
        "served_share": result["objective_trips"] / demand if demand else 0,
        "cost_rub": result["cum_spend_rub"],
        "loaded_flights": loaded,
        "empty_flights": empty,
        "fleet": result["plan"]["fleet"],
        "ports": [y["ports"] for y in result["years"]],
        "valid": result["valid"],
        "upper_bound": upper,
        "gap_percent": (
            100 * (upper - result["objective_trips"]) / upper if upper else 0
        ),
        "bound_kind": "total demand" if upper_bound is None else "CBC",
    }


def reuse_timetable(
    instance: InstanceData,
    source: OperatingResult,
    plan: Plan,
    scenario: Scenario,
) -> OperatingResult:
    """Проверить существующие задания при новом плане или сценарии."""
    # 1. Скопировать события; не переносить дроны и не менять их флот.
    result = copy.deepcopy(source)
    result["plan"] = copy.deepcopy(plan)
    result["model_version"] = MODEL_VERSION
    result["input_hashes"] = instance["hashes"]
    result["configuration_hash"] = fingerprint(instance, plan, scenario)
    result["valid"] = False
    result["violations"] = plan_errors(instance, plan)
    if result["violations"]:
        return result
    cumulative = 0.0
    result["objective_trips"] = 0

    # 2. Проверить физику, затем перераспределить изменённый спрос.
    for year, row in enumerate(result["years"], 1):
        opened = {p for p, t in plan["open_year"].items() if t <= year}
        physical = opened - (
            {scenario["closed_port"]}
            if year == scenario["closed_year"]
            else set()
        )
        for day in row["days"]:
            errors = validate_day(
                instance, day, physical, scenario, plan["fleet"][year - 1]
            )
            if errors:
                result["violations"] = errors
                return result
        if scenario["demand_multiplier"] != 1.0:
            for day in row["days"]:
                day["journeys"] = []
            assign_passengers(instance, row["days"], year, scenario)

        # 3. Начислить деньги заново, сохраняя все фактические перелёты.
        fixed = fixed_cost(instance, plan, year)
        expense = sum(
            day["weight"]
            * sum(
                flight_cost(instance, (f["origin"], f["destination"]))
                for f in day["flights"]
            )
            for day in row["days"]
        )
        cumulative += fixed + expense
        row["trips"] = sum(
            day["weight"] * sum(j["count"] for j in day["journeys"])
            for day in row["days"]
        )
        row["ports"] = len(opened)
        row["fleet"] = plan["fleet"][year - 1]
        row["fixed_cost_rub"], row["flight_cost_rub"] = fixed, expense
        row["cost_rub"] = fixed + expense
        row["cumulative_cost_rub"] = cumulative
        result["objective_trips"] += row["trips"]

    # 4. Независимо восстановить все ограничения и годовые итоги.
    result["cum_spend_rub"] = cumulative
    result["violations"] = validate_result(instance, result, scenario)
    result["valid"] = not result["violations"]
    return result


def remove_unused_ports(
    instance: InstanceData, source: OperatingResult
) -> OperatingResult:
    """Сократить вложения, сохраняя лучший проверенный график."""
    # 1. Сохранить каждый порт с операциями или базированием дронов.
    result = source
    while True:
        used = {p for bases in result["plan"]["bases"] for p in bases}
        for year in result["years"]:
            for day in year["days"]:
                used.update(c["port"] for c in day["charges"])
                for flight in day["flights"]:
                    used.update((flight["origin"], flight["destination"]))
        plan = copy.deepcopy(result["plan"])
        plan["open_year"] = {
            p: year for p, year in plan["open_year"].items() if p in used
        }
        if plan == result["plan"]:
            return result

        # 2. Сохранённые рейсы дают допустимую альтернативу перестройке.
        retained = reuse_timetable(instance, result, plan, base_scenario())
        if not retained["valid"] or (
            retained["objective_trips"] != result["objective_trips"]
        ):
            raise ValueError("Очистка изменила выполнимость или перевозки")
        constructed = evaluate_plan(instance, plan, base_scenario())
        result = max((retained, constructed), key=rank)


def evaluate_recourse(
    instance: InstanceData, baseline: OperatingResult, scenario: Scenario
) -> OperatingResult:
    """Выбрать лучший проверенный ответ, сохраняя пригодные задания."""
    # 1. Не перестраивать неизменённую базовую эксплуатацию.
    same_inputs = baseline["configuration_hash"] == fingerprint(
        instance, baseline["plan"], base_scenario()
    )
    if scenario == base_scenario() and same_inputs:
        return baseline

    # 2. При единственном отказе сохранить предшествующие годы целиком.
    failure_only = (
        same_inputs
        and bool(scenario["closed_port"])
        and scenario["demand_multiplier"] == 1.0
        and scenario["range_km"] == instance["settings"]["range_km"]
    )
    if failure_only:
        candidate = copy.deepcopy(baseline)
        year = scenario["closed_year"]
        previous = (
            baseline["years"][year - 2]["cumulative_cost_rub"]
            if year > 1
            else 0.0
        )
        available = year * instance["settings"]["tranche_rub_per_year"]
        available -= previous + fixed_cost(instance, baseline["plan"], year)
        opened = sorted(
            p
            for p, t in baseline["plan"]["open_year"].items()
            if t <= year and p != scenario["closed_port"]
        )
        candidate["years"][year - 1]["days"] = build_days(
            instance,
            opened,
            baseline["plan"]["bases"][year - 1],
            year,
            scenario,
            available,
        )
        constructed = reuse_timetable(
            instance, candidate, baseline["plan"], scenario
        )
    else:
        constructed = evaluate_plan(instance, baseline["plan"], scenario)

    # 3. Сохранённое расписание остаётся кандидатом, если оно допустимо.
    retained = reuse_timetable(instance, baseline, baseline["plan"], scenario)
    return max((constructed, retained), key=rank)

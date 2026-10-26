"""MILP-релаксация расписания: допустимая верхняя оценка перевозок."""

import hashlib
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

import pulp  # type: ignore[import-untyped]  # Внешний API без py.typed.

from scripts.evaluation import evaluate_plan, rank, remove_unused_ports
from scripts.model_data import (
    MODEL_VERSION,
    base_scenario,
    flight_cost,
    flight_seconds,
    port_cost,
    write_json,
)
from scripts.model_types import InstanceData, OperatingResult, Plan, Scenario

BOUND_VERSION = "annual-resource-2"


def bound_identity(
    instance: InstanceData, scenario: Scenario
) -> dict[str, object]:
    """Связать свидетельство с точной релаксацией и её входами."""
    serial = (
        BOUND_VERSION + repr(instance) + json.dumps(scenario, sort_keys=True)
    )
    return {
        "relaxation_version": BOUND_VERSION,
        "configuration_hash": hashlib.sha256(serial.encode()).hexdigest(),
        "input_hashes": instance["hashes"],
    }


def variable_plan(values: dict[str, float], ids: list[str]) -> Plan:
    """Восстановить стратегию из полного сохранённого incumbent CBC."""
    return {
        "open_year": {
            p: min(t for t in (1, 2, 3) if values[f"x_{p}_{t}"] > 0.5)
            for p in ids
            if values[f"x_{p}_3"] > 0.5
        },
        "fleet": [round(values[f"fleet_{t}"]) for t in (1, 2, 3)],
        "bases": [[], [], []],
    }


def build_relaxation(
    instance: InstanceData,
    scenario: Scenario,
    continuous: bool = False,
) -> tuple[
    pulp.LpProblem,
    dict[tuple[str, int], pulp.LpVariable],
    dict[int, pulp.LpVariable],
]:
    """
    Построить необходимые ограничения любого допустимого расписания.
    """
    # 1. Описать стратегию и годовые рейсы без ограничений их порядка.
    ids, settings = instance["ids"], instance["settings"]
    years = (1, 2, 3)
    arcs = [
        pair
        for pair, distance in instance["distance"].items()
        if distance <= scenario["range_km"] * 10
    ]
    od_pairs = [pair for pair, q in instance["demand"].items() if sum(q)]
    category = "Continuous" if continuous else "Integer"
    binary = "Continuous" if continuous else "Binary"
    problem = pulp.LpProblem("schedule_upper_bound", pulp.LpMaximize)
    maximum_fleet = int(
        3 * settings["tranche_rub_per_year"] // settings["price_rub"]
    )
    window = (settings["day_end_hour"] - settings["day_start_hour"]) * 3600
    days = int(settings["typical_days_per_year"])
    moves = settings["takeoffs_per_hour"] * math.ceil(window / 3600) * days
    x = {
        (p, t): pulp.LpVariable(f"x_{p}_{t}", 0, 1, cat=binary)
        for p in ids
        for t in years
    }
    fleet = {
        t: pulp.LpVariable(f"fleet_{t}", 0, maximum_fleet, cat=category)
        for t in years
    }
    loaded = {
        (a, t): pulp.LpVariable(
            f"loaded_{a[0]}_{a[1]}_{t}", 0, moves, cat=category
        )
        for a in arcs
        for t in years
    }
    empty = {
        (a, t): pulp.LpVariable(
            f"empty_{a[0]}_{a[1]}_{t}", 0, moves, cat=category
        )
        for a in arcs
        for t in years
    }
    charges = {
        (p, t): pulp.LpVariable(f"charges_{p}_{t}", 0, cat=category)
        for p in ids
        for t in years
    }
    served = {
        (od, t): pulp.LpVariable(f"served_{od[0]}_{od[1]}_{t}", 0)
        for od in od_pairs
        for t in years
    }
    flow = {
        (od, a, t): pulp.LpVariable(
            f"flow_{od[0]}_{od[1]}_{a[0]}_{a[1]}_{t}", 0
        )
        for od in od_pairs
        for a in arcs
        for t in years
    }
    outgoing = {p: [a for a in arcs if a[0] == p] for p in ids}
    incoming = {p: [a for a in arcs if a[1] == p] for p in ids}
    for t in (1, 2):
        problem += fleet[t] <= fleet[t + 1]
        for p in ids:
            problem += x[p, t] <= x[p, t + 1]

    # 2. Сохранить пассажирские потоки, места и баланс воздушных судов.
    for t in years:
        for a in arcs:
            count = loaded[a, t] + empty[a, t]
            problem += count <= moves * x[a[0], t]
            problem += count <= moves * x[a[1], t]
            problem += pulp.lpSum(flow[od, a, t] for od in od_pairs) <= (
                settings["seats"] * loaded[a, t]
            )
        for p in ids:
            departures = pulp.lpSum(
                loaded[a, t] + empty[a, t] for a in outgoing[p]
            )
            arrivals = pulp.lpSum(
                loaded[a, t] + empty[a, t] for a in incoming[p]
            )
            problem += departures == arrivals
            problem += departures <= moves * x[p, t]
            problem += arrivals <= moves * x[p, t]
            hub = (
                instance["points"][p]["population"]
                >= settings["hub_if_population_ge"]
            )
            slots = settings[
                "charge_slots_hub" if hub else "charge_slots_small"
            ]
            problem += charges[p, t] * settings["recharge_min"] * 60 <= (
                slots * window * days * x[p, t]
            )
        for od in od_pairs:
            problem += served[od, t] <= math.floor(
                instance["demand"][od][t - 1] * scenario["demand_multiplier"]
            )
            for p in ids:
                balance = pulp.lpSum(flow[od, a, t] for a in outgoing[p])
                balance -= pulp.lpSum(flow[od, a, t] for a in incoming[p])
                rhs = (
                    served[od, t]
                    if p == od[0]
                    else (-served[od, t] if p == od[1] else 0)
                )
                problem += balance == rhs

        # 3. Зарядка восполняет не более полной дальности за сеанс.
        distance = pulp.lpSum(
            instance["distance"][a] * (loaded[a, t] + empty[a, t])
            for a in arcs
        )
        charging_count = pulp.lpSum(charges[p, t] for p in ids)
        problem += distance <= scenario["range_km"] * 10 * charging_count
        resource = pulp.lpSum(
            (flight_seconds(instance, a) + settings["turnaround_min"] * 60)
            * (loaded[a, t] + empty[a, t])
            for a in arcs
        )
        resource += charging_count * settings["recharge_min"] * 60
        problem += resource <= fleet[t] * window * days

    # 4. Начислить инвестиции и все накопленные расходы в рублях.
    cumulative = 0
    for t in years:
        spend = pulp.lpSum(
            port_cost(instance, p) * (x[p, t] - (x[p, t - 1] if t > 1 else 0))
            for p in ids
        )
        spend += settings["price_rub"] * (
            fleet[t] - (fleet[t - 1] if t > 1 else 0)
        )
        spend += settings["opex_per_year_rub"] * pulp.lpSum(
            x[p, t] for p in ids
        )
        spend += pulp.lpSum(
            flight_cost(instance, a) * (loaded[a, t] + empty[a, t])
            for a in arcs
        )
        # Отдельное выражение сохраняет уже добавленные ограничения лет.
        cumulative = cumulative + spend
        problem += cumulative <= t * settings["tranche_rub_per_year"]
    problem += pulp.lpSum(served.values())
    return problem, x, fleet


def certified_gap(lower: float, upper: float) -> float:
    """
    Вычислить процентный разрыв и отвергнуть противоречивую границу.
    """
    if upper + 1e-5 < lower:
        raise ValueError("Верхняя оценка ниже допустимого решения")
    return 0.0 if upper == 0 else max(0.0, (upper - lower) / upper * 100)


def has_integer_solution(solution_status: int) -> bool:
    """Отличить целый incumbent от значений LP после лимита."""
    return solution_status in (
        pulp.LpSolutionOptimal,
        pulp.LpSolutionIntegerFeasible,
    )


def schedule_warm_start(
    problem: pulp.LpProblem, result: OperatingResult
) -> None:
    """Отобразить реальные события в допустимый incumbent релаксации."""
    # 1. Суммировать стратегию, рейсы, зарядки и потоки по годам.
    values: dict[str, float] = defaultdict(float)
    for year in result["years"]:
        t = year["year"]
        values[f"fleet_{t}"] = year["fleet"]
        for port, opening in result["plan"]["open_year"].items():
            values[f"x_{port}_{t}"] = float(opening <= t)
        for day in year["days"]:
            weight = day["weight"]
            flights = {f["id"]: f for f in day["flights"]}
            occupied: set[str] = set()
            for journey in day["journeys"]:
                origin, destination = journey["origin"], journey["destination"]
                count = journey["count"] * weight
                values[f"served_{origin}_{destination}_{t}"] += count
                for flight_id in journey["flights"]:
                    occupied.add(flight_id)
                    flight = flights[flight_id]
                    a, b = flight["origin"], flight["destination"]
                    values[f"flow_{origin}_{destination}_{a}_{b}_{t}"] += count
            for flight in flights.values():
                prefix = "loaded" if flight["id"] in occupied else "empty"
                a, b = flight["origin"], flight["destination"]
                values[f"{prefix}_{a}_{b}_{t}"] += weight
            for charge in day["charges"]:
                values[f"charges_{charge['port']}_{t}"] += weight

    # 2. Убедиться, что отображение выполняет каждое ограничение MILP.
    for variable in problem.variables():
        variable.setInitialValue(values[variable.name])
    if not problem.valid(0.001):
        raise ValueError("Расписание не вложилось в MILP-релаксацию")


def solve_bound(
    instance: InstanceData,
    scenario: Scenario,
    incumbent: OperatingResult,
    output: Path,
    adaptive: bool = True,
    initial_limit: int = 420,
    resume: bool = True,
) -> tuple[OperatingResult, dict[str, object]]:
    """Сохранить LP/CBC-границы и проверить стратегические кандидаты."""
    # 1. Получить строгую LP-границу отдельно от целочисленного
    # incumbent.
    output.mkdir(parents=True, exist_ok=True)
    identity = bound_identity(instance, scenario)
    identity_path = output / "run_identity.json"
    certificate_path = output / "certificate.json"
    attempts: list[dict[str, object]] = []
    if resume and certificate_path.exists():
        if (
            not identity_path.exists()
            or json.loads(identity_path.read_text("utf-8")) != identity
        ):
            raise ValueError("Нельзя продолжить CBC: изменились входы")
        previous = json.loads(certificate_path.read_text("utf-8"))
        attempts = previous["attempts"]
    write_json(identity_path, identity)
    linear, _, _ = build_relaxation(instance, scenario, continuous=True)
    linear.solve(
        pulp.PULP_CBC_CMD(msg=False, threads=0, logPath=str(output / "lp.log"))
    )
    if linear.status != pulp.LpStatusOptimal:
        raise RuntimeError("LP-релаксация не решена до оптимума")
    upper = float(pulp.value(linear.objective))
    lp_bound = upper
    best = incumbent
    derived: OperatingResult | None = None
    warm_variables: dict[str, float] | None = None
    warm_objective = float(best["objective_trips"])
    saved_path = output / "best_scheduled.json"
    if resume and saved_path.exists():
        saved: OperatingResult = json.loads(saved_path.read_text("utf-8"))
        checked = evaluate_plan(instance, saved["plan"], scenario)
        if rank(checked) > rank(best):
            best = checked
    for attempt in attempts:
        upper = min(upper, float(str(attempt["upper_bound"])))
        limit_value = int(str(attempt["limit_s"]))
        variables = output / f"variables_{limit_value}.json"
        if variables.exists():
            values: dict[str, float] = json.loads(variables.read_text("utf-8"))
            objective = sum(
                v for k, v in values.items() if k.startswith("served_")
            )
            if objective > warm_objective:
                warm_variables, warm_objective = values, objective
            candidate = evaluate_plan(
                instance, variable_plan(values, instance["ids"]), scenario
            )
            if candidate["valid"] and scenario == base_scenario():
                candidate = remove_unused_ports(instance, candidate)
            if rank(candidate) > rank(best):
                best = candidate
            if candidate["valid"] and (
                derived is None or rank(candidate) > rank(derived)
            ):
                derived = candidate
    if derived is not None:
        write_json(output / "best_milp_derived.json", derived)
    previous_gap = certified_gap(best["objective_trips"], upper)
    stale, limit = 0, initial_limit
    if attempts:
        stale = int(str(previous.get("stale_attempts", 0)))
        limit = int(str(attempts[-1]["limit_s"])) + initial_limit
        if previous.get("complete") and (
            previous.get("adaptive") or not adaptive
        ):
            previous.update(
                lower_bound=best["objective_trips"],
                gap_percent=previous_gap,
                model_version=MODEL_VERSION,
            )
            write_json(certificate_path, previous)
            write_json(output / "best_scheduled.json", best)
            return best, previous

    # 2. Увеличивать лимит, сохраняя лучшую границу разных попыток.
    while True:
        problem, openings, fleet = build_relaxation(instance, scenario)
        problem.name = f"small_bound_{limit}"
        schedule_warm_start(problem, best)
        start_kind = "scheduled"
        start_objective = float(best["objective_trips"])
        if warm_variables is not None:
            for variable in problem.variables():
                value = warm_variables[variable.name]
                if variable.lowBound is not None:
                    value = max(variable.lowBound, value)
                if variable.upBound is not None:
                    value = min(variable.upBound, value)
                variable.setInitialValue(value)
            if problem.valid(0.001):
                start_kind = "previous_milp_incumbent"
                start_objective = float(pulp.value(problem.objective))
            else:
                schedule_warm_start(problem, best)
        if not (output / "relaxation.mps").exists():
            problem.writeMPS(str(output / "relaxation.mps"))
        log = output / f"cbc_{limit}.log"
        start = time.perf_counter()
        problem.solve(
            pulp.PULP_CBC_CMD(
                msg=False,
                threads=0,
                timeLimit=limit,
                warmStart=True,
                keepFiles=True,
                logPath=str(log),
                options=["randomSeed 1", "randomCbcSeed 1"],
            )
        )
        text = log.read_text(encoding="utf-8", errors="replace")
        for extension in ("mps", "mst", "sol"):
            solver_file = Path(f"{problem.name}-pulp.{extension}")
            if solver_file.exists():
                solver_file.replace(output / f"cbc_{limit}.{extension}")
        warm_start_accepted = "MIPStart provided solution" in text
        matches = re.findall(r"Upper bound:\s*([\d.+eE-]+)", text)
        has_incumbent = has_integer_solution(problem.sol_status)
        optimal_relaxation = problem.sol_status == pulp.LpSolutionOptimal
        if matches:
            upper = min(upper, float(matches[-1]) + 0.001)
        if optimal_relaxation:
            upper = min(upper, float(pulp.value(problem.objective)))
        if has_incumbent:
            current_objective = float(pulp.value(problem.objective))
            if current_objective > warm_objective:
                warm_variables = {
                    v.name: float(v.value()) for v in problem.variables()
                }
                warm_objective = current_objective
            plan: Plan = {
                "open_year": {
                    p: min(
                        t for t in (1, 2, 3) if openings[p, t].value() > 0.5
                    )
                    for p in instance["ids"]
                    if openings[p, 3].value() > 0.5
                },
                "fleet": [round(fleet[t].value()) for t in (1, 2, 3)],
                "bases": [[], [], []],
            }
            candidate = evaluate_plan(instance, plan, scenario)
            if candidate["valid"] and scenario == base_scenario():
                candidate = remove_unused_ports(instance, candidate)
            if rank(candidate) > rank(best):
                best = candidate
            if candidate["valid"] and (
                derived is None or rank(candidate) > rank(derived)
            ):
                derived = candidate
                write_json(output / "best_milp_derived.json", derived)
            write_json(
                output / f"variables_{limit}.json",
                {v.name: v.value() for v in problem.variables()},
            )
        gap = certified_gap(best["objective_trips"], upper)
        stale = stale + 1 if previous_gap - gap < 0.1 else 0
        attempts.append(
            {
                "limit_s": limit,
                "wall_s": time.perf_counter() - start,
                "solver_status": pulp.LpStatus[problem.status],
                "solution_status": pulp.LpSolution[problem.sol_status],
                "termination": "optimal_relaxation"
                if optimal_relaxation
                else (
                    "time_limit" if "time limit" in text.lower() else "other"
                ),
                "has_integer_incumbent": has_incumbent,
                "warm_start_accepted": warm_start_accepted,
                "warm_start_kind": start_kind,
                "warm_start_objective": start_objective,
                "scheduled_lower_bound": best["objective_trips"],
                "upper_bound": upper,
                "gap_percent": gap,
            }
        )
        evidence: dict[str, object] = {
            **identity,
            "model_version": MODEL_VERSION,
            "lp_upper_bound": lp_bound,
            "upper_bound": upper,
            "lower_bound": best["objective_trips"],
            "gap_percent": gap,
            "definition": "100*(upper-scheduled_lower)/upper",
            "attempts": attempts,
            "stale_attempts": stale,
            "adaptive": adaptive,
            "complete": not adaptive or optimal_relaxation or stale >= 3,
            "stopping_rule": "optimal or 3 gap gains <0.1 percentage point",
            "limit_increment_s": initial_limit,
            "relaxation_proof": (
                "Each day starts/ends full. Recharge restores <=range; "
                "distance<=range*charges. Actual flight+turn+charge time "
                "fits fleet hours. Omitting event ordering "
                "enlarges feasibility."
            ),
        }
        write_json(certificate_path, evidence)
        write_json(output / "best_scheduled.json", best)
        print(
            f"small bound limit={limit} L={best['objective_trips']} "
            f"U={upper:.1f} gap={gap:.2f}%",
            flush=True,
        )
        previous_gap = gap
        if not adaptive or optimal_relaxation or stale >= 3:
            break
        limit += initial_limit
    return best, evidence

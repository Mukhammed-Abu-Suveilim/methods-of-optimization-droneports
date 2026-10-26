"""Независимое восстановление выполнимости сохранённого расписания."""

import math
from collections import defaultdict

from scripts.model_data import (
    DAY_WEIGHTS,
    charging_slots,
    fixed_cost,
    flight_cost,
    flight_seconds,
    plan_errors,
)
from scripts.model_types import Day, InstanceData, OperatingResult, Scenario


def validate_day(
    instance: InstanceData,
    day: Day,
    opened: set[str],
    scenario: Scenario,
    fleet: int,
) -> list[str]:
    """Проверить дроны, ресурсы и каждый пассажирский путь."""
    # 1. Восстановить индивидуальные положения, время и остаток батареи.
    errors: list[str] = []
    settings = instance["settings"]
    start = int(settings["day_start_hour"] * 3600)
    end = int(settings["day_end_hour"] * 3600)
    turnaround = math.ceil(settings["turnaround_min"] * 60)
    full = round(scenario["range_km"] * 10)
    if len(day["bases"]) != fleet:
        errors.append("Количество баз не совпадает с размером флота")
    for drone, base in enumerate(day["bases"]):
        if base not in instance["points"] and fleet:
            errors.append("Неизвестный ID базы дрона")
        position, battery, ready = base, full, start
        events = [
            (f["departure_s"], "flight", index)
            for index, f in enumerate(day["flights"])
            if f["drone"] == drone
        ]
        events += [
            (c["start_s"], "charge", index)
            for index, c in enumerate(day["charges"])
            if c["drone"] == drone
        ]
        for time_s, kind, index in sorted(events):
            if time_s < ready:
                errors.append(f"Дрон {drone}: пересечение обслуживания")
            if kind == "charge":
                charge = day["charges"][index]
                if charge["port"] != position or position not in opened:
                    errors.append("Зарядка в недоступном положении")
                if charge["end_s"] - time_s != math.ceil(
                    settings["recharge_min"] * 60
                ):
                    errors.append("Неверная длительность зарядки")
                ready, battery = charge["end_s"], full
            else:
                flight = day["flights"][index]
                pair = flight["origin"], flight["destination"]
                if pair not in instance["distance"]:
                    errors.append("Неизвестный участок рейса")
                    continue
                if pair[0] != position or not set(pair) <= opened:
                    errors.append("Разрыв положения или закрытый порт")
                battery -= instance["distance"][pair]
                if battery < 0:
                    errors.append("Превышен остаток дальности")
                if flight["arrival_s"] - time_s != flight_seconds(
                    instance, pair
                ):
                    errors.append("Неверная продолжительность полёта")
                ready = flight["arrival_s"] + turnaround
                position = pair[1]
            if time_s < start or ready > end:
                errors.append("Операция за пределами рабочего окна")
        if position != base or battery != full:
            errors.append("Не восстановлено исходное состояние дрона")

    # 2. Проверить общие движения и одновременную зарядную ёмкость.
    for flight in day["flights"]:
        if not isinstance(flight["drone"], int) or not (
            0 <= flight["drone"] < fleet
        ):
            errors.append("Рейс назначен отсутствующему дрону")
        if not all(
            isinstance(flight[key], int)
            for key in ("departure_s", "arrival_s")
        ):
            errors.append("Время рейса задано не в целых секундах")
    for charge in day["charges"]:
        if not isinstance(charge["drone"], int) or not (
            0 <= charge["drone"] < fleet
        ):
            errors.append("Зарядка назначена отсутствующему дрону")
    for port in opened:
        movements = [
            [f["departure_s"] for f in day["flights"] if f["origin"] == port],
            [
                f["arrival_s"]
                for f in day["flights"]
                if f["destination"] == port
            ],
        ]
        for movement_times in movements:
            times = sorted(movement_times)
            if any(
                sum(t <= other < t + 3600 for other in times)
                > settings["takeoffs_per_hour"]
                for t in times
            ):
                errors.append(f"Превышен часовой лимит движений: {port}")
        charges = [c for c in day["charges"] if c["port"] == port]
        events_charge = [(c["start_s"], 1) for c in charges]
        events_charge += [(c["end_s"], -1) for c in charges]
        occupied = 0
        for _, change in sorted(events_charge):
            occupied += change
            if occupied > charging_slots(instance, port):
                errors.append(f"Превышено число зарядных мест: {port}")

    # 3. Проверить идентичность рейсов, соединения и занятые места.
    flights = {f["id"]: f for f in day["flights"]}
    if len(flights) != len(day["flights"]):
        errors.append("Повтор ID рейса")
    load: dict[str, int] = defaultdict(int)
    for journey in day["journeys"]:
        if journey["count"] <= 0 or int(journey["count"]) != journey["count"]:
            errors.append("Некорректное число пассажиров")
        if not journey["flights"]:
            errors.append("Пустой пассажирский маршрут")
        position, ready = journey["origin"], start
        for flight_id in journey["flights"]:
            if flight_id not in flights:
                errors.append("Пассажир назначен отсутствующему рейсу")
                continue
            flight = flights[flight_id]
            if position != flight["origin"] or flight["departure_s"] < ready:
                errors.append("Невозможная пересадка пассажира")
            position = flight["destination"]
            ready = flight["arrival_s"] + turnaround
            load[flight_id] += journey["count"]
        if position != journey["destination"]:
            errors.append("Поездка не завершена в конечном пункте")
    if any(count > settings["seats"] for count in load.values()):
        errors.append("Превышена вместимость рейса")
    return errors


def validate_result(
    instance: InstanceData,
    result: OperatingResult,
    scenario: Scenario,
) -> list[str]:
    """Пересчитать три года, не доверяя сохранённым суммам и флагам."""
    # 1. Проверить стратегию и соответствие исходным данным.
    errors = plan_errors(instance, result["plan"])
    if result["input_hashes"] != instance["hashes"]:
        errors.append("Не совпадают хеши входных файлов")
    if len(result["years"]) != 3:
        return errors + ["Отсутствует трёхлетнее расписание"]
    cumulative = 0.0
    total = 0

    # 2. Восстановить ежегодный спрос и расходы из рейсов и поездок.
    for year, row in enumerate(result["years"], 1):
        opened = {
            p for p, t in result["plan"]["open_year"].items() if t <= year
        }
        physical = opened - (
            {scenario["closed_port"]}
            if year == scenario["closed_year"]
            else set()
        )
        days = row["days"]
        if [day["weight"] for day in days] != DAY_WEIGHTS:
            errors.append("Неверные веса типовых суток")
        if days and any(day["bases"] != days[0]["bases"] for day in days):
            errors.append("Разная дислокация между типами суток")
        if days and days[0]["bases"] != result["plan"]["bases"][year - 1]:
            errors.append("Расписание не соответствует базам плана")
        if days and any(base not in opened for base in days[0]["bases"]):
            errors.append("Дрон базируется в непостроенном порту")
        if year > 1 and days:
            previous_bases = result["years"][year - 2]["days"][0]["bases"]
            if days[0]["bases"][: len(previous_bases)] != previous_bases:
                errors.append("Бесплатный перенос базы между годами")
        quantities: dict[tuple[str, str], int] = defaultdict(int)
        flight_expense = 0.0
        for day in days:
            errors.extend(
                validate_day(
                    instance,
                    day,
                    physical,
                    scenario,
                    result["plan"]["fleet"][year - 1],
                )
            )
            for journey in day["journeys"]:
                quantities[journey["origin"], journey["destination"]] += (
                    day["weight"] * journey["count"]
                )
            flight_expense += day["weight"] * sum(
                flight_cost(instance, (f["origin"], f["destination"]))
                for f in day["flights"]
            )
        for pair, count in quantities.items():
            if count > math.floor(
                instance["demand"].get(pair, (0, 0, 0))[year - 1]
                * scenario["demand_multiplier"]
            ):
                errors.append("Превышен годовой спрос OD")
        trips = sum(quantities.values())
        fixed_expense = fixed_cost(instance, result["plan"], year)
        expense = fixed_expense + flight_expense
        cumulative += expense
        total += trips
        if (
            row["year"] != year
            or row["fleet"] != result["plan"]["fleet"][year - 1]
            or row["ports"] != len(opened)
        ):
            errors.append("Не совпадают год, число портов или флот")
        if (
            abs(row["fixed_cost_rub"] - fixed_expense) > 0.01
            or abs(row["flight_cost_rub"] - flight_expense) > 0.01
        ):
            errors.append("Не совпадают составляющие расходов")
        if row["trips"] != trips or abs(row["cost_rub"] - expense) > 0.01:
            errors.append("Сохранённые показатели года не совпадают")
        if abs(row["cumulative_cost_rub"] - cumulative) > 0.01:
            errors.append("Не совпадают накопленные расходы")
        if (
            cumulative
            > year * instance["settings"]["tranche_rub_per_year"] + 0.01
        ):
            errors.append("Нарушен накопленный бюджет")
    if total != result["objective_trips"]:
        errors.append("Целевая не совпадает с завершёнными поездками")
    if abs(cumulative - result["cum_spend_rub"]) > 0.01:
        errors.append("Не совпадает общий расход")
    return sorted(set(errors))

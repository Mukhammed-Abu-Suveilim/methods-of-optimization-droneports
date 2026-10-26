"""Проверки физических, денежных и воспроизводимых инвариантов."""

import copy
import importlib
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pulp  # type: ignore[import-untyped]  # Проверяем внешний CBC.

from scripts import model_data
from scripts.bounds import (
    build_relaxation,
    certified_gap,
    has_integer_solution,
    schedule_warm_start,
)
from scripts.evaluation import (
    evaluate_plan,
    evaluate_recourse,
    read_result,
    remove_unused_ports,
    reuse_timetable,
)
from scripts.model_data import base_scenario, fixed_cost, load_instance
from scripts.model_types import Day, InstanceData, Plan, Point
from scripts.operations import (
    append_route,
    assign_passengers,
    available_charge,
    available_time,
    recharge,
)
from scripts.routing import compare_routes
from scripts.studies import configuration_directory, game
from scripts.validation import validate_day, validate_result


def fixture() -> InstanceData:
    """
    Создать маленькую задачу с направленным спросом и повтором имён.
    """
    # 1. Переиспользовать паспорт и задать дешёвую учебную
    # инфраструктуру.
    instance = copy.deepcopy(load_instance("small"))
    original = next(iter(instance["points"].values()))
    points: dict[str, Point] = {}
    for point_id in ("A", "B", "C"):
        point = copy.deepcopy(original)
        point["id"] = point_id
        point["name"] = "Одинаковое имя"
        points[point_id] = point
    instance["points"] = points
    instance["ids"] = list(points)
    instance["distance"] = {
        (a, b): (1600 if {a, b} == {"A", "C"} else 800)
        for a in points
        for b in points
        if a != b
    }
    instance["demand"] = {
        (a, b): ((3650, 3650, 3650) if a < b else (0, 0, 0))
        for a, b in instance["distance"]
    }
    instance["settings"].update(
        {
            "price_rub": 1000,
            "capex_small_rub": 100,
            "capex_hub_rub": 100,
            "opex_per_year_rub": 10,
            "tranche_rub_per_year": 1_000_000,
            "cost_per_flight_hour_rub": 10,
        }
    )
    return instance


def empty_day() -> Day:
    """Создать одни сутки с одним исходно заряженным дроном."""
    return {
        "weight": 1,
        "bases": ["A"],
        "flights": [],
        "charges": [],
        "journeys": [],
    }


def simple_plan() -> Plan:
    """Открыть два пункта, не смешивая их одинаковые подписи."""
    return {
        "open_year": {"A": 1, "B": 1},
        "fleet": [1, 1, 1],
        "bases": [[], [], []],
    }


class ModelTests(unittest.TestCase):
    """TestCase нужен стандартному unittest, без прикладных классов."""

    def test_passenger_cache_respects_capacity_and_changed_demand(
        self,
    ) -> None:
        """Не переносить назначения на другую вместимость или спрос."""
        # 1. Повторное назначение сохраняет число пассажиров и кэш.
        instance, day = fixture(), empty_day()
        append_route(instance, day, 0, ["A", "B", "A"], 25200, 2000, 2000)
        assign_passengers(instance, [day], 1, base_scenario())
        self.assertEqual(sum(j["count"] for j in day["journeys"]), 10)
        day["journeys"][0]["count"] = 999
        assign_passengers(instance, [day], 1, base_scenario())
        self.assertEqual(sum(j["count"] for j in day["journeys"]), 10)

        # 2. Вместимость и годовой множитель требуют нового решения.
        instance["settings"]["seats"] = 1
        assign_passengers(instance, [day], 1, base_scenario())
        self.assertEqual(sum(j["count"] for j in day["journeys"]), 1)
        scenario = base_scenario()
        scenario["demand_multiplier"] = 0
        assign_passengers(instance, [day], 1, scenario)
        self.assertEqual(day["journeys"], [])

    def test_schedule_preserving_investment_cleanup(self) -> None:
        """Удаление лишнего порта сохраняет пригодные рейсы."""
        # 1. Дополнить реальный график построенным, но неиспользуемым C.
        instance = fixture()
        original = evaluate_plan(instance, simple_plan(), base_scenario())
        expanded = copy.deepcopy(original["plan"])
        expanded["open_year"]["C"] = 1
        source = reuse_timetable(instance, original, expanded, base_scenario())
        self.assertTrue(source["valid"])

        # 2. Проверить сохранение поездок и исключение лишних вложений.
        cleaned = remove_unused_ports(instance, source)
        self.assertTrue(cleaned["valid"])
        self.assertNotIn("C", cleaned["plan"]["open_year"])
        self.assertGreaterEqual(
            cleaned["objective_trips"], source["objective_trips"]
        )
        self.assertLess(cleaned["cum_spend_rub"], source["cum_spend_rub"])

    def test_recourse_retains_feasible_service_and_failure_costs(self) -> None:
        """Сохранять пригодный график и расходы отказавшего порта."""
        # 1. Отказ неиспользуемого C физически не уменьшает перевозки.
        instance = fixture()
        original = evaluate_plan(instance, simple_plan(), base_scenario())
        expanded = copy.deepcopy(original["plan"])
        expanded["open_year"]["C"] = 1
        source = reuse_timetable(instance, original, expanded, base_scenario())
        for day in source["years"][0]["days"]:
            day["flights"], day["charges"], day["journeys"] = [], [], []
        source = reuse_timetable(instance, source, expanded, base_scenario())
        scenario = base_scenario()
        scenario["closed_port"] = "C"
        recourse = evaluate_recourse(instance, source, scenario)
        self.assertTrue(recourse["valid"])
        self.assertGreaterEqual(
            recourse["objective_trips"], source["objective_trips"]
        )
        self.assertEqual(
            recourse["years"][2]["fixed_cost_rub"],
            fixed_cost(instance, expanded, 3),
        )
        self.assertEqual(recourse["years"][:2], source["years"][:2])

        # 2. Изменённый спрос повторно ограничивает пассажирские места.
        scenario = base_scenario()
        scenario["demand_multiplier"] = 0.7
        recourse = evaluate_recourse(instance, source, scenario)
        self.assertFalse(validate_result(instance, recourse, scenario))

    def test_relaxation_contains_scheduled_solution(self) -> None:
        """Проверить вложение фактических рейсов во все строки MILP."""
        instance = fixture()
        result = evaluate_plan(instance, simple_plan(), base_scenario())
        problem, _, _ = build_relaxation(instance, base_scenario())
        schedule_warm_start(problem, result)
        self.assertTrue(problem.valid(0.001))
        self.assertEqual(
            pulp.value(problem.objective), result["objective_trips"]
        )

    def test_route_algorithms_and_game_agree(self) -> None:
        """Проверить оба требуемых алгоритмических равенства."""
        # 1. Сопоставить все достижимые OD с эталоном Dijkstra.
        instance = fixture()
        plan = simple_plan()
        routes = compare_routes(instance, plan)
        self.assertEqual(routes["pairs"], 2)
        self.assertTrue(routes["dijkstra_agreement"])

        # 2. Использовать одни реальные сценарные payoffs для обходов.
        result = evaluate_plan(instance, plan, base_scenario())
        with tempfile.TemporaryDirectory() as temporary:
            values = game(instance, [result], Path(temporary))
        self.assertTrue(values["values_equal"])
        self.assertEqual(values["full_leaves"], 2)

    def test_hourly_boundary(self) -> None:
        """Шесть событий допустимы, седьмое ждёт границы часа."""
        self.assertEqual(available_time([0] * 6, 0, 6), 3600)
        self.assertEqual(available_time([0] * 5, 0, 6), 0)
        self.assertEqual(available_time([3600] * 6, 0, 6), 0)

    def test_future_hourly_conflict(self) -> None:
        """Новый рейс учитывает уже назначенные будущие движения."""
        self.assertGreaterEqual(available_time([100] * 6, 99, 6), 3600)

    def test_charging_overlap(self) -> None:
        """Полуоткрытые интервалы освобождают место ровно в конце."""
        self.assertEqual(available_charge([(0, 10)], 0, 5, 1), 10)
        self.assertEqual(available_charge([(0, 10)], 10, 5, 1), 10)
        self.assertEqual(available_charge([(5, 20)], 0, 10, 1), 20)

    def test_battery_carries_between_legs(self) -> None:
        """Два коротких перелёта используют один запас дальности."""
        instance, day = fixture(), empty_day()
        _, remaining = append_route(
            instance, day, 0, ["A", "B", "A"], 7 * 3600, 2000, 2000
        )
        self.assertEqual(remaining, 400)
        self.assertEqual(day["charges"], [])

    def test_exact_range_and_reset(self) -> None:
        """Полный запас можно израсходовать ровно до нуля."""
        instance, day = fixture(), empty_day()
        instance["distance"]["A", "B"] = 1000
        instance["distance"]["B", "A"] = 1000
        ready, remaining = append_route(
            instance, day, 0, ["A", "B", "A"], 7 * 3600, 2000, 2000
        )
        self.assertEqual(remaining, 0)
        ready = recharge(instance, day, 0, "A", ready)
        self.assertLess(ready, 21 * 3600)
        self.assertEqual(
            validate_day(instance, day, {"A", "B"}, base_scenario(), 1), []
        )

    def test_charge_before_insufficient_leg(self) -> None:
        """
        Нехватка остатка вызывает реальную зарядку на текущем узле.
        """
        instance, day = fixture(), empty_day()
        append_route(
            instance, day, 0, ["A", "B", "A", "B"], 7 * 3600, 2000, 2000
        )
        self.assertEqual(len(day["charges"]), 1)
        self.assertEqual(day["charges"][0]["port"], "A")

    def test_reject_long_leg(self) -> None:
        """
        Нельзя исправить недопустимый участок дополнительной зарядкой.
        """
        instance = fixture()
        instance["distance"]["A", "B"] = 2001
        with self.assertRaises(ValueError):
            append_route(
                instance, empty_day(), 0, ["A", "B"], 7 * 3600, 2000, 2000
            )

    def test_three_year_operating_certificate(self) -> None:
        """Все суммы восстанавливаются из восьми взвешенных шаблонов."""
        instance = fixture()
        result = evaluate_plan(instance, simple_plan(), base_scenario())
        self.assertTrue(result["valid"], result["violations"])
        self.assertGreater(result["objective_trips"], 0)
        self.assertEqual(len(result["years"]), 3)
        for year in result["years"]:
            self.assertEqual(sum(d["weight"] for d in year["days"]), 365)
        self.assertEqual(
            validate_result(instance, result, base_scenario()), []
        )

    def test_roundtrip_ids_and_resume(self) -> None:
        """Сохранение не смешивает одноимённые точки и настройки."""
        instance = fixture()
        result = evaluate_plan(instance, simple_plan(), base_scenario())
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "instance.json"
            model_data.store_instance(snapshot, instance)
            restored = model_data.read_instance(snapshot)
            self.assertEqual(restored, instance)
            self.assertEqual(
                configuration_directory(restored),
                configuration_directory(instance),
            )
            path = Path(directory) / "result.json"
            model_data.write_json(path, result)
            loaded = read_result(path, instance, base_scenario())
            self.assertEqual(loaded["plan"]["open_year"], {"A": 1, "B": 1})
            instance["settings"]["price_rub"] += 1
            with self.assertRaises(ValueError):
                read_result(path, instance, base_scenario())

    def test_tampered_totals(self) -> None:
        """Валидатор не доверяет сохранённому valid=True."""
        instance = fixture()
        result = copy.deepcopy(
            evaluate_plan(instance, simple_plan(), base_scenario())
        )
        result["objective_trips"] += 1
        self.assertIn(
            "Целевая не совпадает с завершёнными поездками",
            validate_result(instance, result, base_scenario()),
        )

    def test_demand_capacity_and_empty_flights(self) -> None:
        """Порожний возврат оплачивается при одностороннем спросе."""
        instance = fixture()
        result = evaluate_plan(instance, simple_plan(), base_scenario())
        for row in result["years"]:
            self.assertLessEqual(row["trips"], 3650)
            day = row["days"][0]
            used = {f for j in day["journeys"] for f in j["flights"]}
            self.assertTrue(any(f["id"] not in used for f in day["flights"]))
            self.assertGreater(row["flight_cost_rub"], 0)

    def test_budget_and_failure_costs(self) -> None:
        """Закрытие не возвращает CAPEX или фиксированное содержание."""
        instance, plan = fixture(), simple_plan()
        expected = [fixed_cost(instance, plan, y) for y in (1, 2, 3)]
        failure = base_scenario()
        failure["closed_port"] = "B"
        result = evaluate_plan(instance, plan, failure)
        self.assertTrue(result["valid"])
        self.assertEqual(
            [y["fixed_cost_rub"] for y in result["years"]], expected
        )
        self.assertEqual(result["years"][2]["trips"], 0)
        self.assertGreater(result["years"][0]["trips"], 0)

    def test_no_borrowing(self) -> None:
        """Нельзя купить в первом году дрон на будущий транш."""
        instance = fixture()
        instance["settings"]["tranche_rub_per_year"] = 100
        result = evaluate_plan(instance, simple_plan(), base_scenario())
        self.assertFalse(result["valid"])

    def test_bases_persist_between_years(self) -> None:
        """Уже приобретённый дрон не получает новую базу бесплатно."""
        instance, plan = fixture(), simple_plan()
        plan["bases"] = [["A"], ["B"], ["B"]]
        result = evaluate_plan(instance, plan, base_scenario())
        self.assertTrue(result["valid"])
        self.assertEqual(result["plan"]["bases"], [["A"], ["A"], ["A"]])
        changed = copy.deepcopy(result)
        changed["years"][1]["days"][0]["bases"] = ["B"]
        self.assertIn(
            "Бесплатный перенос базы между годами",
            validate_result(instance, changed, base_scenario()),
        )

    def test_missing_transfer(self) -> None:
        """Места на участках без временной стыковки не дают поездку."""
        instance, day = fixture(), empty_day()
        day["flights"] = [
            {
                "id": "f0",
                "drone": 0,
                "origin": "A",
                "destination": "B",
                "departure_s": 25200,
                "arrival_s": 27600,
            },
            {
                "id": "f1",
                "drone": 1,
                "origin": "B",
                "destination": "C",
                "departure_s": 27700,
                "arrival_s": 30100,
            },
        ]
        day["journeys"] = [
            {
                "origin": "A",
                "destination": "C",
                "count": 1,
                "flights": ["f0", "f1"],
            }
        ]
        self.assertIn(
            "Невозможная пересадка пассажира",
            validate_day(instance, day, {"A", "B", "C"}, base_scenario(), 2),
        )

    def test_integer_passenger_assignment(self) -> None:
        """Редкий годовой спрос не округляется до 365 поездок."""
        instance = fixture()
        instance["demand"]["A", "B"] = (5, 5, 5)
        regular, extra = empty_day(), empty_day()
        regular["weight"], extra["weight"] = 364, 1
        for day in (regular, extra):
            day["flights"] = [
                {
                    "id": "f0",
                    "drone": 0,
                    "origin": "A",
                    "destination": "B",
                    "departure_s": 25200,
                    "arrival_s": 27600,
                }
            ]
        assign_passengers(instance, [regular, extra], 1, base_scenario())
        count = sum(
            d["weight"] * sum(j["count"] for j in d["journeys"])
            for d in (regular, extra)
        )
        self.assertEqual(count, 5)

    def test_upper_bound_contains_scheduled_solution(self) -> None:
        """LP-граница не ниже проверенного физического решения."""
        instance = fixture()
        result = evaluate_plan(instance, simple_plan(), base_scenario())
        problem, _, _ = build_relaxation(instance, base_scenario(), True)
        problem.solve(pulp.PULP_CBC_CMD(msg=False, threads=0))
        self.assertEqual(problem.status, pulp.LpStatusOptimal)
        self.assertGreaterEqual(
            pulp.value(problem.objective) + 1e-5, result["objective_trips"]
        )
        self.assertEqual(certified_gap(0, 0), 0)
        with self.assertRaises(ValueError):
            certified_gap(11, 10)

    def test_bound_preserves_budget_carryover(self) -> None:
        """Поздняя покупка не должна попадать в бюджет первого года."""
        instance = fixture()
        instance["settings"]["tranche_rub_per_year"] = 1000
        instance["settings"]["cost_per_flight_hour_rub"] = 0
        plan = simple_plan()
        plan["fleet"] = [0, 1, 1]
        scheduled = evaluate_plan(instance, plan, base_scenario())
        self.assertTrue(scheduled["valid"])
        self.assertGreater(scheduled["objective_trips"], 0)
        problem, openings, fleet = build_relaxation(
            instance, base_scenario(), True
        )
        for point_id in instance["ids"]:
            for year in (1, 2, 3):
                problem += openings[point_id, year] == int(
                    plan["open_year"].get(point_id, 4) <= year
                )
        for year in (1, 2, 3):
            problem += fleet[year] == plan["fleet"][year - 1]
        problem.solve(pulp.PULP_CBC_CMD(msg=False, threads=0))
        self.assertEqual(problem.status, pulp.LpStatusOptimal)
        self.assertGreaterEqual(
            pulp.value(problem.objective) + 1e-5, scheduled["objective_trips"]
        )

    def test_imports_do_not_run_work(self) -> None:
        """Импорт основных модулей не выполняет расчёт или запись."""
        before = set(model_data.OUTPUT.rglob("*.json"))
        for name in (
            "operations",
            "evaluation",
            "bounds",
            "studies",
            "run_project",
        ):
            importlib.import_module(f"scripts.{name}")
        self.assertEqual(before, set(model_data.OUTPUT.rglob("*.json")))

    def test_seed_is_local(self) -> None:
        """
        Генератор алгоритма не должен менять глобальную случайность.
        """
        from scripts.search import neighbor

        before = random.getstate()
        instance, plan = fixture(), simple_plan()
        saved = copy.deepcopy(plan)
        first = neighbor(instance, plan, random.Random(2))
        second = neighbor(instance, plan, random.Random(2))
        self.assertEqual(first, second)
        self.assertEqual(plan, saved)
        self.assertEqual(before, random.getstate())

    def test_serial_cbc_obeys_limit(self) -> None:
        """Реальная ветвящаяся small-модель завершается по лимиту."""
        code = (
            "import sys; from pathlib import Path; "
            "from scripts.model_data import load_instance,base_scenario; "
            "from scripts.evaluation import evaluate_plan; "
            "from scripts.bounds import solve_bound; "
            "i=load_instance('small'); s=base_scenario(); "
            "p={'open_year':{},'fleet':[0,0,0],'bases':[[],[],[]]}; "
            "r=evaluate_plan(i,p,s); "
            "solve_bound(i,s,r,Path(sys.argv[1]),False,1)"
        )
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-c", code, directory],
                cwd=model_data.ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            self.assertIn("small bound limit=1", completed.stdout)
            self.assertTrue((Path(directory) / "certificate.json").exists())

    def test_timeout_without_integer_incumbent(self) -> None:
        """Не считать дробные значения допустимым планом."""
        # 1. Без warm start нулевой лимит не даёт целого incumbent.
        instance = load_instance("small")
        problem, _, _ = build_relaxation(instance, base_scenario())
        with tempfile.TemporaryDirectory() as directory:
            problem.solve(
                pulp.PULP_CBC_CMD(
                    msg=False,
                    threads=0,
                    timeLimit=0,
                    logPath=str(Path(directory) / "cbc.log"),
                )
            )

        # 2. Целевая LP положительна, но не является допустимым L.
        self.assertGreater(pulp.value(problem.objective), 0)
        self.assertFalse(has_integer_solution(problem.sol_status))


if __name__ == "__main__":
    unittest.main()

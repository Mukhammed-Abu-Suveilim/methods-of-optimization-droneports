"""Проверки ограниченного вычислительного протокола."""

import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from scripts.bounded import wait_worker
from scripts.commands import main
from scripts.evaluation import evaluate_plan, reuse_timetable
from scripts.model_data import base_scenario
from tests.test_model import fixture, simple_plan


class BoundedTests(unittest.TestCase):
    """Проверить остановку и безопасное повторное использование."""

    def test_bounded_all_does_not_enter_unrestricted_pipeline(self) -> None:
        """Ограниченная команда не отправляется в полный расчёт CBC."""
        with (
            patch("scripts.commands.run_all") as unrestricted,
            patch(
                "scripts.commands.run_quality_checks",
                side_effect=RuntimeError("bounded quality gate"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "bounded quality"):
                main(["all", "--profile", "bounded"])
            unrestricted.assert_not_called()

    def test_worker_deadline(self) -> None:
        """Зависший собственный процесс останавливается в срок."""
        started = time.monotonic()
        process = subprocess.Popen(
            [
                str(getattr(sys, "_base_executable", sys.executable)),
                "-c",
                "import time; time.sleep(30)",
            ]
        )
        self.assertEqual(wait_worker(process, 0.05), "time_limit")
        self.assertIsNotNone(process.poll())
        self.assertLess(time.monotonic() - started, 5)

    def test_budget_only_reuse(self) -> None:
        """Новый транш не требует перераспределения пассажиров."""
        # 1. Получить допустимое расписание на маленьком примере.
        instance = fixture()
        plan = simple_plan()
        source = evaluate_plan(instance, plan, base_scenario())
        instance["settings"]["tranche_rub_per_year"] *= 2
        # 2. Проверить сохранение перевозок без вызова оптимизатора.
        with patch("scripts.evaluation.assign_passengers") as solve:
            result = reuse_timetable(
                instance, source, source["plan"], base_scenario()
            )
            solve.assert_not_called()
        self.assertTrue(result["valid"])
        self.assertEqual(result["objective_trips"], source["objective_trips"])

"""Эксперименты с общим пределом времени и проверенными расписаниями."""

import argparse
import copy
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from scripts.data_audit import clean_candidates, gravity_demand, select_points
from scripts.evaluation import (
    evaluate_plan,
    rank,
    read_result,
    reuse_timetable,
)
from scripts.model_data import (
    MODEL_VERSION,
    OUTPUT,
    ROOT,
    base_scenario,
    load_instance,
    read_instance,
    store_instance,
    write_json,
)
from scripts.model_types import InstanceData, OperatingResult
from scripts.search import neighbor
from scripts.studies import configuration_directory


def worker(folder: Path, seed: int) -> None:
    """Искать до остановки родителем, сохраняя каждое улучшение."""
    # 1. Прочитать изолированный экземпляр и допустимый старт.
    instance = read_instance(folder / "instance.json")
    path = folder / "best.json"
    best = (
        read_result(path, instance, base_scenario()) if path.exists() else None
    )
    if best is None:
        plan = json.loads((folder / "start_plan.json").read_text("utf-8"))
        best = evaluate_plan(instance, plan, base_scenario())
        if not best["valid"]:
            # Слишком дорогой старт заменить дешёвым сохранённым планом.
            source = min(portfolio(), key=lambda row: row["cum_spend_rub"])
            best = evaluate_plan(instance, source["plan"], base_scenario())
            if not best["valid"]:
                return
        write_json(path, best)
    current = best
    rng = random.Random(seed)
    temperature = max(1.0, best["objective_trips"] * 0.01)
    floor = temperature * 0.001

    # 2. Сохранять каждое проверенное улучшение отжига.
    while True:
        temperature = max(floor, temperature * 0.995)
        candidate = evaluate_plan(
            instance, neighbor(instance, current["plan"], rng), base_scenario()
        )
        if not candidate["valid"]:
            continue
        delta = candidate["objective_trips"] - current["objective_trips"]
        if delta > 0 or rng.random() < math.exp(min(0, delta / temperature)):
            current = candidate
        if rank(candidate) > rank(best):
            best = candidate
            write_json(path, best)


def wait_worker(process: subprocess.Popen[bytes], seconds: float) -> str:
    """Остановить только собственный процесс по истечении срока."""
    # 1. Дождаться завершения в выделенном интервале.
    try:
        code = process.wait(timeout=max(0, seconds))
        return "completed" if code == 0 else "worker_error"
    except subprocess.TimeoutExpired:
        # 2. Остановить собственное дерево и забрать код завершения.
        if os.name == "nt":
            stopped = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            if stopped.returncode:
                process.kill()
        else:
            process.kill()
        process.wait()
        return "time_limit"


def portfolio() -> list[OperatingResult]:
    """Собрать совместимые планы, включая завершённые отдельные seed."""
    base = load_instance()
    found: dict[str, OperatingResult] = {}
    # 1. Принимать только текущую модель с неизменной физикой и спросом.
    for snapshot in sorted(
        (OUTPUT / "configurations").glob("*/instance.json")
    ):
        instance = read_instance(snapshot)
        if instance["label"] != "main" or any(
            v != base["settings"][k]
            for k, v in instance["settings"].items()
            if k != "tranche_rub_per_year"
        ):
            continue
        if configuration_directory(instance) != snapshot.parent:
            continue
        paths = list(snapshot.parent.glob("*.json"))
        paths += list((snapshot.parent / "published").glob("*.json"))
        for path in sorted(paths):
            if path.stem not in ("best", "greedy", "staged") and not (
                path.stem.startswith("seed_") and path.stem[5:].isdigit()
            ):
                continue
            result = read_result(path, instance, base_scenario())
            key = json.dumps(result["plan"], sort_keys=True)
            if key not in found or rank(result) > rank(found[key]):
                found[key] = result
    return sorted(found.values(), key=rank, reverse=True)


def choose(
    instance: InstanceData, candidates: list[OperatingResult], deadline: float
) -> OperatingResult | None:
    """Перепроверить задания без нового распределения спроса."""
    best = None
    for source in candidates:
        if time.monotonic() >= deadline:
            break
        result = reuse_timetable(
            instance, source, source["plan"], base_scenario()
        )
        if result["valid"] and (best is None or rank(result) > rank(best)):
            best = result
    return best


def short_search(
    instance: InstanceData,
    initial: OperatingResult | None,
    fallback: OperatingResult,
    seed: int,
    seconds: float,
    folder: Path,
) -> tuple[OperatingResult | None, str]:
    """Ограничить всё время отдельного поиска, включая запуск Python."""
    # 1. Записать входы и старт до запуска независимого процесса.
    store_instance(folder / "instance.json", instance)
    if initial is not None:
        write_json(folder / "best.json", initial)
    write_json(folder / "start_plan.json", fallback["plan"])
    with (folder / "worker.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.bounded", str(folder), str(seed)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        status = wait_worker(process, seconds)
    # 2. После остановки читать только атомарно сохранённое расписание.
    path = folder / "best.json"
    result = (
        read_result(path, instance, base_scenario()) if path.exists() else None
    )
    return result, status


def run_bounded(max_wall_seconds: float = 1800) -> dict[str, object]:
    """Пройти сокращённый протокол с общим монотонным сроком."""
    # 1. Подготовить профиль и проверить эквивалентность данных.
    started = time.monotonic()
    max_wall_seconds = float(max_wall_seconds)
    deadline = started + max_wall_seconds - min(5, max_wall_seconds / 10)
    base = load_instance()
    identity = hashlib.sha256(
        repr((MODEL_VERSION, base, max_wall_seconds, "bounded-1")).encode()
    ).hexdigest()[:16]
    saved = OUTPUT / "experiments_bounded.json"
    if saved.exists():
        previous: dict[str, object] = json.loads(saved.read_text("utf-8"))
        if previous.get("identity") == identity and previous.get(
            "revised_protocol_completed"
        ):
            return previous
    folder = OUTPUT / "bounded" / identity
    candidates = portfolio()
    if not candidates:
        raise ValueError("Нет проверенного портфеля")
    raw = clean_candidates()
    dropped = select_points(raw.loc[raw.population.notna()].copy())
    filled = raw.copy()
    groups = filled.types.fillna("").str.split("|").str[0]
    medians = filled.groupby(groups).population.transform("median")
    filled["population"] = filled.population.fillna(medians).fillna(
        filled.population.median()
    )
    filled = select_points(filled)
    for label, frame in (("drop_missing", dropped), ("type_median", filled)):
        folder.mkdir(parents=True, exist_ok=True)
        frame.to_csv(folder / f"{label}_points.csv", index=False)
        gravity_demand(frame).to_csv(
            folder / f"{label}_demand.csv", index=False
        )
    equivalent = dropped.equals(filled) and gravity_demand(dropped).equals(
        gravity_demand(filled)
    )
    if not equivalent:
        raise ValueError("Экспериментальные входы различаются")
    report: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "input_hashes": base["hashes"],
        "profile": "bounded",
        "identity": identity,
        "max_wall_seconds": max_wall_seconds,
        "data_quality_equivalent": True,
        "data_quality_source": str(configuration_directory(base)),
    }
    funding: list[dict[str, object]] = []
    runs: list[dict[str, object]] = []

    # 2. Пройти сетку и два уточнения с сохранением заданий.
    multipliers = [0.5 + 0.25 * i for i in range(7)]
    for index in range(9):
        if index >= 7:
            ordered = sorted(
                funding, key=lambda row: float(str(row["multiplier"]))
            )
            left, right = max(
                zip(ordered, ordered[1:]),
                key=lambda pair: (
                    (
                        float(str(pair[1]["trips"]))
                        - float(str(pair[0]["trips"]))
                    )
                    / (
                        float(str(pair[1]["multiplier"]))
                        - float(str(pair[0]["multiplier"]))
                    )
                ),
            )
            multipliers.append(
                (
                    float(str(left["multiplier"]))
                    + float(str(right["multiplier"]))
                )
                / 2
            )
        multiplier = multipliers[index]
        instance = copy.deepcopy(base)
        instance["settings"]["tranche_rub_per_year"] *= multiplier
        best = choose(instance, candidates, deadline)
        if multiplier > 1 or index >= 7:
            for seed in range(3):
                remaining = deadline - time.monotonic()
                if remaining < 65:
                    break
                best, status = short_search(
                    instance,
                    best,
                    candidates[0],
                    seed,
                    60,
                    folder / f"funding_{index}_seed_{seed}",
                )
                runs.append(
                    {"case": multiplier, "seed": seed, "status": status}
                )
        if best is not None:
            path = folder / f"funding_{index}.json"
            write_json(path, best)
            candidates.append(best)
            funding.append(
                {
                    "multiplier": multiplier,
                    "trips": best["objective_trips"],
                    "cost_rub": best["cum_spend_rub"],
                    "result": str(path),
                }
            )
        print(
            f"bounded funding={multiplier} saved={best is not None}",
            flush=True,
        )
        report.update({"funding": funding, "runs": runs})
        write_json(OUTPUT / "experiments_bounded.json", report)

    # 3. Закрыть чувствительность: повторная оценка либо короткий поиск.
    sensitivity: list[dict[str, object]] = []
    cases = [
        (key, scale)
        for key in (
            "price_rub",
            "opex_per_year_rub",
            "cost_per_flight_hour_rub",
            "cruise_speed_kmh",
            "recharge_min",
            "port_capex",
        )
        for scale in (0.8, 1.2)
    ] + [
        ("takeoffs_per_hour", 4),
        ("takeoffs_per_hour", 8),
        ("chargers", -1),
        ("chargers", 1),
    ]
    for index, (key, scale) in enumerate(cases):
        instance = copy.deepcopy(base)
        settings = instance["settings"]
        if key == "port_capex":
            for name in ("capex_small_rub", "capex_hub_rub"):
                settings[name] *= scale
        elif key == "chargers":
            for name in ("charge_slots_small", "charge_slots_hub"):
                settings[name] = max(1, settings[name] + scale)
        else:
            settings[key] = (
                scale if key == "takeoffs_per_hour" else settings[key] * scale
            )
        existing = configuration_directory(instance) / "published/best.json"
        best = (
            read_result(existing, instance, base_scenario())
            if existing.exists()
            else choose(instance, candidates, deadline)
        )
        status = (
            "saved_result" if existing.exists() else "saved_timetable_response"
        )
        if not existing.exists() and deadline - time.monotonic() >= 50:
            best, status = short_search(
                instance,
                best,
                candidates[0],
                0,
                45,
                folder / f"sensitivity_search_{index}",
            )
        path = folder / f"sensitivity_{index}.json"
        if best is not None:
            write_json(path, best)
            store_instance(
                folder / f"sensitivity_{index}_instance.json", instance
            )
        sensitivity.append(
            {
                "case": f"{key}_{scale}",
                "status": status
                if best
                else "not_established_within_allowance",
                "trips": best["objective_trips"] if best else None,
                "cost_rub": best["cum_spend_rub"] if best else None,
                "result": str(path) if best else None,
            }
        )
        print(f"bounded sensitivity={key}_{scale} status={status}", flush=True)
    report.update(
        {
            "sensitivity": sensitivity,
            "wall_s": time.monotonic() - started,
            "revised_protocol_completed": len(funding) == 9,
            "original_unrestricted_completed": False,
        }
    )
    write_json(OUTPUT / "experiments_bounded.json", report)
    return report


def finalize_bounded() -> None:
    """Сравнить итоговый общий портфель и записать происхождение."""
    # 1. Добавить опубликованные планы и все новые бюджетные результаты.
    started = time.monotonic()
    path = OUTPUT / "experiments_bounded.json"
    report = json.loads(path.read_text("utf-8"))
    if not report.get("revised_protocol_completed"):
        raise ValueError("Серия ещё не завершена")
    deadline = started + max(
        0, report["max_wall_seconds"] - report["wall_s"] - 1
    )
    candidates = portfolio()
    base = load_instance()
    for row in report["funding"]:
        instance = copy.deepcopy(base)
        instance["settings"]["tranche_rub_per_year"] *= row["multiplier"]
        candidates.append(
            read_result(Path(row["result"]), instance, base_scenario())
        )
    report["portfolio_hashes"] = [r["configuration_hash"] for r in candidates]
    # 2. Применить один итоговый портфель без дополнительных поисков.
    for row in report["funding"]:
        instance = copy.deepcopy(base)
        instance["settings"]["tranche_rub_per_year"] *= row["multiplier"]
        best = choose(instance, candidates, deadline)
        if best is not None:
            write_json(Path(row["result"]), best)
            row.update(
                trips=best["objective_trips"], cost_rub=best["cum_spend_rub"]
            )
    report["search_settings"] = {
        "funding_seeds": [0, 1, 2],
        "funding_seed_seconds": 60,
        "sensitivity_seed": 0,
        "sensitivity_seconds": 45,
        "max_concurrent_workers": 1,
        "refinement_rule": "largest observed marginal gain at selection time",
    }
    # 3. Записать переносимые ссылки и отдачу дополнительного бюджета.
    ordered = sorted(report["funding"], key=lambda row: row["multiplier"])
    report["marginal_gains"] = [
        {
            "left": left["multiplier"],
            "right": right["multiplier"],
            "trips_per_million_rub": (right["trips"] - left["trips"])
            / ((right["multiplier"] - left["multiplier"]) * 3000),
        }
        for left, right in zip(ordered, ordered[1:])
    ]
    for row in report["funding"] + report["sensitivity"]:
        if row["result"]:
            target = Path(row["result"])
            if target.is_absolute():
                row["result"] = target.relative_to(ROOT).as_posix()
    report["data_quality_source"] = (
        configuration_directory(base).relative_to(ROOT).as_posix()
    )
    report["revised_protocol_completed"] = (
        len(report["funding"]) == 9
        and len(report["sensitivity"]) == 16
        and report["data_quality_equivalent"]
    )
    report["wall_s"] += time.monotonic() - started
    write_json(path, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("seed", type=int)
    arguments = parser.parse_args()
    worker(arguments.folder, arguments.seed)

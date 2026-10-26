"""Чтение замороженных входов и общая денежная арифметика."""

import csv
import hashlib
import json
import math
from pathlib import Path

from scripts.model_types import InstanceData, Plan, Point, Scenario

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/results/model_v2"
MODEL_VERSION = "2.1"
RECOURSE_VERSION = 3
DAY_WEIGHTS = [52] * 7 + [1]
FROZEN_HASHES = {
    "data/parameters.json": (
        "a7e4d0674a1c308a562f6e2c9c30641ac96c69fff538fbd0cde4f741f1929859"
    ),
    "data/processed/points_main.csv": (
        "48baf349cee76833d8dae7591a02b3fcc2fa88c2577bce2cb9582ca0c845d126"
    ),
    "data/processed/points_small.csv": (
        "301eb53eccbc438c323ce002159aba701e98c009dd255e19ece56648ba4abbb4"
    ),
    "data/processed/demand_main.csv": (
        "e9812237a8c595dfc3ed9c187acf49a918f576e274a7501c9c7af85e18984b08"
    ),
}


def input_hashes() -> dict[str, str]:
    """Получить SHA-256 четырёх замороженных файлов."""
    paths = [
        "data/parameters.json",
        "data/processed/points_main.csv",
        "data/processed/points_small.csv",
        "data/processed/demand_main.csv",
    ]
    return {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths
    }


def read_points(path: Path) -> dict[str, Point]:
    """Прочитать пункты с явным преобразованием значений CSV."""
    # 1. Прочитать текстовые значения и сохранить идентичность объектов.
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    points: dict[str, Point] = {}
    for row in rows:
        point: Point = {
            "id": row["wikidata_id"],
            "name": row["name"],
            "region": row["region"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "population": float(row["population"]),
            "types": row.get("types", ""),
        }
        if point["id"] in points:
            raise ValueError(f"Повтор ID: {point['id']}")
        points[point["id"]] = point
    return points


def load_instance(
    label: str = "main",
    points_path: Path | None = None,
    demand_path: Path | None = None,
) -> InstanceData:
    """Загрузить данные; расстояние выражено в десятых долях км."""
    # 1. Собрать численные настройки без изменения паспорта v1.
    hashes = input_hashes()
    if hashes != FROZEN_HASHES:
        raise ValueError("Изменены замороженные входы v1")
    raw = json.loads((ROOT / "data/parameters.json").read_text("utf-8"))
    settings: dict[str, float] = {}
    for section in ("drone", "droneport", "operations", "budget"):
        for key, value in raw[section].items():
            if isinstance(value, (int, float)):
                settings[key] = float(value)
    points = read_points(
        points_path or ROOT / f"data/processed/points_{label}.csv"
    )

    # 2. Оставить пары выбранных пунктов и проверить полноту матрицы.
    distance: dict[tuple[str, str], int] = {}
    demand: dict[tuple[str, str], tuple[int, int, int]] = {}
    path = demand_path or ROOT / "data/processed/demand_main.csv"
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            pair = row["origin_id"], row["destination_id"]
            if pair[0] not in points or pair[1] not in points:
                continue
            distance[pair] = round(float(row["distance_km"]) * 10)
            demand[pair] = (int(row["q1"]), int(row["q2"]), int(row["q3"]))
    if len(distance) != len(points) * (len(points) - 1):
        raise ValueError("Неполная матрица расстояний или спроса")
    return {
        "label": label,
        "ids": list(points),
        "points": points,
        "distance": distance,
        "demand": demand,
        "settings": settings,
        "hashes": hashes,
    }


def base_scenario() -> Scenario:
    """Вернуть базовые условия перевозок."""
    return {
        "name": "base",
        "demand_multiplier": 1.0,
        "range_km": 200.0,
        "closed_port": "",
        "closed_year": 3,
    }


def port_cost(instance: InstanceData, point_id: str) -> float:
    """Получить цену порта, включая зарядную инфраструктуру."""
    settings = instance["settings"]
    hub = (
        instance["points"][point_id]["population"]
        >= settings["hub_if_population_ge"]
    )
    return settings["capex_hub_rub" if hub else "capex_small_rub"]


def charging_slots(instance: InstanceData, point_id: str) -> int:
    """Получить число одновременно используемых зарядных мест."""
    settings = instance["settings"]
    hub = (
        instance["points"][point_id]["population"]
        >= settings["hub_if_population_ge"]
    )
    return int(settings["charge_slots_hub" if hub else "charge_slots_small"])


def fixed_cost(instance: InstanceData, plan: Plan, year: int) -> float:
    """Посчитать ввод, закупки и полное содержание открытых объектов."""
    # 1. Начислить CAPEX ровно в год ввода объектов и покупки дронов.
    settings = instance["settings"]
    openings = plan["open_year"]
    fleet = plan["fleet"]
    previous = fleet[year - 2] if year > 1 else 0
    investment = sum(
        port_cost(instance, p) for p, t in openings.items() if t == year
    )
    investment += (fleet[year - 1] - previous) * settings["price_rub"]

    # 2. Начислить содержание независимо от доступности при отказе.
    operating = (
        sum(t <= year for t in openings.values())
        * settings["opex_per_year_rub"]
    )
    return investment + operating


def flight_seconds(instance: InstanceData, pair: tuple[str, str]) -> int:
    """Округлить длительность полёта вверх до целой секунды."""
    distance_km = instance["distance"][pair] / 10
    return math.ceil(
        distance_km / instance["settings"]["cruise_speed_kmh"] * 3600 - 1e-9
    )


def flight_cost(instance: InstanceData, pair: tuple[str, str]) -> float:
    """
    Посчитать затраты по географической длине без округления часов.
    """
    settings = instance["settings"]
    return (
        instance["distance"][pair]
        / 10
        / settings["cruise_speed_kmh"]
        * settings["cost_per_flight_hour_rub"]
    )


def plan_errors(instance: InstanceData, plan: Plan) -> list[str]:
    """Проверить стратегические решения до построения расписания."""
    # 1. Проверить ID, годы, целочисленность и монотонность флота.
    errors: list[str] = []
    if any(
        p not in instance["points"] or t not in (1, 2, 3)
        for p, t in plan["open_year"].items()
    ):
        errors.append("Недопустимый ID или год открытия")
    fleet = plan["fleet"]
    if len(fleet) != 3 or any(n < 0 or int(n) != n for n in fleet):
        return errors + ["Некорректный флот"]
    if fleet != sorted(fleet):
        errors.append("Закупленный флот не может уменьшаться")

    # 2. Проверить неизбежные расходы и будущие обязательства.
    cumulative = 0.0
    if not errors:
        for year in (1, 2, 3):
            cumulative += fixed_cost(instance, plan, year)
            if (
                cumulative
                > year * instance["settings"]["tranche_rub_per_year"] + 0.01
            ):
                errors.append(f"Недостаточно средств на ввод в году {year}")
    return errors


def lifetime_bases(instance: InstanceData, plan: Plan) -> Plan:
    """Назначить базу при покупке и сохранять её до конца горизонта."""
    # 1. Приобретаемые дроны получают базу среди введённых портов.
    repaired: Plan = {
        "open_year": dict(plan["open_year"]),
        "fleet": list(plan["fleet"]),
        "bases": [],
    }
    previous: list[str] = []
    for year in (1, 2, 3):
        opened = sorted(
            (p for p, t in plan["open_year"].items() if t <= year),
            key=lambda p: (-instance["points"][p]["population"], p),
        )
        requested = (
            plan["bases"][year - 1] if len(plan["bases"]) >= year else []
        )
        bases = list(previous)
        for drone in range(len(previous), plan["fleet"][year - 1]):
            preference = requested[drone] if drone < len(requested) else ""
            bases.append(
                preference
                if preference in opened
                else (opened[0] if opened else "")
            )
        repaired["bases"].append(bases)
        previous = bases
    return repaired


def write_json(path: Path, result: object) -> None:
    """Сохранить артефакт UTF-8, не заменяя замороженные файлы."""
    if path.resolve() in {(ROOT / p).resolve() for p in input_hashes()}:
        raise ValueError("Запись в замороженный файл запрещена")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def store_instance(path: Path, instance: InstanceData) -> None:
    """Сохранить настройки и матрицы экспериментального экземпляра."""
    write_json(
        path,
        {
            "label": instance["label"],
            "ids": instance["ids"],
            "points": instance["points"],
            "settings": instance["settings"],
            "hashes": instance["hashes"],
            "distance": [
                [a, b, value] for (a, b), value in instance["distance"].items()
            ],
            "demand": [
                [a, b, *values]
                for (a, b), values in instance["demand"].items()
            ],
        },
    )


def read_instance(path: Path) -> InstanceData:
    """Восстановить снимок и проверить замороженную основу."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["hashes"] != input_hashes():
        raise ValueError("Изменилась замороженная основа экземпляра")
    instance: InstanceData = {
        "label": raw["label"],
        "ids": raw["ids"],
        "points": raw["points"],
        "distance": {(a, b): int(value) for a, b, value in raw["distance"]},
        "demand": {
            (a, b): (int(q1), int(q2), int(q3))
            for a, b, q1, q2, q3 in raw["demand"]
        },
        "settings": raw["settings"],
        "hashes": raw["hashes"],
    }
    return instance

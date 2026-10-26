"""
Отчёт, записка, карты и таблицы из проверенных результатов модели v2.
"""

from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.text import Annotation, Text
from matplotlib.transforms import Bbox
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from scripts.evaluation import read_result, result_metrics
from scripts.model_data import (
    MODEL_VERSION,
    OUTPUT,
    RECOURSE_VERSION,
    ROOT,
    base_scenario,
    load_instance,
    port_cost,
    read_instance,
    write_json,
)
from scripts.model_types import InstanceData, OperatingResult
from scripts.studies import configuration_directory


def read_record(path: Path) -> dict[str, object]:
    """Прочитать объект отчётных метрик, отвергая другой тип JSON."""
    record: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError(f"Ожидается объект JSON: {path}")
    return record


def records(value: object) -> list[dict[str, object]]:
    """Проверить список записей на внешней границе отчёта."""
    if not isinstance(value, list) or any(
        not isinstance(v, dict) for v in value
    ):
        raise ValueError("Ожидается список объектов")
    return [{str(k): v for k, v in row.items()} for row in value]


def number(row: dict[str, object], key: str) -> float:
    """Прочитать числовую метрику без неявного преобразования object."""
    value = row[key]
    if not isinstance(value, (float, int, str)):
        raise ValueError(f"Метрика {key} не является числом")
    return float(value)


def formatted(value: float | int, digits: int = 0) -> str:
    """Оформить число с русскими разделителями."""
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    """Оформить таблицу источника Markdown с неизменными числами."""
    if not rows:
        return "Нет завершённых наблюдений; результаты не утверждаются."
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(str(v) for v in row) + " |" for row in rows)
    return "\n".join(lines)


def readable_table(table: str) -> str:
    """Заменить служебные метки таблицы понятными подписями."""
    # 1. Назвать методы и состояния решателя по-русски.
    labels = {
        "staged_greedy": "Поэтапный жадный",
        "joint_greedy": "Совместный жадный",
        "joint_sa": "Совместный отжиг",
        "Small MILP-derived": "Малая, MILP",
        "Small greedy": "Малая, жадный",
        "Small SA": "Малая, отжиг",
        "Main greedy": "Осн., жадный",
        "Main staged": "Осн., поэтапный",
        "Main SA": "Осн., отжиг",
        "Лучший SA": "Лучший отжиг",
        "Среднее SA": "Среднее отжига",
        "Greedy": "Жадный",
        "Прирост к greedy": "Прирост к жадному",
        "time_limit": "Лимит времени",
        "Incumbent": "Целое решение",
        "True": "Да",
        "| Seed |": "| Запуск |",
        ": base": ": обычные условия",
        ": low": ": спрос -30%",
        ": high": ": спрос +30%",
        ": range160": ": дальность 160 км",
        ": failure_": ": отказ ",
    }
    for source, replacement in labels.items():
        table = table.replace(source, replacement)

    # 2. Оформить годовые суммы без синтаксиса Python-списка.
    def annual_totals(match: re.Match[str]) -> str:
        """Записать три годовых значения с разделителями разрядов."""
        return " / ".join(formatted(int(v)) for v in match.groups())

    table = re.sub(r"\[(\d+), (\d+), (\d+)\]", annual_totals, table)

    # 3. Показать изменяемые параметры в привычных единицах.
    parameters = {
        "recharge_min": ("Зарядка", 1, "мин"),
        "opex_per_year_rub": ("Содержание порта", 1e6, "млн руб./год"),
        "price_rub": ("Цена дрона", 1e6, "млн руб."),
        "cruise_speed_kmh": ("Скорость", 1, "км/ч"),
        "cost_per_flight_hour_rub": ("Лётный час", 1000, "тыс. руб."),
        "capex_small_rub": ("Малый порт", 1e6, "млн руб."),
        "capex_hub_rub": ("Узловой порт", 1e6, "млн руб."),
        "takeoffs_per_hour": ("Взлёты", 1, "в час"),
        "charge_slots_small": ("Зарядные места малого порта", 1, ""),
        "charge_slots_hub": ("Зарядные места узлового порта", 1, ""),
    }

    def parameter_label(match: re.Match[str]) -> str:
        """Расшифровать одно имя и значение параметра."""
        label, divisor, unit = parameters[match[1]]
        value = formatted(float(match[2]) / divisor)
        return f"{label}: {value} {unit}".rstrip()

    pattern = r"\b(" + "|".join(parameters) + r")=([0-9.e+\-]+)"
    return re.sub(pattern, parameter_label, table)


def make_maps(
    instance: InstanceData, result: OperatingResult, directory: Path
) -> None:
    """
    Показать все кандидаты и только фактически выполняемые участки.
    """
    # 1. Сохранить общий географический масштаб всех четырёх карт.
    points = instance["points"]
    for year in (0, 1, 2, 3):
        fig, axis = plt.subplots(figsize=(9, 6.7))
        labels: list[Annotation] = []
        axis.scatter(
            [p["lon"] for p in points.values()],
            [p["lat"] for p in points.values()],
            s=18,
            color="0.75",
            label="Допустимые пункты",
        )
        if not year:
            for (a, b), distance in instance["distance"].items():
                if a < b and distance <= 2000:
                    axis.plot(
                        [points[a]["lon"], points[b]["lon"]],
                        [points[a]["lat"], points[b]["lat"]],
                        color="0.85",
                        linewidth=0.3,
                        zorder=0,
                    )
            axis.plot([], [], color="0.8", label="Потенциальный перелёт")
        if year:
            selected = {
                p for p, t in result["plan"]["open_year"].items() if t <= year
            }
            legs = {
                (f["origin"], f["destination"])
                for day in result["years"][year - 1]["days"]
                for f in day["flights"]
            }
            for a, b in sorted(legs):
                axis.plot(
                    [points[a]["lon"], points[b]["lon"]],
                    [points[a]["lat"], points[b]["lat"]],
                    color="0.55",
                    linewidth=0.8,
                    zorder=0,
                )
            axis.plot([], [], color="0.55", label="Выполняемый участок")
            axis.scatter(
                [points[p]["lon"] for p in sorted(selected)],
                [points[p]["lat"] for p in sorted(selected)],
                color="steelblue",
                s=45,
                label="Открытые порты",
            )
            for p in sorted(selected):
                labels.append(
                    axis.annotate(
                        points[p]["name"],
                        (points[p]["lon"], points[p]["lat"]),
                        fontsize=9,
                        xytext=(3, 3),
                        textcoords="offset points",
                        arrowprops={"arrowstyle": "-", "lw": 0.4},
                        bbox={
                            "facecolor": "white",
                            "edgecolor": "none",
                            "alpha": 0.8,
                            "pad": 0.15,
                        },
                    )
                )
        axis.set_title(
            "Допустимые пункты варианта А"
            if not year
            else (f"Фактическая сеть рейсов, год {year}")
        )
        axis.set_xlabel("Долгота, градусы")
        axis.set_ylabel("Широта, градусы")
        axis.grid(alpha=0.2)
        axis.legend(fontsize=8)
        fig.tight_layout()
        fig.canvas.draw()
        placed: list[Bbox] = []
        for label in labels:
            for step in range(30):
                label.set_position((3, 3 + step * 8))
                box = Text.get_window_extent(label).expanded(1.03, 1.15)
                if not any(box.overlaps(other) for other in placed):
                    break
            placed.append(box)
        fig.savefig(directory / f"map_{year}.png", dpi=160)
        plt.close(fig)


def export_tables(
    instance: InstanceData, result: OperatingResult, directory: Path
) -> None:
    """
    Сохранить открытия, расходы и все события без сокращения строк.
    """
    # 1. Оформить инвестиции и годовые метрики.
    rows: list[dict[str, object]] = []
    for point_id, opening_year in sorted(
        result["plan"]["open_year"].items(),
        key=lambda item: (item[1], item[0]),
    ):
        point = instance["points"][point_id]
        rows.append(
            {
                "wikidata_id": point_id,
                "name": point["name"],
                "region": point["region"],
                "opening_year": opening_year,
            }
        )
    save_csv(directory / "openings.csv", rows)
    save_csv(
        directory / "budget.csv",
        [
            {
                **{k: v for k, v in year.items() if k != "days"},
                "construction_rub": sum(
                    port_cost(instance, p)
                    for p, opening in result["plan"]["open_year"].items()
                    if opening == year["year"]
                ),
                "drone_purchases_rub": instance["settings"]["price_rub"]
                * (
                    year["fleet"]
                    - (
                        result["plan"]["fleet"][year["year"] - 2]
                        if year["year"] > 1
                        else 0
                    )
                ),
                "port_opex_rub": year["ports"]
                * instance["settings"]["opex_per_year_rub"],
                "remaining_funds_rub": year["year"]
                * instance["settings"]["tranche_rub_per_year"]
                - year["cumulative_cost_rub"],
            }
            for year in result["years"]
        ],
    )
    save_csv(
        directory / "fleet.csv",
        [
            {
                "drone": drone,
                "purchase_year": next(
                    year
                    for year in (1, 2, 3)
                    if drone < result["plan"]["fleet"][year - 1]
                ),
                "base_id": base,
                "base_name": instance["points"][base]["name"],
            }
            for drone, base in enumerate(result["plan"]["bases"][2])
        ],
    )

    # 2. Сохранить все типы суток, включая пустые перегоны и зарядки.
    flights: list[dict[str, object]] = []
    charges: list[dict[str, object]] = []
    journeys: list[dict[str, object]] = []
    for year in result["years"]:
        for template, day in enumerate(year["days"]):
            common: dict[str, object] = {
                "year": year["year"],
                "template": template,
                "weight": day["weight"],
            }
            loads: dict[str, int] = {}
            for journey in day["journeys"]:
                for flight_id in journey["flights"]:
                    loads[flight_id] = (
                        loads.get(flight_id, 0) + journey["count"]
                    )
                journeys.append(
                    {
                        **common,
                        **journey,
                        "flights": ";".join(journey["flights"]),
                    }
                )
            for flight in day["flights"]:
                flights.append(
                    {
                        **common,
                        **flight,
                        "passengers": loads.get(flight["id"], 0),
                        "empty": loads.get(flight["id"], 0) == 0,
                    }
                )
            charges.extend({**common, **charge} for charge in day["charges"])
    save_csv(directory / "flights.csv", flights)
    save_csv(directory / "charging.csv", charges)
    save_csv(directory / "passenger_itineraries.csv", journeys)


def clock_text(seconds: int) -> str:
    """Показать время события без округления до минуты."""
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def representative_schedule(
    instance: InstanceData, result: OperatingResult
) -> str:
    """Показать полный маршрут первого дрона в обычные сутки года 3."""
    # 1. Совместить полёты и зарядки в одном хронологическом порядке.
    day = result["years"][2]["days"][0]
    loads: dict[str, int] = {}
    for journey in day["journeys"]:
        for flight_id in journey["flights"]:
            loads[flight_id] = loads.get(flight_id, 0) + journey["count"]
    events: list[tuple[int, list[object]]] = []
    for flight in day["flights"]:
        if flight["drone"] == 0:
            events.append(
                (
                    flight["departure_s"],
                    [
                        clock_text(flight["departure_s"]),
                        clock_text(flight["arrival_s"]),
                        instance["points"][flight["origin"]]["name"],
                        instance["points"][flight["destination"]]["name"],
                        loads.get(flight["id"], 0),
                    ],
                )
            )
    for charge in day["charges"]:
        if charge["drone"] == 0:
            events.append(
                (
                    charge["start_s"],
                    [
                        clock_text(charge["start_s"]),
                        clock_text(charge["end_s"]),
                        instance["points"][charge["port"]]["name"],
                        "Зарядка",
                        "-",
                    ],
                )
            )

    # 2. Не обрезать события выбранного задания; другие дроны в CSV.
    return markdown_table(
        ["Начало", "Конец", "Откуда", "Куда / операция", "Пасс."],
        [row for _, row in sorted(events)],
    )


def save_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """Сохранить непустую таблицу UTF-8 с явным набором столбцов."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_charts(
    directory: Path,
    statistics: dict[str, object],
    experiments: dict[str, object],
) -> None:
    """Построить подписанные графики seed и бюджетного эксперимента."""
    # 1. Показать исходные наблюдения, не скрывая равенства со стартом.
    values = statistics["seed_objectives"]
    if not isinstance(values, list):
        raise ValueError("Нет списка seed")
    fig, axis = plt.subplots(figsize=(8, 4.5))
    axis.plot(
        range(len(values)), values, "o-", color="steelblue", label="Отжиг"
    )
    axis.axhline(
        number(statistics, "greedy"),
        linestyle="--",
        color="0.4",
        label="Жадный",
    )
    axis.set_xlabel("Запуск (seed)")
    axis.set_ylabel("Завершённые поездки за три года")
    axis.set_title("Разброс числа поездок на основном наборе")
    axis.legend()
    fig.tight_layout()
    fig.savefig(directory / "seeds.png", dpi=160)
    plt.close(fig)

    # 2. Отобразить фактические координаты бюджетной сетки.
    funding = records(experiments["funding"])
    fig, axis = plt.subplots(figsize=(8, 4.5))
    axis.plot(
        [number(r, "multiplier") for r in funding],
        [number(r, "trips") for r in funding],
        "o-",
        color="steelblue",
    )
    axis.set_xlabel("Множитель годового транша, базовый транш 1 млрд руб.")
    axis.set_ylabel("Завершённые поездки за три года")
    axis.set_title("Зависимость перевозок от финансирования")
    fig.tight_layout()
    fig.savefig(directory / "funding.png", dpi=160)
    plt.close(fig)


def export_pdf(markdown: Path, destination: Path) -> None:
    """Сверстать русский Markdown в PDF с встроенными шрифтами."""
    # 1. Встроить доступный кириллический шрифт и настроить стили A4.
    fonts = Path(matplotlib.get_data_path()) / "fonts/ttf"
    pdfmetrics.registerFont(TTFont("Report", str(fonts / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(
        TTFont("ReportBold", str(fonts / "DejaVuSans-Bold.ttf"))
    )
    pdfmetrics.registerFontFamily("Report", normal="Report", bold="ReportBold")
    normal = ParagraphStyle(
        "Russian", fontName="Report", fontSize=10, leading=14, spaceAfter=7
    )
    heading = ParagraphStyle(
        "HeadingRussian",
        parent=normal,
        fontName="ReportBold",
        fontSize=14,
        leading=18,
        spaceBefore=12,
        spaceAfter=8,
        keepWithNext=True,
    )
    title = ParagraphStyle(
        "TitleRussian",
        parent=heading,
        fontSize=20,
        leading=25,
        alignment=TA_CENTER,
        spaceAfter=18,
    )
    table_style = ParagraphStyle(
        "TableRussian", parent=normal, fontSize=8, leading=10, spaceAfter=0
    )
    caption = ParagraphStyle(
        "TableCaptionRussian", parent=normal, keepWithNext=True
    )
    story: list[Flowable] = []
    lines = markdown.read_text(encoding="utf-8").splitlines()
    index = 0

    def paragraph(value: str) -> str:
        """Экранировать текст и сохранить простое выделение."""
        escaped = html.escape(value).replace("`", "")
        escaped = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", escaped)
        return re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", escaped)

    # 2. Обработать заголовки, таблицы, изображения и обычные абзацы.
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        image = re.fullmatch(r"!\[([^]]*)\]\(([^)]+)\)", line)
        if image:
            path = markdown.parent / image[2]
            item = Image(str(path))
            ratio = min(480 / item.imageWidth, 350 / item.imageHeight)
            item.drawWidth = item.imageWidth * ratio
            item.drawHeight = item.imageHeight * ratio
            story.append(
                KeepTogether(
                    [
                        item,
                        Paragraph(paragraph(image[1]), table_style),
                        Spacer(1, 10),
                    ]
                )
            )
        elif line.startswith("|"):
            table_rows: list[list[str]] = []
            while index < len(lines) and lines[index].startswith("|"):
                cells = [
                    cell.strip() for cell in lines[index].strip("|").split("|")
                ]
                if not all(re.fullmatch(r"[-: ]+", cell) for cell in cells):
                    table_rows.append(cells)
                index += 1
            width = 480 / len(table_rows[0])
            table = Table(
                [
                    [Paragraph(paragraph(cell), table_style) for cell in row]
                    for row in table_rows
                ],
                colWidths=[width] * len(table_rows[0]),
                repeatRows=1,
            )
            table.setStyle(
                TableStyle(
                    [
                        (
                            "BACKGROUND",
                            (0, 0),
                            (-1, 0),
                            colors.HexColor("#e9edf0"),
                        ),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.extend([table, Spacer(1, 9)])
            continue
        elif line.startswith("# "):
            story.append(Paragraph(paragraph(line[2:]), title))
        elif line.startswith("## "):
            story.append(Paragraph(paragraph(line[3:]), heading))
        elif line.startswith("Таблица "):
            story.append(Paragraph(paragraph(line), caption))
        else:
            text = line
            while (
                index + 1 < len(lines)
                and lines[index + 1].strip()
                and not (lines[index + 1].startswith(("#", "|", "![", "- ")))
            ):
                index += 1
                text += " " + lines[index]
            story.append(Paragraph(paragraph(text), normal))
        index += 1

    # 3. Добавить номера страниц и сохранить готовый документ.
    def footer(canvas: Canvas, document: BaseDocTemplate) -> None:
        """Проставить номер страницы без изменения содержимого."""
        canvas.setFont("Report", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(
            58, 28, f"Сеть дронопортов России | модель {MODEL_VERSION}"
        )
        canvas.drawRightString(535, 28, str(document.page))

    destination.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(destination),
        pagesize=A4,
        leftMargin=57,
        rightMargin=57,
        topMargin=45,
        bottomMargin=45,
        title="Сеть дронопортов России",
        author="Индивидуальный учебный проект",
        invariant=1,
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def available_experiments() -> dict[str, object]:
    """Собрать снимок завершённых серий без запуска нового поиска."""
    # 1. Читать только совместимые конфигурации с итоговым расписанием.
    base = load_instance()
    scale: list[dict[str, object]] = []
    funding: list[dict[str, object]] = []
    quality: list[dict[str, object]] = []
    sensitivity: list[dict[str, object]] = []
    for path in sorted((OUTPUT / "configurations").glob("*/instance.json")):
        instance = read_instance(path)
        folder = path.parent
        if configuration_directory(instance) != folder:
            continue
        if not (folder / "published/best.json").exists():
            continue
        statistics = read_record(folder / "statistics.json")
        runs = records(statistics["seed_runs"])
        if not runs or any(not row.get("complete") for row in runs):
            continue
        best = read_result(
            folder / "published/best.json", instance, base_scenario()
        )
        ten_seeds = {int(number(row, "seed")) for row in runs} == set(
            range(10)
        )

        # 2. Разделять масштаб, финансирование и изменённые параметры.
        changes = {
            key: value
            for key, value in instance["settings"].items()
            if value != base["settings"][key]
        }
        label = instance["label"]
        if (
            ten_seeds
            and not changes
            and label in ("small", "size_25", "size_50", "main")
        ):
            statistics["n"] = len(instance["ids"])
            scale.append(statistics)
        if (
            ten_seeds
            and not changes
            and label in ("drop_missing", "type_median")
        ):
            statistics["strategy"] = label
            quality.append(statistics)
        if label != "main":
            continue
        if ten_seeds and set(changes) <= {"tranche_rub_per_year"}:
            funding.append(
                {
                    "multiplier": instance["settings"]["tranche_rub_per_year"]
                    / base["settings"]["tranche_rub_per_year"],
                    "trips": best["objective_trips"],
                    "cost_rub": best["cum_spend_rub"],
                }
            )
        elif changes and "tranche_rub_per_year" not in changes:
            sensitivity.append(
                {
                    "case": "; ".join(
                        f"{key}={value:g}" for key, value in changes.items()
                    ),
                    "changes": changes,
                    "trips": best["objective_trips"],
                    "cost_rub": best["cum_spend_rub"],
                    "statistics": statistics,
                }
            )

    # 3. Сохранить неполный снимок отдельно от полной серии.
    result: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "input_hashes": base["hashes"],
        "complete": False,
        "scale": sorted(scale, key=lambda row: number(row, "n")),
        "funding": sorted(funding, key=lambda row: number(row, "multiplier")),
        "data_quality": quality,
        "sensitivity": sensitivity,
        "note": (
            "Снимок завершённых вычислительных экспериментов. "
            "Неполные серии не используются как завершённые эксперименты. "
            "Не завершены полная бюджетная сетка с уточнениями, сравнение "
            "стратегий пропусков населения и вся чувствительность."
        ),
    }
    write_json(OUTPUT / "experiments_available.json", result)
    return result


def build_report(available_only: bool = False) -> None:
    """
    Собрать восемь элементов сдачи только из существующих расчётов.
    """
    # 1. Прочитать все обязательные артефакты и проверить главные
    # расписания.
    instance = load_instance()
    directory = configuration_directory(instance)
    best = read_result(
        directory / "published/best.json", instance, base_scenario()
    )
    greedy = read_result(
        directory / "published/greedy.json", instance, base_scenario()
    )
    statistics = read_record(directory / "statistics.json")
    interval = statistics["mean_gain_bootstrap_95"]
    if not isinstance(interval, list) or len(interval) != 2:
        raise ValueError("Ожидаются две границы bootstrap-интервала")
    interval_text = "; ".join(formatted(float(v), 1) for v in interval)
    seed_records = records(statistics["seed_runs"])
    if {int(number(row, "seed")) for row in seed_records} != set(range(10)):
        raise ValueError("Для отчёта нужна полная серия seed 0-9")
    if any(not row.get("complete") for row in seed_records):
        raise ValueError("Серия SA не завершена")
    certificate = read_record(OUTPUT / "small_bound/certificate.json")
    attempts = records(certificate["attempts"])
    if not available_only and (
        not certificate.get("complete")
        or (
            not certificate.get("adaptive")
            and attempts[-1]["termination"] != "optimal_relaxation"
        )
    ):
        raise ValueError("Не завершён согласованный поиск границы small")
    experiments = (
        available_experiments()
        if available_only
        else read_record(OUTPUT / "experiments.json")
    )
    routing = read_record(OUTPUT / "routing.json")
    planning = read_record(OUTPUT / "planning_comparison.json")
    robust = read_record(OUTPUT / "robustness.json")
    game_result = read_record(OUTPUT / "game.json")
    if any(
        record.get("recourse_version") != RECOURSE_VERSION
        for record in (robust, game_result)
    ):
        raise ValueError("Требуется обновить ответы на отказы")
    audit = read_record(ROOT / "output/repro-check/audit.json")
    with (ROOT / "data/metadata/population_observations.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        observations = list(csv.DictReader(source))
    dates = Counter(row["status"] for row in observations)
    if {row["wikidata_id"] for row in observations} != set(instance["ids"]):
        raise ValueError("Метаданные населения относятся к другим пунктам")
    raw_audit = audit["candidate_audit"]
    if not isinstance(raw_audit, dict):
        raise ValueError("Нет журнала обработки кандидатов")
    candidate_audit = {str(key): value for key, value in raw_audit.items()}
    for artifact in (
        certificate,
        experiments,
        routing,
        planning,
        robust,
        game_result,
    ):
        if artifact.get("model_version") != MODEL_VERSION:
            raise ValueError("Отчёт требует завершённых расчётов модели 2.1")
        if artifact.get("input_hashes") != instance["hashes"]:
            raise ValueError("Не совпадают входы отчётного артефакта")
    figures = OUTPUT / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    make_maps(instance, best, figures)
    make_charts(figures, statistics, experiments)
    export_tables(instance, best, OUTPUT / "plan")
    total_demand = sum(sum(q) for q in instance["demand"].values())
    first = [
        instance["points"][p]["name"]
        for p, t in best["plan"]["open_year"].items()
        if t == 1
    ]
    failures = [
        row
        for row in records(robust["base_scenarios"])
        if str(row["scenario"]).startswith("failure_")
    ]
    damaging = min(failures, key=lambda row: number(row, "trips"))
    damaging_id = str(damaging["scenario"]).removeprefix("failure_")
    damaging_name = instance["points"][damaging_id]["name"]

    # 2. Описать постановку, допущения, модель и проверенные результаты.
    sections = [
        (
            "# Сеть дронопортов России\n\nИсследовательский проект по "
            "методам оптимизации. Вариант А. Проверяемая модель 2.1."
        ),
        (
            "## 1. Цель и индивидуальный вариант\n\nМаксимизируется "
            "число завершённых пассажирских поездок за три года. "
            "Территория: Псковская, Новгородская и Тверская области;"
            " первые буквы Ю, Я, А, Б, В. Допустимы пересадки; одна "
            "поездка учитывается один раз при прибытии. Повторные "
            "поездки одного человека считаются разными. Все порты, "
            "включая пересадочные, подчиняются фильтру варианта."
        ),
        (
            "## 2. Данные и воспроизводимость\n\nОсновной набор "
            "содержит 75 пунктов, малый - 12. Оба графа до 200 км "
            "связны, изолированных пунктов нет. Отбор выполняется по"
            " квоте 5 пунктов на регион и букву, по убыванию "
            "населения; малая задача - top-12. Исходный снимок "
            "Wikidata от 12.10.2026 и provenance сохранены. "
            "Замороженные файлы не изменяются; воспроизведение идёт "
            "в output/repro-check. Идентичность пунктов сохраняется "
            "по Wikidata ID, а не по названию."
        ),
        (
            "Население исходного запроса является максимумом "
            "опубликованных значений, а не обязательно последним "
            "наблюдением. Совпадающие даты утверждений сохранены "
            "отдельно в data/metadata; недоступные и неоднозначные "
            "годы явно отмечены. Это ограничивает содержательную "
            "точность синтетического спроса. Дубли, координаты, "
            "пропуски и распределения проверяются скриптом "
            "data_audit; исходный журнал проверки содержит значения "
            "и хеши."
        ),
        f"Годы населения: однозначно совпали {dates['matched']} значений; "
        f"неоднозначны {dates['ambiguous']}; недоступны "
        f"{dates['unavailable']}. Из "
        f"{formatted(number(candidate_audit, 'raw_rows'))} объектов "
        f"сохранены {formatted(number(candidate_audit, 'settlement_rows'))} "
        f"кандидатов типов НП; у "
        f"{formatted(number(candidate_audit, 'missing_population'))} "
        "нет населения. IQR-выбросы "
        "не удаляются: крупные города важны для доступности. В main "
        "Юрино встречается три раза, Юрьево и Южный по два; это "
        "разные ID, а не дубли строк. Спрос направленный и асимметричный.",
        (
            "## 3. Спрос и учебные параметры\n\nНаблюдаемых "
            "пассажиропотоков нет. До оптимизации зафиксирован "
            "синтетический спрос с исходящим объёмом 0,5 поездки на "
            "жителя и ростом 1 / 1,15 / 1,32. Вес назначения "
            "proportional P_destination * s(d)/(d+10)^2, "
            "s(d)=d^2/(d^2+20^2); веса нормируются по отправителю. "
            "Годовой итог строки округляется с распределением "
            "наибольших остатков. Это условная оценка, не "
            "проверенный прогноз реальных перевозок."
        ),
        (
            "Паспорт v1: 10 мест; 120 км/ч; обслуживание 5 мин; "
            "зарядка 25 мин; 200 км между зарядками; дрон 250 млн "
            "руб.; порт 60/120 млн руб.; содержание 10 млн руб./год;"
            " 6 взлётов/час; 2/4 зарядных места; 07:00-21:00; 365 "
            "суток; 50 тыс. руб./лётный час. Кроме вместимости, "
            "дальности и бюджета, значения являются учебными "
            "допущениями. Стоимость порта включает зарядную "
            "инфраструктуру; часовая эксплуатационная ставка "
            "агрегирует энергию, персонал и обслуживание дронов. "
            "Постоянное содержание порта начисляется отдельно. "
            "Выручка не увеличивает доступный бюджет."
        ),
        (
            "## 4. Математическая модель\n\nx_i,t - действующий порт; "
            "N_t - закупленный флот; n_a,t и e_a,t - пассажирские и "
            "порожние рейсы; f_a,t^od - поток по участку; S_od,t - "
            "завершённые поездки; u_od,t=Q_od,t-S_od,t - "
            "неудовлетворённый спрос. Основная цель: max F=sum_t "
            "sum_od S_od,t. При равенстве F выбираются меньшие "
            "затраты."
        ),
        (
            "Ограничения: x_i,t<=x_i,t+1; N_t<=N_t+1; "
            "0<=S_od,t<=Q_od,t; сохранение потоков в пересадочных "
            "узлах; суммарный поток участка не более 10*n_a,t. Рейс "
            "соединяет только открытые порты; остаток дальности "
            "уменьшается на фактическое расстояние каждого участка, "
            "зарядка восстанавливает полный запас. Положение дрона, "
            "порядок обслуживания, зарядки и полётов проверяются во "
            "времени. В начале и конце суток каждый дрон находится в"
            " своей базе и полностью заряжен. Суточные графики "
            "движений и зарядок не превышают мощности."
        ),
        (
            "Для фиксированного расписания z_k,p - целое число "
            "пассажиров на временном пути p шаблона k, w_k - число "
            "таких суток. Для каждого рейса r: sum_(p содержит r) "
            "z_k,p <= 10. Для каждого OD: S_od,t = sum_k w_k * "
            "sum_(p относится к OD) z_k,p <= Q_od,t. Путь начинается "
            "в o, заканчивается в d и связывает только рейсы с "
            "departure_next >= arrival_previous + turnaround. "
            "Поездка с двумя рейсами даёт две занятые секции, но "
            "только одну единицу S."
        ),
        (
            "Для дрона запас B измеряется в десятых долях км: "
            "B_after = B_before - distance_units >= 0; после зарядки "
            "B = range_units. Следующая операция не начинается раньше "
            "окончания предыдущего полёта и обслуживания либо зарядки. "
            "Одновременно в порту заряжается не более 2/4 дронов. "
            "В любом полуоткрытом интервале [s,s+3600) не более шести "
            "взлётов и отдельно не более шести посадок. Физическое "
            "положение дрона непрерывно, конечная база равна начальной."
        ),
        (
            "Бюджет: sum_(y<=t) C_y <= t*1 млрд руб. CAPEX "
            "начисляется при вводе в начале года; покупки - при "
            "расширении флота; OPEX - каждый год работы. Рейсовые "
            "затраты включают все перелёты, включая пустые. Остаток "
            "переносится, заимствования запрещены. Расходы "
            "рассчитываются без округления промежуточных часов; "
            "денежная проверка имеет точность до копейки."
        ),
        (
            "C_t = sum_i capex_i*(x_i,t-x_i,t-1) + "
            "price_drone*(N_t-N_t-1) + opex_port*sum_i x_i,t + "
            "sum_k w_k*sum_(рейсы r) cost_flight(r), где "
            "cost_flight = distance_km/speed_kmh * cost_per_hour. "
            "Для первого года x_i,0=N_0=0. Постоянные ценовые "
            "параметры v1 задают единую учебную модель без инфляции; "
            "скрытых доходов, кредитов или возврата затрат нет."
        ),
        (
            "Год представлен семью суточными шаблонами с весом 52 и "
            "отдельным днём с весом 1. Сумма весов 365. "
            "Инфраструктура, флот и базирование общие внутри года; "
            "годовые OD-ограничения связывают все шаблоны. "
            "База назначается при покупке и сохраняется на все три "
            "года: межгодовых бесплатных перебазирований нет. При "
            "отказе базы её дроны остаются на земле. Это отдельное "
            "консервативное решение проекта. Повторяемость полного "
            "заряда и дислокации является "
            "консервативным допущением проекта. Лимит посадок принят"
            " равным лимиту взлётов."
        ),
        (
            "## 5. Алгоритмы и сертификат малой задачи\n\nGreedy "
            "проверяет перспективные пары, затем открытия, закупки и"
            " совместные инвестиции по приросту поездок на "
            "дополнительные расходы. Скрининг оставляет 12 "
            "перспективных движений на шаг; глобально лучшим жадный "
            "шаг не объявляется. Расписание строится круговыми "
            "заданиями по спросу с проверкой возврата, остатка "
            "батареи, часов и зарядной ёмкости. Пассажиры "
            "распределяются целочисленной моделью HiGHS на "
            "допустимых временных путях; пространственные циклы "
            "заменяются ожиданием."
        ),
        (
            "SA меняет годы открытия, состав сети, покупки и "
            "базирование. Старт выбирается из проверенных greedy и "
            "staged; начальная температура равна 1% стартовой цели, "
            "охлаждение 0,995, нижний порог температуры равен 0,001 "
            "начальной температуры. Каждый seed останавливается после "
            "трёх партий по 800 предложений без улучшения. Критерий "
            "ранжирования одинаков для всех методов. Все "
            "опубликованные решения независимо проверены по "
            "сохранённым событиям."
        ),
        (
            "После поиска ко всем номинальным методам применяется одна "
            "очистка: объект без рейсов, зарядок и базирования удаляется "
            "из первоначального инвестиционного решения. Старое "
            "расписание сохраняется как допустимая альтернатива новой "
            "постройке графика; выбирается лучшее проверенное решение. "
            "Обработанные старты также сохраняются в портфеле SA. "
            "Исходный ход поиска остаётся в корне конфигурации, итоговые "
            "планы - в published/. При стрессах вложения уже фиксированы: "
            "такая очистка не означает возврат затрат после отказа. "
            "Для робастной сети резервные порты могут сохраняться."
        ),
        f"Малая задача: допустимая нижняя "
        f"оценка {formatted(number(certificate, 'lower_bound'))}; "
        f"верхняя оценка {formatted(number(certificate, 'upper_bound'), 2)}; "
        f"разрыв {formatted(number(certificate, 'gap_percent'), 2)}%. "
        "Разрыв определён как 100*(U-L)/U. Upper bound получается из "
        "MILP-релаксации CBC, а не из неподтверждённог"
        "о расписанием incumbent. "
        "Логи и все переменные сохранены. Достигнутый лимит времени не "
        "является доказательством оптимальности.",
        (
            "Корректность верхней оценки: любой фактический план "
            "удовлетворяет годовым балансам рейсов, ограничениям "
            "мест, расходов и фонда времени. При полном начальном и "
            "конечном заряде общий путь не превышает дальность, "
            "умноженную на число зарядок. Снятие порядка событий "
            "увеличивает множество решений. Поэтому оптимум LP или "
            "корректная граница MILP не ниже оптимума расписания. "
            "Зарядка после каждого участка в релаксации не "
            "навязывается."
        ),
        "## 6. Проверенные перевозки и инвестиции\n\n"
        + markdown_table(
            [
                "Метод",
                "Поездки за 3 года",
                "Доля спроса",
                "Затраты, млрд руб.",
            ],
            [
                [
                    "Greedy",
                    formatted(greedy["objective_trips"]),
                    formatted(
                        100 * greedy["objective_trips"] / total_demand, 2
                    )
                    + "%",
                    formatted(greedy["cum_spend_rub"] / 1e9, 3),
                ],
                [
                    "Лучший совместный план",
                    formatted(best["objective_trips"]),
                    formatted(100 * best["objective_trips"] / total_demand, 2)
                    + "%",
                    formatted(best["cum_spend_rub"] / 1e9, 3),
                ],
            ],
        ),
        markdown_table(
            [
                "Год",
                "Поездки",
                "Порты",
                "Флот",
                "Расход, млн руб.",
                "Накоплено, млн руб.",
            ],
            [
                [
                    r["year"],
                    formatted(r["trips"]),
                    r["ports"],
                    r["fleet"],
                    formatted(r["cost_rub"] / 1e6, 2),
                    formatted(r["cumulative_cost_rub"] / 1e6, 2),
                ]
                for r in best["years"]
            ],
        ),
        (
            "Расписания всех трёх лет и восьми типов суток сохранены"
            " вместе с маршрутами пассажиров. CSV-таблицы содержат "
            "ID, каждое движение, пассажиров и порожние рейсы. Флаг "
            "valid подтверждён отдельным восстановлением, а не "
            "суточным балансом. Исторические 233 884 / 237 442 "
            "относятся к старому оценщику и не заменяют эти "
            "результаты."
        ),
        (
            "![Все допустимые "
            "пункты](../../data/results/model_v2/figures/map_0.png)"
        ),
        *[
            f"![Фактическая сеть, год {year}]"
            f"(../../data/results/model_v2/figures/map_{year}.png)"
            for year in (1, 2, 3)
        ],
        "## 7. Маршруты и поэтапное планирование\n\n"
        f"Сравнено {formatted(number(routing, 'pairs'))} направленных пар. "
        "A* проверен относительно Dijkstra для времени и стоимости. "
        "BFS минимизирует число участков; A* - аддитивное время либо "
        "стоимость. Сравниваются одинаковые одиночные запросы и пакет всех "
        "целей. Географическая эвристика учитывает максимальную ошибку "
        "округления расстояний. Ожидание рейсов в стат"
        "ическую метрику не входит. Статический граф содержит "
        "потенциальные участки между открытыми портами, а не только "
        "выполняемые рейсы. Его путь ещё не доказывает наличие "
        "доступного места и подходящего времени отправления.",
        "Поэтапный greedy максимизирует текущий год, сохраняя введённые "
        "объекты и обязательства. Совместный поиск использует весь горизонт "
        "и включает поэтапный план среди стартов. Измеренная разность "
        f"равна {formatted(number(planning, 'myopia_difference_heuristics'))} "
        "поездок; это результат эвристик, не доказанная цена поэтапности.",
        markdown_table(
            ["Подход", "Поездки по годам 1/2/3", "Всего"],
            [
                [name, record["per_year"], formatted(number(record, "trips"))]
                for name in ("staged_greedy", "joint_greedy", "joint_sa")
                for record in records([planning[name]])
            ],
        ),
        (
            "## 8. Устойчивость и игра\n\nПроверены база, спрос ±30%, "
            "дальность 160 км и отказ каждого допустимого порта на "
            "весь третий год. Инфраструктура и купленный флот "
            "фиксированы; CAPEX не возвращается, постоянное "
            "содержание сохраняется полностью. Меняются только "
            "допустимые перевозки и расписания."
        ),
        f"Худший результат базы: {formatted(number(robust, 'base_worst'))}; "
        f"робастного плана: {formatted(number(robust, 'robust_worst'))}. "
        f"Цена в базовом сценарии: "
        f"{formatted(number(robust, 'base_case_loss'))} "
        f"поездок; выигрыш в худшем: "
        f"{formatted(number(robust, 'worst_case_gain'))}. "
        "Робастный max-min улучшается локальным поиском до отсутствия "
        "улучшающих движений; глобальный max-min оптимум не утверждается.",
        f"Значение игры на small: {formatted(number(game_result, 'value'))}. "
        f"Полный перебор: {formatted(number(game_result, 'full_leaves'))} "
        f"листьев; "
        f"alpha-beta: {formatted(number(game_result, 'alpha_beta_leaves'))}. "
        "Значения совпали. Матрица ответных перевозок рассчитывается "
        "один раз; её стоимость отделена от времени обхода дерева. "
        "Листья используют проверенную эвристическую перестройку перевозок.",
        (
            "## 9. Вычислительные эксперименты\n\nМасштаб: 12/25/50/75"
            " пунктов с фиксированным правилом top-population; спрос"
            " ограничен сохранёнными OD, поэтому меняется не только "
            "вычислительный размер. Для каждой основной конфигурации"
            " в опубликованной таблице выполнено десять seed. "
            "План бюджетного эксперимента: от ×0,5 до ×2 с шагом "
            "0,25 и два уточнения интервала максимального прироста "
            "на рубль. Таблица содержит завершённые случаи. "
            "Структурный порог не объявляется "
            "только по абсолютной разности соседних точек."
        ),
        markdown_table(
            ["Пунктов", "Greedy", "Лучший SA", "Среднее SA"],
            [
                [
                    r["n"],
                    formatted(number(r, "greedy")),
                    formatted(number(r, "best")),
                    formatted(number(r, "mean"), 1),
                ]
                for r in records(experiments["scale"])
            ],
        ),
        (
            "Стратегии качества данных: исключение неизвестного "
            "населения из кандидатов и заполнение медианой группы "
            "типов. Это отдельные экспериментальные наборы, не "
            "подмена базового v1. Сохранены выбранные ID и сети. "
            "Дизайн чувствительности включает ±20% цен, расходов, "
            "скорости и зарядки, соседние мощности движений и "
            "зарядных мест; ниже показаны завершённые случаи. Это "
            "чувствительность учебных допущений, не проверка "
            "точности прогноза."
        ),
        f"Для main среднее SA: {formatted(number(statistics, 'mean'), 1)}, "
        f"стандартное отклонение: {formatted(number(statistics, 'std'), 1)}. "
        "Представлены парные результаты и 95%-интервал bootstrap для "
        "среднего прироста при seed=0. Интервал условен на данном экземпляре. "
        "Знаковый тест против собственного сохраняемого старта и ANOVA "
        "не применяются: такой дизайн не доказывает общее превосходство SA.",
        (
            "![Распределение результатов по "
            "seed](../../data/results/model_v2/figures/seeds.png)"
        ),
        (
            "![Бюджетная "
            "кривая](../../data/results/model_v2/figures/funding.png)"
        ),
        (
            "## 10. Ограничения, запуск и использование ИИ\n\n"
            "Конструирование круговых заданий и скрининг "
            "стратегических движений ограничивают качество "
            "найденного решения. Все опубликованные перевозки "
            "выполнимы, но основная задача не решена до доказанного "
            "глобального оптимума. Необслуженный и недостижимый "
            "спрос сохранён в знаменателе. Низкий объём не "
            "исправляется изменением территории."
        ),
        (
            "Воспроизведение: uv sync --locked; затем uv run "
            "--locked python scripts/run_project.py all. Проверки: "
            "Ruff, форматирование, mypy и unittest. Возобновление "
            "разрешено только при совпадении версии модели, входов и"
            " настроек. Изменение параметра на защите выполняется "
            "командой parameter-change; исходный паспорт не "
            "меняется."
        ),
        (
            "Генеративный ИИ (Codex) использован для анализа "
            "задания, реализации, документации и проверок. "
            "Результаты проверяются автоматическими инвариантами и "
            "просмотром артефактов. Автор проекта должен "
            "самостоятельно объяснить модель и выводы. Новые "
            "dev-зависимости Ruff/mypy и stubs нужны для качества "
            "кода; reportlab/pypdf - для формирования и проверки "
            "PDF. Расчёты используют исходные NumPy/SciPy, pandas, "
            "NetworkX и PuLP/CBC."
        ),
        "Источники: исходное задание, с. 3-11; Wikidata SPARQL и сохранённые "
        "provenance; паспорт parameters.json v1; исходные data/raw; "
        "сертификаты, расписания и журналы модели v2. Полные таблицы "
        "и метрики находятся в data/results/model_v2. Хеши и проверка "
        f"воспроизведения: {bool(audit['reproduction_passed'])}.",
    ]
    # 3. Привести подробные числа, необходимые для проверки выводов.
    small = load_instance("small")
    small_directory = configuration_directory(small) / "published"
    metric_rows: list[list[object]] = []
    metric_records: list[dict[str, object]] = []
    for label, problem, path in (
        ("Main greedy", instance, directory / "published/greedy.json"),
        ("Main staged", instance, directory / "published/staged.json"),
        ("Main SA", instance, directory / "published/best.json"),
        ("Small greedy", small, small_directory / "greedy.json"),
        ("Small SA", small, small_directory / "best.json"),
        ("Small MILP-derived", small, OUTPUT / "small_milp_plan.json"),
    ):
        solution = read_result(path, problem, base_scenario())
        metrics = result_metrics(
            problem,
            solution,
            number(certificate, "upper_bound")
            if label.startswith("Small")
            else None,
        )
        metric_records.append({"method": label, **metrics})
        metric_rows.append(
            [
                label,
                formatted(solution["objective_trips"]),
                formatted(number(metrics, "served_share") * 100, 2),
                formatted(solution["cum_spend_rub"] / 1e9, 3),
                "/".join(str(v) for v in solution["plan"]["fleet"]),
                "/".join(str(y["ports"]) for y in solution["years"]),
                formatted(number(metrics, "empty_flights")),
                formatted(number(metrics, "gap_percent"), 2),
            ]
        )
    write_json(OUTPUT / "algorithm_metrics.json", metric_records)
    route_times, route_expansions = routing["timings"], routing["expansions"]
    if not isinstance(route_times, dict) or not isinstance(
        route_expansions, dict
    ):
        raise ValueError("Нет измерений поиска маршрутов")
    sections.extend(
        [
            "## 11. Подробные таблицы и операционное задание",
            markdown_table(
                [
                    "Метод",
                    "Поездки",
                    "% спроса",
                    "Млрд руб.",
                    "Флот 1/2/3",
                    "Порты 1/2/3",
                    "Пустые рейсы",
                    "Разрыв, %",
                ],
                metric_rows,
            ),
            "Для small разрыв относится к границе CBC. Для main "
            "использована лишь грубая верхняя оценка суммарным спросом; "
            "она математически корректна, но не является сильной "
            "оценкой качества сети или сертификатом близости к оптимуму.",
            "Главная серия SA (время включает поиск и построение расписаний; "
            "расчёт стартов greedy/staged приведён отдельно):",
            markdown_table(
                ["Seed", "Поездки", "Прирост к greedy", "Время, с"],
                [
                    [
                        r["seed"],
                        formatted(number(r, "published_trips")),
                        formatted(
                            number(r, "published_trips")
                            - greedy["objective_trips"]
                        ),
                        formatted(number(r, "wall_s"), 2),
                    ]
                    for r in records(statistics["seed_runs"])
                ],
            ),
            f"Greedy: {formatted(number(statistics, 'greedy_wall_s'), 2)} с; "
            f"staged: {formatted(number(statistics, 'staged_wall_s'), 2)} с. "
            f"Медиана SA {formatted(number(statistics, 'median'))}; "
            f"диапазон {formatted(number(statistics, 'min'))} - "
            f"{formatted(number(statistics, 'max'))}. "
            f"95%-интервал среднего прироста: "
            f"[{interval_text}]. "
            "Время seed при параллельном запуске - wall time, а не CPU; "
            "его нельзя складывать для оценки времени всей серии.",
            f"Общая обработка сохранённых кандидатов заняла "
            f"{formatted(number(statistics, 'postprocessing_wall_s'), 2)} с. "
            "В таблице seed приведены итоги после одинаковой очистки; "
            "исходные поисковые значения и полное состояние RNG "
            "сохранены отдельно.",
            "Попытки CBC; статус Solution Found означает наличие "
            "целого incumbent без доказанного оптимума:",
            markdown_table(
                ["Лимит, с", "Время, с", "Завершение", "Incumbent", "U"],
                [
                    [
                        r["limit_s"],
                        formatted(number(r, "wall_s"), 1),
                        r["termination"],
                        r["has_integer_incumbent"],
                        formatted(number(r, "upper_bound"), 2),
                    ]
                    for r in records(certificate["attempts"])
                ],
            ),
            "Представительное полное задание: дрон 0, год 3, первый "
            "шаблон с весом 52. Между прилётом и следующей операцией "
            "выдерживается обслуживание. Остальные дроны и шаблоны "
            "доступны в flights.csv / charging.csv.",
            representative_schedule(instance, best),
            "Поиск маршрутов: одиночные запросы и все назначения. "
            "A* запускается по каждой цели; его общая стоимость "
            "сопоставляется с BFS, который для пакета обходит граф "
            "один раз на отправителя. Стоимость и время - два отдельных "
            "критерия, а не разные графы.",
            markdown_table(
                ["Работа", "Время, с", "Раскрытия"],
                [
                    [
                        name,
                        formatted(float(route_times[key]), 6),
                        route_expansions[expanded],
                    ]
                    for name, key, expanded in (
                        ("BFS запросы", "bfs_single_s", "bfs_single"),
                        ("BFS все цели", "bfs_batch_s", "bfs_batch"),
                        (
                            "A* время, все запросы",
                            "astar_time_s",
                            "astar_time",
                        ),
                        (
                            "A* стоимость, все запросы",
                            "astar_cost_s",
                            "astar_cost",
                        ),
                    )
                ],
            ),
            "Основные сценарии базы и робастной альтернативы. Для "
            "отказов показан наиболее неблагоприятный, все допустимые "
            "ID и расписания находятся в stress/.",
            markdown_table(
                ["План / сценарий", "Поездки", "Годы 1/2/3", "Млрд руб."],
                [
                    [
                        f"{label}: {r['scenario']}",
                        formatted(number(r, "trips")),
                        str(r["per_year"]),
                        formatted(number(r, "cost_rub") / 1e9, 3),
                    ]
                    for label, key, worst_key in (
                        ("База", "base_scenarios", "worst_base_scenario"),
                        (
                            "Робастный",
                            "robust_scenarios",
                            "worst_robust_scenario",
                        ),
                    )
                    for r in records(robust[key])
                    if r["scenario"]
                    in ("base", "low", "high", "range160", robust[worst_key])
                ],
            ),
            f"Изменение затрат робастного плана: "
            f"{formatted(number(robust, 'cost_change_rub') / 1e6, 2)} "
            "млн руб. Цена устойчивости может быть отрицательной, "
            "если робастный поиск улучшил и базовый результат; "
            "эвристические значения не доказывают точную границу Парето.",
            markdown_table(
                ["Обход игры", "Узлы", "Листья", "Время, с"],
                [
                    [
                        "Перебор",
                        game_result["full_nodes"],
                        game_result["full_leaves"],
                        formatted(number(game_result, "full_s"), 8),
                    ],
                    [
                        "Alpha-beta",
                        game_result["alpha_beta_nodes"],
                        game_result["alpha_beta_leaves"],
                        formatted(number(game_result, "alpha_beta_s"), 8),
                    ],
                ],
            ),
            f"Подготовка payoff-матрицы: "
            f"{formatted(number(game_result, 'preprocessing_s'), 3)} с. "
            "Эти расходы одинаковы и не включены во время обходов.",
            "Бюджетная кривая (цели эвристические, не точная функция "
            "оптимума; уточнение выбирается по приросту на рубль):",
            markdown_table(
                ["Множитель", "Поездки", "Затраты, млрд руб."],
                [
                    [
                        formatted(number(r, "multiplier"), 4),
                        formatted(number(r, "trips")),
                        formatted(number(r, "cost_rub") / 1e9, 3),
                    ]
                    for r in records(experiments["funding"])
                ],
            ),
            "Качество данных: завершённые стратегии сопровождаются "
            "выбранными ID, сетями и экспериментальными входами.",
            markdown_table(
                ["Стратегия", "Greedy", "Лучший SA", "Среднее", "SD"],
                [
                    [
                        r["strategy"],
                        formatted(number(r, "greedy")),
                        formatted(number(r, "best")),
                        formatted(number(r, "mean"), 1),
                        formatted(number(r, "std"), 1),
                    ]
                    for r in records(experiments["data_quality"])
                ],
            ),
            "Дополнительная чувствительность: один seed 0 на случай; "
            "она не заменяет десяти seed основных экспериментов.",
            markdown_table(
                ["Случай", "Поездки", "Затраты, млрд руб."],
                [
                    [
                        r["case"],
                        formatted(number(r, "trips")),
                        formatted(number(r, "cost_rub") / 1e9, 3),
                    ]
                    for r in records(experiments["sensitivity"])
                ],
            ),
        ]
    )
    # 4. Соединить редактируемое изложение с расчётными таблицами.
    table_headers = {
        "main_methods": "| Метод | Поездки за 3 года |",
        "annual_plan": "| Год |",
        "planning": "| Подход |",
        "scale": "| Пунктов |",
        "methods_detail": "| Метод | Поездки |",
        "seeds": "| Seed |",
        "cbc": "| Лимит, с |",
        "schedule": "| Начало |",
        "routing": "| Работа |",
        "stress": "| План / сценарий |",
        "game": "| Обход игры |",
        "funding": "| Множитель |",
        "sensitivity": "| Случай |",
    }
    report_values: dict[str, str] = {}
    blocks = [block for part in sections for block in part.split("\n\n")]
    for name, prefix in table_headers.items():
        tables = [block for block in blocks if block.startswith(prefix)]
        if len(tables) != 1:
            raise ValueError(f"Неоднозначная таблица отчёта: {name}")
        report_values[name] = readable_table(tables[0])
    report_values.update(
        {
            "main_trips": formatted(best["objective_trips"]),
            "main_cost": formatted(best["cum_spend_rub"] / 1e9, 3),
            "served_share": formatted(
                100 * best["objective_trips"] / total_demand, 2
            ),
            "main_ports": str(best["years"][-1]["ports"]),
            "main_fleet": str(best["years"][-1]["fleet"]),
            "main_gain": formatted(
                best["objective_trips"] - greedy["objective_trips"]
            ),
            "remaining_funds": formatted(
                (
                    3 * instance["settings"]["tranche_rub_per_year"]
                    - best["cum_spend_rub"]
                )
                / 1e6,
                2,
            ),
            "first_ports": ", ".join(first),
            "small_lower": formatted(number(certificate, "lower_bound")),
            "small_upper": formatted(number(certificate, "upper_bound"), 2),
            "small_gap": formatted(number(certificate, "gap_percent"), 2),
            "planning_gain": formatted(
                number(planning, "myopia_difference_heuristics")
            ),
            "damaging_port": damaging_name,
            "base_worst": formatted(number(robust, "base_worst")),
            "robust_worst": formatted(number(robust, "robust_worst")),
            "base_loss": formatted(number(robust, "base_case_loss")),
            "robust_extra_cost": formatted(
                number(robust, "cost_change_rub") / 1e6, 2
            ),
            "game_value": formatted(number(game_result, "value")),
            "game_preprocessing": formatted(
                number(game_result, "preprocessing_s"), 3
            ),
            "seed_mean": formatted(number(statistics, "mean"), 1),
            "seed_std": formatted(number(statistics, "std"), 1),
            "bootstrap_interval": f"[{interval_text}]",
            "experiment_scope": (
                "Экспериментальная серия пока неполна. Получены "
                "результаты для "
                f"{len(records(experiments['funding']))} из девяти "
                "бюджетных точек и "
                f"{len(records(experiments['sensitivity']))} из 16 "
                "случаев чувствительности. Сравнение исключения пропусков "
                "населения с заполнением по медиане типа ещё не "
                "завершено. Поэтому по имеющимся точкам нельзя определить "
                "наиболее выгодный интервал всей бюджетной сетки или "
                "сделать вывод об эффекте обработки пропусков."
                if available_only
                else "Бюджет исследован на сетке от ×0,5 до ×2 с шагом "
                "0,25 и двумя уточнениями интервала наибольшего прироста "
                "на рубль. Экспериментальные данные и сети сохранены "
                "отдельно от исходного набора."
            ),
            "bound_status": (
                "Поиск остановлен до выполнения критерия сходимости; "
                "оптимальность не доказана."
                if not certificate.get("complete")
                else "Серия расчётов завершена; сохраняющийся разрыв "
                "показывает предел доказанной оценки качества расписания."
            ),
        }
    )
    narrative = (ROOT / "docs/REPORT_TEMPLATE.md").read_text("utf-8")
    for name, value in report_values.items():
        narrative = narrative.replace("{{" + name + "}}", value)
    if re.search(r"\{\{[^}]+\}\}", narrative):
        raise ValueError("В тексте отчёта остались незаполненные поля")
    report = ROOT / "output/reports/report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(narrative, encoding="utf-8")

    # 5. Подготовить отдельную короткую управленческую записку.
    memo_text = (
        (
            "# Управленческая записка\n\nСеть дронопортов: вариант А, "
            "модель 2.1.\n\n## Что строить сначала\n\nВ первом году: "
        )
        + ", ".join(first)
        + ". "
        "Последующие открытия и закупки приведены в op"
        "enings.csv и budget.csv; "
        "все ID сохранены. Рекомендация относится к уч"
        "ебным ценам и спросу.\n\n"
        "## Ожидаемая перевозка и средства\n\n"
        f"Проверенный трёхлетний объём: {formatted(best['objective_trips'])} "
        f"поездок ("
        f"{formatted(100 * best['objective_trips'] / total_demand, 2)}% "
        f"спроса). Затраты {formatted(best['cum_spend_rub'] / 1e9, 3)} "
        f"млрд руб. "
        "Расписания, пассажиры, зарядки и бюджет прове"
        "рены для каждого года.\n\n"
        "## Когда план перестаёт работать\n\nПри сокращении дальности или "
        "закрытии узла часть маршрутов требует перестройки. "
        f"Самый опасный отказ порта: {damaging_name} ({damaging_id}), "
        f"при нём остаётся {formatted(number(damaging, 'trips'))} поездок "
        "за горизонт. Дроны с этой базой не могут быть бесплатно "
        "перенесены; остальные продолжают работу. "
        f"Худшая проверенная перевозка базового плана: "
        f"{formatted(number(robust, 'base_worst'))}. "
        f"Робастная альтернатива повышает её до "
        f"{formatted(number(robust, 'robust_worst'))}, теряя в базе "
        f"{formatted(number(robust, 'base_case_loss'))} поездок. "
        "Отказы рассматриваются на весь третий год без возврата вложений.\n\n"
        "## Решение и оговорки\n\nИспользовать базовый план для максимума "
        "найденных перевозок либо робастный для защиты худшего сценария. "
        "Результаты основаны на синтетическом спросе; перед реальными "
        "инвестициями требуются наблюдения спроса и проверенные цены. "
        "Глобальная оптимальность не доказана.\n"
    )
    if available_only:
        memo_text += (
            "\n## Ограничения рекомендации\n\nРасписания подтверждают "
            "выполнимость плана в принятых условиях. Полная бюджетная "
            "сетка и анализ качества данных ещё не завершены, поэтому "
            "рекомендация относится к исследованным вариантам.\n"
        )
    memo = report.with_name("management_memo.md")
    memo.write_text(memo_text, encoding="utf-8")
    export_pdf(report, ROOT / "output/pdf/report.pdf")
    export_pdf(memo, ROOT / "output/pdf/management_memo.pdf")
    write_json(
        OUTPUT / "submission_manifest.json",
        {
            "report": str(report.relative_to(ROOT)),
            "memo": str(memo.relative_to(ROOT)),
            "input_hashes": instance["hashes"],
            "objective_trips": best["objective_trips"],
            "validated": best["valid"],
            "schedule_templates": 24,
            "report_mode": "available_results" if available_only else "full",
            "all_experiments_completed": not available_only,
            "bound_search_converged": bool(certificate.get("complete")),
            "source": str((directory / "published").relative_to(ROOT)),
            "deliverables": {
                "report": "output/pdf/report.pdf",
                "data": "data/raw + data/processed + data/metadata",
                "code": "scripts + tests + pyproject.toml + uv.lock",
                "maps": "data/results/model_v2/figures/map_0..3.png",
                "investment_plan": "data/results/model_v2/plan/",
                "schedules": str(
                    directory.relative_to(ROOT) / "published/best.json"
                ),
                "comparisons": "data/results/model_v2/*.json",
                "memo": "output/pdf/management_memo.pdf",
            },
        },
    )

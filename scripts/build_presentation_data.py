"""Подготовить фактические данные и анимацию расписания для защиты."""

import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

from scripts.evaluation import read_result
from scripts.model_data import OUTPUT, ROOT, base_scenario, load_instance
from scripts.model_types import Day, InstanceData, OperatingResult
from scripts.studies import configuration_directory


def plot_networks(
    instance: InstanceData, result: OperatingResult, folder: Path
) -> None:
    """Показать фактические участки в общем увеличенном масштабе."""
    # 1. Развести подписи тесной группы возле Великого Новгорода.
    offsets = {
        "Великий Новгород": (-65, -42),
        "Волховец": (-50, 35),
        "Божонка": (35, 18),
        "Бронница": (35, -25),
    }
    points = instance["points"]

    # 2. Сохранить три карты в едином масштабе по реальным рейсам.
    for year in result["years"]:
        fig, axis = plt.subplots(figsize=(9, 5.5), dpi=150)
        axis.scatter(
            [point["lon"] for point in points.values()],
            [point["lat"] for point in points.values()],
            s=15,
            color="#ced7de",
        )
        legs = {
            (flight["origin"], flight["destination"])
            for day in year["days"]
            for flight in day["flights"]
        }
        for origin, destination in sorted(legs):
            a, b = points[origin], points[destination]
            axis.plot(
                [a["lon"], b["lon"]],
                [a["lat"], b["lat"]],
                color="#007f83",
                linewidth=1.6,
                alpha=0.75,
                zorder=2,
            )
        for identity, opened in result["plan"]["open_year"].items():
            if opened > year["year"]:
                continue
            point = points[identity]
            axis.scatter([point["lon"]], [point["lat"]], s=55, color="#007f83")
            axis.annotate(
                point["name"],
                (point["lon"], point["lat"]),
                xytext=offsets.get(point["name"], (7, 8)),
                textcoords="offset points",
                fontsize=12,
                color="#102940",
                arrowprops={"arrowstyle": "-", "color": "#52687d"},
            )
        axis.set_xlim(29.8, 35.7)
        axis.set_ylim(57.3, 59.15)
        axis.set_xlabel("Долгота, градусы", fontsize=12)
        axis.set_ylabel("Широта, градусы", fontsize=12)
        axis.set_title("Открытые порты и фактические участки", fontsize=15)
        axis.grid(alpha=0.15)
        fig.tight_layout()
        fig.savefig(folder / f"network_{year['year']}.png")
        plt.close(fig)


def animate_day(instance: InstanceData, day: Day, folder: Path) -> None:
    """Показать положения дронов по сохранённым временам событий."""
    # 1. Построить географический график фактических участков.
    points = instance["points"]
    fig, axis = plt.subplots(figsize=(11, 6.3), dpi=95)
    fig.patch.set_facecolor("#f4f7fa")
    axis.set_facecolor("#f4f7fa")
    axis.scatter(
        [p["lon"] for p in points.values()],
        [p["lat"] for p in points.values()],
        s=8,
        color="#b5c1ce",
    )
    for a, b in sorted(
        {(f["origin"], f["destination"]) for f in day["flights"]}
    ):
        axis.plot(
            [points[a]["lon"], points[b]["lon"]],
            [points[a]["lat"], points[b]["lat"]],
            color="#97aaba",
            linewidth=0.7,
        )
    for base in sorted(set(day["bases"])):
        p = points[base]
        axis.annotate(
            p["name"],
            (p["lon"], p["lat"]),
            xytext=(5, 8),
            textcoords="offset points",
            fontsize=10,
        )
    axis.set_xlim(29.5, 35.8)
    axis.set_ylim(56.5, 59.2)
    axis.set_xlabel("Долгота, градусы")
    axis.set_ylabel("Широта, градусы")
    colors = ["#007f83", "#ee7b35", "#6938a3"]
    markers = [
        axis.plot([], [], "o", color=c, markersize=11)[0] for c in colors
    ]
    title = axis.set_title("", loc="left", fontsize=16, pad=18)
    fig.subplots_adjust(bottom=0.22, top=0.87, left=0.08, right=0.98)
    labels = [
        fig.text(0.08 + i * 0.3, 0.08, "", fontsize=11, color=c)
        for i, c in enumerate(colors)
    ]
    fig.text(
        0.08,
        0.025,
        "Третий год. Пример дневного расписания. Время ускорено.",
        fontsize=9,
        color="#52687d",
    )
    frames: list[Image.Image] = []

    # 2. Интерполировать координаты между вылетом и прибытием.
    for clock in np.linspace(7 * 3600, 21 * 3600, 150):
        title.set_text(
            f"Работа трёх дронов    {int(clock // 3600):02}:"
            f"{int(clock // 60 % 60):02}"
        )
        for drone, marker in enumerate(markers):
            base_point = points[day["bases"][drone]]
            lon, lat = base_point["lon"], base_point["lat"]
            battery = 200.0
            state = "на земле"
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
            for start, kind, index in sorted(events, key=lambda item: item[0]):
                if clock < start:
                    break
                if kind == "charge":
                    charge = day["charges"][index]
                    if clock >= charge["end_s"]:
                        battery = 200.0
                    else:
                        state = "зарядка"
                        break
                else:
                    flight = day["flights"][index]
                    origin, target = flight["origin"], flight["destination"]
                    fraction = min(
                        1.0, (clock - start) / (flight["arrival_s"] - start)
                    )
                    point_a, point_b = points[origin], points[target]
                    lon = point_a["lon"] + fraction * (
                        point_b["lon"] - point_a["lon"]
                    )
                    lat = point_a["lat"] + fraction * (
                        point_b["lat"] - point_a["lat"]
                    )
                    battery -= (
                        instance["distance"][origin, target] / 10 * fraction
                    )
                    if fraction < 1:
                        state = "в полёте"
                        break
            marker.set_data([lon], [lat])
            labels[drone].set_text(
                f"Дрон {drone + 1}: {state}\nЗапас хода: {battery:.0f} км"
            )
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=95)
        frames.append(Image.open(buffer).convert("RGB"))
    # 3. Сохранить GIF и статический кадр для PDF.
    frames[0].save(
        folder / "schedule.gif",
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
    )
    frames[55].save(folder / "schedule.png")
    plt.close(fig)


def main() -> None:
    """Собрать только существующие результаты, без нового поиска."""
    # 1. Прочитать проверенный основной план и фактические метрики.
    folder = ROOT / "output/presentation/assets"
    folder.mkdir(parents=True, exist_ok=True)
    instance = load_instance()
    directory = configuration_directory(instance)
    result = read_result(
        directory / "published/best.json", instance, base_scenario()
    )
    names = [
        "experiments_available",
        "experiments_bounded",
        "planning_comparison",
        "robustness",
        "game",
        "routing",
        "verification",
    ]
    data = {
        name: json.loads((OUTPUT / f"{name}.json").read_text("utf-8"))
        for name in names
    }
    data["main"] = result
    data["statistics"] = json.loads(
        (directory / "statistics.json").read_text("utf-8")
    )
    data["small_certificate"] = json.loads(
        (OUTPUT / "small_bound/certificate.json").read_text("utf-8")
    )
    data["points"] = instance["points"]
    # 2. Экспортировать данные и визуализацию суточного задания.
    (folder / "data.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    animate_day(instance, result["years"][2]["days"][0], folder)
    plot_networks(instance, result, folder)


if __name__ == "__main__":
    main()

# Комплект документов: сеть пассажирских дронопортов

Подготовлено 26.10.2026 по имеющимся проверенным результатам модели 2.1.
Все числа относятся к замороженным входам v1. Полный первоначальный
экспериментальный план ещё не завершён; ограничения указаны в отчёте.

## Документы

- [Отчёт, PDF, 16 страниц](pdf/report.pdf)
- [Управленческая записка, PDF, 1 страница](pdf/management_memo.pdf)
- [Исходник отчёта, Markdown](reports/report.md)
- [Исходник записки, Markdown](reports/management_memo.md)

## Карты и инвестиционный план

- [Все допустимые пункты](../data/results/model_v2/figures/map_0.png)
- [Фактическая сеть первого года](../data/results/model_v2/figures/map_1.png)
- [Фактическая сеть второго года](../data/results/model_v2/figures/map_2.png)
- [Фактическая сеть третьего года](../data/results/model_v2/figures/map_3.png)
- [Годовой и накопленный бюджет](../data/results/model_v2/plan/budget.csv)
- [Открытия портов](../data/results/model_v2/plan/openings.csv)
- [Покупки и постоянные базы дронов](../data/results/model_v2/plan/fleet.csv)

## Все 24 шаблона трёх лет

Время в CSV задано в секундах от полуночи; `template` нумеруется 0–7.
Веса шаблонов — 52 для 0–6 и 1 для 7. Дроны нумеруются с нуля.
Рейс и маршрут пассажира идентифицируются внутри своего года и шаблона.

- [Рейсы, включая порожние](../data/results/model_v2/plan/flights.csv)
- [Зарядки](../data/results/model_v2/plan/charging.csv)
- [Пассажирские маршруты](../data/results/model_v2/plan/passenger_itineraries.csv)

## Свидетельства и воспроизведение

- [Сертификат малой задачи](../data/results/model_v2/small_bound/certificate.json)
- [Снимок завершённых экспериментов](../data/results/model_v2/experiments_available.json)
- [Автоматическая проверка](../data/results/model_v2/verification.json)
- [Протокол визуальной проверки](../data/results/model_v2/pdf_visual_review.json)
- [Команды запуска и правила данных](../README.md)

Сохранены L=216335, U=242700,108, разрыв 10,8632%. Уточнение границы
остановлено досрочно при переходе к подготовке документов. Сходимость
серии не утверждается. Основной план: 250647 поездок за три года,
2843711083,33 руб.; результат подтверждён расписаниями.

Завершены масштаб 12/25/50/75 и десять seed основной задачи. Бюджетная
кривая содержит ×0,5/×0,75/×1; чувствительность — 9 из 16 случаев.
Сравнение стратегий пропусков населения не завершено. Неполные серии
не выданы за полные. Дополнительные расчёты сейчас не запущены.

Повторная сборка без оптимизации:

```powershell
uv run --locked python scripts/run_project.py report --available-results
uv run --locked python scripts/run_project.py check-submission --available-results
```

// Академическая защита: содержание, доказательства и заметки докладчика.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';

// 1. Прочитать готовые результаты без повторного поиска решений.
const root = process.cwd();
const runtimeRoot = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies');
const modules = process.env.RUNTIME_NODE_MODULES || path.join(runtimeRoot, 'node/node_modules');
process.env.RUNTIME_NODE_MODULES = modules;
const skill = path.join(os.homedir(), '.codex/plugins/cache/openai-primary-runtime/presentations/26.1015.12040/skills/presentations');
const {Presentation, PresentationFile} = await import(pathToFileURL(path.join(modules, '@oai/artifact-tool/dist/artifact_tool.mjs')));
const {applyPresentationChartFont, finalizePresentation} = await import(pathToFileURL(path.join(skill, 'container_tools/artifact_tool_utils.mjs')));
const out = path.resolve(root, process.argv[3] || 'output/presentation/final20');
const build = path.join(root, 'tmp/presentation/academic');
const revision = process.argv[2] || new Date().toISOString().replace(/\D/g, '');
await fs.mkdir(build, {recursive: true});
const content = JSON.parse(await fs.readFile(path.join(out, 'content.json'), 'utf8'));
const data = JSON.parse(await fs.readFile(path.join(root, 'output/presentation/assets/data.json'), 'utf8'));
const years = data.main.years;
const font = 'Arial';
const color = {ink: '#102940', teal: '#007f83', orange: '#df7131', gray: '#52687d', light: '#e7edf1', white: '#ffffff'};
const deck = Presentation.create({slideSize: {width: 1280, height: 720}});
const chartOwners = [];
const tableOwners = [];
const number = (value, digits = 0) => Number(value).toLocaleString('ru-RU', {minimumFractionDigits: digits, maximumFractionDigits: digits});

// 2. Определить небольшие функции оформления без иерархии компонентов.
function text(slide, value, left, top, width, height, size = 31, fill = color.ink, bold = false) {
    // Создать текстовую область, затем явно задать шрифт и отступы.
    const shape = slide.shapes.add({geometry: 'textbox', position: {left, top, width, height}, fill: 'none', line: {fill: 'none', width: 0}});
    shape.text = String(value);
    shape.text.style = {typeface: font, fontSize: size, color: fill, bold, insets: 0, verticalAlignment: 'top'};
    return shape;
}

function lines(slide, values, left = 70, top = 205, width = 1120, size = 32, gap = 91) {
    values.forEach((value, index) => text(slide, value, left, top + gap * index, width, gap - 9, size));
}

function aside(slide, value, explanation) {
    text(slide, value, 915, 220, 300, 110, 53, color.teal, true);
    text(slide, explanation, 915, 355, 300, 240, 28, color.gray);
}

async function picture(slide, file, frame = {left: 55, top: 178, width: 840, height: 440}, alt = 'Расчётная сеть') {
    const bytes = await fs.readFile(path.join(root, file));
    const contentType = file.endsWith('.gif') ? 'image/gif' : 'image/png';
    return slide.images.add({blob: bytes, contentType, fit: 'contain', alt, position: frame});
}

function table(slide, values, frame = {left: 65, top: 205, width: 1150, height: 330}, size = 28) {
    // Создать редактируемую таблицу и оформить заголовок отдельно.
    const result = slide.tables.add({rows: values.length, columns: values[0].length, values, ...frame});
    for (let row = 0; row < values.length; row++) {
        for (let column = 0; column < values[0].length; column++) {
            const cell = result.getCell(row, column);
            cell.fill = row === 0 ? color.ink : color.white;
            cell.text.style = {typeface: font, fontSize: size, bold: row === 0, color: row === 0 ? color.white : color.ink};
        }
    }
    // Зарегистрировать слайд для независимой проверки редактируемости.
    tableOwners.push(deck.slides.items.length);
    return result;
}

function chart(slide, type, categories, series, options = {}) {
    // Построить график из полных численных рядов сохранённых расчётов.
    const result = slide.charts.add(type, {
        position: {left: 65, top: 195, width: 810, height: 415},
        categories,
        series: series.map((item, index) => {
            const {color: seriesColor, ...values} = item;
            const fill = seriesColor || [color.teal, color.orange, color.ink][index];
            return {...values, smooth: false, fill, line: {fill, width: 3, style: 'solid'}};
        }),
        chartFill: color.white, plotAreaFill: color.white,
        hasLegend: series.length > 1,
        legend: {position: 'bottom', textStyle: {fontSize: 23, fill: color.ink}},
        barOptions: {direction: 'column', grouping: 'clustered', gapWidth: 80},
        xAxis: {textStyle: {fontSize: 23, fill: color.ink}},
        yAxis: {min: 0, numberFormatCode: '0', textStyle: {fontSize: 23, fill: color.gray}, majorGridlines: {fill: color.light, width: 1}},
        dataLabels: {showValue: false, position: 'outEnd', textStyle: {fontSize: 22, fill: color.ink}},
        ...options
    });
    // Применить шрифт ко всем осям и сохранить владельца для проверки.
    applyPresentationChartFont(result, {fontFamily: font});
    chartOwners.push(deck.slides.items.length);
    return result;
}

function annotation(slide, value, left = 150, top = 207, width = 650) {
    text(slide, value, left, top, width, 47, 27, color.teal, true);
}

function addSlide(item) {
    // Создать общий каркас и навигацию, сохранив особый титульный слайд.
    const slide = deck.slides.add();
    slide.background.fill = color.white;
    if (item.layout !== 'cover') {
        text(slide, item.chapter, 65, 24, 1090, 28, 18, color.gray);
        text(slide, item.title, 65, 65, 1150, 115, 39, color.ink, true);
    }
    text(slide, String(item.number).padStart(2, '0'), 1190, 664, 45, 27, 18, color.gray);
    // Связать речь и источники с тем же слайдом, который увидит аудитория.
    slide.speakerNotes.text = `${item.backup ? 'Резерв для вопросов' : `Плановое время: ${item.seconds} с`}\n\n${item.notes}\n\nИсточники: ${item.source}`;
    return slide;
}

// 3. Разместить доказательства по структуре доклада и резервных вопросов.
for (const item of content.slides) {
    const slide = addSlide(item);
    switch (item.layout) {
    case 'cover':
        text(slide, content.course, 70, 52, 1140, 50, 30, color.gray);
        text(slide, 'Планирование сети\nпассажирских дронопортов', 70, 130, 1140, 150, 55, color.ink, true);
        text(slide, 'Псковская, Новгородская и Тверская области', 72, 315, 1130, 60, 29, color.gray);
        text(slide, `Докладчик: ${content.presenter}`, 72, 425, 1130, 65, 30);
        text(slide, `Преподаватель: ${content.teacher}, ${content.teacher_degree}`, 72, 510, 1130, 95, 29);
        text(slide, 'Исследовательский проект 2026', 72, 648, 1050, 35, 22, color.gray);
        break;
    case 'question':
        text(slide, 'Максимум завершённых поездок за три года', 70, 205, 1140, 95, 43, color.teal, true);
        lines(slide, ['1 млрд руб. в год. Остаток переносится', '10 мест. До 200 км между зарядками', 'Результат должен подтверждаться расписанием'], 75, 345, 1120, 33, 90);
        break;
    case 'territory':
        await picture(slide, 'data/results/model_v2/figures/map_0.png', undefined, 'Все 75 допустимых пунктов и потенциальные связи до 200 км');
        aside(slide, '75 пунктов', 'Буквы: Ю, Я, А, Б, В\n\nДля проверки качества:\n12 пунктов');
        text(slide, 'Координаты и население: Wikidata. Спрос рассчитан для учебной задачи.', 75, 620, 1120, 38, 24, color.gray);
        break;
    case 'demand':
        chart(slide, 'bar', ['Год 1', 'Год 2', 'Год 3'], [
            {name: 'Расчётный спрос', values: [267450, 307571, 353032], color: '#b1bdc7'},
            {name: 'Завершённые поездки', values: years.map(year => year.trips), color: color.teal}
        ]);
        aside(slide, '27,01%', 'Доля спроса, которую удалось обслужить\n\nРасчёт по учебной формуле');
        break;
    case 'model':
        lines(slide, ['Открываем порты и покупаем дроны в начале года', 'Учитываем спрос и десять мест на каждом рейсе', 'Проверяем остаток заряда, обслуживание и пересадки', 'Начало и конец дня: своя база и полная батарея'], 75, 210, 1120, 32, 100);
        break;
    case 'methods':
        table(slide, [['Что делаем', 'Как выбираем'], ['Начальный план', 'Больше поездок на вложенный рубль'], ['Поиск улучшений', 'Меняем порты, годы открытия и покупки'], ['Проверка', 'Строим расписание и считаем поездки']], {left: 65, top: 205, width: 1150, height: 310});
        text(slide, 'Поиск повторён 10 раз с разными случайными решениями.', 72, 555, 1120, 45, 29, color.teal, true);
        text(slide, 'Остановка: три серии по 800 попыток без улучшения.', 72, 610, 1120, 40, 27);
        break;
    case 'comparison':
        chart(slide, 'bar', ['Жадный план', 'План по годам', 'Улучшенный план'], [{name: 'Поездки за три года', values: [224259, 228367, 250647]}], {dataLabels: {showValue: true, position: 'outEnd', textStyle: {fontSize: 23, fill: color.ink}}});
        annotation(slide, '+26 388 поездок к жадному плану', 240, 193, 610);
        aside(slide, '+11,77%', 'Имитация отжига:\nпоиск изменений плана\n\nВсе планы проверены\nпо одинаковым правилам');
        text(slide, 'Жадный метод выбирает вложение с наибольшим приростом поездок на рубль.', 75, 620, 1120, 37, 23, color.gray);
        break;
    case 'bound':
        text(slide, '216 335', 75, 220, 520, 110, 72, color.teal, true);
        text(slide, 'План с готовым расписанием', 80, 340, 530, 75, 31);
        text(slide, '242 700', 690, 220, 510, 110, 72, color.ink, true);
        text(slide, 'Верхняя оценка числа поездок', 695, 340, 510, 75, 31);
        text(slide, '10,86%', 75, 455, 430, 100, 72, color.orange, true);
        text(slide, 'Разница относительно\nверхней оценки', 550, 465, 640, 90, 32);
        text(slide, 'Точный максимум пока не найден. Расчёт верхней оценки остановлен по времени.', 75, 607, 1130, 46, 24, color.gray);
        break;
    case 'year1':
    case 'year2':
    case 'year3': {
        const year = Number(item.layout.slice(-1));
        const result = years[year - 1];
        await picture(slide, `output/presentation/assets/network_${year}.png`, undefined, `Фактические рейсы и открытые порты года ${year}`);
        aside(slide, `${result.ports} портов`, `${result.fleet} ${year === 1 ? 'дрон' : 'дрона'}\n\n${number(result.trips)} поездок за год`);
        if (year === 3) text(slide, 'Расходы за три года: 2,844 млрд руб. из 3 млрд доступных.', 75, 615, 1120, 40, 27, color.teal, true);
        break;
    }
    case 'budget':
        table(slide, [['Год', 'Поездки', 'Дроны', 'Расход¹', 'Остаток¹'], ...years.map(year => [String(year.year), number(year.trips), String(year.fleet), number(year.cost_rub / 1e6, 1), number((year.year * 1e9 - year.cumulative_cost_rub) / 1e6, 1)])], {left: 65, top: 210, width: 1150, height: 290});
        text(slide, '2,844 млрд руб.', 75, 535, 620, 80, 53, color.teal, true);
        text(slide, 'Общие затраты за три года', 750, 555, 450, 55, 29);
        text(slide, '¹ Млн руб. Остаток денег с начала проекта. Все расходы включены.', 75, 619, 1120, 35, 24, color.gray);
        break;
    case 'schedule':
        await picture(slide, content.schedule_gif || 'output/presentation/assets/schedule.gif', {left: 70, top: 175, width: 1125, height: 360}, 'Ускоренное воспроизведение дневного расписания трёх дронов');
        text(slide, 'За показанный день: 52 рейса, 23 — без пассажиров.', 75, 550, 1120, 40, 28, color.teal, true);
        text(slide, 'В году 8 дневных расписаний: 7 повторяются по 52 раза, ещё 1 — один раз.\nЗа три года проверены все 24 расписания.', 75, 595, 1120, 62, 24);
        break;
    case 'routing':
        table(slide, [['Метод', 'Что ищет', 'Время¹'], ['Поиск в ширину (BFS)', 'Меньше перелётов', '0,499 мс'], ['Поиск A*', 'Меньше времени в пути', '3,295 мс']], {left: 65, top: 220, width: 1150, height: 250}, 28);
        text(slide, 'A* проверен точным поиском Дейкстры. Ответы совпали.', 75, 515, 1120, 65, 29, color.teal, true);
        text(slide, '¹ Время работы программы для 110 запросов, а не длительность полётов.', 75, 615, 1120, 42, 24, color.gray);
        break;
    case 'robust':
        chart(slide, 'bar', ['Без изменений', 'Худшие условия'], [{name: 'Обычный план', values: [250647, 170960]}, {name: 'С учётом сбоев', values: [234608, 185739]}]);
        annotation(slide, 'Худший случай: 170 960 → 185 739', 240, 197, 620);
        aside(slide, '+14 779', 'Без сбоев: −16 039\nпоездок\n\nДоплата за три года:\n151,97 млн руб.');
        text(slide, 'Проверки: спрос ±30%, дальность 160 км, отказ каждого порта в году 3.', 75, 621, 1120, 36, 23, color.gray);
        break;
    case 'game':
        text(slide, '12 пунктов: выбираем сеть, закрываем порт, перестраиваем рейсы.', 75, 190, 1120, 45, 27);
        table(slide, [['Метод', 'Проверено результатов', 'Худший результат¹'], ['Полный перебор', '19', '151 450'], ['Альфа-бета-отсечение', '9', '151 450']], {left: 65, top: 245, width: 1150, height: 230}, 27);
        text(slide, 'Если новая сеть уже хуже найденной,\nостальные отказы для неё можно не проверять.', 75, 505, 1120, 80, 30, color.teal, true);
        text(slide, '¹ Завершённые поездки за три года после закрытия порта.', 75, 613, 1120, 45, 26, color.gray);
        break;
    case 'game-detail':
        table(slide, [['Способ', 'Узлы дерева', 'Конечные исходы', 'Поездки'], ['Полный перебор', '23', '19', '151 450'], ['С отсечением', '13', '9', '151 450']], {left: 65, top: 210, width: 1150, height: 245}, 27);
        text(slide, 'Узел — промежуточный выбор. Исход — результат после отказа.', 75, 500, 1120, 70, 29, color.teal, true);
        text(slide, 'Подготовка ответов: 7,615 с. Перебор готовых ответов: 5,6 и 3,9 мкс.', 75, 605, 1120, 48, 25);
        break;
    case 'experiments':
        table(slide, [['Что проверяли', 'Что получили'], ['Размер задачи', 'Проверены 12, 25, 50 и 75 пунктов'], ['Бюджет', '2 млрд руб./год: 431 915 поездок'], ['Пропуски населения', 'Два способа дали ту же сеть'], ['Повторный поиск', '10 запусков: 241 339–250 647 поездок']], {left: 65, top: 200, width: 1150, height: 360}, 27);
        text(slide, 'Числа показывают найденные решения. Точные максимумы не установлены.', 75, 602, 1120, 52, 27, color.gray);
        break;
    case 'scale': {
        const scales = data.experiments_available.scale;
        table(slide, [['Пунктов', 'Лучший из 10', 'В среднем', 'Среднее время, с'], ...scales.map(row => [String(row.n), number(row.best), number(row.mean, 1), number(row.seed_runs.reduce((sum, run) => sum + run.wall_s, 0) / row.seed_runs.length, 1)])], {left: 65, top: 200, width: 1150, height: 325}, 27);
        text(slide, 'Сравниваем число поездок за три года в десяти запусках.', 75, 555, 1120, 60, 28, color.teal, true);
        text(slide, '75 пунктов: от 241 339 до 250 647 завершённых поездок.', 75, 615, 1120, 42, 26, color.gray);
        break;
    }
    case 'funding': {
        const funding = [...data.experiments_bounded.funding].sort((a, b) => a.multiplier - b.multiplier);
        chart(slide, 'scatter', [], [{name: 'Поездки', xValues: funding.map(row => row.multiplier), values: funding.map(row => row.trips), marker: {symbol: 'circle', size: 6}, points: [{idx: 8, fill: color.orange}]}], {scatterOptions: {style: 'lineWithMarkers'}, xAxis: {min: 0.5, max: 2, majorUnit: 0.25, numberFormatCode: '0.00', title: 'Бюджет, млрд руб. в год', textStyle: {fontSize: 23, fill: color.ink}}});
        annotation(slide, '1,9375–2 млрд руб.: +77 034 поездки', 240, 205, 620);
        aside(slide, '431 915', 'Поездок при 2 млрд руб. в год\n\n≈411 поездок на дополнительный млн руб.');
        text(slide, 'График показывает найденные планы. Максимум для каждого бюджета пока неизвестен.', 75, 623, 1120, 35, 22, color.gray);
        break;
    }
    case 'quality':
        table(slide, [['Проверка', 'Результат'], ['Нет населения: два способа обработки', 'Одинаковые пункты, спрос и сеть'], ['Изменение параметров', '16 проверенных планов'], ['Скорость 144 км/ч', '270 484 поездки'], ['Зарядка 30 минут', '178 920 поездок']], {left: 65, top: 205, width: 1150, height: 340}, 27);
        text(slide, 'В этой задаче оба способа обработки дали ту же сеть.', 75, 577, 1120, 45, 30, color.teal, true);
        text(slide, 'Дополнительные случаи проверены коротким поиском, без десяти повторов.', 75, 625, 1120, 35, 22, color.gray);
        break;
    case 'reproduce':
        lines(slide, ['Данные, параметры, код и журналы входят в комплект', 'Ruff, форматирование, mypy и 30 тестов проходят', 'На защите меняется спрос, бюджет, дальность или порт'], 75, 205, 1120, 31, 89);
        text(slide, 'uv run --locked python scripts/run_project.py\nparameter-change --range-km 160', 75, 485, 1120, 90, 29, color.teal, true);
        text(slide, 'Известное ограничение переноса: окончания строк CSV влияют на хеши.', 75, 610, 1120, 45, 24, color.gray);
        break;
    case 'conclusion':
        text(slide, '250 647 поездок', 70, 205, 1140, 115, 78, color.teal, true);
        lines(slide, ['2,844 млрд руб. Дневные расписания проверены', 'Поездок на 11,77% больше, чем в жадном плане', 'Учёт отказов требует дополнительных денег'], 75, 355, 1120, 31, 78);
        text(slide, 'Спрос и цены заданы для учебного исследования.', 75, 615, 1130, 42, 25, color.gray);
        break;
    case 'references':
        lines(slide, [
            '[1] «Сеть дронопортов России». Задание проекта, с. 3–11.',
            '[2] Wikidata. Снимок 12.10.2026, CC0. query.wikidata.org',
            '[3] Собственные расчёты. Данные v1, модель 2.1. data/results/model_v2/',
            '[4] Код: github.com/Mukhammed-Abu-Suveilim/methods-of-optimization-droneports',
            '[5] Академические слайды: github.com/Gabberflast/academic-pptx-skill',
            '[6] УТР: github.com/loadkpi/simplified-technical-russian-skill'
        ], 70, 195, 1130, 25, 66);
        text(slide, 'Codex использован для кода и редактуры. Результаты проверены.', 75, 615, 1120, 40, 25, color.gray);
        break;
    case 'provenance':
        table(slide, [['Этап', 'Строки', 'Содержание'], ['Wikidata', '3 977', 'География, типы, население'], ['Проверка типов', '2 512', 'Кандидаты в населённые пункты'], ['Отбор до поиска', '75 / 12', 'Основной и малый набор'], ['Направленные пары', '5 550', 'Расстояния и спрос трёх лет']], {left: 65, top: 200, width: 1150, height: 360}, 27);
        text(slide, 'Население: максимум из сохранённых значений. Это не обязательно последний год.', 75, 607, 1120, 48, 24, color.gray);
        break;
    case 'constraints':
        lines(slide, ['Цель: F = Σ S(o,d,t) → max. S — завершённые поездки', '0 ≤ S(o,d,t) ≤ Q(o,d,t). Q — спрос', 'На рейсе ≤ 10 пассажиров. Пересадка ≥ 5 минут', 'Расходы до года t ≤ t × 1 млрд руб.', 'Дронов на зарядке ≤ числа зарядных мест'], 75, 200, 1120, 29, 82);
        break;
    case 'parameters':
        table(slide, [['Параметр', 'Значение'], ['Скорость / обслуживание / зарядка', '120 км/ч / 5 мин / 25 мин'], ['Дрон / малый порт / узловой порт', '250 / 60 / 120 млн руб.'], ['Содержание / лётный час', '10 млн руб./год / 50 тыс. руб.'], ['Рабочее окно / зарядные места', '07:00–21:00 / 2 или 4'], ['Взлёты и посадки', 'До 6 в час каждого вида']], {left: 65, top: 195, width: 1150, height: 395}, 27);
        text(slide, 'База фиксируется при покупке. Полная зарядка в начале и конце суток.', 75, 617, 1120, 38, 24, color.gray);
        break;
    case 'routing-detail':
        table(slide, [['Какие пути искали', 'Время, мс', 'Проверки пунктов'], ['BFS: 110 отдельных путей', '0,499', '715'], ['BFS: все пути из каждого пункта', '0,120', '121'], ['A*: 110 путей по времени', '3,295', '301'], ['A*: 110 путей по стоимости', '3,115', '272']], {left: 65, top: 200, width: 1150, height: 355}, 27);
        text(slide, 'Оценка A* учитывает округление расстояний до 0,1 км.', 75, 590, 1120, 55, 29, color.teal, true);
        break;
    case 'seeds': {
        const runs = data.statistics.seed_runs;
        chart(slide, 'line', runs.map(run => String(run.seed + 1)), [{name: 'Улучшенный план', values: runs.map(run => run.published_trips)}, {name: 'Начальный план', values: runs.map(run => run.initial_trips)}, {name: 'Жадный план', values: runs.map(() => data.statistics.greedy)}], {yAxis: {min: 220000, max: 255000, majorUnit: 5000, textStyle: {fontSize: 23, fill: color.gray}, majorGridlines: {fill: color.light, width: 1}}, xAxis: {title: 'Номер запуска', textStyle: {fontSize: 23, fill: color.ink}}});
        aside(slide, '9 308', 'Разница между лучшим и худшим результатом\n\nОт 241 339 до 250 647 поездок');
        break;
    }
    case 'duty': {
        const day = years[2].days[0];
        const charge = day.charges.filter(event => event.drone === 0)[1];
        const flights = day.flights.filter(event => event.drone === 0);
        const fragment = [...flights.filter(event => event.arrival_s <= charge.start_s).slice(-2), charge, ...flights.filter(event => event.departure_s >= charge.end_s).slice(0, 2)];
        const clock = seconds => [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(value => String(value).padStart(2, '0')).join(':');
        const seats = flight => day.journeys.filter(journey => journey.flights.includes(flight.id)).reduce((sum, journey) => sum + journey.count, 0);
        table(slide, [['Начало / конец', 'Событие', 'Маршрут / порт', 'Пассажиры'], ...fragment.map(event => event.id ? [`${clock(event.departure_s)}\n${clock(event.arrival_s)}`, 'Рейс', `${data.points[event.origin].name}\n${data.points[event.destination].name}`, String(seats(event))] : [`${clock(event.start_s)}\n${clock(event.end_s)}`, 'Зарядка', data.points[event.port].name, '—'])], {left: 65, top: 195, width: 1150, height: 395}, 27);
        text(slide, 'Третий год, дрон 1. Это расписание повторяется 52 дня. Ноль — рейс без пассажиров.', 75, 617, 1120, 38, 23, color.gray);
        break;
    }
    case 'coverage':
        lines(slide, ['Данные и постановка: 2–5, 23–25', 'Методы, границы и сравнения: 6–8, 13, 15, 26–27, 30', 'Карты трёх лет, бюджет и расписание: 9–12, 22, 28', 'Устойчивость и четыре эксперимента: 14–18', 'Воспроизведение, выводы и источники: 19–21'], 75, 200, 1120, 29, 82);
        break;
    case 'bound-detail':
        lines(slide, ['В модели упрощён порядок рейсов и зарядок', 'Полёт + обслуживание + зарядка ≤ время работы дронов', 'Путь ≤ дальность полной батареи × число зарядок', 'Рабочие расписания не исключаются из этой модели'], 75, 210, 1120, 30, 95);
        text(slide, 'Числа из упрощённой модели ещё не доказывают наличие рабочего расписания.', 75, 610, 1120, 43, 27, color.orange, true);
        break;
    default:
        throw new Error(`Unknown slide layout: ${item.layout}`);
    }
}

// 4. Сохранить речь, проверить пакет и экспортировать PDF.
const draft = path.join(build, `candidate_${revision}.pptx`);
await (await PresentationFile.exportPptx(deck)).save(draft);
const markdown = content.slides.map(item => `## ${item.number}. ${item.title} (${item.backup ? 'резерв' : `${item.seconds} с`})\n\n${item.notes}\n\nИсточник: ${item.source}`).join('\n\n');
await fs.writeFile(path.join(out, 'speaker_notes.md'), markdown, 'utf8');
await fs.writeFile(path.join(build, `manifest_${revision}.json`), JSON.stringify({chartOwners, tableOwners, plannedSeconds: content.slides.reduce((sum, item) => sum + item.seconds, 0)}, null, 2));
const finalPath = path.join(out, `droneports_academic_${revision}.pptx`);
await finalizePresentation({
    workspaceDir: root, candidatePath: draft, finalPath,
    pythonExecutable: path.join(runtimeRoot, 'python/python.exe'),
    integrityValidatorPath: path.join(skill, 'container_tools/inspect_presentation_package_integrity.py'),
    layoutValidatorPath: path.join(skill, 'container_tools/inspect_presentation_layout_geometry.py'),
    layoutArgs: ['--expected-slide-size-emu', '12192000,6858000', '--validate-heading-fit', ...tableOwners.flatMap(value => ['--require-native-table-slide', String(value)])],
    requiredNativeChartOwnerSlides: chartOwners, requiredNativeTableOwnerSlides: tableOwners,
    fontPolicy: {basis: 'design', families: [font]}, verifyArtifactToolImport: true,
    nativeChartTargetApplication: 'powerpoint', materializeLiteralChartWorkbooks: true,
    receiptPath: path.join(build, `validation_${revision}.json`)
});
execFileSync('C:/Program Files/LibreOffice/program/soffice.exe', ['--headless', `-env:UserInstallation=${pathToFileURL(path.join(build, 'lo-profile')).href}`, '--convert-to', 'pdf', '--outdir', out, finalPath], {stdio: 'inherit', timeout: 120000});
console.log(`Created ${content.slides.length} slides; main talk: ${content.slides.reduce((sum, item) => sum + item.seconds, 0) / 60} minutes.`);

"""Исторический greedy v1 и совместимый вход новой модели."""

from scripts.legacy_evaluator import Instance, OpenYears, evaluate

N_MAX = 12


def greedy(inst: Instance) -> tuple[OpenYears, list[int]]:
    """Воспроизвести прежние решения для проверки рефакторинга."""
    # 1. Выбрать стартовую пару по историческому отношению поездок к
    # цене.
    open_year: OpenYears = {i: None for i in inst.ids}
    fleet = [0, 0, 0]
    best_pair = None
    for i, j in inst.tau:
        if i > j:
            continue
        open_year[i] = 1
        open_year[j] = 1
        res = evaluate(inst, open_year, [1, 1, 1])
        open_year[i] = None
        open_year[j] = None
        if res and res["trips"] > 0:
            ratio = res["trips"] / max(1.0, res["cum_spend_rub"])
            if best_pair is None or ratio > best_pair[2]:
                best_pair = (i, j, ratio)
    if best_pair is None:
        return open_year, fleet
    open_year[best_pair[0]] = 1
    open_year[best_pair[1]] = 1
    fleet = [1, 1, 1]
    for t in (1, 2, 3):
        while True:
            base = evaluate(inst, open_year, fleet)
            best: tuple[str, str | int, float] | None = None
            for i in inst.ids:
                if open_year[i] is not None:
                    continue
                open_year[i] = t
                res = evaluate(inst, open_year, fleet)
                open_year[i] = None
                if res and base and res["trips"] > base["trips"]:
                    ratio = (res["trips"] - base["trips"]) / max(
                        1.0, res["cum_spend_rub"] - base["cum_spend_rub"]
                    )
                    if best is None or ratio > best[2]:
                        best = ("open", i, ratio)
            for s in (t, t + 1, t + 2):
                cand = list(fleet)
                for k in range(s - 1, 3):
                    cand[k] += 1
                if cand[2] > N_MAX:
                    continue
                res = evaluate(inst, open_year, cand)
                if res and base and res["trips"] > base["trips"]:
                    ratio = (res["trips"] - base["trips"]) / max(
                        1.0, res["cum_spend_rub"] - base["cum_spend_rub"]
                    )
                    if best is None or ratio > best[2]:
                        best = ("drone", s, ratio)
            if best is None:
                break
            if best[0] == "open":
                assert isinstance(best[1], str)
                open_year[best[1]] = t
            else:
                assert isinstance(best[1], int)
                for k in range(best[1] - 1, 3):
                    fleet[k] += 1
    return open_year, fleet

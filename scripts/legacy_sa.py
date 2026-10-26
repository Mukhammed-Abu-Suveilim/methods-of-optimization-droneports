"""Исторический отжиг v1; сохраняется для регрессионной проверки."""

import math
import random
from typing import TypedDict

from scripts.legacy_evaluator import Instance, OpenYears, evaluate

SeedRun = TypedDict("SeedRun", {"seed": int, "trips": int})

N_MAX = 12
ITERS = 800
SEEDS = list(range(10))


def neighbor(
    open_year: OpenYears, fleet: list[int], rng: random.Random
) -> tuple[OpenYears, list[int]] | None:
    """Воспроизвести прежнюю окрестность с тем же расходом RNG."""
    oy = dict(open_year)
    fl = list(fleet)
    if rng.random() < 0.6:
        i = rng.choice(list(oy))
        oy[i] = rng.choice([None, 1, 2, 3])
    else:
        t = rng.randrange(3)
        fl[t] += rng.choice([-1, 1])
        if fl[t] < 0:
            return None
        for k in range(1, 3):
            fl[k] = max(fl[k], fl[k - 1])
        if fl[2] > N_MAX:
            return None
    return oy, fl


def sa(
    inst: Instance,
    init: tuple[OpenYears, list[int]],
    seed: int,
    iters: int = ITERS,
) -> tuple[int, OpenYears, list[int]] | None:
    """Воспроизвести прежнюю температуру и критерий остановки."""
    # 1. Зафиксировать состояние начального решения и генератора.
    rng = random.Random(seed)
    cur_oy, cur_fl = init
    cur = evaluate(inst, cur_oy, cur_fl)
    if cur is None:
        return None
    best = (cur["trips"], dict(cur_oy), list(cur_fl))
    temperature = 3000.0
    for _ in range(iters):
        temperature *= 0.995
        nb = neighbor(cur_oy, cur_fl, rng)
        if nb is None:
            continue
        res = evaluate(inst, nb[0], nb[1])
        if res is None:
            continue
        d = res["trips"] - cur["trips"]
        if d > 0 or rng.random() < math.exp(d / temperature):
            cur_oy, cur_fl = nb
            cur = res
            if cur["trips"] > best[0]:
                best = (cur["trips"], dict(cur_oy), list(cur_fl))
    return best

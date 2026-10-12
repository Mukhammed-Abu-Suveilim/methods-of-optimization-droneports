import json
import time
from pathlib import Path

from evaluator import Instance, evaluate
from solve_greedy import greedy


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    inst = Instance("data/processed/points_small.csv", "data/processed/demand_main.csv", params)
    g_oy, g_fl = greedy(inst)
    open_ports = [i for i in inst.ids if g_oy[i] is not None]
    actions = {"greedy": (dict(g_oy), list(g_fl))}
    for p in sorted(open_ports, key=lambda i: inst.pop[i])[:2]:
        oy = dict(g_oy)
        oy[p] = None
        actions["greedy_minus_" + inst.names[p]] = (oy, list(g_fl))
    extra = max((i for i in inst.ids if g_oy[i] is None), key=lambda i: inst.pop[i])
    oy = dict(g_oy)
    oy[extra] = 1
    actions["greedy_plus_" + inst.names[extra]] = (oy, list(g_fl))

    def value(oy, fl, closed):
        oy2 = dict(oy)
        if closed:
            oy2[closed] = None
        r = evaluate(inst, oy2, fl)
        return r["trips"] if r else 0

    t0 = time.time()
    full = {}
    nodes_full = 0
    for aname, (oy, fl) in actions.items():
        ports = [i for i in inst.ids if oy[i] is not None]
        vals = {}
        for p in ports:
            vals[inst.names[p]] = value(oy, fl, p)
            nodes_full += 1
        full[aname] = vals
    game_full = max(min(v.values()) for v in full.values())
    t_full = time.time() - t0

    t0 = time.time()
    nodes_ab = 0
    alpha = -1
    best_a = None
    best_v = None
    for aname, (oy, fl) in actions.items():
        ports = [i for i in inst.ids if oy[i] is not None]
        worst = None
        for p in ports:
            v = value(oy, fl, p)
            nodes_ab += 1
            worst = v if worst is None else min(worst, v)
            if worst <= alpha:
                break
        if worst is not None and (best_v is None or worst > best_v):
            alpha = worst
            best_a = aname
            best_v = worst
    t_ab = time.time() - t0

    out = {
        "game_value_full_enumeration": game_full,
        "game_value_alpha_beta": best_v,
        "best_action": best_a,
        "nodes_full": nodes_full,
        "nodes_alpha_beta": nodes_ab,
        "time_full_s": round(t_full, 3),
        "time_alpha_beta_s": round(t_ab, 3),
        "values_equal": game_full == best_v,
        "table": full,
        "note": "двухуровневое дерево: выбор сети -> закрытие одного узла противником -> перестройка перевозок оценщиком",
    }
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / "game_small.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "table"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

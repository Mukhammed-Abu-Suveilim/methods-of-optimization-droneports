import heapq
import json
from collections import deque
from pathlib import Path

from evaluator import Instance


def main():
    params = json.loads(Path("data/parameters.json").read_text(encoding="utf-8"))
    inst = Instance("data/processed/points_main.csv", "data/processed/demand_main.csv", params)
    plan = json.loads(Path("data/results/sa_main.json").read_text(encoding="utf-8"))
    rev = {v: k for k, v in inst.names.items()}
    oy = {i: None for i in inst.ids}
    for name, y in plan["open_year"].items():
        oy[rev[name]] = y
    nodes = [i for i in inst.ids if oy[i] is not None and oy[i] <= 3]
    v = params["drone"]["cruise_speed_kmh"]
    transfer = params["drone"]["turnaround_min"] + params["drone"]["recharge_min"]
    seat_cost = params["operations"]["cost_per_flight_hour_rub"] / params["drone"]["seats"]
    adj = {n: [] for n in nodes}
    for (a, b) in inst.tau:
        if a in adj and b in adj:
            adj[a].append((b, inst.tau[(a, b)], inst.fh[(a, b)] * seat_cost))

    def bfs(s):
        parent = {s: None}
        q = deque([s])
        while q:
            u = q.popleft()
            for w, _, _ in adj[u]:
                if w not in parent:
                    parent[w] = u
                    q.append(w)
        return parent, len(parent)

    def path_of(parent, t):
        p = []
        cur = t
        while parent[cur] is not None:
            p.append((parent[cur], cur))
            cur = parent[cur]
        return p[::-1]

    def cost_of(path, kind):
        return sum(inst.tau[e] if kind == "time" else inst.fh[e] * seat_cost for e in path)

    def astar(s, t, kind):
        if kind == "time":
            h = lambda n: 0.0 if n == t else inst.dist[(n, t)] / v * 60 + transfer
        else:
            h = lambda n: 0.0 if n == t else inst.dist[(n, t)] / v * seat_cost
        openq = [(h(s), 0.0, s, None)]
        came = {}
        g = {s: 0.0}
        pops = 0
        while openq:
            _, gu, u, pu = heapq.heappop(openq)
            if u in came:
                continue
            came[u] = pu
            pops += 1
            if u == t:
                break
            for w, tt, cc in adj[u]:
                if w in came:
                    continue
                ng = gu + (tt if kind == "time" else cc)
                if ng < g.get(w, float("inf")):
                    g[w] = ng
                    heapq.heappush(openq, (ng + h(w), ng, w, u))
        if t not in came:
            return None, pops
        p = []
        cur = t
        while came[cur] is not None:
            p.append((came[cur], cur))
            cur = came[cur]
        return p[::-1], pops

    rows = []
    exp_bfs = exp_a_time = exp_a_cost = 0
    worse_time = worse_cost = 0
    sum_excess_time = sum_excess_cost = 0.0
    worst_pair = None
    for s in nodes:
        parent, vis = bfs(s)
        exp_bfs += vis
        for t in nodes:
            if t == s or t not in parent:
                continue
            pb = path_of(parent, t)
            pt, pops_t = astar(s, t, "time")
            pc, pops_c = astar(s, t, "cost")
            exp_a_time += pops_t
            exp_a_cost += pops_c
            tb, tt = cost_of(pb, "time"), cost_of(pt, "time")
            cb, cc = cost_of(pb, "cost"), cost_of(pc, "cost")
            if tt < tb - 1e-9:
                worse_time += 1
                sum_excess_time += tb - tt
                if worst_pair is None or tb - tt > worst_pair["excess_min"]:
                    worst_pair = {
                        "from": inst.names[s],
                        "to": inst.names[t],
                        "bfs_hops": len(pb),
                        "bfs_time_min": round(tb, 1),
                        "astar_time_min": round(tt, 1),
                        "excess_min": round(tb - tt, 1),
                    }
            if cc < cb - 1e-9:
                worse_cost += 1
                sum_excess_cost += cb - cc
            rows.append({"from": inst.names[s], "to": inst.names[t], "bfs_hops": len(pb), "bfs_time": round(tb, 1), "astar_time": round(tt, 1), "bfs_cost": round(cb, 1), "astar_cost": round(cc, 1)})
    out = {
        "network": sorted(inst.names[n] for n in nodes),
        "pairs": len(rows),
        "bfs_expansions_total": exp_bfs,
        "astar_expansions_time": exp_a_time,
        "astar_expansions_cost": exp_a_cost,
        "pairs_where_bfs_suboptimal_time": worse_time,
        "mean_excess_time_min": round(sum_excess_time / worse_time, 1) if worse_time else 0.0,
        "pairs_where_bfs_suboptimal_cost": worse_cost,
        "mean_excess_cost_rub": round(sum_excess_cost / worse_cost, 1) if worse_cost else 0.0,
        "worst_pair": worst_pair,
        "heuristic_admissibility": "h(n)=d(n,t)/v*60+30 (время) и h(n)=d(n,t)/v*cost_fh/10 (стоимость): любой оставшийся путь имеет время полёта >= d(n,t)/v по неравенству треугольников и >=30 мин на каждый участок, поэтому h не превосходит истинной оставшейся стоимости",
        "note": "BFS оптимален по числу перелётов; минимум перелётов не совпадает с минимумом времени/стоимости, когда обходной путь короче по времени полёта или когда среди путей с минимальным числом перелётов есть разные по времени; поиск маршрута не решает задачу размещения дронопортов",
    }
    Path("data/results").mkdir(parents=True, exist_ok=True)
    (Path("data/results") / "routing_comparison.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
astar_vs_aplus_live.py
=======================
A live, interactive, scientific dashboard comparing:

    - Standard A*      : binary-heap, octile heuristic, 8-directional grid
    - A+ (enhanced)     : Jump Point Search (JPS) -- provably same-cost
                           optimal path as A*, but "jumps" over open
                           corridors instead of expanding every cell

NEW in this version (vs. the plain script)
-------------------------------------------
  1. Fully interactive GUI (matplotlib widgets: sliders + buttons) --
     change grid size, obstacle density, batch size and animation speed
     LIVE, no restart needed.
  2. "Live Race" mode: watch both algorithms expand their search frontier
     cell-by-cell in real time, side by side, colour-coded by discovery
     order (a heatmap of *how* each algorithm explores). You can visibly
     see A* fan out one cell at a time while A+ leaps in long jumps
     across open space.
  3. "Run Batch" mode: solves N problems *in parallel across all CPU
     cores* (ProcessPoolExecutor) and streams results into live-updating
     scientific charts (bar / cumulative-speedup line / speedup
     histogram) as they complete -- this is the "batch, in real time"
     the dashboard is built around.
  4. A dark, scientific instrument-panel look: heatmap search-order
     colouring, live numeric readouts, progress bar.

USAGE
-----
    pip install numpy matplotlib
    python astar_vs_aplus_live.py

Everything else (grid size, density, batch count, speed) is controlled
from the on-screen sliders and buttons -- no command line flags needed.
"""

import heapq
import itertools
import math
import queue
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

# ==========================================================================
# constants
# ==========================================================================
FREE, WALL = 0, 1
NEIGH8 = [(-1, -1), (-1, 0), (-1, 1),
          (0, -1),           (0, 1),
          (1, -1),  (1, 0),  (1, 1)]
SQRT2 = math.sqrt(2)


def octile(a, b):
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    return (dx + dy) + (SQRT2 - 2) * min(dx, dy)


def sign(x):
    return (x > 0) - (x < 0)


def reconstruct(came_from, start, goal):
    if goal == start:
        return [start]
    if goal not in came_from:
        return None
    path = [goal]
    cur = goal
    while cur != start:
        cur = came_from[cur]
        path.append(cur)
    path.reverse()
    return path


# ==========================================================================
# dataset generation
# ==========================================================================
def _passable_factory(grid):
    size = grid.shape[0]

    def passable(p):
        x, y = p
        return 0 <= x < size and 0 <= y < size and grid[x, y] != WALL

    return passable


def _bfs_solvable(grid, start, goal):
    passable = _passable_factory(grid)
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur == goal:
            return True
        for dx, dy in NEIGH8:
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt not in seen and passable(nxt):
                seen.add(nxt)
                stack.append(nxt)
    return False


def generate_problem(size, density, rng):
    for _ in range(200):
        grid = np.zeros((size, size), dtype=np.uint8)
        grid[rng.random((size, size)) < density] = WALL
        start, goal = (0, 0), (size - 1, size - 1)
        grid[start] = FREE
        grid[goal] = FREE
        if _bfs_solvable(grid, start, goal):
            return grid, start, goal
    return np.zeros((size, size), dtype=np.uint8), (0, 0), (size - 1, size - 1)


# ==========================================================================
# STANDARD A*  (fast, non-stepping -- used for batch mode)
# ==========================================================================
def astar(grid, start, goal):
    passable = _passable_factory(grid)
    open_heap = [(octile(start, goal), 0.0, start)]
    came_from, gscore, closed = {}, {start: 0.0}, set()
    expanded = 0
    t0 = time.perf_counter()
    while open_heap:
        _, g, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        closed.add(cur)
        expanded += 1
        if cur == goal:
            break
        for dx, dy in NEIGH8:
            nxt = (cur[0] + dx, cur[1] + dy)
            if not passable(nxt):
                continue
            if dx != 0 and dy != 0:
                if not passable((cur[0] + dx, cur[1])) and not passable((cur[0], cur[1] + dy)):
                    continue
            step = SQRT2 if (dx != 0 and dy != 0) else 1.0
            ng = g + step
            if ng < gscore.get(nxt, math.inf):
                gscore[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(open_heap, (ng + octile(nxt, goal), ng, nxt))
    t1 = time.perf_counter()
    return {"name": "A*", "path": reconstruct(came_from, start, goal),
            "time": t1 - t0, "expanded": expanded, "visited": closed,
            "cost": gscore.get(goal)}


# ==========================================================================
# A+  ==  Jump Point Search  (padded grid = branch-free bounds checks)
# ==========================================================================
def _pad_grid(grid):
    size = grid.shape[0]
    padded = np.ones((size + 2, size + 2), dtype=np.uint8)
    padded[1:-1, 1:-1] = grid
    return padded


def _jump(padded, x, y, dx, dy, gx, gy):
    while True:
        if dx != 0 and dy != 0:
            a_free = padded[x + dx + 1, y + 1] == FREE
            b_free = padded[x + 1, y + dy + 1] == FREE
            if not a_free and not b_free:
                return None
        x += dx
        y += dy
        if padded[x + 1, y + 1] != FREE:
            return None
        if x == gx and y == gy:
            return (x, y)
        if dx != 0 and dy != 0:
            if (padded[x - dx + 1, y + dy + 1] == FREE and padded[x - dx + 1, y + 1] != FREE) or \
               (padded[x + dx + 1, y - dy + 1] == FREE and padded[x + 1, y - dy + 1] != FREE):
                return (x, y)
            if _jump(padded, x, y, dx, 0, gx, gy) is not None or \
               _jump(padded, x, y, 0, dy, gx, gy) is not None:
                return (x, y)
        else:
            if dx != 0:
                if (padded[x + dx + 1, y + 2] == FREE and padded[x + 1, y + 2] != FREE) or \
                   (padded[x + dx + 1, y] == FREE and padded[x + 1, y] != FREE):
                    return (x, y)
            else:
                if (padded[x + 2, y + dy + 1] == FREE and padded[x + 2, y + 1] != FREE) or \
                   (padded[x, y + dy + 1] == FREE and padded[x, y + 1] != FREE):
                    return (x, y)


def _pruned_neighbors(padded, cur, parent):
    x, y = cur

    def free(ax, ay):
        return padded[ax + 1, ay + 1] == FREE

    if parent is None:
        return [(dx, dy) for dx, dy in NEIGH8 if free(x + dx, y + dy)]

    dx, dy = sign(x - parent[0]), sign(y - parent[1])
    out = []
    if dx != 0 and dy != 0:
        if free(x, y + dy):
            out.append((0, dy))
        if free(x + dx, y):
            out.append((dx, 0))
        if free(x + dx, y + dy) and not (not free(x, y + dy) and not free(x + dx, y)):
            out.append((dx, dy))
        if not free(x - dx, y) and free(x, y + dy):
            out.append((-dx, dy))
        if not free(x, y - dy) and free(x + dx, y):
            out.append((dx, -dy))
    elif dx == 0:
        if free(x, y + dy):
            out.append((0, dy))
            if not free(x + 1, y):
                out.append((1, dy))
            if not free(x - 1, y):
                out.append((-1, dy))
    else:
        if free(x + dx, y):
            out.append((dx, 0))
            if not free(x, y + 1):
                out.append((dx, 1))
            if not free(x, y - 1):
                out.append((dx, -1))
    return out


def _expand_jump_path(jp_path):
    if not jp_path:
        return jp_path
    full = [jp_path[0]]
    for a, b in zip(jp_path, jp_path[1:]):
        dx, dy = sign(b[0] - a[0]), sign(b[1] - a[1])
        x, y = a
        while (x, y) != b:
            x, y = x + dx, y + dy
            full.append((x, y))
    return full


def aplus(grid, start, goal):
    padded = _pad_grid(grid)
    gx, gy = goal
    open_heap = [(octile(start, goal), 0.0, start)]
    came_from, gscore, closed = {}, {start: 0.0}, set()
    expanded = 0
    t0 = time.perf_counter()
    while open_heap:
        _, g, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        closed.add(cur)
        expanded += 1
        if cur == goal:
            break
        parent = came_from.get(cur)
        cx, cy = cur
        for dx, dy in _pruned_neighbors(padded, cur, parent):
            jp = _jump(padded, cx, cy, dx, dy, gx, gy)
            if jp is None or jp in closed:
                continue
            dist = math.hypot(jp[0] - cx, jp[1] - cy)
            ng = g + dist
            if ng < gscore.get(jp, math.inf):
                gscore[jp] = ng
                came_from[jp] = cur
                heapq.heappush(open_heap, (ng + octile(jp, goal), ng, jp))
    t1 = time.perf_counter()
    jp_path = reconstruct(came_from, start, goal)
    return {"name": "A+ (JPS)", "path": _expand_jump_path(jp_path) if jp_path else None,
            "time": t1 - t0, "expanded": expanded, "visited": closed,
            "cost": gscore.get(goal)}


# ==========================================================================
# STEPPING generators -- identical algorithms, but yield after every node
# expansion so the GUI can animate the search frontier live.
# ==========================================================================
def astar_steps(grid, start, goal):
    passable = _passable_factory(grid)
    open_heap = [(octile(start, goal), 0.0, start)]
    came_from, gscore, closed = {}, {start: 0.0}, set()
    expanded = 0
    t0 = time.perf_counter()
    while open_heap:
        _, g, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        closed.add(cur)
        expanded += 1
        yield {"type": "expand", "cells": [cur]}
        if cur == goal:
            break
        for dx, dy in NEIGH8:
            nxt = (cur[0] + dx, cur[1] + dy)
            if not passable(nxt):
                continue
            if dx != 0 and dy != 0:
                if not passable((cur[0] + dx, cur[1])) and not passable((cur[0], cur[1] + dy)):
                    continue
            step = SQRT2 if (dx != 0 and dy != 0) else 1.0
            ng = g + step
            if ng < gscore.get(nxt, math.inf):
                gscore[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(open_heap, (ng + octile(nxt, goal), ng, nxt))
    t1 = time.perf_counter()
    yield {"type": "done", "path": reconstruct(came_from, start, goal),
           "time": t1 - t0, "expanded": expanded, "cost": gscore.get(goal)}


def aplus_steps(grid, start, goal):
    padded = _pad_grid(grid)
    gx, gy = goal
    open_heap = [(octile(start, goal), 0.0, start)]
    came_from, gscore, closed = {}, {start: 0.0}, set()
    expanded = 0
    t0 = time.perf_counter()
    while open_heap:
        _, g, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        closed.add(cur)
        expanded += 1
        parent = came_from.get(cur)
        # light up the whole jumped segment, not just the endpoint --
        # this is what visually shows the "jump" in the animation
        if parent is not None:
            dx, dy = sign(cur[0] - parent[0]), sign(cur[1] - parent[1])
            x, y = parent
            seg = []
            while (x, y) != cur:
                x, y = x + dx, y + dy
                seg.append((x, y))
        else:
            seg = [cur]
        yield {"type": "expand", "cells": seg}
        if cur == goal:
            break
        cx, cy = cur
        for dx, dy in _pruned_neighbors(padded, cur, parent):
            jp = _jump(padded, cx, cy, dx, dy, gx, gy)
            if jp is None or jp in closed:
                continue
            dist = math.hypot(jp[0] - cx, jp[1] - cy)
            ng = g + dist
            if ng < gscore.get(jp, math.inf):
                gscore[jp] = ng
                came_from[jp] = cur
                heapq.heappush(open_heap, (ng + octile(jp, goal), ng, jp))
    t1 = time.perf_counter()
    jp_path = reconstruct(came_from, start, goal)
    yield {"type": "done", "path": _expand_jump_path(jp_path) if jp_path else None,
           "time": t1 - t0, "expanded": expanded, "cost": gscore.get(goal)}


# ==========================================================================
# Parallel batch worker -- MUST be a top-level function to be picklable
# for ProcessPoolExecutor (this is the "use every CPU core" upgrade).
# ==========================================================================
def solve_problem(args):
    grid, start, goal = args
    r1 = astar(grid, start, goal)
    r2 = aplus(grid, start, goal)
    match = (r1["cost"] is None and r2["cost"] is None) or \
            (r1["cost"] is not None and r2["cost"] is not None and abs(r1["cost"] - r2["cost"]) < 1e-6)
    return {
        "astar_time": r1["time"], "astar_expanded": r1["expanded"], "astar_cost": r1["cost"],
        "aplus_time": r2["time"], "aplus_expanded": r2["expanded"], "aplus_cost": r2["cost"],
        "match": match,
    }


# ==========================================================================
# GUI DASHBOARD
# ==========================================================================
def run_app():
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Slider, Button
    from matplotlib.gridspec import GridSpec
    import os

    plt.style.use("dark_background")
    BG = "#12141c"
    WALL_C = np.array([30, 32, 42]) / 255.0
    BASE_C = np.array([18, 20, 28]) / 255.0
    PATH_C = np.array([255, 195, 0]) / 255.0
    cmap = plt.cm.plasma

    fig = plt.figure(figsize=(15, 9.2), facecolor=BG)
    fig.suptitle("A*  vs  A+ (Jump Point Search)  --  live scientific dashboard",
                 fontsize=15, color="white", weight="bold")

    gs = GridSpec(3, 3, figure=fig, height_ratios=[3.2, 1.15, 0.65],
                  hspace=0.55, wspace=0.32, top=0.90, bottom=0.06, left=0.045, right=0.98)

    ax_left = fig.add_subplot(gs[0, 0:1].subgridspec(1, 1)[0])
    ax_right = fig.add_subplot(gs[0, 1:2].subgridspec(1, 1)[0])
    ax_info = fig.add_subplot(gs[0, 2])
    ax_bar = fig.add_subplot(gs[1, 0])
    ax_line = fig.add_subplot(gs[1, 1])
    ax_hist = fig.add_subplot(gs[1, 2])

    for ax in (ax_left, ax_right):
        ax.set_xticks([]); ax.set_yticks([])
    ax_info.axis("off")

    # ---------------- widget row ----------------
    def slider_ax(x, y, w, h):
        a = fig.add_axes([x, y, w, h], facecolor="#232636")
        return a

    s_size = Slider(slider_ax(0.06, 0.155, 0.17, 0.02), "Grid size", 20, 160, valinit=60, valstep=5, color="#4cc9f0")
    s_density = Slider(slider_ax(0.06, 0.115, 0.17, 0.02), "Obstacle %", 0, 45, valinit=10, valstep=1, color="#f77f00")
    s_batch = Slider(slider_ax(0.32, 0.155, 0.17, 0.02), "Batch size (N)", 5, 300, valinit=60, valstep=5, color="#90be6d")
    s_speed = Slider(slider_ax(0.32, 0.115, 0.17, 0.02), "Race speed (steps/frame)", 1, 60, valinit=8, valstep=1, color="#f94144")
    s_workers = Slider(slider_ax(0.58, 0.115, 0.17, 0.02), "CPU workers", 1, max(1, os.cpu_count() or 1),
                        valinit=max(1, os.cpu_count() or 1), valstep=1, color="#9d4edd")

    for s in (s_size, s_density, s_batch, s_speed, s_workers):
        s.label.set_color("white")
        s.valtext.set_color("white")

    btn_race = Button(fig.add_axes([0.58, 0.155, 0.11, 0.035]), "New Live Race", color="#22304a", hovercolor="#2f4368")
    btn_batch = Button(fig.add_axes([0.70, 0.155, 0.11, 0.035]), "Run Batch (parallel)", color="#22304a", hovercolor="#2f4368")
    btn_stop = Button(fig.add_axes([0.82, 0.155, 0.09, 0.035]), "Stop", color="#4a2222", hovercolor="#682f2f")
    for b in (btn_race, btn_batch, btn_stop):
        b.label.set_color("white")

    ax_progress = fig.add_axes([0.06, 0.075, 0.85, 0.018], facecolor="#232636")
    ax_progress.set_xticks([]); ax_progress.set_yticks([])
    ax_progress.set_xlim(0, 1); ax_progress.set_ylim(0, 1)
    from matplotlib.patches import Rectangle
    progress_fill = Rectangle((0, 0), 0.0, 1, color="#4cc9f0")
    ax_progress.add_patch(progress_fill)
    progress_txt = fig.text(0.06, 0.045, "Ready.", color="#9aa4c0", fontsize=9.5, family="monospace")

    # ---------------- state ----------------
    state = {
        "mode": None,          # None | 'race' | 'batch'
        "timer": None,
        "gen_a": None, "gen_b": None,
        "order_a": None, "order_b": None,
        "n_a": 0, "n_b": 0,
        "done_a": None, "done_b": None,
        "start": None, "goal": None,
        "grid": None,
        "executor": None,
        "futures": None,
        "batch_results": [],
        "batch_total": 0,
    }

    def make_rgb(grid, order, done, start, goal, max_order):
        size = grid.shape[0]
        rgb = np.tile(BASE_C, (size, size, 1))
        rgb[grid == WALL] = WALL_C
        mask = order >= 0
        if mask.any() and max_order > 0:
            t = np.clip(order[mask] / max_order, 0, 1)
            rgb[mask] = cmap(t)[:, :3]
        if done and done.get("path"):
            for (x, y) in done["path"]:
                rgb[x, y] = PATH_C
        return rgb

    def render_panel(ax, grid, order, done, start, goal, title, elapsed, n_expanded):
        max_order = max(1, (order.max() if order.size else 1))
        rgb = make_rgb(grid, order, done, start, goal, max_order)
        ax.clear()
        ax.imshow(rgb, interpolation="nearest")
        ax.plot(start[1], start[0], marker="o", markersize=7, color="#2ecc71", markeredgecolor="white", zorder=5)
        ax.plot(goal[1], goal[0], marker="*", markersize=13, color="#e63946", markeredgecolor="white", zorder=5)
        ax.set_xticks([]); ax.set_yticks([])
        cost_txt = f"{done['cost']:.1f}" if done and done.get("cost") is not None else "..."
        ax.set_title(f"{title}\n{elapsed*1000:6.2f} ms   |   {n_expanded} expanded   |   cost {cost_txt}",
                     color="white", fontsize=10.5)

    def clear_charts():
        ax_bar.clear(); ax_line.clear(); ax_hist.clear()
        for ax, title in ((ax_bar, "Time per algorithm (ms)"),
                          (ax_line, "Cumulative avg speedup"),
                          (ax_hist, "Speedup distribution")):
            ax.set_title(title, color="white", fontsize=9.5)
            ax.tick_params(colors="#9aa4c0", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("#3a3f55")

    clear_charts()

    def stop_everything():
        if state["timer"] is not None:
            state["timer"].stop()
            state["timer"] = None
        if state["executor"] is not None:
            state["executor"].shutdown(wait=False, cancel_futures=True)
            state["executor"] = None
        state["mode"] = None
        progress_txt.set_text("Stopped.")
        fig.canvas.draw_idle()

    # ---------------- LIVE RACE MODE ----------------
    def start_race(event=None):
        stop_everything()
        size = int(s_size.val)
        density = s_density.val / 100.0
        rng = np.random.default_rng()
        grid, start, goal = generate_problem(size, density, rng)
        state.update(grid=grid, start=start, goal=goal,
                     order_a=np.full((size, size), -1, dtype=np.int32),
                     order_b=np.full((size, size), -1, dtype=np.int32),
                     n_a=0, n_b=0, done_a=None, done_b=None,
                     gen_a=astar_steps(grid, start, goal),
                     gen_b=aplus_steps(grid, start, goal),
                     mode="race")
        progress_txt.set_text(f"Live race running on a {size}x{size} grid ({density*100:.0f}% obstacles)...")

        t_start = time.perf_counter()

        def tick():
            steps = int(s_speed.val)
            for _ in range(steps):
                if state["done_a"] is None:
                    try:
                        ev = next(state["gen_a"])
                        if ev["type"] == "expand":
                            for (x, y) in ev["cells"]:
                                if state["order_a"][x, y] < 0:
                                    state["order_a"][x, y] = state["n_a"]
                            state["n_a"] += 1
                        else:
                            state["done_a"] = ev
                    except StopIteration:
                        pass
                if state["done_b"] is None:
                    try:
                        ev = next(state["gen_b"])
                        if ev["type"] == "expand":
                            for (x, y) in ev["cells"]:
                                if state["order_b"][x, y] < 0:
                                    state["order_b"][x, y] = state["n_b"]
                            state["n_b"] += 1
                        else:
                            state["done_b"] = ev
                    except StopIteration:
                        pass

            elapsed_a = state["done_a"]["time"] if state["done_a"] else (time.perf_counter() - t_start)
            elapsed_b = state["done_b"]["time"] if state["done_b"] else (time.perf_counter() - t_start)
            render_panel(ax_left, grid, state["order_a"], state["done_a"], start, goal,
                         "Standard A*", elapsed_a, state["n_a"])
            render_panel(ax_right, grid, state["order_b"], state["done_b"], start, goal,
                         "A+  (Jump Point Search)", elapsed_b, state["n_b"])

            ax_info.clear(); ax_info.axis("off")
            lines = ["LIVE RACE", "-" * 26]
            lines.append(f"grid        : {size} x {size}")
            lines.append(f"density     : {density*100:.0f}%")
            lines.append(f"A* expanded : {state['n_a']}")
            lines.append(f"A+ expanded : {state['n_b']}")
            if state["done_a"] and state["done_b"]:
                ta, tb = state["done_a"]["time"], state["done_b"]["time"]
                sp = ta / tb if tb > 0 else float("inf")
                ca, cb = state["done_a"]["cost"], state["done_b"]["cost"]
                match = (ca is None and cb is None) or (ca is not None and cb is not None and abs(ca - cb) < 1e-6)
                lines.append("-" * 26)
                lines.append(f"A* time     : {ta*1000:.2f} ms")
                lines.append(f"A+ time     : {tb*1000:.2f} ms")
                lines.append(f"speedup     : {sp:.2f}x")
                lines.append(f"cost match  : {'YES (exact)' if match else 'NO'}")
            ax_info.text(0.02, 0.98, "\n".join(lines), transform=ax_info.transAxes,
                         va="top", ha="left", family="monospace", fontsize=10.5, color="#c9d3ee")

            fig.canvas.draw_idle()

            if state["done_a"] and state["done_b"]:
                state["timer"].stop()
                state["mode"] = None
                progress_txt.set_text("Race finished. Adjust sliders and click 'New Live Race' to run another.")

        timer = fig.canvas.new_timer(interval=25)
        timer.add_callback(tick)
        state["timer"] = timer
        timer.start()

    # ---------------- BATCH MODE (multi-core parallel) ----------------
    def start_batch(event=None):
        stop_everything()
        size = int(s_size.val)
        density = s_density.val / 100.0
        n = int(s_batch.val)
        workers = int(s_workers.val)
        rng = np.random.default_rng()
        problems = [generate_problem(size, density, rng) for _ in range(n)]

        executor = ProcessPoolExecutor(max_workers=workers)
        futures = [executor.submit(solve_problem, p) for p in problems]
        state.update(executor=executor, futures=futures, batch_results=[], batch_total=n, mode="batch")

        clear_charts()
        t_batch_start = time.perf_counter()
        progress_txt.set_text(f"Solving {n} problems across {workers} CPU worker(s) in parallel...")

        def tick():
            newly_done = [f for f in state["futures"] if f.done()]
            for f in newly_done:
                state["futures"].remove(f)
                try:
                    state["batch_results"].append(f.result())
                except Exception:
                    pass

            results = state["batch_results"]
            done_n = len(results)
            frac = done_n / state["batch_total"] if state["batch_total"] else 0
            progress_fill.set_xy([[0, 0], [0, 1], [frac, 1], [frac, 0], [0, 0]])

            if results:
                tot_a = sum(r["astar_time"] for r in results)
                tot_b = sum(r["aplus_time"] for r in results)
                matches = sum(1 for r in results if r["match"])
                speedups = [r["astar_time"] / r["aplus_time"] for r in results if r["aplus_time"] > 0]
                avg_speedup = sum(speedups) / len(speedups) if speedups else 0

                ax_bar.clear()
                ax_bar.set_title("Total time so far (ms)", color="white", fontsize=9.5)
                bars = ax_bar.bar(["A*", "A+"], [tot_a * 1000, tot_b * 1000], color=["#f94144", "#4cc9f0"])
                ax_bar.tick_params(colors="#9aa4c0", labelsize=8)
                for b in bars:
                    ax_bar.text(b.get_x() + b.get_width() / 2, b.get_height(), f"{b.get_height():.0f}",
                                ha="center", va="bottom", color="white", fontsize=8)

                ax_line.clear()
                ax_line.set_title("Cumulative avg speedup", color="white", fontsize=9.5)
                running = np.cumsum(speedups) / np.arange(1, len(speedups) + 1) if speedups else []
                ax_line.plot(running, color="#90be6d", linewidth=2)
                ax_line.axhline(1.0, color="#555", linestyle="--", linewidth=1)
                ax_line.tick_params(colors="#9aa4c0", labelsize=8)

                ax_hist.clear()
                ax_hist.set_title("Speedup distribution", color="white", fontsize=9.5)
                if speedups:
                    ax_hist.hist(speedups, bins=min(20, max(3, len(speedups) // 3)), color="#f77f00")
                ax_hist.tick_params(colors="#9aa4c0", labelsize=8)

                wall = time.perf_counter() - t_batch_start
                ax_info.clear(); ax_info.axis("off")
                lines = ["BATCH  (parallel)", "-" * 26]
                lines.append(f"problems      : {done_n}/{state['batch_total']}")
                lines.append(f"CPU workers   : {workers}")
                lines.append(f"grid          : {size} x {size}")
                lines.append(f"density       : {density*100:.0f}%")
                lines.append("-" * 26)
                lines.append(f"A* total      : {tot_a*1000:.1f} ms")
                lines.append(f"A+ total      : {tot_b*1000:.1f} ms")
                lines.append(f"avg speedup   : {avg_speedup:.2f}x")
                lines.append(f"cost matches  : {matches}/{done_n}")
                lines.append(f"wall time     : {wall:.2f} s")
                ax_info.text(0.02, 0.98, "\n".join(lines), transform=ax_info.transAxes,
                             va="top", ha="left", family="monospace", fontsize=10.2, color="#c9d3ee")

            fig.canvas.draw_idle()

            if done_n >= state["batch_total"]:
                state["timer"].stop()
                if state["executor"] is not None:
                    state["executor"].shutdown(wait=False)
                    state["executor"] = None
                state["mode"] = None
                progress_txt.set_text(f"Batch finished: {done_n} problems solved using {workers} worker(s).")

        timer = fig.canvas.new_timer(interval=120)
        timer.add_callback(tick)
        state["timer"] = timer
        timer.start()

    btn_race.on_clicked(start_race)
    btn_batch.on_clicked(start_batch)
    btn_stop.on_clicked(lambda e: stop_everything())

    start_race()  # show something immediately on launch
    plt.show()


if __name__ == "__main__":
    run_app()
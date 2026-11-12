#!/usr/bin/env python3
"""CORRIDOR 404 - a turn-based raycast shooter that lives in a GitHub profile README.

Each move is a GitHub issue titled "game|<action>". A workflow runs this script,
which updates game/state.json, renders game/frames/<run>-<turn>.svg and rewrites the game
section of README.md.

Usage:
  python game/engine.py                     # reads GAME_TITLE / GAME_USER env (Actions)
  python game/engine.py forward [user]      # play one move locally
  python game/engine.py --reset             # wipe everything and start fresh
  python game/engine.py --render            # redraw the current frame + README
"""
import json
import math
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
FRAMES_DIR = ROOT / "frames"  # one file per turn so GitHub's image cache never shows a stale frame
README_FILE = ROOT.parent / "README.md"

START, END = "<!-- GAME:START -->", "<!-- GAME:END -->"

# P = player start, e = enemy, h = health, a = ammo
MAP = [
    "################",
    "#P..#......#...#",
    "#...#..e...#.a.#",
    "#...#......#...#",
    "#.......####.e.#",
    "##.###.........#",
    "#....#..e..#####",
    "#.h..#.....#...#",
    "#....###.###.e.#",
    "#..e.........h.#",
    "#....#.#.#.#...#",
    "####.#.....#####",
    "#a...#..e..#...#",
    "#....#.....e.e.#",
    "#..........#...#",
    "################",
]
assert all(len(r) == len(MAP[0]) for r in MAP)

ACTIONS = {
    "forward": "⬆️ Forward",
    "back": "⬇️ Back",
    "turn-left": "↩️ Turn left",
    "turn-right": "↪️ Turn right",
    "shoot": "🔫 Shoot",
    "new": "🔄 New game",
}
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]  # 45° steps, y down

W, H, HUD_H = 640, 360, 64
COLS = 160
FOV = math.radians(66)
ENEMY_HP = 2


# ---------------------------------------------------------------- state

def is_wall(x, y):
    return not (0 <= y < len(MAP) and 0 <= x < len(MAP[0])) or MAP[y][x] == "#"


def fresh_game(meta=None):
    meta = meta or {"seed": random.randrange(1 << 30), "players": {}, "wins": 0, "losses": 0,
                    "best": None, "game_no": 0, "log": []}
    s = dict(meta)
    s["game_no"] = meta.get("game_no", 0) + 1
    s["seed"] = meta.get("seed", 0) + 7919
    s.update(turn=0, status="playing", kills=0, score=0, enemies=[], items=[],
             last={"shot": False, "hurt": False}, message="A new run begins. Something growls in the dark.")
    for y, row in enumerate(MAP):
        for x, c in enumerate(row):
            if c == "P":
                s["player"] = {"x": x, "y": y, "dir": 0, "hp": 100, "ammo": 20}
            elif c == "e":
                s["enemies"].append({"x": x, "y": y, "hp": ENEMY_HP})
            elif c in "ha":
                s["items"].append({"x": x, "y": y, "kind": "health" if c == "h" else "ammo"})
    s["total"] = len(s["enemies"])
    return s


META_KEYS = ("seed", "players", "wins", "losses", "best", "game_no", "log")


def load():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return fresh_game()


def save(s):
    STATE_FILE.write_text(json.dumps(s, indent=1) + "\n")


# ---------------------------------------------------------------- rules

def occupied(s, x, y, ignore=None):
    if s["player"]["x"] == x and s["player"]["y"] == y:
        return True
    return any(e is not ignore and e["hp"] > 0 and e["x"] == x and e["y"] == y for e in s["enemies"])


def can_step(x, y, dx, dy):
    if is_wall(x + dx, y + dy):
        return False
    if dx and dy and (is_wall(x + dx, y) or is_wall(x, y + dy)):
        return False  # no corner cutting
    return True


def line_of_sight(ax, ay, bx, by):
    ax, ay, bx, by = ax + .5, ay + .5, bx + .5, by + .5
    n = int(math.hypot(bx - ax, by - ay) * 8) + 1
    for i in range(1, n):
        t = i / n
        if is_wall(int(ax + (bx - ax) * t), int(ay + (by - ay) * t)):
            return False
    return True


def player_angle(p):
    return p["dir"] * math.pi / 4


def project(p, ex, ey):
    """Return (screen_x, perp_dist, angle_diff) of a world point (cell centre)."""
    rx, ry = ex + .5 - (p["x"] + .5), ey + .5 - (p["y"] + .5)
    diff = (math.atan2(ry, rx) - player_angle(p) + math.pi) % (2 * math.pi) - math.pi
    dist = math.hypot(rx, ry)
    perp = dist * math.cos(diff)
    sx = W / 2 + math.tan(diff) / math.tan(FOV / 2) * W / 2 if abs(diff) < math.pi / 2 else -1e9
    return sx, perp, diff, dist


def do_shoot(s):
    p = s["player"]
    if p["ammo"] <= 0:
        return "*click* Out of ammo!"
    p["ammo"] -= 1
    s["last"]["shot"] = True
    best = None
    for e in s["enemies"]:
        if e["hp"] <= 0:
            continue
        _, perp, diff, dist = project(p, e["x"], e["y"])
        if perp <= 0 or abs(diff) > max(math.atan(0.45 / dist), math.radians(9)):
            continue
        if not line_of_sight(p["x"], p["y"], e["x"], e["y"]):
            continue
        if best is None or dist < best[0]:
            best = (dist, e)
    if not best:
        return "BLAM! The shot hits nothing but wall."
    dist, e = best
    dmg = 2 if dist < 2.5 else 1
    e["hp"] -= dmg
    if e["hp"] <= 0:
        s["kills"] += 1
        s["score"] += 100
        return "BLAM! A grunt goes down. (+100)"
    return "BLAM! Hit a grunt. It's still standing..."


def do_move(s, sign):
    p = s["player"]
    dx, dy = DIRS[p["dir"]]
    dx, dy = dx * sign, dy * sign
    if not can_step(p["x"], p["y"], dx, dy):
        return "Bonk. There's a wall there."
    if occupied(s, p["x"] + dx, p["y"] + dy):
        return "A grunt is blocking the way!"
    p["x"] += dx
    p["y"] += dy
    for it in list(s["items"]):
        if it["x"] == p["x"] and it["y"] == p["y"]:
            s["items"].remove(it)
            s["score"] += 10
            if it["kind"] == "health":
                p["hp"] = min(100, p["hp"] + 25)
                return "Picked up a medkit. (+25 HP)"
            p["ammo"] += 8
            return "Picked up a box of shells. (+8 ammo)"
    return "Footsteps echo down the corridor." if sign > 0 else "You back away carefully."


def enemies_act(s, rng):
    p = s["player"]
    notes = []
    for e in s["enemies"]:
        if e["hp"] <= 0:
            continue
        d = math.hypot(e["x"] - p["x"], e["y"] - p["y"])
        los = line_of_sight(e["x"], e["y"], p["x"], p["y"])
        if d < 1.6:
            if rng.random() < 0.7:
                dmg = rng.randint(8, 14)
                p["hp"] -= dmg
                notes.append(f"A grunt claws you (-{dmg})")
            continue
        if los and d <= 8 and rng.random() < 0.3:
            if rng.random() < 0.55 - d * 0.04:
                dmg = rng.randint(4, 9)
                p["hp"] -= dmg
                notes.append(f"A fireball hits you (-{dmg})")
            continue
        if (los and d <= 8) or d <= 4:
            best, bd = None, d
            for dx, dy in DIRS:
                nx, ny = e["x"] + dx, e["y"] + dy
                if can_step(e["x"], e["y"], dx, dy) and not occupied(s, nx, ny, ignore=e):
                    nd = math.hypot(nx - p["x"], ny - p["y"])
                    if nd < bd - 1e-9:
                        best, bd = (nx, ny), nd
            if best:
                e["x"], e["y"] = best
    if notes:
        s["last"]["hurt"] = True
    return notes


def apply(s, action, user):
    """Apply one action. Returns the message to report."""
    if action == "new":
        if s["status"] == "playing" and s["turn"] > 0 and s["player"]["hp"] > 0:
            s["losses"] += 1  # abandoning a run counts as a loss
        meta = {k: s[k] for k in META_KEYS}
        s.clear()
        s.update(fresh_game(meta))
        msg = f"@{user} started run #{s['game_no']}."
        log(s, user, action, msg)
        return msg
    if s["status"] != "playing":
        return "This run is over. Start a new game!"

    s["last"] = {"shot": False, "hurt": False}
    p = s["player"]
    rng = random.Random(s["seed"] * 1000 + s["turn"])
    if action == "turn-left":
        p["dir"] = (p["dir"] - 1) % 8
        msg = "You turn left."
    elif action == "turn-right":
        p["dir"] = (p["dir"] + 1) % 8
        msg = "You turn right."
    elif action == "forward":
        msg = do_move(s, 1)
    elif action == "back":
        msg = do_move(s, -1)
    else:
        msg = do_shoot(s)
    s["turn"] += 1

    if s["kills"] == s["total"]:
        bonus = max(0, 1000 - s["turn"] * 5)
        s["score"] += bonus
        s["status"] = "won"
        s["wins"] += 1
        if not s["best"] or s["score"] > s["best"]["score"]:
            s["best"] = {"score": s["score"], "turns": s["turn"], "game": s["game_no"]}
        msg += f" SECTOR CLEARED in {s['turn']} turns! (+{bonus} bonus)"
    else:
        notes = enemies_act(s, rng)
        if notes:
            msg += " " + ". ".join(notes) + "."
        if p["hp"] <= 0:
            p["hp"] = 0
            s["status"] = "dead"
            s["losses"] += 1
            msg += " YOU DIED."
    s["message"] = msg
    log(s, user, action, msg)
    return msg


def log(s, user, action, msg):
    s["players"][user] = s["players"].get(user, 0) + 1
    s["log"] = ([{"turn": s["turn"], "user": user, "action": action, "msg": msg}] + s["log"])[:8]
    s["message"] = msg


# ---------------------------------------------------------------- render

def hexc(r, g, b):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in (r, g, b))


def xml(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def cast(px, py, ang):
    dx, dy = math.cos(ang), math.sin(ang)
    mx, my = int(px), int(py)
    ddx = abs(1 / dx) if abs(dx) > 1e-9 else 1e30
    ddy = abs(1 / dy) if abs(dy) > 1e-9 else 1e30
    sx, sdx = (-1, (px - mx) * ddx) if dx < 0 else (1, (mx + 1 - px) * ddx)
    sy, sdy = (-1, (py - my) * ddy) if dy < 0 else (1, (my + 1 - py) * ddy)
    side = 0
    for _ in range(64):
        if sdx < sdy:
            sdx += ddx; mx += sx; side = 0
        else:
            sdy += ddy; my += sy; side = 1
        if is_wall(mx, my):
            break
    dist = sdx - ddx if side == 0 else sdy - ddy
    hit = (py + dist * dy) if side == 0 else (px + dist * dx)
    return dist, side, mx, my, hit - math.floor(hit)


def enemy_svg(cx, fy, sc, hurt):
    body = "#b83a2e" if hurt else "#7d1f3a"
    return f'''<g transform="translate({cx:.1f},{fy:.1f}) scale({sc:.3f})">
<ellipse cx="0" cy="0" rx="32" ry="6" fill="#000" opacity=".4"/>
<rect x="-20" y="-30" width="12" height="30" rx="4" fill="#3a0d1c"/><rect x="8" y="-30" width="12" height="30" rx="4" fill="#3a0d1c"/>
<ellipse cx="-33" cy="-52" rx="8" ry="17" fill="{body}"/><ellipse cx="33" cy="-52" rx="8" ry="17" fill="{body}"/>
<ellipse cx="0" cy="-56" rx="30" ry="32" fill="{body}"/>
<polygon points="-18,-82 -30,-106 -8,-86" fill="#e8d8b0"/><polygon points="18,-82 30,-106 8,-86" fill="#e8d8b0"/>
<circle cx="-11" cy="-63" r="6" fill="#ffe600"/><circle cx="11" cy="-63" r="6" fill="#ffe600"/>
<circle cx="-11" cy="-63" r="2.5" fill="#300"/><circle cx="11" cy="-63" r="2.5" fill="#300"/>
<polygon points="-15,-45 15,-45 0,-34" fill="#1e0008"/>
<polygon points="-11,-45 -7,-45 -9,-40" fill="#fff"/><polygon points="7,-45 11,-45 9,-40" fill="#fff"/>
</g>'''


def item_svg(cx, fy, sc, kind):
    if kind == "health":
        inner = ('<rect x="-16" y="-24" width="32" height="24" rx="3" fill="#eee" stroke="#999" stroke-width="2"/>'
                 '<rect x="-3" y="-21" width="6" height="18" fill="#d22"/><rect x="-9" y="-15" width="18" height="6" fill="#d22"/>')
    else:
        inner = ('<rect x="-17" y="-20" width="34" height="20" rx="2" fill="#5d5d22" stroke="#33330f" stroke-width="2"/>'
                 '<rect x="-12" y="-30" width="6" height="14" rx="2" fill="#d9a43a"/><rect x="-3" y="-30" width="6" height="14" rx="2" fill="#d9a43a"/>'
                 '<rect x="6" y="-30" width="6" height="14" rx="2" fill="#d9a43a"/>')
    return f'<g transform="translate({cx:.1f},{fy:.1f}) scale({sc:.3f})"><ellipse cx="0" cy="0" rx="20" ry="4" fill="#000" opacity=".35"/>{inner}</g>'


def render(s):
    p = s["player"]
    px, py, pa = p["x"] + .5, p["y"] + .5, player_angle(p)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H + HUD_H}" viewBox="0 0 {W} {H + HUD_H}" font-family="Courier New, monospace">',
           '<defs><linearGradient id="ceil" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1b1b24"/><stop offset="1" stop-color="#05050a"/></linearGradient>'
           '<linearGradient id="floor" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0d0a08"/><stop offset="1" stop-color="#4a3a2c"/></linearGradient>'
           f'<clipPath id="view"><rect width="{W}" height="{H}"/></clipPath></defs>',
           '<g clip-path="url(#view)">',
           f'<rect width="{W}" height="{H / 2}" fill="url(#ceil)"/><rect y="{H / 2}" width="{W}" height="{H / 2}" fill="url(#floor)"/>']

    # walls
    cw = W / COLS
    zbuf = []
    for i in range(COLS):
        camx = 2 * (i + .5) / COLS - 1
        ang = pa + math.atan(camx * math.tan(FOV / 2))
        dist, side, mx, my, u = cast(px, py, ang)
        perp = max(0.05, dist * math.cos(ang - pa))
        zbuf.append(perp)
        h = min(H * 3, H / perp)
        base = (128, 112, 96) if (mx * 3 + my) % 4 else (96, 104, 124)
        k = (0.72 if side else 1.0) * max(0.12, 1 - perp / 9)
        if u < 0.05 or u > 0.95:
            k *= 0.55
        out.append(f'<rect x="{i * cw:.1f}" y="{(H - h) / 2:.1f}" width="{cw + .6:.1f}" height="{h:.1f}" fill="{hexc(*(c * k for c in base))}"/>')

    # sprites, far to near
    sprites = []
    for e in s["enemies"]:
        if e["hp"] > 0:
            sprites.append((e["x"], e["y"], "enemy", e["hp"] < ENEMY_HP))
    for it in s["items"]:
        sprites.append((it["x"], it["y"], it["kind"], False))
    drawn = []
    for x, y, kind, hurt in sprites:
        sx, perp, diff, _ = project(p, x, y)
        if perp < 0.3 or not (-60 < sx < W + 60):
            continue
        col = int(min(COLS - 1, max(0, sx / cw)))
        if zbuf[col] < perp - 0.3:
            continue
        drawn.append((perp, sx, kind, hurt))
    for perp, sx, kind, hurt in sorted(drawn, reverse=True):
        fy = H / 2 + (H / perp) / 2
        if kind == "enemy":
            out.append(enemy_svg(sx, fy, (H / perp) * 0.78 / 106, hurt))
        else:
            out.append(item_svg(sx, fy, (H / perp) / 100, kind))


    # crosshair
    cx, cy = W / 2, H / 2
    out.append(f'<g stroke="#7CFC00" stroke-width="2" opacity=".8"><line x1="{cx - 12}" y1="{cy}" x2="{cx - 4}" y2="{cy}"/><line x1="{cx + 4}" y1="{cy}" x2="{cx + 12}" y2="{cy}"/>'
               f'<line x1="{cx}" y1="{cy - 12}" x2="{cx}" y2="{cy - 4}"/><line x1="{cx}" y1="{cy + 4}" x2="{cx}" y2="{cy + 12}"/></g>')

    # weapon
    if s["last"].get("shot"):
        out.append(f'<polygon points="{cx},{H - 175} {cx + 14},{H - 140} {cx + 42},{H - 150} {cx + 20},{H - 122} {cx + 34},{H - 98} {cx},{H - 112} '
                   f'{cx - 34},{H - 98} {cx - 20},{H - 122} {cx - 42},{H - 150} {cx - 14},{H - 140}" fill="#ffd23f"/>'
                   f'<circle cx="{cx}" cy="{H - 128}" r="14" fill="#fff6c0"/>')
    out.append(f'<polygon points="{cx - 46},{H} {cx - 24},{H - 104} {cx + 24},{H - 104} {cx + 46},{H}" fill="#2c2c30" stroke="#111" stroke-width="2"/>'
               f'<rect x="{cx - 11}" y="{H - 122}" width="22" height="30" fill="#4a4a52" stroke="#111" stroke-width="2"/>'
               f'<rect x="{cx - 4}" y="{H - 122}" width="8" height="6" fill="#000"/>'
               f'<rect x="{cx - 24}" y="{H - 70}" width="48" height="8" fill="#56565e"/>'
               f'<ellipse cx="{cx - 40}" cy="{H - 10}" rx="26" ry="20" fill="#5a3b22"/><ellipse cx="{cx + 40}" cy="{H - 10}" rx="26" ry="20" fill="#5a3b22"/>')

    # minimap
    ms, ox, oy = 5, W - len(MAP[0]) * 5 - 10, 10
    mm = [f'<rect x="{ox - 3}" y="{oy - 3}" width="{len(MAP[0]) * ms + 6}" height="{len(MAP) * ms + 6}" fill="#000" opacity=".55"/>']
    for y, row in enumerate(MAP):
        for x, c in enumerate(row):
            if c == "#":
                mm.append(f'<rect x="{ox + x * ms}" y="{oy + y * ms}" width="{ms}" height="{ms}" fill="#8a8a8a" opacity=".7"/>')
    for it in s["items"]:
        mm.append(f'<circle cx="{ox + it["x"] * ms + 2.5}" cy="{oy + it["y"] * ms + 2.5}" r="1.6" fill="{"#4f4" if it["kind"] == "health" else "#fc3"}"/>')
    for e in s["enemies"]:
        if e["hp"] > 0:
            mm.append(f'<circle cx="{ox + e["x"] * ms + 2.5}" cy="{oy + e["y"] * ms + 2.5}" r="2" fill="#f33"/>')
    dx, dy = math.cos(pa), math.sin(pa)
    pcx, pcy = ox + px * ms, oy + py * ms
    mm.append(f'<line x1="{pcx}" y1="{pcy}" x2="{pcx + dx * 8:.1f}" y2="{pcy + dy * 8:.1f}" stroke="#7CFC00" stroke-width="1.5"/>'
              f'<circle cx="{pcx}" cy="{pcy}" r="2.4" fill="#7CFC00"/>')
    out += mm

    out.append(f'<text x="12" y="24" font-size="16" font-weight="bold" fill="#e33" letter-spacing="3">CORRIDOR 404</text>'
               f'<text x="12" y="40" font-size="11" fill="#aaa">run #{s["game_no"]} · turn {s["turn"]}</text>')

    if s["last"].get("hurt"):
        out.append(f'<rect width="{W}" height="{H}" fill="#f00" opacity=".22"/>')
    if s["status"] == "dead":
        out.append(f'<rect width="{W}" height="{H}" fill="#600" opacity=".6"/>'
                   f'<text x="{W / 2}" y="{H / 2 - 10}" text-anchor="middle" font-size="54" font-weight="bold" fill="#f22" stroke="#000" stroke-width="2">YOU DIED</text>'
                   f'<text x="{W / 2}" y="{H / 2 + 24}" text-anchor="middle" font-size="15" fill="#fff">click 🔄 New game below</text>')
    elif s["status"] == "won":
        out.append(f'<rect width="{W}" height="{H}" fill="#031" opacity=".55"/>'
                   f'<text x="{W / 2}" y="{H / 2 - 10}" text-anchor="middle" font-size="44" font-weight="bold" fill="#5f5" stroke="#000" stroke-width="2">SECTOR CLEARED</text>'
                   f'<text x="{W / 2}" y="{H / 2 + 24}" text-anchor="middle" font-size="15" fill="#fff">score {s["score"]} in {s["turn"]} turns</text>')
    out.append('</g>')

    # HUD
    hp = p["hp"]
    hpc = "#5f5" if hp > 60 else "#fc3" if hp > 30 else "#f33"
    face = ":)" if hp > 60 else ":|" if hp > 30 else ":("
    if s["status"] == "dead":
        face = "x_x"
    y0 = H
    out.append(f'<rect y="{y0}" width="{W}" height="{HUD_H}" fill="#3b3b3f"/><rect y="{y0}" width="{W}" height="3" fill="#77777d"/>')
    cells = [("HEALTH", f"{hp}%", hpc), ("AMMO", str(p["ammo"]), "#fc3"),
             ("KILLS", f'{s["kills"]}/{s["total"]}', "#f66"), ("SCORE", str(s["score"]), "#ddd")]
    for i, (label, val, col) in enumerate(cells):
        x = 20 + i * 110 + (60 if i >= 2 else 0)
        out.append(f'<text x="{x}" y="{y0 + 24}" font-size="11" fill="#aaa">{label}</text>'
                   f'<text x="{x}" y="{y0 + 50}" font-size="26" font-weight="bold" fill="{col}">{xml(val)}</text>')
    out.append(f'<rect x="{240}" y="{y0 + 8}" width="50" height="50" rx="6" fill="#c9a27a" stroke="#222" stroke-width="2"/>'
               f'<text x="265" y="{y0 + 41}" text-anchor="middle" font-size="18" font-weight="bold" fill="#222">{face}</text>')
    msg = s["message"]
    if len(msg) > 80:
        msg = msg[:79] + "…"
    out.append(f'<rect x="0" y="{y0 - 22}" width="{W}" height="22" fill="#000" opacity=".55"/>'
               f'<text x="12" y="{y0 - 7}" font-size="12" fill="#eee">{xml(msg)}</text>')
    out.append('</svg>')
    return "\n".join(out)


# ---------------------------------------------------------------- readme

def issue_link(repo, action):
    body = "Just+press+%22Create%22+and+wait+about+a+minute+-+the+README+updates+itself."
    return f"https://github.com/{repo}/issues/new?title=game%7C{action}&body={body}"


def frame_name(s):
    return f'{s["game_no"]}-{s["turn"]}.svg'


def readme_section(s, repo):
    L = lambda a: f"[{ACTIONS[a]}]({issue_link(repo, a)})"
    lines = [START, "", '<div align="center">', "",
             f'<img src="game/frames/{frame_name(s)}" alt="CORRIDOR 404 game view" width="640">', "", "</div>", ""]
    if s["status"] == "playing":
        lines += ["|  |  |  |", "|:-:|:-:|:-:|",
                  f'| {L("turn-left")} | {L("forward")} | {L("turn-right")} |',
                  f'| | {L("back")} | {L("shoot")} |', ""]
    lines += [f'<p align="center"><a href="{issue_link(repo, "new")}">{ACTIONS["new"]}</a></p>', "",
              f'**Last move:** {xml(s["message"])}', "",
              "<details><summary>How to play · recent moves · top players</summary>", "",
              "Every move is a GitHub issue. Click an action, hit **Create**, and a GitHub Action "
              "plays your move and redraws the screen (takes ~30-60s, then refresh). "
              "Anyone can play, and all moves go into one shared run. Kill every grunt to clear the sector. "
              "Grunts claw you up close and throw fireballs from afar. Shots do double damage at close range.", "",
              "| Turn | Player | Move | Result |", "|---|---|---|---|"]
    for e in s["log"]:
        lines.append(f'| {e["turn"]} | @{e["user"]} | {e["action"]} | {xml(e["msg"])} |')
    top = sorted(s["players"].items(), key=lambda kv: -kv[1])[:5]
    lines += ["", "**Top players:** " + (", ".join(f"@{u} ({n})" for u, n in top) or "nobody yet. Be the first!"), "",
              f'**Runs:** {s["wins"]} cleared · {s["losses"]} lost'
              + (f' · best score {s["best"]["score"]} (run #{s["best"]["game"]}, {s["best"]["turns"]} turns)' if s["best"] else ""),
              "", "</details>", "", END]
    return "\n".join(lines)


def write_readme(s, repo):
    text = README_FILE.read_text() if README_FILE.exists() else f"# Hi there 👋\n\n{START}\n{END}\n"
    section = readme_section(s, repo)
    if START in text and END in text:
        text = re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: section, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n" + section + "\n"
    README_FILE.write_text(text)


# ---------------------------------------------------------------- main

def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "tomdatbenik/tomdatbenik")
    args = sys.argv[1:]
    if args == ["--reset"]:
        s = fresh_game()
        s["game_no"] = 1
    elif args == ["--render"]:
        s = load()
    else:
        if args:
            title, user = "game|" + args[0], (args[1] if len(args) > 1 else "local")
        else:
            title, user = os.environ.get("GAME_TITLE", ""), os.environ.get("GAME_USER", "anonymous")
        m = re.fullmatch(r"\s*game\|([a-z-]+)\s*", title.lower())
        if not m or m.group(1) not in ACTIONS:
            print("Unknown move. Valid moves: " + ", ".join(f"`game|{a}`" for a in ACTIONS))
            return
        if not re.fullmatch(r"[A-Za-z0-9-]+(\[bot\])?", user):
            user = "anonymous"
        s = load()
        print(apply(s, m.group(1), user))
    save(s)
    FRAMES_DIR.mkdir(exist_ok=True)
    for old in FRAMES_DIR.glob("*.svg"):
        old.unlink()
    (FRAMES_DIR / frame_name(s)).write_text(render(s))
    write_readme(s, repo)


if __name__ == "__main__":
    main()

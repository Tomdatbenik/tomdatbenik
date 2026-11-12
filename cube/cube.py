#!/usr/bin/env python3
"""CUBE - a shared 3x3 twisty-cube puzzle that lives in a GitHub profile README.

Each move is a GitHub issue titled "cube|<moves>", e.g. "cube|R", "cube|U'" or
"cube|R U R' U'" (up to 20 moves), or "cube|scramble" to start a new attempt.

Usage:
  python cube/cube.py                  # reads GAME_TITLE / GAME_USER env (Actions)
  python cube/cube.py "R U R'" [user]  # play locally
  python cube/cube.py --reset          # solved cube, wipe stats
  python cube/cube.py --render         # redraw the current frame + README
"""
import json
import os
import random
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
FRAMES_DIR = ROOT / "frames"  # one file per version so GitHub's image cache never shows a stale frame
README_FILE = ROOT.parent / "README.md"
START, END = "<!-- CUBE:START -->", "<!-- CUBE:END -->"

# x = right, y = up, z = towards the viewer
FACES = {"U": (0, 1, 0), "D": (0, -1, 0), "R": (1, 0, 0), "L": (-1, 0, 0), "F": (0, 0, 1), "B": (0, 0, -1)}
COLORS = {"U": "#f4f4f4", "D": "#ffd500", "F": "#009b48", "B": "#0046ad", "R": "#c41e3a", "L": "#ff6a00"}
NAMES = {"U": "Up", "D": "Down", "F": "Front", "B": "Back", "R": "Right", "L": "Left"}
MOVE_RE = re.compile(r"[UDLRFB]('|2)?")
MAX_MOVES = 20


# ---------------------------------------------------------------- cube model

def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def rot(v, n, prime):
    """Quarter turn of v about outward face normal n: clockwise as seen from that face, or anticlockwise if prime."""
    c, d, s = cross(n, v), dot(n, v), (1 if prime else -1)
    return [s * c[i] + n[i] * d for i in range(3)]


def solved_stickers():
    out = []
    for x in (-1, 0, 1):
        for y in (-1, 0, 1):
            for z in (-1, 0, 1):
                for face, n in FACES.items():
                    if dot((x, y, z), n) == 1:
                        out.append([[x, y, z], list(n), face])
    return out  # 54 stickers: [cubie position, sticker normal, colour]


def turn(stickers, move):
    n = FACES[move[0]]
    prime = move.endswith("'")
    for _ in range(2 if move.endswith("2") else 1):
        for st in stickers:
            if dot(st[0], n) == 1:
                st[0], st[1] = rot(st[0], n, prime), rot(st[1], n, prime)


def is_solved(stickers):
    faces = {}
    for _, n, c in stickers:
        faces.setdefault(tuple(n), set()).add(c)
    return all(len(v) == 1 for v in faces.values())


def make_scramble(rng, length=20):
    moves, last = [], None
    for _ in range(length):
        f = rng.choice([f for f in FACES if f != last])
        moves.append(f + rng.choice(["", "'", "2"]))
        last = f
    return moves


# ---------------------------------------------------------------- state

def fresh():
    return {"stickers": solved_stickers(), "status": "solved", "attempt": 0, "moves": 0, "scramble": "",
            "solves": 0, "best": None, "version": 0, "players": {}, "solvers": {}, "attempt_players": [],
            "log": [], "message": "The cube is solved. Hit Scramble to start an attempt!"}


def load():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else fresh()


def save(s):
    STATE_FILE.write_text(json.dumps(s, separators=(",", ":")) + "\n")


def apply(s, moves, user):
    if moves == "scramble":
        scr = make_scramble(random.Random())
        s["stickers"] = solved_stickers()
        for m in scr:
            turn(s["stickers"], m)
        s.update(status="attempt", attempt=s["attempt"] + 1, moves=0, scramble=" ".join(scr), attempt_players=[])
        msg = f"@{user} scrambled the cube: {s['scramble']}"
        shown = "scramble"
    else:
        for m in moves:
            turn(s["stickers"], m)
        s["moves"] += len(moves)
        if user not in s["attempt_players"]:
            s["attempt_players"].append(user)
        shown = " ".join(moves)
        msg = f"@{user} played {shown}."
        if is_solved(s["stickers"]):
            if s["status"] == "attempt":
                s["solves"] += 1
                for u in s["attempt_players"]:
                    s["solvers"][u] = s["solvers"].get(u, 0) + 1
                if not s["best"] or s["moves"] < s["best"]["moves"]:
                    s["best"] = {"moves": s["moves"], "attempt": s["attempt"], "by": s["attempt_players"]}
                msg += f" 🎉 SOLVED in {s['moves']} moves! Final move by @{user}."
            else:
                msg += " Back to solved (doesn't count: scramble first)."
            s["status"] = "solved"
        elif s["status"] == "solved":
            s["status"] = "free"  # turned a solved cube without scrambling; won't count as a solve
    s["version"] += 1
    s["players"][user] = s["players"].get(user, 0) + 1
    s["log"] = ([{"n": s["moves"], "user": user, "moves": shown}] + s["log"])[:8]
    s["message"] = msg
    return msg


# ---------------------------------------------------------------- render

W, H, S = 640, 340, 46
VIEWS = [  # (transform, centre x, caption)
    (lambda v: (v[0], v[1], v[2]), 170, "FRONT · UP · RIGHT"),
    (lambda v: (-v[2], -v[1], -v[0]), 470, "BACK · DOWN · LEFT"),  # 180° turn about (1,0,-1)
]


def proj(p, cx, cy):
    return (cx + (p[0] - p[2]) * 0.866 * S, cy + (p[0] + p[2]) * 0.5 * S - p[1] * S)


def quad(center, n, half, cx, cy):
    axes = [i for i in range(3) if n[i] == 0]
    e1 = [1 if i == axes[0] else 0 for i in range(3)]
    e2 = [1 if i == axes[1] else 0 for i in range(3)]
    pts = []
    for a, b in ((1, 1), (1, -1), (-1, -1), (-1, 1)):
        p = [center[i] + half * (a * e1[i] + b * e2[i]) for i in range(3)]
        pts.append("%.1f,%.1f" % proj(p, cx, cy))
    return " ".join(pts)


def render(s):
    cy = 168
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Helvetica, Arial, sans-serif">',
           f'<rect width="{W}" height="{H}" rx="14" fill="#161b22"/>']
    for T, cx, caption in VIEWS:
        for n in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):  # black plastic body
            out.append(f'<polygon points="{quad([1.5 * c for c in n], n, 1.5, cx, cy)}" fill="#0b0b0b" stroke="#0b0b0b" stroke-width="3" stroke-linejoin="round"/>')
        for p, n, c in s["stickers"]:
            tp, tn = T(p), T(n)
            if sum(tn) != 1:
                continue  # facing away from this view
            center = [tp[i] + tn[i] * 0.5 for i in range(3)]
            out.append(f'<polygon points="{quad(center, tn, 0.43, cx, cy)}" fill="{COLORS[c]}" stroke="#000" stroke-opacity=".25" stroke-linejoin="round"/>')
            if sum(abs(v) for v in p) == 1:  # centre sticker: label the face
                x, y = proj(center, cx, cy)
                ink = "#222" if c in "UD" else "#fff"
                out.append(f'<text x="{x:.1f}" y="{y + 6:.1f}" text-anchor="middle" font-size="17" font-weight="bold" fill="{ink}" opacity=".85">{c}</text>')
        out.append(f'<text x="{cx}" y="{H - 34}" text-anchor="middle" font-size="11" letter-spacing="2" fill="#8b949e">{caption}</text>')

    if s["status"] == "solved" and s["attempt"]:
        status, col = f"SOLVED · attempt #{s['attempt']} in {s['moves']} moves", "#3fb950"
    elif s["status"] == "solved":
        status, col = "SOLVED · scramble to start", "#3fb950"
    elif s["status"] == "attempt":
        status, col = f"attempt #{s['attempt']} · {s['moves']} moves", "#e3b341"
    else:
        status, col = "free play · scramble to start an attempt", "#8b949e"
    out.append(f'<text x="20" y="30" font-size="15" font-weight="bold" fill="#e6edf3" letter-spacing="2">CUBE</text>'
               f'<text x="{W - 20}" y="30" text-anchor="end" font-size="13" font-weight="bold" fill="{col}">{status}</text>')
    last = s["log"][0]["moves"] if s["log"] else "-"
    if len(last) > 70:
        last = last[:69] + "…"
    out.append(f'<text x="{W / 2}" y="{H - 12}" text-anchor="middle" font-size="12" fill="#c9d1d9">last: {xml(last)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def xml(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------- readme

def issue_link(repo, moves):
    title = urllib.parse.quote("cube|" + moves, safe="")
    body = "Just+press+%22Create%22+and+wait+about+a+minute+-+the+README+updates+itself."
    return f"https://github.com/{repo}/issues/new?title={title}&body={body}"


def frame_name(s):
    return f'{s["version"]}.svg'


def readme_section(s, repo):
    L = lambda m: f"[`{m}`]({issue_link(repo, m)})"
    lines = [START, "", '<div align="center">', "",
             f'<img src="cube/frames/{frame_name(s)}" alt="Cube puzzle, current state" width="640">', "", "</div>", "",
             "| Face | Clockwise | Anticlockwise | Double |", "|:-:|:-:|:-:|:-:|"]
    for f in "UDFBLR":
        lines.append(f"| **{f}** ({NAMES[f]}) | {L(f)} | {L(f + chr(39))} | {L(f + '2')} |")
    lines += ["", f'<p align="center"><a href="{issue_link(repo, "scramble")}">🎲 Scramble</a></p>', "",
              f'**Last move:** {xml(s["message"])}', "",
              "<details><summary>How to play · recent moves · solvers</summary>", "",
              "Standard notation: a letter turns that face a quarter turn clockwise *as you look at that face*, "
              "`'` turns it anticlockwise and `2` turns it twice. The left picture shows the front, top and right faces, and the right "
              "picture shows the cube flipped over to the back, bottom and left. Scramble starts a counted attempt. "
              f"Pro tip: open an issue titled `cube|R U R' U'` to play up to {MAX_MOVES} moves at once.", "",
              "| Moves | Player | Played |", "|---|---|---|"]
    for e in s["log"]:
        lines.append(f'| {e["n"]} | @{e["user"]} | `{e["moves"]}` |')
    top = sorted(s["solvers"].items(), key=lambda kv: -kv[1])[:5]
    lines += ["", "**Solvers:** " + (", ".join(f"@{u} ({n})" for u, n in top) or "nobody yet. Be the first!"), "",
              f'**Solves:** {s["solves"]}'
              + (f' · fewest moves {s["best"]["moves"]} (attempt #{s["best"]["attempt"]})' if s["best"] else ""),
              "", "</details>", "", END]
    return "\n".join(lines)


def write_readme(s, repo):
    text = README_FILE.read_text() if README_FILE.exists() else ""
    section = readme_section(s, repo)
    if START in text and END in text:
        text = re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: section, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n## 🧊 Cube\n\n" + section + "\n"
    README_FILE.write_text(text)


# ---------------------------------------------------------------- main

def parse(body):
    body = body.strip().replace("’", "'").replace("‘", "'").replace("′", "'")
    if body.lower() == "scramble":
        return "scramble"
    tokens = body.split()
    if not tokens or len(tokens) > MAX_MOVES or not all(MOVE_RE.fullmatch(t) for t in tokens):
        return None
    return tokens


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "tomdatbenik/tomdatbenik")
    args = sys.argv[1:]
    if args == ["--reset"]:
        s = fresh()
    elif args == ["--render"]:
        s = load()
    else:
        if args:
            title, user = "cube|" + args[0], (args[1] if len(args) > 1 else "local")
        else:
            title, user = os.environ.get("GAME_TITLE", ""), os.environ.get("GAME_USER", "anonymous")
        m = re.fullmatch(r"\s*cube\|(.*)", title, flags=re.S | re.I)
        moves = parse(m.group(1)) if m else None
        if not moves:
            print(f"Couldn't read that. Use moves like `cube|R`, `cube|U'`, `cube|F2`, up to {MAX_MOVES} "
                  "separated by spaces (`cube|R U R' U'`), or `cube|scramble`.")
            return
        if not re.fullmatch(r"[A-Za-z0-9-]+(\[bot\])?", user):
            user = "anonymous"
        s = load()
        print(apply(s, moves, user))
    save(s)
    FRAMES_DIR.mkdir(exist_ok=True)
    for old in FRAMES_DIR.glob("*.svg"):
        old.unlink()
    (FRAMES_DIR / frame_name(s)).write_text(render(s))
    write_readme(s, repo)


if __name__ == "__main__":
    main()

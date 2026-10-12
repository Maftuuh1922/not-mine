#!/usr/bin/env python3
"""Record a real notmine session and render it as an animated terminal SVG.

Stdlib only. Everything shown is the CLI's actual output from this run; the only
edits are cosmetic: the throwaway sandbox path is shown as `~/demo`, and long
evidence lists are trimmed to their first lines (marked with `...`).

    python3 tools/make_demo_svg.py            # writes assets/demo.svg
"""

import html
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "scripts", "notmine.py")
OUT = os.path.join(ROOT, "assets", "demo.svg")


def free_port(pref):
    for p in (pref, 0):
        c = socket.socket()
        c.settimeout(0.3)
        busy = p and c.connect_ex(("127.0.0.1", p)) == 0
        c.close()
        if busy:
            continue
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
    raise RuntimeError("no port")


def serve(cwd, port, env):
    p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=cwd,
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        c = socket.socket()
        if c.connect_ex(("127.0.0.1", port)) == 0:
            c.close()
            return p
        c.close()
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def record():
    root = os.path.realpath(tempfile.mkdtemp(prefix="nmdemo-"))
    ws, prod = os.path.join(root, "shop"), os.path.join(root, "srv", "billing")
    os.makedirs(ws)
    os.makedirs(prod)
    env = dict(os.environ, NOTMINE_HOME=os.path.join(root, "state"), HOME=root,
               XDG_CONFIG_HOME=os.path.join(root, ".config"))
    procs, steps = [], []
    try:
        pport, mport = free_port(8000), free_port(5173)
        procs.append(serve(prod, pport, env))          # someone else's "production" app
        time.sleep(1.2)

        def nm(*args, keep=5):
            r = subprocess.run([sys.executable, CLI] + list(args), cwd=ws, env=env, capture_output=True, text=True)
            lines = r.stdout.replace(root + "/shop", "~/demo/shop").replace(root, "~/demo").rstrip().splitlines()
            if len(lines) > keep:
                lines = lines[:keep - 1] + ["  ...", lines[-1]] if lines[-1].startswith("  =>") else lines[:keep] + ["  ..."]
            lines.append("[exit %d]" % r.returncode)
            steps.append(("notmine " + " ".join(args), lines))

        nm("begin", "--no-auto-root", "--watch-ports", str(pport), keep=3)
        procs.append(serve(ws, mport, env))               # the agent's own dev server
        nm("check", "port", str(pport), keep=8)
        nm("check", "port", str(mport), keep=3)
        nm("check", "db", "postgres://admin:hunter2@prod-db.internal/shop", keep=4)
        nm("kill", ":%d" % pport, keep=2)
    finally:
        for p in procs:
            p.kill()
        shutil.rmtree(root, ignore_errors=True)
    return steps


def color(line):
    if line.startswith("NOT MINE") or line.startswith("UNKNOWN"):
        return "#ff5c5c"
    if line.startswith("MINE"):
        return "#7ee08a"
    if line.startswith("[exit"):
        return "#ff5c5c" if line != "[exit 0]" else "#7ee08a"
    if line.startswith("  =>"):
        return "#ffb454"
    return "#aab2c5"


def render(steps):
    W, LH, PAD, FS = 900, 19, 18, 13.5
    rows = []  # (text, color, start_time, typed)
    t = 0.4
    for cmd, out in steps:
        rows.append(("$ " + cmd, "#e6e9f2", t, True))
        t += 0.25 + 0.022 * len(cmd)
        for line in out:
            rows.append((line, color(line), t, False))
            t += 0.06
        t += 0.7
    total = t + 2.5
    H = PAD * 2 + 34 + LH * len(rows)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
        'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="%s">' % (W, H, W, H, FS),
        "<style>",
        ".r{opacity:0;animation:show %.2fs steps(1) infinite}" % total,
        "@keyframes show{0%{opacity:0}100%{opacity:1}}",
        "</style>",
        '<rect width="%d" height="%d" rx="10" fill="#0b0e17"/>' % (W, H),
        '<rect width="%d" height="30" rx="10" fill="#151a28"/><rect y="20" width="%d" height="10" fill="#151a28"/>' % (W, W),
        '<circle cx="20" cy="15" r="6" fill="#ff5f57"/><circle cx="40" cy="15" r="6" fill="#febc2e"/>'
        '<circle cx="60" cy="15" r="6" fill="#28c840"/>',
        '<text x="%d" y="20" fill="#6b7389" text-anchor="middle" font-size="12">notmine: real session, recorded by tools/make_demo_svg.py</text>' % (W // 2),
    ]
    for i, (text, col, start, typed) in enumerate(rows):
        y = 30 + PAD + LH * (i + 1)
        # each row: hidden until `start`, visible until the loop restarts
        a, b = start / total * 100, 99.5
        name = "k%d" % i
        parts.append("<style>@keyframes %s{0%%,%.2f%%{opacity:0}%.2f%%,%.2f%%{opacity:1}100%%{opacity:0}}"
                     ".%s{animation:%s %.2fs linear infinite}</style>" % (name, a, a + 0.01, b, name, name, total))
        weight = ' font-weight="bold"' if typed or col in ("#ff5c5c", "#7ee08a") else ""
        parts.append('<text class="%s" x="%d" y="%d" fill="%s"%s xml:space="preserve">%s</text>' % (
            name, PAD, y, col, weight, html.escape(text[:118])))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main():
    steps = record()
    svg = render(steps)
    with open(OUT, "w") as fh:
        fh.write(svg)
    for cmd, out in steps:
        print("$ " + cmd)
        print("\n".join(out))
    print("wrote", OUT)


if __name__ == "__main__":
    main()

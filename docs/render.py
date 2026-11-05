"""Render docs/graph.png and docs/banner.png from the test fixture.

The Desktop page draws the same three-column layout with the app's theme; this is the
same graph rendered with Pillow so the README has a picture before the first screenshot.
Run: python docs/render.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FIX = ROOT.parent / "tests-js" / "fixtures" / "graph.json"
FONT_DIR = Path("/usr/share/fonts/opentype/inter")


def font(size: int, weight: str = "Regular"):
    for cand in (FONT_DIR / f"Inter-{weight}.otf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if cand.exists():
            return ImageFont.truetype(str(cand), size)
    return ImageFont.load_default()


BG = (14, 15, 18)
PANEL = (22, 24, 29)
STROKE = (58, 62, 72)
TEXT = (235, 237, 240)
TEXT2 = (160, 166, 178)
TEXT3 = (104, 110, 122)
ACCENT = (120, 170, 255)
EVID = (200, 205, 214)
INFER = (110, 116, 128)

COL_W, NODE_W, NODE_H, GAP, PAD = 300, 268, 56, 18, 28
SCALE = 2  # render at 2x for crispness


def basename(p):
    return os.path.basename(p or "") or p


def title(n):
    if n["kind"] == "file":
        return basename(n.get("path")) or n.get("label")
    if n["kind"] == "url":
        return (n.get("label") or n.get("url") or "")[:40]
    if n["kind"] == "tool_call":
        return (n.get("meta") or {}).get("tool") or n.get("label")
    return n.get("label") or n["id"]


def sub(n, basis, model):
    m = n.get("meta") or {}
    parts = []
    if basis:
        parts.append({"observed": "●", "evidenced": "◉", "inferred": "○"}[basis] + " " + basis)
    if model:
        parts.append("via " + model)
    if n["kind"] == "file":
        parts.append(("written by " + m.get("producer_tool", "")) if n.get("produced") else "read")
    elif n["kind"] == "url":
        parts.append("page")
    elif n["kind"] == "query":
        parts.append("web search")
    elif n["kind"] == "tool_call":
        parts.append(f"{m.get('status', '')} · {m.get('duration_ms', 0)} ms")
    return " · ".join(p for p in parts if p)


def dataflow_lines(nodes, edges):
    lines = []
    for e in edges:
        if e["rel"] == "read":
            a, b, basis = e["dst"], e["src"], "read"
        elif e["rel"] == "wrote":
            a, b, basis = e["src"], e["dst"], e["basis"]
        elif e["rel"] == "derived_from":
            a, b, basis = e["dst"], e["src"], e["basis"]
        else:
            continue
        if a in nodes and b in nodes and a != b:
            lines.append((a, b, basis))
    return lines


def layout(g, root_id):
    """Four semantic columns — the same arrangement as desktop/plugin.js."""
    all_nodes = {n["id"]: n for n in g["nodes"]}
    nodes = {k: v for k, v in all_nodes.items() if v["kind"] not in ("agent", "turn", "aux_call", "model_call")}
    edges = [e for e in g["edges"] if e["src"] in all_nodes and e["dst"] in all_nodes]
    model_for = {e["dst"]: all_nodes[e["src"]] for e in edges if e["rel"] == "invoked"}
    derived = {e["dst"]: e["basis"] for e in edges if e["src"] == root_id and e["rel"] == "derived_from"}
    lines = dataflow_lines(nodes, edges)
    groups = [[], [], [], []]
    for n in nodes.values():
        if n["id"] == root_id:
            groups[3].append(n)
        elif n["kind"] == "tool_call":
            groups[1].append(n)
        elif n["kind"] == "file" and n.get("produced"):
            groups[2].append(n)
        else:
            groups[0].append(n)
    brank = {"observed": 0, "evidenced": 1, "inferred": 2}
    groups[1].sort(key=lambda n: (n.get("meta") or {}).get("started_at") or 0)
    tool_row = {n["id"]: i for i, n in enumerate(groups[1])}

    def bary(n):
        rows = [tool_row[b] for a, b, _ in lines if a == n["id"] and b in tool_row] + [tool_row[a] for a, b, _ in lines if b == n["id"] and a in tool_row]
        return sum(rows) / len(rows) if rows else 1e6

    for gi in (0, 2):
        groups[gi].sort(key=lambda n: (brank.get(derived.get(n["id"]), 3), bary(n)))
    caps = ["SOURCES", "TOOL CALLS", "INTERMEDIATE", "ARTIFACT"]
    cols, captions = [], []
    for gi, grp in enumerate(groups):
        if grp:
            cols.append(grp)
            captions.append(caps[gi])
    pos = {}
    max_rows = max(len(c) for c in cols)
    height = PAD * 2 + max_rows * (NODE_H + GAP) - GAP + 30
    for ci, col in enumerate(cols):
        col_h = len(col) * (NODE_H + GAP) - GAP
        off = (height - 30 - PAD * 2 - col_h) / 2
        for ri, n in enumerate(col):
            pos[n["id"]] = (PAD + ci * COL_W, PAD + 30 + ri * (NODE_H + GAP) + off)
    width = PAD * 2 + COL_W * (len(cols) - 1) + NODE_W
    reads = {(a, b) for a, b, k in lines if k == "read"}
    wrote = {}
    for a, b, k in lines:
        if k != "read" and any(e["src"] == a and e["dst"] == b and e["rel"] == "wrote" for e in edges):
            wrote.setdefault(b, []).append(a)
    shown = []
    for a, b, k in lines:
        is_derived = any(e["src"] == b and e["dst"] == a and e["rel"] == "derived_from" for e in edges)
        if is_derived and any((a, t) in reads for t in wrote.get(b, [])):
            continue
        if a in pos and b in pos:
            shown.append((a, b, k))
    return nodes, pos, shown, width, height, model_for, derived, captions


def bezier(p0, p1, p2, p3, steps=40):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t**2 * p2[0] + t**3 * p3[0]
        y = (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def draw_graph(g, root_id, out: Path):
    nodes, pos, lines, width, height, model_for, derived, captions = layout(g, root_id)
    S = SCALE
    img = Image.new("RGB", (int(width * S), int(height * S)), BG)
    d = ImageDraw.Draw(img)
    f_cap, f_t = font(10 * S, "Medium"), font(12 * S, "Medium")
    f_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 9 * S)
    for i, cap in enumerate(captions):
        if cap:
            d.text(((PAD + i * COL_W) * S, (PAD - 6) * S), cap, font=f_cap, fill=TEXT3)
    style = {"observed": (ACCENT, 3.0, None), "evidenced": (EVID, 2.0, None), "inferred": (INFER, 1.6, 7), "read": (STROKE, 1.2, None)}
    for li, (a, b, basis) in enumerate(lines):
        ax, ay = pos[a]
        bx, by = pos[b]
        color, w, dash = style[basis]
        p0 = ((ax + NODE_W) * S, (ay + NODE_H / 2) * S)
        p3 = (bx * S, (by + NODE_H / 2) * S)
        if p3[0] <= p0[0]:  # back edge: loop under the boxes
            drop = NODE_H * S
            pts = bezier(p0, (p0[0] + 40 * S, p0[1] + drop), (p3[0] - 40 * S, p3[1] + drop), p3)
        elif p3[0] - p0[0] > COL_W * S:
            bus = (height - 30 - (li % 4) * 4) * S if li % 2 == 0 else (PAD + 18 + (li % 4) * 4) * S
            g1, g2 = p0[0] + 36 * S, p3[0] - 36 * S
            pts = bezier(p0, (g1, p0[1]), (g1, bus), (g1 + 30 * S, bus)) + [(g2 - 30 * S, bus)] + bezier((g2 - 30 * S, bus), (g2, bus), (g2, p3[1]), p3)
        else:
            dx = max(40, (p3[0] / S - p0[0] / S) / 2) * S
            pts = bezier(p0, (p0[0] + dx, p0[1]), (p3[0] - dx, p3[1]), p3)
        if dash:
            for i in range(0, len(pts) - 1, 2):
                d.line([pts[i], pts[i + 1]], fill=color, width=int(w * S))
        else:
            d.line(pts, fill=color, width=int(w * S), joint="curve")
        # arrow head
        ex, ey = pts[-1]
        px, py = pts[-4]
        import math

        ang = math.atan2(ey - py, ex - px)
        L = 7 * S
        d.polygon([(ex, ey), (ex - L * math.cos(ang - 0.45), ey - L * math.sin(ang - 0.45)),
                   (ex - L * math.cos(ang + 0.45), ey - L * math.sin(ang + 0.45))], fill=color)
    for nid, (x, y) in pos.items():
        n = nodes[nid]
        is_root = nid == root_id
        box = [x * S, y * S, (x + NODE_W) * S, (y + NODE_H) * S]
        d.rounded_rectangle(box, radius=9 * S, fill=PANEL, outline=ACCENT if is_root else STROKE, width=(2 if is_root else 1) * S)
        glyph = {"file": "▣", "url": "◎", "query": "⌕", "tool_call": "⚙", "skill": "☰"}.get(n["kind"], "○")
        d.text(((x + 10) * S, (y + 17) * S), glyph, font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14 * S), fill=ACCENT if is_root else TEXT2)
        t = title(n)
        if len(t) > 30:
            t = t[:29] + "…"
        d.text(((x + 32) * S, (y + 10) * S), t, font=f_t, fill=TEXT)
        model = model_for.get(nid)
        s = sub(n, derived.get(nid) if not is_root else None, (model or {}).get("label") if model else None)
        if len(s) > 40:
            s = s[:39] + "…"
        d.text(((x + 32) * S, (y + 30) * S), s, font=f_s, fill=TEXT3)
    # legend
    ly = (height - 18) * S
    lx = PAD * S
    for basis, label in (("observed", "observed — Hermes reported it"), ("evidenced", "evidenced — the file names the source"), ("inferred", "inferred — read earlier this turn/session")):
        color, w, dash = style[basis]
        if dash:
            for i in range(0, 26, 8):
                d.line([(lx + i * S, ly), (lx + (i + 4) * S, ly)], fill=color, width=int(w * S))
        else:
            d.line([(lx, ly), (lx + 26 * S, ly)], fill=color, width=int(w * S))
        d.text((lx + 32 * S, ly - 6 * S), label, font=f_s, fill=TEXT3)
        lx += (34 + len(label) * 5.4) * S
    img.save(out, optimize=True)
    return img


def draw_banner(out: Path, graph_img: Image.Image):
    W, H = 1200, 600
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((64, 72), "lineage", font=font(64, "SemiBold"), fill=TEXT)
    d.text((64, 150), "Where did this file come from?", font=font(28, "Medium"), fill=TEXT2)
    d.text((64, 196), "Provenance receipts and a graph for", font=font(17), fill=TEXT3)
    d.text((64, 218), "everything your Hermes agent produces.", font=font(17), fill=TEXT3)
    y = 262
    for glyph, color, label in (("●", ACCENT, "observed — Hermes reported it"), ("◉", EVID, "evidenced — the file names the source"),
                                ("○", INFER, "inferred — read earlier, labelled as such")):
        d.text((64, y), glyph, font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18), fill=color)
        d.text((92, y), label, font=font(17), fill=TEXT2)
        y += 30
    d.text((64, 368), "hermes plugins install lineage", font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 18), fill=ACCENT)
    d.text((64, 540), "A Hermes Agent plugin · hooks only · network only on request · MIT", font=font(14), fill=TEXT3)
    # graph thumbnail on the right
    thumb = graph_img.copy()
    thumb.thumbnail((620, 420))
    tx, ty = W - thumb.width - 36, H - thumb.height - 72
    d.rounded_rectangle([tx - 10, ty - 10, tx + thumb.width + 10, ty + thumb.height + 10], radius=16, fill=PANEL, outline=STROKE)
    img.paste(thumb, (tx, ty))
    img.save(out, optimize=True)


if __name__ == "__main__":
    g = json.loads(FIX.read_text())
    graph_img = draw_graph(g, g["root"], ROOT / "graph.png")
    draw_banner(ROOT / "banner.png", graph_img)
    print("wrote", ROOT / "graph.png", ROOT / "banner.png")

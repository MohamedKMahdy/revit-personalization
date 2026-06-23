"""
Generate a CONSISTENT, TUM-branded ribbon-icon set for the three thesis add-ins.

Design language (one set, six glyphs):
  * a rounded-square tile in the TUM blue gradient (TUM corporate-design blue #3070B3),
  * a single white glyph centred on it,
  * identical tile, corner radius, padding and stroke weight for every button,
so the whole "BIM Personalization" ribbon reads as one branded suite.

Rendered at 8x supersample with Pillow, then downsampled (LANCZOS) to 32 / 16 / 64 px.
TUM colours are the official corporate-design values (committee-defensible, not invented).

Run:  python assets/make_icons.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

# ── TUM corporate-design palette ────────────────────────────────────────────────
TUM_BLUE        = (0x30, 0x70, 0xB3)   # #3070B3  primary "TUM Blue"
TUM_BLUE_TOP    = (0x3A, 0x7E, 0xC2)   # lighter top of the tile gradient
TUM_BLUE_BOT    = (0x24, 0x57, 0x91)   # darker bottom of the tile gradient
TUM_BLUE_DEEP   = (0x07, 0x21, 0x40)   # TUM dark blue — used for glyph cut-outs
WHITE           = (0xFF, 0xFF, 0xFF, 0xFF)
CUT             = (0x2A, 0x60, 0x9C, 0xFF)  # "punch-through" colour inside white glyphs

SS = 8            # supersample factor
BASE = 32         # logical icon size
S = BASE * SS     # render canvas
RADIUS = int(S * 0.22)
PAD = int(S * 0.20)   # glyph inset from the tile edge

OUT = Path(__file__).parent / "icons"
OUT.mkdir(parents=True, exist_ok=True)


# ── tile ────────────────────────────────────────────────────────────────────────
def tile() -> Image.Image:
    """Rounded-square TUM-blue gradient tile (RGBA)."""
    grad = Image.new("RGBA", (S, S))
    px = grad.load()
    for y in range(S):
        t = y / (S - 1)
        r = round(TUM_BLUE_TOP[0] + (TUM_BLUE_BOT[0] - TUM_BLUE_TOP[0]) * t)
        g = round(TUM_BLUE_TOP[1] + (TUM_BLUE_BOT[1] - TUM_BLUE_TOP[1]) * t)
        b = round(TUM_BLUE_TOP[2] + (TUM_BLUE_BOT[2] - TUM_BLUE_TOP[2]) * t)
        for x in range(S):
            px[x, y] = (r, g, b, 255)

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=RADIUS, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def box():
    """Inner content box (x0, y0, x1, y1)."""
    return PAD, PAD, S - PAD, S - PAD


# ── glyphs (white on the tile) ───────────────────────────────────────────────────
def g_folder(d: ImageDraw.ImageDraw):
    """Log Path Settings — a folder."""
    x0, y0, x1, y1 = box()
    w, h = x1 - x0, y1 - y0
    tab_h = h * 0.20
    body_top = y0 + tab_h
    r = w * 0.06
    # back tab
    d.rounded_rectangle([x0, y0 + h * 0.06, x0 + w * 0.5, body_top + r * 2],
                        radius=r, fill=WHITE)
    # folder body
    d.rounded_rectangle([x0, body_top, x1, y1], radius=r, fill=WHITE)
    # thin open-lip line in tile colour
    d.rounded_rectangle([x0 + w * 0.07, body_top + h * 0.20, x1 - w * 0.07, body_top + h * 0.30],
                        radius=r * 0.5, fill=CUT)


def g_cloud(d: ImageDraw.ImageDraw):
    """Data Sharing — an upload cloud (cloud + up arrow)."""
    x0, y0, x1, y1 = box()
    w, h = x1 - x0, y1 - y0
    cy = y0 + h * 0.60
    # cloud silhouette from overlapping circles + base bar
    d.ellipse([x0, cy - h * 0.20, x0 + w * 0.46, cy + h * 0.24], fill=WHITE)
    d.ellipse([x0 + w * 0.30, cy - h * 0.34, x0 + w * 0.78, cy + h * 0.20], fill=WHITE)
    d.ellipse([x0 + w * 0.54, cy - h * 0.16, x1, cy + h * 0.24], fill=WHITE)
    d.rounded_rectangle([x0 + w * 0.06, cy, x1 - w * 0.04, cy + h * 0.24],
                        radius=w * 0.10, fill=WHITE)
    # up arrow (tile colour, punched through the cloud)
    ax = x0 + w * 0.5
    d.polygon([(ax, cy - h * 0.18), (ax - w * 0.16, cy + h * 0.02), (ax + w * 0.16, cy + h * 0.02)],
              fill=CUT)
    d.rounded_rectangle([ax - w * 0.055, cy - h * 0.02, ax + w * 0.055, cy + h * 0.26],
                        radius=w * 0.05, fill=CUT)


def g_power(d: ImageDraw.ImageDraw):
    """Revit MCP Switch — a power symbol (ring with a top gap + stem)."""
    x0, y0, x1, y1 = box()
    w, h = x1 - x0, y1 - y0
    cx, cy = x0 + w / 2, y0 + h * 0.56
    rad = w * 0.40
    sw = int(w * 0.14)
    # ring with a gap at the top
    d.arc([cx - rad, cy - rad, cx + rad, cy + rad], start=-65, end=245, fill=WHITE, width=sw)
    # round the arc ends
    for ang in (-65, 245):
        ex = cx + rad * math.cos(math.radians(ang))
        ey = cy + rad * math.sin(math.radians(ang))
        d.ellipse([ex - sw / 2, ey - sw / 2, ex + sw / 2, ey + sw / 2], fill=WHITE)
    # vertical stem
    d.rounded_rectangle([cx - sw / 2, y0 + h * 0.04, cx + sw / 2, cy - rad * 0.30],
                        radius=sw / 2, fill=WHITE)


def g_gear(d: ImageDraw.ImageDraw):
    """Settings — a gear with rectangular teeth."""
    x0, y0, x1, y1 = box()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    R = (x1 - x0) / 2
    teeth = 8
    rim = R * 0.70
    tooth_w = R * 0.30          # half-width of each tooth, tangential
    r0, r1 = rim * 0.82, R      # tooth spans from inside the rim to the outer radius
    for i in range(teeth):
        a = math.radians(i * 360 / teeth)
        ca, sa = math.cos(a), math.sin(a)
        pa, ps = math.cos(a + math.pi / 2), math.sin(a + math.pi / 2)
        corners = [
            (cx + r0 * ca - tooth_w * pa, cy + r0 * sa - tooth_w * ps),
            (cx + r1 * ca - tooth_w * 0.7 * pa, cy + r1 * sa - tooth_w * 0.7 * ps),
            (cx + r1 * ca + tooth_w * 0.7 * pa, cy + r1 * sa + tooth_w * 0.7 * ps),
            (cx + r0 * ca + tooth_w * pa, cy + r0 * sa + tooth_w * ps),
        ]
        d.polygon(corners, fill=WHITE)
    # gear body + centre hole
    d.ellipse([cx - rim, cy - rim, cx + rim, cy + rim], fill=WHITE)
    d.ellipse([cx - R * 0.28, cy - R * 0.28, cx + R * 0.28, cy + R * 0.28], fill=CUT)


def g_check(d: ImageDraw.ImageDraw):
    """Test Tools — a clipboard with a check mark."""
    x0, y0, x1, y1 = box()
    w, h = x1 - x0, y1 - y0
    r = w * 0.10
    # board
    d.rounded_rectangle([x0 + w * 0.10, y0 + h * 0.08, x1 - w * 0.10, y1], radius=r, fill=WHITE)
    # clip
    d.rounded_rectangle([x0 + w * 0.34, y0, x1 - w * 0.34, y0 + h * 0.16],
                        radius=r * 0.6, fill=WHITE)
    d.rounded_rectangle([x0 + w * 0.40, y0 + h * 0.03, x1 - w * 0.40, y0 + h * 0.13],
                        radius=r * 0.4, fill=CUT)
    # check mark (tile colour)
    sw = int(w * 0.11)
    d.line([(x0 + w * 0.28, y0 + h * 0.58), (x0 + w * 0.44, y0 + h * 0.74),
            (x0 + w * 0.74, y0 + h * 0.36)], fill=CUT, width=sw, joint="curve")


def g_chat(d: ImageDraw.ImageDraw):
    """Open Assistant — a chat/speech bubble with three dots."""
    x0, y0, x1, y1 = box()
    w, h = x1 - x0, y1 - y0
    r = w * 0.22
    body = [x0, y0, x1, y0 + h * 0.74]
    d.rounded_rectangle(body, radius=r, fill=WHITE)
    # tail
    d.polygon([(x0 + w * 0.22, y0 + h * 0.70), (x0 + w * 0.22, y1),
               (x0 + w * 0.48, y0 + h * 0.70)], fill=WHITE)
    # three dots
    cy = y0 + h * 0.37
    for i, fx in enumerate((0.30, 0.5, 0.70)):
        cx = x0 + w * fx
        rr = w * 0.072
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=CUT)


GLYPHS = {
    "logpath":     g_folder,   # RevitLogger – Log Path Settings
    "datasharing": g_cloud,    # RevitLogger – Data Sharing
    "icon":        g_power,    # mcp-servers – Revit MCP Switch  (keeps existing filename stem)
    "settings":    g_gear,     # mcp-servers – Settings
    "test":        g_check,    # mcp-servers – Test Tools
    "assistant":   g_chat,     # BIMAssistant – Open Assistant
}


def render(name: str, drawer) -> None:
    img = tile()
    drawer(ImageDraw.Draw(img))
    for size in (16, 32, 64):
        img.resize((size, size), Image.LANCZOS).save(OUT / f"{name}-{size}.png")
    # also a plain stem (largest) for resize-based loaders
    img.resize((64, 64), Image.LANCZOS).save(OUT / f"{name}.png")


if __name__ == "__main__":
    for name, drawer in GLYPHS.items():
        render(name, drawer)
        print(f"  rendered {name}: {name}-16/32/64.png")
    print(f"\nAll icons written to {OUT}")

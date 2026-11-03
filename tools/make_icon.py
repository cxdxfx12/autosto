"""生成喵喵图标（多尺寸 .ico + PNG + SVG）。

    python tools/make_icon.py

输出：
    app/assets/app.ico          打包进 exe / 窗口图标（16~256 全尺寸）
    app/assets/miao-badge.png   页面 Logo
    app/web/static/favicon.ico  浏览器标签图标
    app/web/static/miao.svg     矢量版（页面内联用的是同一份路径）

画法：先在 4 倍尺寸上绘制再缩小，等于超采样抗锯齿，
比 Pillow 自带的抗锯齿圆滑得多，16px 下也不糊。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "app" / "assets"
STATIC = ROOT / "app" / "web" / "static"

SS = 4          # 超采样倍数
BASE = 1024     # 逻辑画布
S = BASE * SS

# 暖橙虎斑 + 奶油口鼻 + 粉耳。比纯色多一档明暗，缩小后更有质感。
FUR_TOP = (255, 196, 128)
FUR_BOTTOM = (244, 158, 79)
FUR_EDGE = (226, 138, 62)
CREAM = (255, 248, 238)
PINK = (250, 176, 184)
PINK_DEEP = (235, 140, 152)
INK = (58, 42, 36)
WHITE = (255, 255, 255)


def _p(v: float) -> float:
    return v * SS


def _gradient_ellipse(size: int, box, top, bottom) -> Image.Image:
    """在给定圆内做垂直渐变，边缘用椭圆遮罩裁切。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    height = max(1, int(y1 - y0))
    for i in range(height):
        t = i / height
        color = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)
        d.line([(x0, y0 + i), (x1, y0 + i)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    layer.putalpha(mask)
    return layer


def _poly_mask(size: int, points) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask


def _gradient_poly(size: int, points, top, bottom) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ys = [y for _, y in points]
    y0, y1 = min(ys), max(ys)
    height = max(1, int(y1 - y0))
    for i in range(height):
        t = i / height
        color = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)
        d.line([(min(x for x, _ in points), y0 + i), (max(x for x, _ in points), y0 + i)],
               fill=color)
    layer.putalpha(_poly_mask(size, points))
    return layer


def draw_cat() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ── 耳朵：外层渐变三角 + 内层粉色 ─────────────────────────
    left_ear = [(_p(206), _p(438)), (_p(266), _p(168)), (_p(462), _p(318))]
    right_ear = [(_p(818), _p(438)), (_p(758), _p(168)), (_p(562), _p(318))]
    for ear in (left_ear, right_ear):
        img.alpha_composite(_gradient_poly(S, ear, FUR_TOP, FUR_BOTTOM))

    d = ImageDraw.Draw(img)
    d.polygon([(_p(268), _p(330)), (_p(298), _p(236)), (_p(392), _p(312))], fill=PINK)
    d.polygon([(_p(756), _p(330)), (_p(726), _p(236)), (_p(632), _p(312))], fill=PINK)

    # ── 脸：横向略宽，底部略深，做出体积 ──────────────────────
    img.alpha_composite(
        _gradient_ellipse(S, (_p(182), _p(258), _p(842), _p(872)), FUR_TOP, FUR_BOTTOM)
    )

    # 脸部内圈柔光（顶部受光）
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [_p(228), _p(296), _p(796), _p(812)], fill=(255, 226, 188, 90)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(_p(26)))
    img.alpha_composite(glow)

    # ── 头顶三道虎斑 ──────────────────────────────────────────
    for dx, w in ((0, 26), (-96, 22), (96, 22)):
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(
            [_p(512 + dx - w / 2), _p(300), _p(512 + dx + w / 2), _p(372)],
            radius=_p(15),
            fill=FUR_EDGE,
        )

    # ── 口鼻区：奶油色椭圆（带一点上缘阴影）──────────────────
    d = ImageDraw.Draw(img)
    d.ellipse([_p(336), _p(524), _p(688), _p(806)], fill=CREAM)

    # ── 眼睛：大、圆、双高光，是「可爱」的核心 ────────────────
    for cx in (392, 632):
        d.ellipse([_p(cx - 56), _p(474), _p(cx + 56), _p(606)], fill=INK)
        d.ellipse([_p(cx - 20), _p(496), _p(cx + 6), _p(534)], fill=WHITE)
        d.ellipse([_p(cx + 14), _p(560), _p(cx + 34), _p(580)], fill=(255, 255, 255, 165))
        # 眼眶上沿的一道浅弧，像双眼皮褶皱
        d.arc(
            [_p(cx - 62), _p(452), _p(cx + 62), _p(586)],
            start=205, end=335, fill=FUR_EDGE, width=_p(7),
        )

    # ── 腮红 ─────────────────────────────────────────────────
    blush = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blush)
    for cx in (300, 724):
        bd.ellipse([_p(cx - 52), _p(596), _p(cx + 52), _p(660)], fill=(250, 150, 148, 120))
    blush = blush.filter(ImageFilter.GaussianBlur(_p(12)))
    img.alpha_composite(blush)

    # ── 鼻子 + 嘴 ────────────────────────────────────────────
    d = ImageDraw.Draw(img)
    d.polygon([(_p(512), _p(636)), (_p(538), _p(654)), (_p(486), _p(654))], fill=PINK_DEEP)
    d.ellipse([_p(496), _p(628), _p(528), _p(650)], fill=PINK_DEEP)
    d.arc([_p(438), _p(642), _p(514), _p(722)], start=18, end=162, fill=INK, width=_p(10))
    d.arc([_p(510), _p(642), _p(586), _p(722)], start=18, end=162, fill=INK, width=_p(10))
    d.line([_p(512), _p(652), _p(512), _p(668)], fill=INK, width=_p(9))

    # ── 胡须 ─────────────────────────────────────────────────
    for sx in (-1, 1):
        for i, y in enumerate((612, 660)):
            d.line(
                [_p(512 + sx * 172), _p(y + i * 4), _p(512 + sx * 300), _p(y - 22 + i * 40)],
                fill=(255, 255, 255, 205),
                width=_p(8),
            )

    return img.resize((BASE, BASE), Image.LANCZOS)


def draw_badge(size: int = 512) -> Image.Image:
    """圆角方块版 Logo：暖色渐变底 + 猫头 + 柔和投影。"""
    edge = size * SS
    badge = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    radius = int(edge * 0.23)

    bg = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    for y in range(edge):
        t = y / edge
        # 奶油 → 蜜桃，轻微斜向感靠两次叠加
        color = (
            int(255 - 6 * t),
            int(250 - 26 * t),
            int(243 - 58 * t),
            255,
        )
        bd.line([(0, y), (edge, y)], fill=color)
    mask = Image.new("L", (edge, edge), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, edge - 1, edge - 1], radius=radius, fill=255)
    badge.paste(bg, (0, 0), mask)

    cat = draw_cat().resize((int(edge * 0.84), int(edge * 0.84)), Image.LANCZOS)

    # 猫头下方柔和投影，让它「坐」在底色上
    shadow = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse(
        [int(edge * 0.20), int(edge * 0.70), int(edge * 0.80), int(edge * 0.88)],
        fill=(196, 120, 60, 76),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(edge * 0.028)))
    badge.alpha_composite(shadow)

    off = (edge - cat.width) // 2
    badge.alpha_composite(cat, (off, int(edge * 0.055)))

    # 顶部一道极淡高光，避免整块颜色发闷
    gloss = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse(
        [-int(edge * 0.15), -int(edge * 0.42), int(edge * 1.15), int(edge * 0.34)],
        fill=(255, 255, 255, 46),
    )
    gloss.putalpha(Image.composite(gloss.getchannel("A"), Image.new("L", (edge, edge), 0), mask))
    badge.alpha_composite(gloss)

    return badge.resize((size, size), Image.LANCZOS)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="喵喵">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fffaf3"/><stop offset="1" stop-color="#ffe0bb"/>
    </linearGradient>
    <linearGradient id="fur" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffc480"/><stop offset="1" stop-color="#f49e4f"/>
    </linearGradient>
    <linearGradient id="ear" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffd0a0"/><stop offset="1" stop-color="#eb9440"/>
    </linearGradient>
    <filter id="soft" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="10"/>
    </filter>
  </defs>
  <rect width="512" height="512" rx="118" fill="url(#bg)"/>
  <ellipse cx="256" cy="392" rx="150" ry="30" fill="#c4783c" opacity=".18" filter="url(#soft)"/>
  <path d="M110 214 L140 88 L226 158 Z" fill="url(#ear)"/>
  <path d="M402 214 L372 88 L286 158 Z" fill="url(#ear)"/>
  <path d="M134 165 L149 118 L196 156 Z" fill="#fab0b8"/>
  <path d="M378 165 L363 118 L316 156 Z" fill="#fab0b8"/>
  <ellipse cx="256" cy="290" rx="165" ry="153" fill="url(#fur)"/>
  <ellipse cx="256" cy="322" rx="150" ry="120" fill="#fff8ee" opacity=".55"/>
  <g fill="#e28a3e">
    <rect x="243" y="150" width="13" height="36" rx="7"/>
    <rect x="195" y="154" width="11" height="32" rx="6"/>
    <rect x="306" y="154" width="11" height="32" rx="6"/>
  </g>
  <ellipse cx="256" cy="324" rx="88" ry="70" fill="#fff8ee"/>
  <g>
    <ellipse cx="196" cy="270" rx="28" ry="33" fill="#3a2a24"/>
    <ellipse cx="316" cy="270" rx="28" ry="33" fill="#3a2a24"/>
    <ellipse cx="186" cy="258" rx="10" ry="11" fill="#fff"/>
    <ellipse cx="306" cy="258" rx="10" ry="11" fill="#fff"/>
    <ellipse cx="208" cy="286" rx="5" ry="5" fill="#fff" opacity=".65"/>
    <ellipse cx="328" cy="286" rx="5" ry="5" fill="#fff" opacity=".65"/>
  </g>
  <g fill="#fa9694" opacity=".45">
    <ellipse cx="150" cy="314" rx="26" ry="17"/>
    <ellipse cx="362" cy="314" rx="26" ry="17"/>
  </g>
  <path d="M256 318 l14 10 h-28 Z" fill="#eb8c98"/>
  <ellipse cx="256" cy="316" rx="8" ry="6" fill="#eb8c98"/>
  <path d="M219 326 q13 22 37 22 q24 0 37 -22" fill="none" stroke="#3a2a24"
        stroke-width="5" stroke-linecap="round"/>
  <path d="M256 328 v8" stroke="#3a2a24" stroke-width="5" stroke-linecap="round"/>
  <g stroke="#fff" stroke-width="4" stroke-linecap="round" opacity=".8">
    <path d="M120 300 l60 -10"/><path d="M120 326 l60 8"/>
    <path d="M392 300 l-60 -10"/><path d="M392 326 l-60 8"/>
  </g>
</svg>
"""


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)

    cat = draw_cat()
    cat.save(ASSETS / "miao.png")
    cat.save(STATIC / "miao.png")

    badge = draw_badge(512)
    badge.save(ASSETS / "miao-badge.png")
    badge.save(STATIC / "miao-badge.png")

    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    badge.save(ASSETS / "app.ico", format="ICO", sizes=[(s, s) for s in sizes])
    badge.save(STATIC / "favicon.ico", format="ICO", sizes=[(s, s) for s in sizes])
    (STATIC / "miao.svg").write_text(SVG, encoding="utf-8")

    for p in ("app/assets/app.ico", "app/assets/miao-badge.png",
              "app/web/static/miao.png", "app/web/static/miao.svg"):
        f = ROOT / p
        print(f"{p:<34} {f.stat().st_size / 1024:8.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

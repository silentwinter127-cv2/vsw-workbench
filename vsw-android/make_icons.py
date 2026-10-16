"""生成安卓图标 PNG（自适应图标的前景层 + 老系统的方形图标）。

为什么不只用 vector：
  - 自适应图标（API 26+）用 XML 就够了，但 API 24/25 需要有位图兜底，
    否则那些机器上装完之后图标是空的；
  - 顺带把启动图标也画出来，省得工程里塞一堆二进制图说不清来源。
图形与桌面端 UI 的 logo 一致：三个取景角 + 一条扫描线 + 一个搜索圆点。
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "res")

BG = (14, 17, 22, 255)
ACCENT = (78, 161, 255, 255)
DOT = (89, 217, 164, 255)
WHITE = (230, 238, 246, 255)

# 自适应图标：前景层 108dp，安全区是中间 66%（也就是四周各留 18dp 的余量）
FOREGROUND_SIZES = {
    "mipmap-mdpi": 108,
    "mipmap-hdpi": 162,
    "mipmap-xhdpi": 216,
    "mipmap-xxhdpi": 324,
    "mipmap-xxxhdpi": 432,
}
# 老系统方形图标：48dp 基准
LEGACY_SIZES = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}


def draw_mark(size: int, *, with_background: bool, inset: float) -> Image.Image:
    """画一个标记。inset 是图形相对画布的内缩比例（自适应图标要给系统裁切留余量）。"""
    scale = 4  # 先超采样再缩，边缘不锯齿
    px = size * scale
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if with_background:
        radius = int(px * 0.22)
        draw.rounded_rectangle([0, 0, px - 1, px - 1], radius=radius, fill=BG)

    pad = px * inset
    box = [pad, pad, px - pad, px - pad]
    width_in = box[2] - box[0]
    height_in = box[3] - box[1]
    stroke = max(2, int(width_in * 0.062))
    arm = width_in * 0.20          # 取景角的手臂长度

    def corner(x: float, y: float, dx: int, dy: int) -> None:
        draw.line([(x, y), (x + dx * arm, y)], fill=ACCENT, width=stroke)
        draw.line([(x, y), (x, y + dy * arm)], fill=ACCENT, width=stroke)
        # 端点画圆，模拟 round line cap
        for cx, cy in ((x + dx * arm, y), (x, y + dy * arm)):
            draw.ellipse([cx - stroke / 2, cy - stroke / 2, cx + stroke / 2, cy + stroke / 2],
                         fill=ACCENT)

    left, top = box[0], box[1]
    right, bottom = box[2], box[3]
    corner(left, top, 1, 1)
    corner(right, top, -1, 1)
    corner(left, bottom, 1, -1)
    corner(right, bottom, -1, -1)

    # 扫描线（半透明，画在独立层上再合成，免得 alpha 叠到背景色里）
    scan = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    scan_draw = ImageDraw.Draw(scan)
    mid_y = (top + bottom) / 2
    scan_draw.line([(left + width_in * 0.08, mid_y), (right - width_in * 0.08, mid_y)],
                   fill=(78, 161, 255, 130), width=max(1, int(stroke * 0.55)))
    image = Image.alpha_composite(image, scan)

    draw = ImageDraw.Draw(image)
    dot_r = width_in * 0.115
    cx = left + width_in * 0.62
    draw.ellipse([cx - dot_r, mid_y - dot_r, cx + dot_r, mid_y + dot_r], fill=DOT)

    return image.resize((size, size), Image.LANCZOS)


def write(image: Image.Image, folder: str, name: str) -> str:
    target_dir = os.path.join(RES, folder)
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, name)
    image.save(path, format="PNG", optimize=True)
    return path


def main() -> int:
    written: list[str] = []
    for folder, size in FOREGROUND_SIZES.items():
        # 自适应图标前景：图形只占中间 ~62%（安全区之内），四周透明留给系统裁切
        written.append(write(draw_mark(size, with_background=False, inset=0.30),
                             folder, "ic_launcher_foreground.png"))
    for folder, size in LEGACY_SIZES.items():
        written.append(write(draw_mark(size, with_background=True, inset=0.17),
                             folder, "ic_launcher.png"))
        written.append(write(draw_mark(size, with_background=True, inset=0.17),
                             folder, "ic_launcher_round.png"))
    for path in written:
        rel = os.path.relpath(path, HERE)
        print(f"  {rel:56s} {os.path.getsize(path):>7d} B")
    print(f"共 {len(written)} 个图标")
    return 0


if __name__ == "__main__":
    sys.exit(main())

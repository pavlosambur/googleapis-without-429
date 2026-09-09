"""Render the GitHub social preview card.

GitHub shows this image when the repository link is pasted into Slack, a
tweet or a chat. It accepts PNG or JPG only, and the upload is manual --
the API does not expose the field -- so this script exists to make the
image reproducible rather than a one-off nobody can regenerate.

Drawn directly rather than rasterised from the SVG: the mark is plain
rectangles, and rendering SVG needs a system cairo or librsvg that is not
worth a dependency for one picture.

    uv run --with pillow python scripts/make_social_preview.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path("docs/social-preview.png")

# GitHub renders the card at 1280x640; drawing at 2x and downsampling gives
# clean edges without hinting artefacts.
WIDTH, HEIGHT, SCALE = 1280, 640, 2

BACKGROUND = "#0F1719"
TITLE = "#E9EFEF"
ACCENT = "#2FB3B3"
BURST = "#8296A5"
SUBTITLE = "#93A6A8"
FOOTNOTE = "#63797B"

FONT_CANDIDATES = {
    "bold": [
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    "mono": [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.ttf",
    ],
}
# Wanted style, matched against the face's own name. A .ttc holds several
# faces and their order is not fixed, so asking for index 2 and hoping is how
# a title silently comes out in italics -- which is exactly what happened.
WANTED_STYLE = {"bold": "bold", "regular": "regular", "mono": "regular"}


def load_font(role: str, size: int) -> ImageFont.FreeTypeFont:
    """The first candidate offering the wanted style, searched by name."""
    wanted = WANTED_STYLE[role]
    for path in FONT_CANDIDATES[role]:
        if not Path(path).exists():
            continue
        if not path.endswith(".ttc"):
            return ImageFont.truetype(path, size)
        for index in range(24):
            try:
                font = ImageFont.truetype(path, size, index=index)
            except OSError:
                break
            _, style = font.getname()
            if style and style.lower() == wanted:
                return font
    print(f"no usable {role} font found", file=sys.stderr)
    raise SystemExit(1)


def draw_mark(draw: ImageDraw.ImageDraw, left: int, top: int, size: int) -> None:
    """The Cadence mark, in the same proportions as docs/logo.svg."""
    unit = size / 64

    def bar(x: float, y: float, w: float, h: float, colour: str) -> None:
        radius = (w * unit) / 2
        draw.rounded_rectangle(
            [
                left + x * unit,
                top + y * unit,
                left + (x + w) * unit,
                top + (y + h) * unit,
            ],
            radius=radius,
            fill=colour,
        )

    for x in (5, 10.5, 16, 21.5):
        bar(x, 17, 3, 30, BURST)
    bar(29.5, 8, 3, 48, ACCENT)
    for x in (39, 48, 57):
        bar(x, 17, 3, 30, ACCENT)


def centre(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    """Left offset that centres this string on the canvas."""
    box = draw.textbbox((0, 0), text, font=font)
    return (WIDTH * SCALE - (box[2] - box[0])) / 2 - box[0]


def main() -> int:
    """Draw the card and write it to docs/, returning a process exit code."""
    canvas = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    mark_size = 132 * SCALE
    draw_mark(draw, (WIDTH * SCALE - mark_size) // 2, 118 * SCALE, mark_size)

    title_font = load_font("bold", 62 * SCALE)
    subtitle_font = load_font("regular", 27 * SCALE)
    foot_font = load_font("mono", 21 * SCALE)

    title = "googleapis-without-429"
    subtitle = "Stay inside Google API quotas instead of recovering from 429"
    footnote = "pip install googleapis-without-429"

    draw.text(
        (centre(draw, title, title_font), 300 * SCALE),
        title,
        font=title_font,
        fill=TITLE,
    )
    draw.text(
        (centre(draw, subtitle, subtitle_font), 396 * SCALE),
        subtitle,
        font=subtitle_font,
        fill=SUBTITLE,
    )

    rule_y = 470 * SCALE
    draw.rectangle(
        [440 * SCALE, rule_y, 840 * SCALE, rule_y + max(1, SCALE // 2)],
        fill="#1E2C2E",
    )

    draw.text(
        (centre(draw, footnote, foot_font), 502 * SCALE),
        footnote,
        font=foot_font,
        fill=FOOTNOTE,
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    resized = canvas.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    resized.save(OUTPUT, optimize=True)
    print(f"{OUTPUT}: {WIDTH}x{HEIGHT}, {OUTPUT.stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

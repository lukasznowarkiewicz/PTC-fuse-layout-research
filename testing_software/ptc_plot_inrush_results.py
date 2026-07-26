#!/usr/bin/env python3
"""Create SVG duty-cycle charts from PTC inrush tester trial CSV files.

Input is a ptc_inrush_trials_*.csv file. The chart plots:
- X axis: overload duty cycle [%]
- Y axis: completed overload cycles before trip
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


SVG_WIDTH = 1200
SVG_HEIGHT = 800
PLOT_LEFT = 110
PLOT_TOP = 60
PLOT_RIGHT = 390
PLOT_BOTTOM = 95

COLORS = [
    "#0072B2",
    "#D55E00",
    "#009E73",
    "#CC79A7",
    "#E69F00",
]

DASH_PATTERNS = [
    "",
    "",
    "",
    "",
    "",
    "8 4",
    "8 4",
    "8 4",
    "8 4",
    "8 4",
]


@dataclass(frozen=True)
class Point:
    layout: str
    duty_cycle_percent: float
    cycles_before_trip: float
    stdev_cycles: Optional[float] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot PTC inrush trip cycles from ptc_inrush_trials CSV."
    )
    parser.add_argument("csv_path", type=Path, help="Path to ptc_inrush_trials_*.csv.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output SVG path. Default: input filename with _cycles_chart.svg suffix.",
    )
    parser.add_argument("--title", help="Chart title. Default: generated title.")
    parser.add_argument(
        "--no-points",
        action="store_true",
        help="Hide individual trial points and show only mean lines.",
    )
    return parser.parse_args()


def parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def is_yes(value: object) -> bool:
    return str(value).strip().lower() in {"yes", "true", "1", "y"}


def layout_label(row: dict) -> str:
    index = str(row.get("layout_index", "")).strip()
    name = str(row.get("layout_name", "")).strip()
    if index and name:
        return f"{index}: {name}"
    if name:
        return name
    if index:
        return f"Layout {index}"
    return "Layout"


def layout_sort_key(layout: str) -> tuple[int, str]:
    match = re.match(r"^\s*(\d+)\s*:", layout)
    if match:
        return (int(match.group(1)), layout)
    return (10_000, layout)


def display_layout_label(layout: str) -> str:
    if ":" not in layout:
        return layout.replace("_", " ")
    prefix, name = layout.split(":", 1)
    return f"{prefix}: {name.strip().replace('_', ' ')}"


def dash_attr(pattern: str) -> str:
    return "" if not pattern else f' stroke-dasharray="{pattern}"'


def svg_escape(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fmt_number(value: float) -> str:
    if abs(value) >= 10:
        return f"{value:.0f}"
    if abs(value) >= 1:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def nice_linear_ticks(minimum: float, maximum: float, count: int = 7) -> list[float]:
    if minimum == maximum:
        return [minimum]
    span = maximum - minimum
    raw_step = span / max(1, count - 1)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = min((1, 2, 5, 10), key=lambda value: abs(value * magnitude - raw_step)) * magnitude
    start = math.floor(minimum / step) * step
    end = math.ceil(maximum / step) * step
    ticks = []
    value = start
    while value <= end + step * 0.5:
        ticks.append(value)
        value += step
    return ticks


def load_points(path: Path) -> tuple[list[Point], int]:
    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fields = set(reader.fieldnames or [])

    required = {"duty_cycle_percent", "cycles_completed", "tripped"}
    missing = sorted(required - fields)
    if missing:
        raise ValueError(f"CSV does not contain required column(s): {', '.join(missing)}")

    points = []
    skipped_no_trip = 0
    for row in rows:
        if not is_yes(row.get("tripped")):
            skipped_no_trip += 1
            continue
        duty = parse_float(row.get("duty_cycle_percent"))
        cycles = parse_float(row.get("cycles_completed"))
        if duty is None or cycles is None or duty <= 0 or cycles < 0:
            continue
        points.append(
            Point(
                layout=layout_label(row),
                duty_cycle_percent=duty,
                cycles_before_trip=cycles,
            )
        )

    if not points:
        raise ValueError("No tripped rows found. Nothing to plot.")
    return points, skipped_no_trip


def grouped_means(points: list[Point]) -> dict[str, list[Point]]:
    grouped_values: dict[tuple[str, float], list[float]] = defaultdict(list)
    for point in points:
        grouped_values[(point.layout, point.duty_cycle_percent)].append(point.cycles_before_trip)

    by_layout: dict[str, list[Point]] = defaultdict(list)
    for (layout, duty), cycles in grouped_values.items():
        stdev = statistics.stdev(cycles) if len(cycles) > 1 else None
        by_layout[layout].append(
            Point(
                layout=layout,
                duty_cycle_percent=duty,
                cycles_before_trip=statistics.mean(cycles),
                stdev_cycles=stdev,
            )
        )

    for layout in by_layout:
        by_layout[layout].sort(key=lambda point: point.duty_cycle_percent)
    return dict(sorted(by_layout.items(), key=lambda item: layout_sort_key(item[0])))


def build_svg(points: list[Point], source: Path, title: str, show_points: bool) -> str:
    plot_width = SVG_WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_height = SVG_HEIGHT - PLOT_TOP - PLOT_BOTTOM
    means_by_layout = grouped_means(points)

    duties = [point.duty_cycle_percent for point in points]
    cycles = [point.cycles_before_trip for point in points]
    x_min = min(duties)
    x_max = max(duties)
    if x_min == x_max:
        x_min = max(0.0, x_min - 5.0)
        x_max += 5.0
    else:
        pad = (x_max - x_min) * 0.08
        x_min = max(0.0, x_min - pad)
        x_max = min(100.0, x_max + pad)
    y_min = 0.0
    y_max = max(cycles) * 1.12
    if y_max <= 1:
        y_max = 1.0

    def x_px(duty_cycle_percent: float) -> float:
        return PLOT_LEFT + (duty_cycle_percent - x_min) / (x_max - x_min) * plot_width

    def y_px(cycles_before_trip: float) -> float:
        return PLOT_TOP + (1.0 - (cycles_before_trip - y_min) / (y_max - y_min)) * plot_height

    duty_values = sorted({point.duty_cycle_percent for point in points})
    x_ticks = duty_values if len(duty_values) <= 12 else nice_linear_ticks(x_min, x_max)
    y_ticks = nice_linear_ticks(y_min, y_max)

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" height="{SVG_HEIGHT}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{SVG_WIDTH / 2:.1f}" y="32" text-anchor="middle" font-family="Arial, sans-serif" font-size="24" font-weight="700">{svg_escape(title)}</text>',
        f'<text x="{SVG_WIDTH / 2:.1f}" y="{SVG_HEIGHT - 18}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#555">{svg_escape(source)}</text>',
    ]

    for tick in x_ticks:
        if tick < x_min or tick > x_max:
            continue
        x = x_px(tick)
        lines.append(f'<line x1="{x:.2f}" y1="{PLOT_TOP}" x2="{x:.2f}" y2="{PLOT_TOP + plot_height}" stroke="#e5e7eb" stroke-width="1"/>')
        lines.append(f'<text x="{x:.2f}" y="{PLOT_TOP + plot_height + 24}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#333">{fmt_number(tick)}%</text>')

    for tick in y_ticks:
        if tick < y_min or tick > y_max:
            continue
        y = y_px(tick)
        lines.append(f'<line x1="{PLOT_LEFT}" y1="{y:.2f}" x2="{PLOT_LEFT + plot_width}" y2="{y:.2f}" stroke="#e5e7eb" stroke-width="1"/>')
        lines.append(f'<text x="{PLOT_LEFT - 12}" y="{y + 4:.2f}" text-anchor="end" font-family="Arial, sans-serif" font-size="13" fill="#333">{fmt_number(tick)}</text>')

    lines.extend(
        [
            f'<rect x="{PLOT_LEFT}" y="{PLOT_TOP}" width="{plot_width}" height="{plot_height}" fill="none" stroke="#111827" stroke-width="2"/>',
            f'<text x="{PLOT_LEFT + plot_width / 2:.1f}" y="{PLOT_TOP + plot_height + 58}" text-anchor="middle" font-family="Arial, sans-serif" font-size="18" font-weight="700">Duty cycle [%]</text>',
            f'<text x="28" y="{PLOT_TOP + plot_height / 2:.1f}" transform="rotate(-90 28 {PLOT_TOP + plot_height / 2:.1f})" text-anchor="middle" font-family="Arial, sans-serif" font-size="18" font-weight="700">Completed overload cycles before trip</text>',
        ]
    )

    series_items = list(means_by_layout.items())
    for index, (layout, mean_points) in enumerate(series_items):
        color = COLORS[index % len(COLORS)]
        dash = dash_attr(DASH_PATTERNS[index % len(DASH_PATTERNS)])
        if len(mean_points) > 1:
            coords = " ".join(f"{x_px(point.duty_cycle_percent):.2f},{y_px(point.cycles_before_trip):.2f}" for point in mean_points)
            lines.append(
                f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.2" '
                f'stroke-linejoin="round" stroke-linecap="round"{dash}/>'
            )
        for point in mean_points:
            x = x_px(point.duty_cycle_percent)
            y = y_px(point.cycles_before_trip)
            if point.stdev_cycles:
                low = max(point.cycles_before_trip - point.stdev_cycles, y_min)
                high = min(point.cycles_before_trip + point.stdev_cycles, y_max)
                y_low = y_px(low)
                y_high = y_px(high)
                lines.append(f'<line x1="{x:.2f}" y1="{y_high:.2f}" x2="{x:.2f}" y2="{y_low:.2f}" stroke="{color}" stroke-width="1.2" opacity="0.45"{dash}/>')
                lines.append(f'<line x1="{x - 4:.2f}" y1="{y_high:.2f}" x2="{x + 4:.2f}" y2="{y_high:.2f}" stroke="{color}" stroke-width="1.2" opacity="0.45"/>')
                lines.append(f'<line x1="{x - 4:.2f}" y1="{y_low:.2f}" x2="{x + 4:.2f}" y2="{y_low:.2f}" stroke="{color}" stroke-width="1.2" opacity="0.45"/>')
            lines.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.2" fill="{color}" stroke="#ffffff" stroke-width="1.2"/>')

    if show_points:
        mean_point_count = sum(len(mean_points) for _layout, mean_points in series_items)
        if len(points) > mean_point_count:
            for index, (layout, _) in enumerate(series_items):
                color = COLORS[index % len(COLORS)]
                for point in [p for p in points if p.layout == layout]:
                    lines.append(
                        f'<circle cx="{x_px(point.duty_cycle_percent):.2f}" cy="{y_px(point.cycles_before_trip):.2f}" r="2.4" fill="{color}" opacity="0.28"/>'
                    )

    legend_x = PLOT_LEFT + plot_width + 34
    legend_y = PLOT_TOP + 20
    lines.append(f'<text x="{legend_x}" y="{legend_y - 12}" font-family="Arial, sans-serif" font-size="16" font-weight="700">Layouts</text>')
    for index, layout in enumerate(means_by_layout.keys()):
        color = COLORS[index % len(COLORS)]
        dash = dash_attr(DASH_PATTERNS[index % len(DASH_PATTERNS)])
        y = legend_y + index * 30
        lines.append(f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 30}" y2="{y}" stroke="{color}" stroke-width="2.2" stroke-linecap="round"{dash}/>')
        lines.append(f'<circle cx="{legend_x + 15}" cy="{y}" r="4" fill="{color}" stroke="#ffffff" stroke-width="1"/>')
        lines.append(f'<text x="{legend_x + 42}" y="{y + 5}" font-family="Arial, sans-serif" font-size="14" fill="#111827">{svg_escape(display_layout_label(layout))}</text>')

    lines.append(f'<text x="{PLOT_LEFT}" y="{PLOT_TOP - 16}" font-family="Arial, sans-serif" font-size="13" fill="#555">No-trip rows are omitted from the plotted points</text>')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    csv_path = args.csv_path.expanduser().resolve()
    output_path = args.output
    if output_path is None:
        output_path = csv_path.with_name(f"{csv_path.stem}_cycles_chart.svg")
    else:
        output_path = output_path.expanduser().resolve()

    try:
        points, skipped_no_trip = load_points(csv_path)
        title = args.title or "PTC inrush trip cycles vs duty cycle"
        svg = build_svg(
            points=points,
            source=csv_path,
            title=title,
            show_points=not args.no_points,
        )
        output_path.write_text(svg, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    layouts = len({point.layout for point in points})
    print(f"Wrote chart: {output_path}")
    print(f"Plotted {len(points)} tripped trial point(s) across {layouts} layout(s).")
    if skipped_no_trip:
        print(f"Skipped {skipped_no_trip} no-trip row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

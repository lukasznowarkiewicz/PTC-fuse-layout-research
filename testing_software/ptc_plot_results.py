#!/usr/bin/env python3
"""Create SVG trip-time charts from PTC tester CSV files.

Input can be either:
- ptc_trials_*.csv, using set_current_a vs trip_time_s
- ptc_averages_*.csv, using set_current_a vs average_trip_time_s
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
    current_a: float
    time_s: float
    stdev_s: Optional[float] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot PTC trip time chart from ptc_trials or ptc_averages CSV."
    )
    parser.add_argument("csv_path", type=Path, help="Path to ptc_trials_*.csv or ptc_averages_*.csv.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output SVG path. Default: input filename with _chart.svg suffix.",
    )
    parser.add_argument("--title", help="Chart title. Default: generated from input file name.")
    scale_group = parser.add_mutually_exclusive_group()
    scale_group.add_argument(
        "--log-y",
        dest="log_y",
        action="store_true",
        default=True,
        help="Use logarithmic Y axis for time. Default.",
    )
    scale_group.add_argument(
        "--linear-y",
        dest="log_y",
        action="store_false",
        help="Use a linear Y axis instead of the default logarithmic axis.",
    )
    parser.add_argument(
        "--no-points",
        action="store_true",
        help="Hide individual trial points and show only mean/average lines.",
    )
    return parser.parse_args()


def parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        value_f = float(text)
    except ValueError:
        return None
    if not math.isfinite(value_f):
        return None
    return value_f


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
    display_name = name.strip().replace("_", " ")
    if display_name.endswith(" ambient"):
        return display_name.removesuffix(" ambient")
    return f"{prefix}: {display_name}"


def dash_attr(pattern: str) -> str:
    return "" if not pattern else f' stroke-dasharray="{pattern}"'


def load_points(path: Path) -> tuple[list[Point], str, str]:
    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fields = set(reader.fieldnames or [])

    if "set_current_a" not in fields:
        raise ValueError("CSV does not contain required column: set_current_a")

    if "average_trip_time_s" in fields:
        time_column = "average_trip_time_s"
        stdev_column = "stdev_trip_time_s" if "stdev_trip_time_s" in fields else None
    elif "trip_time_s" in fields:
        time_column = "trip_time_s"
        stdev_column = None
    else:
        raise ValueError("CSV does not contain trip_time_s or average_trip_time_s")

    points: list[Point] = []
    for row in rows:
        current = parse_float(row.get("set_current_a"))
        time_s = parse_float(row.get(time_column))
        if current is None or time_s is None or current <= 0 or time_s <= 0:
            continue

        stdev_s = parse_float(row.get(stdev_column)) if stdev_column else None
        points.append(
            Point(
                layout=layout_label(row),
                current_a=current,
                time_s=time_s,
                stdev_s=stdev_s if stdev_s is not None and stdev_s > 0 else None,
            )
        )

    if not points:
        raise ValueError("No plottable trip-time rows found. Check that trips were detected.")

    legend_title = "Temperatures" if "original_layout_name" in fields else "Layouts"
    return points, time_column, legend_title


def nice_linear_ticks(minimum: float, maximum: float, count: int = 6) -> list[float]:
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


def log_ticks(minimum: float, maximum: float) -> list[float]:
    start_exp = math.floor(math.log10(minimum))
    end_exp = math.ceil(math.log10(maximum))
    ticks = []
    for exponent in range(start_exp, end_exp + 1):
        for mantissa in (1, 2, 5):
            value = mantissa * (10**exponent)
            if minimum <= value <= maximum:
                ticks.append(value)
    return ticks


def fmt_number(value: float) -> str:
    if value >= 10:
        return f"{value:.0f}"
    if value >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def svg_escape(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def grouped_means(points: list[Point]) -> dict[str, list[Point]]:
    grouped_values: dict[tuple[str, float], list[float]] = defaultdict(list)
    grouped_stdev: dict[tuple[str, float], list[float]] = defaultdict(list)

    for point in points:
        key = (point.layout, point.current_a)
        grouped_values[key].append(point.time_s)
        if point.stdev_s is not None:
            grouped_stdev[key].append(point.stdev_s)

    by_layout: dict[str, list[Point]] = defaultdict(list)
    for (layout, current), times in grouped_values.items():
        stdev = None
        if grouped_stdev[(layout, current)]:
            stdev = statistics.mean(grouped_stdev[(layout, current)])
        elif len(times) > 1:
            stdev = statistics.stdev(times)
        by_layout[layout].append(
            Point(layout=layout, current_a=current, time_s=statistics.mean(times), stdev_s=stdev)
        )

    for layout in by_layout:
        by_layout[layout].sort(key=lambda point: point.current_a)
    return dict(sorted(by_layout.items(), key=lambda item: layout_sort_key(item[0])))


def build_svg(
    points: list[Point],
    source: Path,
    title: str,
    log_y: bool,
    show_points: bool,
    legend_title: str,
) -> str:
    plot_width = SVG_WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_height = SVG_HEIGHT - PLOT_TOP - PLOT_BOTTOM

    currents = [point.current_a for point in points]
    times = [point.time_s for point in points]
    x_min = min(currents)
    x_max = max(currents)
    if x_min == x_max:
        x_min *= 0.9
        x_max *= 1.1
    else:
        pad = (x_max - x_min) * 0.06
        x_min -= pad
        x_max += pad

    if log_y:
        y_min = max(min(times) * 0.8, 1e-4)
        y_max = max(times) * 1.25

        def y_value(value: float) -> float:
            return math.log10(value)

        y_axis_min = y_value(y_min)
        y_axis_max = y_value(y_max)
        y_ticks = log_ticks(y_min, y_max)
    else:
        y_min = 0.0
        y_max = max(times) * 1.15
        if y_max <= 0:
            y_max = 1.0

        def y_value(value: float) -> float:
            return value

        y_axis_min = y_min
        y_axis_max = y_max
        y_ticks = nice_linear_ticks(y_min, y_max)

    def x_px(current: float) -> float:
        return PLOT_LEFT + (current - x_min) / (x_max - x_min) * plot_width

    def y_px(time_s: float) -> float:
        return PLOT_TOP + (1.0 - (y_value(time_s) - y_axis_min) / (y_axis_max - y_axis_min)) * plot_height

    x_ticks = nice_linear_ticks(x_min, x_max)
    means_by_layout = grouped_means(points)

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
        lines.append(f'<text x="{x:.2f}" y="{PLOT_TOP + plot_height + 24}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#333">{fmt_number(tick)}</text>')

    for tick in y_ticks:
        if tick <= 0 or tick < y_min or tick > y_max:
            continue
        y = y_px(tick)
        lines.append(f'<line x1="{PLOT_LEFT}" y1="{y:.2f}" x2="{PLOT_LEFT + plot_width}" y2="{y:.2f}" stroke="#e5e7eb" stroke-width="1"/>')
        lines.append(f'<text x="{PLOT_LEFT - 12}" y="{y + 4:.2f}" text-anchor="end" font-family="Arial, sans-serif" font-size="13" fill="#333">{fmt_number(tick)}</text>')

    lines.extend(
        [
            f'<rect x="{PLOT_LEFT}" y="{PLOT_TOP}" width="{plot_width}" height="{plot_height}" fill="none" stroke="#111827" stroke-width="2"/>',
            f'<text x="{PLOT_LEFT + plot_width / 2:.1f}" y="{PLOT_TOP + plot_height + 58}" text-anchor="middle" font-family="Arial, sans-serif" font-size="18" font-weight="700">Current [A]</text>',
            f'<text x="28" y="{PLOT_TOP + plot_height / 2:.1f}" transform="rotate(-90 28 {PLOT_TOP + plot_height / 2:.1f})" text-anchor="middle" font-family="Arial, sans-serif" font-size="18" font-weight="700">Trip time [s]</text>',
        ]
    )

    series_items = list(means_by_layout.items())
    for index, (layout, mean_points) in enumerate(series_items):
        color = COLORS[index % len(COLORS)]
        dash = dash_attr(DASH_PATTERNS[index % len(DASH_PATTERNS)])
        if len(mean_points) > 1:
            coords = " ".join(f"{x_px(point.current_a):.2f},{y_px(point.time_s):.2f}" for point in mean_points)
            lines.append(
                f'<polyline points="{coords}" fill="none" stroke="{color}" '
                f'stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"{dash}/>'
            )

        for point in mean_points:
            x = x_px(point.current_a)
            y = y_px(point.time_s)
            if point.stdev_s:
                low = max(point.time_s - point.stdev_s, y_min if log_y else 0)
                high = point.time_s + point.stdev_s
                y_low = y_px(max(low, y_min))
                y_high = y_px(min(high, y_max))
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
                        f'<circle cx="{x_px(point.current_a):.2f}" cy="{y_px(point.time_s):.2f}" r="2.4" fill="{color}" opacity="0.28"/>'
                    )

    legend_x = PLOT_LEFT + plot_width + 34
    legend_y = PLOT_TOP + 20
    lines.append(f'<text x="{legend_x}" y="{legend_y - 12}" font-family="Arial, sans-serif" font-size="16" font-weight="700">{svg_escape(legend_title)}</text>')
    for index, layout in enumerate(means_by_layout.keys()):
        color = COLORS[index % len(COLORS)]
        dash = dash_attr(DASH_PATTERNS[index % len(DASH_PATTERNS)])
        y = legend_y + index * 30
        lines.append(f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 30}" y2="{y}" stroke="{color}" stroke-width="2.2" stroke-linecap="round"{dash}/>')
        lines.append(f'<circle cx="{legend_x + 15}" cy="{y}" r="4" fill="{color}" stroke="#ffffff" stroke-width="1"/>')
        lines.append(f'<text x="{legend_x + 42}" y="{y + 5}" font-family="Arial, sans-serif" font-size="14" fill="#111827">{svg_escape(display_layout_label(layout))}</text>')

    axis_note = "log scale" if log_y else "linear scale"
    lines.append(f'<text x="{PLOT_LEFT}" y="{PLOT_TOP - 16}" font-family="Arial, sans-serif" font-size="13" fill="#555">Y axis: {axis_note}</text>')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    csv_path = args.csv_path.expanduser().resolve()
    output_path = args.output
    if output_path is None:
        output_path = csv_path.with_name(f"{csv_path.stem}_chart.svg")
    else:
        output_path = output_path.expanduser().resolve()

    try:
        points, time_column, legend_title = load_points(csv_path)
        title = args.title or f"PTC trip time vs current ({time_column})"
        svg = build_svg(
            points=points,
            source=csv_path,
            title=title,
            log_y=args.log_y,
            show_points=not args.no_points,
            legend_title=legend_title,
        )
        output_path.write_text(svg, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    layouts = len({point.layout for point in points})
    print(f"Wrote chart: {output_path}")
    print(f"Plotted {len(points)} point(s) across {layouts} layout(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

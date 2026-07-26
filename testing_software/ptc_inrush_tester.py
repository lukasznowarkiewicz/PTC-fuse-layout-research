#!/usr/bin/env python3
"""Pulse-overload PTC test runner for Test 3.

Runs a base CC load with periodic overload pulses and measures time to PTC trip.
"""

from __future__ import annotations

import argparse
import csv
import signal
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from dl24p_minimal import DL24P, DL24PError, DL24PStatus, MAX_CURRENT_A


DEFAULT_LAYOUTS = [
    "normal_routing",
    "thermal_reliefs",
    "top_copper_cutout",
    "top_and_inner_copper_cutout",
    "hole_under_pptc",
    "milling_under_pptc",
    "milling_around_pptc_island",
    "via_stitching_to_pads",
    "pptc_in_silicone",
    "heat_sunk_slow_blow_cell",
]
DEFAULT_DUTY_CYCLES = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90]

RAW_FIELDS = [
    "timestamp",
    "elapsed_s",
    "sample_dt_s",
    "ambient_c",
    "layout_index",
    "layout_name",
    "trial",
    "duty_cycle_percent",
    "period_s",
    "base_current_a",
    "overload_current_a",
    "commanded_phase",
    "commanded_current_a",
    "phase_elapsed_s",
    "cycle_index",
    "baseline_voltage_v",
    "trip_voltage_threshold_v",
    "trip_current_threshold_a",
    "current_arm_threshold_a",
    "voltage_v",
    "current_a",
    "power_w",
    "load",
    "mosfet_temp_c",
    "ext_temp_c",
    "fan_rpm",
    "current_trip_armed",
    "tripped",
    "trip_reason",
]

TRIAL_FIELDS = [
    "timestamp",
    "ambient_c",
    "layout_index",
    "layout_name",
    "trial",
    "duty_cycle_percent",
    "period_s",
    "overload_on_s",
    "base_current_a",
    "overload_current_a",
    "tripped",
    "trip_time_s",
    "trip_reason",
    "cycles_completed",
    "baseline_voltage_v",
    "trip_voltage_threshold_v",
    "trip_current_threshold_a",
    "current_arm_threshold_a",
    "end_voltage_v",
    "end_current_a",
    "max_mosfet_temp_c",
    "sample_count",
]

AVERAGE_FIELDS = [
    "timestamp",
    "ambient_c",
    "layout_index",
    "layout_name",
    "duty_cycle_percent",
    "trials_requested",
    "trials_completed",
    "trips_detected",
    "average_trip_time_s",
    "stdev_trip_time_s",
    "min_trip_time_s",
    "max_trip_time_s",
]


@dataclass(frozen=True)
class TrialResult:
    timestamp: str
    ambient_c: float
    layout_index: int
    layout_name: str
    trial: int
    duty_cycle_percent: float
    period_s: float
    overload_on_s: float
    base_current_a: float
    overload_current_a: float
    tripped: bool
    trip_time_s: Optional[float]
    trip_reason: str
    cycles_completed: int
    baseline_voltage_v: float
    trip_voltage_threshold_v: float
    trip_current_threshold_a: float
    current_arm_threshold_a: float
    end_voltage_v: float
    end_current_a: float
    max_mosfet_temp_c: float
    sample_count: int

    def as_csv_row(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "ambient_c": f"{self.ambient_c:.2f}",
            "layout_index": self.layout_index,
            "layout_name": self.layout_name,
            "trial": self.trial,
            "duty_cycle_percent": f"{self.duty_cycle_percent:g}",
            "period_s": f"{self.period_s:.4f}",
            "overload_on_s": f"{self.overload_on_s:.4f}",
            "base_current_a": f"{self.base_current_a:.4f}",
            "overload_current_a": f"{self.overload_current_a:.4f}",
            "tripped": "yes" if self.tripped else "no",
            "trip_time_s": "" if self.trip_time_s is None else f"{self.trip_time_s:.3f}",
            "trip_reason": self.trip_reason,
            "cycles_completed": self.cycles_completed,
            "baseline_voltage_v": f"{self.baseline_voltage_v:.4f}",
            "trip_voltage_threshold_v": f"{self.trip_voltage_threshold_v:.4f}",
            "trip_current_threshold_a": f"{self.trip_current_threshold_a:.4f}",
            "current_arm_threshold_a": f"{self.current_arm_threshold_a:.4f}",
            "end_voltage_v": f"{self.end_voltage_v:.4f}",
            "end_current_a": f"{self.end_current_a:.4f}",
            "max_mosfet_temp_c": f"{self.max_mosfet_temp_c:.3f}",
            "sample_count": self.sample_count,
        }


class CsvLogger:
    def __init__(self, path: Path, fieldnames: list[str]) -> None:
        self.path = path
        self.file = path.open("w", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=fieldnames)
        self.writer.writeheader()
        self.file.flush()

    def write(self, row: dict) -> None:
        self.writer.writerow(row)
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def parse_float_list(value: str) -> list[float]:
    values = []
    for part in value.split(","):
        part = part.strip()
        if part:
            values.append(float(part))
    if not values:
        raise argparse.ArgumentTypeError("list must contain at least one number")
    return values


def parse_layouts(value: str) -> list[str]:
    layouts = [part.strip() for part in value.split(",") if part.strip()]
    if not layouts:
        raise argparse.ArgumentTypeError("layout list cannot be empty")
    return layouts


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run periodic inrush-overload PTC tests with a DL24P."
    )
    parser.add_argument(
        "--layouts",
        "--layout",
        type=parse_layouts,
        default=DEFAULT_LAYOUTS,
        help="Comma-separated layout names. Default: all 10 research-board layouts.",
    )
    parser.add_argument("--ambient", type=float, default=22.0, metavar="C", help="Ambient temperature label. Default: 22 C.")
    parser.add_argument("--base-current", type=positive_float, default=0.55, metavar="A", help="Base operating current. Default: 0.55 A.")
    parser.add_argument("--overload-current", type=positive_float, default=4.0, metavar="A", help="Pulse overload current. Default: 4 A.")
    parser.add_argument("--period", type=positive_float, default=1.0, metavar="S", help="Pulse period. Default: 1 s.")
    parser.add_argument(
        "--duty-cycles",
        type=parse_float_list,
        default=DEFAULT_DUTY_CYCLES,
        metavar="PERCENT_LIST",
        help="Comma-separated overload duty cycles in percent. Default: 5,10,20,30,40,50,60,70,80,90.",
    )
    parser.add_argument("--trials", type=positive_int, default=3, help="Trials per layout/duty cycle. Default: 3.")
    parser.add_argument("--sample-interval", type=positive_float, default=0.02, metavar="S", help="Target sampling period. Default: 0.02 s.")
    parser.add_argument("--max-test-seconds", type=positive_float, default=30.0, metavar="S", help="Stop a no-trip trial after this time. Default: 30 s.")
    parser.add_argument("--settle-seconds", type=nonnegative_float, default=0.20, metavar="S", help="Pre-load voltage baseline window. Default: 0.20 s.")
    parser.add_argument("--base-settle-seconds", type=nonnegative_float, default=2.0, metavar="S", help="Run base current before starting pulses. Default: 2 s.")
    parser.add_argument(
        "--trip-voltage-ratio",
        type=positive_float,
        default=0.50,
        help="Trip when voltage is below baseline multiplied by this ratio. Default: 0.50.",
    )
    parser.add_argument(
        "--trip-voltage",
        type=nonnegative_float,
        default=None,
        metavar="V",
        help="Absolute voltage trip threshold. If set, the larger of ratio and absolute threshold is used.",
    )
    parser.add_argument(
        "--trip-current",
        type=nonnegative_float,
        default=0.05,
        metavar="A",
        help="Current-collapse threshold. Default: 0.05 A.",
    )
    parser.add_argument(
        "--trip-current-arm-ratio",
        type=nonnegative_float,
        default=0.50,
        help="Arm current-collapse detection after current reaches this fraction of base current. Default: 0.50.",
    )
    parser.add_argument(
        "--trip-current-grace-seconds",
        type=nonnegative_float,
        default=0.02,
        metavar="S",
        help="Ignore current-collapse detection for this long after pulse sequence starts. Default: 0.02 s.",
    )
    parser.add_argument("--confirm-samples", type=positive_int, default=1, help="Consecutive samples required to confirm trip. Default: 1.")
    parser.add_argument(
        "--no-skip-after-first-no-trip",
        action="store_true",
        help="Run all requested trials even when trial 1 for a duty cycle does not trip.",
    )
    parser.add_argument("--cooldown-seconds", type=nonnegative_float, default=25.0, metavar="S", help="Cooldown after each detected trip. Default: 25 s.")
    parser.add_argument("--output-dir", type=Path, default=Path("ptc_results"), help="CSV output directory. Default: ptc_results.")
    parser.add_argument("--path", help="HID path for DL24P. Usually omit and auto-detect.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before layout cable changes.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned matrix without connecting to hardware.")
    parser.add_argument("--debug", action="store_true", help="Enable low-level DL24P debug output.")
    return parser


def validate_plan(args: argparse.Namespace) -> None:
    if args.base_current > MAX_CURRENT_A:
        raise ValueError(f"base current is above the DL24P limit of {MAX_CURRENT_A:.1f} A.")
    if args.overload_current > MAX_CURRENT_A:
        raise ValueError(f"overload current is above the DL24P limit of {MAX_CURRENT_A:.1f} A.")
    if args.trip_voltage_ratio >= 1.0:
        raise ValueError("--trip-voltage-ratio should be below 1.0 for voltage-collapse detection.")
    for duty in args.duty_cycles:
        if duty <= 0 or duty > 100:
            raise ValueError("duty cycles must be greater than 0 and no more than 100 percent.")


def print_plan(args: argparse.Namespace) -> None:
    requested_trial_slots = len(args.layouts) * len(args.duty_cycles) * args.trials
    print("PTC Test 3 pulse-overload plan")
    print(f"  ambient label: {args.ambient:.1f} C")
    print(f"  layouts: {len(args.layouts)}")
    print(f"  base current: {args.base_current:.3f} A")
    print(f"  overload current: {args.overload_current:.3f} A")
    print(f"  period: {args.period:.3f} s")
    print(f"  duty cycles: {', '.join(f'{d:g}%' for d in args.duty_cycles)}")
    print(f"  trials per point: {args.trials}")
    print(f"  requested trial slots: {requested_trial_slots}")
    print(f"  base settle before pulses: {args.base_settle_seconds:.1f} s")
    print(f"  target sample interval: {args.sample_interval:.3f} s")
    print(f"  first no-trip retry skip: {'off' if args.no_skip_after_first_no_trip else 'on'}")
    print(f"  max trial time: {args.max_test_seconds:.1f} s")
    print(f"  cooldown after each trip: {args.cooldown_seconds:.1f} s")


def wait_for_enter(message: str, enabled: bool) -> None:
    if enabled:
        input(message)


def sleep_countdown(seconds: float, stop_flag: callable) -> None:
    end_time = time.monotonic() + seconds
    while time.monotonic() < end_time and not stop_flag():
        remaining = end_time - time.monotonic()
        shown = max(0, int(round(remaining)))
        print(f"\rCooldown: {shown:4d} s remaining", end="", flush=True)
        time.sleep(min(1.0, max(0.0, remaining)))
    if seconds > 0:
        print("\rCooldown complete.          ")


def average_voltage(samples: Iterable[DL24PStatus]) -> float:
    values = [sample.voltage_v for sample in samples]
    return sum(values) / len(values) if values else 0.0


def phase_for_elapsed(elapsed_s: float, period_s: float, overload_on_s: float) -> tuple[str, float, int, float]:
    cycle_index = int(elapsed_s // period_s)
    phase_elapsed_s = elapsed_s - cycle_index * period_s
    if overload_on_s >= period_s or phase_elapsed_s < overload_on_s:
        return "overload", phase_elapsed_s, cycle_index, overload_on_s
    return "base", phase_elapsed_s - overload_on_s, cycle_index, period_s - overload_on_s


def run_trial(
    device: DL24P,
    args: argparse.Namespace,
    raw_log: CsvLogger,
    layout_index: int,
    layout_name: str,
    trial: int,
    duty_cycle_percent: float,
    stop_flag: callable,
) -> TrialResult:
    overload_on_s = args.period * duty_cycle_percent / 100.0
    print(
        f"\nLayout {layout_index}: {layout_name} | "
        f"trial {trial}/{args.trials} | duty = {duty_cycle_percent:g}% "
        f"({overload_on_s:.3f} s at {args.overload_current:.3f} A)"
    )

    device.select_cc_mode()
    time.sleep(0.15)
    baseline_samples = []
    baseline_started_at = time.monotonic()
    while time.monotonic() - baseline_started_at < args.settle_seconds and not stop_flag():
        baseline_samples.append(device.read_fast_status())
        time.sleep(args.sample_interval)
    if not baseline_samples:
        baseline_samples.append(device.read_fast_status())

    baseline_voltage_v = average_voltage(baseline_samples)
    ratio_threshold = baseline_voltage_v * args.trip_voltage_ratio
    trip_threshold_v = ratio_threshold if args.trip_voltage is None else max(ratio_threshold, args.trip_voltage)
    trip_current_threshold_a = args.trip_current
    current_arm_threshold_a = max(trip_current_threshold_a * 2.0, args.base_current * args.trip_current_arm_ratio)

    print(
        f"Baseline={baseline_voltage_v:.3f} V, "
        f"trip thresholds={trip_threshold_v:.3f} V or {trip_current_threshold_a:.3f} A "
        f"(current trip arms at {current_arm_threshold_a:.3f} A)"
    )

    device.set_cc_current_value(args.base_current)
    device.turn_on()

    max_temp = max(sample.mosfet_temp_c for sample in baseline_samples)
    last_status = baseline_samples[-1]
    current_trip_armed = False
    load_on_seen = False
    base_settle_started_at = time.monotonic()
    while time.monotonic() - base_settle_started_at < args.base_settle_seconds and not stop_flag():
        status = device.read_fast_status()
        last_status = status
        max_temp = max(max_temp, status.mosfet_temp_c)
        if status.load_on:
            load_on_seen = True
        if status.current_a >= current_arm_threshold_a:
            current_trip_armed = True
        print(
            f"\rBase settle {time.monotonic() - base_settle_started_at:5.2f}s  "
            f"{status.voltage_v:7.3f} V  "
            f"{status.current_a:7.3f} A",
            end="",
            flush=True,
        )
        time.sleep(args.sample_interval)
    if args.base_settle_seconds > 0:
        print()

    pulse_started_at = time.monotonic()
    next_sample_at = pulse_started_at
    previous_sample_at: Optional[float] = None
    commanded_current_a: Optional[float] = None
    sample_count = 0
    below_voltage_threshold_count = 0
    below_current_threshold_count = 0
    load_switched_off_count = 0
    tripped = False
    trip_reason = ""
    trip_time_s: Optional[float] = None
    cycles_completed = 0

    while not stop_flag():
        now = time.monotonic()
        elapsed_s = now - pulse_started_at
        if elapsed_s >= args.max_test_seconds:
            trip_reason = "timeout_no_trip"
            break

        commanded_phase, phase_elapsed_s, cycle_index, _phase_duration_s = phase_for_elapsed(
            elapsed_s, args.period, overload_on_s
        )
        next_current_a = args.overload_current if commanded_phase == "overload" else args.base_current
        if commanded_current_a is None or abs(next_current_a - commanded_current_a) > 1e-9:
            device.set_cc_current_value(next_current_a)
            commanded_current_a = next_current_a

        status = device.read_fast_status()
        sampled_at = time.monotonic()
        elapsed_s = sampled_at - pulse_started_at
        commanded_phase, phase_elapsed_s, cycle_index, _phase_duration_s = phase_for_elapsed(
            elapsed_s, args.period, overload_on_s
        )
        next_current_a = args.overload_current if commanded_phase == "overload" else args.base_current
        sample_dt_s = 0.0 if previous_sample_at is None else sampled_at - previous_sample_at
        previous_sample_at = sampled_at
        last_status = status
        sample_count += 1
        cycles_completed = cycle_index
        max_temp = max(max_temp, status.mosfet_temp_c)

        if status.load_on:
            load_on_seen = True
        if status.current_a >= current_arm_threshold_a:
            current_trip_armed = True

        is_voltage_trip = status.voltage_v <= trip_threshold_v
        is_current_trip = (
            current_trip_armed
            and elapsed_s >= args.trip_current_grace_seconds
            and status.current_a <= trip_current_threshold_a
        )
        is_load_off_trip = (
            load_on_seen
            and not status.load_on
            and elapsed_s >= args.trip_current_grace_seconds
        )
        below_voltage_threshold_count = below_voltage_threshold_count + 1 if is_voltage_trip else 0
        below_current_threshold_count = below_current_threshold_count + 1 if is_current_trip else 0
        load_switched_off_count = load_switched_off_count + 1 if is_load_off_trip else 0

        voltage_confirmed = below_voltage_threshold_count >= args.confirm_samples
        current_confirmed = below_current_threshold_count >= args.confirm_samples
        load_off_confirmed = load_switched_off_count >= args.confirm_samples
        if voltage_confirmed or current_confirmed or load_off_confirmed:
            tripped = True
            trip_time_s = elapsed_s
            if voltage_confirmed and current_confirmed:
                trip_reason = "voltage_and_current_below_threshold"
            elif current_confirmed:
                trip_reason = "current_below_threshold"
            elif voltage_confirmed:
                trip_reason = "voltage_below_threshold"
            else:
                trip_reason = "load_turned_off"

        raw_log.write(
            {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "elapsed_s": f"{elapsed_s:.3f}",
                "sample_dt_s": f"{sample_dt_s:.4f}",
                "ambient_c": f"{args.ambient:.2f}",
                "layout_index": layout_index,
                "layout_name": layout_name,
                "trial": trial,
                "duty_cycle_percent": f"{duty_cycle_percent:g}",
                "period_s": f"{args.period:.4f}",
                "base_current_a": f"{args.base_current:.4f}",
                "overload_current_a": f"{args.overload_current:.4f}",
                "commanded_phase": commanded_phase,
                "commanded_current_a": f"{next_current_a:.4f}",
                "phase_elapsed_s": f"{phase_elapsed_s:.4f}",
                "cycle_index": cycle_index,
                "baseline_voltage_v": f"{baseline_voltage_v:.4f}",
                "trip_voltage_threshold_v": f"{trip_threshold_v:.4f}",
                "trip_current_threshold_a": f"{trip_current_threshold_a:.4f}",
                "current_arm_threshold_a": f"{current_arm_threshold_a:.4f}",
                "voltage_v": f"{status.voltage_v:.4f}",
                "current_a": f"{status.current_a:.4f}",
                "power_w": f"{status.power_w:.4f}",
                "load": "ON" if status.load_on else "OFF",
                "mosfet_temp_c": f"{status.mosfet_temp_c:.3f}",
                "ext_temp_c": f"{status.ext_temp_c:.3f}",
                "fan_rpm": status.fan_rpm,
                "current_trip_armed": "yes" if current_trip_armed else "no",
                "tripped": "yes" if tripped else "no",
                "trip_reason": trip_reason,
            }
        )

        print(
            f"\r{elapsed_s:7.2f}s  cycle {cycle_index:4d}  "
            f"{commanded_phase:8s} cmd {next_current_a:5.2f} A  "
            f"{status.voltage_v:7.3f} V  "
            f"{status.current_a:7.3f} A  "
            f"{status.power_w:7.3f} W  "
            f"temp {status.mosfet_temp_c:5.1f} C",
            end="",
            flush=True,
        )

        if tripped:
            print()
            break

        next_sample_at += args.sample_interval
        sleep_s = next_sample_at - time.monotonic()
        if sleep_s > 0:
            time.sleep(sleep_s)

    try:
        device.turn_off()
    finally:
        print("\nLoad OFF.")

    if tripped:
        print(f"Trip detected after {trip_time_s:.3f} s ({cycles_completed} completed cycle(s)).")
    else:
        print(f"No trip detected. Reason: {trip_reason or 'stopped'}")

    return TrialResult(
        timestamp=datetime.now().isoformat(timespec="milliseconds"),
        ambient_c=args.ambient,
        layout_index=layout_index,
        layout_name=layout_name,
        trial=trial,
        duty_cycle_percent=duty_cycle_percent,
        period_s=args.period,
        overload_on_s=overload_on_s,
        base_current_a=args.base_current,
        overload_current_a=args.overload_current,
        tripped=tripped,
        trip_time_s=trip_time_s,
        trip_reason=trip_reason or "stopped",
        cycles_completed=cycles_completed,
        baseline_voltage_v=baseline_voltage_v,
        trip_voltage_threshold_v=trip_threshold_v,
        trip_current_threshold_a=trip_current_threshold_a,
        current_arm_threshold_a=current_arm_threshold_a,
        end_voltage_v=last_status.voltage_v,
        end_current_a=last_status.current_a,
        max_mosfet_temp_c=max_temp,
        sample_count=sample_count,
    )


def write_averages(path: Path, args: argparse.Namespace, results: list[TrialResult]) -> None:
    grouped: dict[tuple[int, str, float], list[TrialResult]] = {}
    for result in results:
        grouped.setdefault((result.layout_index, result.layout_name, result.duty_cycle_percent), []).append(result)

    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=AVERAGE_FIELDS)
        writer.writeheader()
        for (layout_index, layout_name, duty_cycle), group in sorted(grouped.items()):
            trip_times = [item.trip_time_s for item in group if item.trip_time_s is not None]
            row = {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "ambient_c": f"{args.ambient:.2f}",
                "layout_index": layout_index,
                "layout_name": layout_name,
                "duty_cycle_percent": f"{duty_cycle:g}",
                "trials_requested": args.trials,
                "trials_completed": len(group),
                "trips_detected": len(trip_times),
                "average_trip_time_s": "",
                "stdev_trip_time_s": "",
                "min_trip_time_s": "",
                "max_trip_time_s": "",
            }
            if trip_times:
                row["average_trip_time_s"] = f"{statistics.mean(trip_times):.3f}"
                row["min_trip_time_s"] = f"{min(trip_times):.3f}"
                row["max_trip_time_s"] = f"{max(trip_times):.3f}"
                if len(trip_times) > 1:
                    row["stdev_trip_time_s"] = f"{statistics.stdev(trip_times):.3f}"
            writer.writerow(row)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        validate_plan(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print_plan(args)
    if args.dry_run:
        return 0

    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    def should_stop() -> bool:
        return stop_requested

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = args.output_dir / f"ptc_inrush_raw_{run_id}.csv"
    trials_path = args.output_dir / f"ptc_inrush_trials_{run_id}.csv"
    averages_path = args.output_dir / f"ptc_inrush_averages_{run_id}.csv"

    raw_log = CsvLogger(raw_path, RAW_FIELDS)
    trial_log = CsvLogger(trials_path, TRIAL_FIELDS)
    results: list[TrialResult] = []
    device = DL24P(path=args.path, debug=args.debug)

    print(f"\nRaw samples: {raw_path}")
    print(f"Trial results: {trials_path}")
    print(f"Averages: {averages_path}")

    try:
        print("\nConnecting to DL24P USB HID...")
        device.connect()
        status = device.read_status()
        print(
            "Connected: "
            f"{status.voltage_v:.3f} V, "
            f"{status.current_a:.3f} A, "
            f"load={'ON' if status.load_on else 'OFF'}"
        )
        if status.load_on:
            print("Load was ON at start; turning it OFF before the test plan.")
            device.turn_off()
            time.sleep(0.5)

        for layout_index, layout_name in enumerate(args.layouts, start=1):
            if should_stop():
                break

            wait_for_enter(
                f"\nConnect wires to layout {layout_index}: {layout_name}, then press Enter...",
                enabled=not args.yes,
            )

            for duty_cycle in args.duty_cycles:
                for trial in range(1, args.trials + 1):
                    if should_stop():
                        break

                    result = run_trial(
                        device=device,
                        args=args,
                        raw_log=raw_log,
                        layout_index=layout_index,
                        layout_name=layout_name,
                        trial=trial,
                        duty_cycle_percent=duty_cycle,
                        stop_flag=should_stop,
                    )
                    results.append(result)
                    trial_log.write(result.as_csv_row())
                    write_averages(averages_path, args, results)

                    if (
                        trial == 1
                        and not result.tripped
                        and not args.no_skip_after_first_no_trip
                    ):
                        print(
                            "Trial 1 did not trip; skipping remaining repeats "
                            "for this duty cycle."
                        )
                        break

                    if should_stop():
                        break
                    if result.tripped and args.cooldown_seconds > 0:
                        sleep_countdown(args.cooldown_seconds, should_stop)

        return 130 if should_stop() else 0

    except (DL24PError, OSError, KeyboardInterrupt) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            device.turn_off()
        except Exception:
            pass
        device.close()
        raw_log.close()
        trial_log.close()
        if results:
            write_averages(averages_path, args, results)
        print("\nDisconnected. Results written:")
        print(f"  {raw_path}")
        print(f"  {trials_path}")
        print(f"  {averages_path}")


if __name__ == "__main__":
    raise SystemExit(main())

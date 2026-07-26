#!/usr/bin/env python3
"""Console runner for PTC fuse trip-time tests with a DL24P load.

This script is intentionally bench-friendly:
- prompts only before a new PTC layout so wires can be moved safely,
- runs repeated constant-current overload tests,
- detects a trip from voltage or current collapse at the DL24P input,
- writes raw samples, trial results, and averaged results to CSV.
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

DEFAULT_CURRENTS_A = [1.5, 1.75, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]

RAW_FIELDS = [
    "timestamp",
    "load_elapsed_s",
    "elapsed_s",
    "sample_dt_s",
    "ambient_c",
    "layout_index",
    "layout_name",
    "trial",
    "set_current_a",
    "baseline_voltage_v",
    "trip_voltage_threshold_v",
    "trip_current_threshold_a",
    "trip_current_arm_threshold_a",
    "start_current_threshold_a",
    "voltage_v",
    "current_a",
    "power_w",
    "load",
    "mosfet_temp_c",
    "ext_temp_c",
    "fan_rpm",
    "current_trip_armed",
    "test_timer_started",
    "tripped",
    "trip_reason",
    "trip_time_basis",
]

TRIAL_FIELDS = [
    "timestamp",
    "ambient_c",
    "layout_index",
    "layout_name",
    "trial",
    "set_current_a",
    "tripped",
    "trip_time_s",
    "trip_reason",
    "trip_time_basis",
    "baseline_voltage_v",
    "trip_voltage_threshold_v",
    "trip_current_threshold_a",
    "trip_current_arm_threshold_a",
    "start_current_threshold_a",
    "load_to_start_s",
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
    "set_current_a",
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
    set_current_a: float
    tripped: bool
    trip_time_s: Optional[float]
    trip_reason: str
    trip_time_basis: str
    baseline_voltage_v: float
    trip_voltage_threshold_v: float
    trip_current_threshold_a: float
    trip_current_arm_threshold_a: float
    start_current_threshold_a: float
    load_to_start_s: Optional[float]
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
            "set_current_a": f"{self.set_current_a:.4f}",
            "tripped": "yes" if self.tripped else "no",
            "trip_time_s": "" if self.trip_time_s is None else f"{self.trip_time_s:.3f}",
            "trip_reason": self.trip_reason,
            "trip_time_basis": self.trip_time_basis,
            "baseline_voltage_v": f"{self.baseline_voltage_v:.4f}",
            "trip_voltage_threshold_v": f"{self.trip_voltage_threshold_v:.4f}",
            "trip_current_threshold_a": f"{self.trip_current_threshold_a:.4f}",
            "trip_current_arm_threshold_a": f"{self.trip_current_arm_threshold_a:.4f}",
            "start_current_threshold_a": f"{self.start_current_threshold_a:.4f}",
            "load_to_start_s": "" if self.load_to_start_s is None else f"{self.load_to_start_s:.3f}",
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
    if value.lower() == "all":
        return DEFAULT_LAYOUTS
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
        description="Run constant-current PTC fuse trip-time tests with a DL24P."
    )
    parser.add_argument(
        "--currents",
        type=parse_float_list,
        default=DEFAULT_CURRENTS_A,
        metavar="A_LIST",
        help="Comma-separated load currents in amperes. Default: 1.5,1.75,2,3,4,5,6,7,8.",
    )
    parser.add_argument(
        "--layouts",
        type=parse_layouts,
        default=DEFAULT_LAYOUTS,
        help="Comma-separated layout names, or 'all'. Default: all 10 research layouts.",
    )
    parser.add_argument("--trials", type=positive_int, default=5, help="Trials per layout/current. Default: 5.")
    parser.add_argument("--ambient", type=float, default=22.0, metavar="C", help="Ambient temperature label. Default: 22 C.")
    parser.add_argument("--sample-interval", type=positive_float, default=0.02, metavar="S", help="Target fast-sampling period. Default: 0.02 s.")
    parser.add_argument("--max-test-seconds", type=positive_float, default=60.0, metavar="S", help="Stop a no-trip trial after this time. Default: 60 s.")
    parser.add_argument("--settle-seconds", type=nonnegative_float, default=0.20, metavar="S", help="Pre-load voltage baseline window. Default: 0.20 s.")
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
        "--trip-current-ratio",
        type=nonnegative_float,
        default=0.05,
        help="Trip when measured current is below set current multiplied by this ratio. Default: 0.05.",
    )
    parser.add_argument(
        "--trip-current",
        type=nonnegative_float,
        default=0.05,
        metavar="A",
        help="Absolute current-collapse threshold. Default: 0.05 A.",
    )
    parser.add_argument(
        "--trip-current-arm-ratio",
        type=nonnegative_float,
        default=0.10,
        help="Arm current-collapse detection after current reaches this fraction of set current. Default: 0.10.",
    )
    parser.add_argument(
        "--trip-current-grace-seconds",
        type=nonnegative_float,
        default=0.02,
        metavar="S",
        help="Ignore current-collapse detection for this long after load-on. Default: 0.02 s.",
    )
    parser.add_argument(
        "--start-current-ratio",
        type=positive_float,
        default=0.90,
        help="Start trip timer after current first reaches this fraction of set current. Default: 0.90.",
    )
    parser.add_argument("--confirm-samples", type=positive_int, default=1, help="Consecutive samples required to confirm trip. Default: 1.")
    parser.add_argument(
        "--no-skip-after-first-no-trip",
        action="store_true",
        help="Run all requested trials even when trial 1 for a layout/current does not trip.",
    )
    parser.add_argument("--cooldown-seconds", type=nonnegative_float, default=25.0, metavar="S", help="Cooldown after each detected trip. Default: 25 s.")
    parser.add_argument("--output-dir", type=Path, default=Path("ptc_results"), help="CSV output directory. Default: ptc_results.")
    parser.add_argument("--path", help="HID path for DL24P. Usually omit and auto-detect.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before layout cable changes.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned matrix without connecting to hardware.")
    parser.add_argument("--debug", action="store_true", help="Enable low-level DL24P debug output.")
    return parser


def validate_plan(args: argparse.Namespace) -> None:
    for current in args.currents:
        if current > MAX_CURRENT_A:
            raise ValueError(
                f"current {current:.3f} A is above the DL24P limit of "
                f"{MAX_CURRENT_A:.1f} A."
            )
    if args.trip_voltage_ratio >= 1.0:
        raise ValueError("--trip-voltage-ratio should be below 1.0 for voltage-collapse detection.")
    if args.start_current_ratio > 1.0:
        raise ValueError("--start-current-ratio should be 1.0 or lower.")


def print_plan(args: argparse.Namespace) -> None:
    total_trials = len(args.layouts) * len(args.currents) * args.trials
    print("PTC constant-current trip-time test plan")
    print(f"  ambient label: {args.ambient:.1f} C")
    print(f"  layouts: {len(args.layouts)}")
    print(f"  currents: {', '.join(f'{c:g} A' for c in args.currents)}")
    print(f"  trials per point: {args.trials}")
    print(f"  total trials: {total_trials}")
    print(f"  target sample interval: {args.sample_interval:.3f} s")
    print(f"  trip timer starts at: {args.start_current_ratio:.1%} of set current")
    print(
        "  current-collapse trip threshold: "
        f"max({args.trip_current:.3f} A, {args.trip_current_ratio:.1%} of set current)"
    )
    print(f"  current-collapse arming threshold: {args.trip_current_arm_ratio:.1%} of set current")
    print("  pre-start current collapse: recorded from load-on after current-collapse is armed")
    print("  unexpected load-off status: recorded as a trip after load was seen ON")
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


def run_trial(
    device: DL24P,
    args: argparse.Namespace,
    raw_log: CsvLogger,
    layout_index: int,
    layout_name: str,
    trial: int,
    set_current_a: float,
    stop_flag: callable,
) -> TrialResult:
    print(
        f"\nLayout {layout_index}: {layout_name} | "
        f"trial {trial}/{args.trials} | current = {set_current_a:.3f} A"
    )

    device.set_cc_current(set_current_a)
    time.sleep(0.2)
    baseline_samples = []
    baseline_started_at = time.monotonic()
    while time.monotonic() - baseline_started_at < args.settle_seconds and not stop_flag():
        baseline_samples.append(device.read_fast_status())
        time.sleep(args.sample_interval)

    if not baseline_samples:
        baseline_samples.append(device.read_fast_status())

    baseline_voltage_v = average_voltage(baseline_samples)
    ratio_threshold = baseline_voltage_v * args.trip_voltage_ratio
    if args.trip_voltage is None:
        trip_threshold_v = ratio_threshold
    else:
        trip_threshold_v = max(ratio_threshold, args.trip_voltage)
    trip_current_threshold_a = max(args.trip_current, set_current_a * args.trip_current_ratio)
    trip_current_arm_threshold_a = max(
        trip_current_threshold_a * 2.0,
        set_current_a * args.trip_current_arm_ratio,
    )
    start_current_threshold_a = set_current_a * args.start_current_ratio

    if baseline_voltage_v < 0.5:
        print(
            "Warning: loaded baseline voltage is below 0.5 V. "
            "Check PSU/wiring or set --trip-voltage explicitly.",
            file=sys.stderr,
        )

    print(
        f"Baseline={baseline_voltage_v:.3f} V, "
        f"trip thresholds={trip_threshold_v:.3f} V or "
        f"{trip_current_threshold_a:.3f} A "
        f"(current trip arms at {trip_current_arm_threshold_a:.3f} A; "
        f"timer starts at {start_current_threshold_a:.3f} A)"
    )

    device.turn_on()
    started_at = time.monotonic()
    next_sample_at = started_at
    previous_sample_at: Optional[float] = None
    max_temp = max(sample.mosfet_temp_c for sample in baseline_samples)
    last_status = baseline_samples[-1]
    sample_count = 0
    below_voltage_threshold_count = 0
    below_current_threshold_count = 0
    below_pre_start_current_threshold_count = 0
    load_switched_off_count = 0
    current_trip_armed = False
    load_on_seen = False
    tripped = False
    trip_reason = ""
    trip_time_s: Optional[float] = None
    trip_time_basis = ""
    test_started_at: Optional[float] = None
    load_to_start_s: Optional[float] = None

    while not stop_flag():
        now = time.monotonic()
        load_elapsed_s = now - started_at
        if load_elapsed_s >= args.max_test_seconds:
            trip_reason = "timeout_no_trip"
            break

        status = device.read_fast_status()
        sampled_at = time.monotonic()
        load_elapsed_s = sampled_at - started_at
        if test_started_at is None and status.current_a >= start_current_threshold_a:
            test_started_at = sampled_at
            load_to_start_s = load_elapsed_s
            print(f"\nTrip timer started at {load_to_start_s:.3f} s after load-on.")
        elapsed_s = 0.0 if test_started_at is None else sampled_at - test_started_at
        sample_dt_s = 0.0 if previous_sample_at is None else sampled_at - previous_sample_at
        previous_sample_at = sampled_at
        last_status = status
        sample_count += 1
        max_temp = max(max_temp, status.mosfet_temp_c)
        if status.load_on:
            load_on_seen = True
        if status.current_a >= trip_current_arm_threshold_a:
            current_trip_armed = True

        is_voltage_trip = (
            test_started_at is not None
            and status.voltage_v <= trip_threshold_v
        )
        is_pre_start_current_trip = (
            current_trip_armed
            and
            test_started_at is None
            and
            load_elapsed_s >= args.trip_current_grace_seconds
            and status.current_a <= trip_current_threshold_a
        )
        is_load_off_trip = (
            load_on_seen
            and
            not status.load_on
            and
            load_elapsed_s >= args.trip_current_grace_seconds
        )
        is_current_trip = (
            current_trip_armed
            and
            test_started_at is not None
            and
            elapsed_s >= args.trip_current_grace_seconds
            and status.current_a <= trip_current_threshold_a
        )
        below_voltage_threshold_count = (
            below_voltage_threshold_count + 1 if is_voltage_trip else 0
        )
        below_current_threshold_count = (
            below_current_threshold_count + 1 if is_current_trip else 0
        )
        below_pre_start_current_threshold_count = (
            below_pre_start_current_threshold_count + 1
            if is_pre_start_current_trip
            else 0
        )
        load_switched_off_count = (
            load_switched_off_count + 1 if is_load_off_trip else 0
        )

        voltage_confirmed = below_voltage_threshold_count >= args.confirm_samples
        current_confirmed = below_current_threshold_count >= args.confirm_samples
        pre_start_current_confirmed = (
            below_pre_start_current_threshold_count >= args.confirm_samples
        )
        load_off_confirmed = load_switched_off_count >= args.confirm_samples
        if (
            voltage_confirmed
            or current_confirmed
            or pre_start_current_confirmed
            or load_off_confirmed
        ):
            tripped = True
            if voltage_confirmed or current_confirmed:
                trip_time_s = elapsed_s
                trip_time_basis = "elapsed_s_after_start_current"
                if voltage_confirmed and current_confirmed:
                    trip_reason = "voltage_and_current_below_threshold"
                elif current_confirmed:
                    trip_reason = "current_below_threshold"
                else:
                    trip_reason = "voltage_below_threshold"
            elif pre_start_current_confirmed:
                trip_time_s = load_elapsed_s
                trip_time_basis = "load_elapsed_s_before_start_current"
                trip_reason = "current_below_threshold_before_start_current"
            else:
                trip_time_s = load_elapsed_s if test_started_at is None else elapsed_s
                trip_time_basis = (
                    "load_elapsed_s_before_start_current"
                    if test_started_at is None
                    else "elapsed_s_after_start_current"
                )
                trip_reason = (
                    "load_turned_off_before_start_current"
                    if test_started_at is None
                    else "load_turned_off"
                )

        raw_log.write(
            {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "load_elapsed_s": f"{load_elapsed_s:.3f}",
                "elapsed_s": "" if test_started_at is None else f"{elapsed_s:.3f}",
                "sample_dt_s": f"{sample_dt_s:.4f}",
                "ambient_c": f"{args.ambient:.2f}",
                "layout_index": layout_index,
                "layout_name": layout_name,
                "trial": trial,
                "set_current_a": f"{set_current_a:.4f}",
                "baseline_voltage_v": f"{baseline_voltage_v:.4f}",
                "trip_voltage_threshold_v": f"{trip_threshold_v:.4f}",
                "trip_current_threshold_a": f"{trip_current_threshold_a:.4f}",
                "trip_current_arm_threshold_a": f"{trip_current_arm_threshold_a:.4f}",
                "start_current_threshold_a": f"{start_current_threshold_a:.4f}",
                "voltage_v": f"{status.voltage_v:.4f}",
                "current_a": f"{status.current_a:.4f}",
                "power_w": f"{status.power_w:.4f}",
                "load": "ON" if status.load_on else "OFF",
                "mosfet_temp_c": f"{status.mosfet_temp_c:.3f}",
                "ext_temp_c": f"{status.ext_temp_c:.3f}",
                "fan_rpm": status.fan_rpm,
                "current_trip_armed": "yes" if current_trip_armed else "no",
                "test_timer_started": "yes" if test_started_at is not None else "no",
                "tripped": "yes" if tripped else "no",
                "trip_reason": trip_reason,
                "trip_time_basis": trip_time_basis,
            }
        )

        print(
            f"\r{load_elapsed_s:7.2f}s load  "
            f"{elapsed_s:7.2f}s test  "
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
        print(f"Trip detected after {trip_time_s:.3f} s.")
    else:
        print(f"No trip detected. Reason: {trip_reason or 'stopped'}")

    return TrialResult(
        timestamp=datetime.now().isoformat(timespec="milliseconds"),
        ambient_c=args.ambient,
        layout_index=layout_index,
        layout_name=layout_name,
        trial=trial,
        set_current_a=set_current_a,
        tripped=tripped,
        trip_time_s=trip_time_s,
        trip_reason=trip_reason or "stopped",
        trip_time_basis=trip_time_basis,
        baseline_voltage_v=baseline_voltage_v,
        trip_voltage_threshold_v=trip_threshold_v,
        trip_current_threshold_a=trip_current_threshold_a,
        trip_current_arm_threshold_a=trip_current_arm_threshold_a,
        start_current_threshold_a=start_current_threshold_a,
        load_to_start_s=load_to_start_s,
        end_voltage_v=last_status.voltage_v,
        end_current_a=last_status.current_a,
        max_mosfet_temp_c=max_temp,
        sample_count=sample_count,
    )


def write_averages(path: Path, args: argparse.Namespace, results: list[TrialResult]) -> None:
    grouped: dict[tuple[int, str, float], list[TrialResult]] = {}
    for result in results:
        grouped.setdefault((result.layout_index, result.layout_name, result.set_current_a), []).append(result)

    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=AVERAGE_FIELDS)
        writer.writeheader()
        for (layout_index, layout_name, set_current_a), group in sorted(grouped.items()):
            trip_times = [item.trip_time_s for item in group if item.trip_time_s is not None]
            row = {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "ambient_c": f"{args.ambient:.2f}",
                "layout_index": layout_index,
                "layout_name": layout_name,
                "set_current_a": f"{set_current_a:.4f}",
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
    raw_path = args.output_dir / f"ptc_raw_{run_id}.csv"
    trials_path = args.output_dir / f"ptc_trials_{run_id}.csv"
    averages_path = args.output_dir / f"ptc_averages_{run_id}.csv"

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

            for set_current_a in args.currents:
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
                        set_current_a=set_current_a,
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
                            "for this layout/current point."
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

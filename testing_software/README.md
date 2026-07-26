# DL24P Minimal USB HID Controller

Standalone minimal controller for the aTorch DL24P active load. It does not
import the original `load_test_bench` codebase; it only needs `hidapi`.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Basic DL24P Use

Set constant-current mode to 100 mA, enable the load, and print CSV readings:

```bash
python3 dl24p_minimal.py --current 0.100 --enable
```

Run for a fixed time:

```bash
python3 dl24p_minimal.py --current 0.100 --enable --duration 60
```

By default, if this program enables the load, it turns the load off on exit.
Use `--leave-on-exit` to keep it on after the program stops.

## Built-In USB Prepare

On macOS, after power-cycling the DL24P, the device may need a HID `SET_IDLE`
request before it responds to normal HID packets. That preparation is built
into `dl24p_minimal.py`; the original repository is not needed.

Run it manually when needed:

```bash
sudo .venv/bin/python3 dl24p_minimal.py --prepare
```

Normal commands also try this preparation once automatically after a
no-response query. If macOS blocks the request, run the manual `sudo` command
above, then rerun the normal test command without `sudo`.

## PTC Fuse Test Runner

`ptc_fuse_tester.py` runs constant-current overload trip-time tests. It prompts
only when moving to another layout, logs every sample, logs every trial, and
keeps an averages CSV updated after each completed trial. Tests within the same
layout continue automatically after cooldown.

Dry-run the default research matrix:

```bash
python3 ptc_fuse_tester.py --dry-run
```

Run a small smoke test:

```bash
python3 ptc_fuse_tester.py \
  --layouts normal_routing \
  --currents 2 \
  --trials 1 \
  --max-test-seconds 30 \
  --cooldown-seconds 10
```

Run the default Test 1 current matrix for a Littelfuse `1812L110/33MR` at 22 C:

```bash
python3 ptc_fuse_tester.py --ambient 22
```

Useful options:

```bash
--currents 1.5,1.75,2,3,4,5,6,7,8
--trip-voltage-ratio 0.50
--trip-voltage 3.0
--trip-current-ratio 0.05
--trip-current 0.05
--trip-current-arm-ratio 0.10
--start-current-ratio 0.90
--sample-interval 0.02
--max-test-seconds 60
--cooldown-seconds 25
```

Trip detection is based on voltage measured at the DL24P input. The script
uses the pre-load voltage during `--settle-seconds` as a baseline, switches the
load on, then starts `trip_time_s` only after measured current first reaches
`--start-current-ratio` of the set current. It still logs `load_elapsed_s` and
`load_to_start_s`, so the DL24P current ramp delay remains visible in the data.
It declares a trip when voltage stays below the threshold for `--confirm-samples`
consecutive samples. If
`--trip-voltage` is provided, the larger of the absolute threshold and the ratio
threshold is used. It also declares a trip when measured current collapses below
`max(--trip-current, set_current * --trip-current-ratio)`, which catches trips
where the PTC opens and the voltage recovers toward the PSU output. Current
collapse detection is armed only after measured current first reaches
`--trip-current-arm-ratio` of the set current, so startup samples before the
active load begins drawing current do not count as trips.
If the PTC trips before current reaches `--start-current-ratio`, the trial is
still recorded once current-collapse detection was armed. In that case
`trip_time_s` is measured from load-on and `trip_time_basis` is
`load_elapsed_s_before_start_current`; normal corrected-timer trips use
`elapsed_s_after_start_current`.
If the DL24P reports `load=ON` and then unexpectedly reports `load=OFF` during
the trial, that is also recorded as a trip. This catches very fast high-current
events where the load turns off before the sampled telemetry captures the
current pulse.

Defaults are tuned for `1812L110/33MR`: load currents are
`1.5,1.75,2,3,4,5,6,7,8 A`,
there are `5` trials per point, cooldown after a detected trip is `25 s`, no-trip
timeout is `60 s`, the target sample interval is `20 ms`, and `confirm-samples`
defaults to `1` so fast trips near 8 A are not filtered out. The raw CSV includes
`sample_dt_s`, which shows actual time between acquired samples.

If trial 1 for a layout/current point does not trip, the tester skips the
remaining repeats for that point and moves on without a cooldown wait. Cooldown is applied
after detected trips. Use `--no-skip-after-first-no-trip` if you want all
repeats anyway.

CSV files are written under `ptc_results/` by default:

- `ptc_raw_<timestamp>.csv`: every sampled DL24P reading
- `ptc_trials_<timestamp>.csv`: one row per trial
- `ptc_averages_<timestamp>.csv`: averaged trip time per layout and current

## PTC Test 3 Pulse-Overload Runner

`ptc_inrush_tester.py` runs the short-burst operation test: a base current is
applied continuously, and periodic overload pulses are commanded until the PTC
trips or the trial times out.

Default Test 3 settings are:

- base current: `0.55 A`
- overload current: `4 A`
- period: `1 s`
- duty cycles: `5,10,20,30,40,50,60,70,80,90%`
- trials per duty cycle: `3`
- cooldown after trip: `25 s`
- no-trip timeout: `30 s`
- if trial 1 for a duty cycle does not trip, remaining repeats for that duty
  are skipped by default

Run the default Test 3 plan for one connected layout:

```bash
python3 ptc_inrush_tester.py --layout normal_routing
```

The equivalent explicit command is:

```bash
python3 ptc_inrush_tester.py \
  --layouts normal_routing \
  --base-current 0.55 \
  --overload-current 4 \
  --period 1 \
  --duty-cycles 5,10,20,30,40,50,60,70,80,90 \
  --trials 3 \
  --cooldown-seconds 25 \
  --max-test-seconds 30
```

Use `--dry-run` to print the plan without connecting to the DL24P. The raw CSV
logs both `commanded_current_a` and measured `current_a`, which is useful for
checking whether very short duty cycles are actually reached by the load. Use
`--no-skip-after-first-no-trip` if you want all repeats even after trial 1 does
not trip.

## PTC Test 3 Chart Generator

`ptc_plot_inrush_results.py` creates an SVG chart from a
`ptc_inrush_trials_*.csv` file. The X axis is overload duty cycle and the Y
axis is completed overload cycles before trip.

```bash
python3 ptc_plot_inrush_results.py ptc_results/ptc_inrush_trials_20260725_231606.csv
```

No-trip rows are skipped because they do not have a real cycles-before-trip
value. Repeated trials are averaged per layout/duty cycle, with individual trial
points shown faintly behind the mean curve.

## PTC Chart Generator

`ptc_plot_results.py` creates an SVG chart from either a `ptc_trials_*.csv` or
`ptc_averages_*.csv` file. The X axis is current in amperes and the Y axis is
trip time in seconds.

```bash
python3 ptc_plot_results.py ptc_results/ptc_averages_20260725_123429.csv
```

By default it writes `<input>_chart.svg` next to the CSV. Useful options:

```bash
--output chart.svg
--title "1812L110/33MR PTC trip time"
--linear-y
```

Charts use a logarithmic Y axis by default, which is usually easier to read for
PTC trip times spanning milliseconds to minutes. Use `--linear-y` only when the
time range is narrow. The first five layouts use solid lines with five colors;
the next five reuse those colors with dashed lines.

If `--prepare` reports that libusb cannot be loaded, install libusb once with
Homebrew:

```bash
brew install libusb
```

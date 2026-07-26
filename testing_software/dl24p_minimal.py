#!/usr/bin/env python3
"""Small standalone USB-HID controller for the aTorch DL24P load.

This intentionally avoids the GUI application's package code. It supports the
basic workflow: connect, select CC mode, set current, optionally enable load,
and print measured/status parameters as CSV.
"""

from __future__ import annotations

import argparse
import signal
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

try:
    import hid
except ImportError:  # pragma: no cover - exercised only without dependency
    hid = None


VENDOR_ID = 0x0483
PRODUCT_ID = 0x5750
REPORT_SIZE = 64
MAX_CURRENT_A = 24.0

USB_PREPARE_BM_REQUEST_TYPE = 0x21
USB_PREPARE_B_REQUEST_SET_IDLE = 0x0A
USB_PREPARE_INTERFACE = 0


class DL24PError(Exception):
    """DL24P communication or protocol error."""


@dataclass(frozen=True)
class DL24PStatus:
    voltage_v: float
    current_a: float
    power_w: float
    capacity_mah: float
    energy_wh: float
    runtime_s: int
    load_on: bool
    mosfet_temp_c: float
    ext_temp_c: float
    fan_rpm: int
    mode: Optional[int]
    setpoint: Optional[float]
    voltage_cutoff_v: Optional[float]


class DL24P:
    """Minimal USB HID transport/protocol for DL24P."""

    CMD_HEADER = 0x55
    RESP_HEADER = 0xAA
    VERSION = 0x05
    CMD_TYPE = 0x01

    SUB_LIVE_DATA = 0x03
    SUB_COUNTERS = 0x05
    SUB_SET_VALUE = 0x21
    SUB_POWER = 0x25

    MODE_SUBCOMMANDS = {
        0: 0x47,  # CC
        1: 0x4A,  # CP
        2: 0x48,  # CV
        3: 0x49,  # CR
    }

    QUERY_DATA = b"\x0b\x00\x8c"

    def __init__(
        self,
        path: Optional[str] = None,
        debug: bool = False,
        auto_prepare: bool = True,
    ) -> None:
        self.path = path
        self.debug = debug
        self.auto_prepare = auto_prepare
        self._dev = None
        self._prepared_after_no_response = False

    @staticmethod
    def list_devices() -> list[dict]:
        if hid is None:
            return []
        return list(hid.enumerate(VENDOR_ID, PRODUCT_ID))

    @staticmethod
    def prepare_usb(verbose: bool = True) -> None:
        """Send the HID SET_IDLE request needed by some macOS power cycles.

        On macOS this usually requires running the same script with sudo because
        the kernel claims the HID interface.
        """
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise DL24PError(
                "Missing dependency for built-in USB preparation: install with "
                "`pip install pyusb`."
            ) from exc

        device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if device is None:
            raise DL24PError(
                f"DL24P not found for USB preparation "
                f"(VID={VENDOR_ID:#06x}, PID={PRODUCT_ID:#06x})."
            )

        if verbose:
            try:
                print(f"Found DL24P: {device.manufacturer} {device.product}")
            except Exception:
                print("Found DL24P.")

        try:
            if device.is_kernel_driver_active(USB_PREPARE_INTERFACE):
                if verbose:
                    print("Detaching kernel driver...")
                device.detach_kernel_driver(USB_PREPARE_INTERFACE)
        except (usb.core.USBError, NotImplementedError) as exc:
            if verbose:
                print(f"Warning: could not detach kernel driver: {exc}", file=sys.stderr)

        try:
            device.ctrl_transfer(
                USB_PREPARE_BM_REQUEST_TYPE,
                USB_PREPARE_B_REQUEST_SET_IDLE,
                0x0000,
                USB_PREPARE_INTERFACE,
            )
            if verbose:
                print("SET_IDLE sent successfully.")
        except usb.core.USBError as exc:
            raise DL24PError(
                "Failed to send built-in USB prepare request. On macOS, run "
                "this script with sudo, for example: "
                "`sudo .venv/bin/python3 dl24p_minimal.py --prepare`."
            ) from exc
        finally:
            try:
                usb.util.dispose_resources(device)
            except Exception:
                pass

        try:
            device.attach_kernel_driver(USB_PREPARE_INTERFACE)
            if verbose:
                print("Kernel driver reattached.")
        except (usb.core.USBError, NotImplementedError) as exc:
            if verbose:
                print(f"Warning: could not reattach kernel driver: {exc}", file=sys.stderr)
                print("You may need to unplug and replug the device.")

    def connect(self) -> None:
        if hid is None:
            raise DL24PError("Missing dependency: install with `pip install hidapi`.")

        path = self.path
        if path is None:
            devices = self.list_devices()
            if not devices:
                raise DL24PError(
                    f"DL24P not found (VID={VENDOR_ID:#06x}, PID={PRODUCT_ID:#06x})."
                )
            path = devices[0]["path"]

        self._dev = hid.device()
        try:
            self._dev.open_path(path if isinstance(path, bytes) else str(path).encode())
            self.path = path.decode() if isinstance(path, bytes) else str(path)
            self._dev.set_nonblocking(False)
            self._clear_pending_reads()
            self._initialize_device()
            self._prepared_after_no_response = False
        except Exception as exc:
            self.close()
            raise DL24PError(f"Could not open DL24P USB HID device: {exc}") from exc

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    def set_cc_current(self, current_a: float) -> None:
        """Select CC mode and set current.

        This is the conservative helper used by simple scripts. For pulse tests
        where setpoints change quickly, call ``select_cc_mode`` once and then
        ``set_cc_current_value`` for each edge.
        """
        self.select_cc_mode()
        time.sleep(0.15)
        self.set_cc_current_value(current_a)

    def select_cc_mode(self) -> None:
        self.set_mode(0)

    def set_cc_current_value(self, current_a: float) -> None:
        if not 0.0 <= current_a <= MAX_CURRENT_A:
            raise ValueError(f"current must be between 0 and {MAX_CURRENT_A:.1f} A")
        self._send(self.SUB_SET_VALUE, struct.pack(">f", current_a))

    def set_mode(self, mode: int) -> None:
        try:
            sub_command = self.MODE_SUBCOMMANDS[mode]
        except KeyError as exc:
            raise ValueError("mode must be 0=CC, 1=CP, 2=CV, or 3=CR") from exc
        self._send(sub_command, b"\x00\x00\x00\x00")

    def turn_on(self) -> None:
        self._send(self.SUB_POWER, b"\x01\x00\x00\x00")

    def turn_off(self) -> None:
        self._send(self.SUB_POWER, b"\x00\x00\x00\x00")

    def read_status(self) -> DL24PStatus:
        counters_resp = self._query(self.SUB_COUNTERS, self.QUERY_DATA)
        live_resp = self._query(self.SUB_LIVE_DATA, self.QUERY_DATA)

        counters = self._parse_counters(counters_resp[4:62])
        return self._parse_live_data(live_resp[4:62], counters)

    def read_fast_status(self) -> DL24PStatus:
        """Read measured values with one HID query.

        This skips the live-data/settings query and is intended for high-speed
        trip detection where voltage/current timing matters more than mode or
        setpoint metadata.
        """
        counters_resp = self._query(self.SUB_COUNTERS, self.QUERY_DATA)
        counters = self._parse_counters(counters_resp[4:62])
        return self._status_from_counters(counters)

    def _initialize_device(self) -> None:
        # The OEM application sends this short sequence before regular polling.
        for cmd_type in range(0x01, 0x0B):
            packet = self._build_packet(cmd_type, 0x04, b"\x00\x00\x00\x00")
            self._write_packet(packet)
            time.sleep(0.16)
        time.sleep(0.6)

    def _clear_pending_reads(self) -> None:
        if self._dev is None:
            return
        self._dev.set_nonblocking(True)
        try:
            for _ in range(10):
                if not self._dev.read(REPORT_SIZE):
                    break
        finally:
            self._dev.set_nonblocking(False)

    def _send(self, sub_cmd: int, data: bytes = b"") -> None:
        self._write_packet(self._build_packet(self.CMD_TYPE, sub_cmd, data))

    def _query(self, sub_cmd: int, data: bytes = b"", timeout_ms: int = 700) -> bytes:
        packet = self._build_packet(self.CMD_TYPE, sub_cmd, data)
        self._write_packet(packet)
        response = bytes(self._dev.read(REPORT_SIZE, timeout_ms=timeout_ms))

        if not response and self.auto_prepare and not self._prepared_after_no_response:
            self._prepared_after_no_response = True
            print(
                "DL24P did not respond; trying built-in USB prepare and reconnect...",
                file=sys.stderr,
            )
            self._prepare_and_reconnect()
            packet = self._build_packet(self.CMD_TYPE, sub_cmd, data)
            self._write_packet(packet)
            response = bytes(self._dev.read(REPORT_SIZE, timeout_ms=timeout_ms))

        if not response:
            raise DL24PError(
                "No response from DL24P. On macOS after a power cycle, run the "
                "built-in prepare step with sudo: "
                "`sudo .venv/bin/python3 dl24p_minimal.py --prepare`."
            )
        if response[0] != self.RESP_HEADER or response[1] != self.VERSION:
            raise DL24PError(f"Unexpected response header: {response[:8].hex()}")
        if response[2] != self.CMD_TYPE or response[3] != sub_cmd:
            self._log(f"warning: response was {response[2]:02x}/{response[3]:02x}")
        return response

    def _prepare_and_reconnect(self) -> None:
        saved_path = self.path
        self.close()
        self.prepare_usb(verbose=True)
        self.path = saved_path
        try:
            self.connect()
        except DL24PError:
            self.path = None
            self.connect()

    def _write_packet(self, packet: bytes) -> None:
        if self._dev is None:
            raise DL24PError("Device is not connected")
        self._log("tx " + packet[:12].hex())
        try:
            self._dev.write(b"\x00" + packet)
        except Exception as exc:
            raise DL24PError(f"USB HID write failed: {exc}") from exc

    @classmethod
    def _build_packet(cls, cmd_type: int, sub_cmd: int, data: bytes = b"") -> bytes:
        packet = bytearray(REPORT_SIZE)
        packet[0] = cls.CMD_HEADER
        packet[1] = cls.VERSION
        packet[2] = cmd_type
        packet[3] = sub_cmd

        data_end = 4
        for index, value in enumerate(data[:58]):
            packet[4 + index] = value
            data_end = 5 + index

        packet[data_end] = 0xEE
        packet[data_end + 1] = 0xFF
        return bytes(packet)

    @staticmethod
    def _parse_counters(payload: bytes) -> dict:
        def u16(offset: int) -> int:
            if len(payload) < offset + 2:
                return 0
            return struct.unpack("<H", payload[offset : offset + 2])[0]

        def u32(offset: int) -> int:
            if len(payload) < offset + 4:
                return 0
            return struct.unpack("<I", payload[offset : offset + 4])[0]

        voltage_mv = u16(4)
        current_ma = u16(8)
        energy_mwh = u32(20)
        capacity_uah = u32(24)
        runtime_ticks = u32(28)
        ext_temp_mc = u32(32)
        mosfet_temp_mc = u32(36)
        fan_mrpm = u32(40)

        return {
            "voltage_v": voltage_mv / 1000.0,
            "current_a": current_ma / 1000.0,
            "power_w": voltage_mv * current_ma / 1_000_000.0,
            "energy_wh": energy_mwh / 1000.0,
            "capacity_mah": capacity_uah / 1000.0,
            "runtime_s": runtime_ticks // 48,
            "ext_temp_c": ext_temp_mc / 1000.0,
            "mosfet_temp_c": mosfet_temp_mc / 1000.0,
            "fan_rpm": fan_mrpm // 1000,
            "load_on": len(payload) > 48 and payload[48] == 0x01,
        }

    @staticmethod
    def _parse_live_data(payload: bytes, counters: dict) -> DL24PStatus:
        def f32(offset: int) -> Optional[float]:
            if len(payload) < offset + 4:
                return None
            return struct.unpack(">f", payload[offset : offset + 4])[0]

        mode = payload[44] if len(payload) > 44 else None
        return DL24PStatus(
            voltage_v=counters["voltage_v"],
            current_a=counters["current_a"],
            power_w=counters["power_w"],
            capacity_mah=counters["capacity_mah"],
            energy_wh=counters["energy_wh"],
            runtime_s=counters["runtime_s"],
            load_on=counters["load_on"],
            mosfet_temp_c=counters["mosfet_temp_c"],
            ext_temp_c=counters["ext_temp_c"],
            fan_rpm=counters["fan_rpm"],
            mode=mode,
            setpoint=f32(0),
            voltage_cutoff_v=f32(16),
        )

    @staticmethod
    def _status_from_counters(counters: dict) -> DL24PStatus:
        return DL24PStatus(
            voltage_v=counters["voltage_v"],
            current_a=counters["current_a"],
            power_w=counters["power_w"],
            capacity_mah=counters["capacity_mah"],
            energy_wh=counters["energy_wh"],
            runtime_s=counters["runtime_s"],
            load_on=counters["load_on"],
            mosfet_temp_c=counters["mosfet_temp_c"],
            ext_temp_c=counters["ext_temp_c"],
            fan_rpm=counters["fan_rpm"],
            mode=None,
            setpoint=None,
            voltage_cutoff_v=None,
        )

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[debug] {message}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set DL24P constant-current load and monitor readings."
    )
    parser.add_argument("--current", type=float, metavar="A", help="CC setpoint in amperes.")
    parser.add_argument("--enable", action="store_true", help="Enable load after setting current.")
    parser.add_argument("--interval", type=float, default=1.0, help="CSV print interval in seconds.")
    parser.add_argument("--duration", type=float, help="Stop after this many seconds.")
    parser.add_argument("--path", help="HID path. Usually omit and auto-detect.")
    parser.add_argument("--list", action="store_true", help="List matching USB HID devices and exit.")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Run built-in USB SET_IDLE preparation and exit. On macOS this usually needs sudo.",
    )
    parser.add_argument(
        "--no-auto-prepare",
        action="store_true",
        help="Do not try built-in USB preparation automatically after a no-response query.",
    )
    parser.add_argument(
        "--leave-on-exit",
        action="store_true",
        help="Do not turn the load off on exit when this program enabled it.",
    )
    parser.add_argument("--debug", action="store_true", help="Print HID traffic debug lines.")
    return parser.parse_args()


def csv_line(status: DL24PStatus) -> str:
    timestamp = datetime.now().isoformat(timespec="milliseconds")
    setpoint = "" if status.setpoint is None else f"{status.setpoint:.3f}"
    cutoff = "" if status.voltage_cutoff_v is None else f"{status.voltage_cutoff_v:.3f}"
    mode = "" if status.mode is None else str(status.mode)
    return (
        f"{timestamp},"
        f"{status.voltage_v:.3f},"
        f"{status.current_a:.3f},"
        f"{status.power_w:.3f},"
        f"{setpoint},"
        f"{cutoff},"
        f"{mode},"
        f"{'ON' if status.load_on else 'OFF'},"
        f"{status.mosfet_temp_c:.3f},"
        f"{status.ext_temp_c:.3f},"
        f"{status.fan_rpm},"
        f"{status.capacity_mah:.3f},"
        f"{status.energy_wh:.3f},"
        f"{status.runtime_s}"
    )


def wait_for_setpoint(device: DL24P, current_a: float, timeout_s: float = 5.0) -> DL24PStatus:
    deadline = time.monotonic() + timeout_s
    last_status: Optional[DL24PStatus] = None

    while time.monotonic() < deadline:
        last_status = device.read_status()
        if (
            last_status.mode == 0
            and last_status.setpoint is not None
            and abs(last_status.setpoint - current_a) <= 0.001
        ):
            return last_status
        time.sleep(0.2)

    if last_status is None:
        raise DL24PError("No status received while waiting for setpoint confirmation.")
    return last_status


def main() -> int:
    args = parse_args()

    if args.prepare:
        try:
            DL24P.prepare_usb(verbose=True)
            print("Device initialization complete.")
            return 0
        except DL24PError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.list:
        if hid is None:
            print("hidapi is not installed. Run: pip install hidapi", file=sys.stderr)
            return 1
        for device in DL24P.list_devices():
            path = device["path"]
            if isinstance(path, bytes):
                path = path.decode(errors="replace")
            print(
                f"{path} | {device.get('manufacturer_string', '')} "
                f"{device.get('product_string', '')}"
            )
        return 0

    if args.current is None:
        print("Error: --current is required unless --list is used.", file=sys.stderr)
        return 2
    if not 0.0 <= args.current <= MAX_CURRENT_A:
        print(f"Error: --current must be between 0 and {MAX_CURRENT_A:.1f} A.", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("Error: --interval must be greater than zero.", file=sys.stderr)
        return 2
    if args.duration is not None and args.duration <= 0:
        print("Error: --duration must be greater than zero.", file=sys.stderr)
        return 2

    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    device = DL24P(path=args.path, debug=args.debug, auto_prepare=not args.no_auto_prepare)
    enabled_by_this_program = False

    try:
        print("Connecting to DL24P USB HID...")
        device.connect()

        initial = device.read_status()
        print(
            "Connected: "
            f"{initial.voltage_v:.3f} V, "
            f"{initial.current_a:.3f} A, "
            f"load={'ON' if initial.load_on else 'OFF'}"
        )

        device.set_cc_current(args.current)
        confirmed = wait_for_setpoint(device, args.current)
        if confirmed.mode == 0 and confirmed.setpoint is not None:
            print(
                f"CC configured: requested={args.current:.3f} A, "
                f"reported={confirmed.setpoint:.3f} A"
            )
        else:
            print("Warning: setpoint was sent but not confirmed.", file=sys.stderr)

        if args.enable:
            device.turn_on()
            enabled_by_this_program = True
            print("Load enabled.")
        else:
            print("Load remains OFF. Add --enable to switch it on.")

        print(
            "timestamp,voltage_V,current_A,power_W,setpoint_A,cutoff_V,"
            "mode,load,mosfet_temp_C,ext_temp_C,fan_RPM,capacity_mAh,energy_Wh,runtime_s"
        )

        start = time.monotonic()
        next_print = 0.0
        while not stop:
            if args.duration is not None and time.monotonic() - start >= args.duration:
                break

            now = time.monotonic()
            if now >= next_print:
                print(csv_line(device.read_status()), flush=True)
                next_print = now + args.interval
            time.sleep(min(0.1, args.interval))

        return 0

    except (DL24PError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if enabled_by_this_program and not args.leave_on_exit:
            print("Turning load OFF...")
            try:
                device.turn_off()
                time.sleep(0.2)
            except DL24PError as exc:
                print(f"Warning: failed to turn load off: {exc}", file=sys.stderr)
        device.close()
        print("Disconnected.")


if __name__ == "__main__":
    raise SystemExit(main())

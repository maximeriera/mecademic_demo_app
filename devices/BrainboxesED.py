"""
Simple, synchronous Brainboxes ED digital device wrapper.

This module intentionally exposes only digital I/O support.
Analogue Brainboxes wrappers were removed to keep usage straightforward.
"""

from typing import Any

from .Device import Device
from .api.ed_device import EDDigitalDeviceSync, WatchdogConfig


class BrainboxesEDDigital(Device):
    """Synchronous Device wrapper for Brainboxes ED digital I/O modules."""

    supports_reconnect = True

    def __init__(
        self,
        ip_address: str,
        address: int = 0x01,
        port: int = 9500,
        timeout: float = 5.0,
        channels: int = 8,
        name: str | None = None,
    ) -> None:
        if name is None:
            name = f"BrainboxesEDDigital_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._address = address
        self._port = port
        self._timeout = float(timeout)
        self._channels = int(channels)
        # Read once per connection: /api/info polls this every second, and
        # every ED exchange opens its own TCP connection.
        self._identity: dict[str, Any] = {}

        self._connected = False
        self._faulted = False
        self._device: EDDigitalDeviceSync | None = None

    def _assert_initialized(self) -> EDDigitalDeviceSync:
        if self._device is None:
            raise RuntimeError(f"[{self.device_id}] Not initialized. Call initialize() first.")
        return self._device

    @property
    def api(self) -> EDDigitalDeviceSync | None:
        return self._device

    @property
    def info(self) -> dict:
        result: dict[str, Any] = {
            "ip_address": self._ip_address,
            "address": self._address,
            "port": self._port,
        }
        if self._connected:
            result.update(self._identity)
        return result

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ready(self) -> bool:
        return self._connected and not self._faulted

    @property
    def faulted(self) -> bool:
        return self._faulted

    def initialize(self) -> None:
        try:
            device = EDDigitalDeviceSync(
                self._ip_address,
                address=self._address,
                port=self._port,
                timeout_sec=self._timeout,
            )
            # Connectivity probe, also the identity shown on the device card.
            self._identity = {"device_name": device.read_device_name()}
            try:
                self._identity["firmware_version"] = device.read_firmware_version()
            except Exception:
                pass
            self._device = device
            self._connected = True
            self._faulted = False
            self.logger.info(f"[{self.device_id}] Connected to {self._ip_address}.")
        except Exception as exc:
            self._connected = False
            self._faulted = True
            self.logger.error(f"[{self.device_id}] Failed to connect: {exc}")
            raise

    def shutdown(self) -> None:
        self._device = None
        self._connected = False
        self.logger.info(f"[{self.device_id}] Shutdown complete.")

    def clear_fault(self) -> None:
        self._faulted = False
        self.logger.info(f"[{self.device_id}] Fault flag cleared.")

    def abort(self) -> None:
        self.logger.warning(f"[{self.device_id}] Abort requested (no motion to clear).")

    def io_points(self) -> list:
        if self._device is None:
            return []
        inputs = self._device.read_inputs_as_int()
        outputs = self._device.read_outputs_as_int()
        return ([{"name": f"di{i}", "kind": "di", "value": bool(inputs >> i & 1), "writable": False}
                 for i in range(self._channels)]
                + [{"name": f"do{i}", "kind": "do", "value": bool(outputs >> i & 1), "writable": True}
                   for i in range(self._channels)])

    def write_io(self, name: str, value) -> None:
        if not name.startswith("do") or not name[2:].isdigit() or int(name[2:]) >= self._channels:
            raise ValueError(f"Unknown or read-only point '{name}'.")
        self._assert_initialized().set_single_output(int(name[2:]), bool(value))

    def read_device_name(self) -> str:
        return self._assert_initialized().read_device_name()

    def read_firmware_version(self) -> str:
        return self._assert_initialized().read_firmware_version()

    def read_io_status(self) -> str:
        return self._assert_initialized().read_io_status()

    def read_inputs_as_int(self) -> int:
        return self._assert_initialized().read_inputs_as_int()

    def read_outputs_as_int(self) -> int:
        return self._assert_initialized().read_outputs_as_int()

    def set_digital_output(self, value: int) -> str:
        return self._assert_initialized().set_digital_output(value)

    def set_single_output(self, channel: int, on: bool) -> str:
        return self._assert_initialized().set_single_output(channel, on)

    def set_single_output_upper(self, channel: int, on: bool) -> str:
        return self._assert_initialized().set_single_output_upper(channel, on)

    def read_input_counter(self, channel: int) -> int:
        return self._assert_initialized().read_input_counter(channel)

    def clear_input_counter(self, channel: int) -> str:
        return self._assert_initialized().clear_input_counter(channel)

    def clear_all_latched_inputs(self) -> str:
        return self._assert_initialized().clear_all_latched_inputs()

    def host_ok(self) -> None:
        self._assert_initialized().host_ok()

    def read_watchdog_status(self) -> str:
        return self._assert_initialized().read_watchdog_status()

    def read_watchdog_timeout(self) -> WatchdogConfig:
        return self._assert_initialized().read_watchdog_timeout()

    def set_watchdog_timeout(self, enable: bool, timeout_tenths_sec: int) -> str:
        return self._assert_initialized().set_watchdog_timeout(enable, timeout_tenths_sec)

    def get_debounce_time(self, channel: int) -> int:
        return self._assert_initialized().get_debounce_time(channel)

    def set_debounce_time(self, channel: int, time_ms: int) -> str:
        return self._assert_initialized().set_debounce_time(channel, time_ms)

    def read_power_on_value(self) -> str:
        return self._assert_initialized().read_power_on_value()

    def set_power_on_value(self) -> str:
        return self._assert_initialized().set_power_on_value()

    def read_safe_value(self) -> str:
        return self._assert_initialized().read_safe_value()

    def set_safe_value(self) -> str:
        return self._assert_initialized().set_safe_value()

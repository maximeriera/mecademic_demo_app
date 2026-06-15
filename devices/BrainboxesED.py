"""
Device wrappers for Brainboxes ED-range I/O devices.

Integrates EDDigitalDevice, EDAnalogueInput, and EDAnalogueOutput from
``ed_device.py`` with the :class:`~devices.Device` abstract base class.

A dedicated background event-loop thread is started per device instance and
is used for ALL async operations, maintaining a single persistent TCP
connection over the device lifetime.

Classes
-------
BrainboxesEDDigital
    Digital I/O devices (ED-588, ED-538, ED-527, ED-516, ED-504,
    ED-008, ED-004, ED-204, ED-038, …).
BrainboxesEDAnalogueInput
    Analogue input devices (ED-549).
BrainboxesEDAnalogueOutput
    Analogue output devices (ED-560).

Usage
-----
::

    from devices import BrainboxesEDDigital

    dio = BrainboxesEDDigital("192.168.0.74")
    dio.initialize()
    print(dio.read_device_name())
    dio.set_digital_output(0xFF)
    dio.shutdown()
"""

import asyncio
import threading
from typing import Any

from .Device import Device
from .api.ed_device import EDAnalogueInput, EDAnalogueOutput, EDDigitalDevice, WatchdogConfig


# ---------------------------------------------------------------------------
# Internal: background event-loop thread
# ---------------------------------------------------------------------------


class _EventLoopThread:
    """
    Runs an ``asyncio`` event loop in a dedicated daemon thread.

    All coroutines dispatched through :meth:`run` execute on that loop,
    making it safe to call from any synchronous thread.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="BrainboxesED-EventLoop",
        )
        self._thread.start()

    def run(self, coro, timeout: float = 10.0) -> Any:
        """Schedule *coro* on the managed loop and block until it completes."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        """Stop the event loop and join the thread (up to 5 s)."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# BrainboxesEDDigital
# ---------------------------------------------------------------------------


class BrainboxesEDDigital(Device):
    """
    Device wrapper for Brainboxes ED digital I/O devices.

    Covers: ED-588, ED-538, ED-527, ED-516, ED-504, ED-008, ED-004,
    ED-204, ED-038.

    Maintains a persistent TCP connection after :meth:`initialize` is called.
    All public methods are synchronous and safe to call from any thread.

    Parameters
    ----------
    ip_address : str
        IP address of the device.
    address : int
        Device address (0x00–0xFF).  Default ``0x01``.
    port : int
        TCP port.  Default ``9500``.
    timeout : float
        Socket timeout in seconds.  Default ``5.0``.
    name : str, optional
        Custom device identifier used for logging.
        Auto-generated from ``ip_address`` if omitted.

    Example
    -------
    ::

        dio = BrainboxesEDDigital("192.168.0.74")
        dio.initialize()
        print(dio.info)
        dio.set_digital_output(0xFF)
        inputs = dio.read_inputs_as_int()
        dio.shutdown()
    """

    def __init__(
        self,
        ip_address: str,
        address: int = 0x01,
        port: int = 9500,
        timeout: float = 5.0,
        name: str | None = None,
    ) -> None:
        if name is None:
            name = f"BrainboxesEDDigital_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._address = address
        self._port = port
        self._timeout = float(timeout)

        self._connected: bool = False
        self._faulted: bool = False
        self._loop_thread = _EventLoopThread()
        self._device: EDDigitalDevice | None = None

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _run(self, coro) -> Any:
        """Run *coro* on the managed event loop (blocking)."""
        return self._loop_thread.run(coro, timeout=self._timeout + 2)

    def _assert_connected(self) -> EDDigitalDevice:
        """Return the device, raising RuntimeError if not yet initialized."""
        if self._device is None:
            raise RuntimeError(f"[{self.device_id}] Not initialized. Call initialize() first.")
        return self._device

    # ------------------------------------------------------------------
    # Device interface — properties
    # ------------------------------------------------------------------

    @property
    def api(self):
        """
        The underlying :class:`~ed_device.EDDigitalDevice` instance.

        Exposes the full async API for advanced use.  Coroutines must be
        awaited on the device's event loop; prefer the sync helpers on this
        class for ordinary use.
        """
        return self._device

    @property
    def info(self) -> dict:
        result: dict = {
            "ip_address": self._ip_address,
            "address": self._address,
            "port": self._port,
        }
        if self._connected and self._device is not None:
            try:
                result["device_name"] = self._run(self._device.read_device_name())
                result["firmware_version"] = self._run(self._device.read_firmware_version())
            except Exception:
                pass
        return result

    @property
    def connected(self):
        return self._connected

    @property
    def ready(self):
        return self._connected and not self._faulted

    @property
    def faulted(self):
        return self._faulted

    # ------------------------------------------------------------------
    # Device interface — lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Open the TCP connection to the device."""
        async def _connect() -> EDDigitalDevice:
            dev = EDDigitalDevice(
                self._ip_address,
                self._address,
                self._port,
                self._timeout,
            )
            await dev.connect()
            return dev

        try:
            self._device = self._run(_connect())
            self._connected = True
            self._faulted = False
            self.logger.info(f"[{self.device_id}] Connected to {self._ip_address}.")
        except Exception as exc:
            self._connected = False
            self._faulted = True
            self.logger.error(f"[{self.device_id}] Failed to connect: {exc}")
            raise

    def shutdown(self) -> None:
        """Close the TCP connection and stop the background event loop."""
        if self._device is not None:
            try:
                self._run(self._device.close())
            except Exception as exc:
                self.logger.warning(f"[{self.device_id}] Error closing connection: {exc}")
            finally:
                self._device = None
        self._connected = False
        try:
            self._loop_thread.stop()
        except Exception:
            pass
        self.logger.info(f"[{self.device_id}] Shutdown complete.")

    def clear_fault(self) -> None:
        self._faulted = False
        self.logger.info(f"[{self.device_id}] Fault flag cleared.")

    def abort(self) -> None:
        self.logger.warning(f"[{self.device_id}] Abort requested (no motion to clear).")

    # ------------------------------------------------------------------
    # Sync helpers — identification
    # ------------------------------------------------------------------

    def read_device_name(self) -> str:
        """Read device name (blocking)."""
        return self._run(self._assert_connected().read_device_name())

    def read_firmware_version(self) -> str:
        """Read firmware version (blocking)."""
        return self._run(self._assert_connected().read_firmware_version())

    # ------------------------------------------------------------------
    # Sync helpers — digital I/O
    # ------------------------------------------------------------------

    def read_io_status(self) -> str:
        """Read digital I/O status hex string (blocking).  See :meth:`EDDigitalDevice.read_io_status`."""
        return self._run(self._assert_connected().read_io_status())

    def read_inputs_as_int(self) -> int:
        """Read current input bitmask (blocking)."""
        return self._run(self._assert_connected().read_inputs_as_int())

    def read_outputs_as_int(self) -> int:
        """Read current output bitmask (blocking)."""
        return self._run(self._assert_connected().read_outputs_as_int())

    def set_digital_output(self, value: int) -> str:
        """Set all digital outputs (blocking).  *value* is a bitmask 0x0000–0xFFFF."""
        return self._run(self._assert_connected().set_digital_output(value))

    def set_single_output(self, channel: int, on: bool) -> str:
        """Set one output channel in the lower bank (blocking)."""
        return self._run(self._assert_connected().set_single_output(channel, on))

    def set_single_output_upper(self, channel: int, on: bool) -> str:
        """Set one output channel in the upper bank (blocking)."""
        return self._run(self._assert_connected().set_single_output_upper(channel, on))

    # ------------------------------------------------------------------
    # Sync helpers — input counters
    # ------------------------------------------------------------------

    def read_input_counter(self, channel: int) -> int:
        """Read the edge counter for *channel* (blocking)."""
        return self._run(self._assert_connected().read_input_counter(channel))

    def clear_input_counter(self, channel: int) -> str:
        """Clear the edge counter for *channel* (blocking)."""
        return self._run(self._assert_connected().clear_input_counter(channel))

    def clear_all_latched_inputs(self) -> str:
        """Clear all latched digital input flags (blocking)."""
        return self._run(self._assert_connected().clear_all_latched_inputs())

    # ------------------------------------------------------------------
    # Sync helpers — watchdog
    # ------------------------------------------------------------------

    def host_ok(self) -> None:
        """Broadcast host-OK to reset the watchdog timer (blocking)."""
        self._run(self._assert_connected().host_ok())

    def read_watchdog_status(self) -> str:
        """Read watchdog timeout status (blocking)."""
        return self._run(self._assert_connected().read_watchdog_status())

    def read_watchdog_timeout(self) -> WatchdogConfig:
        """Read watchdog enable state and timeout (blocking)."""
        return self._run(self._assert_connected().read_watchdog_timeout())

    def set_watchdog_timeout(self, enable: bool, timeout_tenths_sec: int) -> str:
        """Enable/disable watchdog and set its timeout (blocking)."""
        return self._run(self._assert_connected().set_watchdog_timeout(enable, timeout_tenths_sec))

    # ------------------------------------------------------------------
    # Sync helpers — debounce
    # ------------------------------------------------------------------

    def get_debounce_time(self, channel: int) -> int:
        """Read input debounce time in ms for *channel* (blocking)."""
        return self._run(self._assert_connected().get_debounce_time(channel))

    def set_debounce_time(self, channel: int, time_ms: int) -> str:
        """Set input debounce time in ms for *channel* (blocking)."""
        return self._run(self._assert_connected().set_debounce_time(channel, time_ms))

    # ------------------------------------------------------------------
    # Sync helpers — power-on / safe values
    # ------------------------------------------------------------------

    def read_power_on_value(self) -> str:
        """Read stored power-on output value (blocking)."""
        return self._run(self._assert_connected().read_power_on_value())

    def set_power_on_value(self) -> str:
        """Store current outputs as the power-on value (blocking)."""
        return self._run(self._assert_connected().set_power_on_value())

    def read_safe_value(self) -> str:
        """Read stored watchdog safe output value (blocking)."""
        return self._run(self._assert_connected().read_safe_value())

    def set_safe_value(self) -> str:
        """Store current outputs as the watchdog safe value (blocking)."""
        return self._run(self._assert_connected().set_safe_value())


# ---------------------------------------------------------------------------
# BrainboxesEDAnalogueInput
# ---------------------------------------------------------------------------


class BrainboxesEDAnalogueInput(Device):
    """
    Device wrapper for the Brainboxes ED-549 analogue input device (8 channels).

    Maintains a persistent TCP connection after :meth:`initialize` is called.
    All public methods are synchronous and safe to call from any thread.

    Parameters
    ----------
    ip_address : str
        IP address of the device.
    address : int
        Device address (0x00–0xFF).  Default ``0x01``.
    port : int
        TCP port.  Default ``9500``.
    timeout : float
        Socket timeout in seconds.  Default ``5.0``.
    name : str, optional
        Custom device identifier used for logging.

    Example
    -------
    ::

        ai = BrainboxesEDAnalogueInput("192.168.0.75")
        ai.initialize()
        values = ai.read_all_channels()   # list[float], 8 values
        ai.shutdown()
    """

    def __init__(
        self,
        ip_address: str,
        address: int = 0x01,
        port: int = 9500,
        timeout: float = 5.0,
        name: str | None = None,
    ) -> None:
        if name is None:
            name = f"BrainboxesEDAnalogueInput_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._address = address
        self._port = port
        self._timeout = float(timeout)

        self._connected: bool = False
        self._faulted: bool = False
        self._loop_thread = _EventLoopThread()
        self._device: EDAnalogueInput | None = None

    def _run(self, coro) -> Any:
        return self._loop_thread.run(coro, timeout=self._timeout + 2)

    def _assert_connected(self) -> EDAnalogueInput:
        """Return the device, raising RuntimeError if not yet initialized."""
        if self._device is None:
            raise RuntimeError(f"[{self.device_id}] Not initialized. Call initialize() first.")
        return self._device

    # ------------------------------------------------------------------
    # Device interface — properties
    # ------------------------------------------------------------------

    @property
    def api(self):
        """The underlying :class:`~ed_device.EDAnalogueInput` instance."""
        return self._device

    @property
    def info(self) -> dict:
        result: dict = {
            "ip_address": self._ip_address,
            "address": self._address,
            "port": self._port,
        }
        if self._connected and self._device is not None:
            try:
                result["device_name"] = self._run(self._device.read_device_name())
                result["firmware_version"] = self._run(self._device.read_firmware_version())
            except Exception:
                pass
        return result

    @property
    def connected(self):
        return self._connected

    @property
    def ready(self):
        return self._connected and not self._faulted

    @property
    def faulted(self):
        return self._faulted

    # ------------------------------------------------------------------
    # Device interface — lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Open the TCP connection to the device."""
        async def _connect() -> EDAnalogueInput:
            dev = EDAnalogueInput(
                self._ip_address,
                self._address,
                self._port,
                self._timeout,
            )
            await dev.connect()
            return dev

        try:
            self._device = self._run(_connect())
            self._connected = True
            self._faulted = False
            self.logger.info(f"[{self.device_id}] Connected to {self._ip_address}.")
        except Exception as exc:
            self._connected = False
            self._faulted = True
            self.logger.error(f"[{self.device_id}] Failed to connect: {exc}")
            raise

    def shutdown(self) -> None:
        if self._device is not None:
            try:
                self._run(self._device.close())
            except Exception as exc:
                self.logger.warning(f"[{self.device_id}] Error closing connection: {exc}")
            finally:
                self._device = None
        self._connected = False
        try:
            self._loop_thread.stop()
        except Exception:
            pass
        self.logger.info(f"[{self.device_id}] Shutdown complete.")

    def clear_fault(self) -> None:
        self._faulted = False
        self.logger.info(f"[{self.device_id}] Fault flag cleared.")

    def abort(self) -> None:
        self.logger.warning(f"[{self.device_id}] Abort requested (no motion to clear).")

    # ------------------------------------------------------------------
    # Sync helpers — identification
    # ------------------------------------------------------------------

    def read_device_name(self) -> str:
        return self._run(self._assert_connected().read_device_name())

    def read_firmware_version(self) -> str:
        return self._run(self._assert_connected().read_firmware_version())

    # ------------------------------------------------------------------
    # Sync helpers — analogue inputs
    # ------------------------------------------------------------------

    def read_all_channels(self) -> list[float]:
        """Read all 8 analogue inputs in engineering units (blocking)."""
        return self._run(self._assert_connected().read_all_channels())

    def read_channel(self, channel: int) -> str:
        """Read a single analogue input (blocking)."""
        return self._run(self._assert_connected().read_channel(channel))

    def read_channel_enable_status(self) -> int:
        """Read which channels are enabled as a bitmask (blocking)."""
        return self._run(self._assert_connected().read_channel_enable_status())

    def set_channel_enable(self, bitmask: int) -> str:
        """Enable or disable channels (blocking)."""
        return self._run(self._assert_connected().set_channel_enable(bitmask))

    def set_channel_range(self, channel: int, range_code: str) -> str:
        """Set the full-scale range for *channel* (blocking)."""
        return self._run(self._assert_connected().set_channel_range(channel, range_code))

    def read_channel_range(self, channel: int) -> str:
        """Read the full-scale range for *channel* (blocking)."""
        return self._run(self._assert_connected().read_channel_range(channel))

    def read_channel_diagnostics(self) -> str:
        """Read channel diagnostic status (blocking)."""
        return self._run(self._assert_connected().read_channel_diagnostics())

    # ------------------------------------------------------------------
    # Sync helpers — watchdog
    # ------------------------------------------------------------------

    def host_ok(self) -> None:
        """Broadcast host-OK to reset the watchdog timer (blocking)."""
        self._run(self._assert_connected().host_ok())

    def read_watchdog_timeout(self) -> WatchdogConfig:
        """Read watchdog enable state and timeout (blocking)."""
        return self._run(self._assert_connected().read_watchdog_timeout())

    def set_watchdog_timeout(self, enable: bool, timeout_tenths_sec: int) -> str:
        """Enable/disable watchdog and set its timeout (blocking)."""
        return self._run(self._assert_connected().set_watchdog_timeout(enable, timeout_tenths_sec))


# ---------------------------------------------------------------------------
# BrainboxesEDAnalogueOutput
# ---------------------------------------------------------------------------


class BrainboxesEDAnalogueOutput(Device):
    """
    Device wrapper for the Brainboxes ED-560 analogue output device (4 channels).

    Maintains a persistent TCP connection after :meth:`initialize` is called.
    All public methods are synchronous and safe to call from any thread.

    Parameters
    ----------
    ip_address : str
        IP address of the device.
    address : int
        Device address (0x00–0xFF).  Default ``0x01``.
    port : int
        TCP port.  Default ``9500``.
    timeout : float
        Socket timeout in seconds.  Default ``5.0``.
    name : str, optional
        Custom device identifier used for logging.

    Example
    -------
    ::

        ao = BrainboxesEDAnalogueOutput("192.168.0.76")
        ao.initialize()
        ao.set_output_range(0, BrainboxesEDAnalogueOutput.RANGE_0_10V)
        ao.set_output(0, 5.0)
        ao.shutdown()
    """

    # Range code constants (forwarded from EDAnalogueOutput)
    RANGE_0_20MA: str = EDAnalogueOutput.RANGE_0_20MA
    RANGE_4_20MA: str = EDAnalogueOutput.RANGE_4_20MA
    RANGE_0_10V: str = EDAnalogueOutput.RANGE_0_10V

    def __init__(
        self,
        ip_address: str,
        address: int = 0x01,
        port: int = 9500,
        timeout: float = 5.0,
        name: str | None = None,
    ) -> None:
        if name is None:
            name = f"BrainboxesEDAnalogueOutput_{ip_address}"
        super().__init__(device_id=name)

        self._ip_address = ip_address
        self._address = address
        self._port = port
        self._timeout = float(timeout)

        self._connected: bool = False
        self._faulted: bool = False
        self._loop_thread = _EventLoopThread()
        self._device: EDAnalogueOutput | None = None

    def _run(self, coro) -> Any:
        return self._loop_thread.run(coro, timeout=self._timeout + 2)

    def _assert_connected(self) -> EDAnalogueOutput:
        """Return the device, raising RuntimeError if not yet initialized."""
        if self._device is None:
            raise RuntimeError(f"[{self.device_id}] Not initialized. Call initialize() first.")
        return self._device

    # ------------------------------------------------------------------
    # Device interface — properties
    # ------------------------------------------------------------------

    @property
    def api(self):
        """The underlying :class:`~ed_device.EDAnalogueOutput` instance."""
        return self._device

    @property
    def info(self) -> dict:
        result: dict = {
            "ip_address": self._ip_address,
            "address": self._address,
            "port": self._port,
        }
        if self._connected and self._device is not None:
            try:
                result["device_name"] = self._run(self._device.read_device_name())
                result["firmware_version"] = self._run(self._device.read_firmware_version())
            except Exception:
                pass
        return result

    @property
    def connected(self):
        return self._connected

    @property
    def ready(self):
        return self._connected and not self._faulted

    @property
    def faulted(self):
        return self._faulted

    # ------------------------------------------------------------------
    # Device interface — lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Open the TCP connection to the device."""
        async def _connect() -> EDAnalogueOutput:
            dev = EDAnalogueOutput(
                self._ip_address,
                self._address,
                self._port,
                self._timeout,
            )
            await dev.connect()
            return dev

        try:
            self._device = self._run(_connect())
            self._connected = True
            self._faulted = False
            self.logger.info(f"[{self.device_id}] Connected to {self._ip_address}.")
        except Exception as exc:
            self._connected = False
            self._faulted = True
            self.logger.error(f"[{self.device_id}] Failed to connect: {exc}")
            raise

    def shutdown(self) -> None:
        if self._device is not None:
            try:
                self._run(self._device.close())
            except Exception as exc:
                self.logger.warning(f"[{self.device_id}] Error closing connection: {exc}")
            finally:
                self._device = None
        self._connected = False
        try:
            self._loop_thread.stop()
        except Exception:
            pass
        self.logger.info(f"[{self.device_id}] Shutdown complete.")

    def clear_fault(self) -> None:
        self._faulted = False
        self.logger.info(f"[{self.device_id}] Fault flag cleared.")

    def abort(self) -> None:
        self.logger.warning(f"[{self.device_id}] Abort requested (no motion to clear).")

    # ------------------------------------------------------------------
    # Sync helpers — identification
    # ------------------------------------------------------------------

    def read_device_name(self) -> str:
        return self._run(self._assert_connected().read_device_name())

    def read_firmware_version(self) -> str:
        return self._run(self._assert_connected().read_firmware_version())

    # ------------------------------------------------------------------
    # Sync helpers — analogue outputs
    # ------------------------------------------------------------------

    def set_output(self, channel: int, value: int | float | str) -> str:
        """Set the output value for *channel* (blocking)."""
        return self._run(self._assert_connected().set_output(channel, value))

    def read_output(self, channel: int) -> str:
        """Read the current output value for *channel* (blocking)."""
        return self._run(self._assert_connected().read_output(channel))

    def set_output_range(self, channel: int, type_code: str, slew_rate: str = "00") -> str:
        """Set the output range for *channel* (blocking).  Use ``RANGE_*`` constants."""
        return self._run(self._assert_connected().set_output_range(channel, type_code, slew_rate))

    def read_output_range(self, channel: int) -> str:
        """Read the output range for *channel* (blocking)."""
        return self._run(self._assert_connected().read_output_range(channel))

    # ------------------------------------------------------------------
    # Sync helpers — power-on / safe values
    # ------------------------------------------------------------------

    def set_power_on_value(self, channel: int) -> str:
        """Store current output as the power-on value for *channel* (blocking)."""
        return self._run(self._assert_connected().set_power_on_value(channel))

    def read_power_on_value(self, channel: int) -> str:
        """Read the stored power-on value for *channel* (blocking)."""
        return self._run(self._assert_connected().read_power_on_value(channel))

    def set_safe_value(self, channel: int) -> str:
        """Store current output as the watchdog safe value for *channel* (blocking)."""
        return self._run(self._assert_connected().set_safe_value(channel))

    def read_safe_value(self, channel: int) -> str:
        """Read the stored watchdog safe value for *channel* (blocking)."""
        return self._run(self._assert_connected().read_safe_value(channel))

    # ------------------------------------------------------------------
    # Sync helpers — watchdog
    # ------------------------------------------------------------------

    def host_ok(self) -> None:
        """Broadcast host-OK to reset the watchdog timer (blocking)."""
        self._run(self._assert_connected().host_ok())

    def read_watchdog_timeout(self) -> WatchdogConfig:
        """Read watchdog enable state and timeout (blocking)."""
        return self._run(self._assert_connected().read_watchdog_timeout())

    def set_watchdog_timeout(self, enable: bool, timeout_tenths_sec: int) -> str:
        """Enable/disable watchdog and set its timeout (blocking)."""
        return self._run(self._assert_connected().set_watchdog_timeout(enable, timeout_tenths_sec))

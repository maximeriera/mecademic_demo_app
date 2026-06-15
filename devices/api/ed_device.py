"""
Brainboxes ED-range device classes implementing the full ASCII protocol.

Requires Python 3.12+.  All I/O methods are async (asyncio).

Hierarchy:
    EDDevice              — Common commands (all ED devices)
      EDDigitalDevice     — Digital I/O (ED-588, ED-538, ED-527, ED-504,
                            ED-008, ED-004, ED-204, ED-038, …)
      EDAnalogueInput     — Analogue input (ED-549)
      EDAnalogueOutput    — Analogue output (ED-560)

Usage::

    import asyncio
    from ed_device import EDDigitalDevice

    async def main() -> None:
        async with EDDigitalDevice("192.168.0.74") as dev:
            print(await dev.read_device_name())
            await dev.set_digital_output(0xFF)

    asyncio.run(main())

References:
    https://docs.brainboxes.com/reference/protocols/ed-ascii-protocol
    https://docs.brainboxes.com/reference/protocols/ed-analogue-ascii-protocol
"""

import asyncio
from typing import Self

# ---------------------------------------------------------------------------
# Type aliases  (PEP 695 — Python 3.12+)
# ---------------------------------------------------------------------------

type WatchdogConfig = dict[str, bool | int]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EDError(Exception):
    """Raised when the device returns a '?' error response."""


class EDTimeout(Exception):
    """Raised when no response arrives within the configured timeout."""


# ---------------------------------------------------------------------------
# Runtime type-enforcement helper
# ---------------------------------------------------------------------------


def _require(name: str, value: object, *types: type) -> None:
    """Raise ``TypeError`` if *value* is not an instance of any of *types*."""
    if not isinstance(value, types):
        expected = " or ".join(t.__name__ for t in types)
        raise TypeError(f"'{name}' must be {expected}, got {type(value).__name__!r}")


# ---------------------------------------------------------------------------
# Low-level asyncio TCP transport
# ---------------------------------------------------------------------------


class _AsciiIo:
    """
    Async ASCII-over-TCP transport for Brainboxes ED-range devices.

    Frames each command as ASCII bytes terminated with ``\\r`` and reads
    responses up to the next ``\\r``.  Use as an async context manager::

        async with _AsciiIo("192.168.0.74") as io:
            await io.command_noresponse(b"~**")
            data = await io.command_response(b"$01M")
    """

    def __init__(self, ipaddr: str, port: int = 9500, timeout: float = 5.0) -> None:
        self._ipaddr = ipaddr
        self._port = port
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Open the TCP connection to the device."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._ipaddr, self._port),
            timeout=self._timeout,
        )

    async def close(self) -> None:
        """Close the TCP connection gracefully."""
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            finally:
                self._reader = None
                self._writer = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def command_noresponse(self, message: bytes) -> None:
        """Send *message*; no response is expected from the device."""
        await self._send(message)

    async def command_response(self, message: bytes) -> bytes:
        """Send *message* and return the device response (CR stripped)."""
        await self._send(message)
        return await self._receive()

    async def _send(self, message: bytes) -> None:
        if self._writer is None:
            raise RuntimeError("Not connected — use 'async with' or call connect()")
        self._writer.write(message + b"\r")
        await self._writer.drain()

    async def _receive(self) -> bytes:
        if self._reader is None:
            raise RuntimeError("Not connected — use 'async with' or call connect()")
        try:
            data = await asyncio.wait_for(
                self._reader.readuntil(b"\r"),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise EDTimeout("No response received from device") from exc
        except asyncio.IncompleteReadError as exc:
            raise RuntimeError("Connection closed by device") from exc
        return data.rstrip(b"\r")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class EDDevice(_AsciiIo):
    """
    Base class for all Brainboxes ED-range devices.

    Provides commands common to every ED device: identification,
    configuration, firmware, reset, synchronised sampling, and watchdog.

    Parameters
    ----------
    ipaddr : str
        IP address of the device.
    address : int
        Device address (0x00–0xFF).  Default ``0x01``.
    port : int
        TCP port.  Default ``9500``.
    timeout : float
        Socket timeout in seconds.  Default ``5.0``.

    Usage
    -----
    ::

        async with EDDevice("192.168.0.74") as dev:
            print(await dev.read_device_name())
    """

    def __init__(
        self,
        ipaddr: str,
        address: int = 0x01,
        port: int = 9500,
        timeout: float = 5.0,
    ) -> None:
        _require("ipaddr", ipaddr, str)
        _require("address", address, int)
        _require("port", port, int)
        _require("timeout", timeout, float, int)
        if not (0x00 <= address <= 0xFF):
            raise ValueError("address must be in range 0x00-0xFF")
        super().__init__(ipaddr, port, timeout)
        self.address: int = address

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _addr(self) -> str:
        """Two-character upper-case hex address string, e.g. ``"01"``."""
        return f"{self.address:02X}"

    async def _cmd(self, text: str) -> bytes:
        """Send a command that expects a response; return raw bytes."""
        return await self.command_response(text.encode("ascii"))

    async def _cmd_no_response(self, text: str) -> None:
        """Send a broadcast / fire-and-forget command."""
        await self.command_noresponse(text.encode("ascii"))

    def _parse(
        self,
        raw: bytes,
        expected_prefix: str = "!",
        strip_address: bool = True,
    ) -> str:
        """
        Decode and validate a device response.

        Returns the data portion after the delimiter and optional address.
        Raises ``EDError`` on ``?`` responses.
        """
        text = raw.decode("ascii", errors="replace").strip()
        match text[:1]:
            case "?":
                raise EDError(f"Device returned error: {text!r}")
            case p if p == expected_prefix:
                data = text[len(expected_prefix):]
                return data[2:] if strip_address and len(data) >= 2 else data
            case _:
                return text

    def _parse_io(self, raw: bytes) -> str:
        """Parse a ``>``-prefixed I/O response and return the data string."""
        return self._parse(raw, expected_prefix=">", strip_address=False)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def read_device_config(self) -> str:
        """
        Read device configuration.  Command: ``$AA2``

        Returns
        -------
        str
            Config string ``NNTTCCFF`` (address, type, baud rate, format).
        """
        return self._parse(await self._cmd(f"${self._addr}2"))

    async def set_device_config(
        self,
        new_address: int,
        type_code: str,
        baud_code: str,
        data_format: str,
    ) -> str:
        """
        Set device configuration.  Command: ``%AANNTTCCFF``

        Parameters
        ----------
        new_address : int
            New device address (0x00–0xFF).
        type_code : str
            Two-character hex type code (e.g. ``"40"`` for DIO).
        baud_code : str
            Two-character hex baud rate code (``"06"``=9600, ``"0A"``=115200).
        data_format : str
            Two-character hex data format / checksum flags.

        Notes
        -----
        Address changes take effect immediately.  Baud rate and checksum
        changes take effect after a device restart.
        """
        _require("new_address", new_address, int)
        _require("type_code", type_code, str)
        _require("baud_code", baud_code, str)
        _require("data_format", data_format, str)
        if not (0x00 <= new_address <= 0xFF):
            raise ValueError("new_address must be in range 0x00-0xFF")
        cmd = f"%{self._addr}{new_address:02X}{type_code}{baud_code}{data_format}"
        return self._parse(await self._cmd(cmd))

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------

    async def read_device_name(self) -> str:
        """
        Read device name.  Command: ``$AAM``

        Returns
        -------
        str
            Device name, e.g. ``"ED-588"``.
        """
        return self._parse(await self._cmd(f"${self._addr}M"))

    async def set_device_name(self, name: str) -> str:
        """
        Set device name.  Command: ``~AAO(Name)``

        Parameters
        ----------
        name : str
            New name (max 10 characters).
        """
        _require("name", name, str)
        if len(name) > 10:
            raise ValueError("Device name must be 10 characters or fewer")
        return self._parse(await self._cmd(f"~{self._addr}O{name}"))

    async def read_firmware_version(self) -> str:
        """
        Read firmware version.  Command: ``$AAF``

        Returns
        -------
        str
            Version string, e.g. ``"3.65"``.
        """
        return self._parse(await self._cmd(f"${self._addr}F"))

    # ------------------------------------------------------------------
    # Status / reset
    # ------------------------------------------------------------------

    async def read_reset_status(self) -> str:
        """
        Read reset status.  Command: ``$AA5``

        Returns
        -------
        str
            Single-character status byte.
        """
        return self._parse(await self._cmd(f"${self._addr}5"))

    async def reset_device(self) -> None:
        """
        Reboot the device.  Command: ``$AARS``

        No response.  Close the connection after calling this.
        """
        await self._cmd_no_response(f"${self._addr}RS")

    async def restore_factory_defaults(self) -> None:
        """
        Restore factory defaults and reboot.  Command: ``$AAS1``

        Close the connection after calling this.
        """
        await self._cmd_no_response(f"${self._addr}S1")

    # ------------------------------------------------------------------
    # Synchronised sampling
    # ------------------------------------------------------------------

    async def synchronized_sampling(self) -> None:
        """
        Trigger synchronised sampling on all devices.  Command: ``#**``

        Broadcast — no response.  Use ``read_synchronized_data`` to
        retrieve the captured values.
        """
        await self._cmd_no_response("#**")

    async def read_synchronized_data(self) -> str:
        """
        Read data captured by the last synchronised-sampling trigger.
        Command: ``$AA4``

        Returns
        -------
        str
            Raw data string.
        """
        return self._parse(await self._cmd(f"${self._addr}4"))

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    async def host_ok(self) -> None:
        """
        Broadcast host-OK.  Command: ``~**``

        Resets the watchdog timer on all devices.  No response.
        """
        await self._cmd_no_response("~**")

    async def read_watchdog_status(self) -> str:
        """
        Read watchdog timeout status.  Command: ``~AA0``

        Returns
        -------
        str
            ``"00"`` — cleared; ``"04"`` — timed out.
        """
        return self._parse(await self._cmd(f"~{self._addr}0"))

    async def reset_watchdog_status(self) -> str:
        """Clear the watchdog timeout flag.  Command: ``~AA1``"""
        return self._parse(await self._cmd(f"~{self._addr}1"))

    async def read_watchdog_timeout(self) -> WatchdogConfig:
        """
        Read watchdog enable state and timeout.  Command: ``~AA2``

        Returns
        -------
        WatchdogConfig
            ``{"enabled": bool, "timeout_tenths_sec": int}``
        """
        data = self._parse(await self._cmd(f"~{self._addr}2"))
        return {"enabled": data[0] == "1", "timeout_tenths_sec": int(data[1:3], 16)}

    async def set_watchdog_timeout(
        self,
        enable: bool,
        timeout_tenths_sec: int,
    ) -> str:
        """
        Enable/disable watchdog and set its timeout.  Command: ``~AA3EVV``

        Parameters
        ----------
        enable : bool
            ``True`` to enable the watchdog.
        timeout_tenths_sec : int
            Timeout in tenths of a second (1–255, i.e. 0.1 s–25.5 s).
        """
        _require("enable", enable, bool)
        _require("timeout_tenths_sec", timeout_tenths_sec, int)
        if not (1 <= timeout_tenths_sec <= 255):
            raise ValueError("timeout_tenths_sec must be 1–255")
        e = "1" if enable else "0"
        return self._parse(await self._cmd(f"~{self._addr}3{e}{timeout_tenths_sec:02X}"))


# ---------------------------------------------------------------------------
# Digital I/O devices
# ---------------------------------------------------------------------------


class EDDigitalDevice(EDDevice):
    """
    Commands for Brainboxes ED digital I/O devices.

    Covers: ED-588, ED-538, ED-527, ED-516, ED-504, ED-008, ED-004,
            ED-204, ED-038.

    Usage
    -----
    ::

        async with EDDigitalDevice("192.168.0.74") as dev:
            await dev.set_digital_output(0xFF)
            inputs = await dev.read_inputs_as_int()
    """

    # ------------------------------------------------------------------
    # Read I/O status
    # ------------------------------------------------------------------

    async def read_io_status(self) -> str:
        """
        Read digital I/O status.  Command: ``@AA``

        Returns
        -------
        str
            Hex string, e.g. ``"01FA"`` (first byte outputs, second inputs).
        """
        return self._parse_io(await self._cmd(f"@{self._addr}"))

    async def read_io_status_extended(self) -> str:
        """
        Read digital I/O status (extended).  Command: ``$AA6``

        Returns
        -------
        str
            Hex data string, e.g. ``"FF0000"``.
        """
        return self._parse(
            await self._cmd(f"${self._addr}6"),
            expected_prefix="!",
            strip_address=False,
        )

    # ------------------------------------------------------------------
    # Set digital outputs
    # ------------------------------------------------------------------

    async def set_digital_output(self, value: int) -> str:
        """
        Set all digital outputs.  Command: ``@AA(Data)``

        Parameters
        ----------
        value : int
            Bitmask (0x0000–0xFFFF).
        """
        _require("value", value, int)
        if not (0x0000 <= value <= 0xFFFF):
            raise ValueError("value must be 0x0000–0xFFFF")
        return self._parse_io(await self._cmd(f"@{self._addr}{value:02X}"))

    async def set_digital_output_lower8(self, value: int) -> str:
        """
        Set lower 8 output channels.  Command: ``#AA00DD``

        Parameters
        ----------
        value : int
            Bitmask (0x00–0xFF).  Bit 0 = DOut 0.
        """
        _require("value", value, int)
        if not (0x00 <= value <= 0xFF):
            raise ValueError("value must be 0x00–0xFF")
        return self._parse_io(await self._cmd(f"#{self._addr}00{value & 0xFF:02X}"))

    async def set_digital_output_upper8(self, value: int) -> str:
        """
        Set upper 8 output channels.  Command: ``#AA0BDD``

        Parameters
        ----------
        value : int
            Bitmask (0x00–0xFF).  Bit 0 = DOut 8.
        """
        _require("value", value, int)
        if not (0x00 <= value <= 0xFF):
            raise ValueError("value must be 0x00–0xFF")
        return self._parse_io(await self._cmd(f"#{self._addr}0B{value & 0xFF:02X}"))

    async def set_single_output(self, channel: int, on: bool) -> str:
        """
        Set one output channel (lower bank).  Command: ``#AA1cDD``

        Parameters
        ----------
        channel : int
            Channel number (0–7).
        on : bool
            ``True`` for ON, ``False`` for OFF.
        """
        _require("channel", channel, int)
        _require("on", on, bool)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        return self._parse_io(
            await self._cmd(f"#{self._addr}1{channel:X}{'01' if on else '00'}")
        )

    async def set_single_output_upper(self, channel: int, on: bool) -> str:
        """
        Set one output channel (upper bank).  Command: ``#AABcDD``

        Parameters
        ----------
        channel : int
            Upper-bank channel (0–7, maps to DOut 8–15).
        on : bool
            ``True`` for ON, ``False`` for OFF.
        """
        _require("channel", channel, int)
        _require("on", on, bool)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7 (maps to DOut 8–15)")
        return self._parse_io(
            await self._cmd(f"#{self._addr}B{channel:X}{'01' if on else '00'}")
        )

    # ------------------------------------------------------------------
    # Input counters
    # ------------------------------------------------------------------

    async def read_input_counter(self, channel: int) -> int:
        """
        Read the input edge counter for a channel.  Command: ``#AAN``

        Parameters
        ----------
        channel : int
            Channel number (0–15).

        Returns
        -------
        int
            Counter value (up to 32-bit depending on firmware).
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 15):
            raise ValueError("channel must be 0–15")
        return int(self._parse(await self._cmd(f"#{self._addr}{channel:X}")))

    async def clear_input_counter(self, channel: int) -> str:
        """
        Clear input counter for one channel.  Command: ``$AACN``

        Parameters
        ----------
        channel : int
            Channel number (0–15).
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 15):
            raise ValueError("channel must be 0–15")
        return self._parse(await self._cmd(f"${self._addr}C{channel:X}"))

    async def clear_all_latched_inputs(self) -> str:
        """Clear all latched digital input flags.  Command: ``$AAC``"""
        return self._parse(await self._cmd(f"${self._addr}C"))

    async def read_latched_inputs(self) -> str:
        """
        Read latched digital input status.  Command: ``$AALS``

        Returns
        -------
        str
            Hex data string of latched input states.
        """
        return self._parse(
            await self._cmd(f"${self._addr}LS"),
            expected_prefix="!",
            strip_address=False,
        )

    # ------------------------------------------------------------------
    # Power-on / safe values
    # ------------------------------------------------------------------

    async def read_power_on_value(self) -> str:
        """
        Read the stored power-on output value.  Command: ``~AA4P``

        Returns
        -------
        str
            Hex string of the power-on output state.
        """
        return self._parse(await self._cmd(f"~{self._addr}4P"))

    async def set_power_on_value(self) -> str:
        """Store the current outputs as the power-on value.  Command: ``~AA5P``"""
        return self._parse(await self._cmd(f"~{self._addr}5P"))

    async def read_safe_value(self) -> str:
        """
        Read the stored watchdog safe output value.  Command: ``~AA4S``

        Returns
        -------
        str
            Hex string of the safe output state.
        """
        return self._parse(await self._cmd(f"~{self._addr}4S"))

    async def set_safe_value(self) -> str:
        """Store the current outputs as the watchdog safe value.  Command: ``~AA5S``"""
        return self._parse(await self._cmd(f"~{self._addr}5S"))

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------

    async def get_debounce_time(self, channel: int) -> int:
        """
        Read the input debounce time.  Command: ``~AAXCn``

        Parameters
        ----------
        channel : int
            Channel number (0–7).

        Returns
        -------
        int
            Debounce time in milliseconds.
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        return int(self._parse(await self._cmd(f"~{self._addr}XC{channel}")), 16)

    async def set_debounce_time(self, channel: int, time_ms: int) -> str:
        """
        Set the input debounce time.  Command: ``~AAXCnTTTT``

        Parameters
        ----------
        channel : int
            Channel number (0–7).
        time_ms : int
            Debounce time in milliseconds (0–65535).
        """
        _require("channel", channel, int)
        _require("time_ms", time_ms, int)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        if not (0 <= time_ms <= 0xFFFF):
            raise ValueError("time_ms must be 0–65535")
        return self._parse(await self._cmd(f"~{self._addr}XC{channel}{time_ms:04X}"))

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def read_outputs_as_int(self) -> int:
        """
        Read the current output state as an integer bitmask.

        Returns
        -------
        int
            Output bitmask (bit 0 = DOut 0).
        """
        return int((await self.read_io_status())[0:2], 16)

    async def read_inputs_as_int(self) -> int:
        """
        Read the current input state as an integer bitmask.

        Returns
        -------
        int
            Input bitmask (bit 0 = DIn 0).
        """
        return int((await self.read_io_status())[2:4], 16)


# ---------------------------------------------------------------------------
# Analogue input devices (ED-549)
# ---------------------------------------------------------------------------


class EDAnalogueInput(EDDevice):
    """
    Commands for the Brainboxes ED-549 analogue input device (8 channels).

    Usage
    -----
    ::

        async with EDAnalogueInput("192.168.0.74") as dev:
            values = await dev.read_all_channels()
    """

    # Range code constants ────────────────────────────────────────────
    RANGE_PM2_5V:  str = "05"   # ±2.5 V
    RANGE_PM20MA:  str = "06"   # ±20 mA
    RANGE_4TO20MA: str = "07"   # +4 to +20 mA
    RANGE_PM10V:   str = "08"   # ±10 V
    RANGE_PM5V:    str = "09"   # ±5 V
    RANGE_PM1V:    str = "04"   # ±1 V
    RANGE_PM500MV: str = "03"   # ±500 mV
    RANGE_PM150MV: str = "0C"   # ±150 mV
    RANGE_0TO20MA: str = "1A"   # 0 to +20 mA
    RANGE_PM75MV:  str = "3A"   # ±75 mV
    RANGE_PM250MV: str = "3B"   # ±250 mV

    # ------------------------------------------------------------------
    # Reading analogue inputs
    # ------------------------------------------------------------------

    async def read_all_channels(self) -> list[float]:
        """
        Read all 8 analogue inputs in engineering units.  Command: ``#AA``

        Returns
        -------
        list[float]
            8 float values, e.g. ``[0.156, 0.165, …]``.
        """
        data = self._parse_io(await self._cmd(f"#{self._addr}"))
        return [
            float(data[i : i + 7])
            for i in range(0, len(data), 7)
            if data[i : i + 7].strip()
        ]

    async def read_channel(self, channel: int) -> str:
        """
        Read a single analogue input.  Command: ``#AAN``

        Parameters
        ----------
        channel : int
            Channel number (0–7).

        Returns
        -------
        str
            Value string in the device's current data format.
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        return self._parse_io(await self._cmd(f"#{self._addr}{channel}"))

    async def read_all_channels_hex(self) -> str:
        """
        Read all analogue inputs in hex format.  Command: ``$AAA``

        Returns
        -------
        str
            Concatenated hex data string.
        """
        return self._parse_io(await self._cmd(f"${self._addr}A"))

    async def read_synchronized_inputs(self) -> tuple[bool, str]:
        """
        Read analogue data captured by the last ``#**`` trigger.
        Command: ``$AA4``

        Returns
        -------
        tuple[bool, str]
            ``(is_fresh, data)`` — ``is_fresh`` is ``True`` on the first
            read after each ``#**`` broadcast.
        """
        raw = await self._cmd(f"${self._addr}4")
        text = raw.decode("ascii", errors="replace").strip()
        match text[:1]:
            case "?":
                raise EDError(f"Device returned error: {text!r}")
            case _:
                inner = text[1:][2:]   # strip '!' then address
                return inner[0] == "1", inner[1:]

    # ------------------------------------------------------------------
    # Channel enable / disable
    # ------------------------------------------------------------------

    async def read_channel_enable_status(self) -> int:
        """
        Read which channels are enabled.  Command: ``$AA6``

        Returns
        -------
        int
            Bitmask of enabled channels (bit 0 = channel 0).
        """
        return int(self._parse(await self._cmd(f"${self._addr}6")), 16)

    async def set_channel_enable(self, bitmask: int) -> str:
        """
        Enable or disable channels.  Command: ``$AA5VV``

        Parameters
        ----------
        bitmask : int
            Enable mask (bit 0 = channel 0).  ``0xFF`` enables all 8.
        """
        _require("bitmask", bitmask, int)
        if not (0x00 <= bitmask <= 0xFF):
            raise ValueError("bitmask must be 0x00–0xFF")
        return self._parse(await self._cmd(f"${self._addr}5{bitmask & 0xFF:02X}"))

    # ------------------------------------------------------------------
    # Channel range configuration
    # ------------------------------------------------------------------

    async def set_channel_range(self, channel: int, range_code: str) -> str:
        """
        Set the full-scale range for a channel.  Command: ``$AA7CiRrr``

        Parameters
        ----------
        channel : int
            Channel number (0–7).
        range_code : str
            Two-character hex code (use ``RANGE_*`` constants).
        """
        _require("channel", channel, int)
        _require("range_code", range_code, str)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        return self._parse(await self._cmd(f"${self._addr}7C{channel}R{range_code}"))

    async def read_channel_range(self, channel: int) -> str:
        """
        Read the full-scale range for a channel.  Command: ``$AA8Ci``

        Parameters
        ----------
        channel : int
            Channel number (0–7).

        Returns
        -------
        str
            Range string, e.g. ``"C0R09"`` for channel 0 at ±5 V.
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        return self._parse(await self._cmd(f"${self._addr}8C{channel}"))

    async def read_channel_diagnostics(self) -> str:
        """
        Read channel diagnostic status.  Command: ``$AAB``

        Returns
        -------
        str
            Two-character hex status byte.
        """
        return self._parse(await self._cmd(f"${self._addr}B"))

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    async def enable_calibration(self, enable: bool = True) -> str:
        """
        Enable or disable calibration mode.  Command: ``~AAEv``

        Must be called before ``zero_calibration`` or ``span_calibration``.

        Parameters
        ----------
        enable : bool
            ``True`` to enable, ``False`` to disable.
        """
        _require("enable", enable, bool)
        return self._parse(await self._cmd(f"~{self._addr}E{'1' if enable else '0'}"))

    async def zero_calibration(self, channel: int) -> str:
        """
        Zero (offset) calibration on a channel.  Command: ``$AA0Ci``

        Requires ``enable_calibration()`` first.

        Parameters
        ----------
        channel : int
            Channel number (0–7).
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        return self._parse(await self._cmd(f"${self._addr}0C{channel}"))

    async def span_calibration(self, channel: int) -> str:
        """
        Span (gain) calibration on a channel.  Command: ``$AA1Ci``

        Requires ``enable_calibration()`` first.

        Parameters
        ----------
        channel : int
            Channel number (0–7).
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 7):
            raise ValueError("channel must be 0–7")
        return self._parse(await self._cmd(f"${self._addr}1C{channel}"))

    async def internal_calibration(self) -> str:
        """Internal self-calibration.  Command: ``$AAS0``"""
        return self._parse(await self._cmd(f"${self._addr}S0"))

    async def restore_factory_calibration(self) -> str:
        """Reload factory calibration settings.  Command: ``$AAS1``"""
        return self._parse(await self._cmd(f"${self._addr}S1"))

    # ------------------------------------------------------------------
    # Device location
    # ------------------------------------------------------------------

    async def read_device_location(self) -> str:
        """
        Read device location.  Command: ``$AAM1``

        Returns
        -------
        str
            Location string (up to 10 characters).
        """
        return self._parse(await self._cmd(f"${self._addr}M1"))

    async def set_device_location(self, location: str) -> str:
        """
        Set device location.  Command: ``~AAL(Location)``

        Parameters
        ----------
        location : str
            Location description (max 10 characters).
        """
        _require("location", location, str)
        if len(location) > 10:
            raise ValueError("Location must be 10 characters or fewer")
        return self._parse(await self._cmd(f"~{self._addr}L{location}"))


# ---------------------------------------------------------------------------
# Analogue output devices (ED-560)
# ---------------------------------------------------------------------------


class EDAnalogueOutput(EDDevice):
    """
    Commands for the Brainboxes ED-560 analogue output device (4 channels).

    Usage
    -----
    ::

        async with EDAnalogueOutput("192.168.0.74") as dev:
            await dev.set_output_range(0, EDAnalogueOutput.RANGE_0_10V)
            await dev.set_output(0, 5.0)
    """

    # Range code constants ────────────────────────────────────────────
    RANGE_0_20MA: str = "30"   # 0 to +20 mA
    RANGE_4_20MA: str = "31"   # +4 to +20 mA
    RANGE_0_10V:  str = "32"   # 0 to +10 V

    # ------------------------------------------------------------------
    # Set / read analogue outputs
    # ------------------------------------------------------------------

    async def set_output(self, channel: int, value: int | float | str) -> str:
        """
        Set the output value for a channel.  Command: ``#AAN(Data)``

        Parameters
        ----------
        channel : int
            Channel number (0–3).
        value : int | float | str
            Value in engineering units (e.g. ``5.13``) or a pre-formatted
            string.
        """
        _require("channel", channel, int)
        _require("value", value, int, float, str)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        val_str = f"{float(value):+07.3f}" if isinstance(value, (int, float)) else value
        return self._parse_io(await self._cmd(f"#{self._addr}{channel}{val_str}"))

    async def read_output(self, channel: int) -> str:
        """
        Read the current output value.  Command: ``$AA6n``

        Parameters
        ----------
        channel : int
            Channel number (0–3).

        Returns
        -------
        str
            Current value string.
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        return self._parse(await self._cmd(f"${self._addr}6{channel}"))

    # ------------------------------------------------------------------
    # Power-on / safe values
    # ------------------------------------------------------------------

    async def set_power_on_value(self, channel: int) -> str:
        """
        Store current output as power-on value.  Command: ``$AA4n``

        Parameters
        ----------
        channel : int
            Channel number (0–3).
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        return self._parse(await self._cmd(f"${self._addr}4{channel}"))

    async def read_power_on_value(self, channel: int) -> str:
        """
        Read stored power-on value.  Command: ``$AA7n``

        Parameters
        ----------
        channel : int
            Channel number (0–3).

        Returns
        -------
        str
            Stored power-on value string.
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        return self._parse(await self._cmd(f"${self._addr}7{channel}"))

    async def set_safe_value(self, channel: int) -> str:
        """
        Store current output as watchdog safe value.  Command: ``~AA5n``

        Parameters
        ----------
        channel : int
            Channel number (0–3).
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        return self._parse(await self._cmd(f"~{self._addr}5{channel}"))

    async def read_safe_value(self, channel: int) -> str:
        """
        Read stored watchdog safe value.  Command: ``~AA4n``

        Parameters
        ----------
        channel : int
            Channel number (0–3).

        Returns
        -------
        str
            Stored safe value string.
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        return self._parse(await self._cmd(f"~{self._addr}4{channel}"))

    # ------------------------------------------------------------------
    # Output range
    # ------------------------------------------------------------------

    async def set_output_range(
        self,
        channel: int,
        type_code: str,
        slew_rate: str = "00",
    ) -> str:
        """
        Set the output range for a channel.  Command: ``$AA9nttss``

        Parameters
        ----------
        channel : int
            Channel number (0–3).
        type_code : str
            Two-character hex code (use ``RANGE_*`` constants).
        slew_rate : str
            Two-character hex slew-rate (default ``"00"``).
        """
        _require("channel", channel, int)
        _require("type_code", type_code, str)
        _require("slew_rate", slew_rate, str)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        return self._parse(await self._cmd(f"${self._addr}9{channel}{type_code}{slew_rate}"))

    async def read_output_range(self, channel: int) -> str:
        """
        Read the output range for a channel.  Command: ``$AA9n``

        Parameters
        ----------
        channel : int
            Channel number (0–3).

        Returns
        -------
        str
            Type and slew-rate string, e.g. ``"3200"`` for 0–10 V.
        """
        _require("channel", channel, int)
        if not (0 <= channel <= 3):
            raise ValueError("channel must be 0–3")
        return self._parse(await self._cmd(f"${self._addr}9{channel}"))



# ---------------------------------------------------------------------------
# Synchronous wrapper classes (for blocking/sync applications)
# ---------------------------------------------------------------------------


class EDDigitalDeviceSync:
    """
    Synchronous wrapper around EDDigitalDevice for blocking applications.

    All methods are synchronous (non-async) and use ``asyncio.run()`` internally.
    Each method opens a connection, executes the command, and closes the connection.

    Usage::

        from ed_device import EDDigitalDeviceSync

        dev = EDDigitalDeviceSync("192.168.0.74")
        print(dev.read_device_name())
        dev.set_digital_output(0xFF)
    """

    def __init__(self, ip_address: str, port: int = 10001, timeout_sec: int = 5) -> None:
        self.ip_address = ip_address
        self.port = port
        self.timeout_sec = timeout_sec

    def _run(self, coro):
        """Helper: run an async coroutine synchronously."""
        return asyncio.run(coro)

    def _async_call(self, coro_fn):
        """Helper: open connection, call async function, close connection."""
        async def _wrapped():
            device = EDDigitalDevice(self.ip_address, self.port, self.timeout_sec)
            async with device:
                return await coro_fn(device)
        return self._run(_wrapped())

    def read_device_name(self) -> str:
        """Read device name (blocking)."""
        return self._async_call(lambda dev: dev.read_device_name())

    def read_firmware_version(self) -> str:
        """Read firmware version (blocking)."""
        return self._async_call(lambda dev: dev.read_firmware_version())

    def read_module_type(self) -> str:
        """Read module type (blocking)."""
        return self._async_call(lambda dev: dev.read_module_type())

    def read_serial_number(self) -> str:
        """Read serial number (blocking)."""
        return self._async_call(lambda dev: dev.read_serial_number())

    def read_watchdog_timeout(self) -> WatchdogConfig:
        """Read watchdog configuration (blocking)."""
        return self._async_call(lambda dev: dev.read_watchdog_timeout())

    def set_watchdog_timeout(self, enable: bool, timeout_tenths_sec: int) -> None:
        """Set watchdog configuration (blocking)."""
        return self._async_call(lambda dev: dev.set_watchdog_timeout(enable, timeout_tenths_sec))

    def watchdog_host_ok(self) -> None:
        """Send watchdog 'host OK' signal (blocking)."""
        return self._async_call(lambda dev: dev.watchdog_host_ok())

    def read_input_status(self) -> str:
        """Read input status (blocking)."""
        return self._async_call(lambda dev: dev.read_input_status())

    def read_output_status(self) -> str:
        """Read output status (blocking)."""
        return self._async_call(lambda dev: dev.read_output_status())

    def set_digital_output(self, value: int) -> None:
        """Set digital outputs (blocking)."""
        return self._async_call(lambda dev: dev.set_digital_output(value))

    def set_output_bit(self, bit: int, value: bool) -> None:
        """Set a single output bit (blocking)."""
        return self._async_call(lambda dev: dev.set_output_bit(bit, value))

    def read_counter(self, channel: int) -> int:
        """Read input counter (blocking)."""
        return self._async_call(lambda dev: dev.read_counter(channel))

    def reset_counter(self, channel: int) -> None:
        """Reset input counter (blocking)."""
        return self._async_call(lambda dev: dev.reset_counter(channel))

    def read_debounce_time(self) -> int:
        """Read debounce time in ms (blocking)."""
        return self._async_call(lambda dev: dev.read_debounce_time())

    def set_debounce_time(self, milliseconds: int) -> None:
        """Set debounce time in ms (blocking)."""
        return self._async_call(lambda dev: dev.set_debounce_time(milliseconds))


class EDAnalogueInputSync:
    """
    Synchronous wrapper around EDAnalogueInput for blocking applications.

    All methods are synchronous (non-async) and use ``asyncio.run()`` internally.
    Each method opens a connection, executes the command, and closes the connection.

    Usage::

        from ed_device import EDAnalogueInputSync

        dev = EDAnalogueInputSync("192.168.0.74")
        channels = dev.read_channel_values()
        print(channels)
    """

    def __init__(self, ip_address: str, port: int = 10001, timeout_sec: int = 5) -> None:
        self.ip_address = ip_address
        self.port = port
        self.timeout_sec = timeout_sec

    def _run(self, coro):
        """Helper: run an async coroutine synchronously."""
        return asyncio.run(coro)

    def _async_call(self, coro_fn):
        """Helper: open connection, call async function, close connection."""
        async def _wrapped():
            device = EDAnalogueInput(self.ip_address, self.port, self.timeout_sec)
            async with device:
                return await coro_fn(device)
        return self._run(_wrapped())

    def read_device_name(self) -> str:
        """Read device name (blocking)."""
        return self._async_call(lambda dev: dev.read_device_name())

    def read_firmware_version(self) -> str:
        """Read firmware version (blocking)."""
        return self._async_call(lambda dev: dev.read_firmware_version())

    def read_channel_values(self) -> list[float]:
        """Read all channel values (blocking)."""
        return self._async_call(lambda dev: dev.read_channel_values())

    def read_channel_value(self, channel: int) -> float:
        """Read single channel value (blocking)."""
        return self._async_call(lambda dev: dev.read_channel_value(channel))

    def read_channel_range(self, channel: int) -> str:
        """Read channel range (blocking)."""
        return self._async_call(lambda dev: dev.read_channel_range(channel))

    def set_channel_range(self, channel: int, range_code: str) -> None:
        """Set channel range (blocking)."""
        return self._async_call(lambda dev: dev.set_channel_range(channel, range_code))

    def read_data_rate(self) -> int:
        """Read data rate (blocking)."""
        return self._async_call(lambda dev: dev.read_data_rate())

    def set_data_rate(self, rate_hz: int) -> None:
        """Set data rate (blocking)."""
        return self._async_call(lambda dev: dev.set_data_rate(rate_hz))


class EDAnalogueOutputSync:
    """
    Synchronous wrapper around EDAnalogueOutput for blocking applications.

    All methods are synchronous (non-async) and use ``asyncio.run()`` internally.
    Each method opens a connection, executes the command, and closes the connection.

    Usage::

        from ed_device import EDAnalogueOutputSync

        dev = EDAnalogueOutputSync("192.168.0.74")
        dev.set_output_value(0, 10.5)
    """

    def __init__(self, ip_address: str, port: int = 10001, timeout_sec: int = 5) -> None:
        self.ip_address = ip_address
        self.port = port
        self.timeout_sec = timeout_sec

    def _run(self, coro):
        """Helper: run an async coroutine synchronously."""
        return asyncio.run(coro)

    def _async_call(self, coro_fn):
        """Helper: open connection, call async function, close connection."""
        async def _wrapped():
            device = EDAnalogueOutput(self.ip_address, self.port, self.timeout_sec)
            async with device:
                return await coro_fn(device)
        return self._run(_wrapped())

    def read_device_name(self) -> str:
        """Read device name (blocking)."""
        return self._async_call(lambda dev: dev.read_device_name())

    def read_firmware_version(self) -> str:
        """Read firmware version (blocking)."""
        return self._async_call(lambda dev: dev.read_firmware_version())

    def read_output_value(self, channel: int) -> float:
        """Read output value (blocking)."""
        return self._async_call(lambda dev: dev.read_output_value(channel))

    def set_output_value(self, channel: int, value: float) -> None:
        """Set output value (blocking)."""
        return self._async_call(lambda dev: dev.set_output_value(channel, value))



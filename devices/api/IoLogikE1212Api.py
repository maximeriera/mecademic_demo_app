import logging

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException


class IoLogikE1212Api:
    """
    Low-level Modbus TCP API for the Moxa ioLogik E1212 remote I/O controller.

    Wraps :class:`pymodbus.client.ModbusTcpClient` and exposes every register
    in the E1212 datasheet as a named method.

    The E1212 provides:
      - 16 digital input  (DI) channels, each with a 32-bit hardware counter
      - 8  digital output (DO) channels with pulse-output capability

    Parameters
    ----------
    ip_address : str
        IP address of the ioLogik E1212.
    port : int
        Modbus TCP port (default: 502).
    slave_id : int
        Modbus slave / unit ID (default: 1).
    logger : logging.Logger
        Logger instance supplied by the parent :class:`~devices.IoLogikE1212`
        device so that all messages appear under the same logger hierarchy.
    """

    # Channel counts
    DI_COUNT = 8
    DO_COUNT = 8

    # ------------------------------------------------------------------
    # Modbus register map  (addresses are 0-indexed, per the Moxa datasheet)
    # ------------------------------------------------------------------

    # DI - FC02 (read_discrete_inputs)
    _ADDR_DI_STATUS           = 0     # DI on/off, 16 channels
    _ADDR_DI_COUNTER_OVERFLOW = 1000  # Counter overflow flags, 16 channels

    # DI - FC04 (read_input_registers)
    _ADDR_DI_ALL_STATUS       = 48    # All DI status packed in 1 word
    _ADDR_DI_COUNTER_VALUE    = 16    # 32-bit counters, 2 words × 16 ch (high word first)

    # DI - FC01 (read/write coils)
    _ADDR_DI_COUNTER_STATUS   = 256   # Counter start (1) / stop (0), 16 channels
    _ADDR_DI_COUNTER_RESET    = 272   # Counter reset coils,          16 channels
    _ADDR_DI_COUNTER_OVF_CLR  = 288   # Counter overflow clear coils, 16 channels

    # DO - FC01 (read/write coils)
    _ADDR_DO_STATUS           = 0     # DO on/off, 8 channels
    _ADDR_DO_PULSE_STATUS     = 16    # Pulse start (1) / stop (0),  8 channels
    _ADDR_DO_P2P_SAFE_CLR     = 4128  # p2p safe-mode clear coils,   8 channels

    # DO - FC02 (read_discrete_inputs)
    _ADDR_DO_P2P_STATUS       = 4096  # DO p2p transfer status,       8 channels
    _ADDR_DO_P2P_SAFE_FLAG    = 4112  # DO p2p safe-mode flags,       8 channels

    # DO - FC03 (read/write holding registers)
    _ADDR_DO_ALL_STATUS       = 32    # All DO status packed in 1 word
    _ADDR_DO_PULSE_COUNT      = 36    # Pulse count,      8 words
    _ADDR_DO_PULSE_ON_WIDTH   = 52    # Pulse ON width,   8 words (ms)
    _ADDR_DO_PULSE_OFF_WIDTH  = 68    # Pulse OFF width,  8 words (ms)

    # System - FC04 (read_input_registers)
    _ADDR_MODEL_NAME          = 5000  # Model name,       10 words (ASCII)
    _ADDR_LAN_MAC             = 5024  # LAN MAC address,   3 words
    _ADDR_LAN_IP              = 5027  # LAN IP address,    2 words
    _ADDR_FIRMWARE_VERSION    = 5029  # Firmware version,  2 words
    _ADDR_FIRMWARE_BUILD_DATE = 5031  # Firmware build date, 2 words
    _ADDR_DEVICE_UP_TIME      = 5020  # Device uptime (sec), 2 words
    _ADDR_DEVICE_NAME         = 5040  # Device name,      30 words (ASCII)

    # System - FC01 (read/write coils)
    _ADDR_WATCHDOG_ALARM_FLAG = 4144  # Watchdog alarm flag, 1 bit

    # ------------------------------------------------------------------

    def __init__(
        self,
        ip_address: str,
        port: int = 502,
        slave_id: int = 1,
        logger: logging.Logger = None,
    ):
        self.logger = logger or logging.getLogger(__name__)
        self._ip_address = ip_address
        self._port = port
        self._slave_id = slave_id
        self._connected = False
        self._faulted = False
        self._client = ModbusTcpClient(host=ip_address, port=port)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the Modbus TCP connection to the device."""
        self.logger.info(f"Connecting to ioLogik E1212 at {self._ip_address}:{self._port}")
        if not self._client.connect():
            msg = f"Could not reach ioLogik E1212 at {self._ip_address}:{self._port}"
            self.logger.error(msg)
            raise ConnectionError(msg)
        self._connected = True
        self._faulted = False
        self.logger.info("ioLogik E1212 connected successfully.")

    def disconnect(self) -> None:
        """Close the Modbus TCP connection."""
        self.logger.info("Disconnecting from ioLogik E1212.")
        self._client.close()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """True if the socket is currently open."""
        self._connected = self._client.is_socket_open()
        return self._connected

    # ------------------------------------------------------------------
    # Digital Inputs (DI)  – FC02 / FC04 / FC01
    # ------------------------------------------------------------------

    def read_di(self, channel: int) -> bool:
        """Read a single DI channel state.

        Parameters
        ----------
        channel : int
            Channel index 0–15.

        Returns
        -------
        bool
            True = ON, False = OFF.
        """
        self._validate_channel(channel, self.DI_COUNT, "DI")
        rsp = self._client.read_discrete_inputs(
            self._ADDR_DI_STATUS + channel, count=1, device_id=self._slave_id
        )
        self._check_response(rsp, f"read_di({channel})")
        return bool(rsp.bits[0])

    def read_all_di(self) -> list:
        """Read all 16 DI channels.

        Returns
        -------
        list of bool
            Index 0 = channel 0, …, index 15 = channel 15.
        """
        rsp = self._client.read_discrete_inputs(
            self._ADDR_DI_STATUS, count=self.DI_COUNT, device_id=self._slave_id
        )
        self._check_response(rsp, "read_all_di")
        return [bool(b) for b in rsp.bits[: self.DI_COUNT]]

    def get_di_counter(self, channel: int) -> int:
        """Read the 32-bit hardware counter for a DI channel.

        Parameters
        ----------
        channel : int
            Channel index 0–15.

        Returns
        -------
        int
            Counter value (0 – 4 294 967 295).
        """
        self._validate_channel(channel, self.DI_COUNT, "DI counter")
        addr = self._ADDR_DI_COUNTER_VALUE + channel * 2
        rsp = self._client.read_input_registers(addr, count=2, device_id=self._slave_id)
        self._check_response(rsp, f"get_di_counter({channel})")
        high, low = rsp.registers
        return (high << 16) | low

    def start_di_counter(self, channel: int) -> None:
        """Start the hardware counter on a DI channel."""
        self._validate_channel(channel, self.DI_COUNT, "DI counter start")
        self._client.write_coil(
            self._ADDR_DI_COUNTER_STATUS + channel, True, device_id=self._slave_id
        )

    def stop_di_counter(self, channel: int) -> None:
        """Stop the hardware counter on a DI channel."""
        self._validate_channel(channel, self.DI_COUNT, "DI counter stop")
        self._client.write_coil(
            self._ADDR_DI_COUNTER_STATUS + channel, False, device_id=self._slave_id
        )

    def reset_di_counter(self, channel: int) -> None:
        """Reset the hardware counter for a DI channel to zero."""
        self._validate_channel(channel, self.DI_COUNT, "DI counter reset")
        self._client.write_coil(
            self._ADDR_DI_COUNTER_RESET + channel, True, device_id=self._slave_id
        )
        self.logger.debug(f"DI counter channel {channel} reset.")

    def is_di_counter_overflow(self, channel: int) -> bool:
        """Return True if the counter overflow flag is set for a DI channel."""
        self._validate_channel(channel, self.DI_COUNT, "DI overflow")
        rsp = self._client.read_discrete_inputs(
            self._ADDR_DI_COUNTER_OVERFLOW + channel, count=1, device_id=self._slave_id
        )
        self._check_response(rsp, f"is_di_counter_overflow({channel})")
        return bool(rsp.bits[0])

    def clear_di_counter_overflow(self, channel: int) -> None:
        """Clear the counter overflow flag for a DI channel."""
        self._validate_channel(channel, self.DI_COUNT, "DI overflow clear")
        self._client.write_coil(
            self._ADDR_DI_COUNTER_OVF_CLR + channel, True, device_id=self._slave_id
        )

    # ------------------------------------------------------------------
    # Digital Outputs (DO)  – FC01 / FC03
    # ------------------------------------------------------------------

    def read_do(self, channel: int) -> bool:
        """Read the current state of a single DO channel.

        Parameters
        ----------
        channel : int
            Channel index 0–7.
        """
        self._validate_channel(channel, self.DO_COUNT, "DO")
        rsp = self._client.read_coils(
            self._ADDR_DO_STATUS + channel, count=1, device_id=self._slave_id
        )
        self._check_response(rsp, f"read_do({channel})")
        return bool(rsp.bits[0])

    def read_all_do(self) -> list:
        """Read all 8 DO channels.

        Returns
        -------
        list of bool
            Index 0 = channel 0, …, index 7 = channel 7.
        """
        rsp = self._client.read_coils(
            self._ADDR_DO_STATUS, count=self.DO_COUNT, device_id=self._slave_id
        )
        self._check_response(rsp, "read_all_do")
        return [bool(b) for b in rsp.bits[: self.DO_COUNT]]

    def write_do(self, channel: int, value: bool) -> None:
        """Set a single DO channel ON (True) or OFF (False).

        Parameters
        ----------
        channel : int
            Channel index 0–7.
        value : bool
            True = ON, False = OFF.
        """
        self._validate_channel(channel, self.DO_COUNT, "DO")
        rsp = self._client.write_coil(
            self._ADDR_DO_STATUS + channel, bool(value), device_id=self._slave_id
        )
        self._check_response(rsp, f"write_do({channel}, {value})")
        self.logger.debug(f"DO[{channel}] set to {value}.")

    def write_all_do(self, values: list) -> None:
        """Set all 8 DO channels in a single write.

        Parameters
        ----------
        values : list of bool
            Exactly 8 values; index 0 = channel 0.
        """
        if len(values) != self.DO_COUNT:
            raise ValueError(f"Expected {self.DO_COUNT} values, got {len(values)}.")
        rsp = self._client.write_coils(
            self._ADDR_DO_STATUS, [bool(v) for v in values], device_id=self._slave_id
        )
        self._check_response(rsp, "write_all_do")

    # ------------------------------------------------------------------
    # DO Pulse output  – FC01 + FC03
    # ------------------------------------------------------------------

    def configure_do_pulse(
        self,
        channel: int,
        on_width_ms: int,
        off_width_ms: int,
        count: int = 0,
    ) -> None:
        """Configure pulse parameters for a DO channel.

        Parameters
        ----------
        channel : int
            Channel index 0–7.
        on_width_ms : int
            Pulse ON duration in milliseconds (1 ms resolution).
        off_width_ms : int
            Pulse OFF duration in milliseconds (1 ms resolution).
        count : int
            Number of pulses to generate; 0 = continuous until stopped.
        """
        self._validate_channel(channel, self.DO_COUNT, "DO pulse")
        self._client.write_register(
            self._ADDR_DO_PULSE_ON_WIDTH + channel, on_width_ms, device_id=self._slave_id
        )
        self._client.write_register(
            self._ADDR_DO_PULSE_OFF_WIDTH + channel, off_width_ms, device_id=self._slave_id
        )
        self._client.write_register(
            self._ADDR_DO_PULSE_COUNT + channel, count, device_id=self._slave_id
        )

    def start_do_pulse(self, channel: int) -> None:
        """Start pulse output on a DO channel (must be pre-configured first)."""
        self._validate_channel(channel, self.DO_COUNT, "DO pulse start")
        self._client.write_coil(
            self._ADDR_DO_PULSE_STATUS + channel, True, device_id=self._slave_id
        )

    def stop_do_pulse(self, channel: int) -> None:
        """Stop pulse output on a DO channel."""
        self._validate_channel(channel, self.DO_COUNT, "DO pulse stop")
        self._client.write_coil(
            self._ADDR_DO_PULSE_STATUS + channel, False, device_id=self._slave_id
        )

    # ------------------------------------------------------------------
    # System information
    # ------------------------------------------------------------------

    def get_model_name(self) -> str:
        """Read the device model name (e.g. 'ioLogik E1212')."""
        return self._read_ascii_registers(self._ADDR_MODEL_NAME, 10)

    def get_device_name(self) -> str:
        """Read the user-configurable device name stored on the unit."""
        return self._read_ascii_registers(self._ADDR_DEVICE_NAME, 30)

    def get_firmware_version(self) -> str:
        """Read firmware version and return as 'Vmajor.minor.patch' string."""
        rsp = self._client.read_input_registers(
            self._ADDR_FIRMWARE_VERSION, count=2, device_id=self._slave_id
        )
        self._check_response(rsp, "get_firmware_version")
        r0, r1 = rsp.registers
        major = (r0 >> 8) & 0xFF
        minor =  r0       & 0xFF
        patch = (r1 >> 8) & 0xFF
        return f"V{major}.{minor}.{patch}"

    def get_lan_ip(self) -> str:
        """Read LAN IP address and return as dotted-decimal string."""
        rsp = self._client.read_input_registers(
            self._ADDR_LAN_IP, count=2, device_id=self._slave_id
        )
        self._check_response(rsp, "get_lan_ip")
        r0, r1 = rsp.registers
        return f"{(r0 >> 8)}.{r0 & 0xFF}.{(r1 >> 8)}.{r1 & 0xFF}"

    def get_lan_mac(self) -> str:
        """Read LAN MAC address and return as colon-separated hex string."""
        rsp = self._client.read_input_registers(
            self._ADDR_LAN_MAC, count=3, device_id=self._slave_id
        )
        self._check_response(rsp, "get_lan_mac")
        r0, r1, r2 = rsp.registers
        octets = [
            (r0 >> 8) & 0xFF, r0 & 0xFF,
            (r1 >> 8) & 0xFF, r1 & 0xFF,
            (r2 >> 8) & 0xFF, r2 & 0xFF,
        ]
        return ":".join(f"{o:02X}" for o in octets)

    def get_uptime(self) -> int:
        """Return device uptime in seconds (32-bit value)."""
        rsp = self._client.read_input_registers(
            self._ADDR_DEVICE_UP_TIME, count=2, device_id=self._slave_id
        )
        self._check_response(rsp, "get_uptime")
        high, low = rsp.registers
        return (high << 16) | low

    def get_watchdog_alarm(self) -> bool:
        """Return True if the watchdog alarm flag is currently set."""
        rsp = self._client.read_coils(
            self._ADDR_WATCHDOG_ALARM_FLAG, count=1, device_id=self._slave_id
        )
        self._check_response(rsp, "get_watchdog_alarm")
        return bool(rsp.bits[0])

    def clear_watchdog_alarm(self) -> None:
        """Clear the watchdog alarm flag on the device."""
        self._client.write_coil(
            self._ADDR_WATCHDOG_ALARM_FLAG, True, device_id=self._slave_id
        )
        self.logger.info("Watchdog alarm flag cleared.")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_channel(self, channel: int, max_channels: int, label: str) -> None:
        if not (0 <= channel < max_channels):
            raise ValueError(
                f"{label} channel must be 0–{max_channels - 1}, got {channel}."
            )

    def _check_response(self, response, context: str) -> None:
        if response.isError():
            msg = f"Modbus error in '{context}': {response}"
            self.logger.error(msg)
            self._faulted = True
            raise ModbusException(msg)

    def _read_ascii_registers(self, address: int, count: int) -> str:
        """Read *count* input registers and decode each word as two ASCII bytes."""
        rsp = self._client.read_input_registers(address, count=count, device_id=self._slave_id)
        self._check_response(rsp, f"_read_ascii_registers(addr={address})")
        chars = []
        for reg in rsp.registers:
            high = (reg >> 8) & 0xFF
            low  = reg & 0xFF
            if high:
                chars.append(chr(high))
            if low:
                chars.append(chr(low))
        return "".join(chars).strip("\x00").strip()


def example_usage():
    IP = "192.168.127.254"  # Change to your ioLogik E1212 IP address

    api = IoLogikE1212Api(ip_address=IP)

    # --- Connect ---
    print("Connecting...")
    api.connect()
    print(f"Connected: {api.is_connected}")

    # --- System info ---
    print("\n-- System Info --")
    print(f"  Model      : {api.get_model_name()}")
    print(f"  Device name: {api.get_device_name()}")
    print(f"  Firmware   : {api.get_firmware_version()}")
    print(f"  LAN IP     : {api.get_lan_ip()}")
    print(f"  LAN MAC    : {api.get_lan_mac()}")
    print(f"  Uptime (s) : {api.get_uptime()}")
    print(f"  Watchdog   : {api.get_watchdog_alarm()}")

    # --- Read all DI channels ---
    print("\n-- Digital Inputs --")
    di_states = api.read_all_di()
    for ch, state in enumerate(di_states):
        print(f"  DI[{ch:02d}] = {'ON ' if state else 'OFF'}", end="")
        if (ch + 1) % 4 == 0:
            print()

    # --- Read a single DI ---
    di0 = api.read_di(0)
    print(f"\n  DI[0] (single read) = {'ON' if di0 else 'OFF'}")

    # --- DI counter on channel 0 ---
    print("\n-- DI Counter (channel 0) --")
    api.start_di_counter(0)
    print(f"  Counter started. Value = {api.get_di_counter(0)}")
    api.reset_di_counter(0)
    print(f"  Counter reset.  Value = {api.get_di_counter(0)}")
    api.stop_di_counter(0)
    print(f"  Counter stopped. Overflow = {api.is_di_counter_overflow(0)}")

    # --- Read all DO channels ---
    print("\n-- Digital Outputs --")
    do_states = api.read_all_do()
    for ch, state in enumerate(do_states):
        print(f"  DO[{ch}] = {'ON ' if state else 'OFF'}", end="  ")
    print()

    # --- Toggle DO channel 0 ON then OFF ---
    print("\n-- Toggle DO[0] --")
    api.write_do(0, True)
    print(f"  DO[0] after write True  = {'ON' if api.read_do(0) else 'OFF'}")
    api.write_do(0, False)
    print(f"  DO[0] after write False = {'ON' if api.read_do(0) else 'OFF'}")

    # --- Write all DOs at once (all OFF) ---
    api.write_all_do([False] * IoLogikE1212Api.DO_COUNT)
    print(f"  All DOs cleared.")

    # --- Pulse output on DO channel 1 ---
    print("\n-- DO Pulse (channel 1, 200 ms ON / 200 ms OFF, 5 pulses) --")
    api.configure_do_pulse(channel=1, on_width_ms=200, off_width_ms=200, count=5)
    api.start_do_pulse(1)
    print("  Pulse started.")
    api.stop_do_pulse(1)
    print("  Pulse stopped.")

    # --- Disconnect ---
    print("\nDisconnecting...")
    api.disconnect()
    print(f"Connected: {api.is_connected}")


if __name__ == "__main__":
    example_usage()


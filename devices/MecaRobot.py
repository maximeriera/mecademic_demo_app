from .Device import Device

import threading

from mecademicpy.robot import Robot as mdr

#: Robot status code sent when another client already holds the control port.
_MX_ST_USER_ALREADY = "3001"


class MecaRobot(Device):
    """Mecademic robot over mecademicpy.

    Two ``Robot`` objects, never one switching modes (mecademicpy refuses to
    reconnect an object in another mode, and any command sent on a
    monitoring-mode object drops its link):

    * ``api`` — the control connection task code drives.
    * a monitoring connection, opened only while control is released with
      :meth:`disconnect` (``keep_monitoring=True``), so the live view keeps
      receiving the robot's pose while another client (web portal, script)
      drives it.

    Use :attr:`rt` for *cached* real-time getters (``synchronous_update=False``,
    ``GetStatusRobot()``…): it is whichever of the two links is up.
    """

    supports_reconnect = True
    supports_monitoring = True

    def __init__(self, ip_address, name=None):
        if name is None:
            name = f"MecaRobot_{ip_address}"
        super().__init__(device_id=name)
        self._ip_address = ip_address
        self._connected = False
        self._ready = False
        self._faulted = False
        self._api = mdr()  # Initialize the robot's API interface
        self._monitor: mdr | None = None  # created on first release of control
        # Held for the whole of a link transition (connect / release / shutdown).
        # Status reads only try it: while a transition runs they return the last
        # values instead of probing a link that is being torn down, because
        # mecademicpy's IsConnected() disconnects on the polling thread when it
        # finds a broken link.
        self._link_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _monitoring(self) -> bool:
        monitor = self._monitor
        return monitor is not None and monitor.IsConnected()

    def _refresh_status(self) -> None:
        if not self._link_lock.acquire(blocking=False):
            return  # mid-transition: keep the last values
        try:
            controlling = self._api.IsConnected()
            monitoring = not controlling and self._monitoring()
            self._connected = controlling or monitoring
            self._ready = controlling and self._api.IsAllowedToMove()
            if controlling:
                self._faulted = self._api.GetStatusRobot().error_status
            elif monitoring:
                self._faulted = self._monitor.GetStatusRobot().error_status
            else:
                self._faulted = True
        finally:
            self._link_lock.release()

    @property
    def connected(self):
        self._refresh_status()
        return self._connected

    @property
    def ready(self):
        self._refresh_status()
        return self._ready

    @property
    def faulted(self):
        self._refresh_status()
        return self._faulted

    @property
    def link(self) -> str:
        if not self._link_lock.acquire(blocking=False):
            return "monitoring" if self._monitor is not None else "disconnected"
        try:
            if self._api.IsConnected():
                return "control"
            return "monitoring" if self._monitoring() else "disconnected"
        finally:
            self._link_lock.release()

    @property
    def api(self):
        return self._api

    @property
    def rt(self) -> mdr:
        """The connected ``Robot`` (control or monitoring), for cached getters only.

        Never send a command through it: on the monitoring link that raises
        and drops the link.
        """
        if self._api.IsConnected():
            return self._api
        monitor = self._monitor
        if monitor is not None and monitor.IsConnected():
            return monitor
        return self._api

    @property
    def info(self):
        if self.connected:
            info = self.rt.GetRobotInfo()
            return {
                "model": info.model,
                "serial_number": info.serial,
                "firmware_version": info.version.get_str(),
                "ip_address": self._ip_address
            }
        else:
            return {
                "model": None,
                "serial_number": None,
                "firmware_version": None,
                "ip_address": self._ip_address
            }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        # Implement connection logic to the robot here
        with self._link_lock:
            self.logger.info(f"Attempting to connect to MecaRobot at {self._ip_address}")
            try:
                self._api.Connect(self._ip_address, disconnect_on_exception=False)
            except Exception as e:
                self.logger.error(f"Failed to connect to MecaRobot at {self._ip_address}: {e}")
                if _MX_ST_USER_ALREADY in str(e):
                    raise ConnectionError(
                        "Robot is controlled by another connection (web portal or another program). "
                        "Close it first, then connect again."
                    ) from e
                raise ConnectionError(f"Failed to connect: {e}")
            try:
                self._api.ClearMotion()
                self._api.ResumeMotion()
                self._api.ResetError()
                self._api.ActivateRobot()
                self._api.WaitActivated()
                if self._api.GetStatusRobot().error_status:
                    raise Exception(f"{self._api.GetStatusRobot().error_code}")
                self._api.ActivateAndHome()
                self._api.WaitIdle()
            except Exception as e:
                self.logger.error(f"Error during MecaRobot initialization sequence: {e}")
                raise e
            # Back in control: the read-only link is no longer needed. Closed only
            # now, so a failed reconnect leaves the live view its robot data.
            self._close_monitor()

    def disconnect(self, keep_monitoring: bool = False) -> None:
        """Release control so another client can drive the robot.

        The robot is not deactivated, so it stays homed for the other client.
        It does pause: that client must ``ResumeMotion`` before moving it.
        With ``keep_monitoring`` a read-only link is opened *first*, so the
        live view never loses the robot pose.
        """
        with self._link_lock:
            monitor_error = None
            if keep_monitoring:
                try:
                    self._open_monitor()
                except Exception as e:
                    monitor_error = e
                    self.logger.error(f"[{self.device_id}] Could not open the monitoring link: {e}")
            self._close_control()
            self.logger.info(
                f"[{self.device_id}] Control released"
                + (" (monitoring kept)." if self._monitoring() else ".")
            )
            if monitor_error is not None:
                raise ConnectionError(f"Control released, but the monitoring link failed: {monitor_error}")

    def deactivate(self):
        self.logger.info("Deactivating MecaRobot.")
        self._api.DeactivateRobot()
        self._api.WaitDeactivated()

    def shutdown(self):
        # Safe whether the robot was never connected, controlled or only monitored.
        with self._link_lock:
            self.logger.info(f"Shutting down MecaRobot at {self._ip_address}")
            self._close_control()
            self._close_monitor()

    def clear_fault(self):
        if not self._api.IsControlling():
            self.logger.warning(f"[{self.device_id}] Not in control: fault not cleared.")
            return
        self.logger.info("Clearing faults on MecaRobot.")
        self._api.ResetError()
        self._api.ClearMotion()
        self._api.ResumeMotion()

    def abort(self):
        if not self._api.IsControlling():
            return  # nothing of ours is moving; never command another client's robot
        self.logger.warning(f"[{self.device_id}] Aborting: clearing motion queue.")
        try:
            self._api.ClearMotion()
            self._api.ResumeMotion()
        except Exception as e:
            self.logger.error(f"[{self.device_id}] Error during abort: {e}")

    # ------------------------------------------------------------------
    # Link helpers (caller holds _link_lock)
    # ------------------------------------------------------------------

    def _open_monitor(self) -> None:
        if self._monitor is None:
            self._monitor = mdr()
        self._monitor.Connect(self._ip_address, monitor_mode=True, disconnect_on_exception=False)

    def _close_monitor(self) -> None:
        monitor = self._monitor
        if monitor is None:
            return
        try:
            monitor.Disconnect()
            monitor.WaitDisconnected(timeout=5)
        except Exception as e:
            self.logger.warning(f"[{self.device_id}] Error closing the monitoring link: {e}")

    def _close_control(self) -> None:
        if not self._api.IsConnected():
            return
        try:
            self._api.ClearMotion()
        except Exception as e:
            self.logger.warning(f"[{self.device_id}] ClearMotion before disconnect failed: {e}")
        try:
            self._api.Disconnect()
            self._api.WaitDisconnected(timeout=5)
        except Exception as e:
            self.logger.warning(f"[{self.device_id}] Error closing the control link: {e}")


def example_usage():
    robot = MecaRobot("192.168.0.100")
    robot.initialize()

    print(f"Connected: {robot.connected}")
    print(f"Ready: {robot.ready}")
    print(f"Faulted: {robot.faulted}")

    robot._api.WaitHomed()

    print(f"Connected: {robot.connected}")
    print(f"Ready: {robot.ready}")
    print(f"Faulted: {robot.faulted}")

    robot.deactivate()
    robot.shutdown()

if __name__ == "__main__":
    example_usage()

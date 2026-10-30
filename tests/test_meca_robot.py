"""MecaRobot release-control / reconnect sequence, against a fake mecademicpy Robot."""

import pytest

import devices.MecaRobot as meca_module


class _Status:
    def __init__(self, error=False):
        self.error_status = error
        self.error_code = None
        self.activation_state = True
        self.homing_state = True
        self.pause_motion_status = False
        self.simulation_mode = 0


class _Gripper:
    holding_part = False
    closed = False
    opened = True


class _Pose:
    def __init__(self, timestamp, data):
        self.timestamp = timestamp
        self.data = data


class FakeRobot:
    """Minimal stand-in for mecademicpy.robot.Robot, one per connection."""

    control_held_elsewhere = False  # the web portal owns the control port
    calls: list = []

    def __init__(self):
        self.connected = False
        self.monitor_mode = None

    def Connect(self, address, monitor_mode=False, disconnect_on_exception=True, **_):
        if not monitor_mode and FakeRobot.control_held_elsewhere:
            raise Exception("Connection error: Message(3001, 'Another user is already connected')")
        self.connected = True
        self.monitor_mode = monitor_mode
        FakeRobot.calls.append(("Connect", monitor_mode))

    def Disconnect(self):
        FakeRobot.calls.append(("Disconnect", self.monitor_mode))
        self.connected = False

    def WaitDisconnected(self, timeout=None):
        pass

    def IsConnected(self):
        return self.connected

    def IsControlling(self):
        return self.connected and not self.monitor_mode

    def IsAllowedToMove(self):
        return True

    def GetStatusRobot(self):
        return _Status()

    def GetRtTargetCartPos(self, include_timestamp=False, synchronous_update=True):
        if synchronous_update and self.monitor_mode:
            raise AssertionError("synchronous getter used on the monitoring link")
        pose = _Pose(1, [1.0, 2.0, 3.0, 0, 0, 0])
        return pose if include_timestamp else pose.data

    def GetRtTargetJointPos(self, include_timestamp=False, synchronous_update=True):
        if synchronous_update:
            raise AssertionError("telemetry must use cached getters")
        return [0.0, -60.0, 60.0, 0.0, 0.0, 0.0]

    def GetRtGripperState(self, synchronous_update=False):
        return _Gripper()

    def GetRtInputState(self, synchronous_update=False):
        return [1, 0]

    def GetRtOutputState(self, synchronous_update=False):
        return [0, 0]

    def __getattr__(self, name):
        # Commands: refused on a monitoring link, like the real Robot.
        def command(*_args, **_kwargs):
            if self.monitor_mode:
                raise AssertionError(f"{name} sent on the monitoring link")
            if not self.connected:
                raise AssertionError(f"{name} sent while disconnected")
            FakeRobot.calls.append((name, False))
        return command


@pytest.fixture
def robot(monkeypatch):
    FakeRobot.control_held_elsewhere = False
    FakeRobot.calls = []
    monkeypatch.setattr(meca_module, "mdr", FakeRobot)
    robot = meca_module.MecaRobot("192.168.0.100", name="test_meca")
    robot.initialize()
    yield robot
    robot.shutdown()


def test_release_opens_monitoring_before_dropping_control(robot):
    FakeRobot.calls.clear()
    robot.disconnect(keep_monitoring=True)

    assert FakeRobot.calls.index(("Connect", True)) < FakeRobot.calls.index(("Disconnect", False))
    assert robot.link == "monitoring"
    assert robot.connected is True
    assert robot.ready is False
    # Cached getters keep working over the monitoring link.
    assert robot.rt.GetRtTargetCartPos(include_timestamp=True, synchronous_update=False).data[:2] == [1.0, 2.0]


def test_telemetry_uses_cached_getters_over_the_monitoring_link(robot):
    robot.disconnect(keep_monitoring=True)
    data = robot.telemetry()
    assert data["link"] == "monitoring"
    assert data["joints_deg"] == [0.0, -60.0, 60.0, 0.0, 0.0, 0.0]
    assert data["pose_mm_deg"][:3] == [1.0, 2.0, 3.0]
    assert data["gripper"] == "open"
    points = {p["name"]: p for p in robot.io_points()}
    assert points["in1"]["value"] is True and not points["out1"]["writable"]


def test_abort_and_clear_fault_never_command_a_released_robot(robot):
    robot.disconnect(keep_monitoring=True)
    robot.abort()        # would raise in FakeRobot if a command were sent
    robot.clear_fault()
    assert robot.link == "monitoring"


def test_reconnect_blocked_by_portal_keeps_monitoring(robot):
    robot.disconnect(keep_monitoring=True)
    FakeRobot.control_held_elsewhere = True

    with pytest.raises(ConnectionError, match="controlled by another connection"):
        robot.connect()
    assert robot.link == "monitoring"


def test_reconnect_takes_control_then_closes_monitoring(robot):
    robot.disconnect(keep_monitoring=True)
    FakeRobot.calls.clear()

    robot.connect()
    assert robot.link == "control"
    assert FakeRobot.calls.index(("Connect", False)) < FakeRobot.calls.index(("Disconnect", True))
    assert ("ActivateAndHome", False) in FakeRobot.calls


def test_shutdown_is_safe_when_never_connected(monkeypatch):
    monkeypatch.setattr(meca_module, "mdr", FakeRobot)
    robot = meca_module.MecaRobot("192.168.0.100", name="never_connected")
    robot.shutdown()
    assert robot.link == "disconnected"

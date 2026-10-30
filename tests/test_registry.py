"""devices/registry.py: config validation and device creation."""

from devices.registry import (
    DeviceSpec, Param, create_device, describe_types, register_device,
    resolve_type, validate_devices_config,
)


def _errors(issues):
    return [i for i in issues if i["level"] == "error"]


def test_valid_entries_get_defaults_and_coercion():
    entries, issues = validate_devices_config({
        "robot": {"type": "mecademic", "ip_address": "192.168.0.100"},
        "io": {"type": "IOLOGIK", "ip_address": "10.0.0.2", "port": "503"},
        "stage": {"type": "mcs2", "ip_address": "10.0.0.3", "channels": [0, 1]},
    })
    assert issues == []
    by_name = {e.name: e for e in entries}
    assert by_name["io"].type == "iologik"
    assert by_name["io"].kwargs == {"ip_address": "10.0.0.2", "port": 503, "slave_id": 1}
    assert by_name["stage"].type == "smaract_mcs2"
    assert by_name["stage"].kwargs["reference"] == "if_needed"


def test_unknown_type_is_an_error_with_a_suggestion():
    entries, issues = validate_devices_config({"robot": {"type": "mecademik", "ip_address": "1.2.3.4"}})
    assert entries == []
    assert "did you mean 'mecademic'" in _errors(issues)[0]["message"]


def test_unknown_key_is_a_warning_with_a_suggestion():
    entries, issues = validate_devices_config({"board": {"type": "arduino", "Port": "COM7"}})
    assert len(entries) == 1
    assert entries[0].kwargs == {"port": "COM3"}   # the typo is ignored, the default stays
    assert issues[0]["level"] == "warning"
    assert "did you mean 'port'" in issues[0]["message"]


def test_missing_required_and_bad_values_are_errors():
    entries, issues = validate_devices_config({
        "robot": {"type": "mecademic"},
        "io": {"type": "iologik", "ip_address": "10.0.0.2", "port": "five"},
        "stage": {"type": "smaract", "ip_address": "10.0.0.3", "reference": "sometimes"},
        "flag": {"type": "simulated", "fail_to_connect": "maybe"},
    })
    assert entries == []
    messages = {i["device"]: i["message"] for i in _errors(issues)}
    assert "missing required 'ip_address'" in messages["robot"]
    assert "'port'" in messages["io"]
    assert "must be one of" in messages["stage"]
    assert "true or false" in messages["flag"]


def test_disabled_entries_and_malformed_blocks():
    entries, issues = validate_devices_config({
        "off": {"type": "mecademic", "enabled": False},
        "bad": "not a mapping",
        "untyped": {"ip_address": "1.2.3.4"},
    })
    assert entries == []
    assert {i["device"] for i in _errors(issues)} == {"bad", "untyped"}
    assert validate_devices_config(None) == ([], [])
    assert _errors(validate_devices_config(["a"])[1])


def test_custom_driver_passes_keys_through():
    entries, issues = validate_devices_config({
        "sim": {"type": "custom", "driver": "devices.SimulatedDevice:SimulatedDevice", "fail_rate": 0.0},
        "broken": {"type": "custom", "driver": "no_colon_here"},
    })
    assert [i["device"] for i in _errors(issues)] == ["broken"]
    device = create_device(entries[0])
    assert device.device_id == "sim"


def test_register_device_and_describe():
    register_device(DeviceSpec(
        "test_widget", "devices.SimulatedDevice:SimulatedDevice",
        {"connect_delay": Param("float", 0.0)}, aliases=("widget",),
    ))
    assert resolve_type("Widget").type == "test_widget"
    entries, issues = validate_devices_config({"w": {"type": "widget", "connect_delay": 1}})
    assert issues == [] and entries[0].kwargs == {"connect_delay": 1.0}
    assert any(t["type"] == "test_widget" for t in describe_types())


def test_controller_reports_issues_and_blocks_initialize(tmp_path):
    import pytest
    from core.ApplicationController import ApplicationController

    (tmp_path / "config.yaml").write_text(
        "devices:\n"
        "  ok_device: {type: simulated}\n"
        "  typo_device: {type: simulatd}\n"
        "  missing_sdk: {type: custom, driver: 'not_a_module:Nope'}\n",
        encoding="utf-8",
    )
    ctrl = ApplicationController(config_path=str(tmp_path / "config.yaml"))
    try:
        assert list(ctrl.devices) == ["ok_device"]
        assert {i["device"] for i in ctrl.config_issues} == {"typo_device", "missing_sdk"}
        with pytest.raises(RuntimeError, match="Fix the devices in config.yaml"):
            ctrl.initialize()
    finally:
        ctrl.shutdown()
        ctrl.close()

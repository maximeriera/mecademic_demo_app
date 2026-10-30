"""Workspace discovery, module purge, and the in-process switch through Flask."""

import sys

import pytest

from core import Workspaces

from conftest import make_workspace as _make_workspace


def test_discover_lists_both_layouts_and_skips_the_rest(tmp_path):
    a = _make_workspace(tmp_path / "ws_a", "a", title="Line A")
    _make_workspace(tmp_path / "ws_b", "b", layout="app_logic")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "not_a_workspace").mkdir()

    items = Workspaces.discover(str(tmp_path), str(a))
    assert [i["name"] for i in items] == ["ws_a", "ws_b"]
    assert items[0]["current"] and items[0]["title"] == "Line A" and items[0]["layout"] == "flat"
    assert items[1]["layout"] == "app_logic" and not items[1]["current"]


def test_current_workspace_outside_the_root_is_listed(tmp_path):
    outside = _make_workspace(tmp_path / "elsewhere" / "ws_x", "x")
    (tmp_path / "root").mkdir()
    items = Workspaces.discover(str(tmp_path / "root"), str(outside))
    assert [i["name"] for i in items] == ["ws_x"]
    assert Workspaces.discover(None, str(outside))[0]["current"]


def test_resolve_only_accepts_listed_names(tmp_path):
    a = _make_workspace(tmp_path / "ws_a", "a")
    bad = tmp_path / "ws_bad"
    bad.mkdir()
    (bad / "config.yaml").write_text("devices: [unclosed", encoding="utf-8")

    assert Workspaces.resolve(str(tmp_path), str(a), "ws_a") == str(a)
    for name in ("..", "../ws_a", "ws_a/../ws_a", "", "nope"):
        with pytest.raises(ValueError):
            Workspaces.resolve(str(tmp_path), str(a), name)
    with pytest.raises(ValueError, match="invalid config.yaml"):
        Workspaces.resolve(str(tmp_path), str(a), "ws_bad")


def test_default_root_is_never_the_filesystem_root(tmp_path):
    assert Workspaces.default_root(str(tmp_path / "ws")) == str(tmp_path)
    assert Workspaces.default_root("/webapp") is None


def test_purge_reloads_workspace_code_but_keeps_installed_and_framework(tmp_path):
    import yaml
    import core

    a = _make_workspace(tmp_path / "ws_a", "a")
    b = _make_workspace(tmp_path / "ws_b", "b")
    sys.path.insert(0, str(a))
    try:
        import marker
        import ws_helpers
        assert (marker.VERSION, ws_helpers.VERSION) == ("a", "a")

        purged = Workspaces.purge_modules([str(a), str(b)])
        assert {"marker", "ws_helpers"} <= set(purged)
        sys.path.remove(str(a))
        sys.path.insert(0, str(b))

        import marker as marker_b
        import ws_helpers as helpers_b
        assert (marker_b.VERSION, helpers_b.VERSION) == ("b", "b")
        assert sys.modules["yaml"] is yaml and sys.modules["core"] is core
    finally:
        for p in (str(a), str(b)):
            while p in sys.path:
                sys.path.remove(p)
        Workspaces.purge_modules([str(a), str(b)])


def test_switch_through_the_api(flask_app):
    client = flask_app.app.test_client()
    # Start from ws_main whatever ran before (the fixture is shared).
    assert client.post("/api/workspaces/switch", json={"name": "ws_main"}).status_code == 200

    listing = client.get("/api/workspaces").get_json()
    assert listing["current"] == "ws_main"
    assert [w["name"] for w in listing["workspaces"]] == ["ws_main", "ws_other"]

    import marker
    assert marker.VERSION == "main"

    response = client.post("/api/workspaces/switch", json={"name": "ws_other"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["success"] is True
    assert client.get("/api/status").get_json()["workspace"] == "ws_other"
    assert flask_app.APPLICATION.workspace_name == "ws_other"
    assert flask_app.CUSTOM_VIEW_DIR.endswith("custom_view")

    import marker as marker_after
    assert marker_after.VERSION == "other"

    # Reload code: same workspace again, a fresh controller.
    controller_before = flask_app.APPLICATION
    assert client.post("/api/workspaces/switch", json={"name": "ws_other"}).status_code == 200
    assert flask_app.APPLICATION is not controller_before


def test_switch_refusals(flask_app, monkeypatch):
    client = flask_app.app.test_client()
    assert client.post("/api/workspaces/switch", json={"name": ".."}).status_code == 400
    assert client.post("/api/workspaces/switch", json={"name": "missing"}).status_code == 400

    monkeypatch.setattr(flask_app.APPLICATION, "is_busy", lambda: True)
    response = client.post("/api/workspaces/switch", json={"name": "ws_main"})
    assert response.status_code == 409

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from phyagentos_momo_plugin.driver import MomoAgentDriver


def test_action_alias_maps_to_bridge_action(monkeypatch):
    called = {}

    def fake_invoke(self, action, params):
        called["action"] = action
        called["params"] = params
        return {"ok": True, "action": action, "connected": False}, None

    monkeypatch.setattr(MomoAgentDriver, "_invoke_bridge", fake_invoke)
    driver = MomoAgentDriver()
    msg = driver.execute_action("momo_preflight", {})

    assert called["action"] == "preflight"
    assert "preflight" in msg.lower()


def test_joint_delta_is_routed(monkeypatch):
    called = {}

    def fake_invoke(self, action, params):
        called["action"] = action
        called["params"] = params
        return {"ok": True, "action": action}, None

    monkeypatch.setattr(MomoAgentDriver, "_invoke_bridge", fake_invoke)
    driver = MomoAgentDriver()
    msg = driver.execute_action("joint_delta", {"joint": "shoulder_pan", "delta_deg": 3})

    assert called["action"] == "joint_delta"
    assert called["params"]["delta_deg"] == 3
    assert "succeeded" in msg.lower()


def test_unknown_action_returns_error_string():
    driver = MomoAgentDriver()
    msg = driver.execute_action("__nonexistent__", {})
    assert "unknown momoagent action" in msg.lower()

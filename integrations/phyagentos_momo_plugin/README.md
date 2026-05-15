# PhyAgentOS MomoAgent Plugin

External PhyAgentOS plugin draft for direct MomoAgent arm control through `soarmmoce_sdk`.

Control path:

```text
PhyAgentOS HAL Watchdog -> MomoAgentDriver -> runtime/momo_bridge.py -> soarmmoce_sdk -> serial robot
```

This plugin does not require `momo_robot_service` or an HTTP API layer.

## Quick Test

```bash
python runtime/momo_bridge.py preflight --pretty
python runtime/momo_bridge.py state --pretty
python runtime/momo_bridge.py joint_delta --params-json '{"joint":"shoulder_pan","delta_deg":3}' --pretty
```

See [README_ZH.md](README_ZH.md) for the fuller Chinese integration notes.

# MomoAgent PhyAgentOS Runtime

This runtime is intentionally small. It imports `soarmmoce_sdk` directly and exposes JSON CLI actions for the PhyAgentOS HAL driver.

## Preflight

```bash
python runtime/momo_bridge.py preflight --pretty
python runtime/momo_bridge.py preflight --params-json '{"connect": true}' --pretty
```

## State

```bash
python runtime/momo_bridge.py state --pretty
```

## Motion

```bash
python runtime/momo_bridge.py joint_delta \
  --params-json '{"joint":"shoulder_pan","delta_deg":3,"duration":1.0,"speed_percent":30}' \
  --pretty

python runtime/momo_bridge.py cartesian_delta \
  --params-json '{"dz":0.01,"frame":"base","duration":1.0,"speed_percent":25}' \
  --pretty
```

## SDK Path Resolution

The bridge finds `soarmmoce_sdk` in this order:

1. `MOMOAGENT_SDK_SRC`
2. `$MOMOAGENT_REPO_ROOT/sdk/src`
3. `sdk/src` in or near the plugin checkout
4. `runtime/third_party/MomoAgent/sdk/src`

For a standalone plugin repository, copy MomoAgent's `sdk/` into `runtime/third_party/MomoAgent/sdk/`.

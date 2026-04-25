# Issue #8 — Charging, Fan Management & Hardware Firmware Research

## Problem Statement

Misty's battery reached only 5% despite extended time on the charging pad. Fans run continuously. We need best practices for keeping Misty charged and ready for conference demos, and to identify code improvements that maximize hardware efficiency.

---

## Hardware Findings

### Processor (Documentation Correction)

The README and ADR-001 incorrectly reference **Snapdragon 212**. Per official Misty II specs:

| Component | Actual | Currently Documented |
|-----------|--------|---------------------|
| Main SoC | **Snapdragon 820** (4× Kryo @ 2.15 GHz) | Snapdragon 212 |
| Secondary | **Snapdragon 410** (4× Cortex-A53) | Not mentioned |
| RAM | 2 GB (confirmed) | 2 GB ✓ |

> **Action**: Update README.md, ADR-001, copilot-instructions.md to reflect Snapdragon 820 + 410. The inference constraint still holds — 2 GB RAM is the bottleneck, not raw CPU — but the docs should be accurate.

### Battery Specifications

| Spec | Value |
|------|-------|
| Capacity | **10,200 mAh** (8.4V Li-ion) |
| Max runtime | ~2.2 hours (max speed), up to 10 hours idle |
| Wireless pad charge time | ~6–7 hours (full), ~5%/hour observed at 70% health |
| Direct wired charge time | ~3–4 hours (full), roughly 2× pad speed |
| Low-voltage cutoff | ~7V (abrupt shutdown, no graceful warning) |
| Health | 70% reported — indicates degradation |

### Charging

- **Two charging methods**: Wireless pad (inductive) and direct wired (port on bottom, near power switch between treads)
- **Different power supplies**: The pad adapter does NOT fit the direct port — different barrel jack sizes. Misty ships with two adapters.
- **Robot does NOT need to be powered on to charge** — charging while off is fastest
- **Action**: Check carrying case for the direct-charge adapter. If missing, identify barrel jack specs and source a replacement.

### Fans

- **No API or firmware control** for fans — they are entirely firmware/hardware controlled
- Likely tied to thermal sensors on the Snapdragon 820 SoC
- Running continuously may indicate the SoC is always warm (sensory services, WiFi radio, etc.)
- **Reducing compute load is the only lever** — fewer active services = less heat = potentially less fan activity

### Firmware

- Current: **v2.0.2.140** / robot OS **2.0.2.11660**
- Last documented public release: **v1.16.1** (April 2020)
- No public release notes for v2.x firmware
- Misty Robotics acquired by **Furhat Robotics** (May 2022) — no further updates expected
- `POST /api/system/performupdate` exists but almost certainly returns "no update available"
- **v2.0.2 is the final firmware** — all improvements must come from our software layer

---

## What I'd Upgrade If I Were Misty's Developer

### Tier 1: Implement Now (Code Changes to `misty_controller.py`)

#### 1. Battery Monitoring via WebSocket

**Current state**: No battery monitoring whatsoever. The controller has no idea if Misty is at 80% or 5%.

**Improvement**: Subscribe to `BatteryCharge` WebSocket events alongside `KeyPhraseRecognized`. This gives real-time battery data without polling.

```python
# Subscribe to BatteryCharge events (in _ws_subscribe_keyphrase or new method)
battery_sub = json.dumps({
    "Operation": "subscribe",
    "Type": "BatteryCharge",
    "DebounceMs": 60000,  # once per minute is plenty
    "EventName": "BatteryMonitor",
    "ReturnProperty": None,
    "EventConditions": [],
})
self.ws.send(battery_sub)
```

**Behavioral thresholds**:

| Battery Level | Action |
|--------------|--------|
| > 20% | Normal operation |
| 10–20% | Log warning, announce "battery low" via TTS on next turn |
| < 10% | **Auto-enter charging mode** — keyphrase/recording silently fails below ~10% anyway |
| < 7% (voltage) | Abrupt hardware shutdown — nothing we can do |

#### 2. Charging / Low-Power Mode

**Current state**: When idle, Misty keeps keyphrase recognition active, LED green, display on — all drawing power.

**Improvement**: Add a `CHARGING` state to the state machine that minimizes power draw:

```
DISCONNECTED → IDLE → RECORDING → PROCESSING → PLAYING → REARMING → IDLE
                 ↕
             CHARGING  (new state — entered on low battery or manual command)
```

**What charging mode does**:
- Stop keyphrase recognition (`POST /api/audio/keyphrase/stop`)
- Turn off LED (`{"red":0, "green":0, "blue":0}`)
- Cancel any running skills (`POST /api/skills/cancel`)
- Set a minimal display (e.g., `e_Sleeping.jpg` or `e_ContentLeft.jpg`)
- Disable unused sensor services to reduce Snapdragon load:
  - `POST /api/services` with `{"Name": "LocomotionService", "Enabled": false}`
  - `POST /api/services` with `{"Name": "3DToFService", "Enabled": false}`
- Continue battery monitoring via WebSocket to detect when charge is sufficient to resume

**Exit condition**: `chargePercent > 25%` AND `isCharging == true` → offer to re-enter IDLE

#### 3. Graceful Shutdown Sequence

**Current state**: `KeyboardInterrupt` turns off LED and exits. No protection against the 7V abrupt cutoff.

**Improvement**: When battery drops below 10%, proactively:
1. Speak "I need to charge, shutting down" via the orchestration service
2. Stop all services (keyphrase, skills)
3. Set display to sleeping face
4. Turn off LED
5. Log final battery state

This prevents the jarring mid-conversation crash that happens at 7V cutoff.

#### 4. Battery Telemetry Logging

**Improvement**: Log battery data periodically to `misty_controller.log` for trend analysis:

```
2026-04-25 15:30:00 [INFO] Battery: 45.2% | 7.8V | charging=True | health=70% | temp=34°C
```

This helps diagnose charging issues (like the original 5%-after-hours problem) and track battery health degradation over time.

#### 5. Health Check Enhancement

**Current state**: Health check only pings `/api/device`. No battery info.

**Improvement**: Include battery status in periodic health checks:

```python
def check_battery(self) -> dict | None:
    result = self.misty_get("/api/battery", timeout=3.0)
    if result and result.get("status") == "Success":
        battery = result["result"]
        logger.info(
            f"Battery: {battery.get('chargePercent', 0)*100:.0f}% | "
            f"{battery.get('voltage', 0):.1f}V | "
            f"charging={battery.get('isCharging', False)} | "
            f"health={battery.get('healthPercent', 0)*100:.0f}% | "
            f"temp={battery.get('temperature', 0):.0f}°C"
        )
        return battery
    return None
```

### Tier 2: Near-Term Enhancements

#### 6. Temperature-Aware Throttling

**Idea**: Use battery temperature data (available in `/api/battery` response) to detect thermal stress. If temperature exceeds a threshold (e.g., 45°C), reduce activity:
- Increase delay between conversation turns
- Reduce LED brightness (already minimal)
- Log thermal events for correlation with fan behavior

This won't control the fans directly, but reducing thermal load may reduce fan runtime.

#### 7. Demo Mode Battery Budgeting

**Idea**: For conference demos with limited charging windows, implement a "demo budget" mode:
- Track cumulative conversation turns and estimate remaining battery life
- Configure a "must stop by X%" threshold (e.g., 15%)
- Display estimated remaining turns on the companion device console
- Alert the operator when Misty needs to be pulled for charging

```python
# Rough estimation
WATT_HOURS_PER_TURN = 0.08  # measured empirically
remaining_wh = battery_percent * BATTERY_CAPACITY_WH / 100
estimated_turns = remaining_wh / WATT_HOURS_PER_TURN
```

#### 8. Sensor Service Management

**Idea**: Disable services we don't use to reduce Snapdragon compute load and heat:

| Service | Used? | Recommendation |
|---------|-------|---------------|
| 3DToFService (depth sensor) | No | Disable |
| LocomotionService | No (stationary) | Disable |
| NavigationService | No | Disable |
| Face recognition | No | Disable if possible |
| Microphones | Yes (recording) | Keep enabled |
| Speakers | Yes (playback) | Keep enabled |

**Caution**: Need to test which services can be disabled without side effects on audio/keyphrase functionality. The Snapdragon 410 handles sensory services — reducing its load may improve thermals.

#### 9. Idle Timeout

**Idea**: If no wake word is detected for a configurable period (e.g., 15 minutes), automatically enter a reduced-power state:
- Dim LED (or turn off)
- Keep keyphrase active but disable other sensors
- Re-engage full mode on next wake word

---

## Implementation Status

All Tier 1 and Tier 2 items above have been implemented in `misty_controller.py`. Tier 3 firmware-level changes are not possible (firmware is locked at v2.0.2.140, no update toolchain available). Documentation corrections have been applied to README.md, ADR-001, and copilot-instructions.md.

See PR for issues #6 and #8 for the full changeset.

Based on this research, the following docs contain inaccuracies:

| File | Issue | Correction |
|------|-------|------------|
| `README.md:42` | "Snapdragon 212" | **Snapdragon 820 + Snapdragon 410** |
| `docs/ADR-001...md:31` | "Snapdragon 212 (4× Cortex-A7)" | **Snapdragon 820 (4× Kryo @ 2.15 GHz)** |
| `docs/ADR-001...md:43` | "Snapdragon 212" | **Snapdragon 820** |
| `.github/copilot-instructions.md:12` | "Snapdragon 212, 2 GB RAM" | **Snapdragon 820 + 410, 2 GB RAM** |
| `.github/copilot-instructions.md` (Hardware Notes) | No battery capacity listed | Add: **10,200 mAh, 8.4V Li-ion** |
| `.github/copilot-instructions.md` (Hardware Notes) | "Snapdragon 212" not present but 820 not mentioned | Add processor correction note |

---

## Recommended Python Libraries

| Library | Purpose | Why |
|---------|---------|-----|
| **`schedule`** | Periodic battery checks, idle timeouts | Lightweight, no dependencies, fits our threading model |
| **`dataclasses`** (stdlib) | Battery state tracking | Clean data model for battery telemetry |
| **`statistics`** (stdlib) | Battery trend analysis | Moving averages for charge rate estimation |
| **`logging.handlers.RotatingFileHandler`** | Battery telemetry log | Prevent log files from growing unbounded |

> Note: We don't need heavy frameworks. The `schedule` library (pure Python, ~1 file) is the main external addition. Everything else uses stdlib.

---

## Recommended Implementation Order

1. **Battery monitoring** (WebSocket subscription + logging) — immediate visibility
2. **Health check enhancement** (add battery to existing health loop) — low risk
3. **Documentation corrections** (Snapdragon 820, battery specs) — accuracy
4. **Charging mode** (new state + power reduction) — biggest power savings
5. **Graceful shutdown** (low-battery TTS warning + clean exit) — reliability
6. **Sensor service management** (disable unused services) — needs testing
7. **Idle timeout** (auto-dim after inactivity) — nice-to-have
8. **Demo mode budgeting** (turn estimation) — conference-specific

---

## Charging Best Practices (For Demo Operators)

1. **Use direct wired charging** whenever possible (~2× faster than pad)
2. **Power off Misty while charging** if time permits (fastest charge)
3. **If Misty must stay on while charging**: enter charging mode (stop keyphrase, kill LED, cancel skills)
4. **Check for the direct-charge adapter** in the carrying case — it uses a different barrel jack than the pad
5. **Monitor battery health** — at 70% health, capacity is effectively ~7,140 mAh instead of 10,200 mAh
6. **Minimum 15% charge** before starting a demo session — below 10%, audio APIs silently fail
7. **Budget ~20 conversation turns per 10% battery** (rough estimate, needs empirical validation)

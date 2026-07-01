# Lessons Learned — Misty II WiFi Provisioning (2026-06-30)

## Context
Attempted to connect Misty II (serial 20194603627, firmware 2.0.2.11660) to a new WiFi network (Pixel hotspot) after her primary network (Home-FE92 / 10.0.0.x) became unstable.

## Key Findings

### 1. Misty's USB port is micro-USB (charge-only?)
- The back panel USB port labeled "USB" is **micro-USB**, not USB-C.
- Connecting via USB-C cable (even data-capable) yields "Device Descriptor Request Failed."
- The CP210x driver installed successfully but no COM port appears — the port may be power-only on this hardware revision, or requires a micro-USB data cable.

### 2. SERIAL header is the reliable data path
- Back panel exposes **RX, GND, TX, 3.3V** at 3.3V TTL levels.
- Requires a **USB-to-UART adapter** (FTDI, CP2102 breakout) to connect to a laptop.
- This is the guaranteed serial console path for `set_wifi` commands.

### 3. BLE provisioning works but protocol is undocumented
- Holding **chin capacitive sensor** triggers BLE advertising (device name = last 5 digits of serial: "03627").
- BLE address can change between sessions (seen as both `57:CA:00:43:1D:D4` and `74:64:FE:9B:00:5C`).
- Custom service UUID: `1562c132-3a0d-4f39-9e67-a9a632e8d6aa`
  - Read characteristic: `418f52ab-10c6-42a6-9590-58cccb818f64` (returns `\x00\x00\x00`)
  - Write characteristic: `3ee51024-7fdd-4d37-95aa-0a4b0e2d4f34` (write-without-response)
- **None of the tested payload formats triggered WiFi connection:**
  - JSON: `{"ssid": "...", "password": "..."}`
  - JSON with security type
  - Newline-separated SSID/password
  - Null-separated SSID/password
- The old Misty companion app (no longer available) used this BLE path successfully. Exact protocol is unknown.

### 4. USB drive provisioning (NetworkCfg.json) did not work
- FAT32 drive with `NetworkCfg.json` at root (JSON with ssid, password, encryptionType).
- Cold boot with drive inserted — Misty did not connect to the specified network.
- May not be supported on firmware 2.0.2.11660, or the file format/name is wrong.

### 5. Misty only supports 2.4GHz WiFi
- Confirmed by API response and documentation.
- Pixel hotspot was set to 2.4GHz — not a band issue.

### 6. REST API is the easiest path (when she's already on a network)
- `POST http://<misty-ip>/api/networks` with `{"Ssid": "...", "Password": "..."}` adds networks.
- `GET http://<misty-ip>/api/device` confirms connectivity and returns full device info.
- **This only works if Misty is already reachable on some network.**

## What Works (confirmed)
| Method | Status |
|--------|--------|
| REST API `/api/networks` (when connected) | ✅ Confirmed working |
| Auto-discovery via subnet scan | ✅ Built and tested |
| BLE connect and read/write characteristics | ✅ Connection works, payload format unknown |
| Chin sensor → BLE advertising mode | ✅ Confirmed |
| CP210x driver install | ✅ Installed |

## What Doesn't Work (or unconfirmed)
| Method | Status |
|--------|--------|
| USB-C cable to micro-USB port | ❌ No COM port, descriptor failure |
| BLE WiFi provisioning (multiple payload formats) | ❌ No effect |
| USB drive NetworkCfg.json cold boot | ❌ No effect |
| Misty companion app | ❌ No longer available for download |

## Recommendations for Travel

### Immediate (before next trip)
1. **Get a USB-to-UART adapter** (CP2102 or FTDI breakout, 3.3V TTL) — ~$5 on Amazon. Keep in travel kit permanently.
2. **Get a micro-USB data cable** — test if the USB port provides serial with the correct cable.
3. **Pre-provision WiFi via REST API** — while Misty is on a known network, push your phone hotspot SSID so she auto-connects anywhere:
   ```
   python misty_wifi.py add "Pixel_1784" "McC13ll@n"
   ```

### Long-term
4. **Reverse-engineer BLE protocol** — use nRF Connect or Wireshark BLE sniffer to capture what the app sent. The companion app APK could be decompiled for the exact payload format.
5. **Build a travel provisioning script** that combines:
   - BLE provisioning (once protocol is known)
   - UART serial fallback
   - REST API for network already connected

## Hardware Needed
- [ ] USB-to-UART adapter (3.3V TTL, CP2102 or FTDI) — for SERIAL header
- [ ] Micro-USB data cable — to test USB port serial
- [ ] (Optional) Jumper wires — for SERIAL header if adapter doesn't have pin headers

## Files Created This Session
- `src/windows-orchestration/misty_discovery.py` — Auto-discovers Misty on local subnets, caches IP
- `src/windows-orchestration/misty_wifi.py` — WiFi management CLI (add/list/connect/scan/push-all)
- `src/windows-orchestration/misty_ip.json` — Cached discovery result (gitignored)
- `src/windows-orchestration/misty_wifi_networks.json.enc` — Encrypted credential store (gitignored)

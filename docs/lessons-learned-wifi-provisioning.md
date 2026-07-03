# Lessons Learned — Misty II WiFi Provisioning (2026-06-30, updated 2026-07-01)

## Context
Attempted to connect Misty II (serial redacted, firmware 2.0.2.11660) to a new WiFi network (phone hotspot) after her primary home network became unstable.

## Key Findings

### 1. Misty's USB port is micro-USB (charge-only?)
- The back panel USB port labeled "USB" is **micro-USB**, not USB-C.
- Connecting via USB-C cable (even data-capable) yields "Device Descriptor Request Failed."
- The CP210x driver installed successfully but no COM port appears — the port may be power-only on this hardware revision, or requires a micro-USB data cable.

### 2. SERIAL header is the reliable data path
- Back panel exposes **RX, GND, TX, 3.3V** at 3.3V TTL levels.
- Requires a **USB-to-UART adapter** (FTDI, CP2102 breakout) to connect to a laptop.
- This is the guaranteed serial console path for `set_wifi` commands.

### 3. BLE provisioning requires bonded/encrypted connection (BLOCKED)
- Holding **chin capacitive sensor** triggers BLE advertising (device name = last 5 digits of serial).
- BLE address **rotates every advertising session** (observed 3 different addresses: `57:CA:00:43:1D:D4`, `74:64:FE:9B:00:5C`, `5E:19:98:09:B8:7A`, `54:48:4A:95:CF:90`).
- Manufacturer data: ID `0xFFEE`, 12 bytes static per session.
- MTU negotiated to 517 bytes.
- Custom service UUID: `1562c132-3a0d-4f39-9e67-a9a632e8d6aa`
  - Read characteristic: `418f52ab-10c6-42a6-9590-58cccb818f64` (read only, always returns `000000`)
  - Write characteristic: `3ee51024-7fdd-4d37-95aa-0a4b0e2d4f34` (write-without-response AND write-with-response both accepted)
- **40+ payload formats tested — ALL silently discarded:**
  - JSON (multiple key naming conventions, with/without security type)
  - Length-prefixed binary, null/newline/pipe/semicolon separated
  - TLV encoded, command-byte prefixed (0x01-0xFF)
  - Text commands (scan, status, list), ESP-IDF sequences
  - UTF-16LE, Base64, reversed characteristics
  - Multi-step writes, chunked writes, rapid polling
- **Root cause: Firmware requires encrypted/bonded BLE connection.**
  - Attempting BLE pairing causes Misty to **beep** (first sign of firmware processing BLE events)
  - But pairing is always **rejected** by Misty (from both Windows Settings and programmatic attempts)
  - Writes are accepted at the GATT layer but the application discards unencrypted data
  - The companion app likely uses a proprietary pairing flow with a PIN or OOB key
- The companion app (APK v1.2.59) is a Xamarin/.NET app with logic in a compressed XABA blob (undocumented format, could not decompile).

### 4. USB drive provisioning (NetworkCfg.json) did not work
- FAT32 drive with `NetworkCfg.json` at root (JSON with ssid, password, encryptionType).
- Cold boot with drive inserted — Misty did not connect to the specified network.
- May not be supported on firmware 2.0.2.11660, or the file format/name is wrong.

### 5. Misty only supports 2.4GHz WiFi
- Confirmed by API response and documentation.
- Pixel hotspot was set to 2.4GHz — not a band issue.

### 6. REST API is the primary provisioning path
- `POST /api/networks/create` with `{"networkname":"SSID","password":"pass"}` — **adds AND connects** (blocks during attempt)
- `POST /api/networks` with `{"NetworkId":N}` — connects to existing saved network
- `DELETE /api/networks?NetworkId=N` — forgets/removes a saved network
- `GET /api/networks` — lists saved networks with IDs
- `GET /api/networks/scan` — scans visible SSIDs (results may show empty SSIDs)
- `GET /api/device` — confirms connectivity, returns IP, MAC, serial, firmware, current network
- **This only works if Misty is already reachable on some network.**

### 7. Multi-homed laptop causes routing failures (not AP isolation)
- After connecting Misty to a secondary WiFi network via REST API, the laptop couldn't reach her on the new subnet.
- **Root cause**: Laptop was multi-homed — wired on the home network and on WiFi at the same time. Windows routed traffic for the secondary subnet through the wrong interface.
- Confirmed: disconnecting the wired adapter allowed the laptop to reach Misty on the secondary WiFi immediately.
- A phone on the same WiFi could also reach Misty (single-homed, correct routing).
- **Lesson**: When multi-homed, check route metrics. Either disconnect the other adapter or add a static route:
  ```powershell
  route add <wifi_subnet> mask <subnet_mask> <wifi_gateway> IF <wifi_interface_index>
  ```
- The secondary WiFi works fine for Misty — the issue was purely laptop-side routing, not AP isolation.
- Removed that network from Misty's saved networks prematurely (can re-add if needed).

### 8. Travel hotspot invisible to Misty (cause uncertain)
- The phone hotspot was saved in Misty's network list but she could never connect to it.
- WiFi scan (`GET /api/networks/scan`) does NOT show the hotspot even when it is active and set to dual-band (2.4 + 5GHz).
- **Root cause TBD** — hotspot was confirmed active and broadcasting on 2.4GHz. Needs further testing.
- Possible causes: hotspot channel conflict, hidden SSID mode, or Misty firmware quirk with certain hotspot implementations.
- Note: Misty had duplicate hotspot entries — clean up duplicates when possible.
- **Action**: Test again with hotspot active and Misty in range; try fresh `networks/create` with correct credentials.

## What Works (confirmed)
| Method | Status |
|--------|--------|
| REST API `/api/networks/create` (add+connect when reachable) | ✅ Confirmed working |
| REST API `/api/networks` POST (connect to saved network by ID) | ✅ Confirmed working |
| REST API `DELETE /api/networks?NetworkId=N` (forget network) | ✅ Confirmed working |
| Auto-discovery via subnet scan + MAC fingerprint | ✅ Built and tested |
| BLE connect, read/write characteristics | ✅ Connection works |
| Chin sensor → BLE advertising mode | ✅ Confirmed |
| CP210x driver install | ✅ Installed |
| Phone as fallback API client (bypasses AP isolation) | ✅ Confirmed |

## What Doesn't Work (or blocked)
| Method | Status |
|--------|--------|
| BLE WiFi provisioning (40+ payload formats) | ❌ Requires bonded connection |
| BLE pairing from Windows | ❌ Always rejected by Misty |
| USB-C cable to micro-USB port | ❌ No COM port, descriptor failure |
| USB drive NetworkCfg.json cold boot | ❌ No effect |
| Misty companion app on Android 16 | ❌ Crashes (Xamarin too old) |
| Pixel 5GHz hotspot visibility | ❌ Misty 2.4GHz only |

## Recommendations for Travel

### Immediate (before next trip)
1. **Pre-provision WiFi via REST API** — while Misty is on a known network, push your phone hotspot SSID (set to 2.4GHz!):
   ```
   python misty_wifi.py add "TravelHotspot" "password" --ip <misty-ip>
   ```
2. **Set Pixel hotspot to 2.4GHz** — Settings > Hotspot > AP band > "2.4 GHz" or "Compatibility mode."
3. **Get a USB-to-UART adapter** (CP2102 or FTDI breakout, 3.3V TTL) — ~$5 on Amazon. Keep in travel kit as nuclear option.
4. **Get a micro-USB data cable** — test if the USB port provides serial with the correct cable.
5. **Verify new networks before pushing** — ensure your laptop can reach other devices on the target network (test for AP isolation).

### Recovery playbook (if Misty connects to unreachable network)
1. Try reaching Misty from a phone on the same network (AP isolation may be selective)
2. If reachable from phone: browse to `http://<misty-ip>/api/networks` to find the ID, then use bookmarklet/REST client to switch networks
3. If completely unreachable: power off Misty, block the problematic WiFi (turn off router), power her back on — she'll fall back to next saved network
4. Last resort: USB-UART adapter to serial console

### Long-term
5. **Sideload companion app on older Android (8-11)** to capture BLE pairing protocol via nRF Connect/Wireshark
6. **Build travel provisioning script** combining REST API + UART serial fallback

## Hardware Needed
- [x] USB-to-UART adapter (3.3V TTL, CP2102 or FTDI) — for SERIAL header (**ordered, in shipping**)
- [ ] Micro-USB data cable — to test USB port serial
- [ ] (Optional) Jumper wires — for SERIAL header if adapter doesn't have pin headers

## Files Created This Session
- `src/windows-orchestration/misty_discovery.py` — Auto-discovers Misty on local subnets, caches IP
- `src/windows-orchestration/misty_wifi.py` — WiFi management CLI (add/list/connect/scan/push-all)
- `src/windows-orchestration/misty_ip.json` — Cached discovery result (gitignored)
- `src/windows-orchestration/misty_wifi_networks.json.enc` — Obfuscated credential store (gitignored)

# BLE WiFi Provisioning Protocol — Misty II (Reverse Engineering)

## Status: BLOCKED — Pairing/encryption required (2026-07-01)

**Summary**: The BLE provisioning characteristic accepts write data at the GATT level but the ESP32 firmware silently discards all writes. Strong evidence indicates the firmware requires a **bonded/encrypted BLE connection** before processing WiFi credentials. Pairing attempts are acknowledged (Misty beeps) but always rejected.

## Known BLE Interface

| Item | Value |
|------|-------|
| BLE Activation | Hold chin capacitive sensor |
| Device Name | Last 5 digits of serial (e.g., "03627") |
| Service UUID | `1562c132-3a0d-4f39-9e67-a9a632e8d6aa` |
| Write Characteristic | `3ee51024-7fdd-4d37-95aa-0a4b0e2d4f34` (write-without-response) |
| Read Characteristic | `418f52ab-10c6-42a6-9590-58cccb818f64` (returns `\x00\x00\x00` always) |
| BLE Address | Changes between sessions (rotates: seen `57:CA:00:43:1D:D4`, `5E:19:98:09:B8:7A`, `40:03:4D:CD:95:A0`) |
| Manufacturer ID | `0xFFEE` (custom, not Espressif) |
| Manufacturer Data | 12 bytes, static per session (e.g., `f3cebb7fa69bedcd7971beb6`) |
| MTU | 517 bytes |
| Firmware | 2.0.2.11660 |

## APK Decompilation Findings (Phase 1)

### Companion App: `com.mistyrobotics.Companion` v1.2.59

**Architecture**: Xamarin.Forms (.NET) application with Android platform bindings.

**Key assemblies** (inside `assemblies/assemblies.blob`, XABA v1 format, compressed):
- `CompanionApp.dll` — Core app logic (WiFi/SSID references)
- `CompanionApp.Forms.dll` — UI/XAML (WiFiNavigation page)
- `CompanionApp.Droid.dll` — Android platform bindings
- `Plugin.BluetoothLE.dll` — BLE communication library (by Allan Ritchie)
- `Plugin.BluetoothLE.Abstractions.dll` — BLE abstractions

**BLE flow** (reconstructed from string analysis):
1. `SubscribeBluetooth` — scan/discover Misty BLE device
2. `DiscoverCharacteristic` / `AndSubscribe` — find service/characteristics
3. `WiFiNavigation` page — user selects network
4. `GetAvailableNetworks` — may query Misty for visible networks via BLE read
5. `SendAsync` / `Write` — send WiFi credentials to write characteristic
6. `ContinueOnboard` / `ReconnectToMisty` — verify WiFi connection

**Critical findings**:
- BLE UUIDs are **NOT** hardcoded in the app — discovered at runtime from Misty
- No JSON format strings found in the .NET assemblies for BLE payloads
- No protobuf definitions found
- The app uses the REST API (`/api/networks`) when Misty is already on WiFi
- BLE is used **only** for initial provisioning when no network exists
- The Xamarin assemblies are stored in XABA v1 compressed blob format (could not fully decompile)

### ESP32 Connection
- Misty II uses an **ESP32** as its WiFi/BLE controller
- The BLE service uses **custom UUIDs** (not standard ESP-IDF provisioning)
- This rules out standard `wifi_prov_mgr` protobuf protocol

## Hardware Testing Results (2026-07-01)

### What Was Tested
- **40+ payload formats** including JSON (multiple key styles), length-prefixed binary, null/newline/pipe/comma-separated, command-prefixed, TLV, UTF-16LE, Base64, chunked writes, 2-step writes, ESP-IDF sequences, and more.
- **Write-without-response** and **write-with-response** (both accepted without error).
- **Writing to read characteristic** (accepted, no effect).
- **Rapid polling** of read characteristic after writes (never changes).
- **Extended connection** (30+ seconds connected, polling every 5s — no change).
- **Notification subscription** (read characteristic does not support notify/indicate).
- **BLE pairing** via Windows Settings and programmatically (Misty beeps acknowledging request, then rejects pairing).
- **Manufacturer data** (12 bytes at manufacturer ID 0xFFEE, static per session) tested as XOR key, prepended token, session handshake, and hash seed — no effect.
- **App identification** handshake writes (MistyCompanion, version string) — no effect.
- **Fake SSID** write to rule out "already connected" filtering — no effect.

### Key Observations
1. **Misty BEEPS** when pairing is attempted → firmware IS processing BLE events
2. **Pairing always fails** → Misty rejects all pairing modes (Just Works, None, default)
3. **Writes accepted at GATT level** → no write errors, but application layer ignores them
4. **Read characteristic permanently `000000`** → no status feedback over BLE
5. **No physical response** to any write → no LED, sound, movement, or display change
6. **BLE advertising continues** after all write attempts → WiFi never connects

### Conclusion
The BLE WiFi provisioning on firmware 2.0.2.11660 **requires an encrypted/bonded connection** that only the original companion app could establish. Without bonding, all writes are silently discarded at the application layer. The pairing mechanism used by the app is unknown (possibly a custom PIN scheme or Android-specific "Just Works" implementation that Windows cannot replicate).

## Next Steps (Priority Order)

1. **[HIGH] UART serial adapter** — Order USB-to-UART (CP2102/FTDI, 3.3V TTL) for the SERIAL header on Misty's back panel. This is the guaranteed provisioning path.
2. **[MEDIUM] Sideload companion APK** — Install `com.mistyrobotics.Companion` v1.2.59 on an Android device, pair successfully, then use nRF Connect or Wireshark BLE sniffer to capture the pairing/provisioning traffic.
3. **[LOW] Micro-USB data cable** — Test if Misty's back USB port provides serial console with a proper micro-USB data cable (not USB-C adapter).
4. **[LOW] Pre-provision via REST API** — When Misty is on a known network, push additional SSIDs via `python misty_wifi.py add "SSID" "password"` so she auto-connects at other locations.



## Related Files

- `src/windows-orchestration/misty_ble_provision.py` — BLE provisioning tool
- `src/windows-orchestration/misty_wifi.py` — REST API WiFi manager
- `src/windows-orchestration/misty_discovery.py` — Network discovery
- `docs/lessons-learned-wifi-provisioning.md` — Initial investigation notes

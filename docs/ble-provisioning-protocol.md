# BLE WiFi Provisioning Protocol — Misty II (Reverse Engineering)

## Status: IN PROGRESS (Phase 1 complete, Phase 2 ready for hardware testing)

## Known BLE Interface

| Item | Value |
|------|-------|
| BLE Activation | Hold chin capacitive sensor |
| Device Name | Last 5 digits of serial (e.g., "03627") |
| Service UUID | `1562c132-3a0d-4f39-9e67-a9a632e8d6aa` |
| Write Characteristic | `3ee51024-7fdd-4d37-95aa-0a4b0e2d4f34` (write-without-response) |
| Read Characteristic | `418f52ab-10c6-42a6-9590-58cccb818f64` (returns `\x00\x00\x00` at rest) |
| BLE Address | Changes between sessions (not pinnable) |
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

## Protocol Hypotheses

Based on decompilation analysis and community research:

| # | Format | Payload Example | Confidence |
|---|--------|----------------|------------|
| 0 | JSON lowercase | `{"ssid":"X","password":"Y"}` | Medium |
| 1 | JSON PascalCase | `{"Ssid":"X","Password":"Y"}` | Medium-High |
| 2 | JSON + securityType | `{"ssid":"X","password":"Y","securityType":"wpa2"}` | Medium |
| 3 | JSON PascalCase + Security | `{"Ssid":"X","Password":"Y","SecurityType":"WPA2"}` | Medium |
| 4 | Length-prefixed binary | `[len1][ssid][len2][pass]` | Medium |
| 5 | Null-separated | `SSID\0PASSWORD\0` | Low (tested, failed) |
| 6 | Newline-separated | `SSID\nPASSWORD` | Low (tested, failed) |
| 7 | Command + payload | `\x01[len][ssid][len][pass]` | Low |
| 8 | TLV encoding | `\x01[len][ssid]\x02[len][pass]\x03\x01\x03` | Low |

### Why prior JSON attempts may have failed:
1. **Wrong key names** — REST API uses `Ssid`/`Password` (PascalCase), prior test used lowercase
2. **Missing fields** — might need `SecurityType`, `NetworkId`, or other metadata
3. **MTU issue** — payload may exceed BLE MTU and need chunking
4. **Handshake required** — may need to read/subscribe before writing
5. **Notification vs polling** — status may come via notify, not read

## Testing Strategy

Use `misty_ble_provision.py` to systematically test:

```bash
# 1. Discover all characteristics (may reveal additional ones)
python misty_ble_provision.py discover --address XX:XX:XX:XX:XX:XX

# 2. Test with notification subscription (catches async responses)
python misty_ble_provision.py notify "SSID" "password" --format 1

# 3. Brute-force all formats
python misty_ble_provision.py test-formats "SSID" "password"
```

## Next Steps

1. [ ] Hardware test: Run `discover` to confirm all characteristics (may have missed some)
2. [ ] Hardware test: Subscribe to notifications before writing
3. [ ] Hardware test: Try PascalCase JSON format (highest confidence)
4. [ ] Hardware test: Run full format sweep
5. [ ] If all fail: Use nRF Connect BLE sniffer to capture traffic from a working provisioning session (if companion app can be sideloaded on Android)
6. [ ] If all fail: Try UART serial path as alternative

## Related Files

- `src/windows-orchestration/misty_ble_provision.py` — BLE provisioning tool
- `src/windows-orchestration/misty_wifi.py` — REST API WiFi manager
- `src/windows-orchestration/misty_discovery.py` — Network discovery
- `docs/lessons-learned-wifi-provisioning.md` — Initial investigation notes

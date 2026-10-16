# Travel Wi-Fi setup

The companion laptop and Misty need a working local REST and WebSocket link.
Internet access is a separate concern. The new guided commands do not start
Foundry, load models, or change Windows networking automatically.

`wifi setup` defaults to laptop-hotspot mode. It reads Windows hotspot SSID/state
and the Internet Connection Sharing private-adapter identity, then derives a
bounded discovery subnet from that adapter. It does not infer the private adapter
from a familiar IP range or its display name. When discovery is unambiguous, the
only interactive steps are a hidden password and one final review/permission
confirmation. An OFF hotspot must be enabled manually; inaccessible or ambiguous
metadata produces a warning and manual SSID/interface selection. Private-adapter
discovery is not proof that venue sharing is permitted.

## Choose the connection

| Situation | Use |
|---|---|
| Venue Wi-Fi needs browser sign-in or acceptance of terms | Laptop joins and signs in; an approved Windows Mobile hotspot provides Misty's private network. |
| Non-portal network with a shared password and allowed device-to-device traffic | Direct Wi-Fi provisioning. |
| Enterprise credentials/certificates, prohibited sharing, or blocked local traffic | Ask venue IT for an approved option. These commands do not bypass access controls. |
| Misty cannot be reached on any existing network | Restore a previously rehearsed saved network first. There is no guaranteed Bluetooth/USB bootstrap in this tool. |

Windows supports sharing a Wi-Fi upstream through Mobile hotspot. Actual
adapter support, band compatibility, and company/venue permission must be
checked on the travel laptop. A browser login on the laptop does not authorize
Misty as a separate direct guest-network client.

## Before leaving

1. Keep Misty reachable on a working network. A temporary phone hotspot is
   acceptable for initial provisioning; ongoing cellular sharing is not required.
2. Stop any conference runner and run `npx . stop` to safely release the robot.
   Setup refuses an active controller/stage runner. Do not start another robot
   process while the wizard is open.
3. On the laptop, open **Settings > Network & internet > Mobile hotspot**.
   Choose the working **Wi-Fi** connection as the source, not cellular.
   Share over Wi-Fi, set a stable SSID/password, and try **2.4 GHz** for initial
   compatibility. Check hotspot power-saving behavior and keep the laptop awake.
4. Enable the hotspot, then run:

   ```powershell
   npx . wifi setup --mode hotspot --ip <current-Misty-IP>
   ```

5. Review the displayed robot serial/firmware and discovered **laptop hotspot
   SSID**, not the upstream home/venue Wi-Fi name. The wizard checks visibility
   from Misty. If discovery is unavailable, enter the hotspot SSID from Windows
   Mobile hotspot Settings.
6. Review the private adapter and derived subnet. Only if adapter discovery is
   ambiguous, select the private hotspot interface, not upstream Wi-Fi or a
   Hyper-V/VPN adapter; the subnet is then derived automatically. The current
   robot-network interface is labeled and refused in hotspot setup.
   `--subnet /24` derives a subnet from the selected adapter, and a full CIDR
   can also be supplied. No scan occurs until final permission is given.
   Enter the password at the hidden prompt; it is not saved on the laptop.
7. Confirm the review summary, sharing/probing permission, and switch in one
   prompt. It may immediately drop the old connection. A single matching saved
   profile is reused without asking for an ID; the entered password does not
   replace that profile's credentials. Multiple matches require a specific ID,
   or `new` to submit the entered password. Existing profiles are not deleted.
8. Wait for verified robot identity, target SSID, REST, and battery WebSocket
   telemetry. An accepted API request or a ping alone is not success.
9. Start with the explicit verified address printed by the tool:

   ```powershell
   npx . start --misty-ip <verified-Misty-IP>
   ```

Repeat after a robot power cycle with the **phone hotspot off**. Verify a normal
voice exchange. The travel connection is not ready until this rehearsal works.
Cache updates do not overwrite `.env`; its old `MISTY_IP` can still take
precedence on ordinary startup, so use the printed explicit address.

## At a captive-portal venue

1. Join venue Wi-Fi on the laptop and complete its browser sign-in manually.
2. Confirm that sharing is allowed. Enable the same private laptop hotspot,
   with venue **Wi-Fi** as its upstream.
3. Run:

   ```powershell
   npx . wifi verify
   ```

   The project CLI supplies the previously verified serial and robot ID from
   `.misty-services.json`. If those are unavailable, provide the confirmed values:

   ```powershell
   npx . wifi verify --serial <serial> --robot-id <robot-id>
   ```

4. Select and authorize only the private interface/subnet. If the robot's new IP
   is known, `--target-ip <ip>` probes that address alone instead of scanning.
5. Start services with the printed address. Local connectivity verification
   does not test portal authorization or Internet access.

The wizard will not broaden a scan to customer or VPN networks after failure.
It refuses overlapping interface scopes rather than modifying Windows routes.
Do not approve probing a network without authorization.

## Direct non-portal Wi-Fi

```powershell
npx . wifi setup --mode direct --ip <current-Misty-IP>
```

Use only a shared-password network that permits laptop-to-robot traffic.
Confirm the network type, robot identity, SSID, and switch. Join the destination
from Windows Settings when prompted, then select the approved destination
interface/subnet for verification. Open, captive-portal, and enterprise setup
are not supported by this mode.

## Options and dependencies

```powershell
npx . wifi --help
npx . wifi setup --help
npx . wifi verify --help
```

`--interface`, `--subnet`, and `--target-ip` override discovery with known approved
destination details. Subnet prompts in manual verification/direct mode accept
Enter for the displayed default, `/24` shorthand, or a full CIDR and retry
invalid interactive input. `--ip` describes the *current* connection during setup.
There is intentionally no password flag or automatic confirmation.
Use `--python <executable>` when the default Python is not the project runtime.
The existing requirements supply `requests`, `python-dotenv`, and
`websocket-client`; the Wi-Fi flow does not import STT/TTS or microphone modules.

Standalone `misty_wifi.py add/list/connect/scan/forget/push-all` commands remain
available for compatibility. Their positional passwords and legacy obfuscated
credential store are **not** used by the new guided flow.

## Failure and recovery

| Symptom | Next action |
|---|---|
| Target SSID not visible | Check hotspot enabled state, band, range, and spelling; no credentials were sent. |
| API rejects the request | Check SSID/security/password. A rejection is not a verified handoff. |
| Link disappears while switching | Outcome is unknown. The tool verifies the destination without replaying the write. |
| Same robot remains on the old SSID | Setup is not successful; investigate the target connection. |
| Robot not found in approved scope | Check the address in the hotspot client list, subnet selection, routing, and isolation. A timeout alone does not identify the cause. |
| REST works but WebSocket does not | Ask IT about local connectivity restrictions. Do not disable the firewall. |
| Process/interface inspection denied | Resolve Windows permissions; setup fails closed instead of guessing. |
| Cache persistence fails | The connection may be good, but caches may be partially updated. Use the explicit verified IP and resolve filesystem access. |

Keep the known-good robot network saved. If the old link remains reachable, an
explicit legacy `connect` command can switch to a saved profile. Otherwise
restore the already rehearsed saved network or temporary phone hotspot. Software
cannot promise rollback after losing its only control path. Power cycling is
not a guarantee that Misty will select a particular saved network.

Internet interruption does not necessarily prevent local AI when all assets
are cached, but Windows hotspot availability without upstream Internet must be
rehearsed, not assumed. Do not rely on untested UART, Bluetooth, or USB adapters.
Misty's documented robot-hosted hotspot is beta and is not part of this workflow.

End live testing with `npx . stop` to release audio/skills/motion and turn the LED
off. No networking command reboots the robot.

## Hardware acceptance record

An actual Windows laptop-hotspot handoff was validated with an Intel BE201
adapter and Misty firmware `2.0.2.11660`: the laptop stayed on upstream Wi-Fi,
Misty joined the separate hotspot subnet, Windows identified her MAC as its
hotspot client, and REST plus battery WebSocket telemetry worked. Misty reported
`InternetAccess` and fetched the exact public Microsoft connectivity-test text
through her documented `/api/request` endpoint with `Save=false` and
`Apply=false`. No phone hotspot was used for that handoff.

The first verification window expired before the robot finished joining. The
CLI now allows six bounded verification attempts with pauses, without replaying
the provisioning request.

This proves association, local control connectivity, and Internet sharing for
that setup. Cold-boot rejoin, a complete voice exchange, direct-network recovery,
and captive-portal venue behavior remain separate, unverified rehearsal gates.
Automated tests simulate network operations; they do not replace those checks.

Local historical notes claim 2.4-GHz-only behavior; official Misty specifications
list both 2.4 and 5 GHz. The 2.4-GHz recommendation above is a conservative
compatibility choice, not a universal hardware limitation.

Official references:

- Windows hotspot configuration discovery:
  `https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.networkoperatortetheringmanager.getcurrentaccesspointconfiguration`
- Internet Connection Sharing private-adapter identification:
  `https://learn.microsoft.com/en-us/windows/win32/api/netcon/nf-netcon-inetsharingconfiguration-get_sharingconnectiontype`
- Windows Mobile hotspot:
  `https://support.microsoft.com/en-us/windows/experience/connectivity-networking/use-your-windows-device-as-a-mobile-hotspot`
- Misty networking REST API:
  `https://docs.mistyrobotics.com/misty-ii/web-api/api-reference/`
- Misty hardware specifications:
  `https://docs.mistyrobotics.com/misty-ii/robot/misty-ii/`

Related research: #11 (travel networking) and #111 (BLE investigation). Their
closed status does not establish a successful hardware rehearsal.

# wine/ — knowledge base for running the AP apps under Wine

Everything Wine-related lives here: app facts, profiles, patches, findings and links.
The tooling that *uses* it is `../shim` (see `../README.md`).

```
wine/
  apps.toml          static facts per app (exe, registry keys to snapshot, own log files)
  profiles/*.toml    the things we vary between runs (WINEDEBUG, DLL overrides, env, patches, serial)
  patches/reg/*.reg  registry patches, imported by profiles (persistent in the prefix!)
  patches/README.md  how to add patches / winetricks verbs
```

## Profile format

```toml
description = "one line: what hypothesis this profile tests"
extends = "baseline"                 # parent profile; tables merge, scalars/lists replace

[wine]
prefix = "~/.wine"                    # WINEPREFIX
binary = "wine"                       # path to another wine build to compare versions
debug = "+timestamp,+pid,+tid,+comm"  # WINEDEBUG

[dll_overrides]                       # -> WINEDLLOVERRIDES, per run only (not persisted)
mfc80 = "n,b"

[serial]                              # persisted: dosdevices symlink + HKLM\Software\Wine\Ports
com3 = "/dev/rfcomm0"

[patches]
reg = ["ap-message-level-debug.reg"]  # imported before launch (persisted)
winetricks = ["vcrun2005"]            # installed once (skipped if in $WINEPREFIX/winetricks.log)

[env]                                 # env for both apps
[apps.divesight.env]                  # env for one app; any table can be put under apps.<app>
QT_LOGGING_RULES = "*.debug=true"
```

Persisted changes (reg, serial, winetricks) stay in the prefix after the run. Each run snapshots the
relevant registry keys before/after, so leftovers from earlier profiles are visible in
`registry.before.reg`. To undo a reg patch, write an inverse patch (e.g. `ap-message-level-off.reg`).

## Logging sources

| Source | How it is enabled | Where it ends up |
|---|---|---|
| Wine debug channels | `[wine] debug` → `WINEDEBUG` | `<run>/wine.log` (gz if > 5 MB) |
| Win32 `OutputDebugString` | `+debugstr` channel | `wine.log`, summarised |
| Qt (DiveSight) | `QT_LOGGING_RULES`, `QT_FORCE_STDERR_LOGGING`, `QT_MESSAGE_PATTERN` (`[qt …]` prefix) | `wine.log` (stderr), summarised as "Qt messages" |
| DiveSight own log | registry `Message Level` (reg patch) | `AppData/Local/AP/APDiveSight/AP_DiveSight_YYYY_MM_DD.log` → `<run>/app_logs/` |
| Communicator own log | registry `Message Level` (reg patch) | `ProgramData/AP/AP Communicator/StatusLog.txt` (plaintext) → `<run>/app_logs/` |
| App settings written during the run | always | `<run>/registry.diff` |
| Bluetooth packets (HCI/L2CAP/RFCOMM) | `--capture` / `live` profile | `<run>/capture.*`, `## packet capture` |

Notes: DiveSight installs its own `qInstallMessageHandler`, so Qt messages may be routed only to its
own (encrypted) log rather than stderr. Wine's `warn+all` is very noisy (`x11drv`, `font`) — mute
channels with `warn-x11drv` etc. Channel names: see Debug-Channels link below.

## Findings so far (keep updated — newest first)

- 2026-10-12 ModemManager contention: run `20261012-184353_quiet+cli_3cb69892`, with the service
  stopped, downloaded dives 422-400 (23 total); 1,220 profile-block requests/replies matched.
  Earlier captures had `AT\r` interleaved with AP reads while the journal showed ModemManager
  probing `rfcomm0`. Disabling Wine tracing alone did not fix failures. The first two HCI attempts
  in the successful run failed with status 0x04 (Page Timeout); a later link ended with reason
  0x08 (Connection Timeout) before the sustained session. These are separate from modem contention.
  Use [the per-port fix](../BLUETOOTH_FIX.md), then verify with ModemManager running; the permanent
  rule has not yet been tested against the real handset.

- 2026-10-12 Mock handset (`--mock`): Wine happily uses a **pty** as COM3; DiveSight auto-connected,
  65/65 requests answered from the capture (handshake, controllers, dive list, block reads). Run
  `20261012-175822_mock_e60404e8`. DiveSight rewrote `Message Level` 3 → 1 on exit (3 may be out of
  range / combo index) — check before relying on the debug reg patch.

- 2026-10-12 DiveSight startup without device: Wine polls serial status continuously
  (`comm:check_events` / `get_modem_status` ≈ 140/s, ~34k trace lines in 20 s) — WaitCommEvent emulation.
  Candidate factor for timeouts/latency.
- 2026-10-12 Communicator writes a **plaintext** log `C:\ProgramData\AP\AP Communicator\StatusLog.txt`
  (created on first run; logs "Debug Message Level 3", so the reg patch is honoured). Harvested + summarised.
- DiveSight's daily log `AP_DiveSight_*.log` is **encrypted/obfuscated** (no plaintext header). Decoding
  it is an open task — try the AES/XOR routines in `../../inspiration/ccl.py`.
- `install dir/divesight_debug*.log` are older manual `WINEDEBUG` dumps (not written by the app).
- Downloads work but the Bluetooth connection is unreliable and frequently times out.

## App facts

**AP DiveSight** — Qt 6, x86-64, `Qt6SerialPort`. Settings `HKCU\Software\AP\APDiveSight\Settings`:
`Connect\Com Port` = `\\.\COM3`, `Download BT Read Time`, `Download Short Read Time` (look self-calibrated,
watch them in `registry.diff`), `Message Level`, `Secondary Trace Group`. Strings worth chasing:
`APCmdEnhancedLogging`, `Extract Controller Logging`, `comboBoxDebugLevel`, `ADEComms:GetMessage() Timeout`.

**AP Communicator** — MFC 8 (VS2005), i386. Opens `\\.\COM%d` directly and scans ports
("Checking COM%d"). Settings `HKCU\Software\AP\APCommunicator\Settings` (`Com Port`, `Message Level`,
directories). Ships `slab_usb_spi.dll` (Silicon Labs USB bridge → wired-cable path, needs USB access).

**Prefix** — `~/.wine`, wine-11.16 (Staging), win64/WoW64. `COM3 → /dev/rfcomm0`
(`HKLM\Software\Wine\Ports` + `dosdevices/com3`). Bluetooth SPP: handset `AP Vision-2161`
`00:06:66:19:21:61` (Roving Networks module); bound automatically by `shim run` / `./shim bt bind`.

## Hypotheses / experiments backlog

- [ ] Serial timing: compare `quiet` vs `debug` (does tracing overhead change timeouts?).
- [x] Compare quiet / Wine-tracing-off live runs: neither resolves modem interference by itself.
- [x] Identify competing port consumer: ModemManager; stopping it allowed sustained downloads.
- [ ] Validate the per-port ModemManager exclusion with the service running, then on other machines.
- [ ] Compare Wine stable vs staging (`[wine] binary`), and older versions.
- [ ] Watch `Download BT Read Time` drift across runs; try pinning it via a reg patch.
- [ ] rfcomm: `bind` vs `connect`, and reconnect behaviour after timeouts.
- [x] Find Communicator's log destination (StatusLog.txt).
- [ ] Find valid `Message Level` range (Ghidra: `comboBoxDebugLevel`).
- [ ] Decrypt DiveSight daily log.
- [ ] Mock: reproduce timeouts with `--chunk-delay`/`--latency`/`--stall-after`; find the app's thresholds.
- [ ] Mock: record a Communicator session (`./shim run communicator live`) and replay it with
      `--mock-arg=--capture=...` so Communicator can be served too.
- [ ] Live: correlate RFCOMM gaps in `capture.rfcomm_times.txt` with Wine `comm` timeouts in wine.log.

## Resources

- Wine debug channels: https://gitlab.winehq.org/wine/wine/-/wikis/Debug-Channels
- Wine User's Guide (serial ports, DLL overrides, WINEDEBUG): https://gitlab.winehq.org/wine/wine/-/wikis/Wine-User's-Guide
- Wine man page / env vars: https://gitlab.winehq.org/wine/wine/-/wikis/Man-Pages/wine
- Useful registry keys: https://gitlab.winehq.org/wine/wine/-/wikis/Useful-Registry-Keys
- Wine serial implementation (`comm` channel): https://gitlab.winehq.org/wine/wine/-/blob/master/dlls/ntdll/unix/serial.c
- Wine bugs mentioning serial: https://bugs.winehq.org/buglist.cgi?quicksearch=serial%20port
- WineHQ AppDB: https://appdb.winehq.org/
- Winetricks: https://github.com/Winetricks/winetricks
- Qt logging categories / rules: https://doc.qt.io/qt-6/qloggingcategory.html
- Qt debugging env vars: https://doc.qt.io/qt-6/debug.html
- QSerialPort: https://doc.qt.io/qt-6/qserialport.html
- BlueZ rfcomm: `man rfcomm`

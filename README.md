# divesight-wine-shims

Make **AP DiveSight** and **AP Communicator** run painlessly under Wine on Linux — first by
systematically finding the right Wine configuration, then by packaging it for non-technical users.

Every experiment is a *run*: a named profile is applied to the Wine prefix, the app is launched with
full Wine/Qt/app logging, and everything is saved under `logs/<app>/<timestamp>_<profile>_<hash>/`
with a short `summary.txt` you (or an agent) read first.

## Layout

```
shim                 CLI entry point (python3 >= 3.11, stdlib only)
tools/shim.py        run / apply / show / note / list / diff / doctor
tools/logsummary.py  condenses wine.log into summary.txt
wine/                ALL Wine knowledge: apps.toml, profiles/, patches/, findings + links (wine/README.md)
logs/                run directories + index.tsv (one line per run)
AGENTS.md            low-token workflow for coding agents
```

## Quick start

**Repeated Bluetooth disconnects?** See [the reproducible Bluetooth fix](BLUETOOTH_FIX.md).
ModemManager probing the handset's serial port was the main interference found in live tests.
The standalone script installs a reversible per-port exclusion without disabling mobile broadband.
Its `check` and `install` repair missing system and Wine COM mappings; use `check --read-only`
for diagnostics, `--system-only` for the udev fix, or `--com-only` for Wine mapping repair.

```sh
./shim doctor divesight                 # prefix, exe, /dev/rfcomm0 present & writable?
./shim bt bind                          # bind the handset's BT serial port (run also does this)
./shim profiles                         # baseline, debug, verbose-io, quiet
./shim run divesight debug --note "download dive 422 over BT"
#   ... use the app, close it, answer the outcome prompt (ok/partial/fail/crash/skip)
./shim show                             # summary of the latest run
./shim list                             # progress table (logs/index.tsv)
./shim note divesight:latest --outcome fail "timed out at block 3"
./shim diff <runA> <runB>               # what changed in config and in log stats
```

Quick one-off tweaks without writing a profile (they are recorded, profile becomes `debug+cli`):

```sh
./shim run communicator debug --winedebug "+timestamp,+comm,+file" --dll mfc80=n,b --env FOO=1
./shim run divesight debug --timeout 30 --no-prompt   # headless startup smoke test
./shim run divesight debug --dry-run                  # effective config + hash, nothing runs
./shim run divesight debug -- --some-app-arg          # args after -- go to the exe
```

## Bluetooth binding (live runs)

`run` auto-binds the Bluetooth serial port before launching whenever the profile maps a COM port to
`/dev/rfcommN` and that node is missing. The handset is configured in `[bluetooth]` in
`wine/profiles/baseline.toml` (MAC, `channel = "auto"` = SDP lookup, fallback channel). The output goes
to `bluetooth.log` in the run directory. Skip it with `--no-bind`; mock runs never bind.

```sh
./shim bt              # status of /dev/rfcomm0
./shim bt channel      # ask the handset (switched on) for its SPP channel
./shim bt bind         # bind now (sudo)
./shim bt release      # unbind
```

`rfcomm bind` needs root. `run` tries `sudo -n` first and, on a terminal, falls back to a password
prompt. For fully unattended/agentic runs, allow just these commands without a password:

```sh
echo "$USER ALL=(root) NOPASSWD: /usr/bin/rfcomm bind *, /usr/bin/rfcomm release *" \
  | sudo tee /etc/sudoers.d/ap-rfcomm && sudo chmod 440 /etc/sudoers.d/ap-rfcomm && sudo visudo -c
```

Binding only reserves the device node; the radio link opens when the app opens COM3, so the handset
must be on with Bluetooth enabled at that point.

## Mock handset (no rebreather needed)

`--mock` (or the `mock` profile) starts `../inspiration/mockdevice.py`: a fake handset on a Linux
pseudo-terminal that answers **only from a recorded real session** (`../Download dive 422_New_ver.pcapng`).
For the run, the profile's COM port (`com3`) is pointed at the pty; afterwards it is restored to the real
device. Requests the recording cannot answer are logged as `UNKNOWN` (never invented).

```sh
./shim run divesight mock --note "..."                         # debug logging + BT-like fragmentation
./shim run divesight debug --mock                              # any profile against the mock
./shim run divesight debug --mock-arg=--stall-after=40         # simulate the link dying mid-download
./shim run communicator debug --mock-arg=--unknown=nak --mock-arg=--hex
```

mockdevice knobs: `--latency S` (before each reply), `--chunk N` / `--chunk-delay S` (fragment replies like
RFCOMM), `--stall-after N` (stop answering), `--unknown silent|nak`, `--hex` (raw bytes), `--capture FILE`.
Standalone: `cd .. && .venv/bin/python -m inspiration.mockdevice --link /tmp/ap-mock/com3`.
The recording covers handshake, dive list and download of dive 422 from DiveSight (newer version);
anything else (other dives, Communicator-specific commands) shows up as `UNKNOWN` → record a new
usbmon capture of that flow to extend it.

## Packet capture (live BT runs)

`./shim run divesight live --note "..."` (= `debug` + capture), or add `--capture` to any run.
`dumpcap` records the Bluetooth adapter rootless (user in group `wireshark`) to `capture.pcapng`, and is
stopped/decoded automatically after the app exits. Pick interfaces with `--capture-iface`
(repeatable; `tshark -D` lists them):

| interface | content |
|---|---|
| `bluetooth0` (default) | HCI traffic of hci0: connect/disconnect, L2CAP, RFCOMM, payload |
| `bluetooth-monitor` | same plus controller/kernel monitor events (btmon view) |

`[capture] tool = "btmon"` instead writes `capture.btsnoop` + `capture.btmon.txt` via `sudo -n btmon`
(sudoers: `%wireshark ALL=(root) NOPASSWD: /usr/bin/btmon`). Avoid usbmon: the dongle shares USB bus 1
with keyboard/mouse, so it would record keystrokes.

Decoded output: `capture.events.txt` (link events), `capture.rfcomm_times.txt` (per RFCOMM frame time +
direction), `capture.serial.txt` (AP protocol frames). `## packet capture` in the summary lists link
events, the largest RFCOMM gaps (where timeouts happen) and frame counts per command.

Captures contain dive data and are gitignored. A live capture can be replayed by the mock:
`./shim run communicator mock --mock-arg=--capture=$PWD/logs/communicator/<run>/capture.pcapng`
(the loader accepts usbmon, H4 `bluetooth0` and `bluetooth-monitor` pcapng).

## What a run records

| File | Content |
|---|---|
| `manifest.json` | effective config, config hash, command, env, wine version, serial preflight, exit, duration, outcome, notes, shim git rev |
| `summary.txt` | bounded digest: warnings, registry diff, crash markers, top err/warn/fixme, serial (`comm`) calls, OutputDebugString, Qt messages, native DLLs |
| `profile/` | copies of the profile chain and .reg patches exactly as used |
| `wine.log[.gz]` | full stderr: Wine debug channels + Qt stderr (gitignored) |
| `app.stdout.log` | app stdout |
| `apply.log` | patching output (serial mapping, reg import, winetricks) |
| `registry.{before,after}.reg`, `registry.diff` | app + Wine registry keys around the run |
| `bluetooth.log` | rfcomm bind/SDP output (live runs) |
| `capture.*` | (capture runs) pcapng/btsnoop, decoded events, RFCOMM timings, AP frames |
| `mock.log`, `mock.stats.json` | (mock runs) every request with timestamp + verdict, and counters |
| `changed_files.txt`, `app_logs/` | files modified in `drive_c` during the run; app's own logs copied |

Run identity: the directory name is `<YYYYmmdd-HHMMSS>_<profile>_<hash>`. The 8-char hash covers the
whole effective config **and** the content of referenced .reg files, so identical hashes mean identical
configurations; `shim diff` shows exactly what differs.

## Workflow

1. Write a hypothesis as a profile in `wine/profiles/` (`extends = "debug"`, change one thing).
2. `./shim run <app> <profile> --note "..."`, do the test, record the outcome.
3. Read `summary.txt`; grep raw logs only for specifics (`zgrep comm logs/divesight/latest/wine.log*`).
4. Write findings into `wine/README.md` (Findings / backlog). Promote what works into `baseline`.

## Plan

1. **Investigate** current prefix: registry, DLL overrides, serial mapping — which are needed. *(in progress)*
2. **Test** profiles until the BT connection is reliable for both apps.
3. **Package** the winning profile for end users: one-click launcher / installer that creates the
   prefix, maps the serial port and applies patches (`shim apply` is the seed of this).

### Progress

- 2026-10-12: With ModemManager stopped, downloaded 23 dives (422-400) successfully, including a
  long dive and a batch. Initial connection failures still occurred. A standalone, reversible
  per-port udev exclusion is now available; validate it with ModemManager running on each machine.
- 2026-10-12: Bluetooth packet capture (`--capture`, `live` profile) with automatic decode/summary.
- 2026-10-12: mock handset works under Wine (pty as COM3): DiveSight handshake, dive list and
  block reads all served from the capture — safe automated testing without the rebreather.
- 2026-10-12: framework in place; first `debug` smoke run shows heavy Wine serial status polling.

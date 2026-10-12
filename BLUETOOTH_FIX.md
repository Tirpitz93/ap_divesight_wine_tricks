# AP DiveSight Bluetooth on Linux: stop modem detection interfering

**Symptom:** DiveSight briefly connects, then disconnects, or repeatedly fails verification.
Linux's ModemManager can mistake the Bluetooth serial port for a modem and probe it while
DiveSight is using it. That can interfere with AP requests and replies.

**Fix:** reserve one Bluetooth serial port for the handset using a small udev rule.
ModemManager stays running for other devices, including mobile-broadband modems.

## What has actually worked

On 12 October 2026, stopping ModemManager allowed DiveSight under Wine to download **23 dives
(422 through 400)**, including a 70+ minute dive and a batch of 20 dives. With ModemManager
running, captures showed `AT` modem probes interleaved with AP traffic, and the service journal
showed it probing `rfcomm0`. Turning off Wine tracing alone did not resolve the failures.

This script packages the **per-port exclusion**, not a permanent service shutdown. The rule
still needs a real download test with ModemManager running; the successful stop-service test
does not by itself validate the rule on every distribution. Early connection attempts still
failed in that successful run, and the capture recorded a link timeout before the sustained
download session. This is not a cure for all radio or connection problems.

## Before starting

- Use Linux with udev, Python **3.8 or newer**, and ModemManager **1.10 or newer** if installed.
  Python needs no extra packages. The script does not download or install anything.
- Have **Wine 11.16 or newer** and AP DiveSight installed, your handset paired, and a Bluetooth serial port
  configured. This is a Bluetooth interference fix, **not a Wine or DiveSight installer**.
- Know which port is reserved for the handset: normally `/dev/rfcomm0`. If another application
  uses that port for a real Bluetooth modem, choose a different port.
- Close DiveSight and any other program using that port before setup. Do not run Wine as root.

The exclusion is for a **port number, not a handset's Bluetooth address**. Any device later
assigned to that same port will also be ignored by ModemManager. It does not ignore all Bluetooth
ports, USB serial ports, or the Bluetooth adapter.

## Install once on each machine

Keep [ap_bluetooth_fix.py](tools/ap_bluetooth_fix.py) and this guide together. You can copy them
to another computer; the script works on its own, outside this repository. Open a terminal in
the folder containing the script and run:

```sh
python3 ap_bluetooth_fix.py install --dry-run
python3 ap_bluetooth_fix.py install
```

The preview changes nothing. Both `install` and `check` now **repair by default**: they create
the COM symlink, import the Wine Ports entry using **Wine regedit**, and install the udev
exclusion when missing. Correct entries are left untouched, including avoiding a repeated
registry import or udev reload. Only the system-rule step requests administrator permission.
Run the full command **without sudo**; Wine must never run as root.

The system step writes `/etc/udev/rules.d/78-ap-bluetooth-rfcomm0.rules` and reloads udev's rules.
It does not stop/restart ModemManager or reconfigure a live Linux device.
Wine regedit can start Wine processes; **close DiveSight and other Wine programs first**.

| Option | Behaviour for `check` and `install` |
|---|---|
| No scope option | Repair system exclusion and Wine COM mappings, then report their updated state. |
| `--system-only` | Only the original udev exclusion; no Wine dependency or prefix changes. |
| `--com-only` | Only Wine COM symlink and registry entry; no sudo or system rule changes. |
| `check --read-only` | Inspect selected systems without repairs or requesting sudo. |
| `--dry-run` | Preview selected changes without applying them. |

For the original administrator-only installer, run
`sudo python3 ap_bluetooth_fix.py install --system-only`.
Save the script locally before full automatic repair: a script piped into Python cannot
relaunch itself for sudo. Existing pinned Gist download examples refer to an older revision;
they do not automatically receive local changes.

**Reboot the computer before testing.** This avoids leaving an already-running modem probe
attached to the port. The script never reboots automatically. You can rerun installation safely;
it refuses to overwrite a modified or unrelated file with the same name.

For a different port, use the same option for every command:

```sh
python3 ap_bluetooth_fix.py install --port rfcomm2
```

When using the script from this repository, replace `ap_bluetooth_fix.py` in the commands above
with `shim/tools/ap_bluetooth_fix.py` (commands are relative to the repository root).

## Verify, then download

After reboot, re-establish the handset's Bluetooth serial binding as usual. A paired device
alone does not guarantee `/dev/rfcomm0` exists; the Wine shim's `bt bind` command can create it.
Then run:

```sh
python3 ap_bluetooth_fix.py check
```

Look for **`rfcomm0 is protected`** and **`Effective ID_MM_PORT_IGNORE: 1`**. This checks the
actual udev property, not just whether a rule file was saved. It does not open the serial port.
Also look for **`Wine serial: COM3 is configured`** and **`Configuration ready: True`**.
The version requirement is a setup gate, not a claim that older Wine caused the interference;
the original successful stop-service experiment used Wine 11.16 Staging.

| Result | Meaning |
|---|---|
| `protected` | The live port has the ignore property. Check Wine readiness before a download test. |
| `pending-reboot` | Our rule exists but the live property is missing. Reboot, bind, and check again. |
| `port-missing` | The port is not bound; protection cannot yet be verified. |
| `unprotected` | The live port is not excluded. Install the rule for the correct port. |
| `ERROR` | Setup or inspection failed. Read the error; do not treat it as success. |

### Wine version and serial configuration

`check --read-only` runs only `wine --version` and reads existing prefix files. It checks that
`dosdevices/com3` is a symlink to `/dev/rfcomm0`, and that any saved
`HKLM\Software\Wine\Ports` COM3 override in `system.reg` points to the same device.
An absent registry override is fine; a conflicting or unrecognised value is reported.
If the device exists, the check also reports missing read/write permissions for the current user.
It does not initialise a prefix, change registry values, or configure DiveSight.
Default `check` and `install` repair missing or incorrect symlinks and registry values before
listing the result. An absent registry override is installed during repair even though a
matching symlink alone is sufficient for the read-only diagnostic.
Select **COM3 inside DiveSight** yourself.

The default prefix is `$WINEPREFIX`, or `~/.wine` when unset. For another installation:

```sh
python3 ap_bluetooth_fix.py check --wine-prefix "$HOME/my-wine-prefix" \
  --wine-binary /path/to/wine --com-port COM5 --port rfcomm2
```

Close Wine applications before checking: the registry inspection sees **saved configuration**,
not unsaved changes in a running Wine registry. Run `check` as your normal desktop user, **without
sudo**, so it inspects your prefix and permissions. A ready report validates configuration only,
not an active Bluetooth connection or a successful download.

Repair requires an **existing prefix** with a `dosdevices` directory. It refuses to replace
a regular file or directory at the COM symlink location, but corrects an existing wrong symlink
and replaces an incorrect registry value for the selected COM port. Other mappings are preserved.
`remove` still removes only the system rule; it does not remove Wine mappings.
`--system-only` install and removal do not depend on Wine.
The script never binds an unpaired handset, changes device permissions, upgrades Wine, or
changes DiveSight's port selection. Missing bindings and permissions remain explicit diagnostics.

**Keep ModemManager running during the permanent-fix test**, if it is installed. On systems
using systemd, `sudo systemctl start ModemManager.service` restores it if you stopped it for the
earlier experiment. The script's check reports its state. A successful download with the service
still stopped does not demonstrate that the exclusion works.

For the existing Wine shim, run these from the repository root:

```sh
shim/shim bt bind
python3 shim/tools/ap_bluetooth_fix.py check
shim/shim run divesight quiet --note "Per-port ModemManager exclusion; service running"
```

The baseline Bluetooth address is specific to the original test handset. For your own handset,
create `shim/wine/profiles/my-handset.toml`, substituting its paired Bluetooth address:

```toml
extends = "quiet"
description = "My paired AP handset; minimal Wine logging"

[bluetooth]
mac = "AA:BB:CC:DD:EE:FF"
```

Use `shim/shim run divesight my-handset` instead. The run auto-binds a missing port using that
profile. For a port other than `rfcomm0`, also set `[serial] com3 = "/dev/rfcomm2"` in the profile
and install the rule for `rfcomm2`. See [README.md](README.md) for binding, permissions and profiles.
If binding fails, resolve the reported permissions/dependency error; do not make serial devices
world-writable. Close the first run and check protection before doing the download test.

`quiet` avoids Wine's high-volume serial tracing. Add `--capture` only when troubleshooting:
it needs dumpcap/tshark and capture permissions. Capture files may include dive data and personal
information stored on the handset, so do not publish them unredacted.

## Undo

```sh
sudo python3 ap_bluetooth_fix.py remove
```

Then reboot. Removal deletes only the exact rule installed by this script and reloads the rules.
It leaves other rules and service settings alone. If the rule was edited locally, removal
refuses rather than silently deleting your changes. If another rule also excludes the port,
removing this rule will not undo that separate exclusion.

## Reproduce on another machine

Use the same script and port selection, then save the read-only diagnostic report:

```sh
python3 ap_bluetooth_fix.py check --read-only --json > bluetooth-check.json
```

The report contains script version, CPU architecture, kernel/Python versions, selected port,
installed-rule state, effective ignore property, ModemManager service state, and a `wine` object
with version, prefix, COM mapping and diagnostics. `status` remains the Bluetooth-only result;
`ready` combines the checks for the selected scope (normally Bluetooth protection, Wine 11.16+
and serial configuration). `changes` lists applied repairs, and a dry-run report includes
`planned_changes`. The text output lists both COM symlink and registry targets after repairs.
The report omits
handset addresses and captured dive data, but **prefix and executable paths may contain your
username**: redact paths and diagnostic details before sharing publicly.
Check exits **0** when all readiness checks pass, **1** when protection or Wine setup is not
ready, and **2** for an operational inspection error. Install/remove
exit 0 when requested repairs completed (or the dry-run preview succeeded), **not** when
downloads are proven. Check may still exit 1 after successful repair when a reboot is required,
the port is unbound, permissions are insufficient, or Wine is below the minimum version.

Record your distribution, Wine/ModemManager versions, adapter model, connection attempts,
individual download result, batch download result, and whether the connection remained stable.
For shim runs, the manifest already records Wine, kernel, configuration and outcome. Test a known
long dive and a batch; a brief connection is not sufficient. No other machines are claimed as
tested yet.

If the rule is installed but protection stays missing after reboot, check the port number,
udev errors and ModemManager version. On systems without systemd, service status may be
`unknown`; the script reports the diagnostic rather than pretending the service is stopped.
If protection is confirmed but failures persist, investigate radio/link errors and serial timing
separately. Do not change firmware, calibration, or handset configuration to test this fix.

## Sharing

Share this guide and the single Python script as a small release/archive; no virtual environment,
Wine prefix, Windows application binaries, logs or capture files are needed. Recipients can
preview the exact rule before allowing the administrator-only install step. Do not distribute a
copy of your configured Wine prefix or personal handset settings.

The rule uses ModemManager's documented
[`ID_MM_PORT_IGNORE`](https://www.freedesktop.org/software/ModemManager/api/latest/ModemManager-Common-udev-tags.html)
property:

```udev
ACTION=="add|change", SUBSYSTEM=="tty", KERNEL=="rfcomm0", ENV{ID_MM_PORT_IGNORE}="1"
```

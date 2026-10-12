#!/usr/bin/env python3
"""shim - reproducible, fully logged Wine runs of AP DiveSight / AP Communicator.

Stdlib only (Python >= 3.11). See ../README.md and ../AGENTS.md.
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logsummary  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WINE_DIR = ROOT / "wine"
PROFILE_DIR = WINE_DIR / "profiles"
REG_DIR = WINE_DIR / "patches" / "reg"
LOG_DIR = ROOT / "logs"
GZIP_ABOVE = 5 * 1024 * 1024
OUTCOMES = ("ok", "partial", "fail", "crash", "skip")


# ---------------------------------------------------------------- config ---

def load_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def apps() -> dict:
    return load_toml(WINE_DIR / "apps.toml")


def resolve_profile(name: str, seen: tuple = ()) -> tuple[dict, list[str]]:
    path = PROFILE_DIR / f"{name}.toml"
    if not path.exists():
        sys.exit(f"unknown profile '{name}' (see: shim profiles)")
    if name in seen:
        sys.exit(f"profile extends-cycle: {' -> '.join(seen + (name,))}")
    data = load_toml(path)
    parent = data.pop("extends", None)
    if not parent:
        return data, [name]
    base, chain = resolve_profile(parent, seen + (name,))
    return merge(base, data), chain + [name]


def parse_kv(items: list[str], what: str) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            sys.exit(f"--{what} expects KEY=VALUE, got '{item}'")
        key, value = item.split("=", 1)
        out[key] = value
    return out


def effective_config(app: str, profile: str, args=None) -> dict:
    catalogue = apps()
    if app not in catalogue or app == "common":
        sys.exit(f"unknown app '{app}' (choose: {', '.join(k for k in catalogue if k != 'common')})")
    data, chain = resolve_profile(profile)
    per_app = data.pop("apps", {}).get(app, {})
    cfg = merge(data, per_app)

    cli = {}
    if args is not None:
        if args.winedebug:
            cli.setdefault("wine", {})["debug"] = args.winedebug
        for key, src in (("env", args.env), ("dll_overrides", args.dll), ("serial", args.serial)):
            if src:
                cli[key] = parse_kv(src, key.replace("_overrides", ""))
        if getattr(args, "mock", False) or getattr(args, "mock_arg", None):
            cli["mock"] = {"enabled": True}
            if args.mock_arg:
                cli["mock"]["args"] = cfg.get("mock", {}).get("args", []) + args.mock_arg
        if getattr(args, "capture", False) or getattr(args, "capture_iface", None):
            cli["capture"] = {"enabled": True}
            if args.capture_iface:
                cli["capture"]["interfaces"] = args.capture_iface
    cfg = merge(cfg, cli)

    serial = dict(cfg.get("serial", {}))
    mock = dict(cfg.get("mock", {}))
    if mock.get("enabled"):
        port = mock.get("port", "com3").lower()
        mock["port"] = port
        mock["real_device"] = serial.get(port, "")
        mock["link"] = f"/tmp/ap-shim-mock-{os.getuid()}/{port}"
        serial[port] = mock["link"]

    app_def = catalogue[app]
    return {
        "app": app,
        "profile": profile + ("+cli" if cli else ""),
        "chain": chain,
        "description": cfg.get("description", ""),
        "wine": cfg.get("wine", {}),
        "dll_overrides": cfg.get("dll_overrides", {}),
        "serial": serial,
        "bluetooth": cfg.get("bluetooth", {}),
        "capture": cfg["capture"] if cfg.get("capture", {}).get("enabled") else {},
        "mock": mock if mock.get("enabled") else {},
        "patches": {"reg": cfg.get("patches", {}).get("reg", []),
                    "winetricks": cfg.get("patches", {}).get("winetricks", [])},
        "env": {k: str(v) for k, v in cfg.get("env", {}).items()},
        "cli_overrides": cli,
        "app_def": app_def,
        "registry_keys": catalogue.get("common", {}).get("registry", []) + app_def.get("registry", []),
    }


def config_hash(eff: dict) -> str:
    digest = hashlib.sha256()
    payload = {k: v for k, v in eff.items() if k not in ("description", "chain", "profile")}
    digest.update(json.dumps(payload, sort_keys=True).encode())
    for reg in eff["patches"]["reg"]:
        path = REG_DIR / reg
        digest.update(path.read_bytes() if path.exists() else b"missing:" + reg.encode())
    return digest.hexdigest()[:8]


# ------------------------------------------------------------------ wine ---

def prefix_of(eff: dict) -> Path:
    return Path(os.path.expanduser(eff["wine"].get("prefix", "~/.wine")))


def wine_bin(eff: dict) -> str:
    return eff["wine"].get("binary", "wine")


def wineserver_bin(eff: dict) -> str:
    binary = wine_bin(eff)
    if os.sep in binary:
        return str(Path(binary).with_name("wineserver"))
    return "wineserver"


def base_env(eff: dict, debug: str | None = None) -> dict:
    env = dict(os.environ)
    env["WINEPREFIX"] = str(prefix_of(eff))
    env["WINEDEBUG"] = debug if debug is not None else "-all"
    return env


def win_to_unix(prefix: Path, winpath: str) -> Path:
    drive, rest = winpath[0].lower(), winpath[2:].lstrip("\\")
    return prefix / "dosdevices" / f"{drive}:" / rest.replace("\\", "/")


def sh(cmd: list[str], log, env=None, timeout=300) -> int:
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()
    try:
        return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.write(f"!! {exc}\n")
        return -1


def capture(cmd: list[str], env=None, timeout=10, cwd=None) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, cwd=cwd)
        return (res.stdout + res.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"


def set_serial(eff: dict, port: str, device: str, log) -> None:
    port = port.lower()
    link = prefix_of(eff) / "dosdevices" / port
    if link.is_symlink() and os.readlink(link) == device:
        log.write(f"serial {port} -> {device} (already)\n")
    else:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(device)
        log.write(f"serial {port} -> {device} (symlink set)\n")
    sh([wine_bin(eff), "reg", "add", r"HKLM\Software\Wine\Ports", "/v", port.upper(),
        "/d", device, "/f"], log, base_env(eff))


def apply_patches(eff: dict, log) -> list[str]:
    """Persistently apply serial mapping, .reg files and winetricks verbs. Returns warnings."""
    warnings = []
    prefix, env = prefix_of(eff), base_env(eff)
    for port, device in eff["serial"].items():
        set_serial(eff, port, device, log)
    for reg in eff["patches"]["reg"]:
        path = REG_DIR / reg
        if not path.exists():
            warnings.append(f"reg patch missing: {reg}")
            continue
        winpath = "Z:" + str(path).replace("/", "\\")
        if sh([wine_bin(eff), "reg", "import", winpath], log, env) != 0:
            warnings.append(f"reg import failed: {reg}")
    if eff["patches"]["winetricks"]:
        done_log = prefix / "winetricks.log"
        done = set(done_log.read_text().split()) if done_log.exists() else set()
        for verb in eff["patches"]["winetricks"]:
            if verb in done:
                log.write(f"winetricks {verb} (already installed)\n")
            elif sh([eff["wine"].get("winetricks", "winetricks"), "-q", verb], log, env, timeout=1800) != 0:
                warnings.append(f"winetricks failed: {verb}")
    return warnings


def preflight(eff: dict) -> tuple[dict, list[str]]:
    warnings = []
    prefix = prefix_of(eff)
    info = {
        "wine_version": capture([wine_bin(eff), "--version"]),
        "kernel": capture(["uname", "-sr"]),
        "prefix": str(prefix),
        "exe_exists": win_to_unix(prefix, eff["app_def"]["exe"]).exists(),
        "serial": {},
        "rfcomm": capture(["rfcomm", "-a"], timeout=3),
    }
    if not prefix.is_dir():
        warnings.append(f"prefix missing: {prefix}")
    if not info["exe_exists"]:
        warnings.append(f"exe missing: {eff['app_def']['exe']}")
    for port, device in eff["serial"].items():
        present = os.path.exists(device)
        rw = present and os.access(device, os.R_OK | os.W_OK)
        info["serial"][port] = {"device": device, "present": present, "rw": rw}
        if not present:
            warnings.append(f"{port}: {device} not present (./shim bt bind)")
        elif not rw:
            warnings.append(f"{port}: {device} not read/writable (dialout group?)")
    return info, warnings


# -------------------------------------------------------------- registry ---

def reg_snapshot(prefix: Path, keys: list[str]) -> str:
    """Extract matching sections from the prefix's .reg text files (no wine process needed)."""
    files = {"HKCU": prefix / "user.reg", "HKLM": prefix / "system.reg"}
    wanted: dict[str, list[str]] = {}
    for key in keys:
        hive, _, sub = key.partition("\\")
        wanted.setdefault(hive.upper(), []).append(sub.lower())
    out = []
    for hive, subs in wanted.items():
        path = files.get(hive)
        if not path or not path.exists():
            continue
        keep = False
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("["):
                name = line[1:line.rfind("]")].replace("\\\\", "\\")
                keep = any(name.lower().startswith(s) for s in subs)
                if keep:
                    out.append(f"[{hive}\\{name}]")
                continue
            if keep and not line.startswith("#time="):
                out.append(line)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------- harvest ---

def harvest(eff: dict, since: float, run_dir: Path) -> tuple[list[str], list[str]]:
    drive_c = prefix_of(eff) / "drive_c"
    changed = []
    for dirpath, dirnames, filenames in os.walk(drive_c):
        if Path(dirpath) == drive_c:
            dirnames[:] = [d for d in dirnames if d.lower() != "windows"]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.stat().st_mtime >= since:
                    changed.append(str(path.relative_to(drive_c)))
            except OSError:
                pass
    copied = []
    dest = run_dir / "app_logs"
    for pattern in eff["app_def"].get("harvest", []):
        for path in drive_c.glob(pattern):
            if path.is_file() and path.stat().st_mtime >= since:
                dest.mkdir(exist_ok=True)
                shutil.copy2(path, dest / path.name)
                copied.append(str(path.relative_to(drive_c)))
    return sorted(changed), sorted(set(copied))


# ------------------------------------------------------------- bluetooth ---

RFCOMM_DEV = re.compile(r"^/dev/rfcomm(\d+)$")


def rfcomm_targets(eff: dict) -> list[tuple[str, int]]:
    """(device, index) for every serial mapping that points at an rfcomm node."""
    found = []
    for device in eff["serial"].values():
        match = RFCOMM_DEV.match(device)
        if match:
            found.append((device, int(match.group(1))))
    return found


def sdp_serial_channel(mac: str) -> int | None:
    """Ask the device (must be on) which RFCOMM channel its Serial Port service uses."""
    out = capture(["sdptool", "browse", mac], timeout=20)
    channel, in_serial = None, False
    for line in out.splitlines():
        if line.startswith("Service Name:"):
            in_serial = "serial" in line.lower() or "spp" in line.lower()
        match = re.search(r"Channel:\s*(\d+)", line)
        if match and (in_serial or channel is None):
            channel = int(match.group(1))
            if in_serial:
                return channel
    return channel


def run_root(cmd: list[str], log) -> int:
    """sudo without a prompt first; fall back to an interactive sudo prompt on a terminal."""
    if sh(["sudo", "-n", *cmd], log, timeout=30) == 0:
        return 0
    if not sys.stdin.isatty():
        log.write("!! sudo needs a password and there is no terminal (see README: sudoers rule)\n")
        return 1
    print(f"sudo {' '.join(cmd)}", file=sys.stderr)
    log.write(f"$ sudo {' '.join(cmd)}  (interactive)\n")
    return subprocess.run(["sudo", *cmd]).returncode


def ensure_rfcomm(eff: dict, log) -> list[str]:
    """Bind /dev/rfcommN to the handset if a serial mapping needs it and it is missing."""
    bt = eff.get("bluetooth", {})
    warnings = []
    for device, index in rfcomm_targets(eff):
        if os.path.exists(device):
            log.write(f"bluetooth {device} already bound: {capture(['rfcomm', 'show', str(index)])}\n")
            continue
        if not bt.get("autobind", True) or not bt.get("mac"):
            warnings.append(f"{device} missing and [bluetooth] autobind/mac not configured")
            continue
        channel = bt.get("channel", "auto")
        if channel == "auto":
            channel = sdp_serial_channel(bt["mac"])
            log.write(f"bluetooth sdp channel lookup -> {channel}\n")
            if channel is None:
                channel = bt.get("fallback_channel", 1)
                warnings.append(f"SDP lookup failed (handset off/out of range?); using channel {channel}")
        if run_root(["rfcomm", "bind", str(index), bt["mac"], str(channel)], log) != 0:
            warnings.append(f"rfcomm bind {index} {bt['mac']} {channel} failed")
            continue
        for _ in range(30):  # udev needs a moment to create the node and set group perms
            if os.access(device, os.R_OK | os.W_OK):
                break
            time.sleep(0.1)
        log.write(f"bluetooth bound {device} -> {bt['mac']} ch {channel}\n")
    return warnings


def cmd_bt(args) -> int:
    eff = effective_config(args.app, args.profile)
    targets = rfcomm_targets(eff)
    if not targets:
        print("profile maps no /dev/rfcommN serial port")
        return 1
    status = 0
    for device, index in targets:
        if args.action == "bind":
            warnings = ensure_rfcomm(eff, sys.stdout)
            for warning in warnings:
                print(f"WARNING: {warning}")
            status |= bool(warnings)
        elif args.action == "release":
            status |= run_root(["rfcomm", "release", str(index)], sys.stdout) != 0
        elif args.action == "channel":
            print(sdp_serial_channel(eff["bluetooth"].get("mac", "")))
        ok = os.access(device, os.R_OK | os.W_OK)
        print(f"{device}: {'bound, rw' if ok else 'present, no rw' if os.path.exists(device) else 'not bound'}"
              f" | {capture(['rfcomm', 'show', str(index)])}")
    return int(status)


# --------------------------------------------------------------- capture ---

def start_capture(eff: dict, run_dir: Path) -> tuple[subprocess.Popen | None, list[str]]:
    cap = eff["capture"]
    tool = cap.get("tool", "dumpcap")
    log = (run_dir / "capture.stderr.log").open("w")
    if tool == "btmon":
        cmd = ["sudo", "-n", "btmon", "-w", str(run_dir / "capture.btsnoop")]
    else:
        cmd = ["dumpcap", "-q", "-w", str(run_dir / "capture.pcapng")]
        for iface in cap.get("interfaces", ["bluetooth0"]):
            cmd += ["-i", iface]
    proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
    time.sleep(cap.get("settle_s", 1.0))
    if proc.poll() is not None:
        return None, [f"capture ({tool}) failed to start, see capture.stderr.log"]
    print(f"capture: {' '.join(cmd)}", file=sys.stderr)
    return proc, []


def stop_capture(proc: subprocess.Popen) -> None:
    # dumpcap/btmon flush on SIGINT; btmon runs under sudo, so signal the process group.
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, PermissionError, ProcessLookupError):
        subprocess.run(["sudo", "-n", "kill", "-INT", "--", f"-{proc.pid}"], capture_output=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def decode_capture(eff: dict, run_dir: Path) -> None:
    """Write capture.events.txt (BT link events), capture.serial.txt (AP frames) for the summary."""
    btsnoop, pcap = run_dir / "capture.btsnoop", run_dir / "capture.pcapng"
    if btsnoop.exists():
        (run_dir / "capture.btmon.txt").write_text(capture(["btmon", "-r", str(btsnoop)], timeout=120))
        if not pcap.exists():  # tshark reads btsnoop; convert so replay/mock can use it
            capture(["tshark", "-r", str(btsnoop), "-F", "pcapng", "-w", str(pcap)], timeout=120)
    if not pcap.exists():
        return
    def frames(display_filter: str, *extra: str) -> str:
        columns = ('gui.column.format:"No.","%m","Time","%t","Source","%s","Destination","%d",'
                   '"Protocol","%p","Info","%i"')
        out = capture(["tshark", "-o", columns, "-r", str(pcap), "-Y", display_filter, *extra], timeout=120)
        # keep packet lines only (personal Lua dissectors may print reports to stdout)
        return "\n".join(r for r in out.splitlines() if re.match(r"\s*\d+(\t|\s+\d)", r)) + "\n"

    (run_dir / "capture.events.txt").write_text(
        frames(eff["capture"].get("event_filter", CAPTURE_EVENT_FILTER)))
    (run_dir / "capture.rfcomm_times.txt").write_text(frames(
        "btrfcomm && btrfcomm.len > 0", "-T", "fields", "-e", "frame.number",
        "-e", "frame.time_relative", "-e", "hci_h4.direction"))
    mock = eff.get("mock") or {}
    repo = (ROOT / mock.get("cwd", "..")).resolve()
    python = repo / ".venv" / "bin" / "python"
    (run_dir / "capture.serial.txt").write_text(capture(
        [str(python if python.exists() else "python3"), "-m", "inspiration.replay", str(pcap), "--inspect"],
        timeout=120, cwd=repo) + "\n")


CAPTURE_EVENT_FILTER = ("bthci_cmd || (bthci_evt && !(bthci_evt.code == 0x13)) || "
                        "(btrfcomm && !(btrfcomm.frame_type == 0xef)) || "
                        "btl2cap.cmd_code || btsdp")


def summarise_capture(run_dir: Path) -> list[str]:
    pcap = run_dir / "capture.pcapng"
    if not pcap.exists():
        return []
    lines = ["", f"## packet capture: {pcap.name} {pcap.stat().st_size} bytes "
                 f"(open in Wireshark; serial frames in capture.serial.txt)"]
    events = (run_dir / "capture.events.txt")
    if events.exists():
        rows = [r for r in events.read_text().splitlines() if r.strip()]
        keep = [r for r in rows if re.search(r"Disconnect|Connect|SABM|DISC|UA|DM|Fail|Error|Timeout|"
                                             r"Role|Mode Change|Sniff|Encrypt|Auth|PIN|Link Key", r, re.I)]
        lines += [f"link events: {len(rows)} control frames; connect/disconnect/mode (first 40):"]
        lines += keep[:40] if keep else [r[:160] for r in rows[:20]]
    times = run_dir / "capture.rfcomm_times.txt"
    if times.exists():
        stamps = []
        for row in times.read_text().split("\n"):
            parts = row.split("\t")
            try:
                stamps.append((float(parts[1]), parts[2] if len(parts) > 2 else "?"))
            except (ValueError, IndexError):
                pass
        gaps = sorted(((b[0] - a[0], a[0], a[1], b[1]) for a, b in zip(stamps, stamps[1:])), reverse=True)
        lines += [f"rfcomm data frames: {len(stamps)}; largest gaps (s @t, dir before->after; 0=host->dev):"]
        lines += [f"  {g:7.3f} @{t:8.3f} {d1}->{d2}" for g, t, d1, d2 in gaps[:10]]
    serial = run_dir / "capture.serial.txt"
    if serial.exists():
        rows = serial.read_text().splitlines()
        frames = [r for r in rows if ": TX " in r or ": RX " in r]
        if frames:
            counts: dict[str, int] = {}
            for r in frames:
                key = " ".join(r.split()[1:4])
                counts[key] = counts.get(key, 0) + 1
            lines += [f"AP serial frames: {len(frames)}; by direction/device/command:"]
            lines += [f"  {n:5d} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]]
        else:
            lines += ["AP serial decode: " + (rows[-1] if rows else "nothing")]
    return lines


# ------------------------------------------------------------------ mock ---

def start_mock(eff: dict, run_dir: Path) -> subprocess.Popen:
    """Start the mock handset on a pty behind eff['mock']['link']; wait for READY."""
    mock = eff["mock"]
    cwd = (ROOT / mock.get("cwd", "..")).resolve()
    cmd = [str(c) for c in mock.get("command", ["python3", "-m", "inspiration.mockdevice"])]
    if os.sep in cmd[0]:
        cmd[0] = str((cwd / cmd[0]).resolve())
    cmd += ["--link", mock["link"], "--log", str(run_dir / "mock.log"),
            "--stats", str(run_dir / "mock.stats.json")]
    if mock.get("capture"):
        cmd += ["--capture", str((cwd / mock["capture"]).resolve())]
    cmd += [str(a) for a in mock.get("args", [])]
    with (run_dir / "mock.stderr.log").open("w") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=err, text=True,
                                start_new_session=True)
    ready, _, _ = select.select([proc.stdout], [], [], 30)
    line = proc.stdout.readline() if ready else ""
    if not line.startswith("READY"):
        proc.kill()
        sys.exit(f"mock device failed to start: {' '.join(cmd)}\nsee {run_dir / 'mock.stderr.log'}")
    print(f"mock: {line.strip()}", file=sys.stderr)
    return proc


def stop_mock(proc: subprocess.Popen, run_dir: Path) -> dict:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    stats = run_dir / "mock.stats.json"
    return json.loads(stats.read_text()) if stats.exists() else {"error": "no stats written"}


# ------------------------------------------------------------------- run ---

def summarise_run(run_dir: Path) -> str:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    eff = manifest["config"]
    lines = [
        f"# {manifest['run_id']}",
        f"app={eff['app']} profile={eff['profile']} chain={'>'.join(eff['chain'])} hash={manifest['config_hash']}",
        f"exit={manifest.get('exit_code')} duration={manifest.get('duration_s')}s "
        f"timed_out={manifest.get('timed_out')} outcome={manifest.get('outcome') or '-'}",
        f"wine={manifest['preflight']['wine_version']} prefix={eff['wine'].get('prefix')}",
        f"WINEDEBUG={eff['wine'].get('debug', '')}",
        f"WINEDLLOVERRIDES={manifest.get('dll_overrides_env') or '-'}",
        f"serial={json.dumps(manifest['preflight']['serial'])}",
        f"mock={'on args=' + json.dumps(eff['mock'].get('args', [])) if eff.get('mock') else 'off'} "
        f"capture={eff['capture'].get('tool', 'dumpcap') + ':' + ','.join(eff['capture'].get('interfaces', [])) if eff.get('capture') else 'off'}",
        f"patches={json.dumps(eff['patches'])} env={json.dumps(eff['env'])}",
    ]
    for note in manifest.get("notes", []):
        lines.append(f"note[{note['time']}]: {note['text']}")
    if manifest.get("warnings"):
        lines += ["", "## WARNINGS"] + manifest["warnings"]
    reg_diff = run_dir / "registry.diff"
    if reg_diff.exists() and reg_diff.stat().st_size:
        diff = reg_diff.read_text().splitlines()
        lines += ["", f"## registry changed by run ({len(diff)} diff lines, first 30)"] + diff[:30]
    lines += ["", f"## files changed in drive_c: {len(manifest.get('changed_files', []))} "
                  f"(see changed_files.txt); harvested: {manifest.get('harvested') or 'none'}"]
    wine_log = next((p for p in (run_dir / "wine.log", run_dir / "wine.log.gz") if p.exists()), None)
    if wine_log:
        body, stats = logsummary.summarise_wine_log(wine_log)
        manifest["stats"] = stats
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        lines += [""] + body
    lines += logsummary.summarise_text(run_dir / "app.stdout.log", "app stdout")
    lines += summarise_capture(run_dir)
    mock_log = run_dir / "mock.log"
    if mock_log.exists():
        events = mock_log.read_text(errors="replace").splitlines()
        odd = [e for e in events if " UNKNOWN " in e or " STALL " in e]
        reqs = [e for e in events if " REQ " in e]
        lines += ["", f"## mock device: {json.dumps(manifest.get('mock', {}))}",
                  f"first request: {reqs[0] if reqs else 'NONE - app never sent a valid frame'}",
                  f"last request:  {reqs[-1] if reqs else '-'}",
                  f"unknown/stalled ({len(odd)}, first 20):"] + odd[:20]
    for app_log in sorted((run_dir / "app_logs").glob("*")):
        if logsummary.is_text(app_log):
            lines += logsummary.summarise_text(app_log, f"app log {app_log.name}", 30)
        else:
            lines += ["", f"## app log {app_log.name}: binary/encrypted, {app_log.stat().st_size} bytes"]
    text = "\n".join(lines) + "\n"
    (run_dir / "summary.txt").write_text(text)
    return text


def cmd_run(args) -> int:
    eff = effective_config(args.app, args.profile, args)
    digest = config_hash(eff)
    if args.dry_run:
        print(json.dumps({"config_hash": digest, **eff}, indent=2))
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}_{eff['profile']}_{digest}"
    run_dir = LOG_DIR / args.app / run_id
    run_dir.mkdir(parents=True)
    latest = LOG_DIR / args.app / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_id)
    (run_dir / "profile").mkdir()
    for name in eff["chain"]:
        shutil.copy2(PROFILE_DIR / f"{name}.toml", run_dir / "profile")
    for reg in eff["patches"]["reg"]:
        if (REG_DIR / reg).exists():
            shutil.copy2(REG_DIR / reg, run_dir / "profile")

    mock_proc = start_mock(eff, run_dir) if eff.get("mock") else None
    bt_warnings = []
    if not args.no_bind and rfcomm_targets(eff):
        with (run_dir / "bluetooth.log").open("w") as log:
            bt_warnings = ensure_rfcomm(eff, log)
    info, warnings = preflight(eff)
    warnings = bt_warnings + warnings
    cap_proc = None
    if eff.get("capture"):
        cap_proc, cap_warnings = start_capture(eff, run_dir)
        warnings += cap_warnings
    prefix = prefix_of(eff)
    with (run_dir / "apply.log").open("w") as log:
        if args.fresh:
            sh([wineserver_bin(eff), "-k"], log, base_env(eff), timeout=30)
        if not args.no_apply:
            warnings += apply_patches(eff, log)
        sh([wineserver_bin(eff), "-w"], log, base_env(eff), timeout=60)
    reg_before = reg_snapshot(prefix, eff["registry_keys"])
    (run_dir / "registry.before.reg").write_text(reg_before)

    dll_env = ";".join(f"{k}={v}" for k, v in eff["dll_overrides"].items())
    env = base_env(eff, eff["wine"].get("debug", ""))
    env.update(eff["env"])
    if dll_env:
        env["WINEDLLOVERRIDES"] = dll_env
    exe = eff["app_def"]["exe"]
    cmd = [wine_bin(eff), exe] + args.app_args
    cwd = win_to_unix(prefix, exe).parent

    manifest = {
        "run_id": run_id, "app": args.app, "config_hash": digest, "config": eff,
        "command": cmd, "cwd": str(cwd), "dll_overrides_env": dll_env,
        "env_overrides": {k: env[k] for k in ("WINEPREFIX", "WINEDEBUG", "WINEDLLOVERRIDES", *eff["env"]) if k in env},
        "preflight": info, "warnings": warnings, "notes": [],
        "shim_git": capture(["git", "-C", str(ROOT), "describe", "--always", "--dirty"]),
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    if args.note:
        manifest["notes"].append({"time": manifest["started"], "text": args.note})
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"run {args.app}/{run_id} -> {run_dir}", file=sys.stderr)
    start = time.time()
    timed_out = False
    with (run_dir / "app.stdout.log").open("w") as out, (run_dir / "wine.log").open("w") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True)
        try:
            proc.wait(timeout=args.timeout or None)
        except KeyboardInterrupt:
            manifest["warnings"].append("interrupted by user/signal")
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait()
            subprocess.run([wineserver_bin(eff), "-k"], env=base_env(eff), timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            subprocess.run([wineserver_bin(eff), "-k"], env=base_env(eff), timeout=30)
    duration = round(time.time() - start, 1)
    if mock_proc:
        manifest["mock"] = stop_mock(mock_proc, run_dir)
    if cap_proc:
        stop_capture(cap_proc)
        decode_capture(eff, run_dir)
    try:
        subprocess.run([wineserver_bin(eff), "-w"], env=base_env(eff), timeout=60)
    except subprocess.TimeoutExpired:
        manifest["warnings"].append("wineserver still busy; registry.after may be stale")

    reg_after = reg_snapshot(prefix, eff["registry_keys"])
    (run_dir / "registry.after.reg").write_text(reg_after)
    diff = difflib.unified_diff(reg_before.splitlines(), reg_after.splitlines(), "before", "after", lineterm="", n=0)
    (run_dir / "registry.diff").write_text("".join(f"{line}\n" for line in diff))
    changed, copied = harvest(eff, start - 1, run_dir)
    (run_dir / "changed_files.txt").write_text("\n".join(changed) + "\n")
    if mock_proc and eff["mock"].get("real_device"):  # don't leave the prefix on a dead pty
        with (run_dir / "apply.log").open("a") as log:
            set_serial(eff, eff["mock"]["port"], eff["mock"]["real_device"], log)

    manifest.update(exit_code=proc.returncode, duration_s=duration, timed_out=timed_out,
                    ended=datetime.now().isoformat(timespec="seconds"),
                    changed_files=changed[:500], harvested=copied)
    if not args.no_prompt and sys.stdin.isatty():
        outcome = input(f"Outcome {'/'.join(OUTCOMES)} [skip]: ").strip().lower() or "skip"
        manifest["outcome"] = outcome if outcome in OUTCOMES else "skip"
        text = input("Note (what you tried / saw) []: ").strip()
        if text:
            manifest["notes"].append({"time": manifest["ended"], "text": text})
    elif args.outcome:
        manifest["outcome"] = args.outcome
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    summary = summarise_run(run_dir)
    wine_log = run_dir / "wine.log"
    if wine_log.stat().st_size > GZIP_ABOVE:
        with wine_log.open("rb") as src, gzip.open(f"{wine_log}.gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        wine_log.unlink()
    rebuild_index()
    print(summary if args.print_summary else "\n".join(summary.splitlines()[:12]))
    print(f"\nfull summary: {run_dir / 'summary.txt'}")
    return 0


# --------------------------------------------------------------- queries ---

def all_runs() -> list[Path]:
    runs = (p.parent for p in LOG_DIR.glob("*/*/manifest.json"))
    return sorted((r for r in runs if not r.is_symlink()), key=lambda p: p.name)


def find_run(ref: str) -> Path:
    runs = all_runs()
    if not runs:
        sys.exit("no runs yet")
    app, _, rest = ref.partition(":")
    if ref == "latest":
        return runs[-1]
    if app in ("divesight", "communicator") and rest in ("", "latest"):
        scoped = [r for r in runs if r.parent.name == app]
        if scoped:
            return scoped[-1]
        sys.exit(f"no runs for {app}")
    if Path(ref).is_dir():
        return Path(ref).resolve()
    hits = [r for r in runs if ref in r.name]
    if len(hits) != 1:
        sys.exit(f"'{ref}' matches {len(hits)} runs" + "".join(f"\n  {h.parent.name}/{h.name}" for h in hits[:10]))
    return hits[0]


def rebuild_index() -> None:
    header = "run_id\tapp\tprofile\thash\twine\texit\tdur_s\terr\tcomm\tcrash\toutcome\tnotes"
    rows = [header]
    for run in all_runs():
        m = json.loads((run / "manifest.json").read_text())
        s = m.get("stats", {})
        notes = " | ".join(n["text"] for n in m.get("notes", [])).replace("\t", " ")
        rows.append("\t".join(str(x) for x in (
            m["run_id"], m["app"], m["config"]["profile"], m["config_hash"],
            m["preflight"]["wine_version"], m.get("exit_code"), m.get("duration_s"),
            s.get("err", ""), s.get("comm_lines", ""), s.get("crash_markers", ""),
            m.get("outcome") or "-", notes)))
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / "index.tsv").write_text("\n".join(rows) + "\n")


def cmd_note(args) -> int:
    run = find_run(args.run)
    path = run / "manifest.json"
    m = json.loads(path.read_text())
    if args.outcome:
        m["outcome"] = args.outcome
    if args.text:
        m["notes"].append({"time": datetime.now().isoformat(timespec="seconds"), "text": " ".join(args.text)})
    path.write_text(json.dumps(m, indent=2))
    summarise_run(run)
    rebuild_index()
    print(f"updated {run.parent.name}/{run.name}")
    return 0


def cmd_show(args) -> int:
    run = find_run(args.run)
    summary = run / "summary.txt"
    text = summary.read_text() if summary.exists() else summarise_run(run)
    lines = text.splitlines()
    print("\n".join(lines if args.full else lines[:args.lines]))
    if not args.full and len(lines) > args.lines:
        print(f"... ({len(lines) - args.lines} more lines: shim show {args.run} --full)")
    return 0


def cmd_summarize(args) -> int:
    print(summarise_run(find_run(args.run)))
    rebuild_index()
    return 0


def cmd_list(args) -> int:
    rebuild_index()
    rows = (LOG_DIR / "index.tsv").read_text().splitlines()
    body = [r for r in rows[1:] if not args.app or r.split("\t")[1] == args.app]
    for row in [rows[0]] + body[-args.n:]:
        print(row)
    return 0


def cmd_diff(args) -> int:
    a, b = find_run(args.a), find_run(args.b)
    ma, mb = (json.loads((r / "manifest.json").read_text()) for r in (a, b))
    for title, x, y in (("config", ma["config"], mb["config"]), ("stats", ma.get("stats", {}), mb.get("stats", {}))):
        print(f"## {title}")
        diff = list(difflib.unified_diff(json.dumps(x, indent=1, sort_keys=True).splitlines(),
                                         json.dumps(y, indent=1, sort_keys=True).splitlines(),
                                         a.name, b.name, lineterm="", n=0))
        print("\n".join(diff[2:]) or "(identical)")
    return 0


def cmd_profiles(_args) -> int:
    for path in sorted(PROFILE_DIR.glob("*.toml")):
        data = load_toml(path)
        parent = f" (extends {data['extends']})" if "extends" in data else ""
        print(f"{path.stem:<16}{parent:<22} {data.get('description', '')}")
    return 0


def cmd_config(args) -> int:
    eff = effective_config(args.app, args.profile, args)
    print(json.dumps({"config_hash": config_hash(eff), **eff}, indent=2))
    return 0


def cmd_apply(args) -> int:
    eff = effective_config(args.app, args.profile, args)
    warnings = apply_patches(eff, sys.stdout)
    for warning in warnings:
        print(f"WARNING: {warning}")
    return 1 if warnings else 0


def cmd_doctor(args) -> int:
    eff = effective_config(args.app, args.profile)
    info, warnings = preflight(eff)
    print(json.dumps(info, indent=2))
    for warning in warnings:
        print(f"WARNING: {warning}")
    print("OK" if not warnings else f"{len(warnings)} problem(s)")
    return 1 if warnings else 0


# ------------------------------------------------------------------- cli ---

def add_overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument("--winedebug", help="override WINEDEBUG for this run")
    p.add_argument("--env", action="append", metavar="K=V", help="extra env var (repeatable)")
    p.add_argument("--dll", action="append", metavar="NAME=MODE", help="DLL override, e.g. mfc80=n,b")
    p.add_argument("--serial", action="append", metavar="comN=/dev/X", help="serial mapping")
    p.add_argument("--mock", action="store_true", help="use the mock handset instead of the real device")
    p.add_argument("--mock-arg", action="append", metavar="ARG",
                   help="extra mockdevice arg, e.g. --mock-arg=--chunk=17 (implies --mock)")
    p.add_argument("--capture", action="store_true", help="record Bluetooth packets during the run")
    p.add_argument("--capture-iface", action="append", metavar="IFACE",
                   help="dumpcap interface (repeatable; implies --capture), e.g. bluetooth-monitor")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="shim", description=__doc__.splitlines()[0],
                                     epilog="run: arguments after a bare -- are passed to the exe")
    sub = parser.add_subparsers(dest="cmd", required=True)
    apps_ = ("divesight", "communicator")

    p = sub.add_parser("run", help="apply profile, launch app, log everything, summarise")
    p.add_argument("app", choices=apps_)
    p.add_argument("profile", nargs="?", default="debug")
    add_overrides(p)
    p.add_argument("--note", help="what this run is testing")
    p.add_argument("--outcome", choices=OUTCOMES, help="record outcome without prompting")
    p.add_argument("--timeout", type=int, default=0, help="kill app after N s (headless smoke tests)")
    p.add_argument("--fresh", action="store_true", help="wineserver -k before starting")
    p.add_argument("--no-apply", action="store_true", help="skip serial/reg/winetricks patching")
    p.add_argument("--no-bind", action="store_true", help="don't auto-bind /dev/rfcommN")
    p.add_argument("--no-prompt", action="store_true", help="never ask for outcome")
    p.add_argument("--print-summary", action="store_true", help="print the whole summary")
    p.add_argument("--dry-run", action="store_true", help="print effective config + hash only")
    p.set_defaults(func=cmd_run)

    for name, func, helptext in (("config", cmd_config, "print effective config + hash"),
                                 ("apply", cmd_apply, "apply profile patches without running")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("app", choices=apps_)
        p.add_argument("profile", nargs="?", default="debug")
        add_overrides(p)
        p.set_defaults(func=func)

    p = sub.add_parser("doctor", help="check prefix, exe, serial device")
    p.add_argument("app", choices=apps_)
    p.add_argument("profile", nargs="?", default="baseline")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("bt", help="bind/release/inspect the Bluetooth serial port (rfcomm)")
    p.add_argument("action", choices=("status", "bind", "release", "channel"), nargs="?", default="status")
    p.add_argument("--app", default="divesight", choices=apps_)
    p.add_argument("--profile", default="baseline")
    p.set_defaults(func=cmd_bt)

    p = sub.add_parser("show", help="print a run summary (default: latest)")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("-n", "--lines", type=int, default=60)
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("summarize", help="re-generate summary.txt for a run")
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("note", help="record outcome / note on a run")
    p.add_argument("run")
    p.add_argument("--outcome", choices=OUTCOMES)
    p.add_argument("text", nargs="*")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("list", help="tail of logs/index.tsv")
    p.add_argument("--app", choices=apps_)
    p.add_argument("-n", type=int, default=15)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("diff", help="diff config + stats of two runs")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_diff)

    sub.add_parser("profiles", help="list profiles").set_defaults(func=cmd_profiles)

    argv = list(sys.argv[1:] if argv is None else argv)
    app_args = argv[argv.index("--") + 1:] if "--" in argv else []
    argv = argv[:argv.index("--")] if "--" in argv else argv
    args = parser.parse_args(argv)
    args.app_args = app_args
    signal.signal(signal.SIGTERM, _sigterm)
    return args.func(args)


def _sigterm(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())

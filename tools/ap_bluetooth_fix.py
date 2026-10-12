#!/usr/bin/env python3
"""Repair AP Bluetooth system exclusion and Wine COM mapping. Python 3.8+, stdlib only.

Current local version: save the script, then run check or install WITHOUT sudo.
Use --system-only for the original sudo installer, --com-only for Wine, or
check --read-only for diagnostics without changes. Pinned downloads below may
contain an older version.

Get & check
```
curl -sSL https://gist.githubusercontent.com/Tirpitz93/353ce38e20747a28ad7b18d3055344f4/raw | python3 - --port rfcomm0 check

```
```
curl -sSL https://gist.githubusercontent.com/Tirpitz93/353ce38e20747a28ad7b18d3055344f4/raw | python3 - --port rfcomm0 install --dry-run
```
Then
```
curl -sSL https://gist.githubusercontent.com/Tirpitz93/353ce38e20747a28ad7b18d3055344f4/raw | sudo python3 - --port rfcomm0 install
```

OR
```
wget https://gist.githubusercontent.com/Tirpitz93/353ce38e20747a28ad7b18d3055344f4/raw -O ap_bluetooth_fix.py
python3 ap_bluetooth_fix.py --port rfcomm0 check
python3 ap_bluetooth_fix.py --port rfcomm0 install --dry-run
sudo python3 ap_bluetooth_fix.py --port rfcomm0 install
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

RULES_DIR = Path("/etc/udev/rules.d")
SYS_TTY_DIR = Path("/sys/class/tty")
VERSION = "3"
MINIMUM_WINE = (11, 16) # Includes required binaries


class FixError(RuntimeError):
    """An actionable setup or verification failure."""


def port_name(value: str) -> str:
    if not re.fullmatch(r"rfcomm[0-9]+", value):
        raise argparse.ArgumentTypeError("Use a port name such as rfcomm0, not a path or wildcard.")
    return value


def com_port_name(value: str) -> str:
    if not re.fullmatch(r"com[1-9][0-9]*", value, re.IGNORECASE):
        raise argparse.ArgumentTypeError("Use a COM port such as COM3, not a path.")
    return value.lower()


def rule_text(port: str) -> str:
    return (
        "# Managed by ap_bluetooth_fix.py; remove with its remove command.\n"
        "# Reserve this port for AP Bluetooth serial traffic, not modem detection.\n"
        f'ACTION=="add|change", SUBSYSTEM=="tty", KERNEL=="{port}", '
        'ENV{ID_MM_PORT_IGNORE}="1"\n'
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Check/install repair by default. Run as your normal user; only the udev step "
               "uses sudo. No service is stopped and no packages are installed.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("action", choices=("install", "remove", "check"))
    parser.add_argument("--port", type=port_name, default="rfcomm0")
    parser.add_argument("--dry-run", action="store_true", help="Preview selected repairs without changes.")
    parser.add_argument("--json", action="store_true", help="Machine-readable check report.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--system-only", action="store_true", help="Only manage the udev exclusion.")
    scope.add_argument("--com-only", action="store_true", help="Only manage Wine COM mappings.")
    parser.add_argument("--read-only", action="store_true", help="Inspect without repairs; check only.")
    parser.add_argument("--wine-prefix", type=Path,
                        default=Path(os.environ.get("WINEPREFIX", "~/.wine")),
                        help="Wine prefix (default WINEPREFIX or ~/.wine).")
    parser.add_argument("--wine-binary", default="wine", help="Wine executable to check (default wine).")
    parser.add_argument("--com-port", type=com_port_name, default="com3",
                        help="Windows serial port to inspect (default COM3).")
    args = parser.parse_args(argv)
    if args.json and args.action != "check":
        parser.error("--json is only available for check.")
    if args.read_only and args.action != "check":
        parser.error("--read-only is only available for check.")
    if args.action == "remove" and args.com_only:
        parser.error("remove only removes the system rule; COM mappings are preserved.")
    if args.dry_run and args.system_only and args.action != "check":
        print(f"Would {args.action} {rule_path(args.port)}")
        if args.action != "remove":
            print(rule_text(args.port), end="")
        return 0
    try:
        if sys.platform != "linux":
            raise FixError("This fix requires Linux with udev.")
        if args.action == "remove":
            if args.dry_run:
                print(f"Would remove {rule_path(args.port)}")
                return 0
            remove(args.port)
            return 0
        report = configure(args)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print_report(report)
        return 0 if args.action == "install" or report["ready"] else 1
    except (FixError, OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def configure(args) -> dict:
    changes = []
    prefix = args.wine_prefix.expanduser().absolute()
    repairing = not args.read_only and not args.dry_run
    if repairing and not args.system_only and os.geteuid() == 0:
        raise FixError("Do not repair Wine as root. Run without sudo, or use --system-only.")
    if not args.system_only and repairing:
        changes.extend(repair_com(prefix, args.com_port, args.wine_binary, args.port))
    if not args.com_only:
        validate_owned_rule(args.port)
        if not rule_path(args.port).exists() and repairing:
            changes.append(repair_system(args.port))
    report = check(args.port) if not args.com_only else {"port": args.port}
    report["scope"] = "system" if args.system_only else "com" if args.com_only else "all"
    report["changes"] = changes
    report["dry_run"] = args.dry_run
    report["ready"] = report.get("status") == "protected" if not args.com_only else True
    if not args.system_only:
        report["wine"] = check_wine(prefix, args.com_port, args.wine_binary, args.port)
        report["ready"] = report["ready"] and report["wine"]["ready"]
    if args.dry_run:
        report["planned_changes"] = planned_changes(args, prefix)
    return report


def planned_changes(args, prefix: Path) -> list:
    planned = []
    if not args.com_only and not rule_path(args.port).exists():
        planned.append(f"Install {rule_path(args.port)} and reload udev rules (reboot required).")
    if not args.system_only:
        expected = f"/dev/{args.port}"
        mapping = prefix / "dosdevices" / args.com_port
        if not mapping.is_symlink() or os.readlink(mapping) != expected:
            planned.append(f"Set symlink {mapping} -> {expected}")
        if saved_registry_port(prefix, args.com_port) != expected:
            planned.append(f"Use Wine regedit to set {args.com_port.upper()} = {expected}")
    return planned


def repair_system(port: str) -> str:
    if os.geteuid() == 0:
        with redirect_stdout(StringIO()):
            install(port)
    else:
        script = Path(sys.argv[0]).absolute()
        if not script.is_file():
            raise FixError("Save this script to a file before automatic sudo repair, or install "
                           "the rule separately with sudo and --system-only.")
        result = subprocess.run(
            ["sudo", sys.executable, str(script), "install", "--system-only", "--port", port],
            stdout=subprocess.PIPE, text=True, timeout=120,
        )
        if result.returncode not in (0, 1):
            raise FixError(f"System repair failed (exit {result.returncode}): {result.stdout.strip()}")
        if not rule_path(port).is_file() or rule_path(port).read_text() != rule_text(port):
            raise FixError("System repair did not create the expected udev rule.")
    return "Installed system udev exclusion; reboot before testing."


def repair_com(prefix: Path, com_port: str, binary: str, port: str) -> list:
    if not (prefix / "dosdevices").is_dir():
        raise FixError(f"Existing Wine prefix required: {prefix}. Install DiveSight there first.")
    expected = f"/dev/{port}"
    mapping = prefix / "dosdevices" / com_port
    change_link = not mapping.is_symlink() or os.readlink(mapping) != expected
    change_registry = saved_registry_port(prefix, com_port) != expected
    if not change_link and not change_registry:
        return []
    if change_link and mapping.exists() and not mapping.is_symlink():
        raise FixError(f"Refusing to replace a non-symlink COM mapping: {mapping}")
    changes = []
    if change_registry:
        import_serial_registry(prefix, com_port, binary, expected)
        if saved_registry_port(prefix, com_port) != expected:
            raise FixError("Wine regedit returned without saving the requested COM mapping. "
                           "Close Wine applications and retry.")
        changes.append(f"Set Wine registry {com_port.upper()} = {expected}")
    if change_link:
        if mapping.is_symlink():
            mapping.unlink()
        mapping.symlink_to(expected)
        changes.append(f"Set COM symlink {mapping} -> {expected}")
    return changes


def import_serial_registry(prefix: Path, com_port: str, binary: str, device: str) -> None:
    content = ('Windows Registry Editor Version 5.00\n\n'
               '[HKEY_LOCAL_MACHINE\\Software\\Wine\\Ports]\n'
               f'"{com_port.upper()}"="{device}"\n')
    env = dict(os.environ, WINEPREFIX=str(prefix), WINEDEBUG="-all")
    with tempfile.TemporaryDirectory(prefix="ap-com-") as directory:
        reg_file = Path(directory) / "serial.reg"
        reg_file.write_text(content, encoding="utf-8")
        result = subprocess.run([binary, "regedit", "/S", str(reg_file)],
                                env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise FixError(f"Wine regedit failed: {detail}")


def install(port: str) -> None:
    require_root()
    path = rule_path(port)
    content = rule_text(port)
    validate_owned_rule(port)
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=RULES_DIR,
                                             prefix=".ap-bluetooth-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            temporary.chmod(0o644)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    print(f"Rule saved: {path}")
    reload_rules("Rule saved")
    print("Reboot before testing: existing ports and ModemManager probes are not changed.")


def remove(port: str) -> None:
    require_root()
    path = rule_path(port)
    validate_owned_rule(port)
    if path.exists():
        path.unlink()
        print(f"Rule removed: {path}")
    else:
        print(f"Rule already absent: {path}")
    reload_rules("Rule absent")
    print("Reboot to clear the old ignore property from existing ports.")


def rule_path(port: str) -> Path:
    return RULES_DIR / f"78-ap-bluetooth-{port}.rules"


def validate_owned_rule(port: str) -> None:
    path = rule_path(port)
    if path.is_symlink() or (path.exists() and path.read_text() != rule_text(port)):
        raise FixError(f"Refusing to modify an unrecognised rule: {path}")


def require_root() -> None:
    if os.geteuid() != 0:
        raise FixError("Changes need administrator permission. Run with sudo.")
    if shutil.which("udevadm") is None:
        raise FixError("udevadm is missing. Install your distribution's udev package first.")


def reload_rules(state: str) -> None:
    result = subprocess.run(["udevadm", "control", "--reload-rules"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise FixError(f"{state}, but reload failed: {detail}. "
                       "Retry the command after fixing udev, or reboot.")


def check_wine(prefix: Path, com_port: str, binary: str, port: str) -> dict:
    result = {
        "minimum_version": ".".join(map(str, MINIMUM_WINE)),
        "version": None,
        "version_ok": False,
        "prefix": str(prefix),
        "com_port": com_port.upper(),
        "binary": binary,
    }
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        result["version"] = version.stdout.strip()
        match = re.fullmatch(r"wine-(\d+)\.(\d+)(?:[.\-\s].*)?", result["version"])
        result["version_ok"] = bool(
            version.returncode == 0 and match and
            tuple(map(int, match.groups())) >= MINIMUM_WINE
        )
        if not result["version_ok"]:
            result["version_diagnostic"] = (
                version.stderr.strip() or f"Wine {result['minimum_version']} or newer is required; "
                f"reported {result['version'] or 'no recognised version'}."
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        result["version_diagnostic"] = f"Cannot check Wine executable {binary}: {error}"
    result.update(check_serial_mapping(prefix, com_port, port))
    result["ready"] = result["version_ok"] and result["serial_status"] == "configured"
    return result


def check_serial_mapping(prefix: Path, com_port: str, port: str) -> dict:
    mapping = prefix / "dosdevices" / com_port.lower()
    expected = Path("/dev") / port
    target = str(mapping.resolve()) if mapping.is_symlink() else None
    registry_target = saved_registry_port(prefix, com_port)
    issues = []
    if not prefix.is_dir():
        issues.append("Wine prefix does not exist; select the prefix where DiveSight is installed.")
    if target != str(expected.resolve()):
        issues.append(f"{com_port.upper()} must be a symlink to {expected}; "
                      f"found {target or 'no symlink'}.")
    if registry_target is not None and registry_target != str(expected):
        issues.append(f"HKLM\\Software\\Wine\\Ports overrides {com_port.upper()} "
                      f"with {registry_target}; expected {expected}.")
    if expected.exists() and not os.access(expected, os.R_OK | os.W_OK):
        issues.append(f"The current user cannot read/write {expected}; check serial-port permissions.")
    return {
        "serial_status": "misconfigured" if issues else "configured",
        "symlink_target": target,
        "registry_target": registry_target,
        "serial_diagnostic": " ".join(issues) or
            "COM mapping matches. Set DiveSight to this COM port; no serial device was opened.",
    }


def saved_registry_port(prefix: Path, com_port: str) -> Optional[str]:
    registry = prefix / "system.reg"
    if not registry.is_file():
        return None
    in_ports = False
    with registry.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("["):
                in_ports = line.split("]", 1)[0] == r"[Software\\Wine\\Ports"
            if not in_ports:
                continue
            match = re.fullmatch(r'"([^"]+)"=(.*)', line.strip())
            if match and match[1].lower() == com_port.lower():
                try:
                    value = json.loads(match[2])
                except json.JSONDecodeError:
                    return f"unrecognised registry value: {match[2]}"
                return value if isinstance(value, str) else "non-string registry value"
    return None


def check(port: str) -> dict:
    path = rule_path(port)
    installed = path.is_file() and not path.is_symlink() and path.read_text() == rule_text(port)
    present = (SYS_TTY_DIR / port).exists()
    ignored = None
    if present:
        result = subprocess.run(["udevadm", "info", "--query=property",
                                 "--path", str(SYS_TTY_DIR / port)],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise FixError(f"Cannot inspect {port}: {result.stderr.strip() or result.stdout.strip()}")
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        ignored = properties.get("ID_MM_PORT_IGNORE")
    status = "protected" if ignored == "1" else "unprotected"
    if not present:
        status = "port-missing"
    elif installed and ignored != "1":
        status = "pending-reboot"
    report = {
        "script_version": VERSION,
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "port": port,
        "port_present": present,
        "rule_path": str(path),
        "rule_installed": installed,
        "ignore_property": ignored,
        "status": status,
    }
    report.update(modem_manager_status())
    return report


def modem_manager_status() -> dict:
    if shutil.which("systemctl") is None:
        return {"modem_manager": "unknown", "service_diagnostic": "systemctl is not installed"}
    result = subprocess.run(["systemctl", "is-active", "ModemManager.service"],
                            capture_output=True, text=True, timeout=15)
    state = result.stdout.strip()
    if state in ("active", "inactive", "failed", "activating", "deactivating", "unknown"):
        return {"modem_manager": state}
    return {"modem_manager": "unknown",
            "service_diagnostic": result.stderr.strip() or f"systemctl exit {result.returncode}"}


def print_report(report: dict) -> None:
    for change in report["changes"]:
        print(f"Applied: {change}")
    for change in report.get("planned_changes", []):
        print(f"Would apply: {change}")
    if not report["changes"] and not report["dry_run"]:
        print("No changes applied.")
    if report["scope"] != "com":
        print_system_report(report)
    if "wine" not in report:
        print(f"Configuration ready: {report['ready']} (system-only; not a download test)")
        return
    wine = report["wine"]
    print(f"Wine prefix: {wine['prefix']}; executable: {wine['binary']}")
    print(f"Wine: {wine['version'] or 'unavailable'}; requires >= {wine['minimum_version']}")
    if "version_diagnostic" in wine:
        print(f"Wine diagnostic: {wine['version_diagnostic']}")
    print(f"Wine serial: {wine['com_port']} is {wine['serial_status']}")
    print(f"COM symlink: {wine['symlink_target'] or 'missing'}")
    print(f"Wine registry: {wine['registry_target'] or 'no override'}")
    print(f"Serial diagnostic: {wine['serial_diagnostic']}")
    print(f"Configuration ready: {report['ready']} (scope: {report['scope']}; not a download test)")


def print_system_report(report: dict) -> None:
    print(f"AP Bluetooth fix {VERSION}: {report['port']} is {report['status']}")
    print(f"Machine: {report['architecture']}; kernel {report['kernel']}; Python {report['python']}")
    print(f"ModemManager: {report['modem_manager']}")
    if "service_diagnostic" in report:
        print(f"Service diagnostic: {report['service_diagnostic']}")
    print(f"Rule installed: {report['rule_installed']} ({report['rule_path']})")
    print(f"Effective ID_MM_PORT_IGNORE: {report['ignore_property'] or 'not set'}")
    if report["status"] == "protected":
        print("udev marks this port to be ignored. This does not prove DiveSight downloads work.")
    elif report["status"] == "port-missing":
        print("Port is not bound. Bind your handset, then check again; protection is unverified.")
    elif report["status"] == "pending-reboot":
        print("Rule is saved but not effective on this port. Reboot, rebind, and check again.")
    else:
        print("Port is not excluded. Preview with install --dry-run, then install using sudo.")


if __name__ == "__main__":
    raise SystemExit(main())

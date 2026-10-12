from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ap_bluetooth_fix


class TestWineChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "prefix"
        (self.prefix / "dosdevices").mkdir(parents=True)
        (self.prefix / "dosdevices/com3").symlink_to("/dev/rfcomm0")
        self.output = StringIO()

    def run_check(self, version="wine-11.18"):
        def command_result(command, **kwargs):
            output = version if command[-1] == "--version" else "active"
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch.object(ap_bluetooth_fix.subprocess, "run", side_effect=command_result):
            return ap_bluetooth_fix.check_wine(self.prefix, "com3", "wine", "rfcomm0")

    def test_minimum_version_and_matching_serial_mapping_are_ready(self):
        report = self.run_check()
        self.assertTrue(report["ready"])
        self.assertEqual(report["version"], "wine-11.18")
        self.assertEqual(report["serial_status"], "configured")

    def test_cli_reports_wine_checks_and_fails_for_old_wine_even_with_protected_port(self):
        sys_tty = Path(self.temp.name) / "tty"
        (sys_tty / "rfcomm0").mkdir(parents=True)

        def command_result(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, "wine-11.15\n", "")
            output = "ID_MM_PORT_IGNORE=1\n" if command[0] == "udevadm" else "active\n"
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch.object(ap_bluetooth_fix, "SYS_TTY_DIR", sys_tty), \
                patch.object(ap_bluetooth_fix.subprocess, "run", side_effect=command_result), \
                redirect_stdout(self.output):
            result = ap_bluetooth_fix.main(["check", "--read-only", "--json",
                                           "--wine-prefix", str(self.prefix)])
        report = json.loads(self.output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "protected")
        self.assertFalse(report["ready"])
        self.assertFalse(report["wine"]["version_ok"])

    def test_versions_are_compared_numerically(self):
        for version, accepted in [
            ("wine-11.9", False), ("wine-11.15 (Staging)", False),
            ("wine-11.18 (Staging)", True), ("wine-11.18.1", True),
            ("wine-11.100", True), ("wine-12.0", True), ("not-wine", False),
        ]:
            with self.subTest(version=version):
                self.assertEqual(self.run_check(version)["version_ok"], accepted)

    def test_missing_wine_reports_an_explicit_diagnostic(self):
        with patch.object(ap_bluetooth_fix.subprocess, "run", side_effect=FileNotFoundError("wine")):
            report = ap_bluetooth_fix.check_wine(self.prefix, "com3", "wine", "rfcomm0")
        self.assertFalse(report["ready"])
        self.assertIn("Cannot check Wine", report["version_diagnostic"])

    def test_failed_version_command_cannot_claim_readiness(self):
        with patch.object(ap_bluetooth_fix.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, "wine-11.18", "failed")):
            report = ap_bluetooth_fix.check_wine(self.prefix, "com3", "wine", "rfcomm0")
        self.assertFalse(report["ready"])
        self.assertEqual(report["version_diagnostic"], "failed")

    def test_version_timeout_reports_an_explicit_diagnostic(self):
        with patch.object(ap_bluetooth_fix.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("wine", 15)):
            report = ap_bluetooth_fix.check_wine(self.prefix, "com3", "wine", "rfcomm0")
        self.assertFalse(report["ready"])
        self.assertIn("Cannot check Wine", report["version_diagnostic"])

    def test_wrong_symlink_is_not_ready(self):
        mapping = self.prefix / "dosdevices/com3"
        mapping.unlink()
        mapping.symlink_to("/dev/rfcomm1")
        report = self.run_check()
        self.assertFalse(report["ready"])
        self.assertIn("/dev/rfcomm1", report["serial_diagnostic"])

    def test_registry_override_conflict_is_not_ready(self):
        (self.prefix / "system.reg").write_text(
            '[Software\\\\Wine\\\\Ports] 123\n"COM3"="/dev/rfcomm1"\n', encoding="utf-8")
        report = self.run_check()
        self.assertFalse(report["ready"])
        self.assertEqual(report["registry_target"], "/dev/rfcomm1")

    def test_matching_case_insensitive_registry_override_is_ready(self):
        (self.prefix / "system.reg").write_text(
            '[Software\\\\Wine\\\\Ports] 123\n"com3"="/dev/rfcomm0"\n'
            '[Software\\\\Other] 124\n"COM3"="/dev/other"\n', encoding="utf-8")
        self.assertTrue(self.run_check()["ready"])

    def test_non_string_registry_override_is_not_ready(self):
        (self.prefix / "system.reg").write_text(
            '[Software\\\\Wine\\\\Ports] 123\n"COM3"=dword:00000001\n', encoding="utf-8")
        self.assertFalse(self.run_check()["ready"])

    def test_missing_prefix_is_not_ready(self):
        missing = Path(self.temp.name) / "missing"
        with patch.object(ap_bluetooth_fix.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "wine-11.18", "")):
            report = ap_bluetooth_fix.check_wine(missing, "com3", "wine", "rfcomm0")
        self.assertFalse(report["ready"])
        self.assertFalse(missing.exists())
        self.assertIn("prefix does not exist", report["serial_diagnostic"])

    def test_cli_uses_environment_prefix_and_selected_com_port_and_executable(self):
        (self.prefix / "dosdevices/com5").symlink_to("/dev/rfcomm2")
        sys_tty = Path(self.temp.name) / "tty"
        (sys_tty / "rfcomm2").mkdir(parents=True)
        commands = []

        def command_result(command, **kwargs):
            commands.append(command)
            output = "wine-12.0" if command[-1] == "--version" else "ID_MM_PORT_IGNORE=1\n"
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch.dict(os.environ, {"WINEPREFIX": str(self.prefix)}), \
                patch.object(ap_bluetooth_fix, "SYS_TTY_DIR", sys_tty), \
                patch.object(ap_bluetooth_fix.subprocess, "run", side_effect=command_result), \
                redirect_stdout(self.output):
            result = ap_bluetooth_fix.main([
                "check", "--read-only", "--json", "--port", "rfcomm2", "--com-port", "CoM5",
                "--wine-binary", "/opt/wine/bin/wine",
            ])
        report = json.loads(self.output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["wine"]["prefix"], str(self.prefix))
        self.assertEqual(report["wine"]["com_port"], "COM5")
        self.assertIn(["/opt/wine/bin/wine", "--version"], commands)
        self.assertEqual(sorted(path.name for path in self.prefix.iterdir()), ["dosdevices"])


if __name__ == "__main__":
    unittest.main()

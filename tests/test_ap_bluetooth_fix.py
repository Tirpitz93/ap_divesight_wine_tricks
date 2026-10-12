from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import ap_bluetooth_fix


class TestBluetoothFix(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rules = Path(self.temp.name) / "rules.d"
        self.sys_tty = Path(self.temp.name) / "tty"
        self.sys_tty.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(ap_bluetooth_fix, "RULES_DIR", self.rules).start()
        patch.object(ap_bluetooth_fix, "SYS_TTY_DIR", self.sys_tty).start()
        patch.object(ap_bluetooth_fix.shutil, "which", side_effect=lambda name: "/usr/bin/" + name).start()
        patch.dict(os.environ, {"WINEPREFIX": str(Path(self.temp.name) / "unused-prefix")}).start()
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=1000).start()
        self.stdout = StringIO()
        self.stderr = StringIO()

    def run_cli(self, *args):
        args = list(args)
        if args[0] == "install":
            args.append("--system-only")
        elif args[0] == "check":
            args.append("--read-only")
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return ap_bluetooth_fix.main(args)

    def test_dry_run_shows_a_single_port_rule_without_writing_or_running_commands(self):
        with patch.object(ap_bluetooth_fix.subprocess, "run") as run:
            result = self.run_cli("install", "--port", "rfcomm3", "--dry-run")
        self.assertEqual(result, 0)
        self.assertFalse(self.rules.exists())
        run.assert_not_called()
        rule = self.stdout.getvalue()
        self.assertIn('KERNEL=="rfcomm3"', rule)
        self.assertIn('SUBSYSTEM=="tty"', rule)
        self.assertIn('ENV{ID_MM_PORT_IGNORE}="1"', rule)
        self.assertNotIn('KERNEL=="rfcomm*"', rule)
        self.assertNotIn("ID_MM_DEVICE_IGNORE", rule)

    def test_install_is_repeatable_and_only_reloads_rules_without_touching_live_devices(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        with patch.object(ap_bluetooth_fix.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(self.run_cli("install"), 0)
            rule = self.rules / "78-ap-bluetooth-rfcomm0.rules"
            content, inode = rule.read_text(), rule.stat().st_ino
            self.assertEqual(self.run_cli("install"), 0)
        self.assertEqual(rule.read_text(), content)
        self.assertEqual(rule.stat().st_ino, inode)
        self.assertEqual(rule.stat().st_mode & 0o777, 0o644)
        reloads = [call.args[0] for call in run.call_args_list if call.args[0][0] == "udevadm"]
        self.assertEqual(reloads, [["udevadm", "control", "--reload-rules"]])
        self.assertIn("reboot", self.stdout.getvalue().lower())

    def test_install_refuses_to_overwrite_an_unrecognised_existing_rule(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        self.rules.mkdir()
        rule = self.rules / "78-ap-bluetooth-rfcomm0.rules"
        rule.write_text("# Someone else's rule\n")
        with patch.object(ap_bluetooth_fix.subprocess, "run") as run:
            self.assertEqual(self.run_cli("install"), 2)
        self.assertEqual(rule.read_text(), "# Someone else's rule\n")
        run.assert_not_called()
        self.assertIn("refus", self.stderr.getvalue().lower())

    def test_remove_only_deletes_its_own_rule_and_is_repeatable(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        with patch.object(ap_bluetooth_fix.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(self.run_cli("install", "--port", "rfcomm2"), 0)
            unrelated = self.rules / "99-local.rules"
            unrelated.write_text("# Keep me\n")
            self.assertEqual(self.run_cli("remove", "--port", "rfcomm2"), 0)
            self.assertEqual(self.run_cli("remove", "--port", "rfcomm2"), 0)
        self.assertFalse((self.rules / "78-ap-bluetooth-rfcomm2.rules").exists())
        self.assertEqual(unrelated.read_text(), "# Keep me\n")

    def test_check_reports_effective_protection_without_root_or_writes(self):
        (self.sys_tty / "rfcomm0").mkdir()
        prefix = Path(self.temp.name) / "prefix"
        (prefix / "dosdevices").mkdir(parents=True)
        (prefix / "dosdevices/com3").symlink_to("/dev/rfcomm0")

        def command_result(command, **kwargs):
            output = "ID_MM_PORT_IGNORE=1\n" if command[:2] == ["udevadm", "info"] else "active\n"
            if command[-1] == "--version":
                output = "wine-11.18\n"
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch.object(ap_bluetooth_fix.subprocess, "run", side_effect=command_result):
            self.assertEqual(self.run_cli("check", "--json", "--wine-prefix", str(prefix)), 0)
        report = json.loads(self.stdout.getvalue())
        self.assertEqual(report["status"], "protected")
        self.assertEqual(report["ignore_property"], "1")
        self.assertFalse(report["rule_installed"])
        self.assertFalse(self.rules.exists())

    def test_invalid_ports_are_rejected_before_any_side_effect(self):
        for port in ("rfcomm*", "/dev/rfcomm0", "../rfcomm0", 'rfcomm0", ENV{X}="1', "ttyUSB0"):
            with self.subTest(port=port), patch.object(ap_bluetooth_fix.subprocess, "run") as run:
                with self.assertRaises(SystemExit) as error:
                    self.run_cli("install", "--port", port)
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()
        self.assertFalse(self.rules.exists())

    def test_dry_run_removal_is_read_only_without_root(self):
        with patch.object(ap_bluetooth_fix.subprocess, "run") as run:
            self.assertEqual(self.run_cli("remove", "--dry-run"), 0)
        run.assert_not_called()
        self.assertFalse(self.rules.exists())
        self.assertIn("Would remove", self.stdout.getvalue())

    def test_changes_require_root_before_writing(self):
        with patch.object(ap_bluetooth_fix.subprocess, "run") as run:
            self.assertEqual(self.run_cli("install"), 2)
            self.assertEqual(self.run_cli("remove"), 2)
        run.assert_not_called()
        self.assertFalse(self.rules.exists())
        self.assertIn("sudo", self.stderr.getvalue())

    def test_symlink_rules_are_never_followed_or_removed(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        self.rules.mkdir()
        target = Path(self.temp.name) / "untouched"
        rule = self.rules / "78-ap-bluetooth-rfcomm0.rules"
        rule.symlink_to(target)
        for action in ("install", "remove"):
            with self.subTest(action=action), patch.object(ap_bluetooth_fix.subprocess, "run") as run:
                self.assertEqual(self.run_cli(action), 2)
                run.assert_not_called()
        self.assertTrue(rule.is_symlink())
        self.assertFalse(target.exists())

    def test_remove_refuses_a_locally_modified_rule(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        self.rules.mkdir()
        rule = self.rules / "78-ap-bluetooth-rfcomm0.rules"
        rule.write_text("# Custom local rule\n")
        with patch.object(ap_bluetooth_fix.subprocess, "run") as run:
            self.assertEqual(self.run_cli("remove"), 2)
        run.assert_not_called()
        self.assertEqual(rule.read_text(), "# Custom local rule\n")

    def test_saved_rule_is_not_reported_as_effective_without_the_live_property(self):
        self.rules.mkdir()
        (self.rules / "78-ap-bluetooth-rfcomm0.rules").write_text(ap_bluetooth_fix.rule_text("rfcomm0"))
        (self.sys_tty / "rfcomm0").mkdir()
        with patch.object(ap_bluetooth_fix.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(self.run_cli("check", "--json"), 1)
            report = json.loads(self.stdout.getvalue())
            self.assertEqual(report["status"], "pending-reboot")
            self.stdout.truncate(0)
            self.stdout.seek(0)
            (self.sys_tty / "rfcomm0").rmdir()
            self.assertEqual(self.run_cli("check", "--json"), 1)
        self.assertEqual(json.loads(self.stdout.getvalue())["status"], "port-missing")

    def test_reload_failure_reports_saved_rule_and_allows_retry(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        with patch.object(ap_bluetooth_fix.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 1, "", "udev unavailable")):
            self.assertEqual(self.run_cli("install"), 2)
        self.assertTrue((self.rules / "78-ap-bluetooth-rfcomm0.rules").exists())
        self.assertIn("Rule saved, but reload failed", self.stderr.getvalue())
        self.assertIn("udev unavailable", self.stderr.getvalue())
        with patch.object(ap_bluetooth_fix.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(self.run_cli("install"), 0)

    def test_missing_udev_blocks_changes_before_writing(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        with patch.object(ap_bluetooth_fix.shutil, "which", return_value=None):
            self.assertEqual(self.run_cli("install"), 2)
        self.assertFalse(self.rules.exists())
        self.assertIn("udevadm is missing", self.stderr.getvalue())

    def test_failed_property_query_is_an_error_not_a_protected_report(self):
        (self.sys_tty / "rfcomm0").mkdir()
        with patch.object(ap_bluetooth_fix.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 1, "", "cannot read udev database")):
            self.assertEqual(self.run_cli("check", "--json"), 2)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("cannot read udev database", self.stderr.getvalue())

    def test_failed_atomic_install_cleans_up_temporary_rule(self):
        patch.object(ap_bluetooth_fix.os, "geteuid", return_value=0).start()
        with patch.object(ap_bluetooth_fix.os, "replace", side_effect=OSError("disk error")):
            self.assertEqual(self.run_cli("install"), 2)
        self.assertEqual(list(self.rules.iterdir()), [])
        self.assertIn("disk error", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

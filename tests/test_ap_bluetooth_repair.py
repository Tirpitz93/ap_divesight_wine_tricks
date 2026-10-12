from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ap_bluetooth_fix as fix


class TestRepair(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "prefix"
        (self.prefix / "dosdevices").mkdir(parents=True)
        self.output = StringIO()
        self.commands = []

    def command(self, command, **kwargs):
        self.commands.append(command)
        if "regedit" in command:
            self.assertEqual(kwargs["env"]["WINEPREFIX"], str(self.prefix))
            content = Path(command[-1]).read_text()
            self.assertIn('[HKEY_LOCAL_MACHINE\\Software\\Wine\\Ports]', content)
            self.assertIn('"COM3"="/dev/rfcomm0"', content)
            (self.prefix / "system.reg").write_text(
                '[Software\\\\Wine\\\\Ports] 123\n"COM3"="/dev/rfcomm0"\n')
        return subprocess.CompletedProcess(command, 0, "wine-11.18", "")

    def cli(self, *args):
        with patch.object(fix.os, "geteuid", return_value=1000), \
                patch.object(fix.subprocess, "run", side_effect=self.command), \
                redirect_stdout(self.output), redirect_stderr(self.output):
            return fix.main([*args, "--wine-prefix", str(self.prefix)])

    def test_com_only_check_repairs_and_reports_updated_mapping(self):
        self.assertEqual(self.cli("check", "--com-only", "--json"), 0)
        report = json.loads(self.output.getvalue())
        self.assertEqual(report["wine"]["registry_target"], "/dev/rfcomm0")
        self.assertEqual((self.prefix / "dosdevices/com3").readlink(), Path("/dev/rfcomm0"))
        self.assertTrue(report["ready"])
        self.assertEqual(len(report["changes"]), 2)

    def test_repeat_com_repair_does_not_rewrite_or_launch_regedit(self):
        self.assertEqual(self.cli("install", "--com-only"), 0)
        inode = (self.prefix / "dosdevices/com3").lstat().st_ino
        self.commands.clear()
        self.output = StringIO()
        self.assertEqual(self.cli("check", "--com-only", "--json"), 0)
        report = json.loads(self.output.getvalue())
        self.assertEqual(report["changes"], [])
        self.assertEqual((self.prefix / "dosdevices/com3").lstat().st_ino, inode)
        self.assertFalse(any("regedit" in command for command in self.commands))

    def test_read_only_does_not_create_missing_mappings(self):
        self.assertEqual(self.cli("check", "--com-only", "--read-only", "--json"), 1)
        report = json.loads(self.output.getvalue())
        self.assertEqual(report["changes"], [])
        self.assertFalse((self.prefix / "dosdevices/com3").is_symlink())
        self.assertFalse(any("regedit" in command for command in self.commands))

    def test_dry_run_lists_repairs_without_creating_them(self):
        self.assertEqual(self.cli("install", "--com-only", "--dry-run"), 0)
        self.assertIn("Would apply:", self.output.getvalue())
        self.assertFalse((self.prefix / "dosdevices/com3").is_symlink())
        self.assertFalse(any("regedit" in command for command in self.commands))

    def test_wrong_mapping_is_replaced_and_registry_import_uses_selected_prefix(self):
        (self.prefix / "dosdevices/com3").symlink_to("/dev/rfcomm9")
        self.assertEqual(self.cli("check", "--com-only"), 0)
        self.assertEqual((self.prefix / "dosdevices/com3").readlink(), Path("/dev/rfcomm0"))
        self.assertIn("Wine registry: /dev/rfcomm0", self.output.getvalue())

    def test_regular_file_mapping_is_preserved(self):
        mapping = self.prefix / "dosdevices/com3"
        mapping.write_text("keep")
        self.assertEqual(self.cli("install", "--com-only"), 2)
        self.assertEqual(mapping.read_text(), "keep")
        self.assertFalse(any("regedit" in command for command in self.commands))

    def test_root_cannot_repair_wine(self):
        with patch.object(fix.os, "geteuid", return_value=0), redirect_stderr(self.output):
            result = fix.main(["install", "--com-only", "--wine-prefix", str(self.prefix)])
        self.assertEqual(result, 2)
        self.assertIn("Do not repair Wine as root", self.output.getvalue())

    def test_failed_regedit_does_not_claim_success_or_replace_link(self):
        mapping = self.prefix / "dosdevices/com3"
        mapping.symlink_to("/dev/rfcomm9")

        def failure(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "registry import failed")

        self.command = failure
        self.assertEqual(self.cli("install", "--com-only"), 2)
        self.assertEqual(mapping.readlink(), Path("/dev/rfcomm9"))
        self.assertIn("registry import failed", self.output.getvalue())

    def test_default_check_repairs_system_rule_and_com_mapping_and_reports_reboot(self):
        rules = Path(self.temp.name) / "rules.d"
        tty = Path(self.temp.name) / "tty"
        (tty / "rfcomm0").mkdir(parents=True)
        original = self.command

        def repair_command(command, **kwargs):
            if command[0] == "sudo":
                rules.mkdir()
                (rules / "78-ap-bluetooth-rfcomm0.rules").write_text(fix.rule_text("rfcomm0"))
            return original(command, **kwargs)

        self.command = repair_command
        with patch.object(fix, "RULES_DIR", rules), patch.object(fix, "SYS_TTY_DIR", tty), \
                patch.object(fix.sys, "argv", [fix.__file__]):
            self.assertEqual(self.cli("check", "--json"), 1)
        report = json.loads(self.output.getvalue())
        self.assertTrue(report["rule_installed"])
        self.assertEqual(report["status"], "pending-reboot")
        self.assertEqual(report["wine"]["serial_status"], "configured")
        self.assertEqual(len(report["changes"]), 3)
        sudo = next(command for command in self.commands if command[0] == "sudo")
        self.assertIn("--system-only", sudo)

    def test_successful_regedit_without_saved_mapping_is_not_reported_as_repaired(self):
        self.command = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "")
        self.assertEqual(self.cli("install", "--com-only"), 2)
        self.assertIn("without saving", self.output.getvalue())
        self.assertFalse((self.prefix / "dosdevices/com3").is_symlink())


if __name__ == "__main__":
    unittest.main()

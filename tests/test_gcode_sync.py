"""Run with python3 -m unittest discover -s tests -v; no hardware/root needed."""
import copy
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("gcode_sync", str(ROOT / "gcode_sync"))
spec = importlib.util.spec_from_loader(loader.name, loader)
g = importlib.util.module_from_spec(spec)
loader.exec_module(g)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "G-code μm files"
        self.source.mkdir()
        self.mount = self.root / "card"
        self.mount.mkdir()
        self.config = self.root / "profiles" / "config.json"
        self.printer = {"name": "Test Printer", "source": str(self.source), "config_file": ""}
        self.card = {"uuid": "ABCD-1234", "mount": "/mnt/gcode_sync/card1", "printer": "test"}
        self.data = {"version": 1, "printers": {"test": self.printer}, "cards": {"card1": self.card}}
        self.args = SimpleNamespace(dry_run=False, delete=False, rename_bin=False,
                                    printer=None, card=None, config=self.config)
        self.stdout = patch("sys.stdout", new_callable=io.StringIO)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)

    def local_card(self):
        return dict(self.card, mount=str(self.mount))

    def test_round_trip_atomic_config(self):
        g.save_config(self.config, self.data)
        self.assertEqual(g.load_config(self.config), self.data)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_duplicate_uuid_and_unknown_printer_rejected(self):
        bad = copy.deepcopy(self.data)
        bad["cards"]["card2"] = dict(self.card, uuid="abcd-1234", mount="/mnt/gcode_sync/card2")
        with self.assertRaises(g.SyncError):
            g.validate_config(bad)
        bad = copy.deepcopy(self.data)
        bad["cards"]["card1"]["printer"] = "missing"
        with self.assertRaises(g.SyncError):
            g.validate_config(bad)

    def test_invalid_config_not_overwritten(self):
        self.config.parent.mkdir()
        self.config.write_text("broken json")
        with self.assertRaises(g.SyncError):
            g.load_config(self.config)
        self.assertEqual(self.config.read_text(), "broken json")

    def test_prompt_rejects_empty_and_file_paths(self):
        file = self.root / "part.gcode"
        file.write_text("G28")
        answers = ["new", "New Printer", "", str(file), str(self.source), "yes", ""]
        with patch("builtins.input", side_effect=answers):
            g.add_printer(self.data, self.config)
        self.assertEqual(g.load_config(self.config)["printers"]["new"]["source"], str(self.source))

    def test_cancel_printer_leaves_no_config(self):
        with patch("builtins.input", side_effect=["new", "New", str(self.source), "n"]):
            with self.assertRaises(g.SyncError):
                g.add_printer(self.data, self.config)
        self.assertFalse(self.config.exists())

    def test_edit_printer_retains_card_association(self):
        with patch("builtins.input", side_effect=["Renamed", "", "y", ""]):
            g.add_printer(self.data, self.config, "test")
        self.assertEqual(self.data["cards"]["card1"]["printer"], "test")
        self.assertEqual(self.data["printers"]["test"]["source"], str(self.source))

    def test_fstab_exact_duplicate_and_conflicts(self):
        line = g.fstab_line(self.card, "vfat")
        self.assertFalse(g.check_fstab("# comment\n", line))
        self.assertTrue(g.check_fstab(line + " # existing\n", line))
        for text in (line.replace("/card1", "/other"), line.replace("ABCD-1234", "DEAD-BEEF"), line + "\n" + line):
            with self.subTest(text=text), self.assertRaises(g.SyncError):
                g.check_fstab(text, line)

    def test_unsupported_filesystem_rejected(self):
        with self.assertRaises(g.SyncError):
            g.fstab_line(self.card, "crypto_LUKS")

    def test_fstab_cancellation_never_elevates(self):
        with patch.object(Path, "read_text", return_value=""), patch.object(g, "confirm", return_value=False), patch.object(g, "run") as run:
            with self.assertRaises(g.SyncError):
                g.install_fstab(self.card, "vfat")
            run.assert_not_called()

    def test_helper_atomic_write_backup_idempotency_and_conflict(self):
        # Relocate EVERY privileged path into the temporary directory, never /etc.
        etc = self.root / "etc"
        etc.mkdir()
        fstab = etc / "fstab"
        original = "# keep existing content\nUUID=ROOT / ext4 defaults 0 1"
        fstab.write_text(original)
        fstab.chmod(0o644)
        mountbase = self.root / "mounts"
        helper = g.FSTAB_HELPER.replace("/etc/", str(etc) + "/").replace("/mnt/gcode_sync/", str(mountbase) + "/")
        line = g.fstab_line(self.card, "vfat").replace("/mnt/gcode_sync/", str(mountbase) + "/")

        def invoke(value):
            return subprocess.run([sys.executable, "-c", helper], input=json.dumps({"line": value}),
                                  capture_output=True, text=True)

        result = invoke(line)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = original + "\n" + line + "\n"
        self.assertEqual(fstab.read_text(), expected)
        self.assertEqual(fstab.stat().st_mode & 0o777, 0o644)
        backups = list(etc.glob("fstab.gcode_sync.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(invoke(line).returncode, 0)
        self.assertEqual(fstab.read_text(), expected)
        self.assertNotEqual(invoke(line.replace("ABCD-1234", "OTHER")).returncode, 0)
        self.assertEqual(fstab.read_text(), expected)
        self.assertNotEqual(invoke(line.replace(" vfat ", "\nvfat ")).returncode, 0)
        self.assertEqual(fstab.read_text(), expected)

    def test_lsblk_nested_removable_and_unicode_label(self):
        tree = {"blockdevices": [{"name": "/dev/sdb", "type": "disk", "rm": True,
                 "children": [{"name": "/dev/sdb1", "type": "part", "uuid": "1234-ABCD",
                               "fstype": "vfat", "label": "μm"}]}]}
        with patch.object(g, "run", return_value=SimpleNamespace(stdout=json.dumps(tree))):
            cards = g.devices()
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["removable"])

    def test_wrong_uuid_never_copies_and_unmounts(self):
        with patch.object(g, "mounted_uuid", side_effect=[None, "WRONG"]), patch.object(g, "run") as run:
            with self.assertRaises(g.SyncError):
                g.sync_card("card1", self.local_card(), self.printer, self.args)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["mount", "umount"])

    def test_rsync_safe_defaults_and_paths(self):
        with patch.object(g, "mounted_uuid", return_value="ABCD-1234"), patch.object(g, "run") as run:
            g.sync_card("card1", self.local_card(), self.printer, self.args)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "rsync")
        self.assertNotIn("--delete-after", command)
        self.assertIn("--checksum", command)
        self.assertEqual(command[-3:], ["--", str(self.source) + "/", str(self.mount) + "/"])
        self.assertEqual(run.call_count, 1)  # Already mounted: leave it mounted.

    def test_failure_still_unmounts_card(self):
        def fail(args, **kwargs):
            if args[0] == "rsync":
                raise g.SyncError("copy failed")
        with patch.object(g, "mounted_uuid", side_effect=[None, "ABCD-1234"]), patch.object(g, "run", side_effect=fail) as run:
            with self.assertRaises(g.SyncError):
                g.sync_card("card1", self.local_card(), self.printer, self.args)
        self.assertEqual(run.call_args.args[0][0], "umount")

    def test_dry_run_never_backs_up_or_renames(self):
        (self.mount / "config.txt").write_text("card config")
        self.printer["config_file"] = "config.txt"
        self.args.dry_run = self.args.delete = self.args.rename_bin = True
        with patch.object(g, "mounted_uuid", return_value="ABCD-1234"), patch.object(g, "run") as run, patch.object(g, "rename_root_bin") as rename:
            g.sync_card("card1", self.local_card(), self.printer, self.args)
        self.assertFalse((self.source / "config.txt").exists())
        self.assertIn("--dry-run", run.call_args.args[0])
        self.assertIn("--delete-after", run.call_args.args[0])
        rename.assert_not_called()

    def test_config_copied_back_before_sync(self):
        (self.mount / "config.txt").write_text("card config")
        self.printer["config_file"] = "config.txt"
        def verify(args, **kwargs):
            self.assertEqual((self.source / "config.txt").read_text(), "card config")
        with patch.object(g, "mounted_uuid", return_value="ABCD-1234"), patch.object(g, "run", side_effect=verify):
            g.sync_card("card1", self.local_card(), self.printer, self.args)

    def test_source_destination_overlap_rejected(self):
        with patch.object(g, "run") as run:
            with self.assertRaises(g.SyncError):
                g.sync_card("card1", dict(self.card, mount=str(self.source)), self.printer, self.args)
        run.assert_not_called()

    def test_rename_collision_and_repeat_tracking(self):
        (self.mount / "firmware.bin").write_text("firmware")
        with patch.object(g.time, "time", return_value=12345678):
            first = g.rename_root_bin(self.mount)
            (self.mount / "firmware.bin").write_text("new firmware")
            second = g.rename_root_bin(self.mount, first, "firmware.bin")
        self.assertNotEqual(first, second)
        self.assertFalse((self.mount / first).exists())
        self.assertEqual((self.mount / second).read_text(), "new firmware")
        self.assertRegex(second, r"^F[0-9]{7}\.BIN$")

    def test_multiple_unknown_binaries_are_never_removed(self):
        for name in ["one.bin", "two.bin"]:
            (self.mount / name).write_text("firmware")
        with self.assertRaises(g.SyncError):
            g.rename_root_bin(self.mount, "F0000001.BIN", "one.bin")
        self.assertEqual(len(list(self.mount.iterdir())), 2)

    def test_card_registration_persists_selected_uuid_and_printer(self):
        device = {"name": "/dev/test1", "uuid": "CAFE-1234", "fstype": "vfat",
                  "size": "8G", "removable": True, "mountpoint": None}
        with patch("builtins.input", side_effect=["spare", "", "1"]), patch.object(g, "devices", return_value=[device]), patch.object(g, "install_fstab") as install:
            g.add_card(self.data, self.config, "test")
        saved = g.load_config(self.config)["cards"]["spare"]
        self.assertEqual(saved, {"printer": "test", "uuid": "CAFE-1234", "mount": "/mnt/gcode_sync/spare"})
        install.assert_called_once_with(saved, "vfat")

    def test_real_rsync_preserves_card_only_files_and_copies_contents(self):
        if g.shutil.which("rsync") is None:
            self.skipTest("rsync is not installed")
        (self.source / "part.gcode").write_text("G28\n")
        (self.mount / "keep.txt").write_text("keep")
        with patch.object(g, "mounted_uuid", return_value="ABCD-1234"):
            g.sync_card("card1", self.local_card(), self.printer, self.args)
        self.assertEqual((self.mount / "part.gcode").read_text(), "G28\n")
        self.assertTrue((self.mount / "keep.txt").exists())
        self.assertFalse((self.mount / self.source.name).exists())
        self.args.delete = True
        with patch.object(g, "mounted_uuid", return_value="ABCD-1234"):
            g.sync_card("card1", self.local_card(), self.printer, self.args)
        self.assertFalse((self.mount / "keep.txt").exists())

    def test_missing_cards_fail_without_sync(self):
        with patch.object(g, "devices", return_value=[]), patch.object(g.shutil, "which", return_value="rsync"), patch.object(g, "sync_card") as sync:
            with self.assertRaises(g.SyncError):
                g.sync_all(self.data, self.args)
        sync.assert_not_called()

    def test_clone_uuids_and_automounted_cards_fail_without_sync(self):
        for devices in ([{"uuid": "ABCD-1234"}] * 2,
                        [{"uuid": "ABCD-1234", "mountpoint": "/media/user/card"}]):
            with self.subTest(devices=devices), patch.object(g, "devices", return_value=devices), patch.object(g.shutil, "which", return_value="rsync"), patch.object(g, "sync_card") as sync, patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(g.sync_all(self.data, self.args), 1)
                sync.assert_not_called()

    def test_cli_assign_card_persists_new_printer(self):
        self.data["printers"]["other"] = dict(self.printer, name="Other")
        g.save_config(self.config, self.data)
        with patch.object(g.os, "geteuid", return_value=1000):
            self.assertEqual(g.main(["--config", str(self.config), "--assign-card", "card1", "--printer", "other"]), 0)
        self.assertEqual(g.load_config(self.config)["cards"]["card1"]["printer"], "other")

    def test_help_is_safe_direct_via_python_and_via_shell(self):
        script = str(ROOT / "gcode_sync")
        for prefix in ([script], [sys.executable, script], ["sh", script]):
            for flag in ("-h", "--help"):
                with self.subTest(prefix=prefix, flag=flag):
                    result = subprocess.run(prefix + ["--config", str(self.config), flag],
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout)
                    self.assertIn("--setup", result.stdout)
                    self.assertIn("--add-printer", result.stdout)
                    self.assertFalse(self.config.exists())

    def test_missing_config_starts_setup_without_tty(self):
        with patch.object(g.os, "geteuid", return_value=1000), patch.object(g.sys.stdin, "isatty", return_value=False), patch.object(g, "add_printer", return_value="test") as printer, patch.object(g, "add_card") as card, patch.object(g, "sync_all") as sync:
            self.assertEqual(g.main(["--config", str(self.config)]), 0)
        printer.assert_called_once()
        card.assert_called_once_with(printer.call_args.args[0], self.config, "test")
        sync.assert_not_called()

    def test_missing_config_dry_run_does_not_start_setup(self):
        with patch.object(g.os, "geteuid", return_value=1000), patch.object(g, "add_printer") as setup:
            with self.assertRaisesRegex(g.SyncError, "--setup first"):
                g.main(["--config", str(self.config), "--dry-run"])
        setup.assert_not_called()

    def test_cli_disallows_setup_dry_run(self):
        with self.assertRaises(g.SyncError):
            g.main(["--setup", "--dry-run"])


if __name__ == "__main__":
    unittest.main()

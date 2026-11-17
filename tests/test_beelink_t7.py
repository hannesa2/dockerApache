import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class T7GuardsTest(unittest.TestCase):
    def test_device_guards_run_before_any_disk_changes(self):
        source = (ROOT / "monitoring/setup-beelink-t7.sh").read_text()
        identity = source.split('python3 - "$disk" <<\'PY\'\n', 1)[1].split("\nPY", 1)[0]
        device = dict(type="disk", model="PSSD T7", serial="H758101R0JN7T5S",
                      size=2000398934016, tran="usb", mountpoints=[None],
                      children=[dict(type="part", mountpoints=[None])])
        with tempfile.TemporaryDirectory() as tmp:
            program = Path(tmp) / "lsblk"
            program.write_text(
                '#!/bin/sh\n'
                'case " $* " in *" --tree "*) ;; *) exit 42 ;; esac\n'
                'printf "%s\\n" "$LSBLK_FIXTURE"\n'
            )
            program.chmod(0o755)
            for label, change, accepted in (
                ("expected", {}, True),
                ("wrong serial", {"serial": "another-disk"}, False),
                ("wrong capacity", {"size": 512000000000}, False),
                ("wrong model", {"model": "SSD 512GB"}, False),
                ("mounted partition", {"children": [dict(type="part", mountpoints=["/backup"])]}, False),
                ("active mapping", {"children": [dict(type="crypt", mountpoints=[None])]}, False),
            ):
                with self.subTest(label=label):
                    fixture = dict(device, **change)
                    result = subprocess.run(
                        ["python3", "-c", identity, "/dev/test-only"],
                        env=dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                                 LSBLK_FIXTURE=json.dumps({"blockdevices": [fixture]})),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)
        self.assertLess(source.index('python3 - "$disk"'), source.index('wipefs --all'))
        self.assertIn("nofail,headless", source)
        self.assertIn("x-systemd.automount", source)
        self.assertIn('findmnt -rn -M /daten -t ext4 -o SOURCE', source)

    def test_requires_explicit_erase_argument(self):
        result = subprocess.run(
            ["bash", str(ROOT / "monitoring/setup-beelink-t7.sh")],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--erase-confirmed-t7", result.stderr)


if __name__ == "__main__":
    unittest.main()

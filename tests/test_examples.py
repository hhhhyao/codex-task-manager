"""The example reports delegated outcomes without forcing every result to flush."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHECK = Path(__file__).resolve().parents[1] / "examples/check_manifest.py"


class ManifestExampleTests(unittest.TestCase):
    def observe(self, state, *flags, run_id="expected"):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "status.json"
            original = json.dumps({"run_id": run_id, "state": state})
            manifest.write_text(original)
            result = subprocess.run([sys.executable, str(CHECK), "--manifest", str(manifest),
                                     "--run-id", "expected", *flags], capture_output=True, text=True, timeout=5)
            self.assertEqual(manifest.read_text(), original)
            return result

    def test_running_updates_status_without_requesting_attention(self):
        result = self.observe("running")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "running", "events": []})

    def test_terminal_outcomes_do_not_automatically_force_early_delivery(self):
        for state in ("completed", "failed"):
            with self.subTest(state=state):
                result = self.observe(state)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertTrue(value["done"])
                self.assertFalse(value["events"][0]["immediate"])
                self.assertEqual(value["events"][0]["important"], state == "failed")

    def test_early_attention_can_be_explicitly_selected(self):
        result = self.observe("completed", "--immediate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["events"][0]["immediate"])

    def test_identity_error_is_not_a_completion_or_waiting_notice(self):
        result = self.observe("completed", run_id="other-run")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import deploy_comparison_model as dcm  # noqa: E402


def _dep(name, model="FW-Kimi-K3", version="1", sku="DataZoneStandard", cap=30, state="Succeeded", modified="t1"):
    return {
        "name": name,
        "sku": {"name": sku, "capacity": cap},
        "properties": {
            "model": {"name": model, "version": version, "format": "Fireworks"},
            "provisioningState": state,
            "rateLimits": [{"key": "request", "count": 30.0}, {"key": "token", "count": 30000.0}],
        },
        "systemData": {"lastModifiedAt": modified},
    }


def _args(models):
    return Namespace(models=models, deployment_name="x", model_name="x", model_format="x", model_version="1", sku="x")


class ResolveTargetsTests(unittest.TestCase):
    def test_key_can_carry_its_own_capacity(self):
        targets = dcm._resolve_targets(_args("glm-5.3=100,kimi-k3"))
        self.assertEqual([t["deployment_name"] for t in targets], ["glm-5.3", "kimi-k3"])
        self.assertEqual(targets[0]["capacity"], "100")
        self.assertNotIn("capacity", targets[1])

    def test_unknown_key_and_bad_capacity_exit(self):
        with self.assertRaises(SystemExit):
            dcm._resolve_targets(_args("nope"))
        with self.assertRaises(SystemExit):
            dcm._resolve_targets(_args("glm-5.3=lots"))


class QuotaTests(unittest.TestCase):
    def test_fireworks_prefix_is_dropped_when_matching_quota_rows(self):
        rows = {"aiservices.datazonestandard.deepseek-v4.1-flash": (0.0, 250.0)}
        dep = _dep("d", model="FW-DeepSeek-V4.1-Flash")
        self.assertEqual(dcm._quota_for(rows, dep), "0/250")

    def test_missing_quota_row_is_a_dash(self):
        self.assertEqual(dcm._quota_for({}, _dep("d")), "-")


class CheckTests(unittest.TestCase):
    def _run_check(self, saved, now, only=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.json"
            path.write_text(json.dumps({"deployments": saved}))
            out = io.StringIO()
            with mock.patch.object(dcm, "_deployments_json", return_value=now), contextlib.redirect_stdout(out):
                rc = dcm._check("acct", "rg", str(path), only)
        return rc, out.getvalue()

    def test_identical_state_passes_even_if_capacity_changed(self):
        rc, out = self._run_check([_dep("k", cap=30)], [_dep("k", cap=100)])
        self.assertEqual(rc, 0)
        self.assertIn("capacity 30 -> 100", out)

    def test_changed_model_version_state_or_removal_fails(self):
        for now in (
            [_dep("k", version="2")],
            [_dep("k", model="FW-Kimi-K4")],
            [_dep("k", state="Disabled")],
            [],
        ):
            rc, out = self._run_check([_dep("k")], now)
            self.assertEqual(rc, 1, out)

    def test_only_limits_the_comparison(self):
        rc, _ = self._run_check([_dep("a"), _dep("b")], [_dep("a"), _dep("b", version="2")], only={"a"})
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

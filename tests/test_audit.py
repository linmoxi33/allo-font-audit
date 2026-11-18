import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import allo_font_audit as audit  # noqa: E402

EXAMPLE = os.path.join(ROOT, "examples", "in")
CLI = os.path.join(ROOT, "allo_font_audit.py")


class Fixture:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="allo-audit-")
        self.root = os.path.join(self.tmp, "release")
        shutil.copytree(EXAMPLE, self.root)
        self.contract = os.path.join(self.root, "contract.json")
        self.sums = os.path.join(self.root, "fonts", "SHA256SUMS")

    def load_contract(self):
        with open(self.contract, encoding="utf-8") as fh:
            return json.load(fh)

    def save_contract(self, data):
        with open(self.contract, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def run(self, **kwargs):
        return audit.run_audit(self.root, self.contract, self.sums, **kwargs)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def cli(*args, env=None):
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          cwd=ROOT, env=env)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    # --- unit-level parsing -------------------------------------------------
    def test_parse_variable_family_and_axes(self):
        var = audit.Font("v", open(os.path.join(self.fx.root, "fonts", "variable", "AlloVariable.ttf"), "rb").read())
        self.assertEqual(var.family, "Allo Variable")
        self.assertEqual([a["tag"] for a in var.axes], ["opsz", "wght", "ROND"])
        self.assertEqual(var.axes[0]["min"], 9)
        self.assertEqual(var.axes[0]["default"], 14)
        self.assertEqual(var.axes[2]["max"], 100)

    def test_parse_static_weight_and_italic(self):
        reg = audit.Font("r", open(os.path.join(self.fx.root, "fonts", "otf", "Allo-Regular.otf"), "rb").read())
        self.assertEqual(reg.weight, 400)
        self.assertFalse(reg.italic)
        self.assertEqual(reg.family, "Allo")

    def test_cmap_and_feature_tables(self):
        reg = audit.Font("r", open(os.path.join(self.fx.root, "fonts", "otf", "Allo-Regular.otf"), "rb").read())
        self.assertGreater(len(reg.cmap), 600)
        self.assertIn(0x41, reg.cmap)
        self.assertIn("tnum", reg.features)
        self.assertIn("frac", reg.features)

    # --- clean audit --------------------------------------------------------
    def test_clean_example_has_no_findings(self):
        report = self.fx.run()
        self.assertEqual(report["summary"]["findings"], 0)
        self.assertEqual(report["summary"]["statics"], 1)
        self.assertEqual(len(report["input_sha256"]), 64)

    # --- SHA256SUMS integrity ----------------------------------------------
    def test_sums_mismatch_is_detected(self):
        path = os.path.join(self.fx.root, "fonts", "otf", "Allo-Regular.otf")
        with open(path, "ab") as fh:
            fh.write(b"\0")
        codes = {f["code"] for f in self.fx.run()["findings"]}
        self.assertIn("SUMS_MISMATCH", codes)

    def test_sums_missing_file_is_detected(self):
        os.remove(os.path.join(self.fx.root, "fonts", "otf", "Allo-Regular.otf"))
        codes = {f["code"] for f in self.fx.run()["findings"]}
        self.assertIn("SUMS_MISSING", codes)
        self.assertIn("STATIC_MISSING", codes)

    def test_unlisted_font_is_detected(self):
        shutil.copy(os.path.join(self.fx.root, "fonts", "otf", "Allo-Regular.otf"),
                    os.path.join(self.fx.root, "fonts", "otf", "Stray.otf"))
        codes = {f["code"] for f in self.fx.run()["findings"]}
        self.assertIn("SUMS_UNLISTED", codes)

    # --- contract errors ----------------------------------------------------
    def test_weight_contract_mismatch(self):
        c = self.fx.load_contract()
        c["statics"][0]["weight"] = 500
        self.fx.save_contract(c)
        codes = {f["code"] for f in self.fx.run()["findings"]}
        self.assertIn("WEIGHT", codes)

    def test_family_contract_mismatch(self):
        c = self.fx.load_contract()
        c["statics"][0]["family"] = "Not Allo"
        self.fx.save_contract(c)
        codes = {f["code"] for f in self.fx.run()["findings"]}
        self.assertIn("NAME_FAMILY", codes)

    def test_axis_range_mismatch(self):
        c = self.fx.load_contract()
        c["axes"]["opsz"]["max"] = 33
        self.fx.save_contract(c)
        f = [x for x in self.fx.run()["findings"] if x["code"] == "AXIS_RANGE"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["detail"]["field"], "max")

    def test_feature_missing_in_contract(self):
        c = self.fx.load_contract()
        c["features"].append("zzzz")
        self.fx.save_contract(c)
        codes = {f["code"] for f in self.fx.run()["findings"]}
        self.assertIn("FEATURE_MISSING", codes)

    def test_missing_static_is_detected(self):
        c = self.fx.load_contract()
        c["statics"].append({"file": "fonts/otf/Allo-Black.otf", "family": "Allo", "weight": 900})
        self.fx.save_contract(c)
        codes = {f["code"] for f in self.fx.run()["findings"]}
        self.assertIn("STATIC_MISSING", codes)

    # --- CLI behavior -------------------------------------------------------
    def test_cli_json_clean_exit_zero(self):
        p = cli(self.fx.root, "--contract", self.fx.contract, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)
        self.assertEqual(report["summary"]["errors"], 0)
        self.assertEqual(report["tool"], "allo-font-audit")

    def test_cli_finding_exit_one(self):
        c = self.fx.load_contract()
        c["statics"][0]["weight"] = 500
        self.fx.save_contract(c)
        p = cli(self.fx.root, "--contract", self.fx.contract, "--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("WEIGHT", {f["code"] for f in json.loads(p.stdout)["findings"]})

    def test_cli_missing_contract_exit_two(self):
        p = cli(self.fx.root, "--contract", os.path.join(self.fx.tmp, "nope.json"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("cannot read contract", p.stderr)

    def test_cli_bad_json_exit_two(self):
        with open(self.fx.contract, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        p = cli(self.fx.root, "--contract", self.fx.contract)
        self.assertEqual(p.returncode, 2)
        self.assertIn("not valid JSON", p.stderr)

    def test_cli_offline_equals_online(self):
        env = dict(os.environ, http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9")
        p1 = cli(self.fx.root, "--contract", self.fx.contract, "--json")
        p2 = cli(self.fx.root, "--contract", self.fx.contract, "--json", env=env)
        self.assertEqual(p1.returncode, p2.returncode)
        self.assertEqual(json.loads(p1.stdout)["input_sha256"], json.loads(p2.stdout)["input_sha256"])


if __name__ == "__main__":
    unittest.main()
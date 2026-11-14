import json, unittest
from tests import stubs  # noqa: F401
from infinity.comparison import parse_json_object, run_comparison, validate_comparison
from tests.test_rag import ev


class FakeLLM:
    def __init__(self, reply): self.reply = reply
    def complete_with_instructions(self, prompt, *, system_instruction): return self.reply


class ComparisonTests(unittest.TestCase):
    def test_parse_handles_fences_and_prose(self):
        self.assertEqual(parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_object('Here you go: {"a": {"b": 2}} done'), {"a": {"b": 2}})
        with self.assertRaises(ValueError): parse_json_object("no json here")

    def test_unsupported_cells_are_downgraded(self):
        data = {"rows": [{"aspect": "Class", "values": {
            "Mk6N": {"value": "0.2S", "citations": ["S1"], "status": "verified"},
            "Mk10M": {"value": "0.5S", "citations": ["S9"], "status": "verified"}}},
            {"aspect": "Ports", "values": {"Mk6N": {"value": "RS-232", "citations": [], "status": "verified"}}}]}
        out = validate_comparison(data, ["Mk6N", "Mk10M"], ["Class", "Ports", "Weight"], n_evidence=2)
        cells = {r["aspect"]: r["values"] for r in out["rows"]}
        self.assertEqual(cells["Class"]["Mk6N"]["status"], "verified")
        self.assertEqual(cells["Class"]["Mk10M"], {"value": "", "citations": [], "status": "not_found"})  # fabricated citation S9
        self.assertEqual(cells["Ports"]["Mk6N"]["status"], "not_found")                                  # no citation
        self.assertEqual(cells["Weight"]["Mk6N"]["status"], "not_found")                                 # row missing from model output
        self.assertEqual(out["downgraded_cells"], 2); self.assertEqual(out["used_markers"], ["S1"])

    def test_end_to_end_with_fake_model(self):
        reply = json.dumps({"rows": [{"aspect": "Class", "values": {"A": {"value": "0.2S", "citations": ["S1"], "status": "verified"}, "B": {"value": "", "citations": [], "status": "not_found"}}}], "conflicts": ["x"]})
        pool = {"A": [ev(1)], "B": [ev(2)]}
        data, cites = run_comparison(FakeLLM(reply), None, "admin", ["A", "B"], ["Class"], lambda q: pool["A"] if q.startswith("A") else pool["B"])
        self.assertEqual([c["id"] for c in cites], ["S1"]); self.assertEqual(data["conflicts"], ["x"])

    def test_no_evidence_skips_model(self):
        class Boom:
            def complete_with_instructions(self, *a, **k): raise AssertionError("must not call model")
        data, cites = run_comparison(Boom(), None, "admin", ["A", "B"], ["Class"], lambda q: [])
        self.assertEqual(cites, []); self.assertEqual(data["rows"][0]["values"]["A"]["status"], "not_found")


if __name__ == "__main__":
    unittest.main()

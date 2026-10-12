import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import rerun_failed  # noqa: E402


def _rec(model, prompt, repeat, *, answer="42", error=None, turns=2):
    return {
        "model": model, "prompt": prompt, "repeat": repeat, "answer": answer,
        "success": bool(answer) and error is None, "error": error, "num_turns": turns,
    }


class FindFailuresTests(unittest.TestCase):
    def test_only_failed_runs_are_returned_with_their_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "run_meta.json").write_text(json.dumps({"settings": {"api": "responses"}}))
            batch = run / "m" / "batch1"
            batch.mkdir(parents=True)
            (batch / "run_001_m_1.json").write_text(json.dumps(_rec("m", "p1", 1)))
            (batch / "run_002_m_2.json").write_text(
                json.dumps(_rec("m", "p1", 2, answer="", error="ChatClientException: 429", turns=0))
            )
            (batch / "run_003_m_1.json").write_text(json.dumps(_rec("m", "p2", 1, answer="")))

            failures = rerun_failed.find_failures(run)

        self.assertEqual([(f["prompt"], f["original_repeat"]) for f in failures], [("p1", 2), ("p2", 1)])
        self.assertIn("429", failures[0]["original_error"])
        self.assertTrue(failures[0]["original_file"].endswith("run_002_m_2.json"))


if __name__ == "__main__":
    unittest.main()

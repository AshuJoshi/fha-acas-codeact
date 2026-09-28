import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import compare_reasoning_runs as crr  # noqa: E402


def _record(model, prompt, *, answer="42", tools=("execute_code",), wall_ms=1000, retries=0):
    return {
        "model": model,
        "prompt": prompt,
        "answer": answer,
        "success": bool(answer),
        "num_turns": len(tools) + 1,
        "num_tool_calls": len(tools),
        "tool_calls": [{"tool": t, "code": "print(1)"} for t in tools],
        "total_wall_ms": wall_ms,
        "total_tokens": 100,
        "retry_count": retries,
    }


class SummarizeTests(unittest.TestCase):
    def test_stable_prompt_requires_identical_path_on_every_repeat(self):
        records = [
            _record("m", "p1"),
            _record("m", "p1"),
            _record("m", "p2"),
            _record("m", "p2", tools=("execute_code", "run_shell")),
        ]
        (summary,) = crr._summarize(records)
        self.assertEqual(summary["prompts"], 2)
        self.assertEqual(summary["stable_prompts"], 1)

    def test_failure_markers_and_empty_answers_are_not_successes(self):
        records = [
            _record("m", "p", answer=""),
            _record("m", "p", answer="Function invocation limit reached before a final answer"),
            _record("m", "p", answer="ok", wall_ms=3000, retries=2),
        ]
        (summary,) = crr._summarize(records)
        self.assertEqual(summary["effective_successes"], 1)
        self.assertEqual(summary["failures"], 2)
        self.assertEqual(summary["mean_success_wall_s"], 3.0)
        self.assertEqual((summary["runs_with_retries"], summary["total_retries"]), (1, 2))

    def test_model_with_no_successes_reports_none_and_prints(self):
        (summary,) = crr._summarize([_record("m", "p", answer="")])
        self.assertIsNone(summary["mean_success_wall_s"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            crr._print_table([summary])
        self.assertIn("n/a", out.getvalue())


class LoadRecordsTests(unittest.TestCase):
    def test_skipped_files_are_reported_and_overlapping_paths_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch1"
            batch.mkdir()
            (batch / "run_001.json").write_text(json.dumps(_record("m", "p")))
            (batch / "run_002.json").write_text("{not json")
            (batch / "run_003.json").write_text(json.dumps([1, 2]))

            with contextlib.redirect_stderr(io.StringIO()) as err:
                records = crr._load_records([root, batch])

        self.assertEqual(len(records), 1)
        self.assertIn("run_002.json", err.getvalue())
        self.assertIn("run_003.json", err.getvalue())
        self.assertIn("2 file(s) skipped", err.getvalue())


if __name__ == "__main__":
    unittest.main()

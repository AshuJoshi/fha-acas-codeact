import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import compare_reasoning_runs as crr  # noqa: E402
import write_run_meta  # noqa: E402


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

    def test_run_meta_is_not_read_as_a_run_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_meta.json").write_text(json.dumps({"label": "x"}))
            (root / "run_001_m_1.json").write_text(json.dumps(_record("m", "p")))
            with contextlib.redirect_stderr(io.StringIO()) as err:
                records = crr._load_records([root])
        self.assertEqual(len(records), 1)
        self.assertNotIn("run_meta", err.getvalue())

    def test_malformed_url_in_generated_code_does_not_crash_the_summary(self):
        record = _record("m", "p")
        record["tool_calls"] = [{"tool": "execute_code", "code": "requests.get('http://[bad')"}]
        (summary,) = crr._summarize([record])
        self.assertEqual(summary["runs"], 1)

    def test_by_path_keeps_the_same_model_in_two_runs_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "aug", Path(tmp) / "oct"
            for folder, wall in ((old, 1000), (new, 3000)):
                folder.mkdir()
                (folder / "run_001.json").write_text(json.dumps(_record("m", "p", wall_ms=wall)))

            merged = crr._summarize(crr._load_records([old, new]))
            separate = crr._summarize(crr._load_by_path([old, new]))

        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(s["model"] for s in separate), ["m @ aug", "m @ oct"])
        self.assertEqual({s["model"]: s["mean_wall_s"] for s in separate}, {"m @ aug": 1.0, "m @ oct": 3.0})


class RunMetaTests(unittest.TestCase):
    def test_start_records_settings_and_prompt_hash_then_finish_closes_it(self):
        env = {"MODELS": "a,b", "API": "responses", "EFFORT": "medium", "GAP_S": "3", "PARALLEL": "1"}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "prompts.txt").write_text("one\ntwo\n")
            with mock.patch.dict("os.environ", env):
                write_run_meta.main(["start", str(out), "--", "--temperature", "0"])
            meta = json.loads((out / "run_meta.json").read_text())
            self.assertEqual(meta["status"], "running")
            self.assertEqual(meta["settings"]["reasoning_effort"], "medium")
            self.assertTrue(meta["settings"]["parallel"])
            self.assertEqual(meta["settings"]["extra_args"], ["--temperature", "0"])
            self.assertEqual(meta["prompts"]["count"], 2)
            self.assertEqual(len(meta["prompts"]["sha256"]), 64)

            write_run_meta.main(["finish", str(out), "ok"])
            meta = json.loads((out / "run_meta.json").read_text())
            self.assertEqual(meta["status"], "ok")
            self.assertIsNotNone(meta["ended_at"])


if __name__ == "__main__":
    unittest.main()

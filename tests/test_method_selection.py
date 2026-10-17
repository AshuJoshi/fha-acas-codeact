import importlib.util
import sys
import unittest
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent / "experiments" / "method-selection"
spec = importlib.util.spec_from_file_location("method_selection_run", EXP / "run_experiment.py")
exp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = exp
spec.loader.exec_module(exp)

BASE = """\
You are a CodeAct agent.

Use run_shell for environment setup, package installation, and file inspection.
Use execute_code for Python 3 code execution.

Rules:
- When the user asks for computation, data transformation, debugging, or code
  execution, prefer using the tools instead of answering from memory.
- Check tool results directly. Do not claim code ran successfully unless the
  tool output shows success.
- Sandbox state persists across calls in this conversation, including files and
  installed packages.
"""
NEUTRAL = (EXP / "instructions-neutral.txt").read_text()


class InstructionArmTests(unittest.TestCase):
    def test_arm_a_is_verbatim(self):
        self.assertEqual(exp.build_instructions("A", BASE, NEUTRAL), BASE)

    def test_arm_b_replaces_only_the_directives(self):
        b = exp.build_instructions("B", BASE, NEUTRAL)
        self.assertNotIn("prefer using the tools", b)
        self.assertNotIn("Use execute_code for Python 3 code execution", b)
        self.assertIn(NEUTRAL.strip(), b)
        for kept in ("You are a CodeAct agent.", "Rules:", "- Check tool results directly.", "- Sandbox state persists"):
            self.assertIn(kept, b)

    def test_arm_b_fails_if_source_instructions_drift(self):
        with self.assertRaises(RuntimeError):
            exp.build_instructions("B", BASE.replace("prefer using", "favor using"), NEUTRAL)


class ClassifyTests(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(exp.classify([]), "direct")
        self.assertEqual(exp.classify([{"code": "print(sum(range(10)))"}]), "execution")
        self.assertEqual(exp.classify([{"code": "import urllib.request"}]), "retrieval_via_code")
        self.assertEqual(
            exp.classify([{"command": "curl -s https://x.org"}, {"code": "print(1+1)"}]), "mixed"
        )

    def test_check_answer_ignores_thousands_separators(self):
        self.assertTrue(exp.check_answer("There are 78,498 primes.", r"\b78498\b"))
        self.assertFalse(exp.check_answer("About 78,000", r"\b78498\b"))
        self.assertIsNone(exp.check_answer("anything", None))

    def test_every_expectation_matches_a_prompt(self):
        self.assertTrue(exp.load_config())


if __name__ == "__main__":
    unittest.main()

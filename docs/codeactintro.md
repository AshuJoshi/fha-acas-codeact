## Why CodeAct + ACAS?

There are two ways to give an agent capability:

1. **A catalog of 5–10 tools** — `read_file`, `write_file`, `http_get`,
   `set_egress`, `run_sql`, … The LLM picks one per turn and emits a
   JSON tool call.
2. **One tool: `execute_code(code: str)`** — the LLM emits a Python
   snippet; the runtime runs it in a sandbox. Anything the agent wants
   to do, it does *inside* that Python.

The second shape is the **CodeAct pattern**
([Wang et al., 2024](https://arxiv.org/abs/2402.01030)). It wins on
five things that compound:

* **The attack surface shrinks ~10×.** One tool means one entry point
  to threat-model — and one route on whatever HTTP surface eventually
  fronts the agent (`POST /sessions/{id}/exec_python` instead of one
  route per primitive).
* **Python is the LLM's strongest channel.** Frontier models were
  trained on more Python than they were on any particular tool-call
  schema. Composition (`for x in xs: …`), conditionals, error
  handling, intermediate variables, library calls — all free,
  expressed in the language the model knows best.
* **The combinatorial blow-up disappears.** "Read these five files,
  grep for X, write a summary" is one snippet, one round-trip, one
  model call — not five `read_file` + five `regex_match` + one
  `write_file`. Latency and token cost grow with task complexity,
  not linearly with primitives.
* **Errors arrive as tracebacks the model already understands.** When
  a `dict` lookup raises `KeyError`, the model sees the traceback and
  knows immediately what went wrong, because Python tracebacks are
  dense in its training data — unlike a runtime's structured tool
  error.
* **"Tools" become Python libraries.** Want SQL? Make `psycopg2`
  importable and let the agent write `cursor.execute(…)`. Want
  HTTP? `requests`. Want data? `pandas`. The tool ecosystem becomes
  *what is installed in the sandbox image* — a problem your
  packaging system already solves.

The standard objection — *"giving the model `exec` is too
dangerous"* — assumes there's nothing strong underneath the
interpreter. With ACAS there is: each session runs in its own VM
with its own kernel, filesystem, network tap, and egress proxy. The
sandbox boundary holds, which lets the safety story live there
instead of in ten hand-curated tool gates. That is why this toolkit
picks the CodeAct shape as its default — ACAS is what makes the
shape credible.
# Benchmark prompt suites

Prompt suites in [`prompts/`](../prompts/) used with the local CodeAct benchmark
harness (`scripts/compare_models.py --prompts prompts/<suite>.txt`). One prompt
per line; blank lines are ignored. Each suite targets a different aspect of model
behaviour.

| Suite | File | Prompts | Purpose / what it exercises |
|---|---|---|---|
| **easy** | [easy.txt](../prompts/easy.txt) | 5 | Explicit, self-contained coding tasks (sum, Fibonacci, primes, CSV, string). Baseline latency/tokens. Same prompts as the built-in `DEFAULT_PROMPTS` in `compare_models.py`. |
| **hard** | [hard.txt](../prompts/hard.txt) | 8 | Harder imperative tasks: pip install, reproduce-then-fix a bug, iterate-until-condition, verification, timing, file-create-retry, adaptive perf, ambiguous cleanup. |
| **vague** | [vague.txt](../prompts/vague.txt) | 3 | Outcome questions with **no "write code" instruction** (ISS location, day-of-week, prime count). Tests whether the model *decides* to use CodeAct. |
| **outcome** | [outcome.txt](../prompts/outcome.txt) | 6 | Outcome/live-data questions requiring a real fetch (people in space, ISS, BTC price, xkcd, advice API) plus a dependency-recovery bait (use `requests`, likely missing, so install or fall back to `urllib`). Probes autonomous tool use, network, and multi-turn error/dependency recovery. |
| **volatile** | [volatile.txt](../prompts/volatile.txt) | 3 | The 3 prompts `volatility_report.py` flagged as the most solution-path-unstable / tail-event-prone across two dated batteries (ISS position, people-in-space, ISS+hemisphere). Meant to be run with `--repeats` to build a within-model repeat distribution. |
| **reasoning** | [reasoning.txt](../prompts/reasoning.txt) | 20 | Open-ended prompts that force a choice between trained recall, computation, and a live lookup: all 9 vague + outcome prompts, plus 11 new ones (currency exchange, UN Secretary-General, Tokyo time, Python version, seconds since Apollo 11, latest SpaceX launch, gold price, largest city population, landmark coordinates, marathon record, India vs China population). Run with `scripts/run_reasoning_overnight.sh` (batches of 5, 5 repeats). |

## How the suites overlap

- **reasoning** contains every prompt in **vague** (lines 1 to 3) and **outcome** (lines 4 to 9).
- **volatile** is a subset of **reasoning** (lines 1, 4, and 5).
- **easy** and **hard** do not overlap with any other suite.

Results for a shared prompt can be compared across suites, but only when the
harness settings (`--api`, temperature, date) match, because live-data answers
and external API health change over time.

## Closed-form prompts in the reasoning suite

Two reasoning prompts have a single computable answer: the day of the week for
1 January 2000 and the count of primes below one million. They are there because
the suite deliberately includes all 9 earlier prompts. No further closed-form
prompts were added: an earlier trial suite showed that such tasks converge on one
textbook algorithm and produce little solution-path variance, which is what this
suite is meant to measure.

## Early findings (July 2026; gpt-5.4, GLM-5.2, Kimi-K2.7-Code)

These predate the larger model set and have not been re-checked against it.

- **Turns:** explicit prompts (easy, hard) mostly collapsed to 2 turns (write
  code, read result, answer). Models folded "iteration" into a single script.
  Extra turns appeared only when the environment forced them: a package install,
  or an unexpected runtime error (for example a missing directory or library).
- **Tool use:** on the vague suite, all three models chose to use the tool.
- **Network:** the ACA Sandbox can make outbound HTTP calls (verified live against
  `http://api.open-notify.org/iss-now.json`).
- **Dependency recovery:** for the ISS prompt, Kimi tried `import requests`, hit
  `ModuleNotFoundError`, and fell back to stdlib `urllib.request` (3 turns).
- **API surface matters:** run with `--api chat` for token usage on all models
  (including Fireworks); `--api responses` matches the deployed FHA. See the
  README "Benchmark models locally" section.

## Run examples

```bash
uv run --extra compare python scripts/compare_models.py \
    --api chat --warmup --prompts prompts/hard.txt --out-dir benchmark-results/hard-suite

# Long batched run of the reasoning suite, then a per-model summary
mkdir -p benchmark-results/reasoning-run
scripts/run_reasoning_overnight.sh benchmark-results/reasoning-run
uv run python scripts/compare_reasoning_runs.py benchmark-results/reasoning-run
```

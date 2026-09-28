#!/usr/bin/env bash
# Runs a prompt suite through compare_models.py in batches, one subdirectory per
# batch, because compare_models.py restarts run_001 numbering on every call.
#
# Usage:
#   scripts/run_reasoning_overnight.sh OUT_DIR [extra compare_models.py args...]
#   Env: PROMPTS (default prompts/reasoning.txt), BATCH_SIZE (5), REPEATS (5)
#
# Unattended launch (OUT_DIR must exist before the shell opens the log file):
#   mkdir -p benchmark-results/my-run
#   nohup caffeinate -i scripts/run_reasoning_overnight.sh benchmark-results/my-run \
#       > benchmark-results/my-run/overnight.log 2>&1 &
#   disown
set -uo pipefail
cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
  echo "usage: $0 OUT_DIR [extra compare_models.py args...]" >&2
  exit 2
fi
OUT_ROOT="$1"
shift
PROMPTS="${PROMPTS:-prompts/reasoning.txt}"
BATCH_SIZE="${BATCH_SIZE:-5}"
REPEATS="${REPEATS:-5}"

if [[ ! -f "$PROMPTS" ]]; then
  echo "prompts file not found: $PROMPTS" >&2
  exit 2
fi
if ! [[ "$BATCH_SIZE" =~ ^[1-9][0-9]*$ && "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
  echo "BATCH_SIZE and REPEATS must be positive integers" >&2
  exit 2
fi
if compgen -G "$OUT_ROOT/batch*/run_*.json" > /dev/null; then
  echo "refusing to overwrite existing results in $OUT_ROOT; choose a new OUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUT_ROOT"
# Blank lines are dropped (as compare_models.py does) so batch slices line up.
grep -v '^[[:space:]]*$' "$PROMPTS" > "$OUT_ROOT/prompts.txt"
total=$(wc -l < "$OUT_ROOT/prompts.txt" | tr -d ' ')
if (( total == 0 )); then
  echo "no prompts in $PROMPTS" >&2
  exit 2
fi
batches=$(( (total + BATCH_SIZE - 1) / BATCH_SIZE ))
failed_batches=""

for (( i = 1; i <= batches; i++ )); do
  start=$(( (i - 1) * BATCH_SIZE + 1 ))
  end=$(( i * BATCH_SIZE < total ? i * BATCH_SIZE : total ))
  batch_dir="$OUT_ROOT/batch${i}"
  mkdir -p "$batch_dir"
  sed -n "${start},${end}p" "$OUT_ROOT/prompts.txt" > "$batch_dir/prompts.txt"

  echo "=== batch $i/$batches (prompts $start-$end) ==="
  cat "$batch_dir/prompts.txt"

  { time uv run --extra compare python scripts/compare_models.py --api chat \
      --warmup --gap-s 20 --repeats "$REPEATS" \
      --prompts "$batch_dir/prompts.txt" \
      --out-dir "$batch_dir" ${@+"$@"} \
    ; } 2>&1 | tee "$batch_dir/batch${i}.log"
  rc=${PIPESTATUS[0]}

  # Keep going so one crashed batch does not cost the rest of the night.
  if (( rc != 0 )); then
    echo "=== batch $i FAILED (exit $rc) ===" >&2
    failed_batches="$failed_batches $i"
  else
    echo "=== batch $i DONE ==="
  fi
done

if [[ -n "$failed_batches" ]]; then
  echo "FINISHED WITH FAILED BATCHES:$failed_batches" >&2
  exit 1
fi
echo "ALL BATCHES DONE"

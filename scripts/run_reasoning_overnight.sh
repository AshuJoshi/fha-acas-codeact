#!/usr/bin/env bash
# Runs a prompt suite through compare_models.py in batches, one subdirectory per
# batch, because compare_models.py restarts run_001 numbering on every call.
#
# Usage:
#   scripts/run_reasoning_overnight.sh OUT_DIR [extra compare_models.py args...]
#   Env: PROMPTS (default prompts/reasoning.txt), BATCH_SIZE (5), REPEATS (5),
#        API (chat|responses, default chat), GAP_S (seconds between runs, 20),
#        EFFORT (reasoning effort, needs API=responses, default unset),
#        MODELS (comma list, needed for PARALLEL), PARALLEL (1 = one process per
#        model), SNAPSHOT (0 = skip the deployment snapshots)
#
# PARALLEL=1 MODELS=a,b,c runs one process per model at the same time, each into
# OUT_DIR/<model>/batchN. Every model then sees the same time window and the run
# takes about as long as the slowest model. Use a short GAP_S (3 to 5): rate
# limits are per deployment, so one model's pacing does not slow the others.
#
# Before and after the run the deployment state is saved to
# OUT_DIR/deployments.before.json and .after.json and compared with
# scripts/deploy_comparison_model.py --check (a changed model or version is
# reported at the end and in deployments.check.txt).
#
# Pilot (every prompt once, all models in parallel):
#   mkdir -p benchmark-results/pilot
#   PARALLEL=1 MODELS=glm-5.3,kimi-k3 API=responses EFFORT=medium GAP_S=3 \
#     REPEATS=1 BATCH_SIZE=20 scripts/run_reasoning_overnight.sh benchmark-results/pilot
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
API="${API:-chat}"
GAP_S="${GAP_S:-20}"
EFFORT="${EFFORT:-}"
MODELS="${MODELS:-}"
PARALLEL="${PARALLEL:-0}"
SNAPSHOT="${SNAPSHOT:-1}"

if [[ ! -f "$PROMPTS" ]]; then
  echo "prompts file not found: $PROMPTS" >&2
  exit 2
fi
if ! [[ "$BATCH_SIZE" =~ ^[1-9][0-9]*$ && "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
  echo "BATCH_SIZE and REPEATS must be positive integers" >&2
  exit 2
fi
if ! [[ "$GAP_S" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  echo "GAP_S must be a non-negative number" >&2
  exit 2
fi
if [[ "$API" != "chat" && "$API" != "responses" ]]; then
  echo "API must be chat or responses" >&2
  exit 2
fi
if [[ -n "$EFFORT" && "$API" != "responses" ]]; then
  echo "EFFORT needs API=responses" >&2
  exit 2
fi
if [[ "$PARALLEL" == "1" && -z "$MODELS" ]]; then
  echo "PARALLEL=1 needs MODELS=comma,separated,deployment,names" >&2
  exit 2
fi
if compgen -G "$OUT_ROOT/batch*/run_*.json" > /dev/null \
   || compgen -G "$OUT_ROOT/*/batch*/run_*.json" > /dev/null; then
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

# run_batches OUT_DIR [extra compare_models.py args...]
# Runs every batch of the prompt suite into OUT_DIR/batchN; a failed batch is
# reported but does not stop the rest.
run_batches() {
  local out="$1" failed="" i start end batch_dir rc
  shift
  for (( i = 1; i <= batches; i++ )); do
    start=$(( (i - 1) * BATCH_SIZE + 1 ))
    end=$(( i * BATCH_SIZE < total ? i * BATCH_SIZE : total ))
    batch_dir="$out/batch${i}"
    mkdir -p "$batch_dir"
    sed -n "${start},${end}p" "$OUT_ROOT/prompts.txt" > "$batch_dir/prompts.txt"

    echo "=== batch $i/$batches (prompts $start-$end) ==="
    cat "$batch_dir/prompts.txt"

    { time uv run --extra compare python scripts/compare_models.py --api "$API" \
        --warmup --gap-s "$GAP_S" --repeats "$REPEATS" \
        --prompts "$batch_dir/prompts.txt" \
        --out-dir "$batch_dir" ${EFFORT:+--reasoning-effort "$EFFORT"} ${@+"$@"} \
      ; } 2>&1 | tee "$batch_dir/batch${i}.log"
    rc=${PIPESTATUS[0]}

    if (( rc != 0 )); then
      echo "=== batch $i FAILED (exit $rc) ===" >&2
      failed="$failed $i"
    else
      echo "=== batch $i DONE ==="
    fi
  done
  if [[ -n "$failed" ]]; then
    echo "FINISHED WITH FAILED BATCHES:$failed" >&2
    return 1
  fi
  return 0
}

if [[ "$SNAPSHOT" == "1" ]]; then
  uv run python scripts/deploy_comparison_model.py --snapshot "$OUT_ROOT/deployments.before.json" \
    || echo "WARNING: could not snapshot deployments; the end-of-run check will be skipped" >&2
fi

run_rc=0
if [[ "$PARALLEL" == "1" ]]; then
  IFS=',' read -r -a model_list <<< "$MODELS"
  pids=()
  echo "parallel: ${#model_list[@]} models, $(( total * REPEATS )) runs each, logs in $OUT_ROOT/<model>/run.log"
  for m in "${model_list[@]}"; do
    mkdir -p "$OUT_ROOT/$m"
    ( run_batches "$OUT_ROOT/$m" ${@+"$@"} --models "$m" ) > "$OUT_ROOT/$m/run.log" 2>&1 &
    pids+=("$!")
    sleep 2  # stagger starts so sandbox leases and auth are not all requested at once
  done
  tick=0
  while :; do
    alive=0
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    (( alive )) || break
    sleep 30
    tick=$(( tick + 1 ))
    if (( tick % 10 == 0 )); then  # progress line every 5 minutes
      printf '%s progress:' "$(date +%H:%M)"
      for m in "${model_list[@]}"; do
        printf ' %s=%s' "$m" "$(find "$OUT_ROOT/$m" -name 'run_*.json' 2>/dev/null | wc -l | tr -d ' ')"
      done
      echo
    fi
  done
  failed_models=""
  for idx in "${!pids[@]}"; do
    if wait "${pids[$idx]}"; then
      echo "=== ${model_list[$idx]} DONE ==="
    else
      echo "=== ${model_list[$idx]} FAILED ===" >&2
      failed_models="$failed_models ${model_list[$idx]}"
    fi
  done
  if [[ -n "$failed_models" ]]; then
    echo "FINISHED WITH FAILED MODELS:$failed_models" >&2
    run_rc=1
  fi
else
  run_batches "$OUT_ROOT" ${@+"$@"} || run_rc=1
fi

check_rc=0
if [[ "$SNAPSHOT" == "1" && -f "$OUT_ROOT/deployments.before.json" ]]; then
  uv run python scripts/deploy_comparison_model.py --snapshot "$OUT_ROOT/deployments.after.json" || true
  uv run python scripts/deploy_comparison_model.py --check "$OUT_ROOT/deployments.before.json" \
    | tee "$OUT_ROOT/deployments.check.txt"
  check_rc=${PIPESTATUS[0]}
  if (( check_rc != 0 )); then
    echo "WARNING: a deployment changed during the run; see $OUT_ROOT/deployments.check.txt" >&2
  fi
fi

if (( run_rc != 0 )); then
  exit 1
fi
echo "ALL BATCHES DONE"
exit "$check_rc"

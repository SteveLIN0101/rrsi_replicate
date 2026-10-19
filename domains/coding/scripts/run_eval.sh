#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Run the forked AgentHarness on a harbor dataset.
# Usage: run_eval.sh <job-name> [dataset] [n_attempts] [n_concurrent] [extra harbor flags...]
#   dataset default: terminal-bench/terminal-bench-2-1
#   extra flags: --jobs-dir <dir> (default <this checkout>/runs/jobs), -i <task> ...
# Model/infra invariants are pinned here (frozen; not evolvable).
set -euo pipefail
REPO="${RRSI_CODING_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
VENV="${RRSI_CODING_VENV:-$REPO/.venv}"
if [[ "${RRSI_ENV_LOADED:-}" != 1 ]]; then
  exec "$VENV/bin/python" "$REPO/../../rrsi/env.py" bash "$0" "$@"
fi
: "${OPENAI_BASE_URL:?set OPENAI_BASE_URL in the root .env}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY in the root .env}"
: "${OPENAI_MODEL_NAME:?set OPENAI_MODEL_NAME in the root .env}"

JOB_NAME="${1:?job name required}"
DATASET="${2:-terminal-bench/terminal-bench-2-1}"
N_ATTEMPTS="${3:-1}"
N_CONCURRENT="${4:-4}"
shift $(( $# > 4 ? 4 : $# ))

JOBS_DIR="$REPO/runs/jobs"
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jobs-dir) JOBS_DIR="$2"; shift 2;;
    *) EXTRA+=("$1"); shift;;
  esac
done

# The sudo shim is for Linux hosts; Docker Desktop uses the user's socket.
if [[ "$(uname -s)" == Linux ]]; then
  export PATH="$REPO/bin:$PATH"
fi
ROOT="$(cd "$REPO/../.." && pwd)"
export PYTHONPATH="$ROOT:$ROOT/third_party"

case "${RRSI_TOOL_IMAGES:-preinstalled}" in
  preinstalled) TOOL_ENV=rrsi.coding_environment:PreparedDockerEnvironment;;
  original) TOOL_ENV=docker;;
  *) echo 'RRSI_TOOL_IMAGES must be preinstalled or original' >&2; exit 2;;
esac
# A fresh output directory prevents Harbor resume/overwrite and mixed preparation.
if [[ -e "$JOBS_DIR/$JOB_NAME" ]]; then
  echo "Job already exists: $JOBS_DIR/$JOB_NAME; choose a new job name" >&2
  exit 2
fi
"$VENV/bin/python" -m rrsi.coding_provenance --jobs-dir "$JOBS_DIR"

exec "$VENV/bin/harbor" run \
  -d "$DATASET" \
  --env "$TOOL_ENV" \
  --agent rrsi.harbor_agent:AgentHarness \
  -m "openai/$OPENAI_MODEL_NAME" \
  --agent-kwarg "api_base=$OPENAI_BASE_URL" \
  --agent-kwarg 'use_responses_api=false' \
  --agent-kwarg 'llm_kwargs={"num_retries": 10}' \
  -k "$N_ATTEMPTS" \
  -n "$N_CONCURRENT" \
  --jobs-dir "$JOBS_DIR" \
  --job-name "$JOB_NAME" \
  ${EXTRA[@]+"${EXTRA[@]}"}

# Copyright 2026 The rrsi Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
"""Central config for the EngDesign instance.

Layout:

  third_party/archipelago/runner       FROZEN official archipelago runner. Never edited.
  third_party/archipelago/harness_eng  THE EVOLVABLE SCAFFOLD (main/tools/resum/tool_result/
                 prompts/mechanisms), loaded through RRSI_HARNESS_MODULE.
  mcp_gateway/   the tool ENVIRONMENT. Frozen.
  engdesign_bench/  the benchmark tree, built from the OFFICIAL EngDesign
                 checkout. Holds each task's frozen verifier. NEVER reachable
                 from the agent's sandbox.

WHY THIS BENCHMARK. Each task is graded by its own frozen code verifier that
returns a CONTINUOUS score (raw_score/score_max) for resolution, a BINARY
`passed` for the correctness headline, and a `valid` flag for the damage
guard, with no LLM anywhere in the loop. All per-task variance is therefore
policy variance.

WHAT THE SCAFFOLD IS EVOLVED ON, AND WHERE IT IS JUDGED. Evolution runs on all
61 tasks with no held-out split (the set is too small to spend tasks on one).
The verdict comes from two external surfaces that never enter selection:
EngDesign v1 (the same tasks with grading exploits closed, so a scaffold that
learned to game a verifier shows up as a drop) and Frontier-Eng minus its
EngDesign domain (a different benchmark's verifiers).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rrsi.env import litellm_model  # noqa: E402; loads root .env

# Keep this endpoint fixed across baseline and candidate evaluations.
ORCHESTRATOR_MODEL = litellm_model()
ORCHESTRATOR_EXTRA_ARGS: dict = {}


def policy_endpoints() -> list[dict]:
    return [{"model": ORCHESTRATOR_MODEL,
             "extra_args": {"api_base": os.environ.get("OPENAI_BASE_URL", "")}}]


POLICY_ENDPOINTS = policy_endpoints()

# ---- Paths -------------------------------------------------------------------
REPO = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.environ.get("BENCH_ROOT", os.path.join(REPO, "engdesign_bench"))
BENCH_TASKS = os.path.join(BENCH_ROOT, "benchmarks")
GRADING_PYTHON = os.environ.get(
    "GRADING_PYTHON", os.path.join(REPO, ".venvs", "engdesign", "bin", "python"))
WORKSPACE_BASE = os.environ.get("WORKSPACE_BASE", os.path.join(REPO, "workspaces"))
RUNS = os.environ.get("RRSI_RUNS_DIR", os.path.join(REPO, "runs"))
SPLIT_PATH = os.environ.get("RRSI_SPLIT_PATH", os.path.join(REPO, "data", "split_engd.json"))

# The graded deliverable. Every task's frontier_eval/candidate_destination.txt
# names this same path, and it is the ONLY thing the verifier reads.
CANDIDATE_REL = "submission/payload.py"

# ---- MCP gateway -------------------------------------------------------------
GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8994"))
GATEWAY_URL = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/mcp/"

# ---- Agent config ------------------------------------------------------------
AGENT_CONFIG_VALUES = {
    "max_steps": int(os.environ.get("MAX_STEPS", "250")),
    "timeout": int(os.environ.get("AGENT_TIMEOUT", "5400")),
    "llm_response_timeout": int(os.environ.get("LLM_RESPONSE_TIMEOUT", "600")),
}
# A trial that exceeds this comes back with no payload and SCORES ZERO. That is
# deliberate and must stay that way for the baseline and every candidate:
# refilling only the trials that failed would launder the score upward, and
# asymmetrically, since a candidate that issues more model calls hits more rate
# limits and more timeouts -- exactly the cost the token guard measures.
TIMEOUT_PER_TASK = int(os.environ.get("TIMEOUT_PER_TASK", "5400"))

# ---- Verifier ----------------------------------------------------------------
# Per-task wall clock for the frozen verifier. The verifiers run in seconds,
# so this is a crash backstop, not a budget.
VERIFY_TIMEOUT = int(os.environ.get("VERIFY_TIMEOUT", "420"))
VERIFY_JOBS = int(os.environ.get("VERIFY_JOBS", "12"))

# ---- Concurrency -------------------------------------------------------------
# Must match the concurrency the BASELINE was measured at: a champion and a
# candidate measured at different concurrency are not comparable.
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "12"))

# ---- Eval protocol -----------------------------------------------------------
# k=4 is the floor imposed by calibration: bench/calibrate.py measures the null
# by splitting the baseline's own trials 2-vs-2 on the same tasks, which needs
# at least four. It must match the n data/metric.json was calibrated at.
N_ATTEMPTS = int(os.environ.get("N_ATTEMPTS", "4"))

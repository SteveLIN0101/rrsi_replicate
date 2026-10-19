# Coding instance: Terminal-Bench 2.1

* Evolve set: all 89 Terminal-Bench 2.1 tasks (`data/tb21_tasks.txt`), `k = 2` trials per task.
* Starting harness H_0: the Terminus-2 agent from harbor, `third_party/harbor_terminus2/` (`harbor_terminus2:AgentHarness`).
* Score: fraction of trials whose hidden unit tests pass; a missing or crashed trial counts as a failure.
* Out-of-distribution test: SWE-bench Verified (500 instances, resolve rate).

## Setup

* Docker. The harness drives each task's container through a tmux session. macOS uses Docker Desktop directly. On Linux, `bin/docker` wraps `sudo -E docker`; edit it if your user is in the `docker` group.
* harbor: `uv venv --python 3.12 domains/coding/.venv && uv pip install --python domains/coding/.venv/bin/python -e ".[coding]"` (or point `RRSI_CODING_PYTHON` / `RRSI_CODING_VENV` at an existing environment). harbor pulls `terminal-bench/terminal-bench-2-1` and `swe-bench/swe-bench-verified` from its registry on first use.
* Configure `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL_NAME` in the repository root `.env`. The adapter, smoke check and standalone evaluation scripts use the same model and Chat Completions endpoint.
* `rrsi.harbor_agent` buffers streamed Chat Completions before Harbor parses them. This avoids idle timeouts on long responses and preserves token usage without changing the candidate harness.

## Recording-tool images

New runs default to `RRSI_TOOL_IMAGES=preinstalled`. The repository's
`rrsi.coding_environment:PreparedDockerEnvironment` derives a separate
`rrsi-tools:<content-hash>` image from the task's exact original image ID. It
installs only tmux, asciinema and their required runtime dependencies with
`--no-install-recommends`. It does not change the original tag, task files,
working directory, entrypoint or command. All 89 cached Terminal-Bench 2.1 task
definitions use Debian/Ubuntu prebuilt images, which this implementation supports.
Other package managers and Dockerfile-only tasks fail explicitly; select original
mode for those tasks.

Warm only task images already downloaded (safe to repeat, no task/model calls):

```bash
domains/coding/.venv/bin/python -m rrsi.tool_images --local-tasks \
  --output runs/coding/tool-images/warmup.json
```

Remaining images are prepared on demand before their task container starts.
The default cache is the main checkout's `runs/coding/tool-images`, shared by
linked worktrees; `RRSI_TOOL_IMAGE_CACHE` can point to another shared local path.
Keep the same cache for baseline and evolution. Process locks serialize the
same source image/recipe, including concurrent jobs. Existing prepared image
IDs are reused directly, so subsequent containers make no tool-install calls.

BuildKit shares only apt archives, downloaded indexes and the two-tool version
lock. Cache keys distinguish distribution, release, architecture, repository
configuration and preparation recipe. Archive filenames include package version
and architecture, and apt verifies their hashes. `sharing=locked` protects
concurrent cache use ([Docker cache documentation](https://docs.docker.com/build/cache/optimize/)).
Neither `/usr`, `/var/lib/dpkg`, task directories nor container writable layers
are shared. Each trial retains Harbor's normal independent container and mounts.

Builds have a 900-second ceiling, downloads have 30-second connection timeouts
and two retries, and lock waits are bounded. Harbor's task-specific environment
build deadline still applies to on-demand preparation; prewarm large images
before launching a baseline. `build.log` is retained on failure. The original
installer's 120-second command and 240-second total limits remain; its native
`TimeoutError` and Harbor-wrapped timeout `RuntimeError` now produce a clear
preparation failure and diagnostic JSON. A missing tmux never counts as success.

## Separate results and reproduce preparation

Use a **new runs root** for this preparation policy. Existing output directories
are never overwritten or automatically renamed. The runner rejects a changed
policy or recipe in an existing runs root; per-task image IDs are also locked
across jobs, so a baseline and evolved candidate cannot silently use different
images. Do not combine these results with older runtime-install baselines.

```bash
# Two real priority tasks, one at a time; a new job name is required each run.
bash domains/coding/scripts/run_eval.sh tools-check terminal-bench/terminal-bench-2-1 1 1 \
  --jobs-dir "$PWD/runs/coding-preinstalled/jobs" \
  -i terminal-bench/torch-tensor-parallelism -i terminal-bench/dna-assembly

# After committing the infrastructure change so worktrees contain it:
.venv/bin/python rrsi.py --domain coding --runs "$PWD/runs/preinstalled" baseline
.venv/bin/python rrsi.py --domain coding --runs "$PWD/runs/preinstalled" run

# Opt out: original task images plus the bounded runtime installer.
RRSI_TOOL_IMAGES=original bash domains/coding/scripts/run_eval.sh original-check \
  terminal-bench/terminal-bench-2-1 1 1 \
  --jobs-dir "$PWD/runs/coding-original/jobs" -i terminal-bench/dna-assembly
```

Each prepared trial saves `tool-image.json` (source/derived IDs, platform, recipe,
tool/package versions, reuse and timing) and `tool-preparation.json` (actual
version checks, stdout/stderr, timeout tracebacks and timing). The cache retains
`manifest.json`, `Dockerfile`, `build.log` and before/after package inventories.
`environment-policy.json` and `environment-images.json` next to `jobs/` record
the comparison contract. Preserve them along with results and prepared images.
Deleting images/caches can require rebuilding; an image-ID change then requires
a new runs root and baseline rather than reusing old scores.

To verify preparation twice per task without calling a model, pass cached task
directories (the directories containing `task.toml`) to:

```bash
domains/coding/.venv/bin/python domains/coding/scripts/verify_tool_images.py \
  --output runs/tool-setup-check /absolute/task/directory /absolute/other/task/directory
.venv/bin/python -m pytest -q tests/test_tool_images.py
```

This starts fresh containers, opens tmux, records a command with asciinema and
asserts no package-install commands ran. It removes only its own temporary
containers. Existing experiments continue unchanged; applying the new policy to
them requires a separate run, not an in-place restart.

If the host DNS returns no IPv4 addresses and task/verifier downloads fail,
`PreparedDockerEnvironment` accepts an explicit per-container DNS override:

```bash
bash domains/coding/scripts/run_eval.sh dns-check terminal-bench/terminal-bench-2-1 1 2 \
  --jobs-dir "$PWD/runs/coding-dns-check/jobs" \
  --environment-kwarg 'dns_servers=["1.1.1.1","8.8.8.8"]' \
  -i terminal-bench/torch-tensor-parallelism -i terminal-bench/dna-assembly
```

This does not change host DNS, images or other projects. The default remains
Docker's inherited DNS. Use a fresh output root when changing DNS; the override
is recorded in `tool-image.json`, and scoring rejects mixed DNS policies.
Retain results from failed verifier dependency installation as infrastructure
failures, rather than interpreting their zero rewards as model task failures.

## Run

```bash
python3 rrsi.py --domain coding smoke        # two tasks, one trial each
python3 rrsi.py --domain coding baseline     # 89 tasks x k=2 = 178 trials
python3 rrsi.py --domain coding run          # T=20 rounds, 2 candidates per round
bash domains/coding/scripts/swe_eval.sh      # OOD: H_0 and the incumbent on SWE-bench Verified, then scripts/swe_summary.py
```

One harbor job per evaluation: `runs/coding/jobs/<job>/<task>__<hash>/` holds the trajectory, the terminal recording and the verifier result. A task whose container never came up is an infrastructure failure, not a harness failure: the round is marked invalid and re-measured once. After killing a run, `scripts/cleanup_docker.sh` removes leftover task containers and compose networks; leftover networks exhaust Docker's address pool and every later evaluation fails.

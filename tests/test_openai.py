"""Offline protocol and process-boundary tests; no provider credentials needed."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import threading

import pytest

from rrsi import llm
from rrsi.config import RRSIConfig
from rrsi.env import require_openai_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("harness", ["harness_workspace.main", "harness_eng.main"])
def test_agentic_harness_imports(harness):
    pytest.importorskip("fastmcp")
    result = subprocess.run(
        [sys.executable, "-c", f"import {harness}"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "third_party" / "archipelago")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def completion(content="OK", finish_reason="stop", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
            "model": "team/custom-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}


def stream_chunks(response):
    base = {k: response[k] for k in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"
    choice = response["choices"][0]
    message = choice["message"]
    for half in range(2):
        delta = {"role": "assistant"} if half == 0 else {}
        content = message.get("content") or ""
        cut = len(content) // 2
        delta["content"] = content[:cut] if half == 0 else content[cut:]
        if message.get("tool_calls"):
            calls = []
            for i, tool in enumerate(message["tool_calls"]):
                args = tool["function"]["arguments"]
                cut = len(args) // 2
                fn = {"arguments": args[:cut] if half == 0 else args[cut:]}
                call = {"index": i, "function": fn}
                if half == 0:
                    fn["name"] = tool["function"]["name"]
                    call.update(id=tool["id"], type="function")
                calls.append(call)
            delta["tool_calls"] = calls
        yield {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    if choice["finish_reason"]:
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]}
    yield {**base, "choices": [], "usage": response["usage"]}


@pytest.fixture
def endpoint(monkeypatch):
    requests, replies = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            status, response = replies.pop(0) if replies else (200, completion())
            streaming = status == 200 and body.get("stream")
            if streaming:
                data = ("".join(f"data: {json.dumps(c)}\n\n" for c in stream_chunks(response))
                        + "data: [DONE]\n\n").encode()
            else:
                data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "team/custom-model")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.delenv("LITELLM_PROXY_API_BASE", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    yield requests, replies
    if llm._client is not None:
        llm._client.close()
    server.shutdown()
    server.server_close()
    thread.join()


def test_search_chat_protocol_and_json(endpoint):
    requests, replies = endpoint
    replies.append((200, completion('```json\n{"ok": true}\n```')))
    assert json.loads(llm.generate("task", system="rules", cache_prefix="source",
                                   json_only=True, max_tokens=42)) == {"ok": True}
    path, auth, body = requests[0]
    assert path == "/v1/chat/completions" and auth == "Bearer test-key"
    assert body["model"] == "team/custom-model" and body["max_tokens"] == 42
    assert body["stream"] is True
    assert body["messages"][0]["role"] == "system"
    assert "rules" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "source\n\ntask"}
    assert "cache_control" not in json.dumps(body)


def test_retry_and_token_parameter_compatibility(endpoint):
    requests, replies = endpoint
    replies.extend([(429, {"error": {"message": "busy", "type": "rate_limit_error"}}),
                    (400, {"error": {"message": "Use max_completion_tokens instead of max_tokens",
                                      "type": "invalid_request_error"}}),
                    (200, completion())])
    assert llm.generate("hello", max_tokens=25, max_retries=2) == "OK"
    assert len(requests) == 3
    assert requests[-1][2]["max_completion_tokens"] == 25
    assert "max_tokens" not in requests[-1][2]


def test_auth_errors_are_not_retried(endpoint):
    requests, replies = endpoint
    replies.append((401, {"error": {"message": "invalid key"}}))
    with pytest.raises(llm.APIStatusError):
        llm.generate("hello")
    assert len(requests) == 1


def test_empty_and_truncated_responses(endpoint):
    requests, replies = endpoint
    replies.extend([(200, completion("")), (200, completion("OK"))])
    assert llm.generate("hello", max_retries=2) == "OK"
    replies.append((200, completion('{"unfinished":', "length")))
    with pytest.raises(ValueError, match="token limit"):
        llm.generate("hello", json_only=True)
    assert len(requests) == 3


def test_incomplete_stream_is_not_returned_as_success(endpoint):
    _, replies = endpoint
    replies.extend([(200, completion("partial", finish_reason=None)), (200, completion("complete"))])
    assert llm.generate("hello", max_retries=2) == "complete"


def test_async_incomplete_stream_is_rejected(endpoint):
    litellm = pytest.importorskip("litellm")
    from rrsi.chat_transport import acompletion
    _, replies = endpoint
    replies.append((200, completion("partial", finish_reason=None)))
    with pytest.raises(litellm.APIConnectionError, match="finish reason"):
        asyncio.run(acompletion(model="openai/team/custom-model",
                               messages=[{"role": "user", "content": "hello"}],
                               api_base=os.environ["OPENAI_BASE_URL"]))


def test_configuration_overrides_legacy_models(endpoint, tmp_path, monkeypatch):
    monkeypatch.setenv("RRSI_SEARCH_MODEL", "old-search")
    monkeypatch.setenv("ORCHESTRATOR_MODEL", "vertex_ai/old-policy")
    path = tmp_path / "rrsi.json"
    path.write_text(json.dumps({"proposer_model": "old", "analyst_model": "old",
                                "critic_model": "old", "T": 3}))
    cfg = RRSIConfig.load(path, critic_model="old-override")
    assert (cfg.proposer_model, cfg.analyst_model, cfg.critic_model) == ("team/custom-model",) * 3
    assert cfg.T == 3 and "test-key" not in json.dumps(cfg.dump())
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        require_openai_env()


def test_env_loader_outside_repo_and_child_inheritance(tmp_path):
    repo = tmp_path / "repo"
    (repo / "rrsi").mkdir(parents=True)
    shutil.copy(ROOT / "rrsi/env.py", repo / "rrsi/env.py")
    (repo / ".env").write_text('OPENAI_BASE_URL=https://example.test/v1\n'
                               'OPENAI_API_KEY="a key with spaces"\nOPENAI_MODEL_NAME=from-file\n')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENAI_", "RRSI_ENV_"))}
    env["OPENAI_MODEL_NAME"] = "process-wins"
    code = "import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith('OPENAI_')}))"
    result = subprocess.run([sys.executable, str(repo / "rrsi/env.py"), sys.executable, "-c", code],
                            cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values == {"OPENAI_BASE_URL": "https://example.test/v1",
                      "OPENAI_API_KEY": "a key with spaces", "OPENAI_MODEL_NAME": "process-wins"}


def test_linked_worktree_reads_main_env(tmp_path):
    repo, worktree = tmp_path / "repo", tmp_path / "candidate"
    (repo / "rrsi").mkdir(parents=True)
    shutil.copy(ROOT / "rrsi/env.py", repo / "rrsi/env.py")
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("OPENAI_BASE_URL=https://example.test/v1\n"
                               "OPENAI_API_KEY=test-key\nOPENAI_MODEL_NAME=worktree-model\n")
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init")
    git("add", "rrsi/env.py", ".gitignore")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "fixture")
    git("worktree", "add", "--detach", str(worktree))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "RRSI_ENV_"))}
    result = subprocess.run([sys.executable, str(worktree / "rrsi/env.py"), sys.executable,
                             "-c", "import os; print(os.environ['OPENAI_MODEL_NAME'])"],
                            cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "worktree-model"
    assert not (worktree / ".env").exists()


@pytest.mark.parametrize("extra", [[], ["--jobs-dir", "/tmp/jobs with spaces", "-i", "terminal-bench/test"]])
def test_coding_shell_passes_endpoint_without_vertex(endpoint, tmp_path, extra):
    bindir = tmp_path / "venv/bin"
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    harbor = bindir / "harbor"
    harbor.write_text(f"#!{sys.executable}\nimport os,sys,json\n"
                      "print(json.dumps({'args':sys.argv[1:], 'key':os.environ['OPENAI_API_KEY']}))\n")
    harbor.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VERTEX", "RRSI_ENV_"))}
    env["RRSI_CODING_VENV"] = str(bindir.parent)
    result = subprocess.run(["bash", str(ROOT / "domains/coding/scripts/run_eval.sh"),
                             "test", "dataset", "1", "2", *extra],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["key"] == "test-key"
    args = output["args"]
    assert args[args.index("-m") + 1] == "openai/team/custom-model"
    assert args[args.index("--agent") + 1] == "rrsi.harbor_agent:AgentHarness"
    assert args[args.index("--env") + 1] == "rrsi.coding_environment:PreparedDockerEnvironment"
    assert f"api_base={os.environ['OPENAI_BASE_URL']}" in args
    assert "use_responses_api=false" in args
    assert "test-key" not in " ".join(args)
    if extra:
        assert "/tmp/jobs with spaces" in args and args[-2:] == ["-i", "terminal-bench/test"]


def test_workspace_judge_and_filename_matching(endpoint, tmp_path):
    from domains.workspace.openai_judge import OpenAIJudge, match_deliverables
    requests, replies = endpoint
    (tmp_path / "rubric_criterion.txt").write_text("Task: {task_description}")
    replies.extend([(200, completion('{"verdict":"maybe"}')),
                    (200, completion('{"verdict":"pass","reasoning":"complete"}'))])
    judge = OpenAIJudge(tmp_path)
    assert judge.evaluate_from_file("rubric_criterion", {"task_description": "test"})["verdict"] == "pass"
    assert all(r[2]["model"] == "team/custom-model" for r in requests)
    (tmp_path / "actual.txt").write_text("deliverable contents")
    replies.append((200, completion('{"expected.txt":"actual.txt","unknown":"bad.txt"}')))
    assert match_deliverables({"expected.txt": "expected.txt"}, ["actual.txt"], tmp_path,
                              Path.read_text) == {"expected.txt": "actual.txt"}


def test_archipelago_tool_call_uses_chat_completions(endpoint, monkeypatch):
    pytest.importorskip("datadog_api_client", reason="install rrsi[agentic] for runner tests")
    monkeypatch.syspath_prepend(str(ROOT / "third_party/archipelago"))
    runner = importlib.import_module("runner.utils.llm")
    monkeypatch.setattr(runner.settings, "LITELLM_PROXY_API_BASE", "https://legacy.example")
    monkeypatch.setattr(runner.settings, "LITELLM_PROXY_API_KEY", "legacy-key")
    requests, replies = endpoint
    tool = {"id": "call-test", "type": "function",
            "function": {"name": "check", "arguments": '{"value": 1}'}}
    replies.append((200, completion(None, "tool_calls", [tool])))
    response = asyncio.run(runner.generate_response(
        model="openai/team/custom-model", messages=[{"role": "user", "content": "check"}],
        tools=[{"type": "function", "function": {"name": "check", "parameters": {"type": "object"}}}],
        llm_response_timeout=10, extra_args={"api_base": os.environ["OPENAI_BASE_URL"]}))
    assert response.choices[0].message.tool_calls[0].id == "call-test"
    assert json.loads(response.choices[0].message.tool_calls[0].function.arguments) == {"value": 1}
    assert requests[0][0] == "/v1/chat/completions"
    assert requests[0][1] == "Bearer test-key"
    assert requests[0][2]["model"] == "team/custom-model"
    assert requests[0][2]["stream"] is True
    assert response.usage.total_tokens == 12


def test_harbor_uses_chat_completions(endpoint, monkeypatch):
    harbor = pytest.importorskip("harbor.llms.lite_llm", reason="install rrsi[coding]")
    monkeypatch.syspath_prepend(str(ROOT / "third_party"))
    importlib.import_module("rrsi.harbor_agent")
    requests, _ = endpoint
    client = harbor.LiteLLM(model_name="openai/team/custom-model",
                           api_base=os.environ["OPENAI_BASE_URL"], use_responses_api=False)
    response = asyncio.run(client.call("Reply OK"))
    assert response.content == "OK"
    assert requests[0][0] == "/v1/chat/completions"
    assert requests[0][1] == "Bearer test-key"
    assert requests[0][2]["model"] == "team/custom-model"
    assert requests[0][2]["stream"] is True


def test_domain_model_configuration(endpoint, monkeypatch):
    pytest.importorskip("litellm", reason="install rrsi[agentic]")
    monkeypatch.setenv("ORCHESTRATOR_MODEL", "vertex_ai/old-policy")
    eng = importlib.import_module("domains.eng.config")
    importlib.reload(eng)
    assert eng.POLICY_ENDPOINTS == [{"model": "openai/team/custom-model",
                                    "extra_args": {"api_base": os.environ["OPENAI_BASE_URL"]}}]
    workspace = importlib.import_module("domains.workspace.workspace_driver")
    importlib.reload(workspace)
    assert workspace.ORCHESTRATOR_MODEL == "openai/team/custom-model"

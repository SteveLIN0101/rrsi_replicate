"""Recording-tool preparation contract, without Docker or an API credential."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'third_party'))


def session(tmp_path, *, preinstalled=False, missing=False):
    from rrsi.coding_session import PreparedTmuxSession
    calls = []
    async def execute(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(return_code=127 if missing else 0, stdout='version', stderr='missing' if missing else '')
    env = SimpleNamespace(exec=execute, tools_preinstalled=preinstalled,
                          trial_paths=SimpleNamespace(trial_dir=tmp_path))
    result = PreparedTmuxSession('test', env, Path('/logs/pane'), None, Path('/logs/recording.cast'))
    return result, calls


@pytest.mark.parametrize('error', [TimeoutError(), RuntimeError('Command timed out after 120 seconds')])
def test_timeout_is_diagnostic_failure(tmp_path, error):
    from rrsi.coding_session import ToolPreparationError
    obj, _ = session(tmp_path)
    async def fail():
        raise error
    obj._install_recording_tools = fail
    with pytest.raises(ToolPreparationError, match='timed out'):
        asyncio.run(obj._attempt_tmux_installation())
    assert 'timeout' in (tmp_path/'tool-preparation.json').read_text()


def test_unrelated_runtime_error_not_misclassified(tmp_path):
    obj, _ = session(tmp_path)
    async def fail():
        raise RuntimeError('permission denied')
    obj._install_recording_tools = fail
    with pytest.raises(RuntimeError, match='permission denied'):
        asyncio.run(obj._attempt_tmux_installation())
    data = json.loads((tmp_path/'tool-preparation.json').read_text())
    assert data['status'] == 'error'


def test_preinstalled_never_downloads(tmp_path):
    obj, calls = session(tmp_path, preinstalled=True)
    async def forbidden():
        pytest.fail('attempted runtime install')
    obj._install_recording_tools = forbidden
    asyncio.run(obj._attempt_tmux_installation())
    assert calls == ['tmux -V', 'asciinema --version']


def test_missing_tmux_is_not_success(tmp_path):
    from rrsi.coding_session import ToolPreparationError
    obj, calls = session(tmp_path, preinstalled=True, missing=True)
    with pytest.raises(ToolPreparationError, match='tmux'):
        asyncio.run(obj._attempt_tmux_installation())
    assert json.loads((tmp_path/'tool-preparation.json').read_text())['status'] == 'error'


def test_image_cache_reuses_and_pins_reference(tmp_path, monkeypatch):
    from rrsi import tool_images as tools
    ref = 'example/task:mutable'
    record = {'original_reference': ref, 'original_id': 'sha256:original',
              'derived_id': 'sha256:prepared'}
    path = tmp_path/tools.recipe_id()/'refs'/(tools.digest(ref)+'.json')
    tools.atomic_json(path, record)
    calls = []
    def inspect(image):
        calls.append(image)
        return {'Id': image}
    monkeypatch.setattr(tools, 'inspect', inspect)
    monkeypatch.setattr(tools, 'run_image', lambda *args: pytest.fail('repeated preparation'))
    actual = tools.prepare(ref, cache=tmp_path)
    assert actual['reused'] and actual['derived_id'] == 'sha256:prepared'
    assert calls == ['sha256:prepared']


def test_concurrent_callers_share_one_preparation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from rrsi import tool_images as tools
    ref = 'example/task:tag'
    ready = threading.Event()
    path = tmp_path/tools.recipe_id()/'refs'/(tools.digest(ref)+'.json')
    def producer():
        with tools.locked(path.with_suffix('.lock')):
            ready.set()
            time.sleep(.2)
            tools.atomic_json(path, {'derived_id': 'sha256:ready'})
    monkeypatch.setattr(tools, 'inspect', lambda image: {'Id': image})
    monkeypatch.setattr(tools, 'run_image', lambda *args: pytest.fail('duplicate build'))
    with ThreadPoolExecutor(max_workers=3) as pool:
        future = pool.submit(producer)
        assert ready.wait(2)
        other = [pool.submit(tools.prepare, ref, cache=tmp_path) for _ in range(2)]
        future.result()
        assert all(f.result()['reused'] for f in other)


def test_policy_rejects_old_results_and_image_drift(tmp_path, monkeypatch):
    from rrsi.coding_provenance import ensure_run_policy, register_image
    from rrsi.tool_images import atomic_json
    monkeypatch.setenv('RRSI_TOOL_IMAGES', 'preinstalled')
    atomic_json(tmp_path/'jobs/old/trial/result.json', {})
    with pytest.raises(ValueError, match='different preparation policy'):
        ensure_run_policy(tmp_path)
    new = tmp_path/'new'
    assert ensure_run_policy(new)['mode'] == 'preinstalled'
    record = {'recipe':'one', 'original_id':'base', 'derived_id':'derived', 'platform':'linux/amd64'}
    register_image(new/'jobs', 'task', record)
    register_image(new/'jobs', 'task', record)
    with pytest.raises(ValueError, match='identity changed'):
        register_image(new/'jobs', 'task', {**record, 'derived_id':'drifted'})
    monkeypatch.setenv('RRSI_TOOL_IMAGES', 'original')
    with pytest.raises(ValueError, match='policy changed'):
        ensure_run_policy(new)


@pytest.mark.parametrize('dns_servers', [None, ['1.1.1.1', '8.8.8.8']])
def test_derived_environment_preserves_task_and_uses_pinned_image(tmp_path, monkeypatch, dns_servers):
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths
    import rrsi.coding_environment as mod
    trial = tmp_path/'jobs/job/trial'
    trial.mkdir(parents=True)
    env_dir = tmp_path/'task/environment'
    env_dir.mkdir(parents=True)
    config = EnvironmentConfig(docker_image='original:tag')
    record = {'recipe': 'r', 'original_id': 'sha256:base', 'derived_id': 'sha256:derived',
              'platform':'linux/amd64'}
    monkeypatch.setattr(mod, 'prepare', lambda ref: dict(record))
    calls=[]
    async def start(self, force_build):
        calls.append(force_build)
        assert self.task_env_config.docker_image == 'sha256:derived'
        text = self._docker_compose_paths[-1].read_text()
        assert 'pull_policy: never' in text and 'build: !reset null' in text
        assert ('    dns:' in text) == bool(dns_servers)
        if dns_servers:
            assert json.loads(text.split('    dns: ')[1]) == dns_servers
    monkeypatch.setattr(mod.DockerEnvironment, 'start', start)
    env = mod.PreparedDockerEnvironment(environment_dir=env_dir, environment_name='task',
          session_id='test', trial_paths=TrialPaths(trial), task_env_config=config,
          dns_servers=dns_servers)
    asyncio.run(env.start(False))
    assert config.docker_image == 'original:tag' and calls == [False]
    assert not list(env_dir.iterdir())
    assert json.loads((trial/'tool-image.json').read_text())['status'] == 'ready'
    assert json.loads((trial/'tool-image.json').read_text())['dns_servers'] == (dns_servers or [])


@pytest.mark.parametrize('invalid', ['1.1.1.1', ['not-an-ip']])
def test_invalid_dns_rejected_before_container_creation(invalid):
    from rrsi.coding_environment import PreparedDockerEnvironment
    with pytest.raises(ValueError):
        PreparedDockerEnvironment(dns_servers=invalid)


def test_existing_incomplete_job_is_never_moved(tmp_path, monkeypatch):
    from domains.coding import adapter
    monkeypatch.setattr(adapter, 'require_openai_env', lambda: None)
    monkeypatch.setenv('RRSI_TOOL_IMAGES', 'preinstalled')
    job = tmp_path/'jobs/active'
    job.mkdir(parents=True)
    marker = job/'still-running.log'
    marker.write_text('keep')
    with pytest.raises(ValueError, match='may still be running'):
        adapter.CodingDomain()._harbor(ROOT, tmp_path, 'active', ['task'], 1, 'dataset', '')
    assert marker.read_text() == 'keep'
    assert not list(job.parent.glob('*.stale.*'))


def test_scoring_rejects_mixed_preparation(tmp_path, monkeypatch):
    from domains.coding import adapter
    from rrsi.tool_images import atomic_json
    for name in ['old', 'new']:
        path = tmp_path/'jobs/mixed'/name
        atomic_json(path/'result.json', {'task_name':'task', 'verifier_result':{'rewards':{'reward':1}}})
    atomic_json(tmp_path/'jobs/mixed/new/tool-image.json', {'mode':'preinstalled','recipe':'new'})
    with pytest.raises(ValueError, match='different environment policies'):
        adapter.CodingDomain().score(tmp_path, 'mixed', ['task'], 2)


def test_scoring_rejects_mixed_dns_settings(tmp_path):
    from domains.coding import adapter
    from rrsi.tool_images import atomic_json
    for name, dns in [('default', []), ('override', ['1.1.1.1'])]:
        path = tmp_path/'jobs/mixed'/name
        atomic_json(path/'result.json', {'task_name': 'task', 'verifier_result': {'rewards': {'reward': 1}}})
        atomic_json(path/'tool-image.json', {'mode': 'preinstalled', 'recipe': 'same', 'dns_servers': dns})
    with pytest.raises(ValueError, match='different environment policies'):
        adapter.CodingDomain().score(tmp_path, 'mixed', ['task'], 2)


def test_tool_preparation_failure_is_infrastructure_not_a_scored_attempt():
    from domains.coding.adapter import infra_failure
    assert infra_failure({'exception_info':{'exception_type':'ToolPreparationError',
                                           'exception_message':'Required tool unavailable: tmux -V'}})

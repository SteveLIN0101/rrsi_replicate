#!/usr/bin/env python3
"""Exercise real Harbor setup twice per image; no model, downloads or verifier."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/'third_party')]
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from rrsi.coding_environment import PreparedDockerEnvironment
from rrsi.coding_session import PreparedTmuxSession
from rrsi.tool_images import atomic_json


async def verify(task_path, output):
    data = tomllib.loads((task_path/'task.toml').read_text())
    config = EnvironmentConfig.model_validate(data['environment'])
    records = []
    for attempt in range(2):
        trial = output / 'jobs' / 'environment-only' / f'{task_path.parent.name}-{attempt}'
        trial.mkdir(parents=True, exist_ok=False)
        env = PreparedDockerEnvironment(environment_dir=task_path/'environment',
            environment_name=task_path.parent.name, session_id='rrsi-tools-check-'+uuid.uuid4().hex[:10],
            trial_paths=TrialPaths(trial), task_env_config=config)
        start = time.monotonic()
        calls = []
        original_exec = env.exec
        async def tracked(*args, **kwargs):
            calls.append(kwargs.get('command', args[0] if args else ''))
            return await original_exec(*args, **kwargs)
        env.exec = tracked
        record = {'task': data['task']['name'], 'attempt': attempt}
        try:
            await env.start(False)
            record['environment_seconds'] = time.monotonic()-start
            await env.ensure_dirs(['/logs/agent'])
            (trial/'agent').mkdir(parents=True, exist_ok=True)
            setup = time.monotonic()
            session = PreparedTmuxSession('rrsi-check', env, Path('/logs/agent/pane.log'),
                                           trial/'agent/recording.cast', Path('/logs/agent/recording.cast'))
            await session.start()
            assert await session.is_session_alive(), 'tmux session not usable'
            await session.send_keys(['printf "RRSI_PREINSTALLED_OK\\n"', 'Enter'], min_timeout_sec=1)
            await session.stop()
            record['session_seconds'] = time.monotonic()-setup
            assert not any(x in cmd for cmd in calls for x in ('apt-get', 'pip install', 'apk add', 'dnf install'))
            assert (trial/'agent/recording.cast').stat().st_size > 0
            record['status'] = 'ready'
        except Exception as exc:
            record.update(status='error', error=str(exc))
            raise
        finally:
            record['commands'] = calls
            atomic_json(trial/'validation.json', record)
            # Only this newly allocated validation project's containers.
            await env.stop(delete=True)
        records.append(record)
    return records


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('tasks', nargs='+', type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for task in args.tasks:
        results.extend(await verify(task, args.output))
        atomic_json(args.output/'summary.json', results)
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    asyncio.run(main())

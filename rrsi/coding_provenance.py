"""Reject comparisons across preparation policies or pinned task image IDs."""
import json
from pathlib import Path

from rrsi.tool_images import atomic_json, locked, policy


def ensure_run_policy(runs_dir: Path):
    path = runs_dir / 'environment-policy.json'
    expected = policy()
    with locked(path.with_suffix('.lock')):
        if path.exists():
            if json.loads(path.read_text()) != expected:
                raise ValueError('Coding environment policy changed; use a new runs root and baseline')
        else:
            for result in (runs_dir / 'jobs').glob('*/*/result.json'):
                provenance = result.parent / 'tool-image.json'
                actual = json.loads(provenance.read_text()) if provenance.exists() else {'mode': 'original', 'recipe': None}
                if any(actual.get(k) != v for k, v in expected.items()):
                    raise ValueError('Existing coding results use a different preparation policy; use a new runs root')
            atomic_json(path, expected)
    return expected


def register_image(jobs_dir: Path, task: str, record: dict):
    # Shared across all jobs in one runs root, including baseline/evolution.
    path = jobs_dir.parent / 'environment-images.json'
    identity = {k: record[k] for k in ('recipe', 'original_id', 'derived_id', 'platform')}
    with locked(path.with_suffix('.lock')):
        registry = json.loads(path.read_text()) if path.exists() else {}
        if task in registry and registry[task] != identity:
            raise ValueError(f'Image identity changed for {task}; use a new runs root and baseline')
        registry[task] = identity
        atomic_json(path, registry)


def trial_policy(trial_dir: Path):
    path = trial_dir / 'tool-image.json'
    if not path.exists():
        return {'mode': 'original', 'recipe': None}
    data = json.loads(path.read_text())
    policy = {k: data.get(k) for k in ('mode', 'recipe')}
    if data.get('dns_servers'):
        policy['dns_servers'] = data['dns_servers']
    return policy


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs-dir', required=True, type=Path)
    args = parser.parse_args()
    ensure_run_policy(args.jobs_dir.resolve().parent)

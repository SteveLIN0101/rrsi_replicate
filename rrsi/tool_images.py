"""Content-addressed recording-tool images; no task container state is shared."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'domains/coding/tool_images/install.sh'
SCHEMA = 1
BUILD_TIMEOUT = 900


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def recipe_id() -> str:
    return digest({'schema': SCHEMA, 'installer': INSTALL.read_text(),
                   'builder': Path(__file__).read_text()})[:24]


def cache_root() -> Path:
    if configured := os.environ.get('RRSI_TOOL_IMAGE_CACHE'):
        return Path(configured).resolve()
    root = ROOT
    if (root / '.git').is_file():
        common = subprocess.check_output(['git', '-C', str(root), 'rev-parse',
                                          '--git-common-dir'], text=True).strip()
        root = (root / common).resolve().parent
    return root / 'runs/coding/tool-images'


@contextmanager
def locked(path: Path, timeout: float = 1200):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as file:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'Timed out waiting for tool-image lock {path}')
                time.sleep(.2)
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        json.dump(data, f, indent=2)
        name = f.name
    os.replace(name, path)


def docker(*args, timeout=60, check=True):
    return subprocess.run(['docker', *args], capture_output=True, text=True,
                          timeout=timeout, check=check)


def inspect(image):
    result = docker('image', 'inspect', image, check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def run_image(image, command, platform):
    # These are new, disposable probes, never existing task containers.
    return docker('run', '--rm', '--network', 'none', '--platform', platform,
                  '--user', '0', '--entrypoint', '/bin/sh', image, '-ec', command).stdout


def prepare(reference: str, *, pull=True, cache: Path | None = None) -> dict:
    start = time.monotonic()
    cache = cache or cache_root()
    recipe = recipe_id()
    # The first successful resolution pins mutable source tags for this recipe.
    ref_path = cache / recipe / 'refs' / (digest(reference) + '.json')
    with locked(ref_path.with_suffix('.lock')):
        if ref_path.exists():
            record = json.loads(ref_path.read_text())
            derived = inspect(record['derived_id'])
            if derived:
                return {**record, 'reused': True, 'prepare_seconds': time.monotonic()-start}
            # Rebuild only the pinned base; never replace it with a newer tag.
            base = inspect(record['original_id'])
            if base is None:
                raise RuntimeError(f'Pinned images missing for {reference}; restore the recorded image IDs before reuse')
        else:
            base = inspect(reference)
            if base is None:
                if not pull:
                    raise RuntimeError(f'Original image is not downloaded: {reference}')
                docker('pull', reference, timeout=600)
                base = inspect(reference)
        if not base or base['Os'] != 'linux':
            raise RuntimeError(f'Expected a Linux task image: {reference}')
        base_id = base['Id']
        platform = f"{base['Os']}/{base['Architecture']}"
        if base.get('Variant'):
            platform += '/' + base['Variant']
        key = digest({'base': base_id, 'platform': platform, 'recipe': recipe})[:32]
        image = f'rrsi-tools:{key}'
        artifact = cache / recipe / 'images' / key
        artifact.mkdir(parents=True, exist_ok=True)
        with locked(artifact / 'build.lock'):
            manifest = artifact / 'manifest.json'
            if manifest.exists():
                record = json.loads(manifest.read_text())
                if inspect(record['derived_id']):
                    record = {**record, 'original_reference': reference}
                    atomic_json(ref_path, record)
                    return {**record, 'reused': True, 'prepare_seconds': time.monotonic()-start}
            info = run_image(base_id, '. /etc/os-release; printf "%s\\n%s\\n" "$ID" "$VERSION_ID"; '
                             'command -v apt-get; dpkg --print-architecture; '
                             'cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null || true', platform)
            lines = info.splitlines()
            if len(lines) < 4 or lines[2] != '/usr/bin/apt-get':
                raise RuntimeError(f'Unsupported package manager in {reference}; use RRSI_TOOL_IMAGES=original')
            # Repository identity prevents mixing same-release private/replaced repos.
            cache_key = digest({'os': lines[0], 'release': lines[1], 'arch': lines[3],
                                'sources': lines[4:], 'recipe': recipe})[:32]
            user = (base.get('Config') or {}).get('User') or '0'
            # FROM uses a private alias of the exact local ID: Docker does not pull
            # a mutable original tag while building. Never retag the original.
            alias = f'rrsi-tools-base:{base_id.split(":")[1]}'
            docker('tag', base_id, alias)
            dockerfile = (f'FROM {alias}\nUSER 0\n'
                f'RUN --mount=type=cache,id=rrsi-apt-{cache_key},target=/var/cache/apt/archives,sharing=locked '
                f'--mount=type=cache,id=rrsi-lists-{cache_key},target=/var/lib/apt/lists,sharing=locked '
                f'--mount=type=cache,id=rrsi-pins-{cache_key},target=/rrsi-package-lock,sharing=locked '
                '--mount=type=bind,source=install.sh,target=/tmp/rrsi-install-tools.sh '
                '/bin/sh /tmp/rrsi-install-tools.sh\n'
                f'LABEL org.rrsi.tools.recipe="{recipe}" org.rrsi.tools.base="{base_id}"\n'
                f'USER {user}\n')
            (artifact / 'Dockerfile').write_text(dockerfile)
            (artifact / 'install.sh').write_text(INSTALL.read_text())
            log_path = artifact / 'build.log'
            with log_path.open('a') as log:
                log.write(f'\nBuild started {time.time()} base={base_id}\n'); log.flush()
                try:
                    subprocess.run(['docker', 'build', '--progress=plain', '--pull=false',
                                    '--platform', platform, '-t', image, str(artifact)],
                                   stdout=log, stderr=subprocess.STDOUT, check=True,
                                   timeout=BUILD_TIMEOUT, env={**os.environ, 'DOCKER_BUILDKIT': '1'})
                except (subprocess.SubprocessError, OSError) as exc:
                    raise RuntimeError(f'Tool image preparation failed for {reference}; log: {log_path}') from exc
            derived = inspect(image)
            versions = run_image(derived['Id'], 'tmux -V; asciinema --version; '
                                 'cat /opt/rrsi-tools/packages.lock', platform).splitlines()
            packages = run_image(derived['Id'], 'cat /opt/rrsi-tools/packages-before.tsv; '
                                 'printf "\\n---AFTER---\\n"; cat /opt/rrsi-tools/packages-after.tsv', platform)
            (artifact / 'packages.tsv').write_text(packages)
            record = {'schema': SCHEMA, 'mode': 'preinstalled', 'recipe': recipe,
                      'original_reference': reference, 'original_id': base_id,
                      'original_digests': base.get('RepoDigests', []),
                      'derived_tag': image, 'derived_id': derived['Id'], 'platform': platform,
                      'os': lines[0], 'release': lines[1], 'package_arch': lines[3],
                      'download_cache_key': cache_key, 'tools': versions[:2],
                      'package_pins': versions[2:], 'build_log': str(log_path),
                      'build_seconds': time.monotonic()-start}
            atomic_json(manifest, record)
            atomic_json(ref_path, record)
            return {**record, 'reused': False, 'prepare_seconds': time.monotonic()-start}


def local_task_images(task_cache: Path):
    images = set()
    for path in task_cache.rglob('task.toml'):
        data = tomllib.loads(path.read_text())
        image = data.get('environment', {}).get('docker_image')
        if image and inspect(image):
            images.add(image)
    return sorted(images)


def policy():
    mode = os.environ.get('RRSI_TOOL_IMAGES', 'preinstalled')
    if mode not in ('preinstalled', 'original'):
        raise ValueError('RRSI_TOOL_IMAGES must be preinstalled or original')
    return {'mode': mode, 'recipe': recipe_id() if mode == 'preinstalled' else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', nargs='*')
    parser.add_argument('--local-tasks', action='store_true')
    parser.add_argument('--task-cache', type=Path, default=Path.home()/'.cache/harbor/tasks/packages/terminal-bench')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    images = args.images + (local_task_images(args.task_cache) if args.local_tasks else [])
    if not images:
        parser.error('Provide image references or --local-tasks')
    results = []
    for image in dict.fromkeys(images):
        try:
            result = prepare(image, pull=not args.local_tasks)
        except Exception as exc:
            result = {'original_reference': image, 'error': str(exc)}
        results.append(result)
        print(json.dumps(result), flush=True)
        if args.output:
            atomic_json(args.output, results)
    if any('error' in x for x in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()

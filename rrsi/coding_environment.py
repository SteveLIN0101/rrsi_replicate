"""Harbor Docker extension: pin a prepared image, keep trial isolation intact."""
import asyncio
import ipaddress
import json
import time
import traceback

from harbor.environments.docker.docker import DockerEnvironment
from rrsi.tool_images import atomic_json, prepare, recipe_id
from rrsi.coding_provenance import register_image


class PreparedDockerEnvironment(DockerEnvironment):
    tools_preinstalled = True

    def __init__(self, *args, dns_servers=None, **kwargs):
        if dns_servers is not None and not isinstance(dns_servers, list):
            raise ValueError('dns_servers must be a list of IP addresses')
        self._dns_servers = [str(ipaddress.ip_address(ip)) for ip in (dns_servers or [])]
        self._tools_compose_path = None
        super().__init__(*args, **kwargs)

    @property
    def _docker_compose_paths(self):
        paths = super()._docker_compose_paths
        if self._tools_compose_path:
            paths.append(self._tools_compose_path)
        return paths

    async def start(self, force_build=False):
        started = time.monotonic()
        path = self.trial_paths.trial_dir / 'tool-image.json'
        report = {'mode': 'preinstalled', 'recipe': recipe_id(), 'status': 'preparing',
                  'dns_servers': self._dns_servers}
        try:
            reference = self.task_env_config.docker_image
            if force_build or not reference:
                raise ValueError('Prepared tool images require task.docker_image and --no-force-build; '
                                 'use RRSI_TOOL_IMAGES=original for Dockerfile-only tasks')
            record = await asyncio.to_thread(prepare, reference)
            report.update(record, status='image_ready')
            register_image(self.trial_paths.trial_dir.parent.parent, reference, record)
            # Copy the config; never mutate the cached task or another trial.
            self.task_env_config = self.task_env_config.model_copy(
                update={'docker_image': record['derived_id']})
            self._env_vars.prebuilt_image_name = record['derived_id']
            self._tools_compose_path = self.trial_paths.trial_dir / 'tool-image.compose.yaml'
            self._tools_compose_path.write_text(
                'services:\n  main:\n'
                f'    image: {json.dumps(record["derived_id"])}\n'
                f'    platform: {json.dumps(record["platform"])}\n'
                '    pull_policy: never\n    build: !reset null\n'
                + (f'    dns: {json.dumps(self._dns_servers)}\n' if self._dns_servers else ''))
            atomic_json(path, report)
            await super().start(force_build=False)
            report['status'] = 'ready'
        except BaseException as exc:
            report.update(status='error', error=str(exc), traceback=traceback.format_exc())
            raise
        finally:
            report['environment_seconds'] = time.monotonic() - started
            atomic_json(path, report)

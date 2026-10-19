"""Frozen setup policy for the evolvable Terminus harness."""
import asyncio
import re
import time
import traceback

from harbor_terminus2.tmux_session import TmuxSession
from rrsi.tool_images import atomic_json


class ToolPreparationError(RuntimeError):
    pass


def is_timeout(exc):
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, RuntimeError) and re.fullmatch(
            r'Command timed out after [\d.]+ seconds', str(exc)):
        return True
    return isinstance(exc.__cause__, TimeoutError)


class PreparedTmuxSession(TmuxSession):
    async def _attempt_tmux_installation(self):
        started = time.monotonic()
        preinstalled = getattr(self.environment, 'tools_preinstalled', False)
        report = {'mode': 'preinstalled' if preinstalled else 'original', 'checks': []}
        path = self.environment.trial_paths.trial_dir / 'tool-preparation.json'
        try:
            if not preinstalled:
                # Use the existing installer only in explicit original-image mode.
                await asyncio.wait_for(self._install_recording_tools(),
                                       timeout=self._TOOL_INSTALL_BUDGET_SEC)
            checks = ['tmux -V']
            if self._remote_asciinema_recording_path or preinstalled:
                checks.append('asciinema --version')
            for command in checks:
                result = await self.environment.exec(command=command, user=self._user, timeout_sec=15)
                report['checks'].append({'command': command, 'return_code': result.return_code,
                                         'stdout': result.stdout, 'stderr': result.stderr})
                if result.return_code != 0:
                    raise ToolPreparationError(f'Required tool unavailable: {command}; see {path}')
            report['status'] = 'ready'
        except Exception as exc:
            report.update(status='timeout' if is_timeout(exc) else 'error',
                          error=str(exc), traceback=traceback.format_exc())
            self._logger.exception('Recording-tool preparation failed; diagnostic: %s', path)
            if is_timeout(exc):
                raise ToolPreparationError(
                    f'Recording-tool preparation timed out; no usable session was started. '
                    f'Use preinstalled tool images. Diagnostic: {path}') from exc
            raise
        finally:
            report['seconds'] = time.monotonic() - started
            atomic_json(path, report)

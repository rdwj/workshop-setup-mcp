"""
Lifecycle hook system for BaseAgent.

Hooks allow external processes to run at specific points in the agent lifecycle.
Each hook is a shell command executed asynchronously with configurable timeout.
Hooks can be registered via config or auto-discovered from a directory of YAML files.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os as _os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class HookEntry:
    """A single hook binding."""

    event: str
    command: str
    timeout: float = 10.0
    matcher: str | None = None
    name: str | None = None
    source: str = "config"


@dataclass
class HookResult:
    """Result of executing a hook."""

    hook: HookEntry
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    @property
    def blocked(self) -> bool:
        return self.exit_code is not None and self.exit_code != 0


class HookRunner:
    """Manages and executes lifecycle hooks."""

    def __init__(self, hooks: list[HookEntry] | None = None) -> None:
        self._hooks: list[HookEntry] = list(hooks or [])

    def add(self, hook: HookEntry) -> None:
        """Register a new hook."""
        self._hooks.append(hook)

    def hooks_for_event(
        self, event: str, *, tool_name: str | None = None
    ) -> list[HookEntry]:
        """Return hooks matching event, optionally filtered by tool matcher."""
        matched = []
        for hook in self._hooks:
            if hook.event != event:
                continue

            if hook.matcher is None:
                matched.append(hook)
            elif tool_name is not None and fnmatch.fnmatch(tool_name, hook.matcher):
                matched.append(hook)

        return matched

    async def fire(
        self,
        event: str,
        *,
        env_extra: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        tool_name: str | None = None,
    ) -> list[HookResult]:
        """Fire all hooks for event. Returns results in registration order."""
        hooks = self.hooks_for_event(event, tool_name=tool_name)
        results = []

        for hook in hooks:
            result = await self._run_one(hook, env_extra=env_extra, cwd=cwd)
            logger.info(
                "Hook %s for event %s: exit_code=%s, timed_out=%s, error=%s",
                hook.name or hook.command[:40],
                event,
                result.exit_code,
                result.timed_out,
                result.error,
            )
            results.append(result)

        return results

    async def _run_one(
        self,
        hook: HookEntry,
        *,
        env_extra: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> HookResult:
        """Execute a single hook command."""
        env = dict(_os.environ)
        env["HOOK_EVENT"] = hook.event
        if env_extra:
            env.update(env_extra)

        try:
            proc = await asyncio.create_subprocess_shell(
                hook.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(cwd) if cwd else None,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=hook.timeout
            )

            return HookResult(
                hook=hook,
                exit_code=proc.returncode,
                stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
                stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
            )

        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return HookResult(hook=hook, timed_out=True)

        except OSError as exc:
            return HookResult(hook=hook, error=str(exc))

    def __len__(self) -> int:
        return len(self._hooks)

    def __bool__(self) -> bool:
        return len(self._hooks) > 0


def load_hooks_from_dir(hooks_dir: str | Path) -> list[HookEntry]:
    """Auto-discover hook YAML files from a directory.

    Each .yaml/.yml file declares one hook binding with required fields
    'event' and 'command', plus optional 'timeout', 'matcher', 'name'.
    Files starting with _ or . are skipped. Non-YAML files are skipped.
    Returns entries sorted by filename.
    """
    hooks_path = Path(hooks_dir)
    if not hooks_path.is_dir():
        return []

    entries = []
    yaml_files = sorted(
        [
            f
            for f in hooks_path.iterdir()
            if f.is_file()
            and f.suffix in {".yaml", ".yml"}
            and not f.name.startswith(("_", "."))
        ]
    )

    for yaml_file in yaml_files:
        try:
            with yaml_file.open("r", encoding="utf-8") as fp:
                data: Any = yaml.safe_load(fp)

            if not isinstance(data, dict):
                logger.warning("Hook file %s does not contain a dict, skipping", yaml_file)
                continue

            if "event" not in data or "command" not in data:
                logger.warning(
                    "Hook file %s missing required 'event' or 'command', skipping",
                    yaml_file,
                )
                continue

            entry = HookEntry(
                event=data["event"],
                command=data["command"],
                timeout=data.get("timeout", 10.0),
                matcher=data.get("matcher"),
                name=data.get("name", yaml_file.stem),
                source=f"file:{yaml_file}",
            )
            entries.append(entry)

        except yaml.YAMLError as exc:
            logger.warning("Failed to parse hook file %s: %s", yaml_file, exc)
        except Exception as exc:
            logger.warning("Unexpected error loading hook file %s: %s", yaml_file, exc)

    return entries


def create_hook_runner(
    config_hooks: list | None = None,
    hooks_dir: str | Path | None = None,
) -> HookRunner:
    """Build a HookRunner from config entries and auto-discovered files.

    Config hooks are loaded first, then file hooks are appended.
    *config_hooks* should be a list of objects with ``event``, ``command``,
    ``timeout``, ``matcher``, and ``name`` attributes (e.g.
    ``HookEntryConfig`` Pydantic models from ``agent.yaml``).
    """
    hooks: list[HookEntry] = []

    if config_hooks:
        for cfg in config_hooks:
            hooks.append(
                HookEntry(
                    event=cfg.event,
                    command=cfg.command,
                    timeout=cfg.timeout,
                    matcher=cfg.matcher,
                    name=cfg.name,
                    source="config",
                )
            )

    if hooks_dir:
        hooks.extend(load_hooks_from_dir(hooks_dir))

    return HookRunner(hooks)

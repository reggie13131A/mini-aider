from __future__ import annotations

from abc import ABC, abstractmethod
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from states import RecoveryResult

CommandResult = Dict[str, Any]


@dataclass
class RuntimeStatus:
    is_healthy: bool = field(default=True)
    sandbox_id: Optional[str] = field(default=None)
    cwd: Optional[str] = field(default=None)
    last_error: str = ""
    consecutive_failures: int = field(default=0)
    last_command: str = field(default="")
    last_commands: List[str] = field(default_factory=list)


class BaseCommander(ABC):
    ENV_ERROR_KEYWORDS = [
        "sandbox was not found",
        "sandbox not found",
        "peer closed connection",
        "connection reset",
        "broken pipe",
        "timed out waiting",
        "timeout while connecting",
        "transport error",
        "unable to connect",
        "connection aborted",
        "network is unreachable",
        "temporary failure in name resolution",
        "no such container",
        "container not found",
        "session not found",
        "failed to connect",
        "connection refused",
        "cwd does not exist",
        "working directory does not exist",
        "permission denied",
        "read-only file system",
        "the sandbox was not found",
    ]

    COMMAND_ERROR_KEYWORDS = [
        "command not found",
        "no such file or directory",
        "cannot access",
        "invalid option",
        "unknown option",
        "usage:",
        "is not recognized as an internal or external command",
        "empty command",
    ]

    def __init__(self):
        self.runtime = RuntimeStatus()

    @abstractmethod
    def health_check(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def ensure_ready(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def recover_environment(self) -> RecoveryResult:
        raise NotImplementedError

    @abstractmethod
    def run(self, command: str) -> List[CommandResult]:
        raise NotImplementedError

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def read_file(self, path: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def stage_local_file(self, local_path: str, remote_path: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def classify_failure_type(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        explicit_failure: Optional[str] = None,
    ) -> str:
        if explicit_failure in {
            "none",
            "environment_failed",
            "code_failed",
            "command_failed",
            "unknown_failed",
        }:
            return str(explicit_failure)

        merged = f"{stdout}\n{stderr}".lower()
        if any(k in merged for k in self.ENV_ERROR_KEYWORDS):
            return "environment_failed"
        if any(k in merged for k in self.COMMAND_ERROR_KEYWORDS):
            return "command_failed"
        if isinstance(exit_code, int) and exit_code >= 0:
            return "code_failed"
        return "unknown_failed"

    def build_result(
        self,
        command: str,
        success: bool,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        failure_type: Optional[str] = None,
        failure_reason: str = "",
    ) -> CommandResult:
        final_failure_type = (
            "none"
            if success
            else self.classify_failure_type(
                stdout,
                stderr,
                exit_code,
                explicit_failure=failure_type,
            )
        )

        if success:
            failure_reason = ""

        return {
            "command": command,
            "success": success,
            "exit_code": exit_code,
            "stdout": str(stdout).strip(),
            "stderr": str(stderr).strip(),
            "failure_type": final_failure_type,
            "failure_reason": failure_reason or ("" if success else str(stderr).strip()),
            "sandbox_id": self.runtime.sandbox_id,
            "cwd": self.runtime.cwd,
        }

    def mark_failure(self, error_message: str) -> None:
        self.runtime.is_healthy = False
        self.runtime.last_error = error_message
        self.runtime.consecutive_failures += 1

    def mark_success(self) -> None:
        self.runtime.is_healthy = True
        self.runtime.last_error = ""
        self.runtime.consecutive_failures = 0

    def format_results(self, results: List[CommandResult], max_chars: int = 1200) -> str:
        if not results:
            return "No commands were executed."

        blocks: List[str] = []
        for item in results:
            status = "SUCCESS" if item.get("success") else "FAILED"
            stdout = self._clip(str(item.get("stdout", "")), max_chars)
            stderr = self._clip(str(item.get("stderr", "")), max_chars)

            block = [
                f"Command: {item.get('command', '')}",
                f"Status: {status}",
                f"Exit Code: {item.get('exit_code', '')}",
                f"Failure Type: {item.get('failure_type', '')}",
            ]
            if item.get("failure_reason"):
                block.append(f"Failure Reason: {item.get('failure_reason', '')}")
            if stdout:
                block.append(f"STDOUT:\n{stdout}")
            if stderr:
                block.append(f"STDERR:\n{stderr}")
            blocks.append("\n".join(block))

        return "\n\n".join(blocks)

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"


class LocalCommander(BaseCommander):
    def __init__(self, root: str = ".") -> None:
        super().__init__()
        self.root = str(Path(root).resolve())
        self.runtime.cwd = self.root

    def health_check(self) -> bool:
        ok = Path(self.root).exists()
        if ok:
            self.mark_success()
        else:
            self.mark_failure(f"local root not found: {self.root}")
        return ok

    def ensure_ready(self) -> bool:
        return self.health_check()

    def recover_environment(self) -> RecoveryResult:
        ok = self.health_check()
        if ok:
            return RecoveryResult(
                success=True,
                message="local environment health check passed",
                mode="noop",
                workspace_preserved=True,
                requires_restage=False,
                sandbox_id=None,
            )
        return RecoveryResult(
            success=False,
            message=self.runtime.last_error or "local environment unavailable",
            mode="local_check_failed",
            workspace_preserved=True,
            requires_restage=False,
            sandbox_id=None,
        )

    def run(self, command: str) -> List[CommandResult]:
        clean_cmd = str(command).strip()
        self.runtime.last_command = clean_cmd
        self.runtime.last_commands = [clean_cmd] if clean_cmd else []

        if not self.ensure_ready():
            return [
                self.build_result(
                    command=clean_cmd,
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=self.runtime.last_error,
                    failure_type="environment_failed",
                    failure_reason=self.runtime.last_error,
                )
            ]

        if not clean_cmd:
            return [
                self.build_result(
                    command="",
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr="empty command",
                    failure_type="command_failed",
                    failure_reason="empty command",
                )
            ]

        try:
            proc = subprocess.run(
                clean_cmd,
                shell=True,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            success = proc.returncode == 0
            if success:
                self.mark_success()
            else:
                self.mark_failure(proc.stderr.strip() or "command exited non-zero")

            return [
                self.build_result(
                    command=clean_cmd,
                    success=success,
                    exit_code=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            ]
        except Exception as e:
            msg = f"[{type(e).__name__}] {e}"
            self.mark_failure(msg)
            return [
                self.build_result(
                    command=clean_cmd,
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=msg,
                    failure_type="unknown_failed",
                    failure_reason=msg,
                )
            ]

    def write_file(self, path: str, content: str) -> None:
        if not self.ensure_ready():
            raise RuntimeError(self.runtime.last_error or "local environment unavailable")

        abs_path = Path(self.root) / path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    def read_file(self, path: str) -> str:
        if not self.ensure_ready():
            raise RuntimeError(self.runtime.last_error or "local environment unavailable")

        abs_path = Path(self.root) / path
        return abs_path.read_text(encoding="utf-8")

    def stage_local_file(self, local_path: str, remote_path: str) -> None:
        local_file = Path(local_path)
        if not local_file.exists():
            raise FileNotFoundError(f"本地文件不存在: {local_path}")
        if not local_file.is_file():
            raise ValueError(f"不是普通文件，无法 stage: {local_path}")

        content = local_file.read_text(encoding="utf-8")
        self.write_file(remote_path, content)

    def close(self) -> None:
        return None


class SandboxCommander(BaseCommander):
    def __init__(
        self,
        sandbox_cwd: str = "/home/user",
        timeout: int = 60,
        connect_sandbox_id: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.sandbox_cwd = sandbox_cwd
        self.timeout = timeout
        self._connected_existing = bool(connect_sandbox_id)

        try:
            from e2b_code_interpreter import Sandbox
        except ImportError as e:
            raise ImportError(
                "未安装 E2B Python SDK。请先安装对应 SDK，并配置 E2B_API_KEY。"
            ) from e

        self._Sandbox = Sandbox

        if connect_sandbox_id:
            self.sandbox = Sandbox.connect(connect_sandbox_id)
        else:
            self.sandbox = Sandbox.create(timeout=1800)

        self.sandbox_id = getattr(self.sandbox, "sandbox_id", None)
        self.runtime.sandbox_id = self.sandbox_id
        self.runtime.cwd = self.sandbox_cwd
        self._ensure_dir(self.sandbox_cwd)
        self.health_check()

    def _reconnect_or_recreate_sandbox(self) -> str:
        """
        返回:
        - reconnected
        - recreated
        """
        if self._connected_existing and self.sandbox_id:
            self.sandbox = self._Sandbox.connect(self.sandbox_id)
            mode = "reconnected"
        else:
            self.sandbox = self._Sandbox.create()
            self.sandbox_id = getattr(self.sandbox, "sandbox_id", None)
            self.runtime.sandbox_id = self.sandbox_id
            mode = "recreated"

        self._ensure_dir(self.sandbox_cwd)
        return mode

    def _ensure_dir(self, path: str) -> None:
        result = self.sandbox.commands.run(
            f"mkdir -p {self._shell_quote(path)}",
            cwd="/",
            timeout=self.timeout,
        )
        exit_code = getattr(result, "exit_code", 1)
        if exit_code != 0:
            stderr = getattr(result, "stderr", "") or "验证 dir 失败"
            raise RuntimeError(f"创建/校验目录失败 {path}: {stderr}")

    def _resolve_remote_path(self, path: str) -> str:
        p = PurePosixPath(path)
        if p.is_absolute():
            return str(p)
        return str(PurePosixPath(self.sandbox_cwd) / p)

    @staticmethod
    def _shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    def health_check(self) -> bool:
        try:
            result = self.sandbox.commands.run(
                "pwd && echo __health_ok__",
                cwd=self.sandbox_cwd,
                timeout=self.timeout,
            )
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            exit_code = getattr(result, "exit_code", 1)
            ok = exit_code == 0 and "__health_ok__" in str(stdout)

            if ok:
                self.mark_success()
            else:
                self.mark_failure(str(stderr).strip() or "sandbox health check failed")
            return ok
        except Exception as e:
            self.mark_failure(f"[{type(e).__name__}] {e}")
            return False

    def ensure_ready(self) -> bool:
        return self.health_check()

    def recover_environment(self) -> RecoveryResult:
        old_sandbox_id = self.runtime.sandbox_id
        try:
            mode = self._reconnect_or_recreate_sandbox()
            ok = self.health_check()
            if not ok:
                return RecoveryResult(
                    success=False,
                    message=self.runtime.last_error or "sandbox recovery failed",
                    mode=mode,  # type: ignore[arg-type]
                    workspace_preserved=(mode != "recreated"),
                    requires_restage=(mode == "recreated"),
                    sandbox_id=self.runtime.sandbox_id,
                )

            workspace_preserved = mode != "recreated"
            requires_restage = (mode == "recreated") or (
                old_sandbox_id is not None and old_sandbox_id != self.runtime.sandbox_id
            )

            return RecoveryResult(
                success=True,
                message=(
                    f"sandbox recovered successfully via {mode}"
                    + (
                        ", workspace needs restage"
                        if requires_restage
                        else ", workspace preserved"
                    )
                ),
                mode=mode,  # type: ignore[arg-type]
                workspace_preserved=workspace_preserved,
                requires_restage=requires_restage,
                sandbox_id=self.runtime.sandbox_id,
            )
        except Exception as e:
            msg = f"[{type(e).__name__}] {e}"
            self.mark_failure(msg)
            return RecoveryResult(
                success=False,
                message=msg,
                mode="recreated",
                workspace_preserved=False,
                requires_restage=True,
                sandbox_id=self.runtime.sandbox_id,
            )

    def run(self, command: str) -> List[CommandResult]:
        clean_cmd = str(command).strip()
        self.runtime.last_command = clean_cmd
        self.runtime.last_commands = [clean_cmd] if clean_cmd else []

        if not self.ensure_ready():
            return [
                self.build_result(
                    command=clean_cmd,
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=self.runtime.last_error,
                    failure_type="environment_failed",
                    failure_reason=self.runtime.last_error,
                )
            ]

        if not clean_cmd:
            return [
                self.build_result(
                    command="",
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr="empty command",
                    failure_type="command_failed",
                    failure_reason="empty command",
                )
            ]

        try:
            result = self.sandbox.commands.run(
                clean_cmd,
                cwd=self.sandbox_cwd,
                timeout=self.timeout,
            )
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            exit_code = getattr(result, "exit_code", 0)
            success = exit_code == 0

            if success:
                self.mark_success()
            else:
                self.mark_failure(str(stderr).strip() or "command exited non-zero")

            return [
                self.build_result(
                    command=clean_cmd,
                    success=success,
                    exit_code=exit_code,
                    stdout=str(stdout),
                    stderr=str(stderr),
                )
            ]
        except Exception as e:
            msg = f"[{type(e).__name__}] {e}"
            self.mark_failure(msg)
            return [
                self.build_result(
                    command=clean_cmd,
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=msg,
                    failure_type="environment_failed",
                    failure_reason=msg,
                )
            ]

    def write_file(self, path: str, content: str) -> None:
        if not self.ensure_ready():
            raise RuntimeError(self.runtime.last_error or "sandbox unavailable")

        remote_path = self._resolve_remote_path(path)
        parent = str(PurePosixPath(remote_path).parent)
        self._ensure_dir(parent)
        self.sandbox.files.write(remote_path, content)

    def read_file(self, path: str) -> str:
        if not self.ensure_ready():
            raise RuntimeError(self.runtime.last_error or "sandbox unavailable")

        remote_path = self._resolve_remote_path(path)
        return self.sandbox.files.read(remote_path)

    def stage_local_file(self, local_path: str, remote_path: str) -> None:
        local_file = Path(local_path)
        if not local_file.exists():
            raise FileNotFoundError(f"本地文件不存在: {local_path}")
        if not local_file.is_file():
            raise ValueError(f"不是普通文件，无法 stage: {local_path}")

        content = local_file.read_text(encoding="utf-8")
        self.write_file(remote_path, content)

    def close(self) -> None:
        try:
            self.sandbox.kill()
        except Exception:
            pass
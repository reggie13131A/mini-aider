from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from states import VerifyAction, VerificationReport, FailureType
from prompt import VerifierPrompts

Message = Dict[str, str]


class Verifier:
    """
    功能型验证 agent：
    - 接收 coder 产物（assistant_output）
    - 维护线性验证目标列表 pending_goals
    - 一次只执行一个 current_goal
    - 若当前目标失败，则基于 observation 围绕当前目标重规划
    - 当前目标成功后，才推进到下一个目标

    核心原则：
    1. verifier 负责 runtime / validation 层面的判断与恢复
    2. coder 只在明确 code_failed 时才进行代码反思
    3. 环境故障优先 recover_env / restage_files
    4. 单目标推进，避免整批命令混淆 observation
    """

    def __init__(
        self,
        model: Any,
        commander: Any,
        use_sandbox: bool,
        io: Optional[Any] = None,
        max_reflection: int = 10,
    ) -> None:
        self.model = model
        self.commander = commander
        self.use_sandbox = use_sandbox
        self.io = io
        self.max_reflection = max_reflection
        self.prompts = VerifierPrompts()

        self.observation: List[Message] = []
        self.last_commands: List[str] = []
        self.pending_goals: List[str] = []
        self.current_goal: str = ""

        self.ENV_ERROR_KEYWORDS = list(getattr(commander, "ENV_ERROR_KEYWORDS", []))
        self.COMMAND_ERROR_KEYWORDS = list(getattr(commander, "COMMAND_ERROR_KEYWORDS", []))

    # ============================================================
    # IO helpers
    # ============================================================
    def _tool_output(self, text: str) -> None:
        if self.io is not None:
            self.io.tool_output(text)

    def _tool_warning(self, text: str) -> None:
        if self.io is not None:
            self.io.tool_warning(text)

    def reset_round_state(self) -> None:
        self.observation = []
        self.last_commands = []
        self.pending_goals = []
        self.current_goal = ""

    def add_to_observation(self, tool_name: str, content: str) -> None:
        self.observation.append(
            {
                "role": "system",
                "content": f"[tool:{tool_name}]\n{content}",
            }
        )

    # ============================================================
    # Formatting helpers
    # ============================================================
    def _format_context_block(
        self,
        title: str,
        context: Dict[str, str],
        max_chars_per_file: int = 12000,
    ) -> str:
        if not context:
            return ""

        parts: List[str] = [f"{title}:"]
        for fname, content in context.items():
            clipped = content[:max_chars_per_file]
            if len(content) > max_chars_per_file:
                clipped += "\n...[truncated]"
            parts.append(f"\n### FILE: {fname}\n{clipped}")
        return "\n".join(parts)

    def _normalize_commands(self, commands: Any) -> List[str]:
        normalized: List[str] = []
        if not commands:
            return normalized

        for item in commands:
            if isinstance(item, str):
                cmd = item.strip()
            elif isinstance(item, dict):
                cmd = str(item.get("command", "") or "").strip()
            else:
                cmd = str(getattr(item, "command", "") or "").strip()

            if cmd:
                normalized.append(cmd)

        return normalized

    def _set_initial_goals_if_needed(self, cmd_list: List[str]) -> None:
        normalized = self._normalize_commands(cmd_list)
        if self.pending_goals:
            return
        if not normalized:
            return
        self.pending_goals = list(normalized)
        if not self.current_goal and self.pending_goals:
            self.current_goal = self.pending_goals[0]

    def _advance_goal(self) -> None:
        if self.pending_goals and self.current_goal and self.pending_goals[0] == self.current_goal:
            self.pending_goals.pop(0)
        elif self.current_goal and self.current_goal in self.pending_goals:
            self.pending_goals.remove(self.current_goal)

        self.current_goal = self.pending_goals[0] if self.pending_goals else ""

    # ============================================================
    # Prompt construction
    # ============================================================
    def construct_messages(
        self,
        read_only_context: Optional[Dict[str, str]] = None,
        editable_context: Optional[Dict[str, str]] = None,
        assistant_output: Optional[Dict[str, Any]] = None,
        retry: bool = False,
    ) -> List[Message]:
        system_prompt = self.prompts.build_verifier_system_prompt(
            use_sandbox=self.use_sandbox
        )

        messages: List[Message] = [{"role": "system", "content": system_prompt}]

        if retry:
            messages.append(
                {
                    "role": "system",
                    "content": self.prompts.reflection_retry_prompt.strip(),
                }
            )

        if read_only_context:
            messages.append(
                {
                    "role": "system",
                    "content": self.prompts.context_prompt.strip()
                    + "\n\n"
                    + self._format_context_block("Read-only file context", read_only_context),
                }
            )

        if editable_context:
            messages.append(
                {
                    "role": "system",
                    "content": self.prompts.editable_prompt.strip()
                    + "\n\n"
                    + self._format_context_block("Editable file context", editable_context),
                }
            )

        if assistant_output:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "下面是 coder 本轮产物，请基于它生成或修正验证计划。\n\n"
                        "你要重点关注：\n"
                        "- task_summary\n"
                        "- validation_summary\n"
                        "- file_edits\n\n"
                        + json.dumps(assistant_output, ensure_ascii=False, indent=2, default=str)
                    ),
                }
            )

        if self.current_goal:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"当前正在攻克的验证目标命令是：\n{self.current_goal}\n\n"
                        "你应该优先围绕这个目标前进，而不是重新发散整个计划。"
                    ),
                }
            )

        if self.pending_goals:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "当前待完成的线性目标列表如下：\n"
                        + json.dumps(self.pending_goals, ensure_ascii=False, indent=2)
                    ),
                }
            )

        if self.observation:
            messages.extend(self.observation)

        return messages

    # ============================================================
    # Failure classification
    # ============================================================
    def _match_keywords(self, text: str, keywords: List[str]) -> bool:
        lowered = (text or "").lower()
        return any(k in lowered for k in keywords)

    def _classify_result(self, result: Dict[str, Any]) -> FailureType:
        if result.get("success", False):
            return "none"

        explicit = str(result.get("failure_type", "") or "").strip()
        if explicit in {
            "environment_failed",
            "code_failed",
            "command_failed",
            "unknown_failed",
        }:
            return explicit  # type: ignore[return-value]

        stdout = str(result.get("stdout", "") or "")
        stderr = str(result.get("stderr", "") or "")
        merged = f"{stdout}\n{stderr}"

        if self._match_keywords(merged, self.ENV_ERROR_KEYWORDS):
            return "environment_failed"

        if self._match_keywords(merged, self.COMMAND_ERROR_KEYWORDS):
            return "command_failed"

        exit_code = result.get("exit_code", None)
        if isinstance(exit_code, int) and exit_code >= 0:
            return "code_failed"

        return "unknown_failed"

    def _summarize_results(
        self,
        results: List[Dict[str, Any]],
        verifier_action: str = "",
        success_status: str = "passed",
        success_summary: str = "所有验证动作都成功完成。",
        success_should_retry_verifier: bool = False,
    ) -> VerificationReport:
        if not results:
            return VerificationReport(
                status="not_run",
                failure_type="none",
                summary="没有执行到任何验证命令。",
                should_reflect_code=False,
                should_retry_verifier=False,
                should_recover_env=False,
                verifier_action=verifier_action,
            )

        failure_types = [
            self._classify_result(r)
            for r in results
            if not r.get("success", False)
        ]

        if not failure_types:
            return VerificationReport(
                status=success_status,
                failure_type="none",
                summary=success_summary,
                should_reflect_code=False,
                should_retry_verifier=success_should_retry_verifier,
                should_recover_env=False,
                verifier_action=verifier_action,
            )

        if "environment_failed" in failure_types:
            return VerificationReport(
                status="failed",
                failure_type="environment_failed",
                summary="当前更像是执行环境故障，而不是代码实现问题。",
                should_reflect_code=False,
                should_retry_verifier=True,
                should_recover_env=True,
                verifier_action=verifier_action,
            )

        if "command_failed" in failure_types:
            return VerificationReport(
                status="failed",
                failure_type="command_failed",
                summary="当前更像是验证命令或验证策略有问题，需要 verifier 自己修正计划。",
                should_reflect_code=False,
                should_retry_verifier=True,
                should_recover_env=False,
                verifier_action=verifier_action,
            )

        if "unknown_failed" in failure_types:
            return VerificationReport(
                status="failed",
                failure_type="unknown_failed",
                summary="当前验证失败，但归因还不够明确，建议 verifier 再观察并重新规划一步。",
                should_reflect_code=False,
                should_retry_verifier=True,
                should_recover_env=False,
                verifier_action=verifier_action,
            )

        return VerificationReport(
            status="failed",
            failure_type="code_failed",
            summary="验证已经真实落到了代码层，当前失败属于代码实现问题。",
            should_reflect_code=True,
            should_retry_verifier=False,
            should_recover_env=False,
            verifier_action=verifier_action,
        )

    # ============================================================
    # Staging helpers
    # ============================================================
    def _stage_workspace_files(
        self,
        read_only_context: Optional[Dict[str, str]] = None,
        editable_context: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        if not self.use_sandbox:
            return []

        staged: List[str] = []
        failures: List[Dict[str, Any]] = []

        merged: Dict[str, str] = {}
        if read_only_context:
            merged.update(read_only_context)
        if editable_context:
            merged.update(editable_context)

        for fname, content in merged.items():
            try:
                # 将绝对路径转换为相对于沙箱工作目录的路径
                relative_fname = self._make_relative_path(fname)
                self.commander.write_file(relative_fname, content)
                staged.append(fname)
            except Exception as e:
                failures.append(
                    {
                        "command": f"stage_context::{fname}",
                        "success": False,
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": f"[{type(e).__name__}] {e}",
                        "failure_type": "environment_failed",
                        "failure_reason": f"同步上下文文件失败: {fname}",
                    }
                )

        if staged:
            self.add_to_observation(
                "workspace_stage",
                "已同步上下文文件到执行环境：\n" + "\n".join(f"- {f}" for f in staged),
            )

        if failures:
            self.add_to_observation(
                "workspace_stage_failed",
                json.dumps(failures, ensure_ascii=False, indent=2, default=str),
            )

        return failures

    def _stage_file_edits_from_coder(
        self,
        assistant_output: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not self.use_sandbox:
            return []

        if not assistant_output:
            return []

        file_edits = assistant_output.get("file_edits", []) or []
        if not file_edits:
            return []

        staged: List[str] = []
        failures: List[Dict[str, Any]] = []

        for edit in file_edits:
            if isinstance(edit, dict):
                file_name = edit.get("file_name")
                content = edit.get("content")
            else:
                file_name = getattr(edit, "file_name", None)
                content = getattr(edit, "content", None)

            if not file_name or content is None:
                continue

            try:
                # 将绝对路径转换为相对于沙箱工作目录的路径
                relative_file_name = self._make_relative_path(file_name)
                self.commander.write_file(relative_file_name, content)
                staged.append(file_name)
            except Exception as e:
                failures.append(
                    {
                        "command": f"stage_file_edits::{file_name}",
                        "success": False,
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": f"[{type(e).__name__}] {e}",
                        "failure_type": "environment_failed",
                        "failure_reason": f"写入 coder file_edits 失败: {file_name}",
                    }
                )

        if staged:
            self.add_to_observation(
                "file_edits_stage",
                "已将 coder 本轮 file_edits 写入执行环境：\n" + "\n".join(f"- {f}" for f in staged),
            )

        if failures:
            self.add_to_observation(
                "file_edits_stage_failed",
                json.dumps(failures, ensure_ascii=False, indent=2, default=str),
            )

        return failures

    def _make_relative_path(self, file_path: str) -> str:
        """将文件路径转换为相对于沙箱工作目录的路径"""
        import os
        from pathlib import Path

        # 将路径转换为相对于项目根的路径
        abs_path = Path(file_path).resolve()
        # 获取项目根目录，通常是 /home/xyc/GNN_FFFFFF/base/langchain/mini-codex/
        project_root = Path(self.commander.root if hasattr(self.commander, 'root') else '.').resolve()

        try:
            # 计算相对于项目根的路径
            rel_path = abs_path.relative_to(project_root)
            return str(rel_path)
        except ValueError:
            # 如果文件不在项目根目录下，使用原路径的basename
            return os.path.basename(file_path)

    # ============================================================
    # Runtime actions
    # ============================================================
    def _run_single_command(self, command: str) -> List[Dict[str, Any]]:
        clean_cmd = str(command).strip()
        self.last_commands = [clean_cmd] if clean_cmd else []

        if not clean_cmd:
            results = [
                {
                    "command": "",
                    "success": False,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "empty command",
                    "failure_type": "command_failed",
                    "failure_reason": "empty command",
                }
            ]
            self.add_to_observation(
                "commands",
                json.dumps(results, ensure_ascii=False, indent=2, default=str),
            )
            return results

        try:
            results = self.commander.run(clean_cmd)
        except Exception as e:
            results = [
                {
                    "command": clean_cmd,
                    "success": False,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"[{type(e).__name__}] {e}",
                    "failure_type": "environment_failed",
                    "failure_reason": "commander.run raised exception",
                }
            ]

        try:
            obs = self.commander.format_results(results)
        except Exception:
            obs = json.dumps(results, ensure_ascii=False, indent=2, default=str)

        self.add_to_observation("commands", obs)
        return results

    def decide_next_action(
        self,
        assistant_output: Dict[str, Any],
        read_only_context: Optional[Dict[str, str]] = None,
        editable_context: Optional[Dict[str, str]] = None,
        retry: bool = False,
    ) -> VerifyAction:
        messages = self.construct_messages(
            read_only_context=read_only_context,
            editable_context=editable_context,
            assistant_output=assistant_output,
            retry=retry,
        )
        return self.model.invoke_structured(messages, schema=VerifyAction)

    def execute_action(
        self,
        action: VerifyAction,
        assistant_output: Dict[str, Any],
        read_only_context: Optional[Dict[str, str]] = None,
        editable_context: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], VerificationReport]:
        action_name = action.action

        if action_name == "recover_env":
            recovery = self.commander.recover_environment()
            results = [
                {
                    "command": "recover_environment",
                    "success": recovery.success,
                    "exit_code": 0 if recovery.success else -1,
                    "stdout": recovery.message if recovery.success else "",
                    "stderr": "" if recovery.success else recovery.message,
                    "failure_type": "none" if recovery.success else "environment_failed",
                    "failure_reason": "" if recovery.success else recovery.message,
                    "recovery_mode": recovery.mode,
                    "workspace_preserved": recovery.workspace_preserved,
                    "requires_restage": recovery.requires_restage,
                    "sandbox_id": recovery.sandbox_id,
                }
            ]
            self.add_to_observation(
                "recover_env",
                json.dumps(results, ensure_ascii=False, indent=2, default=str),
            )

            if not recovery.success:
                return results, VerificationReport(
                    status="failed",
                    failure_type="environment_failed",
                    summary="环境恢复失败，当前仍无法继续验证。",
                    should_reflect_code=False,
                    should_retry_verifier=False,
                    should_recover_env=True,
                    verifier_action=action_name,
                )

            if recovery.requires_restage:
                return results, VerificationReport(
                    status="not_run",
                    failure_type="none",
                    summary="环境已恢复，但工作区需要重新同步，下一步应执行 restage_files。",
                    should_reflect_code=False,
                    should_retry_verifier=True,
                    should_recover_env=False,
                    verifier_action=action_name,
                )

            return results, VerificationReport(
                status="not_run",
                failure_type="none",
                summary="环境已恢复，可以继续执行当前目标。",
                should_reflect_code=False,
                should_retry_verifier=True,
                should_recover_env=False,
                verifier_action=action_name,
            )

        if action_name == "restage_files":
            failures: List[Dict[str, Any]] = []
            failures.extend(
                self._stage_workspace_files(
                    read_only_context=read_only_context,
                    editable_context=editable_context,
                )
            )
            failures.extend(
                self._stage_file_edits_from_coder(
                    assistant_output=assistant_output,
                )
            )

            if not failures:
                results = [
                    {
                        "command": "restage_files",
                        "success": True,
                        "exit_code": 0,
                        "stdout": "files restaged successfully",
                        "stderr": "",
                        "failure_type": "none",
                        "failure_reason": "",
                    }
                ]
                self.add_to_observation(
                    "restage_files",
                    json.dumps(results, ensure_ascii=False, indent=2, default=str),
                )
                return results, VerificationReport(
                    status="not_run",
                    failure_type="none",
                    summary="文件重新同步成功，可以继续当前目标。",
                    should_reflect_code=False,
                    should_retry_verifier=True,
                    should_recover_env=False,
                    verifier_action=action_name,
                )

            return failures, self._summarize_results(
                failures,
                verifier_action=action_name,
            )

        if action_name == "rerun_last_command":
            if not self.last_commands:
                results = [
                    {
                        "command": "rerun_last_command",
                        "success": False,
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": "no previous commands to rerun",
                        "failure_type": "command_failed",
                        "failure_reason": "no previous commands",
                    }
                ]
                self.add_to_observation(
                    "rerun_last_command",
                    json.dumps(results, ensure_ascii=False, indent=2, default=str),
                )
                return results, self._summarize_results(results, verifier_action=action_name)

            results = self._run_single_command(self.last_commands[0])
            return results, self._summarize_results(
                results,
                verifier_action=action_name,
                success_status="passed" if not self.pending_goals else "not_run",
                success_summary=(
                    "重跑上一轮验证命令成功，当前所有目标已完成。"
                    if len(self.pending_goals) <= 1
                    else "重跑上一轮验证命令成功，可以继续推进下一个目标。"
                ),
                success_should_retry_verifier=(len(self.pending_goals) > 1),
            )

        if action_name == "run_checks":
            if not self.current_goal:
                self._set_initial_goals_if_needed(action.cmd_list)

            if not self.current_goal:
                return [], VerificationReport(
                    status="failed",
                    failure_type="command_failed",
                    summary="verifier 没有拿到可执行的 current_goal。",
                    should_reflect_code=False,
                    should_retry_verifier=True,
                    should_recover_env=False,
                    verifier_action=action_name,
                )

            results = self._run_single_command(self.current_goal)
            report = self._summarize_results(results, verifier_action=action_name)

            if report.status == "passed":
                finished_goal = self.current_goal
                self._advance_goal()

                if self.current_goal:
                    return results, VerificationReport(
                        status="not_run",
                        failure_type="none",
                        summary=(
                            f"当前目标已完成：{finished_goal}\n"
                            f"下一步继续推进目标：{self.current_goal}"
                        ),
                        should_reflect_code=False,
                        should_retry_verifier=True,
                        should_recover_env=False,
                        verifier_action=action_name,
                    )

                return results, VerificationReport(
                    status="passed",
                    failure_type="none",
                    summary="所有线性验证目标都已完成。",
                    should_reflect_code=False,
                    should_retry_verifier=False,
                    should_recover_env=False,
                    verifier_action=action_name,
                )

            return results, report

        if action_name == "report_code_error":
            return [], VerificationReport(
                status="failed",
                failure_type="code_failed",
                summary=action.message or "verifier 已明确判断当前问题属于代码错误。",
                should_reflect_code=True,
                should_retry_verifier=False,
                should_recover_env=False,
                verifier_action=action_name,
            )

        if action_name == "report_env_error":
            return [], VerificationReport(
                status="failed",
                failure_type="environment_failed",
                summary=action.message or "verifier 已明确判断当前问题属于环境错误。",
                should_reflect_code=False,
                should_retry_verifier=False,
                should_recover_env=True,
                verifier_action=action_name,
            )

        if action_name == "finish":
            if self.current_goal or self.pending_goals:
                return [], VerificationReport(
                    status="not_run",
                    failure_type="none",
                    summary="仍有未完成的线性验证目标，不能提前 finish。",
                    should_reflect_code=False,
                    should_retry_verifier=True,
                    should_recover_env=False,
                    verifier_action=action_name,
                )

            return [], VerificationReport(
                status="passed",
                failure_type="none",
                summary=action.message or "verifier 已完成当前验证。",
                should_reflect_code=False,
                should_retry_verifier=False,
                should_recover_env=False,
                verifier_action=action_name,
            )

        results = [
            {
                "command": f"unknown_action::{action_name}",
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"unknown verifier action: {action_name}",
                "failure_type": "command_failed",
                "failure_reason": "unknown verifier action",
            }
        ]
        self.add_to_observation(
            "unknown_action",
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
        )
        return results, self._summarize_results(results, verifier_action=action_name)

    # ============================================================
    # Main loop
    # ============================================================
    def work(
        self,
        assistant_output: Dict[str, Any],
        read_only_context: Optional[Dict[str, str]] = None,
        editable_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        self.reset_round_state()
    
        stage_failures: List[Dict[str, Any]] = []
        stage_failures.extend(
            self._stage_workspace_files(
                read_only_context=read_only_context,
                editable_context=editable_context,
            )
        )
        stage_failures.extend(
            self._stage_file_edits_from_coder(
                assistant_output=assistant_output,
            )
        )
    
        if stage_failures:
            self._tool_warning("初始 staging 出现问题，已写入 observation，交给 verifier 继续判断。")
        else:
            self._tool_output("初始 staging 完成。")
    
        retry_count = 0
        last_report = VerificationReport(
            status="not_run",
            failure_type="none",
            summary="",
        )
        last_results: List[Dict[str, Any]] = []
    
        current_action: Optional[VerifyAction] = None
        action_trace: List[Dict[str, Any]] = []
    
        while True:
            current_action = self.decide_next_action(
                assistant_output=assistant_output,
                read_only_context=read_only_context,
                editable_context=editable_context,
                retry=(retry_count > 0),
            )
    
            action_trace.append(current_action.model_dump())
    
            if current_action.action == "run_checks" and not self.pending_goals and current_action.cmd_list:
                self._set_initial_goals_if_needed(current_action.cmd_list)
    
            self._tool_output(
                "verifier 动作 = "
                f"{current_action.action} | guess = {current_action.failure_type_guess} | "
                f"confidence = {current_action.confidence} | current_goal = {self.current_goal or '[none]'}"
            )
    
            last_results, last_report = self.execute_action(
                action=current_action,
                assistant_output=assistant_output,
                read_only_context=read_only_context,
                editable_context=editable_context,
            )
    
            if last_report.status == "passed":
                self._tool_output("verifier 判断当前验证已完成。")
                break
            
            if last_report.failure_type == "code_failed":
                self._tool_warning("verifier 已确认当前失败属于代码错误，交回 coder。")
                break
            
            if not last_report.should_retry_verifier:
                self._tool_warning("当前状态不再继续 verifier planning loop。")
                break
            
            if retry_count >= self.max_reflection:
                self._tool_warning("verifier 已达到最大 planning loop 次数。")
                break
            
            retry_count += 1
            self._tool_warning(f"verifier 开始第 {retry_count} 次重新规划。")
    
        return {
            "verifier_message": last_report.summary,
            "commands": list(self.last_commands),
            "current_goal": self.current_goal,
            "pending_goals": list(self.pending_goals),
            "command_results": last_results,
            "observation": list(self.observation),
            "verification_report": last_report.model_dump(),
            "failure_type": last_report.failure_type,
    
            # 最后一轮真正执行的 action
            "final_action": current_action.model_dump() if current_action else {},
    
            # 整个 verifier loop 中所有 action 的轨迹
            "action_trace": action_trace,
        }
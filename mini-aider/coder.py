from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ioio import InputOutput as IO
from history import ChatHistory
from states import AssistantAction
from prompt import CoderPrompts
import difflib
import html
from html_render import *

Message = Dict[str, str]


class Coder:
    """
    核心 agent：
    - 管理文件上下文
    - 构造 prompt
    - 调模型生成代码修改方案
    - 维护当前任务轨迹（cur_messages）
    - 调 verifier 做验证
    - 根据 verifier 的 observation 决定是否 reflection

    约定：
    - cur_messages: 当前任务中的临时轨迹（trajectory），任务未结束就持续保留并追加
    - done_messages: 已完成且值得保留的消息
    - coder 不直接负责 runtime/sandbox 的同步与执行细节
    - runtime / verifier 负责验证、执行、环境问题处理
    """

    def __init__(
        self,
        model: Any,
        verifier: Any = None,
        fnames: Optional[List[str]] = None,
        read_only_fnames: Optional[List[str]] = None,
        edit_fnames: Optional[List[str]] = None,
        show_diffs: bool = True,
        verbose: bool = False,
        cur_messages: Optional[List[Message]] = None,
        done_messages: Optional[List[Message]] = None,
        auto_test: bool = False,
        test_cmd: Optional[str] = None,
        suggest_shell_commands: bool = True,
        input_history_file: Optional[str] = None,
        chat_history_file: Optional[str] = None,
        llm_history_file: Optional[str] = None,
        input_func=input,
        output_func=print,
        encoding: str = "utf-8",
        root: str = ".",
        commander: Any = None,
        max_reflection: int = 2,
        use_sandbox: bool = False,
        sandbox_cwd: str = "/home/user",
        connect_sandbox_id: Optional[str] = None,
    ) -> None:
        self.model = model
        self.verbose = verbose
        self.show_diffs = show_diffs
        self.auto_test = auto_test
        self.test_cmd = test_cmd
        self.suggest_shell_commands = suggest_shell_commands
        self.root = str(Path(root).resolve())

        self.use_sandbox = use_sandbox
        self.sandbox_cwd = sandbox_cwd
        self.connect_sandbox_id = connect_sandbox_id

        self.message_tokens_sent = 0
        self.message_tokens_received = 0

        self.abs_fnames: Dict[str, str] = {}
        self.abs_read_only_fnames: Dict[str, str] = {}
        self.abs_edit_fnames: Dict[str, str] = {}

        self.cur_messages: List[Message] = list(cur_messages or [])
        self.done_messages: List[Message] = list(done_messages or [])
        self.need_reflection = False
        self.max_reflection = max_reflection
        self.cur_reflection_count = 0

        self.commander = commander
        self.verifier = verifier

        self.io = IO(
            input_history_file=input_history_file,
            chat_history_file=chat_history_file,
            llm_history_file=llm_history_file,
            input_func=input_func,
            output_func=output_func,
            encoding=encoding,
            root=root,
        )

        self.history = ChatHistory()
        self.prompts = CoderPrompts()

        self._add_file(fnames or [], self.abs_fnames)
        self._add_file(read_only_fnames or [], self.abs_read_only_fnames)
        self._add_file(edit_fnames or [], self.abs_edit_fnames)

    # ------------------------------------------------------------
    # 基础注入接口
    # ------------------------------------------------------------
    def set_verifier(self, verifier: Any) -> None:
        self.verifier = verifier

    # ------------------------------------------------------------
    # 文件上下文管理
    # ------------------------------------------------------------
    def _resolve_path(self, fname: str) -> str:
        path = Path(fname)
        if not path.is_absolute():
            path = Path(self.root) / path
        resolved_path = str(path.resolve())

        # 如果使用沙箱，返回相对于项目根的路径
        if self.use_sandbox:
            project_root = Path(self.root).resolve()
            try:
                rel_path = Path(resolved_path).relative_to(project_root)
                return str(rel_path)
            except ValueError:
                # 如果文件不在项目根目录下，返回原始路径
                return resolved_path
        return resolved_path
    
    def _add_file(self, fnames: List[str], target_dict: Dict[str, str]) -> None:
        for fname in fnames:
            if not fname:
                continue
            target_dict[fname] = self._resolve_path(fname)

    def _read_file(
        self,
        file_names: Optional[List[str]] = None,
        source_dict: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        context: Dict[str, str] = {}
        source = source_dict or self.abs_fnames

        if file_names is None:
            file_names = list(source.keys())

        for fname in file_names:
            abs_path = source.get(fname) or self.abs_fnames.get(fname)
            if not abs_path:
                self.io.tool_warning(f"找不到文件 {fname} 的路径")
                continue

            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    context[fname] = f.read()
            except FileNotFoundError:
                self.io.tool_warning(f"找不到文件: {fname}")
            except PermissionError:
                self.io.tool_warning(f"没有权限打开文件: {fname}")
            except UnicodeDecodeError:
                self.io.tool_warning(f"文件无法按 utf-8 解码: {fname}")
            except Exception as e:
                self.io.tool_warning(f"读取文件 {fname} 失败: {e}")

        return context

    # ------------------------------------------------------------
    # 输入初始化
    # ------------------------------------------------------------
    def get_input(
        self,
        source_type: str = "cmd",
        txt_path: Optional[str] = None,
        prompt: str = "你觉得哪里不妥？或者你需要问啥？：",
    ) -> tuple[Dict[str, str], Dict[str, str], str]:
        user_input = self.io.get_user_input(
            source_type=source_type,
            txt_path=txt_path,
            prompt=prompt,
        )

        if self.io.all_file_names:
            editable_fnames = [
                fn for fn in self.io.all_file_names
                if fn not in self.io.read_only_fnames
            ]

            self._add_file(self.io.all_file_names, self.abs_fnames)
            self._add_file(self.io.read_only_fnames, self.abs_read_only_fnames)
            self._add_file(editable_fnames, self.abs_edit_fnames)

        read_only_context = self._read_file(source_dict=self.abs_read_only_fnames)
        editable_context = self._read_file(source_dict=self.abs_edit_fnames)

        return read_only_context, editable_context, user_input

    # ------------------------------------------------------------
    # Prompt 构造
    # ------------------------------------------------------------
    def _default_system_prompt(self) -> str:
        return self.prompts.build_coder_system_prompt(
            suggest_shell_commands=self.suggest_shell_commands
        )

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

    def construct_messages(
        self,
        read_only_context: Optional[Dict[str, str]] = None,
        editable_context: Optional[Dict[str, str]] = None,
    ) -> List[Message]:
        messages: List[Message] = [
            {"role": "system", "content": self._default_system_prompt()}
        ]

        if read_only_context:
            messages.append(
                {
                    "role": "system",
                    "content": self.prompts.read_only_files_prefix.strip()
                    + "\n\n"
                    + self._format_context_block("Read-only file context", read_only_context),
                }
            )

        if editable_context:
            messages.append(
                {
                    "role": "system",
                    "content": self.prompts.files_content_prefix.strip()
                    + "\n\n"
                    + self._format_context_block("Editable file context", editable_context),
                }
            )

        return messages

    def _build_messages_for_attempt(
        self,
        user_input: str,
        read_only_context: Optional[Dict[str, str]] = None,
        editable_context: Optional[Dict[str, str]] = None,
    ) -> List[Message]:
        """
        每一次 attempt 都重新构造完整 messages：

        system/context
        + done_messages
        + user_input
        + cur_messages

        注意：
        cur_messages 表示当前任务尚未完成时的轨迹，不会在 reflection 中被清空。
        """
        messages = self.construct_messages(
            read_only_context=read_only_context,
            editable_context=editable_context,
        )

        if self.done_messages:
            messages.extend(self.done_messages)

        messages.append({"role": "user", "content": user_input})

        if self.cur_messages:
            messages.extend(self.cur_messages)

        return messages

    # ------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------
    def _extract_text_from_response(self, response: Any) -> Dict[str, Any]:
        if hasattr(response, "model_dump"):
            payload = response.model_dump(exclude_none=True)
        elif isinstance(response, dict):
            payload = response
        else:
            payload = {
                "action": getattr(response, "action", "continue"),
                "message": getattr(response, "message", ""),
                "task_summary": getattr(response, "task_summary", ""),
                "validation_summary": getattr(response, "validation_summary", ""),
                "file_edits": getattr(response, "file_edits", []),
            }

        return {
            "action": payload.get("action", "continue"),
            "message": payload.get("message", ""),
            "task_summary": payload.get("task_summary", ""),
            "validation_summary": payload.get("validation_summary", ""),
            "file_edits": payload.get("file_edits", []) or [],
        }

    def send_action(self, messages: List[Message]) -> AssistantAction:
        try:
            self.io.tool_output("正在发送结构化消息...")
            action = self.model.invoke_structured(
                messages,
                schema=AssistantAction,
            )

            self.io.append_llm_history(
                json.dumps(
                    action.model_dump(exclude_none=True),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            )
            return action

        except Exception as e:
            self.io.tool_warning(f"结构化模型调用失败: {e}")
            raise

    # ------------------------------------------------------------
    # 当前任务轨迹管理
    # ------------------------------------------------------------
    def add_assistant_action_trace(self, assistant_output: Dict[str, Any]) -> None:
        """
        把 assistant 本轮的结构化输出加入当前任务轨迹。
        只要任务没结束，这些轨迹都应该保留。
        """
        trace = json.dumps(
            assistant_output,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        self.cur_messages.append(
            {
                "role": "assistant",
                "content": trace,
            }
        )

    def add_tool_observation(self, tool_name: str, observation: str) -> None:
        self.cur_messages.append(
            {
                "role": "system",
                "content": f"[tool:{tool_name}]\n{observation}",
            }
        )

    def add_reflection_prompt(self, verifier_output: Optional[Dict[str, Any]]) -> None:
        if not verifier_output:
            return

        verification_report = verifier_output.get("verification_report", {}) or {}
        failure_type = verification_report.get("failure_type", "unknown_failed")
        summary = verification_report.get("summary", "")
        verifier_action = verifier_output.get("verifier_action", {}) or {}

        reflection_text = self.prompts.reflection_retry_prompt.strip()
        extra = (
            "\n\nVerifier structured feedback:\n"
            + json.dumps(
                {
                    "failure_type": failure_type,
                    "summary": summary,
                    "verification_report": verification_report,
                    "verifier_action": verifier_action,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )

        self.cur_messages.append(
            {
                "role": "system",
                "content": reflection_text + extra,
            }
        )

    def _archive_current_task(
        self,
        user_input: str,
        final_output: Dict[str, Any],
    ) -> None:
        """
        当前任务结束后，把本轮用户输入 + 最终 assistant 输出归档到 done_messages，
        并清空 cur_messages。
        """
        assistant_summary = json.dumps(
            final_output,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

        turn = [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": assistant_summary},
        ]
        self.done_messages.extend(turn)
        self.cur_messages.clear()

    # ------------------------------------------------------------
    # reflection / verifier 结果判断
    # ------------------------------------------------------------

    def _get_verification_report(
        self,
        verifier_output: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not verifier_output:
            return {}
        return verifier_output.get("verification_report", {}) or {}

    def _should_reflect(
        self,
        verifier_output: Optional[Dict[str, Any]],
    ) -> bool:
        report = self._get_verification_report(verifier_output)
        if not report:
            return False

        failure_type = report.get("failure_type", "none")
        should_reflect_code = bool(report.get("should_reflect_code", False))

        return failure_type == "code_failed" and should_reflect_code

    # ------------------------------------------------------------
    # 对外主流程
    # ------------------------------------------------------------
    def run_one_turn(
        self,
        source_type: str = "cmd",
        txt_path: Optional[str] = None,
        prompt: str = "你觉得哪里不妥？或者你需要问啥？：",
    ) -> Dict[str, Any]:
        read_only_context, editable_context, user_input = self.get_input(
            source_type=source_type,
            txt_path=txt_path,
            prompt=prompt,
        )
        self.io.tool_output("已完成代码文件上下文读取")

        self.need_reflection = False
        self.cur_reflection_count = 0

        final_output: Dict[str, Any] = {}

        while True:
            messages = self._build_messages_for_attempt(
                user_input=user_input,
                read_only_context=read_only_context,
                editable_context=editable_context,
            )
            self.io.tool_output("已完成消息构造")

            raw = self.send_action(messages)
            assistant_output = self._extract_text_from_response(raw)

            self.io.tool_output("已接收到 assistant 输出，将输出写入当前轨迹...")
            self.add_assistant_action_trace(assistant_output)

            verifier_output = None
            if self.verifier is not None:
                verifier_output = self.verifier.work(
                    assistant_output=assistant_output,
                    read_only_context=read_only_context,
                    editable_context=editable_context,
                )

            if verifier_output:
                assistant_output["verifier_message"] = verifier_output.get("verifier_message", "")
                assistant_output["command_results"] = verifier_output.get("command_results", [])
                assistant_output["commands"] = verifier_output.get("commands", [])
                assistant_output["observation"] = verifier_output.get("observation", [])
                assistant_output["verification_report"] = verifier_output.get("verification_report", {})
                assistant_output["failure_type"] = verifier_output.get("failure_type", "none")
                assistant_output["verifier_action"] = verifier_output.get("verifier_action", {})

            if self.use_sandbox and self.commander is not None and hasattr(self.commander, "sandbox_id"):
                assistant_output["sandbox_id"] = getattr(self.commander, "sandbox_id", None)

            if self.commander is not None and hasattr(self.commander, "runtime"):
                runtime = getattr(self.commander, "runtime", None)
                if runtime is not None:
                    assistant_output["runtime_state"] = {
                        "sandbox_id": getattr(runtime, "sandbox_id", None),
                        "cwd": getattr(runtime, "cwd", None),
                        "workspace_epoch": getattr(runtime, "workspace_epoch", None),
                        "requires_restage": getattr(runtime, "requires_restage", None),
                        "last_error": getattr(runtime, "last_error", ""),
                        "staged_files": sorted(list(getattr(runtime, "staged_files", set()))),
                    }

            final_output = assistant_output

            action = assistant_output.get("action", "")
            verification_report = assistant_output.get("verification_report", {}) or {}
            failure_type = verification_report.get("failure_type", "none")
            should_reflect = self._should_reflect(verifier_output)

            if action == "finish":
                self.io.tool_output("模型返回 finish，结束当前任务。")
                break

            if failure_type == "environment_failed":
                self.io.tool_warning("本轮为环境故障，不进入 coder reflection。")
                break

            if failure_type == "command_failed":
                self.io.tool_warning("本轮为 verifier 的验证计划/命令问题，不进入 coder 代码 reflection。")
                break

            if failure_type == "unknown_failed":
                self.io.tool_warning("本轮失败归因不明，先不让 coder 修改业务代码。")
                break

            if not should_reflect:
                self.io.tool_output("本轮已完成验证决策，结束当前任务。")
                break

            if self.cur_reflection_count >= self.max_reflection:
                self.io.tool_warning("已达到最大 reflection 次数，停止继续反思。")
                break

            self.need_reflection = True
            self.cur_reflection_count += 1
            self.io.tool_warning(
                f"检测到代码级验证失败，进入第 {self.cur_reflection_count} 次 reflection。"
            )

            self.add_reflection_prompt(verifier_output)
            continue

        self._archive_current_task(user_input, final_output)
        self.io.append_chat_history(
            "[user]\n"
            + user_input
            + "\n\n[assistant]\n"
            + json.dumps(final_output, ensure_ascii=False, indent=2, default=str)
            + "\n\n"
        )
        self.show_diff(final_output.get("file_edits", []))
        return final_output

    # ============================================================
    # 预留接口
    # ============================================================
    def human_reply(self) -> None:
        pass

    def apply_message(self) -> None:
        pass

    def show_diff(self, file_edits, auto_open: bool = True) -> None:
        if not self.show_diffs:
            return

        diff_dir = Path(self.root) / "diffs"
        diff_dir.mkdir(parents=True, exist_ok=True)

        html_sections: List[str] = []
        changed_count = 0

        for edit in file_edits:
            if isinstance(edit, dict):
                file_name = edit["file_name"]
                content = edit["content"]
            else:
                file_name = edit.file_name
                content = edit.content

            file_path = Path(self.root) / file_name

            if file_path.exists():
                old_content = file_path.read_text(encoding="utf-8")
            else:
                old_content = ""

            new_content = content

            diff_lines = list(
                difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=f"a/{file_name}",
                    tofile=f"b/{file_name}",
                    n=3,
                )
            )

            diff_text = "".join(diff_lines).strip()

            if not diff_text:
                section = f"""
                <section class="file-block">
                    <h2>{html.escape(file_name)}</h2>
                    <div class="meta">无内容变化</div>
                </section>
                """
            else:
                changed_count += 1
                section = f"""
                <section class="file-block">
                    <h2>{html.escape(file_name)}</h2>
                    <pre class="diff-block">{html.escape(diff_text)}</pre>
                </section>
                """

            html_sections.append(section)

        page_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>File Diffs</title>
            <style>
                body {{ font-family: monospace; margin: 20px; }}
                .file-block {{ margin-bottom: 20px; border: 1px solid #ccc; padding: 10px; }}
                .diff-block {{ background-color: #f8f8f8; overflow-x: auto; }}
                .meta {{ color: #666; font-style: italic; }}
                h2 {{ margin-top: 0; }}
            </style>
        </head>
        <body>
            <h1>文件差异对比</h1>
            {''.join(html_sections)}
        </body>
        </html>
        """

        output_path = diff_dir / "index.html"
        output_path.write_text(page_html, encoding="utf-8")

        opened = False
        if auto_open and not self.use_sandbox:
            import webbrowser
            try:
                webbrowser.open(output_path.as_uri())
                opened = True
            except Exception:
                opened = False

        msg_lines = [
            f"已生成 diff 页面：{output_path}",
            f"共 {len(file_edits)} 个文件，{changed_count} 个文件存在实际改动。",
        ]

        if opened:
            msg_lines.append("已尝试自动打开浏览器。")
        else:
            if self.use_sandbox:
                msg_lines.append("当前为 sandbox 模式，通常无法自动在本机打开浏览器。")
            else:
                msg_lines.append("未自动打开浏览器，请手动打开该 HTML 文件查看。")

        self.io.tool_output("\n".join(msg_lines))

    def close(self) -> None:
        pass
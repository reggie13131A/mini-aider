from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
import os
import re

class InputOutput:
    """
    纯 IO 层：
    - 读取用户输入
    - 读取 txt 配置式输入
    - 追加日志文件
    - 打印 warning / info

    不负责：
    - 决定哪些历史喂给模型
    - 做 summary
    - 管理 done_messages
    """

    def __init__(
        self,
        input_history_file: Optional[str] = None,
        chat_history_file: Optional[str] = None,
        llm_history_file: Optional[str] = None,
        input_func: Optional[Callable[[str], str]] = None,
        output_func: Optional[Callable[..., None]] = None,
        encoding: str = "utf-8",
        root: str = ".",
    ) -> None:
        self.input_func = input_func or input
        self.output_func = output_func or print

        self.input_history_file = input_history_file
        self.chat_history_file = Path(chat_history_file) if chat_history_file else None
        self.llm_history_file = llm_history_file

        self.user_input = ""
        self.all_file_names: list[str] = []
        self.read_only_fnames: list[str] = []

        self.encoding = encoding
        self.root = str(Path(root).resolve())

        self._ensure_parent(self.input_history_file)
        self._ensure_parent(self.chat_history_file)
        self._ensure_parent(self.llm_history_file)

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.append_chat_history(f"\n# mini-codex started at {current_time}\n\n")

    def _ensure_parent(self, file_path: Optional[str | Path]) -> None:
        if not file_path:
            return
        try:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as e:
            self.tool_warning(f"Could not create directory for {file_path}: {e}")

    def tool_warning(self, message: str) -> None:
        self.output_func(f"[warning] {message}")

    def tool_output(self, message: str) -> None:
        self.output_func(message)

    def _append_text(self, file_path: Optional[str | Path], text: str) -> None:
        if not file_path:
            return
        try:
            with Path(file_path).open("a", encoding=self.encoding) as f:
                f.write(text)
        except Exception as e:
            self.tool_warning(f"Failed to append to {file_path}: {e}")

    def append_chat_history(self, text: str) -> None:
        self._append_text(self.chat_history_file, text)

    def append_input_history(self, text: str) -> None:
        self._append_text(self.input_history_file, text)

    def append_llm_history(self, text: str) -> None:
        self._append_text(self.llm_history_file, text)

    def get_user_input(
        self,
        source_type: str,
        txt_path: Optional[str] = None,
        prompt: str = "你觉得哪里不妥？或者你需要问啥？：",
    ) -> str:
        if source_type == "txt":
            if not txt_path:
                raise ValueError("source_type='txt' 时必须提供 txt_path")
            return self._parse_txt_input(txt_path)

        if source_type == "cmd":
            return self._get_cmd_input(prompt)

        raise ValueError(f"Invalid source_type: {source_type}")

    def _parse_txt_input(self, txt_path: str) -> str:
        if not os.path.exists(txt_path):
            raise FileNotFoundError(f"{txt_path} does not exist.")

        with open(txt_path, "r", encoding=self.encoding) as f:
            content = f.read()

        user_input_match = re.search(r"user_input:\s*(.*)", content)
        if user_input_match:
            self.user_input = user_input_match.group(1).strip()
        else:
            self.user_input = ""

        context_file_match = re.search(r"context_file:\s*(.*)", content)
        if context_file_match:
            context_str = context_file_match.group(1).strip()
            self.all_file_names = [fn.strip() for fn in context_str.split(",") if fn.strip()]
        else:
            self.all_file_names = []

        read_only_match = re.search(r"read_only_files:\s*(.*?)(?=\n\S|$)", content, re.DOTALL)
        if read_only_match:
            read_only_block = read_only_match.group(1).strip()
            read_only_lines = re.findall(r"- (.*)", read_only_block)
            self.read_only_fnames = [fn.strip() for fn in read_only_lines if fn.strip()]
        else:
            self.read_only_fnames = []

        self.append_input_history(f"[txt_input] {self.user_input}\n")
        return self.user_input

    def _get_cmd_input(self, prompt: str) -> str:
        try:
            cmd_input = self.input_func(prompt).strip()
        except EOFError:
            cmd_input = ""
        except KeyboardInterrupt:
            cmd_input = ""

        self.user_input = cmd_input

        if cmd_input:
            self.append_input_history(f"[cmd_input] {cmd_input}\n")
        else:
            self.tool_warning("啥也没输入，用户指令为空")

        return self.user_input





    
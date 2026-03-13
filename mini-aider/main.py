from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import dotenv
from langchain_openai import ChatOpenAI

from coder import Coder
from verifier import Verifier
from commands import BaseCommander, LocalCommander, SandboxCommander

Message = Dict[str, str]


class ChatOpenAIAdapter:
    """
    适配当前 coder.py / verifier.py 的消息格式：
    统一转换成 ChatOpenAI 可接受的：
    - {"role": "...", "content": "..."}
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        temperature: float = 0.2,
        timeout: int = 60,
        max_retries: int = 2,
    ) -> None:
        kwargs = {
            "model": model,
            "api_key": api_key,
            "temperature": temperature,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if base_url:
            kwargs["base_url"] = base_url

        self.llm = ChatOpenAI(**kwargs)

    def with_structured_output(self, schema: Any, method: Optional[str] = None):
        if method is not None:
            return self.llm.with_structured_output(schema, method=method)
        return self.llm.with_structured_output(schema)

    def _normalize_messages(self, messages: List[Message]) -> List[Message]:
        normalized: List[Message] = []

        for msg in messages:
            role = str(msg.get("role", "user")).strip().lower()
            content = (
                msg.get("content")
                or msg.get("prompt")
                or msg.get("context")
                or ""
            )
            normalized.append(
                {
                    "role": role,
                    "content": str(content),
                }
            )

        return normalized

    def invoke(self, messages: List[Message]) -> Any:
        normalized = self._normalize_messages(messages)
        return self.llm.invoke(normalized)

    def invoke_structured(
        self,
        messages: List[Message],
        schema: Any,
        method: Optional[str] = None,
    ) -> Any:
        normalized = self._normalize_messages(messages)

        if method is not None:
            structured_llm = self.llm.with_structured_output(schema, method=method)
        else:
            structured_llm = self.llm.with_structured_output(schema)

        return structured_llm.invoke(normalized)


def project_root() -> Path:
    return Path(__file__).resolve().parent


def resolve_path(path_str: str, base_dir: Optional[Path] = None) -> str:
    path = Path(path_str)
    if not path.is_absolute():
        base = base_dir or project_root()
        path = base / path
    return str(path.resolve())


def ensure_dir(file_path: Optional[str]) -> None:
    if not file_path:
        return
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)


def load_project_env(env_path: Optional[str] = None) -> Path:
    """
    默认加载上一级目录中的 .env
    """
    if env_path:
        env_file = Path(resolve_path(env_path))
    else:
        env_file = project_root().parent / ".env"

    if not env_file.exists():
        raise FileNotFoundError(f"未找到 .env 文件: {env_file}")

    dotenv.load_dotenv(dotenv_path=env_file, override=False)
    return env_file


def build_model(args: argparse.Namespace) -> ChatOpenAIAdapter:
    env_file = load_project_env(args.env_file)

    api_key = (
        os.getenv("DASHSCOPE_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    base_url = (
        os.getenv("DASHSCOPE_BASE_URL", "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or None
    )

    if not api_key:
        raise ValueError(
            f"未检测到 API KEY，请检查环境变量或 .env 文件是否正确加载：{env_file}"
        )

    return ChatOpenAIAdapter(
        model=args.model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=args.temperature,
        timeout=args.timeout,
        max_retries=args.max_retries,
    )


def build_commander(args: argparse.Namespace, root: str) -> BaseCommander:
    """
    runtime 层统一持有 commander：
    - sandbox 模式：创建 / 连接共享沙箱
    - local 模式：直接使用本地执行器
    """
    if args.use_sandbox:
        return SandboxCommander(
            sandbox_cwd=args.sandbox_cwd,
            timeout=args.timeout,
            connect_sandbox_id=args.connect_sandbox_id,
        )

    return LocalCommander(root=root)


def build_coder(
    model: Any,
    commander: BaseCommander,
    verifier: Optional[Verifier],
    args: argparse.Namespace,
) -> Coder:
    root = resolve_path(args.root)

    chat_history_file = resolve_path(args.chat_history_file, Path(root))
    input_history_file = resolve_path(args.input_history_file, Path(root))
    llm_history_file = resolve_path(args.llm_history_file, Path(root))

    ensure_dir(chat_history_file)
    ensure_dir(input_history_file)
    ensure_dir(llm_history_file)

    return Coder(
        model=model,
        verifier=verifier,
        fnames=[],
        read_only_fnames=[],
        edit_fnames=[],
        show_diffs=args.show_diffs,
        verbose=args.verbose,
        cur_messages=[],
        done_messages=[],
        auto_test=False,
        test_cmd=None,
        suggest_shell_commands=args.suggest_shell_commands,
        input_history_file=input_history_file,
        chat_history_file=chat_history_file,
        llm_history_file=llm_history_file,
        root=root,
        commander=commander,
        max_reflection=args.max_reflection,
        use_sandbox=args.use_sandbox,
        sandbox_cwd=args.sandbox_cwd,
        connect_sandbox_id=args.connect_sandbox_id,
    )


def build_verifier(
    model: Any,
    commander: BaseCommander,
    args: argparse.Namespace,
    io: Optional[Any] = None,
) -> Verifier:
    return Verifier(
        model=model,
        commander=commander,
        use_sandbox=args.use_sandbox,
        io=io,
        max_reflection=args.verifier_max_reflection,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mini-codex runner")

    parser.add_argument(
        "--root",
        type=str,
        default=str(project_root()),
        help="项目根目录。默认是当前 mini-codex 目录。",
    )
    parser.add_argument(
        "--env_file",
        type=str,
        default="",
        help="显式指定 .env 文件路径；为空时默认使用上一级目录下的 .env。",
    )

    parser.add_argument(
        "--source_type",
        type=str,
        default="txt",
        choices=["txt", "cmd"],
        help="用户输入来源类型。",
    )
    parser.add_argument(
        "--txt_path",
        type=str,
        default=str(project_root() / "mooc.txt"),
        help="当 source_type=txt 时，输入文本文件路径。",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default="你觉得哪里不妥？或者你需要问啥？：",
        help="当 source_type=cmd 时的命令行提示语。",
    )

    parser.add_argument(
        "--chat_history_file",
        type=str,
        default="logs/chat_history.txt",
        help="聊天历史文件路径（相对 root 或绝对路径）。",
    )
    parser.add_argument(
        "--input_history_file",
        type=str,
        default="logs/input_history.txt",
        help="输入历史文件路径（相对 root 或绝对路径）。",
    )
    parser.add_argument(
        "--llm_history_file",
        type=str,
        default="logs/llm_history.txt",
        help="LLM 历史文件路径（相对 root 或绝对路径）。",
    )

    parser.add_argument(
        "--model_name",
        type=str,
        default="qwen-plus",
        help="模型名称。",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.3,
        help="模型温度。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="模型调用 / 命令执行超时时间（秒）。",
    )
    parser.add_argument(
        "--max_retries",
        type=int,
        default=2,
        help="模型调用最大重试次数。",
    )

    parser.add_argument(
        "--use_sandbox",
        action="store_true",
        default=True,
        help="启用 sandbox 模式。",
    )
    parser.add_argument(
        "--sandbox_cwd",
        type=str,
        default="/tmp/mini_codex",
        help="sandbox 中的工作目录。",
    )
    parser.add_argument(
        "--connect_sandbox_id",
        type=str,
        default="",
        help="连接已有 sandbox_id；为空则新建沙箱。",
    )

    parser.add_argument(
        "--max_reflection",
        type=int,
        default=2,
        help="coder 最大 reflection 次数。",
    )
    parser.add_argument(
        "--verifier_max_reflection",
        type=int,
        default=10,
        help="verifier 内部最大 reflection 次数。",
    )

    parser.add_argument(
        "--show_diffs",
        action="store_true",
        default=True,
        help="是否展示 diff（预留）。",
    )
    parser.add_argument(
        "--suggest_shell_commands",
        action="store_true",
        default=True,
        help="是否允许 agent 生成 shell 命令。",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出更多调试信息。",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    coder: Optional[Coder] = None
    commander: Optional[BaseCommander] = None

    try:
        root = resolve_path(args.root)

        model = build_model(args)
        commander = build_commander(args, root=root)

        # 先创建 coder，再把 coder.io 注给 verifier，再把 verifier 挂回 coder
        coder = build_coder(
            model=model,
            commander=commander,
            verifier=None,
            args=args,
        )

        verifier = build_verifier(
            model=model,
            commander=commander,
            args=args,
            io=coder.io,
        )

        if hasattr(coder, "set_verifier"):
            coder.set_verifier(verifier)
        else:
            coder.verifier = verifier

        if args.use_sandbox and hasattr(commander, "sandbox_id"):
            sandbox_id = getattr(commander, "sandbox_id", None)
            coder.io.tool_output(f"当前为 sandbox 模式，共享 sandbox_id = {sandbox_id}")
        else:
            coder.io.tool_output("当前为 local 模式。")

        coder.run_one_turn(
            source_type=args.source_type,
            txt_path=args.txt_path,
            prompt=args.prompt,
        )

        coder.io.tool_output("任务执行结束。")
        # coder.io.tool_output(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    except KeyboardInterrupt:
        print("\n已中断。")
    except Exception as e:
        print(f"\n程序运行失败：{type(e).__name__}: {e}")
        raise
    finally:
        try:
            if coder is not None:
                coder.close()
        except Exception:
            pass

        try:
            if commander is not None:
                commander.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
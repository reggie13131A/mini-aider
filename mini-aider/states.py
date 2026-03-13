from pydantic import BaseModel, Field
from typing import List, Literal, Optional


FailureType = Literal[
    "none",
    "environment_failed",
    "code_failed",
    "command_failed",
    "unknown_failed",
]

VerifyActionType = Literal[
    "run_checks",          # 正常执行/推进验证
    "recover_env",         # 恢复环境
    "restage_files",       # 重新同步文件
    "rerun_last_command",  # 重跑上一次命令
    "report_code_error",   # 明确判断为代码错误
    "report_env_error",    # 明确判断为环境错误
    "finish",              # 当前验证流程结束
]


class FileEditPlan(BaseModel):
    file_name: str = Field(description="要编辑的目标文件")
    content: str = Field(description="写入的代码内容，不要包含 markdown 代码块")


class RecoveryResult(BaseModel):
    success: bool = Field(description="环境恢复是否成功")
    message: str = Field(default="", description="恢复结果说明")
    mode: Literal["noop", "reconnected", "recreated", "local_check_failed"] = Field(
        default="noop",
        description="恢复模式"
    )
    workspace_preserved: bool = Field(
        default=True,
        description="恢复后工作区内容是否仍然保留"
    )
    requires_restage: bool = Field(
        default=False,
        description="恢复后是否需要重新同步文件"
    )
    sandbox_id: Optional[str] = Field(
        default=None,
        description="恢复后的 sandbox id（如果有）"
    )


class VerifyAction(BaseModel):
    action: VerifyActionType = Field(
        description="verifier 当前这一步决定采取的动作"
    )
    message: str = Field(
        default="",
        description="verifier 对本轮动作的简短解释"
    )
    reason: str = Field(
        default="",
        description="基于 observation 的判断依据"
    )
    cmd_list: List[str] = Field(
        default_factory=list,
        description=(
            "线性验证目标列表。"
            "当 action=run_checks 且当前还没有 pending_goals 时，"
            "程序会把这里当作初始 goal 列表；"
            "实际执行时一次只会执行一个 current_goal。"
        )
    )
    failure_type_guess: FailureType = Field(
        default="none",
        description="基于当前 observation 的失败类型猜测；最终失败类型仍由程序侧归因"
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="verifier 对当前动作判断的置信度"
    )


class VerificationReport(BaseModel):
    status: Literal["passed", "failed", "not_run"] = Field(default="not_run")
    failure_type: FailureType = Field(default="none")
    summary: str = Field(default="")
    should_reflect_code: bool = Field(default=False)
    should_retry_verifier: bool = Field(default=False)
    should_recover_env: bool = Field(default=False)
    verifier_action: str = Field(default="")


class AssistantAction(BaseModel):
    action: Literal["continue", "finish"] = Field(
        default="continue",
        description="当前这一步的动作类型；finish 表示当前任务可结束"
    )
    message: str = Field(description="给用户/系统的简短说明")
    task_summary: str = Field(
        default="",
        description="coder 对当前任务性质的简短摘要，供 verifier 理解任务"
    )
    validation_summary: str = Field(
        default="",
        description="coder 对本轮需要验证什么的摘要说明"
    )
    file_edits: List[FileEditPlan] = Field(default_factory=list)
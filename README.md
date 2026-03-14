mini-aider
一个轻量版的多轮代码修改 Agent，复刻 Aider 核心设计并做轻量化适配，基于消息流实现代码的迭代式开发、修改与验证，融入 reflection 反思机制，支持代码编辑、质量校验、Git 审计追踪、沙箱隔离运行等核心能力，是面向终端的工程化代码辅助工具。
项目简介
mini-aider 并非简单的代码补全工具，而是聚焦全仓库级别的多轮代码迭代修改，核心是将「思考 - 执行 - 验证 - 反思」的闭环融入代码开发流程，解决 LLM 代码生成中上下文爆炸、修改不可追溯、执行无校验、异常无恢复等问题。
项目复刻 Aider 的核心设计哲学，同时做轻量化改造：
采用非 LangGraph 的纯消息流控制，简化状态机复杂度
拆分Coder/Verifier 双 Agent 架构，分摊认知负荷
内置Git 全链路审计，保证代码修改可追溯、可回滚
支持沙箱隔离运行，宿主机与运行环境解耦，避免越权风险
集成代码质量门禁（Lint/Test），自动校验代码规范性与可运行性
实现上下文智能管理，抑制 token 消耗，提升 LLM 调用效率
核心创新 / 设计亮点
全仓库符号地图：对仓库文件结构做结构化映射，精准选择代码上下文
双 Agent 协作：Coder 负责代码生成与修改，Verifier 负责沙箱运行、测试与环境监控
分层异常处理：Parse/Apply/Lint/Test 任一环节失败均生成反思消息，自动开启新一轮迭代
工程化审计体系：基于 Git、Diff、命令历史、消息日志实现全流程可追溯
沙箱增量同步：单会话沙箱持久化，文件增量提交，保证环境一致性
轻量化 IO 交互：封装终端交互层，兼顾用户体验与开发效率
核心架构
mini-aider 以Coder 类为核心聚合体，拆解为 5 个子系统，同时配合 Verifier、GitRepo、Commands、Memory、IO 等核心模块实现全流程能力，整体遵循Plan-Action-Observation-Reflection的核心骨架。
整体模块设计
plaintext
mini-aider/
├── core/
│   ├── coder.py        # 核心代码生成/修改Agent，含多类Coder实现
│   ├── verifier.py     # 沙箱验证Agent，负责运行/测试/环境监控
│   ├── memory.py       # 记忆管理，含ChatHistory、KV Cache、消息总结
│   ├── git_repo.py     # Git仓库封装，审计/回滚/提交/Diff
│   ├── commands.py     # 命令解析/执行，宿主机+沙箱命令调度
│   └── io.py           # 终端交互层，用户输入/输出/日志/确认提示
├── tools/
│   ├── lint.py         # 代码风格校验工具封装
│   ├── test.py         # 自动化测试工具封装
│   └── sandbox.py      # 沙箱环境管理，健康检查/增量同步/异常分类
└── main.py             # 项目入口，拼装所有模块并启动主循环
核心子系统（Coder 聚合）
Workspace/Repo 管理：文件路径、读写权限、Git 仓库状态、提交记录维护
编辑协议与落盘执行器：支持 Diff/WholeFile 等编辑模式，解析 EditBlock 并应用到文件
质量闭环工具：自动 Lint/Test，工具输出捕获并触发修复流程
上下文与缓存管理：仓库符号地图、LLM 缓存预热、上下文窗口控制、token 消耗统计
交互体验层：流式输出、URL 检测、命令建议、键盘中断处理、彩色日志
核心 Agent 分工
表格
Agent	核心职责	关键能力
Coder	接收用户需求、构造代码上下文、调用 LLM 生成 EditBlock、解析并应用代码修改	多轮反思、上下文管理、代码编辑
Verifier	沙箱环境管理、执行代码 / 测试命令、捕获运行异常、环境健康检查、增量文件同步	异常分类、沙箱监控、结果反馈
核心运行闭环
mini-aider 包含主执行闭环和异常反思闭环，两个闭环相互配合，实现代码的迭代式优化，所有流程均基于消息流驱动，无复杂状态机编排。
主闭环（正常执行）
plaintext
用户输入 → 构造代码上下文 → 调用LLM生成EditBlock → 解析编辑内容 → 应用到文件 → Lint/Test校验 → Git提交/Diff展示 → 完成
异常闭环（失败恢复）
plaintext
Parse/Apply/Lint/Test任一环节失败 → 生成Reflection Message → 重新构造上下文 → 再次调用LLM生成修复方案 → 回到主闭环
关键执行规则
反思轮数可配置，达到最大反思轮数则终止迭代并反馈错误
所有代码修改均先在临时文件执行校验，通过后再应用到正式文件
应用修改前会向用户确认，Human in Loop保证修改可控
沙箱与宿主机隔离，所有运行 / 测试命令均在沙箱中执行，避免宿主机环境污染
单会话沙箱持久化，文件增量同步，保证多次修改的环境一致性
核心功能
1. 多模式代码编辑
支持两种核心编辑模式，适配不同代码修改场景：
UnifiedDiff 模式：仅修改文件指定片段，高效轻量，适合小范围调整
WholeFile 模式：对整个文件进行改写，适合大文件重构、全新文件生成
2. 全流程审计追踪
实现可追溯、可解释、可回滚的审计能力，审计维度包含：
Git 提交记录 / Commit Hash（标记 Aider 自动提交）
代码修改 Diff 日志
终端命令执行历史（建议 / 执行 / 结果）
对话消息 / 消息总结（History）
Shell 运行输出
仓库 / 文件状态监控日志
3. 智能上下文管理
解决 LLM 上下文爆炸问题，提升调用效率：
全仓库符号地图，精准筛选需要加入上下文的文件
区分可编辑文件/只读文件，避免无效上下文加载
消息总结能力（ChatSummary），压缩历史对话，减少 token 消耗
缓存预热机制，提升 LLM 大请求响应速度，降低计费成本
上下文窗口计数，监控并抑制窗口溢出
4. 沙箱隔离运行
内置沙箱环境管理，兼顾运行安全性与环境一致性：
沙箱健康检查、自动重连、异常分类处理
单会话沙箱持久化，多轮修改无需重复初始化
仓库文件增量同步到沙箱，避免全量拷贝的性能损耗
宿主机与沙箱命令解耦，Verifier 专属管理沙箱命令执行
5. 代码质量自动校验
集成质量门禁，保证代码生成的工程化质量：
自动 Lint：代码风格、语法错误实时校验，捕获并反馈格式问题
自动 Test：支持自定义测试命令，自动执行并捕获测试失败
校验失败自动触发反思，LLM 生成修复方案
支持Human in Loop，可手动选择是否执行校验 / 应用修复
6. 多轮反思迭代
基于 Reflection 机制实现代码的自修复，核心规则：
解析失败、应用失败、Lint 错误、Test 失败均触发反思
反思消息携带具体错误信息，让 LLM 精准定位问题
可配置最大反思轮数，避免无限迭代
反思过程中保留所有上下文，保证修复的连续性
7. Git 仓库深度集成
Git 作为核心审计与版本管理工具，深度融入所有流程：
代码修改前自动提交当前状态，保证可回滚
标记 Aider 自动提交的 Hash，区分人工 / 自动修改
实时生成 Diff，展示代码修改内容，支持用户确认
维护文件修改状态，避免多轮修改的冲突问题
快速开始
环境要求
Python 3.9+
Git（已配置本地仓库，开启提交 / 回滚权限）
可访问的 LLM 接口（支持 OpenAI-like 接口，如 GPT-3.5/4、智谱、文心等）
沙箱环境（推荐 Docker，实现宿主机隔离）
常用 Lint/Test 工具（如 ruff、pytest，根据开发语言适配）
安装与启动
克隆项目到本地
bash
运行
git clone <你的仓库地址> mini-aider
cd mini-aider
安装依赖
bash
运行
pip install -r requirements.txt
配置环境变量（LLM 接口、沙箱地址、Git 信息等）
bash
运行
# 示例：配置LLM API
export LLM_API_KEY="你的API密钥"
export LLM_BASE_URL="你的LLM接口地址"
# 配置沙箱地址
export SANDBOX_URL="docker://localhost:2375"
启动 mini-aider
bash
运行
python main.py
基础使用
启动后进入终端交互模式，直接输入代码开发 / 修改需求即可，示例：
plaintext
# 需求示例1：单文件修改
> 给core/coder.py添加一个函数，用于统计LLM调用的token消耗

# 需求示例2：多文件重构
> 重构tools/sandbox.py，将沙箱健康检查逻辑抽离为独立函数，并添加单元测试

# 需求示例3：代码修复
> 修复lint检测出的core/verifier.py中的语法错误，并保证pytest测试通过
核心命令
mini-aider 内置命令解析器，支持终端快捷命令，核心命令：
/lint：手动触发当前文件的 Lint 校验
/test：手动执行测试命令，验证代码可运行性
/diff：展示当前代码修改的 Diff 日志
/commit：手动提交当前代码修改到 Git
/rollback：回滚到上一次 Git 提交状态
/history：展示当前会话的对话 / 命令历史
/exit：退出 mini-aider 并关闭沙箱
关键设计细节
1. 双 Agent 通信机制
Coder 与 Verifier 通过消息流 + 状态共享实现通信，无直接的函数调用，保证解耦：
Coder 生成代码修改后，将EditPlan + 目标文件发送给 Verifier
Verifier 在沙箱中应用修改、执行校验，将运行结果 / 异常信息返回给 Coder
异常信息作为 Reflection Message，触发 Coder 的新一轮迭代
双方共享Git 仓库状态和消息历史，保证上下文一致性
2. LLM 调用优化
内置重试 / 超时 / 格式清洗机制，保证 LLM 调用的稳定性
支持结构化输出（Structured Output），强制 LLM 生成指定格式的 EditBlock，提升解析成功率
维护 LLM 调用日志（请求 / 响应 Hash），方便 Debug
统计 token 消耗和调用次数，实现成本可控
3. 内存 / 状态管理
采用哈希表 + 列表的组合方式维护运行时状态，兼顾查询效率和顺序性：
哈希表（Set/Dict）：维护文件路径、Git 提交 Hash、LLM 调用日志等需快速查询的状态
列表（List）：维护对话消息、命令历史、反思记录等需保序的状态
实现交互节流：避免重复的用户确认提示（如 30 秒内不重复询问是否 Commit）
4. 工具调用体系
mini-aider 并非让 LLM 直接调用工具，而是通过Commands 层做统一组织，保证可控性：
Coder：负责与 LLM 对话 + 代码编辑，不直接执行外部命令
Commands：负责解析并执行所有外部命令（Lint/Test/Shell/ 沙箱命令）
Verifier：负责沙箱内的工具调用，独立于宿主机命令
所有命令执行结果均作为 Observation 反馈给 Coder，融入上下文
待办事项（TODO）
完成双 LLM 部署：分别为 Coder/Verifier 配置专属 LLM，适配不同任务的模型能力
明确沙箱调用主体：确定 Verifier/Coder 谁作为沙箱调用的发起方，优化通信流程
完善多文件重构的上下文选择逻辑：基于用户查询 + 仓库结构，自动筛选多文件重构的上下文
实现ACL（Agent-Computer Interface）：完善搜索 / 导航 / 编辑命令，提升 Agent 与环境的交互效率
丰富异常分类体系：对 Parse/Apply/Lint/Test/ 沙箱运行的异常做精细化分类，提升反思修复的精准度
优化消息总结算法：提升历史对话的压缩效率，减少 token 消耗的同时保证信息完整性
支持多语言适配：扩展 Lint/Test 工具的适配性，支持 Python/Java/Go 等多语言开发
实现流式输出：支持 LLM 生成代码的流式展示，提升用户体验
参考设计
本项目基于 Aider 的核心设计理念复刻并做轻量化改造，同时参考 SWE-agent 的Agent-Computer Interface（ACI） 设计思想，聚焦 Agent 与开发环境的交互效率优化，核心参考点：
Aider 的多轮反思、Git 审计、编辑协议、上下文管理
SWE-agent 的 ACL 设计、文件导航 / 搜索 / 编辑命令、环境反馈机制
Google Design Pattern 的不同 Role 分离思想，解决单 Agent 认知负荷过高问题
开发规范
代码风格：遵循 PEP8，使用 ruff 做 Lint 校验，禁止语法错误 / 格式不规范的代码提交
测试要求：核心模块（Coder/Verifier/GitRepo）需添加单元测试，pytest 测试通过率 100%
提交规范：Git 提交信息遵循type: description格式，如feat: 实现沙箱增量同步、fix: 修复EditBlock解析失败问题
日志规范：使用 io.py 封装的日志接口，按INFO/WARN/ERROR分级输出，关键流程需打印日志
异常处理：所有外部调用（LLM / 沙箱 / Git）均需做异常捕获，避免程序崩溃，同时生成反思消息
开发说明：本项目为轻量版代码修改 Agent，聚焦核心能力的实现与工程化落地，可根据实际需求扩展功能（如多模型支持、可视化界面、多仓库管理等）。

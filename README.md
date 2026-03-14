# Mini-Aider

一个面向代码任务的最小可运行 Agent 原型，核心目标不是“把 LLM 接上代码编辑”，而是把 **代码生成、验证、命令执行、环境恢复** 这几件事情拆开，做成一个有工程边界感的可以冲实习的，完成从**Plaining、tool using and action、observation、reflection、replan的闭环** 。

当前项目已经完成第一阶段：

* 初步搭建了 **Coder → Verifier → Commander** 的分层架构
* 支持 **structured output 驱动的动作决策**
* 支持 **文件编辑、命令执行、验证反馈** 的基础闭环
* 初步实现了 **环境错误 / 代码错误 / 命令错误** 的分类
* 引入了 **sandbox / local 双执行环境**
* 开始将 Verifier 从“命令生成器”升级为“运行时验证 Agent”

---

## 0. usage：

* 准备好您的LLM API key，以及E2B沙箱 API key，E2B 的 KEY 可以在如下途径获取：https://e2b.dev/
* 所有的 runtime 参数在 main.py 中可以自行配置
* 仅需把 mooc.txt 中的user input，以及上下文文件，只读文件补充完整便可，强烈建议使用文件名，而非文件路径（该bug还未修复）
* 启动项目：python main.py 便可完成一轮输出

---

## 1. 项目目标

这个项目的目标是手搓一个“麻雀虽小，五脏俱全”的 coding agent 原型，用来理解和验证 Agent 系统中最关键的几个问题：

1. LLM 应该输出什么：自然语言、代码、还是结构化动作？
2. 代码修改和代码验证应不应该由同一个 Agent 负责？
3. 验证失败时，如何区分是 **代码错** 还是 **环境错**？
4. 执行环境不稳定时，Agent 如何做恢复、重试和重新同步？
5. 一个真正可扩展的 coding agent，状态应该如何管理？

当前设计选择是：

* **Coder** 负责根据任务生成代码修改方案
* **Verifier** 负责设计和执行验证步骤，并对失败原因做归因
* **Commander** 负责统一抽象底层执行环境
* **infra** 负责确定性的文件落盘、命令执行、结果采集与状态更新

也就是说，这个系统不是“让 LLM 直接输出最终正确代码”，而是：

> 让 LLM 输出下一步动作决策，再由infra完成确定性执行。

---

## 2. 整体架构

### 2.1 模块分层

```text
User Task
   |
   v
+-------------------+
|       Coder       |
|  生成修改动作计划  |
+-------------------+
   |
   v
+-------------------+
|      Verifier     |
|  规划验证 / 归因   |
+-------------------+
   |
   v
+-------------------+
|     Commander     |
|  执行命令 / 写文件 |
+-------------------+
   |
   v
+-------------------+
| Local / Sandbox   |
|   实际运行环境     |
+-------------------+
```

---

## 3. 当前系统组成

### 3.1 Coder

Coder 接收用户任务和上下文文件，输出结构化动作对象，例如：

* `message`
* `task_summary`
* `validation_summary`
* `file_edits`

其中 `file_edits` 是待落盘的文件修改计划，而不是让 LLM 自己直接执行 shell 或修改磁盘。

### 3.2 Verifier

Verifier 是当前项目最核心的部分之一。

它不是简单地“生成一串测试命令”，而是在逐步演化为一个真正的运行时验证 Agent：

* 读取 observation
* 判断当前更像代码错误、命令错误还是环境故障
* 选择下一步动作：

  * `run_checks`
  * `recover_env`
  * `restage_files`
  * `rerun_last_command`
  * `report_code_error`
  * `report_env_error`
  * `finish`

当前 Verifier 的重要设计方向：

* 从“批量执行 cmd_list”转向“**线性目标推进**”
* 一次只推进一个 `current_goal`
* 当前目标失败时，围绕该目标持续重规划
* 环境问题优先恢复，不把环境错误误交给 Coder

### 3.3 Commander

Commander 是执行环境抽象层，目标是统一：

* 本地执行环境
* Sandbox 执行环境

当前接口负责：

* `run(command)`
* `write_file(path, content)`
* `read_file(path)`
* `recover_environment()`
* `health_check()`

Commander 的存在意义在于把“执行”从 Agent 决策中分离出来，使上层逻辑不依赖具体运行平台。

---

## 4. 当前已经完成的关键能力

### 4.1 结构化输出驱动

当前系统已经采用结构化输出，而不是让模型直接输出松散文本。这样做的意义是：

* 降低解析歧义
* 更容易做状态更新
* 更适合程序侧确定性执行
* 更适合做 reflection loop

### 4.2 失败归因分层

当前系统开始显式区分：

* `code_failed`
* `environment_failed`
* `command_failed`
* `unknown_failed`

这是项目当前最重要的工程价值之一。

如果没有这层分离，系统会把所有失败都误导成“继续改代码”，导致大量无效反思。

### 4.3 初步环境恢复机制

在 sandbox 模式下，系统已经开始考虑：

* health check
* reconnect / recreate
* restage files
* rerun current goal

虽然当前这部分还在迭代，但已经明确了方向：

> 环境失败不应该伪装成代码失败。

### 4.4 单文件上下文下的最小闭环

当前第一阶段已经能够在单文件任务中完成基础闭环：

* 读上下文
* 生成 file edits
* 写回执行环境
* 运行验证命令
* 收集 observation
* 根据结果判断是否继续验证或交回 coder

---

## 5. 当前遇到的关键问题

### 5.1 Sandbox 生命周期不稳定

当前最大的实际问题之一，是 sandbox 可能在 verifier 内部多轮循环中断开连接，导致：

* reconnect 成功，但 workspace 丢失
* 需要 restage
* restage 过程中再次断连
* verifier 陷入 recover / restage 的 livelock

这说明当前系统的真正瓶颈不在 prompt，而在 runtime substrate 的稳定性。

### 5.2 路径模型混乱

另一个关键问题是路径语义尚未完全分层：

* 宿主机路径
* agent 逻辑路径
* sandbox 内路径

如果将宿主机绝对路径暴露给 LLM，Verifier 可能会在 sandbox 中生成错误命令，例如直接运行宿主机路径，最终造成“文件缺失”的伪环境错误。

### 5.3 未完成多轮对话下的 Memory 模块

memory 模块是多轮对话下的一个重要组件，适合需求不明确、理解上下文、反复修改代码的时候用上

* 采用 KV cache 的技术

敬请期待






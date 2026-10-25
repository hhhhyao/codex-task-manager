# Codex Task Manager

一个供多个 Agent 共用的轻量观察任务管理器，用于暂时把外部工作的关注交给后台。
Agent 指定等待什么、如何观察、何时值得提醒和下一步动作，然后继续其他工作或进入等待。
daemon 管理检查、单次/循环生命周期、等待 review 和每个任务独立的消息 buffer；
将值得处理的结果或问题整理成可读消息，通过 **Repowire** 投递到指定会话。

它管理观察和提醒。原计算任务由原执行器负责；Agent 接手后继续原来的任务。
正常使用不需要领取 inbox、核销消息或读取事件历史。

## 什么时候使用

| 情况 | 默认处理 |
| --- | --- |
| 一两秒就完成，当前流程马上读取结果 | 直接执行、读取；不查询/注册 Task Manager。 |
| 多个步骤明确的短检查 | 在原工作脚本中批量执行，返回阶段结果。 |
| 已有可靠执行句柄或完成通知，后续会自然读取 | 复用现有路径，不重复安排观察。 |
| 工作还需一段时间，Agent 准备转去做其他事 | 交给后台观察，在约定条件下提醒；同回合即可使用。 |
| 外部条件可能长期不满足 | 观察目标条件，按需配置 review；闲时也可唤醒接续。 |
| 明确要求的定时工作、长期健康监控 | 按约定策略运行，不把每次采样都变成提示。 |

按业务等待和自然读取路径判断，不设置固定的时长门槛：一秒查询三小时作业的状态可以合理，
一秒完成且立即读取的工作通常不需要。观察可以细，提醒可以少。
注册前在原计划里写清等待条件、独立跟进的收益、通知条件和具体 `next_action`。
一项任务覆盖应当一起处理的阶段或结果，不必每条命令都建一个任务。
`status` 是可查询状态，源文件保存详细结果，`events` 才请求额外注意力。
buffer 整理已经值得发送的通知，不能靠加大窗口补救过多的通知来源。

```text
Agent A ──提交/查询/取消──┐
Agent B ──提交/发布事件──┼─→ Task Manager daemon
                       │     ├─ 任务 1：检查 + 独立 buffer + review interval
                       │     └─ 任务 2：回调 + 独立 buffer
                       └──────────────────→ Repowire → 原 Agent 会话
```

## 安装

需要 Python 3.11+、Linux 或 macOS；运行时代码只有 Python 标准库依赖。
先按 [Repowire 配置](docs/repowire.md) 接通原生会话投递，再安装本项目：

```bash
cd /path/to/codex-task-manager
uv tool install --editable .
ctm --version
ctm daemon start
```

也可以使用虚拟环境：`python3 -m venv .venv`，然后
`.venv/bin/python -m pip install -e .`；后续使用 `.venv/bin/ctm`。
`codex-task-manager` 与 `ctm` 是同一个 CLI。

将项目内 skill 安装到 Codex：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/event-driven-wakeup"
cp -R skills/event-driven-wakeup/. "${CODEX_HOME:-$HOME/.codex}/skills/event-driven-wakeup/"
```

更新现有 skill 时先备份；复制会更新说明，保留目录中其他文件，便于已有后台脚本过渡。
新建等待使用 Task Manager；已有观察任务先核对，再逐个迁移，避免重复提醒。
更新 skill 不会改变已有任务的发送策略。接手旧观察时复核它是否仍需独立跟进、
哪些事件需要通知、`immediate` 和 review 是否仍适用；复用原工作，不重新启动业务计算。

## 多 Agent 共用

### 终端看板

```bash
ctm ui                 # 交互界面，默认每 5 秒刷新
ctm ui --refresh 10    # 改为每 10 秒刷新
ctm ui --once          # 打印一次状态快照
ctm ui --json          # 一次结构化快照
```

主界面按使用频率分为三页，启动时默认进入 Tasks：

- **Tasks**：当前 CTM 任务、用途说明 `description`、状态、buffer 数量、下次检查和 review；宽屏补充所有者与单次/循环模式。
  选中后在底部直接显示说明和 `next_action`；详情包含观测到的工作状态、review 间隔/次数、上次触发/发送、错误及完整身份。
- **Agents**：Session 名字、Repowire agent 名称、工作状态、接收状态与待处理 ask 数；详情显示后端、来源及可寻址性。
  Codex 会话名字通过精确 thread ID 只读匹配本地 `$CODEX_HOME`（默认 `~/.codex`）的状态库；不读取对话正文。
  远端会话、未命名会话或状态库不可用时保留 agent 名称；窄屏优先显示 Session，底部保留 agent 名称。
  详情也保留 `session_id`、子 agent 昵称和 Repowire 提供的 `tmux_session`。
  默认显示主会话；按 `a` 查看全部记录。`STATE` 标出 `subagent`（子 agent）、`internal`（临时/内部线程）、
  `offline`、`no input`（不可直接输入）、`archived` 或 `unmatched`（本地未匹配，可能残留或属于另一个 CODEX_HOME）。
  底部和详情可查看分类原因及原始连接状态。未命名、长期空闲、远端会话或本地状态库不可用不会单独导致隐藏；
  未匹配但正在执行或有待处理 ask 的会话也保留。列表过滤不删除注册记录。
- **Services**：CTM daemon、Repowire daemon、Codex bridge、Codex App Server。
  分别展示接口可达性和本机进程证据；进程存在不等于接口健康，不依赖 systemd user bus。

保留终端默认前景和背景；绿色表示正常，青色表示执行中，黄色表示需要留意，红色表示错误。
选中项使用 `›` 和加粗，不反转整行背景。状态文字始终保留，单色终端或设置 `NO_COLOR` 时也能使用。

| 按键 | 操作 |
| --- | --- |
| `←` / `→` 或 `h` / `l` | 左右切换页面 |
| `Tab` / `Shift-Tab` / `1`–`3` | 切换页面的备用按键 |
| `↑` / `↓` 或 `j` / `k` | 选择项目；详情里滚动 |
| `Enter` / `Esc` | 打开/关闭详情 |
| `PageUp` / `PageDown` | 翻页 |
| `/`，输入后按 `Enter` | 搜索当前列表；退格删除过滤内容 |
| `r` | 手动刷新 |
| `p` | 暂停/恢复自动刷新 |
| `a` | Agents 页面切换主会话/全部记录 |
| `q` | 退出 |

看板只读，不启动、停止服务，不发消息或创建观察任务，也不展示认证配置。
查询在后台执行，某一数据源不可用时仍可查看其他页面，退出不影响 daemon。
交互模式使用 Python 标准库 curses；非 TTY 自动输出一次快照。
窄终端简化列，完整内容仍可进入详情查看，所有时间按 UTC 展示。
Repowire 原生 schedules/Jobs 与 CTM 任务相互独立，本版不混入 CTM 子项。

### 命令行管理

同一用户的 Agent 默认连接同一个 daemon：

```bash
ctm daemon status
ctm list --all
ctm list --mine
ctm show TASK_ID
ctm describe TASK_ID "等待后台评估完成，汇总可比较的运行结果"
ctm cancel TASK_ID
```

`--mine` 使用 `CODEX_THREAD_ID`；也可显式 `ctm list --owner THREAD_UUID`。
列表展示所有者、单次/循环、当前状态、buffer 数量、下次检查、等待检查和上次事件时间。
`next_action` 记录唤醒后要做的具体动作，列表展示简写，详情和通知展示完整内容。
`description` 用一句话说明任务用途，在提交配置中填写；旧任务可用 `ctm describe ID "说明"` 补充或修改。
更新说明保留任务 ID、观察进度、计时和 buffer；省略该字段的旧任务显示 `-`。
`show` 还展示上次成功检查、上次发送、下次 buffer 发送、当前发送状态和最近错误。
`--json` 提供结构化输出；`show --buffer` 才额外显示当前未发送事件。

`daemon status` 分别统计活动任务和实际正在执行的检查，等待中的任务不占常驻脚本进程。
所有者与接收者绑定精确 peer ID 和 thread UUID，显示名只供人阅读。
这是同一 OS 用户下的协作工具；所有者用于归属查询，不是 Agent 之间的安全隔离。

默认状态目录为 `${XDG_STATE_HOME:-~/.local/state}/codex-task-manager`。
设置 `CODEX_TASK_MANAGER_HOME` 或全局 `--state-dir PATH` 可以选择另一个实例；
所有客户端必须使用同一个目录。Unix socket 位于私有运行时目录，以兼容较长的 HOME 路径。

## 一个可运行的单次观察

以下是缩时演示，不代表实际秒级任务需要注册；快速已有结果通常直接读取。
先在目标 Agent 中调用 Repowire `whoami`/`list_peers`，取得自己的精确 peer ID 和
`metadata.runtime_session_id`。以下命令中的两个身份参数需要替换：

```bash
python3 examples/make_demo.py \
  --peer-id YOUR_EXACT_PEER_ID \
  --thread-id YOUR_THREAD_UUID \
  --directory /tmp/ctm-manifest-demo
ctm submit /tmp/ctm-manifest-demo/task.json
ctm list
```

示例每 2 秒读取 manifest；仍在等待时，每 5 分钟发起一次检查提醒，直到目标事件到达。
演示配置的普通结果通过 10 秒 buffer 发送；只有确需提前处理时才为检查脚本增加 `--immediate`。
模拟外部工作完成时，原子更新 manifest：

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path('/tmp/ctm-manifest-demo')
temporary = root / 'status.tmp'
temporary.write_text(json.dumps({'run_id': 'demo', 'state': 'completed'}))
os.replace(temporary, root / 'status.json')
PY
```

Agent 会直接收到“demo 已完成”的带时间戳消息和结果路径。
发送被 Repowire 接收后，单次观察结束；这不表示 Agent 已处理结果。
发送后的正文会释放，结束状态默认保留 5 分钟供查询后自动清理。

使用正式 Codex Goal 时，若事件需长时间等待且没有其他可推进的工作，先注册观察、
长等待检查并保存唤醒交接，再按用户对这套流程的指令暂停；唤醒后确认条件允许继续，
恢复同一个 Goal 并核对身份、预算和计数。该流程的持续授权不需要每轮重复确认。
详见 [Goal 等待与恢复](skills/event-driven-wakeup/references/goals.md)。
daemon 和 Repowire 只触发消息，不自动改变 Goal 状态。

## 检查脚本、回调和循环任务

检查脚本是短时、非交互的程序，每次输出一个 JSON 对象：

```json
{
  "status": "running: 2/3 ready",
  "events": [],
  "done": false
}
```

有新进度但下一次自然检查会读取时，仍返回空 `events`。
足够的结果使 Agent 需要回来比较，或问题需要介入时，再产生具体通知。
`done: true` 表示这一轮观察已结束，
已接受的事件仍会发送。退出 1 且无 stdout 也表示等待；其他非零退出、非法 JSON、
超时属于观察错误。每次检查最多 64 KiB stdout/stderr，默认限时 15 秒。
检查只观察已经启动的工作，不在每次调用时提交新工作。

`mode: "once"` 在第一次业务事件后停止后续检查并发送已接受的事件；
周期 review 和观察错误提醒不会提前结束它。
`mode: "repeat"` 持续检查，直至 `done` 或取消。
单次提醒本身也可以检查很多次；循环检查不等于每轮都发送。
具体字段见 [任务与事件协议](docs/protocol.md)。

已有程序可以主动发布事件。创建一个不含 `check`/`interval_seconds`/`fire_at` 的任务，
然后回调执行：

```bash
ctm emit TASK_ID /absolute/event.json
ctm flush TASK_ID
```

`emit` 接受一个事件对象或数组。`flush` 是显式业务条件满足时提前发送；
也可在最后一个事件上设置 `immediate: true`。
同一任务的多个来源使用同一 task ID；不同任务有独立 buffer。

## Buffer 与长等待

- 固定窗口从本批第一个事件进入时开始；新事件不会无限延后发送。
- `important` 标记正文；`immediate` 提前发送。完成/失败本身不等于需要立即处理，由脚本按业务判断。
- 稳定事件 ID 去重；`replace_key` 可替换尚未发送的同类进度，不合并独立成员的结果。
- 按时间排列，正文限制大小，超长批次分开发送；超过单条预算时拒绝，要求缩短并引用源文件。
- 发送时冻结一批；期间的新事件进入下一批，不会被旧批次清空。
- 发送被接收后删除正文；近期 ID 哈希有数量上限，任务结束后一起清理。

长时等待应设置 `review_interval_seconds`，由 Agent 选择合适的检查间隔。
它跟随当前暂时委托关注的等待，Agent 忙其他事或进入闲时都可使用。
每个间隔到期且本轮仍在等待时，生成一次立即检查事件，随后自动安排下一次。
状态展示 `next_review_at`、`last_review_at` 和 `review_count`。
到期检查优先读取目标结果；完成与 review 同时发生时先处理完成。
提醒要求核对进度，不判定业务失败，也不会结束单次观察。调整或关闭间隔：

```bash
ctm review TASK_ID --interval 1800
ctm review TASK_ID --disable
```

只要观察尚未结束，单次、循环和回调任务都可配置多次 review。
Agent 恢复正常跟进该工作后，应调整或关闭该轮 review；下次转移关注时再开启。
观察任务可以保留，`--disable` 只关闭 review。daemon 不会从消息发送成功推断 Agent 已接手。
业务完成或取消后自动停止；空 buffer、发送成功和普通心跳都不会重置 review 间隔。
重启后只补一次已到期的检查，不追发全部错过的提醒；未发送 review 在 buffer 中只保留最新一条。
需要下轮观察时复用原业务工作，重新注册观察，不重复启动计算。

## 运行与恢复

`ctm daemon start` 在后台运行，不创建 tmux 窗口，重复调用会复用已有实例。
需要开机/登录后启动时，用已有 systemd user、launchd 或 supervisor 托管
`ctm daemon run`；本项目不修改系统服务。`ctm daemon stop` 停止检查服务并保留待发送状态。

任务默认最多 4 个检查和 4 个 HTTP 发送并发，各任务检查不重叠。
前台模式可配置 `--max-checks`、`--max-sends`、`--retention-seconds`。
轮询/间隔计时器恢复后合并错过的检查，不重放大量历史 tick。
`fire_at` 支持单次绝对时间，`interval_seconds` 支持循环计时。
本版不解析日历 cron；固定日历提醒可使用 Repowire 原生 cron，复杂日历脚本由已有调度器回调。

取消会停止本项目启动的观察进程组、丢弃未发送内容；已交给 HTTP 的消息不能撤回。
它不会取消外部计算、下载或改变 Codex Goal。进程存活、业务状态、消息投递分别展示。

明确投递拒绝按任务配置有限重试；超时、断连或不确定回执不自动重发。
核对实际会话和 Repowire trace 后，使用：

```bash
ctm resolve TASK_ID --outcome accepted
# 或：确认没有投递后才允许重新尝试
ctm resolve TASK_ID --outcome not-delivered
```

无法确认的批次只影响自己的队列。buffer 有容量限制，满时明确拒绝新事件，
生产者应保留原始结果并在恢复后重试；不会静默丢弃关键事实。
daemon 正常停止会清理正在运行的检查。异常退出时，发送中批次标为不确定；
运行中检查暂停，核对记录的 PID 后用 `ctm resume TASK_ID` 恢复，避免盲目重启脚本。

活动状态保存在小型 SQLite 表中，没有事件流水表、消息归档或 Agent 核销表。
运行日志只记录服务生命周期，并按 128 KiB × 2 轮换。
原始日志、业务结果和 Agent 的实际处理进度仍归原任务所有。

## 验证与开发

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m codex_task_manager --help
```

测试启动隔离 daemon 和本机模拟 Repowire，覆盖多 Agent 提交、定时积攒、并发到达、
去重、正文释放、等待超期、重启、取消与失败恢复；不会向真实会话发送测试消息。

也可运行约 50 秒的完整假工作流，查看实际消息和状态证据：

```bash
python3 examples/verify_workflow.py --directory /tmp/ctm-verification-new
```

目录需不存在。该脚本运行隔离 daemon 与本地模拟接收端，验证多次 review 后完成、
buffer 聚合/去重、发送中到达、故障队列隔离、定时、取消、重启与自动清理。
结果保存在该目录的 `report.json`、`messages.json` 和 `snapshots.json`。
默认不接触共享实例；`--completion-task-id` 仅用于显式安排好的真实完成通知。
这里的检查间隔被刻意缩短以验证机制，不是生产任务选择时长或提醒频率的模板。

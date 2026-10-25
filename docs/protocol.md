# 任务与事件协议

配置使用 JSON。CLI 提交时，相对 `cwd` 相对于配置文件所在目录；daemon 始终保存绝对目录。
检查命令是 argv 数组，不自动通过 shell。脚本应执行有时限的观察，业务工作提前启动。
先判断是否需要独立跟进：当前流程马上能读到的短任务通常不注册；Agent 转去做其他事时，
中长期或不确定等待可以委托观察，同回合和闲时都适用。检查脚本很快不代表业务等待很短。

## 任务配置

```json
{
  "name": "compare-evaluations",
  "description": "等待评估运行产生可比较的结果，并提醒处理需要干预的问题。",
  "owner": {
    "peer_id": "YOUR_EXACT_PEER_ID",
    "thread_id": "00000000-0000-4000-8000-000000000001",
    "label": "evaluation-agent"
  },
  "cwd": "/absolute/work",
  "mode": "repeat",
  "check": ["/absolute/python", "/absolute/check.py"],
  "interval_seconds": 60,
  "window_seconds": 120,
  "review_interval_seconds": 1800,
  "next_action": "对比已完成成员的指标；如果是超期提醒，检查剩余成员进度。",
  "plan_path": "/absolute/work/plan.txt"
}
```

`review_interval_seconds` 由 Agent 按任务选择；例子表示仍在等待时每 30 分钟检查一次。
`target` 可显式指定另一个获授权的接收者，结构同 `owner`；默认发回所有者。
保存精确 thread UUID 和 peer ID，不能用窗口名或显示名猜测接收者。
本版不自动重定向失效 peer；核对原线程的新身份后重新注册。

| 字段 | 默认/含义 |
| --- | --- |
| `id` | 可选 UUID；默认生成。显式 ID 可使相同配置提交幂等。每轮使用新 ID。 |
| `name`, `owner` | 必需。名称和提交者身份。 |
| `description` | 可选用途说明，1..1000 字符；列表和详情可见。用 `ctm describe ID "说明"` 修改，保留观察与 buffer 状态。 |
| `mode` | `once`；或 `repeat`。 |
| `check` | 可选。每次执行的短检查 argv。 |
| `interval_seconds` | 有 `check` 时默认 60 秒；每次检查结束后计时，不重叠。 |
| `fire_at` | 可选绝对计时器时间；不与 `check` 同用。 |
| `message` | 可选计时器提醒正文；具体下一步也可放入 `plan_path`。 |
| `review_interval_seconds` | 可选的等待检查间隔（秒）；长时闲时等待应配置或有等效保障。到期后自动安排下一次。 |
| `window_seconds` | 默认 60，范围 0..86400；从当前批第一个事件进入时开始。 |
| `check_timeout` | 默认 15 秒，上限 300 秒。 |
| `max_events` | 默认 50，达到数量后提前发送，上限 1000。 |
| `max_buffer_bytes` | 默认 128 KiB，上限 1 MiB；buffer 满时明确拒绝。 |
| `max_message_chars` | 默认 6000，范围 800..16000；不含传输信封。 |
| `seen_limit` | 默认保留最近 256 个 ID/哈希，至少等于 `max_events`。 |
| `retry_seconds`, `max_attempts` | 默认 10 秒、3 次；仅明确投递拒绝可自动重试。 |
| `error_notify_after` | 默认连续 3 次检查错误后提醒一次；成功检查后可重新进入新错误事件。 |
| `plan_path` | 可选绝对路径，附在消息末尾作为接续入口。 |
| `next_action` | Schema 可选，skill 要求委托关注时填写具体动作；1..1000 字符，列表、详情和通知可见，以最新计划为准。 |

无 `check` 时：指定 `fire_at` 或 `interval_seconds` 表示计时器；两者都没有表示回调任务。
循环绝对计时器需要同时给出 `interval_seconds`。不自动补发重启期间每个错过的 tick。

`once` 在第一次业务事件后停止后续检查，发送已接受的内容后结束。
review 与观察错误提醒不结束单次观察；目标仍在等待时 review 会按配置间隔重复。
积攒窗口和发送期间已经接受的新内容仍会排空，可能因长度限制分成多条消息。
若希望持续收集同一阶段的多次结果，选 `repeat`，并由脚本在阶段结束时返回 `done: true`。
完成、取消和到期均不隐含 Codex Goal 状态转换。

## 检查返回值

退出 0 时 stdout 是单个 JSON 对象，允许的字段只有：

```json
{"status": "running", "events": [], "done": false}
```

- `status` 是最近业务状态的简短说明，上限 500 字符，供查询；不自动产生提醒。
- `events` 是需要额外注意力的通知数组；普通进度或自然检查点将读取的结果留在源文件，数组为空。
- `done` 表示本轮观察结束；未发送内容仍会排空。
- 退出 1 且无输出是仍在等待的简写；其他错误与业务失败分开。

检查不要休眠到远期时间，也不要重新提交业务工作。
默认最多 4 个检查并发；同一任务不重叠。取消仅终止本项目启动的检查进程组。
daemon 允许 Agent 执行其本来就有权限执行的脚本，不提供额外沙箱或提权。

## 事件

```json
{
  "id": "run-42:member-a:failed",
  "at": "2026-11-01T14:25:00Z",
  "text": "成员 A 已失败，需要检查退出日志。",
  "important": true,
  "immediate": true,
  "detail_path": "/absolute/run-42/member-a/result.json"
}
```

`id` 和非空 `text` 必需。`at` 缺省时使用接收时间，显式时间必须包含时区。
时间戳不参与去重哈希；同一 ID 的实际事实变化会报错，不能用于变相修改已发送消息。
`important` 只突出显示；`immediate` 请求提前发送当前 buffer。
失败或完成本身不要求立即发送；只有提前处理有实际价值时才选择 `immediate`。
`replace_key` 是可选的进度归并键：新事件只替换尚未发送的同键进度。
它不改变已经冻结的批次，也不应拿来覆盖不同成员的完成或失败。

正文按事件时间排序，同一时间按接收顺序。优先装入立即/重要事件，再将选中的事件按时间呈现。
超过消息预算的批次继续发送后续批次；单条放不下则拒绝，要求生产者缩短并引用详情。
不要为了刷状态把时间戳或当前计数加入同一事实的事件 ID。

去重范围是当前 buffer、当前发送批次和有数量上限的近期 ID。
长期精确去重由源端的一次性回调、成员完成标志或当前轮次状态负责。
等待检查另有本轮已触发标志，不因近期 ID 淘汰而每轮重复触发。
生产者保留原业务结果；回调失败或 buffer 满时按实际返回结果处理，不假定事件已接收。

## 状态与恢复

活动状态包括 `waiting`、`checking`、`buffering`、`check_error`、`paused`、
`delivery_ready/sending/rejected/ambiguous`。结束状态是 `finished` 或 `cancelled`。
`finished` 只表示观察与已接收通知发送结束，不证明 Agent 已完成业务处理。

`last_trigger_at` 是新的有用事件被接受的时间；重复事件不会刷新它。
`last_sent_at` 是传输确认接受的时间。`last_check_at` 包含失败检查，
`last_successful_check_at` 只包含可解析的有效检查。所有展示时间为 UTC。
`next_review_at` 和 `last_review_at` 是间隔计算出的下一次时间及上次产生检查事件的时间；
`review_count` 统计实际产生的 review，不表示消息被 Agent 处理的次数。
review 用于暂时委托关注的等待，可以在 Agent 忙其他事时开启；正常跟进该工作后应按需调整或关闭。
daemon 不判断 Agent 当前关注哪项工作；开启后按间隔运行，不会因已发消息而自动结束。
普通事件和心跳不延后 review；重启合并错过的间隔。可用 `ctm review ID --interval SECONDS`
调整并从当前时间重新计时，或 `--disable` 关闭。未发送的旧 review 可以被新 review 替换。

异常重启保留未发送内容；中断检查暂停，不盲目启动第二份；发送中的批次转为不确定。
先核对，再使用 `resume` 或 `resolve`。同一队列只保留一个待解决发送批次，
该批次未解决时新事件仍可积攒到容量上限，其他任务独立推进。
发送确认后正文被删除；没有历史查询或 Agent 核销步骤。

# 配合 Repowire 使用

Task Manager 负责观察、积攒和请求注意力；Repowire 负责把消息送入目标会话。
运行时直接调用本机 Repowire 的认证 `/notify` 接口，不启动新 Agent 或新 tmux 窗口。

`ctm ui` 的 Tasks 页也只读汇总 Repowire 定时消息和 recurring Jobs。Agent 可以继续直接用
Repowire 管理 cron，无需再向 CTM 注册副本。按 `s` 筛选来源，Enter 查看原始 ID、目标会话、
连接实例和原生管理命令；已取消的 recurring 计划用 `a` 展开。
看板查询 `/schedules` 与 `/work?view=summary` 的 `recurring`，不把 Job 的历次执行展成历史列表。
目标离线只影响接收健康提示，不隐藏计划；cron 触发时间推进与实际投递、执行结果分别展示。

## 安装和原生接入

在已能使用 Codex 的终端安装 Repowire，再执行其 setup：

```bash
curl -fsSL https://github.com/prassanna-ravishankar/repowire/releases/latest/download/install.sh | sh
repowire setup
repowire status
repowire service status
```

安装与 setup 以[上游安装说明](https://docs.repowire.io/)和
[setup 文档](https://docs.repowire.io/start/setup/)为准。项目适配已在 Repowire 0.21.0 的
HTTP 回执上测试；升级后用同样的接收验证确认行为。

Codex 使用共享 App Server 与 Repowire bridge 的原生接入。
在支持这些选项的 Codex 版本中，合并到 `~/.codex/config.toml` 现有配置：

```toml
[features]
daemon_auto_start = true
hooks = true
```

`repowire setup` 安装 MCP 配置、bridge 和必要的 hook。启动或恢复正常的 Codex 会话，
让它使用共享 daemon；终端仍可正常交互。原生桥接负责消息进入会话，
MCP 提供发送工具，Stop hook 用于未关闭 ask 的提醒。
详见[上游 Codex 接入](https://docs.repowire.io/use/features/connect-codex/)和
[OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server)。

```bash
codex app-server daemon version
repowire peer list
```

确认目标 peer 的 `source`/`metadata.transport` 为 `codex-app-server`，可被寻址，
且保存的 thread UUID 对应原会话。工作中接收消息和空闲时恢复会话都通过原生桥接完成。
本地已验证组合包括 Codex 0.164.0 / Repowire 0.21.0；版本能力以实际命令和元数据为准。

## 没有 systemd user 的环境

可以保留原有 supervisor/launchd 等进程管理器，跳过 Repowire 自动服务安装：

```bash
repowire setup --no-service --non-interactive
codex app-server daemon start
```

用进程管理器分别托管下列两个前台命令，各保持一个实例：

```text
/absolute/path/to/repowire serve
/absolute/path/to/repowire codex-bridge
```

Task Manager 同样可托管 `ctm daemon run`。这些服务使用同一用户的 HOME、
CODEX_HOME、Repowire 配置和可用 PATH。Codex 共享 App Server 由 Codex 管理，
bridge 连接它；复用已有服务和原会话，观察脚本不承担连接服务的启动工作。

## 切换 Codex 登录账号

安装本项目时会一并安装 `codex-server` 和登录客户端依赖：

```bash
codex-server status
codex-server login --device
codex-server status
```

在浏览器中用目标账号完成 device auth 后，当前共享 App Server 自动保存凭据并更新内存认证，
命令再读回账号核对；连接该服务的会话使用更新后的账号。
CTM 任务、Repowire daemon 和 bridge 无需为换号而重启。
默认遵循 `CODEX_HOME` 的控制 socket；自定义路径、超时和影响范围见
[账号使用说明](../README.md#codex-账号登录与切换)。Repowire 的 HTTP token 仍单独管理。

## 获取接收身份和验证

在目标 Agent 中调用 Repowire `whoami`，必要时用 `list_peers` 检查精确 peer：
记录 `peer_id`、`metadata.runtime_session_id` 和可选显示名。
将它们填入 Task Manager 的 `owner`；默认发送回该会话。

新安装时先用一个短单次任务确认收到原始消息，再在会话空闲后确认能收到消息并继续处理。
投递被服务接受、原始消息进入会话、Agent 实际处理，是不同的验证结果。
验证完成后取消或等待单次观察自动结束。

普通工作使用 notify。Codex Goal 的暂停、恢复依照 skill 中的明确授权流程，
不由收到通知自动完成。任务被取消后，已经进入会话的消息仍可能迟到；
接收者核对 task ID 与最新计划。

## HTTP 配置

默认每次发送用 `repowire config get` 读取 host、port 和认证 token。
token 不写入任务配置，不出现在状态查询或日志中。
需要覆盖时，在启动 daemon 的环境里设置：

```text
CODEX_TASK_MANAGER_REPOWIRE_URL=http://127.0.0.1:8377
REPOWIRE_DAEMON__AUTH_TOKEN=通过现有安全来源注入
REPOWIRE_BIN=/absolute/path/to/repowire
```

也支持 `REPOWIRE_DAEMON__HOST`、`REPOWIRE_DAEMON__PORT` 和 `REPOWIRE_AUTH_TOKEN`。
环境覆盖在 daemon 启动时继承；使用正常配置读取时，后续发送会读取新配置。
本地 HTTP token 属于 Repowire，与 Codex 登录账号分别管理。

若投递失败，先查 `ctm show TASK_ID`，再查 Repowire 服务、原线程身份与 bridge 日志。
HTTP 顶层的 `delivered` 不作为处理成功证据；实现优先检查嵌套回执。
只有明确拒绝才有限重试；不确定结果需要核对后 `ctm resolve`。

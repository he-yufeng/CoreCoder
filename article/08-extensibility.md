# 不动主循环的三种加法：MCP、钩子与计划模式

第七篇结束的时候，你手里已经有一个能跑的 agent 了。接下来你几乎一定会想做三件事：给它接新工具（接进来一个文件系统服务、一个数据库服务），在工具调用前后插一段自己的逻辑（每次跑 bash 前先过一道检查），以及让它偶尔「只许看、不许动」（先出个方案再开工）。这三件事对应 v0.6.0 新增的三块代码：`mcp.py`（208 行）、`hooks.py`（87 行）、以及 `agent.py` 里一个五十行的布尔量。它们的共同点是：都不碰第一到第六篇讲的那个主循环。这一篇讲为什么「不碰主循环」不是克制，而是这三块功能能成立的前提。

## MCP：把「协议」裁成「能用的那一片」

MCP（Model Context Protocol）是让 agent 调用外部工具服务的标准。完整协议有生命周期管理、能力协商、资源订阅、提示词模板、采样回调一大堆东西。`mcp.py` 只留了一个 agent 真正用的切面：起进程、握手、问它有哪些工具、调用工具、最后关掉它。

服务端配置在 `~/.corecoder/mcp.json`：

```json
{"mcpServers": {"fs": {"command": "npx", "args": ["-y", "some-fs-server", "/tmp"]}}}
```

每个 server 是一个子进程，走 stdio，一行一个 JSON-RPC 2.0 消息。启动时发 `initialize` 握手，然后 `tools/list` 把远程工具拉下来，每个远程工具改名叫 `mcp__<server>__<tool>` 注册进 agent。改名的作用不只是不撞名：第二篇说过，工具在内核里就一个形状——名字、参数、字符串结果。远程工具改名之后走同一条注册路径，同意层、钩子和主循环看到的跟内建工具一模一样，根本不知道它是外来的。

真正体现工程的是两个线程问题。第一，工具调用是并发的（第五篇），几个线程同时调同一个 server 时，请求和响应怎么对上号？`mcp.py` 起了一个守护线程独占 stdout，每读到一个响应就按 request id 停进 `_responses` 字典，调用方拿自己的 id 去等：

```python
threading.Thread(target=self._read_loop, daemon=True).start()
```

第二，stdin 只有一个，两个线程的请求体不能在字节流里搅在一起，所以写操作走一把 `_write_lock`。一个读线程按 id 分发，一把写锁保顺序，stdio 这种「一根管子双向说话」的协议最常用的处理就是这个形状，不用上队列也不用上事件循环。

协议层最克制的地方在出错的时候。server 挂了、超时了、返回了看不懂的东西，怎么处理？

```python
# a server that hangs or dies fails that one call as an ordinary
# error string; it never kills the loop
```

失败只是这一次工具调用返回一个错误字符串，模型读到「这个服务现在不好使」然后自己想办法，主循环连一个异常都看不到。这个选择和第三篇讲模型接口时的原则是同一条：外围世界永远可能坏，内核不能跟着死。

## 钩子：能否决，但永远杀不死循环

MCP 给 agent 加能力，钩子（hooks）改的是已有工具的行为。定义放在 `~/.corecoder/hooks.json`：

```json
{"PreToolUse":  [{"matcher": "bash", "command": "python check.py"}],
 "PostToolUse": [{"matcher": "*",    "command": "./log.sh"}]}
```

每个钩子是一条 shell 命令，内核把这次工具调用以 JSON 喂到它的标准输入。PreToolUse 钩子的退出码是 2 就表示否决这次调用，它的 stderr 会作为否决理由原样回给模型：

```python
if out is not None and out.returncode == 2:
    reason = out.stderr.strip() or "no reason given"
    return "Blocked by hook: " + reason
```

注意这个理由的去向：它不是打印给用户看的，是塞回给模型当工具结果的。模型读到「这次调用被钩子拦了，因为什么」，然后自己决定换个写法还是换个路径。钩子拦的不是人，是模型接下来的行为，所以理由一定要回到模型的上下文里才有意义。

PostToolUse 钩子只能观察，退出码是什么都不管。还有一个很要紧的兜底：钩子本身也是不可信的外围，它报错、它挂住，怎么办？

```python
TIMEOUT = 10  # seconds; a hung hook must not hang the agent
```

超时跳过，打一条警告继续跑。文件末尾那句注释把整个设计一句话说完了：hooks assist the loop, they never get to kill it。钩子可以否决单次调用，但没有任何一个钩子能让整个 agent 停摆。这条不变量和 MCP 那条是同一个：外围的东西全是客人，客人可以提意见，不能掀桌子。

## 计划模式：五十行的布尔量压住整个权限层

第三件事最少代码。`agent.py` 里就这一个状态：

```python
self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused
```

用户敲 `/plan` 把它翻成 True，此后所有写操作类工具一律拒绝，而且它的优先级被 explicitly 放在最顶上：

```python
# plan mode outranks consent, even --yes: while it's on nothing mutates
if self.plan_mode and tc.name not in Permission.READ_ONLY:
```

第二篇讲过权限层的 `--yes` 一键放行，在这里照样被压住：计划模式高于用户自己的「全部同意」。这是一个挺反直觉的排序，但想想就对：用户开 `--yes` 是想少点几次确认，不是想放弃「先让 agent 把方案讲清楚」这个权利。约束的可信度恰恰来自它连最方便的那个后门都没有。

拒绝时的返回值也是花了心思的，它同样是一段写给模型的指令：

```python
"Plan mode is on, so this call was refused: plan mode is "
"read-only tools, then present the plan and stop. The user can "
'approve it by typing "approve", or exit plan mode with /plan.'
```

模型读到这个，就知道正确的动作是改用只读工具继续调研，然后把方案摆出来停住，等用户敲 approve 或者敲 /plan 解除。一个布尔值加一段精心措辞的拒绝文案，就把「先方案后动手」这个工作流完整逼出来了，不用状态机，不用审批队列。

## 压轴：为什么这三种加法都不用碰内核

把三块放在一起看，会发现它们挂在同一个契约上，这个契约第一到第六篇其实一直在铺：

1. 工具同形。不管什么来源（内建、MCP 远程），工具在内核里都是「名字、参数、字符串结果」三件套，所以权限、钩子、计划模式能一刀切地管到所有工具。
2. 错误是普通返回值。MCP 服务死了、钩子超时了、调用被拒了，全都是回给模型的一个字符串，没有任何一条以异常形式上涌到主循环。
3. 循环不被外围杀死。server 挂了不杀循环，钩子挂了不杀循环，计划模式也只是把「不许做」变成模型看得懂的下一步指令。

这三条合起来，就是「不动主循环也能加东西」的结构原因：外围能力想挂进来，只要遵守这个契约，内核根本不用知道它存在。第七篇结尾让你照着造一个自己的 agent，第八篇想说的是：等你真想给它加东西的时候，先想清楚你的东西属于哪一层——是加工具（MCP 的位置）、改行为（钩子的位置）、还是立规矩（计划模式的位置），然后守住同一条契约。守住了，你的内核也可以一直不胖。

系列到这里就先收尾了。从主循环到这层外围，51 万行 Claude Code 拆出来的最核心一课其实不是哪个具体机制，而是这条反复出现的主线：好内核都是小气的，它只认很少的契约，然后把整个世界挡在外面。

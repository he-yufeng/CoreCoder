# 并行执行与子 agent

第一篇讲主循环时，我在「执行工具」那一步留了个口子：模型一次只要一个工具，和一次要好几个工具，走的是两条不同的路。这一篇先把这个口子补上，再讲一个让 agent 能给自己「分身」的工具。两件事其实是同一个主题的两面：怎么让一个 agent 同时处理多件事，而不互相搞乱。

## 一次回来好几个工具调用

模型并不总是一次只要一个工具。你让它「看看这三个文件分别写了啥」，它很可能一口气返回三个 `read_file` 调用。这三个读操作彼此独立，串行跑就是干等，明明可以一起来。

CoreCoder 在主循环里就是这么分流的：

```python
if len(resp.tool_calls) == 1:
    tc = resp.tool_calls[0]
    # ...直接执行...
else:
    # parallel execution for multiple tool calls
    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
```

单个就直接跑，多个就交给 `_exec_tools_parallel`，一个线程池：

```python
def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
    for tc in tool_calls:
        if on_tool:
            on_tool(tc.name, tc.arguments)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(self._exec_tool, tc) for tc in tool_calls]
        return [f.result() for f in futures]
```

最多 8 个线程并发，每个跑一个工具，最后按原顺序收齐结果。三个文件读取本来要等三趟 IO，现在大致只等一趟。对一个会频繁同时读多个文件的 agent，这个并行省下的时间相当可观。

用线程而不是进程或者协程，是因为工具干的基本都是 IO 密集的活：读盘、跑子进程、等网络。这种场景下 Python 的 GIL 不怎么碍事，线程池又是标准库里最省事的并发原语，几行就搞定。选型和项目「能简则简」的基调一致。

## 这个简化，相对 Claude Code 差在哪

这里得把简化的边界讲清楚。Claude Code 的并发执行（公开拆解里叫 StreamingToolExecutor）比这激进得多，也精细得多。它有两点是 CoreCoder 没做的。

一是它边生成边执行。模型还在吐后面的内容，前面已经成形的工具调用就可以先跑起来，不必等整段响应结束。CoreCoder 是老老实实等模型把这一轮说完，拿到完整的工具调用列表，才开始执行。少了这个「投机执行」，省的是实现复杂度，丢的是一点点延迟。

二是它会区分工具安不安全并发。公开拆解里 Claude Code 给每个工具标了个 `isConcurrencySafe`，只读的工具（读文件、搜索）可以放心并发，会写的工具（改文件、跑命令）则要小心。CoreCoder 没做这个区分，它把一批工具调用一股脑全丢进线程池，不管它们是读是写。

第二点不只是「少了个优化」，它埋着一个真问题。

## 并行不是免费的：共享可变状态会咬人

并发执行最容易出事的地方，是多个并发任务共享同一份可变状态。一旦不加防护，它们会互相踩。

CoreCoder 里 bash 的工作目录跟踪，正是一个绝佳的例子。上一篇之前讲过，bash 需要在多次命令之间记住 `cd` 去了哪，所以它得把当前目录存到某个地方。最直觉的存法，是一个模块级的全局变量：

```python
# the naive way to track cwd: a module-level global
_cwd: str | None = None
```

串行执行时，这毫无问题，一次只有一个 bash 在跑，读写这个全局井然有序。可一旦走到并行分支，模型同时返回了两个 bash 调用，它们会在不同线程里同时读写这同一个 `_cwd`。一个调用刚把它改成目录 A，另一个调用可能正巧读到了这个 A，或者反手把它覆盖成 B。两个本该各自独立的命令，因为共享了一份全局状态，结果纠缠在了一起。这就是典型的竞态：平时跑一万次都对，偏在并发、时序不巧的那一次，给你一个莫名其妙、还很难复现的错。

所以 CoreCoder 没这么存。干净的解法是把这种「每个执行流各自的状态」隔离开，别让它们共享。Python 里现成的工具是 `threading.local()`，它给每个线程一份独立的副本，线程之间互不可见，`bash.py` 里真正用的就是这一版：

```python
import threading
_local = threading.local()

# 读：取当前线程自己的 cwd，没有就退回进程 cwd
cwd = getattr(_local, "cwd", None) or os.getcwd()
# 写：只动当前线程自己的那份
_local.cwd = running
```

我把这一段单独拎出来讲，是因为它是个特别好的教学点：**当你给 agent 加并行，你就同时给所有带可变状态的工具加了一道并发正确性的要求**。这条要求平时藏得很深，单看 bash 那个文件你看不出问题，单看并行那个函数你也看不出问题，只有把「cwd 存进全局」和「这些工具会被并发调用」两件事摆在一起，隐患才浮出来。这正是第四篇讲孤儿 tool 消息时那种跨越多个文件的不变式，最阴险的 bug 几乎都长这样。如果你打算 fork CoreCoder 往里加会写状态的工具，这是你必须先想清楚的一课：你的工具，扛得住被两个线程同时调用吗？

## 子 agent：给自己开一个分身

`agent` 工具（`corecoder/tools/agent.py`，72 行）解决的是另一个问题。有些子任务很重，比如「把这个陌生代码库摸一遍，告诉我认证是怎么实现的」。这种活如果让主 agent 自己干，它得读一大堆文件、跑一堆搜索，这些中间过程全堆进主对话的窗口，等它摸清楚了，窗口也被探索垃圾塞得差不多了，真正的任务反而没空间了。

子 agent 的思路是：派一个有独立上下文的分身去干这件重活，它在自己的窗口里折腾，干完只把一个精简结论交回来。主 agent 的窗口始终干净，只多了一句「认证是这么实现的」。

代码很直白：

```python
def execute(self, task: str) -> str:
    if self._parent_agent is None:
        return "Error: agent tool not initialized (no parent agent)"

    from ..agent import Agent

    parent = self._parent_agent
    sub = Agent(
        llm=parent.llm,
        tools=[t for t in parent.tools if t.name != "agent"],  # no recursive agents
        max_context_tokens=parent.context.max_tokens,
        max_rounds=20,
    )

    try:
        result = sub.chat(task)
        if len(result) > 5000:
            result = result[:4500] + "\n... (sub-agent output truncated)"
        return f"[Sub-agent completed]\n{result}"
    except Exception as e:
        return f"Sub-agent error: {e}"
```

主 agent 造一个新的 `Agent`，共享同一个 LLM，但有它自己全新的 `messages` 列表，这就是上下文隔离的来源。子 agent 跑完一个完整的循环（它自己的轮次上限压到了 20），返回的结果如果太长还会截到 5000 字符以内，免得它辛辛苦苦省下来的空间又被一个超长结论吐回去抵消掉。

注意那行 `_parent_agent`。它是在主 `Agent` 的构造函数里接上的，就是第一篇提过的那个循环：

```python
for t in self.tools:
    if isinstance(t, AgentTool):
        t._parent_agent = self
```

构造 agent 时，把每个 `AgentTool` 的「父指针」指回自己，这样工具被调用时才知道找谁要 LLM、要工具集。

## 为什么子 agent 不准生孙 agent

上面那行注释 `# no recursive agents` 是这段代码里最重要的一句。

派生子 agent 时，给它的工具集被刻意过滤掉了 `agent` 工具本身：`[t for t in parent.tools if t.name != "agent"]`。于是子 agent 手里没有「开分身」这个能力，它干不了的事只能自己硬扛，不能再往下派。

为什么要禁？因为递归的 agent 是一颗随时会失控的炸弹。设想不禁会怎样：主 agent 派了个子 agent，子 agent 觉得任务还是太大又派了孙 agent，孙 agent 再派……每一层都在烧 token、占线程、加延迟，而且模型对「这个子任务该不该再拆」的判断并不可靠，它完全可能陷进一个越拆越细、永远收不拢的无底洞。一刀切死递归，是最省心的安全策略：分身只能有一层，要么这个子 agent 自己搞定，要么它失败返回，没有第三种走向。

还记得第一篇强调过 `_tool_by_name` 是实例级的吗？正是因为每个 agent 认的工具是它自己的事，这里「把 agent 工具从子 agent 的集合里摘掉」才真正生效。子 agent 即便在某段历史里看到过 `agent` 这个名字，去调它也只会得到 `unknown tool 'agent'`。两个看起来不相干的设计，在这里咬合上了。测试 `test_agent_tool_scope_is_per_instance` 守的就是这条咬合。

## 和 Claude Code 的对照

Claude Code 的子 agent 系统（它的 AgentTool 公开拆解里超过一千行）要丰富得多：子 agent 有好几种运行模式，包括在独立的 git worktree 里跑、在后台异步跑；还内置了若干种预设的 agent 类型，各有各的系统提示和工具集。CoreCoder 把这一切收敛成最朴素的一种：同步派生、跑完返回、不准递归。

但「用独立上下文的子 agent 来隔离重活、保护主窗口」这个核心动机，两者是一样的。读懂 CoreCoder 这 58 行，你就抓住了多 agent 协作最本质的那一点：它首先是个上下文管理手段，其次才是个任务分解手段。很多人以为子 agent 是为了「并行干更多活」，其实它最大的价值是「把别的活的噪音挡在主对话之外」。

## 这一篇带走什么

- 模型一次返回多个独立工具调用时，用线程池并发跑是笔划算的买卖，IO 密集场景下线程池是最省事的并发原语。
- 并行不是免费的：它给每个带可变状态的工具都加了一道并发正确性要求。bash 的 cwd 若存进模块全局，在并行下就会 race，所以 CoreCoder 用 `threading.local` 把状态隔离到线程。
- 最阴险的 bug 往往跨越多个文件：单看任一处都正常，两处摆一起才暴露。加并发前，先问每个工具扛不扛得住被同时调用。
- 子 agent 的首要价值是上下文隔离，让重活在独立窗口里折腾、只把精简结论交回主对话，其次才是任务分解。
- 子 agent 不准递归，是用「一刀切」换「绝不失控」。它靠的是实例级工具集这个机制落地。

下一篇，我们把这些零件装进一个真正能用的命令行工具：会话怎么存、断点怎么续、斜杠命令怎么接，以及一个藏在存盘里的安全细节。

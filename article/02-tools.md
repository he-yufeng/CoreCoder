# 工具系统：让模型安全地动手

上一篇那个循环里，有一步被我一带而过：执行工具。这一篇把它展开。

模型本身只会做一件事，根据上文吐出下文。它不能读你的文件，不能跑你的测试，不能往磁盘写一个字节。让它从「会说」变成「会做」的，是工具。工具是 agent 真正接触世界的那只手。所以一个 agent 强不强，很大程度上取决于它的工具设计得好不好：接口是否清晰、错误反馈是否到位、危险操作是否拦得住。

CoreCoder 给了模型八个工具：`bash`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`、`todo_write`、`agent`。这一篇我们先看它们共同的骨架，再细抠其中两个最值得说的，最后我带你写一个自己的。

## 一个工具长什么样

所有工具继承自 `tools/base.py` 里的 `Tool`，整个基类 32 行：

```python
class Tool(ABC):
    """Minimal tool interface. Subclass this to add new capabilities."""

    name: str
    description: str
    parameters: dict  # JSON Schema for the function args

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """Run the tool and return a text result."""
        ...

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
```

一个工具就是四样东西：一个名字，一段给模型看的描述，一份描述参数的 JSON Schema，一个真正干活的 `execute`。`schema()` 把前三样拼成 OpenAI function calling 要的格式，发给模型，模型据此决定要不要调、怎么填参数。

这里有个设计选择值得点一句。CoreCoder 没有搞工具的继承体系，没有 `FileTool` 派生 `ReadTool` 派生什么的。每个工具就是 `Tool` 的一个直接子类，自己管自己。Claude Code 走得更彻底，公开拆解里它压根不用 class 继承，而是用一个 `buildTool()` 工厂函数，把名字、schema、执行逻辑、权限检查当作配置传进去，拼出一个工具对象。两者背后是同一个判断：工具之间没什么真正可共享的行为，硬套继承只会增加耦合。组合优于继承，在这种地方体现得特别干净。

工具的注册也朴素到家，`tools/__init__.py` 里就是一个列表：

```python
ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    AgentTool(),
]
```

想加工具，往这个列表里塞一个实例就行。这件事我们留到本篇最后真做一次。

## edit_file：一个看着平平无奇的关键创新

八个工具里，如果只能挑一个讲，我会挑 `edit_file`。因为「让模型修改一个已有文件」这个看似简单的需求，背后死过好几条路。

第一条死路是让模型按行号打补丁，比如「把第 42 行换成这样」。问题是模型对行号的感知极不可靠，它脑子里的第 42 行和文件里真实的第 42 行经常对不上，差一行就改错地方。而且只要文件在前面被动过一次，后面所有行号全部漂移。

第二条死路是让模型把整个文件重写一遍发回来。小文件还行，文件一大，又慢又贵，而且模型在「誊抄」那些它不该动的部分时，会时不时手滑改掉一两个字符，你还很难发现。

第三条死路是让模型生成标准的 diff/patch 格式。听上去优雅，实际上模型生成那种带 `@@ -42,7 +42,8 @@` 行号头的统一 diff，错误率高得让人头疼，那套上下文行数和偏移量它算不准。

Claude Code 的解法，也是 CoreCoder 照搬的解法，是第四条路：搜索替换，外加一个唯一性约束。模型给出一段「要找的原文」和「替换成的新文」，工具在文件里找这段原文，要求它恰好出现一次，然后替换。看 `tools/edit.py` 的核心：

```python
occurrences = content.count(old_string)

if occurrences == 0:
    preview = content[:500] + ("..." if len(content) > 500 else "")
    return (
        f"Error: old_string not found in {file_path}.\n"
        f"File starts with:\n{preview}"
    )
if occurrences > 1:
    return (
        f"Error: old_string appears {occurrences} times in {file_path}. "
        f"Include more surrounding lines to make it unique."
    )

new_content = content.replace(old_string, new_string, 1)
```

精妙之处全在那个「恰好一次」。

如果原文一次都没找到，说明模型记错了内容，工具不会瞎猜，而是把文件开头贴回去让它重新对照。如果原文出现了不止一次，工具拒绝执行，因为它没法确定模型想改的是哪一处，于是回一句「这段文字出现了 N 次，多带几行上下文让它唯一」。这句话不只是报错，它是在教模型怎么把请求修对：下一轮模型会自觉地把 `old_string` 扩到包含足够的上下文，直到它在全文里唯一。

这个约束把「改文件」这件事从一个模糊问题变成了一个确定问题。模型不需要懂行号，不需要会算偏移，它只需要原样引用一段它想改的代码，工具来保证这段引用没有歧义。Claude Code 的系统提示词里专门叮嘱模型「old_string 必须在文件里唯一」，CoreCoder 的提示词里也有同一条规则。这是一个用约束换可靠性的典范设计。

替换完，工具还会生成一段统一 diff 返回给模型和用户：

```python
diff = _unified_diff(content, new_content, str(p))
return f"Edited {file_path}\n{diff}"
```

注意方向反过来了：让模型生成 diff 不可靠，但让工具生成 diff 给模型看，完全可靠。模型看着这段 diff 就能确认自己改对了没有。生成放在工具侧，消费放在模型侧，各司其职。

还有个容易被忽略的边角：编辑前先确认文件是 UTF-8 文本。

```python
try:
    content = p.read_text(encoding="utf-8")
except UnicodeDecodeError:
    return f"Error: {file_path} is not a UTF-8 text file (edit_file only edits text files)"
```

要是没这道判断，模型一不小心对一个二进制文件做 `edit_file`，得到的会是一大坨 Python 解码异常栈，既污染上下文又毫无帮助。有了它，模型拿到的是一句它读得懂的话。给模型的每一条错误信息，都该是「人话」，这是贯穿 CoreCoder 所有工具的一条隐性准则。

## bash：把危险操作拦在门外，但别假装这是沙箱

`read_file`、`edit_file` 这些工具能造成的破坏有限。`bash` 不一样，它能跑任意 shell 命令，模型一旦写出 `rm -rf /`，后果是真实的。

Claude Code 的 `BashTool` 公开拆解里是 1143 行，里头有命令分类器、有基于 `sandbox-exec` 和 `seccomp` 的真沙箱、有输出截断、有交互式命令拦截。CoreCoder 的 `bash.py` 是 203 行的蒸馏版，保留了四件最要紧的事：危险命令检测、输出截断、超时、工作目录跟踪。

危险命令检测是一张正则黑名单：

```python
_DANGEROUS_PATTERNS = [
    # recursive delete aimed at root/home (force flag optional)
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    # recursive (-r/-R) and force (-f) flags together, in any order or spacing
    (r"\brm\b(?=(?:.*\s)?-\w*[rR])(?=(?:.*\s)?-\w*f)", "force recursive delete"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    # ...还有块设备覆写、chmod 777 根目录、fork 炸弹、curl/wget 管道执行等
]
```

执行前先过一遍这张表，命中就直接拦下，连命令都不会真的跑：

```python
warning = _check_dangerous(command)
if warning:
    return f"⚠ Blocked: {warning}\nCommand: {command}\n..."
```

那两条 `rm` 正则我想多说一句，因为它们体现了写黑名单的人有没有认真想过对手。第一条盯的是「递归删除指向根目录或家目录」，注意 force 标志写成可选，因为 `rm -r /` 不带 `-f` 一样危险。第二条用了两个前瞻断言，分别要求命令里同时出现 `-r`（或 `-R`）和 `-f`，但不管它们的顺序和写法。这是因为 `rm -rf`、`rm -fr`、`rm -r -f`、`rm -f -r` 是同一件事的四种写法，一条朴素的 `rm -rf` 字面匹配会漏掉后三种。测试 `test_bash_blocks_rm_force_recursive_variants` 把这些变体连同长选项 `--recursive --force` 一起喂进去，逐个验证拦得住。同时它还得放过正常的 `rm -f notes.log`、`rm -r ./build_output`，不能一杆子打死所有 `rm`。

这里要把一条边界划清楚：**这张黑名单不是安全边界，它只是一道防手滑的闸**。正则黑名单天生拦不住有心人，命令可以 base64 编码，可以从变量里拼，可以用一百种方式绕过去。它能挡住的是模型一时糊涂生成的那种最常见、最直白的灾难命令，挡不住蓄意攻击。Claude Code 之所以要上 `seccomp` 这种内核级沙箱，正是因为黑名单这条路在安全上走不通。CoreCoder 选择黑名单，是在「教学清晰度」和「真实安全」之间做的明确取舍：它让你一眼看懂「危险拦截」这个设计点长什么样，但它没假装自己是生产级的安全方案。如果你拿 CoreCoder 去接一个不可信的使用场景，沙箱是你必须自己补上的一课。这点[第七篇](07-build-your-own.md)还会回来谈。

剩下两件事也顺带提一句。输出截断保留头尾，命令吐出几万行时只留前 6000 字符和后 3000 字符，中间用一行说明顶替，既不撑爆上下文又保住了最有用的开头和结尾。工作目录跟踪让 `cd` 在多次命令之间能记住，`_update_cwd` 还专门处理了 `cd a && cd b` 这种链式跳转，让 b 相对 a 解析而不是相对起点（测试 `test_bash_chained_cd_resolves_sequentially` 盯着它）。这些都是「跑命令」这件事在真实使用里会冒出来的小坑，一个一个填掉。

## 两阶段：先验形状，再验安全

把上一篇和这一篇连起来看，CoreCoder 对一次工具调用其实做了两道关。第一道在 `agent._exec_tool` 里，用 `inspect.signature().bind()` 验参数能不能对上函数签名，这是验「形状」。第二道在工具内部，比如 `bash` 的危险命令检测、`edit_file` 的 UTF-8 判断，这是验「该不该真做」。

这正对应 Claude Code 的两阶段门控，公开拆解里叫 `validateInput` 和 `checkPermissions`：一个验输入合不合法，一个验这个操作允不允许。把「格式对不对」和「该不该做」分成两关，好处是各自的失败能给出各自精准的反馈，模型也能针对性地修正。一个混在一起的大 try-except 做不到这种精度。

## 门之外：plan 模式、hooks，和住在别的进程里的工具

v0.6.0 在「调用前后」这条缝上又加了三样东西。它们不改变工具本身，改的是一次调用从「模型想调」到「真的执行」之间还经过谁。

第一个是 plan 模式，第三道闸，而且权限最高。`agent.py` 里它就一行判断：`self.plan_mode and tc.name not in Permission.READ_ONLY`，命中就拒绝，连 `--yes` 都压不过它。这个次序是刻意的：用户开 plan 模式就是想让 agent 先只读地读一遍代码再说，这时候任何「我已经授权过了」都不该生效。模型收到的拒绝消息也不是一句冷冰冰的报错，而是一段解释 plan 模式是什么的话，让它知道自己该先把计划讲清楚。只读先行、改动靠后，这个模式值得记住，因为它几乎是零成本的：一个布尔值，加在白名单判断前面。

第二个是 hooks。`~/.corecoder/hooks.json` 里用户可以挂 shell 命令，分 PreToolUse 和 PostToolUse 两种。Pre 钩子在授权检查之前触发（`agent.py` 里就是 `_pre_hooks(tc) or self._permit(tc)` 这一个 `or` 决定先后），返回码 2 表示否决，否决理由从 stderr 原样喂回模型，模型拿到的是一个能读懂、能修正的文本，而不是一个异常栈。Post 钩子只能旁观，永远不能拦。还有一个更关键的取舍：钩子挂了、超时了、返回码不对，都只是记一条 warning 然后跳过。钩子是建议者，不是法官，主循环的命不能交给用户随手写的一段 shell。

第三个是 MCP。`mcp.py` 里每个 server 是一个子进程，走 stdio 上一行一条的 JSON-RPC：启动握手、`tools/list` 拉清单，然后每个远程工具以 `mcp__server__tool` 的名字登记进工具表，从此授权、hooks、plan 模式对它们和内置工具一视同仁。值得学的是这个「一视同」是怎么来的：不是靠 MCP 代码里写多少特殊分支，而是因为工具边界本身（`Tool` 基类加上面三道闸）定义得够干净，外部工具只是又一个实现类。一个挂着死掉的 server 最坏也就是那一次调用返回个错误字符串，循环照转。

这三块都属于「进阶件」：它们不在 agent 的骨架上，骨架仍然是那个循环加八件工具。但它们回答的是同一个问题——一次工具调用前后，还有谁能说话。答案从「两道闸」变成了「两道闸加一圈可插拔的旁路」，而旁路的每一环都被设计成「坏了也不拖累主循环」。

## 动手：写你自己的第一个工具

讲了这么多，不如真加一个。假设我们想给 agent 一个查当前时间的能力（模型自己是不知道现在几点的）。新建 `corecoder/tools/now.py`：

```python
"""A tool that tells the agent the current time."""

import time
from .base import Tool


class NowTool(Tool):
    name = "now"
    description = "Get the current local date and time. Use this when the user asks about the current time or you need a timestamp."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")
```

然后在 `tools/__init__.py` 里把它登记进去：

```python
from .now import NowTool

ALL_TOOLS = [
    BashTool(),
    # ...原有的工具...
    NowTool(),
]
```

就这样。没有别的步骤。重新跑 `corecoder`，问它「现在几点」，你会看到它调 `now`，再用结果回答你。

回头看你写了什么。`name` 是模型用来点名调用的标识。`description` 是模型决定「什么时候该用这个工具」的唯一依据，所以这句话要写得像在跟一个聪明但没有先验知识的同事交代，告诉它这工具干嘛、什么场景下用。`parameters` 是空的，因为查时间不需要参数，但如果你的工具需要参数，这里就是那份 JSON Schema，模型照着它填。`execute` 返回一个字符串，这个字符串会原样变成一条 `tool` 消息喂回模型。

整个工具系统的可扩展性就浓缩在这几步里：定义一个类，登记一下，模型立刻就会用了。你给 agent 长出一只新的手，成本低到这种程度。[第七篇](07-build-your-own.md)我们会写一个比查时间有用得多的工具，把这套机制用在刀刃上。

## 这一篇带走什么

- 工具是 agent 接触世界的手，工具设计的好坏直接决定 agent 的能力上限。
- `edit_file` 的「唯一性搜索替换」是关键创新：用「原文必须在全文唯一」这个约束，把不可靠的「改文件」变成确定的操作，连报错都在教模型怎么改对。
- 让模型生成 diff 不可靠，让工具生成 diff 给模型看完全可靠。生成与消费各归其位。
- bash 的正则黑名单是防手滑的闸，不是安全边界。真要面对不可信场景，沙箱得自己补。把这点想清楚，比假装安全重要得多。
- 加一个工具的成本极低：一个类加一行注册。这是 agent 可扩展性的来源。

下一篇，我们看这只手背后接的是哪个大脑，以及怎么把任意一家的模型接进来、顺手算清楚花了多少钱。

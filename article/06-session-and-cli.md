# 把它跑成一个真正的命令行工具

前五篇拆的是 agent 的内脏：循环、工具、模型接口、上下文压缩、并发与子 agent。这些零件再漂亮，你也没法直接用，因为还缺一层皮，一个让人能坐下来跟它对话、能存盘、能续聊、能查状态的命令行界面。这一篇讲这层皮，对应 `cli.py`（358 行）和 `session.py`（97 行）。

它不光是「锦上添花的 UI」。这层皮里藏着一个很值得讲的安全细节，我们留到后面压轴。

## REPL：一个对话循环套着一个对话循环

`cli.py` 的主体是 `_repl`，一个 `while True`。注意它和第一篇那个 agent 主循环是两层不同的循环：这里这层是「读用户输入、交给 agent、打印回复、再读下一句」，是人机交互的循环；agent 内部那层是「问模型、跑工具」，是任务执行的循环。一次人类输入，往往触发 agent 内部转好几圈。

输入这块用了 `prompt_toolkit`，带历史记录，还自定义了一个挺贴心的键位：

```python
@kb.add("enter")
def _submit(event):
    event.current_buffer.validate_and_handle()

@kb.add("escape", "enter")
def _newline(event):
    event.current_buffer.insert_text("\n")
```

回车直接提交，Esc 加回车才是换行。这样你贴一段多行代码进去，不会刚贴到第二行就被提前提交了。一个小细节，但用过就回不去。

拿到用户输入后，真正调 agent 的是这几行，它把第一篇和第三篇的两个回调在这里落了地：

```python
def on_token(tok):
    streamed.append(tok)
    print(tok, end="", flush=True)

def on_tool(name, kwargs):
    console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

response = agent.chat(user_input, on_token=on_token, on_tool=on_tool)
```

`on_token` 让模型的文字一个个实时冒出来，就是第三篇流式那一层在界面上的样子。`on_tool` 在每次调工具前打一行灰字，告诉你「它正要去读哪个文件、跑哪条命令」。这两个回调是 agent 内核和界面之间唯一的耦合点，内核不关心你怎么显示，只在该出事件的时候喊一声，界面爱怎么呈现是界面的事。这种「内核出事件、外壳管呈现」的切分很干净，你想把 CoreCoder 嵌进别的程序（比如一个 web 服务），只要换掉这两个回调就行，内核一行不用动。第七篇会用到这个性质。

## 斜杠命令：在不打断对话的前提下管状态

REPL 里认一批斜杠命令，它们不发给模型，而是直接操作 agent 的状态：

```
/help      显示帮助
/reset     清空对话历史
/model     查看或切换模型
/tokens    显示 token 用量和估算花费
/compact   手动触发上下文压缩
/diff      列出本次会话改过的文件
/save      把会话存到磁盘
/sessions  列出已存的会话
```

这些命令把前几篇讲的内核能力暴露成了用户能直接拨的开关。`/tokens` 调的是第三篇那个 `estimated_cost`，`/compact` 调的是第四篇那个 `maybe_compress`，`/diff` 读的是第二篇 `edit_file` 一直在维护的那个「改过的文件」集合。内核早就把这些能力准备好了，CLI 只是给每个能力配了一个顺手的入口。

这里有个不起眼但体现品味的判断。一句以 `/` 开头、却不在上面名单里的输入，该怎么办？

```python
# an unknown /command shouldn't be sent to the model as a prompt
if user_input.startswith("/"):
    console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
    continue
```

它不会把 `/qiut`（手滑拼错的 quit）当成一句话发给模型，而是提示「没这个命令」。要是没这道拦截，你打错一个斜杠命令，模型会一本正经地把它当任务来理解，浪费一轮调用还可能干出莫名其妙的事。把「用户显然是想敲命令但敲错了」和「用户真的在给任务」区分开，是交互设计里很小但很真实的体贴。

## 一次性模式：让它能被脚本调用

除了交互式 REPL，CLI 还有个 `-p` 一次性模式，跑一个 prompt 就退出，方便塞进脚本或者管道：

```python
def _run_once(agent, prompt):
    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
```

注意退出码。被 Ctrl+C 打断退 130（这是 Unix 下「被信号中断」的惯例退出码），出错退 1，正常退 0。一个想被脚本调用的命令行工具，必须把退出码当回事，因为调用方就靠这个数字判断你成没成。这又是那种「demo 不会管、产品必须管」的细节。

## 压轴：别让一个会话名把你的文件系统掀了

现在讲 `session.py` 里那个我最想让你记住的东西。

会话存盘看着是最人畜无害的功能：把 `messages` 和模型名 dump 成 JSON 写到磁盘，读的时候再 load 回来。文件名用会话 id。问题就出在这个 id 上，它是会从用户那儿来的，你 `/save` 之后用 `corecoder -r <id>` 续聊，这个 `<id>` 是用户在命令行里敲的任意字符串。

设想最朴素的实现：`SESSIONS_DIR / f"{session_id}.json"`。如果用户（或者某个喂给它会话名的上游程序）把 id 设成 `../../etc/passwd` 会怎样？这个路径会解析到会话目录之外，你的「存会话」变成了「往任意位置写文件」，「读会话」变成了「读任意文件」。这是经典的路径穿越漏洞，无数真实系统栽在它上面。

CoreCoder 用两道关来防，纵深防御。第一道，把 id 规整成一个安全的纯文件名：

```python
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")

def _normalize_session_id(session_id):
    if not session_id:
        return _new_session_id()
    name = session_id.strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_SESSION_RE.sub("-", name).strip(".-_")
    if len(name) > _MAX_SESSION_ID_LEN:
        name = name[:_MAX_SESSION_ID_LEN].strip(".-_")
    return name or _new_session_id()
```

它先把反斜杠统一成正斜杠（堵住 Windows 的 `..\..\` 写法），再用 `split("/")[-1]` 只取最后一段，把所有目录部分全扔掉。于是 `../../etc/passwd` 取到 `passwd`，`/etc/shadow` 取到 `shadow`。然后把剩下的字符里凡不是字母数字和 `._-` 的，全替换成 `-`，再砍掉过长的部分。一个恶意路径进去，出来就是一个老老实实待在会话目录里的普通文件名。

第二道关，是哪怕第一道万一有疏漏，也再兜一层。`_session_path` 把最终路径解析出来后，明确检查它的父目录就是会话目录本身：

```python
def _session_path(session_id):
    path = (SESSIONS_DIR / f"{_normalize_session_id(session_id)}.json").resolve()
    root = SESSIONS_DIR.resolve()
    if root != path.parent:
        raise ValueError("Invalid session id")
    return path
```

`resolve()` 会把路径里所有的 `..` 和符号链接都摊平成真实绝对路径，然后一句 `root != path.parent` 卡死：只要最终落点不是直接躺在会话目录里，立刻拒绝。

为什么要两道关，第一道不是已经够了吗？因为安全这件事，单点防御是脆弱的。第一道是基于「净化输入」的，万一哪天有人改了那个正则、漏了一种攻击写法，第二道基于「校验输出落点」的关还能兜住，反过来也一样。两道关用的是完全不同的思路（一个管输入、一个管输出），所以它们不会一起失效。这就是纵深防御的精髓：不指望任何单一防线绝对可靠，而是叠几道原理不同的防线，让攻击者得同时骗过所有人。这套防御被一串测试盯死，路径穿越、绝对路径、Windows 反斜杠、超长名字，逐个验证攻击字符串进去都变成了乖乖的文件名。

顺带一提，`load_session` 对坏文件也很克制：

```python
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["messages"], data["model"]
except (json.JSONDecodeError, KeyError, OSError):
    # a corrupt or truncated session file shouldn't crash resume
    return None
```

一个写到一半断电、内容截断的会话文件，不该让你下次 `-r` 续聊时直接崩在脸上，而该安静地返回 `None`，让上层提示一句「没找到这个会话」。处理得和前面一脉相承：坏数据就安静降级，别把异常栈甩到用户脸上。

## 和 Claude Code 的对照

Claude Code 的会话与查询引擎（公开拆解里上千行）远比这复杂，它要管的状态、要兼容的终端环境、要处理的并发会话都多得多。但 CoreCoder 这版把「一个能用的 CLI agent」需要的东西凑齐了：流式显示、工具事件提示、一批管状态的命令、一次性模式、会话存取，外加一道正经的安全防线。读它的好处是，你能完整看到「内核」和「外壳」的接缝在哪，以及一个看似无害的功能（存个会话）背后，认真的人会想到多少。

## 这一篇带走什么

- CLI 是内核之上的一层壳。内核出事件（`on_token`、`on_tool`），外壳管呈现，两者只通过回调耦合，换壳不用动内核。
- 斜杠命令把内核能力暴露成用户能直接拨的开关；拦住敲错的斜杠命令，别把它当任务发给模型。
- 想被脚本调用，就得认真对待退出码：中断 130、出错 1、正常 0。
- 会话 id 会从用户来，路径穿越是真实威胁。用纵深防御应对：一道净化输入，一道校验落点，两道思路不同所以不会一起失效。
- 坏数据就该安静降级：存档损坏返回 `None`，别拿异常栈砸用户。

下一篇是收尾，也是最实操的一篇：把这六篇拆开看过的零件重新装起来，带你 fork CoreCoder，改出一个真正属于你自己的 coding agent。

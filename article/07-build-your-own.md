# Fork CoreCoder，搭一个你自己的 coding agent

前六篇我们把 CoreCoder 拆开看了个遍。循环、工具、模型接口、上下文压缩、并发、子 agent、CLI，每个零件都摊在桌上看过了。这一篇我们把它们装回去，而且装成你自己的。

读懂和做出来之间，隔着一次动手。这篇就是带你跨过去。读完，你手上会有一个能跑、接着你常用的模型、带着一个你亲手加的工具、按你的脾气调教过的 coding agent。

## 第零步：拿到一个绿的基线

先确认起点是好的。

```bash
# 在 GitHub 上 fork he-yufeng/CoreCoder 到你自己名下，然后
git clone https://github.com/<你的用户名>/CoreCoder
cd CoreCoder
pip install -e .
python -m pytest tests/ -q
```

最后那行应该看到 231 个测试全绿。这一步别跳。它确认了你的环境是干净的，后面你改出问题时，才能确定是你改的，不是环境本来就坏。这个项目自带的测试不是摆设，它们是你重构时的安全网，前几篇反复提到的那些坑（孤儿 tool 消息、并发竞态、路径穿越）全都有测试钉着。你接下来每改一处，都该回来跑一遍这行。

## 第一步：接上你自己的模型

CoreCoder 默认 `gpt-5.5`，但你未必想用它。第三篇讲过，换模型就是换环境变量。开发期我强烈建议先用本地的 Ollama，一分钱不花，随便折腾：

```bash
# 装个 Ollama，拉一个 coder 模型
ollama pull qwen2.5-coder

export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
export CORECODER_MODEL=qwen2.5-coder

corecoder
```

本地模型能力比旗舰差一截，但用来验证「我的改动有没有把流程跑通」绰绰有余，还省得你调试一个工具就烧一次 API 的钱。等逻辑都对了，再把环境变量换成 DeepSeek 或者别的，看真实效果。

## 第二步：加一个真正有用的工具

第二篇我们加过一个查时间的 `now`，太小儿科。这次加个有用的：让 agent 能抓取一个网页或者 API 的文本内容。有了它，你的 agent 就能去读在线文档、查一个接口返回了什么，能力一下子打开了。

新建 `corecoder/tools/fetch.py`：

```python
"""A read-only tool that fetches the text content of a URL."""

import urllib.request
from .base import Tool


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Fetch the text content of an http(s) URL. "
        "Use this to read documentation, API responses, or web pages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http:// or https:// URL to fetch",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 15)",
            },
        },
        "required": ["url"],
    }

    def execute(self, url: str, timeout: int = 15) -> str:
        if not url.startswith(("http://", "https://")):
            return "Error: only http and https URLs are supported"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CoreCoder"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(1_000_000)  # 顶多读 1MB，别让一个巨页撑爆内存
                text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error fetching {url}: {e}"
        # 太长就留头尾，跟 bash 输出截断一个套路
        if len(text) > 8000:
            text = text[:6000] + f"\n... (truncated, {len(text)} chars) ...\n" + text[-1000:]
        return text
```

注册进 `tools/__init__.py`：

```python
from .fetch import FetchUrlTool

ALL_TOOLS = [
    BashTool(),
    # ...原有的...
    FetchUrlTool(),
]
```

跑起来，问它「抓一下 https://raw.githubusercontent.com/he-yufeng/CoreCoder/main/README.md 看看这项目干嘛的」，它会调 `fetch_url` 再讲给你听。

这个小工具里其实把前几篇的几条经验都用上了。截断留头尾，是第二篇和第四篇反复出现的套路。`errors="replace"` 解码、用一个大 try-except 把任何异常都变成一句人话返回，也是前面一路见过的那套「坏数据不甩锅给用户」。还有第五篇那条最该上心的：你的工具扛不扛得住被并发调用？这个 `fetch_url` 扛得住，因为它压根没有共享可变状态，每次调用自带 URL、自吐结果，两个线程同时跑它互不干扰。这不是运气，是设计。能做成无状态的工具，就别给它加状态，这是让你的工具默认就并发安全的最省力办法。

但我也得像第二篇讲 bash 那样，把这工具的一个软肋摆到台面上。这个 `fetch_url` 有个真实的安全隐患，叫 SSRF：它能访问任意 URL，也就包括 `http://localhost`、内网地址、还有云上那个臭名昭著的 `http://169.254.169.254` 元数据接口。在你自己机器上自己用，无所谓。可一旦你把这个 agent 接到一个会执行陌生人指令的场景，这工具就成了让人探你内网的口子。要堵，就得在 `execute` 里把 URL 解析出 IP，挡掉私有地址段和环回地址。我这里故意没写这段，是想让你清楚地看到这个口子长什么样，而不是把它藏起来假装不存在。每加一个能对外发起动作的工具，先问一句「它最坏能被用来干嘛」，这个习惯比任何具体的防护代码都值钱。

## 第三步：调教它的脾气

agent 的行为风格，不在某段 if-else 里，在系统提示词里。打开 `corecoder/prompt.py`，那段 `# Rules` 就是你的 agent 的「行为准则」。原版里有这么几条：

```
1. Read before edit. 改文件前先读。
3. Verify your work. 改完跑相关测试确认。
4. Be concise. 多给代码，少废话。
```

这些规则直接塑造它怎么干活。想让它更谨慎，加一条「破坏性操作前先征求确认」；想让它写中文注释，加一条「代码注释一律用中文」；想让它每次改完必跑测试，把第 3 条写得更硬。改一行提示词，它的整个工作习惯就变了。这是 agent 工程里性价比最高的调参旋钮，没有之一，比你去改任何代码都见效快。值得你专门留出时间，把这段提示词当成产品的一部分来打磨。

## 第四步：不止做个 CLI，把它当库用

CoreCoder 顶层导出了 `Agent`、`LLM`、`Config`，意味着你不必只能用它那个交互式终端，你可以把它当库，搭出一个完全不同形态的 agent。

举个有意思的例子。第一篇和第五篇讲过，每个 `Agent` 只认它自己那套工具。利用这点，我们能拼一个「只读」的代码审查 agent，它在物理上就不可能改你的文件或者跑命令，因为我们根本没给它写工具和 bash：

```python
from corecoder import Agent, LLM
from corecoder.tools import get_tool

llm = LLM(
    model="deepseek-chat",
    api_key="sk-...",
    base_url="https://api.deepseek.com",
)

# 只给读、搜、找文件这三样，没有 write_file，没有 bash
reviewer = Agent(
    llm=llm,
    tools=[get_tool("read_file"), get_tool("grep"), get_tool("glob")],
    max_rounds=15,
)

report = reviewer.chat(
    "审查 corecoder/agent.py，找出并发相关的隐患，列成一份清单。"
)
print(report)
```

这个 `reviewer` 是个安全到你敢挂自动化里跑的 agent。它能把整个代码库读穿、搜遍，但它一个字节都改不了，一条命令都跑不了，因为那些工具压根不在它的工具集里。这正是第一篇强调「工具集是实例级的」时埋下的用法：约束一个 agent 能做什么，最干净的办法不是写一堆规则求它别乱来，而是从源头上不给它那个能力。给它的工具，就是它能力的全部边界。

把它当库用，你能搭的远不止 CLI：一个跑在 CI 里的审查 bot、一个接在 web 后端的对话接口、一个批量处理一堆仓库的脚本。内核就那一千三百行出头，外面套什么壳，是你的自由。

## 第五步：给你的改动配上测试

加完 `fetch_url`，顺手给它写个测试，跟项目里其他工具一个风格。在 `tests/test_tools.py` 里加：

```python
def test_fetch_rejects_non_http():
    fetch = get_tool("fetch_url")
    r = fetch.execute(url="file:///etc/passwd")
    assert "only http" in r
```

然后 `python -m pytest tests/ -q`，看着它从 231 个变 232 个。

我把这步单列出来，是因为它是区分「玩票」和「认真做」的分水岭。前六篇里那些最值钱的设计，孤儿 tool 消息的防御、并发安全、路径穿越的两道关，全都配着测试。这些测试不是写给别人看的形式，它们是把「藏在脑子里的不变式」固化下来，让三个月后的你、或者接手的别人，改坏了能立刻知道。你给 agent 加的每一项能力，都值得用一个测试把它的边界钉住。这是这个项目想顺便教给你的工作习惯。

## 你可以往哪走得更远

CoreCoder 是个起点，不是终点。它故意留白了不少地方，每一处都是你可以往下做的方向，而且前面几篇基本都点过名：

- **给 bash 上真沙箱**。第二篇说透了，正则黑名单只是防手滑，不是安全边界。要面对不可信输入，得上 `seccomp` 或者容器级隔离。
- **补 fallback 模型和美元硬预算**。第三篇讲过 CoreCoder 故意没做这两样，因为它们会拖进 provider 专属逻辑。你要做生产部署，这两样迟早得加。
- **把并发做得更细**。第五篇说的那条，区分工具「读」还是「写」来决定能不能并发，CoreCoder 还没做，是可以认真补的。
- **接 MCP**。让你的 agent 能挂上 Model Context Protocol 的工具生态，一下子接通一大批现成的外部能力。
- **给子 agent 更多模式**。第五篇提过 Claude Code 的子 agent 能在独立 worktree 或后台跑，CoreCoder 只做了最朴素的同步一种。

挑一个你真正需要的去做。别因为清单长就焦虑，agent 的迷人之处恰恰在于，它的核心小到一个人一个周末能读透，而它的边界又开阔到你能往任意方向长。

## 写在最后

如果这个系列只让你记住一件事，我希望是这个：编码 agent 没有想象中那么神秘，它就是一摞你完全够得着的工程决定堆起来的。

往回看这六篇，它其实就几块东西。一个有上限的循环（第一篇），外加一组接口干净的工具让它能动手（第二篇）。模型接口是薄薄一层 provider 包装（第三篇），上下文靠分层压缩对抗遗忘（第四篇）。它能分身、能并发，靠的是对共享状态的克制（第五篇）；最外面那层 CLI 会出事件、管呈现，还顺手堵了路径穿越那个口子（第六篇）。引擎加起来一千三百行出头，连这层 CLI 外壳算上整个包也才 2284 行，没有一处是你看不懂的。

现在你不光看懂了，你还 fork 了它，接了自己的模型，加了自己的工具，调了自己的脾气。它是你的了。

去给它加点别人没有的能力吧。

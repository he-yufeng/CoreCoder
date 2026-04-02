# NanoCoder

[English](README.md) | [中文](README_CN.md) | [Claude Code 源码深度导读（7 篇）](article/)

[![PyPI](https://img.shields.io/pypi/v/nanocoder)](https://pypi.org/project/nanocoder/)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/NanoCoder/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/NanoCoder/actions)

**51万行 TypeScript → 1300 行 Python。**

我通读了 Claude Code 泄露的全部源码，然后把所有不承重的部分扔掉，用 Python 重写了核心。这就是结果：一个装得进你脑子里的 AI 编程 Agent。

> *可以理解为编程 Agent 领域的 [nanoGPT](https://github.com/karpathy/nanoGPT)。*

---

```
$ nanocoder -m deepseek-chat

You > 读一下 main.py，修掉拼错的 import

  > read_file(file_path='main.py')
  > edit_file(file_path='main.py', ...)

--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-from utils import halper
+from utils import helper

修好了：halper → helper。
```

---

## 为什么做这个

Claude Code 只支持 Anthropic API。源码 51 万行你改不了。其他"替代品"要么是 10 万行你读不完的大工程，要么是套壳没真正的架构。

NanoCoder 是 **1300 行**，Claude Code 每个关键设计模式都有：

- **搜索替换编辑** — 必须唯一匹配，编辑后输出 unified diff。不会改错行。
- **并行工具执行** — 线程池同时跑多个工具。对标 Claude Code 的 StreamingToolExecutor。
- **三层上下文压缩** — 裁工具输出 → LLM 摘要 → 硬压缩。对标 HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE。
- **子代理生成** — 复杂子任务交给独立 Agent，各有自己的上下文。
- **危险命令拦截** — `rm -rf /`、fork bomb、`curl | bash`。
- **工作目录追踪** — bash 里 `cd` 之后下一条命令确实在新目录里。
- **API 重试** — 429、超时、5xx 自动指数退避重试。
- **会话持久化** — 保存和恢复对话。

## 安装

```bash
pip install nanocoder
```

```bash
# DeepSeek（国内推荐）
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com
nanocoder -m deepseek-chat

# 通义千问
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
nanocoder -m qwen-plus

# Kimi（月之暗面）
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.moonshot.cn/v1
nanocoder -m moonshot-v1-128k

# Ollama（本地）
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1
nanocoder -m qwen2.5-coder

# OpenAI
export OPENAI_API_KEY=sk-...
nanocoder

# 单次模式
nanocoder -p "给 parse_config() 加上错误处理"
```

支持**任何 OpenAI 兼容 API**：OpenAI、DeepSeek、Qwen、Kimi、GLM、Ollama、vLLM、OpenRouter、Together AI。

## 架构

```
nanocoder/
├── cli.py            REPL + 命令                   160 行
├── agent.py          Agent 循环 + 并行执行          120 行
├── llm.py            流式客户端 + 重试              150 行
├── context.py        三层压缩                       145 行
├── session.py        会话保存/恢复                   65 行
├── prompt.py         系统提示词                      35 行
├── config.py         环境变量配置                    30 行
└── tools/
    ├── bash.py       Shell + 安全 + cd 追踪          95 行
    ├── edit.py       搜索替换 + diff                  70 行
    ├── read.py       文件读取                         40 行
    ├── write.py      文件写入                         30 行
    ├── glob_tool.py  文件搜索                         35 行
    ├── grep.py       内容搜索                         65 行
    └── agent.py      子代理生成                       50 行
```

## 当库用

```python
from nanocoder import Agent, LLM

llm = LLM(model="deepseek-chat", api_key="sk-...", base_url="https://api.deepseek.com")
agent = Agent(llm=llm)
response = agent.chat("找出项目里所有 TODO 注释")
```

## 加自定义工具

```python
from nanocoder.tools.base import Tool

class HttpTool(Tool):
    name = "http"
    description = "请求一个 URL。"
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def execute(self, url: str) -> str:
        import urllib.request
        return urllib.request.urlopen(url).read().decode()[:5000]
```

## 命令

```
/model <名称>    切换模型
/compact         压缩上下文
/tokens          查看 token 用量
/save            保存会话
/sessions        列出已保存的会话
/reset           清空历史
quit             退出
```

## 对比

|  | Claude Code | Claw-Code | Aider | NanoCoder |
|---|---|---|---|---|
| 代码量 | 51万行（闭源） | 10万+行 | 5万+行 | **1300 行** |
| 模型 | 仅 Anthropic | 多模型 | 多模型 | **任意 OpenAI 兼容** |
| 能通读吗？ | 不能 | 很难 | 有点费劲 | **一个下午** |
| 适合 | 直接用 | 直接用 | 直接用 | **先看懂，再造自己的** |

## 源码导读

我还写了 [7 篇 Claude Code 架构深度导读](article/)：Agent 循环、工具系统、上下文压缩、流式执行、多 Agent、隐藏功能。想知道 NanoCoder 为什么这样设计，从那里开始。

## License

MIT。Fork，然后拿去造更好的东西，如果能标注此出处就更好了。

---

作者 **[何宇峰](https://github.com/he-yufeng)** · Agentic AI Researcher @ Moonshot AI (Kimi)

[Claude Code 源码分析（知乎 17 万阅读，6000收藏）](https://zhuanlan.zhihu.com/p/1898797658343862272)

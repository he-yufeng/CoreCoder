# 第三篇：让 AI 安全地改你的代码

Claude Code 里最让我佩服的设计不是什么高深的架构模式，是 `FileEditTool` 里一个看似简单的约束：**old_string 必须在文件中唯一出现。**

就这一条规则，解决了 AI 代码编辑领域最头疼的问题。

---

## 代码编辑的三条死路

让 LLM 编辑代码，业界试过很多方案，踩过很多坑。

**方案一：行号补丁**（"把第 42 行改成 xxx"）。

看起来最直接，实际最不靠谱。LLM 对行号的记忆能力非常差——尤其在上下文压缩之后，它记住的行号可能已经完全对不上了。更致命的是并发场景：如果 LLM 在第 30 行插入了一行，后面所有行号都变了。下一个编辑操作如果还在用旧行号，就会改错地方。

这不是小概率事件。多轮编辑时几乎一定会遇到。

**方案二：整文件重写**（"输出完整的新文件"）。

最暴力也最安全的方案。问题是成本。一个 500 行的文件，你只想改 2 行，LLM 要重新生成全部 500 行。一个 GPT-4o 的 output token 大约 $15/M，500 行大概 2000 token，一次编辑就是 $0.03。如果一个任务需要改 20 个文件，每个文件改好几轮……费用爆炸。

而且 LLM 在"复制"长文本时经常出错——丢行、改格式、把 tab 变空格。你本来只想改个函数签名，结果整个文件的缩进被弄乱了。

**方案三：unified diff 格式**（"输出一个 diff 补丁"）。

diff 是人类代码审查的标准格式，理论上效率最高。但 LLM 生成的 diff 格式正确率很低。`@@` 行号偏移经常算错，上下文行数不对，有时候连 `+` `-` 前缀都会乱。你需要一个很复杂的容错解析器来处理这些格式错误，而且就算解析成功了，apply 到错误的位置也没办法检测出来。

Aider 在这条路上走得最远，做了大量 prompt engineering 来让 LLM 输出规范的 diff 格式，但还是无法完全消除格式错误。

---

## Claude Code 的解法：搜索替换

Claude Code 选了第四条路。

LLM 不需要知道行号，不需要输出整个文件，不需要生成 diff 格式。它只需要做两件事：

1. 给出一段**精确的**、**在文件中唯一存在**的文本（old_string）
2. 给出替换后的文本（new_string）

```json
{
  "file_path": "src/auth.py",
  "old_string": "def verify_token(token):\n    return jwt.decode(token, SECRET)",
  "new_string": "def verify_token(token):\n    try:\n        return jwt.decode(token, SECRET)\n    except jwt.ExpiredSignatureError:\n        return None"
}
```

关键约束：**old_string 在文件中必须恰好出现 1 次**。

0 次 → LLM 记错了文件内容。返回错误，附带文件开头的预览，让它先 `read_file` 再试。

2 次以上 → 给的上下文不够，无法确定改哪一处。返回错误，要求 LLM 包含更多周围的行来消除歧义。

恰好 1 次 → 确定性替换。不可能改错位置。

这个约束的妙处在于：它把"编辑文件"这个本质模糊的操作变成了一个**确定性**操作。不管 LLM 的记忆力有多差，只要它能给出一段唯一的文本，编辑就一定是准确的。

CoreCoder 完整实现了这个模式（`tools/edit.py`，70 行），而且加了 unified diff 输出——每次编辑后你能看到 `--- a/file +++ b/file` 格式的变更对比。

---

## 工具接口：buildTool() 工厂

做过 Agent 框架的人可能都写过一个 `BaseTool` 基类然后 extend。Claude Code 完全没有继承。

所有工具都是通过 `buildTool()` 工厂函数生成的纯对象，类型定义在 `src/Tool.ts`（792 行）：

```typescript
// src/Tool.ts (简化)
type ToolDef<Input, Output> = {
  name: string
  description: string
  inputSchema: ZodSchema<Input>        // Zod 做校验 + 自动生成 JSON Schema
  call(input: Input, ctx: ToolUseContext): AsyncGenerator<ToolProgress | ToolResult>

  // 安全
  isReadOnly(): boolean
  getPermissions(input: Input): ToolPermission[]
  validateInput?(input: Input, ctx: ToolUseContext): ValidationResult
  checkPermissions(input: Input, ctx: ToolUseContext): PermissionResult

  // UI
  renderToolUse?(input: Input): ReactNode
  renderToolResult?(result: Output): ReactNode

  // 上下文经济
  getToolUseSummary?(input: Input, result: Output): string
  maxResultSizeChars: number
}
```

几个值得注意的字段：

**`getToolUseSummary()`**：当上下文压缩时，这个方法决定一个工具调用怎么被摘要。比如 `BashTool` 的摘要可能是"ran: npm test (exit code 0)"，把 500 行输出压缩成一句话。不同工具知道自己输出的哪些部分重要，所以摘要策略也不同。

**`maxResultSizeChars`**：超过这个阈值的输出会写入磁盘，上下文里只留一个文件路径引用和摘要。设为 `Infinity` 表示永远不写盘。`GrepTool` 的阈值比 `ReadFileTool` 低，因为搜索结果通常比较长。

**`renderToolUse()` 返回 ReactNode**：是的，工具的终端渲染组件是 React 组件。工具调用在终端里有漂亮的进度条、代码高亮、diff 预览，全是 React + Ink 渲染的。

---

## 两阶段门控：为什么权限弹窗没那么烦

Claude Code 的权限弹窗应该是所有 AI 编程工具里最不烦人的。原因是两阶段门控：

```
用户说："删掉 temp/ 目录"
  │
  ▼
validateInput()
  ├─ 路径存在？→ 不存在 → 直接告诉 LLM "路径不存在"，不弹窗
  ├─ 参数合法？→ 不合法 → 直接告诉 LLM "参数错误"，不弹窗
  └─ 全部通过 →
          │
          ▼
      checkPermissions()
          ├─ 当前权限级别允许？→ 是 → 直接执行
          └─ 需要确认？→ 弹窗问用户
```

大部分被拒绝的工具调用是因为输入不合法（文件不存在、路径格式错误、参数类型不对），而不是权限不够。第一阶段在不弹窗的情况下就处理了这些，用户只在真正需要做权限决策时才被打扰。

Claude Code 有五级权限模式：全自动、推荐操作自动、写操作需确认、所有操作需确认、只读模式。企业管理员可以通过配置文件强制设定。

---

## BashTool：1143 行的安全堡垒

BashTool 单文件 1143 行。它做的远不止 `subprocess.run(command)`：

**命令分类器。** 每条命令被解析成 search / read / write 三个类别。`grep` 是 search，`cat` 是 read，`rm` 是 write。分类结果用于匹配权限规则——用户可能允许了所有 search 和 read 操作，但 write 需要逐一确认。复合命令（`ls && git push`）会被拆开逐段判定。

**沙箱。** macOS 上走 `sandbox-exec`，Linux 上走 `seccomp`。沙箱限制了网络访问、文件系统访问范围等。

**大输出管理。** 命令输出超过阈值时截断，保留头部和尾部。头部通常有命令回显和初始结果，尾部通常有总结信息和错误消息。中间的重复输出对 LLM 做决策用处不大。

**交互式命令拦截。** `vim`、`less`、`ssh`、`python`（无参数）这些需要用户交互的命令，直接拒绝。LLM 不会操作交互式终端。

**超时和后台转换。** 超过 15 秒的命令自动转后台执行，LLM 可以继续做其他事情。

**sed 检测。** 检测到 `sed -i` 命令时，UI 从 "Bash" 样式切换成文件编辑样式。因为 `sed -i` 本质上是在编辑文件，应该像 edit_file 一样展示 diff。

CoreCoder 的 BashTool 是 80 行的精简版：输出截断（头尾保留）、超时控制、9 个危险命令正则检测（`rm -rf /`、fork bomb、`curl | bash` 等）。覆盖了最核心的安全需求。

---

## 工具动态组装

Claude Code 没有全局工具注册表。每个 session 的工具池是动态组装的：

```typescript
// 会话开始时组装工具列表
const tools = [
  ...builtinTools,                 // 内置的 30+ 个工具
  ...mcpTools,                     // 从 MCP 服务器发现的工具
  ...agentDefinedTools,            // Agent 定义的自定义工具
  ...skillTools,                   // Skill 暴露的工具
]
```

部分工具是"懒加载"的——标记了 `shouldDefer: true` 的工具默认不在工具列表里，只有当 LLM 触发了 `ToolSearch` 之后才被发现并加载。这避免了工具列表过长导致 LLM 选择困难（工具太多 LLM 会犹豫不决，影响响应速度和准确率）。

CoreCoder 用的是最简单的静态列表。但接口设计留了扩展口——`Agent.__init__` 接受 `tools` 参数，传什么用什么。

---

> 本文是 [Claude Code 源码导读](00-index.md) 系列第 3 篇。配套实现：[CoreCoder](https://github.com/he-yufeng/CoreCoder)

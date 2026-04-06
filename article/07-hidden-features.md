# 第七篇：Feature Flag 背后的秘密

读 Claude Code 源码最刺激的部分不是已发布的功能——那些你用过的工具循环、编辑命令、上下文压缩，虽然做得精细，但概念上并不超出预期。真正让我在凌晨四点从椅子上跳起来的，是那些**编译时就被删掉、用户永远看不到**的代码。

我在源码里 grep 了所有 `feature('` 调用和 `tengu_` 前缀的 flag，找到了 44 个。有些是小的优化开关。有些是完整的、成千上万行代码的产品功能，只差一个布尔值就能上线。其中几个如果发布了，可能会改变整个 AI 编程工具市场的游戏规则。

## Feature Flag 的工作原理

Claude Code 使用两层 Feature Flag 系统：

**编译时 Flag**：通过 `feature()` 函数包裹的代码块。如果 flag 未启用，Bun 的打包器在编译时就把整个代码块删掉（Dead Code Elimination）。最终发布的 npm 包里完全不包含这些代码。

```typescript
// 编译时 flag 示例
if (feature('KAIROS')) {
  // 这整个块在发布版本中不存在
  import('./kairos/daemon').then(m => m.startDaemon())
}
```

**运行时 Flag**：通过 GrowthBook 服务远程控制。以 `tengu_` 为前缀（Tengu/天狗是 Claude Code 项目的内部代号）。用于灰度发布和 A/B 测试。

```typescript
// 运行时 flag 示例
if (growthbook.isOn('tengu_amber_quartz_disabled')) {
  disableVoiceMode()
}
```

编译时 flag 保护的是"还没准备好"的功能，运行时 flag 控制的是"准备好了但要逐步放量"的功能。

## KAIROS：永驻模式

这是我最感兴趣的未发布功能。

KAIROS 的核心概念：Claude Code 不再是"你打开它 → 聊天 → 关掉"的会话模式，而是**一直在后台运行的守护进程**。它会定期醒来，检查是否有事情需要做，然后自主决定是否行动。

从源码中的 feature flag 和相关常量来看，KAIROS 至少包含：

- **DAEMON 模式**：Claude Code 作为守护进程运行，不需要终端窗口
- **KAIROS_BRIEF**：定期给用户发"简报"——你的代码库最近有什么变化、CI 有没有挂、有没有新的 PR 需要 review
- **PROACTIVE 模式**：Agent 不等用户指令，自主决定执行任务（比如发现测试挂了就自动修）
- **autoDream**：后台的"做梦"模式——在空闲时整理和压缩记忆，类似人类睡眠时的记忆巩固

这个方向如果做出来，AI 编程助手就从"工具"变成了"同事"——它一直在看你的项目，有事就干，没事就默默学习代码库。

## Buddy 宠物系统

是的，你没看错。Claude Code 源码里有一个完整的电子宠物系统。

`src/buddy/types.ts` 定义了 18 个物种，名称用 hex 编码（大概是为了不在代码里出现明显的宠物名影响搜索结果）。`src/buddy/companion.ts` 用 Mulberry32 伪随机数生成器来决定宠物的属性。

这个系统的存在说明 Anthropic 在认真思考"怎么让开发者跟 AI 工具建立情感连接"。不管最终会不会发布，这种产品思考值得注意。

## Voice Mode（Amber Quartz）

内部代号 Amber Quartz（琥珀石英）。`src/voice/` 目录是入口。

从代码结构看，Voice Mode 允许用户通过语音跟 Claude Code 交互。不只是语音输入——它涉及语音合成、实时转写、语音打断处理（你说话的时候 Claude 的语音要停下来）。

运行时 flag `tengu_amber_quartz_disabled` 是一个 kill switch，说明这个功能可能已经在内部测试了。

## Bridge Mode

31 个文件，是未发布功能里代码量最大的。

Bridge Mode 让 Claude Code 可以被远程控制——一个在云端运行的 Claude Code 实例，通过 WebSocket 或类似协议跟本地的 IDE/终端连接。支持 32 个并发 session。

这意味着 Anthropic 可能在做"云端 Agent"——你不需要在本地跑 Claude Code，而是连接到一个远程运行的实例。对于 CI/CD 场景或者资源受限的设备，这个功能很有价值。

## Coordinator Mode

多 Agent 编排的更高层抽象。不同于 AgentTool 的"主从"模式，Coordinator 可以让多个 Agent 平等协作，由一个"协调者"来分配任务和整合结果。

源码里有 `src/coordinator/coordinatorMode.ts`，但大部分逻辑还在 Feature Flag 后面。

## Undercover Mode

这个最有趣。

`src/utils/undercover.ts` 是 Anthropic 内部员工专用的模式。当 Anthropic 的工程师用 Claude Code 给**外部开源项目**提交代码时，这个模式会自动去除所有 AI 归因标记（比如 `Co-Authored-By: Claude` 这种 commit 信息），让提交看起来完全是人类写的。

这说明 Anthropic 内部已经在大规模使用 Claude Code 做日常开发了，而且对外的 PR 不想让人知道是 AI 写的。

## 对开发者的启示

这些未发布功能的价值不在于你能不能用它们（它们还没发布），而在于它们揭示了 AI 编程工具的发展方向：

1. **从工具到同事**（KAIROS）——AI 不再需要你主动发起对话
2. **从文本到多模态**（Voice Mode）——编程不只是打字
3. **从本地到云端**（Bridge Mode）——Agent 不需要跑在你的笔记本上
4. **从单 Agent 到组织**（Coordinator）——多个 AI 协作完成复杂项目

这些方向现在都是开源社区可以探索的空白地带。

---

> 本文是 [Claude Code 源码导读](00-index.md) 系列第 7 篇。配套实现：[CoreCoder](https://github.com/he-yufeng/CoreCoder)

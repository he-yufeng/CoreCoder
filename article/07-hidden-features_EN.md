# Part 7: The Secret Behind Feature Flags

The most exciting part of reading Claude Code isn't the released features—the tool loops, editing commands, context compression you've used before, while sophisticated, aren't conceptually groundbreaking. What truly makes me jump out of my chair at 4 AM is the code that's **deleted during compilation, and users will never see**.

I grepped through the source code for all flags with `feature(` calls and `tengu_` prefixes and found 44. Some are small optimization switches. Others are complete product features with thousands of lines of code, just one boolean value away from deployment. Several of these, if released, could potentially be game-changers in the entire AI programming tool market.

## How Feature Flags Work

Claude Code uses a two-layer Feature Flag system:

**Compile-time Flags**: Code blocks wrapped by the `feature()` function. If the flag is not enabled, Bun's packager removes the entire code block during compilation (Dead Code Elimination). The final npm package does not contain this code at all.

```typescript
// Example of a compile-time flag
if (feature('KAIROS')) {
  // This entire block does not exist in the release version
  import('./kairos/daemon').then(m => m.startDaemon())
}
```

**Runtime Flags**: Via GrowthBook Remote control of services. Prefixed with `tengu_` (Tengu is the internal codename for the Claude Code project). Used for canary releases and A/B testing.

```typescript
// Runtime flag example
if (growthbook.isOn('tengu_amber_quartz_disabled')) {
  disableVoiceMode()
}
```

Compile-time flags protect features that are "not yet ready," while runtime flags control features that are "ready but to be gradually rolled out."

## KAIROS: Persistent Mode

This is the unreleased feature I'm most interested in.

KAIROS's core concept: Claude Code is no longer a "you open it → chat → close" session mode, but rather a **daemon process that runs continuously in the background**. It wakes up periodically, checks if there's anything that needs to be done, and then autonomously decides whether to act.

Based on the feature flags and related constants in the source code, KAIROS includes at least:

- **DAEMON mode**: Claude Code Running as a daemon so that it doesn't require a terminal window.
- **KAIROS_BRIEF**: regularly sends users "briefings"—reporting recent changes to your codebase, CI failures, and new PRs requiring review.
- **PROACTIVE Mode**: the agent executes tasks autonomously without user commands (e.g., automatically fixing failed tests).
- **autoDream**: a background "dreaming" mode—organizing and compressing memories during idle time, similar to memory consolidation during human sleep.

If this direction is successfully implemented, the AI ​​programming assistant will transform from a "tool" into a "colleague"—it's constantly monitoring your project, performing tasks when needed, and silently learning from the codebase when not.

## Buddy Pet System

Yes, you read that. The Claude Code source code contains a complete electronic pet system.

`src/buddy/types.ts` defines 18 species, with names encoded in hexadecimal (presumably to avoid obvious pet names appearing in the code and affecting search results). `src/buddy/companion.ts` uses... A pseudo-random number generator on the Mulberry32 determines the pet's attributes.

The existence of this system demonstrates Anthropic's serious consideration of "how to create an emotional connection between developers and AI tools." Regardless of whether it will ultimately be released, this product thinking is noteworthy.

## Voice Mode (Amber Quartz)

Internal codename: Amber Quartz. The `src/voice/` directory is the entry point.

From the code structure, Voice Mode allows users to interact with Claude Code via voice. It's not just about voice input—it involves speech synthesis, real-time transcription, and voice interruption handling (Claude's voice stops when you speak).

The runtime flag `tengu_amber_quartz_disabled` is a kill switch, indicating that this feature may already be in internal testing.

## Bridge Mode

31 files, the largest codebase among the unreleased features.

Bridge Mode allows Claude Code to be remotely controlled—an instance of Claude Code running in the cloud connects to the local IDE/terminal via WebSocket or a similar protocol. Supports... 32 concurrent sessions.

This means Anthropic may be acting as a "cloud agent"—you don't need to run Claude code locally, but instead connect to a remotely running instance. This feature is valuable for CI/CD scenarios or resource-constrained devices.

## Coordinator Mode

A higher-level abstraction for multi-agent orchestration. Unlike AgentTool's "master-slave" mode, Coordinator allows multiple agents to collaborate equally, with a "coordinator" allocating tasks and integrating results.

The source code contains `src/coordinator/coordinatorMode.ts`, but most of the logic is behind the Feature Flag.

## Undercover Mode

This is the most interesting.

`src/utils/undercover.ts` is a mode used internally by Anthropic employees. When Anthropic engineers commit code to **external open-source projects** using Claude code, this mode automatically removes all AI attribution tags (such as `Co-Authored-By: Claude` commits). The information makes the submissions appear entirely human-written.

This indicates that Anthropic is already using Claude Code extensively for daily development internally, and they don't want people to know if their public PRs is AI-written.

## Implications for Developers

The value of these unreleased features isn't whether you can use them (they haven't been released yet), but rather that they reveal the future direction of AI programming tools:

1. **From Tool to Colleague** (KAIROS) – AI no longer needs you to initiate conversations.
2. **From Text to Multimodal** (Voice Mode) – Programming is more than just typing.
3. **From Local to Cloud** (Bridge Mode) – Agents don't need to run on your laptop.
4. **From Single Agent to Organization** (Coordinator) – Multiple AIs collaborate on complex projects.

These directions are currently unexplored areas for the open-source community.

---

> This article is the 7th in the [Claude Code Source Code Guide](00-index_EN.md) series. This article is accompanied by the implementation: [NanoCoder](https://github.com/he-yufeng/NanoCoder).
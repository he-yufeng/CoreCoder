"""Interactive REPL - the user-facing terminal interface."""

import argparse
import json
import os
import sys

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent
from .config import Config
from .hooks import load_hooks
from .llm import LLM, LiteLLM
from .mcp import load_mcp_tools
from .permissions import DEFAULT_STORE, Permission
from .session import SavedSession, common_message_prefix, fork_session, list_sessions, read_session, save_session
from .tools import ALL_TOOLS

console = Console()
err_console = Console(stderr=True)


def _parse_args():
    p = argparse.ArgumentParser(
        prog="corecoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--yes", action="store_true", help="Auto-approve every tool call (for scripts and CI)")
    p.add_argument("--demo", action="store_true", help="Run the offline scripted demo (no API key needed)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    sessions = p.add_mutually_exclusive_group()
    sessions.add_argument(
        "--list-sessions",
        nargs="?",
        const="",
        metavar="QUERY",
        help="List saved sessions offline, optionally matching ID, model or preview",
    )
    sessions.add_argument("--inspect-session", metavar="ID", help="Check a saved session and show its metadata offline")
    sessions.add_argument("--export-session", metavar="ID", help="Export saved dialogue as Markdown offline")
    sessions.add_argument("--fork-session", metavar="ID", help="Create a session before a retained turn, offline")
    sessions.add_argument("--compare-sessions", nargs=2, metavar=("LEFT", "RIGHT"), help="Compare two saved histories offline")
    p.add_argument("--before-turn", type=int, metavar="N", help="Retained user turn to leave out (1-based, for --fork-session)")
    p.add_argument("--json", action="store_true", help="Print the session list as JSON (with --list-sessions)")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.json and args.list_sessions is None:
        p.error("--json requires --list-sessions")
    if (args.fork_session is None) != (args.before_turn is None):
        p.error("--fork-session and --before-turn must be used together")
    if args.before_turn is not None and args.before_turn < 1:
        p.error("--before-turn must be positive")
    if args.demo or args.prompt is not None or args.resume is not None:
        for option, active in (
            ("--list-sessions", args.list_sessions is not None),
            ("--inspect-session", args.inspect_session is not None),
            ("--export-session", args.export_session is not None),
            ("--fork-session", args.fork_session is not None),
            ("--compare-sessions", args.compare_sessions is not None),
        ):
            if active:
                p.error(f"{option} cannot be combined with --demo, --prompt or --resume")
    return args


def _show_sessions(query: str = ""):
    sessions = list_sessions(query)
    if not sessions:
        message = "No saved sessions match." if query.strip() else "No saved sessions."
        console.print(message, style="dim")
    for s in sessions:
        console.print(f"  {s['id']} ({s['model']}, {s['saved_at']}) {s['preview']}", markup=False)


def _require_saved_session(session_id: str) -> SavedSession:
    try:
        return read_session(session_id)
    except FileNotFoundError:
        reason = "not found"
    except OSError:
        reason = "could not read the session file"
    except ValueError:
        reason = "invalid session file"
    err_console.print(f"Session '{session_id}': {reason}.", style="red", markup=False)
    raise SystemExit(1)


def main():
    args = _parse_args()

    if args.list_sessions is not None:
        if args.json:
            print(json.dumps(list_sessions(args.list_sessions)))
        else:
            _show_sessions(args.list_sessions)
        return

    if args.inspect_session is not None:
        saved = _require_saved_session(args.inspect_session)
        console.print(
            f"Session: {saved.session_id}\nModel: {saved.model}\nSaved: {saved.saved_at}\n"
            f"Messages: {len(saved.messages)}\nRetained turns: {len(saved.retained_turn_starts())}",
            markup=False,
        )
        return

    if args.export_session is not None:
        saved = _require_saved_session(args.export_session)
        sys.stdout.buffer.write(saved.to_markdown().encode("utf-8"))
        return

    if args.fork_session is not None:
        saved = _require_saved_session(args.fork_session)
        try:
            sid = fork_session(saved, args.before_turn)
        except ValueError as exc:
            err_console.print(str(exc), style="red", markup=False)
            raise SystemExit(1) from None
        except OSError:
            err_console.print("Could not create a new session file.", style="red")
            raise SystemExit(1) from None
        console.print(f"Forked session: {sid}", markup=False)
        return

    if args.compare_sessions is not None:
        left, right = (_require_saved_session(sid) for sid in args.compare_sessions)
        shared = common_message_prefix(left.messages, right.messages)
        left_remaining = len(left.messages) - shared
        right_remaining = len(right.messages) - shared
        console.print(
            f"Left: {left.session_id}\nRight: {right.session_id}\n"
            f"Shared stored messages: {shared}\n"
            f"Remaining messages: {left_remaining} left, {right_remaining} right",
            markup=False,
        )
        if left_remaining and right_remaining:
            console.print(f"First different stored message: {shared + 1}")
        return

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo())

    saved = _require_saved_session(args.resume) if args.resume is not None else None
    config = Config.from_env()

    # CLI args override the saved model and environment defaults.
    if args.model:
        config.model = args.model
    elif saved is not None:
        config.model = saved.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        fallback_models=config.fallback_models,
        on_fallback=lambda model: err_console.print(f"[dim]Using fallback model: {model}[/dim]"),
    )
    # consent layer: ask in the REPL, refuse in one-shot mode, --yes skips it
    if args.yes:
        permission = Permission(allow_all=True)
    elif args.prompt:
        permission = Permission()
    else:
        permission = Permission(ask=_ask_permission, persist_path=DEFAULT_STORE)
    agent = Agent(
        llm=llm,
        tools=[*ALL_TOOLS, *load_mcp_tools()],
        max_context_tokens=config.max_context_tokens,
        permission=permission,
        hooks=load_hooks(),
    )

    if saved is not None:
        agent.messages = saved.messages
        console.print(f"Resumed session: {saved.session_id} (model: {config.model})", style="green", markup=False)

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config)


def _ask_permission(tool_name: str, arguments: dict) -> str:
    """REPL consent prompt. Anything but a clear yes counts as a no."""
    console.print(f"\n[bold yellow]permission requested:[/] [cyan]{tool_name}[/cyan]({_brief(arguments)})")
    try:
        answer = pt_prompt("  [y] allow once  [a] always allow this tool  [n] deny: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]denied[/dim]")
        return "deny"
    if answer in ("y", "yes"):
        return "once"
    if answer in ("a", "always"):
        return "always"
    return "deny"


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    perm = agent.permission
    if perm is not None and perm.ask is None and not perm.allow_all:
        console.print("[dim]one-shot mode: mutating tools are refused unless you pass --yes[/dim]")

    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    def on_reasoning(tok):
        # thinking goes to stderr so piped stdout stays the plain answer
        err_console.print(f"[dim]{tok}[/dim]", end="")

    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool, on_reasoning=on_reasoning)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        # one-shot mode: print whatever went wrong and exit non-zero
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    print()


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    perm = agent.permission
    mode = "auto-approve every tool call (--yes)" if (perm and perm.allow_all) else "ask before mutating tools"
    mcp_count = sum(1 for t in agent.tools if t.name.startswith("mcp__"))
    console.print(Panel(
        f"[bold]CoreCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + f"\nPermissions: [cyan]{mode}[/cyan]"
        + (f"\nHooks: [cyan]{len(agent.hooks.pre)} pre, {len(agent.hooks.post)} post[/cyan]"
           " from ~/.corecoder/hooks.json" if agent.hooks else "")
        + (f"\nMCP: [cyan]{mcp_count} tools[/cyan] from ~/.corecoder/mcp.json" if mcp_count else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.corecoder_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        try:
            user_input = pt_prompt(
                "You (plan) > " if agent.plan_mode else "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/plan":
            agent.plan_mode = not agent.plan_mode
            if agent.plan_mode:
                console.print(
                    "[yellow]Plan mode on.[/yellow] The agent can look but not touch: it will "
                    "investigate read-only and present a plan. Type [bold]approve[/bold] to "
                    "accept the plan, or [bold]/plan[/bold] again to exit."
                )
            else:
                console.print("[yellow]Plan mode off.[/yellow]")
            continue
        if agent.plan_mode and user_input.lower() in ("approve", "/approve"):
            agent.plan_mode = False
            console.print("[yellow]Plan mode off.[/yellow]")
            user_input = "approve"  # the approval itself goes to the model, which then executes
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.context.maybe_compress(agent.messages, agent.llm)
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            sid = save_session(agent.messages, config.model)
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: corecoder -r {sid}")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/undo":
            from .checkpoints import pending, undo
            console.print(undo())
            left = pending()
            if left:
                console.print(f"[dim]{left} more checkpoint(s) on the stack.[/dim]")
            continue
        if user_input == "/sessions":
            _show_sessions()
            continue
        if user_input == "/mcp" or user_input.startswith("/mcp "):
            _mcp(user_input[4:].strip())
            continue

        # an unknown /command shouldn't be sent to the model as a prompt
        if user_input.startswith("/"):
            console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok, streamed=streamed):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        def on_reasoning(tok, streamed=streamed):
            # dim the thinking inline; mark the stream non-empty so the final
            # newline logic below still fires even if the reply itself is empty
            streamed.append(tok)
            console.print(f"[dim]{tok}[/dim]", end="")

        try:
            response = agent.chat(user_input, on_token=on_token, on_tool=on_tool,
                                  on_reasoning=on_reasoning)
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:  # noqa: BLE001
            # keep the REPL alive no matter what chat() throws
            console.print(f"\n[red]Error: {e}[/red]")


def _mcp(arg: str):
    """The /mcp command: server states, or `reconnect <name>` to revive one."""
    from . import mcp
    parts = arg.split()
    if not parts:
        if not mcp._live_clients:
            console.print("[dim]No MCP servers running (configure ~/.corecoder/mcp.json).[/dim]")
        for client in mcp._live_clients:
            if client.error is None:
                console.print(f"  [cyan]{client.name}[/cyan]: alive, {len(client.tools)} tools")
            else:
                console.print(
                    f"  [cyan]{client.name}[/cyan]: [red]dead[/red] ({client.error})"
                    f", revive with /mcp reconnect {client.name}"
                )
        return
    if len(parts) == 2 and parts[0] == "reconnect":
        client = next((c for c in mcp._live_clients if c.name == parts[1]), None)
        if client is None:
            console.print(f"[red]No MCP server named '{parts[1]}'.[/red]")
            return
        try:
            client.reconnect()
        except (mcp.MCPError, OSError) as e:
            console.print(f"[red]Reconnect failed:[/red] {e}")
        else:
            console.print(f"[green]Reconnected:[/green] [cyan]{client.name}[/cyan], {len(client.tools)} tools")
        return
    console.print("[yellow]Usage: /mcp [reconnect <name>][/yellow]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /undo          Revert the most recent file change\n"
        "  /plan          Toggle plan mode: read-only, then a plan to approve\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  /mcp           MCP server status; /mcp reconnect <name> revives a dead one\n"
        "  quit           Exit CoreCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CoreCoder Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")

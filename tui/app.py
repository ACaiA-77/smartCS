from __future__ import annotations

import argparse
import re
import uuid
from dataclasses import dataclass

import httpx

from tui.client import AgentApiClient, AgentApiError, ChatResult


HELP_TEXT = """Commands:
  /help      Show this help
  /health    Check backend health
  /history   Show current session history
  /session   Show current session id
  /exit      Exit
  /quit      Exit
"""


@dataclass(frozen=True)
class ParsedInput:
    name: str
    value: str = ""


def make_session_id() -> str:
    return f"tui_{uuid.uuid4().hex[:8]}"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SmartCS terminal chat client")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user-id", default="terminal_user")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--timeout", type=float, default=30)
    return parser


def parse_input(raw_input: str) -> ParsedInput:
    text = raw_input.strip()
    if not text:
        return ParsedInput("empty")

    command = text.lower()
    if command in {"/exit", "/quit"}:
        return ParsedInput("exit")
    if command in {"/help", "/health", "/history", "/session"}:
        return ParsedInput(command.removeprefix("/"))
    return ParsedInput("message", text)


def format_terminal_text(text: str) -> str:
    """Convert common Markdown marks into readable plain terminal text."""
    text = re.sub(r"\[\[([^\]]+)\]\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("`", "")
    return text


def format_chat_result(result: ChatResult) -> str:
    response = format_terminal_text(result.response)
    return (
        f"\nAgent: {response}\n"
        f"[intent={result.intent}; compliance_passed={result.compliance_passed}; "
        f"session_id={result.session_id}]"
    )


def format_history(history: dict) -> str:
    messages = history.get("messages", [])
    if not messages:
        return "No history for this session yet."

    lines = [f"History for session {history.get('session_id', '')}:"]
    for item in messages:
        role = item.get("role", "unknown")
        content = format_terminal_text(item.get("content", ""))
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)


def run_repl(client: AgentApiClient, user_id: str, session_id: str) -> None:
    print("SmartCS TUI")
    print(f"Backend: {client.base_url}")
    print(f"User: {user_id}")
    print(f"Session: {session_id}")
    print("Type /help for commands. Type /exit to quit.")

    try:
        health = client.health()
        print(f"Backend health: {health}")
    except (AgentApiError, httpx.HTTPError) as exc:
        print(f"Backend health check failed: {exc}")
        print("Start the API service with: python -m api.main")

    while True:
        try:
            raw_input = input("\nYou> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return

        parsed = parse_input(raw_input)
        try:
            if parsed.name == "empty":
                continue
            if parsed.name == "exit":
                print("Bye.")
                return
            if parsed.name == "help":
                print(HELP_TEXT)
                continue
            if parsed.name == "session":
                print(f"session_id={session_id}")
                continue
            if parsed.name == "health":
                print(client.health())
                continue
            if parsed.name == "history":
                print(format_history(client.history(session_id)))
                continue

            result = client.chat(parsed.value, user_id=user_id, session_id=session_id)
            session_id = result.session_id
            print(format_chat_result(result))
        except (AgentApiError, httpx.HTTPError) as exc:
            print(f"Request failed: {exc}")


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    session_id = args.session_id or make_session_id()
    client = AgentApiClient(base_url=args.base_url, timeout=args.timeout)
    try:
        run_repl(client=client, user_id=args.user_id, session_id=session_id)
    finally:
        client.close()


if __name__ == "__main__":
    main()

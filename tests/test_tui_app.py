from __future__ import annotations

from tui.app import build_arg_parser, format_chat_result, make_session_id, parse_input
from tui.client import ChatResult


def test_parse_input_classifies_commands_and_messages():
    assert parse_input("你好").name == "message"
    assert parse_input("你好").value == "你好"
    assert parse_input("/help").name == "help"
    assert parse_input("/health").name == "health"
    assert parse_input("/history").name == "history"
    assert parse_input("/session").name == "session"
    assert parse_input("/exit").name == "exit"
    assert parse_input("/quit").name == "exit"
    assert parse_input("   ").name == "empty"


def test_make_session_id_uses_tui_prefix_and_eight_hex_chars():
    session_id = make_session_id()

    assert session_id.startswith("tui_")
    assert len(session_id) == 12
    int(session_id.removeprefix("tui_"), 16)


def test_arg_parser_defaults_match_tui_plan():
    args = build_arg_parser().parse_args([])

    assert args.base_url == "http://localhost:8000"
    assert args.user_id == "terminal_user"
    assert args.session_id is None
    assert args.timeout == 30


def test_format_chat_result_includes_answer_and_debug_fields():
    result = ChatResult(
        response="iPhone年化约3.5%-5.2%。",
        session_id="tui_12345678",
        intent="knowledge_rag",
        compliance_passed=True,
    )

    formatted = format_chat_result(result)

    assert "iPhone年化约3.5%-5.2%。" in formatted
    assert "target=knowledge_rag" in formatted
    assert "compliance_passed=True" in formatted
    assert "session_id=tui_12345678" in formatted

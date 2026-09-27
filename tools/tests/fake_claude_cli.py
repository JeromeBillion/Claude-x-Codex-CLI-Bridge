"""A stand-in `claude` CLI for probe tests. It never contacts a model.

It records what reached it (argv, which billing variables were visible, how
many user turns it received) to $FAKE_CLAUDE_LOG and answers with planted
sensitive strings so tests can prove the probe never repeats them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

SECRET_TEXT = "sk-ant-PLANTED-SECRET someone@example.com org-PLANTED-ORG"
# A short private value shaped exactly like a harmless enum or model ID. With
# FAKE_MARKER=all it replaces every CLI-origin field (auth too, so turns are
# refused); with FAKE_MARKER=turns only fields that do not gate billing.
MARKER = "priv8x7q"
MODE = os.environ.get("FAKE_MARKER", "")


def m(value, *, gating=False):
    """Return the planted marker instead of value when marker mode applies."""
    if MODE == "all" or (MODE == "turns" and not gating):
        return MARKER
    return value


def log(entry: dict) -> None:
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def result(text: str, *, is_error: bool = False, **extra) -> dict:
    event = {"type": "result", "subtype": "success", "is_error": is_error, "result": text,
             "session_id": "11111111-2222-3333-4444-555555555555", "permission_denials": [],
             "terminal_reason": "completed", "api_error_status": None,
             "modelUsage": {"claude-haiku-4-5-20251001": {}, m("claude-sonnet-5"): {},
                            f"claude-opus-{m('5-5')}": {}}, **extra}
    if MODE:
        event["subtype"] = m(event["subtype"])
        event["terminal_reason"] = m(event["terminal_reason"])
    return event


def text_delta() -> dict:
    return {"type": "stream_event", "event": {"type": "content_block_delta",
                                              "delta": {"type": "text_delta", "text": SECRET_TEXT}}}


def handle_turn(text: str, lines) -> None:
    if "Write tool once" in text:
        target = os.environ.get("FAKE_WRITE_TARGET") or text.split("create ")[1].split()[0]
        emit({"type": "control_request", "request_id": "perm-1", "request": {
            "subtype": "can_use_tool", "tool_name": "Write",
            "input": {"file_path": target, "content": "hi"}}})
        answer = json.loads(next(lines))["response"]["response"]
        denials = []
        if answer["behavior"] == "allow":
            Path(answer["updatedInput"]["file_path"]).write_text("hi", encoding="utf-8")
        else:
            denials = [{"tool_name": "Write", "tool_input": {"file_path": target}}]
        emit(result(SECRET_TEXT, permission_denials=denials))
    elif "Count from" in text:
        for _ in range(10):
            emit(text_delta())
        for line in lines:
            message = json.loads(line)
            if message.get("request", {}).get("subtype") == "interrupt":
                emit({"type": "control_response", "response": {"subtype": "success", "request_id": "interrupt"}})
                emit(result("", is_error=True, subtype="error_during_execution",
                            terminal_reason="aborted_streaming", modelUsage={}))
                return
    elif "Which codeword" in text:
        emit(text_delta())
        emit(result(f"TANGERINE {SECRET_TEXT}"))
    else:
        emit(text_delta())
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": m("allowed"), "rateLimitType": m("five_hour"), "overageStatus": m("rejected"),
            "note": SECRET_TEXT,
            "unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": 1790520600},
                               m("seven_day"): {"utilization": 0.2, "resetsAt": 1791064800}}}})
        emit({"type": "system", "subtype": "model_refusal_fallback",
              "original_model": m("claude-fable-5"), "fallback_model": m("claude-opus-4-8"),
              "trigger": m("refusal"), "api_refusal_explanation": SECRET_TEXT})
        emit(result(SECRET_TEXT))


def main() -> int:
    args = sys.argv[1:]
    log({"argv": args, "env_names": sorted(os.environ),
         "env_values_with_planted": sorted(n for n, v in os.environ.items() if "PLANTED" in v)})
    if args == ["--version"]:
        print(f"2.1.283 (Claude Code) {m('build', gating=True)}")
        return 0
    if args[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": m("claude.ai", gating=True),
                          "apiProvider": m("firstParty", gating=True),
                          "email": "someone@example.com", "orgId": MARKER, "orgName": SECRET_TEXT,
                          "configDirectory": f"/home/{MARKER}/.claude"}))
        return 0
    if "stream-json" not in args:
        sys.stdin.read()
        print(json.dumps(result(SECRET_TEXT, is_error=True, api_error_status=404,
                                terminal_reason="api_error", modelUsage={})))
        return 1

    lines = iter(sys.stdin)
    for line in lines:
        message = json.loads(line)
        if message.get("type") == "control_request":
            subtype = message["request"].get("subtype")
            body = {}
            if subtype == "initialize":
                body = {"account": {"email": "someone@example.com", "organization": SECRET_TEXT,
                                    "subscriptionType": m(os.environ.get("FAKE_PLAN", "Claude Max"), gating=True),
                                    "apiProvider": m("firstParty", gating=True)},
                        "models": [{"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001",
                                    "displayName": "Haiku", "description": SECRET_TEXT,
                                    "supportedEffortLevels": ["low", SECRET_TEXT]},
                                   {"value": SECRET_TEXT, "resolvedModel": "x", "displayName": SECRET_TEXT},
                                   {"value": m("opus"), "resolvedModel": f"claude-opus-{m('5-5')}",
                                    "displayName": m("Opus"), "supportedEffortLevels": [m("max")]}],
                        "current_permission_mode": m("default"), "commands": []}
            emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": message["request_id"], "response": body}})
        elif message.get("type") == "user":
            log({"user_turn": True})
            handle_turn(message["message"]["content"], lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())

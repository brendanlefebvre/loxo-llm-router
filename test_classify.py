"""Tests for the A1 request classifier. Pure functions; no network.

Rule expectations are grounded in the 2026-07-19 OpenCode capture session:
title chores are tiny tool-less bodies with a distinctive system prompt;
main turns carry the OpenCode agent system prompt (two variants — OpenCode
swaps prompts by target model family) plus a tool inventory.
"""

from loxo_llm_router.classify import CLASSIFIER_VERSION, Classification, classify


def _body(system=None, tools=0, messages=1, stream=True):
    msgs = []
    if system is not None:
        msgs.append({"role": "system", "content": system})
    msgs += [{"role": "user", "content": f"turn {i}"} for i in range(messages)]
    b = {"model": "loxo/auto", "messages": msgs, "stream": stream}
    if tools:
        b["tools"] = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(tools)]
    return b


def test_title_chore_is_chore():
    c = classify(_body(system="You are a title generator. You output ONLY a thread title.", tools=0))
    assert c.cls == "chore"
    assert c.version == CLASSIFIER_VERSION


def test_title_fingerprint_with_tools_is_not_chore():
    # A tool inventory contradicts the chore shape; fall through (-> unknown).
    c = classify(_body(system="You are a title generator. Etc.", tools=11))
    assert c.cls == "unknown"


def test_main_opencode_generic_variant():
    c = classify(_body(system="You are opencode, an interactive CLI tool that helps users.", tools=11))
    assert c.cls == "main"


def test_main_opencode_anthropic_variant():
    c = classify(_body(
        system="You are OpenCode, the best coding agent on the planet.  You are an int...",
        tools=11))
    assert c.cls == "main"


def test_main_fingerprint_without_tools_is_unknown():
    # Main turns always carry the tool inventory; without it, don't guess main.
    c = classify(_body(system="You are opencode, an interactive CLI tool.", tools=0))
    assert c.cls == "unknown"


def test_system_prompt_as_content_parts_is_supported():
    body = {"model": "loxo/auto", "stream": True, "tools": [{"x": 1}], "messages": [
        {"role": "system", "content": [
            {"type": "text", "text": "You are opencode, an interactive CLI tool."}]},
        {"role": "user", "content": "hi"},
    ]}
    assert classify(body).cls == "main"


def test_no_system_prompt_is_unknown():
    assert classify(_body(system=None, tools=11)).cls == "unknown"


def test_garbage_body_is_unknown_not_error():
    assert classify({}).cls == "unknown"
    assert classify({"messages": "not-a-list"}).cls == "unknown"


def test_signals_recorded():
    c = classify(_body(system="You are a title generator.", tools=0, messages=2, stream=True))
    assert c.signals["messages"] == 3          # system + 2 user
    assert c.signals["tools"] == 0
    assert c.signals["stream"] is True


def test_classification_is_frozen():
    c = classify(_body(system=None))
    try:
        c.cls = "main"
        raised = False
    except Exception:
        raised = True
    assert raised


# --- v2: compaction + Pi fingerprints + developer role (captured 2026-07-20) --

def test_opencode_compaction_classified():
    c = classify(_body(
        system="You are an anchored context summarization assistant for coding sessions.",
        tools=0, messages=11))
    assert c.cls == "compaction"
    assert c.version == 2


def test_pi_compaction_classified():
    c = classify(_body(
        system="You are a context summarization assistant. Your task is to read a conversation.",
        tools=0, messages=2))
    assert c.cls == "compaction"


def test_pi_main_classified():
    c = classify(_body(
        system="You are an expert coding assistant operating inside pi, a coding agent harness.",
        tools=4))
    assert c.cls == "main"


def test_developer_role_system_prompt_is_read():
    # Pi sends the system prompt under role "developer" on reasoning-model
    # turns (captured 2026-07-20); the fingerprint must still be found.
    body = {"model": "loxo/reason", "stream": True,
            "tools": [{"x": 1}] * 4,
            "messages": [
                {"role": "developer",
                 "content": "You are an expert coding assistant operating inside pi, etc."},
                {"role": "user", "content": "hi"},
            ]}
    assert classify(body).cls == "main"


def test_compaction_rule_order():
    # Rule order: chore -> compaction -> main. Order is part of the contract.
    c = classify(_body(system="You are an anchored context summarization assistant.", tools=0))
    assert c.cls == "compaction"


def test_version_bumped_everywhere():
    assert CLASSIFIER_VERSION == 2
    assert classify(_body(system=None)).version == 2

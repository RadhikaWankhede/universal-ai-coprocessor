"""The planning guide: advice to the model, kept honest.

The guide grants nothing - the broker still decides - but advice that names a
tool the broker does not know, or a pattern the plugin refuses, would make
plans fail in ways that look like model errors. These tests stop that drift.
"""
from __future__ import annotations

import re

from uaic.broker import default_registry
from uaic.core import ContextAssembler, GeminiPlanner
from uaic.core.context import PLANNING_GUIDE, SYSTEM_RULES, system_block
from uaic.plugins.fs import check_pattern


def test_the_guide_follows_the_trust_rules_in_the_system_block():
    text = system_block().content
    assert text.startswith(SYSTEM_RULES)               # trust rules come first, unchanged
    assert PLANNING_GUIDE in text


def test_every_tool_the_guide_names_is_declared():
    named = set(re.findall(r"\b([a-z]+\.[a-z_]+)\b", PLANNING_GUIDE))
    named = {n for n in named if n.split(".")[0] in default_registry().plugins()}
    assert "fs.move_matching" in named
    assert named <= set(default_registry().names())


def test_every_pattern_the_guide_gives_is_one_the_plugin_accepts():
    patterns = re.findall(r"\*\.[a-z0-9]+", PLANNING_GUIDE)
    assert {"*.docx", "*.doc", "*.jpg", "*.pdf"} <= set(patterns)
    for pattern in patterns:
        assert check_pattern(pattern) == pattern


def test_the_guide_reaches_the_model_as_its_system_instruction():
    assembler = ContextAssembler()
    assembler.add_user("move all the word files")
    payload = GeminiPlanner(api_key_value="k")._payload(assembler)
    system = payload["systemInstruction"]["parts"][0]["text"]
    assert "Word documents = *.docx and *.doc" in system
    assert all(c["role"] == "user" for c in payload["contents"])

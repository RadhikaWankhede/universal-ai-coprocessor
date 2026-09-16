"""Manifests must fail loudly at startup, never silently at decision time."""
from __future__ import annotations

from pathlib import Path

import pytest

from uaic.broker import ManifestError, default_registry, load_registry
from uaic.contracts import Rev, Risk


def write(tmp_path: Path, name: str, body: str) -> Path:
    (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


# ---------- the real manifests ----------

def test_default_registry_loads():
    reg = default_registry()
    assert len(reg) >= 10
    assert set(reg.plugins()) >= {"fs", "git", "mail"}


def test_every_scope_arg_is_path_typed():
    reg = default_registry()
    for name in reg.names():
        spec = reg.lookup(name)
        for a in spec.scope_args:
            assert spec.args[a].type == "path", f"{name}.{a} must be path-typed"


def test_irreversible_tools_are_declared_as_such():
    reg = default_registry()
    for name in ("mail.send", "git.push_force", "fs.overwrite"):
        assert reg.lookup(name).rev is Rev.IRREVERSIBLE


def test_delete_is_reversible_because_it_is_trash_backed():
    spec = default_registry().lookup("fs.delete")
    assert spec.rev is Rev.REVERSIBLE
    assert spec.inverse == "fs.restore"


def test_force_push_is_critical():
    assert default_registry().lookup("git.push_force").risk is Risk.CRITICAL


# ---------- malformed manifests ----------

def test_tool_name_must_be_dotted(tmp_path):
    write(tmp_path, "x.yaml", """
plugin: x
tools:
  - name: listfiles
    args: {path: {type: path}}
    risk: low
    rev: reversible
""")
    with pytest.raises(ManifestError, match=r"plugin\.action"):
        load_registry(tmp_path)


def test_tool_must_belong_to_its_plugin(tmp_path):
    write(tmp_path, "x.yaml", """
plugin: x
tools:
  - name: y.list
    args: {path: {type: path}}
    risk: low
    rev: reversible
""")
    with pytest.raises(ManifestError, match="does not belong"):
        load_registry(tmp_path)


def test_scope_arg_must_exist_in_args(tmp_path):
    write(tmp_path, "x.yaml", """
plugin: x
tools:
  - name: x.list
    args: {path: {type: path}}
    scope_args: [nowhere]
    risk: low
    rev: reversible
""")
    with pytest.raises(ManifestError, match="scope_args not in args"):
        load_registry(tmp_path)


def test_scope_arg_must_be_path_typed(tmp_path):
    write(tmp_path, "x.yaml", """
plugin: x
tools:
  - name: x.send
    args: {to: {type: email}}
    scope_args: [to]
    risk: low
    rev: reversible
""")
    with pytest.raises(ManifestError, match="must be path-typed"):
        load_registry(tmp_path)


def test_unregistered_escalation_predicate_is_rejected(tmp_path):
    """A manifest cannot name a predicate that does not exist in code."""
    write(tmp_path, "x.yaml", """
plugin: x
tools:
  - name: x.list
    args: {path: {type: path}}
    scope_args: [path]
    risk: low
    rev: reversible
    escalate: [if_the_moon_is_full]
""")
    with pytest.raises(ManifestError, match="unregistered escalation"):
        load_registry(tmp_path)


def test_duplicate_tool_names_are_rejected(tmp_path):
    body = """
plugin: x
tools:
  - name: x.list
    args: {path: {type: path}}
    risk: low
    rev: reversible
"""
    write(tmp_path, "a.yaml", body)
    write(tmp_path, "b.yaml", body)
    with pytest.raises(ManifestError, match="declared twice"):
        load_registry(tmp_path)


def test_bad_yaml_is_reported(tmp_path):
    write(tmp_path, "x.yaml", "plugin: x\ntools: [ unclosed")
    with pytest.raises(ManifestError):
        load_registry(tmp_path)


def test_unknown_risk_value_is_rejected(tmp_path):
    write(tmp_path, "x.yaml", """
plugin: x
tools:
  - name: x.list
    args: {path: {type: path}}
    risk: apocalyptic
    rev: reversible
""")
    with pytest.raises(ManifestError):
        load_registry(tmp_path)


def test_empty_directory_is_rejected(tmp_path):
    with pytest.raises(ManifestError, match="no tools declared"):
        load_registry(tmp_path)


def test_missing_directory_is_rejected(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        load_registry(tmp_path / "nope")

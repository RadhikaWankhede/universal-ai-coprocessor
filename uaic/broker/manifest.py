"""Plugin manifests: the single source of truth for what a tool is allowed to do.

Loaded and validated at startup. A manifest that names an unregistered
escalation predicate, or declares a tool whose name does not match its plugin,
fails here rather than silently at decision time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from uaic.broker.risk import known_predicates
from uaic.contracts import Rev, Risk

ArgType = Literal["path", "str", "bool", "int", "email"]

MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


class ManifestError(Exception):
    """A manifest is malformed. Raised at startup, never at decision time."""


class ArgSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: ArgType
    required: bool = True
    default: Any | None = None


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    summary: str = ""
    args: dict[str, ArgSpec] = {}
    scope_args: tuple[str, ...] = ()
    risk: Risk
    rev: Rev
    escalate: tuple[str, ...] = ()
    inverse: str | None = None

    @field_validator("name")
    @classmethod
    def _dotted(cls, v: str) -> str:
        if v.count(".") != 1 or not all(v.split(".")):
            raise ValueError(f"tool name must be 'plugin.action', got {v!r}")
        return v

    @property
    def plugin(self) -> str:
        return self.name.split(".", 1)[0]

    def required_args(self) -> tuple[str, ...]:
        return tuple(n for n, a in self.args.items() if a.required)


class PluginManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    plugin: str
    version: int = 1
    tools: tuple[ToolSpec, ...]


class Registry:
    """All declared tools, keyed by name."""

    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self._specs = dict(specs)

    def lookup(self, tool: str) -> ToolSpec | None:
        return self._specs.get(tool)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def plugins(self) -> tuple[str, ...]:
        return tuple(sorted({s.plugin for s in self._specs.values()}))

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, tool: object) -> bool:
        return tool in self._specs


def load_manifest(path: Path) -> PluginManifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path.name}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{path.name}: expected a mapping at the top level")
    try:
        manifest = PluginManifest.model_validate(raw)
    except Exception as exc:
        raise ManifestError(f"{path.name}: {exc}") from exc

    for spec in manifest.tools:
        if spec.plugin != manifest.plugin:
            raise ManifestError(
                f"{path.name}: tool {spec.name!r} does not belong to plugin "
                f"{manifest.plugin!r}")
        unknown = set(spec.scope_args) - set(spec.args)
        if unknown:
            raise ManifestError(
                f"{path.name}: {spec.name} lists scope_args not in args: {sorted(unknown)}")
        bad_types = [a for a in spec.scope_args if spec.args[a].type != "path"]
        if bad_types:
            raise ManifestError(
                f"{path.name}: {spec.name} scope_args must be path-typed: {bad_types}")
        missing_preds = set(spec.escalate) - known_predicates()
        if missing_preds:
            raise ManifestError(
                f"{path.name}: {spec.name} names unregistered escalation "
                f"predicate(s): {sorted(missing_preds)}")
    return manifest


def load_registry(directory: Path | None = None) -> Registry:
    directory = directory or MANIFEST_DIR
    if not directory.is_dir():
        raise ManifestError(f"manifest directory not found: {directory}")

    specs: dict[str, ToolSpec] = {}
    for path in sorted(directory.glob("*.yaml")):
        for spec in load_manifest(path).tools:
            if spec.name in specs:
                raise ManifestError(f"tool {spec.name!r} declared twice")
            specs[spec.name] = spec
    if not specs:
        raise ManifestError(f"no tools declared in {directory}")
    return Registry(specs)


_DEFAULT: Registry | None = None


def default_registry() -> Registry:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_registry()
    return _DEFAULT

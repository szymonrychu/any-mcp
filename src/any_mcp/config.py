"""The any-mcp YAML (or JSON) config file: models, loaders and ``--plugin`` shortcut."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from any_mcp.errors import ConfigError
from any_mcp.sources import parse_source

_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,19}$"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HttpSettings(_Model):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=0, le=65535)
    path: str = "/mcp"
    bearer_token: SecretStr | None = None
    allowed_origins: list[str] = Field(default_factory=list)
    insecure: bool = False

    @field_validator("bearer_token")
    @classmethod
    def _token_not_empty(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("bearer_token must not be empty (unset it to disable auth)")
        return value


class ServerSettings(_Model):
    name: str = "any-mcp"
    instructions: str | None = None
    transport: Literal["stdio", "http"] = "stdio"
    log_level: str = "info"
    http: HttpSettings = Field(default_factory=HttpSettings)


class ToolSettings(_Model):
    max_name_length: int = Field(default=60, ge=16, le=128)
    list_ttl_ms: int = Field(default=30000, ge=0)
    cache_scope: Literal["public", "private"] = "public"
    response_budget_chars: int = Field(default=40000, ge=1)


class PluginEntry(_Model):
    """One plugin: a ``plugin`` reference, a ``source`` package, or a source and its entry point."""

    name: str = Field(pattern=_NAME_PATTERN)
    source: str | None = None
    plugin: str | None = None
    namespace: str | None = None
    allow: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    timeout: float | None = Field(default=None, gt=0)
    reload_every: float | None = Field(default=None, gt=0)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _parse_source(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parse_source(value)
            except ConfigError as exc:
                raise ValueError(str(exc)) from None
        return value

    @field_validator("plugin")
    @classmethod
    def _entry_point_with_source(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and ":" in value and info.data.get("source") is not None:
            raise ValueError(
                "with a source, plugin names one of the source package's entry points, "
                "not module:Class"
            )
        return value

    @model_validator(mode="after")
    def _plugin_or_source(self) -> PluginEntry:
        if self.plugin is None and self.source is None:
            raise ValueError("an entry needs a plugin, a source or both")
        return self


class AnyMcpConfig(_Model):
    server: ServerSettings = Field(default_factory=ServerSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    python_path: list[str] = Field(default_factory=list)
    plugins: list[PluginEntry] = Field(default_factory=list)
    base_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _unique_plugin_names(self) -> AnyMcpConfig:
        seen: set[str] = set()
        for entry in self.plugins:
            if entry.name in seen:
                raise ValueError(f"duplicate plugin name '{entry.name}'")
            seen.add(entry.name)
        return self


def _dotted(loc: Iterable[Any]) -> str:
    return ".".join(str(part) for part in loc) or "<root>"


def _expand(value: Any, env: Mapping[str, str], path: tuple[Any, ...], source: str) -> Any:
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            if var in env:
                return env[var]
            if default is not None:
                return default
            raise ConfigError(
                f"{source}: {_dotted(path)}: environment variable {var} is not set "
                f"and has no default"
            )

        return _VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v, env, (*path, k), source) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env, (*path, i), source) for i, v in enumerate(value)]
    return value


def _validate(data: dict[str, Any], source: str) -> AnyMcpConfig:
    try:
        return AnyMcpConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{_dotted(e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"{source}: {problems}") from None


def _read(path: str | os.PathLike[str]) -> tuple[dict[str, Any], str, Path]:
    """Parse a config file into its top-level mapping, its display name and its directory."""
    file = Path(path)
    source = str(file)
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{source}: cannot read config file: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"{source}: invalid {'JSON' if file.suffix == '.json' else 'YAML'}: {exc}"
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping, got {type(data).__name__}")
    return data, source, file.resolve().parent


def load_config(
    path: str | os.PathLike[str], *, env: Mapping[str, str] | None = None
) -> AnyMcpConfig:
    """Read a YAML or JSON config file, expand ``${VAR}``/``${VAR:-d}`` and validate it."""
    data, source, base_dir = _read(path)
    data = _expand(data, os.environ if env is None else env, (), source)
    config = _validate(data, source)
    config.base_dir = base_dir
    config.python_path = [
        str((base_dir / entry).resolve()) if not Path(entry).is_absolute() else entry
        for entry in config.python_path
    ]
    return config


def load_sources(
    path: str | os.PathLike[str], *, env: Mapping[str, str] | None = None
) -> tuple[list[str], Path]:
    """The ``plugins[].source`` strings of a config file and the directory they resolve against.

    ``${VAR}`` expands inside the source strings alone and nothing else is validated, so a
    variable that only a plugin's runtime ``config`` needs may be unset here.
    """
    data, source, base_dir = _read(path)
    plugins = data.get("plugins") or []
    if not isinstance(plugins, list):
        raise ConfigError(f"{source}: plugins: must be a list, got {type(plugins).__name__}")
    found: list[str] = []
    for index, entry in enumerate(plugins):
        raw = entry.get("source") if isinstance(entry, dict) else None
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ConfigError(f"{source}: plugins.{index}.source: must be a string")
        where = ("plugins", index, "source")
        found.append(_expand(raw, os.environ if env is None else env, where, source))
    return found, base_dir


def config_from_plugins(refs: Iterable[str]) -> AnyMcpConfig:
    """Build a default config with one entry per ``--plugin`` reference."""
    plugins = [
        {"name": ref.rsplit(":", 1)[1].lower() if ":" in ref else ref, "plugin": ref}
        for ref in refs
    ]
    config = _validate({"plugins": plugins}, "--plugin")
    config.base_dir = Path.cwd()
    return config

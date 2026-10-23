import json
from pathlib import Path

import pytest

from any_mcp.config import AnyMcpConfig, config_from_plugins, load_config
from any_mcp.errors import ConfigError


def write(tmp_path: Path, text: str, name: str = "any-mcp.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_defaults(tmp_path):
    cfg = load_config(write(tmp_path, "plugins:\n  - name: a\n    plugin: m:C\n"), env={})
    assert cfg.server.name == "any-mcp"
    assert cfg.server.instructions is None
    assert cfg.server.transport == "stdio"
    assert cfg.server.log_level == "info"
    http = cfg.server.http
    assert (http.host, http.port, http.path) == ("127.0.0.1", 8000, "/mcp")
    assert http.bearer_token is None
    assert http.allowed_origins == []
    assert cfg.tools.max_name_length == 60
    assert cfg.tools.list_ttl_ms == 30000
    assert cfg.tools.cache_scope == "public"
    assert cfg.tools.response_budget_chars == 40000
    assert cfg.python_path == []
    p = cfg.plugins[0]
    assert (p.namespace, p.allow, p.tags, p.timeout, p.reload_every, p.config) == (
        None,
        [],
        [],
        None,
        None,
        {},
    )


def test_env_expansion(tmp_path):
    text = (
        "server:\n  name: ${NAME}-x\n"
        "plugins:\n  - name: a\n    plugin: m:C\n"
        "    config:\n      deep:\n        - url: https://${HOST:-localhost}/${P:-}v1\n"
    )
    cfg = load_config(write(tmp_path, text), env={"NAME": "svc"})
    assert cfg.server.name == "svc-x"
    assert cfg.plugins[0].config["deep"][0]["url"] == "https://localhost/v1"
    cfg = load_config(write(tmp_path, text), env={"NAME": "s", "HOST": "h", "P": "z"})
    assert cfg.plugins[0].config["deep"][0]["url"] == "https://h/zv1"
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text), env={})
    assert "NAME" in str(exc.value)
    assert "server.name" in str(exc.value)


def test_env_default_is_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("ANY_MCP_T", "yo")
    cfg = load_config(write(tmp_path, "server:\n  name: ${ANY_MCP_T}\n"))
    assert cfg.server.name == "yo"


def test_unknown_keys_rejected(tmp_path):
    cases = {
        "bogus: 1\n": "bogus",
        "server:\n  bogus: 1\n": "server.bogus",
        "server:\n  http:\n    bogus: 1\n": "server.http.bogus",
        "tools:\n  bogus: 1\n": "tools.bogus",
        "plugins:\n  - name: a\n    plugin: m:C\n    bogus: 1\n": "plugins.0.bogus",
    }
    for text, key in cases.items():
        with pytest.raises(ConfigError) as exc:
            load_config(write(tmp_path, text), env={})
        assert key in str(exc.value)
        assert "any-mcp.yaml" in str(exc.value)


def test_validation_errors_name_key_and_hide_secret(tmp_path):
    text = "server:\n  http:\n    bearer_token: s3cr3t\n    port: nope\n"
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text), env={})
    assert "server.http.port" in str(exc.value)
    assert "s3cr3t" not in str(exc.value)
    with pytest.raises(ConfigError, match=r"tools\.max_name_length"):
        load_config(write(tmp_path, "tools:\n  max_name_length: 8\n"), env={})
    with pytest.raises(ConfigError, match=r"tools\.max_name_length"):
        load_config(write(tmp_path, "tools:\n  max_name_length: 129\n"), env={})


def test_plugin_entry_rules(tmp_path):
    for bad in ("A", "1a", "a.b", "x" * 21, ""):
        text = f"plugins:\n  - name: '{bad}'\n    plugin: m:C\n"
        with pytest.raises(ConfigError, match=r"plugins\.0\.name"):
            load_config(write(tmp_path, text), env={})
    ok = "plugins:\n  - name: a_b-1\n    plugin: m:C\n  - name: " + "x" * 20 + "\n    plugin: m:D\n"
    assert len(load_config(write(tmp_path, ok), env={}).plugins) == 2
    dup = "plugins:\n  - name: a\n    plugin: m:C\n  - name: a\n    plugin: m:D\n"
    with pytest.raises(ConfigError, match=r"duplicate.*'a'"):
        load_config(write(tmp_path, dup), env={})


def test_python_path_relative_to_config(tmp_path):
    sub = tmp_path / "conf"
    sub.mkdir()
    path = write(sub, "python_path:\n  - lib\n  - /abs/dir\n  - ../up\n")
    cfg = load_config(path, env={})
    assert cfg.base_dir == sub.resolve()
    assert cfg.python_path == [
        str(sub.resolve() / "lib"),
        "/abs/dir",
        str((sub / ".." / "up").resolve()),
    ]


def test_config_from_plugins():
    cfg = config_from_plugins(["my.mod:MyPlugin", "petstore"])
    assert isinstance(cfg, AnyMcpConfig)
    assert [(e.name, e.plugin) for e in cfg.plugins] == [
        ("myplugin", "my.mod:MyPlugin"),
        ("petstore", "petstore"),
    ]
    assert cfg.server.transport == "stdio"
    assert config_from_plugins([]).plugins == []
    with pytest.raises(ConfigError, match="plugin"):
        config_from_plugins(["a:B", "c:b"])


def test_invalid_files(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError, match="nope.yaml"):
        load_config(missing, env={})
    with pytest.raises(ConfigError, match="bad.yaml"):
        load_config(write(tmp_path, "a: [unclosed\n", "bad.yaml"), env={})
    with pytest.raises(ConfigError, match="list.yaml"):
        load_config(write(tmp_path, "- 1\n- 2\n", "list.yaml"), env={})
    with pytest.raises(ConfigError, match="bad.json"):
        load_config(write(tmp_path, "{not json", "bad.json"), env={})
    with pytest.raises(ConfigError, match="dir"):
        d = tmp_path / "dir"
        d.mkdir()
        load_config(d, env={})


def test_json_config_loads(tmp_path):
    data = {
        "server": {"name": "j", "transport": "http", "http": {"port": 9000}},
        "tools": {"cache_scope": "private"},
        "python_path": ["lib"],
        "plugins": [{"name": "a", "plugin": "m:C", "config": {"k": "${V:-d}"}}],
    }
    import yaml

    y = write(tmp_path, yaml.safe_dump(data), "any-mcp.yaml")
    j = write(tmp_path, json.dumps(data), "any-mcp.json")
    j2 = write(tmp_path, json.dumps(data), "other.yaml")
    base = load_config(y, env={})
    assert load_config(j, env={}) == base
    assert load_config(j2, env={}) == base
    assert base.plugins[0].config == {"k": "d"}
    assert base.server.http.port == 9000

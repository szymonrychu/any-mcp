import pytest

from any_mcp.errors import ConfigError
from any_mcp.sources import Source, parse_source

ACCEPTED_FORMS = ("./", "../", "/", "git::https://", "git::ssh://", "git::file://", "github.com/")


def refused(text: str) -> str:
    with pytest.raises(ConfigError) as info:
        parse_source(text)
    message = str(info.value)
    assert message.startswith(f"plugin source '{text}': ")
    assert "\n" not in message
    return message


@pytest.mark.parametrize("text", ["./x", "../x", "/abs/x", "./a/b-c_d.e", "../../up/x", "/"])
def test_local_sources(text):
    assert parse_source(text) == Source(
        raw=text, kind="local", path=text, url=None, subdir="", ref=None
    )


@pytest.mark.parametrize("text", ["./x//sub", "../x?ref=v1", "/abs//x", "//abs/x", "./x?"])
def test_local_sources_refuse_double_slash_and_query(text):
    assert "local" in refused(text)


@pytest.mark.parametrize(
    ("text", "url", "subdir", "ref"),
    [
        ("git::https://github.com/o/r.git", "https://github.com/o/r.git", "", None),
        ("git::https://example.com/o/r//sub/dir", "https://example.com/o/r", "sub/dir", None),
        ("git::https://example.com/o/r?ref=v1.2.3", "https://example.com/o/r", "", "v1.2.3"),
        (
            "git::https://example.com/o/r.git//examples/petstore?ref=release/v0.5.0",
            "https://example.com/o/r.git",
            "examples/petstore",
            "release/v0.5.0",
        ),
        ("git::ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git", "", None),
        (
            "git::ssh://git@github.com:22/o/r.git//sub?ref=main",
            "ssh://git@github.com:22/o/r.git",
            "sub",
            "main",
        ),
        ("git::file:///tmp/repo", "file:///tmp/repo", "", None),
        ("git::file:///tmp/repo//plugins/a?ref=abc123", "file:///tmp/repo", "plugins/a", "abc123"),
    ],
)
def test_git_sources(text, url, subdir, ref):
    assert parse_source(text) == Source(
        raw=text, kind="git", path=None, url=url, subdir=subdir, ref=ref
    )


@pytest.mark.parametrize(
    ("text", "url", "subdir", "ref"),
    [
        ("github.com/owner/repo", "https://github.com/owner/repo.git", "", None),
        ("github.com/owner/repo.git", "https://github.com/owner/repo.git", "", None),
        ("github.com/owner/repo//sub/dir", "https://github.com/owner/repo.git", "sub/dir", None),
        ("github.com/owner/repo?ref=v0.1.0", "https://github.com/owner/repo.git", "", "v0.1.0"),
        (
            "github.com/owner/repo.git//examples/x?ref=main",
            "https://github.com/owner/repo.git",
            "examples/x",
            "main",
        ),
    ],
)
def test_github_shorthand(text, url, subdir, ref):
    assert parse_source(text) == Source(
        raw=text, kind="git", path=None, url=url, subdir=subdir, ref=ref
    )


@pytest.mark.parametrize(
    "text", ["github.com/owner", "github.com/owner/", "github.com//repo", "github.com/o/r/extra"]
)
def test_github_shorthand_needs_owner_and_repo(text):
    assert "github.com/<owner>/<repo>" in refused(text)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("git::https://example.com/o/r?depth=1", "depth"),
        ("git::https://example.com/o/r?ref=v1&depth=1", "depth"),
        ("git::ssh://git@example.com/o/r?sshkey=abc", "sshkey"),
        ("github.com/o/r?archive=zip", "archive"),
        ("git::file:///tmp/repo//sub?foo=bar&ref=v1", "foo"),
    ],
)
def test_unsupported_query_refused(text, key):
    message = refused(text)
    assert f"'{key}'" in message
    assert "only 'ref'" in message


def test_duplicate_ref_refused():
    assert "more than once" in refused("git::https://example.com/o/r?ref=a&ref=b")


@pytest.mark.parametrize(
    "ref",
    ["", "-x", "--upload-pack=evil", "v1 2", "v1;rm", "v1%20", "a~1", "a^", "a:b", "a..b", ".."],
)
def test_bad_refs_refused(ref):
    for prefix in ("git::https://example.com/o/r?ref=", "github.com/o/r//sub?ref="):
        assert "ref" in refused(prefix + ref)


def test_bare_ref_key_is_an_empty_ref():
    assert "empty ref" in refused("git::https://example.com/o/r?ref")


@pytest.mark.parametrize("subdir", ["/abs", "a//b", "a/", "", "..", "a/../b", "a/.."])
def test_bad_subdirs_refused(subdir):
    for prefix in ("git::https://example.com/o/r//", "github.com/o/r//"):
        assert "subdir" in refused(prefix + subdir + "?ref=v1")
        assert "subdir" in refused(prefix + subdir)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "foo/bar",
        "owner/repo",
        "x",
        ".",
        "..",
        "https://github.com/o/r",
        "git+https://github.com/o/r.git",
        "git+https://github.com/o/r.git@v1#subdirectory=x",
        "git@github.com:o/r.git",
        "git::git@github.com:o/r.git",
        "git::http://example.com/o/r",
        "git::ftp://example.com/o/r",
        "git::example.com/o/r",
        "git::",
        "gitlab.com/o/r",
        "s3::https://bucket/x",
    ],
)
def test_unrecognised_forms_refused(text):
    message = refused(text)
    for form in ACCEPTED_FORMS:
        assert form in message


@pytest.mark.parametrize("text", ["git@github.com:o/r.git", "git::git@github.com:o/r.git"])
def test_scp_like_hints_ssh(text):
    assert "use git::ssh://" in refused(text)


@pytest.mark.parametrize("text", ["git::https://", "git::https:////sub", "git::file://?ref=v1"])
def test_git_url_must_not_be_empty(text):
    assert "URL" in refused(text)


@pytest.mark.parametrize(
    "text",
    [
        "git::https://github.com",
        "git::https://github.com/",
        "git::https://host//sub",
        "git::https:///x",
        "git::ssh://host",
        "git::ssh:///x",
    ],
)
def test_git_location_needs_host_and_path(text):
    message = refused(text)
    assert "host" in message
    assert "path" in message


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("git::https://example.com/o/r\n", "a remote source may not contain whitespace"),
        ("git::https://exam ple.com/o/r", "a remote source may not contain whitespace"),
        ("git::https://h/o/r x", "a remote source may not contain whitespace"),
        ("git::https://example.com/o/r\t//sub", "a remote source may not contain whitespace"),
        ("git::https://example.com/o/r//su b", "a remote source may not contain whitespace"),
        ("git::https://example.com/o/r?ref=v1\nx", "a remote source may not contain whitespace"),
        ("github.com/o/r\n", "a remote source may not contain whitespace"),
        ("github.com/o/ r", "a remote source may not contain whitespace"),
        ("git::file:///tmp/a\nb", "a remote source may not contain whitespace"),
        ("./a\nb", "a source may not contain control characters"),
        ("/tmp/a\tb", "a source may not contain control characters"),
        ("../a\x7fb", "a source may not contain control characters"),
    ],
)
def test_whitespace_and_control_refused(text, problem):
    with pytest.raises(ConfigError) as info:
        parse_source(text)
    assert str(info.value).startswith(f"plugin source '{text}': {problem}")


@pytest.mark.parametrize("text", ["./my plugins/x", "/Users/me/My Plugins/x", "../a b"])
def test_local_path_with_space_accepted(text):
    assert parse_source(text).path == text

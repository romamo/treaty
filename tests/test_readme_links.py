"""README.md is the PyPI long description: a relative link there resolves against pypi.org."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/romamo/treaty/blob/main/"

FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"(`+).+?\1", re.DOTALL)
TARGETS = (
    re.compile(r"\]\(\s*<?([^)\s>]+)"),  # [text](target) and ![alt](target)
    re.compile(r"^\s*\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.MULTILINE),  # [ref]: target
    re.compile(r"""\b(?:href|src)\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE),  # HTML
)
ABSOLUTE = re.compile(r"^(?:https?:|mailto:)", re.IGNORECASE)


def link_targets(markdown: str) -> list[str]:
    prose = CODE_SPAN.sub("", FENCE.sub("", markdown))
    return [m.group(1) for pattern in TARGETS for m in pattern.finditer(prose)]


def relative_links(markdown: str) -> list[str]:
    """Targets that are neither absolute URLs nor in-page anchors."""
    return [t for t in link_targets(markdown) if not ABSOLUTE.match(t) and not t.startswith("#")]


def test_finds_every_kind_of_relative_link() -> None:
    sample = """
[a](docs/a.md) ![b](img/b.png) [c](<docs/c d.md>) [ok](https://example.com) [top](#top)
[ref]: ../e.md
<a href="f.html">f</a> <img src='g.svg'>
`[code](skipped.md)`

```
[fenced](skipped.md)
```
"""
    assert relative_links(sample) == [
        "docs/a.md",
        "img/b.png",
        "docs/c",
        "../e.md",
        "f.html",
        "g.svg",
    ]


def test_readme_has_no_relative_links() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert link_targets(readme), "found no links: the patterns no longer match the README"
    assert relative_links(readme) == [], f"use absolute {REPO}<path> links"


def test_readme_repo_links_point_at_files_that_exist() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    paths = [
        t.removeprefix(REPO).partition("#")[0] for t in link_targets(readme) if t.startswith(REPO)
    ]
    assert paths
    assert [p for p in paths if not (ROOT / p).is_file()] == []

import pytest

from treaty import ParseError
from treaty._dispatch import parse_dispatch_line


def test_dispatch_line_splits_path_opts_and_payload() -> None:
    req = parse_dispatch_line(
        '{"_cmd": "deploy.rollback", "_opts": {"dry-run": true, "to": "1.3.9", "replicas": 2},'
        ' "service": "api"}',
        1,
    )
    assert req.path.parts == ("deploy", "rollback")
    assert req.opts == {"dry-run": True, "to": "1.3.9", "replicas": 2}
    assert req.payload == {"service": "api"}


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"_opts": {}}',
        '{"_cmd": "Bad.Path"}',
        '{"_cmd": "a", "_opts": {"x": [1]}}',
    ],
)
def test_dispatch_rejects_malformed(line: str) -> None:
    with pytest.raises(ParseError):
        parse_dispatch_line(line, 3)

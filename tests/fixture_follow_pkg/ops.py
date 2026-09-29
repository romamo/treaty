import os


class Store:
    @staticmethod
    def load() -> str:
        return os.environ.get("OTHER_TOOL_TOKEN", "")


def read_token() -> str:
    return Store.load()


def pull() -> bytes:
    from .net import fetch

    return fetch("https://example.com")


def beyond() -> None:
    from ...nowhere import thing  # type: ignore[import-not-found]

    thing()

import urllib.request


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        return bytes(response.read())

"""Read local configuration with process environment taking priority."""

import os
from pathlib import Path


class ModelError(RuntimeError):
    """The model service could not complete a review."""


def environment() -> dict[str, str]:
    path = Path(".env")
    entries = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if separator:
                entries[name.strip()] = value.strip().strip("\"'")
    return entries | os.environ

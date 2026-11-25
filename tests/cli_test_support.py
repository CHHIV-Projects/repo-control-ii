from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock


@contextmanager
def isolated_legacy_cli(state_root: Path) -> Iterator[None]:
    with (
        mock.patch("repoctl.managed_cli_binding.load_managed_cli_binding", return_value=None),
        mock.patch("repoctl.scanner.core.DEFAULT_STATE_ROOT", state_root),
        mock.patch("repoctl.snapshot.manager.DEFAULT_STATE_ROOT", state_root),
    ):
        yield

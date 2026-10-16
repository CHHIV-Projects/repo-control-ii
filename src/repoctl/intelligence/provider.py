from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Any, Protocol

VERSION = "0.9.91"
RUNTIME = Path.home() / ".local/share/repoctl-tools/graphify-0.9.91/venv/bin/python"
MAX_GRAPH_BYTES = 24 * 1024 * 1024


class IntelligenceError(ValueError):
    pass


class Provider(Protocol):
    version: str

    def build(self, source: Path, output: Path) -> None: ...
    def query(self, graph: Path, arguments: dict[str, Any], timeout: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GraphifyProvider:
    version: str = VERSION

    def _run(self, payload: dict[str, Any], directory: Path, timeout: float) -> bytes:
        if not RUNTIME.is_file():
            raise IntelligenceError(f"Pinned Graphify runtime missing: {RUNTIME}. See tools/graphify-requirements.txt.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = directory / "home"
        home.mkdir(mode=0o700, exist_ok=True)
        env = {
            "HOME": str(home), "XDG_CONFIG_HOME": str(home), "XDG_CACHE_HOME": str(home / "cache"),
            "TMPDIR": str(home), "PATH": str(RUNTIME.parent) + ":/usr/bin:/bin",
            "GRAPHIFY_OUT": str(directory), "GRAPHIFY_NO_AUTO_REFRESH": "1",
            "GRAPHIFY_QUERY_LOG_DISABLE": "1", "GRAPHIFY_MAX_GRAPH_BYTES": str(MAX_GRAPH_BYTES),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "LANG": "C.UTF-8",
        }
        worker = Path(__file__).with_name("worker.py")
        process = subprocess.Popen(
            [str(RUNTIME), "-I", "-B", str(worker)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=directory,
            close_fds=True, start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        output, errors = bytearray(), bytearray()
        started = time.monotonic()
        try:
            process.stdin.write(json.dumps(payload).encode("utf-8"))
            process.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, errors)
                while selector.get_map():
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise IntelligenceError("Graphify request timed out; child process group terminated.")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        block = os.read(key.fd, 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        key.data.extend(block)
                        cap = 2 * 1024 * 1024 if key.data is output else 64 * 1024
                        if len(key.data) > cap:
                            raise IntelligenceError("Graphify native output exceeded independent pipe limit.")
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise IntelligenceError("Graphify request timed out.")
                process.wait(timeout=remaining)
            if process.returncode:
                detail = errors.decode("utf-8", errors="replace")[-1500:]
                raise IntelligenceError(f"Graphify child failed ({process.returncode}): {detail}")
            return bytes(output)
        except subprocess.TimeoutExpired as exc:
            raise IntelligenceError("Graphify request timed out.") from exc
        except OSError as exc:
            raise IntelligenceError(f"Graphify process I/O failed: {exc}") from exc
        finally:
            # A timed-out/crashed parent must not leave extraction workers behind.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def build(self, source: Path, output: Path) -> None:
        self._run({"action": "build", "source": str(source)}, output, 90.0)
        path = output / "graph.json"
        if not path.is_file() or path.stat().st_size > MAX_GRAPH_BYTES:
            raise IntelligenceError("Graphify did not produce a graph within its size bound.")

    def query(self, graph: Path, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        raw = self._run(
            {"action": "query", "graph": str(graph), "arguments": arguments},
            graph.parent, timeout,
        )
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise IntelligenceError("Graphify returned an invalid bridge result.") from exc
        if not isinstance(result, dict) or result.get("outcome") not in {
            "OK", "AMBIGUOUS", "NOT_FOUND", "INCONCLUSIVE", "TRUNCATED",
        }:
            raise IntelligenceError("Graphify returned an unrecognized bridge outcome.")
        if not isinstance(result.get("nodes"), list) or not isinstance(result.get("relationships"), list):
            raise IntelligenceError("Graphify bridge result omitted structural arrays.")
        return result

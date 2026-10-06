from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock
from contextlib import redirect_stderr

from repoctl.analysis.contracts import MAX_ANALYSIS_PACKET_BYTES, derive_request_id
from repoctl.analysis.manager import AnalysisError, analyze_comparison
from repoctl.analysis.packet import build_analysis_packet
from repoctl.analysis.provider import (
    FAIL_INVALID_CONTENT_JSON,
    FAIL_INVALID_CONTENT_TYPE,
    FAIL_INVALID_ENVELOPE,
    FAIL_MISSING_CONTENT,
    FAIL_MISSING_MESSAGE,
    FAIL_PROVIDER_HTTP,
    ModelIdentity,
    OllamaLocalProvider,
    ProviderError,
)
from repoctl.cli import main
from repoctl.compare.manager import compare_snapshots
from repoctl.scanner.core import run_scan_with_artifacts
from repoctl.snapshot.manager import create_snapshot


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout


def _init_repo(root: Path) -> Path:
    repo = root / "analysis-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "module_a.py").write_text(
        "from module_b import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    (repo / "module_b.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "test_module_a.py").write_text(
        "from module_b import helper\n\n\ndef test_run():\n    helper()\n",
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _make_comparison(repo: Path, state_root: Path, *, mutate: bool) -> dict[str, str]:
    before = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)
    if mutate:
        (repo / "module_b.py").write_text(
            "from module_c import new_func\n\ndef helper():\n    return new_func()\n",
            encoding="utf-8",
        )
        (repo / "module_c.py").write_text("def new_func():\n    return 3\n", encoding="utf-8")
        (repo / "test_module_a.py").write_text(
            "from module_c import new_func\n\n\ndef test_run():\n    new_func()\n",
            encoding="utf-8",
        )
        (repo / "requirements.txt").write_text("requests\npytest\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "delta")

    after = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)
    compared = compare_snapshots(before["snapshot_id"], after["snapshot_id"], str(repo), state_root=state_root)
    return {
        "comparison_id": compared["comparison_id"],
        "repository_id": compared["repository_id"],
    }


def _read_comparison_json(state_root: Path, repository_id: str, comparison_id: str) -> dict:
    path = state_root / repository_id / "comparisons" / comparison_id / "comparison.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_response(packet_payload: dict, *, non_zero: bool, summary: str = "Deterministic structural evidence reviewed.") -> dict:
    if non_zero:
        return {
            "summary": summary,
            "summary_evidence_ids": ["A001"],
            "review_signals": [
                {
                    "category": "cross_category",
                    "review_priority": "low",
                    "observation": "Structural changes are present in deterministic aggregate evidence.",
                    "interpretation": "Direct source and test inspection is appropriate before drawing conclusions.",
                    "evidence_ids": ["A001"],
                }
            ],
            "questions_for_human_review": [
                {
                    "review_priority": "low",
                    "question": "Were these structural deltas intentional in this change set?",
                    "evidence_ids": ["A001"],
                }
            ],
        }

    return {
        "summary": summary,
        "summary_evidence_ids": ["A001"],
        "review_signals": [],
        "questions_for_human_review": [],
    }


class FakeProvider:
    def __init__(self, response_payload: dict, digest: str = "a" * 64) -> None:
        self.response_payload = response_payload
        self.identity = ModelIdentity(provider="ollama", model_name="gpt-oss:20b", model_digest=digest)

    def resolve_model_identity(self) -> ModelIdentity:
        return self.identity

    def generate_analysis(self, **kwargs):
        return self.response_payload


class StubOllamaProvider(OllamaLocalProvider):
    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:11434")
        self.calls: list[str] = []

    def _request_json(self, method: str, path: str, body=None):
        self.calls.append(path)
        if path == "/api/tags":
            return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
        if path == "/api/chat":
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "ok",
                            "summary_evidence_ids": ["A001"],
                            "review_signals": [
                                {
                                    "category": "cross_category",
                                    "review_priority": "low",
                                    "observation": "obs",
                                    "interpretation": "interp",
                                    "evidence_ids": ["A001"],
                                }
                            ],
                            "questions_for_human_review": [
                                {
                                    "review_priority": "low",
                                    "question": "q",
                                    "evidence_ids": ["A001"],
                                }
                            ],
                        }
                    )
                }
            }
        raise RuntimeError("unexpected path")


class CaptureRequestProvider(OllamaLocalProvider):
    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:11434")
        self.chat_body = None

    def _request_json(self, method: str, path: str, body=None):
        if path == "/api/tags":
            return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
        if path == "/api/chat":
            self.chat_body = body
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "ok",
                            "summary_evidence_ids": ["A001"],
                            "review_signals": [
                                {
                                    "category": "cross_category",
                                    "review_priority": "low",
                                    "observation": "obs",
                                    "interpretation": "interp",
                                    "evidence_ids": ["A001"],
                                }
                            ],
                            "questions_for_human_review": [
                                {
                                    "review_priority": "low",
                                    "question": "q",
                                    "evidence_ids": ["A001"],
                                }
                            ],
                        }
                    )
                }
            }
        raise RuntimeError("unexpected path")


class AnalysisTests(unittest.TestCase):
    def test_cli_analyze_routes_command(self) -> None:
        fake = {
            "analysis_id": "ana--x",
            "analysis_dir": "/tmp/x",
            "request_id": "areq--x",
            "packet_id": "aip--x",
            "repository_root": "/tmp/r",
            "repository_id": "repo--x",
            "comparison_id": "cmp--x",
            "provider": "ollama",
            "model_name": "gpt-oss:20b",
            "model_digest": "c" * 64,
            "reused_existing": False,
        }
        with mock.patch("repoctl.cli.analyze_comparison", return_value=fake) as patched:
            code = main(["analyze", "cmp--x", "--repository", "/tmp/repo"])
            self.assertEqual(code, 0)
            self.assertTrue(patched.called)

    def test_analyze_missing_comparison_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            with self.assertRaises(AnalysisError):
                analyze_comparison("cmp--missing", str(repo), state_root=root / "state", provider=FakeProvider({}))

    def test_packet_is_deterministic_and_excludes_source_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)
            comparison = _read_comparison_json(state, ids["repository_id"], ids["comparison_id"])

            packet1 = build_analysis_packet(comparison)
            packet2 = build_analysis_packet(comparison)
            self.assertEqual(packet1, packet2)
            packet_text = json.dumps(packet1, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("def helper():", packet_text)

    def test_packet_respects_byte_bound(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            before = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            for idx in range(80):
                (repo / f"many_{idx}.py").write_text(f"def fn_{idx}():\n    return {idx}\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "many changes")
            after = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            cmp_result = compare_snapshots(before["snapshot_id"], after["snapshot_id"], str(repo), state_root=state)
            comparison = _read_comparison_json(state, cmp_result["repository_id"], cmp_result["comparison_id"])

            packet = build_analysis_packet(comparison)
            self.assertLessEqual(packet["truncation_metadata"]["final_serialized_packet_byte_count"], MAX_ANALYSIS_PACKET_BYTES)
            self.assertEqual(packet["truncation_metadata"]["records_retained_after_category_cap"]["file_changes"], 25)

    def test_zero_delta_rejects_fabricated_signal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=False)
            bad = _valid_response({}, non_zero=False)
            bad["review_signals"] = [
                {
                    "category": "cross_category",
                    "review_priority": "low",
                    "observation": "bad",
                    "interpretation": "bad",
                    "evidence_ids": ["A001"],
                }
            ]
            provider = FakeProvider(bad)
            with self.assertRaises(AnalysisError):
                analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=provider)

    def test_non_zero_requires_at_least_one_signal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)
            bad = _valid_response({}, non_zero=False)
            provider = FakeProvider(bad)
            with self.assertRaises(AnalysisError):
                analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=provider)

    def test_unknown_evidence_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)

            bad = _valid_response({}, non_zero=True)
            bad["summary_evidence_ids"] = ["Z999"]
            provider = FakeProvider(bad)
            with self.assertRaises(AnalysisError):
                analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=provider)

    def test_newline_text_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)

            bad = _valid_response({}, non_zero=True)
            bad["summary"] = "line1\nline2"
            provider = FakeProvider(bad)
            with self.assertRaises(AnalysisError):
                analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=provider)

    def test_analysis_publication_reuses_identical_analysis_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)

            provider = FakeProvider(_valid_response({}, non_zero=True))
            first = analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=provider)
            second = analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=provider)
            self.assertEqual(first["analysis_id"], second["analysis_id"])
            self.assertTrue(second["reused_existing"])

    def test_different_valid_outputs_produce_different_analysis_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)

            first = analyze_comparison(
                ids["comparison_id"],
                str(repo),
                state_root=state,
                provider=FakeProvider(_valid_response({}, non_zero=True, summary="summary one")),
            )
            second = analyze_comparison(
                ids["comparison_id"],
                str(repo),
                state_root=state,
                provider=FakeProvider(_valid_response({}, non_zero=True, summary="summary two")),
            )
            self.assertNotEqual(first["analysis_id"], second["analysis_id"])

    def test_request_id_changes_with_model_digest(self) -> None:
        packet_id = "aip--1111222233334444"
        one = derive_request_id(packet_id, "ollama", "gpt-oss:20b", "a" * 64, "repoctl-structural-analysis-v1")
        two = derive_request_id(packet_id, "ollama", "gpt-oss:20b", "b" * 64, "repoctl-structural-analysis-v1")
        self.assertNotEqual(one, two)

    def test_provider_digest_lookup_uses_api_tags(self) -> None:
        provider = StubOllamaProvider()
        identity = provider.resolve_model_identity()
        self.assertEqual(identity.model_name, "gpt-oss:20b")
        self.assertEqual(provider.calls, ["/api/tags"])

    def test_provider_request_uses_think_low_and_no_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)

            provider = CaptureRequestProvider()
            analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=provider)

            self.assertIsNotNone(provider.chat_body)
            self.assertEqual(provider.chat_body["think"], "low")
            self.assertNotIn("tools", provider.chat_body)
            self.assertEqual(provider.chat_body["stream"], False)
            self.assertEqual(provider.chat_body["options"], {"temperature": 0})
            self.assertIn("format", provider.chat_body)
            self.assertIsInstance(provider.chat_body["format"], dict)

    def test_provider_rejects_missing_message(self) -> None:
        class P(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                return {}

        p = P()
        with self.assertRaises(ProviderError) as ctx:
            p.generate_analysis(
                model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                packet_payload={"x": 1},
                request_id="x",
                prompt_contract_version="repoctl-structural-analysis-v1",
            )
        self.assertEqual(ctx.exception.code, FAIL_MISSING_MESSAGE)

    def test_provider_http_failure_code(self) -> None:
        class P(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                raise ProviderError(FAIL_PROVIDER_HTTP, "ollama unavailable: URLError")

        p = P()
        with self.assertRaises(ProviderError) as ctx:
            p.generate_analysis(
                model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                packet_payload={"x": 1},
                request_id="x",
                prompt_contract_version="repoctl-structural-analysis-v1",
            )
        self.assertEqual(ctx.exception.code, FAIL_PROVIDER_HTTP)

    def test_provider_invalid_envelope_code(self) -> None:
        class P(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                raise ProviderError(FAIL_INVALID_ENVELOPE, "ollama response body was not valid JSON")

        p = P()
        with self.assertRaises(ProviderError) as ctx:
            p.generate_analysis(
                model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                packet_payload={"x": 1},
                request_id="x",
                prompt_contract_version="repoctl-structural-analysis-v1",
            )
        self.assertEqual(ctx.exception.code, FAIL_INVALID_ENVELOPE)

    def test_provider_rejects_missing_message_content(self) -> None:
        class P(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                return {"message": {}}

        p = P()
        with self.assertRaises(ProviderError) as ctx:
            p.generate_analysis(
                model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                packet_payload={"x": 1},
                request_id="x",
                prompt_contract_version="repoctl-structural-analysis-v1",
            )
        self.assertEqual(ctx.exception.code, FAIL_MISSING_CONTENT)

    def test_provider_rejects_non_string_message_content(self) -> None:
        class P(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                return {"message": {"content": {"x": 1}}}

        p = P()
        with self.assertRaises(ProviderError) as ctx:
            p.generate_analysis(
                model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                packet_payload={"x": 1},
                request_id="x",
                prompt_contract_version="repoctl-structural-analysis-v1",
            )
        self.assertEqual(ctx.exception.code, FAIL_INVALID_CONTENT_TYPE)

    def test_provider_rejects_invalid_structured_content_json(self) -> None:
        class P(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                return {"message": {"content": "not-json"}}

        p = P()
        with self.assertRaises(ProviderError) as ctx:
            p.generate_analysis(
                model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                packet_payload={"x": 1},
                request_id="x",
                prompt_contract_version="repoctl-structural-analysis-v1",
            )
        self.assertEqual(ctx.exception.code, FAIL_INVALID_CONTENT_JSON)

    def test_provider_rejects_markdown_fenced_and_prose_wrapped_json(self) -> None:
        class P1(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                return {"message": {"content": "```json\\n{\"summary\":\"x\"}\\n```"}}

        class P2(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                return {"message": {"content": "prefix {\"summary\":\"x\"} suffix"}}

        for provider in (P1(), P2()):
            with self.assertRaises(ProviderError) as ctx:
                provider.generate_analysis(
                    model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                    packet_payload={"x": 1},
                    request_id="x",
                    prompt_contract_version="repoctl-structural-analysis-v1",
                )
            self.assertEqual(ctx.exception.code, FAIL_INVALID_CONTENT_JSON)

    def test_thinking_content_never_leaked_in_provider_error_message(self) -> None:
        secret = "THINKING_SECRET_12345"

        class P(OllamaLocalProvider):
            def _request_json(self, method: str, path: str, body=None):
                if path == "/api/tags":
                    return {"models": [{"name": "gpt-oss:20b", "digest": "b" * 64}]}
                return {"message": {"thinking": secret, "content": "not-json"}}

        p = P()
        with self.assertRaises(ProviderError) as ctx:
            p.generate_analysis(
                model_identity=ModelIdentity("ollama", "gpt-oss:20b", "b" * 64),
                packet_payload={"x": 1},
                request_id="x",
                prompt_contract_version="repoctl-structural-analysis-v1",
            )
        self.assertNotIn(secret, str(ctx.exception))

    def test_cli_provider_error_shape(self) -> None:
        with mock.patch(
            "repoctl.cli.analyze_comparison",
            side_effect=AnalysisError("provider error [invalid_structured_content_json]: message.content was not valid JSON"),
        ):
            buf = StringIO()
            with redirect_stderr(buf):
                code = main(["analyze", "cmp--x", "--repository", "/tmp/repo"])
            self.assertEqual(code, 2)
            self.assertIn("provider error [invalid_structured_content_json]: message.content was not valid JSON", buf.getvalue())
            self.assertNotIn("analyze failed:", buf.getvalue())

    def test_validation_error_codes_distinguish_schema_and_grounding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)

            schema_bad = {
                "summary": "x",
                "summary_evidence_ids": ["A001"],
                "review_signals": [
                    {
                        "category": "not-a-category",
                        "review_priority": "low",
                        "observation": "obs",
                        "interpretation": "interp",
                        "evidence_ids": ["A001"],
                    }
                ],
                "questions_for_human_review": [],
            }
            with self.assertRaises(AnalysisError) as ctx1:
                analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=FakeProvider(schema_bad))
            self.assertIn("provider error [structured_content_validation_failed]", str(ctx1.exception))

            grounding_bad = _valid_response({}, non_zero=True)
            grounding_bad["summary_evidence_ids"] = ["Z999"]
            with self.assertRaises(AnalysisError) as ctx2:
                analyze_comparison(ids["comparison_id"], str(repo), state_root=state, provider=FakeProvider(grounding_bad))
            self.assertIn("provider error [evidence_grounding_validation_failed]", str(ctx2.exception))

    def test_analyze_does_not_change_target_git_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            ids = _make_comparison(repo, state, mutate=True)

            before = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            analyze_comparison(
                ids["comparison_id"],
                str(repo),
                state_root=state,
                provider=FakeProvider(_valid_response({}, non_zero=True)),
            )
            after = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from unittest import mock

from repoctl.workflow.context_verification import VERIFICATION_RECORD_SCHEMA_VERSION
from repoctl.workflow.lifecycle import workflow_runtime_capabilities
from repoctl.workflow.runtime_compatibility import (
    RuntimeCompatibilityError,
    require_event_runtime_compatibility,
)


class RuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "service": "repo-control",
            "runtime_capabilities": {
                "capability_schema_version": 1,
                "instance_id": "a" * 32,
                "workflow": {
                    **workflow_runtime_capabilities(),
                    "context_verification_record_schema_version": (
                        VERIFICATION_RECORD_SCHEMA_VERSION
                    ),
                },
            },
        }

    def _response(self, payload: object, *, status: int = 200):
        response = mock.Mock()
        response.status = status
        response.read.return_value = json.dumps(payload).encode("utf-8")
        connection = mock.Mock()
        connection.getresponse.return_value = response
        return connection

    def test_accepts_compatible_running_service_and_records_its_identity(self) -> None:
        connection = self._response(self.payload)
        with mock.patch(
            "repoctl.workflow.runtime_compatibility.http.client.HTTPConnection",
            return_value=connection,
        ) as constructor:
            result = require_event_runtime_compatibility("context_verification_recorded")

        self.assertEqual(result["status"], "COMPATIBLE")
        self.assertEqual(result["runtime_instance_id"], "a" * 32)
        constructor.assert_called_once_with("127.0.0.1", 8765, timeout=1.5)
        connection.request.assert_called_once_with(
            "GET", "/healthz", headers={"Accept": "application/json"}
        )
        connection.close.assert_called_once()

    def test_rejects_old_reader_before_event_write(self) -> None:
        old_payload = json.loads(json.dumps(self.payload))
        old_payload["runtime_capabilities"]["workflow"][
            "context_verification_record_schema_version"
        ] = 1
        connection = self._response(old_payload)
        with mock.patch(
            "repoctl.workflow.runtime_compatibility.http.client.HTTPConnection",
            return_value=connection,
        ):
            with self.assertRaises(RuntimeCompatibilityError) as caught:
                require_event_runtime_compatibility("context_verification_recorded")
        self.assertEqual(caught.exception.code, "runtime_upgrade_required")

    def test_unavailable_unknown_and_non_loopback_runtime_fail_closed(self) -> None:
        connection = mock.Mock()
        connection.request.side_effect = OSError("connection refused")
        with mock.patch(
            "repoctl.workflow.runtime_compatibility.http.client.HTTPConnection",
            return_value=connection,
        ):
            with self.assertRaises(RuntimeCompatibilityError) as caught:
                require_event_runtime_compatibility("context_verification_recorded")
        self.assertEqual(caught.exception.code, "runtime_unavailable")

        malformed = self._response({"service": "repo-control"})
        with mock.patch(
            "repoctl.workflow.runtime_compatibility.http.client.HTTPConnection",
            return_value=malformed,
        ):
            with self.assertRaises(RuntimeCompatibilityError) as caught:
                require_event_runtime_compatibility("context_verification_recorded")
        self.assertEqual(caught.exception.code, "runtime_capability_unknown")

        with self.assertRaises(RuntimeCompatibilityError) as caught:
            require_event_runtime_compatibility(
                "context_verification_recorded",
                "http://example.org:8765",
            )
        self.assertEqual(caught.exception.code, "runtime_capability_unknown")

    def test_unregistered_event_contract_fails_before_network_access(self) -> None:
        with mock.patch(
            "repoctl.workflow.runtime_compatibility.http.client.HTTPConnection"
        ) as constructor:
            with self.assertRaises(RuntimeCompatibilityError) as caught:
                require_event_runtime_compatibility("future_event")
        self.assertEqual(caught.exception.code, "runtime_contract_missing")
        constructor.assert_not_called()

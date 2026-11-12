from __future__ import annotations

from collections.abc import Callable

from flask import Blueprint, jsonify, render_template, request

from ..information_store import InformationStoreError
from ..workflow.closeout_publication import (
    CloseoutPublicationError,
    CloseoutPublicationService,
    FixtureGitReviewHandoffAdapter,
)


def create_fixture_closeout_publication_blueprint(
    service: CloseoutPublicationService,
    *,
    fixture_po_confirmation: Callable[[dict], bool],
    name: str = "fixture_closeout_publication",
) -> Blueprint:
    """Create an unregistered fixture-only flow with an injected PO-confirmation gate."""
    if service.context.test_only is not True:
        raise ValueError("The closeout publication UI is fixture-only.")
    if not callable(fixture_po_confirmation):
        raise ValueError("An explicit fixture PO-confirmation gate is required.")
    blueprint = Blueprint(name, __name__, template_folder="templates")

    @blueprint.errorhandler(ValueError)
    def handle_invalid_request(error: ValueError):
        return jsonify({"error": {"code": "invalid_request", "message": str(error)}}), 400

    @blueprint.errorhandler(CloseoutPublicationError)
    def handle_publication_error(error: CloseoutPublicationError):
        return jsonify({"error": {"code": type(error).__name__, "message": str(error)}}), 409

    @blueprint.errorhandler(InformationStoreError)
    def handle_store_error(error: InformationStoreError):
        return jsonify({"error": {"code": type(error).__name__, "message": str(error)}}), 409

    @blueprint.get("/")
    def index():
        return render_template(
            "closeout_publication.html",
            result=None,
            integration_status=service.integration_status,
            error=None,
        )

    @blueprint.post("/preview")
    def preview():
        result = service.preview(
            request_id=request.form.get("request_id", ""),
            document_id=request.form.get("document_id", ""),
            revision_id=request.form.get("revision_id", ""),
            expected_sha256=request.form.get("content_sha256", ""),
        )
        return render_template(
            "closeout_publication.html",
            result=result,
            integration_status=result["integration_status"],
            error=None,
        )

    @blueprint.post("/<publication_id>/publish")
    def publish(publication_id: str):
        record = service.store.get_document_publication(publication_id=publication_id)
        if record is None:
            return jsonify({"error": {"code": "not_found", "message": "Publication preview was not found."}}), 404
        if request.form.get("confirm_publish") != "yes" or not fixture_po_confirmation(record):
            return jsonify({"error": {"code": "po_confirmation_required", "message": "Fixture PO confirmation is required."}}), 403
        result = service.materialize(
            publication_id,
            request.form.get("preview_fingerprint", ""),
            fixture_po_confirmation=True,
        )
        revision = service.store.get_revision(result["document_id"], result["revision_id"])
        rendered = {
            **result,
            "repository_key": service.identity.repository_key,
            "registered_repository_root": service.identity.repository_root,
            "branch": result["target_branch"],
            "head": result["target_head"],
            "target_path": result["target_path"],
            "source": {
                "document_id": result["document_id"],
                "revision_id": result["revision_id"],
                "content_sha256": result["content_sha256"],
                "content": revision["content"],
            },
            "proposed_diff": result["git_observation"].get("proposed_diff", ""),
            "integration_status": service.integration_status,
        }
        return render_template(
            "closeout_publication.html",
            result=rendered,
            integration_status=service.integration_status,
            error=None,
        )

    @blueprint.get("/<publication_id>/handoff")
    def handoff(publication_id: str):
        return jsonify(FixtureGitReviewHandoffAdapter(service).prepare(publication_id))

    return blueprint

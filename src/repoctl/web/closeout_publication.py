from __future__ import annotations

from collections.abc import Callable

from flask import Blueprint, jsonify, render_template, request

from ..information_store import InformationStoreError
from ..workflow.closeout_publication import (
    CloseoutPublicationError,
    CloseoutPublicationService,
    GitReviewHandoffAdapter,
)


def create_fixture_closeout_publication_blueprint(
    service: CloseoutPublicationService,
    *,
    fixture_po_confirmation: Callable[[dict], bool],
    name: str = "fixture_closeout_publication",
) -> Blueprint:
    if not service.fixture_only:
        raise ValueError("The closeout publication UI is fixture-only.")
    if not callable(fixture_po_confirmation):
        raise ValueError("An explicit fixture PO-confirmation gate is required.")
    return _create_closeout_publication_blueprint(
        lambda: service,
        confirmation=fixture_po_confirmation,
        mutation_guard=lambda: None,
        fixture_only=True,
        name=name,
    )


def create_managed_closeout_publication_blueprint(
    service_factory: Callable[[], CloseoutPublicationService],
    *,
    mutation_guard: Callable[[], None],
    name: str = "managed_closeout_publication",
) -> Blueprint:
    if not callable(service_factory) or not callable(mutation_guard):
        raise ValueError("Managed publication requires a bound service and a mutation guard.")
    return _create_closeout_publication_blueprint(
        service_factory,
        confirmation=lambda _record: True,
        mutation_guard=mutation_guard,
        fixture_only=False,
        name=name,
    )


def _create_closeout_publication_blueprint(
    service_factory: Callable[[], CloseoutPublicationService],
    *,
    confirmation: Callable[[dict], bool],
    mutation_guard: Callable[[], None],
    fixture_only: bool,
    name: str,
) -> Blueprint:
    blueprint = Blueprint(name, __name__, template_folder="templates")

    def service_for_request() -> CloseoutPublicationService:
        service = service_factory()
        if service.fixture_only != fixture_only:
            raise CloseoutPublicationError("Publication service authority changed; reload and retry.")
        return service

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
        service = service_for_request()
        return render_template(
            "closeout_publication.html",
            result=None,
            integration_status=service.integration_status,
            error=None,
            fixture_only=fixture_only,
        )

    @blueprint.post("/preview")
    def preview():
        mutation_guard()
        service = service_for_request()
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
            fixture_only=fixture_only,
        )

    @blueprint.post("/<publication_id>/publish")
    def publish(publication_id: str):
        mutation_guard()
        service = service_for_request()
        record = service.store.get_document_publication(publication_id=publication_id)
        if record is None:
            return jsonify({"error": {"code": "not_found", "message": "Publication preview was not found."}}), 404
        if request.form.get("confirm_publish") != "yes" or not confirmation(record):
            return jsonify({"error": {"code": "po_confirmation_required", "message": "PO confirmation is required."}}), 403
        result = service.materialize(
            publication_id,
            request.form.get("preview_fingerprint", ""),
            fixture_po_confirmation=fixture_only,
            managed_po_confirmation=not fixture_only,
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
            fixture_only=fixture_only,
        )

    @blueprint.get("/<publication_id>/handoff")
    def handoff(publication_id: str):
        return jsonify(GitReviewHandoffAdapter(service_for_request()).prepare(publication_id))

    @blueprint.post("/<publication_id>/reconcile")
    def reconcile(publication_id: str):
        mutation_guard()
        service = service_for_request()
        commit_id = request.form.get("commit_id") or None
        return jsonify(service.reconcile(publication_id, commit_id=commit_id))

    return blueprint

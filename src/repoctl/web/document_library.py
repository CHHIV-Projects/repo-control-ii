from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from ..document_library import (
    DocumentLibrary,
    DocumentLibraryError,
    DocumentNotFoundError,
    DocumentTooLargeError,
    InvalidCursorError,
    RevisionNotFoundError,
    SourceEnumerationError,
    StaleCursorError,
)


_FILTERS = (
    "repository_id",
    "repository_key",
    "milestone_id",
    "milestone_text",
    "document_class",
    "authority",
    "source",
    "status",
)


def create_document_library_blueprint(
    library: DocumentLibrary,
    *,
    name: str = "fixture_document_library",
) -> Blueprint:
    """Create an unregistered-by-default read-only UI component over an injected library."""
    blueprint = Blueprint(name, __name__, template_folder="templates")

    @blueprint.errorhandler(ValueError)
    def handle_invalid_request(error: ValueError):
        return jsonify({"error": {"code": "invalid_request", "message": str(error)}}), 400

    @blueprint.errorhandler(DocumentLibraryError)
    def handle_library_error(error: DocumentLibraryError):
        if isinstance(error, InvalidCursorError):
            status = 400
        elif isinstance(error, StaleCursorError):
            status = 409
        elif isinstance(error, (DocumentNotFoundError, RevisionNotFoundError)):
            status = 404
        elif isinstance(error, DocumentTooLargeError):
            status = 413
        elif isinstance(error, SourceEnumerationError):
            status = 503
        else:
            status = 400
        return jsonify({"error": {"code": type(error).__name__, "message": str(error)}}), status

    @blueprint.get("/")
    def index():
        filters = {key: request.args.get(key) for key in _FILTERS if request.args.get(key)}
        try:
            page_size = int(request.args.get("page_size", "20"))
            page = library.search(
                query=request.args.get("q", ""),
                filters=filters,
                page_size=page_size,
                cursor=request.args.get("cursor"),
            )
        except ValueError as exc:
            return jsonify({"error": {"code": "invalid_request", "message": str(exc)}}), 400
        search_args = {
            "q": request.args.get("q", ""),
            **filters,
            "page_size": page_size,
        }
        return render_template(
            "document_library.html",
            page=page,
            search_args=search_args,
        )

    @blueprint.get("/documents/<document_id>/<revision_id>")
    def document(document_id: str, revision_id: str):
        item = library.read_page(
            document_id,
            revision_id,
            cursor=request.args.get("cursor"),
        )
        return render_template("document_library_document.html", document=item)

    @blueprint.get("/compare")
    def compare():
        left_document_id, left_revision_id = _split_selection(request.args.get("left", ""))
        right_document_id, right_revision_id = _split_selection(request.args.get("right", ""))
        result = library.compare(
            left_document_id,
            left_revision_id,
            right_document_id,
            right_revision_id,
        )
        return render_template("document_library_compare.html", result=result)

    @blueprint.get("/export")
    def export():
        selections = request.args.getlist("selection")
        items = [_split_selection(item) for item in selections]
        payload = library.export(items)
        response = jsonify(payload)
        response.headers["Content-Disposition"] = 'attachment; filename="document-library-export.json"'
        return response

    def _split_selection(value: str) -> tuple[str, str]:
        if not isinstance(value, str) or value.count("|") != 1:
            raise ValueError("Select exact document/revision IDs; paths are not accepted.")
        document_id, revision_id = value.split("|", 1)
        if not document_id or not revision_id:
            raise ValueError("Both document ID and revision ID are required.")
        return document_id, revision_id

    return blueprint

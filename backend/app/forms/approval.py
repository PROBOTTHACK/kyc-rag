"""Approved-forms allowlist for multimodal extraction (M2 Pipeline B; docs/04).

Only documents listed in `data/raw_pdfs/forms_approved.yaml` with a named approver and
date, the "public blank form" attestation, and a SHA-256 equal to both the manifest and
`documents.sha256`, may be rendered and sent to an external model. Everything else
(including any future upload path) is refused before rendering.
"""

import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "data" / "raw_pdfs" / "forms_approved.yaml"


class NotApproved(ValueError):
    """The document may not be sent to a multimodal provider."""


class ApprovedForm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attestation: Literal["public_blank_form"]  # no filled forms, IDs or customer data
    source: str
    approved_by: str | None = None
    approved_on: dt.date | None = None

    @property
    def is_approved(self) -> bool:
        return bool(self.approved_by and self.approved_by.strip()) and self.approved_on is not None


def load_approvals(path: Path = DEFAULT_PATH) -> dict[str, ApprovedForm]:
    """Parse the allowlist; any malformed or duplicate entry fails the whole file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise NotApproved(f"cannot read approved-forms list: {type(exc).__name__}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise NotApproved("approved-forms list must be a YAML list")
    out: dict[str, ApprovedForm] = {}
    for i, item in enumerate(raw):
        try:
            entry = ApprovedForm.model_validate(item)
        except ValidationError as exc:
            raise NotApproved(
                f"approved-forms entry #{i} is invalid: {exc.errors()[0]['msg']}"
            ) from exc
        if entry.document_key in out:
            raise NotApproved(f"duplicate approved-forms entry: {entry.document_key}")
        out[entry.document_key] = entry
    return out


def check_approved(
    approvals: dict[str, ApprovedForm],
    *,
    document_key: str,
    document_type: str,
    manifest_sha256: str,
    stored_sha256: str,
) -> ApprovedForm:
    """Return the approval, or raise `NotApproved` with the reason."""
    if document_type != "form":
        raise NotApproved(f"{document_key}: document_type is {document_type!r}, not 'form'")
    entry = approvals.get(document_key)
    if entry is None:
        raise NotApproved(f"{document_key}: not on the approved-forms list")
    if not entry.is_approved:
        raise NotApproved(f"{document_key}: listed but not yet approved (approved_by/on unset)")
    if not entry.sha256 == manifest_sha256 == stored_sha256:
        raise NotApproved(f"{document_key}: approved SHA-256 differs from manifest or database")
    return entry

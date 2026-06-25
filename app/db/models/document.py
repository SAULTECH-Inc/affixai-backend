from enum import Enum

from tortoise import fields
from tortoise.models import Model


class DocumentType(str, Enum):
    PASSPORT = "passport"
    ID_CARD = "id_card"
    DRIVERS_LICENSE = "drivers_license"
    FORM = "form"
    CONTRACT = "contract"
    APPLICATION = "application"
    CERTIFICATE = "certificate"
    OTHER = "other"


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    DRAFT = "draft"
    PENDING_SIGNATURE = "pending_signature"
    SIGNED = "signed"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


class ProcessingMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    HYBRID = "hybrid"


class RoutingMode(str, Enum):
    """How participants are sequenced through the signing flow.

    PARALLEL   — all signers receive the invite at once; any order.
    SEQUENTIAL — signers go in `participant.sequence_order`. The next signer
                 receives their invite only when the previous one signs.
    """
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class RoutingStatus(str, Enum):
    """The workflow state of the document (orthogonal to DocumentStatus,
    which describes the file processing pipeline).

    DRAFT       — owner is still preparing; not visible to participants
    SENT        — owner clicked "Send for signature"; invites dispatched
    IN_PROGRESS — at least one participant has acted but completion is pending
    COMPLETED   — all signers signed + reviewers approved
    DECLINED    — a signer declined; workflow halts
    EXPIRED     — expires_at passed without completion
    VOIDED      — owner cancelled the workflow
    """
    DRAFT = "draft"
    SENT = "sent"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DECLINED = "declined"
    EXPIRED = "expired"
    VOIDED = "voided"


class Document(Model):
    id = fields.UUIDField(pk=True)
    # Either user_id (consumer flow) or enterprise_id (B2B API flow) is set.
    user_id = fields.UUIDField(index=True, null=True)
    enterprise_id = fields.UUIDField(index=True, null=True)

    file_name = fields.CharField(max_length=512)
    original_file_name = fields.CharField(max_length=512)
    file_url = fields.CharField(max_length=1024)
    file_mime_type = fields.CharField(max_length=128)
    file_size = fields.BigIntField()

    document_type = fields.CharEnumField(
        DocumentType, max_length=32, default=DocumentType.OTHER
    )
    status = fields.CharEnumField(
        DocumentStatus, max_length=32, default=DocumentStatus.UPLOADED
    )
    processing_mode = fields.CharEnumField(
        ProcessingMode, max_length=16, default=ProcessingMode.AUTO
    )

    template_id = fields.UUIDField(null=True)
    extracted_fields = fields.JSONField(null=True)
    field_placements = fields.JSONField(null=True)
    overall_confidence_score = fields.FloatField(null=True)

    is_template = fields.BooleanField(default=False)
    template_name = fields.CharField(max_length=255, null=True)

    version = fields.IntField(default=1)
    parent_document_id = fields.UUIDField(null=True)

    signature_data = fields.JSONField(null=True)
    completed_file_url = fields.CharField(max_length=1024, null=True)
    completed_at = fields.DatetimeField(null=True)

    # --- Workflow layer (Phase D) -------------------------------------------
    # These describe the SIGNING FLOW, separate from the document's processing
    # status. Defaults keep legacy single-signer / owner-only docs working
    # without any code changes — those rows stay in routing_status=DRAFT until
    # someone clicks Send.
    routing_mode = fields.CharEnumField(
        RoutingMode, max_length=16, default=RoutingMode.PARALLEL
    )
    routing_status = fields.CharEnumField(
        RoutingStatus, max_length=16, default=RoutingStatus.DRAFT
    )
    sent_at = fields.DatetimeField(null=True)
    expires_at = fields.DatetimeField(null=True)
    declined_by = fields.UUIDField(null=True)
    declined_reason = fields.TextField(null=True)

    shareable_link = fields.CharField(max_length=128, null=True)
    shareable_link_expiry = fields.DatetimeField(null=True)

    metadata = fields.JSONField(null=True)
    notes = fields.TextField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "documents"

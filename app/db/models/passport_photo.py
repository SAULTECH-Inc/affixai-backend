"""Passport-style photographs stored for affixing onto documents.

Distinct from signatures — these are rectangular portrait images (typically
~3.5×4.5 cm) that get stamped into PHOTOGRAPH/PHOTO slots on forms.
"""
from tortoise import fields
from tortoise.models import Model


class PassportPhoto(Model):
    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField(index=True)

    photo_url = fields.CharField(max_length=1024)  # local:// or s3 key
    name = fields.CharField(max_length=255, null=True)
    is_default = fields.BooleanField(default=False)

    # Dimensions of the captured image — useful if we ever auto-crop or report
    # quality issues to the user.
    width_px = fields.IntField(null=True)
    height_px = fields.IntField(null=True)

    metadata = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "passport_photos"

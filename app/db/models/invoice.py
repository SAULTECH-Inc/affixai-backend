"""Local invoice ledger.

We persist a row per gateway invoice/charge so the Billing page can render
history without round-tripping to the provider on every request. Webhook
handlers UPSERT on (provider, provider_invoice_id) — that pair is unique
per row.
"""
from __future__ import annotations

from enum import Enum

from tortoise import fields
from tortoise.models import Model


class InvoiceStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class Invoice(Model):
    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField(index=True)

    provider = fields.CharField(max_length=32)  # stripe | paystack | flutterwave
    provider_invoice_id = fields.CharField(max_length=128)

    amount = fields.DecimalField(max_digits=12, decimal_places=2)
    currency = fields.CharField(max_length=8)
    status = fields.CharEnumField(InvoiceStatus, max_length=16, default=InvoiceStatus.PENDING)

    description = fields.CharField(max_length=512, null=True)
    hosted_url = fields.CharField(max_length=512, null=True)   # gateway-hosted invoice page (Stripe)
    pdf_url = fields.CharField(max_length=512, null=True)      # downloadable PDF (Stripe)

    paid_at = fields.DatetimeField(null=True)

    metadata = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "invoices"
        unique_together = (("provider", "provider_invoice_id"),)

"""Email service — powered by Resend.

Drop-in replacement for the old aiosmtplib backend. All public function
signatures are unchanged so existing call sites require no edits.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from decimal import Decimal
from typing import Any

from loguru import logger

from app.core.config import settings

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BRAND_PURPLE = "#7c3aed"
_BRAND_PURPLE_DARK = "#5b21b6"
_BG = "#f0f0f5"
_CARD_BG = "#ffffff"
_TEXT_MAIN = "#1a1a2e"
_TEXT_MUTED = "#6b7280"
_BORDER = "#e5e7eb"


def _base_template(content: str, preheader: str = "") -> str:
    """Wrap `content` HTML in the standard AffixAI email shell.

    Uses a table-based layout for maximum email-client compatibility.
    Inline CSS only — no external stylesheets or web fonts.
    """
    app_name = settings.EMAIL_FROM_NAME
    year = __import__("datetime").date.today().year
    preheader_html = (
        f'<span style="display:none;max-height:0;overflow:hidden;'
        f'mso-hide:all;visibility:hidden;opacity:0;color:transparent;'
        f'height:0;width:0;">{preheader}&nbsp;‌&nbsp;‌&nbsp;‌</span>'
        if preheader
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta http-equiv="X-UA-Compatible" content="IE=edge"/>
  <title>{app_name}</title>
</head>
<body style="margin:0;padding:0;background-color:{_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  {preheader_html}
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
         style="background-color:{_BG};min-height:100vh;">
    <tr>
      <td align="center" style="padding:40px 16px;">

        <!-- Card -->
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
               style="max-width:560px;background:{_CARD_BG};border-radius:12px;
                      box-shadow:0 4px 24px rgba(0,0,0,0.07);overflow:hidden;">

          <!-- Header -->
          <tr>
            <td style="background:{_BRAND_PURPLE};padding:28px 32px;text-align:center;">
              <span style="font-size:22px;font-weight:700;color:#ffffff;
                           letter-spacing:-0.3px;">{app_name}</span>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 36px 28px;">
              {content}
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding:0 36px;">
              <hr style="border:none;border-top:1px solid {_BORDER};margin:0;"/>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:20px 36px 28px;text-align:center;">
              <p style="margin:0 0 4px;font-size:12px;color:{_TEXT_MUTED};line-height:1.5;">
                You received this email because you have an account with {app_name}.
              </p>
              <p style="margin:0;font-size:12px;color:{_TEXT_MUTED};">
                &copy; {year} {app_name}. All rights reserved.
              </p>
            </td>
          </tr>

        </table>
        <!-- /Card -->

      </td>
    </tr>
  </table>
</body>
</html>"""


def _cta_button(label: str, url: str) -> str:
    """Render a full-width CTA button cell."""
    return f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
       style="margin-top:28px;">
  <tr>
    <td align="center">
      <a href="{url}"
         style="display:inline-block;background:{_BRAND_PURPLE};color:#ffffff;
                font-size:15px;font-weight:600;text-decoration:none;
                padding:14px 36px;border-radius:8px;
                letter-spacing:0.1px;line-height:1;">
        {label}
      </a>
    </td>
  </tr>
</table>"""


def _fallback_link(url: str, label: str = "Or copy and paste this link into your browser:") -> str:
    return f"""
<p style="margin:20px 0 4px;font-size:12px;color:{_TEXT_MUTED};">{label}</p>
<p style="margin:0;font-size:12px;word-break:break-all;">
  <a href="{url}" style="color:{_BRAND_PURPLE};text-decoration:none;">{url}</a>
</p>"""


def _info_box(html_content: str, *, color: str = "#f5f3ff", border: str = "#ddd6fe") -> str:
    return f"""
<div style="margin-top:20px;padding:14px 18px;background:{color};
            border-left:3px solid {border};border-radius:0 6px 6px 0;
            font-size:14px;line-height:1.6;color:{_TEXT_MAIN};">
  {html_content}
</div>"""


def _quote_block(html_content: str) -> str:
    return f"""
<div style="margin-top:20px;padding:14px 18px;background:#f9fafb;
            border-left:3px solid {_BORDER};border-radius:0 6px 6px 0;
            font-size:14px;line-height:1.6;color:{_TEXT_MUTED};font-style:italic;">
  {html_content}
</div>"""


def _role_badge(role: str) -> str:
    colors = {
        "signer": (_BRAND_PURPLE, "#f5f3ff"),
        "reviewer": ("#059669", "#ecfdf5"),
        "viewer": ("#0369a1", "#eff6ff"),
    }
    fg, bg = colors.get(role, (_TEXT_MUTED, "#f3f4f6"))
    label = role.capitalize()
    return (
        f'<span style="display:inline-block;padding:3px 10px;border-radius:99px;'
        f'font-size:12px;font-weight:600;color:{fg};background:{bg};">{label}</span>'
    )


# ---------------------------------------------------------------------------
# Resend transport
# ---------------------------------------------------------------------------

def _send_sync(
    to: str,
    subject: str,
    html: str,
    *,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Synchronous Resend call — run in a thread via asyncio.to_thread()."""
    import resend  # lazy import so a missing package won't kill startup

    resend.api_key = settings.RESEND_API_KEY

    params: dict[str, Any] = {
        "from": f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM}>",
        "to": [to],
        "subject": subject,
        "html": html,
    }

    if attachments:
        params["attachments"] = [
            {
                "filename": fname,
                "content": base64.b64encode(data).decode(),
            }
            for fname, data, _mime in attachments
        ]

    api_key_hint = (settings.RESEND_API_KEY[:6] + "…") if settings.RESEND_API_KEY else "NOT SET"
    logger.info(f"Email | to={to!r} subject={subject!r} from={settings.EMAIL_FROM!r} api_key={api_key_hint}")
    try:
        result = resend.Emails.send(params)
        logger.info(f"Email sent OK | to={to!r} subject={subject!r} id={result.get('id') if isinstance(result, dict) else getattr(result, 'id', result)}")
    except Exception as exc:
        logger.error(f"Email FAILED | to={to!r} subject={subject!r} error={exc}")
        raise


async def _send(
    to: str,
    subject: str,
    html: str,
    *,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Async wrapper — offloads the blocking Resend HTTP call to a thread."""
    await asyncio.to_thread(_send_sync, to, subject, html, attachments=attachments)


# ---------------------------------------------------------------------------
# Public email functions
# ---------------------------------------------------------------------------

async def send_verification_email(email: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    _verify_note = _info_box(
        "<strong>⏱ This link expires in 24 hours.</strong><br/>"
        "If you didn't create an account, you can safely ignore this email."
    )
    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  Verify your email address
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  Welcome to {settings.EMAIL_FROM_NAME}! Just one more step — confirm your email
  address to activate your account and start signing documents in seconds.
</p>
{_cta_button("Verify Email Address", link)}
{_fallback_link(link)}
{_verify_note}
"""
    await _send(
        email,
        f"Verify your email — {settings.EMAIL_FROM_NAME}",
        _base_template(content, preheader="Confirm your email to activate your account"),
    )


async def send_password_reset_email(email: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    _reset_note = _info_box(
        "<strong>⏱ This link expires in 1 hour.</strong><br/>"
        "If you didn't request a password reset, please ignore this email — "
        "your password will remain unchanged.",
        color="#fff7ed",
        border="#fed7aa",
    )
    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  Reset your password
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  We received a request to reset your {settings.EMAIL_FROM_NAME} password.
  Click the button below to choose a new password.
</p>
{_cta_button("Reset Password", link)}
{_fallback_link(link)}
{_reset_note}
"""
    await _send(
        email,
        "Reset your password",
        _base_template(content, preheader="Reset your password — link expires in 1 hour"),
    )


async def send_signed_document_email(
    to: str,
    document_name: str,
    pdf_bytes: bytes,
    *,
    sender_name: str | None = None,
    subject: str | None = None,
    message: str | None = None,
) -> None:
    who = sender_name or "Someone"
    raw = (message or "").strip()
    if raw:
        body_inner = raw if "<" in raw else raw.replace("\n", "<br/>")
        personal_msg = _quote_block(body_inner)
    else:
        personal_msg = ""

    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  {who} sent you a signed document
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  The signed copy of <strong style="color:{_TEXT_MAIN};">{document_name}</strong>
  is attached to this email as a PDF.
</p>
{personal_msg}
{_info_box(
    f'📎&nbsp; <strong>{document_name}</strong> is attached to this email.',
    color="#f0fdf4",
    border="#bbf7d0",
)}
<p style="margin:28px 0 0;font-size:13px;color:{_TEXT_MUTED};line-height:1.5;">
  Sent via {settings.EMAIL_FROM_NAME} · legally binding e-signatures
</p>
"""
    final_subject = (subject or "").strip() or f"{who} sent you a signed document: {document_name}"
    await _send(
        to=to,
        subject=final_subject,
        html=_base_template(content, preheader=f"{who} sent you {document_name} — PDF attached"),
        attachments=[(document_name, pdf_bytes, "application/pdf")],
    )


async def send_document_shared_email(
    email: str,
    document_name: str,
    share_url: str,
    sender_name: str | None = None,
) -> None:
    who = sender_name or "Someone"
    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  {who} shared a document with you
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  You've been given access to view
  <strong style="color:{_TEXT_MAIN};">{document_name}</strong>.
  Click below to open it.
</p>
{_cta_button("View Document", share_url)}
{_fallback_link(share_url)}
<p style="margin:24px 0 0;font-size:13px;color:{_TEXT_MUTED};line-height:1.5;">
  If you weren't expecting this, you can safely ignore this email.
</p>
"""
    await _send(
        email,
        f"{who} shared a document with you: {document_name}",
        _base_template(content, preheader=f"{who} gave you access to {document_name}"),
    )


async def send_document_completed_email(
    email: str,
    document_name: str,
    download_url: str,
) -> None:
    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  Your document is ready 🎉
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  All parties have signed <strong style="color:{_TEXT_MAIN};">{document_name}</strong>.
  The fully executed copy is now available for download.
</p>
{_info_box(
    '✅&nbsp; <strong>All signatures collected.</strong> This document is legally binding.',
    color="#f0fdf4",
    border="#86efac",
)}
{_cta_button("Download Signed Document", download_url)}
{_fallback_link(download_url)}
"""
    await _send(
        email,
        f"Document ready: {document_name}",
        _base_template(content, preheader=f"{document_name} has been fully signed — download now"),
    )


async def send_collaboration_invite_email(
    *,
    email: str,
    document_name: str,
    sender_name: str,
    role: str,
    invite_url: str,
    personal_message: str | None = None,
) -> None:
    action = {
        "signer": "review and sign",
        "reviewer": "review and approve",
        "viewer": "view",
    }.get(role, "review")

    badge = _role_badge(role)

    msg_block = ""
    if personal_message:
        raw = personal_message.strip()
        body_inner = raw if "<" in raw else raw.replace("\n", "<br/>")
        msg_block = _quote_block(body_inner)

    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  You've been invited to {action} a document
</h1>
<p style="margin:0 0 16px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  <strong style="color:{_TEXT_MAIN};">{sender_name}</strong> has added you as
  {badge} on:
</p>

<!-- Document name pill -->
<div style="padding:14px 18px;background:#f9fafb;border:1px solid {_BORDER};
            border-radius:8px;font-size:15px;font-weight:600;color:{_TEXT_MAIN};">
  📄&nbsp; {document_name}
</div>

{msg_block}
{_cta_button("Open Document", invite_url)}
{_fallback_link(invite_url)}
{_info_box(
    "Don't have an account yet? The link above will walk you through creating one "
    "so you can access and sign the document.",
    color="#f5f3ff",
    border="#ddd6fe",
)}
"""
    await _send(
        to=email,
        subject=f"{sender_name} invited you to {action}: {document_name}",
        html=_base_template(
            content,
            preheader=f"{sender_name} needs your {role} on {document_name}",
        ),
    )


# ---------------------------------------------------------------------------
# Subscription / billing emails
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "GBP": "£", "EUR": "€",
    "NGN": "₦", "KES": "KSh", "GHS": "₵", "ZAR": "R",
}


def _fmt_amount(amount: Decimal | float | str, currency: str) -> str:
    sym = _CURRENCY_SYMBOLS.get(currency.upper())
    try:
        val = f"{Decimal(str(amount)):,.2f}"
    except Exception:
        val = str(amount)
    return f"{sym}{val}" if sym else f"{currency.upper()} {val}"


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%B %d, %Y")


async def send_subscription_activated_email(
    email: str,
    *,
    plan_name: str,
    amount: Decimal | float | str,
    currency: str,
    billing_period_end: datetime | None = None,
    invoice_url: str | None = None,
) -> None:
    """Sent when a payment succeeds and the subscription becomes active."""
    formatted_amount = _fmt_amount(amount, currency)
    renewal_line = (
        f"Your next renewal is on <strong>{_fmt_date(billing_period_end)}</strong>."
        if billing_period_end
        else "Your subscription is now active."
    )
    invoice_block = (
        f'<p style="margin:16px 0 0;font-size:14px;">'
        f'<a href="{invoice_url}" style="color:{_BRAND_PURPLE};text-decoration:none;font-weight:600;">'
        f"View invoice &rarr;</a></p>"
        if invoice_url
        else ""
    )
    dashboard_url = f"{settings.FRONTEND_URL}/dashboard"
    success_note = _info_box(
        f"✅&nbsp; <strong>Payment of {formatted_amount} received.</strong><br/>{renewal_line}",
        color="#f0fdf4",
        border="#86efac",
    )
    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  You're all set! 🎉
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  Your <strong style="color:{_TEXT_MAIN};">{plan_name}</strong> subscription is now active.
  Enjoy full access to all {settings.EMAIL_FROM_NAME} features.
</p>
{success_note}
{invoice_block}
{_cta_button("Go to Dashboard", dashboard_url)}
<p style="margin:24px 0 0;font-size:13px;color:{_TEXT_MUTED};line-height:1.5;">
  Questions about your bill? Reply to this email and we'll sort it out.
</p>
"""
    await _send(
        email,
        f"Payment confirmed — {plan_name} is active",
        _base_template(content, preheader=f"Your {plan_name} subscription is now active"),
    )


async def send_payment_failed_email(
    email: str,
    *,
    plan_name: str,
    amount: Decimal | float | str,
    currency: str,
    update_url: str,
    invoice_url: str | None = None,
) -> None:
    """Sent when a payment attempt fails."""
    formatted_amount = _fmt_amount(amount, currency)
    invoice_block = (
        f'<p style="margin:12px 0 0;font-size:14px;">'
        f'<a href="{invoice_url}" style="color:{_BRAND_PURPLE};text-decoration:none;font-weight:600;">'
        f"View invoice &rarr;</a></p>"
        if invoice_url
        else ""
    )
    failed_note = _info_box(
        f"⚠️&nbsp; <strong>Payment of {formatted_amount} for {plan_name} could not be processed.</strong><br/>"
        "Please update your payment method to keep uninterrupted access to your account.",
        color="#fff7ed",
        border="#fed7aa",
    )
    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  Payment unsuccessful
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  We were unable to charge your card for your
  <strong style="color:{_TEXT_MAIN};">{plan_name}</strong> subscription.
  Update your payment details to avoid any interruption to your service.
</p>
{failed_note}
{invoice_block}
{_cta_button("Update Payment Method", update_url)}
{_info_box(
    "Your account remains accessible for a short grace period. "
    "If payment is not resolved, access to paid features may be suspended.",
    color="#fef2f2",
    border="#fca5a5",
)}
"""
    await _send(
        email,
        f"Action required: payment failed for {plan_name}",
        _base_template(content, preheader=f"Your {plan_name} payment failed — update your card"),
    )


async def send_subscription_expiring_email(
    email: str,
    *,
    plan_name: str,
    days_left: int,
    expires_at: datetime,
    renew_url: str,
    is_trial: bool = False,
) -> None:
    """Sent 7 days and 3 days before a subscription (or trial) expires."""
    days_label = f"{days_left} day" + ("s" if days_left != 1 else "")
    expires_label = _fmt_date(expires_at)
    plan_label = "free trial" if is_trial else f"{plan_name} subscription"

    urgency_color = "#fef2f2" if days_left <= 3 else "#fff7ed"
    urgency_border = "#fca5a5" if days_left <= 3 else "#fed7aa"
    urgency_icon = "🔴" if days_left <= 3 else "🟡"

    expiry_note = _info_box(
        f"{urgency_icon}&nbsp; <strong>Your {plan_label} expires on {expires_label} "
        f"({days_label} left).</strong><br/>"
        + (
            "Upgrade now to keep access to all features without interruption."
            if is_trial
            else "Renew now to keep uninterrupted access to your account."
        ),
        color=urgency_color,
        border=urgency_border,
    )
    headline = (
        "Your free trial is ending soon"
        if is_trial
        else f"Your {plan_name} subscription expires in {days_label}"
    )
    cta_label = "Upgrade Now" if is_trial else "Renew Subscription"
    content = f"""
<h1 style="margin:0 0 8px;font-size:24px;font-weight:700;color:{_TEXT_MAIN};line-height:1.2;">
  {headline}
</h1>
<p style="margin:0 0 6px;font-size:15px;color:{_TEXT_MUTED};line-height:1.6;">
  {"You have " + days_label + " left on your free trial of " if is_trial else "Your "}
  <strong style="color:{_TEXT_MAIN};">{plan_name}</strong>
  {" plan." if not is_trial else "."} After <strong>{expires_label}</strong>,
  {"you'll lose access to paid features." if is_trial else "your access will not be renewed."}
</p>
{expiry_note}
{_cta_button(cta_label, renew_url)}
<p style="margin:24px 0 0;font-size:13px;color:{_TEXT_MUTED};line-height:1.5;">
  Need help choosing a plan? Reply to this email and we'll help you find the right fit.
</p>
"""
    subject = (
        f"Your free trial expires in {days_label}"
        if is_trial
        else f"Your {plan_name} subscription expires in {days_label}"
    )
    await _send(
        email,
        subject,
        _base_template(content, preheader=f"{days_label} left — renew your {plan_label}"),
    )

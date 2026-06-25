"""Async SMTP email sender. Mirrors the NestJS EmailService templates."""
from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib
from loguru import logger

from app.core.config import settings


async def _send(
    to: str,
    subject: str,
    html: str,
    *,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send an email. `attachments` is a list of (filename, bytes, mime_type)."""
    msg = EmailMessage()
    msg["From"] = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("This email requires an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    if attachments:
        for filename, data, mime_type in attachments:
            maintype, _, subtype = mime_type.partition("/")
            msg.add_attachment(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=filename,
            )

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=settings.SMTP_USE_TLS,
        )
    except Exception as exc:
        # Don't fail the request when SMTP is unavailable in dev.
        logger.warning(f"Email to {to} failed: {exc}")
        raise  # let the caller decide whether to surface the error to the user


async def send_signed_document_email(
    to: str,
    document_name: str,
    pdf_bytes: bytes,
    *,
    sender_name: str | None = None,
    subject: str | None = None,
    message: str | None = None,
) -> None:
    """Send a signed PDF to someone via email.

    `message` may contain HTML (from the rich text editor on the frontend) —
    in that case we embed it verbatim inside a styled container. We detect
    "is this HTML?" by looking for `<` anywhere in the string; anything else
    is treated as plain text and gets its newlines converted to <br/>.
    """
    who = sender_name or "Someone"
    raw = (message or "").strip()
    if "<" in raw:
        # Trust frontend Tiptap output. The editor's allow-list (bold,
        # italic, lists, links, headings, blockquote, strike) is intentionally
        # email-safe — no scripts, no inline event handlers can be produced.
        body_block = raw
    else:
        body_block = raw.replace("\n", "<br/>")

    body = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#222">
      <h2 style="margin-bottom:4px">{who} shared a signed document with you</h2>
      <p style="color:#555;margin-top:0"><strong>{document_name}</strong> is attached.</p>
      {f'<div style="margin-top:16px;padding:14px 16px;background:#f5f5f7;border-radius:10px;line-height:1.5">{body_block}</div>' if body_block else ''}
      <p style="color:#888;font-size:12px;margin-top:24px">
        Sent via {settings.EMAIL_FROM_NAME}.
      </p>
    </div>
    """
    final_subject = (subject or "").strip() or f"Signed document: {document_name}"
    await _send(
        to=to,
        subject=final_subject,
        html=body,
        attachments=[(document_name, pdf_bytes, "application/pdf")],
    )


async def send_verification_email(email: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    html = f"""
    <h2>Welcome to {settings.EMAIL_FROM_NAME}</h2>
    <p>Confirm your email by clicking the link below:</p>
    <p><a href="{link}">Verify Email</a></p>
    <p>If you didn't sign up, ignore this email.</p>
    """
    await _send(email, "Verify your email", html)


async def send_password_reset_email(email: str, token: str) -> None:
    link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    html = f"""
    <h2>Password reset</h2>
    <p>Click the link below to reset your password. It expires in 1 hour.</p>
    <p><a href="{link}">Reset password</a></p>
    """
    await _send(email, "Reset your password", html)


async def send_document_shared_email(
    email: str, document_name: str, share_url: str, sender_name: str | None = None
) -> None:
    who = sender_name or "Someone"
    html = f"""
    <h2>{who} shared a document with you</h2>
    <p><strong>{document_name}</strong></p>
    <p><a href="{share_url}">View document</a></p>
    """
    await _send(email, f"{who} shared a document with you", html)


async def send_document_completed_email(
    email: str, document_name: str, download_url: str
) -> None:
    html = f"""
    <h2>Document completed</h2>
    <p><strong>{document_name}</strong> has been signed and completed.</p>
    <p><a href="{download_url}">Download signed document</a></p>
    """
    await _send(email, "Your document is ready", html)


async def send_collaboration_invite_email(
    *,
    email: str,
    document_name: str,
    sender_name: str,
    role: str,
    invite_url: str,
    personal_message: str | None = None,
) -> None:
    """Notify someone they've been added as a signer/reviewer/viewer.

    `invite_url` is the magic-link URL the recipient uses to access the
    document (Phase D wires up token-based guest access; for now the link
    just opens the app's accept page, which logs them in if they have an
    account or prompts them to register).
    """
    action_by_role = {
        "signer": "review and sign",
        "reviewer": "review and approve",
        "viewer": "view",
    }.get(role, "review")
    msg_block = ""
    if personal_message:
        raw = personal_message.strip()
        body_inner = raw if "<" in raw else raw.replace("\n", "<br/>")
        msg_block = (
            f'<div style="margin-top:16px;padding:14px 16px;background:#f5f5f7;'
            f'border-radius:10px;line-height:1.5;font-style:italic;">'
            f'{body_inner}</div>'
        )
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#222">
      <h2 style="margin-bottom:4px">{sender_name} invited you to {action_by_role} a document</h2>
      <p style="color:#555;margin-top:0">
        <strong>{document_name}</strong>
      </p>
      {msg_block}
      <p style="margin-top:24px">
        <a href="{invite_url}"
           style="background:#7c3aed;color:#fff;padding:12px 20px;border-radius:8px;
                  text-decoration:none;font-weight:600;display:inline-block">
          Open document
        </a>
      </p>
      <p style="color:#888;font-size:12px;margin-top:24px">
        If you don't have an account, the link will help you create one.
        Sent via {settings.EMAIL_FROM_NAME}.
      </p>
    </div>
    """
    await _send(
        to=email,
        subject=f"{sender_name} shared a document with you: {document_name}",
        html=html,
    )

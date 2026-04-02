"""File upload handling, FileRef creation, and FileRef resolution."""

from __future__ import annotations

import base64
import logging
from typing import Any, Literal

import httpx
from fastapi import UploadFile

from evalforge_runtime.storage import LocalStorage
from evalforge_runtime.types import FileRef

logger = logging.getLogger(__name__)

# Extensions whose content is automatically read as text during FileRef resolution.
# For these files, the resolved dict will include a "content" field with the text.
# Binary files (PDF, images, etc.) only get a "path" — read them in execution.py.
TEXT_EXTENSIONS: set[str] = {
    # Plain text
    "txt", "text", "log", "ini", "cfg", "conf",
    # Email
    "eml",
    # Data / markup
    "csv", "tsv", "json", "jsonl", "xml", "yaml", "yml", "toml",
    # Web / docs
    "html", "htm", "xhtml", "md", "markdown", "rst", "tex",
    # Code (common)
    "py", "js", "ts", "java", "c", "cpp", "h", "cs", "rb", "go", "rs", "sh", "bat", "ps1", "sql",
}


def _decode_eml(content: bytes) -> str:
    """Parse an .eml file and return a human-readable text representation.

    Decodes MIME parts (base64, quoted-printable, etc.) so execution code
    receives plain text rather than the raw MIME envelope.
    """
    import email as _email

    msg = _email.message_from_bytes(content)
    subject = msg.get("Subject", "")
    sender = msg.get("From", "")
    recipient = msg.get("To", "")
    date = msg.get("Date", "")

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        else:
            body = msg.get_payload() or ""

    lines = []
    if sender:
        lines.append(f"From: {sender}")
    if recipient:
        lines.append(f"To: {recipient}")
    if date:
        lines.append(f"Date: {date}")
    if subject:
        lines.append(f"Subject: {subject}")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _is_text_file(filename: str, mime_type: str) -> bool:
    """Determine if a file should be auto-read as text."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in TEXT_EXTENSIONS:
        return True
    # Also check MIME type for anything text/*
    if mime_type.startswith("text/") or mime_type in ("application/json", "application/xml", "message/rfc822"):
        return True
    return False


async def process_uploaded_file(
    file: UploadFile,
    execution_id: str,
    storage: LocalStorage,
    direction: Literal["input", "output"] = "input",
) -> FileRef:
    """Process an uploaded file: store it and return a FileRef."""
    content = await file.read()
    filename = file.filename or "unnamed"
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    mime_type = file.content_type or "application/octet-stream"
    key = f"executions/{execution_id}/{direction}/{filename}"

    await storage.put(key, content, mime_type)

    return FileRef(
        type="local",
        key=key,
        filename=filename,
        size=len(content),
        mimeType=mime_type,
        extension=extension,
    )


def is_file_ref(value: Any) -> bool:
    """Check if a value looks like a FileRef dict."""
    if not isinstance(value, dict):
        return False
    return (
        "type" in value
        and "key" in value
        and "filename" in value
        and value.get("type") in ("local", "s3", "url")
    )


async def resolve_file_refs(
    obj: Any,
    execution_id: str,
    storage: LocalStorage,
) -> Any:
    """Walk an input dict and resolve FileRefs.

    For each FileRef found:
    - If it has ``data`` (base64): decode and store locally
    - Elif it has ``url``: download and store locally
    - Otherwise: leave as-is (already a local ref)

    The FileRef is replaced with a simplified dict containing ``filename``,
    ``path`` (local storage path), ``mimeType``, ``extension``, and ``size``.
    For text files (.eml, .txt, .csv, .json, .xml, etc.), a ``content`` field
    is added with the decoded text so execution.py can use it directly.
    Binary files only get ``path`` — use a library to read them.
    """
    if isinstance(obj, list):
        return [await resolve_file_refs(item, execution_id, storage) for item in obj]
    if not isinstance(obj, dict):
        return obj

    if is_file_ref(obj):
        return await _resolve_single(obj, execution_id, storage)

    result: dict[str, Any] = {}
    for key, value in obj.items():
        result[key] = await resolve_file_refs(value, execution_id, storage)
    return result


async def _resolve_single(
    ref: dict[str, Any],
    execution_id: str,
    storage: LocalStorage,
) -> dict[str, Any]:
    """Resolve a single FileRef dict into a local file."""
    filename = ref.get("filename", "unnamed")
    mime_type = ref.get("mimeType", "application/octet-stream")
    extension = ref.get("extension", "")
    local_key = f"executions/{execution_id}/input/{filename}"

    content: bytes | None = None

    # Priority 1: base64 data embedded in the ref (sent by EvalForge executor)
    if ref.get("data"):
        try:
            content = base64.b64decode(ref["data"])
        except Exception:
            logger.warning("Failed to decode base64 data for %s", filename)

    # Priority 2: download from URL
    if content is None and ref.get("url"):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(ref["url"])
                resp.raise_for_status()
                content = resp.content
        except Exception:
            logger.warning("Failed to download file from %s", ref.get("url"))

    # Priority 3: file already exists in local storage (same runtime)
    if content is None and ref.get("key"):
        try:
            content = await storage.get(ref["key"])
        except FileNotFoundError:
            pass

    if content is None:
        logger.warning("Could not resolve FileRef for %s — leaving as metadata", filename)
        return {
            "filename": filename,
            "mimeType": mime_type,
            "extension": extension,
            "size": ref.get("size", 0),
            "_unresolved": True,
        }

    # Store locally
    await storage.put(local_key, content, mime_type)
    local_path = str(storage.base_path / local_key)

    result: dict[str, Any] = {
        "filename": filename,
        "path": local_path,
        "mimeType": mime_type,
        "extension": extension,
        "size": len(content),
    }

    # Auto-read text content so execution.py doesn't have to
    if _is_text_file(filename, mime_type):
        try:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext == "eml" or mime_type == "message/rfc822":
                result["content"] = _decode_eml(content)
            else:
                result["content"] = content.decode("utf-8", errors="replace")
        except Exception:
            logger.debug("Could not decode %s as text, skipping content field", filename)

    return result

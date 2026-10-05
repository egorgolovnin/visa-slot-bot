"""Strip identifying metadata from files before they reach a client.

PDF: drop the document info dictionary (Author, Creator, Producer, Title...) and XMP metadata.
Images: re-encode without EXIF (camera, phone model, GPS). Everything gets a neutral file name.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger("sanitize")


def clean_pdf(data: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer._info = None                                 # no /Info dictionary at all
    root = writer._root_object
    for key in ("/Metadata", "/PieceInfo"):
        if NameObject(key) in root:
            del root[NameObject(key)]
    for page in writer.pages:                           # page-level XMP / app data
        for key in ("/Metadata", "/PieceInfo"):
            if NameObject(key) in page:
                del page[NameObject(key)]
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def clean_image(data: bytes) -> tuple[bytes, str]:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    fmt = "PNG" if img.format == "PNG" else "JPEG"
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))                  # pixels only, no EXIF/ICC/text chunks
    out = io.BytesIO()
    clean.save(out, format=fmt, quality=95)
    return out.getvalue(), "png" if fmt == "PNG" else "jpg"


def clean_file(data: bytes, mime: str | None, filename: str | None, base_name: str) -> tuple[bytes, str]:
    """Returns (cleaned bytes, neutral file name)."""
    mime = (mime or "").lower()
    ext = (filename or "").rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    try:
        if mime == "application/pdf" or ext == "pdf" or data[:5] == b"%PDF-":
            return clean_pdf(data), f"{base_name}.pdf"
        if mime.startswith("image/") or ext in ("jpg", "jpeg", "png", "webp", "heic"):
            cleaned, new_ext = clean_image(data)
            return cleaned, f"{base_name}.{new_ext}"
    except Exception:
        log.exception("could not clean %s, sending with neutral name only", filename)
    return data, f"{base_name}.{ext or 'bin'}"

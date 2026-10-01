"""Barcodes and QR codes: validate, store, render to PNG/SVG, and decode from
images (pyzbar / libzbar)."""
from __future__ import annotations

import io
import re

import barcode
import qrcode
import qrcode.image.svg
from barcode.writer import ImageWriter, SVGWriter
from PIL import Image, ImageOps, UnidentifiedImageError

from . import validation as v
from .db import transaction
from .repo import _delete, _like, _page, _update, one, rows
from .validation import NotFound, ValidationError

KINDS = {
    "qr": "QR code",
    "ean13": "EAN-13",
    "ean8": "EAN-8",
    "upca": "UPC-A",
    "code128": "Code 128",
    "code39": "Code 39",
    "isbn13": "ISBN-13",
}
# python-barcode class names
_BARCODE_CLS = {"ean13": "ean13", "ean8": "ean8", "upca": "upca", "code128": "code128", "code39": "code39", "isbn13": "isbn13"}
# pyzbar symbology -> our kind
_ZBAR_KIND = {"QRCODE": "qr", "EAN13": "ean13", "EAN8": "ean8", "UPCA": "upca", "CODE128": "code128",
              "CODE39": "code39", "ISBN13": "isbn13"}


def _gs1_check_digit(digits: str) -> str:
    """Mod-10 check digit used by EAN/UPC: weights 3,1,3,1... from the right."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def normalize_payload(kind: str, payload: str) -> str:
    """Validate a payload for its symbology and return the canonical form
    (check digit appended/verified for the numeric ones)."""
    if kind not in KINDS:
        raise ValidationError({"kind": f"must be one of {', '.join(KINDS)}"})
    p = (payload or "").strip()
    if not p:
        raise ValidationError({"payload": "is required"})
    lengths = {"ean13": 13, "ean8": 8, "upca": 12, "isbn13": 13}
    if kind in lengths:
        p = re.sub(r"[\s-]", "", p)
        n = lengths[kind]
        if not p.isdigit() or len(p) not in (n - 1, n):
            raise ValidationError({"payload": f"{KINDS[kind]} needs {n - 1} or {n} digits"})
        if kind == "isbn13" and not p.startswith(("978", "979")):
            raise ValidationError({"payload": "ISBN-13 must start with 978 or 979"})
        if len(p) == n - 1:
            p += _gs1_check_digit(p)
        elif _gs1_check_digit(p[:-1]) != p[-1]:
            raise ValidationError({"payload": f"bad check digit (expected {_gs1_check_digit(p[:-1])})"})
    elif kind == "code39":
        p = p.upper()
        if not re.fullmatch(r"[0-9A-Z \-.$/+%]+", p):
            raise ValidationError({"payload": "Code 39 allows 0-9, A-Z, space and - . $ / + %"})
    elif kind == "code128":
        if not all(32 <= ord(c) < 127 for c in p):
            raise ValidationError({"payload": "Code 128 supports printable ASCII only"})
        if len(p) > 80:
            raise ValidationError({"payload": "is too long for a Code 128 barcode (80 max)"})
    elif kind == "qr" and len(p.encode()) > 2900:
        raise ValidationError({"payload": "is too long for a QR code"})
    return p


def render(kind: str, payload: str, fmt: str = "png", scale: int = 10) -> tuple[bytes, str]:
    if kind == "qr":
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=max(2, min(scale, 40)), border=3)
        q.add_data(payload)
        q.make(fit=True)
        if fmt == "svg":
            buf = io.BytesIO()
            q.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
            return buf.getvalue(), "image/svg+xml"
        buf = io.BytesIO()
        q.make_image(fill_color="black", back_color="white").save(buf, "PNG")
        return buf.getvalue(), "image/png"
    cls = barcode.get_barcode_class(_BARCODE_CLS[kind])
    # python-barcode wants the payload without the check digit for EAN/UPC/ISBN
    data = payload[:-1] if kind in ("ean13", "ean8", "upca", "isbn13") else payload
    kwargs = {"add_checksum": False} if kind == "code39" else {}
    writer = SVGWriter() if fmt == "svg" else ImageWriter()
    buf = io.BytesIO()
    cls(data, writer=writer, **kwargs).write(buf, options={"module_width": 0.3, "module_height": 15, "font_size": 9,
                                                           "quiet_zone": 4, "text_distance": 4, "dpi": 254})
    # 0.3 mm at 254 dpi = exactly 3 px per module: crisp bars that scanners can read
    return buf.getvalue(), ("image/svg+xml" if fmt == "svg" else "image/png")


def decode_image(data: bytes) -> list[dict]:
    """Find every barcode/QR in an image. Tries the image as-is, then a
    grayscale/upscaled pass, then rotated passes — phone photos are messy."""
    from pyzbar import pyzbar  # imported lazily: needs the libzbar shared library

    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    except (UnidentifiedImageError, OSError):
        raise ValidationError({"file": "is not a readable image"}) from None
    gray = img.convert("L")
    attempts = [img.convert("RGB"), gray, ImageOps.autocontrast(gray)]
    if max(gray.size) < 800:
        attempts.append(gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS))
    attempts += [gray.rotate(a, expand=True) for a in (90, 45, -45)]
    found = {}
    for candidate in attempts:
        for sym in pyzbar.decode(candidate):
            text = sym.data.decode("utf-8", errors="replace")
            kind = _ZBAR_KIND.get(sym.type)
            if kind == "ean13" and text.startswith(("978", "979")):
                kind = "isbn13"
            elif kind == "ean13" and text.startswith("0"):  # zbar reports UPC-A as 0-prefixed EAN-13
                kind, text = "upca", text[1:]
            found.setdefault((sym.type, text), {"symbology": sym.type, "kind": kind, "payload": text,
                                                "rect": list(sym.rect)})
        if found:
            break
    return list(found.values())


def describe_payload(kind: str, payload: str) -> dict:
    """Light interpretation for the UI: what a QR payload *is*."""
    info = {"type": "text"}
    if kind == "qr":
        if re.match(r"^https?://", payload, re.I):
            info = {"type": "url", "url": payload}
        elif payload.startswith("WIFI:"):
            parts = dict(re.findall(r"([A-Z]):((?:\\.|[^;])*)", payload[5:]))
            info = {"type": "wifi", "ssid": parts.get("S", ""), "security": parts.get("T", ""), "hidden": parts.get("H") == "true"}
        elif payload.startswith("BEGIN:VCARD"):
            fn = re.search(r"^FN:(.*)$", payload, re.M)
            info = {"type": "vcard", "name": fn.group(1).strip() if fn else ""}
        elif payload.lower().startswith("mailto:"):
            info = {"type": "email", "email": payload[7:]}
        elif payload.lower().startswith("tel:"):
            info = {"type": "phone", "phone": payload[4:]}
    elif kind in ("ean13", "upca", "ean8"):
        info = {"type": "product", "gtin": payload}
    elif kind == "isbn13":
        info = {"type": "book", "isbn": payload}
    return info


def wifi_payload(ssid: str, password: str = "", security: str = "WPA", hidden: bool = False) -> str:
    esc = lambda s: re.sub(r'([\\;,:"])', r"\\\1", s)  # noqa: E731
    sec = security if security in ("WPA", "WEP", "nopass") else "WPA"
    return f"WIFI:T:{sec};S:{esc(ssid)};P:{esc(password)};H:{'true' if hidden else 'false'};;"


# ------------------------------------------------------------------- repo

def _out(d: dict) -> dict:
    d["kind_label"] = KINDS.get(d["kind"], d["kind"])
    d["info"] = describe_payload(d["kind"], d["payload"])
    return d


def list_codes(conn, *, q="", kind="", limit=100, offset=0) -> dict:
    limit, offset = _page(limit, offset)
    where, params = [], {}
    if q:
        where.append("(payload LIKE :q ESCAPE '\\' OR label LIKE :q ESCAPE '\\' OR notes LIKE :q ESCAPE '\\')")
        params["q"] = _like(q)
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM codes {w}", params).fetchone()[0]
    items = rows(conn.execute(f"SELECT * FROM codes {w} ORDER BY updated_at DESC, id DESC LIMIT {limit} OFFSET {offset}", params))
    return {"items": [_out(i) for i in items], "total": total, "limit": limit, "offset": offset}


def get_code(conn, code_id: int) -> dict:
    d = one(conn, "SELECT * FROM codes WHERE id = ?", (code_id,))
    if not d:
        raise NotFound(f"code {code_id} not found")
    return _out(d)


def save_code(conn, data: dict) -> tuple[dict, bool]:
    """Insert, or — if this exact (kind, payload) already exists — bump its
    scan counter and return it (an UPSERT keyed on the UNIQUE constraint)."""
    kind = (data.get("kind") or "qr").lower()
    payload = normalize_payload(kind, data.get("payload", ""))
    source = data.get("source") or "generated"
    if source not in ("generated", "scanned", "imported"):
        raise ValidationError({"source": "invalid"})
    with transaction(conn):
        existed = conn.execute("SELECT id FROM codes WHERE kind = ? AND payload = ?", (kind, payload)).fetchone()
        row = conn.execute("""
            INSERT INTO codes (kind, payload, label, notes, source, scan_count)
            VALUES (:kind, :payload, :label, :notes, :source, :sc)
            ON CONFLICT(kind, payload) DO UPDATE SET
                scan_count = codes.scan_count + excluded.scan_count,
                label = CASE WHEN codes.label = '' THEN excluded.label ELSE codes.label END
            RETURNING id""", {"kind": kind, "payload": payload, "label": v.text(200)(data.get("label")),
                              "notes": v.text(5000)(data.get("notes")), "source": source,
                              "sc": 1 if source == "scanned" else 0}).fetchone()
    return get_code(conn, row[0]), existed is None


def update_code(conn, code_id: int, data: dict) -> dict:
    clean = {}
    for k, n in (("label", 200), ("notes", 5000)):
        if k in data:
            clean[k] = v.text(n)(data[k])
    with transaction(conn):
        _update(conn, "codes", code_id, clean) if clean else get_code(conn, code_id)
    return get_code(conn, code_id)


def delete_code(conn, code_id: int) -> None:
    with transaction(conn):
        _delete(conn, "codes", code_id)

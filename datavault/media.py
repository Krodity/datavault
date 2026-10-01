"""Picture ingestion: content-addressed storage, EXIF extraction, thumbnails."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from .config import Config
from .db import transaction
from .repo import _delete, get_picture
from .validation import ValidationError

ALLOWED = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif"}
MAX_BYTES = 40 * 1024 * 1024
THUMB_PX = 480
Image.MAX_IMAGE_PIXELS = 120_000_000  # refuse decompression bombs


def _exif(img: Image.Image) -> tuple[str | None, str | None]:
    try:
        exif = img.getexif()
    except Exception:
        return None, None
    tags = {ExifTags.TAGS.get(k, k): val for k, val in exif.items()}
    sub = exif.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {}
    taken = sub.get(36867) or tags.get("DateTime")  # DateTimeOriginal, else DateTime
    taken_iso = None
    if isinstance(taken, str) and len(taken) >= 10:
        taken_iso = taken[:10].replace(":", "-")
    camera = " ".join(str(tags[k]).strip("\x00 ") for k in ("Make", "Model") if tags.get(k)) or None
    return taken_iso, camera


def media_path(cfg: Config, stored_name: str) -> Path:
    return cfg.media_dir / stored_name[:2] / stored_name


def thumb_path(cfg: Config, sha: str) -> Path:
    return cfg.thumbs_dir / f"{sha}.jpg"


def ingest(conn, cfg: Config, data: bytes, original_name: str, meta: dict | None = None) -> tuple[dict, bool]:
    """Store an image. Returns (picture, created). Identical bytes are
    de-duplicated by SHA-256 and return the existing row."""
    if len(data) > MAX_BYTES:
        raise ValidationError({"file": f"is larger than {MAX_BYTES // 1024 // 1024} MB"})
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()                      # cheap structural check
        img = Image.open(io.BytesIO(data))  # verify() leaves the object unusable
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ValidationError({"file": f"is not a supported image ({e.__class__.__name__})"}) from None
    if img.format not in ALLOWED:
        raise ValidationError({"file": f"format {img.format} is not allowed"})

    sha = hashlib.sha256(data).hexdigest()
    existing = conn.execute("SELECT id FROM pictures WHERE sha256 = ?", (sha,)).fetchone()
    if existing:
        return get_picture(conn, existing[0]), False

    stored = f"{sha}.{ALLOWED[img.format]}"
    path = media_path(cfg, stored)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)  # atomic on the same filesystem

    taken, camera = _exif(img)
    thumb = ImageOps.exif_transpose(img)
    thumb.thumbnail((THUMB_PX, THUMB_PX))
    if thumb.mode not in ("RGB", "L"):
        bg = Image.new("RGB", thumb.size, (255, 255, 255))
        rgba = thumb.convert("RGBA")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        thumb = bg
    thumb.save(thumb_path(cfg, sha), "JPEG", quality=82, optimize=True)
    w, h = ImageOps.exif_transpose(img).size

    meta = meta or {}
    with transaction(conn):
        pid = conn.execute("""
            INSERT INTO pictures (sha256, stored_name, original_name, mime, size_bytes, width, height,
                                  taken_at, camera, title, description, album)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sha, stored, Path(original_name).name[:255] or stored, Image.MIME.get(img.format, "image/*"),
             len(data), w, h, meta.get("taken_at") or taken, camera,
             (meta.get("title") or "").strip()[:200], (meta.get("description") or "").strip()[:5000],
             (meta.get("album") or "").strip()[:100])).lastrowid
    return get_picture(conn, pid), True


def delete_picture(conn, cfg: Config, pic_id: int) -> None:
    pic = get_picture(conn, pic_id)
    with transaction(conn):
        _delete(conn, "pictures", pic_id)  # FKs null out contact photos / receipts
    for p in (media_path(cfg, pic["stored_name"]), thumb_path(cfg, pic["sha256"])):
        p.unlink(missing_ok=True)


def verify_media(conn, cfg: Config, repair_thumbs: bool = True) -> dict:
    """Integrity sweep: DB rows whose file is missing, files with no row, and
    files whose hash no longer matches."""
    missing, corrupt, rebuilt = [], [], 0
    known = set()
    for r in conn.execute("SELECT id, sha256, stored_name FROM pictures"):
        p = media_path(cfg, r["stored_name"])
        known.add(p.name)
        if not p.exists():
            missing.append(r["id"])
            continue
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != r["sha256"]:
            corrupt.append(r["id"])
        elif repair_thumbs and not thumb_path(cfg, r["sha256"]).exists():
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
            img.thumbnail((THUMB_PX, THUMB_PX))
            img.convert("RGB").save(thumb_path(cfg, r["sha256"]), "JPEG", quality=82)
            rebuilt += 1
    orphans = [str(p.relative_to(cfg.media_dir)) for p in cfg.media_dir.rglob("*") if p.is_file() and p.name not in known]
    return {"missing_files": missing, "hash_mismatch": corrupt, "orphan_files": orphans, "thumbs_rebuilt": rebuilt}

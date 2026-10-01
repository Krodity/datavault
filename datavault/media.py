"""Picture ingestion: content-addressed storage, EXIF extraction, thumbnails."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from .config import Config
from .db import transaction
from . import validation as v
from .repo import _delete, get_picture
from .validation import ValidationError

try:  # optional: iPhone HEIC/HEIF photos
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the environment
    HEIF_SUPPORTED = False

# Pillow format -> stored extension. Most phone/camera JPEGs open as "MPO"
# (multi-picture JPEG with an embedded preview), so it must be accepted too.
ALLOWED = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif",
           **({"HEIF": "heic"} if HEIF_SUPPORTED else {})}
MIME = {"MPO": "image/jpeg", "HEIF": "image/heic"}
# formats every browser can display; anything else gets a JPEG "view" copy
BROWSER_SAFE = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
VIEW_PX = 2560
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
        try:  # cameras write "0000:00:00 00:00:00" when the clock was never set
            taken_iso = v.iso_date(taken[:10].replace(":", "-"))
        except ValueError:
            taken_iso = None
    camera = " ".join(str(tags[k]).strip("\x00 ") for k in ("Make", "Model") if tags.get(k))[:100] or None
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

    meta = v.PICTURE_META.clean({k: (meta or {}).get(k) for k in ("title", "description", "album", "taken_at")
                                 if (meta or {}).get(k) not in (None, "")}, partial=True)
    sha = hashlib.sha256(data).hexdigest()
    existing = conn.execute("SELECT id FROM pictures WHERE sha256 = ?", (sha,)).fetchone()
    if existing:
        return get_picture(conn, existing[0]), False

    # Decode fully *before* touching the disk: a truncated file can pass
    # verify() and only fail here, and we don't want to leave an orphan behind.
    try:
        taken, camera = _exif(img)
        oriented = ImageOps.exif_transpose(img)
        w, h = oriented.size
        thumb = _thumbnail(oriented)
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise ValidationError({"file": f"could not be decoded ({e.__class__.__name__})"}) from None

    stored = f"{sha}.{ALLOWED[img.format]}"
    path = media_path(cfg, stored)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)  # atomic on the same filesystem
    thumb.save(thumb_path(cfg, sha), "JPEG", quality=82, optimize=True)

    try:
        with transaction(conn):
            row = conn.execute("""
                INSERT INTO pictures (sha256, stored_name, original_name, mime, size_bytes, width, height,
                                      taken_at, camera, title, description, album)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING RETURNING id""",
                (sha, stored, Path(original_name or "").name[:255] or stored, MIME.get(img.format) or Image.MIME.get(img.format, "image/*"),
                 len(data), w, h, meta.get("taken_at") or taken, camera, meta.get("title", ""),
                 meta.get("description", ""), meta.get("album", ""))).fetchone()
    except BaseException:
        # the files are content-addressed, so only remove them if no row owns them
        if not conn.execute("SELECT 1 FROM pictures WHERE sha256 = ?", (sha,)).fetchone():
            path.unlink(missing_ok=True)
            thumb_path(cfg, sha).unlink(missing_ok=True)
        raise
    if row is None:  # a concurrent upload of the same bytes won the race
        return get_picture(conn, conn.execute("SELECT id FROM pictures WHERE sha256 = ?", (sha,)).fetchone()[0]), False
    return get_picture(conn, row[0]), True


def _thumbnail(img: Image.Image) -> Image.Image:
    thumb = img.copy()
    thumb.thumbnail((THUMB_PX, THUMB_PX))
    return _thumbnail_mode(thumb)


def _thumbnail_mode(thumb: Image.Image) -> Image.Image:
    """Convert any mode to something JPEG can store (flattening alpha onto white)."""
    if thumb.mode in ("RGB", "L"):
        return thumb
    if thumb.mode in ("RGBA", "LA", "PA") or (thumb.mode == "P" and "transparency" in thumb.info):
        rgba = thumb.convert("RGBA")
        bg = Image.new("RGB", thumb.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    if thumb.mode.startswith("I;16") or thumb.mode in ("I", "F"):  # 16-bit/float PNG/TIFF
        thumb = thumb.point(lambda x: x * (1 / 256)).convert("L")
    return thumb.convert("RGB")


def view_path(cfg: Config, sha: str) -> Path:
    return cfg.thumbs_dir / f"{sha}.view.jpg"


def ensure_thumb(cfg: Config, pic: dict) -> Path | None:
    """Thumbnails are derived data: rebuild one on demand if it's missing
    (e.g. after a restore, since backups carry originals only)."""
    t = thumb_path(cfg, pic["sha256"])
    if t.exists():
        return t
    src = media_path(cfg, pic["stored_name"])
    if not src.exists():
        return None
    try:
        _thumbnail(ImageOps.exif_transpose(Image.open(src))).save(t, "JPEG", quality=82, optimize=True)
    except (OSError, ValueError):
        return None
    return t


def ensure_view(cfg: Config, pic: dict) -> tuple[Path, str] | None:
    """A browser-displayable version: the original when the browser can show
    it, otherwise a cached JPEG (HEIC, TIFF)."""
    src = media_path(cfg, pic["stored_name"])
    if not src.exists():
        return None
    if pic["mime"] in BROWSER_SAFE:
        return src, pic["mime"]
    out = view_path(cfg, pic["sha256"])
    if not out.exists():
        try:
            img = ImageOps.exif_transpose(Image.open(src))
            img.thumbnail((VIEW_PX, VIEW_PX))
            _thumbnail_mode(img).save(out, "JPEG", quality=88, optimize=True)
        except (OSError, ValueError):
            return None
    return out, "image/jpeg"


def delete_picture(conn, cfg: Config, pic_id: int) -> None:
    pic = get_picture(conn, pic_id)
    with transaction(conn):
        _delete(conn, "pictures", pic_id)  # FKs null out contact photos / receipts
    for p in (media_path(cfg, pic["stored_name"]), thumb_path(cfg, pic["sha256"]), view_path(cfg, pic["sha256"])):
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
            try:
                _thumbnail(ImageOps.exif_transpose(Image.open(io.BytesIO(data)))).save(
                    thumb_path(cfg, r["sha256"]), "JPEG", quality=82)
                rebuilt += 1
            except (OSError, ValueError):
                corrupt.append(r["id"])
    orphans = [str(p.relative_to(cfg.media_dir)) for p in cfg.media_dir.rglob("*") if p.is_file() and p.name not in known]
    return {"missing_files": missing, "hash_mismatch": corrupt, "orphan_files": orphans, "thumbs_rebuilt": rebuilt}

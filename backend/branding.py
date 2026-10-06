"""The logo shown inside the app: one image in the data folder, else the bundled default."""
import config

MAX_BYTES = 2_000_000
_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _kind(data: bytes):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def current():
    """(path, media type) of the logo to show."""
    for ext, mime in _TYPES.items():
        p = config.DATA_DIR / f"logo.{ext}"
        if p.is_file():
            return p, mime
    return config.static_dir() / "logo.png", "image/png"


def save(data: bytes):
    if len(data) > MAX_BYTES:
        raise ValueError("That image is too big. Use one under 2 MB.")
    ext = _kind(data)
    if not ext:
        raise ValueError("Use a PNG, JPG or WebP image.")
    reset()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / f"logo.{ext}").write_bytes(data)


def reset():
    for ext in _TYPES:
        (config.DATA_DIR / f"logo.{ext}").unlink(missing_ok=True)

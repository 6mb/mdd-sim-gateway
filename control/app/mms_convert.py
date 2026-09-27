"""Converting and shrinking MMS attachments on the gateway.

Every client -- the WebUI, a script calling the API, later a SIP user agent -- hands the
gateway the file as the user picked it; the gateway turns it into something the carrier and the
recipient's phone accept, within the line's size limit. Doing it here rather than in a browser
means one implementation, the same result for every client, and the original kept at hand
while a message is composed so each re-fit starts from full quality (see mms.fit_attachments).

Converters are registered per kind of media (mms_media.MediaFormat.kind). A converter says
which types it can take, whether a given file can be made smaller ("adjustable"), and fits one
file into a byte budget. Only pictures have one today. A video converter -- re-encoding to
H.264/AAC in MP4 or 3GP at a lower bitrate, e.g. with ffmpeg -- slots in as CONVERTERS["video"]
with the same three methods; the planner, the staging API and the WebUI already treat every
adjustable attachment alike, and the capability table offers a "convert" format as soon as a
converter takes it.
"""
from __future__ import annotations

import hashlib
import io
import threading
from collections import OrderedDict
from dataclasses import dataclass

try:
    from PIL import Image
except ImportError:  # pragma: no cover - the control requirements install Pillow
    Image = None
try:
    # pi-heif is the decode-only build of pillow-heif (same author, same plugin API): reading
    # HEIC is all MMS needs, and it leaves out the x265 encoder and its GPL.
    import pi_heif
except ImportError:  # pragma: no cover - HEIC/HEIF is then simply not convertible
    pi_heif = None
else:
    pi_heif.register_heif_opener()

# A picture this large is not a photo anyone means to send by MMS; refusing it also keeps a
# crafted file from making the decoder allocate gigabytes.
MAX_PIXELS = 64_000_000

if Image is not None:
    # Pillow's own guard is a backstop, not the limit: it raises only above twice
    # MAX_IMAGE_PIXELS and does no more than warn in between, so a 100-megapixel file set
    # against this would be decoded anyway. _open_header() enforces MAX_PIXELS itself, from
    # the header, before a single row is decoded.
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ConversionError(ValueError):
    """The file cannot be converted, or not made small enough; the message says why."""


@dataclass
class Fitted:
    content_type: str
    data: bytes
    width: int | None = None
    height: int | None = None
    converted: bool = False     # re-encoded, as opposed to passed through unchanged


# Longest edges tried from largest to smallest; about what phones themselves send by MMS. At
# each size the highest JPEG quality that fits is searched for, and a size is given up once
# even its quality floor is too big -- higher for large sizes, so a tight budget buys a smaller
# clean picture rather than a big blocky one.
IMAGE_EDGES = (1600, 1280, 1024, 800, 640, 480, 320, 240)
MAX_QUALITY, MIN_QUALITY, LAST_RESORT_QUALITY = 90, 60, 40
QUALITY_STEPS = 6
# Sent as they are when they fit and are no larger than the first edge; anything else that can
# be decoded goes out as baseline JPEG, the one picture type every MMS phone shows.
PASS_THROUGH_IMAGES = ("image/jpeg", "image/png", "image/gif")


@dataclass(frozen=True)
class Probe:
    """What a picture's header says, read without decoding a single pixel."""
    format: str | None
    width: int                  # as shown, i.e. after the EXIF orientation
    height: int
    orientation: int
    animated: bool

    @property
    def longest(self) -> int:
        return max(self.width, self.height)


# ImageOps.exif_transpose's table, applied to the shrunk picture instead of the full-size one.
_TRANSPOSE = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
              6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def _open_header(data: bytes):
    """The picture opened -- its header read, nothing decoded -- and held to MAX_PIXELS."""
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        raise ConversionError("the picture is too large to convert") from None
    except Exception as exc:  # noqa: BLE001 -- any decoder failure means "unreadable"
        raise ConversionError(f"the picture could not be read ({exc})") from None
    # Opening reads the header, not the pixels; this is the point at which the size is known
    # and nothing has been allocated for it yet.
    width, height = image.size
    if width * height > MAX_PIXELS:
        image.close()
        raise ConversionError(f"the picture is {width}x{height} pixels; at most "
                              f"{MAX_PIXELS // 1_000_000} megapixels can be converted")
    return image


def probe(data: bytes) -> Probe:
    image = _open_header(data)
    try:
        width, height = image.size
        orientation = image.getexif().get(0x0112, 1)
        animated = bool(getattr(image, "is_animated", False))
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"the picture could not be read ({exc})") from None
    finally:
        image.close()
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    return Probe(image.format, width, height, orientation, animated)


def decode(data: bytes, longest: int, orientation: int = 1):
    """The picture as an upright RGB image no larger than `longest` on either side.

    The full-size picture is only ever held for as long as it takes to shrink it: everything a
    fit tries afterwards -- each size, each quality -- starts from this one, a few megabytes
    whatever the camera. A JPEG is not even decoded at full size: DCT scaling reads it straight
    at the smallest scale that still covers `longest`. HEIC has nothing like that, so libheif
    decodes it whole, and that decode is what a conversion costs in memory."""
    image = _open_header(data)
    try:
        width, height = image.size
        if image.format == "JPEG" and max(width, height) > longest:
            scale = longest / max(width, height)
            image.draft("RGB", (int(width * scale) + 1, int(height * scale) + 1))
        image.load()
        if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and
                                                  "transparency" in image.info):
            image = image.convert("RGBA")
        elif image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if max(image.size) > longest:
            image.thumbnail((longest, longest), Image.LANCZOS)
    except ConversionError:
        raise
    except Image.DecompressionBombError:
        raise ConversionError("the picture is too large to convert") from None
    except Exception as exc:  # noqa: BLE001 -- any decoder failure means "unreadable"
        raise ConversionError(f"the picture could not be read ({exc})") from None
    if image.mode == "RGBA":
        # JPEG has no alpha: paint transparent areas white rather than black.
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    if orientation in _TRANSPOSE:
        image = image.transpose(getattr(Image.Transpose, _TRANSPOSE[orientation]))
    return image


class ImageConverter:
    kind = "image"
    # Pictures already decoded and shrunk to IMAGE_EDGES[0], by content: typing in the composer
    # changes the room the text leaves and so every picture's byte target, and without these
    # each keystroke would decode every photo again. About 6 MB each.
    BASE_CACHE_BYTES = 64 * 1024 * 1024

    def __init__(self):
        self._bases: OrderedDict[tuple, object] = OrderedDict()
        self._bases_lock = threading.Lock()

    def can_convert(self, content_type: str) -> bool:
        if Image is None:
            return False
        if content_type in ("image/heic", "image/heif"):
            return pi_heif is not None
        return content_type in ("image/jpeg", "image/png", "image/gif", "image/webp",
                                "image/bmp", "image/avif")

    def adjustable(self, content_type: str, data: bytes) -> bool:
        """Whether this picture can be made smaller: an animated GIF cannot without losing
        its animation, so it is sent as it is or not at all."""
        if not self.can_convert(content_type):
            return False
        if content_type != "image/gif":
            return True
        return not probe(data).animated

    def base(self, data: bytes, info: Probe, digest: bytes | None = None):
        """The picture decoded, upright and shrunk to IMAGE_EDGES[0]; decoded once per content
        however many times it is fitted."""
        key = (digest or hashlib.sha256(data).digest(), IMAGE_EDGES[0])
        with self._bases_lock:
            if key in self._bases:
                self._bases.move_to_end(key)
                return self._bases[key]
        image = decode(data, IMAGE_EDGES[0], info.orientation)
        with self._bases_lock:
            self._bases[key] = image
            while len(self._bases) > 1 and sum(
                    i.width * i.height * 3 for i in self._bases.values()) > self.BASE_CACHE_BYTES:
                self._bases.popitem(last=False)
        return image

    @staticmethod
    def _encode(image, quality: int) -> bytes:
        out = io.BytesIO()
        # Baseline (not progressive) JPEG with no metadata: what older handsets decode, and
        # nothing of the camera's EXIF -- location included -- leaves with the picture.
        image.save(out, "JPEG", quality=int(quality), optimize=True, progressive=False)
        return out.getvalue()

    def fit(self, content_type: str, data: bytes, target: int, *,
            force: bool = False, digest: bytes | None = None) -> Fitted:
        """`data` as a picture of at most `target` bytes: unchanged when it already is one a
        phone shows, fits and is no larger than IMAGE_EDGES[0] (and `force` is not set);
        otherwise the largest size, then the highest quality, whose JPEG fits."""
        info = probe(data)
        width, height = info.width, info.height
        if content_type == "image/gif" and info.animated:
            if len(data) <= target:
                return Fitted(content_type, data, width, height)
            raise ConversionError("an animated GIF cannot be made smaller without losing its "
                                  "animation")
        if not force and content_type in PASS_THROUGH_IMAGES \
                and info.longest <= IMAGE_EDGES[0] and info.orientation == 1:
            # Sent as it is, apart from its metadata: a phone photo's EXIF carries where it
            # was taken. (A rotated one is re-encoded instead: dropping its EXIF would drop
            # the rotation with it.)
            clean = strip_metadata(content_type, data)
            if clean is not None and len(clean) <= target:
                return Fitted(content_type, clean, width, height)
        if target <= 0:
            raise ConversionError("there is no room left for this picture")
        base = self.base(data, info, digest)
        longest = max(base.size)
        edges = [e for e in IMAGE_EDGES if e < longest]
        edges.insert(0, min(longest, IMAGE_EDGES[0]))
        for edge in dict.fromkeys(edges):
            scaled = base.copy()
            if max(scaled.size) > edge:
                scaled.thumbnail((edge, edge), Image.LANCZOS)
            floor_quality = MIN_QUALITY if edge > 640 else LAST_RESORT_QUALITY
            best = self._encode(scaled, floor_quality)
            if len(best) > target:
                continue
            top = self._encode(scaled, MAX_QUALITY)
            if len(top) <= target:
                best = top
            else:
                low, high = floor_quality, MAX_QUALITY
                for _ in range(QUALITY_STEPS):
                    middle = (low + high) // 2
                    if middle in (low, high):
                        break
                    candidate = self._encode(scaled, middle)
                    if len(candidate) <= target:
                        best, low = candidate, middle
                    else:
                        high = middle
            return Fitted("image/jpeg", best, *scaled.size, converted=True)
        raise ConversionError(f"the picture cannot be made smaller than {target // 1024 + 1} KB")


# JPEG segments and PNG chunks that describe a picture rather than draw it: EXIF (camera,
# time, location), XMP, IPTC, and PNG text. Removing them changes no pixel.
_JPEG_METADATA_MARKERS = {0xE1, 0xED}          # APP1 (EXIF, XMP), APP13 (IPTC)
_PNG_METADATA_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}


def _strip_jpeg(data: bytes) -> bytes | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    out, pos = bytearray(data[:2]), 2
    while pos + 2 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:                      # fill byte before a marker
            pos += 1
            continue
        if marker == 0xDA or marker == 0xD9:    # start of scan: the rest is image data
            return bytes(out + data[pos:])
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += data[pos:pos + 2]
            pos += 2
            continue
        if pos + 4 > len(data):
            return None
        end = pos + 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
        if end > len(data):
            return None
        if marker not in _JPEG_METADATA_MARKERS:
            out += data[pos:end]
        pos = end
    return None


def _strip_png(data: bytes) -> bytes | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    out, pos = bytearray(data[:8]), 8
    while pos + 12 <= len(data):
        end = pos + 12 + int.from_bytes(data[pos:pos + 4], "big")
        if end > len(data):
            return None
        kind = data[pos + 4:pos + 8]
        if kind not in _PNG_METADATA_CHUNKS:
            out += data[pos:end]
        pos = end
        if kind == b"IEND":
            return bytes(out)
    return None


def strip_metadata(content_type: str, data: bytes) -> bytes | None:
    """`data` without its descriptive metadata, losslessly; the bytes unchanged for a type
    that carries none worth removing (GIF); None when the file's structure is not what it
    should be, so the caller re-encodes it instead."""
    if content_type == "image/jpeg":
        return _strip_jpeg(data)
    if content_type == "image/png":
        return _strip_png(data)
    return data


CONVERTERS = {"image": ImageConverter()}


def converter_for(kind: str, content_type: str):
    """The converter that takes this kind and type, or None."""
    converter = CONVERTERS.get(kind)
    return converter if converter is not None and converter.can_convert(content_type) else None

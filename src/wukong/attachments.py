"""Where an attached image goes, and what it is allowed to be.

Attachments are user data that outlives a single request, so they are files
under the home directory rather than blobs in the event log. The checks here
are the only ones that exist -- the model never chooses what to attach, so
there is no permission decision to make. What there is instead is a size and
type ceiling, because a client that pastes a 40 MB screenshot is a mistake,
and an attachment that is not an image is a bug in the client.

One rule worth stating: nothing here trusts the *name*. The extension the
browser reports is beside the point; what is written is what the bytes say
they are, and the media type is stored so the provider gets a correct
envelope. A `.png` that is really a zip is written with a `.png` name and a
`image/png` type and the provider will refuse it loudly -- which is the
right outcome for a corrupt attachment.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any

from wukong.types import ImageRef

#: What may be attached. The providers in this project accept these four;
#: anything else would have to be converted first, and converting silently
#: is how a screenshot becomes a mystery.
ALLOWED: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

#: Per-image ceiling. A screenshot of a full desktop at retina scale is
#: around 2 MB; 5 MB leaves room without letting one paste fill the disk.
MAX_BYTES = 5 * 1024 * 1024

#: Per-task ceiling. More than this and the prompt is mostly pixels.
MAX_IMAGES = 8


class AttachmentError(ValueError):
    """The client sent something that cannot become an ImageRef."""


def _decode(data: str) -> bytes:
    """Base64, with or without the data-URL prefix.

    Both shapes arrive in practice: a browser hands over `data:image/png;
    base64,iVBOR...`, and a script hands over the bare base64. Refusing the
    one the sender happens not to use is a self-inflicted 500.
    """
    raw = data.strip()
    if raw.startswith("data:"):
        _, _, tail = raw.partition(",")
        raw = tail
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("图片数据不是合法的 base64") from exc


def save_attachments(
    home: Path, task_id: str, items: list[dict[str, Any]]
) -> list[ImageRef]:
    """Validate, write and describe the images a task was given.

    Written under `home/attachments/<task_id>/`. Deleting the home directory
    deletes the tasks with it, which is the same lifetime the database
    already has -- there is no case where the log survives and the image
    should not.
    """
    if len(items) > MAX_IMAGES:
        raise AttachmentError(f"一次最多附加 {MAX_IMAGES} 张图片")

    refs: list[ImageRef] = []
    for index, item in enumerate(items):
        name = str(item.get("name") or f"image-{index + 1}")
        media = str(item.get("media_type") or "image/png").lower()
        # A client that reports `image/jpg` (common, and wrong) is corrected
        # rather than refused: the bytes are what matter.
        media = "image/jpeg" if media == "image/jpg" else media
        if media not in ALLOWED:
            allowed = ", ".join(sorted(ALLOWED))
            raise AttachmentError(f"不支持的图片类型 {media}（可用：{allowed}）")
        if not str(item.get("data") or "").strip():
            raise AttachmentError(f"图片 {name} 没有携带数据")

        blob = _decode(str(item["data"]))
        if len(blob) > MAX_BYTES:
            raise AttachmentError(
                f"图片 {name} 有 {len(blob) // 1024} KB，超过 "
                f"{MAX_BYTES // 1024 // 1024} MB 上限"
            )
        # Magic bytes, not the declared type. A PNG that is really something
        # else is corrupt input, and the provider deserves to see it as such.
        if not _sniff(blob, media):
            raise AttachmentError(f"图片 {name} 的内容不是 {media}")

        digest = hashlib.sha256(blob).hexdigest()
        directory = Path(home) / "attachments" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest[:16]}{ALLOWED[media]}"
        # Written once per content, not once per attachment: pasting the same
        # screenshot into two tasks is one file on disk.
        if not path.exists():
            path.write_bytes(blob)
        refs.append(
            ImageRef(
                path=str(path),
                media_type=media,
                name=name,
                sha256=digest,
                bytes=len(blob),
            )
        )
    return refs


def ref_from_file(path: Path) -> ImageRef:
    """Describe an image already on disk, for a caller that has the file.

    The CLI takes `--image path`: the bytes are where the user put them, and
    copying them into the attachments directory would mean two copies of a
    screenshot nobody asked to duplicate. The reference points at the
    original, which also means a rewind of the workspace takes the attachment
    with it -- and a missing attachment then fails loudly instead of
    silently sending a prompt that lost its image.
    """
    path = Path(path)
    if not path.is_file():
        raise AttachmentError(f"图片不存在：{path}")
    blob = path.read_bytes()
    if len(blob) > MAX_BYTES:
        raise AttachmentError(
            f"{path.name} 有 {len(blob) // 1024} KB，超过 {MAX_BYTES // 1024 // 1024} MB 上限"
        )
    sniffed = next((m for m, heads in _MAGIC.items() if blob.startswith(heads)), "")
    if not sniffed:
        raise AttachmentError(f"{path.name} 的内容不是受支持的图片类型")
    return ImageRef(
        path=str(path.resolve()),
        media_type=sniffed,
        name=path.name,
        sha256=hashlib.sha256(blob).hexdigest(),
        bytes=len(blob),
    )


_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}


def _sniff(blob: bytes, media: str) -> bool:
    return blob.startswith(_MAGIC[media])

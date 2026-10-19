"""Detect reviewable visual media (images/GIFs/videos) in a message. Pure, so it
is unit-tested and shared by the media-flag pipeline."""

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".apng")
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gifv")
_MEDIA_EMBED_TYPES = {"image", "gifv", "video"}


def is_visual_attachment(attachment) -> bool:
    ct = (getattr(attachment, "content_type", None) or "").lower()
    if ct.startswith("image/") or ct.startswith("video/"):
        return True
    name = (getattr(attachment, "filename", "") or "").lower()
    return name.endswith(_IMAGE_EXTS + _VIDEO_EXTS)


def message_has_visual_media(message) -> bool:
    for att in getattr(message, "attachments", None) or []:
        if is_visual_attachment(att):
            return True
    for emb in getattr(message, "embeds", None) or []:
        if getattr(emb, "type", None) in _MEDIA_EMBED_TYPES:
            return True
        if getattr(emb, "image", None) or getattr(emb, "video", None):
            return True
    return False

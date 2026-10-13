"""Owner-only local booking preset. The only place pickup details may be stored."""

import json
import os
import stat
import tempfile
from pathlib import Path

from pydantic import ValidationError

from thsr_watcher.booking_models import DocumentType, PickupDetails


class PresetError(Exception):
    """The booking preset is missing, unreadable, or not private."""


def default_preset_path() -> Path:
    return Path.home() / ".config" / "thsr-watcher" / "booking-preset.json"


def masked_summary(details: PickupDetails) -> str:
    """Show document kind, last three characters, and the membership choice."""
    kind = {
        DocumentType.NATIONAL_ID: "national ID",
        DocumentType.PASSPORT: "passport",
        DocumentType.RESIDENCE_PERMIT: "residence permit",
    }[details.document_type]
    email = "set" if details.email else "none"
    membership = "on" if details.use_same_id_membership else "off"
    return (
        f"Preset: {kind} {_mask(details.document_number)}, "
        f"mobile {_mask(details.mobile)}, email {email}, "
        f"membership checkbox {membership}"
    )


def save_preset(details: PickupDetails, path: Path | None = None) -> None:
    path = path or default_preset_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = details.model_dump(mode="json")
    # Write privately before publishing, even when replacing a wide-mode file
    # or a symlink. A failed write must leave the previous preset intact.
    descriptor, temporary = tempfile.mkstemp(prefix=".booking-preset-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_preset(path: Path | None = None) -> PickupDetails:
    path = path or default_preset_path()
    try:
        # Check the opened file rather than a path that could change before read.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise PresetError("Booking preset must be a regular file.")
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise PresetError("Booking preset is not owner-only or is readable by other users; reservation was not started.")
            payload = json.load(handle)
            return PickupDetails.model_validate(payload)
    except FileNotFoundError as exc:
        raise PresetError("No booking preset. Run book-preset before book.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise PresetError("Booking preset is invalid.") from exc


def clear_preset(path: Path | None = None) -> None:
    path = path or default_preset_path()
    path.unlink(missing_ok=True)


def _mask(value: str) -> str:
    if len(value) <= 3:
        return "***"
    return "*" * (len(value) - 3) + value[-3:]

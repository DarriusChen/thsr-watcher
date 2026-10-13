import json
import stat

import pytest
from pydantic import ValidationError

from thsr_watcher.booking_models import PickupDetails
from thsr_watcher.booking_preset import PresetError, clear_preset, load_preset, masked_summary, save_preset

SECRET = "A123456789"
MOBILE = "0912345678"


def details(**changes):
    payload = dict(document_type="national_id", document_number=SECRET, mobile=MOBILE, use_same_id_membership=True)
    return PickupDetails(**(payload | changes))


def test_preset_roundtrip_is_private_and_masked(tmp_path):
    path = tmp_path / "booking-preset.json"
    save_preset(details(email="person@example.com"), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = load_preset(path)
    assert loaded.document_number == SECRET and loaded.use_same_id_membership
    summary = masked_summary(loaded)
    assert SECRET not in summary and MOBILE not in summary
    assert summary.endswith("membership checkbox on")
    assert "789" in summary and "678" in summary and "email set" in summary
    clear_preset(path)
    assert not path.exists()
    clear_preset(path)


def test_missing_wide_and_invalid_presets_hide_contents(tmp_path):
    path = tmp_path / "booking-preset.json"
    with pytest.raises(PresetError, match="No booking preset"):
        load_preset(path)
    path.write_text(json.dumps({"document_number": SECRET, "mobile": MOBILE}), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(PresetError, match="other users") as wide:
        load_preset(path)
    assert SECRET not in str(wide.value) and MOBILE not in str(wide.value)
    path.chmod(0o600)
    path.write_text('{"document_number": "%s"' % SECRET, encoding="utf-8")
    with pytest.raises(PresetError, match="invalid") as broken:
        load_preset(path)
    assert SECRET not in str(broken.value)


def test_membership_requires_national_id_before_save(tmp_path):
    path = tmp_path / "booking-preset.json"
    with pytest.raises(ValidationError):
        save_preset(details(document_type="passport", document_number="AB123456"), path)
    assert not path.exists()


def test_save_replaces_symlink_without_writing_its_target(tmp_path):
    target = tmp_path / "public.json"
    target.write_text("unchanged", encoding="utf-8")
    target.chmod(0o644)
    path = tmp_path / "booking-preset.json"
    path.symlink_to(target)
    with pytest.raises(PresetError):
        load_preset(path)
    save_preset(details(), path)
    assert not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_preset(path) == details()
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_failed_save_preserves_existing_preset_and_removes_temporary_file(tmp_path, monkeypatch):
    from thsr_watcher import booking_preset

    path = tmp_path / "booking-preset.json"
    save_preset(details(), path)
    previous = path.read_bytes()

    def fail_write(payload, handle):
        handle.write("partial")
        raise OSError("disk full")

    monkeypatch.setattr(booking_preset.json, "dump", fail_write)
    with pytest.raises(OSError):
        save_preset(details(email="person@example.com"), path)
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_unreadable_preset_maps_to_safe_error(tmp_path, monkeypatch):
    from thsr_watcher import booking_preset

    def denied(*args):
        raise PermissionError(SECRET)

    monkeypatch.setattr(booking_preset.os, "open", denied)
    with pytest.raises(PresetError, match="invalid") as error:
        load_preset(tmp_path / "booking-preset.json")
    assert SECRET not in str(error.value)

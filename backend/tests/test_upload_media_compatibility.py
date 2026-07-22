from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.review_media.service import parse_media_probe_output, validate_media_identity

from .conftest import api_data, api_error, upload_init_request


def iso_bmff_ftyp(major_brand: bytes, *compatible_brands: bytes, extended_size: bool = False) -> bytes:
    brand_payload = major_brand + b"\x00\x00\x02\x00" + b"".join(compatible_brands)
    if extended_size:
        box_size = 16 + len(brand_payload)
        return b"\x00\x00\x00\x01ftyp" + box_size.to_bytes(8, "big") + brand_payload
    box_size = 8 + len(brand_payload)
    return box_size.to_bytes(4, "big") + b"ftyp" + brand_payload


def test_default_principal_quota_accepts_ten_active_upload_sessions(client: TestClient) -> None:
    for index in range(10):
        initialized = upload_init_request(
            client,
            json={
                "original_filename": f"concurrent-{index}.mp4",
                "mime_type": "video/mp4",
                "file_size": 1024,
                "sha256": "0" * 64,
            },
        )
        assert initialized.status_code == 200, initialized.text


@pytest.mark.parametrize("brand", [b"MSNV", b"XAVC"])
def test_structurally_valid_unknown_mp4_brand_reaches_probe_and_completes(client: TestClient, brand: bytes) -> None:
    blob = iso_bmff_ftyp(brand, b"isom") + b"bounded-test-payload"
    upload = upload_init_request(
        client,
        json={
            "original_filename": "camera.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    assert upload.status_code == 200, upload.text
    upload_id = api_data(upload)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text

    complete = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"complete-{upload_id}"},
    )

    assert complete.status_code == 200, complete.text
    assert api_data(complete)["file_id"]


@pytest.mark.parametrize(
    ("filename", "mime_type", "blob"),
    [
        ("standard.mp4", "video/mp4", iso_bmff_ftyp(b"isom", b"iso2", b"avc1")),
        ("standard.m4v", "video/mp4", iso_bmff_ftyp(b"mp42", b"isom")),
        ("standard.mov", "video/quicktime", iso_bmff_ftyp(b"qt  ", b"qt  ")),
        ("quicktime-brand.mp4", "video/mp4", iso_bmff_ftyp(b"qt  ", b"qt  ")),
        ("mp4-brand.mov", "video/quicktime", iso_bmff_ftyp(b"isom", b"mp42")),
        ("extended.mp4", "video/mp4", iso_bmff_ftyp(b"MSNV", b"XAVC", extended_size=True)),
    ],
)
def test_iso_bmff_structure_accepts_supported_file_and_mime_pairs(
    filename: str,
    mime_type: str,
    blob: bytes,
) -> None:
    validate_media_identity(filename, mime_type, blob)


@pytest.mark.parametrize(
    "blob",
    [
        b"<html>ftyp<script>alert(1)</script></html>",
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00",
        b"\x00\x01\x00\x04ftypisom\x00\x00\x02\x00",
        b"\x00\x00\x00\x12ftypisom\x00\x00\x02\x00xx",
        b"\x00\x00\x00\x10ftyp\x00\x00\x00\x00\x00\x00\x00\x00",
        b"\x00\x00\x00\x00ftypisom\x00\x00\x02\x00",
    ],
)
def test_iso_bmff_structure_rejects_disguised_truncated_oversize_or_malformed_ftyp(blob: bytes) -> None:
    with pytest.raises(ReviewError) as exc_info:
        validate_media_identity("fake.mp4", "video/mp4", blob)
    assert exc_info.value.code == "FILE_TYPE_NOT_ALLOWED"


def test_iso_bmff_still_rejects_extension_and_mime_mismatch() -> None:
    blob = iso_bmff_ftyp(b"isom", b"mp42")
    with pytest.raises(ReviewError):
        validate_media_identity("mismatch.mov", "video/mp4", blob)
    with pytest.raises(ReviewError):
        validate_media_identity("mismatch.mp4", "video/quicktime", blob)


def test_probe_result_without_exactly_one_video_stream_is_rejected() -> None:
    with pytest.raises(ReviewError) as exc_info:
        parse_media_probe_output(b'{"streams": [], "format": {"duration": "1"}}')
    assert exc_info.value.code == "FILE_TYPE_NOT_ALLOWED"


def test_unknown_brand_with_no_video_track_remains_fail_closed(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "no-video-probe"
    probe.write_text(
        '#!/usr/bin/env python3\nprint(\'{"streams": [], "format": {"duration": "1"}}\')\n',
        encoding="utf-8",
    )
    probe.chmod(0o700)
    settings = __import__("backend.app.settings", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "media_probe_command", str(probe))

    blob = iso_bmff_ftyp(b"MSNV", b"XAVC") + b"not-a-video-track"
    upload = upload_init_request(
        client,
        json={
            "original_filename": "no-track.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(upload)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    complete = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"complete-no-track-{upload_id}"},
    )
    assert complete.status_code == 422
    assert api_error(complete)["code"] == "FILE_TYPE_NOT_ALLOWED"

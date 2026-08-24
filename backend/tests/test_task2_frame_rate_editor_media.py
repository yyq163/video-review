from __future__ import annotations

import hashlib
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.review_media.service import MediaProbeResult, parse_media_probe_output, probe_media, validate_streamed_frame_timing

from .conftest import api_data, api_error, command, tiny_video_bytes, upload_init_request


def _probe_payload(
    *,
    fps_num: int = 25,
    fps_den: int = 1,
    timestamps: tuple[str, ...] = ("0.000000", "0.040000", "0.080000", "0.120000"),
) -> dict[str, Any]:
    duration = f"{float(timestamps[-1]) + (fps_den / fps_num):.6f}"
    return {
        "packets": [
            {
                "stream_index": 0,
                "pts_time": timestamp,
                "duration_time": f"{fps_den / fps_num:.6f}",
            }
            for timestamp in timestamps
        ],
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "codec_tag_string": "avc1",
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "has_b_frames": 2,
                "duration": duration,
                "start_time": "0",
                "nb_frames": str(len(timestamps)),
                "nb_read_packets": str(len(timestamps)),
                "avg_frame_rate": f"{fps_num}/{fps_den}",
                "r_frame_rate": f"{fps_num}/{fps_den}",
            }
        ],
        "format": {"duration": duration},
    }


def _write_probe(tmp_path: Path, payload: dict[str, Any]) -> Path:
    probe = tmp_path / f"task2-probe-{uuid.uuid4().hex}"
    metadata_payload = {key: value for key, value in payload.items() if key != "packets"}
    encoded = json.dumps(metadata_payload, separators=(",", ":"))
    video_stream = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), None)
    frame_rate = str((video_stream or {}).get("avg_frame_rate", "0/1")).split("/", maxsplit=1)
    try:
        interval = int(frame_rate[1]) / int(frame_rate[0])
    except (IndexError, ValueError, ZeroDivisionError):
        interval = 0
    video_index = (video_stream or {}).get("index", 0)
    video_packets = [packet for packet in payload.get("packets", []) if packet.get("stream_index") == video_index]
    timing_output = "".join(
        f"{packet.get('pts_time', '')}|{(index - 2) * interval:.6f}|{packet.get('duration_time', '')}\n"
        for index, packet in enumerate(video_packets)
    )
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "output_format = sys.argv[sys.argv.index('-of') + 1]\n"
        f"sys.stdout.write({encoded!r} if output_format.startswith('json') else {timing_output!r})\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    return probe


def _write_two_stage_probe(
    tmp_path: Path,
    *,
    fps_num: int,
    fps_den: int,
    duration_seconds: int,
    mode: str = "cfr",
) -> tuple[Path, Path]:
    probe = tmp_path / f"task2-streaming-probe-{uuid.uuid4().hex}"
    marker = tmp_path / f"task2-streaming-probe-{uuid.uuid4().hex}.log"
    probe.write_text(
        f'''#!/usr/bin/env python3
import json
import sys

FPS_NUM = {fps_num}
FPS_DEN = {fps_den}
DURATION = {duration_seconds}
MODE = {mode!r}
MARKER = {str(marker)!r}
FRAME_COUNT = round(DURATION * FPS_NUM / FPS_DEN)
AUDIO_PACKET_COUNT = round(DURATION * 48000 / 1024)
INTERVAL = FPS_DEN / FPS_NUM

def metadata():
    return {{
        "streams": [
            {{
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "codec_tag_string": "avc1",
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "has_b_frames": 2,
                "duration": f"{{DURATION:.6f}}",
                "start_time": "0.000000",
                "nb_frames": str(FRAME_COUNT),
                "nb_read_packets": str(FRAME_COUNT),
                "avg_frame_rate": f"{{FPS_NUM}}/{{FPS_DEN}}",
                "r_frame_rate": f"{{FPS_NUM}}/{{FPS_DEN}}",
            }},
            {{
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "codec_tag_string": "mp4a",
                "channels": 2,
                "sample_rate": "48000",
                "duration": f"{{DURATION:.6f}}",
                "start_time": "0.000000",
                "nb_frames": str(AUDIO_PACKET_COUNT),
                "nb_read_packets": str(AUDIO_PACKET_COUNT),
                "avg_frame_rate": "0/0",
                "r_frame_rate": "0/0",
            }},
        ],
        "format": {{"duration": f"{{DURATION:.6f}}"}},
    }}

def decode_order():
    if FRAME_COUNT <= 0:
        return
    yield 0
    first_block = list(range(1, min(5, FRAME_COUNT)))
    if len(first_block) == 4:
        yield from (first_block[3], first_block[1], first_block[0], first_block[2])
    else:
        yield from first_block
    for base in range(5, FRAME_COUNT, 2):
        if base + 1 < FRAME_COUNT:
            yield base + 1
        yield base

args = sys.argv[1:]
is_packet_phase = "-show_packets" in args
output_format = args[args.index("-of") + 1] if "-of" in args else ""
if output_format.startswith("json") and is_packet_phase:
    with open(MARKER, "a", encoding="utf-8") as handle:
        handle.write("legacy-combined\\n")
    payload = metadata()
    payload["packets"] = [
        {{"stream_index": 0, "pts_time": f"{{index * INTERVAL:.6f}}", "duration_time": f"{{INTERVAL:.6f}}"}}
        for index in range(FRAME_COUNT)
    ] + [
        {{"stream_index": 1, "pts_time": f"{{index * 1024 / 48000:.6f}}", "duration_time": f"{{1024 / 48000:.6f}}"}}
        for index in range(AUDIO_PACKET_COUNT)
    ]
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
elif output_format.startswith("json"):
    with open(MARKER, "a", encoding="utf-8") as handle:
        handle.write("metadata\\n")
    sys.stdout.write(json.dumps(metadata(), separators=(",", ":")))
else:
    with open(MARKER, "a", encoding="utf-8") as handle:
        handle.write("timing\\n")
    if MODE == "long-line":
        sys.stdout.write("x" * 1024 + "\\n")
        raise SystemExit(0)
    limit = FRAME_COUNT - 1 if MODE == "truncated" else FRAME_COUNT
    for decode_index, presentation_index in enumerate(decode_order()):
        if decode_index >= limit:
            break
        if MODE == "missing-first" and presentation_index == 0:
            continue
        pts = presentation_index * INTERVAL
        dts = (decode_index - 2) * INTERVAL
        duration = INTERVAL
        if MODE == "vfr" and presentation_index == FRAME_COUNT // 2:
            pts += INTERVAL / 2
            duration += INTERVAL / 2
        sys.stdout.write(f"{{pts:.6f}}|{{dts:.6f}}|{{duration:.6f}}\\n")
''',
        encoding="utf-8",
    )
    probe.chmod(0o700)
    return probe, marker


def _probe_settings(probe: Path) -> Any:
    return SimpleNamespace(
        media_probe_command=str(probe),
        media_probe_timeout_seconds=5,
        media_max_dimension=8192,
        media_max_pixels=35_389_440,
    )


def _timing_metadata(
    *,
    start_time: str,
    duration: str,
    frame_count: int,
    fps_num: int = 24,
    fps_den: int = 1,
    reorder_depth: int = 2,
) -> MediaProbeResult:
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "has_b_frames": reorder_depth,
                "start_time": start_time,
                "duration": duration,
                "nb_frames": str(frame_count),
                "nb_read_packets": str(frame_count),
                "avg_frame_rate": f"{fps_num}/{fps_den}",
                "r_frame_rate": f"{fps_num}/{fps_den}",
            }
        ],
        "format": {"duration": duration},
    }
    return parse_media_probe_output(json.dumps(payload).encode())


def _select_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, Any]) -> None:
    from backend.app.settings import get_settings

    monkeypatch.setattr(get_settings(), "media_probe_command", str(_write_probe(tmp_path, payload)))


def _upload_file(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    seed: bytes,
) -> str:
    _select_probe(monkeypatch, tmp_path, payload)
    blob = tiny_video_bytes(seed)
    initialized = upload_init_request(
        client,
        json={
            "original_filename": "controlled.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    assert initialized.status_code == 200, initialized.text
    upload_id = api_data(initialized)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text
    completed = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"complete-{upload_id}"},
    )
    assert completed.status_code == 200, completed.text
    return api_data(completed)["file_id"]


def _create_project(client: TestClient, *, code: str = "TASK2", fps_num: int = 25, fps_den: int = 1) -> dict[str, Any]:
    body = command(
        "CreateProject",
        {
            "project_code": code,
            "project_name": f"Project {code}",
            "fps_num": fps_num,
            "fps_den": fps_den,
        },
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 201, response.text
    return api_data(response)


def _create_editor(client: TestClient, display_name: str) -> dict[str, Any]:
    body = command("CreateResponsibleEditor", {"display_name": display_name})
    response = client.post(
        "/api/v1/final-cut-review/edit/responsible-editors",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 201, response.text
    return api_data(response)


def _binding_payload(
    project_ref_id: str,
    file_id: str,
    editor_id: str,
    *,
    item_code: str | None = None,
    review_item_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_ref_id": project_ref_id,
        "original_file_id": file_id,
        "responsible_editor_id": editor_id,
        "responsible_editor_name_snapshot": "client supplied name must not be trusted",
        "local_probe_claim": {
            "fps_num": 25,
            "fps_den": 1,
            "is_variable_frame_rate": False,
            "probe_version": "client-claim-must-not-be-trusted",
        },
    }
    if item_code is not None:
        payload.update({"item_code": item_code, "title": f"Cut {item_code}", "episode_no": 1})
    if review_item_id is not None:
        payload["review_item_id"] = review_item_id
    return payload


def _mark_current_version_ready_and_create_issue(client: TestClient, project_ref_id: str, item: dict[str, Any]) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import MediaDerivativeTaskModel, ReviewVersionModel

    with SessionLocal.begin() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        version.playback_asset_id = version.original_file_id
        task = session.scalar(
            select(MediaDerivativeTaskModel).where(
                MediaDerivativeTaskModel.version_id == version.id,
                MediaDerivativeTaskModel.kind == "playback_faststart",
            )
        )
        assert task is not None
        task.status = "ready"
        task.output_file_id = version.original_file_id
    body = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project_ref_id,
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "request next version",
            "timestamp_ms": 1000,
            "frame_number": 25,
        },
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project_ref_id}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert response.status_code == 200, response.text


def _get_item(client: TestClient, project_ref_id: str, review_item_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}")
    assert response.status_code == 200, response.text
    return api_data(response)


def _list_versions(client: TestClient, project_ref_id: str, review_item_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions")
    assert response.status_code == 200, response.text
    return api_data(response)


def test_frame_rate_requires_bounded_packet_timestamp_and_count_evidence() -> None:
    missing_evidence = _probe_payload()
    missing_evidence.pop("packets")
    metadata = parse_media_probe_output(json.dumps(missing_evidence).encode())
    with pytest.raises(ReviewError) as exc_info:
        validate_streamed_frame_timing(io.BytesIO(), metadata)
    assert exc_info.value.code == "MEDIA_FRAME_RATE_UNKNOWN"

    evidence = b"0.000000|-0.080000|0.040000\n0.040000|-0.040000|0.040000\n0.080000|0.000000|0.040000\n0.120000|0.040000|0.040000\n"
    assert validate_streamed_frame_timing(io.BytesIO(evidence), metadata) == 4


def test_frame_rate_uses_packet_timestamps_instead_of_equal_avg_and_nominal_rates() -> None:
    variable = _probe_payload(timestamps=("0.000000", "0.040000", "0.086000", "0.126000"))
    variable.pop("packets")
    metadata = parse_media_probe_output(json.dumps(variable).encode())
    evidence = b"0.000000|-0.080000|0.040000\n0.040000|-0.040000|0.040000\n0.086000|0.006000|0.046000\n0.126000|0.046000|0.040000\n"
    with pytest.raises(ReviewError) as exc_info:
        validate_streamed_frame_timing(io.BytesIO(evidence), metadata)
    assert exc_info.value.code == "MEDIA_VARIABLE_FRAME_RATE"


def test_frame_rate_rejects_non_allowed_rational_even_with_constant_timestamps() -> None:
    unsupported = _probe_payload(
        fps_num=48,
        fps_den=1,
        timestamps=("0.000000", "0.020833", "0.041667", "0.062500"),
    )
    unsupported.pop("packets")
    metadata = parse_media_probe_output(json.dumps(unsupported).encode())
    with pytest.raises(ReviewError) as exc_info:
        validate_streamed_frame_timing(io.BytesIO(), metadata)
    assert exc_info.value.code == "MEDIA_FRAME_RATE_NOT_ALLOWED"


def test_frame_rate_evidence_keeps_existing_audio_video_stream_inventory() -> None:
    payload = _probe_payload()
    payload["streams"].append(
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "sample_rate": "48000",
            "duration": "0.160",
            "start_time": "0",
            "nb_read_packets": "1",
        }
    )
    payload["packets"].append(
        {"stream_index": 1, "pts_time": "0", "duration_time": "0.021333"}
    )

    result = parse_media_probe_output(json.dumps(payload).encode())

    assert result.av_streams == (
        ("video", "h264", 0, 0),
        ("audio", "aac", 2, 48_000),
    )


@pytest.mark.parametrize(("fps_num", "fps_den"), [(30, 1), (60, 1)])
def test_two_stage_probe_streams_120_second_b_frame_timing_without_combined_json_cap(
    tmp_path: Path,
    fps_num: int,
    fps_den: int,
) -> None:
    probe, marker = _write_two_stage_probe(
        tmp_path,
        fps_num=fps_num,
        fps_den=fps_den,
        duration_seconds=120,
    )
    media = tmp_path / "controlled-input.bin"
    media.write_bytes(b"controlled")

    with media.open("rb") as handle:
        result = probe_media(handle.fileno(), _probe_settings(probe))

    assert (result.fps_num, result.fps_den) == (fps_num, fps_den)
    assert result.duration_ms == 120_000
    assert result.stream_types == ("video", "audio")
    assert result.video_frame_count == 120 * fps_num // fps_den
    assert marker.read_text(encoding="utf-8").splitlines() == ["metadata", "timing"]


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("vfr", "MEDIA_VARIABLE_FRAME_RATE"),
        ("missing-first", "MEDIA_FRAME_RATE_UNKNOWN"),
        ("truncated", "MEDIA_FRAME_RATE_UNKNOWN"),
        ("long-line", "MEDIA_FRAME_RATE_UNKNOWN"),
    ],
)
def test_streamed_timing_fails_closed_for_vfr_incomplete_or_overlong_evidence(
    tmp_path: Path,
    mode: str,
    expected_code: str,
) -> None:
    probe, _marker = _write_two_stage_probe(
        tmp_path,
        fps_num=60,
        fps_den=1,
        duration_seconds=2,
        mode=mode,
    )
    media = tmp_path / "controlled-input.bin"
    media.write_bytes(b"controlled")

    with media.open("rb") as handle, pytest.raises(ReviewError) as exc_info:
        probe_media(handle.fileno(), _probe_settings(probe))

    assert exc_info.value.code == expected_code


def test_streamed_timing_rejects_frame_count_above_duration_times_allowed_maximum(
    tmp_path: Path,
) -> None:
    probe, _marker = _write_two_stage_probe(
        tmp_path,
        fps_num=60,
        fps_den=1,
        duration_seconds=1,
    )
    script = probe.read_text(encoding="utf-8")
    probe.write_text(
        script.replace("FRAME_COUNT = round(DURATION * FPS_NUM / FPS_DEN)", "FRAME_COUNT = 63"),
        encoding="utf-8",
    )
    probe.chmod(0o700)
    media = tmp_path / "controlled-input.bin"
    media.write_bytes(b"controlled")

    with media.open("rb") as handle, pytest.raises(ReviewError) as exc_info:
        probe_media(handle.fileno(), _probe_settings(probe))

    assert exc_info.value.code == "MEDIA_FRAME_RATE_UNKNOWN"


def test_streamed_timing_rejects_reviewer_collision_instead_of_accepting_summary_statistics() -> None:
    metadata = _timing_metadata(start_time="0.000000", duration="0.375000", frame_count=9)
    collision = (0, 0, 3, 3, 5, 5, 6, 6, 8)
    interval = Fraction(1, 24)
    evidence = "".join(
        f"{float(index * interval):.6f}|{float((decode_index - 2) * interval):.6f}|0.041667\n"
        for decode_index, index in enumerate(collision)
    ).encode()

    with pytest.raises(ReviewError) as exc_info:
        validate_streamed_frame_timing(io.BytesIO(evidence), metadata)

    assert exc_info.value.code == "MEDIA_VARIABLE_FRAME_RATE"


def test_streamed_timing_rejects_presentation_pts_beyond_declared_reorder_window() -> None:
    metadata = _timing_metadata(start_time="0.000000", duration="0.375000", frame_count=9, reorder_depth=2)
    out_of_window = (5, 0, 1, 2, 3, 4, 6, 7, 8)
    interval = Fraction(1, 24)
    evidence = "".join(
        f"{float(index * interval):.6f}|{float((decode_index - 2) * interval):.6f}|0.041667\n"
        for decode_index, index in enumerate(out_of_window)
    ).encode()

    with pytest.raises(ReviewError) as exc_info:
        validate_streamed_frame_timing(io.BytesIO(evidence), metadata)

    assert exc_info.value.code == "MEDIA_FRAME_RATE_UNKNOWN"


def test_streamed_timing_accepts_long_six_decimal_timestamps_without_db_integer_numerator_cap() -> None:
    metadata = _timing_metadata(start_time="3600.000001", duration="0.125000", frame_count=3)
    evidence = (
        b"3600.000001|3599.916668|0.041667\n"
        b"3600.041668|3599.958334|0.041667\n"
        b"3600.083334|3600.000001|0.041667\n"
    )

    assert validate_streamed_frame_timing(io.BytesIO(evidence), metadata) == 3


@pytest.mark.parametrize(
    "evidence",
    [
        (
            b"0.0000001|-0.0833333|0.0416667\n"
            b"0.0416668|-0.0416666|0.0416667\n"
            b"0.0833334|0.0000001|0.0416667\n"
        ),
        (
            b"3600.000001|3599.916668|0.041667\n"
            b"3600.041668|3599.958334|0.041667\n"
            b"3600.083334|3600.000001|0.041667\n"
        ),
    ],
)
def test_streamed_timing_rejects_excess_precision_or_values_outside_authoritative_range(
    evidence: bytes,
) -> None:
    metadata = _timing_metadata(start_time="0.000000", duration="0.125000", frame_count=3)

    with pytest.raises(ReviewError) as exc_info:
        validate_streamed_frame_timing(io.BytesIO(evidence), metadata)

    assert exc_info.value.code == "MEDIA_FRAME_RATE_UNKNOWN"


def test_new_project_persists_required_frame_rate_and_old_project_lock_succeeds_once(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import ProjectRefModel

    created = _create_project(client, code="NEW-FPS", fps_num=30_000, fps_den=1_001)
    assert (created["fps_num"], created["fps_den"]) == (30_000, 1_001)

    old_id = "prj_task2_legacy"
    with SessionLocal.begin() as session:
        session.add(
            ProjectRefModel(
                id=old_id,
                project_code="LEGACY-FPS",
                project_name="Legacy frame rate",
                description="",
                source="local",
                fps_num=None,
                fps_den=None,
            )
        )

    def lock(fps_num: int, fps_den: int) -> Any:
        body = command(
            "LockProjectFrameRate",
            {"project_ref_id": old_id, "fps_num": fps_num, "fps_den": fps_den},
        )
        return client.post(
            f"/api/v1/final-cut-review/edit/projects/{old_id}/frame-rate/lock",
            json=body,
            headers={"Idempotency-Key": body["command_id"], "If-Match": "1"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair: lock(*pair), ((24, 1), (25, 1))))

    assert sorted(response.status_code for response in responses) == [200, 409]
    failed = next(response for response in responses if response.status_code == 409)
    assert api_error(failed)["code"] in {"PROJECT_FRAME_RATE_LOCKED", "OPTIMISTIC_LOCK_CONFLICT"}
    with SessionLocal() as session:
        locked = session.get(ProjectRefModel, old_id)
        assert locked is not None
        assert (locked.fps_num, locked.fps_den) in {(24, 1), (25, 1)}


def test_responsible_editor_list_create_rename_and_v1_v2_v3_snapshots_use_current_authority(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _create_project(client, code="EDITOR-SNAP")
    editor = _create_editor(client, "Editor One")

    listed = client.get("/api/v1/final-cut-review/responsible-editors")
    assert listed.status_code == 200, listed.text
    assert (editor["id"], "Editor One") in [
        (entry["id"], entry["display_name"]) for entry in api_data(listed)
    ]

    file_v1 = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"v1")
    create_body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_v1, editor["id"], item_code="ITEM-1"),
    )
    created = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=create_body,
        headers={"Idempotency-Key": create_body["command_id"]},
    )
    assert created.status_code == 201, created.text
    item = api_data(created)

    rename_body = command(
        "RenameResponsibleEditor",
        {"responsible_editor_id": editor["id"], "display_name": "Editor Renamed"},
    )
    renamed = client.patch(
        f"/api/v1/final-cut-review/edit/responsible-editors/{editor['id']}",
        json=rename_body,
        headers={"If-Match": str(editor["lock_version"])},
    )
    assert renamed.status_code == 200, renamed.text
    assert api_data(renamed)["display_name"] == "Editor Renamed"

    for version_no in (2, 3):
        item = _get_item(client, project["project_ref_id"], item["id"])
        _mark_current_version_ready_and_create_issue(client, project["project_ref_id"], item)
        item = _get_item(client, project["project_ref_id"], item["id"])
        file_id = _upload_file(
            client,
            monkeypatch,
            tmp_path,
            _probe_payload(),
            seed=f"v{version_no}".encode(),
        )
        body = command(
            "UploadReviewVersion",
            _binding_payload(
                project["project_ref_id"],
                file_id,
                editor["id"],
                review_item_id=item["id"],
            ),
        )
        uploaded = client.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
            json=body,
            headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
        )
        assert uploaded.status_code == 200, uploaded.text

    versions = _list_versions(client, project["project_ref_id"], item["id"])
    assert [version["version_no"] for version in versions] == [1, 2, 3]
    assert [version["responsible_editor_name_snapshot"] for version in versions] == [
        "Editor One",
        "Editor Renamed",
        "Editor Renamed",
    ]


@pytest.mark.parametrize(
    ("probe_payload", "expected_code"),
    [
        (_probe_payload(fps_num=30, fps_den=1, timestamps=("0", "0.033333", "0.066667", "0.100000")), "MEDIA_FRAME_RATE_MISMATCH"),
        (_probe_payload(timestamps=("0", "0.040000", "0.086000", "0.126000")), "MEDIA_VARIABLE_FRAME_RATE"),
        ({**_probe_payload(), "packets": []}, "MEDIA_FRAME_RATE_UNKNOWN"),
    ],
)
def test_binding_reprobes_server_side_and_cleans_definitively_rejected_unbound_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_payload: dict[str, Any],
    expected_code: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel

    project = _create_project(client, code=f"REJECT-{expected_code[-8:]}")
    editor = _create_editor(client, f"Editor {expected_code[-8:]}")
    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=expected_code.encode())
    with SessionLocal() as session:
        file = session.get(FileObjectModel, file_id)
        assert file is not None
        storage_path = Path(file.storage_path)
        assert storage_path.exists()

    _select_probe(monkeypatch, tmp_path, probe_payload)
    body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_id, editor["id"], item_code="REJECTED"),
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code in {409, 422}, response.text
    assert api_error(response)["code"] == expected_code
    with SessionLocal() as session:
        assert session.get(FileObjectModel, file_id) is None
    assert not storage_path.exists()


def test_binding_rejects_missing_project_frame_rate_and_missing_editor_without_using_client_claims(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ProjectRefModel

    old_id = "prj_task2_unlocked"
    with SessionLocal.begin() as session:
        session.add(
            ProjectRefModel(
                id=old_id,
                project_code="UNLOCKED",
                project_name="Unlocked legacy",
                description="",
                source="local",
                fps_num=None,
                fps_den=None,
            )
        )

    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"unlocked")
    body = command(
        "CreateReviewItem",
        _binding_payload(old_id, file_id, "editor-does-not-exist", item_code="UNLOCKED"),
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{old_id}/items",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 409, response.text
    assert api_error(response)["code"] == "PROJECT_FRAME_RATE_REQUIRED"
    with SessionLocal() as session:
        assert session.get(FileObjectModel, file_id) is None

    project = _create_project(client, code="NO-EDITOR")
    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"no-editor")
    body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_id, "editor-does-not-exist", item_code="NO-EDITOR"),
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 404, response.text
    assert api_error(response)["code"] == "RESPONSIBLE_EDITOR_NOT_FOUND"
    with SessionLocal() as session:
        assert session.get(FileObjectModel, file_id) is None


def test_rejected_cleanup_does_not_delete_or_queue_an_already_referenced_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import (
        POST_COMMIT_FILE_DELETIONS_KEY,
        SqlAlchemyReviewRepository,
    )
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel
    from backend.app.settings import get_settings

    project = _create_project(client, code="REFERENCED-CLEANUP")
    editor = _create_editor(client, "Referenced Editor")
    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"referenced")
    body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_id, editor["id"], item_code="REFERENCED"),
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 201, response.text

    context = ExecutionContext(
        entry_source="edit",
        request_id="referenced-cleanup",
        principal=PrincipalRef(kind="system", id="test-system", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    with SessionLocal() as session:
        repository = SqlAlchemyReviewRepository(session, get_settings())
        assert repository.cleanup_unbound_uploaded_file(file_id, context) is False
        assert POST_COMMIT_FILE_DELETIONS_KEY not in session.info
        file = session.get(FileObjectModel, file_id)
        assert file is not None
        storage_path = Path(file.storage_path)
    assert storage_path.is_file()
    pending_root = get_settings().storage_root / "pending-deletes"
    assert not pending_root.exists() or list(pending_root.glob("*.json")) == []


def test_rejected_cleanup_does_not_delete_or_queue_another_principals_unbound_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import (
        POST_COMMIT_FILE_DELETIONS_KEY,
        SqlAlchemyReviewRepository,
    )
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel
    from backend.app.settings import get_settings

    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"other-principal")
    context = ExecutionContext(
        entry_source="edit",
        request_id="other-principal-cleanup",
        principal=PrincipalRef(kind="system", id="different-principal", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    with SessionLocal() as session:
        repository = SqlAlchemyReviewRepository(session, get_settings())
        assert repository.cleanup_unbound_uploaded_file(file_id, context) is False
        assert POST_COMMIT_FILE_DELETIONS_KEY not in session.info
        file = session.get(FileObjectModel, file_id)
        assert file is not None
        storage_path = Path(file.storage_path)
    assert storage_path.is_file()
    pending_root = get_settings().storage_root / "pending-deletes"
    assert not pending_root.exists() or list(pending_root.glob("*.json")) == []


def test_binding_commit_outcome_unknown_preserves_unbound_file_without_post_commit_unlink(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, IdempotencyRecordModel, ReviewItemModel
    from backend.app.settings import get_settings
    from sqlalchemy.orm import Session as OrmSession

    project = _create_project(client, code="BIND-UNKNOWN")
    editor = _create_editor(client, "Unknown Commit Editor")
    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"commit-unknown")
    with SessionLocal() as session:
        file = session.get(FileObjectModel, file_id)
        assert file is not None
        storage_path = Path(file.storage_path)

    body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_id, editor["id"], item_code="UNKNOWN"),
    )
    original_commit = OrmSession.commit
    raised = False

    def fail_binding_commit(session: OrmSession) -> None:
        nonlocal raised
        record = session.get(IdempotencyRecordModel, body["command_id"])
        is_binding_commit = record is not None and record.command_type == "CreateReviewItem" and record.status_code == 200
        if is_binding_commit and not raised:
            raised = True
            raise RuntimeError("synthetic binding commit outcome unknown")
        original_commit(session)

    monkeypatch.setattr(OrmSession, "commit", fail_binding_commit)
    no_raise = TestClient(client.app, headers=client.headers, raise_server_exceptions=False)
    try:
        response = no_raise.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
            json=body,
            headers={"Idempotency-Key": body["command_id"]},
        )
    finally:
        no_raise.close()

    assert response.status_code == 500
    assert raised is True
    with SessionLocal() as session:
        assert session.get(FileObjectModel, file_id) is not None
        assert session.scalar(select(ReviewItemModel).where(ReviewItemModel.item_code == "UNKNOWN")) is None
    assert storage_path.is_file()
    pending_root = get_settings().storage_root / "pending-deletes"
    assert not pending_root.exists() or list(pending_root.glob("*.json")) == []

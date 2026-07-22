from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.application.context import ExecutionContext
from backend.app.modules.final_cut_review.domain.errors import ReviewError, not_found
from backend.app.modules.final_cut_review.infra.repositories import new_id
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, UploadSessionModel, utcnow
from backend.app.modules.review_contracts.generated import UploadSessionDTO
from backend.app.safe_files import (
    UnsafeFilePathError,
    contained_path,
    ensure_private_directory,
    exclusive_file_writer,
    pin_managed_root,
    pin_regular_file,
    unlink_regular_file,
    unlink_regular_file_if_identity,
)
from backend.app.settings import Settings
from backend.app.upload_parts import new_upload_part_path, validated_upload_part_path


MEDIA_SIGNATURES = {
    "mp4": {
        "extensions": {".mp4", ".m4v"},
        "mimes": {"video/mp4", "application/octet-stream"},
    },
    "quicktime": {
        "extensions": {".mov", ".qt"},
        "mimes": {"video/quicktime", "application/octet-stream"},
    },
}
SERVER_COMPUTED_SHA256 = "0" * 64
ALLOWED_MIME = {mime for rule in MEDIA_SIGNATURES.values() for mime in rule["mimes"]}
ISO_BMFF_FTYP_MAX_BYTES = 64 * 1024
MEDIA_PROBE_MAX_OUTPUT_BYTES = 64 * 1024
MEDIA_PROBE_VERSION = "ffprobe-json-v1"
MAX_DATABASE_INTEGER = 2_147_483_647
UPLOAD_INIT_ADVISORY_LOCK_KEY = int.from_bytes(b"FCRUPLD1", "big")
UPLOAD_PEAK_RESERVATION_MULTIPLIER = 2


@dataclass(frozen=True)
class MediaProbeResult:
    duration_ms: int
    width: int
    height: int
    fps_num: int
    fps_den: int


@dataclass(frozen=True, slots=True)
class PreparedUploadPart:
    response: dict[str, Any]
    candidate_path: Path
    superseded_path: Path | None


@dataclass(frozen=True, slots=True)
class PreparedUploadCompletion:
    response: dict[str, Any]
    final_path: Path | None
    final_device: int | None
    final_inode: int | None
    part_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class UploadFinalizationClaim:
    upload_id: str
    lease_id: str
    file_id: str
    recovered_expired_lease: bool
    resumed_active_lease: bool
    original_filename: str
    mime_type: str
    declared_size: int
    expected_sha256: str
    owner_principal_id: str
    owner_principal_kind: str
    part_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class FinalizedUploadFile:
    file_id: str
    storage_path: Path
    device: int
    inode: int
    sha256: str
    probe: MediaProbeResult
    part_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class PreparedUploadAbort:
    response: dict[str, Any]
    part_paths: tuple[Path, ...]


def _media_probe_rejected() -> ReviewError:
    return ReviewError("FILE_TYPE_NOT_ALLOWED", "无法验证上传媒体")


def _media_probe_unavailable() -> ReviewError:
    return ReviewError("STORAGE_UNAVAILABLE", "媒体探测服务不可用")


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > MAX_DATABASE_INTEGER:
        raise _media_probe_rejected()
    return value


def _positive_duration_ms(*values: object) -> int:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            duration = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if not duration.is_finite() or duration <= 0:
            continue
        duration_ms = int((duration * 1000).to_integral_value(rounding=ROUND_HALF_UP))
        if 0 < duration_ms <= MAX_DATABASE_INTEGER:
            return duration_ms
    raise _media_probe_rejected()


def _positive_frame_rate(*values: object) -> Fraction:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            frame_rate = Fraction(str(value))
        except (ValueError, ZeroDivisionError):
            continue
        if frame_rate > 0 and frame_rate.numerator <= MAX_DATABASE_INTEGER and frame_rate.denominator <= MAX_DATABASE_INTEGER:
            return frame_rate
    raise _media_probe_rejected()


def _reject_nonstandard_json_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant")


def parse_media_probe_output(output: bytes) -> MediaProbeResult:
    try:
        payload = json.loads(output.decode("utf-8"), parse_constant=_reject_nonstandard_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _media_probe_rejected() from None
    if not isinstance(payload, dict):
        raise _media_probe_rejected()
    streams = payload.get("streams")
    format_data = payload.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict) or not isinstance(format_data, dict):
        raise _media_probe_rejected()
    stream = streams[0]
    if stream.get("codec_type") != "video":
        raise _media_probe_rejected()
    frame_rate = _positive_frame_rate(stream.get("avg_frame_rate"), stream.get("r_frame_rate"))
    return MediaProbeResult(
        duration_ms=_positive_duration_ms(format_data.get("duration"), stream.get("duration")),
        width=_positive_int(stream.get("width")),
        height=_positive_int(stream.get("height")),
        fps_num=frame_rate.numerator,
        fps_den=frame_rate.denominator,
    )


def _read_probe_stdout(
    stream: BinaryIO,
    process: subprocess.Popen[bytes],
    output: bytearray,
    output_too_large: threading.Event,
    output_failed: threading.Event,
) -> None:
    try:
        while True:
            remaining = MEDIA_PROBE_MAX_OUTPUT_BYTES + 1 - len(output)
            if remaining <= 0:
                output_too_large.set()
                process.kill()
                return
            chunk = stream.read(min(8192, remaining))
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > MEDIA_PROBE_MAX_OUTPUT_BYTES:
                output_too_large.set()
                process.kill()
                return
    except (OSError, ValueError):
        output_failed.set()
        try:
            process.kill()
        except OSError:
            pass


def probe_media(descriptor: int, settings: Settings) -> MediaProbeResult:
    probe_path = f"/dev/fd/{descriptor}"
    command = [
        settings.media_probe_command,
        "-hide_banner",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-select_streams",
        "v",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,duration,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        probe_path,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            pass_fds=(descriptor,),
            start_new_session=True,
        )
    except OSError:
        raise _media_probe_unavailable() from None

    if process.stdout is None:
        process.kill()
        process.wait()
        raise _media_probe_unavailable()

    output = bytearray()
    output_too_large = threading.Event()
    output_failed = threading.Event()
    reader = threading.Thread(
        target=_read_probe_stdout,
        args=(process.stdout, process, output, output_too_large, output_failed),
        daemon=True,
        name="media-probe-stdout",
    )
    reader.start()
    try:
        process.wait(timeout=settings.media_probe_timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=1)
        process.stdout.close()
        raise _media_probe_unavailable() from None

    reader.join(timeout=1)
    if reader.is_alive():
        process.stdout.close()
        raise _media_probe_unavailable()
    process.stdout.close()
    if output_failed.is_set():
        raise _media_probe_unavailable()
    if output_too_large.is_set() or process.returncode != 0:
        raise _media_probe_rejected()
    return parse_media_probe_output(bytes(output))


def _valid_iso_bmff_brand(brand: bytes) -> bool:
    return len(brand) == 4 and all(0x20 <= byte <= 0x7E for byte in brand)


def _iso_bmff_brands(data: bytes) -> frozenset[bytes]:
    if len(data) < 16 or data[4:8] != b"ftyp":
        raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件魔数不是允许的视频格式")

    short_size = int.from_bytes(data[0:4], byteorder="big")
    if short_size == 0:
        raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件魔数不是允许的视频格式")

    if short_size == 1:
        if len(data) < 24:
            raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件魔数不是允许的视频格式")
        box_size = int.from_bytes(data[8:16], byteorder="big")
        brand_offset = 16
    else:
        box_size = short_size
        brand_offset = 8

    minimum_size = brand_offset + 8
    if box_size < minimum_size or box_size > ISO_BMFF_FTYP_MAX_BYTES or box_size > len(data):
        raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件魔数不是允许的视频格式")

    brand_payload = data[brand_offset:box_size]
    compatible_brand_bytes = brand_payload[8:]
    if len(compatible_brand_bytes) % 4 != 0:
        raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件魔数不是允许的视频格式")

    brands = {
        brand_payload[0:4],
        *(compatible_brand_bytes[index : index + 4] for index in range(0, len(compatible_brand_bytes), 4)),
    }
    if not all(_valid_iso_bmff_brand(brand) for brand in brands):
        raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件魔数不是允许的视频格式")
    return frozenset(brands)


def detect_magic(data: bytes) -> str:
    _iso_bmff_brands(data)
    return "iso-bmff"


def validate_media_identity(filename: str, declared_mime: str, data: bytes) -> None:
    suffix = Path(filename).suffix.lower()
    detect_magic(data)
    if not any(suffix in rule["extensions"] and declared_mime in rule["mimes"] for rule in MEDIA_SIGNATURES.values()):
        raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件扩展名与声明 MIME 不匹配")


class LocalMediaService:
    def __init__(self, session: Session, settings: Settings, context: ExecutionContext) -> None:
        self.session = session
        self.settings = settings
        self.context = context
        self.storage_root = settings.storage_root
        try:
            self.upload_root = ensure_private_directory(self.storage_root, "uploads")
            self.file_root = ensure_private_directory(self.storage_root, "files")
        except (OSError, ValueError) as exc:
            raise ReviewError("STORAGE_UNAVAILABLE", "上传存储目录不可用") from exc

    def init_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.context.principal.id:
            raise ReviewError("PRINCIPAL_AUTHENTICATION_REQUIRED", "缺少可信 principal 上下文")
        if payload["mime_type"] not in ALLOWED_MIME:
            raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件类型不允许")
        suffix = Path(payload["original_filename"]).suffix.lower()
        if not any(suffix in rule["extensions"] and payload["mime_type"] in rule["mimes"] for rule in MEDIA_SIGNATURES.values()):
            raise ReviewError("FILE_TYPE_NOT_ALLOWED", "文件扩展名与 MIME 不匹配")
        if payload["file_size"] > self.settings.max_upload_bytes:
            raise ReviewError("FILE_TOO_LARGE", "文件过大")
        reservation_bytes = payload["file_size"] * UPLOAD_PEAK_RESERVATION_MULTIPLIER
        self._reserve_upload_quota(reservation_bytes)
        upload_id = new_id("upl")
        session = UploadSessionModel(
            id=upload_id,
            original_filename=payload["original_filename"],
            mime_type=payload["mime_type"],
            declared_size=payload["file_size"],
            expected_sha256=payload["sha256"],
            status="initiated",
            temp_path=str(self._contained_path(self.upload_root / f"{upload_id}.parts", self.upload_root)),
            owner_principal_id=self.context.principal.id,
            owner_principal_kind=self.context.principal.kind,
            received_parts={},
            reserved_bytes=reservation_bytes,
            parts_cleanup_confirmed_at=None,
            duration_ms=payload.get("duration_ms", 1000),
            width=payload.get("width", 1920),
            height=payload.get("height", 1080),
            fps_num=payload.get("fps_num", 25),
            fps_den=payload.get("fps_den", 1),
        )
        self.session.add(session)
        self.session.flush()
        return self.dto(session)

    def _reserve_upload_quota(self, reservation_bytes: int) -> None:
        bind = self.session.get_bind()
        if bind.dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": UPLOAD_INIT_ADVISORY_LOCK_KEY},
            )
        active_reservation = UploadSessionModel.parts_cleanup_confirmed_at.is_(None)
        global_count, global_bytes = self.session.execute(
            select(
                func.count(UploadSessionModel.id),
                func.coalesce(func.sum(UploadSessionModel.reserved_bytes), 0),
            ).where(active_reservation)
        ).one()
        principal_count, principal_bytes = self.session.execute(
            select(
                func.count(UploadSessionModel.id),
                func.coalesce(func.sum(UploadSessionModel.reserved_bytes), 0),
            ).where(
                active_reservation,
                UploadSessionModel.owner_principal_id == self.context.principal.id,
                UploadSessionModel.owner_principal_kind == self.context.principal.kind,
            )
        ).one()
        if int(global_count) >= self.settings.max_active_upload_sessions_global:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "全局上传会话配额已满")
        if int(principal_count) >= self.settings.max_active_upload_sessions_per_principal:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "当前主体上传会话配额已满")
        if int(global_bytes) + reservation_bytes > self.settings.max_reserved_upload_bytes_global:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "全局上传预留空间配额不足")
        if int(principal_bytes) + reservation_bytes > self.settings.max_reserved_upload_bytes_per_principal:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "当前主体上传预留空间配额不足")
        try:
            free_bytes = shutil.disk_usage(self.storage_root).free
        except OSError as exc:
            raise ReviewError("STORAGE_UNAVAILABLE", "无法检查上传存储可用空间") from exc
        conservatively_available = free_bytes - int(global_bytes) - reservation_bytes
        if conservatively_available < self.settings.upload_storage_low_watermark_bytes:
            raise ReviewError("STORAGE_UNAVAILABLE", "上传存储已达到低水位保护线")

    def _max_part_write_bytes(self, upload: UploadSessionModel, part_no: int) -> int:
        if not isinstance(upload.received_parts, dict) or len(upload.received_parts) > self.settings.max_upload_parts:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片元数据无效")
        other_parts_size = 0
        for stored_part_no, part in upload.received_parts.items():
            if not isinstance(stored_part_no, str) or not stored_part_no.isdigit() or not isinstance(part, dict):
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片元数据无效")
            part_size = part.get("size")
            if not isinstance(part_size, int) or isinstance(part_size, bool) or part_size <= 0:
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片元数据无效")
            if stored_part_no != str(part_no):
                other_parts_size += part_size
        remaining_bytes = upload.declared_size - other_parts_size
        if remaining_bytes <= 0:
            raise ReviewError("FILE_TOO_LARGE", "文件已达到声明大小")
        return min(self.settings.max_upload_part_bytes, remaining_bytes)

    def ensure_can_stream_part(self, upload_id: str, part_no: int) -> int:
        if not 1 <= part_no <= self.settings.max_upload_parts:
            raise ReviewError("VALIDATION_ERROR", "上传分片号超出允许范围")
        session = self._get_upload(upload_id)
        self._assert_upload_owner(session)
        if session.status not in {"initiated", "receiving"}:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传会话状态不允许继续写入")
        return self._max_part_write_bytes(session, part_no)

    def claim_part_stream(self, upload_id: str, part_no: int) -> int:
        session = self.ensure_can_receive_part(upload_id, part_no)
        max_write_bytes = self._max_part_write_bytes(session, part_no)
        session.updated_at = utcnow()
        self.session.flush()
        return max_write_bytes

    def ensure_can_receive_part(self, upload_id: str, part_no: int) -> UploadSessionModel:
        if not 1 <= part_no <= self.settings.max_upload_parts:
            raise ReviewError("VALIDATION_ERROR", "上传分片号超出允许范围")
        session = self._get_upload_for_update(upload_id)
        self._assert_upload_owner(session)
        if session.status not in {"initiated", "receiving"}:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传会话状态不允许继续写入")
        return session

    def put_part_file(
        self,
        upload_id: str,
        part_no: int,
        staged_path: Path,
        size: int,
        *,
        locked_upload: UploadSessionModel | None = None,
    ) -> PreparedUploadPart:
        if not 1 <= part_no <= self.settings.max_upload_parts:
            raise ReviewError("VALIDATION_ERROR", "上传分片号超出允许范围")
        if size <= 0:
            raise ReviewError("VALIDATION_ERROR", "上传分片不能为空")
        if size > self.settings.max_upload_part_bytes:
            raise ReviewError("FILE_TOO_LARGE", "上传分片过大")
        session = locked_upload if locked_upload is not None else self.ensure_can_receive_part(upload_id, part_no)
        if session.id != upload_id:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传会话锁与分片不匹配")
        if locked_upload is not None:
            self._assert_upload_owner(session)
            if session.status not in {"initiated", "receiving"}:
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传会话状态不允许继续写入")
        max_write_bytes = self._max_part_write_bytes(session, part_no)
        if size > max_write_bytes:
            raise ReviewError("FILE_TOO_LARGE", "文件过大")
        try:
            staged_path = validated_upload_part_path(staged_path, self.upload_root, upload_id, session.temp_path, part_no)
            with pin_regular_file(staged_path, self.upload_root) as pinned:
                if pinned is None or not pinned.exists or pinned.size != size:
                    raise ReviewError("STORAGE_UNAVAILABLE", "上传分片暂存文件无效")
        except (OSError, ValueError) as exc:
            raise ReviewError("VALIDATION_ERROR", "上传分片路径非法") from exc
        received_parts = dict(session.received_parts)
        if str(part_no) not in received_parts and len(received_parts) >= self.settings.max_upload_parts:
            raise ReviewError("VALIDATION_ERROR", "上传分片数量超出允许范围")
        existing_part_size = int(received_parts.get(str(part_no), {}).get("size", 0))
        current_size = sum(int(part["size"]) for part in received_parts.values()) - existing_part_size + size
        if current_size > session.declared_size or current_size > self.settings.max_upload_bytes:
            raise ReviewError("FILE_TOO_LARGE", "文件过大")
        superseded_path: Path | None = None
        existing_part = received_parts.get(str(part_no))
        if isinstance(existing_part, dict) and isinstance(existing_part.get("path"), str):
            try:
                existing_path = validated_upload_part_path(existing_part["path"], self.upload_root, upload_id, session.temp_path, part_no)
            except (OSError, ValueError) as exc:
                raise ReviewError("RESOURCE_STATE_CONFLICT", "已有上传分片元数据无效") from exc
            if existing_path != staged_path:
                superseded_path = existing_path
        received_parts[str(part_no)] = {"size": size, "path": str(staged_path)}
        session.received_parts = received_parts
        session.status = "receiving"
        session.updated_at = utcnow()
        self.session.flush()
        return PreparedUploadPart(
            response=self.dto(session),
            candidate_path=staged_path,
            superseded_path=superseded_path,
        )

    def put_part(self, upload_id: str, part_no: int, data: bytes) -> dict[str, Any]:
        locked_upload = self.ensure_can_receive_part(upload_id, part_no)
        if len(data) > self.settings.max_upload_part_bytes:
            raise ReviewError("FILE_TOO_LARGE", "上传分片过大")
        staged_path = new_upload_part_path(self.upload_root, upload_id, part_no)
        try:
            with exclusive_file_writer(staged_path, self.upload_root) as handle:
                handle.write(data)
            prepared = self.put_part_file(
                upload_id,
                part_no,
                staged_path,
                len(data),
                locked_upload=locked_upload,
            )
        except Exception:
            unlink_regular_file(staged_path, self.upload_root)
            raise
        return prepared.response

    def discard_uncommitted_part(self, prepared: PreparedUploadPart) -> None:
        unlink_regular_file(prepared.candidate_path, self.upload_root)

    def discard_superseded_part(self, prepared: PreparedUploadPart) -> None:
        if prepared.superseded_path is not None:
            unlink_regular_file(prepared.superseded_path, self.upload_root)

    def _completion_part_paths(self, upload: UploadSessionModel) -> tuple[Path, ...]:
        if len(upload.received_parts) > self.settings.max_upload_parts:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片数量超出允许范围")
        ordered_parts = sorted(upload.received_parts.items(), key=lambda item: int(item[0]))
        part_paths: list[Path] = []
        for part_number, part in ordered_parts:
            if not isinstance(part, dict) or not isinstance(part.get("path"), str):
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片元数据无效")
            try:
                part_paths.append(validated_upload_part_path(part["path"], self.upload_root, upload.id, upload.temp_path, int(part_number)))
            except (OSError, TypeError, ValueError) as exc:
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片元数据无效") from exc
        return tuple(part_paths)

    def claim_completion(
        self,
        upload_id: str,
        *,
        idempotency_key_hash: str,
        request_hash: str,
        allow_active_lease_resume: bool = False,
    ) -> UploadFinalizationClaim | PreparedUploadCompletion:
        if not re.fullmatch(r"[0-9a-f]{64}", idempotency_key_hash) or not re.fullmatch(r"[0-9a-f]{64}", request_hash):
            raise ReviewError("VALIDATION_ERROR", "上传完成幂等身份无效")
        upload = self._get_upload_for_update(upload_id)
        self._assert_upload_owner(upload)
        part_paths = self._completion_part_paths(upload)
        if upload.status == "completed":
            return PreparedUploadCompletion(
                response=self.dto(upload),
                final_path=None,
                final_device=None,
                final_inode=None,
                part_paths=part_paths,
            )
        if upload.status == "aborted":
            raise ReviewError("UPLOAD_INCOMPLETE", "上传已终止")
        if not part_paths:
            raise ReviewError("UPLOAD_INCOMPLETE", "上传未完成")

        recovered_expired_lease = False
        resumed_active_lease = False
        file_id = new_id("file")
        lease_id = new_id("lease")
        if upload.status == "finalizing":
            if (
                not upload.finalization_idempotency_key_hash
                or not upload.finalization_request_hash
                or not secrets.compare_digest(upload.finalization_idempotency_key_hash, idempotency_key_hash)
                or not secrets.compare_digest(upload.finalization_request_hash, request_hash)
            ):
                raise ReviewError("IDEMPOTENCY_CONFLICT", "上传完成操作身份不匹配")
            expires_at = upload.finalization_lease_expires_at
            now = utcnow()
            comparable_now = now if expires_at is None or expires_at.tzinfo is not None else now.replace(tzinfo=None)
            if expires_at is None or expires_at > comparable_now:
                if not allow_active_lease_resume:
                    raise ReviewError("RESOURCE_STATE_CONFLICT", "上传正在完成处理中")
                resumed_active_lease = True
            if not upload.finalization_file_id:
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传完成租约元数据无效")
            file_id = upload.finalization_file_id
            if resumed_active_lease:
                if not upload.finalization_lease_id:
                    raise ReviewError("RESOURCE_STATE_CONFLICT", "上传完成租约元数据无效")
                lease_id = upload.finalization_lease_id
            else:
                recovered_expired_lease = True
        elif upload.status not in {"initiated", "receiving"}:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传会话状态不允许完成")

        if not resumed_active_lease:
            upload.status = "finalizing"
            upload.finalization_lease_id = lease_id
            upload.finalization_lease_expires_at = utcnow() + timedelta(seconds=self.settings.upload_finalization_lease_seconds)
            upload.finalization_file_id = file_id
            upload.finalization_idempotency_key_hash = idempotency_key_hash
            upload.finalization_request_hash = request_hash
            upload.updated_at = utcnow()
            self.session.flush()
        return UploadFinalizationClaim(
            upload_id=upload.id,
            lease_id=lease_id,
            file_id=file_id,
            recovered_expired_lease=recovered_expired_lease,
            resumed_active_lease=resumed_active_lease,
            original_filename=upload.original_filename,
            mime_type=upload.mime_type,
            declared_size=upload.declared_size,
            expected_sha256=upload.expected_sha256,
            owner_principal_id=upload.owner_principal_id,
            owner_principal_kind=upload.owner_principal_kind,
            part_paths=part_paths,
        )

    def _inspect_final_descriptor(
        self,
        descriptor: int,
        claim: UploadFinalizationClaim,
    ) -> tuple[os.stat_result, str, MediaProbeResult]:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != claim.declared_size:
            raise ReviewError("UPLOAD_INCOMPLETE", "上传大小与声明不一致")
        sha256_hash = hashlib.sha256()
        file_type_header = bytearray()
        bytes_hashed = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            if len(file_type_header) < ISO_BMFF_FTYP_MAX_BYTES:
                remaining = ISO_BMFF_FTYP_MAX_BYTES - len(file_type_header)
                file_type_header.extend(chunk[:remaining])
            bytes_hashed += len(chunk)
            sha256_hash.update(chunk)
        if bytes_hashed != metadata.st_size:
            raise UnsafeFilePathError("upload output changed while it was hashed")
        hashed_metadata = os.fstat(descriptor)
        if (
            hashed_metadata.st_dev != metadata.st_dev
            or hashed_metadata.st_ino != metadata.st_ino
            or hashed_metadata.st_size != metadata.st_size
            or hashed_metadata.st_mtime_ns != metadata.st_mtime_ns
            or hashed_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise UnsafeFilePathError("upload output changed while it was hashed")
        validate_media_identity(claim.original_filename, claim.mime_type, bytes(file_type_header))
        sha256 = sha256_hash.hexdigest()
        if claim.expected_sha256 != SERVER_COMPUTED_SHA256 and sha256 != claim.expected_sha256:
            raise ReviewError("FILE_HASH_MISMATCH", "文件 SHA-256 不匹配")
        os.lseek(descriptor, 0, os.SEEK_SET)
        probe = probe_media(descriptor, self.settings)
        probed_metadata = os.fstat(descriptor)
        if (
            probed_metadata.st_dev != metadata.st_dev
            or probed_metadata.st_ino != metadata.st_ino
            or probed_metadata.st_size != metadata.st_size
            or probed_metadata.st_mtime_ns != metadata.st_mtime_ns
            or probed_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise UnsafeFilePathError("upload output changed while it was probed")
        return probed_metadata, sha256, probe

    def _reuse_finalized_file(
        self,
        claim: UploadFinalizationClaim,
        storage_path: Path,
    ) -> FinalizedUploadFile | None:
        with pin_regular_file(storage_path, self.file_root) as pinned:
            if pinned is None or not pinned.exists:
                return None
            with pinned.open_readonly() as handle:
                metadata, sha256, probe = self._inspect_final_descriptor(handle.fileno(), claim)
            if pinned.device != metadata.st_dev or pinned.inode != metadata.st_ino:
                raise UnsafeFilePathError("recovered upload file identity changed")
            return FinalizedUploadFile(
                file_id=claim.file_id,
                storage_path=storage_path,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                sha256=sha256,
                probe=probe,
                part_paths=claim.part_paths,
            )

    def finalize_claim(self, claim: UploadFinalizationClaim) -> FinalizedUploadFile:
        try:
            storage_path = self._contained_path(self.file_root / claim.file_id, self.file_root)
            recovered = self._reuse_finalized_file(claim, storage_path)
            if recovered is not None:
                return recovered
            staging_path = self._contained_path(self.file_root / f".{claim.file_id}.{secrets.token_hex(16)}.tmp", self.file_root)
        except ReviewError as exc:
            raise ReviewError("STORAGE_UNAVAILABLE", "上传存储目录不可用") from exc
        except (OSError, ValueError) as exc:
            raise ReviewError("STORAGE_UNAVAILABLE", "上传存储目录不可用") from exc

        try:
            managed_root = pin_managed_root(self.file_root)
            _root_path, file_root_fd = managed_root.__enter__()
        except (OSError, ValueError) as exc:
            raise ReviewError("STORAGE_UNAVAILABLE", "上传存储目录不可用") from exc
        descriptor = -1
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(staging_path.name, flags, 0o600, dir_fd=file_root_fd)
            output_descriptor = os.dup(descriptor)
            with os.fdopen(output_descriptor, "wb") as output:
                for part_path in claim.part_paths:
                    with pin_regular_file(part_path, self.upload_root) as pinned:
                        if pinned is None or not pinned.exists:
                            raise ReviewError("UPLOAD_INCOMPLETE", "上传分片不存在")
                        with pinned.open_readonly() as handle:
                            while chunk := handle.read(1024 * 1024):
                                output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            probe_metadata, sha256, probe = self._inspect_final_descriptor(descriptor, claim)
            if not stat.S_ISREG(probe_metadata.st_mode) or probe_metadata.st_dev != os.fstat(file_root_fd).st_dev:
                raise UnsafeFilePathError("upload output is not a regular file on the configured filesystem")
            staging_metadata = os.stat(staging_path.name, dir_fd=file_root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(staging_metadata.st_mode)
                or staging_metadata.st_dev != probe_metadata.st_dev
                or staging_metadata.st_ino != probe_metadata.st_ino
            ):
                raise UnsafeFilePathError("upload staging file changed after validation")
            try:
                os.link(
                    staging_path.name,
                    storage_path.name,
                    src_dir_fd=file_root_fd,
                    dst_dir_fd=file_root_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                os.unlink(staging_path.name, dir_fd=file_root_fd)
                os.fsync(file_root_fd)
                recovered = self._reuse_finalized_file(claim, storage_path)
                if recovered is None:
                    raise ReviewError("RESOURCE_STATE_CONFLICT", "上传完成文件竞态尚未收敛") from None
                return recovered
            published_metadata = os.stat(storage_path.name, dir_fd=file_root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(published_metadata.st_mode)
                or published_metadata.st_dev != probe_metadata.st_dev
                or published_metadata.st_ino != probe_metadata.st_ino
            ):
                raise UnsafeFilePathError("published upload does not match the validated file")
            os.unlink(staging_path.name, dir_fd=file_root_fd)
            os.fsync(file_root_fd)
        except Exception as exc:
            if descriptor >= 0:
                pinned_metadata = os.fstat(descriptor)
                try:
                    candidate_metadata = os.stat(staging_path.name, dir_fd=file_root_fd, follow_symlinks=False)
                    if (
                        stat.S_ISREG(candidate_metadata.st_mode)
                        and candidate_metadata.st_dev == pinned_metadata.st_dev
                        and candidate_metadata.st_ino == pinned_metadata.st_ino
                    ):
                        os.unlink(staging_path.name, dir_fd=file_root_fd)
                except FileNotFoundError:
                    pass
                # A successor finalizer can commit the canonical hard link after
                # this lease expires. Exception cleanup owns only its staging name.
                os.fsync(file_root_fd)
            if isinstance(exc, UnsafeFilePathError):
                raise ReviewError("STORAGE_UNAVAILABLE", "上传分片文件不安全") from exc
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            managed_root.__exit__(None, None, None)
        return FinalizedUploadFile(
            file_id=claim.file_id,
            storage_path=storage_path,
            device=probe_metadata.st_dev,
            inode=probe_metadata.st_ino,
            sha256=sha256,
            probe=probe,
            part_paths=claim.part_paths,
        )

    def publish_completion(
        self,
        claim: UploadFinalizationClaim,
        finalized: FinalizedUploadFile,
    ) -> PreparedUploadCompletion:
        upload = self._get_upload_for_update(claim.upload_id)
        self._assert_upload_owner(upload)
        if upload.status == "completed" and upload.file_id == finalized.file_id:
            return PreparedUploadCompletion(
                response=self.dto(upload),
                final_path=finalized.storage_path,
                final_device=finalized.device,
                final_inode=finalized.inode,
                part_paths=self._completion_part_paths(upload),
            )
        if upload.status != "finalizing" or upload.finalization_lease_id != claim.lease_id or upload.finalization_file_id != finalized.file_id:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传完成租约已失效")
        with pin_regular_file(finalized.storage_path, self.file_root) as pinned:
            if pinned is None or not pinned.exists or pinned.device != finalized.device or pinned.inode != finalized.inode:
                raise ReviewError("STORAGE_UNAVAILABLE", "上传完成文件身份已变化")
        file = FileObjectModel(
            id=finalized.file_id,
            original_filename=claim.original_filename,
            mime_type=claim.mime_type,
            file_size=claim.declared_size,
            sha256=finalized.sha256,
            storage_path=str(finalized.storage_path),
            owner_principal_id=claim.owner_principal_id,
            owner_principal_kind=claim.owner_principal_kind,
            duration_ms=finalized.probe.duration_ms,
            width=finalized.probe.width,
            height=finalized.probe.height,
            fps_num=finalized.probe.fps_num,
            fps_den=finalized.probe.fps_den,
            media_probe_version=MEDIA_PROBE_VERSION,
        )
        self.session.add(file)
        upload.status = "completed"
        upload.file_id = finalized.file_id
        upload.finalization_lease_id = None
        upload.finalization_lease_expires_at = None
        upload.finalization_file_id = None
        upload.finalization_idempotency_key_hash = None
        upload.finalization_request_hash = None
        upload.updated_at = utcnow()
        self.session.flush()
        return PreparedUploadCompletion(
            response=self.dto(upload),
            final_path=finalized.storage_path,
            final_device=finalized.device,
            final_inode=finalized.inode,
            part_paths=finalized.part_paths,
        )

    def release_failed_finalization(
        self,
        claim: UploadFinalizationClaim,
        *,
        abort: bool,
    ) -> PreparedUploadAbort | None:
        upload = self._get_upload_for_update(claim.upload_id)
        self._assert_upload_owner(upload)
        if upload.status == "completed":
            return None
        if upload.status != "finalizing" or upload.finalization_lease_id != claim.lease_id:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传完成租约已失效")
        upload.status = "aborted" if abort else "receiving"
        upload.finalization_lease_id = None
        upload.finalization_lease_expires_at = None
        upload.finalization_file_id = None
        upload.finalization_idempotency_key_hash = None
        upload.finalization_request_hash = None
        if abort:
            upload.parts_cleanup_confirmed_at = None
        upload.updated_at = utcnow()
        self.session.flush()
        if not abort:
            return None
        return PreparedUploadAbort(
            response=self.dto(upload),
            part_paths=self._completion_part_paths(upload),
        )

    def confirm_part_cleanup(self, upload_id: str, expected_status: str) -> None:
        upload = self._get_upload_for_update(upload_id)
        self._assert_upload_owner(upload)
        if upload.status != expected_status or expected_status not in {"completed", "aborted"}:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传清理确认状态不匹配")
        upload.received_parts = {}
        upload.parts_cleanup_confirmed_at = utcnow()
        upload.updated_at = utcnow()
        self.session.flush()

    def prepare_abort(self, upload_id: str) -> PreparedUploadAbort:
        session = self._get_upload_for_update(upload_id)
        self._assert_upload_owner(session)
        if session.file_id:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "已经绑定业务文件的上传不可 abort")
        if session.status == "finalizing":
            raise ReviewError("RESOURCE_STATE_CONFLICT", "上传正在完成处理中")
        part_paths: list[Path] = []
        for part_number, part in session.received_parts.items():
            if not isinstance(part, dict) or not isinstance(part.get("path"), str):
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片元数据无效")
            try:
                part_paths.append(validated_upload_part_path(part["path"], self.upload_root, upload_id, session.temp_path, int(part_number)))
            except (OSError, TypeError, ValueError) as exc:
                raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片元数据无效") from exc
        session.status = "aborted"
        session.parts_cleanup_confirmed_at = None
        session.updated_at = utcnow()
        self.session.flush()
        return PreparedUploadAbort(response=self.dto(session), part_paths=tuple(part_paths))

    def cleanup_part_paths(self, part_paths: tuple[Path, ...]) -> None:
        first_error: OSError | None = None
        for path in part_paths:
            try:
                unlink_regular_file(path, self.upload_root)
            except OSError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def discard_uncommitted_file(self, file_id: str, *, device: int, inode: int) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", file_id):
            raise ReviewError("VALIDATION_ERROR", "file_id 包含非法路径字符")
        path = self._contained_path(self.file_root / file_id, self.file_root)
        unlink_regular_file_if_identity(path, self.file_root, device=device, inode=inode)

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        session = self._get_upload(upload_id)
        self._assert_upload_owner(session)
        return self.dto(session)

    def _get_upload(self, upload_id: str) -> UploadSessionModel:
        session = self.session.get(UploadSessionModel, upload_id)
        if not session:
            raise not_found()
        return session

    def _get_upload_for_update(self, upload_id: str) -> UploadSessionModel:
        self.session.expire_all()
        session = self.session.scalar(select(UploadSessionModel).where(UploadSessionModel.id == upload_id).with_for_update())
        if not session:
            raise not_found()
        return session

    def _assert_upload_owner(self, session: UploadSessionModel) -> None:
        if not self.context.principal.id:
            raise ReviewError("PRINCIPAL_AUTHENTICATION_REQUIRED", "缺少可信 principal 上下文")
        if session.owner_principal_id != self.context.principal.id or session.owner_principal_kind != self.context.principal.kind:
            raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "当前 principal 无权访问该上传")

    @staticmethod
    def _contained_path(path: Path, root: Path) -> Path:
        try:
            return contained_path(path, root)
        except UnsafeFilePathError as exc:
            raise ReviewError("VALIDATION_ERROR", "上传路径越界") from exc

    @staticmethod
    def dto(session: UploadSessionModel) -> dict[str, Any]:
        received = sum(int(part["size"]) for part in session.received_parts.values())
        public_status = "receiving" if session.status == "finalizing" else session.status
        return UploadSessionDTO(
            upload_id=session.id,
            status=public_status,  # type: ignore[arg-type]
            original_filename=session.original_filename,
            mime_type=session.mime_type,
            declared_size=session.declared_size,
            received_size=received,
            file_id=session.file_id,
        ).model_dump(mode="json")


def parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int, int]:
    if not range_header:
        return 0, file_size - 1, 200
    if not range_header.startswith("bytes="):
        raise ReviewError("VALIDATION_ERROR", "Range header 非法")
    start_s, _, end_s = range_header.removeprefix("bytes=").partition("-")
    try:
        if not start_s and end_s:
            suffix_len = int(end_s)
            if suffix_len <= 0:
                raise ValueError
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        else:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
    except ValueError as exc:
        raise ReviewError("VALIDATION_ERROR", "Range header 非法") from exc
    if start < 0 or end < start or end >= file_size:
        raise ReviewError("VALIDATION_ERROR", "Range header 越界")
    return start, end, 206

"""Type verification, malware scanning and archive safety (SEC-UPL-02..09, HUB-04).

Uploaded files are hostile until proven otherwise. A file leaves quarantine only if:
  1. its real type (from content, not name) is on the allow-list and matches its extension,
  2. every scan engine reports clean,
  3. structural checks pass (archive limits, zip-slip, polyglots, decoder bombs).

Engines:
  * ``SignatureEngine``  built-in: EICAR, executable/script/macro markers, polyglots.
  * ``ClamdEngine``      optional: ClamAV daemon over its INSTREAM protocol (set CP_CLAMD_HOST).
If a configured engine is unreachable the verdict is ``error`` and the file stays in
quarantine (fail closed, HA-18). The built-in engine is a baseline, not a replacement for
a commercial scanner; production must run with at least clamd enabled.
"""

from __future__ import annotations

import os
import socket
import stat
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..config import Settings, get_settings

ALLOWED_TYPES = {
    "csv", "tsv", "xlsx", "json", "ndjson", "xml", "zip", "jpeg", "png", "webp", "gif", "pdf",
}
EXTENSIONS = {
    "csv": {"csv", "txt"}, "tsv": {"tsv", "tab", "txt"}, "xlsx": {"xlsx"}, "json": {"json"},
    "ndjson": {"ndjson", "jsonl"}, "xml": {"xml"}, "zip": {"zip"}, "jpeg": {"jpg", "jpeg"},
    "png": {"png"}, "webp": {"webp"}, "gif": {"gif"}, "pdf": {"pdf"},
}
IMAGE_TYPES = {"jpeg", "png", "webp", "gif"}
MAX_IMAGE_DIM = 25_000
MAX_IMAGE_PIXELS = 100_000_000

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


@dataclass
class Verdict:
    engine: str
    verdict: str  # clean | infected | suspicious | error | type_mismatch | limit_exceeded
    detail: str = ""

    @property
    def clean(self) -> bool:
        return self.verdict == "clean"


def _head(path: Path, n: int = 8192) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def detect_type(path: Path) -> str:
    """Returns an allow-listed type name, or 'rejected:<reason>'."""
    head = _head(path)
    if not head:
        return "rejected:empty file"
    if head[:2] == b"MZ" or head[:4] == b"\x7fELF" or head[:4] in (
        b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
    ):
        return "rejected:executable"
    if head[:2] == b"#!":
        return "rejected:script"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "rejected:legacy Office (OLE) file; save as .xlsx or .csv"
    if head[:4] in (b"Rar!", b"7z\xbc\xaf"):
        return "rejected:only ZIP archives are accepted"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _detect_zip(path)
    return _detect_text(head)


def _detect_zip(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return "rejected:corrupt archive"
    if "[Content_Types].xml" in names:
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            return "rejected:macro-enabled workbook"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "rejected:unsupported Office document"
    return "zip"


def _detect_text(head: bytes) -> str:
    text = None
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = head.decode("utf-16", errors="replace")
    else:
        for enc in ("utf-8-sig", "cp1252"):
            try:
                # Tolerate a multi-byte character cut by the read boundary.
                text = head[: len(head) - 3].decode(enc) if len(head) >= 8192 else head.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    if text is None or "\x00" in text:
        return "rejected:unrecognised binary content"
    sample = text.lstrip()
    lowered = sample[:2048].lower()
    # Only markup documents are judged here; "<script" inside a CSV cell is data and is
    # removed later by HTML sanitization.
    if lowered.startswith(("<!doctype html", "<html", "<script")) or (sample.startswith("<") and "<script" in lowered):
        return "rejected:HTML or script content"
    if lowered.startswith("<svg") or ("<svg" in lowered and lowered.startswith("<?xml")):
        return "rejected:SVG is not accepted"
    if sample.startswith("<"):
        return "xml"
    if sample.startswith("["):
        return "json"
    if sample.startswith("{"):
        first_line = sample.split("\n", 1)[0].strip()
        rest = sample.split("\n", 1)[1].lstrip() if "\n" in sample else ""
        return "ndjson" if first_line.endswith("}") and rest.startswith("{") else "json"
    first = sample.split("\n", 1)[0]
    return "tsv" if first.count("\t") > max(first.count(","), first.count(";")) else "csv"


def extension_matches(name: str, detected: str) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in EXTENSIONS.get(detected, set())


# ---------------------------------------------------------------- engines

class SignatureEngine:
    name = "builtin-signatures/1"

    def scan(self, path: Path, detected: str) -> Verdict:
        size = path.stat().st_size
        with path.open("rb") as fh:
            overlap = b""
            offset = 0
            while chunk := fh.read(1 << 20):
                window = overlap + chunk
                if EICAR in window:
                    return Verdict(self.name, "infected", "EICAR test signature")
                if detected in IMAGE_TYPES or detected == "pdf":
                    # Polyglots: an image or PDF that is also HTML/JS or carries an archive.
                    lowered = window.lower()
                    if b"<script" in lowered or b"<html" in lowered or (detected != "pdf" and b"javascript:" in lowered):
                        return Verdict(self.name, "suspicious", "active content embedded in media file")
                    if detected in IMAGE_TYPES and b"PK\x03\x04" in window and offset + len(chunk) > 1024:
                        return Verdict(self.name, "suspicious", "archive appended to image (polyglot)")
                    if detected == "pdf" and (b"/javascript" in lowered or b"/launch" in lowered or b"/js " in lowered):
                        return Verdict(self.name, "suspicious", "PDF contains active content")
                overlap = chunk[-128:]
                offset += len(chunk)
        if size == 0:
            return Verdict(self.name, "error", "empty file")
        return Verdict(self.name, "clean")


class ClamdEngine:
    name = "clamd"

    def __init__(self, host: str, port: int = 3310, timeout: float = 30.0):
        self.host, self.port, self.timeout = host, port, timeout

    def scan(self, path: Path, detected: str) -> Verdict:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(b"zINSTREAM\0")
                with path.open("rb") as fh:
                    while chunk := fh.read(1 << 16):
                        sock.sendall(struct.pack("!I", len(chunk)) + chunk)
                sock.sendall(struct.pack("!I", 0))
                reply = b""
                while part := sock.recv(4096):
                    reply += part
        except OSError as exc:
            return Verdict(self.name, "error", f"scanner unavailable: {exc}")  # fail closed
        text = reply.decode(errors="replace").strip("\0\n ")
        if text.endswith("OK"):
            return Verdict(self.name, "clean")
        if "FOUND" in text:
            return Verdict(self.name, "infected", text[:200])
        return Verdict(self.name, "error", text[:200])


def engines() -> list:
    result: list = [SignatureEngine()]
    host = os.environ.get("CP_CLAMD_HOST")
    if host:
        result.append(ClamdEngine(host, int(os.environ.get("CP_CLAMD_PORT", "3310"))))
    return result


# ---------------------------------------------------------------- structural checks

def check_zip(path: Path, settings: Settings) -> Verdict:
    """Zip-bomb and zip-slip protection without extracting anything (SEC-UPL-05)."""
    name = "structure/zip"
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        return Verdict(name, "error", f"corrupt archive: {exc}")
    if len(infos) > settings.zip_max_entries:
        return Verdict(name, "limit_exceeded", f"more than {settings.zip_max_entries} entries")
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            return Verdict(name, "suspicious", "encrypted archives are not accepted")
        if stat.S_ISLNK(info.external_attr >> 16):
            return Verdict(name, "suspicious", f"symlink entry: {info.filename[:80]}")
        raw = info.filename.replace("\\", "/")
        pure = PurePosixPath(raw)
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or ".." in pure.parts:
            return Verdict(name, "suspicious", f"path traversal entry: {info.filename[:80]}")
        if info.is_dir():
            continue
        if raw.lower().endswith((".zip", ".rar", ".7z", ".gz", ".tar")):
            return Verdict(name, "limit_exceeded", "nested archives are not accepted")
        total += info.file_size
        if info.compress_size and info.file_size / info.compress_size > settings.zip_max_ratio and info.file_size > 1 << 20:
            return Verdict(name, "limit_exceeded", f"compression ratio above {settings.zip_max_ratio}:1")
        if total > settings.zip_max_uncompressed:
            return Verdict(name, "limit_exceeded", "uncompressed size above limit")
    return Verdict(name, "clean")


def check_image(path: Path, detected: str) -> Verdict:
    """Reject decompression bombs from header dimensions before any decoder runs (SEC-UPL-06)."""
    name = "structure/image"
    head = _head(path, 64)
    width = height = None
    if detected == "png" and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
    elif detected == "gif" and len(head) >= 10:
        width, height = struct.unpack("<HH", head[6:10])
    if width is not None:
        if width == 0 or height == 0:
            return Verdict(name, "error", "invalid image dimensions")
        if width > MAX_IMAGE_DIM or height > MAX_IMAGE_DIM or width * height > MAX_IMAGE_PIXELS:
            return Verdict(name, "limit_exceeded", f"image dimensions {width}x{height} above limit")
    return Verdict(name, "clean")


def check_xlsx(path: Path, settings: Settings) -> Verdict:
    name = "structure/xlsx"
    base = check_zip(path, settings)
    if not base.clean and base.verdict != "limit_exceeded":
        return Verdict(name, base.verdict, base.detail)
    try:
        with zipfile.ZipFile(path) as zf:
            total = sum(i.file_size for i in zf.infolist())
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return Verdict(name, "error", str(exc))
    if total > 4 * 1024**3:
        return Verdict(name, "limit_exceeded", "workbook expands beyond 4 GB")
    if any("embeddings/" in n.lower() or n.lower().endswith((".bin", ".exe", ".dll")) for n in names):
        return Verdict(name, "suspicious", "workbook contains embedded objects")
    return Verdict(name, "clean")


def scan_file(path: Path, original_name: str, settings: Settings | None = None) -> tuple[str | None, list[Verdict]]:
    """Returns (detected_type or None, verdicts). The file is releasable only if all are clean."""
    settings = settings or get_settings()
    verdicts: list[Verdict] = []
    if path.stat().st_size > settings.max_file_bytes:
        return None, [Verdict("limits", "limit_exceeded", "file is larger than the plan allows")]
    detected = detect_type(path)
    if detected.startswith("rejected:"):
        return None, [Verdict("type-check", "type_mismatch", detected.split(":", 1)[1])]
    if detected not in ALLOWED_TYPES:
        return None, [Verdict("type-check", "type_mismatch", f"type '{detected}' is not accepted")]
    if not extension_matches(original_name, detected):
        return detected, [Verdict(
            "type-check", "type_mismatch",
            f"file name says '.{original_name.rsplit('.', 1)[-1]}' but the content is {detected}",
        )]
    verdicts.append(Verdict("type-check", "clean", detected))
    for engine in engines():
        verdicts.append(engine.scan(path, detected))
    if detected == "zip":
        verdicts.append(check_zip(path, settings))
    elif detected == "xlsx":
        verdicts.append(check_xlsx(path, settings))
    elif detected in IMAGE_TYPES:
        verdicts.append(check_image(path, detected))
    return detected, verdicts

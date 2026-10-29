"""Security scan for uploaded documents.

Uploaded files are untrusted input. Two layers:

1. **Structural checks**, always on — the ones a document scanner can perform
   without an external daemon: active-content markers in PDF (`/JavaScript`,
   `/OpenAction`, `/Launch`, embedded files), macro parts in DOCX, and zip-bomb
   ratios.
2. **ClamAV**, off by default and enabled per environment
   (`PAPERS_CLAMAV_ENABLED`). No daemon ships in this deployment today, so the
   scanner is a configured capability rather than an assumed one — a scan stage
   that silently passes because nothing is listening is worse than no stage.

`scan()` never raises for a bad document; the verdict is the return value.
"""

from __future__ import annotations

import io
import socket
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from creditpaper.onboarding.services.papers import config

# Markers that make a PDF do something rather than say something.
PDF_ACTIVE_CONTENT = (
    b"/JavaScript",
    b"/JS",
    b"/OpenAction",
    b"/AA",
    b"/Launch",
    b"/EmbeddedFile",
    b"/RichMedia",
)

# OOXML macro parts. A credit paper has no reason to carry one.
DOCX_MACRO_MEMBERS = ("word/vbaProject.bin", "word/vbaData.xml")

# A legitimate DOCX compresses maybe 10-20x. Far beyond that is a decompression
# bomb aimed at the extraction stage.
MAX_ZIP_RATIO = 200


@dataclass
class ScanResult:
    clean: bool
    scanner: str = "structural"
    threat: str | None = None
    findings: list[str] = field(default_factory=list)


def scan(filename: str, content: bytes) -> ScanResult:
    ext = Path(filename).suffix.lower()

    structural = _scan_structural(ext, content)
    if not structural.clean:
        return structural

    if config.clamav_enabled():
        av = _scan_clamav(content)
        if av is not None:
            if not av.clean:
                return av
            return ScanResult(clean=True, scanner="clamav", findings=structural.findings)

    return structural


# --------------------------------------------------------------------------


def _scan_structural(ext: str, content: bytes) -> ScanResult:
    findings: list[str] = []

    if ext == ".pdf":
        for marker in PDF_ACTIVE_CONTENT:
            if marker in content:
                name = marker.decode("ascii", errors="replace")
                # /JS and /AA alone are weak signals — plenty of benign exports
                # carry them. Only executable actions are treated as a threat.
                if marker in (b"/Launch", b"/EmbeddedFile", b"/RichMedia"):
                    return ScanResult(
                        clean=False,
                        threat=f"active content: {name}",
                        findings=findings,
                    )
                findings.append(f"pdf-marker:{name}")

    if ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = set(zf.namelist())
                for macro in DOCX_MACRO_MEMBERS:
                    if macro in names:
                        return ScanResult(clean=False, threat="macro content", findings=findings)
                declared = sum(i.file_size for i in zf.infolist())
                if content and declared / max(len(content), 1) > MAX_ZIP_RATIO:
                    return ScanResult(clean=False, threat="compression ratio", findings=findings)
                if any(n.startswith("/") or ".." in n for n in names):
                    return ScanResult(clean=False, threat="path traversal in archive", findings=findings)
        except zipfile.BadZipFile:
            return ScanResult(clean=False, threat="unreadable archive", findings=findings)

    return ScanResult(clean=True, findings=findings)


def _scan_clamav(content: bytes) -> ScanResult | None:
    """INSTREAM against a clamd socket. Returns None when unreachable, so the
    caller can tell "scanned and clean" from "not scanned"."""
    host, port = config.clamav_address()
    if not host:
        return None
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(b"zINSTREAM\0")
            view = memoryview(content)
            chunk = 1 << 16
            for start in range(0, len(view), chunk):
                block = view[start : start + chunk]
                sock.sendall(len(block).to_bytes(4, "big") + bytes(block))
            sock.sendall((0).to_bytes(4, "big"))
            reply = sock.recv(4096).decode("utf-8", errors="replace").strip()
    except OSError:
        return None

    if "FOUND" in reply:
        threat = reply.split(":", 1)[-1].replace("FOUND", "").strip() or "malware"
        return ScanResult(clean=False, scanner="clamav", threat=threat)
    return ScanResult(clean=True, scanner="clamav")

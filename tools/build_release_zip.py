"""Rebuild the release ZIP from this repository's package files, byte for byte.

Usage: python3 -I tools/build_release_zip.py OUTPUT_DIR
Packs SHA256SUMS and every file it lists, under admission-delivery-pilot-<VERSION>/, in sorted
order, stored without compression so any Python and zlib rebuild it byte for byte, mode 0644, dated 2026-11-23 00:00:00, and writes the ZIP and its .sha256 file.
Files that are not in SHA256SUMS (this tools folder, .github, .gitattributes) are not packed.
"""
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]).resolve()
version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
name = f"admission-delivery-pilot-{version}"
listed = [line.split("  ", 1)[1] for line in (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines()]
for line in (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
    value, rel = line.split("  ", 1)
    if hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() != value:
        raise SystemExit(f"Checksum differs for {rel}.")
members = sorted(listed + ["SHA256SUMS"])
OUT.mkdir(parents=True, exist_ok=True)
zpath = OUT / f"{name}.zip"
with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
    for rel in members:
        info = zipfile.ZipInfo(f"{name}/{rel}", date_time=(2026, 11, 23, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        z.writestr(info, (ROOT / rel).read_bytes())
digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
(OUT / f"{name}.zip.sha256").write_text(f"{digest}  {name}.zip\n", encoding="utf-8")
print(digest)

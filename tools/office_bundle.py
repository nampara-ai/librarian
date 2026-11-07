"""Fetch and stage a checksum-pinned, private LibreOffice renderer. Build-time only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "26.8.2"
BASE = f"https://download.documentfoundation.org/libreoffice/stable/{VERSION}/"
SPECS = {
    "aarch64-apple-darwin": (f"mac/aarch64/LibreOffice_{VERSION}_MacOS_aarch64.dmg", "9f24fe83ab1ed5c95eb3df580119f32932f713e577feeac02bf619fb00749b55"),
    "x86_64-apple-darwin": (f"mac/x86_64/LibreOffice_{VERSION}_MacOS_x86-64.dmg", "0af062779cc17f6f852918b2424c745f7dd2d20d51d7fb8a0925eb92ec0417a7"),
    "x86_64-pc-windows-msvc": (f"win/x86_64/LibreOffice_{VERSION}_Win_x86-64.msi", "ff4b911403b67ee9e6249f820f9782c44bb32c94fdaa45b0da53a8e419e5307a"),
    "x86_64-unknown-linux-gnu": (f"deb/x86_64/LibreOffice_{VERSION}_Linux_x86-64_deb.tar.gz", "8290c8f46cb33d6df8f681cb796e0697461ea070f86ec55b63c94d7eb9d9a476"),
}

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def bundle(target):
    relative, expected = SPECS[target]
    cache = ROOT / ".build-assets"
    cache.mkdir(exist_ok=True)
    archive = cache / Path(relative).name
    destination = cache / f"office-{target}"
    marker = destination / "librarian-office.json"
    if marker.is_file() and json.loads(marker.read_text())["archive_sha256"] == expected:
        return destination
    if not archive.exists() or digest(archive) != expected:
        partial = archive.with_suffix(archive.suffix + ".partial")
        print(f"Downloading LibreOffice {VERSION} for {target}", flush=True)
        with urllib.request.urlopen(BASE + relative, timeout=600) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(partial) != expected:
            raise RuntimeError("Office renderer checksum mismatch")
        partial.replace(archive)
    # Only disposable, build-owned staging is replaced after an interrupted build.
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    if "apple" in target:
        mounted = plistlib.loads(subprocess.check_output(["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(archive)]))
        mount = next(Path(e["mount-point"]) for e in mounted["system-entities"] if "mount-point" in e)
        try:
            shutil.copytree(mount / "LibreOffice.app", destination / "LibreOffice.app", symlinks=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=True)
    elif "windows" in target:
        extracted = cache / f"office-msi-{VERSION}"
        extracted.mkdir(exist_ok=True)
        subprocess.run(["msiexec", "/a", str(archive.resolve()), "/qn", f"TARGETDIR={extracted.resolve()}"], check=True)
        program = next(extracted.rglob("soffice.com")).parent
        shutil.copytree(program.parent, destination, dirs_exist_ok=True)
    else:
        extracted = cache / f"office-debs-{VERSION}"
        extracted.mkdir(exist_ok=True)
        with tarfile.open(archive) as source:
            source.extractall(extracted, filter="data")
        unpacked = cache / f"office-root-{VERSION}"
        unpacked.mkdir(exist_ok=True)
        for package in extracted.rglob("*.deb"):
            subprocess.run(["dpkg-deb", "-x", str(package), str(unpacked)], check=True)
        program = next(unpacked.glob("opt/libreoffice*/program/soffice")).parent
        shutil.copytree(program.parent, destination, symlinks=True, dirs_exist_ok=True)
        # Ship the non-glibc dependency closure, scoped to this child process.
        libs = destination / "lib"
        libs.mkdir(exist_ok=True)
        todo = [p for p in (destination / "program").rglob("*") if p.is_file() and (".so" in p.name or p.name == "soffice.bin")]
        seen = set()
        excluded = re.compile(r"^(?:ld-linux|libc\.|libm\.|libpthread\.|librt\.|libdl\.|libresolv\.|libutil\.|libnss_)")
        while todo:
            binary = todo.pop()
            if str(binary) in seen:
                continue
            seen.add(str(binary))
            result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                match = re.search(r"=> (/\S+)", line)
                if not match:
                    continue
                dependency = Path(match[1])
                if excluded.match(dependency.name) or str(dependency).startswith(str(destination)):
                    continue
                output = libs / dependency.name
                if not output.exists():
                    shutil.copy2(dependency.resolve(), output)
                    todo.append(dependency)
    marker.write_text(json.dumps({"version": VERSION, "url": BASE + relative, "archive_sha256": expected}, indent=2))
    return destination

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=SPECS)
    print(bundle(parser.parse_args().target))

#!/usr/bin/env python3
"""Build a relocatable native distribution from an already compiled executable.

Python is a packaging tool only; it is not needed to run Librarian.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    "aarch64-apple-darwin": "macos-aarch64",
    "x86_64-apple-darwin": "macos-x86_64",
    "x86_64-pc-windows-msvc": "windows-x86_64",
    "x86_64-unknown-linux-gnu": "linux-x86_64",
}


def digest(path):
    checksum=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def download(spec, cache):
    filename = Path(urllib.parse.urlparse(spec["url"]).path).name
    archive = cache / filename
    if archive.is_file() and digest(archive) == spec["sha256"]:
        return archive
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            pending = Path(output.name)
            print(f"Downloading {spec['name']}", flush=True)
            with urllib.request.urlopen(spec["url"], timeout=600) as source:
                expected_size=source.headers.get("Content-Length")
                shutil.copyfileobj(source, output)
                if expected_size is not None and output.tell()!=int(expected_size):
                    raise ValueError(f"Truncated archive: {spec['name']}")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if pending is not None:
            pending.unlink(missing_ok=True)
        raise
    if digest(pending) != spec["sha256"]:
        pending.unlink()
        raise ValueError(f"Checksum mismatch: {spec['name']}")
    pending.replace(archive)
    return archive


def extract(archive, spec, destination):
    records = []

    def copy(name, source):
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or any(":" in p for p in path.parts):
            raise ValueError(f"Unsafe archive path: {name}")
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            shutil.copyfileobj(source, output)
        records.append({"path": path.as_posix(), "sha256": digest(target), "bytes": target.stat().st_size})

    if spec["archive"] == "zip":
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if not entry.is_dir():
                    with bundle.open(entry) as source:
                        copy(entry.filename, source)
    else:
        with tarfile.open(archive, "r:*") as bundle:
            for entry in bundle:
                if entry.isfile():
                    with bundle.extractfile(entry) as source:
                        copy(entry.name, source)
    (destination / "manifest.json").write_text(json.dumps({"asset": spec, "files": records}, indent=2), encoding="utf-8")


def rust_licenses(destination):
    metadata = json.loads(subprocess.check_output(["cargo", "metadata", "--format-version", "1", "--locked"], cwd=ROOT))
    notices = []
    for package in metadata["packages"]:
        if not package["source"]:
            continue
        notices.append(f"{package['name']} {package['version']} — {package.get('license') or 'see license files'}\n{package.get('repository') or package['source']}\n")
        source = Path(package["manifest_path"]).parent
        for path in sorted(source.iterdir()):
            if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                notices.append(path.read_text(encoding="utf-8", errors="replace"))
        notices.append("\n" + "=" * 72 + "\n")
    destination.write_text("\n".join(notices), encoding="utf-8")


def windows_runtime(folder):
    """Use the build machine's Visual Studio redistributable CRT, never System32."""
    roots = []
    if os.environ.get("VCToolsRedistDir"):
        roots.append(Path(os.environ["VCToolsRedistDir"]))
    vswhere=Path(os.environ.get("ProgramFiles(x86)","C:/Program Files (x86)"))/"Microsoft Visual Studio/Installer/vswhere.exe"
    if vswhere.is_file():
        installation=subprocess.check_output([str(vswhere),"-latest","-products","*","-requires","Microsoft.VisualStudio.Component.VC.Tools.x86.x64","-property","installationPath"],text=True).strip()
        if installation:
            roots.extend(sorted((Path(installation)/"VC/Redist/MSVC").glob("*"),reverse=True))
    candidates=[path for root in roots for path in root.glob("x64/Microsoft.VC*.CRT") if (path/"vcruntime140.dll").is_file()]
    if not candidates:
        raise RuntimeError("Visual Studio redistributable CRT directory is required to package Windows")
    records=[]
    for path in sorted(candidates[0].glob("*.dll")):
        shutil.copy2(path,folder/path.name)
        records.append({"name":path.name,"sha256":digest(path)})
    for path in (folder/"engines/onnxruntime").rglob("onnxruntime_providers_shared.dll"):
        shutil.copy2(path,folder/path.name)
        records.append({"name":path.name,"sha256":digest(path)})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=PLATFORMS)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--check-engines",action="store_true",help="Initialize the packaged native libraries and real OCR sessions before archiving")
    args = parser.parse_args()
    executable = "librarian.exe" if "windows" in args.target else "librarian"
    binary = args.binary or ROOT / "target" / args.target / "release" / executable
    if not binary.is_file():
        raise SystemExit(f"Build the release executable first: {binary}")
    specs = json.loads((ROOT / "crates/librarian-native/src/engine-assets.json").read_text())
    assets = specs["platforms"][PLATFORMS[args.target]] + specs["common"]
    cache = ROOT / ".build-assets"
    cache.mkdir(exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    name = f"librarian-{args.target}"
    with tempfile.TemporaryDirectory(dir=args.output) as staging:
        folder = Path(staging) / name
        folder.mkdir()
        shutil.copy2(binary, folder / executable)
        (folder / executable).chmod(0o755)
        for spec in assets:
            target = folder / "engines" / spec["folder"]
            target.mkdir(parents=True)
            try:
                extract(download(spec, cache), spec, target)
            except Exception as error:
                raise RuntimeError(f"Cannot package {spec['name']}: {error}") from error
        runtime=windows_runtime(folder) if "windows" in args.target else []
        for source, target in [("LICENSE", "LICENSE"), ("docs/RUST_FOUNDATION.md", "README.md"), ("docs/THIRD_PARTY.md", "THIRD_PARTY.md")]:
            shutil.copy2(ROOT / source, folder / target)
        rust_licenses(folder / "RUST-LICENSES.txt")
        shutil.copytree(ROOT / "docs/licenses", folder / "licenses")
        shutil.copy2(ROOT / "Cargo.lock", folder / "Cargo.lock")
        (folder / "build.json").write_text(json.dumps({"target": args.target, "git": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "rust": subprocess.check_output(["rustc", "--version"], text=True).strip(), "binary_sha256": digest(folder / executable), "engines": assets,"windows_runtime":runtime}, indent=2), encoding="utf-8")
        if args.check_engines:
            subprocess.run([str(folder/executable),"--library",str(Path(staging)/"engine-check"),"doctor","--load-engines"],check=True,stdout=subprocess.DEVNULL)
            print("Initialized bundled PDFium and both OCR model sessions",flush=True)
        archive_format = "zip" if "windows" in args.target else "gztar"
        archive = Path(shutil.make_archive(str(args.output / name), archive_format, staging, name))
        archive.with_suffix(archive.suffix + ".sha256").write_text(f"{digest(archive)}  {archive.name}\n")
        print(archive)


if __name__ == "__main__":
    main()

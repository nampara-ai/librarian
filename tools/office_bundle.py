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
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "26.8.2"
BUNDLE_REVISION = 3
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
    if marker.is_file():
        prior = json.loads(marker.read_text())
        if prior.get("archive_sha256") == expected and prior.get("bundle_revision") == BUNDLE_REVISION:
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
        notices = destination / "system-library-notices"
        notices.mkdir(exist_ok=True)
        records = []
        todo = []
        for path in (destination / "program").rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    if stream.read(4) == b"\x7fELF":
                        todo.append(path)
        seen = set()
        environment = dict(os.environ, LD_LIBRARY_PATH=os.pathsep.join([str(destination / "program"), str(libs)]))
        excluded = re.compile(r"^(?:ld-linux|libc\.|libm\.|libpthread\.|librt\.|libdl\.|libresolv\.|libutil\.|libnss_)")
        while todo:
            binary = todo.pop()
            if str(binary) in seen:
                continue
            seen.add(str(binary))
            result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, env=environment)
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
                    record = {"name": output.name, "sha256": digest(output), "build_path": str(dependency)}
                    for candidate in [dependency, dependency.resolve(), Path("/usr") / dependency.relative_to("/")]:
                        owner = subprocess.run(["dpkg-query", "-S", str(candidate)], capture_output=True, text=True)
                        if owner.returncode == 0 and ": " in owner.stdout:
                            package = owner.stdout.splitlines()[0].split(": ", 1)[0]
                            record["package"] = package
                            record["source"] = subprocess.check_output(["dpkg-query", "-W", "-f=${source:Package} ${source:Version}", package], text=True)
                            copyright = Path("/usr/share/doc") / package.split(":")[0] / "copyright"
                            if copyright.is_file():
                                shutil.copy2(copyright, notices / (package.replace(":", "_") + "-copyright.txt"))
                            break
                    records.append(record)
        (notices / "libraries.json").write_text(json.dumps(records, indent=2))
    marker.write_text(json.dumps({"version": VERSION, "bundle_revision": BUNDLE_REVISION, "url": BASE + relative, "archive_sha256": expected}, indent=2))
    return destination

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=SPECS)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    destination = bundle(args.target)
    print(destination, flush=True)
    if args.check:
        from check_layout import documents
        executable = destination / ("LibreOffice.app/Contents/MacOS/soffice" if "apple" in args.target else "program/soffice.com" if "windows" in args.target else "program/soffice")
        environment = dict(os.environ, SAL_USE_VCLPLUGIN="svp")
        if "linux" in args.target:
            environment["LD_LIBRARY_PATH"] = os.pathsep.join([str(destination / "program"), str(destination / "lib")])
        with tempfile.TemporaryDirectory(prefix="librarian-office-") as folder:
            folder = Path(folder)
            for source in documents(folder):
                command = [str(executable), "--headless", "--nologo", "--nodefault", "--norestore", f"-env:UserInstallation={(folder / 'profile').as_uri()}", "--convert-to", "pdf", "--outdir", str(folder), str(source)]
                try:
                    subprocess.run(command, env=environment, check=True, timeout=120)
                except subprocess.CalledProcessError:
                    if "linux" in args.target:
                        diagnostic = subprocess.run(command, env=dict(environment, LD_DEBUG="libs", SAL_LOG="+WARN"), capture_output=True, text=True, timeout=120)
                        relevant = [line for line in diagnostic.stderr.splitlines() if any(term in line.lower() for term in ["error", "warn", "softokn", "freebl", "nss", "pdf", "not found"]) ]
                        print("\n".join(relevant[-100:]), flush=True)
                    raise
                if not source.with_suffix(".pdf").is_file():
                    raise RuntimeError(f"Office renderer did not convert {source.name}")

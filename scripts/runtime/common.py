# SPDX-License-Identifier: BSD-3-Clause
"""Shared, fail-closed runtime build primitives (Python 3.12+)."""

import hashlib
import json
import ntpath
import re
import shutil
import struct
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE / "toolchain-lock.json").read_text(encoding="utf-8"))
PLATFORMS = {"windows": "x64", "linux": "x64", "macos": "arm64"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def sha256(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def run(args, *, cwd=None, env=None, log=None):
    args = [str(a) for a in args]
    print("+", subprocess.list2cmdline(args), flush=True)
    start = time.monotonic()
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with Path(log).open("a", encoding="utf-8") as stream:
            stream.write(subprocess.list2cmdline(args) + "\n" + result.stdout)
            stream.write(
                f"\nElapsed: {time.monotonic() - start:.3f}s; exit: {result.returncode}\n"
            )
    print(result.stdout, end="", flush=True)
    require(
        result.returncode == 0,
        f"Command failed ({result.returncode}): {args[0]}",
    )
    return result.stdout


def download(entry, directory):
    """Never execute a download before comparing its locked digest."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / entry["url"].rsplit("/", 1)[-1]
    if target.exists() and sha256(target) == entry["sha256"]:
        return target
    partial = target.with_suffix(target.suffix + ".partial")
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                entry["url"], headers={"User-Agent": "colmap-runtime-builder"}
            )
            with (
                urllib.request.urlopen(request, timeout=120) as source,
                partial.open("wb") as dest,
            ):
                shutil.copyfileobj(source, dest)
            require(
                sha256(partial) == entry["sha256"],
                f"SHA-256 mismatch: {entry['url']}",
            )
            partial.replace(target)
            return target
        except (OSError, RuntimeError):
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2)


def safe_member(name):
    path = PurePosixPath(name.replace("\\", "/"))
    require(
        bool(name)
        and not path.is_absolute()
        and not ntpath.splitdrive(name)[0]
        and ".." not in path.parts,
        f"Unsafe archive path: {name}",
    )
    return path


def extract(archive, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as container:
            for info in container.infolist():
                safe_member(info.filename)
                require(
                    (info.external_attr >> 16) & 0o170000 != 0o120000,
                    "ZIP symlinks are not supported",
                )
            container.extractall(destination)
    else:
        with tarfile.open(archive) as container:
            for info in container.getmembers():
                safe_member(info.name)
            container.extractall(destination, filter="data")


def check_cuda(metadata, platform, nvcc_output):
    cuda = LOCK["cuda"]
    require(platform in cuda["sdk"], "CUDA is unavailable for this platform")
    expected = {"cuda": cuda["sdk"][platform], **cuda["components"]}
    for key, version in expected.items():
        require(
            isinstance(metadata.get(key), dict)
            and metadata[key].get("version") == version,
            f"CUDA identity mismatch: {key}; expected {version}",
        )
    found = re.findall(r"\bV(\d+\.\d+\.\d+)\b", nvcc_output)
    require(found == [cuda["nvccVersion"]], "Wrong actual nvcc version")
    return expected


def check_vocabulary(path):
    path = Path(path)
    locked = LOCK["offlineVocabulary"]
    require(path.is_file(), "Missing required offline vocabulary tree")
    require(
        path.stat().st_size == locked["bytes"],
        "Offline vocabulary size mismatch",
    )
    require(
        sha256(path) == locked["sha256"], "Offline vocabulary SHA-256 mismatch"
    )
    with path.open("rb") as stream:
        header = stream.read(12)
    require(
        len(header) == 12
        and struct.unpack("<3i", header)
        == (
            locked["fileVersion"],
            locked["descriptorDimension"],
            locked["embeddingDimension"],
        ),
        "Wrong FAISS/SIFT vocabulary format",
    )
    return dict(locked)


def canonical_path(value, platform):
    value = value.strip().strip('"')
    if platform == "windows":
        return ntpath.normcase(ntpath.normpath(value))
    return str(Path(value).resolve())


def parse_cache(text):
    return dict(
        re.findall(r"^([^/#\n][^:\n]*):[^=\n]+=(.*)$", text, re.MULTILINE)
    )


def check_cmake(cache_text, compiler_text, platform, nvcc=None):
    cache = parse_cache(cache_text)
    for name in LOCK["disabledFeatures"]:
        require(cache.get(name) == "OFF", f"Feature must be OFF: {name}")
    enabled = platform != "macos"
    for name in ("CUDA_ENABLED", "CASPAR_ENABLED"):
        require(
            cache.get(name) == ("ON" if enabled else "OFF"), f"Wrong {name}"
        )
    require(
        cache.get("CASPAR_USE_DOUBLE") == "OFF",
        "Caspar must be single precision",
    )
    if enabled:
        selected = re.search(
            r'set\(CMAKE_CUDA_COMPILER "([^"]+)"\)', compiler_text
        )
        version = re.search(
            r'set\(CMAKE_CUDA_COMPILER_VERSION "([^"]+)"\)', compiler_text
        )
        require(
            selected and version, "Missing actual CUDA compiler information"
        )
        for value in (cache.get("CMAKE_CUDA_COMPILER", ""), selected.group(1)):
            require(
                canonical_path(value, platform)
                == canonical_path(str(nvcc), platform),
                "CMake selected another CUDA compiler",
            )
        require(
            version.group(1) == LOCK["cuda"]["nvccVersion"],
            "Wrong CMake CUDA version",
        )
        require(
            cache.get("CMAKE_CUDA_ARCHITECTURES")
            == ";".join(map(str, LOCK["architectures"])),
            "Wrong CMake CUDA architectures",
        )
        root = Path(nvcc).parent.parent
        require(
            canonical_path(cache.get("CUDAToolkit_ROOT", ""), platform)
            == canonical_path(str(root), platform),
            "Wrong CUDAToolkit_ROOT",
        )
    return cache


def check_cuda_commands(commands, platform, nvcc):
    require(commands, "No CUDA compile commands found")
    for item in commands:
        command = item["command"].replace("\\", "/")
        compiler = str(nvcc).replace("\\", "/")
        require(
            re.search(
                r'(?:^|\s)["\']?' + re.escape(compiler) + r'["\']?(?:\s|$)',
                command,
                re.I,
            ),
            "Actual command uses another nvcc",
        )
        require(
            not re.search(r"(?:^|\s)@", command),
            "Unexpanded CUDA response file",
        )
        require(
            "allow-unsupported-compiler" not in command,
            "Unsupported host compiler override",
        )
        archs = {int(x) for x in re.findall(r"(?:compute|sm)_(\d+)", command)}
        require(
            archs == set(LOCK["architectures"]),
            "Actual command has wrong CUDA architectures",
        )
        require(
            {int(x) for x in re.findall(r"sm_(\d+)", command)}
            == set(LOCK["architectures"]),
            "Actual command lacks native code for a requested GPU architecture",
        )
        if platform == "windows":
            require(
                "/Zc:preprocessor-" not in command,
                "Traditional MSVC preprocessor enabled",
            )
            require(
                re.search(
                    r"-Xcompiler(?:=|\s)[\"']?/Zc:preprocessor\b", command
                ),
                "Actual nvcc command lacks -Xcompiler=/Zc:preprocessor",
            )


def check_paths(paths, temp, platform):
    records = sorted(((len(str(p)), str(p)) for p in paths), reverse=True)
    if platform == "windows":
        for length, path in records:
            require(
                length < 240, f"Windows path budget exceeded ({length}): {path}"
            )
        require(len(str(temp)) < 32, "Windows TEMP directory is too long")
        require(
            not re.search(
                r"colmap-[0-9a-f]{40}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}",
                str(temp),
            ),
            "Unstable TEMP directory",
        )
    return records[:10]


def compatible_key(platform, toolchain, dependency_files, triplet):
    return (
        "colmap-runtime-v1-"
        + platform
        + "-"
        + digest(
            {
                "architecture": PLATFORMS[platform],
                "toolchain": toolchain,
                "cuda": LOCK["cuda"] if platform != "macos" else None,
                "vcpkg": LOCK["vcpkgCommit"],
                "triplet": triplet,
                "dependencies": dependency_files,
                "features": LOCK["disabledFeatures"],
                "casparDouble": False,
                "buildType": "Release",
                "hashBackend": "STD",
                "cmake": LOCK["cmakeVersion"],
                "ninja": LOCK["ninjaVersion"],
                "ccache": LOCK["ccacheVersion"],
            }
        )[:32]
    )


def checksum_lines(root):
    root = Path(root)
    return [
        f"{sha256(p)}  {p.relative_to(root).as_posix()}"
        for p in sorted(
            root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()
        )
        if p.is_file() and p.name != "SHA256SUMS"
    ]


def verify_checksums(root):
    root = Path(root)
    manifest = root / "SHA256SUMS"
    require(manifest.is_file(), "Missing SHA256SUMS")
    expected = manifest.read_text(encoding="utf-8").splitlines()
    require(
        expected == checksum_lines(root), "Runtime file set / SHA-256 mismatch"
    )

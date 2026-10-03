# SPDX-License-Identifier: BSD-3-Clause
"""Conservative runtime closure, provenance, relocation and immutable archives."""

import os
import re
import shutil
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path

from common import (
    HERE,
    LOCK,
    PLATFORMS,
    check_vocabulary,
    checksum_lines,
    download,
    extract,
    read_json,
    require,
    sha256,
    verify_checksums,
    write_json,
)

LINUX_SYSTEM = re.compile(
    r"^(?:ld-linux-x86-64\.so\.2|libgcc_s\.so\.1|lib(?:c|m|pthread|dl|rt|resolv|util|anl)\.so\.[0-9]+)$"
)
DRIVERS = re.compile(
    r"^(?:libcuda\.so(?:\..*)?|libnvidia-.*|nvcuda\.dll|nvapi(?:64)?\.dll)$",
    re.I,
)


def linux_rpath(path, bundled_dependencies):
    path = Path(path)
    if path.name.startswith("libstdc++.so."):
        require(
            not bundled_dependencies,
            "Bundled libstdc++ unexpectedly needs a non-system runtime",
        )
        # Ubuntu 22.04 patchelf 0.14.3 crashes while rewriting GCC's
        # libstdc++. It has only system-base dependencies and is selected by
        # the executable's $ORIGIN/../lib rpath, so it needs no rpath itself.
        return None
    return "$ORIGIN" if path.parent.name == "lib" else "$ORIGIN/../lib"


def runtime_type(path):
    with Path(path).open("rb") as stream:
        header = stream.read(64)
    if header[:4] == b"\x7fELF":
        endian = "<" if header[5] == 1 else ">"
        kind = struct.unpack_from(endian + "H", header, 16)[0]
        if header[4] == 2 and kind in (2, 3):
            require(
                struct.unpack_from(endian + "H", header, 18)[0] == 62,
                "Runtime must be Linux x64",
            )
            return "elf"
        return None
    if header[:2] == b"MZ":
        return "pe"
    if header[:4] in (
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    ):
        return "macho"
    return None


def pe_imports(path):
    """Read ordinary AND delay-load PE imports without running a DLL."""
    data = Path(path).read_bytes()
    require(data[:2] == b"MZ", "Expected a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    require(data[pe : pe + 4] == b"PE\0\0", "Invalid PE signature")
    machine, count = struct.unpack_from("<HH", data, pe + 4)
    require(machine == 0x8664, "Runtime must be Windows x64")
    size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    require(
        struct.unpack_from("<H", data, optional)[0] == 0x20B, "Expected PE32+"
    )
    sections = []
    for n in range(count):
        off = optional + size + n * 40
        virtual_size, address, raw_size, raw = struct.unpack_from(
            "<IIII", data, off + 8
        )
        sections.append((address, max(virtual_size, raw_size), raw))

    def offset(rva):
        for address, length, raw in sections:
            if address <= rva < address + length:
                return raw + rva - address
        raise RuntimeError(f"Unmapped PE RVA: {rva}")

    def name(rva):
        pos = offset(rva)
        return data[pos : data.index(0, pos)].decode("ascii")

    imports = []
    for index, stride, name_offset in ((1, 20, 12), (13, 32, 4)):
        rva, length = struct.unpack_from(
            "<II", data, optional + 112 + index * 8
        )
        if not rva:
            continue
        start = offset(rva)
        for pos in range(start, start + length, stride):
            if not any(data[pos : pos + stride]):
                break
            if index == 13:
                require(
                    struct.unpack_from("<I", data, pos)[0] & 1,
                    "Unsupported VA delay import",
                )
            imports.append(
                name(struct.unpack_from("<I", data, pos + name_offset)[0])
            )
    return sorted(set(imports))


def shared_runtime(path, kind):
    with Path(path).open("rb") as stream:
        header = stream.read(64)
        if kind == "elf":
            endian = "<" if header[5] == 1 else ">"
            return struct.unpack_from(endian + "H", header, 16)[0] == 3
        if kind == "pe":
            stream.seek(struct.unpack_from("<I", header, 0x3C)[0] + 22)
            return bool(struct.unpack("<H", stream.read(2))[0] & 0x2000)
    return False


class Collector:
    def __init__(self, build):
        self.build = build
        self.root = build.stage
        self.components = {}
        self.origins = {}
        self.queue = []
        self.checked = set()
        self.external = {}
        self.platform = build.platform
        self.owners = {}
        self.files = {}
        self.brew = {}
        if self.platform != "macos":
            for listing in (build.root / "i/vcpkg/info").glob("*.list"):
                component = listing.name.split("_")[0]
                for line in listing.read_text().splitlines():
                    self.owners[str((build.root / "i" / line).resolve())] = (
                        component
                    )
            for path in (build.root / "i" / build.triplet).rglob("*"):
                if path.is_file() and runtime_type(path):
                    self.files.setdefault(path.name.lower(), []).append(path)
            for path in (build.root / "cuda").rglob("*"):
                if path.is_file() and runtime_type(path):
                    self.files.setdefault(path.name.lower(), []).append(path)
        else:
            self.brew = {
                f["name"]: f
                for f in build.state["dependencyInventory"]["formulae"]
            }

    def license(self, component, files, version=None, source=None):
        require(files, f"Missing license files for {component}")
        dest = self.root / "licenses" / component
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for n, file in enumerate(files):
            target = dest / (str(n) + "-" + file.name)
            shutil.copy2(file, target)
            copied.append(target.relative_to(self.root).as_posix())
        self.components[component] = {
            "name": component,
            "version": version,
            "source": source,
            "licenseFiles": copied,
            "runtimeFiles": [],
        }

    def brew_license(self, name):
        if name in self.components:
            return
        require(name in self.brew, f"Unknown Homebrew component: {name}")
        formula = self.brew[name]
        cellar = Path(self.build.state["brewPrefix"]) / "Cellar" / name
        installed = formula["installed"]
        require(installed, f"Homebrew {name} is not installed")
        versions = [cellar / x["version"] for x in installed]
        candidates = [
            p
            for v in versions
            for p in v.rglob("*")
            if p.is_file()
            and re.match(
                r"^(LICENSE|COPYING|NOTICE|COPYRIGHT)(?:[._-].*)?$",
                p.name,
                re.I,
            )
        ]
        source = formula["urls"]["stable"]
        if not candidates:
            # Bottle receipts usually omit full license texts. Obtain the exact
            # formula source archive, verify its published SHA, then copy texts.
            require(
                len(installed) == 1
                and installed[0]["version"].split("_")[0]
                == formula["versions"]["stable"],
                f"Cannot identify the installed source license for {name}",
            )
            archive = download(
                {"url": source["url"], "sha256": source["checksum"]},
                self.build.root / "license-sources" / name,
            )
            target = self.build.root / "license-sources" / name / "source"
            extract(archive, target)
            candidates = [
                p
                for p in target.rglob("*")
                if p.is_file()
                and re.match(
                    r"^(LICENSE|COPYING|NOTICE|COPYRIGHT)(?:[._-].*)?$",
                    p.name,
                    re.I,
                )
            ]
        self.license(
            name, candidates, [x["version"] for x in installed], source
        )
        self.components[name]["homebrewRecipe"] = {
            k: formula.get(k)
            for k in (
                "tap_git_head",
                "ruby_source_path",
                "ruby_source_checksum",
                "license",
            )
        }

    def owner(self, path):
        path = path.resolve()
        if path.is_relative_to(self.root):
            original = self.origins.get(str(path))
            require(original, f"Unattributed staged runtime: {path}")
            path = Path(original).resolve()
        if self.platform == "macos":
            prefix = Path(self.build.state["brewPrefix"]) / "Cellar"
            try:
                name = path.relative_to(prefix).parts[0]
            except ValueError as error:
                raise RuntimeError(
                    f"Unattributed macOS runtime: {path}"
                ) from error
            self.brew_license(name)
            return name
        if str(path) in self.owners:
            return self.owners[str(path)]
        cuda = self.build.root / "cuda"
        if path.is_relative_to(cuda):
            rel = path.relative_to(cuda).as_posix()
            assembly = read_json(cuda / "assembly.json")
            require(
                assembly["files"].get(rel) == sha256(path),
                "Unverified CUDA runtime file",
            )
            name = (
                "cuda_cudart"
                if "cudart" in path.name
                else "libcurand"
                if "curand" in path.name
                else None
            )
            if name is None:
                name = "cuda-runtime-components"
                require(name in self.components, "Unattributed CUDA component")
            return name
        if self.platform == "windows" and path.is_relative_to(
            Path(os.environ["VCToolsRedistDir"]).resolve()
        ):
            return "MSVC"
        if self.platform == "linux":
            candidates = [str(path)]
            if str(path).startswith("/usr/lib/"):
                candidates.append(
                    str(path)[4:]
                )  # Ubuntu usrmerge ownership spelling.
            result = subprocess.run(
                ["dpkg-query", "-S", *candidates],
                capture_output=True,
                text=True,
            )
            require(result.stdout, f"Unattributed system runtime: {path}")
            package = result.stdout.splitlines()[0].split(": ", 1)[0]
            if package not in self.components:
                copyright = (
                    Path("/usr/share/doc") / package.split(":")[0] / "copyright"
                )
                require(
                    copyright.is_file(), f"No Debian copyright for {package}"
                )
                version = self.build.execute(
                    ["dpkg-query", "-W", "-f=${Version}", package],
                    "system-provenance",
                ).strip()
                self.license(
                    package.replace(":", "-"),
                    [copyright],
                    version,
                    "Ubuntu signed package repository",
                )
                if ":" in package:
                    self.components[package] = self.components.pop(
                        package.replace(":", "-")
                    )
            return package
        raise RuntimeError(f"Unattributed Windows runtime: {path}")

    def copy(self, origin, directory=None):
        origin = Path(origin)
        if origin.resolve().is_relative_to(self.root):
            original = self.origins.get(str(origin.resolve()))
            require(original, f"Unattributed staged runtime: {origin}")
            origin = Path(original)
        require(
            not DRIVERS.match(origin.name),
            f"NVIDIA driver must not be bundled: {origin}",
        )
        require("stubs" not in origin.parts, "CUDA stubs must not be bundled")
        component = self.owner(origin)
        directory = directory or (
            "bin" if self.platform == "windows" else "lib"
        )
        target = self.root / directory / origin.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            previous = self.origins.get(str(target))
            require(
                sha256(previous or target) == sha256(origin),
                f"Conflicting runtime library basename: {origin.name}",
            )
        else:
            shutil.copy2(origin.resolve(), target)
        rel = target.relative_to(self.root).as_posix()
        if rel not in self.components[component]["runtimeFiles"]:
            self.components[component]["runtimeFiles"].append(rel)
        self.origins.setdefault(str(target), str(origin.resolve()))
        self.queue.append(target)
        return target

    def windows(self, path):
        for name in pe_imports(path):
            if DRIVERS.match(name):
                self.external[name] = (
                    "External NVIDIA driver; GPU execution only"
                )
                continue
            candidates = self.files.get(name.lower(), [])
            if candidates:
                require(
                    len({sha256(p) for p in candidates}) == 1,
                    f"Mixed DLLs: {name}",
                )
                self.copy(candidates[0])
            elif (self.root / "bin" / name).exists():
                self.queue.append(self.root / "bin" / name)
            elif (
                name.lower().startswith(("api-ms-win-", "ext-ms-win-"))
                or (Path(os.environ["SystemRoot"]) / "System32" / name).exists()
            ):
                require(
                    not name.lower().startswith(
                        ("msvcp", "vcruntime", "concrt", "vcomp")
                    ),
                    f"MSVC runtime must be app-local: {name}",
                )
                self.external[name] = "Windows system component"
            else:
                raise RuntimeError(
                    f"Unresolved ordinary/delay-load DLL: {name}"
                )

    def linux(self, path):
        # runtime_type rejects archives and relocatable objects before ldd.
        text = self.build.execute(["ldd", path], "dependency-closure")
        require("not found" not in text, f"Unresolved ELF dependencies: {path}")
        bundled_dependencies = []
        for line in text.splitlines():
            match = re.match(r"\s*(\S+) => (/\S+)", line)
            if not match:
                continue
            name, origin = match.groups()
            if LINUX_SYSTEM.match(name) or DRIVERS.match(name):
                self.external[name] = (
                    "System base runtime supplied by the supported Ubuntu target"
                    if LINUX_SYSTEM.match(name)
                    else "External GPU driver"
                )
                continue
            target = self.copy(origin)
            bundled_dependencies.append(name)
            # Preserve the DT_NEEDED name even when resolving a versioned symlink.
            alias = self.root / "lib" / name
            if alias != target and not alias.exists():
                alias.symlink_to(target.name)
                self.components[self.owner(Path(origin))][
                    "runtimeFiles"
                ].append(alias.relative_to(self.root).as_posix())
        rpath = linux_rpath(path, bundled_dependencies)
        if rpath:
            self.build.execute(
                ["patchelf", "--set-rpath", rpath, path],
                "relocation",
            )
        versions = self.build.execute(
            ["readelf", "--version-info", path], "elf-abi"
        )
        required = [
            tuple(map(int, x.split(".")))
            for x in re.findall(r"\bGLIBC_(\d+\.\d+)", versions)
        ]
        require(
            not required or max(required) <= (2, 35),
            f"glibc requirement exceeds 2.35: {path}",
        )

    def macos(self, path):
        architecture = self.build.execute(
            ["lipo", "-archs", path], "macos-abi"
        ).split()
        require("arm64" in architecture, f"Runtime lacks macOS arm64: {path}")
        origin = Path(self.origins.get(str(path), str(path)))
        libraries = self.build.execute(
            ["otool", "-L", path], "dependency-closure"
        ).splitlines()[1:]
        raw = self.build.execute(["otool", "-l", origin], "dependency-closure")
        rpaths = re.findall(
            r"cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.*?) \(offset", raw
        )
        for line in libraries:
            name = line.strip().split(" (", 1)[0]
            if name == str(origin) or (
                path.suffix == ".dylib" and name.endswith("/" + path.name)
            ):
                continue
            if name.startswith(("/usr/lib/", "/System/Library/")):
                self.external[name] = "macOS system component"
                continue
            resolved = name.replace("@loader_path", str(origin.parent)).replace(
                "@executable_path", str(self.build.root / "install/bin")
            )
            if name.startswith("@rpath/"):
                candidates = [
                    Path(p.replace("@loader_path", str(origin.parent)))
                    / name[7:]
                    for p in rpaths
                ]
                candidates += [
                    Path(self.build.state["brewPrefix"]) / "lib" / name[7:]
                ]
                resolved = str(next((p for p in candidates if p.is_file()), ""))
            require(
                Path(resolved).is_file(), f"Unresolved Mach-O library: {name}"
            )
            target = self.copy(resolved)
            self.build.execute(
                [
                    "install_name_tool",
                    "-change",
                    name,
                    "@rpath/" + target.name,
                    path,
                ],
                "relocation",
            )
        if path.suffix == ".dylib":
            self.build.execute(
                ["install_name_tool", "-id", "@rpath/" + path.name, path],
                "relocation",
            )
        # Remove all original build/Homebrew paths, then add the relative path.
        current = self.build.execute(["otool", "-l", path], "relocation")
        old = re.findall(
            r"cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.*?) \(offset", current
        )
        wanted = (
            "@loader_path"
            if path.parent.name == "lib"
            else "@loader_path/../lib"
        )
        for p in set(old):
            self.build.execute(
                ["install_name_tool", "-delete_rpath", p, path], "relocation"
            )
        self.build.execute(
            ["install_name_tool", "-add_rpath", wanted, path], "relocation"
        )
        version_info = self.build.execute(
            ["vtool", "-show-build", path], "macos-abi"
        )
        minimum = re.findall(r"minos (\d+)\.(\d+)", version_info)
        if not minimum:
            minimum = re.findall(
                r"cmd LC_VERSION_MIN_MACOSX\n\s+cmdsize \d+\n\s+version (\d+)\.(\d+)",
                current,
            )
        require(
            minimum and all(tuple(map(int, v)) <= (14, 0) for v in minimum),
            f"Dependency requires a newer macOS than 14: {path}",
        )


def collect(build):
    require(
        build.state.get("compiled"),
        "Build must complete before dependency collection",
    )
    require(not build.stage.exists(), "Staging directory must be fresh")
    collector = Collector(build)
    if build.platform == "linux":
        build.env["LD_LIBRARY_PATH"] = os.pathsep.join(
            str(p)
            for p in (
                build.stage / "lib",
                build.root / "i" / build.triplet / "lib",
                build.root / "cuda/lib",
                build.root / "cuda/lib64",
                build.root / "install/lib",
                build.root / "install/thirdparty",
            )
        )
    for directory in ("bin", "lib", "licenses"):
        (build.stage / directory).mkdir(parents=True, exist_ok=True)
    executable = (
        build.root
        / "install/bin"
        / ("colmap.exe" if build.platform == "windows" else "colmap")
    )
    require(executable.is_file(), "Missing actual installed COLMAP executable")
    collector.license(
        "COLMAP",
        [build.source / "COPYING.txt"],
        LOCK["colmapVersion"],
        LOCK["upstreamCommit"],
    )
    for component in ("VLFeat", "SiftGPU", "PoissonRecon", "Symforce-Caspar"):
        if (
            component in ("SiftGPU", "Symforce-Caspar")
            and build.platform == "macos"
        ):
            continue
        collector.license(
            component,
            [build.source / "src/thirdparty" / component / "LICENSE"],
            version="bundled@" + LOCK["upstreamCommit"],
            source=LOCK["upstreamCommit"],
        )
    for component in ("poselib", "faiss"):
        source = build.build / "_deps" / (component + "-src")
        texts = [
            p
            for p in source.glob("*")
            if p.is_file() and re.match("LICENSE|COPYING|NOTICE", p.name, re.I)
        ]
        declaration = re.search(
            r"FetchContent_Declare\("
            + component
            + r"\s+URL\s+(\S+)\s+URL_HASH SHA256=(\w+)",
            (build.source / "src/thirdparty/CMakeLists.txt").read_text(),
        )
        require(
            declaration, f"Missing upstream source identity for {component}"
        )
        url, checksum = declaration.groups()
        collector.license(
            component,
            texts,
            url.rsplit("/", 1)[-1].removesuffix(".zip"),
            {"url": url, "sha256": checksum},
        )
    if build.platform == "macos":
        for name in (
            "boost",
            "eigen",
            "openimageio",
            "metis",
            "glog",
            "sqlite",
            "suite-sparse",
            "ceres-solver",
            "glew",
            "libomp",
        ):
            collector.brew_license(name)
        for formula in collector.brew.values():
            if formula["name"] not in ("openimageio",):
                continue
            prefix = Path(build.state["brewPrefix"]) / "opt" / formula["name"]
            for path in prefix.resolve().rglob("*"):
                if (
                    path.is_file()
                    and runtime_type(path) == "macho"
                    and path.suffix in (".so", ".dylib")
                ):
                    collector.copy(path)
    else:
        status = build.state["dependencyInventory"]
        for share in (build.root / "i" / build.triplet / "share").iterdir():
            if share.is_dir() and (share / "copyright").is_file():
                stanza = next(
                    (
                        s
                        for s in status.split("\n\n")
                        if s.startswith("Package: " + share.name + "\n")
                    ),
                    "",
                )
                version = re.search(r"^Version: (.+)$", stanza, re.M)
                collector.license(
                    share.name,
                    [share / "copyright"],
                    version.group(1) if version else None,
                    LOCK["vcpkgCommit"],
                )
        all_cuda_texts = []
        for name, entry in build.state["cudaAssembly"].items():
            texts = [build.root / "cuda" / p for p in entry["licenseFiles"]]
            collector.license(
                name,
                texts,
                entry["version"],
                {"url": entry["url"], "sha256": entry["sha256"]},
            )
            all_cuda_texts += texts
        collector.license(
            "cuda-runtime-components",
            all_cuda_texts,
            LOCK["cuda"]["release"],
            "NVIDIA redistributable archives",
        )
        # Keep all dependency runtime libraries/plugins. No size/name heuristic
        # selects which unknown libraries are safe to remove.
        dependency = build.root / "i" / build.triplet
        for p in dependency.rglob("*"):
            if (
                p.is_file()
                and "debug" not in p.relative_to(dependency).parts
                and runtime_type(p)
            ):
                # ELF headers distinguish shared objects from helper executables;
                # neither library names nor executable permission bits suffice.
                kind = runtime_type(p)
                shared = shared_runtime(p, kind)
                if kind == "elf":
                    # PIE helpers are ET_DYN too; installed tools are an explicit
                    # development category, while plugin directories are retained.
                    shared = shared and not p.is_relative_to(
                        dependency / "tools"
                    )
                if shared:
                    collector.copy(p)
        if build.platform == "windows":
            redist = Path(os.environ["VCToolsRedistDir"])
            runtime_license = download(
                LOCK["msvcRuntimeLicense"], build.root / "downloads"
            )
            require(
                runtime_license.stat().st_size
                == LOCK["msvcRuntimeLicense"]["bytes"],
                "MSVC runtime license size mismatch",
            )
            collector.license(
                "MSVC",
                [runtime_license],
                os.environ["VCToolsVersion"],
                {
                    "license": LOCK["msvcRuntimeLicense"]["directoryUrl"],
                    "document": LOCK["msvcRuntimeLicense"]["url"],
                    "sha256": LOCK["msvcRuntimeLicense"]["sha256"],
                    "redistributable": "Microsoft Visual Studio redistributable CRT",
                },
            )
            for p in redist.glob("x64/Microsoft.VC*.*/*.dll"):
                if p.parent.name.endswith((".CRT", ".OpenMP")):
                    collector.copy(p)
                collector.files.setdefault(p.name.lower(), []).append(p)
    target = build.stage / "bin" / executable.name
    shutil.copy2(executable, target)
    collector.components["COLMAP"]["runtimeFiles"].append(
        target.relative_to(build.stage).as_posix()
    )
    collector.origins[str(target)] = str(executable)
    collector.queue.append(target)
    while collector.queue:
        path = collector.queue.pop()
        if str(path) in collector.checked:
            continue
        collector.checked.add(str(path))
        kind = runtime_type(path)
        require(
            kind
            == {"windows": "pe", "linux": "elf", "macos": "macho"}[
                build.platform
            ],
            f"Unexpected runtime file type: {path}",
        )
        getattr(collector, build.platform)(path)
    if build.platform == "macos":
        for file in sorted(collector.checked):
            build.execute(["codesign", "--force", "--sign", "-", file], "sign")
            build.execute(
                ["codesign", "--verify", "--strict", "--verbose=2", file],
                "sign",
            )
    else:
        collector.external["NVIDIA CUDA driver (runtime-loaded)"] = (
            "External GPU-only dependency; not bundled; CUDA SIFT / Caspar execution 未验证"
        )
        if build.platform == "linux":
            collector.external["ld-linux-x86-64.so.2"] = "System glibc loader"
            collector.external["GLVND vendor driver (runtime-loaded)"] = (
                "External system graphics driver selected by GLVND; not a Toolkit stub"
            )
    from smoke import create_model

    vocabulary = download(LOCK["offlineVocabulary"], build.root / "downloads")
    destination = build.stage / LOCK["offlineVocabulary"]["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vocabulary, destination)
    vocabulary_info = check_vocabulary(destination)
    collector.license(
        "COLMAP-SIFT-vocabulary",
        [build.source / "COPYING.txt"],
        "FAISS-v1-flickr100K-words256K",
        vocabulary_info,
    )
    collector.components["COLMAP-SIFT-vocabulary"]["runtimeFiles"] = [
        vocabulary_info["path"]
    ]
    collector.components["COLMAP-SIFT-vocabulary"]["licenseBasis"] = (
        "Upstream COLMAP resource; retain the upstream COLMAP copyright/license notice"
    )

    create_model(build.stage / "lib/validation/model")
    source_files = sorted((build.stage / "lib/validation/model").glob("*"))
    collector.license(
        "validation-model",
        [HERE / "VALIDATION-LICENSE.txt"],
        "1",
        "Authored deterministic synthetic fixture",
    )
    collector.components["validation-model"]["runtimeFiles"] = [
        p.relative_to(build.stage).as_posix() for p in source_files
    ]
    write_json(
        build.stage / "BUNDLED-COMPONENTS.json",
        {
            "schemaVersion": 1,
            "components": list(collector.components.values()),
            "systemDependencies": collector.external,
            "note": "Static dependencies retain license notices; runtime closure includes dynamically loaded plugins.",
        },
    )
    before = sum(
        p.stat().st_size
        for p in (build.root / "install").rglob("*")
        if p.is_file()
    )
    after = sum(p.stat().st_size for p in build.stage.rglob("*") if p.is_file())
    metadata = {
        **build.state,
        "colmapVersion": LOCK["colmapVersion"],
        "sourceRepository": LOCK["upstreamRepository"],
        "toolchainLockSha256": sha256(HERE / "toolchain-lock.json"),
        "tools": {
            "cmake": LOCK["cmakeVersion"],
            "ninja": LOCK["ninjaVersion"],
            "ccache": LOCK["ccacheVersion"],
        },
        "features": {
            **{k: False for k in LOCK["disabledFeatures"]},
            "cpuSift": True,
            "ceresCpuBA": True,
            "offlineLoopDetection": True,
            "vocabTreeMatching": True,
            "CUDA_ENABLED": build.platform != "macos",
            "CASPAR_ENABLED": build.platform != "macos",
            "CASPAR_USE_DOUBLE": False,
        },
        "cudaRelease": LOCK["cuda"]["release"]
        if build.platform != "macos"
        else None,
        "gpuArchitectures": LOCK["architectures"]
        if build.platform != "macos"
        else [],
        "offlineVocabulary": vocabulary_info,
        "compatibilityTarget": {
            "windows": "Windows 10 22H2 / Windows 11 (未实机验证)",
            "linux": "Ubuntu 22.04; glibc 2.35",
            "macos": "macOS 14 arm64",
        }[build.platform],
        "gpuValidation": {
            "cudaSift": "未验证" if build.platform != "macos" else "不适用",
            "casparBA": "未验证" if build.platform != "macos" else "不适用",
        },
        "bytes": {
            "installedBeforePruning": before,
            "runtimeAfterPruning": after,
            "definition": "Before: install tree; after: runtime closure plus licenses/fixture, before BUILD-INFO/SHA256SUMS",
        },
    }
    # Avoid embedding machine-specific source/tool paths as a public interface.
    for key in ("dependencyInventory", "cudaAssembly"):
        metadata.pop(key, None)
    metadata["dependencyVersions"] = [
        {"name": c["name"], "version": c["version"]}
        for c in collector.components.values()
    ]
    write_json(build.stage / "BUILD-INFO.json", metadata)
    build.state["collected"] = True
    build.save()


def package(build):
    require(build.state.get("verified"), "Cannot package an unverified runtime")
    name = (
        f"colmap-{LOCK['colmapVersion']}-runtime.{LOCK['runtimeRevision']}-{build.platform}-"
        f"{PLATFORMS[build.platform]}-{build.state['scriptCommit'][:8]}-"
        f"run{build.state['runId']}-attempt{build.state['runAttempt']}"
    )
    output = build.root / "dist"
    output.mkdir(exist_ok=True)
    sums = build.stage / "SHA256SUMS"
    sums.write_text(
        "\n".join(checksum_lines(build.stage)) + "\n", encoding="utf-8"
    )
    archive = output / (
        name + (".zip" if build.platform == "windows" else ".tar.xz")
    )
    require(
        not archive.exists(),
        "Archive already exists; never overwrite a verified archive",
    )
    if build.platform == "windows":
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as container:
            for p in sorted(build.stage.rglob("*")):
                if p.is_file():
                    container.write(p, p.relative_to(build.stage).as_posix())
    else:
        with tarfile.open(archive, "w:xz", preset=6) as container:
            for p in sorted(build.stage.iterdir()):
                container.add(p, arcname=p.name)
    unpacked = build.root / "extracted"
    require(
        not unpacked.exists(),
        "Extraction validation requires a fresh directory",
    )
    extract(archive, unpacked)
    verify_checksums(unpacked)
    from smoke import verify

    report = verify(build, unpacked)
    manifest = {
        "schemaVersion": 1,
        "verified": True,
        "archive": archive.name,
        "sha256": sha256(archive),
        "compressedBytes": archive.stat().st_size,
        "uncompressedBytes": sum(
            p.stat().st_size for p in build.stage.rglob("*") if p.is_file()
        ),
        "platform": build.platform,
        "architecture": PLATFORMS[build.platform],
        "version": LOCK["colmapVersion"],
        "runtimeRevision": LOCK["runtimeRevision"],
        "sourceCommit": LOCK["upstreamCommit"],
        "scriptCommit": build.state["scriptCommit"],
        "repository": build.state["repository"],
        "runId": build.state["runId"],
        "runAttempt": build.state["runAttempt"],
        "validation": report,
        "buildInfoSha256": sha256(unpacked / "BUILD-INFO.json"),
        "gpuValidation": read_json(unpacked / "BUILD-INFO.json")[
            "gpuValidation"
        ],
    }
    write_json(output / (name + ".manifest.json"), manifest)
    (output / (archive.name + ".sha256")).write_text(
        f"{manifest['sha256']}  {archive.name}\n"
    )
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write(f"artifact={name}\ndist={output}\n")

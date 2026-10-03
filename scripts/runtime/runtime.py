# SPDX-License-Identifier: BSD-3-Clause
"""Explicit build stages. Run --help; no stage commits, pushes or publishes."""

import argparse
import ctypes
import json
import os
import platform as host
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from common import (
    HERE,
    LOCK,
    PLATFORMS,
    canonical_path,
    check_cmake,
    check_cuda,
    check_cuda_commands,
    check_paths,
    compatible_key,
    digest,
    download,
    extract,
    read_json,
    require,
    run,
    sha256,
    write_json,
)


def emit(file, key, value):
    if file:
        with Path(file).open("a", encoding="utf-8") as stream:
            stream.write(f"{key}={value}\n")


def git(directory, *args):
    return run(["git", "-C", directory, *args]).strip()


def checkout(url, commit, destination):
    require(
        not destination.exists(),
        f"Source directory already exists: {destination}",
    )
    destination.mkdir(parents=True)
    git(destination, "init")
    git(destination, "remote", "add", "origin", url)
    git(destination, "fetch", "--depth=1", "origin", commit)
    git(destination, "checkout", "--detach", "FETCH_HEAD")
    require(
        git(destination, "rev-parse", "HEAD") == commit,
        "Source commit mismatch",
    )


WINDOWS_ENVIRONMENT_NAMES = (
    "PATH",
    "INCLUDE",
    "LIB",
    "LIBPATH",
    "VCToolsInstallDir",
    "VCToolsVersion",
    "VSINSTALLDIR",
    "VisualStudioVersion",
    "WindowsSdkDir",
    "WindowsSDKVersion",
    "VSCMD_ARG_TGT_ARCH",
    "VCToolsRedistDir",
)


def normalize_windows_environment(environment):
    """Restore canonical VS variable names after os.environ.copy().

    Python's Windows environment mapping is case-insensitive, but copy() is a
    plain case-sensitive dict whose keys are normally upper-case.  Build stages
    run in separate processes, so values imported through GITHUB_ENV need this
    normalization before they are recorded or reused.
    """
    result = dict(environment)
    folded = {key.casefold(): value for key, value in environment.items()}
    for name in WINDOWS_ENVIRONMENT_NAMES:
        value = folded.get(name.casefold())
        if value is not None:
            result[name] = value
    return result


def windows_vcpkg_toolset(sdk):
    full_version = sdk["VCToolsVersion"].rstrip("\\/")
    match = re.fullmatch(r"(\d+\.\d+)\.\d+", full_version)
    require(match, f"Unexpected VCToolsVersion: {full_version}")
    visual_studio = sdk["VSINSTALLDIR"].rstrip("\\/")
    require(visual_studio, "Empty VSINSTALLDIR")
    # vcpkg requires this triplet value without a trailing separator.  Escape
    # native backslashes for the generated CMake source rather than changing
    # the path spelling to forward slashes.
    cmake_path = visual_studio.replace("\\", "\\\\")
    return match.group(1), cmake_path


def linux_chainload_toolchain(vcpkg):
    official = (Path(vcpkg) / "scripts/toolchains/linux.cmake").as_posix()
    return "\n".join(
        (
            'set(CMAKE_C_COMPILER "/usr/bin/gcc-12" CACHE FILEPATH "" FORCE)',
            'set(CMAKE_CXX_COMPILER "/usr/bin/g++-12" CACHE FILEPATH "" FORCE)',
            'set(CMAKE_Fortran_COMPILER "/usr/bin/gfortran-12" CACHE FILEPATH "" FORCE)',
            'if(NOT VCPKG_TARGET_ARCHITECTURE STREQUAL "x64")',
            '  message(FATAL_ERROR "Expected vcpkg x64 target")',
            "endif()",
            f'include("{official}")',
            'if(NOT CMAKE_SYSTEM_PROCESSOR STREQUAL "x86_64")',
            '  message(FATAL_ERROR "Expected x86_64 CMake target")',
            "endif()",
            "",
        )
    )


class Build:
    def __init__(self, platform, root):
        self.platform = platform
        self.root = Path(root).resolve()
        self.source = self.root / "s"
        self.build = self.root / "b"
        self.temp = self.root / "t"
        self.logs = self.root / "logs"
        self.stage = self.root / "stage"
        self.triplet = {
            "windows": "x64-windows-release",
            "linux": "x64-linux",
            "macos": None,
        }[platform]
        self.state_file = self.root / "state.json"
        self.state = (
            read_json(self.state_file) if self.state_file.exists() else {}
        )
        self.env = os.environ.copy()
        if self.platform == "windows":
            self.env = normalize_windows_environment(self.env)
        self.env.update(
            {
                "TMP": str(self.temp),
                "TEMP": str(self.temp),
                "TMPDIR": str(self.temp),
                "CCACHE_DIR": str(self.root / "ccache"),
                "CCACHE_BASEDIR": str(self.root),
                "CCACHE_COMPILERCHECK": "content",
                "CCACHE_MAXSIZE": "1G",
            }
        )
        if self.state:
            toolpaths = [
                str(Path(p).parent) for p in self.state["tools"].values()
            ]
            if self.platform != "macos":
                cuda = self.root / "cuda"
                toolpaths.insert(0, str(cuda / "bin"))
                self.env.update(
                    CUDA_PATH=str(cuda),
                    CUDACXX=str(self.nvcc),
                    CUDAToolkit_ROOT=str(cuda),
                )
                if self.platform == "linux":
                    self.env.update(
                        CUDAHOSTCXX="/usr/bin/g++-12",
                        CC="/usr/bin/gcc-12",
                        CXX="/usr/bin/g++-12",
                        FC="/usr/bin/gfortran-12",
                    )
            self.env["PATH"] = (
                os.pathsep.join(toolpaths) + os.pathsep + self.env["PATH"]
            )
            self.env["CCACHE_EXTRAFILES"] = str(
                self.root / "compiler-identity.json"
            )
        if self.platform == "macos":
            self.env["MACOSX_DEPLOYMENT_TARGET"] = "14.0"

    @property
    def nvcc(self):
        return (
            self.root
            / "cuda/bin"
            / ("nvcc.exe" if self.platform == "windows" else "nvcc")
        )

    def tool(self, name):
        return self.state["tools"][name]

    def save(self):
        write_json(self.state_file, self.state)

    def execute(self, args, name, **kwargs):
        return run(
            args, env=self.env, log=self.logs / (name + ".log"), **kwargs
        )

    def prepare(self):
        expected = {"windows": "Windows", "linux": "Linux", "macos": "Darwin"}
        require(
            host.system() == expected[self.platform],
            "Requested platform differs from build host",
        )
        require(
            host.machine().lower()
            in (
                ["arm64", "aarch64"]
                if self.platform == "macos"
                else ["amd64", "x86_64"]
            ),
            "Wrong host architecture",
        )
        require(not self.state, "Existing build state; use a fresh root")
        require(
            not git(HERE.parent.parent, "status", "--porcelain"),
            "Build-script checkout must be clean to identify its actual commit",
        )
        for directory in (
            self.temp,
            self.logs,
            self.build,
            self.root / "vcpkg-cache",
            self.root / "ccache",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        emit(os.environ.get("GITHUB_OUTPUT"), "root", self.root)
        tools = {}
        for name, entry in LOCK["tools"][self.platform].items():
            archive = download(entry, self.root / "downloads")
            target = self.root / "tools" / name
            extract(archive, target)
            candidates = [
                p
                for p in target.rglob(
                    name + (".exe" if self.platform == "windows" else "")
                )
                if p.is_file() and (p.parent.name == "bin" or name != "cmake")
            ]
            require(
                len(candidates) == 1,
                f"Ambiguous {name} executable: {candidates}",
            )
            tools[name] = str(candidates[0])
            if self.platform != "windows":
                candidates[0].chmod(candidates[0].stat().st_mode | 0o111)
            output = run([tools[name], "--version"])
            require(
                LOCK[name + "Version"] in output, f"Wrong actual {name} version"
            )
        self.state = {
            "schemaVersion": 1,
            "platform": self.platform,
            "architecture": PLATFORMS[self.platform],
            "tools": tools,
            "sourceCommit": LOCK["upstreamCommit"],
            "scriptCommit": git(HERE.parent.parent, "rev-parse", "HEAD"),
            "repository": os.environ.get(
                "GITHUB_REPOSITORY", "ooolabdev/ooosplat-colmap"
            ),
            "runId": os.environ.get("GITHUB_RUN_ID", "local"),
            "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
            "runtimeRevision": LOCK["runtimeRevision"],
            "runnerImage": os.environ.get("ImageVersion", "local"),
            "testedSystem": host.platform(),
            "triplet": self.triplet,
        }
        if self.platform == "windows":
            launcher = self.root / "tools/compiler-launcher.py"
            shutil.copy2(HERE / "compiler_launcher.py", launcher)
            self.state["tools"]["compilerLauncher"] = str(launcher)
        if self.platform != "macos":
            cuda = self.root / "cuda"
            cuda.mkdir()
            manifest = read_json(
                download(
                    LOCK["cuda"]["redistributionManifest"],
                    self.root / "downloads",
                )
            )
            inventory = {}
            for component, entry in LOCK["cuda"]["archives"][
                self.platform
            ].items():
                require(
                    manifest[component]["version"] == entry["version"],
                    "CUDA manifest component mismatch",
                )
                vendor = manifest[component][
                    "windows-x86_64"
                    if self.platform == "windows"
                    else "linux-x86_64"
                ]
                require(
                    vendor["sha256"] == entry["sha256"],
                    "CUDA manifest hash mismatch",
                )
                archive = download(entry, self.root / "downloads")
                unpack = self.root / "unpack" / component
                extract(archive, unpack)
                children = list(unpack.iterdir())
                require(
                    len(children) == 1 and children[0].is_dir(),
                    "Unexpected NVIDIA archive layout",
                )
                origin = children[0]
                license_paths = []
                for item in origin.rglob("*"):
                    if item.is_dir():
                        continue
                    rel = item.relative_to(origin)
                    if re.search(r"license|eula|notice", item.name, re.I):
                        dest = cuda / "licenses" / component / rel
                        license_paths.append(dest.relative_to(cuda).as_posix())
                    elif item.name == "version.json":
                        dest = cuda / "component-metadata" / component / rel
                    else:
                        dest = cuda / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        require(
                            sha256(dest) == sha256(item),
                            f"CUDA archive collision: {dest}",
                        )
                    else:
                        shutil.copy2(item.resolve(), dest)
                require(
                    license_paths,
                    f"Missing NVIDIA license in {component} archive",
                )
                inventory[component] = {**entry, "licenseFiles": license_paths}
                shutil.rmtree(unpack)
            if self.platform == "linux":
                # NVIDIA redistributable archives use lib/, while nvcc's
                # compiler-identification link command follows the conventional
                # Toolkit layout and searches lib64/ (including lib64/stubs).
                # Keep one verified set of files and expose that expected path.
                lib64 = cuda / "lib64"
                require(not lib64.exists(), "Unexpected CUDA lib64 collision")
                lib64.symlink_to("lib", target_is_directory=True)
                for library in ("libcudadevrt.a", "libcudart_static.a"):
                    require(
                        (lib64 / library).is_file(),
                        f"CUDA link-time runtime is missing: {library}",
                    )
            # Preserve the official vendor SDK metadata verbatim. This is an
            # assembled redistributable toolkit, not an installer-generated SDK.
            metadata = download(
                LOCK["cuda"]["metadata"][self.platform], self.root / "downloads"
            )
            shutil.copy2(metadata, cuda / "version.json")
            write_json(
                cuda / "assembly.json",
                {
                    "origin": "NVIDIA redistributable archives",
                    "manifest": LOCK["cuda"]["redistributionManifest"],
                    "components": inventory,
                    "files": {
                        p.relative_to(cuda).as_posix(): sha256(p)
                        for p in cuda.rglob("*")
                        if p.is_file()
                    },
                },
            )
            self.state["cudaAssembly"] = inventory
        checkout(
            LOCK["upstreamRepository"], LOCK["upstreamCommit"], self.source
        )
        require(
            re.search(
                r'set\(COLMAP_VERSION "4\.2\.1"\)',
                (self.source / "CMakeLists.txt").read_text(),
            ),
            "Unexpected COLMAP source version",
        )
        self.save()
        for key, value in {
            "runtime_root": self.root,
            "runtime_platform": self.platform,
        }.items():
            emit(os.environ.get("GITHUB_ENV"), key.upper(), value)
        emit(os.environ.get("GITHUB_OUTPUT"), "root", self.root)

    def compiler_identity(self):
        compiler = shutil.which(
            "cl.exe"
            if self.platform == "windows"
            else ("g++-12" if self.platform == "linux" else "clang++"),
            path=self.env["PATH"],
        )
        require(compiler, "Host C++ compiler not found")
        result = subprocess.run(
            [compiler, "/Bv" if self.platform == "windows" else "--version"],
            env=self.env,
            capture_output=True,
            text=True,
            errors="replace",
        )
        version = result.stdout + result.stderr
        require(
            "Microsoft" in version
            if self.platform == "windows"
            else result.returncode == 0,
            "Unable to identify host compiler",
        )
        windows_sdk = {
            key: self.env.get(key)
            for key in ("VCToolsVersion", "WindowsSDKVersion", "VSINSTALLDIR")
        }
        if self.platform == "windows":
            missing = [key for key, value in windows_sdk.items() if not value]
            require(
                not missing,
                "Missing Visual Studio environment values: "
                + ", ".join(missing),
            )
        identity = {
            "compiler": str(Path(compiler).absolute()),
            "sha256": sha256(compiler),
            "version": version,
            "sdk": windows_sdk,
        }
        # Fortran selection affects LAPACK ABI even when COLMAP itself uses C++.
        # vcpkg-provided compilers are locked by the registry recipe; external
        # compiler candidates must also participate in the compatibility key.
        identity["auxiliaryCompilers"] = {}
        if self.platform != "macos":
            for name in ("gfortran-12", "gfortran", "flang", "flang-new"):
                candidate = shutil.which(name, path=self.env["PATH"])
                if candidate:
                    identity["auxiliaryCompilers"][name] = {
                        "path": candidate,
                        "sha256": sha256(candidate),
                        "version": self.execute(
                            [candidate, "--version"], "compiler-identity"
                        ).strip(),
                    }
            if self.platform == "linux":
                require(
                    "gfortran-12" in identity["auxiliaryCompilers"],
                    "Locked gfortran-12 compiler not found",
                )
                identity["selectedFortranCompiler"] = identity[
                    "auxiliaryCompilers"
                ]["gfortran-12"]
        if self.platform == "macos":
            identity["sdk"] = self.execute(
                ["xcrun", "--show-sdk-version"], "sdk"
            ).strip()
            identity["xcode"] = self.execute(
                ["xcodebuild", "-version"], "sdk"
            ).strip()
        write_json(self.root / "compiler-identity.json", identity)
        self.state["toolchain"] = identity
        return identity

    def preflight(self):
        require(
            git(self.source, "rev-parse", "HEAD") == LOCK["upstreamCommit"],
            "Upstream identity changed",
        )
        require(
            not git(
                self.source, "status", "--porcelain", "--untracked-files=no"
            ),
            "Modified upstream source",
        )
        identity = self.compiler_identity()
        cuda_sources = list(
            (self.source / "src/thirdparty/Symforce-Caspar/generated/f32").glob(
                "*.cu"
            )
        )
        require(cuda_sources, "Missing generated Caspar sources")
        longest = max(cuda_sources, key=lambda p: len(str(p)))
        suffix = ".obj" if self.platform == "windows" else ".o"
        output = (
            self.build
            / "_deps/caspar-build/CMakeFiles/caspar_lib_core.dir"
            / (longest.name + suffix)
        )
        predicted = list(self.source.rglob("*")) + [
            output,
            Path(str(output) + ".d"),
            self.temp / (longest.stem + ".cpp1.ii"),
            self.temp / (longest.stem + ".cudafe1.cpp"),
        ]
        self.state["pathPreflight"] = check_paths(
            predicted, self.temp, self.platform
        )
        cc = self.tool("ccache")
        self.execute([cc, "--zero-stats"], "cache-probe")
        probe = self.root / "p"
        probe.mkdir(exist_ok=True)
        cpp = probe / "cache.cc"
        cpp.write_text("int cache_probe() { return 42; }\n", encoding="utf-8")
        time.sleep(1.1)  # Respect ccache's file timestamp safety checks.
        cxx = identity["compiler"]
        if self.platform == "windows":
            command = [
                cc,
                cxx,
                "/nologo",
                "/c",
                cpp,
                "/Fo" + str(probe / "cache.obj"),
            ]
        else:
            command = [cc, cxx, "-c", cpp, "-o", probe / "cache.o"]
        self.execute(command, "cache-probe")
        self.execute(command, "cache-probe")
        if self.platform != "macos":
            cuda = self.root / "cuda"
            assembly = read_json(cuda / "assembly.json")
            for name, expected in assembly["files"].items():
                require(
                    sha256(cuda / name) == expected,
                    f"Changed CUDA component file: {name}",
                )
            self.state["cudaIdentity"] = check_cuda(
                read_json(cuda / "version.json"),
                self.platform,
                self.execute([self.nvcc, "--version"], "cuda-identity"),
            )
            flags = [
                "--std=c++17",
                "--expt-relaxed-constexpr",
                "--use_fast_math",
            ]
            for arch in LOCK["architectures"]:
                flags.append(
                    f"--generate-code=arch=compute_{arch},code=[sm_{arch},compute_{arch}]"
                )
            if self.platform == "windows":
                flags += [
                    "-Xcompiler=/Zc:preprocessor",
                    "--pre-include="
                    + str(
                        self.source
                        / "src/thirdparty/Symforce-Caspar/msvc_cuda_compact.h"
                    ),
                ]
            else:
                flags += ["-ccbin=/usr/bin/g++-12", "-Xcompiler=-fPIC"]
            # Compile the original longest filename at the predicted real object
            # path; nvcc's temporary outputs are also kept for a path audit.
            output.parent.mkdir(parents=True, exist_ok=True)
            if (cuda / "include/cccl").is_dir():
                flags += ["-I" + str(cuda / "include/cccl")]
            self.execute(
                [
                    self.nvcc,
                    *flags,
                    "--keep",
                    "--keep-dir",
                    self.temp,
                    "-c",
                    longest,
                    "-o",
                    output,
                ],
                "caspar-path-probe",
            )
            command = [cc, self.nvcc, *flags, "-c", longest, "-o", output]
            self.execute(command, "caspar-probe")
            self.execute(command, "caspar-probe")
            check_paths(
                list(self.temp.rglob("*")) + [output], self.temp, self.platform
            )
            output.unlink()
            sample = probe / "cccl.cu"
            sample.write_text(
                "#include <thrust/device_vector.h>\n#include <cub/cub.cuh>\n"
                "#include <cooperative_groups/reduce.h>\n__global__ void probe() {}\n"
            )
            self.execute(
                [
                    self.nvcc,
                    *flags,
                    "-c",
                    sample,
                    "-o",
                    probe / ("cccl" + suffix),
                ],
                "cccl-probe",
            )
        stats = self.execute([cc, "--print-stats"], "cache-probe")
        hits = sum(
            int(n)
            for key, n in re.findall(r"^(\w+)\s+(\d+)$", stats, re.M)
            if key
            in (
                "cache_hit_direct",
                "cache_hit_preprocessed",
                "direct_cache_hit",
                "preprocessed_cache_hit",
            )
        )
        require(
            hits >= (2 if self.platform != "macos" else 1),
            "Compiler cache probe did not hit",
        )
        require(
            not git(
                self.source, "status", "--porcelain", "--untracked-files=no"
            ),
            "Probe modified source",
        )
        files = {
            str(p.relative_to(self.source)): sha256(p)
            for p in [
                self.source / "vcpkg.json",
                self.source / "vcpkg-configuration.json",
                *sorted((self.source / "cmake/vcpkg/ports").rglob("*")),
            ]
            if p.is_file()
        }
        dependency_recipe = {
            "defaultFeatures": False,
            "features": ["cuda"] if self.platform != "macos" else [],
            "forceLockedCMakeAndNinja": self.platform != "macos",
            "windowsSdk": (
                identity["sdk"] if self.platform == "windows" else None
            ),
            "windowsVcpkgToolset": (
                windows_vcpkg_toolset(identity["sdk"])
                if self.platform == "windows"
                else None
            ),
            "linuxChainload": (
                linux_chainload_toolchain(Path("/locked-vcpkg-root"))
                if self.platform == "linux"
                else None
            ),
        }
        files["scripts/runtime/generated-dependency-recipe"] = digest(
            dependency_recipe
        )
        self.state["cacheFamily"] = compatible_key(
            self.platform, identity, files, self.triplet
        )
        self.state["dependencyFiles"] = files
        self.state["dependencyRecipe"] = dependency_recipe
        self.state["preflightPassed"] = True
        self.save()
        emit(
            os.environ.get("GITHUB_OUTPUT"), "family", self.state["cacheFamily"]
        )

    def dependencies(self):
        require(
            self.state.get("preflightPassed"),
            "Preflight must pass before dependencies",
        )
        memory = available_memory()
        jobs = max(1, min(2, os.cpu_count() or 1, int(memory / (3 * 1024**3))))
        self.env["VCPKG_MAX_CONCURRENCY"] = str(jobs)
        self.env["CMAKE_BUILD_PARALLEL_LEVEL"] = str(jobs)
        self.state["dependencyResources"] = {
            "cpuCount": os.cpu_count(),
            "availableMemory": memory,
            "jobs": jobs,
        }
        if self.platform == "macos":
            formulas = [
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
            ]
            self.execute(["brew", "install", *formulas], "dependencies")
            data = json.loads(
                self.execute(
                    ["brew", "info", "--json=v2", "--installed"],
                    "brew-inventory",
                )
            )
            self.state["dependencyInventory"] = data
            self.state["homebrewVersion"] = self.execute(
                ["brew", "--version"], "brew-inventory"
            ).strip()
            self.state["homebrewCoreCommit"] = sorted(
                {
                    f.get("tap_git_head")
                    for f in data["formulae"]
                    if f.get("tap_git_head")
                }
            )
            self.state["brewPrefix"] = self.execute(
                ["brew", "--prefix"], "brew-inventory"
            ).strip()
        else:
            vcpkg = self.root / "v"
            self.env["VCPKG_FORCE_SYSTEM_BINARIES"] = "1"
            checkout(
                "https://github.com/microsoft/vcpkg.git",
                LOCK["vcpkgCommit"],
                vcpkg,
            )
            self.execute(
                [str(vcpkg / "bootstrap-vcpkg.bat"), "-disableMetrics"]
                if self.platform == "windows"
                else ["sh", vcpkg / "bootstrap-vcpkg.sh", "-disableMetrics"],
                "dependencies",
            )
            triplets = self.root / "triplets"
            triplets.mkdir()
            original = next(
                vcpkg.glob("triplets/**/" + self.triplet + ".cmake")
            )
            content = original.read_text()
            if self.platform == "windows":
                sdk = self.state["toolchain"]["sdk"]
                version, visual_studio = windows_vcpkg_toolset(sdk)
                content += f'\nset(VCPKG_PLATFORM_TOOLSET v143)\nset(VCPKG_PLATFORM_TOOLSET_VERSION "{version}")\n'
                content += f'set(VCPKG_VISUAL_STUDIO_PATH "{visual_studio}")\n'
            else:
                chain = triplets / "host.cmake"
                chain.write_text(linux_chainload_toolchain(vcpkg))
                content += f'\nset(VCPKG_CHAINLOAD_TOOLCHAIN_FILE "{chain.as_posix()}")\n'
            content += (
                "\nset(VCPKG_ENV_PASSTHROUGH CUDA_PATH CUDACXX CUDAHOSTCXX)\n"
            )
            (triplets / (self.triplet + ".cmake")).write_text(content)
            self.env["VCPKG_BINARY_SOURCES"] = (
                f"clear;files,{self.root / 'vcpkg-cache'},readwrite"
            )
            executable = vcpkg / (
                "vcpkg.exe" if self.platform == "windows" else "vcpkg"
            )
            self.execute(
                [
                    executable,
                    "install",
                    "--triplet=" + self.triplet,
                    "--x-manifest-root=" + str(self.source),
                    "--x-install-root=" + str(self.root / "i"),
                    "--overlay-triplets=" + str(triplets),
                    "--x-no-default-features",
                    "--x-feature=cuda",
                ],
                "dependencies",
            )
            bundled_build_tools = [
                path
                for path in (vcpkg / "downloads/tools").rglob("*")
                if path.is_file()
                and path.name.lower()
                in ("cmake", "cmake.exe", "ninja", "ninja.exe")
            ]
            require(
                not bundled_build_tools,
                "vcpkg bypassed the locked CMake or Ninja: "
                + ", ".join(map(str, bundled_build_tools)),
            )
            status = (self.root / "i/vcpkg/status").read_text()
            require(
                not re.search(
                    r"Package: ceres\n(?:(?!\n\n).)*Feature: (cuda|cudss)",
                    status,
                    re.S,
                ),
                "Ceres must provide CPU BA without GPU dependencies",
            )
            self.state["dependencyInventory"] = status
            self.state["vcpkgCommit"] = git(vcpkg, "rev-parse", "HEAD")
        inventory = self.state["dependencyInventory"]
        if self.platform == "macos":
            inventory = {
                f["name"]: {
                    "versions": sorted(x["version"] for x in f["installed"]),
                    "recipe": f.get("ruby_source_checksum"),
                    "stable": f["urls"].get("stable"),
                    "tapCommit": f.get("tap_git_head"),
                }
                for f in inventory["formulae"]
            }
        self.state["compilerCacheFamily"] = (
            self.state["cacheFamily"] + "-" + digest(inventory)[:16]
        )
        self.save()
        emit(
            os.environ.get("GITHUB_OUTPUT"),
            "family",
            self.state["compilerCacheFamily"],
        )

    def configure(self):
        cmake = self.tool("cmake")
        self.env["PATH"] = (
            str(Path(self.tool("ccache")).parent)
            + os.pathsep
            + self.env["PATH"]
        )
        flags = [
            "-GNinja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DCMAKE_INSTALL_PREFIX=" + str(self.root / "install"),
            "-DCMAKE_INSTALL_LIBDIR=lib",
            "-DCMAKE_MAKE_PROGRAM:FILEPATH=" + self.tool("ninja"),
            "-DCASPAR_USE_DOUBLE=OFF",
            "-DCMAKE_CXX_COMPILER:FILEPATH="
            + self.state["toolchain"]["compiler"],
            "-DCOLMAP_HASH_MAP_BACKEND=STD",
            "-DCCACHE_ENABLED=ON",
            "-DHIP_ENABLED=OFF",
            *[f"-D{name}=OFF" for name in LOCK["disabledFeatures"]],
        ]
        if self.platform == "windows":
            manifest = self.root / "tools/windows-utf8.manifest"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE / "windows-utf8.manifest", manifest)
            flags += [
                "-DCCACHE:STRING="
                + ";".join(
                    (
                        sys.executable,
                        self.tool("compilerLauncher"),
                        self.tool("ccache"),
                    )
                ),
                "-DCMAKE_EXE_LINKER_FLAGS:STRING=/MANIFEST:EMBED "
                "/MANIFESTINPUT:" + manifest.as_posix(),
            ]
            self.state["windowsUtf8Manifest"] = {
                "path": str(manifest),
                "sha256": sha256(manifest),
            }
        if self.platform == "macos":
            omp = self.execute(
                ["brew", "--prefix", "libomp"], "configure"
            ).strip()
            flags += [
                "-DCUDA_ENABLED=OFF",
                "-DCASPAR_ENABLED=OFF",
                "-DCMAKE_OSX_ARCHITECTURES=arm64",
                "-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0",
                "-DCMAKE_PREFIX_PATH=" + self.state["brewPrefix"],
                "-DOpenMP_ROOT=" + omp,
                "-DOpenMP_C_FLAGS=-Xpreprocessor -fopenmp",
                "-DOpenMP_CXX_FLAGS=-Xpreprocessor -fopenmp",
                "-DOpenMP_C_LIB_NAMES=omp",
                "-DOpenMP_CXX_LIB_NAMES=omp",
                "-DOpenMP_omp_LIBRARY=" + omp + "/lib/libomp.dylib",
                "-DOpenMP_C_INCLUDE_DIR=" + omp + "/include",
                "-DOpenMP_CXX_INCLUDE_DIR=" + omp + "/include",
            ]
        else:
            flags += [
                "-DCUDA_ENABLED=ON",
                "-DCASPAR_ENABLED=ON",
                "-DCMAKE_CUDA_COMPILER:FILEPATH=" + str(self.nvcc),
                "-DCUDAToolkit_ROOT:PATH=" + str(self.root / "cuda"),
                "-DCMAKE_CUDA_ARCHITECTURES:STRING="
                + ";".join(map(str, LOCK["architectures"])),
                "-DCMAKE_TOOLCHAIN_FILE="
                + str(self.root / "v/scripts/buildsystems/vcpkg.cmake"),
                "-DVCPKG_TARGET_TRIPLET=" + self.triplet,
                "-DVCPKG_MANIFEST_INSTALL=OFF",
                "-DVCPKG_INSTALLED_DIR=" + str(self.root / "i"),
                "-DVCPKG_OVERLAY_TRIPLETS=" + str(self.root / "triplets"),
            ]
            flags += (
                ["-DCMAKE_CUDA_FLAGS:STRING=-Xcompiler=/Zc:preprocessor"]
                if self.platform == "windows"
                else [
                    "-DCMAKE_C_COMPILER=/usr/bin/gcc-12",
                    "-DCMAKE_CXX_COMPILER=/usr/bin/g++-12",
                    "-DCMAKE_CUDA_HOST_COMPILER:FILEPATH=/usr/bin/g++-12",
                ]
            )
        self.execute(
            [cmake, "-S", self.source, "-B", self.build, *flags], "configure"
        )
        self.check_configuration()
        self.state["configurePassed"] = True
        self.save()

    def check_configuration(self):
        cache = (self.build / "CMakeCache.txt").read_text()
        if self.platform == "windows":
            manifest = self.state["windowsUtf8Manifest"]
            require(
                sha256(manifest["path"]) == manifest["sha256"],
                "Windows UTF-8 manifest changed after configuration",
            )
            require(
                str(Path(manifest["path"])).replace("\\", "/").casefold()
                in (self.build / "build.ninja")
                .read_text(encoding="utf-8")
                .replace("\\", "/")
                .casefold(),
                "Actual linker commands omit the Windows UTF-8 manifest",
            )
        files = list(
            (self.build / "CMakeFiles").glob("*/CMakeCUDACompiler.cmake")
        )
        compiler = files[0].read_text() if files else ""
        parsed = check_cmake(cache, compiler, self.platform, self.nvcc)
        cpp_info = list(
            (self.build / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake")
        )
        require(len(cpp_info) == 1, "Missing actual host compiler information")
        selected = re.search(
            r'set\(CMAKE_CXX_COMPILER "([^"]+)"\)', cpp_info[0].read_text()
        )
        require(
            selected
            and canonical_path(selected.group(1), self.platform)
            == canonical_path(
                self.state["toolchain"]["compiler"], self.platform
            ),
            "CMake selected another host C++ compiler",
        )
        self.state["actualCxxCompiler"] = cpp_info[0].read_text()
        commands = read_json(self.build / "compile_commands.json")
        actual = json.loads(
            self.execute(
                [self.tool("ninja"), "-C", self.build, "-t", "compdb", "-x"],
                "actual-compilation-database",
            )
        )
        compilations = [
            c
            for c in actual
            if Path(c["file"]).suffix in (".c", ".cc", ".cpp", ".cxx", ".cu")
        ]
        require(compilations, "Missing actual Ninja compilation rules")
        for command in compilations:
            require(
                self.tool("ccache").replace("\\", "/").casefold()
                in command["command"].replace("\\", "/").casefold(),
                "Actual compiler command lacks cache launcher",
            )
        if self.platform != "macos":
            cuda = [c for c in commands if c["file"].endswith(".cu")]
            check_cuda_commands(cuda, self.platform, self.nvcc)
            check_cuda_commands(
                [c for c in compilations if c["file"].endswith(".cu")],
                self.platform,
                self.nvcc,
            )
            require(
                any(
                    "Symforce-Caspar/generated/f32"
                    in c["file"].replace("\\", "/")
                    for c in cuda
                ),
                "No actual f32 Caspar compile command",
            )
            require(
                not any(
                    "generated/f64" in c["file"].replace("\\", "/")
                    for c in cuda
                ),
                "f64 Caspar built",
            )
        for language in ("C", "CXX") + (
            ("CUDA",) if self.platform != "macos" else ()
        ):
            require(
                parsed.get(
                    "CMAKE_" + language + "_COMPILER_LAUNCHER",
                    self.tool("ccache"),
                )
                == self.tool("ccache"),
                "Compiler cache launcher mismatch",
            )
        paths = []
        for c in compilations:
            paths.append(Path(c["directory"]) / c["file"])
            output = c.get("output")
            if not output:
                match = re.search(
                    r'(?: -o | /Fo)(?:"([^"]+)"|(\S+))', c["command"]
                )
                output = (
                    next((v for v in match.groups() if v), None)
                    if match
                    else None
                )
            require(output, "Cannot audit actual object path")
            obj = Path(c["directory"]) / output
            paths += [obj, Path(str(obj) + ".d")]
        self.state["configuredPaths"] = check_paths(
            paths, self.temp, self.platform
        )
        self.state["cmakeConfiguration"] = {
            k: parsed[k]
            for k in parsed
            if k in LOCK["disabledFeatures"]
            or k.startswith(("CMAKE_CUDA", "CASPAR_", "CUDA_ENABLED"))
        }
        self.execute(
            [self.tool("ninja"), "-C", self.build, "-t", "commands"],
            "actual-commands",
        )

    def compile(self):
        require(
            self.state.get("configurePassed"),
            "Configuration audit must pass before compile",
        )
        original = self.state["toolchain"]
        require(
            self.compiler_identity() == original,
            "Host toolchain changed after dependency installation",
        )
        self.check_configuration()
        self.execute([self.tool("ccache"), "--zero-stats"], "compile")
        memory = available_memory()
        jobs = max(1, min(2, os.cpu_count() or 1, int(memory / (3 * 1024**3))))
        self.state["buildResources"] = {
            "cpuCount": os.cpu_count(),
            "availableMemory": memory,
            "jobs": jobs,
        }
        self.save()
        self.execute(
            [
                self.tool("cmake"),
                "--build",
                self.build,
                "--parallel",
                jobs,
                "--verbose",
                "--",
                "-d",
                "keeprsp",
            ],
            "compile",
        )
        self.check_configuration()
        check_paths(
            list(self.temp.rglob("*")) + list(self.build.rglob("*.rsp")),
            self.temp,
            self.platform,
        )
        self.execute([self.tool("cmake"), "--install", self.build], "install")
        require(
            not git(
                self.source, "status", "--porcelain", "--untracked-files=no"
            ),
            "Build modified tracked upstream source",
        )
        self.state["compiled"] = True
        self.save()

    def stats(self):
        self.execute(
            [self.tool("ccache"), "--show-stats", "--verbose"],
            "compiler-cache-statistics",
        )
        self.execute(
            [self.tool("ccache"), "--print-stats"], "compiler-cache-statistics"
        )


def available_memory():
    if host.system() == "Windows":

        class Memory(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                *[
                    (n, ctypes.c_ulonglong)
                    for n in (
                        "total",
                        "available",
                        "pageTotal",
                        "pageAvailable",
                        "virtualTotal",
                        "virtualAvailable",
                        "extended",
                    )
                ],
            ]

        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        require(
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)),
            "Cannot read available RAM",
        )
        return memory.available
    if host.system() == "Linux":
        return (
            int(
                re.search(
                    r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text()
                ).group(1)
            )
            * 1024
        )
    pages = subprocess.check_output(["vm_stat"], text=True)
    size = int(re.search(r"page size of (\d+) bytes", pages).group(1))
    return (
        sum(
            int(re.search(key + r":\s+(\d+)", pages).group(1))
            for key in ("Pages free", "Pages inactive", "Pages speculative")
        )
        * size
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=[
            "prepare",
            "preflight",
            "dependencies",
            "configure",
            "compile",
            "collect",
            "verify",
            "package",
            "stats",
        ],
    )
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument(
        "--root", help="Fresh short build root; default D:/c or /tmp/c"
    )
    args = parser.parse_args()
    build = Build(
        args.platform,
        args.root or ("D:/c" if args.platform == "windows" else "/tmp/c"),
    )
    if args.stage == "collect":
        from bundle import collect

        collect(build)
    elif args.stage == "verify":
        from smoke import verify

        verify(build, build.stage)
        build.state["verified"] = True
        build.save()
    elif args.stage == "package":
        from bundle import package

        package(build)
    else:
        getattr(build, args.stage)()


if __name__ == "__main__":
    main()

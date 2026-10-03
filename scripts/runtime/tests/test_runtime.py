# SPDX-License-Identifier: BSD-3-Clause
import copy
import hashlib
import io
import json
import os
import sqlite3
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bundle import Collector, collect, package, runtime_type, shared_runtime
from common import (
    HERE,
    LOCK,
    PLATFORMS,
    check_cmake,
    check_cuda,
    check_cuda_commands,
    check_paths,
    check_vocabulary,
    checksum_lines,
    compatible_key,
    extract,
    read_json,
    safe_member,
    sha256,
    verify_checksums,
    write_json,
)
from release import CHECKS, assert_new_release, validate_manifests
from runtime import (
    linux_chainload_toolchain,
    normalize_windows_environment,
    windows_vcpkg_toolset,
)
from smoke import (
    check_loop_matches,
    check_mask,
    create_model,
    images,
    inspect_package,
    keypoints,
    sanitized_env,
)


class CudaIdentityTests(unittest.TestCase):
    def test_official_snapshots_and_line_endings(self):
        for platform in ("windows", "linux"):
            data = (
                HERE / "tests/fixtures" / ("cuda-" + platform + ".json")
            ).read_bytes()
            self.assertEqual(
                hashlib.sha256(data).hexdigest(),
                LOCK["cuda"]["metadata"][platform]["sha256"],
            )
            for ending in (b"\n", b"\r\n"):
                parsed = json.loads(
                    data.replace(b"\r\n", b"\n").replace(b"\n", ending)
                )
                self.assertEqual(
                    check_cuda(parsed, platform, "release 13.2, V13.2.51")[
                        "cuda"
                    ],
                    LOCK["cuda"]["sdk"][platform],
                )

    def test_missing_and_mixed_components(self):
        for platform in ("windows", "linux"):
            original = read_json(
                HERE / "tests/fixtures" / ("cuda-" + platform + ".json")
            )
            for key in ("cuda", *LOCK["cuda"]["components"]):
                missing = copy.deepcopy(original)
                del missing[key]
                with (
                    self.subTest(platform=platform, missing=key),
                    self.assertRaises(RuntimeError),
                ):
                    check_cuda(missing, platform, "V13.2.51")
                mixed = copy.deepcopy(original)
                mixed[key]["version"] = "13.1.80"
                with (
                    self.subTest(platform=platform, mixed=key),
                    self.assertRaises(RuntimeError),
                ):
                    check_cuda(mixed, platform, "V13.2.51")

    def test_wrong_actual_nvcc(self):
        data = read_json(HERE / "tests/fixtures/cuda-linux.json")
        for output in ("V13.1.80", "release 13.2", "V13.2.51 V12.9.1"):
            with self.assertRaises(RuntimeError):
                check_cuda(data, "linux", output)


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.nvcc = Path("D:/c/cuda/bin/nvcc.exe").as_posix()
        self.cache = (
            "\n".join(f"{name}:BOOL=OFF" for name in LOCK["disabledFeatures"])
            + "\n"
            + "\n".join(
                [
                    "CUDA_ENABLED:BOOL=ON",
                    "CASPAR_ENABLED:BOOL=ON",
                    "CASPAR_USE_DOUBLE:BOOL=OFF",
                    "CMAKE_CUDA_ARCHITECTURES:STRING=75;80;86;89;90;100;120",
                    "CMAKE_CUDA_COMPILER:FILEPATH=" + str(self.nvcc),
                    "CUDAToolkit_ROOT:PATH="
                    + Path(self.nvcc).parent.parent.as_posix(),
                ]
            )
        )
        self.compiler = f'set(CMAKE_CUDA_COMPILER "{self.nvcc}")\nset(CMAKE_CUDA_COMPILER_VERSION "13.2.51")'
        self.command = (
            str(self.nvcc)
            + " -Xcompiler=/Zc:preprocessor "
            + " ".join(
                f"--generate-code=arch=compute_{a},code=[sm_{a},compute_{a}]"
                for a in LOCK["architectures"]
            )
        )

    def test_actual_configuration(self):
        check_cmake(self.cache, self.compiler, "windows", self.nvcc)
        check_cuda_commands([{"command": self.command}], "windows", self.nvcc)

    def test_wrong_selected_path_version_architecture_and_feature(self):
        cases = [
            (self.cache.replace("cuda/bin", "other/bin"), self.compiler),
            (self.cache, self.compiler.replace("cuda/bin", "other/bin")),
            (self.cache, self.compiler.replace("13.2.51", "13.1.80")),
            (self.cache.replace("75;80;86;89;90;100;120", "75"), self.compiler),
            (
                self.cache.replace(
                    "GUI_ENABLED:BOOL=OFF", "GUI_ENABLED:BOOL=ON"
                ),
                self.compiler,
            ),
            (
                self.cache.replace(
                    "CASPAR_ENABLED:BOOL=ON", "CASPAR_ENABLED:BOOL=OFF"
                ),
                self.compiler,
            ),
        ]
        for cache, compiler in cases:
            with self.assertRaises(RuntimeError):
                check_cmake(cache, compiler, "windows", self.nvcc)

    def test_actual_command_rejects_missing_preprocessor_and_wrong_nvcc_arch(
        self,
    ):
        for command in (
            self.command.replace("-Xcompiler=/Zc:preprocessor", ""),
            self.command.replace("/Zc:preprocessor", "/Zc:preprocessor-"),
            self.command.replace("cuda/bin", "other/bin"),
            self.command.replace("cuda/bin", "other/bin")
            + " -DFALSE_COMPILER="
            + str(self.nvcc),
            self.command.replace("compute_120", "compute_121"),
            self.command + " -allow-unsupported-compiler",
            self.command + " @response.rsp",
        ):
            with self.assertRaises(RuntimeError):
                check_cuda_commands(
                    [{"command": command}], "windows", self.nvcc
                )

    def test_long_source_object_dependency_and_temp_paths(self):
        for kind in (
            "source.cu",
            "source.cu.obj",
            "source.cu.obj.d",
            "source.cudafe1.cpp",
        ):
            with self.assertRaises(RuntimeError):
                check_paths(["D:/c/" + "a" * 240 + kind], "D:/c/t", "windows")
        with self.assertRaises(RuntimeError):
            check_paths(["D:/c/s/short.cu"], "D:/c/" + "a" * 40, "windows")
        check_paths(
            ["D:/c/s/file.cu", "D:/c/b/file.cu.obj"], "D:/c/t", "windows"
        )

    def test_windows_environment_copy_restores_visual_studio_key_case(self):
        environment = {
            "PATH": "D:/toolchain",
            "VCTOOLSVERSION": "14.44.35207\\",
            "VSINSTALLDIR": "C:/Visual Studio/",
            "WINDOWSSDKVERSION": "10.0.26100.0\\",
        }
        normalized = normalize_windows_environment(environment)
        self.assertEqual(normalized["VCToolsVersion"], "14.44.35207\\")
        self.assertEqual(normalized["WindowsSDKVersion"], "10.0.26100.0\\")
        self.assertEqual(normalized["VSINSTALLDIR"], "C:/Visual Studio/")

    def test_windows_vcpkg_selector_uses_prefix_and_canonical_path(self):
        version, path = windows_vcpkg_toolset(
            {
                "VCToolsVersion": "14.44.35207\\",
                "VSINSTALLDIR": (
                    "C:\\Program Files\\Microsoft Visual Studio\\2022"
                    "\\Enterprise\\"
                ),
            }
        )
        self.assertEqual(version, "14.44")
        self.assertEqual(
            path,
            "C:\\\\Program Files\\\\Microsoft Visual Studio\\\\2022"
            "\\\\Enterprise",
        )
        self.assertFalse(path.endswith(("/", "\\")))

    def test_linux_chainload_keeps_vcpkg_architecture_toolchain(self):
        text = linux_chainload_toolchain(Path("/tmp/c/v"))
        self.assertIn("VCPKG_TARGET_ARCHITECTURE", text)
        self.assertIn('scripts/toolchains/linux.cmake")', text)
        self.assertIn('CMAKE_SYSTEM_PROCESSOR STREQUAL "x86_64"', text)
        self.assertIn("/usr/bin/gcc-12", text)
        self.assertIn("FORCE", text)

        changed = text.replace("gcc-12", "gcc-13")
        self.assertNotEqual(
            compatible_key("linux", {}, {"recipe": text}, "x64-linux"),
            compatible_key("linux", {}, {"recipe": changed}, "x64-linux"),
        )


class CacheAndArchiveTests(unittest.TestCase):
    def test_compatible_family_changes_only_for_real_inputs(self):
        a = compatible_key(
            "windows",
            {"version": "19.39"},
            {"manifest": "abc"},
            "x64-windows-release",
        )
        self.assertEqual(
            a,
            compatible_key(
                "windows",
                {"version": "19.39"},
                {"manifest": "abc"},
                "x64-windows-release",
            ),
        )
        self.assertNotEqual(
            a,
            compatible_key(
                "windows",
                {"version": "19.40"},
                {"manifest": "abc"},
                "x64-windows-release",
            ),
        )
        self.assertNotEqual(
            a,
            compatible_key(
                "windows",
                {"version": "19.39"},
                {"manifest": "xyz"},
                "x64-windows-release",
            ),
        )
        self.assertNotEqual(
            a,
            compatible_key(
                "linux", {"version": "19.39"}, {"manifest": "abc"}, "x64-linux"
            ),
        )

    def test_no_runtime_uploaded_without_acceptance(self):
        class Fake:
            state = {}

        with self.assertRaises(RuntimeError):
            collect(Fake())
        with self.assertRaises(RuntimeError):
            package(Fake())

    def test_elf_classification_excludes_static_archive_and_objects(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "object"
            file.write_bytes(b"!<arch>\n" + bytes(64))
            self.assertIsNone(runtime_type(file))
            header = bytearray(64)
            header[:6] = b"\x7fELF\x02\x01"
            struct.pack_into("<H", header, 18, 62)
            for value, expected in ((1, None), (2, "elf"), (3, "elf")):
                struct.pack_into("<H", header, 16, value)
                file.write_bytes(header)
                self.assertEqual(runtime_type(file), expected)
            struct.pack_into("<H", header, 18, 183)
            file.write_bytes(header)
            with self.assertRaises(RuntimeError):
                runtime_type(file)

    def test_unknown_shared_object_and_relocated_reuse_keep_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stage = root / "stage"
            build = SimpleNamespace(
                root=root, stage=stage, platform="linux", triplet="x64-linux"
            )
            collector = Collector(build)
            source = root / "unknown-runtime"
            header = bytearray(64)
            header[:6] = b"\x7fELF\x02\x01"
            struct.pack_into("<HH", header, 16, 3, 62)
            source.write_bytes(header)
            self.assertTrue(shared_runtime(source, runtime_type(source)))
            license_file = root / "LICENSE"
            license_file.write_text("fixture license")
            collector.license("fixture", [license_file])
            collector.owners[str(source.resolve())] = "fixture"
            target = collector.copy(source)
            target.write_bytes(target.read_bytes() + b"relocated")
            self.assertEqual(collector.owner(target), "fixture")
            self.assertEqual(collector.copy(target), target)
            self.assertTrue(target.read_bytes().endswith(b"relocated"))

    def test_checksum_inventory_detects_changes_and_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "LICENSE").write_text("license")
            (root / "SHA256SUMS").write_text(
                "\n".join(checksum_lines(root)) + "\n"
            )
            verify_checksums(root)
            (root / "LICENSE").write_text("tampered")
            with self.assertRaises(RuntimeError):
                verify_checksums(root)
            (root / "LICENSE").unlink()
            with self.assertRaises(RuntimeError):
                verify_checksums(root)

    def test_archive_traversal(self):
        for path in (
            "../escape",
            "/absolute",
            "C:\\escape",
            "safe/../../escape",
        ):
            with self.assertRaises(RuntimeError):
                safe_member(path)
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "bad.zip"
            with zipfile.ZipFile(file, "w") as archive:
                archive.writestr("../escape", "bad")
            with self.assertRaises(RuntimeError):
                extract(file, Path(d) / "output")


def lock_small_vocabulary(test):
    # Tiny gate fixture only. The actual official 72 MB asset is downloaded by
    # collect; tests never claim that these bytes are a usable FAISS index.
    payload = struct.pack("<3i", 1, 128, 64) + b"vocabulary gate fixture"
    patcher = patch.dict(
        LOCK["offlineVocabulary"],
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
    )
    patcher.start()
    test.addCleanup(patcher.stop)
    return payload


class RuntimeGateTests(unittest.TestCase):
    def setUp(self):
        self.vocabulary = lock_small_vocabulary(self)

    def fixture(self, root):
        (root / "bin").mkdir()
        (root / "licenses").mkdir()
        (root / "lib").mkdir()
        (root / "licenses/license.txt").write_text("fixture license")
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        for path in (root / "bin/colmap", root / "lib/png.imageio.so"):
            path.write_bytes(header)
        vocabulary = root / LOCK["offlineVocabulary"]["path"]
        vocabulary.parent.mkdir(parents=True)
        vocabulary.write_bytes(self.vocabulary)
        info = {
            "schemaVersion": 1,
            "sourceCommit": LOCK["upstreamCommit"],
            "colmapVersion": LOCK["colmapVersion"],
            "runtimeRevision": 1,
            "platform": "linux",
            "features": {
                **{k: False for k in LOCK["disabledFeatures"]},
                "CASPAR_ENABLED": True,
                "CASPAR_USE_DOUBLE": False,
                "offlineLoopDetection": True,
                "vocabTreeMatching": True,
            },
            "gpuArchitectures": LOCK["architectures"],
            "offlineVocabulary": dict(LOCK["offlineVocabulary"]),
        }
        write_json(root / "BUILD-INFO.json", info)
        write_json(
            root / "BUNDLED-COMPONENTS.json",
            {
                "components": [
                    {
                        "name": "fixture",
                        "licenseFiles": ["licenses/license.txt"],
                        "runtimeFiles": [
                            "bin/colmap",
                            "lib/png.imageio.so",
                            LOCK["offlineVocabulary"]["path"],
                        ],
                    }
                ]
            },
        )

    def test_missing_runtime_plugin_and_license_block_delivery(self):
        for missing in (
            "bin/colmap",
            "lib/png.imageio.so",
            "licenses/license.txt",
            LOCK["offlineVocabulary"]["path"],
        ):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.fixture(root)
                (root / missing).unlink()
                with self.assertRaises(RuntimeError):
                    inspect_package(root, "linux")

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            file = root / "BUNDLED-COMPONENTS.json"
            components = read_json(file)
            components["components"][0]["runtimeFiles"].remove(
                LOCK["offlineVocabulary"]["path"]
            )
            write_json(file, components)
            with self.assertRaisesRegex(
                RuntimeError, "license/component provenance"
            ):
                inspect_package(root, "linux")

    def test_missing_dependency_or_unrelocated_library_blocks_delivery(self):
        for response in (
            "libpng.so => not found",
            "libpng.so => /outside/libpng.so (0x1)",
        ):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.fixture(root)
                with (
                    patch("smoke.run", return_value=response),
                    self.assertRaises(RuntimeError),
                ):
                    inspect_package(root, "linux")

    def test_mask_and_image_failures(self):
        original = [(20, 30), (400, 40)]
        check_mask(original, [(400, 40)])
        for invalid in (original, [(20, 30)], []):
            with self.assertRaises(RuntimeError):
                check_mask(original, invalid)
        with tempfile.TemporaryDirectory() as d:
            database = Path(d) / "empty.db"
            with closing(sqlite3.connect(database)) as db:
                db.executescript(
                    "CREATE TABLE images(image_id INTEGER,name TEXT);"
                    "CREATE TABLE keypoints(image_id INTEGER,rows INTEGER,cols INTEGER,data BLOB);"
                )
            with self.assertRaises(RuntimeError):
                keypoints(database, "broken.jpg")

    def test_failed_extracted_acceptance_does_not_emit_usable_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stage = root / "stage"
            stage.mkdir()
            self.fixture(stage)
            build = SimpleNamespace(
                root=root,
                stage=stage,
                platform="windows",
                state={
                    "verified": True,
                    "scriptCommit": "a" * 40,
                    "runId": "42",
                    "runAttempt": "1",
                },
            )
            output = root / "outputs.txt"
            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}),
                patch(
                    "smoke.verify",
                    side_effect=RuntimeError(
                        "isolated image/plugin validation failed"
                    ),
                ),
                self.assertRaises(RuntimeError),
            ):
                package(build)
            self.assertFalse(output.exists())
            self.assertFalse(list((root / "dist").glob("*.manifest.json")))


class VocabularyTests(unittest.TestCase):
    def test_official_sift_asset_lock(self):
        asset = LOCK["offlineVocabulary"]
        self.assertEqual(
            asset["sha256"],
            "96ca8ec8ea60b1f73465aaf2c401fd3b3ca75cdba2d3c50d6a2f6f760f275ddc",
        )
        self.assertEqual(asset["bytes"], 72412636)
        self.assertTrue(
            asset["url"].startswith(
                "https://github.com/colmap/colmap/releases/download/3.11.1/"
            )
        )
        self.assertEqual(asset["featureType"], "SIFT")

    def test_required_tree_size_hash_and_faiss_format(self):
        payload = lock_small_vocabulary(self)
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "vocabulary.bin"
            with self.assertRaisesRegex(RuntimeError, "Missing required"):
                check_vocabulary(file)
            file.write_bytes(payload)
            check_vocabulary(file)
            file.write_bytes(payload[:-1])
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                check_vocabulary(file)
            file.write_bytes(payload[:-1] + b"!")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                check_vocabulary(file)
            for header in ((262144, 128, 64), (1, 256, 64), (1, 128, 32)):
                wrong = struct.pack("<3i", *header) + payload[12:]
                file.write_bytes(wrong)
                with (
                    patch.dict(LOCK["offlineVocabulary"], sha256=sha256(file)),
                    self.assertRaisesRegex(RuntimeError, "FAISS/SIFT"),
                ):
                    check_vocabulary(file)

    def test_adjacent_matches_cannot_claim_loop_detection(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "loops.db"
            with closing(sqlite3.connect(file)) as db:
                db.executescript(
                    "CREATE TABLE images(image_id INTEGER,name TEXT);"
                    "CREATE TABLE two_view_geometries(pair_id INTEGER,rows INTEGER);"
                    "INSERT INTO images VALUES(10,'third'),(2,'first'),(7,'second');"
                )
                db.execute(
                    "INSERT INTO two_view_geometries VALUES(?,50)",
                    (2 * 2147483647 + 7,),
                )
                db.commit()
                with self.assertRaisesRegex(RuntimeError, "non-adjacent"):
                    check_loop_matches(file)
                db.execute(
                    "INSERT INTO two_view_geometries VALUES(?,50)",
                    (2 * 2147483647 + 10,),
                )
                db.commit()
                self.assertEqual(check_loop_matches(file), 1)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.vocabulary = lock_small_vocabulary(self)

    def make_archives(self, root):
        for platform, arch in PLATFORMS.items():
            info = {
                "schemaVersion": 1,
                "repository": "ooolabdev/ooosplat-colmap",
                "platform": platform,
                "architecture": arch,
                "runtimeRevision": 1,
                "runId": "42",
                "runAttempt": "1",
                "scriptCommit": "a" * 40,
                "sourceCommit": LOCK["upstreamCommit"],
                "cpuValidation": {"passed": True, "checks": sorted(CHECKS)},
                "features": {
                    **{k: False for k in LOCK["disabledFeatures"]},
                    "CASPAR_ENABLED": platform != "macos",
                    "CASPAR_USE_DOUBLE": False,
                    "offlineLoopDetection": True,
                    "vocabTreeMatching": True,
                },
                "gpuArchitectures": LOCK["architectures"]
                if platform != "macos"
                else [],
                "offlineVocabulary": dict(LOCK["offlineVocabulary"]),
            }
            folder = root / platform
            folder.mkdir()
            # Metadata gate fixtures never execute a pretend COLMAP binary.
            archive = folder / (
                "runtime.zip" if platform == "windows" else "runtime.tar.xz"
            )
            metadata = json.dumps(info).encode()
            entries = {
                "BUILD-INFO.json": metadata,
                LOCK["offlineVocabulary"]["path"]: self.vocabulary,
                "bin/colmap.exe"
                if platform == "windows"
                else "bin/colmap": b"fixture",
            }
            entries["SHA256SUMS"] = (
                "\n".join(
                    f"{hashlib.sha256(content).hexdigest()}  {name}"
                    for name, content in sorted(entries.items())
                )
                + "\n"
            ).encode()
            if platform == "windows":
                with zipfile.ZipFile(archive, "w") as z:
                    for name, content in entries.items():
                        z.writestr(name, content)
            else:
                with tarfile.open(archive, "w:xz") as z:
                    for name, content in entries.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        z.addfile(member, io.BytesIO(content))
            digest = sha256(archive)
            manifest = {
                **info,
                "version": LOCK["colmapVersion"],
                "verified": True,
                "validation": info["cpuValidation"],
                "archive": archive.name,
                "sha256": digest,
                "compressedBytes": archive.stat().st_size,
                "buildInfoSha256": hashlib.sha256(metadata).hexdigest(),
            }
            write_json(folder / "runtime.manifest.json", manifest)
            (folder / (archive.name + ".sha256")).write_text(
                f"{digest}  {archive.name}\n"
            )

    def validate(self, root):
        return validate_manifests(
            root, "ooolabdev/ooosplat-colmap", 42, 1, "a" * 40, 1
        )

    def test_all_three_same_original_archives(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_archives(root)
            assets, data = self.validate(root)
            self.assertEqual(len(assets), 9)
            self.assertEqual(len(data), 3)

    def test_missing_platform_mixed_identity_and_failed_acceptance(self):
        changes = [
            ("verified", False),
            ("scriptCommit", "b" * 40),
            ("runAttempt", "2"),
            ("runtimeRevision", 2),
            ("platform", "linux"),
            ("validation", {"passed": True, "checks": ["cpu-sift"]}),
        ]
        for key, value in changes:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.make_archives(root)
                file = root / "windows/runtime.manifest.json"
                data = read_json(file)
                data[key] = value
                write_json(file, data)
                with self.assertRaises(RuntimeError):
                    self.validate(root)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_archives(root)
            (root / "windows/runtime.manifest.json").unlink()
            with self.assertRaises(RuntimeError):
                self.validate(root)

    def test_hash_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_archives(root)
            with (root / "linux/runtime.tar.xz").open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaises(RuntimeError):
                self.validate(root)

    def test_existing_tag_or_release(self):
        for values in (({}, None), (None, {})):
            with (
                patch("release.api", side_effect=values),
                self.assertRaises(RuntimeError),
            ):
                assert_new_release(
                    "ooolabdev/ooosplat-colmap", "colmap-4.2.1-runtime.1"
                )

    def test_internal_tampering_rejected_even_with_updated_outer_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_archives(root)
            archive = root / "windows/runtime.zip"
            with zipfile.ZipFile(archive) as container:
                entries = {
                    i.filename: container.read(i.filename)
                    for i in container.infolist()
                }
            entries["bin/colmap.exe"] = b"changed runtime"
            with zipfile.ZipFile(archive, "w") as container:
                for name, content in entries.items():
                    container.writestr(name, content)
            file = root / "windows/runtime.manifest.json"
            manifest = read_json(file)
            manifest.update(
                sha256=sha256(archive), compressedBytes=archive.stat().st_size
            )
            write_json(file, manifest)
            archive.with_name(archive.name + ".sha256").write_text(
                f"{manifest['sha256']}  {archive.name}\n"
            )
            with self.assertRaisesRegex(
                RuntimeError, "internal file set / checksum mismatch"
            ):
                self.validate(root)

    def test_release_rejects_missing_tree_even_with_consistent_checksums(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_archives(root)
            archive = root / "windows/runtime.zip"
            with zipfile.ZipFile(archive) as container:
                entries = {
                    i.filename: container.read(i.filename)
                    for i in container.infolist()
                }
            del entries[LOCK["offlineVocabulary"]["path"]]
            del entries["SHA256SUMS"]
            entries["SHA256SUMS"] = (
                "\n".join(
                    f"{hashlib.sha256(content).hexdigest()}  {name}"
                    for name, content in sorted(entries.items())
                )
                + "\n"
            ).encode()
            with zipfile.ZipFile(archive, "w") as container:
                for name, content in entries.items():
                    container.writestr(name, content)
            file = root / "windows/runtime.manifest.json"
            manifest = read_json(file)
            manifest.update(
                sha256=sha256(archive), compressedBytes=archive.stat().st_size
            )
            write_json(file, manifest)
            archive.with_name(archive.name + ".sha256").write_text(
                f"{manifest['sha256']}  {archive.name}\n"
            )
            with self.assertRaisesRegex(
                RuntimeError, "locked offline vocabulary"
            ):
                self.validate(root)


class WorkflowAndFixtureTests(unittest.TestCase):
    def test_cache_miss_restore_and_failed_run_save_contract(self):
        import yaml

        data = yaml.load(
            (
                HERE.parents[1] / ".github/workflows/runtime-build.yml"
            ).read_text(),
            Loader=yaml.BaseLoader,
        )
        steps = data["jobs"]["build"]["steps"]
        restore = [
            s for s in steps if "actions/cache/restore@" in s.get("uses", "")
        ]
        self.assertEqual(len(restore), 2)
        for step in restore:
            self.assertIn("outputs.family", step["with"]["restore-keys"])
            self.assertNotIn("\n", step["with"]["restore-keys"])
            self.assertIn("github.run_id", step["with"]["key"])
            self.assertIn("github.run_attempt", step["with"]["key"])
        # A miss follows the same preflight, dependency and compiler audits;
        # no cache-hit expression can skip them or bypass a failed verification.
        self.assertNotIn("cache-hit", json.dumps(steps))
        preflight = next(
            i for i, s in enumerate(steps) if s.get("id") == "preflight"
        )
        self.assertLess(
            preflight, next(i for i, s in enumerate(steps) if s in restore)
        )
        save = [s for s in steps if "actions/cache/save@" in s.get("uses", "")]
        self.assertTrue(
            all(
                "always()" in s["if"] and s.get("continue-on-error") == "true"
                for s in save
            )
        )

    def test_only_manual_full_builds_and_default_disabled_publication(self):
        import yaml

        workflows = HERE.parents[1] / ".github/workflows"
        for path in list(workflows.glob("build-*.yml")) + [
            workflows / "runtime-build.yml",
            workflows / "runtime-release.yml",
        ]:
            data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(set(data["on"]), {"workflow_dispatch"}, path)
        release = yaml.load(
            (workflows / "runtime-release.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            release["on"]["workflow_dispatch"]["inputs"]["publish"]["default"],
            "false",
        )
        build = yaml.load(
            (workflows / "runtime-build.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            build["jobs"]["build"]["strategy"]["fail-fast"], "false"
        )
        save = [
            s
            for s in build["jobs"]["build"]["steps"]
            if "actions/cache/save@" in s.get("uses", "")
        ]
        self.assertEqual(len(save), 2)
        for step in save:
            self.assertIn("always()", step["if"])
            self.assertIn("github.run_attempt", step["with"]["key"])

    def test_authored_fixtures_and_alpha_mask(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mask = images(root / "images")
            rgba = Image.open(root / "images/rgba.png")
            reference = Image.open(root / "images/view0.png")
            self.assertEqual(rgba.convert("RGB").tobytes(), reference.tobytes())
            self.assertEqual(
                rgba.getchannel("A").tobytes(),
                Image.open(mask / "rgba.png.png").tobytes(),
            )
            create_model(root / "model")
            self.assertEqual(
                len((root / "model/images.txt").read_text().splitlines()), 6
            )

    def test_development_paths_removed(self):
        with patch.dict(
            os.environ,
            {
                "LD_LIBRARY_PATH": "/cuda/lib",
                "CUDA_PATH": "D:/cuda",
                "LIB": "dev",
                "OPENIMAGEIO_PLUGIN_PATH": "dev",
            },
        ):
            env = sanitized_env("linux")
            self.assertNotIn("LD_LIBRARY_PATH", env)
            self.assertNotIn("LIB", env)
            self.assertNotIn("CUDA_PATH", env)
            self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "-1")


if __name__ == "__main__":
    unittest.main()

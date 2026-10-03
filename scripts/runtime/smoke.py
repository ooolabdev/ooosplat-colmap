# SPDX-License-Identifier: BSD-3-Clause
"""CPU acceptance on the installed and re-extracted runtime, never the build binary."""

import argparse
import os
import random
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from common import (
    LOCK,
    check_vocabulary,
    read_json,
    require,
    run,
    verify_checksums,
    write_json,
)


def create_model(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    points = [
        (x * 0.3, y * 0.3, 4 + ((x + y) % 3) * 0.3)
        for y in range(-2, 3)
        for x in range(-3, 4)
    ]
    (directory / "cameras.txt").write_text(
        "1 PINHOLE 640 480 600 600 320 240\n"
    )
    images = []
    for image, center in enumerate((-0.3, 0, 0.3), 1):
        observations = []
        for point, (x, y, z) in enumerate(points, 1):
            observations += [
                str(600 * (x - center) / z + 320),
                str(600 * y / z + 240),
                str(point),
            ]
        images += [
            f"{image} 1 0 0 0 {-center} 0 0 1 view{image}.png",
            " ".join(observations),
        ]
    (directory / "images.txt").write_text("\n".join(images) + "\n")
    tracks = []
    for point, (x, y, z) in enumerate(points, 1):
        tracks.append(
            f"{point} {x + 0.015} {y - 0.01} {z + 0.02} 128 128 128 1.0 "
            + " ".join(f"{image} {point - 1}" for image in range(1, 4))
        )
    (directory / "points3D.txt").write_text("\n".join(tracks) + "\n")


def images(directory):
    from PIL import Image, ImageFilter

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(421)
    patches = []
    for y in range(6):
        for x in range(8):
            texture = Image.frombytes(
                "L", (48, 48), bytes(rng.randrange(256) for _ in range(48 * 48))
            )
            texture = texture.filter(ImageFilter.GaussianBlur(0.6)).convert(
                "RGB"
            )
            patches.append(
                (45 + x * 75, 40 + y * 75, 3 + rng.random() * 3, texture)
            )
    for index, center in enumerate((-0.18, -0.06, 0.06, 0.18)):
        image = Image.new("RGB", (640, 480), (40, 40, 40))
        for x, y, z, texture in patches:
            image.paste(texture, (round(x - 600 * center / z - 24), y - 24))
        image.save(directory / f"view{index}.png")
    reference = Image.open(directory / "view0.png").convert("RGB")
    reference.save(directory / "jpeg.jpg", quality=95)
    alpha = Image.new("L", reference.size, 255)
    alpha.paste(0, (0, 0, 320, 480))
    rgba = reference.convert("RGBA")
    rgba.putalpha(alpha)
    rgba.save(directory / "rgba.png")
    # COLMAP drops the image alpha channel; the separate mask explicitly carries
    # its semantics. No image decoder success is mistaken for mask application.
    mask = directory.parent / "masks"
    mask.mkdir(exist_ok=True)
    rgba.getchannel("A").save(mask / "rgba.png.png")
    return mask


def sanitized_env(platform):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(
            ("CUDA", "VCPKG", "CCACHE", "DYLD_", "LD_", "QT_")
        ) or key in (
            "CMAKE_PREFIX_PATH",
            "OPENIMAGEIO_PLUGIN_PATH",
            "OIIO_LIBRARY_PATH",
            "INCLUDE",
            "LIB",
            "LIBPATH",
        ):
            env.pop(key, None)
    env["PATH"] = (
        str(Path(env["SystemRoot"]) / "System32")
        + os.pathsep
        + env["SystemRoot"]
        if platform == "windows"
        else "/usr/bin:/bin:/usr/sbin:/sbin"
    )
    env["CUDA_VISIBLE_DEVICES"] = "-1"
    env["LC_ALL"] = "C.UTF-8" if platform == "linux" else "en_US.UTF-8"
    env["OMP_NUM_THREADS"] = "1"
    return env


def inspect_package(root, platform):
    from bundle import DRIVERS, LINUX_SYSTEM, pe_imports, runtime_type

    root = Path(root).resolve()
    info = read_json(root / "BUILD-INFO.json")
    require(
        info["schemaVersion"] == 1
        and info["sourceCommit"] == LOCK["upstreamCommit"],
        "Wrong source identity",
    )
    require(
        info["colmapVersion"] == LOCK["colmapVersion"]
        and info["platform"] == platform,
        "Wrong version/platform",
    )
    require(
        info["runtimeRevision"] == LOCK["runtimeRevision"],
        "Wrong runtime revision",
    )
    require(
        info.get("offlineVocabulary") == LOCK["offlineVocabulary"],
        "Wrong offline vocabulary identity",
    )
    require(
        info["features"].get("offlineLoopDetection") is True
        and info["features"].get("vocabTreeMatching") is True,
        "Missing offline loop capability",
    )
    check_vocabulary(root / LOCK["offlineVocabulary"]["path"])
    for feature in LOCK["disabledFeatures"]:
        require(
            info["features"].get(feature) is False,
            f"Wrong feature metadata: {feature}",
        )
    require(
        info["features"]["CASPAR_ENABLED"] is (platform != "macos"),
        "Wrong Caspar capability",
    )
    require(
        info["features"]["CASPAR_USE_DOUBLE"] is False, "Wrong Caspar precision"
    )
    require(
        info["gpuArchitectures"]
        == (LOCK["architectures"] if platform != "macos" else []),
        "Wrong GPU architectures",
    )
    components = read_json(root / "BUNDLED-COMPONENTS.json")
    covered = set()
    license_texts = set()
    for component in components["components"]:
        require(
            component["licenseFiles"], f"No license for {component['name']}"
        )
        for name in component["licenseFiles"]:
            require(
                Path(name).parts and Path(name).parts[0] == "licenses",
                f"License text is outside licenses/: {name}",
            )
            path = root / name
            require(
                path.is_relative_to(root)
                and path.is_file()
                and path.stat().st_size > 0,
                f"Missing license text: {name}",
            )
            license_texts.add(name)
        for name in component["runtimeFiles"]:
            require((root / name).is_file(), f"Missing component file: {name}")
            covered.add(name)
    require(
        LOCK["offlineVocabulary"]["path"] in covered,
        "Offline vocabulary has no license/component provenance",
    )
    env = sanitized_env(platform)
    runtime_files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        require(not DRIVERS.match(path.name), "NVIDIA driver was bundled")
        development_suffix = path.suffix.lower() in (
            ".a",
            ".lib",
            ".h",
            ".hpp",
            ".pdb",
            ".obj",
            ".o",
        )
        require(
            not development_suffix or relative in license_texts,
            f"Development file was bundled: {relative}",
        )
        require(
            path.name.lower()
            not in (
                "ffmpeg",
                "ffmpeg.exe",
                "ffprobe",
                "ffprobe.exe",
                "brush_app",
                "brush_app.exe",
            ),
            "Unrelated executable was bundled",
        )
        kind = runtime_type(path)
        if not kind:
            continue
        require(
            relative in covered,
            f"Runtime has no license provenance: {path}",
        )
        runtime_files.append(path)
        if platform == "linux":
            require(kind == "elf", "Wrong runtime architecture/type")
            text = run(["ldd", path], env=env)
            require(
                "not found" not in text,
                f"Missing relocated runtime dependency: {path}",
            )
            for name, origin in re.findall(
                r"^\s*(\S+) => (.*?) \(", text, re.M
            ):
                require(
                    LINUX_SYSTEM.match(name)
                    or DRIVERS.match(name)
                    or Path(origin).resolve().is_relative_to(root),
                    f"Runtime loaded an unbundled development library: {origin}",
                )
        elif platform == "windows":
            for name in pe_imports(path):
                require(
                    DRIVERS.match(name)
                    or (root / "bin" / name).is_file()
                    or name.lower().startswith(("api-ms-win-", "ext-ms-win-"))
                    or (Path(env["SystemRoot"]) / "System32" / name).is_file(),
                    f"Missing DLL: {name}",
                )
        else:
            require(kind == "macho", "Wrong runtime architecture/type")
            run(["codesign", "--verify", "--strict", path], env=env)
            text = run(["otool", "-L", path], env=env)
            for line in text.splitlines()[1:]:
                name = line.strip().split(" (", 1)[0]
                require(
                    name.startswith(("/usr/lib/", "/System/Library/"))
                    or (
                        name.startswith("@rpath/")
                        and (root / "lib" / name[7:]).is_file()
                    ),
                    f"Unrelocated Mach-O dependency: {name}",
                )
    require(runtime_files, "Empty runtime")
    if (root / "SHA256SUMS").exists():
        verify_checksums(root)
    return info


def execute(executable, args, root, env, log, platform):
    import psutil

    stream = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(executable), *map(str, args)],
        env=env,
        stdout=stream,
        stderr=subprocess.STDOUT,
    )
    mapped = set()
    return_code = None
    try:
        while process.poll() is None:
            if (
                platform != "macos"
            ):  # psutil memory_maps is unavailable on Darwin.
                with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    mapped.update(
                        x.path
                        for x in psutil.Process(process.pid).memory_maps()
                        if x.path
                    )
            time.sleep(0.05)
        return_code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        stream.close()
    output = log.read_text(encoding="utf-8", errors="replace")
    if return_code != 0:
        diagnostic = Path(root).parent / "logs" / "acceptance-failure"
        diagnostic.mkdir(parents=True, exist_ok=True)
        shutil.copy2(log, diagnostic / log.name)
    require(
        return_code == 0,
        f"CPU acceptance failed ({return_code}): {' '.join(map(str, args))}; "
        f"see {log}\nLast command output:\n{output[-8000:]}",
    )
    for name in mapped:
        normalized = name.replace("\\", "/").casefold()
        require(
            not any(
                p in normalized
                for p in ("/cellar/", "/vcpkg/", "/cuda/", "/i/x64-")
            ),
            f"CPU process loaded a development library: {name}",
        )
    return output, sorted(mapped)


def keypoints(database, image):
    with closing(sqlite3.connect(database)) as db:
        row = db.execute(
            "SELECT k.rows,k.cols,k.data FROM keypoints k JOIN images i ON k.image_id=i.image_id WHERE i.name=?",
            (image,),
        ).fetchone()
    require(
        row and row[0] > 0 and row[1] >= 2, f"No CPU SIFT keypoints: {image}"
    )
    return [v[:2] for v in struct.iter_unpack("<" + "f" * row[1], row[2])]


def check_mask(original, masked):
    require(
        masked
        and len(masked) < len(original)
        and all(x >= 320 for x, _ in masked),
        "Alpha-derived mask did not filter keypoints",
    )


def check_loop_matches(database):
    # COLMAP 4.2.1 util/types.h encodes image pairs with int32_t::max().
    with closing(sqlite3.connect(database)) as db:
        ordered = [
            row[0]
            for row in db.execute("SELECT image_id FROM images ORDER BY name")
        ]
        rank = {image: index for index, image in enumerate(ordered)}
        pairs = [
            row[0]
            for row in db.execute(
                "SELECT pair_id FROM two_view_geometries WHERE rows>0"
            )
        ]
    loops = [
        pair
        for pair in pairs
        if abs(rank[pair // 2147483647] - rank[pair % 2147483647]) >= 2
    ]
    require(
        loops,
        "Offline vocabulary loop detection produced no verified non-adjacent image pair",
    )
    return len(loops)


def local_verify(root, work, platform):
    root = Path(root).resolve()
    work = Path(work).resolve() / "中文 路径 CPU 验收"
    work.mkdir(parents=True, exist_ok=True)
    info = inspect_package(root, platform)
    env = sanitized_env(platform)
    if platform == "macos":
        env["DYLD_PRINT_LIBRARIES"] = "1"
    exe = root / "bin" / ("colmap.exe" if platform == "windows" else "colmap")
    calls = []
    loaded = set()

    def cli(command, *args):
        text, mapped = execute(
            exe,
            [command, *args],
            root,
            env,
            work / f"{len(calls):02}-{command}.log",
            platform,
        )
        if platform == "macos":
            require(
                "/opt/homebrew/" not in text,
                "Runtime loaded a Homebrew library",
            )
            mapped = re.findall(r"dyld\[\d+\]:.*? (/.*)$", text, re.M)
        loaded.update(mapped)
        calls.append(command)
        return text

    text = cli("-h")
    require(
        "COLMAP 4.2.1" in text and LOCK["upstreamCommit"][:7] in text,
        "Executable version/source identity mismatch",
    )
    require(
        ("with CUDA" in text) is (platform != "macos"),
        "Executable CUDA capability mismatch",
    )
    help_ba = cli("bundle_adjuster", "-h")
    require(
        ("BundleAdjustmentCaspar." in help_ba) is (platform != "macos"),
        "Executable Caspar capability mismatch",
    )
    images_dir = work / "图像 images"
    masks = images(images_dir)
    database = work / "features 数据库.db"
    common = [
        "--ImageReader.camera_model",
        "PINHOLE",
        "--ImageReader.camera_params",
        "600,600,320,240",
        "--ImageReader.single_camera",
        "1",
        "--FeatureExtraction.use_gpu",
        "0",
        "--FeatureExtraction.num_threads",
        "1",
    ]
    cli(
        "feature_extractor",
        "--database_path",
        database,
        "--image_path",
        images_dir,
        *common,
    )
    for name in ("jpeg.jpg", "rgba.png", "view0.png"):
        keypoints(database, name)
    rgba = keypoints(database, "rgba.png")
    reference = keypoints(database, "view0.png")
    require(
        rgba == reference,
        "RGBA decoding changed RGB values / implicitly applied alpha",
    )
    only = work / "rgba-only"
    only.mkdir(exist_ok=True)
    shutil.copy2(images_dir / "rgba.png", only / "rgba.png")
    masked_db = work / "masked.db"
    cli(
        "feature_extractor",
        "--database_path",
        masked_db,
        "--image_path",
        only,
        "--ImageReader.mask_path",
        masks,
        *common,
    )
    masked = keypoints(masked_db, "rgba.png")
    check_mask(rgba, masked)
    # Start from unmatched features. Adjacent-only matching cannot satisfy this
    # check; at least one non-adjacent verified pair must come from retrieval.
    loop_database = work / "offline loops.db"
    shutil.copy2(database, loop_database)
    loop_text = cli(
        "sequential_matcher",
        "--database_path",
        loop_database,
        "--FeatureMatching.use_gpu",
        "0",
        "--FeatureMatching.num_threads",
        "1",
        "--SequentialMatching.overlap",
        "1",
        "--SequentialMatching.quadratic_overlap",
        "0",
        "--SequentialMatching.loop_detection",
        "1",
        "--SequentialMatching.loop_detection_period",
        "1",
        "--SequentialMatching.loop_detection_min_index_distance",
        "2",
        "--SequentialMatching.loop_detection_num_images",
        "6",
        "--SequentialMatching.loop_detection_max_num_features",
        "1500",
        "--SequentialMatching.num_threads",
        "1",
        "--SequentialMatching.vocab_tree_path",
        root / LOCK["offlineVocabulary"]["path"],
    )
    require(
        "Generating image pairs with vocabulary tree" in loop_text,
        "Vocabulary retrieval did not run",
    )
    loop_pairs = check_loop_matches(loop_database)
    cli(
        "exhaustive_matcher",
        "--database_path",
        database,
        "--FeatureMatching.use_gpu",
        "0",
        "--FeatureMatching.num_threads",
        "1",
    )
    with closing(sqlite3.connect(database)) as db:
        require(
            db.execute(
                "SELECT COUNT(*) FROM two_view_geometries WHERE rows>0"
            ).fetchone()[0]
            > 0,
            "CPU matching produced no verified geometries",
        )
    sparse = work / "sparse"
    sparse.mkdir(exist_ok=True)
    cli(
        "mapper",
        "--database_path",
        database,
        "--image_path",
        images_dir,
        "--output_path",
        sparse,
        "--Mapper.ba_use_gpu",
        "0",
        "--Mapper.ba_local_backend",
        "CERES",
        "--Mapper.ba_global_backend",
        "CERES",
        "--Mapper.num_threads",
        "1",
        "--Mapper.min_num_matches",
        "10",
        "--Mapper.init_min_num_inliers",
        "15",
    )
    models = [p for p in sparse.iterdir() if p.is_dir()]
    require(models, "CPU sparse reconstruction produced no model")
    cli("model_analyzer", "--path", models[0])
    adjusted = work / "ceres-ba"
    adjusted.mkdir(exist_ok=True)
    cli(
        "bundle_adjuster",
        "--input_path",
        root / "lib/validation/model",
        "--output_path",
        adjusted,
        "--BundleAdjustment.backend",
        "CERES",
        "--BundleAdjustmentCeres.use_gpu",
        "0",
    )
    text = cli("model_analyzer", "--path", adjusted)
    require(
        "Points" in text or "points" in text,
        "Ceres BA output is not a valid model",
    )
    require(loaded, "No actual runtime library mappings were observed")
    report = {
        "passed": True,
        "checks": [
            "source-identity",
            "runtime-closure",
            "licenses",
            "jpeg",
            "rgba-png",
            "alpha-mask",
            "unicode-space-path",
            "cpu-sift",
            "cpu-matching",
            "cpu-sparse-reconstruction",
            "ceres-cpu-ba",
            "offline-vocabulary",
            "cpu-loop-detection",
        ],
        "environment": {
            "developmentSearchPathsRemoved": True,
            "cudaVisibleDevices": "-1",
            "testedSystem": __import__("platform").platform(),
        },
        "loadedFiles": sorted(loaded),
        "commands": calls,
        "gpuValidation": info["gpuValidation"],
        "offlineVocabulary": {
            "sha256": LOCK["offlineVocabulary"]["sha256"],
            "path": LOCK["offlineVocabulary"]["path"],
            "verifiedLoopPairs": loop_pairs,
        },
    }
    write_json(work / "acceptance.json", report)
    return report


@contextmanager
def isolate(build):
    candidates = [build.root / name for name in ("i", "cuda", "install")]
    renamed = []
    try:
        for source in candidates:
            if not source.exists():
                continue
            require(
                source.resolve().is_relative_to(build.root),
                "Unsafe isolation target",
            )
            target = source.with_name(source.name + ".colmap-hidden")
            require(
                not target.exists(),
                f"Isolation destination already exists: {target}",
            )
            source.rename(target)
            renamed.append((source, target))
        yield
    finally:
        for source, target in reversed(renamed):
            require(
                not source.exists(),
                f"Cannot restore development directory: {source}",
            )
            target.rename(source)


def macos_sandbox_profile(build):
    require(
        os.environ.get("GITHUB_ACTIONS") == "true",
        "Homebrew isolation is restricted to GitHub Actions",
    )
    prefix = Path(build.state["brewPrefix"])
    require(
        prefix.as_posix() == "/opt/homebrew",
        "Unexpected Homebrew prefix",
    )
    require(shutil.which("sandbox-exec"), "sandbox-exec is unavailable")
    return (
        "(version 1)\n(allow default)\n"
        '(deny file-read* (subpath "/opt/homebrew"))\n'
    )


def verify(build, root):
    require(
        build.state.get("collected"), "Collect dependencies before verification"
    )
    index = "archive" if Path(root).name == "extracted" else "stage"
    work = build.root / ("verify-" + index)
    require(not work.exists(), "Verification requires a fresh work directory")
    work.mkdir()
    if build.platform == "linux":
        image = "colmap-runtime-validation:22.04"
        build.execute(
            [
                "docker",
                "build",
                "-t",
                image,
                "-f",
                Path(__file__).parent / "validation.Dockerfile",
                Path(__file__).parent,
            ],
            "validation-environment",
        )
        build.execute(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--security-opt=no-new-privileges",
                "-v",
                str(Path(__file__).parent.resolve()) + ":/verify:ro",
                "-v",
                str(Path(root).resolve()) + ":/package:ro",
                "-v",
                str(work) + ":/work",
                image,
                "python3",
                "/verify/smoke.py",
                "--package",
                "/package",
                "--work",
                "/work",
                "--platform",
                "linux",
            ],
            "acceptance-" + index,
        )
        report = read_json(work / "中文 路径 CPU 验收/acceptance.json")
    else:
        with isolate(build):
            if build.platform == "macos":
                build.execute(
                    [
                        "sandbox-exec",
                        "-p",
                        macos_sandbox_profile(build),
                        sys.executable,
                        Path(__file__).resolve(),
                        "--package",
                        Path(root).resolve(),
                        "--work",
                        work,
                        "--platform",
                        "macos",
                    ],
                    "acceptance-" + index,
                )
                report = read_json(
                    work / "涓枃 璺緞 CPU 楠屾敹/acceptance.json"
                )
            else:
                report = local_verify(root, work, build.platform)
    shutil.copytree(
        work, build.logs / ("acceptance-" + index), dirs_exist_ok=True
    )
    if index == "stage":
        info = read_json(Path(root) / "BUILD-INFO.json")
        info["cpuValidation"] = report
        write_json(Path(root) / "BUILD-INFO.json", info)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument(
        "--platform", choices=("windows", "linux", "macos"), required=True
    )
    args = parser.parse_args()
    local_verify(args.package, args.work, args.platform)

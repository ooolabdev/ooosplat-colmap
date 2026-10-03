# SPDX-License-Identifier: BSD-3-Clause
"""Validate an existing build's archives; publication requires --publish."""

import argparse
import hashlib
import json
import os
import re
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from common import (
    LOCK,
    PLATFORMS,
    read_json,
    require,
    run,
    safe_member,
    sha256,
    write_json,
)

CHECKS = {
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
}


def api(repo, path, *, payload=None, missing=False):
    token = os.environ.get("GH_TOKEN")
    require(
        token,
        "The maintainer workflow needs GH_TOKEN; public Release downloads do not",
    )
    request = urllib.request.Request(
        "https://api.github.com/repos/" + repo + "/" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and missing:
            return None
        raise RuntimeError(
            f"GitHub API rejected {path}: HTTP {error.code}"
        ) from error


def check_archive_checksums(container, files):
    require("SHA256SUMS" in files, "Archive lacks internal SHA256SUMS")
    require(len(files) == len(set(files)), "Duplicate archive entries")
    lines = []
    hashes = {}
    lengths = {}
    opener = (
        container.open
        if isinstance(container, zipfile.ZipFile)
        else container.extractfile
    )
    for name in sorted(files):
        if Path(name).name == "SHA256SUMS":
            continue
        checksum = hashlib.sha256()
        length = 0
        with opener(name) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(block)
                length += len(block)
        lines.append(f"{checksum.hexdigest()}  {name}")
        hashes[name] = checksum.hexdigest()
        lengths[name] = length
    with opener("SHA256SUMS") as stream:
        expected = stream.read().decode("utf-8").splitlines()
    require(expected == lines, "Archive internal file set / checksum mismatch")
    vocabulary = LOCK["offlineVocabulary"]
    require(
        hashes.get(vocabulary["path"]) == vocabulary["sha256"]
        and lengths.get(vocabulary["path"]) == vocabulary["bytes"],
        "Release archive lacks the locked offline vocabulary tree",
    )


def archive_metadata(archive):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as container:
            names = container.namelist()
            for entry in container.infolist():
                safe_member(entry.filename)
                require(
                    (entry.external_attr >> 16) & 0o170000 != 0o120000,
                    "ZIP contains a symlink",
                )
            check_archive_checksums(
                container,
                [i.filename for i in container.infolist() if not i.is_dir()],
            )
            require(
                names.count("BUILD-INFO.json") == 1
                and "bin/colmap.exe" in names,
                "Wrong ZIP layout",
            )
            data = container.read("BUILD-INFO.json")
    else:
        with tarfile.open(archive) as container:
            members = container.getmembers()
            for member in members:
                safe_member(member.name)
                if member.issym() or member.islnk():
                    safe_member(member.linkname)
            check_archive_checksums(
                container, [m.name for m in members if not m.isdir()]
            )
            require(
                sum(m.name == "BUILD-INFO.json" for m in members) == 1
                and any(m.name == "bin/colmap" for m in members),
                "Wrong tar layout",
            )
            data = container.extractfile("BUILD-INFO.json").read()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def validate_manifests(
    directory, repo, run_id, attempt, script_commit, revision
):
    require(
        revision == LOCK["runtimeRevision"],
        "Revision must match the built lock file",
    )
    manifests = list(Path(directory).rglob("*.manifest.json"))
    require(
        len(manifests) == 3,
        "Release requires exactly three verified platform manifests",
    )
    seen = set()
    assets = []
    validated = []
    for file in manifests:
        data = read_json(file)
        platform = data.get("platform")
        require(
            platform in PLATFORMS and platform not in seen,
            "Duplicate/missing platform",
        )
        seen.add(platform)
        require(
            data.get("verified") is True
            and data.get("validation", {}).get("passed") is True,
            "Archive has not passed final extraction acceptance",
        )
        require(
            set(data["validation"].get("checks", [])) >= CHECKS,
            "Incomplete runtime acceptance",
        )
        for key, value in {
            "repository": repo,
            "runId": str(run_id),
            "runAttempt": str(attempt),
            "scriptCommit": script_commit,
            "sourceCommit": LOCK["upstreamCommit"],
            "runtimeRevision": revision,
            "version": LOCK["colmapVersion"],
            "architecture": PLATFORMS[platform],
        }.items():
            require(data.get(key) == value, f"Mixed release identity: {key}")
        require(
            Path(data["archive"]).name == data["archive"],
            "Unsafe archive basename",
        )
        archive = file.parent / data["archive"]
        require(
            archive.name.endswith(
                ".zip" if platform == "windows" else ".tar.xz"
            ),
            "Wrong platform archive format",
        )
        require(
            archive.is_file() and sha256(archive) == data["sha256"],
            "Archive hash mismatch",
        )
        require(
            archive.stat().st_size == data["compressedBytes"],
            "Archive size mismatch",
        )
        info, info_hash = archive_metadata(archive)
        require(
            info_hash == data["buildInfoSha256"], "BUILD-INFO identity mismatch"
        )
        for key in (
            "scriptCommit",
            "sourceCommit",
            "platform",
            "architecture",
            "runtimeRevision",
            "runId",
            "runAttempt",
            "repository",
        ):
            require(info[key] == data[key], f"Archive metadata mismatch: {key}")
        require(
            info.get("cpuValidation", {}).get("passed") is True,
            "Package lacks CPU validation",
        )
        require(
            set(info["cpuValidation"].get("checks", [])) >= CHECKS,
            "Package acceptance is incomplete",
        )
        for key in LOCK["disabledFeatures"]:
            require(
                info["features"].get(key) is False,
                "Archive features differ from the runtime policy",
            )
        require(
            info["features"]["CASPAR_ENABLED"] is (platform != "macos")
            and info["features"]["CASPAR_USE_DOUBLE"] is False,
            "Wrong Caspar configuration",
        )
        require(
            info["gpuArchitectures"]
            == (LOCK["architectures"] if platform != "macos" else []),
            "Wrong GPU architectures",
        )
        require(
            info.get("offlineVocabulary") == LOCK["offlineVocabulary"]
            and info["features"].get("offlineLoopDetection") is True,
            "Release lacks verified offline loop capability",
        )
        checksum = archive.with_name(archive.name + ".sha256")
        require(
            checksum.read_text().strip() == f"{data['sha256']}  {archive.name}",
            "External checksum mismatch",
        )
        assets += [archive, file, checksum]
        validated.append(data)
    require(seen == set(PLATFORMS), "Release requires all platforms")
    return assets, validated


def assert_new_release(repo, tag):
    require(
        api(repo, "git/ref/tags/" + tag, missing=True) is None,
        "Tag already exists; refusing to overwrite",
    )
    require(
        api(repo, "releases/tags/" + tag, missing=True) is None,
        "Release already exists; refusing to overwrite",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--revision", type=int, default=LOCK["runtimeRevision"])
    parser.add_argument("--directory", default=".runtime/release")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Explicitly create a new tag and Release",
    )
    args = parser.parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY", "ooolabdev/ooosplat-colmap")
    require(
        re.fullmatch(r"ooolabdev/[A-Za-z0-9_.-]+", repo),
        "Unexpected target repository",
    )
    require(args.run_id > 0 and args.attempt > 0, "Invalid build run/attempt")
    build = api(repo, f"actions/runs/{args.run_id}/attempts/{args.attempt}")
    require(
        build["event"] == "workflow_dispatch"
        and build["conclusion"] == "success",
        "Source build must be a successful manually dispatched run",
    )
    workflow = api(repo, "actions/workflows/" + str(build["workflow_id"]))
    require(
        workflow["path"] == ".github/workflows/runtime-build.yml",
        "Unexpected source build workflow",
    )
    require(
        build["repository"]["full_name"] == repo
        and build["head_repository"]["full_name"] == repo,
        "Source run belongs to another repository",
    )
    directory = Path(args.directory).resolve()
    require(
        not directory.exists(), "Release validation directory must be fresh"
    )
    directory.mkdir(parents=True)
    run(
        [
            "gh",
            "run",
            "download",
            str(args.run_id),
            "--repo",
            repo,
            "--dir",
            directory,
            "--pattern",
            f"colmap-{LOCK['colmapVersion']}-runtime.{args.revision}-*-run{args.run_id}-attempt{args.attempt}",
        ]
    )
    assets, data = validate_manifests(
        directory,
        repo,
        args.run_id,
        args.attempt,
        build["head_sha"],
        args.revision,
    )
    tag = f"colmap-{LOCK['colmapVersion']}-runtime.{args.revision}"
    assert_new_release(repo, tag)
    index = directory / "RUNTIME-INDEX.json"
    write_json(
        index,
        {"schemaVersion": 1, "repository": repo, "tag": tag, "platforms": data},
    )
    notes = directory / "release-notes.md"
    body = [
        f"COLMAP {LOCK['colmapVersion']}, runtime revision {args.revision}.",
        f"Source: `{LOCK['upstreamCommit']}`. Build scripts: `{build['head_sha']}`.",
        "",
        "Download one archive below, verify its adjacent SHA-256 file, then extract it.",
        "No GitHub token, CUDA Toolkit, vcpkg or local compilation is required.",
        f"Offline SIFT vocabulary is bundled at `{LOCK['offlineVocabulary']['path']}` (SHA-256 `{LOCK['offlineVocabulary']['sha256']}`).",
        "COLMAP CPU fallback does not imply other projects can train without a GPU.",
        "",
        "CUDA SIFT / actual Caspar GPU BA: 未验证. CPU acceptance is not GPU BA acceptance.",
        "Windows 10 22H2/11 are compatibility targets, not tested desktop environments.",
        "",
        "| Platform | Download | SHA-256 |",
        "|---|---|---|",
    ]
    for entry in sorted(data, key=lambda x: x["platform"]):
        url = f"https://github.com/{repo}/releases/download/{tag}/{entry['archive']}"
        body.append(
            f"| {entry['platform']} {entry['architecture']} | [archive]({url}) | `{entry['sha256']}` |"
        )
    body += [
        "",
        f"Usage, licenses, compatibility and source reproduction: https://github.com/{repo}/blob/{build['head_sha']}/doc/runtime-distribution.md",
    ]
    notes.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(
        f"Validated all three original archives for {tag}; publish={args.publish}"
    )
    if not args.publish:
        return
    # Atomically reserve the new tag. A race/existing tag is an API error, never
    # permission to adopt another publisher's tag or overwrite an asset.
    assert_new_release(repo, tag)
    api(
        repo,
        "git/refs",
        payload={"ref": "refs/tags/" + tag, "sha": build["head_sha"]},
    )
    run(
        [
            "gh",
            "release",
            "create",
            tag,
            *assets,
            index,
            "--repo",
            repo,
            "--verify-tag",
            "--draft",
            "--title",
            tag,
            "--notes-file",
            notes,
        ]
    )
    run(["gh", "release", "edit", tag, "--repo", repo, "--draft=false"])


if __name__ == "__main__":
    main()

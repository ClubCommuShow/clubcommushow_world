#!/usr/bin/env python3
"""Prepare one reviewed ClubCommuShow import; this script never commits or pushes."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import warnings
import zipfile

SOURCE_REPOSITORY = "ClubCommuShow/ClubCommuShow"
SOURCE_HEAD = "8d17f7e13c9312408fe4af231a08e873ad38eceb"
DESTINATION_REPOSITORY = "ClubCommuShow/clubcommushow_world"
PROJECT_ROOT = "docs/EBK/projects/prj_275fcf6a380e53889135988a06e61846"
VENUE_ROOT = "docs/EBK/venues/ebk_d96094eb919f487d9b1f6c0401881c5f"
IMPORT_ROOT = ".ebk/projects/prj_275fcf6a380e53889135988a06e61846/imports/clubcommushow_8d17f7e13c93"
INPUT_NAMES = {
    IMPORT_ROOT + "/import_report.json",
    *(IMPORT_ROOT + "/source/" + name for name in (
        "data/clubcard_manifest.json", "data/content_manifest.json",
        "data/room_media_slides.json", "data/roster_frame_map.txt",
        "data/roster_manifest.json", "data/roster_resolved_urls.txt",
        "ebk-room-media/manifest.json")),
    *(root + "/data/" + name for root in (PROJECT_ROOT, VENUE_ROOT)
      for name in ("clubcard_manifest.json", "content_manifest.json", "roster_manifest.json")),
}
PURPOSE_PREFIXES = {
    "clubcard_image": "images/clubcards/", "poster_image": "images/posters/",
    "member_portrait": "images/portraits/", "room_slide_source": "media/room-slides/",
}


class TransferRejected(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise TransferRejected(message)


def valid_path(value):
    require(isinstance(value, str) and value and "\\" not in value
            and not any(ord(char) < 32 for char in value), "Invalid repository path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix() == value
            and all(part not in ("", ".", "..", ".git") for part in path.parts),
            "Noncanonical or unsafe repository path: " + value)
    return value


def safe_path(root, name):
    path = root
    parts = PurePosixPath(valid_path(name)).parts
    for index, part in enumerate(parts):
        path = path / part
        require(not path.is_symlink(), "Symlink in selected path: " + name)
        if index < len(parts) - 1 and path.exists():
            require(path.is_dir(), "Non-directory parent: " + name)
    return path


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, timeout=60).decode("utf-8").strip()


def remote_head(root):
    fields = git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()
    require(len(fields) == 2 and fields[1] == "refs/heads/main"
            and re.fullmatch(r"[0-9a-f]{40}", fields[0]), "Unexpected remote main response")
    return fields[0]


def require_fresh_head(root, expected):
    require(git(root, "rev-parse", "HEAD") == expected, "Local checkout HEAD changed")
    require(remote_head(root) == expected, "Remote main changed; refusing stale import")


def require_context(root):
    require(os.environ.get("GITHUB_REPOSITORY") == DESTINATION_REPOSITORY,
            "Import is restricted to the selected destination repository")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Import is restricted to main")
    head = os.environ.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", head), "Missing exact workflow commit")
    origin = git(root, "remote", "get-url", "origin").removesuffix(".git").rstrip("/")
    require(origin == "https://github.com/" + DESTINATION_REPOSITORY, "Unexpected origin repository")
    require_fresh_head(root, head)
    return head


def file_hashes(path):
    require(path.is_file() and not path.is_symlink(), "Expected a regular file: " + str(path))
    size = path.stat().st_size
    blob = hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\0")
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            blob.update(chunk)
            sha256.update(chunk)
    return {"bytes": size, "gitBlobSha": blob.hexdigest(), "sha256": sha256.hexdigest()}


def tree_entries(root):
    raw = subprocess.check_output(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=root, timeout=60)
    result = {}
    for entry in raw.split(b"\0"):
        if entry:
            header, name = entry.split(b"\t", 1)
            mode, kind, sha = header.decode("ascii").split()
            result[name.decode("utf-8")] = (mode, kind, sha)
    return result


def current_blob(root, path, tree):
    file = safe_path(root, path)
    entry = tree.get(path)
    if entry is not None:
        require(entry[:2] == ("100644", "blob"), "Selected path is not a regular Git file: " + path)
        require(file_hashes(file)["gitBlobSha"] == entry[2], "Working file differs from HEAD: " + path)
        return entry[2]
    require(not file.exists(), "Untracked file would be overwritten: " + path)
    return None


def validate_request(request):
    for key, value in (("sourceRepository", SOURCE_REPOSITORY), ("sourceHead", SOURCE_HEAD),
                       ("destinationRepository", DESTINATION_REPOSITORY),
                       ("projectRoot", PROJECT_ROOT), ("venueRoot", VENUE_ROOT),
                       ("importRoot", IMPORT_ROOT), ("preparedZip", "prepared_inputs.zip")):
        require(request.get(key) == value, "Unexpected request field: " + key)
    files = request.get("files", [])
    require(len(files) == 14 and {row["path"] for row in files} == INPUT_NAMES,
            "Prepared input list differs from the 14 reviewed paths")
    assets = request.get("assets", [])
    require(len(assets) == 170 and request.get("expectedCounts", {}).get("assetPaths") == 170,
            "Expected all 170 asset paths")
    source_paths, target_paths, unique = set(), set(), {}
    for row in assets:
        source = valid_path(row["sourcePath"])
        require(source not in source_paths, "Duplicate source mapping: " + source)
        source_paths.add(source)
        prefix = PURPOSE_PREFIXES.get(row.get("purpose"))
        require(prefix is not None and source.startswith(prefix), "Asset purpose/path mismatch: " + source)
        require(source.endswith(".png") or (row["purpose"] == "room_slide_source"
                and source.endswith("/.gitkeep") and row["sourceBytes"] == 0), "Unexpected asset type: " + source)
        require(row.get("sourceRef") == SOURCE_HEAD, "Unpinned asset reference")
        require(re.fullmatch(r"[0-9a-f]{40}", row["sourceBlobSha"]), "Invalid source Git SHA")
        require(type(row["sourceBytes"]) is int and row["sourceBytes"] >= 0, "Invalid source byte count")
        for key, root in (("destinationProjectPath", PROJECT_ROOT), ("destinationVenuePath", VENUE_ROOT)):
            path = valid_path(row[key])
            require(path == root + "/" + source and path not in target_paths, "Invalid or duplicate asset destination")
            target_paths.add(path)
        old = unique.setdefault(row["sourceBlobSha"], row["sourceBytes"])
        require(old == row["sourceBytes"], "Inconsistent duplicate blob size")
    require(len(unique) == 168, "Expected the 168 reviewed unique blobs")
    for row in files:
        valid_path(row["path"])
        require(type(row["bytes"]) is int and row["bytes"] >= 0, "Invalid input size")
        require(re.fullmatch(r"[0-9a-f]{40}", row["gitBlobSha"])
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "Invalid input hash")
        require(row["replacesTargetBlob"] is None or re.fullmatch(r"[0-9a-f]{40}", row["replacesTargetBlob"]),
                "Invalid target baseline SHA")
    require(re.fullmatch(r"[0-9a-f]{64}", request["preparedZipSha256"]), "Invalid ZIP hash")
    preserved = request.get("preservedBlobs")
    require(isinstance(preserved, dict) and preserved, "Missing preservation baseline")
    for path, sha in preserved.items():
        valid_path(path)
        require(path not in target_paths and path not in INPUT_NAMES
                and re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid or overlapping preserved path")
    return assets, files


def validate_target_state(root, request, tree):
    for row in request["files"]:
        require(current_blob(root, row["path"], tree) == row["replacesTargetBlob"],
                "Reviewed target input changed: " + row["path"])
    for row in request["assets"]:
        for key in ("destinationProjectPath", "destinationVenuePath"):
            actual = current_blob(root, row[key], tree)
            require(actual is None or actual == row["sourceBlobSha"], "Asset collision: " + row[key])
    for path, expected in request["preservedBlobs"].items():
        require(current_blob(root, path, tree) == expected, "Preserved target data changed: " + path)


def read_prepared_inputs(root, request):
    archive = safe_path(root, IMPORT_ROOT + "/" + request["preparedZip"])
    require(file_hashes(archive)["sha256"] == request["preparedZipSha256"], "Prepared ZIP SHA256 mismatch")
    expected = {row["path"]: row for row in request["files"]}
    result = {}
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        require(len(names) == len(set(names)) and set(names) == set(expected), "ZIP entry list mismatch")
        for entry in bundle.infolist():
            valid_path(entry.filename)
            mode = (entry.external_attr >> 16) & 0xFFFF
            require(not entry.is_dir() and not entry.flag_bits & 1
                    and stat.S_IFMT(mode) in (0, stat.S_IFREG), "Unsafe ZIP entry")
            require(entry.file_size == expected[entry.filename]["bytes"], "ZIP member size mismatch")
        require(bundle.testzip() is None, "ZIP CRC verification failed")
        for name, row in expected.items():
            data = bundle.read(name)
            require(len(data) == row["bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"]
                    and hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == row["gitBlobSha"],
                    "Prepared input hash mismatch: " + name)
            result[name] = data
    return result


def inspect_image(path, clubcard):
    from PIL import Image
    if clubcard:
        require(path.stat().st_size <= 50 * 1024 * 1024, "ClubCard exceeds 50 MiB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            image_format = image.format
            if clubcard:
                require(image_format == "PNG", "Expected actual PNG bytes for ClubCard")
            else:
                require(image_format in ("PNG", "JPEG"),
                        "Unsupported source image format: " + str(image_format))
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
            if clubcard:
                require(max(width, height) <= 2048 and frames == 1,
                        "ClubCard must be at most 2048px and exactly one frame")
            for index in range(frames):
                image.seek(index)
                image.load()
    return {"format": image_format, "width": width, "height": height, "frames": frames,
            "fullDecode": True, "clubcardConstraintsChecked": clubcard}


def download_blob(rows, cache, opener=urlopen):
    row = rows[0]
    target = cache / (row["sourceBlobSha"] + ".blob")
    url = "https://raw.githubusercontent.com/" + SOURCE_REPOSITORY + "/" + SOURCE_HEAD + "/" + quote(row["sourcePath"], safe="/")
    if row["sourceBytes"] == 0:
        target.write_bytes(b"")
    else:
        for attempt in range(3):
            try:
                request = Request(url, headers={"User-Agent": "EBK-Reviewed-Asset-Import", "Accept-Encoding": "identity"})
                with opener(request, timeout=45) as response, target.open("wb") as stream:
                    require(urlsplit(response.geturl()).netloc == "raw.githubusercontent.com", "Unexpected asset redirect host")
                    received = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        require(received <= row["sourceBytes"], "Source download exceeds reviewed size")
                        stream.write(chunk)
                break
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                target.unlink(missing_ok=True)
                if attempt == 2 or (isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504)):
                    raise
                time.sleep(2 ** attempt)
    hashes = file_hashes(target)
    require(hashes["bytes"] == row["sourceBytes"] and hashes["gitBlobSha"] == row["sourceBlobSha"],
            "Source blob bytes/Git SHA mismatch: " + row["sourcePath"])
    image = inspect_image(target, any(item["purpose"] == "clubcard_image" for item in rows)) if row["sourceBytes"] else None
    return target, {**hashes, "image": image}


def atomic_write(path, source=None, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ebk-import-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if source is not None:
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, stream, 1024 * 1024)
            else:
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def set_import_output(value):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("import=" + ("true" if value else "false") + "\n")


def run(request_path):
    root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    require(Path.cwd().resolve() == root, "Run the importer from the repository root")
    head = require_context(root)
    result_path = safe_path(root, IMPORT_ROOT + "/IMPORT_RESULT.json")
    if result_path.exists():
        require(result_path.is_file(), "Import result is not a regular file")
        set_import_output(False)
        print("Import result already exists; preserving all subsequently edited data.", flush=True)
        return
    require(not git(root, "status", "--porcelain=v1", "--untracked-files=all"), "Checkout is not clean")
    expected_request = safe_path(root, IMPORT_ROOT + "/transfer_request.json")
    require(Path(request_path).resolve() == expected_request.resolve(), "Unexpected request location")
    request = json.loads(expected_request.read_text(encoding="utf-8"))
    assets, files = validate_request(request)
    require(re.fullmatch(r"[0-9a-f]{40}", request["reviewedDestinationHead"]), "Invalid reviewed baseline")
    subprocess.run(["git", "merge-base", "--is-ancestor", request["reviewedDestinationHead"], head],
                   cwd=root, check=True, timeout=60)
    tree = tree_entries(root)
    validate_target_state(root, request, tree)
    prepared = read_prepared_inputs(root, request)
    groups = {}
    for row in assets:
        groups.setdefault(row["sourceBlobSha"], []).append(row)
    with tempfile.TemporaryDirectory(prefix="ebk-club-verified-", dir=os.environ["RUNNER_TEMP"]) as temporary:
        cache = Path(temporary)
        verified, errors = {}, []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(download_blob, rows, cache): sha for sha, rows in groups.items()}
            for future in as_completed(futures):
                sha = futures[future]
                try:
                    verified[sha] = future.result()
                except Exception as error:
                    errors.append({"sourceBlobSha": sha, "error": type(error).__name__, "message": str(error)[:500]})
                completed = len(verified) + len(errors)
                if completed % 20 == 0 or completed == len(groups):
                    print(json.dumps({"checkedUniqueBlobs": completed, "verified": len(verified), "failed": len(errors)}), flush=True)
        require(not errors, "Source verification failed before any destination write: " + json.dumps(errors, ensure_ascii=False))
        require_fresh_head(root, head)
        validate_target_state(root, request, tree)
        asset_records = []
        for row in assets:
            source, hashes = verified[row["sourceBlobSha"]]
            destinations = [row["destinationProjectPath"], row["destinationVenuePath"]]
            for path in destinations:
                destination = safe_path(root, path)
                atomic_write(destination, source=source)
                require(file_hashes(destination) == {key: hashes[key] for key in ("bytes", "gitBlobSha", "sha256")},
                        "Written asset readback mismatch: " + path)
            asset_records.append({"sourcePath": row["sourcePath"], "sourceBlobSha": row["sourceBlobSha"],
                                  "purpose": row["purpose"], "destinationPaths": destinations, **hashes})
        for row in files:
            destination = safe_path(root, row["path"])
            atomic_write(destination, data=prepared[row["path"]])
            require(file_hashes(destination) == {key: row[key] for key in ("bytes", "gitBlobSha", "sha256")},
                    "Written input readback mismatch: " + row["path"])
        for path, expected in request["preservedBlobs"].items():
            require(file_hashes(safe_path(root, path))["gitBlobSha"] == expected, "Preserved file changed: " + path)
        require_fresh_head(root, head)
        report = {"schemaVersion": 1, "status": "VERIFIED_AND_WRITTEN", "sourceRepository": SOURCE_REPOSITORY,
                  "sourceHead": SOURCE_HEAD, "destinationRepository": DESTINATION_REPOSITORY,
                  "projectRoot": PROJECT_ROOT, "venueRoot": VENUE_ROOT, "importRoot": IMPORT_ROOT,
                  "workflowHead": head, "requestSha256": file_hashes(expected_request)["sha256"],
                  "preparedZipSha256": request["preparedZipSha256"], "assets": asset_records,
                  "files": files, "preservedBlobs": request["preservedBlobs"],
                  "counts": {"assetPaths": len(assets), "uniqueAssetBlobs": len(groups),
                             "assetDestinationPaths": len(assets) * 2, "preparedInputFiles": len(files),
                             "uniqueSourceBytes": sum(value[1]["bytes"] for value in verified.values()),
                             "clubcardImages": sum(row["purpose"] == "clubcard_image" for row in assets)},
                  "verifiedAt": datetime.now(timezone.utc).isoformat(), "imagesModified": False,
                  "committedOrPushed": False}
        atomic_write(safe_path(root, IMPORT_ROOT + "/transfer_prepared.json"),
                     data=(json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    set_import_output(True)
    print(json.dumps({"status": report["status"], "counts": report["counts"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    run(parser.parse_args().request)

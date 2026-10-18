#!/usr/bin/env python3
"""Verify the actual generated EBK media and project only the selected venue."""
from pathlib import Path
from urllib.parse import unquote, urlsplit
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path, git=False):
    path = Path(path)
    value = hashlib.sha1() if git else hashlib.sha256()
    if git:
        value.update(b"blob " + str(path.stat().st_size).encode("ascii") + b"\0")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def checked_path(root, relative):
    require(isinstance(relative, str) and relative and "\\" not in relative, "Invalid relative path")
    parts = Path(relative).parts
    require(not Path(relative).is_absolute() and ".." not in parts, "Path escapes repository")
    path = root / relative
    require(path.resolve().is_relative_to(root.resolve()), "Resolved path escapes repository")
    for parent in [path, *path.parents]:
        if parent == root:
            break
        require(not parent.is_symlink(), "Symlink in import path: " + relative)
    return path


def check_preserved(root, request):
    for relative, expected in request["preservedBlobs"].items():
        path = checked_path(root, relative)
        require(path.is_file() and digest(path, git=True) == expected,
                "Existing target setting changed: " + relative)


def generated_url_path(root, request, url):
    base = request["publicProjectBase"].rstrip("/")
    require(isinstance(url, str) and url.startswith(base + "/"), "Generated URL uses another project")
    parsed = urlsplit(url)
    require(not parsed.query and not parsed.fragment, "Unexpected generated URL suffix")
    relative = unquote(parsed.path[len(urlsplit(base).path) + 1:])
    path = checked_path(root, request["projectRoot"] + "/" + relative)
    require(path.is_file(), "Generated URL target missing: " + relative)
    return path


def inspect_video(path, expected_frames, width, height, generation=None):
    result = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames",
        "-of", "json", str(path)
    ], timeout=120))
    streams = result.get("streams", [])
    require(len(streams) == 1, "Generated video stream missing: " + str(path))
    stream = streams[0]
    require(stream.get("width") == width and stream.get("height") == height,
            "Generated video dimensions differ: " + str(path))
    require(stream.get("r_frame_rate") == "30/1", "Generated video must use 30 fps")
    require(int(stream.get("nb_read_frames", -1)) == expected_frames,
            "Generated video frame count differs: " + str(path))
    marker = "NOT_APPLICABLE"
    if generation is not None:
        require(len(generation) == 32 and all(c in "0123456789abcdef" for c in generation),
                "Invalid generation identifier")
        pixels = subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(path), "-vf", r"select=eq(n\,1)",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"
        ], timeout=120)
        require(len(pixels) == width * height * 3, "Could not decode generation marker")
        bits = ""
        for index in range(160):
            x = ((index % 20) * 2 + 1) * width // 40
            y = ((index // 20) * 2 + 1) * height // 16
            position = (y * width + x) * 3
            bits += "1" if sum(pixels[position:position + 3]) > 384 else "0"
        decoded = "".join(format(int(bits[i:i + 4], 2), "x") for i in range(0, 160, 4))
        require(decoded == "eb4b26c1" + generation, "Video pixels contain another generation")
        marker = "PASS"
    return {"path": str(path), "sha256": digest(path), "frames": expected_frames,
            "width": width, "height": height, "fps": 30, "generationMarker": marker}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    request_path = args.request.resolve()
    request = read_json(request_path)
    require(os.environ.get("GITHUB_REPOSITORY") == request["destinationRepository"], "Wrong repository")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Wrong target branch")
    require(request_path == checked_path(root, request["importRoot"] + "/transfer_request.json"),
            "Unexpected request location")
    project = checked_path(root, request["projectRoot"])
    venue = checked_path(root, request["venueRoot"])
    import_root = checked_path(root, request["importRoot"])
    require(not (import_root / "IMPORT_RESULT.json").exists(), "This import was already finalized")
    prepared = read_json(import_root / "transfer_prepared.json")
    require(prepared["sourceHead"] == request["sourceHead"], "Transfer source differs")
    check_preserved(root, request)

    archive = import_root / request["preparedZip"]
    require(digest(archive) == request["preparedZipSha256"], "Prepared input archive changed")
    with zipfile.ZipFile(archive) as bundle:
        require(bundle.testzip() is None, "Prepared input archive CRC failed")
        authored = {
            name: json.loads(bundle.read(request["projectRoot"] + "/data/" + name))
            for name in ["roster_manifest.json", "clubcard_manifest.json", "content_manifest.json"]
        }
    for record in request["files"]:
        path = checked_path(root, record["path"])
        require(path.is_file() and digest(path) == record["sha256"], "Input changed during generation")
    for record in request["assets"]:
        for field in ["destinationProjectPath", "destinationVenuePath"]:
            path = checked_path(root, record[field])
            require(path.is_file() and path.stat().st_size == record["sourceBytes"]
                    and digest(path, git=True) == record["sourceBlobSha"], "Copied asset changed")

    common = read_json(project / "common/manifest.json")
    common_report = read_json(project / "common/build_report.json")
    counts = request["expectedCounts"]
    source_roster = authored["roster_manifest.json"]
    require(len(common["members"]) == counts["members"] == 43, "Wrong member count")
    require(common["members"] == source_roster["members"], "Generated member fields differ")
    for field in ["schemaVersion", "imageSchemaVersion", "roleTypes", "eventSchedule", "posterTextEnabled"]:
        require(common[field] == source_roster[field], "Roster setting changed: " + field)
    require(len(common["eventSchedule"]["rules"]) == counts["scheduleRules"] == 4, "Current calendar lost")
    require(common["posters"][:counts["authoredPosters"]] == source_roster["posters"], "Authored poster data changed")
    require(len(common["posters"]) == counts["generatedPosters"] == 63, "Wrong generated poster count")
    require(common_report["captured"] == counts["commonKeys"] == 104, "Wrong common capture count")
    require(common_report["fallbackKeys"] == [], "An image was replaced with a fallback")
    require(common_report["ignoredPortraitUrls"] == [], "A portrait URL was corrected during generation")
    video = common["videoRoster"]
    require(video["status"] == "ready" and video["format"] == "ebk-roster-v1", "Common media is not ready")
    require(video["generation"] == common_report["generation"], "Common generation differs")
    expected_keys = {row["key"]: row for row in request["expectedImageKeys"]}
    frame_map = generated_url_path(root, request, video["frameMapUrl"])
    lines = [line.split("|") for line in frame_map.read_text().splitlines() if line]
    require(len(lines) == counts["commonKeys"], "Wrong frame map row count")
    records = []
    found_keys = []
    for index, parts in enumerate(lines):
        require(len(parts) >= 5 and int(parts[0]) == (index + 1) * 3 + 1, "Common frame ordering differs")
        key = "|".join(parts[1:4])
        require(key in expected_keys and key not in found_keys, "Unknown or duplicate semantic image key")
        expected = expected_keys[key]
        path = generated_url_path(root, request, parts[4])
        require(unquote(parts[4]) == unquote(expected["projectImageUrl"]), "Image key points to another source")
        require(digest(path, git=True) == expected["sourceBlobSha"], "Semantic image bytes differ")
        found_keys.append(key)
        records.append({"key": key, "sha256": digest(path)})
    require(set(found_keys) == set(expected_keys), "A semantic image key was lost")
    require(common_report["sourceHashes"] == [row["sha256"] for row in records], "Captured source hash order differs")
    generation_payload = {"format": "ebk-roster-v1", "fps": 30, "framesPerEntry": 3,
                          "sampleFrame": 1, "generationMarker": "eb4b26c1-v1", "records": records}
    generation = hashlib.sha256(json.dumps(generation_payload, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()[:32]
    require(generation == video["generation"], "Common generation does not match image hashes")
    common_video = generated_url_path(root, request, video["videoUrl"])
    require(digest(common_video) == video["sha256"] == common_report["videoSha256"], "Common video checksum differs")
    video_checks = [inspect_video(common_video, (counts["commonKeys"] + 1) * 3, 1280, 720, generation)]
    video_checks.append(inspect_video(project / "common/roster.mp4", counts["commonKeys"] * 3, 1280, 720))

    cards = read_json(project / "ebk-clubcards/manifest.json")
    source_cards = authored["clubcard_manifest.json"]
    require(len(cards["cards"]) == counts["cards"] == 63, "Wrong card count")
    by_number = {row["cardNumber"]: row for row in cards["cards"]}
    require(len(by_number) == 63, "Duplicate card number")
    for original in source_cards["cards"]:
        actual = by_number.get(original["cardNumber"])
        require(actual is not None, "Original card lost")
        require(all(actual.get(k) == v for k, v in original.items()), "Original card setting changed")
        require(actual["imageSha256"] == digest(project / actual["imagePath"]), "Card image hash differs")
    for field in ["schemaVersion", "revision", "title", "rarityRules", "sheetLayout", "commonTextures"]:
        require(cards[field] == source_cards[field], "Card setting changed: " + field)
    card_video = cards["videoRoster"]
    require(card_video["status"] == "ready" and card_video["format"] == "ebk-clubcards-v2", "Cards are not ready")
    require(card_video["sheetCount"] == counts["cardSheets"] == 17, "Card sheet count differs")
    require(len({(row["sheetKey"], row["sheetIndex"]) for row in cards["cards"]}) == 63, "Card cells overlap")
    card_path = generated_url_path(root, request, card_video["videoUrl"])
    require(digest(card_path) == card_video["sha256"], "Card video checksum differs")
    card_frames = (card_video["sheetCount"] + len(card_video["commonTextures"]) + 1) * 3
    require(card_video["frameCount"] == card_frames, "Card frame metadata differs")
    video_checks.append(inspect_video(card_path, card_frames, 1280, 768, card_video["generation"]))
    card_map = generated_url_path(root, request, card_video["frameMapUrl"])
    card_map_lines = [line.split("|") for line in card_map.read_text().splitlines() if line]
    require(len(card_map_lines) == card_frames // 3, "Card frame map size differs")
    require({parts[3] for parts in card_map_lines if parts[1:3] == ["clubcard", "sheet"]}
            == {row["sheetKey"] for row in cards["cards"]}, "Card sheets differ from the frame map")

    rooms = read_json(project / "ebk-room-media/manifest.json")
    require(rooms["format"] == "ebk-room-media-v1", "Wrong room media format")
    albums = {row["name"]: row for row in rooms["albums"]}
    require(set(albums) == {"サンプル", "ポチョ"}, "Unexpected room album")
    require(albums["サンプル"]["imageCount"] == 0 and albums["ポチョ"]["imageCount"] == 1,
            "Source room images differ")
    for album in rooms["albums"]:
        if album["imageCount"] == 0:
            require(album["videoUrl"] == "" and album["images"] == [], "Empty album gained media")
            continue
        for row in album["images"]:
            image = project / "media/room-slides" / album["name"] / row["file"]
            require(digest(image, git=True) == row["sourceBlobSha"], "Room image source differs")
        album_path = generated_url_path(root, request, album["videoUrl"])
        require(digest(album_path) == album["sha256"], "Room video checksum differs")
        video_checks.append(inspect_video(album_path, album["imageCount"] * 3,
                                          album["encodedWidth"], album["encodedHeight"]))

    media_root = read_json(project / "ebk_media_root.json")
    modules = {row["id"]: row for row in media_root["modules"]}
    require(set(modules) == {"common", "clubcards", "roomMedia", "themes"}, "Missing media module")
    for name, generation in [("common", video["generation"]), ("clubcards", card_video["generation"]),
                             ("roomMedia", rooms["generation"])]:
        require(modules[name]["generation"] == generation, "Module generation differs: " + name)
    require(len(authored["content_manifest.json"]["contents"]) == counts["contents"] == 58, "Content count differs")
    require(read_json(project / "data/content_manifest.json") == authored["content_manifest.json"],
            "Reception/content data changed")
    active = read_json(venue / "data/active_project.json")
    require(active["deploymentId"] == request["venueId"] and active["projectRoot"].strip("/") == request["projectRoot"],
            "Selected venue is bound to another project")
    copied = []
    for source in sorted(project.rglob("*")):
        require(not source.is_symlink(), "Symlink in generated project")
        if not source.is_file():
            continue
        relative = source.relative_to(project).as_posix()
        if relative in {"venue.json", "data/active_project.json"}:
            continue
        target = checked_path(root, request["venueRoot"] + "/" + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(relative)
    shutil.copyfile(project / "common/manifest.json", venue / "data/roster_manifest.json")
    require(digest(project / "common/manifest.json") == digest(venue / "data/roster_manifest.json"),
            "Fixed venue roster does not match this generation")
    for relative in copied:
        source = project / relative
        target = venue / relative
        if relative != "data/roster_manifest.json":
            require(digest(source) == digest(target), "Venue projection differs: " + relative)
    check_preserved(root, request)
    changes = subprocess.check_output(["git", "diff", "--name-only", "-z"], timeout=30).decode().split("\0")
    allowed = [request["projectRoot"] + "/", request["venueRoot"] + "/", request["importRoot"] + "/"]
    require(all(not path or any(path.startswith(prefix) for prefix in allowed) for path in changes),
            "A tracked change is outside the selected project and venue")
    for row in video_checks:
        row["path"] = str(Path(row["path"]).relative_to(root))
    result = {
        "schemaVersion": 1, "status": "GENERATED_VERIFIED_PENDING_GIT_PUSH_AND_PAGES",
        "sourceRepository": request["sourceRepository"], "sourceHead": request["sourceHead"],
        "destinationRepository": request["destinationRepository"], "projectId": request["projectId"],
        "venueId": request["venueId"], "workflowHead": os.environ.get("GITHUB_SHA"),
        "workflowRunId": os.environ.get("GITHUB_RUN_ID"), "requestSha256": digest(request_path),
        "preparedZipSha256": digest(archive), "counts": counts, "preservedBlobs": len(request["preservedBlobs"]),
        "projectedFiles": len(copied), "commonGeneration": video["generation"],
        "cardGeneration": card_video["generation"], "roomGeneration": rooms["generation"],
        "fixedRosterSha256": digest(venue / "data/roster_manifest.json"),
        "fixedRosterUrl": request["fixedRosterUrl"], "videoChecks": video_checks,
        "checks": ["all original source asset blobs retained", "43 members and permission fields retained",
                   "104 semantic image keys match original frame map", "63 cards and 17 unique sheets",
                   "4 current calendar rules retained", "58 content entries retained",
                   "video checksums and frame counts", "common/card generation markers decoded from video",
                   "selected fixed venue projection equals project generation", "all unrelated target settings retained"],
        "pending": ["Git push readback", "GitHub Pages deployment", "live OPS/VRChat acceptance"]
    }
    (import_root / "IMPORT_RESULT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "members": 43, "cards": 63, "commonKeys": 104,
                      "scheduleRules": 4, "projectedFiles": len(copied), "videoChecks": len(video_checks),
                      "commonGeneration": video["generation"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Read a unique draft release asset by numeric ID; never mutate releases."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

REPOSITORY = "a-adamovich/dbt-dqm"
ASSET = "local-validation-0.2.4.json"
MAX_BYTES = 32 * 1024 * 1024


def api(path, *, binary=False):
    args = ["gh", "api", path]
    if binary:
        args += ["-H", "Accept: application/octet-stream"]
    else:
        args += ["--paginate", "--slurp"]
    raw = subprocess.run(args, capture_output=True, check=True).stdout
    return raw if binary else json.loads(raw)


def download(tag, expected_digest, output):
    if tag not in {"v0.2.4"} or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("Unsupported evidence tag or digest")
    pages = api(f"repos/{REPOSITORY}/releases?per_page=100")
    matches = [item for page in pages for item in page if item.get("tag_name") == tag]
    if len(matches) != 1 or matches[0].get("draft") is not True:
        raise ValueError("Expected exactly one matching draft release")
    release = matches[0]
    prefix = f"https://api.github.com/repos/{REPOSITORY}/releases/"
    if release.get("url") != prefix + str(release["id"]):
        raise ValueError("Unexpected draft repository identity")
    pages = api(f"repos/{REPOSITORY}/releases/{release['id']}/assets?per_page=100")
    assets = [item for page in pages for item in page if item.get("name") == ASSET]
    if len(assets) != 1:
        raise ValueError("Expected exactly one evidence asset")
    asset = assets[0]
    if (asset.get("state") != "uploaded" or not 0 < asset.get("size", 0) <= MAX_BYTES
            or asset.get("digest") != "sha256:" + expected_digest
            or asset.get("url") != prefix + "assets/" + str(asset["id"])):
        raise ValueError("Incorrect evidence asset identity, size or digest")
    raw = api(f"repos/{REPOSITORY}/releases/assets/{asset['id']}", binary=True)
    if len(raw) != asset["size"] or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("Downloaded evidence digest or size differs")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        download(args.tag, args.digest, args.output)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Evidence download blocked: {error}\n")


if __name__ == "__main__":
    main()

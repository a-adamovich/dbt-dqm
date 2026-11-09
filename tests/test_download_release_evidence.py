"""Synthetic recovery checks never authorize publication."""

import copy
import hashlib
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return None, importlib.import_module("download_release_evidence")


@pytest.mark.parametrize("change", [None, "missing", "duplicate", "published", "repo", "asset_missing",
    "asset_duplicate", "size", "digest", "body", "tag", "release_id", "asset_id"])
def test_draft_download_is_bound_to_numeric_identity(modules, monkeypatch, tmp_path, change):
    _, module = modules
    data = b'{"synthetic":true}'
    digest = hashlib.sha256(data).hexdigest()
    prefix = f"https://api.github.com/repos/{module.REPOSITORY}/releases/"
    release = {"tag_name": "v0.2.4", "draft": True, "id": 7, "url": prefix + "7"}
    asset = {"id": 8, "name": module.ASSET, "url": prefix + "assets/8", "state": "uploaded",
             "size": len(data), "digest": "sha256:" + digest}
    releases, assets = [release], [asset]
    if change == "missing": releases.clear()
    elif change == "duplicate": releases.append(copy.deepcopy(release))
    elif change == "published": release["draft"] = False
    elif change == "repo": release["url"] = "https://example.com/7"
    elif change == "asset_missing": assets.clear()
    elif change == "asset_duplicate": assets.append(copy.deepcopy(asset))
    elif change == "size": asset["size"] = module.MAX_BYTES + 1
    elif change == "digest": asset["digest"] = "sha256:" + "a" * 64
    elif change == "body": data = b'changed'
    elif change == "release_id": release["id"] = "7"
    elif change == "asset_id": asset["id"] = "8"
    def api(path, *, binary=False):
        if binary:
            assert path.endswith("/assets/8")
            return data
        return [assets if "/7/assets" in path else releases]
    monkeypatch.setattr(module, "api", api)
    target = tmp_path / "evidence.json"
    if change is None:
        module.download("v0.2.4", digest, target)
        assert target.read_bytes() == data
    else:
        with pytest.raises(ValueError):
            module.download("v0.2.5" if change == "tag" else "v0.2.4", digest, target)
        assert not target.exists()



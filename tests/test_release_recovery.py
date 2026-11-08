"""Synthetic recovery checks never authorize publication."""

import copy
import hashlib
import importlib
import subprocess
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("release_recovery"), importlib.import_module("download_release_evidence")


@pytest.mark.parametrize("change", [None, "missing", "duplicate", "published", "repo", "asset_missing",
    "asset_duplicate", "size", "digest", "body", "tag"])
def test_draft_download_is_bound_to_numeric_identity(modules, monkeypatch, tmp_path, change):
    _, module = modules
    data = b'{"synthetic":true}'
    digest = hashlib.sha256(data).hexdigest()
    prefix = f"https://api.github.com/repos/{module.REPOSITORY}/releases/"
    release = {"tag_name": "v0.2.2", "draft": True, "id": 7, "url": prefix + "7"}
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
    def api(path, *, binary=False):
        if binary:
            assert path.endswith("/assets/8")
            return data
        return [assets if "/7/assets" in path else releases]
    monkeypatch.setattr(module, "api", api)
    target = tmp_path / "evidence.json"
    if change is None:
        module.download("v0.2.2", digest, target)
        assert target.read_bytes() == data
    else:
        with pytest.raises(ValueError):
            module.download("v0.2.4" if change == "tag" else "v0.2.2", digest, target)
        assert not target.exists()


@pytest.mark.parametrize("change", [None, "runtime", "rename", "dependency", "lock", "config", "matrix"])
def test_source_comparison_rejects_changes_outside_recovery(modules, monkeypatch, tmp_path, change):
    module, _ = modules
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True).stdout
    git("init", "-q")
    files = {"src/dbt_dqm_app/__init__.py": '__version__ = "0.2.2"\n', "pyproject.toml": '[project]\nname="dbt-dqm"\nversion="0.2.2"\ndependencies=["example==1"]\n',
             "dbt_project.yml": 'name: dbt_dqm\nversion: 0.2.2\n',
             "uv.lock": 'version=1\n[[package]]\nname="dbt-dqm"\nversion="0.2.2"\n[[package]]\nname="example"\nversion="1"\n',
             "src/runtime.py": "original", "scripts/ci_validation.py": "original"}
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    def commit():
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
    commit()
    monkeypatch.setattr(module, "BASE_SHA", git("rev-parse", "HEAD").decode().strip())
    monkeypatch.setattr(module, "BASE_TREE", git("rev-parse", "HEAD^{tree}").decode().strip())
    monkeypatch.setattr(module, "git", git)
    for name in module.VERSION_FILES:
        p = tmp_path / name
        p.write_text(p.read_text().replace("0.2.2", "0.2.3"))
    if change == "runtime": (tmp_path / "src/runtime.py").write_text("changed")
    elif change == "rename": (tmp_path / "src/runtime.py").rename(tmp_path / "README.md")
    elif change in {"dependency", "config"}:
        p = tmp_path / "pyproject.toml"
        p.write_text(p.read_text().replace("example==1", "example==2") if change == "dependency" else p.read_text() + 'description="changed"\n')
    elif change == "lock":
        p = tmp_path / "uv.lock"
        p.write_text(p.read_text().replace('version="1"', 'version="2"'))
    elif change == "matrix": (tmp_path / "scripts/ci_validation.py").write_text("changed")
    commit()
    if change is None:
        assert module.verify_source()["release_version"] == "0.2.3"
    else:
        with pytest.raises(ValueError): module.verify_source()


def test_wheel_payload_comparison_checks_runtime_and_requirements(modules, tmp_path):
    module, _ = modules
    def wheel(version, runtime=b"code", requirement="example==1"):
        path = tmp_path / (version + ".whl")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dbt_dqm_app/app.py", runtime)
            archive.writestr(f"dbt_dqm-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.4\nVersion: {version}\nRequires-Dist: {requirement}\n")
            archive.writestr(f"dbt_dqm-{version}.dist-info/RECORD", "hashes")
        return path
    baseline = wheel("0.2.2")
    module.verify_wheels(baseline, wheel("0.2.3"))
    for runtime, requirement in ((b"changed", "example==1"), (b"code", "example==2")):
        with pytest.raises(ValueError): module.verify_wheels(baseline, wheel("0.2.3", runtime, requirement))


def test_privileged_draft_fetch_is_separate_from_verifier_and_publisher():
    import yaml
    flow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text())
    fetch = flow["jobs"]["fetch-evidence"]
    assert "environment" not in fetch and fetch["permissions"] == {"contents": "write", "actions": "read"}
    assert "workflow_dispatch" in fetch["if"]
    assert flow["jobs"]["preflight"]["permissions"] == {"contents": "read", "actions": "read"}
    assert all("gh release download" not in step.get("run", "") for job in flow["jobs"].values() for step in job["steps"])

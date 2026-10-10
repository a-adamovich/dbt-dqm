"""Catch documentation drift: every model is cataloged and relative doc links resolve."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _slug(heading):
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path):
    anchors, fenced = set(), False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            anchors.add(_slug(line.lstrip("#")))
    return anchors


def test_every_model_is_in_the_catalog():
    catalog = yaml.safe_load((ROOT / "models/schema.yml").read_text())
    documented = {model["name"] for model in catalog["models"]}
    models = {path.stem for path in (ROOT / "models").glob("*.sql")}
    assert models - documented == set()


@pytest.mark.parametrize("document", DOCS, ids=lambda path: path.name)
def test_relative_links_resolve(document):
    broken = []
    for target in LINK.findall(document.read_text()):
        if re.match(r"[a-z]+:", target):
            continue  # external URL
        path, _, anchor = target.partition("#")
        resolved = (document.parent / path).resolve() if path else document
        if not resolved.exists():
            broken.append(target)
            continue
        if anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            broken.append(target)
    assert broken == []

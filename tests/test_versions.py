"""The version lives in four places; the release workflow compares a tag with one of them."""

import re
import tomllib
from pathlib import Path

import yaml

import diematic

ROOT = Path(__file__).resolve().parent.parent


def pyproject_version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def app_version():
    config = yaml.safe_load((ROOT / "diematic_modbus" / "config.yaml").read_text(encoding="utf-8"))
    return config["version"]


def changelog_versions():
    text = (ROOT / "diematic_modbus" / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^## (\S+)$", text, re.M)


def test_the_version_is_a_plain_semantic_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", pyproject_version())


def test_package_metadata_module_and_app_agree():
    assert diematic.__version__ == pyproject_version() == app_version()


def test_the_newest_changelog_entry_is_the_current_version():
    versions = changelog_versions()
    assert versions[0] == app_version()


def test_the_changelog_lists_each_version_once_newest_first():
    versions = changelog_versions()
    assert len(versions) == len(set(versions))
    as_tuples = [tuple(int(part) for part in version.split(".")) for version in versions]
    assert as_tuples == sorted(as_tuples, reverse=True)

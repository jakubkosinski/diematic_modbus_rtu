"""Mechanical checks that the documentation matches the code."""

import re
from pathlib import Path

import pytest

from diematic.config.settings import BusSettings, GatewaySettings
from diematic.plugins.mqtt.settings import MqttSettings

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
DIAGRAMS = ("flowchart", "sequenceDiagram", "stateDiagram", "graph", "classDiagram")


def valid_variables() -> set[str]:
    names = {f"DIEMATIC_{name.upper()}" for name in GatewaySettings.model_fields}
    names |= {f"DIEMATIC_BUS__{name.upper()}" for name in BusSettings.model_fields}
    names |= {f"MQTT_{name.upper()}" for name in MqttSettings.model_fields}
    return names | {"DIEMATIC_CONFIG", "DIEMATIC_ENABLED_PLUGINS"}


WILDCARDS = {"DIEMATIC_", "MQTT_", "DIEMATIC_BUS__"}


def mentioned_variables(text: str) -> set[str]:
    found = set(re.findall(r"\b((?:DIEMATIC|MQTT)_[A-Z][A-Z_]*[A-Z])\b", text))
    return {name for name in found if name not in WILDCARDS}


def test_env_example_only_sets_real_settings():
    declared = set(re.findall(r"^#?\s*([A-Z][A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert declared
    assert declared <= valid_variables()


def test_env_example_covers_every_mqtt_and_gateway_setting_users_need():
    declared = set(re.findall(r"^([A-Z][A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    for required in ("DIEMATIC_HOST", "DIEMATIC_PORT", "MQTT_HOST", "MQTT_PASSWORD"):
        assert required in declared


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_documents_mention_only_real_environment_variables(path):
    unknown = mentioned_variables(path.read_text(encoding="utf-8")) - valid_variables()
    assert not unknown, f"{path.name}: {sorted(unknown)}"


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_relative_links_resolve(path):
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)", text):
        location = (path.parent / target.split("#")[0]).resolve()
        assert location.exists(), f"{path.name} links to missing {target}"


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_mermaid_blocks_are_closed_and_typed(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    inside = False
    for index, line in enumerate(lines):
        if line.strip() == "```mermaid":
            assert not inside, f"{path.name}: nested fence at line {index + 1}"
            inside = True
            assert lines[index + 1].strip().startswith(DIAGRAMS), f"{path.name}:{index + 2}"
        elif inside and line.strip() == "```":
            inside = False
    assert not inside, f"{path.name}: unterminated mermaid block"


def test_every_document_is_reachable_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for document in (ROOT / "docs").glob("*.md"):
        assert f"docs/{document.name}" in readme, document.name


def test_diagrams_exist_where_the_conventions_ask_for_them():
    assert "```mermaid" in (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "```mermaid" in (ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")


def test_documentation_does_not_reproduce_the_vendor_pdf_by_name_or_path():
    for path in MARKDOWN:
        assert ".pdf" not in path.read_text(encoding="utf-8").lower(), path.name

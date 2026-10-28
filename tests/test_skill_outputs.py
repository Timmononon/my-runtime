"""Real installed helpers/native tool artifacts, with honest offline boundaries."""
import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader


def validator():
    path = Path(__file__).resolve().parents[1] / "scripts/validate_bundled_skills.py"
    spec = importlib.util.spec_from_file_location("validate_bundled_skills", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_eight_installed_workflows_produce_consumable_native_outputs(tmp_path):
    output, report = tmp_path / "outputs", tmp_path / "evidence.json"
    result = await validator().validate(output, report)
    assert len(result["packages"]) == 8
    assert result["language_model_workflow_quality"] == "unsupported" and result["paid_calls"] == 0
    assert all(package["model_workflow_quality"] == "unsupported" for package in result["packages"])
    packages = {package["name"]: package for package in result["packages"]}
    assert packages["mcp-builder"]["native_workflow_checks"]["stdio_sdk_actual_call"] == 6
    assert packages["mcp-builder"]["native_workflow_checks"]["oversized_input_rejected"] is True
    assert packages["webapp-testing"]["native_workflow_checks"]["wrong_digest_rejected"] is True
    assert packages["doc-coauthoring"]["native_workflow_checks"]["factual_verification"] == "not_performed"
    assert all(package["allowed_tools"] is None for package in result["packages"])
    assert result["existing_file_read_outputs"] == 10
    assert "one observed page" in PdfReader(output / "source.pdf").pages[0].extract_text()
    with Image.open(output / "source-page-1.jpg") as image:
        assert image.width <= 512 and image.height <= 512
    assert "Source SHA256:" in (output / "reading-notes.md").read_text(encoding="utf-8")
    assert json.loads(report.read_text())["native_outputs"] == result["native_outputs"]
    assert not (output / "credentials.yaml").exists() and not (output / "runtime.db").exists()


@pytest.mark.asyncio
async def test_validation_refuses_to_overwrite_an_existing_deliverable(tmp_path):
    output = tmp_path / "outputs"
    output.mkdir()
    original = output / "original.html"
    original.write_text("User-owned existing artifact", encoding="utf-8")
    with pytest.raises(ValueError, match="empty directory"):
        await validator().validate(output, tmp_path / "report.json")
    assert original.read_text() == "User-owned existing artifact"

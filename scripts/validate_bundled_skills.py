"""Execute eight installed workflows' native helpers, without a language model.

All installation/state lives in a TemporaryDirectory. Only synthetic native
outputs and their hashes are retained in the explicitly selected output folder.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from aster.customization.skills import SkillRegistry, SkillScope, UseSkillTool
from aster.domain import ToolContext
from aster.mcp.client import MCPClient, MCPServerConfig
from aster.packages.manager import SkillPackageManager
from aster.platform.processes import ProcessSupervisor
from aster.tools.documents import DocumentTool
from aster.tools.files import build_file_tools
from aster.tools.images import ImageTool

ENTRYPOINTS = {
    "webapp-testing": "scripts/check_download.py",
    "mcp-builder": "templates/server.py",
    "skill-creator": "templates/SKILL.template.md",
    "doc-coauthoring": "scripts/check_sources.py",
    "frontend-design": "templates/index.html",
    "theme-factory": "scripts/showcase.py",
    "web-artifacts-builder": "scripts/check_artifact.py",
    "pdf-reader": "templates/reading-notes.md",
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def helper(script, *arguments, expect=0):
    result = subprocess.run([sys.executable, str(script), *map(str, arguments)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=20, check=False)
    require(result.returncode == expect, "Installed Skill helper failed its expected outcome")
    return result.stdout


def make_pdf(path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=320, height=180)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 14 Tf 20 120 Td (Synthetic local source: one observed page.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as file:
        writer.write(file)


async def browser_checks(output):
    from playwright.async_api import async_playwright

    measurements = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for width in (1440, 390):
                page = await browser.new_page(viewport={"width": width, "height": 900})
                errors, remote_requests = [], []
                page.on("pageerror", lambda error, failures=errors: failures.append(type(error).__name__))
                page.on("request", lambda request, requests=remote_requests: requests.append(request.url)
                        if request.url.startswith(("http://", "https://")) else None)
                await page.goto((output / "frontend.html").as_uri())
                await page.locator("#complete").click()
                require(await page.locator("#status").inner_text() == "Review complete for this page view.",
                        "Frontend template did not complete its actual interaction")
                geometry = await page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})")
                require(geometry["scroll"] <= geometry["width"], "Frontend template overflows the viewport")
                await page.goto((output / "reading-list.html").as_uri())
                await page.locator("#query").fill("Design")
                require(await page.locator("#items li:visible").count() == 1 and
                        await page.locator("#count").inner_text() == "1 entries", "HTML filtering did not execute")
                await page.locator("#query").fill("absent synthetic title")
                require(await page.locator("#empty").is_visible() and
                        await page.locator("#items li:visible").count() == 0, "HTML empty state failed")
                geometry = await page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})")
                require(geometry["scroll"] <= geometry["width"], "HTML artifact overflows the viewport")
                require(not errors and not remote_requests, "Native templates attempted a remote request or JS failed")
                measurements.append({"width": width, "interaction": "passed", "overflow": False,
                                     "page_errors": 0, "remote_requests": 0})
                await page.close()
        finally:
            await browser.close()
    return measurements


async def validate(output: Path, report: Path, *, browser=False):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    require(not any(output.iterdir()), "Use a new empty directory for Skill validation outputs")
    checks = {}
    with tempfile.TemporaryDirectory(prefix="aster-installed-skills-") as directory:
        manager = SkillPackageManager(Path(directory) / "home")
        catalog = manager.catalog()
        require({item["name"] for item in catalog} == set(ENTRYPOINTS), "The eight bundled workflows changed")
        for item in catalog:
            plan = manager.preview("bundled:" + item["name"])
            receipt = manager.install(plan["plan_id"], expected_digest=item["digest"])
            require(receipt["digest"] == item["digest"], "Installed source digest does not match the catalog")
        registry = SkillRegistry(manager.root)
        skills = {skill.name: skill for skill in registry.reload()}
        def no_secret(_reference):
            raise ValueError("This offline validation has no credential authority")
        scope = SkillScope(registry, bound_skills=list(ENTRYPOINTS), secret_resolver=no_secret)
        context = ToolContext("synthetic-skill-session", "synthetic-skill-run", output,
            Path(directory) / "tool-artifacts", asyncio.Event(), services={"skill_scope": scope})
        packages = []
        for item in catalog:
            skill = skills[item["name"]]
            require(skill.version == item["digest"] and not skill.secrets, "Installed metadata differs")
            require(bool(skill.read_reference("LICENSE.txt")) and bool(skill.read_reference("NOTICE.md")),
                    "Installed license or provenance notice is missing")
            loaded = await UseSkillTool().execute({"name": skill.name}, context)
            reference = await UseSkillTool().execute({"name": skill.name,
                "reference": ENTRYPOINTS[skill.name]}, context)
            require(not loaded.is_error and not reference.is_error and bool(reference.content),
                    "Installed workflow entrypoint cannot be read through use_skill")
            require(loaded.data["allowed_tools"] == (sorted(skill.allowed_tools) if skill.allowed_tools is not None else None),
                    "Skill permissions were altered by loading")
            packages.append({"name": skill.name, "source": "bundled:" + skill.name,
                "digest": skill.version, "license": item["license"],
                "allowed_tools": loaded.data["allowed_tools"], "entrypoint": ENTRYPOINTS[skill.name],
                "source_install_reference_license": "passed", "model_workflow_quality": "unsupported"})

        def path(name, reference):
            return skills[name].resolve(reference)

        helper(path("theme-factory", "scripts/showcase.py"), "--output", output / "theme-catalog.html")
        showcase = (output / "theme-catalog.html").read_text(encoding="utf-8")
        require(showcase.count("<section>") == 10, "Theme helper did not emit all ten actual references")
        palette = re.findall(r"#[0-9a-fA-F]{6}\b", skills["theme-factory"].read_reference("themes/forest-canopy.md"))
        require(len(palette) == 4, "Theme reference palette changed")
        (output / "palette.css").write_text(":root{" + "".join(f"--forest-{i}:{color};" for i, color in enumerate(palette)) + "}\n", encoding="utf-8")
        svg = ET.Element("svg", xmlns="http://www.w3.org/2000/svg", width="320", height="80", viewBox="0 0 320 80")
        for i, color in enumerate(palette):
            ET.SubElement(svg, "rect", x=str(i * 80), y="0", width="80", height="80", fill=color)
        ET.ElementTree(svg).write(output / "palette.svg", encoding="utf-8", xml_declaration=True)
        require(len(ET.parse(output / "palette.svg").getroot()) == 4, "Generated SVG palette is invalid")
        checks["theme-factory"] = {"helper_sections": 10, "css_svg_palette": "reference_colors_applied_mechanically"}

        shutil.copyfile(path("frontend-design", "templates/index.html"), output / "frontend.html")
        shutil.copyfile(path("web-artifacts-builder", "templates/interactive.html"), output / "reading-list.html")
        artifact = json.loads(helper(path("web-artifacts-builder", "scripts/check_artifact.py"), output / "reading-list.html"))
        require(artifact["passed"] and artifact["javascript_execution"] == "not_performed", "Artifact static check failed")
        checks["web-artifacts-builder"] = artifact
        checks["frontend-design"] = {"native_template": "copied_and_read", "responsive_interaction": "pending" if browser else "not_performed"}

        source = "This synthetic local source records one observed page."
        (output / "source.md").write_text(source, encoding="utf-8")
        document = skills["doc-coauthoring"].read_reference("templates/document.md")
        document = document.replace("[who will read this]", "Offline delivery reviewers").replace("[decision or action]", "Verify the native workflow")
        document = document.replace("State the problem using observed facts. Cite each important claim as [S1].", source + " [S1]")
        document = document.replace("- TODO: Resolve the remaining consequential uncertainty.", "- Language-model writing quality was not evaluated.")
        document = document.replace("Source path or URL; page/paragraph; observation date; exact supporting passage.", "source.md; paragraph 1; synthetic fixture; " + source)
        (output / "document.md").write_text(document, encoding="utf-8")
        sources = json.loads(helper(path("doc-coauthoring", "scripts/check_sources.py"), output / "document.md"))
        require(sources["source_markers"] == ["[S1]"] and not sources["unresolved"] and
                sources["factual_verification"] == "not_performed", "Document source inventory failed")
        checks["doc-coauthoring"] = sources

        local_skill = output / "created-skills" / "synthetic-validation"
        local_skill.mkdir(parents=True)
        created = skills["skill-creator"].read_reference("templates/SKILL.template.md").replace("example-workflow", "synthetic-validation")
        created = created.replace("Describe when this workflow should be used.", "Use only to validate local synthetic native artifacts offline.")
        (local_skill / "SKILL.md").write_text(created, encoding="utf-8")
        created_registry = SkillRegistry(local_skill.parent)
        created_registry.reload()
        created_scope = SkillScope(created_registry, secret_resolver=no_secret)
        created_scope.activate("synthetic-validation")
        require(created_scope.check_tool("file_read") and not created_scope.check_tool("send_email"),
                "Created workflow did not enforce its tool restriction")
        checks["skill-creator"] = {"registry_loaded": True, "allowed_read": True, "external_tool_denied": True}

        mcp_config = MCPServerConfig(name="synthetic-helper", command=sys.executable,
            args=(str(path("mcp-builder", "templates/server.py")),), cwd=output,
            required=True, trust_annotations=True, timeout_seconds=10, reconnect_attempts=0)
        async with MCPClient([mcp_config]) as client:
            tools = await client.list_tools()
            require(len(tools) == 1, "MCP template returned an unexpected catalog")
            tool = tools[0]
            result = await tool.execute({"values": [1, 2, 3]}, context)
            require(not result.is_error and float(result.content) == 6, "Actual MCP template call returned an invalid sum")
            invalid = await tool.execute({"values": [1] * 1001}, context)
            require(invalid.is_error, "Actual MCP template omitted its input bound")
            require(tool.spec.replay_safe and tool.spec.risk == "read", "Template annotations were not honored")
        shutil.copyfile(path("mcp-builder", "templates/server.py"), output / "mcp-server.py")
        checks["mcp-builder"] = {"stdio_sdk_actual_call": 6, "oversized_input_rejected": True, "read_only_idempotent": True}

        pdf = output / "source.pdf"
        make_pdf(pdf)
        supervisor = ProcessSupervisor()
        try:
            text = await DocumentTool(supervisor, "pdf").execute({"path": "source.pdf"}, context)
            require(not text.is_error and '"page": 1' in text.content and "one observed page" in text.content,
                    "Existing PDF text tool did not preserve source coordinates")
            pages = await ImageTool(supervisor, pdf=True).execute({"path": "source.pdf", "pages": "1",
                "max_pages": 1, "max_dimension": 512}, context)
            require(not pages.is_error and pages.data["pages"] == [1] and len(pages.artifacts) == 1,
                    "Existing PDF page tool failed its bounded render")
            from PIL import Image
            raster = Path(pages.artifacts[0]["path"])
            with Image.open(raster) as image:
                require(max(image.size) <= 512 and image.getextrema() != ((255, 255),) * 3, "Rendered PDF is blank or unbounded")
            shutil.copyfile(raster, output / "source-page-1.jpg")
        finally:
            await supervisor.close()
        notes = skills["pdf-reader"].read_reference("templates/reading-notes.md")
        notes = notes.replace("Source file:", "Source file: source.pdf").replace("Source SHA256:",
            "Source SHA256: " + hashlib.sha256(pdf.read_bytes()).hexdigest()).replace("Total pages:", "Total pages: 1")
        notes = notes.replace("Text-extracted pages:", "Text-extracted pages: 1").replace("Visually inspected pages:",
            "Visually inspected pages: not model-inspected; actual page-1 render verified")
        notes = notes.replace("| | | text / visual / inference | | |", "| One observed page | 1 | text | one observed page | Synthetic fixture only |")
        (output / "reading-notes.md").write_text(notes, encoding="utf-8")
        checks["pdf-reader"] = {"actual_text_tool": "page_1_preserved", "actual_pdf_page_tool": "bounded_jpeg", "ocr": "unsupported"}

        digest = hashlib.sha256((output / "reading-list.html").read_bytes()).hexdigest()
        download = json.loads(helper(path("webapp-testing", "scripts/check_download.py"), output / "reading-list.html", "--sha256", digest))
        bad_download = json.loads(helper(path("webapp-testing", "scripts/check_download.py"), output / "reading-list.html", "--sha256", "0" * 64, expect=1))
        require(download["passed"] and not bad_download["passed"], "Download helper did not distinguish correct bytes from corruption")
        checks["webapp-testing"] = {"actual_download_digest": download, "wrong_digest_rejected": True}

        file_read = next(tool for tool in build_file_tools() if tool.spec.name == "file_read")
        read_count = 0
        for file in output.rglob("*"):
            if file.is_file() and file.suffix in {".html", ".css", ".svg", ".md", ".py"}:
                read = await file_read.execute({"path": str(file.relative_to(output))}, context)
                require(not read.is_error, "A native output cannot be consumed by existing file_read")
                read_count += 1
    measurements = await browser_checks(output) if browser else []
    if browser:
        checks["frontend-design"]["responsive_interaction"] = "passed"
    for package in packages:
        package["native_workflow_checks"] = checks[package["name"]]
    files = {file.relative_to(output).as_posix(): {"bytes": file.stat().st_size,
        "sha256": hashlib.sha256(file.read_bytes()).hexdigest()} for file in sorted(output.rglob("*")) if file.is_file()}
    result = {"schema_version": 1, "mode": "offline_native_helpers", "packages": packages,
        "native_outputs": files, "output_directory": str(output), "existing_file_read_outputs": read_count,
        "browser_measurements": measurements, "paid_calls": 0, "real_credentials_used": False,
        "language_model_workflow_quality": "unsupported", "design_quality": "unsupported"}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"packages": len(packages), "native_outputs": len(files), "browser_widths": [row["width"] for row in measurements],
        "paid_calls": 0, "model_quality": "unsupported", "report": str(report)}))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--browser", action="store_true", help="Run actual owned local Chromium interaction and geometry checks")
    args = parser.parse_args()
    output = args.output or args.report.with_suffix("").with_name(args.report.stem + "-outputs")
    asyncio.run(validate(output, args.report, browser=args.browser))

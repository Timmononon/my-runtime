"""Explicitly budgeted provider acceptance test; never runs by default.

ASTER_RUN_LIVE=1 and ASTER_LIVE_REQUEST_BUDGET=7 enable at most seven
generation requests, each with <=2048 output tokens and no provider retries.
Credentials come only from CredentialStore. The report contains usage, not keys.
"""

import json
import os
from pathlib import Path

import pytest

from aster.customization.credentials import CredentialStore
from aster.domain import Message, ToolSpec
from aster.providers.deepseek import DeepSeekResponsesClient, ProviderError


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("ASTER_RUN_LIVE") != "1", reason="live API disabled by default")
async def test_budgeted_deepseek_contract(tmp_path):
    budget = int(os.environ.get("ASTER_LIVE_REQUEST_BUDGET", "0"))
    assert 7 <= budget <= 8, "This scenario needs seven explicitly authorized requests (maximum eight)"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    client = DeepSeekResponsesClient(
        lambda: CredentialStore().get() or "",
        effort="low",
        max_output_tokens=2048,
        max_retries=0,
        timeout_seconds=180,
        artifact_root=artifacts,
    )
    report = {"requests": 0, "results": []}

    async def generate(messages, tools, label):
        assert report["requests"] < budget
        report["requests"] += 1
        turn = await client.generate(
            messages,
            tools,
            system="You are a test assistant. Follow explicit tool requests. Keep responses brief.",
        )
        report["results"].append(
            {
                "case": label,
                "finish_reason": turn.finish_reason,
                "calls": len(turn.tool_calls),
                "output_types": [item["type"] for item in turn.provider_items],
                "usage": turn.usage.raw,
            }
        )
        return turn

    def append_turn(messages, turn):
        messages.append(
            Message("assistant", turn.text, tool_calls=turn.tool_calls, provider_items=turn.provider_items)
        )
        for call in turn.tool_calls:
            assert call.name == "add_numbers"
            result = call.arguments["x"] + call.arguments["y"]
            messages.append(Message("tool", str(result), tool_call_id=call.id))

    tool = ToolSpec(
        "add_numbers",
        "Add two integers. Use whenever the user requests a sum.",
        {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
            "additionalProperties": False,
        },
    )
    try:
        capabilities = await client.refresh_capabilities()
        report["models"] = {
            name: {
                "context_window": cap.context_window,
                "modalities": cap.input_modalities,
                "max_output": cap.max_output_tokens,
            }
            for name, cap in capabilities.items()
        }
        assert client.model in capabilities
        messages = [
            Message("user", "Call add_numbers with x=2, y=3. Do not calculate without using the tool.")
        ]
        turn = await generate(messages, [tool], "first_tool")
        assert turn.tool_calls
        append_turn(messages, turn)
        turn = await generate(messages, [tool], "tool_result")
        assert not turn.tool_calls and "5" in turn.text
        append_turn(messages, turn)
        messages.append(Message("user", "Now call add_numbers with x=7, y=8. You must use the tool."))
        turn = await generate(messages, [tool], "cross_user_turn")
        assert turn.tool_calls
        append_turn(messages, turn)
        turn = await generate(messages, [tool], "cross_user_result")
        assert not turn.tool_calls and "15" in turn.text
        # A new active context contains a factual summary, never fabricated reasoning.
        compacted = [
            Message(
                "user", "Earlier completed sums were 2+3=5 and 7+8=15. Now call add_numbers with x=9,y=1."
            )
        ]
        turn = await generate(compacted, [tool], "compacted_context")
        assert turn.tool_calls
        append_turn(compacted, turn)
        turn = await generate(compacted, [tool], "compacted_result")
        assert not turn.tool_calls and "10" in turn.text
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 640, "height": 240})
            await page.set_content(
                "<html><body style='font:32px sans-serif'><h1>ASTER VISION 47</h1><p>Local browser test.</p></body></html>"
            )
            screenshot = artifacts / "browser.png"
            await page.screenshot(path=str(screenshot))
            await browser.close()
        # Tool-output imagery specifically exercises the chosen Responses protocol.
        from aster.domain import ToolCall

        image_messages = [
            Message("user", "Read the heading in the browser screenshot. Reply with the heading only."),
            Message("assistant", tool_calls=[ToolCall("local_screenshot", "browser_screenshot", {})]),
            Message(
                "tool",
                "Screenshot",
                tool_call_id="local_screenshot",
                metadata={
                    "artifacts": [{"artifact_id": "screenshot", "path": str(screenshot)}],
                    "content_parts": [
                        {
                            "type": "input_image",
                            "artifact_id": "screenshot",
                            "path": str(screenshot),
                            "mime_type": "image/png",
                        }
                    ],
                },
            ),
        ]
        screenshot_tool = ToolSpec(
            "browser_screenshot",
            "Take a local browser screenshot",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        # Synthetic tool histories do not include model-generated reasoning.
        # Exercise that documented insertion path with thinking explicitly off.
        client.effort = "none"
        turn = await generate(image_messages, [screenshot_tool], "browser_image_tool_output")
        assert "ASTER VISION 47" in turn.text
        report["passed"] = True
    except ProviderError as exc:
        report["error"] = {"category": exc.category, "status_code": exc.status_code}
        pytest.fail(f"Live provider check failed: {exc.category}, HTTP {exc.status_code}", pytrace=False)
    finally:
        await client.aclose()
        path = Path(os.environ.get("ASTER_LIVE_REPORT", str(tmp_path / "live-report.json")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("DeepSeek live report:", json.dumps(report, ensure_ascii=False))


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("ASTER_LIVE_IMAGE_RETEST") != "1", reason="separate one-request authorization required"
)
async def test_budgeted_image_tool_output(tmp_path):
    assert os.environ.get("ASTER_LIVE_REQUEST_BUDGET") == "1"
    from playwright.async_api import async_playwright

    from aster.domain import ToolCall

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 640, "height": 240})
        await page.set_content("<h1 style='font:40px sans-serif'>ASTER VISION 47</h1>")
        screenshot = artifacts / "browser.png"
        await page.screenshot(path=str(screenshot))
        await browser.close()
    report = {"requests": 1, "case": "browser_image_tool_output_with_definition_nonthinking"}

    client = DeepSeekResponsesClient(
        lambda: CredentialStore().get() or "",
        effort="none",
        max_output_tokens=2048,
        max_retries=0,
        timeout_seconds=180,
        artifact_root=artifacts,
    )
    messages = [
        Message("user", "Read the heading in the browser screenshot. Reply with the heading only."),
        Message("assistant", tool_calls=[ToolCall("local_screenshot", "browser_screenshot", {})]),
        Message(
            "tool",
            "Screenshot",
            tool_call_id="local_screenshot",
            metadata={
                "artifacts": [{"artifact_id": "screenshot", "path": str(screenshot)}],
                "content_parts": [
                    {
                        "type": "input_image",
                        "artifact_id": "screenshot",
                        "path": str(screenshot),
                        "mime_type": "image/png",
                    }
                ],
            },
        ),
    ]
    tool = ToolSpec(
        "browser_screenshot",
        "Take a browser screenshot",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    try:
        turn = await client.generate(
            messages, [tool], system="Read the already provided screenshot; no further tool call needed."
        )
        report.update(
            passed="ASTER VISION 47" in turn.text, usage=turn.usage.raw, finish_reason=turn.finish_reason
        )
        assert report["passed"]
    except ProviderError as exc:
        report.update(
            passed=False,
            error={"category": exc.category, "status_code": exc.status_code, "details": exc.details},
        )
        pytest.fail(f"Live image check failed: {exc.category}, HTTP {exc.status_code}", pytrace=False)
    finally:
        await client.aclose()
        path = Path(os.environ.get("ASTER_LIVE_REPORT", str(tmp_path / "live-report.json")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("DeepSeek image report:", json.dumps(report, ensure_ascii=False))


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("ASTER_LIVE_THINKING_IMAGE") != "1", reason="separate two-request authorization required"
)
async def test_budgeted_thinking_image_chain(tmp_path):
    assert os.environ.get("ASTER_LIVE_REQUEST_BUDGET") == "2"
    from playwright.async_api import async_playwright

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 640, "height": 240})
        await page.set_content("<h1 style='font:40px sans-serif'>ASTER VISION 47</h1>")
        screenshot = artifacts / "browser.png"
        await page.screenshot(path=str(screenshot))
        await browser.close()
    report = {"requests": 0, "case": "thinking_model_tool_call_to_browser_image", "results": []}
    client = DeepSeekResponsesClient(
        lambda: CredentialStore().get() or "",
        effort="low",
        max_output_tokens=2048,
        max_retries=0,
        timeout_seconds=180,
        artifact_root=artifacts,
    )
    tool = ToolSpec(
        "browser_screenshot",
        "Take a screenshot of the currently open local browser page",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    messages = [
        Message(
            "user",
            "Call browser_screenshot to observe the current browser page, then report its exact heading.",
        )
    ]
    system = "Use browser_screenshot once, then read its image result and return the exact heading only."
    try:
        report["requests"] += 1
        turn = await client.generate(messages, [tool], system=system)
        report["results"].append(
            {
                "phase": "model_tool_call",
                "usage": turn.usage.raw,
                "output_types": [item["type"] for item in turn.provider_items],
            }
        )
        assert turn.tool_calls
        messages.append(
            Message("assistant", turn.text, tool_calls=turn.tool_calls, provider_items=turn.provider_items)
        )
        for call in turn.tool_calls:
            assert call.name == "browser_screenshot"
            messages.append(
                Message(
                    "tool",
                    "Current local browser screenshot",
                    tool_call_id=call.id,
                    metadata={
                        "artifacts": [{"artifact_id": "screenshot", "path": str(screenshot)}],
                        "content_parts": [
                            {
                                "type": "input_image",
                                "artifact_id": "screenshot",
                                "path": str(screenshot),
                                "mime_type": "image/png",
                            }
                        ],
                    },
                )
            )
        report["requests"] += 1
        turn = await client.generate(messages, [tool], system=system)
        report["results"].append(
            {
                "phase": "image_result",
                "usage": turn.usage.raw,
                "output_types": [item["type"] for item in turn.provider_items],
            }
        )
        report["passed"] = "ASTER VISION 47" in turn.text and not turn.tool_calls
        assert report["passed"]
    except ProviderError as exc:
        report.update(
            passed=False,
            error={"category": exc.category, "status_code": exc.status_code, "details": exc.details},
        )
        pytest.fail(
            f"Live thinking image check failed: {exc.category}, HTTP {exc.status_code}", pytrace=False
        )
    finally:
        await client.aclose()
        path = Path(os.environ.get("ASTER_LIVE_REPORT", str(tmp_path / "live-report.json")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("DeepSeek thinking image report:", json.dumps(report, ensure_ascii=False))

from pathlib import Path

from aster.platform.processes import ProcessSupervisor
from aster.service.previews import PreviewManager
from aster.tools.basics import build_basic_tools
from aster.tools.browser import BrowserManager, build_browser_tools
from aster.tools.deliverables import DeliverableTool
from aster.tools.documents import DocumentTool
from aster.tools.files import build_file_tools
from aster.tools.http import HTTPTool
from aster.tools.images import ImageTool
from aster.tools.process import ProcessTool
from aster.tools.search import SearchTool


class ToolFactory:
    """Application-owned resources. Construction starts no processes or network."""

    def __init__(self, data_dir: Path, *, browser_enabled=True, headless=True):
        self.supervisor = ProcessSupervisor()
        self.preview = PreviewManager()
        self.browser = BrowserManager(Path(data_dir) / "browser_profiles", headless=headless) if browser_enabled else None
        self.tools = build_file_tools() + [ProcessTool(self.supervisor), ProcessTool(self.supervisor, shell=True)]
        self.tools += [DocumentTool(self.supervisor)] + [DocumentTool(self.supervisor, kind)
                                                      for kind in ("pdf", "docx", "xlsx", "pptx")]
        self.tools += [ImageTool(self.supervisor), ImageTool(self.supervisor, pdf=True),
                       HTTPTool(), HTTPTool(fetch=True), DeliverableTool()]
        self.tools += build_basic_tools(self.supervisor) + [SearchTool()]
        if self.browser:
            self.tools += build_browser_tools(self.browser)

    @property
    def services(self):
        return {"process_supervisor": self.supervisor, "browser_manager": self.browser,
                "preview_manager": self.preview}

    async def close(self):
        await self.preview.close()
        if self.browser:
            await self.browser.close()
        await self.supervisor.close()


def build_tools(data_dir: Path, *, browser_enabled=True, headless=True) -> list:
    factory = ToolFactory(data_dir, browser_enabled=browser_enabled, headless=headless)
    for tool in factory.tools:
        tool.factory = factory
    return factory.tools


async def close_tools(tools: list):
    seen = set()
    for tool in tools:
        owner = getattr(tool, "factory", None)
        if owner and id(owner) not in seen:
            seen.add(id(owner))
            await owner.close()

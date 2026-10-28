"""Authenticated operator management routes; configuration cannot grant UI authority."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aster.config import read_yaml
from aster.customization.management import ManagementService
from aster.memory.semantic import validate_embedding_config


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Update(Body):
    expected_revision: str = Field(min_length=64, max_length=64)
    mcp_servers: list[dict] | None = None
    hooks: list[dict] | None = None
    permissions: dict | None = None
    embedding: dict | None = None
    web_search: dict | None = None
    browser: dict | None = None


class AgentUpdate(Body):
    expected_revision: str = Field(min_length=64, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    instructions: str | None = Field(default=None, max_length=500_000)
    selection: dict | None = None
    permissions: dict | None = None


class AgentCreate(AgentUpdate):
    id: str = Field(min_length=1, max_length=128)


class Credential(Body):
    expected_revision: str = Field(min_length=64, max_length=64)
    account: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=16_384)


def create_management_router(coordinator):
    router = APIRouter(prefix="/management")
    manager = ManagementService(coordinator)

    @router.get("")
    async def read():
        return await manager.read()

    @router.patch("")
    async def update(body: Update):
        values = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
        previous_embedding = None
        if "embedding" in values:
            try:
                previous_embedding = validate_embedding_config(read_yaml(coordinator.data_dir / "config.yaml").get("embedding", {}))
            except (ValueError, TypeError):
                pass  # A valid replacement may repair an invalid previous value.
        manager.update(body.expected_revision, values)
        if "mcp_servers" in values:
            await coordinator.invalidate_mcp()
        if "embedding" in values and previous_embedding != validate_embedding_config(values["embedding"]):
            await coordinator.invalidate_local_embeddings()
        return await manager.read()

    @router.post("/agents", status_code=201)
    async def create_agent(body: AgentCreate):
        manager.save_agent(body.id, body.expected_revision,
                           body.model_dump(exclude_unset=True, exclude={"expected_revision", "id"}), create=True)
        return await manager.read()

    @router.get("/agents/{agent_id}")
    async def read_agent(agent_id: str):
        result = await manager.read()
        return next((item for item in result["agents"] if item["id"] == agent_id), None) or _missing_agent()

    @router.put("/agents/{agent_id}")
    async def update_agent(agent_id: str, body: AgentUpdate):
        manager.save_agent(agent_id, body.expected_revision,
                           body.model_dump(exclude_unset=True, exclude={"expected_revision"}))
        return await manager.read()

    @router.post("/mcp/{name}/diagnose")
    async def diagnose(name: str):
        rows = await coordinator.mcp_health(probe=True)
        selected = [row for row in rows if row["name"] == name]
        if not selected:
            raise KeyError(name)
        return {"health": selected}

    @router.post("/mcp/{name}/reconnect")
    async def reconnect(name: str):
        return {"health": await coordinator.reconnect_mcp(name)}

    @router.post("/credentials", status_code=201)
    async def credential(body: Credential):
        result = manager.save_credential(body.expected_revision, body.account, body.value)
        await coordinator.invalidate_mcp()
        return result

    return router


def _missing_agent():
    raise KeyError("Agent does not exist")

"""Minimal stdio MCP server; no external service and no writes."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server=FastMCP('aster-example')
@server.tool(annotations=ToolAnnotations(readOnlyHint=True,idempotentHint=True))
def example_sum(values:list[float])->float:
    """Sum bounded numeric input for a local protocol smoke test."""
    if len(values)>1000:
        raise ValueError('At most 1000 values')
    return sum(values)
if __name__=='__main__':
    server.run(transport='stdio')

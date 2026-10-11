"""Tool boxes whose limits come from the MCP server's own annotations, enforced at offer time and at call time."""

from __future__ import annotations

from chaoslab.agents.runtime import ToolBox

#: Tools that change the plant or start a fault. No restricted role may hold them.
FORBIDDEN_TOOLS = frozenset({"run_experiment", "emergency_stop", "plant_start", "plant_stop",
                             "wait_for_experiment"})


class RestrictedToolBox(ToolBox):
    """Offers and calls only tools annotated read-only, plus the named non-destructive write tools."""

    writes: frozenset[str] = frozenset()
    forbidden: frozenset[str] = FORBIDDEN_TOOLS

    async def _permitted(self) -> set[str]:
        permitted = set()
        for tool in await self.server.list_tools():
            notes = tool.annotations
            if tool.name in self.forbidden:
                continue
            if getattr(notes, "read_only_hint", None) is True:
                permitted.add(tool.name)
            elif tool.name in self.writes and getattr(notes, "destructive_hint", None) is False:
                permitted.add(tool.name)
        return permitted

    async def specs(self) -> list[dict]:
        permitted = await self._permitted()
        refused = [name for name in self.allowed if name not in permitted]
        if refused:
            raise PermissionError(f"refusing to offer tools outside this role's limits: {refused}")
        return await super().specs()

    async def call(self, name: str, arguments: dict) -> str:
        if name not in self.allowed or name not in await self._permitted():
            return f"ERROR: {name} is outside this agent's permitted tools; refused"
        return await super().call(name, arguments)


class ReadOnlyToolBox(RestrictedToolBox):
    writes = frozenset()


class PlannerToolBox(RestrictedToolBox):
    """Read-only tools plus propose_experiment, which saves a proposal and runs nothing."""

    writes = frozenset({"propose_experiment"})

from any_mcp import Plugin, tool


class Hello(Plugin):
    name = "hello"

    @tool
    async def greet(self, name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}!"

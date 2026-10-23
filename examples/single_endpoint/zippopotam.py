"""One upstream endpoint exposed as one tool, declared as data with no call code."""

from any_mcp import HttpBinding, Plugin, ToolSpec


class Zippopotam(Plugin):
    name = "zippopotam"

    async def load_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="lookup_postal_code",
                description="Look up the places that belong to a postal code.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "country": {"type": "string", "description": "Country code, e.g. us"},
                        "postal_code": {"type": "string", "description": "Postal code, e.g. 90210"},
                    },
                    "required": ["country", "postal_code"],
                },
                # {country} and {postal_code} are filled from the tool arguments.
                http=HttpBinding(
                    "GET", "/{country}/{postal_code}", base_url="https://api.zippopotam.us"
                ),
            )
        ]

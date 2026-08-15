# Agent instructions
## Testing
To run the full test suite, target the tests directory:
uv run python -m pytest .\tests -q 2>&1
## User interaction
Do not use the AskUserQuestion tool, just ask the question in plain text.
## Developing Lua code in this project
All new Lua code should use the Lua template file structure:
- Add a Lua template file in src/civ_mcp/lua
- In a python file in src/civ_mcp/lua add a builder function that loads the lua using load_lua_template from civ_mcp.lua._helpers
- The python function can call replace to set values of template parameters for the Lua code

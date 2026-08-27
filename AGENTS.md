# Agent instructions
## Testing
To run the full test suite, target the tests directory, otherwise it will run other files with test in name that are not intended to be tests.
Powershell: uv run python -m pytest .\tests -q 2>&1
Or bash equivalent.
## User interaction
Do not use the AskUserQuestion tool, just ask the question in plain text.
## Developing Lua code in this project
All new Lua code should use the Lua template file structure:
- Add a Lua template file in src/civ_mcp/lua
- In a python file in src/civ_mcp/lua add a builder function that loads the lua using load_lua_template from civ_mcp.lua._helpers
- The python function can call replace to set values of template parameters for the Lua code
## How to read files
Bash and PowerShell tools for reading are disabled so use the file read tools like glob, grep and read instead.
## Note on sub agents and rate limits
We are running against the Z.ai backend and currently have a concurrency limit of 1, so we should not run parallel sub agents as this will likely cause them to hit a rate limit.
## Reading the Civ 6 source code Lua files
To develop code that interacts with the game engine you may need to look at the game's actual source code. The Civ 6 UI Lua files are in: 
C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\Base\Assets\UI

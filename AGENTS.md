# Agent instructions
## About the project
This is an mcp server that allows agents to connect to a live game of Civ 6 through the debugging interface in order to play the game. The main entry point is src/civ_mcp/server.py.
## Testing
To run the full test suite, target the tests directory, otherwise it will run other files with test in name that are not intended to be tests.
Powershell: uv run python -m pytest .\tests -q 2>&1
Or bash equivalent.
The evals/ directory is no longer maintained. Ignore it when checking references during refactors or migrations; tests/ is the live suite.
## User interaction
Do not use the AskUserQuestion tool, just ask the question in plain text. Its popup blocks the user from reading the chat.
## Editing files
Edit files with the built-in Edit/Write tools, even for large block replacements. Never use a Bash, PowerShell or Python script that performs the replacement: a script triggers a permission prompt where the user has to read code, while an Edit shows as a reviewable diff. Keep shell use for running things (tests, git).
## Changing files outside the workspace
Always type the complete literal absolute path. Never use env-var expansion (`$env:TEMP`), `~` or wildcards, because the user cannot see what they expand to. First show a read-only listing with literal paths, then change by spelled-out name.
## Memory and documentation
Do not use Claude auto-memory in this project; it is disabled because it is not transparent to the user. Record working instructions in this file, and durable facts about the project or game engine in docs/engine-and-project-notes.md.
## Engine and project notes
Read docs/engine-and-project-notes.md before working on Lua, diplomacy, combat outcomes, end-turn logic or the tuner connection. It records live-verified engine behavior, such as which Lua context has fresh state, the diplomacy session recipe and nil-returning APIs.
## General direction
This project previously had different modes but is now only focused on the handoff mode
## Developing Lua code in this project
All new Lua code should use the Lua template file structure, never Lua inlined as a Python f-string (the older inline builders are legacy):
- Add a Lua template file in src/civ_mcp/lua
- In a python file in src/civ_mcp/lua add a builder function that loads the lua using load_lua_template from civ_mcp.lua._helpers
- The python function can call replace to set values of template parameters for the Lua code
## How to read files
Bash and PowerShell tools for reading are disabled so use the file read tools like glob, grep and read instead.
## Reading the Civ 6 source code Lua files
To develop code that interacts with the game engine you may need to look at the game's actual source code. The Civ 6 files are in: 
C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\
with the Lua UI files in
C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\Base\Assets\UI
## Note on error handling
The previous owner of this project had a tendency to ignore errors and unexpected code behavior. Going forward we should try to always log errors and warnings instead.

"""Static checks on the Lua template files in ``src/civ_mcp/lua``.

Builders fill templates with ``str.replace``, which substitutes a
placeholder everywhere it appears — comments included. Value placeholders
(``__UNIT_ID__``, ``__MCP_SENTINEL_TAG__``, ...) expand to a single line and
are harmless in a comment, but snippet placeholders (``__LUA_*__``) expand
to multi-line Lua: named in a ``--`` comment, only the snippet's first line
stays commented and the rest lands as stray code, breaking the script with a
syntax error (this took down attack_unit.lua). No Lua runtime here, so the
rule is enforced on the template text.
"""

import re
from pathlib import Path

import pytest

import civ_mcp.lua

_TEMPLATE_DIR = Path(civ_mcp.lua.__file__).resolve().parent
_TEMPLATES = sorted(_TEMPLATE_DIR.glob("*.lua"))
_SNIPPET_TOKEN = re.compile(r"__LUA_[A-Z0-9_]+__")


def test_templates_found():
    assert _TEMPLATES, f"no .lua templates found in {_TEMPLATE_DIR}"


@pytest.mark.parametrize("template", _TEMPLATES, ids=lambda p: p.name)
def test_snippet_placeholders_stand_alone(template: Path):
    """A ``__LUA_*__`` snippet placeholder must be the only thing on its line.

    That rules out naming one in a comment (or anywhere mid-line), where the
    multi-line substitution would corrupt the script.
    """
    offenders = [
        f"{template.name}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(
            template.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _SNIPPET_TOKEN.search(line)
        and not _SNIPPET_TOKEN.fullmatch(line.strip())
    ]
    assert not offenders, (
        "snippet placeholders must stand alone on a line "
        "(write the name without its double underscores in comments):\n"
        + "\n".join(offenders)
    )

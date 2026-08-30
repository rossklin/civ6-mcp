"""Tests for command_executor param conversion (legacy names, accept strings)."""

from __future__ import annotations

from civ_mcp.command_executor import _convert_params


class TestConvertParams:
    def test_unit_id_passthrough(self):
        # unit_id is the engine's per-player unit ID — passed through verbatim.
        assert _convert_params({"unit_id": 5}) == {"unit_id": 5}

    def test_legacy_unit_index_alias(self):
        # Diary plans written before the unification used "unit_index" with
        # the same numeric value; it must still reach the method as unit_id.
        assert _convert_params({"unit_index": 5}) == {"unit_id": 5}

    def test_legacy_target_unit_index_alias(self):
        assert _convert_params({"target_unit_index": 7}) == {
            "target_unit_id": 7
        }

    def test_accept_string_coercion(self):
        assert _convert_params({"accept": "yes"}) == {"accept": True}
        assert _convert_params({"accept": "false"}) == {"accept": False}

    def test_other_params_untouched(self):
        params = {"target_x": 10, "target_y": 20, "improvement_name": "IMPROVEMENT_MINE"}
        assert _convert_params(params) == params

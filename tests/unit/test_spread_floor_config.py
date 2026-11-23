"""
Unit tests for Spread Floor configuration and Doppler unification.
Verifies that MIN_DYNAMIC_SPREAD_CENTS defaults to MIN_SPREAD when unset,
and respects explicit overrides without breaking fail-fast integer validation.
"""

import os
from unittest.mock import patch
import pytest

from config import _get_int_env


def test_spread_floor_inherits_min_spread_by_default():
    """When MIN_DYNAMIC_SPREAD_CENTS is not set, it inherits MIN_SPREAD."""
    with patch.dict(os.environ, {"MIN_SPREAD": "7"}, clear=True):
        min_spread = int(os.environ.get("MIN_SPREAD", "7"))
        dynamic_spread = _get_int_env("MIN_DYNAMIC_SPREAD_CENTS", min_spread)
        assert dynamic_spread == 7


def test_spread_floor_respects_explicit_override():
    """When MIN_DYNAMIC_SPREAD_CENTS is explicitly configured, it uses that value."""
    with patch.dict(os.environ, {"MIN_SPREAD": "7", "MIN_DYNAMIC_SPREAD_CENTS": "6"}):
        min_spread = int(os.environ.get("MIN_SPREAD", "7"))
        dynamic_spread = _get_int_env("MIN_DYNAMIC_SPREAD_CENTS", min_spread)
        assert dynamic_spread == 6


def test_int_env_fail_fast_on_decimals():
    """Enforces strict integer validation, rejecting float strings like '6.0'."""
    with pytest.raises(ValueError, match="must be a positive integer"):
        with patch.dict(os.environ, {"MIN_DYNAMIC_SPREAD_CENTS": "6.0"}):
            _get_int_env("MIN_DYNAMIC_SPREAD_CENTS", 7)

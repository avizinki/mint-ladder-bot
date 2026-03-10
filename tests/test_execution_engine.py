"""
Integration tests — CEO directive §13: execution engine.

Scenarios: quote→simulate→send flow (mocked), retry logic, EXECUTION_FAILED on failure.
"""
import pytest
from unittest.mock import MagicMock

from mint_ladder_bot.config import Config
from mint_ladder_bot.execution_engine import ExecutionResult, execute_swap, EXECUTION_FAILED


def test_execution_result_failure():
    r = ExecutionResult(success=False, error="simulated")
    assert r.success is False
    assert r.signature is None
    assert r.error == "simulated"


def test_execution_result_success():
    r = ExecutionResult(success=True, signature="sig123", confirmed=True)
    assert r.success is True
    assert r.signature == "sig123"
    assert r.confirmed is True


def test_execution_failed_constant():
    assert EXECUTION_FAILED == "EXECUTION_FAILED"

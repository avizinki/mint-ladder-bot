from __future__ import annotations

import json

import pytest

from mint_ladder_bot.history_checkpoint import (
    HistoryCheckpoint,
    HistoryPageEntry,
    advance_checkpoint,
    init_checkpoint_from_page,
    next_before_anchor,
)


def test_init_checkpoint_from_first_page_sets_oldest_slot():
    page = [
        HistoryPageEntry(signature="sig_new", slot=200),
        HistoryPageEntry(signature="sig_old", slot=100),
    ]
    cp = init_checkpoint_from_page(page)
    assert cp.earliest_signature == "sig_old"
    assert cp.earliest_slot == 100
    assert cp.exhausted is False


def test_init_checkpoint_from_empty_page_marks_exhausted():
    cp = init_checkpoint_from_page([])
    assert cp.earliest_signature is None
    assert cp.earliest_slot is None
    assert cp.exhausted is True


def test_advance_moves_checkpoint_backward_on_older_page():
    first_page = [
        HistoryPageEntry(signature="sig_2", slot=20),
        HistoryPageEntry(signature="sig_1", slot=10),
    ]
    cp1 = init_checkpoint_from_page(first_page)
    assert cp1.earliest_slot == 10

    older_page = [
        HistoryPageEntry(signature="sig_0", slot=5),
        HistoryPageEntry(signature="sig_x", slot=7),
    ]
    cp2 = advance_checkpoint(cp1, older_page)
    assert cp2.earliest_slot == 5
    assert cp2.earliest_signature == "sig_0"
    assert cp2.exhausted is False


def test_advance_is_idempotent_on_same_page():
    page = [HistoryPageEntry(signature="sig_1", slot=10)]
    cp1 = init_checkpoint_from_page(page)
    cp2 = advance_checkpoint(cp1, page)
    # No change expected.
    assert cp2 == cp1


def test_advance_raises_on_non_monotonic_newer_page():
    first_page = [HistoryPageEntry(signature="sig_old", slot=10)]
    cp1 = init_checkpoint_from_page(first_page)

    newer_page = [HistoryPageEntry(signature="sig_new", slot=20)]
    with pytest.raises(ValueError):
        advance_checkpoint(cp1, newer_page)


def test_exhausted_condition_stable_on_empty_pages():
    page = [HistoryPageEntry(signature="sig_1", slot=10)]
    cp1 = init_checkpoint_from_page(page)
    cp2 = advance_checkpoint(cp1, [])
    assert cp2.exhausted is True
    cp3 = advance_checkpoint(cp2, [])
    assert cp3.exhausted is True
    # Anchor should be None when exhausted.
    assert next_before_anchor(cp3) is None


def test_next_before_anchor_returns_earliest_signature_when_not_exhausted():
    page = [HistoryPageEntry(signature="sig_1", slot=10)]
    cp = init_checkpoint_from_page(page)
    assert next_before_anchor(cp) == "sig_1"


def test_checkpoint_determinism_for_same_input():
    page = [
        HistoryPageEntry(signature="sig_new", slot=200),
        HistoryPageEntry(signature="sig_old", slot=100),
    ]
    cp1 = init_checkpoint_from_page(page)
    cp2 = init_checkpoint_from_page(page)
    j1 = json.dumps(cp1.__dict__, sort_keys=True, default=str)
    j2 = json.dumps(cp2.__dict__, sort_keys=True, default=str)
    assert j1 == j2


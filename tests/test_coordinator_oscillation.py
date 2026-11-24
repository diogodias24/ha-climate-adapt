# SPDX-License-Identifier: Apache-2.0
"""Tests for climate mode oscillation bug."""
import pytest


@pytest.fixture(autouse=True)
def async_setup_test_fixture():
    class State:
        def __init__(self):
            self.called = False

    return State()


def test_bug_analysis():
    """Document the oscillation bug root cause."""

if __name__ == "__main__":
    test_bug_analysis(None)

# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Shared pytest fixtures for the emporia_hydro test suite."""

from pathlib import Path

import pytest

from emporia_hydro.rates import RatesConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config() -> RatesConfig:
    """Load the real repo config/rates.json for use as test ground truth."""
    return load_config(REPO_ROOT / "config")

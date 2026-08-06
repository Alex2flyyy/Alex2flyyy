"""Shared fixtures.

Tests run against the *real* config in `config/`, not a fake one. A test suite
that mocks away the vocabulary can't catch the failure that actually bites you:
somebody edits a YAML file and every video starts looking the same.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pawparty.config import load_channel, load_settings
from pawparty.ideas.concept import ConceptGenerator
from pawparty.ideas.novelty import NoveltyLedger
from pawparty.media import ffmpeg
from pawparty.storage import Workspace

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Real settings, but pointed at a throwaway workspace."""
    monkeypatch.setenv("PAWPARTY_WORKSPACE", str(tmp_path / "Videos"))
    return load_settings(PROJECT_ROOT / "config" / "settings.yaml")


@pytest.fixture
def channel(settings):
    return load_channel(settings, "kittens_puppies")


@pytest.fixture
def workspace(settings) -> Workspace:
    return Workspace.create(settings.workspace)


@pytest.fixture
def ledger(workspace) -> NoveltyLedger:
    return NoveltyLedger(workspace.novelty_db)


@pytest.fixture
def generator(settings, channel, ledger) -> ConceptGenerator:
    return ConceptGenerator(settings, channel, ledger)


@pytest.fixture
def concept(generator):
    return generator.generate_one(seed=12345)


def pytest_runtest_setup(item):
    """Skip ffmpeg-marked tests when ffmpeg isn't installed."""
    if item.get_closest_marker("ffmpeg") and not ffmpeg.is_available():
        pytest.skip("ffmpeg/ffprobe not available on PATH")


@pytest.fixture
def has_ffmpeg() -> bool:
    return ffmpeg.is_available()

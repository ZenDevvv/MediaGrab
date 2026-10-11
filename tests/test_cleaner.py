import os
import time
import pytest
from pathlib import Path
from app import config
from app.config import DOWNLOADS_DIR
from app.services.cleaner import purge_orphan_files, deferred_file_removal

# These exercise the deletion behaviour that desktop mode disables
# (§1.3 Decision #12), so retention is switched on explicitly here.
@pytest.fixture(autouse=True)
def _retention_enabled(monkeypatch):
    monkeypatch.setattr(config, "RETENTION_ENABLED", True)

@pytest.mark.asyncio
async def test_deferred_file_removal(tmp_path):
    test_file = tmp_path / "test_ephemeral.mp4"
    test_file.write_text("ephemeral video data")
    assert test_file.exists()

    # Deferred removal with 0.1s delay
    await deferred_file_removal(test_file, delay_seconds=0.1)
    assert not test_file.exists()

def test_purge_orphan_files():
    # Create one old file and one new file in DOWNLOADS_DIR
    old_file = DOWNLOADS_DIR / "old_orphan_test.mp4"
    new_file = DOWNLOADS_DIR / "new_active_test.mp4"

    old_file.write_text("old file")
    new_file.write_text("new file")

    # Set old_file mtime to 2 hours ago
    two_hours_ago = time.time() - 7200
    os.utime(old_file, (two_hours_ago, two_hours_ago))

    purge_orphan_files(max_age_seconds=1800)

    assert not old_file.exists()
    assert new_file.exists()

    # Clean up new_file
    new_file.unlink()

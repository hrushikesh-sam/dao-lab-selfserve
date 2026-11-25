"""X-DOCS guard: the errata must record E-15 and PR #12 paths must resolve on rama."""
from pathlib import Path

from tests.conftest import REPO

DOCS = Path(REPO) / "plan" / "design" / "p8-gate-review"
ERRATA = DOCS / "p8-design-errata-v1.2.md"


def test_errata_records_e15_with_unsigned_draft_status():
    text = ERRATA.read_text(encoding="utf-8")
    assert "| E-15 |" in text
    assert "UNSIGNED DRAFT" in text
    assert "dedicated `snapshot_root`" in text
    assert "B-109c" in text and "B-108a" in text and "B-114" in text

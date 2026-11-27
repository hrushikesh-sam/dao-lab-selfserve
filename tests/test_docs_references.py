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


DESIGN_REF = "origin/akshay-p8-gate:plan/design/p8-gate-review/p8-snapshot-design-v1.1.md"
BAD_REFS = ("~/notes/", "fils/", "p8-task-breakdown.md", "p11-snapshot-storage-capacity-research.md", "SD:308")


def test_pr12_docs_have_no_unresolvable_path_references():
    for name in ("p8-design-errata-v1.2.md", "E-15-storage-quota.md", "snapshot-design.md"):
        text = (DOCS / name).read_text(encoding="utf-8")
        for bad in BAD_REFS:
            assert bad not in text, f"{name}: unresolved reference {bad}"


def test_branch_only_design_paths_are_qualified():
    assert DESIGN_REF in ERRATA.read_text(encoding="utf-8")
    text = (DOCS / "snapshot-design.md").read_text(encoding="utf-8")
    assert DESIGN_REF in text
    assert "design v1.1 §1" not in text


def test_e15_uses_the_split_task_id():
    text = (DOCS / "E-15-storage-quota.md").read_text(encoding="utf-8")
    assert "- B-108a:" in text
    assert "- B-108:" not in text


def test_errata_authority_and_e15_status_are_unambiguous():
    text = ERRATA.read_text(encoding="utf-8")
    assert "- This file records the v1.2 decisions as signed off in `plan/design/p8-impl/B-104-DDL.md` §0, §7 and §11, with B-104 as the task authority." not in text
    assert "Transient root-only ciphertext staging on the DPU was decided in PR #11 on 24 Nov 2026 (Q-E15); the rest of E-15 remains UNSIGNED DRAFT." in text
    assert "is DECIDED (Q-E15). Owners:" not in text


def test_dao_doc_verification_refs_resolve():
    text = (Path(REPO) / "docs" / "dao-doc-verification.md").read_text(encoding="utf-8")
    assert "Clean reinstall ...md" not in text
    assert "Clean reinstall of dao-smart-nic on CN10K DPU (xeon3-oct-112).md" in text
    assert "dao-devel/applications/" in text
    assert "catalog.py:36-121" not in text and "service/catalog.py:33-127" in text
    assert "plan/07-usecase-tls-proxy.md:145" not in text
    assert "plan/07-usecase-tls-proxy.md:152" in text
    assert "service/catalog.py:73" not in text and "service/catalog.py:78" in text
    assert "tests/test_api.py:87" not in text and "tests/test_api.py:88" in text

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from datamind_console.common import research_queue as rq


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def queue_root(tmp_path: Path) -> Path:
    """A fresh research_queue layout under tmp_path."""
    for sub in ("pending", "sent", "responses", "ingested", "archive"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _base_prompt_kwargs(**overrides):
    base = dict(
        prompt_type="stop_grounding_detail",
        route_code="NE-03",
        unit="dmq_quito_norte",
        province="sample_region",
        trigger_condition="Phase 3 returned grounded_stops=0",
        priority=1,
        estimated_research_budget="standard",
        depends_on=[],
        dedup_key="stop_grounding_detail:dmq_quito_norte:NE-03:grounded_stops_zero",
        prompt_markdown_content="# Prompt body\n\nResearch X.\n",
        generated_by_skill="06c_DEEP_RESEARCH_STOP_GROUNDING",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. write_prompt: creates file + returns path
# ---------------------------------------------------------------------------


def test_write_prompt_creates_file_in_pending(queue_root):
    path = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())

    assert path.exists()
    assert path.parent == queue_root / "pending"
    # filename pattern: {priority}_{prompt_type}_{unit}_{route_code}_{timestamp}.md
    name = path.name
    assert name.startswith("01_stop_grounding_detail_dmq_quito_norte_ne-03_")
    assert name.endswith(".md")

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    # All nine required frontmatter fields present
    for key in (
        "prompt_type:",
        "route_code:",
        "unit:",
        "province:",
        "trigger_condition:",
        "priority:",
        "generated_at:",
        "generated_by_skill:",
        "estimated_research_budget:",
        "depends_on:",
        "dedup_key:",
    ):
        assert key in text, f"missing frontmatter key: {key}"
    # body appears after the closing frontmatter fence
    assert "# Prompt body" in text


def test_write_prompt_unit_scoped_uses_ALL_in_filename(queue_root):
    path = rq.write_prompt(
        queue_root=queue_root,
        **_base_prompt_kwargs(
            prompt_type="exhaustive_route_inventory_rerun",
            route_code=None,
            unit="ruminahui",
            province="sample_region",
            trigger_condition="Unit-scoped rerun",
            priority=1,
            dedup_key="exhaustive_route_inventory_rerun:ruminahui:ALL:manual",
        ),
    )
    assert "_ALL_" in path.name
    text = path.read_text(encoding="utf-8")
    assert "route_code: null" in text or 'route_code: ~' in text


# ---------------------------------------------------------------------------
# 2. duplicate dedup_key returns existing path without overwriting
# ---------------------------------------------------------------------------


def test_write_prompt_dedup_returns_existing_without_overwrite(queue_root):
    first = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    original_bytes = first.read_bytes()

    # second call with same dedup_key and different body
    second = rq.write_prompt(
        queue_root=queue_root,
        **_base_prompt_kwargs(prompt_markdown_content="# DIFFERENT body\n"),
    )

    assert second == first
    # file not overwritten
    assert first.read_bytes() == original_bytes
    # only one file in pending/
    assert len(list((queue_root / "pending").glob("*.md"))) == 1


def test_write_prompt_dedup_scans_sent_too(queue_root):
    first = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    # operator moves to sent/
    moved = queue_root / "sent" / first.name
    first.rename(moved)

    second = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    assert second == moved
    # no new file created in pending/
    assert not list((queue_root / "pending").glob("*.md"))


def test_write_prompt_dedup_does_not_scan_ingested_or_archive(queue_root):
    first = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    (queue_root / "ingested" / first.name).write_bytes(first.read_bytes())
    first.unlink()

    second = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    # new file written because ingested/ is out of scope
    assert second.parent == queue_root / "pending"
    assert second.exists()


def test_write_prompt_supersede_moves_existing_to_archive(queue_root):
    first = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs(priority=2))
    original_name = first.name

    second = rq.write_prompt(
        queue_root=queue_root,
        supersede=True,
        **_base_prompt_kwargs(priority=0, prompt_markdown_content="# new body\n"),
    )

    # original moved to archive/ under its old name
    assert not first.exists()
    assert (queue_root / "archive" / original_name).exists()
    # new file written in pending/
    assert second.parent == queue_root / "pending"
    assert second != first
    # superseded.log recorded a line
    log = (queue_root / "archive" / "superseded.log").read_text(encoding="utf-8")
    assert original_name in log
    assert second.name in log


# ---------------------------------------------------------------------------
# 3. list_pending filter combinations
# ---------------------------------------------------------------------------


def test_list_pending_no_filter_returns_all(queue_root):
    rq.write_prompt(
        queue_root=queue_root,
        **_base_prompt_kwargs(
            dedup_key="a", prompt_type="stop_grounding_detail", route_code="A-1"
        ),
    )
    rq.write_prompt(
        queue_root=queue_root,
        **_base_prompt_kwargs(
            dedup_key="b", prompt_type="schedules_operations", route_code="A-2"
        ),
    )
    items = rq.list_pending(queue_root=queue_root)
    assert len(items) == 2


def test_list_pending_filters_by_priority_type_and_unit(queue_root):
    rq.write_prompt(
        queue_root=queue_root,
        **_base_prompt_kwargs(
            dedup_key="a",
            priority=0,
            prompt_type="stop_grounding_detail",
            unit="ruminahui",
            route_code="A-1",
        ),
    )
    rq.write_prompt(
        queue_root=queue_root,
        **_base_prompt_kwargs(
            dedup_key="b",
            priority=2,
            prompt_type="stop_grounding_detail",
            unit="ruminahui",
            route_code="A-2",
        ),
    )
    rq.write_prompt(
        queue_root=queue_root,
        **_base_prompt_kwargs(
            dedup_key="c",
            priority=0,
            prompt_type="schedules_operations",
            unit="dmq_quito_norte",
            route_code="A-3",
        ),
    )

    only_p0 = rq.list_pending(queue_root=queue_root, priority=0)
    assert len(only_p0) == 2

    only_grounding_p0 = rq.list_pending(
        queue_root=queue_root, priority=0, prompt_type="stop_grounding_detail"
    )
    assert len(only_grounding_p0) == 1

    ruminahui = rq.list_pending(queue_root=queue_root, unit="ruminahui")
    assert len(ruminahui) == 2


# ---------------------------------------------------------------------------
# 4. move_to_sent moves + records sent_at
# ---------------------------------------------------------------------------


def test_move_to_sent_moves_file_and_updates_sent_at(queue_root):
    p = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    moved = rq.move_to_sent(queue_root=queue_root, filename=p.name)

    assert not p.exists()
    assert moved == queue_root / "sent" / p.name
    text = moved.read_text(encoding="utf-8")
    assert "sent_at:" in text


def test_move_to_sent_raises_when_file_missing(queue_root):
    with pytest.raises(FileNotFoundError):
        rq.move_to_sent(queue_root=queue_root, filename="does-not-exist.md")


# ---------------------------------------------------------------------------
# 5. match_response_to_prompt: success + raises on no match
# ---------------------------------------------------------------------------


def test_match_response_to_prompt_finds_sent_pair(queue_root):
    p = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    sent_path = rq.move_to_sent(queue_root=queue_root, filename=p.name)

    # response lands in responses/ with matching base name
    response_name = sent_path.stem + ".json"
    (queue_root / "responses" / response_name).write_text("{}", encoding="utf-8")

    matched = rq.match_response_to_prompt(
        queue_root=queue_root, response_filename=response_name
    )
    assert matched == sent_path


def test_match_response_to_prompt_raises_when_no_prompt(queue_root):
    (queue_root / "responses" / "orphan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(LookupError):
        rq.match_response_to_prompt(
            queue_root=queue_root, response_filename="orphan.json"
        )


# ---------------------------------------------------------------------------
# 6. ingest_and_pair_move: atomic paired move + writes sidecar
# ---------------------------------------------------------------------------


def test_ingest_and_pair_move_moves_both_and_writes_sidecar(queue_root):
    p = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    sent_path = rq.move_to_sent(queue_root=queue_root, filename=p.name)
    response_name = sent_path.stem + ".json"
    response_path = queue_root / "responses" / response_name
    response_path.write_text(json.dumps({"stops": []}), encoding="utf-8")

    result = {"status": "ok", "fields_merged": ["stops"], "warnings": []}
    rq.ingest_and_pair_move(
        queue_root=queue_root,
        response_filename=response_name,
        ingestion_result=result,
    )

    # both moved
    assert not sent_path.exists()
    assert not response_path.exists()
    assert (queue_root / "ingested" / sent_path.name).exists()
    assert (queue_root / "ingested" / response_name).exists()

    # sidecar next to moved prompt
    sidecar = queue_root / "ingested" / (sent_path.stem + ".ingest.json")
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == result


# ---------------------------------------------------------------------------
# 7. partial failure rolls back cleanly
# ---------------------------------------------------------------------------


def test_ingest_and_pair_move_rolls_back_when_response_missing(queue_root):
    p = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    sent_path = rq.move_to_sent(queue_root=queue_root, filename=p.name)

    # no matching response in responses/
    with pytest.raises((FileNotFoundError, LookupError)):
        rq.ingest_and_pair_move(
            queue_root=queue_root,
            response_filename=sent_path.stem + ".json",
            ingestion_result={"status": "ok"},
        )

    # sent prompt unmoved; no sidecar left behind
    assert sent_path.exists()
    assert not list((queue_root / "ingested").glob("*"))


def test_ingest_and_pair_move_rolls_back_on_mid_move_failure(queue_root, monkeypatch):
    """Response moves into ingested/, prompt move then fails → response
    returns to responses/, sidecar cleaned up, no half-moved state."""
    p = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    sent_path = rq.move_to_sent(queue_root=queue_root, filename=p.name)
    response_name = sent_path.stem + ".json"
    response_path = queue_root / "responses" / response_name
    response_path.write_text(json.dumps({"stops": []}), encoding="utf-8")

    import shutil as _shutil

    call_count = {"n": 0}
    real_move = _shutil.move

    def flaky_move(src, dst, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:  # second move = prompt → ingested/
            raise OSError("simulated filesystem failure moving prompt")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(rq.shutil, "move", flaky_move)

    with pytest.raises(OSError, match="simulated filesystem failure"):
        rq.ingest_and_pair_move(
            queue_root=queue_root,
            response_filename=response_name,
            ingestion_result={"status": "ok"},
        )

    # response rolled back to responses/
    assert response_path.exists(), "response must have been returned to responses/"
    # prompt never moved
    assert sent_path.exists(), "prompt must still be in sent/"
    # ingested/ is empty — no half-moved files, no sidecar
    assert not list((queue_root / "ingested").glob("*")), \
        f"ingested/ should be empty, found: {list((queue_root / 'ingested').glob('*'))}"


# ---------------------------------------------------------------------------
# 8. archive_orphan_response creates dated subfolder
# ---------------------------------------------------------------------------


def test_archive_orphan_response_moves_to_dated_subfolder(queue_root):
    orphan = queue_root / "responses" / "orphan.json"
    orphan.write_text("{}", encoding="utf-8")

    moved = rq.archive_orphan_response(
        queue_root=queue_root,
        response_filename="orphan.json",
        reason="no matching prompt",
    )

    assert not orphan.exists()
    # path is under archive/{date}_orphans/
    assert moved.parent.parent == queue_root / "archive"
    assert moved.parent.name.endswith("_orphans")
    assert moved.name == "orphan.json"
    # error sidecar recorded reason
    sidecar = moved.parent / (moved.stem + ".error.txt")
    assert sidecar.exists()
    assert "no matching prompt" in sidecar.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 9. frontmatter parsing round-trip
# ---------------------------------------------------------------------------


def test_write_prompt_frontmatter_roundtrip(queue_root):
    p = rq.write_prompt(queue_root=queue_root, **_base_prompt_kwargs())
    fm = rq._parse_frontmatter(p.read_text(encoding="utf-8"))

    assert fm["prompt_type"] == "stop_grounding_detail"
    assert fm["route_code"] == "NE-03"
    assert fm["unit"] == "dmq_quito_norte"
    assert fm["province"] == "sample_region"
    assert fm["priority"] == 1
    assert fm["depends_on"] == []
    assert (
        fm["dedup_key"]
        == "stop_grounding_detail:dmq_quito_norte:NE-03:grounded_stops_zero"
    )


# ---------------------------------------------------------------------------
# 10. extra_frontmatter: priority_bump + validation
# ---------------------------------------------------------------------------


def test_write_prompt_extra_frontmatter_renders_after_standard_fields(queue_root):
    p = rq.write_prompt(
        queue_root=queue_root,
        extra_frontmatter={"priority_bump": True},
        **_base_prompt_kwargs(),
    )
    text = p.read_text(encoding="utf-8")
    assert "priority_bump: true" in text
    # standard fields still parse cleanly
    fm = rq._parse_frontmatter(text)
    assert fm["priority_bump"] is True
    assert fm["prompt_type"] == "stop_grounding_detail"
    # extra key appears after standard 11 fields
    dedup_idx = text.index("dedup_key:")
    bump_idx = text.index("priority_bump:")
    assert bump_idx > dedup_idx


def test_write_prompt_extra_frontmatter_rejects_non_snake_case_key(queue_root):
    with pytest.raises(ValueError, match="snake_case"):
        rq.write_prompt(
            queue_root=queue_root,
            extra_frontmatter={"PriorityBump": True},
            **_base_prompt_kwargs(),
        )
    with pytest.raises(ValueError, match="snake_case"):
        rq.write_prompt(
            queue_root=queue_root,
            extra_frontmatter={"priority-bump": True},
            **_base_prompt_kwargs(dedup_key="alt_key_1"),
        )


def test_write_prompt_extra_frontmatter_rejects_non_json_value(queue_root):
    class _Weird:
        pass

    with pytest.raises(ValueError, match="JSON-serializable"):
        rq.write_prompt(
            queue_root=queue_root,
            extra_frontmatter={"bad": _Weird()},
            **_base_prompt_kwargs(),
        )
    # collision with standard field also rejected
    with pytest.raises(ValueError, match="collides"):
        rq.write_prompt(
            queue_root=queue_root,
            extra_frontmatter={"priority": 9},
            **_base_prompt_kwargs(dedup_key="alt_key_2"),
        )

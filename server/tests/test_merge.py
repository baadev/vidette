"""The merge pass (ADR-0018): conservative, audited, reversible, respectful of decisions.

Faces are the fake engine's vectors (`fakes/faces.py`): two variants of one fake identity
score about 0.75, two identities about 0. No picture of a person anywhere.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fakes.faces import identity_vector

from vidette.core.config import VidetteConfig
from vidette.people.identities import (
    GroupFace,
    IdentityError,
    IdentityStore,
    MergeCandidate,
    SightingContext,
)
from vidette.people.merge import (
    CLEAR,
    SAME_MOMENT,
    MergeSettings,
    Planner,
    apply_plan,
    merge_pass,
    plan_merges,
)
from vidette.people.setup import merge_settings_for
from vidette.people.vectors import normalize

DAY = 86400.0
T0 = datetime(2026, 11, 14, 10, 0).timestamp()


def face(who: str, variant: str, px: int = 100) -> GroupFace:
    return GroupFace(identity_vector(who, variant), px, 0.95, f"crop:{who}/{variant}".encode())


def at(seconds: float, camera: str = "street", clip: str | None = None) -> SightingContext:
    name = clip or f"c{seconds:g}"
    return SightingContext(
        at=T0 + seconds, camera=camera, source="clip-folder", source_id=name,
        clip=f"/data/{name}.mp4",
    )  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> IdentityStore:
    return IdentityStore(tmp_path / "identities", clock=lambda: T0 + DAY)


def new_weak(store: IdentityStore, who: str, variant: str, ctx: SightingContext) -> int:
    result = store.assign([face(who, variant, px=24)], ctx, weak=True)
    assert result.brief is not None and result.reason == "new weak"
    return result.brief.id


def new_clear(store: IdentityStore, vector: tuple[float, ...], ctx: SightingContext) -> int:
    """A clear identity that live matching would not join (it forces a new one)."""
    with store._write() as conn:
        identity = store._new_identity(conn)
        store._add_sighting(conn, identity, [GroupFace(vector, 100, 0.95, b"c")],
                            [GroupFace(vector, 100, 0.95, b"c")], ctx)  # fmt: skip
    return identity


def mix(a: tuple[float, ...], b: tuple[float, ...], weight: float) -> tuple[float, ...]:
    """A unit vector whose cosine with `a` is about `weight` when a ⟂ b."""
    w2 = (1 - weight * weight) ** 0.5
    return normalize(weight * x + w2 * y for x, y in zip(a, b, strict=True))


def basis(n: int) -> list[tuple[float, ...]]:
    return [tuple(1.0 if i == k else 0.0 for i in range(128)) for k in range(n)]


# --- the same moment ---------------------------------------------------------------------


def test_one_face_cut_into_two_clips_is_merged_and_the_merge_is_undone_exactly(
    store: IdentityStore,
) -> None:
    a = new_weak(store, "dave", "1", at(0, clip="early"))
    b = new_weak(store, "dave", "1", at(4, clip="whole"))  # the same pixels, 4 s later
    other = new_weak(store, "erin", "1", at(10, clip="third"))
    before = sorted((s.id, s.identity) for i in (a, b, other) for s in store.sightings(i))

    plan = plan_merges(store)
    assert [(g.target, g.members) for g in plan.groups] == [(a, [a, b])]
    assert plan.groups[0].links[0].rule == SAME_MOMENT
    applied = apply_plan(store, plan, run="r1")
    assert applied.merged == 1 and applied.backup is not None
    assert Path(applied.backup).stat().st_mode & 0o077 == 0
    assert store.get(b) is None and store.require(a).sightings == 2
    assert store.resolve(b) == a  # an old alert's number still leads somewhere
    [row] = store.merge_log()
    assert (row.source, row.target, row.rule, row.sightings) == (b, a, SAME_MOMENT, 1)

    undone = store.undo_run("r1")
    assert undone == [(b, 1)]
    after = sorted((s.id, s.identity) for i in (a, b, other) for s in store.sightings(i))
    assert after == before and store.resolve(b) == b
    assert plan_merges(store).groups == []  # taken back: never merged again
    with pytest.raises(IdentityError, match="undone already"):
        store.unmerge(row.id)


def test_the_same_face_hours_apart_or_on_another_camera_is_not_a_same_moment(
    store: IdentityStore,
) -> None:
    new_weak(store, "dave", "1", at(0, clip="a"))
    new_weak(store, "dave", "1", at(3 * 3600, clip="b"))  # a static "face", hours later
    new_weak(store, "dave", "1", at(30, camera="yard", clip="c"))
    assert plan_merges(store).groups == []


def test_two_faces_in_one_clip_are_two_people(store: IdentityStore) -> None:
    ctx = at(0, clip="one")
    a = new_weak(store, "dave", "1", ctx)
    with store._write() as conn:  # a second group of the same clip, as the check makes them
        b = store._new_identity(conn)
        store._add_sighting(conn, b, [face("dave", "1", 24)], [face("dave", "1", 24)], ctx,
                            weak=True)  # fmt: skip
    plan = plan_merges(store)
    assert plan.groups == [] and plan.blocked[f"{SAME_MOMENT}: seen in one clip"] == 1
    with pytest.raises(IdentityError, match="one clip"):
        store.auto_merge(b, a, rule=SAME_MOMENT, score=1.0, run="x")


# --- clear faces -------------------------------------------------------------------------


def test_clear_faces_merge_only_when_unambiguous(store: IdentityStore) -> None:
    e = basis(4)
    a = new_clear(store, e[0], at(0, clip="a"))
    b = new_clear(store, mix(e[0], e[1], 0.6), at(7200, clip="b"))  # 0.6: one person
    c = new_clear(store, e[2], at(9000, clip="c"))  # someone else
    plan = plan_merges(store)
    assert [(g.target, sorted(g.members)) for g in plan.groups] == [(a, [a, b])]
    assert plan.groups[0].links[0].rule == CLEAR
    assert c not in plan.groups[0].members

    # a third identity as close to `a` as `b` is: ambiguous, nothing merges
    new_clear(store, mix(e[0], e[3], 0.58), at(10000, clip="d"))
    plan = plan_merges(store)
    assert plan.groups == []
    assert plan.blocked[f"{CLEAR}: another candidate within the margin"] >= 1


def test_complete_linkage_never_chains_look_alikes(store: IdentityStore) -> None:
    e = basis(3)
    a = new_clear(store, e[0], at(0, clip="a"))
    b = new_clear(store, normalize(x + y for x, y in zip(e[0], e[1], strict=True)),
                  at(3600, clip="b"))  # fmt: skip
    c = new_clear(store, e[1], at(7200, clip="c"))
    # a~b 0.71, b~c 0.71, a~c 0: one merge at most, never all three
    settings = MergeSettings(margin=0.0)
    plan = plan_merges(store, settings)
    assert all(len(g.members) == 2 for g in plan.groups)
    assert not any({a, c} <= set(g.members) for g in plan.groups)
    assert b in plan.groups[0].members


# --- decisions are a person's ------------------------------------------------------------


def test_named_marked_and_known_identities_are_never_moved_and_keep_their_status(
    store: IdentityStore,
) -> None:
    e = basis(2)
    marked = new_clear(store, e[0], at(0, clip="a"))
    store.set_status(marked, "suspicious")
    store.rename(marked, "Gatecrasher")
    plain = new_clear(store, mix(e[0], e[1], 0.62), at(3600, clip="b"))
    plan = plan_merges(store)
    assert [(g.target, g.members) for g in plan.groups] == [(marked, [marked, plain])]
    apply_plan(store, plan, run="r", backup=False)
    row = store.require(marked)
    assert (row.status, row.name, row.sightings) == ("suspicious", "Gatecrasher", 2)
    third = new_clear(store, e[1], at(9000, clip="c"))
    with pytest.raises(IdentityError, match="named or marked"):
        store.auto_merge(marked, third, rule=CLEAR, score=0.9, run="x")


def test_two_marked_identities_or_a_known_one_are_never_merged(store: IdentityStore) -> None:
    e = basis(2)
    one = new_clear(store, e[0], at(0, clip="a"))
    two = new_clear(store, mix(e[0], e[1], 0.7), at(3600, clip="b"))
    store.set_status(one, "familiar")
    store.rename(two, "Neighbour")
    plan = plan_merges(store)
    assert plan.groups == [] and plan.blocked[f"{CLEAR}: both named or marked"] == 1
    store.mark_known(one, "sam", "Sam")
    plain = new_clear(store, e[1], at(9000, clip="c"))
    with pytest.raises(IdentityError, match="known person"):
        store.auto_merge(plain, one, rule=CLEAR, score=0.9, run="x")


def test_a_split_is_respected_by_later_passes(store: IdentityStore) -> None:
    e = basis(2)
    a = new_clear(store, e[0], at(0, clip="a"))
    b = new_clear(store, mix(e[0], e[1], 0.65), at(3600, clip="b"))
    apply_plan(store, plan_merges(store), run="r", backup=False)
    [moved] = [s.id for s in store.sightings(a) if s.source_id == "b"]
    store.split(a, [moved])  # a person: that one is someone else
    plan = plan_merges(store)
    assert plan.groups == [] and plan.blocked[f"{CLEAR}: kept apart by a person"] == 1
    del b


def test_probably_household_and_strangers_are_never_mixed(store: IdentityStore) -> None:
    a = store.assign([face("dave", "1", 24)], at(0, clip="a"), weak=True, known_score=0.35)
    b = new_weak(store, "dave", "1", at(5, clip="b"))
    assert a.brief is not None and a.brief.near_known
    plan = plan_merges(store)
    assert plan.groups == []
    assert plan.blocked[f"{SAME_MOMENT}: only one is close to someone enrolled"] == 1
    del b


# --- the pass as a whole -----------------------------------------------------------------


def test_merge_pass_audits_and_a_second_pass_finds_nothing(store: IdentityStore) -> None:
    new_weak(store, "dave", "1", at(0, clip="a"))
    new_weak(store, "dave", "1", at(5, clip="b"))
    plan, applied = merge_pass(store, clock=lambda: T0)
    assert applied.merged == 1 and len(plan.groups) == 1
    assert list((store.root / "backups").glob("identities-*.db"))
    _plan, again = merge_pass(store, clock=lambda: T0 + 60)
    assert again.merged == 0 and again.backup is None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM merges").fetchone()[0] == 1


def test_expired_identities_take_their_audit_rows_along(store: IdentityStore) -> None:
    a = new_weak(store, "dave", "1", at(0, clip="a"))
    new_weak(store, "dave", "1", at(5, clip="b"))
    merge_pass(store, backup=False)
    assert len(store.merge_log()) == 1
    store.delete(a)
    assert store.merge_log() == []


def test_planner_on_plain_candidates_needs_no_store() -> None:
    e = basis(2)
    candidates = [
        MergeCandidate(1, "unknown", False, False, [(1, T0, "street", "s:a")], [], [e[0]]),
        MergeCandidate(2, "unknown", False, False, [(2, T0 + 30, "street", "s:b")], [], [e[0]]),
    ]
    plan = Planner(candidates, set()).plan()
    assert [(g.target, g.members) for g in plan.groups] == [(1, [1, 2])]
    plan = Planner(candidates, {(1, 2)}).plan()  # sightings kept apart
    assert plan.groups == []


def test_config_defaults_and_validation() -> None:
    config = VidetteConfig()
    settings = merge_settings_for(config.people)
    assert (settings.clear, settings.same_moment, settings.same_moment_within_s) == (
        0.5, 0.85, 600.0,
    )  # fmt: skip
    assert config.people.identities.merge.auto is True
    with pytest.raises(ValueError, match="clear_floor"):
        VidetteConfig.model_validate(
            {"people": {"identities": {"merge": {"clear": 0.5, "clear_floor": 0.6}}}}
        )

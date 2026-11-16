"""The merge pass: identities that are one person, found after the fact (ADR-0018).

Live matching (vidette.people.identities) decides once, when a face arrives, against the
identities that exist at that moment. It splits people for reasons that only show later:
the same clip cut twice (an early window and the whole clip, overlapping clips of a
continuous camera), a face too blurred to join anything, an identity that only got a
clear face after the stranger's first pass. The merge pass looks at the whole store and
joins what is certainly one person, with two rules:

- **same moment**: two identities with faces at least `same_moment` (0.85) alike, on one
  camera, within `same_moment_within` (10 min) of each other. Unclear faces count here:
  the same pixels cut into two clips score 1.0, while different people seen at the same
  time stayed at or below 0.74 on real footage.
- **clear faces**: identities with clear faces whose best pair scores `clear` (0.5, the
  live join threshold) or more, no pair of their clear faces below `clear_floor` (0.4),
  and each the other's closest by `margin` (0.06) over the next candidate.

Groups grow by complete linkage (every member with every other, never a chain of
look-alikes) and never join two identities that:

- were seen in one clip (two faces in one clip are two people, or one person the check
  could not follow, which a person can merge by hand);
- hold sightings a person kept apart (a split, an undone merge);
- were both named or marked by a person (two decisions are a person's to reconcile);
- differ in being close to someone enrolled (that marks a probable household member: a
  merge must not make a stranger quiet, nor a household member a recurring stranger);
- include a known person (promoted identities are matched as that person, not here).

A group keeps the number of its marked member, else of the one seen most (then the
oldest), and that identity's name and status; merges never move a named or marked
identity away. Every merge is written to an audit with what it takes to undo it
(`IdentityStore.unmerge`, `undo_run`). Unclear faces at other times or on other cameras
are never merged: 20-pixel faces of different people score as high as one person's, and
a merge invents visits. Appearance is never used, only face embeddings, time, camera.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from vidette.people.identities import IdentityError, IdentityStore, MergeCandidate
from vidette.people.vectors import Vector, cosine

logger = logging.getLogger(__name__)

CLEAR = "clear"
SAME_MOMENT = "same_moment"
EDGES = (0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)


@dataclass(frozen=True)
class MergeSettings:
    clear: float = 0.5  # best clear-face cosine (the live join threshold)
    clear_floor: float = 0.4  # every clear pair across the two groups at least this
    margin: float = 0.06  # over the next-closest candidate, both ways
    same_moment: float = 0.85  # any faces, one camera, close in time
    same_moment_within_s: float = 600.0


@dataclass(frozen=True)
class Link:
    a: int
    b: int
    score: float
    rule: str


@dataclass
class Group:
    target: int  # the identity the others go into
    members: list[int]  # target first
    links: list[Link]  # the links that justified each join, in order


@dataclass
class Plan:
    groups: list[Group]
    considered: int  # identities the pass looked at
    clear_scores: list[float] = field(default_factory=list)  # best clear pair, every pair
    moment_scores: list[float] = field(default_factory=list)  # any faces, one camera, close
    blocked: Counter[str] = field(default_factory=Counter)  # why a qualifying link was not used

    @property
    def merges(self) -> int:
        return sum(len(g.members) - 1 for g in self.groups)

    def histogram(self, scores: list[float]) -> str:
        """Counts at or above each edge: what a threshold there would let through."""
        return " ".join(f">={e:g}:{sum(1 for s in scores if s >= e)}" for e in EDGES)


@dataclass
class _Cluster:
    members: list[int]
    links: list[Link] = field(default_factory=list)


def _matrix(vectors: dict[int, list[Vector]]) -> dict[tuple[int, int], float]:
    """Best cosine of any face of a with any face of b, for every pair with faces."""
    ids = sorted(i for i, refs in vectors.items() if refs)
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - the people extra brings numpy
        pairs: dict[tuple[int, int], float] = {}
        for x, a in enumerate(ids):
            for b in ids[x + 1 :]:
                pairs[(a, b)] = max(cosine(p, q) for p in vectors[a] for q in vectors[b])
        return pairs
    if not ids:
        return {}
    data = np.array([v for i in ids for v in vectors[i]], dtype=np.float32)
    sims = data @ data.T
    bounds: dict[int, tuple[int, int]] = {}
    start = 0
    for i in ids:
        bounds[i] = (start, start + len(vectors[i]))
        start += len(vectors[i])
    out: dict[tuple[int, int], float] = {}
    for x, a in enumerate(ids):
        a0, a1 = bounds[a]
        rows = sims[a0:a1]
        for b in ids[x + 1 :]:
            b0, b1 = bounds[b]
            out[(a, b)] = float(rows[:, b0:b1].max())
    return out


def _key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


class Planner:
    """Finds the groups; changes nothing."""

    def __init__(
        self,
        candidates: list[MergeCandidate],
        apart: set[tuple[int, int]],
        settings: MergeSettings | None = None,
    ) -> None:
        self.settings = settings or MergeSettings()
        self.by_id = {c.id: c for c in candidates}
        self.clear = _matrix({c.id: c.clear for c in candidates})
        self.every = _matrix({c.id: c.every for c in candidates})
        owner = {s[0]: c.id for c in candidates for s in c.sightings}
        self.apart = {_key(owner[a], owner[b]) for a, b in apart if a in owner and b in owner}
        clips: dict[str, set[int]] = {}
        for c in candidates:
            for _sid, _at, _camera, clip in c.sightings:
                clips.setdefault(clip, set()).add(c.id)
        self.shared = {_key(a, b) for ids in clips.values() for a in ids for b in ids if a < b}

    # --- facts about pairs ---------------------------------------------------------------

    def marked(self, identity: int) -> bool:
        c = self.by_id[identity]
        return c.status != "unknown" or c.named

    def why_not(self, a: int, b: int) -> str | None:
        """Why a and b may never be one identity, or None."""
        key = _key(a, b)
        if key in self.shared:
            return "seen in one clip"
        if key in self.apart:
            return "kept apart by a person"
        if self.marked(a) and self.marked(b):
            return "both named or marked"
        if self.by_id[a].near_known != self.by_id[b].near_known:
            return "only one is close to someone enrolled"
        return None

    def moment(self, a: int, b: int) -> float | None:
        """The any-face score when a and b were on one camera within the window."""
        within = self.settings.same_moment_within_s
        close = any(
            ca == cb and abs(ta - tb) <= within
            for _s, ta, ca, _c in self.by_id[a].sightings
            for _t, tb, cb, _d in self.by_id[b].sightings
        )
        return self.every.get(_key(a, b)) if close else None

    def _can_join(self, x: _Cluster, y: _Cluster) -> str | None:
        for a in x.members:
            for b in y.members:
                why = self.why_not(a, b)
                if why is not None:
                    return why
        if sum(1 for m in [*x.members, *y.members] if self.marked(m)) > 1:
            return "both named or marked"
        return None

    # --- stage 1: the same moment ---------------------------------------------------------

    def _same_moment(self, plan: Plan) -> list[_Cluster]:
        threshold = self.settings.same_moment
        moments: dict[tuple[int, int], float] = {}
        ids = sorted(self.by_id)
        for n, a in enumerate(ids):
            for b in ids[n + 1 :]:
                score = self.moment(a, b)
                if score is not None:
                    moments[(a, b)] = score
        plan.moment_scores = sorted(moments.values(), reverse=True)
        clusters = {i: _Cluster([i]) for i in ids}
        for (a, b), score in sorted(moments.items(), key=lambda kv: -kv[1]):
            if score < threshold:
                break
            x, y = clusters[a], clusters[b]
            if x is y:
                continue
            why = self._can_join(x, y)
            if why is None and not all(
                moments.get(_key(p, q), -1.0) >= threshold for p in x.members for q in y.members
            ):
                why = "not every face alike (complete linkage)"
            if why is not None:
                plan.blocked[f"{SAME_MOMENT}: {why}"] += 1
                continue
            merged = _Cluster([*x.members, *y.members], [*x.links, *y.links])
            merged.links.append(Link(a, b, score, SAME_MOMENT))
            for member in merged.members:
                clusters[member] = merged
        return list({id(c): c for c in clusters.values()}.values())

    # --- stage 2: clear faces -------------------------------------------------------------

    def _clear_pair(self, x: _Cluster, y: _Cluster) -> tuple[float, float, Link] | None:
        """(best, worst, the best pair's link) over clear faces across the two clusters;
        None when either has no clear face."""
        found = [
            (self.clear[_key(a, b)], a, b)
            for a in x.members
            for b in y.members
            if _key(a, b) in self.clear
        ]
        if not found:
            return None
        top, a, b = max(found)
        return top, min(s for s, _a, _b in found), Link(a, b, top, CLEAR)

    def _clear_faces(self, clusters: list[_Cluster], plan: Plan) -> list[_Cluster]:
        settings = self.settings
        plan.clear_scores = sorted(self.clear.values(), reverse=True)
        while True:
            with_faces = [c for c in clusters if any(self.by_id[m].clear for m in c.members)]
            near: dict[int, list[tuple[float, int]]] = {}
            pairs: list[tuple[float, float, int, int, Link]] = []
            for i, x in enumerate(with_faces):
                for j in range(i + 1, len(with_faces)):
                    found = self._clear_pair(x, with_faces[j])
                    if found is None or found[0] < settings.clear - settings.margin:
                        continue
                    top, worst, link = found
                    near.setdefault(i, []).append((top, j))
                    near.setdefault(j, []).append((top, i))
                    pairs.append((top, worst, i, j, link))
            joined: list[tuple[int, int, Link]] = []
            used: set[int] = set()
            for top, worst, i, j, link in sorted(pairs, key=lambda p: -p[0]):
                if top < settings.clear or i in used or j in used:
                    continue
                others = [s for s, k in near[i] if k != j] + [s for s, k in near[j] if k != i]
                if others and top - max(others) < settings.margin:
                    plan.blocked[f"{CLEAR}: another candidate within the margin"] += 1
                    continue
                if worst < settings.clear_floor:
                    plan.blocked[f"{CLEAR}: not every clear face alike (complete linkage)"] += 1
                    continue
                why = self._can_join(with_faces[i], with_faces[j])
                if why is not None:
                    plan.blocked[f"{CLEAR}: {why}"] += 1
                    continue
                joined.append((i, j, link))
                used.update((i, j))
            if not joined:
                return clusters
            gone: set[int] = set()
            for i, j, link in joined:
                x, y = with_faces[i], with_faces[j]
                merged = _Cluster([*x.members, *y.members], [*x.links, *y.links, link])
                gone.update((id(x), id(y)))
                clusters.append(merged)
            clusters = [c for c in clusters if id(c) not in gone]

    # --- the plan -------------------------------------------------------------------------

    def _target(self, members: list[int]) -> int:
        marked = [m for m in members if self.marked(m)]
        if marked:
            return marked[0]
        return max(members, key=lambda m: (len(self.by_id[m].sightings), -m))

    def plan(self) -> Plan:
        plan = Plan(groups=[], considered=len(self.by_id))
        for cluster in self._clear_faces(self._same_moment(plan), plan):
            if len(cluster.members) < 2:
                continue
            target = self._target(cluster.members)
            members = [target, *sorted(m for m in cluster.members if m != target)]
            plan.groups.append(Group(target, members, cluster.links))
        plan.groups.sort(key=lambda g: g.target)
        return plan


def plan_merges(store: IdentityStore, settings: MergeSettings | None = None) -> Plan:
    return Planner(store.candidates(), store.apart_pairs(), settings).plan()


@dataclass
class Applied:
    run: str
    merged: int = 0
    skipped: list[str] = field(default_factory=list)
    backup: str | None = None


def run_name(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock()).strftime("%Y%m%d-%H%M%S")


def apply_plan(store: IdentityStore, plan: Plan, *, run: str, backup: bool = True) -> Applied:
    """Carry the plan out, one audited merge at a time. Each merge checks its rules again
    when it writes, so a store that changed since the plan (a new sighting, a person's
    answer) only loses that merge. With `backup`, the database is copied first."""
    done = Applied(run)
    if not plan.groups:
        return done
    if backup:
        done.backup = str(store.backup())
    for group in plan.groups:
        for member in group.members[1:]:
            link = max(
                (link for link in group.links if member in (link.a, link.b)),
                key=lambda link: link.score,
                default=Link(member, group.target, 0.0, CLEAR),
            )
            try:
                store.auto_merge(member, group.target, rule=link.rule, score=link.score, run=run)
            except IdentityError as exc:
                done.skipped.append(f"#{member} → #{group.target}: {exc}")
                continue
            done.merged += 1
    return done


def merge_pass(
    store: IdentityStore,
    settings: MergeSettings | None = None,
    *,
    clock: Callable[[], float] = time.time,
    backup: bool = True,
) -> tuple[Plan, Applied]:
    """Plan and apply in one go (the server's pass)."""
    plan = plan_merges(store, settings)
    applied = apply_plan(store, plan, run=run_name(clock), backup=backup)
    if applied.merged:
        logger.info(
            "identity merge pass %s: %d merge(s) in %d group(s)",
            applied.run, applied.merged, len(plan.groups),
        )  # fmt: skip
    return plan, applied

"""From the ranked candidates to the edit plan: narrative structure, budgets, selection and durations.

Basic version of the narrative structure of section 6:

1. the budget of each section is its share of `--duration`; the Closing gets at least one clip
   (the extra time comes from Development) and a section too short for a single clip is merged with
   its neighbor. Short targets (or very little material) use the compressed structure: an Opening
   of at most 5 s, one chronological body and a Closing of a single clip;
2. the Opening takes the highest `narrative_score` of the whole vacation, out of chronological order,
   with variety of `moment_type` and days, quick clips and no `transition`;
3. the Closing is chosen by criteria: serene clips (`scenery` or `social`, low motion) with a high
   `technical_score`, preferring the last stretch of the vacation;
4. the other candidates are split into three chronological bands at the quantiles of the useful
   material, in proportion to the budgets of Beginning, Development and Finale (not on calendar
   time), and each band is filled in score order and shown in chronological order;
5. a section that runs out of material gives its unused budget to the others, in proportion to
   their shares (first within their ranges, then beyond), never by repeating clips;
6. clip durations are adjusted within their ranges so that every section, and the whole edit with
   the dissolves taken into account, lasts what was planned. If the material is not enough the
   video is shorter and the report says why.

`--structure chronological` drops the sections: one chronological selection over the whole vacation.
"""

import bisect
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from vacation_memories.edit_plan import PHOTO, VIDEO, EditPlan, OutputSpec, PlanElement, finalize_timeline
from vacation_memories.scene_ranker import RankedCandidate, Ranking
from vacation_memories.selection_config import SECTIONS, SelectionConfig
from vacation_memories.transitions import CUT, DISSOLVE, TransitionRequest, TransitionRules, layout_timeline

NARRATIVE = "narrative"
CHRONOLOGICAL = "chronological"
COMPRESSED = "compressed"
BODY = "body"  # Beginning, Development and Finale together, in the compressed structure
SERENE_MOMENTS = ("scenery", "social")
VIDEO_END_MARGIN_SECONDS = 0.1  # never cut a clip right at the end of its segment
OPENING_SAME_TYPE_PENALTY = 0.15
OPENING_SAME_DAY_PENALTY = 0.10
OPENING_MOTION_BONUS = 0.10
CLOSING_WEIGHTS = {"technical": 0.4, "calm": 0.3, "late": 0.3}
FIT_ITERATIONS = 12
MAX_REDISTRIBUTIONS = 10
_EPSILON = 1e-6


@dataclass
class Clip:
    item: RankedCandidate
    section: str
    min_duration: float
    max_duration: float
    duration: float
    reason: str
    transition: TransitionRequest = field(default_factory=TransitionRequest)

    @property
    def preferred(self) -> float:
        return (self.min_duration + self.max_duration) / 2


@dataclass
class Section:
    name: str
    share: float  # proportion used when budget is redistributed
    budget: float
    allowed: tuple[float, float] | None  # range in seconds, None for the compressed and chronological sections
    chronological: bool
    pool: list[RankedCandidate] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    initial_budget: float = 0.0
    redistributed: bool = False
    ran_out: bool = False
    relaxed_runs: bool = False

    def __post_init__(self) -> None:
        self.initial_budget = self.budget


@dataclass(frozen=True)
class PlanningResult:
    plan: EditPlan
    report: dict


def estimated_clip_count(target: float, config: SelectionConfig) -> int:
    """How many clips an edit of `target` seconds needs, roughly (for the AI budget)."""
    if target < config.short_target_seconds:
        typical = (config.short_photo_min_seconds + config.short_photo_max_seconds) / 2
    else:
        typical = (config.photo_min_seconds + config.photo_max_seconds) / 2
    return max(1, math.ceil(target / typical))


def plan_edit(
    ranking: Ranking,
    *,
    target: float,
    output: OutputSpec,
    config: SelectionConfig,
    rules: TransitionRules,
    structure: str = NARRATIVE,
    assumed_tz: str | None = None,
) -> PlanningResult:
    planner = _Planner(ranking, target, output, config, rules, structure)
    return planner.run(assumed_tz)


class _Planner:
    def __init__(self, ranking, target, output, config, rules, structure):
        self.ranking = ranking
        self.target = target
        self.output = output
        self.fps: Fraction = output.frame_rate
        self.config = config
        self.rules = rules
        self.structure = structure
        self.short = target < config.short_target_seconds
        self.index = {item.id: position for position, item in enumerate(ranking.ranked)}
        self.useful = [item for item in ranking.ranked if self._usable(item)]
        self.used: set[str] = set()
        self.closest = np.zeros(len(ranking.ranked), dtype=np.float32)  # similarity to the chosen clips
        self.file_seconds: Counter = Counter()
        self.moment_seconds: Counter = Counter()
        self.file_cap = max(config.max_file_share * target, config.min_file_cap_seconds)
        self.transition_cap = config.transition_time_share * target
        self.sections: list[Section] = []
        self.compressed = False

    # --- the whole procedure ---------------------------------------------------------------------

    def run(self, assumed_tz: str | None) -> PlanningResult:
        self.sections = self._make_sections()
        by_name = {section.name: section for section in self.sections}
        if "opening" in by_name:
            self._fill_opening(by_name["opening"])
        if "closing" in by_name:
            self._fill_closing(by_name["closing"])
        self._assign_bands()
        for section in self.sections:
            if section.name not in ("opening", "closing"):
                self._fill(section)
        self._redistribute()
        self._drop_misplaced_transitions()

        sequence = self._sequence()
        self._assign_transitions(sequence)
        self._fit(sequence)
        plan = self._build_plan(sequence, assumed_tz)
        plan, layout = finalize_timeline(plan, self.rules)
        report = self._report(sequence, plan, layout)
        return PlanningResult(plan, report)

    # --- sections and budgets --------------------------------------------------------------------

    def _make_sections(self) -> list[Section]:
        config, target = self.config, self.target
        if self.structure == CHRONOLOGICAL:
            return [Section(CHRONOLOGICAL, 1.0, target, None, True, pool=list(self.useful))]

        shares = config.sections
        self.compressed = target < config.short_target_seconds or len(self.useful) < config.compressed_min_clips
        if self.compressed:
            opening = min(config.compressed_opening_max_seconds, shares["opening"].share * target)
            if opening < config.min_section_seconds:
                opening = 0.0
            closing = max(config.closing_short_min_seconds, shares["closing"].share * target)
            sections = [
                Section("opening", shares["opening"].share, opening, None, False),
                Section(BODY, sum(shares[name].share for name in ("beginning", "development", "finale")),
                        target - opening - closing, None, True),
                Section("closing", shares["closing"].share, closing, None, True),
            ]
            return [section for section in sections if section.budget > 0]

        budgets = {name: shares[name].share * target for name in SECTIONS}
        closing_minimum = min(config.closing_photo_min_seconds, config.closing_video_min_seconds)
        if budgets["closing"] < closing_minimum:  # one clip at least, paid by Development
            budgets["development"] -= closing_minimum - budgets["closing"]
            budgets["closing"] = closing_minimum
        merge_into = {"opening": "beginning", "beginning": "development", "finale": "development"}
        for name, neighbor in merge_into.items():
            if 0 < budgets[name] < config.min_section_seconds:
                budgets[neighbor] += budgets[name]
                budgets[name] = 0.0
        return [
            Section(name, shares[name].share, budgets[name],
                    (shares[name].min * target, shares[name].max * target), name != "opening")
            for name in SECTIONS
            if budgets[name] > 0
        ]

    def _assign_bands(self) -> None:
        """The candidates left after Opening and Closing, split into chronological bands of useful material."""
        bands = [section for section in self.sections if section.name not in ("opening", "closing")]
        remaining = [item for item in self.useful if item.id not in self.used]
        if not bands:
            return
        if len(bands) == 1:
            bands[0].pool = remaining
            return
        weights = [self._range(item, bands[0].name)[2] for item in remaining]
        cumulative = np.cumsum(weights) if weights else np.array([])
        total = float(cumulative[-1]) if len(cumulative) else 0.0
        budget_total = sum(band.budget for band in bands)
        start, edge = 0, 0.0
        for number, band in enumerate(bands):
            if number == len(bands) - 1:
                band.pool = remaining[start:]
                break
            edge += band.budget / budget_total * total
            stop = int(np.searchsorted(cumulative, edge - _EPSILON, side="left")) + 1
            band.pool = remaining[start:stop]
            start = stop

    # --- clips ------------------------------------------------------------------------------------

    def _usable(self, item: RankedCandidate) -> bool:
        available = item.available_seconds
        return available is None or available - VIDEO_END_MARGIN_SECONDS >= self.config.min_clip_seconds

    def _range(self, item: RankedCandidate, section: str) -> tuple[float, float, float] | None:
        """(minimum, maximum, preferred) duration of a candidate in a section; None if it cannot be used there."""
        config = self.config
        photo = item.candidate.kind == PHOTO
        if section == "opening":
            low, high = config.min_clip_seconds, config.opening_clip_max_seconds
        elif section == "closing":
            short = self.short or self.compressed
            if photo:
                low, high = config.closing_photo_min_seconds, config.closing_photo_max_seconds
            else:
                low, high = config.closing_video_min_seconds, config.closing_video_max_seconds
            if short:
                low = config.closing_short_min_seconds
        elif self.short:
            low, high = ((config.short_photo_min_seconds, config.short_photo_max_seconds) if photo
                         else (config.short_video_min_seconds, config.short_video_max_seconds))
        else:
            low, high = ((config.photo_min_seconds, config.photo_max_seconds) if photo
                         else (config.video_min_seconds, config.video_max_seconds))
            if item.moment_type == "detail":
                low, high = config.min_clip_seconds, min(high, config.detail_max_seconds)
            elif item.moment_type == "scenery":
                low, high = low + config.scenery_extra_seconds, high + config.scenery_extra_seconds

        if not photo:
            available = item.available_seconds - VIDEO_END_MARGIN_SECONDS
            if section == "closing" and available < low:
                return None
            high = min(high, available)
            low = min(low, high)
            if low < config.min_clip_seconds:
                return None
        return low, high, (low + high) / 2

    def _make_clip(self, item: RankedCandidate, section: Section, reason: str) -> Clip | None:
        found = self._range(item, section.name)
        if found is None:
            return None
        low, high, preferred = found
        return Clip(item, section.name, low, high, preferred, reason)

    def _dissolve(self, section: str, duration: float) -> float:
        """The dissolve expected into a clip of `duration` seconds (the Opening is cut)."""
        if section == "opening":
            return 0.0
        wanted = self.config.closing_dissolve_seconds if section == "closing" else self.rules.default_seconds
        return min(wanted, self.rules.max_fraction * duration)

    def _contribution(self, section: str, duration: float) -> float:
        return duration - self._dissolve(section, duration)

    def _totals(self, section: Section) -> tuple[float, float, float]:
        """(minimum, preferred, maximum) time the section's clips can fill on the timeline."""
        sums = [0.0, 0.0, 0.0]
        for clip in section.clips:
            for number, duration in enumerate((clip.min_duration, clip.preferred, clip.max_duration)):
                sums[number] += self._contribution(section.name, duration)
        return sums[0], sums[1], sums[2]

    # --- choosing ---------------------------------------------------------------------------------

    def _sequence(self) -> list[Clip]:
        return [clip for section in self.sections for clip in section.clips]

    def _accept(self, clip: Clip, section: Section) -> None:
        item = clip.item
        self.used.add(item.id)
        self.closest = np.maximum(self.closest, self.ranking.similarity[self.index[item.id]])
        # Caps count the longest the clip may become once the durations are fitted.
        self.file_seconds[item.candidate.rel_path] += clip.max_duration
        if item.moment_type:
            self.moment_seconds[item.moment_type] += clip.max_duration

    def _redundancy_factor(self, item: RankedCandidate) -> float:
        if self.closest[self.index[item.id]] >= self.config.redundancy_similarity:
            return 1.0 - self.config.redundancy_penalty
        return 1.0

    def _fits(self, clip: Clip, section: Section, totals: tuple[float, float, float]) -> bool:
        """The clip can be squeezed into the remaining budget, and the section still needs time."""
        low, preferred, high = totals
        if low + self._contribution(section.name, clip.min_duration) > section.budget + _EPSILON:
            return False
        if high < section.budget:
            return True
        return preferred + self._contribution(section.name, clip.preferred) / 2 <= section.budget

    def _allowed(self, clip: Clip) -> str | None:
        """Rules that do not depend on the position: file cap, transition cap, weak transitions."""
        item = clip.item
        if self.file_seconds[item.candidate.rel_path] + clip.max_duration > self.file_cap + _EPSILON:
            return "file cap"
        if item.moment_type == "transition":
            if clip.section in ("opening", "closing"):
                return "no transition in the Opening and Closing"
            value = item.narrative_value
            if value is None or value < self.config.transition_min_narrative_value:
                return "narratively weak transition"
            if self.moment_seconds["transition"] + clip.max_duration > self.transition_cap + _EPSILON:
                return "transition time cap"
        return None

    def _breaks_rules(self, sequence: list[Clip], position: int, strict_runs: bool) -> str | None:
        """Rules around a clip just inserted at `position`: same moment type runs, transitions as links."""
        clip = sequence[position]
        moment = clip.item.moment_type
        if strict_runs and moment is not None:
            run = 1
            for step in (-1, 1):
                other = position + step
                while 0 <= other < len(sequence) and sequence[other].item.moment_type == moment:
                    run += 1
                    other += step
            if run > self.config.max_same_moment_run:
                return "moment type run"
        for other in (position - 1, position, position + 1):
            if 0 <= other < len(sequence) and not self._is_link(sequence, other):
                return "transition not between days"
        return None

    @staticmethod
    def _is_link(sequence: list[Clip], position: int) -> bool:
        """A `transition` clip must sit where the day changes (or at an end of the edit)."""
        clip = sequence[position]
        if clip.item.moment_type != "transition":
            return True
        day = clip.item.day
        before = sequence[position - 1].item.day if position else None
        after = sequence[position + 1].item.day if position + 1 < len(sequence) else None
        return before != day or after != day

    def _insert(self, section: Section, clip: Clip, strict_runs: bool) -> str | None:
        """Put the clip in its place (chronological, or at the end) if no rule is broken."""
        offset = sum(len(other.clips) for other in self.sections[: self.sections.index(section)])
        if section.chronological:
            keys = [(c.item.captured_at, c.item.id) for c in section.clips]
            place = bisect.bisect(keys, (clip.item.captured_at, clip.item.id))
        else:
            place = len(section.clips)
        section.clips.insert(place, clip)
        problem = self._breaks_rules(self._sequence(), offset + place, strict_runs)
        if problem:
            del section.clips[place]
        return problem

    def _fill(self, section: Section, score=None, reason=None) -> None:
        """Add clips in score order until the section's budget is covered or the material runs out."""
        score = score or self._body_score(section)
        reason = reason or (lambda item, value: f"{section.name}: score {value:.2f}")
        for strict_runs in (True, False):
            rejected: set[str] = set()
            blocked_by_runs = False
            while True:
                best = None
                totals = self._totals(section)
                for item in section.pool:
                    if item.id in self.used or item.id in rejected:
                        continue
                    clip = self._make_clip(item, section, "")
                    if clip is None or not self._fits(clip, section, totals):
                        continue
                    value = score(item)
                    if best is None or value > best[0]:
                        best = (value, clip)
                if best is None:
                    break
                value, clip = best
                clip.reason = reason(clip.item, value)
                problem = self._allowed(clip) or self._insert(section, clip, strict_runs)
                if problem:
                    rejected.add(clip.item.id)
                    blocked_by_runs = blocked_by_runs or problem == "moment type run"
                    continue
                self._accept(clip, section)
            covered = self._totals(section)[2] >= section.budget - _EPSILON
            if covered or not blocked_by_runs:
                break
            section.relaxed_runs = True  # not enough variety: the rule gives way rather than the duration
        section.ran_out = self._totals(section)[2] < section.budget - _EPSILON

    def _body_score(self, section: Section):
        config = self.config
        openers = self._day_openers(section.pool)

        def score(item: RankedCandidate) -> float:
            value = item.final_score * self._redundancy_factor(item)
            moment = item.moment_type
            if moment is not None:
                share = config.moment_shares.get(moment, 1.0)
                if (self.moment_seconds[moment] + config.photo_max_seconds) / self.target > share:
                    value -= config.moment_share_penalty
            if item.id in openers:
                value += config.day_opener_bonus
            return value

        return score

    def _day_openers(self, pool: list[RankedCandidate]) -> set[str]:
        """The shot that should open each day: the first `scenery`, or without AI the calmest early shot."""
        days: dict = defaultdict(list)
        for item in pool:
            days[item.day].append(item)
        openers = set()
        for items in days.values():
            if any(item.moment_type for item in items):
                scenery = next((item for item in items if item.moment_type == "scenery"), None)
                if scenery:
                    openers.add(scenery.id)
            else:
                openers.add(min(items[:3], key=lambda item: item.motion_level).id)
        return openers

    def _fill_opening(self, section: Section) -> None:
        section.pool = [item for item in self.useful if item.moment_type != "transition"]
        chosen = section.clips

        def score(item: RankedCandidate) -> float:
            same_type = sum(1 for clip in chosen if item.moment_type and clip.item.moment_type == item.moment_type)
            same_day = sum(1 for clip in chosen if clip.item.day == item.day)
            return (item.narrative_score * self._redundancy_factor(item) + OPENING_MOTION_BONUS * item.motion_level
                    - OPENING_SAME_TYPE_PENALTY * same_type - OPENING_SAME_DAY_PENALTY * same_day)

        self._fill(section, score, lambda item, value: f"opening: narrative score {item.narrative_score:.2f}")
        section.clips[:] = self._varied_order(section.clips)

    def _varied_order(self, clips: list[Clip]) -> list[Clip]:
        """Best first, never the same moment type twice in a row when there is a choice."""
        remaining = sorted(clips, key=lambda clip: -clip.item.narrative_score)
        ordered: list[Clip] = []
        while remaining:
            last = ordered[-1].item.moment_type if ordered else None
            pick = next((c for c in remaining if last is None or c.item.moment_type != last), remaining[0])
            remaining.remove(pick)
            ordered.append(pick)
        return ordered

    def _fill_closing(self, section: Section) -> None:
        candidates = [item for item in self.useful if item.id not in self.used and item.moment_type != "transition"]
        serene = [item for item in candidates if item.moment_type in SERENE_MOMENTS or item.moment_type is None]
        section.pool = serene or candidates
        position = {item.id: number for number, item in enumerate(self.useful)}
        last = max(1, len(self.useful) - 1)

        def score(item: RankedCandidate) -> float:
            weights = CLOSING_WEIGHTS
            return (weights["technical"] * item.technical_score + weights["calm"] * (1 - item.motion_level)
                    + weights["late"] * position[item.id] / last) * self._redundancy_factor(item)

        self._fill(section, score, lambda item, value: f"closing: calm shot, closing score {value:.2f}")

    # --- adapting to the material -----------------------------------------------------------------

    def _redistribute(self) -> None:
        for _ in range(MAX_REDISTRIBUTIONS):
            deficit = 0.0
            for section in self.sections:
                capacity = self._totals(section)[2]
                if section.ran_out and capacity < section.budget - _EPSILON:
                    deficit += section.budget - capacity
                    section.budget = capacity
                    section.redistributed = True
            receivers = [section for section in self.sections if not section.ran_out]
            if deficit < _EPSILON or not receivers:
                break
            self._share_out(deficit, receivers)
            for section in receivers:
                if section.name == "opening":
                    self._fill_opening(section)
                elif section.name == "closing":
                    self._fill_closing(section)
                else:
                    self._fill(section)

    def _share_out(self, amount: float, receivers: list[Section]) -> None:
        """First within the allowed ranges, then beyond them, always in proportion to the shares."""
        for within_range in (True, False):
            open_sections = [s for s in receivers if not within_range or s.allowed is None or s.budget < s.allowed[1]]
            total_share = sum(section.share for section in open_sections)
            if amount < _EPSILON or total_share <= 0:
                continue
            given = 0.0
            for section in open_sections:
                part = amount * section.share / total_share
                if within_range and section.allowed is not None:
                    part = min(part, section.allowed[1] - section.budget)
                section.budget += part
                section.redistributed = True
                given += part
            amount -= given

    def _drop_misplaced_transitions(self) -> None:
        """A late change of a neighbor (the Opening reordered, a refill) can leave a `transition` inside a day."""
        while True:
            sequence = self._sequence()
            misplaced = next((clip for position, clip in enumerate(sequence) if not self._is_link(sequence, position)),
                             None)
            if misplaced is None:
                return
            section = next(section for section in self.sections if misplaced in section.clips)
            section.clips.remove(misplaced)

    # --- order, transitions and durations -------------------------------------------------------------

    def _assign_transitions(self, sequence: list[Clip]) -> None:
        default = self.rules.default_seconds
        for position, clip in enumerate(sequence):
            if position == 0:
                clip.transition = TransitionRequest()
                continue
            previous = sequence[position - 1]
            if clip.section == "opening":
                clip.transition = TransitionRequest(CUT, 0.0)
            elif clip.section == "closing" and previous.section != "closing":
                clip.transition = TransitionRequest(DISSOLVE, self.config.closing_dissolve_seconds)
            else:
                clip.transition = TransitionRequest(DISSOLVE, default) if default > 0 else TransitionRequest()

    def _layout(self, sequence: list[Clip]):
        frames = [max(1, round(clip.duration * self.fps)) for clip in sequence]
        return layout_timeline(frames, [clip.transition for clip in sequence], self.fps, self.rules)

    def _section_spans(self, sequence: list[Clip]) -> list[tuple[Section, list[int]]]:
        spans, start = [], 0
        for section in self.sections:
            spans.append((section, list(range(start, start + len(section.clips)))))
            start += len(section.clips)
        return [(section, indices) for section, indices in spans if indices]

    def _fit(self, sequence: list[Clip]) -> None:
        """Move durations within their ranges until each section lasts its budget on the timeline."""
        if not sequence:
            return
        half_frame = 0.5 / float(self.fps)
        for _ in range(FIT_ITERATIONS):
            layout = self._layout(sequence)
            changed = False
            previous_end = 0
            for section, indices in self._section_spans(sequence):
                end = layout.placements[indices[-1]].end_frame
                span = float((end - previous_end) / self.fps)
                previous_end = end
                difference = section.budget - span
                if abs(difference) <= half_frame:
                    continue
                clips = [sequence[i] for i in indices]
                slack = [(c.max_duration - c.duration) if difference > 0 else (c.duration - c.min_duration) for c in clips]
                total = sum(slack)
                if total <= _EPSILON:
                    continue
                ratio = min(1.0, abs(difference) / total)
                for clip, room in zip(clips, slack):
                    clip.duration += math.copysign(room * ratio, difference)
                changed = True
            if not changed:
                break
        for clip in sequence:
            frames = max(1, round(clip.duration * self.fps))
            clip.duration = min(max(float(frames / self.fps), clip.min_duration - _EPSILON), clip.max_duration + _EPSILON)

    def _build_plan(self, sequence: list[Clip], assumed_tz: str | None) -> EditPlan:
        elements = []
        for clip in sequence:
            item = clip.item
            candidate = item.candidate
            start = end = None
            duration = clip.duration
            if candidate.kind == VIDEO:
                middle = (candidate.start + candidate.end) / 2
                start = min(max(candidate.start, middle - duration / 2), candidate.end - VIDEO_END_MARGIN_SECONDS - duration)
                start = max(candidate.start, round(start, 6))
                end = round(start + duration, 6)
                duration = end - start
            score = {
                "final": round(item.final_score, 4),
                "technical_score": round(item.technical_score, 4),
                "narrative_score": round(item.narrative_score, 4),
                "components": item.components,
            }
            elements.append(PlanElement(
                source=candidate.rel_path.as_posix(), kind=candidate.kind, duration=round(duration, 6),
                start=start, end=end, transition_in=clip.transition, score=score,
                moment_type=item.moment_type, category=item.category, section=clip.section,
                description=item.ai.description if item.ai else None, reason=clip.reason,
                captured_at=item.captured_at.isoformat(timespec="milliseconds"), date_source=item.date_source,
                assumed_tz=item.assumed_tz,
            ))
        return EditPlan(output=self.output, elements=elements, requested_duration=self.target, assumed_tz=assumed_tz)

    # --- report -----------------------------------------------------------------------------------

    def _report(self, sequence: list[Clip], plan: EditPlan, layout) -> dict:
        planned = plan.planned_duration or 0.0
        sections = []
        previous_end, position = 0, 0
        for section in self.sections:
            span = 0.0
            if section.clips:
                position += len(section.clips)
                end = layout.placements[position - 1].end_frame
                span = float((end - previous_end) / self.fps)
                previous_end = end
            sections.append({
                "name": section.name,
                "initial_budget": round(section.initial_budget, 3),
                "budget": round(section.budget, 3),
                "planned": round(span, 3),
                "clips": len(section.clips),
                "redistributed": section.redistributed,
                "ran_out_of_material": section.ran_out,
                "allowed_range": None if section.allowed is None else [round(v, 3) for v in section.allowed],
                "moment_runs_relaxed": section.relaxed_runs,
            })

        insufficient = not sequence or sum(section.budget for section in self.sections) < self.target - 0.5
        warnings = []
        if not sequence:
            warnings.append("no usable material: nothing above the technical threshold that is not a duplicate")
        elif insufficient:
            warnings.append(
                f"insufficient material: the video lasts {planned:.1f} s instead of {self.target:.1f} s, because after "
                f"the quality and duplicate filters only {len(self.useful)} usable candidate(s) are left, and no "
                "discarded scene or repeated clip is used to fill the gap"
            )
        for section in self.sections:
            if section.relaxed_runs:
                warnings.append(
                    f"{section.name}: not enough variety of moment_type, more than "
                    f"{self.config.max_same_moment_run} clips of the same type in a row were allowed"
                )
        structure = CHRONOLOGICAL if self.structure == CHRONOLOGICAL else (COMPRESSED if self.compressed else NARRATIVE)
        return {
            "structure": structure,
            "requested_duration": self.target,
            "planned_duration": planned,
            "insufficient_material": insufficient,
            "clips": len(sequence),
            "usable_candidates": len(self.useful),
            "sections": sections,
            "warnings": warnings,
        }

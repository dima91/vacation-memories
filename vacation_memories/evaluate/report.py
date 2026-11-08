"""`evaluate report`: the ratings of `review.html` turned into metrics, a private report and an anonymous summary.

It reads `prepare.json` and validates `ratings.json` (code 2 when it is missing or not valid), and
only then `mapping.json`: nothing before the ratings knows which letter is which variant. It never
reads the media or calls Ollama, so it runs with `--no-deps` and `--require-ai` does not apply.

- `eval_report.md`, private: the answers to the five questions, naming the files of the moments
  flagged as important;
- `eval_summary.json`, anonymous, the only file to share: segment ids are HMACs with the workdir's
  secret salt, days are relative, and there are no paths, file names, thumbnails, descriptions or
  dates;
- `<workdir>/eval/labels.json` takes the flags of this cycle, and the labelled important and
  repetitive moments are measured on the full edit of this cycle;
- `<workdir>/eval/eval_history.json` takes this cycle, compared with the previous one and with `random`.

A report can be made again (for example after correcting the ratings): it replaces its own cycle.
"""

import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from vacation_memories import exit_codes
from vacation_memories.atomic import atomic_write_json, atomic_write_text
from vacation_memories.config import ConfigError, Settings
from vacation_memories.evaluate.labels import (
    LENGTHS,
    OK,
    TOO_LONG,
    TOO_SHORT,
    Labels,
    anonymous_id,
    eval_folder,
    load_history,
    load_labels,
    record_cycle,
    save_labels,
    workdir_salt,
)
from vacation_memories.evaluate.page import MAX_RATING, MIN_RATING, RATINGS_NAME, RATINGS_VERSION, RHYTHMS, segment_of
from vacation_memories.evaluate.prepare import (
    FULL,
    MAPPING_NAME,
    MAPPING_VERSION,
    PREPARE_NAME,
    PREPARE_VERSION,
    RANDOM,
    RUN_ID_PATTERN,
    VARIANTS,
)
from vacation_memories.workdir import WorkdirInUseError, WorkdirLock

logger = logging.getLogger(__name__)

REPORT_MD_NAME = "eval_report.md"
SUMMARY_NAME = "eval_summary.json"
SUMMARY_VERSION = 1
STATISTICAL_MIN_COMPARISONS = 10
UNKNOWN_MOMENT = "unknown"
NOTE = ("indicative, not statistical: {count} blind comparison(s) of the full edit with the random one so far, "
        "from a single viewer")


@dataclass(frozen=True)
class ReportRequest:
    run_id: str | None = None  # default: the latest prepared run
    ratings: Path | None = None  # default: eval/<run_id>/ratings.json; a relative path is inside the workdir

    def __post_init__(self) -> None:
        if self.run_id is not None and not RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ConfigError(f"--run-id {self.run_id!r} is not the id of a run")


def run_report(settings: Settings, request: ReportRequest, *, out=print) -> int:
    """`evaluate report`: write the report, the summary, the labels and the history; returns the exit code."""
    workdir = Path(settings.workdir).absolute()
    eval_dir = eval_folder(workdir)
    try:
        run_id = request.run_id or latest_run(eval_dir)
        if not (eval_dir / run_id).is_dir():
            raise ConfigError(f"the run {run_id} is not prepared in {eval_dir}")
    except ConfigError as error:
        return exit_codes.usage_error(error)
    lock = WorkdirLock(eval_dir)
    try:
        lock.acquire()
    except WorkdirInUseError as error:
        print(f"error: {error}", file=sys.stderr)
        return exit_codes.WORKDIR_IN_USE
    state = "failed"
    try:
        try:
            ratings_path = request.ratings or eval_dir / run_id / RATINGS_NAME
            if not ratings_path.is_absolute():
                ratings_path = workdir / ratings_path
            lines = make_report(eval_dir, run_id, ratings_path)
        except ConfigError as error:
            return exit_codes.usage_error(error)
        except Exception:
            logger.exception("the report failed")
            return exit_codes.ERROR
        for line in lines:
            out(line)
        state = "done"
        return exit_codes.OK
    finally:
        try:
            lock.status.finish(state)
        except OSError as error:
            logger.warning("cannot record the final state: %s", error)
        lock.release()


def latest_run(eval_dir: Path) -> str:
    """The run prepared last in `eval_dir`."""
    runs = []
    folders = [path for path in Path(eval_dir).iterdir() if path.is_dir()] if Path(eval_dir).is_dir() else []
    for folder in folders:
        try:
            prepared = json.loads((folder / PREPARE_NAME).read_text(encoding="utf-8"))
            runs.append((str(prepared["created_at"]), folder.name))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not runs:
        raise ConfigError(f"no prepared run in {eval_dir}: run `evaluate prepare` first")
    return max(runs)[1]


def make_report(eval_dir: Path, run_id: str, ratings_path: Path) -> list[str]:
    run_dir = eval_dir / run_id
    prepared = load_prepared(run_dir)
    ratings = load_ratings(ratings_path, prepared)
    mapping = load_mapping(run_dir, prepared)  # only now: the ratings are fixed
    labels = load_labels(eval_dir)
    load_history(eval_dir)  # a broken history fails before anything is written
    salt = workdir_salt(eval_dir)

    names = mapping["letters"]
    _apply_labels(labels, prepared, ratings, run_id)
    variants = {names[letter]: _variant_metrics(prepared, letter, ratings["variants"].get(letter))
                for letter in sorted(names)}
    full_letter = next(letter for letter, name in names.items() if name == FULL)
    important, repetitive = _label_metrics(prepared, ratings, labels, full_letter)
    beats = _beats(variants[FULL], variants[RANDOM]) if RANDOM in variants else None

    entry = {
        "run_id": run_id,
        "created_at": prepared["created_at"],
        "code_version": prepared["code_version"],
        "config_hash": prepared["config_hash"],
        "duration": prepared["duration"],
        "ai_used": prepared["ai"]["used"],
        "variants": {name: {key: metrics[key] for key in ("rating", "rank", "clips", "repetitive_share",
                                                          "too_long_share", "too_short_share")}
                     for name, metrics in variants.items()},
        "important": important,
        "repetitive_labels": repetitive,
        "full_beats_random": beats,
    }
    cycles = record_cycle(eval_dir, entry)
    position = next(number for number, cycle in enumerate(cycles) if cycle["run_id"] == run_id)
    previous = cycles[position - 1] if position else None
    comparisons = [cycle["full_beats_random"] for cycle in cycles if cycle.get("full_beats_random") is not None]
    totals = {"cycles": len(cycles), "comparisons": len(comparisons), "full_beats_random": sum(comparisons)}
    context = {
        "cycle": entry["cycle"], "names": names, "variants": variants, "important": important,
        "repetitive": repetitive, "beats": beats, "previous": _changes(entry, previous), "totals": totals,
        "note": NOTE.format(count=len(comparisons)) if len(comparisons) < STATISTICAL_MIN_COMPARISONS else None,
    }

    atomic_write_json(run_dir / SUMMARY_NAME, _summary(prepared, ratings, names, salt, context))
    atomic_write_text(run_dir / REPORT_MD_NAME, _markdown(prepared, ratings, names, labels, context))
    save_labels(eval_dir, labels)
    return _printed(run_dir, context)


# --- reading and validating ---------------------------------------------------------------------------


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {path}") from None
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {what} {path}: {error}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{what} {path} is not a JSON object")
    return data


def load_prepared(run_dir: Path) -> dict:
    prepared = _read_json(run_dir / PREPARE_NAME, "the prepared run")
    if prepared.get("version") != PREPARE_VERSION or prepared.get("run_id") != run_dir.name:
        raise ConfigError(f"{run_dir / PREPARE_NAME} is of another version or another run")
    return prepared


def load_ratings(path: Path, prepared: dict) -> dict:
    """The ratings exported by the page, checked against the run; raises `ConfigError` (code 2)."""
    if not Path(path).is_file():
        raise ConfigError(f"no ratings: {path} not found. Export ratings.json from review.html and copy it there, "
                          "or give its path with --ratings")
    data = _read_json(path, "the ratings")
    if data.get("version") != RATINGS_VERSION:
        raise ConfigError(f"the ratings {path} are of version {data.get('version')!r}, expected {RATINGS_VERSION}")
    if data.get("run_id") != prepared["run_id"]:
        raise ConfigError(f"the ratings {path} are of the run {data.get('run_id')!r}, not {prepared['run_id']}")
    try:
        return {"variants": _check_variants(data.get("variants", {}), prepared),
                "excluded": _check_excluded(data.get("excluded", {}), prepared)}
    except (TypeError, ValueError, AttributeError) as error:
        raise ConfigError(f"the ratings {path} are not valid: {error}") from None


def _check_variants(given: dict, prepared: dict) -> dict:
    letters = prepared["variants"]
    if not isinstance(given, dict) or set(given) - set(letters):
        raise ValueError(f"the versions are {', '.join(sorted(letters))}")
    checked, ranks = {}, []
    for letter, entry in given.items():
        variant = letters[letter]
        rating, rank = entry.get("rating"), entry.get("rank")
        if rating is not None and not (_integer(rating) and MIN_RATING <= rating <= MAX_RATING):
            raise ValueError(f"version {letter}: the rating is {MIN_RATING} to {MAX_RATING}, got {rating!r}")
        if rank is not None and not (_integer(rank) and 1 <= rank <= len(letters)):
            raise ValueError(f"version {letter}: the rank is 1 to {len(letters)}, got {rank!r}")
        if rank is not None:
            ranks.append(rank)
        sections = {section["name"] for section in variant["sections"]}
        rhythm = entry.get("rhythm") or {}
        for name, value in rhythm.items():
            if name not in sections or value not in (None, *RHYTHMS):
                raise ValueError(f"version {letter}: rhythm {name}={value!r}")
        keys = {clip["key"] for clip in variant["clips"]}
        clips = {}
        for key, flags in (entry.get("clips") or {}).items():
            repetitive, length = flags.get("repetitive", False), flags.get("length", OK)
            if key not in keys or not isinstance(repetitive, bool) or length not in LENGTHS:
                raise ValueError(f"version {letter}: clip {key} with {flags!r}")
            clips[key] = {"repetitive": repetitive, "length": length}
        checked[letter] = {"rating": rating, "rank": rank, "rhythm": dict(rhythm), "clips": clips}
    if len(ranks) != len(set(ranks)):
        raise ValueError("two versions have the same rank")
    return checked


def _check_excluded(given: dict, prepared: dict) -> dict:
    keys = {item["key"] for item in prepared["excluded"]}
    checked = {}
    for key, flags in given.items():
        important = flags.get("important", False)
        if key not in keys or not isinstance(important, bool):
            raise ValueError(f"left-out moment {key} with {flags!r}")
        checked[key] = {"important": important}
    return checked


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_mapping(run_dir: Path, prepared: dict) -> dict:
    mapping = _read_json(run_dir / MAPPING_NAME, "the mapping of the versions")
    letters = mapping.get("letters")
    if (mapping.get("version") != MAPPING_VERSION or mapping.get("run_id") != prepared["run_id"]
            or not isinstance(letters, dict) or set(letters) != set(prepared["variants"])
            or not set(letters.values()) <= set(VARIANTS) or len(set(letters.values())) != len(letters)
            or FULL not in letters.values()):
        raise ConfigError(f"{run_dir / MAPPING_NAME} does not match the prepared run")
    return mapping


# --- labels ---------------------------------------------------------------------------------------


def _apply_labels(labels: Labels, prepared: dict, ratings: dict, run_id: str) -> None:
    """The flags of this cycle, one per segment: repetitive in any version, the first length that is not ok."""
    flags: dict[str, dict] = {}
    for letter in sorted(ratings["variants"]):
        for key, clip in ratings["variants"][letter]["clips"].items():
            current = flags.setdefault(key, {"repetitive": False, "length": OK})
            current["repetitive"] = current["repetitive"] or clip["repetitive"]
            if current["length"] == OK:
                current["length"] = clip["length"]
    for key, item in ratings["excluded"].items():
        flags.setdefault(key, {})["important"] = item["important"]
    segments = prepared["segments"]
    for key in sorted(flags, key=lambda k: int(k[1:])):
        labels.set(segment_of(segments[key]), run_id, **flags[key])


def _label_metrics(prepared: dict, ratings: dict, labels: Labels, full_letter: str) -> tuple[dict, dict]:
    segments = prepared["segments"]
    gallery = prepared["excluded"]
    reviewed = [item for item in gallery if item["key"] in ratings["excluded"]]
    flagged = [item for item in reviewed if ratings["excluded"][item["key"]]["important"]]
    reasons = Counter(item["reason"] for item in flagged)
    shown = [segment_of(segments[clip["key"]]) for clip in prepared["variants"][full_letter]["clips"]]

    def included(name: str) -> tuple[int, int]:
        marked = labels.flagged(name)
        return len(marked), sum(1 for label in marked if any(clip.shows(label.segment) for clip in shown))

    labelled, kept = included("important")
    important = {
        "gallery": len(gallery),
        "gallery_by_reason": dict(sorted(Counter(item["reason"] for item in gallery).items())),
        "reviewed": len(reviewed),
        "flagged": len(flagged),
        "flagged_by_reason": dict(sorted(reasons.items())),
        "most_frequent_reason": reasons.most_common(1)[0][0] if reasons else None,
        "labelled": labelled,
        "included_in_full": kept,
        "recall": _share(kept, labelled),
    }
    labelled, kept = included("repetitive")
    return important, {"labelled": labelled, "included_in_full": kept}


# --- metrics -------------------------------------------------------------------------------------


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"count": len(values), "mean": round(statistics.fmean(values), 4), "min": round(min(values), 4),
            "median": round(statistics.median(values), 4), "max": round(max(values), 4)}


def _variant_metrics(prepared: dict, letter: str, rated: dict | None) -> dict:
    segments = prepared["segments"]
    variant = prepared["variants"][letter]
    clips = variant["clips"]
    rated = rated or {"rating": None, "rank": None, "rhythm": {}, "clips": {}}
    flags = rated["clips"]
    reviewed = [clip for clip in clips if clip["key"] in flags]

    def share_of(test) -> float | None:
        return _share(sum(1 for clip in reviewed if test(flags[clip["key"]])), len(reviewed))

    time = sum(clip["duration"] for clip in clips)
    by_day, by_source, by_moment = defaultdict(float), defaultdict(float), defaultdict(float)
    for clip in clips:
        entry = segments[clip["key"]]
        by_day[entry["day"]] += clip["duration"]
        by_source[entry["source"]] += clip["duration"]
        by_moment[entry["moment_type"] or UNKNOWN_MOMENT] += clip["duration"]
    material = {int(day): count for day, count in prepared["material_by_day"].items()}
    days = [{"day": day, "time_share": _share(by_day[day], time), "material_share": _share(material.get(day, 0),
                                                                                           sum(material.values()))}
            for day in range(1, prepared["days"] + 1)]
    gaps = [abs(day["time_share"] - day["material_share"]) for day in days
            if day["time_share"] is not None and day["material_share"] is not None]

    def nearest(repetitive: bool) -> list[float]:
        return [clip["nearest_similarity"] for clip in reviewed if clip["nearest_similarity"] is not None
                and flags[clip["key"]]["repetitive"] == repetitive]

    return {
        "rating": rated["rating"],
        "rank": rated["rank"],
        "rhythm": {section["name"]: rated["rhythm"].get(section["name"]) for section in variant["sections"]
                   if section["clips"]},
        "clips": len(clips),
        "reviewed_clips": len(reviewed),
        "requested_duration": variant["requested_duration"],
        "planned_duration": variant["planned_duration"],
        "obtained_duration": variant["obtained_duration"],
        "structure": variant["structure"],
        "repetitive_share": share_of(lambda flag: flag["repetitive"]),
        "too_long_share": share_of(lambda flag: flag["length"] == TOO_LONG),
        "too_short_share": share_of(lambda flag: flag["length"] == TOO_SHORT),
        "days": days,
        "days_covered": _share(sum(1 for day in days if by_day[day["day"]] > 0), len(days)),
        "day_balance_gap": round(max(gaps), 4) if gaps else None,
        "max_source_share": _share(max(by_source.values(), default=0.0), time),
        "moment_type_share": {name: _share(seconds, time) for name, seconds in sorted(by_moment.items())},
        "sections": [{**section, "error": round(section["planned"] - section["budget"], 3)}
                     for section in variant["sections"]],
        "durations": {
            "by_moment_type": _durations(clips, flags, lambda clip: segments[clip["key"]]["moment_type"] or UNKNOWN_MOMENT),
            "by_section": _durations(clips, flags, lambda clip: clip["section"]),
        },
        "similarity": {"repetitive": _stats(nearest(True)), "other": _stats(nearest(False))},
    }


def _durations(clips: list[dict], flags: dict, group) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for clip in clips:
        groups[group(clip)].append(clip)
    result = {}
    for name, members in sorted(groups.items()):
        lengths = [flags[clip["key"]]["length"] for clip in members if clip["key"] in flags]
        result[name] = {**_stats([clip["duration"] for clip in members]),
                        "too_long": lengths.count(TOO_LONG), "too_short": lengths.count(TOO_SHORT)}
    return result


def _beats(full: dict, other: dict) -> bool | None:
    """Whether the full edit beats `other`: by rating, then by rank; None for a tie or no answer."""
    if full["rating"] is not None and other["rating"] is not None and full["rating"] != other["rating"]:
        return full["rating"] > other["rating"]
    if full["rank"] is not None and other["rank"] is not None and full["rank"] != other["rank"]:
        return full["rank"] < other["rank"]
    return None


def _changes(entry: dict, previous: dict | None) -> dict | None:
    """What moved since the previous cycle, for the full edit."""
    if previous is None:
        return None

    def delta(current, before):
        return None if current is None or before is None else round(current - before, 4)

    now, then = entry["variants"][FULL], previous["variants"].get(FULL, {})
    changes = {
        "cycle": previous["cycle"],
        "same_configuration": previous.get("config_hash") == entry["config_hash"],
        "same_code": previous.get("code_version") == entry["code_version"],
    }
    for key in ("rating", "repetitive_share", "too_long_share", "too_short_share"):
        changes[f"full_{key}"] = delta(now.get(key), then.get(key))
    changes["important_recall"] = delta(entry["important"]["recall"], previous.get("important", {}).get("recall"))
    return changes


# --- outputs ---------------------------------------------------------------------------------------


def _summary(prepared: dict, ratings: dict, names: dict, salt: bytes, context: dict) -> dict:
    """The anonymous summary: no path, file name, thumbnail, description or date."""
    segments = prepared["segments"]

    def anonymous(key: str) -> str:
        return anonymous_id(salt, segment_of(segments[key]))

    variants = {}
    for letter, name in sorted(names.items()):
        flags = ratings["variants"].get(letter, {}).get("clips", {})
        clips = []
        for clip in prepared["variants"][letter]["clips"]:
            entry, flag = segments[clip["key"]], flags.get(clip["key"])
            clips.append({
                "segment": anonymous(clip["key"]), "kind": entry["kind"], "day": entry["day"],
                "section": clip["section"], "moment_type": entry["moment_type"], "start": clip["start"],
                "duration": clip["duration"], "score": clip["score"], "nearest_similarity": clip["nearest_similarity"],
                "repetitive": None if flag is None else flag["repetitive"],
                "length": None if flag is None else flag["length"],
            })
        variants[name] = {**context["variants"][name], "clips": clips}
    excluded = []
    for item in prepared["excluded"]:
        entry, flag = segments[item["key"]], ratings["excluded"].get(item["key"])
        excluded.append({
            "segment": anonymous(item["key"]), "kind": entry["kind"], "day": entry["day"],
            "moment_type": entry["moment_type"], "reason": item["reason"], "picked": item["picked"],
            "score": item["score"], "important": None if flag is None else flag["important"],
        })
    coverage = prepared["ai"]["coverage"]
    return {
        "version": SUMMARY_VERSION,
        "cycle": context["cycle"],
        "code_version": prepared["code_version"],
        "config_hash": prepared["config_hash"],
        "duration": prepared["duration"],
        "structure": prepared["structure"],
        "ai": {"used": prepared["ai"]["used"], "coverage": coverage},
        "candidates": prepared["candidates"],
        "days": prepared["days"],
        "material_by_day": prepared["material_by_day"],
        "variants": variants,
        "excluded": excluded,
        "important": context["important"],
        "repetitive_labels": context["repetitive"],
        "blind": {"full_beats_random": context["beats"], "history": context["totals"]},
        "previous_cycle": context["previous"],
        "note": context["note"],
    }


def _number(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return f"{value}{suffix}"
    return f"{value:.{digits}f}{suffix}"


def _percent(value) -> str:
    return "-" if value is None else f"{value:.0%}"


def _markdown(prepared: dict, ratings: dict, names: dict, labels: Labels, context: dict) -> str:
    variants, important, repetitive = context["variants"], context["important"], context["repetitive"]
    letters = {name: letter for letter, name in names.items()}
    lines = [
        f"# Evaluation report: run {prepared['run_id']} (cycle {context['cycle']})",
        "",
        f"Prepared {prepared['created_at']}, code {prepared['code_version']}, configuration "
        f"{prepared['config_hash']}, {prepared['duration']:g} s per version, AI "
        f"{'used' if prepared['ai']['used'] else 'not used'}. Private: it names files of the vacation; share only "
        f"`{SUMMARY_NAME}`.",
        "",
    ]
    if context["note"]:
        lines += [f"> {context['note'][0].upper()}{context['note'][1:]}.", ""]
    lines += ["## Versions", "", "| Version | Variant | Rating | Rank | Clips | Planned | Repetitive | Too long | Too short |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, metrics in variants.items():
        lines.append(f"| {letters[name]} | {name} | {_number(metrics['rating'])} | {_number(metrics['rank'])} | "
                     f"{metrics['clips']} | {_number(metrics['planned_duration'], 1, ' s')} | "
                     f"{_percent(metrics['repetitive_share'])} | {_percent(metrics['too_long_share'])} | "
                     f"{_percent(metrics['too_short_share'])} |")

    lines += ["", "## 1. Important moments left out", "",
              f"{important['flagged']} of the {important['gallery']} left-out moments of the gallery are flagged as "
              f"important; most frequent reason: {important['most_frequent_reason'] or '-'}.", "",
              "| Reason | In the gallery | Flagged important |", "|---|---|---|"]
    for reason, count in important["gallery_by_reason"].items():
        lines.append(f"| {reason} | {count} | {important['flagged_by_reason'].get(reason, 0)} |")
    flagged = [item for item in prepared["excluded"] if ratings["excluded"].get(item["key"], {}).get("important")]
    if flagged:
        lines += ["", "Flagged in this cycle:", ""]
        for item in flagged:
            entry = prepared["segments"][item["key"]]
            extent = "" if entry["start"] is None else f", {entry['start']:.1f}-{entry['end']:.1f} s"
            lines.append(f"- `{entry['source']}` (day {entry['day']}{extent}): {item['reason']}")
    lines += ["", f"Over all cycles, {important['labelled']} moment(s) are labelled important; the full edit of this "
                  f"cycle shows {important['included_in_full']} of them (recall {_percent(important['recall'])})."]

    lines += ["", "## 2. Repetitive scenes", "",
              "| Variant | Flagged repetitive | Similarity of flagged clips (median) | Of the others (median) |",
              "|---|---|---|---|"]
    for name, metrics in variants.items():
        similar = metrics["similarity"]
        lines.append(f"| {name} | {_percent(metrics['repetitive_share'])} | "
                     f"{_number((similar['repetitive'] or {}).get('median'))} | "
                     f"{_number((similar['other'] or {}).get('median'))} |")
    lines += ["", "The similarity is the perceptual hash of each clip against the most similar other clip of the same "
                  f"edit. {repetitive['labelled']} segment(s) are labelled repetitive over all cycles; the full edit "
                  f"of this cycle shows {repetitive['included_in_full']} of them."]

    lines += ["", "## 3. Balance", "", "| Variant | Part | Budget | Planned | Clips | Rhythm |", "|---|---|---|---|---|---|"]
    for name, metrics in variants.items():
        for section in metrics["sections"]:
            rhythm = metrics["rhythm"].get(section["name"])
            lines.append(f"| {name} | {section['name']} | {section['budget']:.1f} s | {section['planned']:.1f} s | "
                         f"{section['clips']} | {(rhythm or '-').replace('_', ' ')} |")
    lines += ["", "| Variant | Days covered | Largest day gap | Largest file share | Moment types |", "|---|---|---|---|---|"]
    for name, metrics in variants.items():
        moments = ", ".join(f"{kind} {_percent(share)}" for kind, share in metrics["moment_type_share"].items())
        lines.append(f"| {name} | {_percent(metrics['days_covered'])} | {_percent(metrics['day_balance_gap'])} | "
                     f"{_percent(metrics['max_source_share'])} | {moments or '-'} |")

    lines += ["", "## 4. Clip durations", ""]
    for name, metrics in variants.items():
        lines += [f"### {name}", "", "| Group | Clips | Mean | Min | Max | Too long | Too short |", "|---|---|---|---|---|---|---|"]
        for kind in ("by_moment_type", "by_section"):
            for group, stats in metrics["durations"][kind].items():
                lines.append(f"| {group} | {stats['count']} | {stats['mean']:.1f} s | {stats['min']:.1f} s | "
                             f"{stats['max']:.1f} s | {stats['too_long']} | {stats['too_short']} |")
        lines.append("")

    totals, beats = context["totals"], context["beats"]
    outcome = {True: "yes", False: "no", None: "tie or not rated"}[beats]
    lines += ["## 5. Against the random selection", ""]
    if RANDOM in variants:
        lines.append(f"The full edit beats the random one in this cycle: {outcome}.")
    else:
        lines.append("This cycle has no random variant.")
    lines += [f"Over all cycles: {totals['full_beats_random']} win(s) in {totals['comparisons']} comparison(s), "
              f"{totals['cycles']} cycle(s).", ""]

    previous = context["previous"]
    lines += ["## Compared with the previous cycle", ""]
    if previous is None:
        lines.append("This is the first cycle: it sets the baseline.")
    else:
        lines.append(f"Cycle {previous['cycle']}: configuration {'unchanged' if previous['same_configuration'] else 'changed'}, "
                     f"code {'unchanged' if previous['same_code'] else 'changed'}.")
        for key in ("full_rating", "full_repetitive_share", "full_too_long_share", "full_too_short_share",
                    "important_recall"):
            value = previous[key]
            lines.append(f"- {key.replace('_', ' ')}: {'-' if value is None else f'{value:+.2f}'}")
    return "\n".join(lines) + "\n"


def _printed(run_dir: Path, context: dict) -> list[str]:
    variants = context["variants"]
    lines = [f"Evaluation, cycle {context['cycle']}:"]
    for name, metrics in variants.items():
        lines.append(f"  {name:<15} rating {_number(metrics['rating'])}, rank {_number(metrics['rank'])}, "
                     f"repetitive {_percent(metrics['repetitive_share'])}, too long {_percent(metrics['too_long_share'])}, "
                     f"too short {_percent(metrics['too_short_share'])}")
    important = context["important"]
    lines.append(f"  important moments left out: {important['flagged']} of {important['gallery']} "
                 f"(most often: {important['most_frequent_reason'] or '-'})")
    if context["note"]:
        lines.append(f"  note: {context['note']}")
    lines += [f"Report: {run_dir / REPORT_MD_NAME}", f"Anonymous summary (the file to share): {run_dir / SUMMARY_NAME}"]
    return lines

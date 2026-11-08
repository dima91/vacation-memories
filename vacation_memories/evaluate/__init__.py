"""Perceived quality evaluation (Milestone 2): the `evaluate` subcommand and what it builds on.

- `ground_truth`: what a synthetic vacation contains, written by its generator;
- `metrics`: how a plan does against that truth;
- `synthetic`: `evaluate synthetic`, the full plan compared with the random baseline on a synthetic vacation;
- `prepare`: `evaluate prepare`, previews of the variants and the review page, for a blind comparison;
- `page`: `review.html`, its data model and the `ratings.json` it exports;
- `report`: `evaluate report`, metrics, private report and anonymous summary from the ratings;
- `labels`: flags kept per stable segment across cycles, the history and the anonymous ids.
"""

# PokerBank

Compute-only mirror of a private poker study tool: the `pn` package and a vendored
[TexasSolver](https://github.com/bupticybee/TexasSolver) (AGPL-3.0, see `third_party/texassolver/LICENSE`).
Its one job is the `fill` workflow, which solves heads-up flop spots in shards on GitHub-hosted runners.
Inputs and results are encrypted; nothing here is meant to run on its own.

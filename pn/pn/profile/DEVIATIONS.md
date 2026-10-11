# Deviations and interpretation choices (pn.profile vs contracts/profile.md and the brief)

## Additions (superset of the contract; consumers can ignore them)
- Positional (PT4-style) blocks: `by_position` keys include `BTN_HU`/`BB_HU` and `IP`/`OOP` and
  cover every stat, plus `by_position_vs_opener`, `preflop_vs_gto` and
  `bet_size_hist_by_position`. `leaks` and the hero review use positional cells only.
  Definitions: `POSITIONS.md`.
- `stats` also has `rfi` (raise first in) and `four_bet` (re-raise facing exactly two raises).
  They feed range inference and tips.
- The profile has a top-level `priors_source` (`seed` | `rebuilt`).
- Each `vs.<id>` entry also has `call_vs_their_bet` and `hands_b_involved`, the number of
  hands summed into `net_vs`.
- Each `preflop_range_est.<pos>.<action>` entry also has `ci90`, `prior_width_pct` and
  `freq {n, successes}`.
- Each `lines[].shown[]` entry also has `hand_id`.
- `locks` also has `preflop:*:first`, `<street>:*:facing_raise`, and per-size heads-up
  path keys (`flop:bet33:facing_bet`, `flop:check-bet33:facing_bet`, `turn:bet150:...`). These
  are built from `fold_vs_size`, with call:raise split in the generic ratio. They follow the
  grammar but only match heads-up trees. The engine/adapter must let an explicit path override
  `*`. Multiway per-size locks are v2. Every lock carries `"order": "strength"`.

- Contract additions requested by the lead (2026-10-01):
  - `stats.fold_to_squeeze` / `stats.call_vs_squeeze`. The opportunity belongs to the opener
    and to every player who called the open before the squeeze. It is their first decision
    facing the squeeze; success is fold / call. A 4-bet counts as neither.
  - `preflop_size_hist.three_bet.{all, ip, oop, callers_0, callers_1plus}`: counts per
    bucket `<2.5x`, `2.5-3.5x`, `3.5-4.5x`, `>4.5x` (half-open, so exactly 4.5x is `>4.5x`),
    plus `n` and `mean_multiple`. The multiple is the 3-bet's raise-to divided by the open's
    raise-to. IP/OOP is the 3-bettor's postflop position vs the opener: SB, BB, STR, UTG… then
    BTN last, which also holds heads-up. `callers` is the number of calls between the open
    and the 3-bet. All-in 3-bets are included, so jams land in `>4.5x`.
  - `by_depth.{<=60bb, 60-150bb, 150-250bb, >250bb}` for vpip, pfr, three_bet,
    fold_to_three_bet, cbet_flop and fold_to_cbet_flop. Depth is the effective stack at hand
    start, min(own stack, largest other stack) / `blinds.bb`, with upper bounds inclusive.
    Prior = the player's overall posterior. `gto` = the 100bb default baseline.
  - Every stat object has `gto`, `dev_pop` and `dev_gto` (contract). Pairwise `vs` objects
    get `gto` for vpip_when_in / three_bet_vs and `null` otherwise. The top-level
    `gto_source` is `seed` | `solver`.
  - `leaks[]` follows the contract, plus `ci90` and `dev_gto`. Candidates are the overall,
    by_position, by_players_in_pot and fold_vs_size stats, with n ≥ 10 (`MIN_LEAK_N`). Below
    that the posterior mostly restates the population prior. AF leaks are ranked on the
    aggr/(aggr+calls) scale so they are comparable with frequencies. At most 15 are kept.
  - `tips` takes the strongest leak per stat first (the full list stays in `leaks`), then
    population deviations not already covered, then pairwise tips. The cap is 12.

- `pn review <hero>` (REVIEW.md) is a CLI/JSON output, not a profile field. To support it,
  the walker records which opponents each opportunity was against (`PlayerFacts.vs`), and
  `fold_vs_size` entries carry the aggressor.
- `pn charts show|validate|import` manage config/preflop_charts. Charts are pure, with 3x
  opens (lead decision 2026-10-01); see the README there.

- GTO Wizard data (2026-10-01): `pn.profile.gtowizard` computes preflop baselines from
  `data/gtowizard/*_freqs.json`, and `pn baselines import-gtowizard` merges them. The charts
  loader prefers `gtowizard_<depth>bb.txt`; the mapping is in config/preflop_charts/README.md.
  `pn charts validate/show/import` also cover the GTO Wizard files. Since `open UTG` and
  similar keys now exist in several files, `import` needs `--file`. Range inference still uses
  100bb charts.

## Lock situations (match crates/tree lock matching)
`first` / `facing_bet` / `facing_raise` mean 0 / 1 / ≥2 **voluntary** bets or raises on the
street before the decision. Preflop, blinds and straddles are posts, not bets. Facing only the
blinds is `first`, facing an open is `facing_bet`, and facing a 3-bet or more is
`facing_raise`. This is what crates/tree `Situation` matches (`voluntary == 0/1/>=2`).
In the action counts used for rebuilt priors, a preflop BB check counts as `call`
(passive continue) and a free fold counts as `check` (`priors.canonical_action`).

## Stat definitions (full list in pn/profile/stats.py docstring)
- AF stat objects report `post_mean`, `ci90` and `prior_mean` on the AF scale. `n` counts
  bets+raises+calls. The posterior is computed on `aggr/(aggr+calls)` (see PRIORS.md).
- Walks (everyone folds to the BB) are excluded from the BB's VPIP/PFR opportunities.
- `cold_call` includes the blinds (e.g. BB flatting an open). Use `by_position` to separate them.
- A limp-reraise counts as a 3-bet. `fold_to_three_bet` applies only to the original opener.
- A straddle post is not voluntary. Calling the straddle unopened is a limp. Opening over
  limpers is not RFI.
- `fold_vs_size` covers any first bet faced on a street, not only c-bets. Raises are excluded.
  Buckets use the nearest solver size in log space (geometric midpoints): flop <28.7 → 25,
  <40.6 → 33, <70.7 → 50, else 100; turn/river <106.1 → 75, else 150. Bet % = bet / pot before
  the bet.
- `bet_size_hist` buckets are half-open: [0,30), [30,45), [45,70), [70,120), [120,∞).
- `by_players_in_pot`: hu means 2 players at the start of the street where the decision
  happened (wtsd/wsd/wwsf: players who saw the flop), multiway means 3 or more.
- `donk_flop` / `probe_turn` require the preflop aggressor to act later on that street. An
  all-in preflop aggressor has no decision there, so it gives no opportunity.
  Probe also requires the flop to have gone unbet.
- `showdowns_seen` counts hands where the player reached showdown and both hole cards are
  known. A single shown card (a PokerNow option) is ignored everywhere.
- `post_missed_bb` (ingest addition) is live money: it raises the poster's commitment, so a
  missed-BB poster can check. `post_missing_sb` and `post_ante` are dead money.
- Hands flagged `bomb_pot` or `incomplete` are skipped entirely, including `hands`.
  Run-it-twice uses the first-run board for strength classes. Any pot share counts as a win.
- `vs.fold_to_their_bet` / `call_vs_their_bet` count A's postflop decisions (not hands) facing
  a bet or raise whose last aggressor is B. `three_bet_vs` counts once per hand (A facing B's
  single raise). `net_vs` is A's net in hands where B was involved (B put chips in voluntarily
  preflop or saw the flop). It is not zero-sum: in multiway pots both players can show a
  profit "vs" each other.

## Range inference
- Table kind: 2 players = hu, 3–6 = 6max, 7+ = 8max. The estimate uses the player's most
  common table kind only; frequencies are kept per table kind (`RawStats.range_freq`).
- `call` / `3bet` priors average the vs-open charts over every opener that can act before the
  seat. Charts are grouped (EP/MP/CO/BTN/SB) rather than per seat.
- The output range is `R(width)` by the chart-first ordering. Classes get weight 1, except the
  boundary class, which is fractional. Every shown class is forced in. Mixed-frequency chart
  weights are not carried over.
- Shown hands are a biased sample (strong hands reach showdown more often). The noise term
  `ETA = 0.05` blunts this, but the bias is not modelled.
- `hand_ranking.txt` orders hands by HU equity vs a random hand, not by a solver ordering.
  It is only used to extend or trim beyond the chart.

## Integration
- `pn/pn/cli/main.py`: two lines were added, an import and `app.add_typer(profile_app)`. The
  brief said one line, but the import is unavoidable.
- The DB schema comes from `pn/pn/ingest/schema.sql` when present, else
  `pn/pn/profile/_schema_fallback.sql`. Default DB is `data/pn.sqlite` (env `PN_DB`). The config
  dir can be overridden with `PN_CONFIG_DIR`.
- pandas is not used; plain dicts and numpy/scipy were enough.
- The fixtures are hand-written as compact action scripts in
  `tests/test_profile_fixture_build.py`. A builder derives amounts, uncalled bets and nets, and
  a test pins the committed JSON to that source.

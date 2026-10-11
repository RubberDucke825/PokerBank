# Hero review (`pn review <hero>`, pn.profile.review)

Question: is each of hero's GTO deviations a **justified exploit** or a **leak** (torching)?

## Input
- Hero's `leaks` from the profile (90% CI excludes the GTO baseline, n ≥ 10; see leaks.py).
- Every opportunity behind each leak, attributed to opponents by the walker (`PlayerFacts.vs`):
  - the **aggressor faced** where there is one: fold/call vs c-bets, 3-bets, squeezes,
    sized bets (`fold_vs_size`), check-raise, cold-call, 3-bet, 4-bet; donk/probe vs the
    preflop aggressor;
  - otherwise **everyone else involved**, with weight split equally: other dealt players for
    preflop stats, other players who saw the flop for postflop stats. AF opportunities are
    weighted by hero's number of bets/raises/calls on that street.
- Each opponent's own posteriors (`stats_block`, all their hands vs anyone) and archetype.

## Rule
Every hero stat maps to the **opponent counter-stat** whose deviation would make hero's
deviation correct, with a relation (`review.COUNTER`):

| hero stat | opponent counter-stat | relation |
|---|---|---|
| fold_to_cbet_X | cbet_X | opposite (fold more vs players who c-bet less = value-heavy) |
| fold_vs_size street_b | af_street | opposite |
| fold_to_three_bet / fold_to_squeeze | three_bet / squeeze | opposite |
| call_vs_squeeze | squeeze | same |
| three_bet / squeeze / four_bet | fold_to_three_bet / fold_to_squeeze / three_bet | same |
| rfi / steal / pfr | three_bet | opposite (open more vs players who 3-bet less) |
| cold_call | rfi | same (defend wider vs wide openers) |
| limp | pfr | opposite (limp more vs passive tables) |
| vpip, cbet_X, probe_turn, af_X | fold_to_cbet_flop / fold_to_cbet_X | same (bet/play more vs over-folders) |
| check_raise_flop / donk_flop | cbet_flop | same / opposite |
| wtsd | af_river | same (call down more vs aggressive bettors) |
| wsd, wwsf | – | results, not decisions → always `unknown` |

The **required opponent sign** is hero's direction (+1 over, −1 under) × relation.

For each opponent, the counter-stat sign is decided as follows:
1. **Measured**, when the counter-stat has n ≥ 10 (`MIN_OPP_N`). The sign of its CI-aware
   `dev_gto` gives +1 or −1. If the 90% CI contains GTO and is tight (width ≤ 0.25), the
   opponent is GTO-like (0). If the CI is wider, the read is inconclusive and step 2 applies.
2. **Archetype** fallback, when the archetype's confidence is at least 0.3.
   - Aggression counter-stats: aggressive/lag → +1; nit, calling_station, punching_bag → −1.
   - Fold counter-stats: punching_bag/nit → +1; calling_station, aggressive, lag → −1.
   - Looseness counter-stats (rfi, pfr, vpip, steal): lag, calling_station, aggressive → +1;
     nit, tag → −1.
3. Otherwise **unknown**.

Each opportunity's weight goes to one bucket:
- **support**: the opponent's sign equals the required sign;
- **against**: the opposite sign, or GTO-like. A deviation is not warranted against opponents
  who play that spot near GTO;
- **unknown**.

Verdict:
- `unknown` if the known share (support + against) is below 50% of the opportunities
  (`MIN_COVERAGE`). The opponent samples are too small to judge.
- `justified` if support > against.
- `leak` otherwise.

The reason sentence gives the support share, the counter-stat and direction, the share that
was GTO-like or opposite, and the main opponents with their archetypes.

## Cost (EV proxy)
`cost = |dev_gto| × n`. The deviation is CI-aware, so it is already the conservative distance
from GTO; AF is measured on the aggr/(aggr+calls) scale. Rows are sorted by cost, so the most
expensive patterns come first. Cost measures size only. A justified deviation with a high cost
is a profitable exploit to keep using.

## Limitations
- The GTO seeds are 6-max aggregates (config/GTO_BASELINES.md). In heads-up and
  short-handed games, hero's preflop looseness is over-flagged.
- Opponent attribution is per opportunity. It ignores what happened later in the hand and
  hand-strength selection. It judges frequencies, not individual hands.
- The counter-stat map is one tendency per stat (v1). A spot can have several relevant
  opponent traits. For example, cold-calling also depends on how often the opener folds
  postflop.

## EV burned per decision (`pn ev`)
The table above judges frequencies. `pn ev run [player] [--source gto]` grades
single decisions: for every voluntary action of a player whose hole cards are known (hero from
the CSV export, villains from showdowns) it solves that street from its start with the player as
hero and reads `action_ev` for the actual combo at the decision node. The stored loss is
`max(action_ev) - action_ev[chosen]` in cents (`decision_ev` table, one row per
hand/seq/source). The baseline is `gto` (chart ranges, no villain locks; Plan 7). `pn ev summary` and the review page's "EV burned"
section give totals, bb/100, by street, by spot and the costliest hands. The hand replay shows
the loss next to each graded action.

Rows with NULL EVs carry a `note`. Preflop spots with more than 3 players still in the pot
(`preflop_root_too_large`), all-in players at the street start, solver timeouts and actions that
leave the solved tree are not graded. Heads-up postflop spots use the `review` solve preset
(hu backend, at most 2 bets per street, 120 s). hu has no counterfactual EV off the solved
path, so only a player's first deviation on a street is graded exactly. Later decisions on that
street are stored as `off_path_hu`. Each row records the solve's exploitability (% of pot). Preflop rows
always get `gto_freq_chosen`, the GTO Wizard frequency of the chosen action for the hand
class. When the modeled range excludes the actual combo, it is added at weight 0.001 so it can
be graded (`combo_added_to_range`). Other solves use 300 iterations / 30 s per spot, so the numbers
are approximate. Run it overnight with `PN_EV=1 ./poker.sh study`.
`pn ev run` skips decisions already stored. After a solver or spot-builder change, `pn ev run
--force` regrades them. Off-model rows (`combo_added_to_range`) are reported apart from the
totals; `pn ev summary --include-unmodeled` counts them.

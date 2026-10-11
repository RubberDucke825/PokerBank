# Positional cells (PokerTracker 4 style)

Every frequency is reported per position, the way PT4 does it. Table-wide numbers mix seats with
very different GTO frequencies, so a "3-bet 5%" is never a leak on its own. Two views use these cells.

| view | code | where |
|---|---|---|
| profile JSON / `pn profile <id> --by-position` / web profile page | `spots.py` -> `stats.py` -> `build.py` | `by_position`, `by_position_vs_opener`, `preflop_vs_gto` |
| `pn pos <id>` / `pn pos leaks <id>` | `positional.py`, `positional_gto.py`, `positional_render.py` | standalone matrix |

Both views derive opportunities from the walker's `Decision` list (`walk.py`). They share the
seat keys and the GTO Wizard mapping below. Their shrinkage differs slightly, so the same cell can
differ by a point or two between the two views.

## Seat keys
- 6-max and short-handed seats keep the ingest label: `UTG`, `UTG1`... `LJ`, `HJ`, `CO`, `BTN`,
  `SB`, `BB`, `STR`.
- Heads-up seats are `BTN_HU` (posts the small blind) and `BB_HU`. They are kept apart because a
  heads-up button opens about 80% of hands, versus about 45% for a 6-max button.
- `IP` / `OOP` (profile only) hold postflop stats. A player is IP on a street when they act last
  among that street's actors. In `pn pos` the postflop splits are `ip`, `oop`, `hu` and
  `multiway`, where IP means last to act among the players who saw the flop.

## Preflop situations (`PlayerFacts.situ`)
`O` is the opener's key, `T` the 3-bettor's key and `P` the player's key.

| situation | when | stats |
|---|---|---|
| `("rfi", P)` | first in, no limpers | vpip, pfr, rfi, limp, steal |
| `("vs_limp", P)` | BB facing a lone SB / BTN_HU limp | vpip, pfr |
| `("vs_open", O, P)` | first decision facing one raise, nobody called before | vpip, pfr, three_bet, cold_call, fold_to_steal |
| `("cold", O, T, P)` | first decision facing open + 3-bet, nobody called before | vpip, pfr, four_bet |
| `("vs_3bet", O, T)` | opener facing a 3-bet, nobody called before | fold_to_three_bet, four_bet |
| `("vs_4bet", O, T)` | 3-bettor facing the opener's 4-bet, nobody called before | fold_to_four_bet, five_bet |
| `("vs_5bet", O, T)` | opener facing the 3-bettor's 5-bet | fold_to_five_bet, six_bet |
| `("vs_6bet", O, T)` | 3-bettor facing the opener's 6-bet | fold_to_six_bet |
| `None` | anything else: squeezes, limped or multiway pots | the stat counts; its GTO is not defined |

The 4-, 5- and 6-bet rows need a heads-up raise war (opener, 3-bettor, opener, ...). A cold
4-bet still counts for the 3-bettor's fold_to_four_bet, but it has no GTO situation.
"Nobody called before" means no limp or flat call before the decision. Squeeze and
open-limp-raise spots are not in the GTO Wizard data, so comparing them with a clean-spot
frequency would be wrong.

`group[stat]` is the opener group for vs-open stats: `EP` (UTG..LJ), `MP` (HJ), `CO`, `BTN`, `SB`
and `BTN_HU`. It feeds `by_position_vs_opener.<pos>.<group>`. `fold_to_steal` is the first
decision of the SB, BB or BB_HU facing a lone open from CO, BTN, SB or BTN_HU, with no limper
and no caller.

## GTO per cell
- **Preflop:** the mean GTO Wizard frequency over the situations the player actually faced
  (`data/gtowizard/cash6m_cev_2.5x_freqs.json`, `gtowizard.situation_freq`). The success
  actions are summed: vpip = raise + call. Our seats map to GTO Wizard's 6-max seats as
  follows: `UTG*`/`LJ` -> UTG, `HJ`/`CO`/`BTN`/`SB`/`BB` keep their names, and
  `BTN_HU`/`BB_HU` -> SB/BB. `STR` has no spot.
  - The profile uses this value when at least half of the cell's opportunities map to a spot.
    Otherwise it falls back to `config/gto_baselines.json`.
  - `pn pos` uses the nearest available depth, 100 or 200 bb, by effective stack, and falls back
    to 100 bb.
- **Postflop:** these are seeds from `config/gto_baselines.json`. A position cell uses `default`.
  `IP`/`OOP`/`hu`/`multiway` cells use their own entry when it exists.

## Shrinkage
Hierarchy is population -> position -> player (`PRIORS.md`). A position cell's prior is the
population position mean, or the table-wide prior with half the pseudo-count when no position
prior exists. That prior is shifted on the logit scale by the player's pooled deviation for the
stat. In `pn pos`, a postflop split uses the same prior as its position cell, not the position
posterior, because that posterior already contains the split's hands.

## Leaks
Leaks are cells with n >= 10 whose 90% CI excludes GTO. They are ranked by |dev_gto| (profile),
or by |dev_gto| x n (`pn pos`). The profile's candidates are `by_position` (including IP/OOP),
`by_position_vs_opener` and `fold_vs_size`. `pn pos` skips a split that holds every hand of its
position cell, because it would repeat that cell. The hero review (`review.py`) matches each
opportunity to the leak's cell through `pos_key`, `group` and `ipos`.

# Archetypes (pn.profile.archetype)

The labels follow the exploitative framing of Uri Peleg's Upswing course (aggressive, calling
station, punching bag) plus the standard HUD types (nit, TAG, LAG).

## Scoring

All inputs are posterior means, so small samples are already pulled toward the population.
Two building blocks:

    up(x, c, s)   = 1 / (1 + exp(-(x - c)/s))     0.5 at the centre c, rises with x
    down(x, c, s) = 1 / (1 + exp(-(c - x)/s))     0.5 at c, rises as x falls

`af` is the mean of af_flop, af_turn and af_river. `ratio = pfr / vpip`.

| label | score | reads as |
|---|---|---|
| aggressive | mean(up(af, 2.6, 0.5), up(cbet_flop, 0.72, 0.06), up(cbet_turn, 0.62, 0.07), up(three_bet, 0.09, 0.02)) | bets and raises a lot, barrels, 3-bets |
| calling_station | mean(up(vpip, 0.35, 0.05), down(ratio, 0.45, 0.08), down(fold_to_cbet_flop, 0.38, 0.06), up(wtsd, 0.34, 0.03)) | plays many hands passively, rarely folds, goes to showdown |
| punching_bag | mean(up(fold_to_cbet_flop, 0.58, 0.06), down(wtsd, 0.26, 0.03), down(af, 1.4, 0.4)) | folds too much postflop and seldom fights back |
| nit | down(vpip, 0.17, 0.025) | plays very few hands |
| tag | geomean(up(vpip, 0.17, 0.02), down(vpip, 0.27, 0.02), up(ratio, 0.70, 0.07)) | VPIP 17–27% and raises most of what it plays |
| lag | geomean(up(vpip, 0.28, 0.03), up(ratio, 0.65, 0.07), up(pfr, 0.21, 0.03)) | VPIP above 28%, mostly raising, PFR above 21% |

The geometric mean in tag and lag makes them conjunctive: every condition must hold. The
exploit labels use arithmetic means, so most symptoms are enough.

## Label and confidence

- `label` = the highest-scoring archetype, if that score is ≥ 0.5 and the player has ≥ 30
  hands. Otherwise `unknown`.
- `confidence` = best score × hands / (hands + 75). A perfect fit at 75 hands gives 0.5.
- `scores` lists all six, so a consumer can see the runner-up (e.g. a LAG who is also aggressive).

## Centre values

Centres sit about one between-player SD (see PRIORS.md) from the population mean in the
exploitable direction. For example, fold_to_cbet_flop has population 0.48 and SD 0.12, so the
punching-bag centre is 0.58 and the calling-station centre is 0.38. A population-average player
scores below 0.5 on every label and is classed `unknown`, which a test checks. The highest is
tag at about 0.43, because the seed VPIP of 0.28 sits just above the TAG band.

# Priors and shrinkage (pn.profile)

## Model

Every frequency stat is Beta-Binomial. With population mean `m` and prior strength `k`
(pseudo-observations), a player with `s` successes in `n` opportunities has posterior
`Beta(m·k + s, (1−m)·k + n − s)`, so

    post_mean = (m·k + s) / (k + n)          ci90 = Beta quantiles 5% / 95%

`k` is the sample size at which the player's own data and the population get equal weight.
Multinomial action distributions (lock frequencies) use `Dirichlet(k·p_pop) + counts`, which is
the same rule per component; marginal intervals are Beta(α_i, α_0 − α_i).

Aggression factor `AF = (bets+raises)/calls` is a ratio, so it is modelled through
`p = aggr/(aggr+calls)` (Beta-Binomial) and reported on the AF scale `p/(1−p)`; `n` is
bets+raises+calls.

## Hierarchy: population → position → player

1. **Population**: `config/population_priors.json` `stats.<stat>.{mean,k}`.
2. **Player overall**: prior = population mean, strength `k`.
3. **Player by position**: prior mean = position-level population mean (`by_position`, or the
   population mean if absent) shifted on the logit scale by the player's overall deviation:
   `logit(m_pos) + logit(player_post) − logit(m_pop)`. A loose player is assumed loose in every
   seat, but the seat still matters (BTN wider than UTG).
4. **Player hu / multiway** split: prior = the player's overall posterior.
5. **fold_vs_size** per bucket: population bucket mean shifted by the player's
   `fold_to_cbet_<street>` deviation; `k = fold_vs_size.k`.
6. **Pairwise** (player A vs B): prior = A's own overall posterior (A's VPIP, 3-bet, or
   postflop fold/call rate vs aggression).

## Choosing k

### Empirical Bayes (preferred, `pn priors rebuild`)

For each stat, pool every player with ≥ `--min-hands` (default 100) hands and fit the
beta-binomial marginal likelihood `Π BetaBinom(s_i | n_i, m·k, (1−m)·k)` by maximum likelihood
over `(m, k)` (L-BFGS-B on `(logit m, log k)`). This is the standard empirical-Bayes
estimate of a beta prior from many units with different sample sizes
(Efron & Morris 1975; Casella 1985; Robinson 2017). `k` is clipped to [2, 400]; hitting 400 means
no detectable between-player variance (the stat is then mostly noise, so shrink hard).
A fit needs ≥ 8 players with data (`MIN_PLAYERS_EB`). With fewer, the mean is replaced by the
pooled rate (if pooled n ≥ 30) and the seed `k` is kept. Position means, action distributions
and fold-vs-size buckets are pooled rates. `_meta.fit_report` records which rule each stat used.

### Seed values (hand-set, used until the DB is large enough)

For a Beta prior, the between-player variance is `m(1−m)/(k+1)`, so a between-player standard
deviation `sd` implies

    k = m(1−m)/sd² − 1

The seeds take typical live low-stakes means and a between-player SD read off the spread of
HUD stats seen in such games (e.g. VPIP spans roughly 15–55%, SD ≈ 0.10). The `sd` used is
stored next to each seed. For AF the SD is on the `p = aggr/(aggr+calls)` scale.

| stat | mean | sd | k | note |
|---|---|---|---|---|
| vpip | 0.28 | 0.098 | 20 | converges fastest; every hand is an opportunity |
| pfr | 0.17 | 0.070 | 28 | |
| rfi | 0.20 | 0.080 | 24 | |
| three_bet | 0.06 | 0.030 | 60 | rare event, tight spread: shrink hard |
| four_bet | 0.12 | 0.064 | 25 | |
| fold_to_three_bet | 0.55 | 0.121 | 16 | |
| limp | 0.22 | 0.101 | 16 | live games vary a lot |
| cold_call | 0.15 | 0.078 | 20 | |
| steal | 0.30 | 0.100 | 20 | |
| squeeze | 0.05 | 0.034 | 40 | |
| fold_to_squeeze | 0.55 | 0.130 | 14 | live players flat squeezes often |
| call_vs_squeeze | 0.38 | 0.120 | 15 | |
| cbet_flop / turn / river | 0.62 / 0.50 / 0.45 | 0.118 / 0.125 / 0.138 | 16 / 15 / 12 | |
| fold_to_cbet_flop / turn / river | 0.48 / 0.42 / 0.40 | 0.121 / 0.127 / 0.136 | 16 / 14 / 12 | |
| check_raise_flop | 0.08 | 0.049 | 30 | |
| donk_flop | 0.10 | 0.059 | 25 | |
| probe_turn | 0.25 | 0.108 | 15 | |
| af_flop / turn / river | 1.8 / 1.6 / 1.4 | ≈0.105 (p scale) | 20 | |
| wtsd | 0.32 | 0.062 | 55 | spread is narrow relative to noise |
| wsd | 0.50 | 0.064 | 60 | mostly card luck: shrink hard |
| wwsf | 0.44 | 0.069 | 50 | |
| fold_vs_size buckets | see file | – | 12 | split of fold_to_cbet across sizes |
| action Dirichlets | see file | – | 15 (10 facing raise) | |

### Cross-check against HUD convergence rules of thumb

Posterior weight on the player's data is `n/(n+k)`. With the seeds, VPIP is half
player-driven after 20 hands and 83% after 100, matching the common advice that VPIP/PFR are
usable after a few dozen hands and solid after ~100 (BlackRain79; 2+2 "minimum samples"
threads). 3-bet needs 60 *opportunities* for half weight, i.e. a few hundred hands, matching
the "3-bet needs 300+ hands" rule. Postflop stats have few opportunities per hand, so with
k ≈ 12–16 they still need dozens of hands before they move much. That is the intended
behaviour: tips and archetypes only fire once the credible interval clears the population mean.

## References

- B. Efron, C. Morris (1975). Data analysis using Stein's estimator and its generalizations.
  *JASA* 70(350):311–319. (Shrinking batting averages toward the group mean.)
- G. Casella (1985). An introduction to empirical Bayes data analysis. *The American
  Statistician* 39(2):83–87.
- A. Gelman et al. (2013). *Bayesian Data Analysis*, 3rd ed., §5.3 (hierarchical
  beta-binomial, rat-tumor example; the `(m, k)` parametrisation).
- D. Robinson (2017). *Introduction to Empirical Bayes: Examples from Baseball Statistics*;
  blog version: "Understanding empirical Bayes estimation (using baseball statistics)",
  varianceexplained.org/r/empirical_bayes_baseball/.
- HUD sample-size rules of thumb: BlackRain79, "Poker HUD Stat Sample Sizes Explained",
  blackrain79.com/2017/11/poker-hud-stat-sample-size.html; Two Plus Two forum thread
  "Minimum samples for HUD stats".

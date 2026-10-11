"""Beta-Binomial / Dirichlet shrinkage and empirical-Bayes prior fitting. See PRIORS.md."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
from scipy import optimize, stats

EPS = 1e-3
K_MIN, K_MAX = 2.0, 400.0
CI_LEVEL = 0.90


def _clip(p: float) -> float:
    return min(1.0 - EPS, max(EPS, p))


def logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1.0 - p))


def expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def shift_prior(base_mean: float, player_mean: float, pop_mean: float) -> float:
    """Hierarchical prior: base (e.g. position-level) mean shifted by the player's overall
    deviation from the population, additively on the logit scale."""
    return expit(logit(base_mean) + logit(player_mean) - logit(pop_mean))


def beta_binomial(successes: int, n: int, prior_mean: float, k: float) -> dict:
    """Posterior for a frequency: Beta(m*k + s, (1-m)*k + n - s).

    Returns the contract stat object {post_mean, n, k, ci90, prior_mean}.
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"bad counts s={successes} n={n}")
    m = _clip(prior_mean)
    a = m * k + successes
    b = (1.0 - m) * k + (n - successes)
    tail = (1.0 - CI_LEVEL) / 2.0
    lo, hi = stats.beta.ppf([tail, 1.0 - tail], a, b)
    return {
        "post_mean": round(a / (a + b), 4),
        "n": int(n),
        "k": round(float(k), 2),
        "ci90": [round(float(lo), 4), round(float(hi), 4)],
        "prior_mean": round(float(prior_mean), 4),
    }


def af_posterior(aggr: int, total: int, prior_af: float, k: float) -> dict:
    """Aggression factor (bets+raises)/calls via p = aggr/(aggr+calls) ~ Beta-Binomial.

    post_mean / ci90 / prior_mean are reported on the AF scale p/(1-p); n = bets+raises+calls.
    """
    prior_p = prior_af / (1.0 + prior_af)
    post = beta_binomial(aggr, total, prior_p, k)

    def to_af(p: float) -> float:
        p = min(p, 0.999)
        return round(p / (1.0 - p), 3)

    return {
        "post_mean": to_af(post["post_mean"]),
        "n": post["n"],
        "k": post["k"],
        "ci90": [to_af(post["ci90"][0]), to_af(post["ci90"][1])],
        "prior_mean": round(prior_af, 3),
    }


def dirichlet(counts: Mapping[str, int], prior: Mapping[str, float], k: float) -> dict[str, float]:
    """Posterior mean of a multinomial action distribution under Dirichlet(k * prior)."""
    total_prior = sum(prior.values())
    if total_prior <= 0:
        raise ValueError("prior must have positive mass")
    keys = list(prior)
    n = sum(counts.get(a, 0) for a in keys)
    return {a: (k * prior[a] / total_prior + counts.get(a, 0)) / (k + n) for a in keys}


def dirichlet_marginal_ci(
    counts: Mapping[str, int], prior: Mapping[str, float], k: float
) -> dict[str, list[float]]:
    """90% marginal intervals: each component of a Dirichlet is Beta(alpha_i, alpha_0 - alpha_i)."""
    total_prior = sum(prior.values())
    alphas = {a: k * prior[a] / total_prior + counts.get(a, 0) for a in prior}
    a0 = sum(alphas.values())
    tail = (1.0 - CI_LEVEL) / 2.0
    return {
        a: [round(float(x), 4) for x in stats.beta.ppf([tail, 1 - tail], al, a0 - al)]
        for a, al in alphas.items()
    }


def fit_beta_binomial(pairs: Sequence[tuple[int, int]]) -> tuple[float, float] | None:
    """Empirical-Bayes MLE of (mean, k) from per-player (successes, n). None if degenerate.

    k is clipped to [K_MIN, K_MAX]; K_MAX means 'no detectable between-player variance'.
    """
    data = [(s, n) for s, n in pairs if n > 0]
    if len(data) < 2:
        return None
    s = np.array([d[0] for d in data], dtype=float)
    n = np.array([d[1] for d in data], dtype=float)
    pooled = _clip(float(s.sum() / n.sum()))

    def nll(theta: np.ndarray) -> float:
        m = expit(float(theta[0]))
        k = math.exp(float(theta[1]))
        return -float(stats.betabinom.logpmf(s, n, m * k, (1 - m) * k).sum())

    x0 = np.array([logit(pooled), math.log(20.0)])
    bounds = [(-7.0, 7.0), (math.log(K_MIN), math.log(K_MAX))]
    res = optimize.minimize(nll, x0, method="L-BFGS-B", bounds=bounds)
    if not res.success and not np.isfinite(res.fun):
        return None
    return expit(float(res.x[0])), float(math.exp(res.x[1]))

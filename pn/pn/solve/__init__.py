"""Solve service (contracts/solve_service.md): range sources, spot building, backends, cache,
subgames and the action-EV table."""

from pn.solve.backend import SolveError, choose_backend, run_backend
from pn.solve.build import SpotError, build, build_spot
from pn.solve.cache import SolveCache, spot_key
from pn.solve.ev import EvUnavailable, action_ev_loss
from pn.solve.request import SOURCES, Budget, RangeSource, Seat, SpotRequest
from pn.solve.subgame import SubgameError, subgame, subgame_request

__all__ = ["SOURCES", "Budget", "EvUnavailable", "RangeSource", "Seat", "SolveCache",
           "SolveError", "SpotError", "SpotRequest", "SubgameError", "action_ev_loss", "build",
           "build_spot", "choose_backend", "run_backend", "spot_key", "subgame",
           "subgame_request"]  # fmt: skip

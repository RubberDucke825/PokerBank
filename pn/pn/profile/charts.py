"""Loader for config/preflop_charts (range text keyed by action/position) and hand ranking."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pn import ranges
from pn.profile.paths import config_dir

TABLE_FILES = {"hu": ("common.txt", "hu.txt"), "6max": ("common.txt", "6max.txt"),
               "8max": ("common.txt", "8max.txt")}  # fmt: skip
ORDER = {
    "hu": ("BTN", "BB"),
    "6max": ("LJ", "HJ", "CO", "BTN", "SB", "BB"),
    "8max": ("UTG", "UTG1", "UTG2", "LJ", "HJ", "CO", "BTN", "SB", "BB"),
}
OPENER_GROUP = {"UTG": "EP", "UTG1": "EP", "UTG2": "EP", "LJ": "EP", "HJ": "MP", "CO": "CO",
                "BTN": "BTN", "SB": "SB"}  # fmt: skip
ACTIONS = ("open", "call", "3bet", "4bet", "call_3bet", "5bet", "call_4bet")
# GTO Wizard 6-max files (pure, scraped; see README). Our position -> their seat name: their
# UTG is our 6-max LJ (and 8-max LJ: same number of players behind). As OPENER, 8-max
# UTG/UTG1/UTG2 map to their UTG (approximation: slightly wide). As HERO, 8-max UTG/UTG1/UTG2
# have no equivalent and fall back to the hand-written charts.
GTOW_DEPTHS = (100, 200)
GTOW_HERO = {"LJ": "UTG", "HJ": "HJ", "CO": "CO", "BTN": "BTN", "SB": "SB", "BB": "BB"}
GTOW_OPENER = {**GTOW_HERO, "UTG": "UTG", "UTG1": "UTG", "UTG2": "UTG"}
# PokerCoaching 9-handed 100bb charts (pure; tools/charts_from_pdf.py): consulted before GTO
# Wizard. 6-max uses LJ..BB. 8-max UTG/UTG1 read the 9-handed UTG1/UTG2 seats (same players
# behind), as hero and as the opener/3-bettor in "vs X" keys; LJ..BB map directly. Ingested 7- /
# 9-handed games (table kind "8max" too) shift by their own count: `players`, default 8.
PC_FILE = "pokercoaching_100bb.txt"
PC_EP = ("UTG", "UTG1", "UTG2")  # the 9-handed seats in front of LJ


class ChartError(KeyError):
    """No chart entry for the requested spot."""


def charts_dir() -> Path:
    return config_dir() / "preflop_charts"


def table_kind(n_players: int) -> str:
    if n_players <= 2:
        return "hu"
    return "6max" if n_players <= 6 else "8max"


def normalize_position(position: str, table: str) -> str:
    """Map ingest positions onto chart positions (HU SB->BTN, STR->BB, 6max UTG->LJ)."""
    if table == "hu":
        return "BTN" if position in ("SB", "BTN") else "BB"
    if position == "STR":
        return "BB"
    if table == "6max" and position in ("UTG", "UTG1", "UTG2"):
        return "LJ"
    return position


def hero_group(position: str) -> str:
    return position if position in ("SB", "BB") else "IP"


@lru_cache(maxsize=16)
def _read(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, text = line.partition("=")
        if not sep:
            raise ValueError(f"bad chart line in {path}: {raw!r}")
        out[" ".join(key.split())] = text.strip()
    return out


def load_table(table: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for name in TABLE_FILES[table]:
        merged.update(_read(str(charts_dir() / name)))
    return merged


def gtow_file(depth_bb: int) -> str:
    return f"gtowizard_{depth_bb}bb.txt"


def _gtow_depths(depth_bb: float) -> list[int]:
    nearest = min(GTOW_DEPTHS, key=lambda d: abs(d - depth_bb))
    return [nearest] + [d for d in (100,) if d != nearest]


def _gtow_key(table: str, action: str, pos: str, other: str | None) -> str | None:
    if table not in ("6max", "8max"):
        return None
    hero = GTOW_HERO.get(pos)
    if hero is None:
        return None
    if action == "open":
        return f"open {hero}"
    vs = GTOW_OPENER.get(normalize_position(other, table)) if other else None
    if vs is None:
        return None
    return {
        "call": f"call {hero} vs {vs}",
        "3bet": f"3bet {hero} vs {vs}",
        "4bet": f"4bet {hero} vs3bet {vs}",
        "call_3bet": f"call {hero} vs3bet {vs}",
        "5bet": f"5bet {hero} vs4bet {vs}",
        "call_4bet": f"call {hero} vs4bet {vs}",
        "6bet": f"6bet {hero} vs5bet {vs}",
        "call_5bet": f"call {hero} vs5bet {vs}",
        "call_6bet": f"call {hero} vs6bet {vs}",
    }.get(action)


def _pc_seat(pos: str | None, table: str, players: int | None) -> str | None:
    """The 9-handed seat with as many players behind (ingest names EP seats from the front)."""
    if table != "8max" or pos not in PC_EP:
        return pos
    i = PC_EP.index(pos) + 9 - (players or 8)
    return PC_EP[min(max(i, 0), len(PC_EP) - 1)]


def _pc_key(table: str, action: str, pos: str, other: str | None,
            players: int | None = None) -> str | None:  # fmt: skip
    if table not in ("6max", "8max"):
        return None
    pos = _pc_seat(pos, table, players)
    if action in ("open", "limp"):
        return f"{action} {pos}"
    vs = _pc_seat(normalize_position(other, table) if other else None, table, players)
    if vs is None:
        return None
    return {
        "call": f"call {pos} vs {vs}",
        "3bet": f"3bet {pos} vs {vs}",
        "4bet": f"4bet {pos} vs3bet {vs}",
        "call_3bet": f"call {pos} vs3bet {vs}",
        "3bet_limp_vs_raise": f"3bet {pos} limp_vs_raise {vs}",
        "call_limp_vs_raise": f"call {pos} limp_vs_raise {vs}",
        "3bet_value_limp_vs_raise": f"3bet_value {pos} limp_vs_raise {vs}",  # bots' SB limps
    }.get(action)


def pc_key(table: str, action: str, position: str, other: str | None = None, *,
           players: int | None = None) -> str | None:  # fmt: skip
    """PokerCoaching chart key for the spot (whether or not the file has it); `players` = the
    hand's player count (8-max table kind: 7 to 9, default 8)."""
    return _pc_key(table, action, normalize_position(position, table), other, players)


def pc_range(table: str, action: str, position: str, other: str | None = None, *,
             players: int | None = None) -> dict | None:  # fmt: skip
    """PokerCoaching range for the spot, or None when the PokerCoaching file does not cover it."""
    key = pc_key(table, action, position, other, players=players)
    path = charts_dir() / PC_FILE
    if key is None or not path.exists():
        return None
    text = _read(str(path)).get(key)
    return ranges.parse(text) if text is not None else None


def gtow_range(table: str, action: str, position: str, other: str | None = None,
               depth_bb: float = 100) -> dict | None:  # fmt: skip
    """GTO Wizard range for the spot, or None when no GTO Wizard file covers it."""
    key = _gtow_key(table, action, normalize_position(position, table), other)
    if key is None:
        return None
    for depth in _gtow_depths(depth_bb):
        path = charts_dir() / gtow_file(depth)
        if path.exists():
            text = _read(str(path)).get(key)
            if text is not None:
                return ranges.parse(text)
    return None


def chart_range(table: str, action: str, position: str, opener: str | None = None,
                depth_bb: float = 100, *, players: int | None = None) -> dict:  # fmt: skip
    """Range (combo -> weight) for one chart spot. PokerCoaching 100bb charts first (6-max/
    8-max, any depth), then GTO Wizard files (depth nearest of 100/200bb, then 100bb), then the
    hand-written charts.

    action: open | call | 3bet (vs `opener`) | 4bet / call_3bet (opener `position` facing a
    3bet; `opener` = the 3-bettor if known) | 5bet / call_4bet (3bettor facing a 4bet from
    `opener`). PokerCoaching only: limp (SB) | 3bet_limp_vs_raise / call_limp_vs_raise (SB
    limped, `opener` = BB raised). `players`: as pc_key.
    """
    pc = pc_range(table, action, position, opener, players=players)
    if pc is not None:
        return pc
    gtow = gtow_range(table, action, position, opener, depth_bb)
    if gtow is not None:
        return gtow
    pos = normalize_position(position, table)
    data = load_table(table)
    if action == "open":
        key = f"open {pos}"
    elif action in ("call", "3bet"):
        if opener is None:
            raise ChartError("opener required for call/3bet")
        op = normalize_position(opener, table)
        op_key = op if table == "hu" else OPENER_GROUP.get(op, "EP")
        key = f"{action} {hero_group(pos) if table != 'hu' else pos} vs {op_key}"
    elif action in ("4bet", "call_3bet"):
        verb = "4bet" if action == "4bet" else "call"
        key = f"{verb} vs3bet {pos if table == 'hu' else OPENER_GROUP.get(pos, 'EP')}"
    elif action in ("5bet", "call_4bet"):
        verb = "5bet" if action == "5bet" else "call"
        group = OPENER_GROUP.get(normalize_position(opener or "BTN", table), "EP")
        key = f"{verb} vs4bet {'EP' if group in ('EP', 'MP') else 'LATE'}"
    else:
        raise ChartError(f"unknown action {action!r}")
    if key not in data:
        raise ChartError(f"no chart for {table}: {key!r}")
    return ranges.parse(data[key])


def openers_before(table: str, position: str) -> list[str]:
    order = ORDER[table]
    pos = normalize_position(position, table)
    if pos not in order:
        return []
    return [p for p in order[: order.index(pos)] if p != "BB"]


def prior_range(table: str, position: str, action: str, depth_bb: float = 100) -> dict[str, float]:
    """Chart prior for preflop_range_est: `open`, or `call`/`3bet` averaged over all openers
    that can act before `position` (equal weight per opener)."""
    if action == "open":
        return chart_range(table, "open", position, depth_bb=depth_bb)
    openers = openers_before(table, position)
    if not openers:
        raise ChartError(f"no opener can act before {position} in {table}")
    acc: dict[str, float] = {}
    for op in openers:
        for combo, w in chart_range(table, action, position, op, depth_bb).items():
            acc[combo] = acc.get(combo, 0.0) + w / len(openers)
    return acc


@lru_cache(maxsize=1)
def hand_ranking() -> tuple[str, ...]:
    """169 classes, strongest first (config/preflop_charts/hand_ranking.txt)."""
    out = []
    for raw in (charts_dir() / "hand_ranking.txt").read_text().splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        out.append(raw.split()[1])
    if sorted(out) != sorted(ranges.all_classes()):
        raise ValueError("hand_ranking.txt must list all 169 classes exactly once")
    return tuple(out)


@lru_cache(maxsize=1)
def class_percentiles() -> dict[str, float]:
    """Combo-weighted percentile (0..1) of the END of each class in the ranking."""
    out: dict[str, float] = {}
    acc = 0
    for cls in hand_ranking():
        acc += ranges.class_n_combos(cls)
        out[cls] = acc / ranges.N_COMBOS
    return out


# ---------------------------------------------------------------- validation / import

CHART_FILES = (PC_FILE, "gtowizard_100bb.txt", "gtowizard_200bb.txt", "common.txt", "6max.txt",
               "8max.txt", "hu.txt")  # fmt: skip
PURE_THRESHOLD = 0.5


def chart_entries(name: str) -> dict[str, str]:
    """Uncached key -> range text of one chart file."""
    _read.cache_clear()
    return _read(str(charts_dir() / name))


def validate_charts() -> list[dict]:
    """One row per chart line: file, key, width, error (None if OK). A line fails if it does
    not parse or carries any weight (charts must be pure)."""
    rows = []
    for name in CHART_FILES:
        path = charts_dir() / name
        if not path.exists():
            rows.append({"file": name, "key": "-", "width": None, "error": "missing file"})
            continue
        for key, text in chart_entries(name).items():
            row = {"file": name, "key": key, "width": None, "error": None}
            try:
                d = ranges.parse(text)
                row["width"] = ranges.width(d)
                if ":" in text or any(w != 1.0 for w in d.values()):
                    row["error"] = "weighted range (charts must be pure)"
            except ranges.RangeParseError as e:
                row["error"] = str(e)
            rows.append(row)
    try:
        hand_ranking.cache_clear()
        hand_ranking()
    except (OSError, ValueError, IndexError) as e:
        rows.append({"file": "hand_ranking.txt", "key": "-", "width": None, "error": str(e)})
    return rows


def _strip_brackets(text: str) -> str:
    """Flopzilla/PokerStove style `[75]AKs,AQs[/75]` -> `AKs:0.75,AQs:0.75`."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "[" and not text.startswith("[/", i):
            j = text.index("]", i)
            w = float(text[i + 1 : j]) / 100.0
            end = text.index("[/", j)
            close = text.index("]", end)
            out += [f"{t.strip()}:{w}" for t in text[j + 1 : end].split(",") if t.strip()]
            i = close + 1
        else:
            j = text.find("[", i)
            j = len(text) if j == -1 else j
            out += [t.strip() for t in text[i:j].split(",") if t.strip()]
            i = j
    return ",".join(out)


def pure_range(text: str, threshold: float = PURE_THRESHOLD) -> dict[str, float]:
    """Parse an exported range (Pio / GTO Wizard `AA:1,AKs:0.75`, plain comma list, or
    `[75]...[/75]` brackets) and round it to a pure range: a hand class is included when its
    mean frequency over its combos is >= threshold."""
    cleaned = " ".join(text.replace("\n", ",").split())
    if "[" in cleaned:
        cleaned = _strip_brackets(cleaned)
    d = ranges.parse(cleaned)
    out: dict[str, float] = {}
    for cls in ranges.all_classes():
        combos = ranges.class_combos(cls)
        if sum(d.get(c, 0.0) for c in combos) / len(combos) >= threshold:
            out.update(dict.fromkeys(combos, 1.0))
    return out


def files_with_key(key: str) -> list[str]:
    key = " ".join(key.split())
    return [n for n in CHART_FILES if (charts_dir() / n).exists() and key in chart_entries(n)]


def write_chart_key(name: str, key: str, text: str) -> None:
    """Replace (or append) `key = text` in chart file `name`, keeping comments/order."""
    key = " ".join(key.split())
    path = charts_dir() / name
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], False
    for raw in lines:
        k, sep, _ = raw.split("#", 1)[0].partition("=")
        if sep and " ".join(k.split()) == key:
            out.append(f"{key} = {text}")
            done = True
        else:
            out.append(raw)
    if not done:
        out.append(f"{key} = {text}")
    path.write_text("\n".join(out) + "\n")
    _read.cache_clear()

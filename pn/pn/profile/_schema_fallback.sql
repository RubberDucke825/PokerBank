-- Fallback copy of pn/pn/ingest/schema.sql (contracts/hand_history.md) used only when the
-- ingest package schema is absent. pn.profile.source prefers the ingest file.
-- PokerNow ingestion schema (contracts/hand_history.md). Amounts in cents.
CREATE TABLE IF NOT EXISTS games (
    game_id       TEXT PRIMARY KEY,
    url           TEXT,
    first_hand_at TEXT,
    last_hand_at  TEXT,
    fetched_at    TEXT
);

CREATE TABLE IF NOT EXISTS hands (
    hand_id    TEXT PRIMARY KEY,
    game_id    TEXT NOT NULL REFERENCES games(game_id),
    hand_no    INTEGER NOT NULL,
    started_at TEXT,
    n_players  INTEGER NOT NULL,
    flags      TEXT NOT NULL DEFAULT '',   -- comma-separated, sorted
    json       TEXT NOT NULL               -- full normalized hand
);
CREATE INDEX IF NOT EXISTS idx_hands_game ON hands(game_id, hand_no);

-- vpip/pfr/saw_flop/went_to_sd/won_sd: 0/1, definitions in pn/ingest/derive.py
CREATE TABLE IF NOT EXISTS hand_players (
    hand_id     TEXT NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    player_id   TEXT NOT NULL,
    seat        INTEGER,
    position    TEXT,
    stack_start INTEGER,
    net         INTEGER NOT NULL,
    cards       TEXT,                      -- e.g. 'AhKd' or NULL
    vpip        INTEGER NOT NULL,
    pfr         INTEGER NOT NULL,
    saw_flop    INTEGER NOT NULL,
    went_to_sd  INTEGER NOT NULL,
    won_sd      INTEGER NOT NULL,
    PRIMARY KEY (hand_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_hp_player ON hand_players(player_id);

-- pot_before: all chips in the middle before the action (dead money included)
-- n_active: players not folded (all-ins included) before the action
-- facing_bet_pct: last bet/raise size still faced, % of the pot it was made into; NULL if none
CREATE TABLE IF NOT EXISTS actions (
    hand_id        TEXT NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    street         TEXT NOT NULL,
    player_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    to_amount      INTEGER NOT NULL,
    allin          INTEGER NOT NULL,
    pot_before     INTEGER NOT NULL,
    facing_bet_pct INTEGER,
    n_active       INTEGER NOT NULL,
    PRIMARY KEY (hand_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_actions_player ON actions(player_id, street);

CREATE TABLE IF NOT EXISTS players (
    player_id    TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    display_name TEXT,                     -- user-settable (pn alias merge --name / pn fetch --name)
    nickname     TEXT,                     -- latest PokerNow nickname (extra column)
    first_seen   TEXT,
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    player_id    TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    note         TEXT
);

CREATE VIEW IF NOT EXISTS hand_players_canon AS
SELECT hp.*, COALESCE(p.canonical_id, hp.player_id) AS canonical_id
FROM hand_players hp LEFT JOIN players p ON p.player_id = hp.player_id;

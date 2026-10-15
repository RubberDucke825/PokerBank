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

-- EV review (contracts/solve_service.md): one row per decision of a player with known hole
-- cards, per range source. ev_*/loss in cents (NULL when not solvable; `note` says why).
-- `spot`: preflop situation kind (pn.profile.spots) or postflop first/facing_bet/facing_raise.
CREATE TABLE IF NOT EXISTS decision_ev (
    hand_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    source          TEXT NOT NULL,
    street          TEXT NOT NULL,
    player_id       TEXT NOT NULL,
    chosen          TEXT NOT NULL,
    best            TEXT NOT NULL,
    ev_chosen       INTEGER,
    ev_best         INTEGER,
    loss            INTEGER,
    gto_freq_chosen REAL,
    backend         TEXT,
    solve_key       TEXT,
    exploitability  REAL,                  -- meta.exploitability_pct_pot of the solve
    computed_at     TEXT NOT NULL,
    note            TEXT,
    spot            TEXT,
    grade           TEXT,                  -- Plan 7: best | mixed | blunder (pn.train.grading), NULL = not graded
    freq_chosen     REAL,                  -- solver (postflop) / GTO Wizard (preflop) frequency of the chosen action
    best_freq       REAL,                  -- frequency of the most frequent action
    PRIMARY KEY (hand_id, seq, source)
);
CREATE INDEX IF NOT EXISTS idx_decision_ev_player ON decision_ev(player_id, source);

-- Trainer history (contracts/trainer.md "Plan 4 additions"): written once at hand end, never for
-- an abandoned hand. net_bb = hero's net for the hand; loss_bb NULL unless the solve had EVs
-- (Plan 7: TexasSolver has none); grade best|mixed|blunder|none (ev|freq before Plan 7);
-- rng / rng_action / matched_rng NULL when the RNG was off.
-- Columns added later are also ALTERed into older databases by pn.train.stats.ensure.
CREATE TABLE IF NOT EXISTS train_hands (
    hand_uid       TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    played_at      TEXT NOT NULL,
    table_size     INTEGER NOT NULL,
    depth          INTEGER NOT NULL,
    source         TEXT NOT NULL,
    hero_pos       TEXT NOT NULL,
    net_bb         REAL NOT NULL,
    opponents_json TEXT NOT NULL,
    drill_spot     TEXT,                   -- Plan 6: spot key of a drill attempt (NULL = natural hand)
    drill_landed   INTEGER                 -- 1 when hero reached the drilled spot
);
CREATE INDEX IF NOT EXISTS idx_train_hands_played ON train_hands(played_at);

CREATE TABLE IF NOT EXISTS train_decisions (
    hand_uid     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    street       TEXT NOT NULL,
    position     TEXT NOT NULL,
    situation    TEXT NOT NULL,            -- rfi|vs_open|vs_3bet|vs_4bet+|cold|first|facing_bet|facing_raise
    hand_class   TEXT NOT NULL,
    rng          INTEGER,
    options_json TEXT NOT NULL,            -- feedback.options
    chosen       TEXT NOT NULL,
    rng_action   TEXT,
    best         TEXT,
    loss_bb      REAL,
    grade        TEXT NOT NULL,          -- best | mixed | blunder | none (pn.train.grading)
    matched_rng  INTEGER,
    pure         INTEGER NOT NULL DEFAULT 0,
    spot         TEXT,                     -- Plan 6: pn.train.spots.spot_key
    note         TEXT,                     -- e.g. solver_unavailable (decision not graded)
    board        TEXT,                     -- Plan 14: board cards at the decision ("Kd7s2c"), NULL preflop
    pot_bb       REAL,                     -- Plan 14: pot at the decision in bb
    formation    TEXT,                     -- Plan 14: "BTN vs BB - SRP" on heads-up postflop streets
    PRIMARY KEY (hand_uid, seq)
);

# pn.ingest — deviations from / additions to contracts/hand_history.md

All changes are additive: every field named in the contract is present with the contract's meaning.

## Hand JSON
- **New action kind `post_missed_bb`.** A returning player posts a missed big blind. It is live money: it counts toward `to_amount` and toward calling. Fixture hand #86 proves this, because no uncalled bet was returned to the real BB. Mapping it to `post_bb` would have created two BB posters.
- **`post_missing_sb` is dead money.** Its `amount` is the chips posted, and `to_amount` does not change.
- **Bomb pots.** The line `"P" posts a bet of N (bomb pot bet)` and the line `calls N (bomb pot bet)` both map to kind `post_ante`, which is dead money. The hand also gets the `bomb_pot` flag. This wording comes from other open-source PokerNow parsers, because the fixture contains no bomb pots.
- **New flags.**
  - `dead_button`: the hand start says "(dead button)". `button_seat` is then `null`.
  - `dead_sb`: the log has a "Dead Small Blind" line, and no player gets the SB position.
  - `double_board`: the log has "Flop (second board)" lines. These are stored in `runs` like a second run.
- **New key `issues`.** It is a list of strings with human-readable details for `pot_mismatch` and `incomplete`. It is empty for clean hands.
- **New key `showdowns[].voluntary`.** It is true when the cards were shown without reaching a contested showdown, for example a winner showing after everyone folded.
- **Meaning of `showdowns[].shown`.** It is true when the cards came from an explicit "shows" line. It is false when the cards were recovered from a "collected ... (combination: ...)" line.
- **Single-card shows.** "shows a K♠." appears in `showdowns`, but `players[].cards` stays `null` because it needs two cards.
- **New key `pots[].winners[].run`.** It is present and equal to 2 only for second-run or second-board winners.
- **Meaning of `runs`.** It has one entry per dealt run. It is `[]` when no board was dealt. A second-run line lists the full board, and any missing shared streets are filled from run 1.
- **Board with no flop.** `board` is `{"flop": [], "turn": null, "river": null}`.
- **Optional `hero_cards`.** This is a CSV export's "Your hand is" cards. They are also put in `players[].cards` with `cards_source: "hero_export"` once the hero is known. The hero is `--hero`, or else the player whose shown cards matched "Your hand is" in the same hand.
- **Optional `bounties`.** These are 7-2 bounty payments in the form `[{from, to, amount}]`, from lines like `"P" paid N for the ... bounty to "Q"`. They are included in `net`, which still sums to 0, so `net` matches the ledger. They are not part of pot math.
- **Shows after the hand ends.** "shows" lines logged after `-- ending hand #N --` and before the next start are attached to hand N. The fixture has 16 of them.

## Positions
- **Heads-up.** The button is labelled `BTN`, and that player posts the SB. The other player is `BB`.
- **Dead small blind.** There is no `SB` position. The BB is the player who actually posted the big blind.
- **Dead button.** `button_seat` is `null`. The last non-blind preflop actor is labelled `BTN`.
- **Ten-handed tables.** They get a `UTG3` position, which the contract did not list.
- **Missed big blind.** A player who posts a missed BB keeps their seat position.

## SQLite
- **Extra column `players.nickname`.** It holds the latest PokerNow nickname and is updated on every load. `display_name` is set to the nickname on first insert. After that it changes only through `--name` or `alias merge --name`, so reloads never clobber a full name.
- **Extra view `hand_players_canon`.** It is `hand_players` plus `canonical_id`.
- **`hands.flags`.** Comma-separated sorted flags.
- **`hand_players.cards`.** A string like `"AhKd"` or NULL. The boolean columns are 0/1.
- **Derived-column definitions.** These are in `derive.py` and `schema.sql`:
  - `pot_before` covers all streets and includes dead money.
  - `n_active` counts players who have not folded, all-ins included.
  - `facing_bet_pct` is the last bet or raise the actor still faces, as an integer % of the pot it was made into. A raise counts its increment over the pot after the raiser's call, so a pot-sized raise is 100. It is NULL when the actor faces only blinds or nothing.
- **Idempotent load.** Duplicate `hand_id`s are skipped. The one exception is a stored hand flagged `incomplete`, for example one fetched mid-hand, which is replaced when a complete version arrives.
- **Merging hole cards.** When a `hand_id` is already stored, any hole cards the stored hand lacks are merged in from the new version. This counts as `enriched`. It is how `pn import-csv` adds hero cards to a game first loaded from the public log, without duplicating hands. Cards from the CSV export (`hero_export`) also replace cards that came only from a showdown, because showdown cards are a biased sample of the hand's range. Nothing else in the stored hand changes, including its `source`.
- **Automatic aliases.** PokerNow lines saying a player "changed the ID from X to Y because authenticated login" are merged automatically.

## Fetch
- **`after_at` behaviour.** `after_at` is an exclusive filter, but the server still returns the *newest* 50 rows. Incremental refresh therefore pages backwards with `after_at=<cached max>&before_at=...` until it gets a short page. This was verified live on 2026-10-01.
- **Ledger download.** The ledger is downloaded from `/games/{id}/ledger_{id}.csv` to `data/raw/ledger_<id>.csv`. This file matched the fixture ledger byte for byte.
- **Local files.** `pn fetch` also accepts a local log `.json` file, either a list or the cache format. This allows offline use and tests.

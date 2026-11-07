# flattop

WW2 Pacific carrier campaign aid. The engine runs in phases, with simultaneous orders and fog of war. Battles resolve automatically or are handed off to the tabletop.

## Engine (`src/flattop`)

| Module | Role |
|---|---|
| `model.py` | `GameState`: umpire truth (task forces, bases, contacts, missions, log) |
| `orders.py` | Per-phase orders: move, CAP, search, strike |
| `engine.py` | `step()` (fixed phase), `advance()` (run until something happens), `resolve_pending()` (tabletop result in) |
| `resolve.py` | `AutoResolver` (seeded) and `TabletopResolver` (pauses for the umpire) |
| `bot.py` | Solo opponent: reads only its own `SideView`; searches, holds CAP, strikes fresh contacts, closes to strike range, retires when crippled |
| `view.py` | `view(state, side)`: the only thing a player client should receive |
| `sheet.py` | Markdown engagement sheet for tabletop play |
| `scenarios/` | Scenario JSON (Eastern Solomons, approximate) |
| `store.py` | SQLite sessions: seats, phase submissions, append-only action log |
| `server.py` | FastAPI app; a seat token is the only credential |
| `web/` | Plotting-table page (MapLibre, no build step); open `/?seat=<token>` |

## Server

```sh
FLATTOP_DB=flattop.db uv run flattop-server   # map: http://127.0.0.1:8000/  API: /docs
```

| Mode | Seats | Clock runs when | Tabletop results entered by |
|---|---|---|---|
| `solo` | one side; `bot.py` plays the other | that side submits | the solo player |
| `umpire` | allied, japan, umpire | both sides submit, or the umpire forces it | umpire |
| `two_player` | allied, japan | both sides submit | n/a (auto only) |

Each submission asks for `phase` (fixed minutes) or `run` (until an interrupt). The more cautious request wins.
Only events a player can see stop their clock. With two hidden players that means public events only; in solo the bot re-plans on its own events without stopping yours.

## Weather

Scenarios list fronts (`weather` in the JSON): a circle that drifts in a straight line and exists between `start` and `end`.
A group under cloud is harder to find: a search's chance of seeing it is multiplied by `search_factor`.
A strike must come within `acquire_nm` of it to find it, instead of 30 nm.
Weather is public; both sides see the same fronts. A search that cloud defeats reports nothing, as any miss does.

Properties the tests enforce:

- Same state and orders give the same result.
- `step` never mutates its input.
- Player views carry no enemy ids, names or true positions; every allied/japan API response over a full strike cycle is checked for this.
- The bot's orders depend only on its side's view (moving an unseen enemy changes nothing), and its private events never stop the player's clock.
- Weather is the same in both views, and moving an unseen enemy under cloud changes nothing in the other side's view.
- Ids are counted per side, so gaps in your own ids reveal nothing about the enemy.
- Concurrent final submissions resolve a phase once (`BEGIN IMMEDIATE`).

```sh
uv run pytest
```

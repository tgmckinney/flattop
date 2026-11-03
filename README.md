# flattop

WW2 Pacific carrier campaign aid. The engine runs in phases, with simultaneous orders and fog of war. Battles resolve automatically or are handed off to the tabletop.

## Engine (`src/flattop`)

| Module | Role |
|---|---|
| `model.py` | `GameState`: umpire truth (task forces, bases, contacts, missions, log) |
| `orders.py` | Per-phase orders: move, CAP, search, strike |
| `engine.py` | `step()` (fixed phase), `advance()` (run until something happens), `resolve_pending()` (tabletop result in) |
| `resolve.py` | `AutoResolver` (seeded) and `TabletopResolver` (pauses for the umpire) |
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
| `solo` | one side | that side submits | the solo player |
| `umpire` | allied, japan, umpire | both sides submit, or the umpire forces it | umpire |
| `two_player` | allied, japan | both sides submit | n/a (auto only) |

Each submission asks for `phase` (fixed minutes) or `run` (until an interrupt). The more cautious request wins.
With two hidden players, only events both sides see stop the clock.

Properties the tests enforce:

- Same state and orders give the same result.
- `step` never mutates its input.
- Player views carry no enemy ids, names or true positions; every allied/japan API response over a full strike cycle is checked for this.
- Ids are counted per side, so gaps in your own ids reveal nothing about the enemy.
- Concurrent final submissions resolve a phase once (`BEGIN IMMEDIATE`).

```sh
uv run pytest
```

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

Properties the tests enforce:

- Same state and orders give the same result.
- `step` never mutates its input.
- Player views carry no enemy ids, names or true positions.

```sh
uv run pytest
```

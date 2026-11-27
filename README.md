# flattop

WW2 Pacific carrier campaign aid. The engine runs in phases, with simultaneous orders and fog of war. Battles resolve automatically or are handed off to the tabletop.

## Engine (`src/flattop`)

| Module | Role |
|---|---|
| `model.py` | `GameState`: umpire truth (task forces, bases, contacts, missions, log) |
| `orders.py` | Per-phase orders: move, CAP, search, strike |
| `engine.py` | `step()` (fixed phase), `advance()` (run until something happens), `resolve_pending()` (tabletop result in) |
| `resolve.py` | Air: `AutoResolver` (seeded) and `TabletopResolver` (pauses for the umpire). Surface: `AutoSurfaceResolver` and `TabletopSurfaceResolver` |
| `bot.py` | Solo opponent: reads only its own `SideView`; searches, holds CAP, strikes fresh contacts, closes to strike range, retires when crippled, hides under cloud when threatened, searches into fronts |
| `view.py` | `view(state, side)`: the only thing a player client should receive |
| `sheet.py` | Markdown sheets for tabletop play: air engagements and surface actions |
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

## Surface action

Every tick, each group looks for each enemy group in three ways. Each way has its own roll:

| Way | Range | Chance per tick |
|---|---|---|
| Radar | the best `radar_nm` among her afloat ships; dark and weather do not matter | 0.8 |
| Lookouts | the side's `day_lookout_nm` or `night_lookout_nm` (`doctrine` in the scenario), times 0.4 under cloud | 0.6 |
| Chance | twice lookout range | 0.1 |

The engine uses the closest distance along both tracks during the tick, so groups that pass through each other still meet.
Contact can be one-sided. A contact is private (`surface_contact`), gives a contact with 1–2 nm error, and reports again only after 60 minutes.

If one side has contact and the groups come within gun range (15 nm by day, 6 nm at night), they fight a surface action (`surface_action`, one report per side).
A side that did not see the enemy does not fire in the first round.
The same two groups do not fight again for 120 minutes.
Gun and torpedo values are on the ship classes (`model.py`). The side's `torpedo_factor` scales torpedo hits.
`surface_resolver` in the game config picks `auto` or `tabletop`. If you omit it, it follows `resolver`.
With `tabletop`, the clock stops at the action (`engagement_pending` to both sides). The umpire, or the solo player, gets the sheet from `GET /api/seats/<token>/engagements` (`kind: "surface"`) and posts a `SurfaceResult` to `/api/seats/<token>/surface-actions/<id>`: `{"ship_damage": {"allied": {"Balch": 1}, "japan": {"Jintsu": 2}}}`.
Two-player games refuse `tabletop` for surface actions too, because the sheet shows both sides' ships.

## Damage control

Damage control works only at sea. Each new point of damage, from the air or from a surface action, is repairable with chance 0.5.
Each tick, every damaged ship may fix one repairable point (`damage_control_p` per hour, from the side's doctrine). It does not repair in a tick when her group fought a surface action.
Permanent damage stays. A carrier can get her flight deck back if repairs bring her below `deck_out_at`. A sunk ship has nothing to repair.

Properties the tests enforce:

- Same state and orders give the same result.
- `step` never mutates its input.
- Player views carry no enemy ids, names or true positions; every allied/japan API response over a full strike cycle is checked for this.
- The bot's orders depend only on its side's view (moving an unseen enemy changes nothing), and its private events never stop the player's clock.
- Weather is the same in both views, and moving an unseen enemy under cloud changes nothing in the other side's view.
- Ids are counted per side, so gaps in your own ids reveal nothing about the enemy.
- A surface action comes from contact plus range, is the same for the same seed, and does not repeat inside the cooldown.
- A private surface contact does not stop a two-player clock. A surface action does.
- Damage control never repairs past permanent damage and never repairs a sunk ship.
- Concurrent final submissions resolve a phase once (`BEGIN IMMEDIATE`).

```sh
uv run pytest
```

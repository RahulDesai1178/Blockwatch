# BLOCKWATCH

A Roblox intelligence console: live player counts (CCU), trends, categories and analysis for the games on Roblox's charts.

- `collector/collect.py`: runs every ~10 minutes on GitHub Actions, pulls Roblox's public charts and game stats, and saves them to the `data` branch.
- `collector/tags.json`: BLOCKWATCH's game categories. Edit `overrides` to re-tag a game (key = lowercase game name without emoji or [tags]).
- `index.html`: the dashboard. It reads the latest data straight from the `data` branch.

## Data branch layout
| File | What it is |
|---|---|
| `latest.json` | Every tracked game right now, with 1h / 24h / 7d change, peaks, categories |
| `series.json` | Hourly player counts for the last 7 days |
| `events.json` | Detected events: surges, drops, updates, new entries, milestones |
| `history/YYYY-MM-DD.csv` | Raw readings (ts, universe id, players), kept 15 days |
| `hourly/YYYY-MM.csv` | Hourly average and max per game, kept forever |
| `status.json` | Result of the last collector run, including any errors |

Data comes from Roblox's public web APIs. Not affiliated with Roblox.

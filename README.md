# lineboard-requests

The Line Board app (valuevault-epl.github.io/lineboard) writes `refresh.json` here when you ask for fresh predictions:
"Get current odds" on the odds check page, or the Refresh buttons on the board and the NBA Matchups page.

Two things answer it:
- **The cloud** (`.github/workflows/refresh.yml`): GitHub Actions re-runs the asked-for leagues (NBA for now) and
  publishes to the app within a few minutes. Works with both laptops off.
- **The always-on laptop**, when it's on: it watches this repo and does the same.

Holds only the latest request (league list, time, id). No keys or personal data.

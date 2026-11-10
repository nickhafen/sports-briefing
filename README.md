# Daily sports briefing

`python briefing.py --open` builds the briefing and opens it. Takes about 15 seconds.

- **Facts are scripted.** Scores, records, standings, rankings, next games and recap lines come from ESPN's free public JSON feed and are laid out by code. No key or account needed.
- **The AI only filters headlines.** The script collects recent headlines (ESPN team news, byucougars.com, Google News for MLR), and Claude Haiku returns the *numbers* of the ones that are major. The page shows the original title and link, so the model cannot add or change a fact. It runs through your installed `claude` command, so no API key.
- **Failures are visible.** Anything that could not be loaded is listed at the bottom under "Could not load today" instead of being guessed.

## Files
- `config.json` — teams, playoff leagues, marquee events, news feeds, time windows. Edit this to add or drop a team.
- `briefing.py` — the whole program (Python standard library only).
- `output/briefing.html` — latest briefing; `output/briefing.md` — same in text; `output/archive/` — one page per day.
- `run.cmd` — double-click (or schedule) to build and open.

## Organizing the page
Tabs at the top of the page switch the team sections between **By location** (the `groups` in `config.json`: Detroit, Chicago, Utah, BYU), **By league** (NFL, College football, NBA, …) and **By sport** (Football, Basketball, Hockey, Baseball, Soccer). Your browser remembers the last tab you picked. Playoffs, major events and other headlines stay in their own sections below.

`organize_by` in `config.json` (`"location"`, `"league"` or `"sport"`) sets the layout for the text version, the email, and the tab a browser starts on before you've picked one.

## Options
- `--by location|league|sport` overrides `organize_by` for one run.
- `--no-llm` skips the AI filter and shows ESPN's top team headlines unfiltered.
- `--email` sends the page by email. Fill in the `email` block in `config.json` and put the SMTP password in the `SPORTS_BRIEF_SMTP_PASSWORD` environment variable. This path has not been tested yet.

## What it shows
- A team appears when it played in the last 10 days or plays in the next 14; otherwise only if it has a major headline.
- Results from the last 48 hours, or the most recent game if within 8 days.
- Playoff games in NFL, NBA, NHL, MLB and MLS, whoever is playing.
- World Cup, Six Nations, Rugby Championship, Rugby World Cup and MLR playoffs when they are on; Olympics headlines during the 2028 and 2030 Games.

## Schedule
A Windows scheduled task named "Daily Sports Briefing" runs `briefing.py --open` every day at 9:15 AM under your account (and at next sign-in if the computer was off). Change or remove it in Task Scheduler.

## License
MIT. See `LICENSE`.

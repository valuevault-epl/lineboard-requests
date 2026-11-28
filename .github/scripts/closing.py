"""
Closing prices for NBA player props, so the Line Board's bet tracker can show closing line value (CLV) on every bet.

Free source: DraftKings' prop prices as published by ESPN's odds API (no key, no credits). A GitHub Actions schedule
runs this every 15 minutes; for each game tipping off soon it waits until LEAD minutes before tip-off, then saves
every player's main line with both prices (and DraftKings' opening line and prices) to nba.json on the 'closing'
branch of this repo. The phone reads that file and works out each bet's vig, fair (no-vig) price and CLV.

    python closing.py data/nba.json            # scheduled run: games tipping within WINDOW minutes
    TEST=1 python closing.py data/test.json    # every game not yet started, right now (checking the source)

nba.json: {updated, games: {espn game id: {tip, at, away, home, src, p: {espn athlete id: {stat: [line, over, under,
open line, open over, open under]}}}}}, prices decimal. Kept KEEP_DAYS days.

ESPN lists each main line as two prices without saying which is the over. It's the first: on 23 Nov 2026, 71 of 73
lines matched DraftKings' one-sided "N+" ladder (= over N-0.5) on the first price. The ladder is still checked per
line, and a pair is swapped if its second price clearly matches the ladder instead.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
SITE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
CORE = 'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36'}
MARKETS = {'Total Points': 'pts', 'Total Rebounds': 'reb', 'Total Assists': 'ast', 'Total 3-Point Field Goals': 'tpm',
           'Total Points, Rebounds, and Assists': 'pra', 'Total Points and Rebounds': 'pr', 'Total Points and Assists': 'pa',
           'Total Assists and Rebounds': 'ra', 'Total Steals': 'stl', 'Total Blocks': 'blk'}
LADDERS = {'Points Milestones': 'pts', 'Rebounds Milestones': 'reb', 'Assists Milestones': 'ast',
           '3-Point Field Goals Milestones': 'tpm', 'Points + Assists + Rebounds Milestones': 'pra',
           'Points + Rebounds Milestones': 'pr', 'Points + Assists Milestones': 'pa', 'Rebounds + Assists Milestones': 'ra',
           'Steals Milestones': 'stl', 'Blocks Milestones': 'blk'}
LEAD = dt.timedelta(minutes=3)             # how long before tip-off the closing prices are taken
BUDGET = dt.timedelta(minutes=int(os.environ.get('BUDGET', '330')))   # one run waits at most this long (jobs stop at 6h)
RETAKE = dt.timedelta(minutes=3)           # a captured game that still hasn't started is taken again this often
CHECK = dt.timedelta(seconds=60)           # how often ESPN's scoreboard is asked whether captured games have started
LATE = dt.timedelta(hours=3)               # a game still 'pre' is followed at most this long past its scheduled time
KEEP_DAYS = 6
# GitHub's schedule is unreliable (on 23-24 Nov 2026 it fired twice in a day instead of 96 times), so a run also
# starts when the phone asks (closing-request.json on main: {at, until}, written when bets are placed or the Bets tab
# opens) and, while games up to 'until' still need their closing prices, each run starts the next one before it ends.


def log(msg):
    print(f'{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S} ET  {msg}', flush=True)


def get(url, tries=3):
    url = url.replace('http://', 'https://')
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            if i == tries - 1:
                raise
            log(f'  retry {url[:90]}: {e}')
            time.sleep(3 * (i + 1))


def slate(now):
    """Games on ESPN's scoreboard today and tomorrow (US Eastern dates)."""
    out = {}
    for d in (now.astimezone(ET).date(), now.astimezone(ET).date() + dt.timedelta(days=1)):
        for e in get(f'{SITE}/scoreboard?dates={d:%Y%m%d}').get('events', []):
            c = (e.get('competitions') or [{}])[0]
            teams = {t.get('homeAway'): (t.get('team') or {}).get('abbreviation', '') for t in c.get('competitors', [])}
            out[str(e['id'])] = {'tip': dt.datetime.fromisoformat(e['date'].replace('Z', '+00:00')),
                                 'state': ((e.get('status') or {}).get('type') or {}).get('state', ''),
                                 'away': teams.get('away', ''), 'home': teams.get('home', '')}
    return out


def props(gid):
    """{athlete id: {stat: [line, over, under, open line, open over, open under]}} from DraftKings via ESPN."""
    odds = get(f'{CORE}/events/{gid}/competitions/{gid}/odds?lang=en&region=us')
    ref = next((((it.get('propBets') or {}).get('$ref')) for it in odds.get('items', []) if (it.get('propBets') or {}).get('$ref')), None)
    if not ref:
        return {}, None
    src = next(((it.get('provider') or {}).get('name') for it in odds.get('items', []) if it.get('propBets')), None) or 'DraftKings'
    src = src.replace('Draft Kings', 'DraftKings')
    items, page = [], 1
    while True:
        d = get(f'{ref}&limit=1000&page={page}')
        items += d.get('items', [])
        if page >= int(d.get('pageCount') or 1):
            break
        page += 1
    aid = lambda x: ((x.get('athlete') or {}).get('$ref') or '').split('/athletes/')[-1].split('?')[0]
    num = lambda v: float(str(v).rstrip('+'))
    pairs, ladder = {}, {}
    for x in items:
        t, a = (x.get('type') or {}).get('name'), aid(x)
        if not a:
            continue
        o = x.get('odds') or {}
        try:
            cur = (num(o['total']['value']), float(o['decimal']['value']))
            opn = (num(o['total'].get('open', o['total']['value'])), float(o['decimal'].get('open', o['decimal']['value'])))
        except (KeyError, TypeError, ValueError):
            continue
        if t in MARKETS:
            pairs.setdefault((a, MARKETS[t]), []).append((cur, opn))
        elif t in LADDERS:
            ladder[(a, LADDERS[t], cur[0])] = cur[1]
    out, swapped = {}, 0
    for (a, stat), xs in pairs.items():
        if len(xs) != 2 or xs[0][0][0] != xs[1][0][0]:
            continue
        (over, oo), (under, ou) = xs
        lad = ladder.get((a, stat, over[0] + 0.5))       # "N+" = over N - 0.5
        if lad and abs(under[1] - lad) + 0.05 < abs(over[1] - lad):
            (over, oo), (under, ou) = (under, ou), (over, oo)
            swapped += 1
        if 1 / over[1] + 1 / under[1] < 1:                 # no vig = not a real two-way pair
            continue
        out.setdefault(a, {})[stat] = [over[0], over[1], under[1], oo[0], oo[1], ou[1]]
    if swapped:
        log(f'  {gid}: {swapped} pairs had the under first (ladder check)')
    return out, src


def push(path, msg):
    git = lambda *a: subprocess.run(['git', '-C', os.path.dirname(os.path.abspath(path)), *a], capture_output=True, text=True)
    git('add', os.path.basename(path))
    if git('diff', '--cached', '--quiet').returncode == 0:
        return
    git('commit', '-q', '-m', msg)
    for _ in range(3):
        if git('push', '-q').returncode == 0:
            return
        git('pull', '-q', '--rebase')
    log('  push failed')


def asked_until(path):
    """The latest time the phone wants closing prices covered to (closing-request.json), or None."""
    try:
        with open(path, encoding='utf-8') as f:
            return dt.datetime.fromisoformat(str(json.load(f)['until']).replace('Z', '+00:00'))
    except Exception:
        return None


def next_run():
    """Start the next run of this workflow (GITHUB_TOKEN can dispatch workflows)."""
    repo, tok = os.environ.get('GITHUB_REPOSITORY'), os.environ.get('GITHUB_TOKEN')
    if not repo or not tok:
        return log('no token to start the next run')
    req = urllib.request.Request(f'https://api.github.com/repos/{repo}/actions/workflows/closing.yml/dispatches', method='POST',
                                 data=json.dumps({'ref': 'main'}).encode(), headers={'Authorization': f'Bearer {tok}',
                                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=30)
        log('started the next run (games still to come before the requested time)')
    except Exception as e:
        log(f'could not start the next run: {e}')


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def save(path, data, msg):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))
    push(path, msg)


def capture(gid, g, data, path, again=False):
    """Take DraftKings' prices for one game now; True if there were any."""
    try:
        p, src = props(gid)
    except Exception as e:
        log(f'{g["away"]}@{g["home"]}: failed ({e})')
        return False
    if not p:                                              # not posted yet: tried again later
        log(f'{g["away"]}@{g["home"]}: no player props posted yet')
        return False
    at = utcnow()
    data['games'][gid] = {'tip': g['tip'].strftime('%Y-%m-%dT%H:%M:%SZ'), 'at': at.strftime('%Y-%m-%dT%H:%M:%SZ'),
                          'away': g['away'], 'home': g['home'], 'src': src or 'DraftKings', 'p': p, 'final': False}
    data['updated'] = data['games'][gid]['at']
    mins = (g['tip'] - at).total_seconds() / 60
    log(f'{g["away"]}@{g["home"]}: {sum(len(v) for v in p.values())} lines for {len(p)} players ({src}), '
        + (f'{mins:.0f} min before tip-off' if mins >= 0 else f'{-mins:.0f} min past its time and not started yet'))
    save(path, data, f'Closing prop prices: {g["away"]} @ {g["home"]}' + (' (again: not started yet)' if again else ''))
    return True


def main():
    path = sys.argv[1]
    until = asked_until(sys.argv[2]) if len(sys.argv) > 2 else None
    test = bool(os.environ.get('TEST'))
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {'games': {}}
    now = utcnow()
    games = slate(now)
    keep_after = now - dt.timedelta(days=KEEP_DAYS)
    data['games'] = {k: v for k, v in data.get('games', {}).items()
                     if dt.datetime.fromisoformat(v['tip'].replace('Z', '+00:00')) > keep_after}
    end = now + BUDGET
    # games ESPN shows as not started (state 'pre'): tip-offs run late and games get pushed back, so a game is followed
    # up to LATE past its scheduled time. First capture LEAD before the scheduled tip-off; then, while it still hasn't
    # started, taken again every RETAKE; once ESPN says it has started, the last pre-game prices are marked final.
    new = lambda gs, t: sorted(((gid, g) for gid, g in gs.items() if g['state'] == 'pre' and g['tip'] > t - LATE
                                and gid not in data['games']), key=lambda x: x[1]['tip'])
    todo = [(gid, g) for gid, g in new(games, now) if test or g['tip'] - LEAD <= end]
    log(f'{len(games)} games on the slate' + (f'; asked to cover until {until.astimezone(ET):%a %H:%M} ET' if until else '')
        + '; to capture in this run: ' + (', '.join(f'{g["away"]}@{g["home"]} {g["tip"].astimezone(ET):%H:%M}' for _, g in todo) or 'none'))
    if test:
        for gid, g in todo:
            capture(gid, g, data, path)
        return
    watch = {gid for gid, v in data['games'].items() if not v.get('final') and gid in games}
    next_check = now
    while True:
        now = utcnow()
        if now >= end:
            break
        games = slate(now)
        for gid, g in new(games, now):                     # first captures that are due
            if g['tip'] - LEAD <= now and capture(gid, g, data, path):
                watch.add(gid)
        if watch and now >= next_check:                    # captured games: started yet?
            for gid in sorted(watch):
                g, v = games.get(gid), data['games'][gid]
                tip = dt.datetime.fromisoformat(v['tip'].replace('Z', '+00:00'))
                if not g or g['state'] != 'pre' or now > tip + LATE:
                    v['final'] = True
                    watch.discard(gid)
                    log(f'{v["away"]}@{v["home"]} has started: prices from {v["at"][11:16]} UTC are the close')
                    save(path, data, f'Closing prop prices final: {v["away"]} @ {v["home"]}')
                elif now - dt.datetime.fromisoformat(v['at'].replace('Z', '+00:00')) >= RETAKE - dt.timedelta(seconds=10):
                    capture(gid, g, data, path, again=True)
            next_check = now + CHECK
        times = [g['tip'] - LEAD for gid, g in new(games, now)] + ([next_check] if watch else [])
        if not times or min(times) > end:
            break
        time.sleep(max(5, (min(times) - utcnow()).total_seconds()))
    # games the phone asked for that this run couldn't reach: hand over to a new run
    if until:
        later = [g for gid, g in games.items() if g['state'] == 'pre' and gid not in data['games']
                 and utcnow() + LEAD < g['tip'] <= until + dt.timedelta(minutes=5)]
        if later:                                          # use this run's whole budget first: one hand-over per 5.5h
            rest = (end - utcnow()).total_seconds()
            if rest > 0:
                log(f'next requested game {min(g["tip"] for g in later).astimezone(ET):%a %H:%M} ET is beyond this run; waiting {rest / 60:.0f} min, then handing over')
                time.sleep(rest)
            next_run()


if __name__ == '__main__':
    main()

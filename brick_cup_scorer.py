"""
brick_cup_scorer.py
────────────────────────────────────────────────────────
PMDF Brick Cup 2026 — Automated match scorer & window manager.
Runs every 15 minutes via GitHub Actions.

What it does:
  1. Fetches WC 2026 match schedule from football-data.org
  2. Auto-locks window when first match in window kicks off
  3. Auto-scores finished matches (win/draw/loss + goal scorers + clean sheets)
  4. Auto-resolves predictions and pays out bricks
  5. Auto-opens next window + pays participation bonus when window is done
  6. Never double-scores a match

Secrets needed in GitHub repo settings:
  FDKEY         — football-data.org API key (bea2dd3c46ea4a31856a81c6828dd835)
  FIREBASE_KEY  — Firestore REST API key (AIzaSyCF8NqKVTqBuV37qAaefzL6q6RVjoChous)
  FIREBASE_PID  — worldcup2026-4d178

Scoring rules (what the API provides):
  Team win:           +4 pts
  Team draw:          +2 pts
  Goal scorer:        +8 pts  (from goals array — has player name)
  Clean sheet GK/DEF: +5 pts  (derived: goals conceded = 0)
  Clean sheet MID:    +2 pts
  NOTE: Assists and cards not available in football-data.org free tier.

Window mapping (group-stage clusters, roughly 3-4 days each):
  Window 1: Jun 11–15 (Group Stage matchday 1)
  Window 2: Jun 18–22 (Group Stage matchday 2)
  Window 3: Jun 25–Jul 2 (Group Stage MD3 + Round of 16)
  Window 4: Jul 5–7 (Quarter-Finals)
  Window 5: Jul 9–10 (Semi-Finals)
  Window 6: Jul 13 (Final + 3rd place)
"""

import os, json, time, datetime, requests
from datetime import timezone

# ── Config ──────────────────────────────────────────────────────────────────
FDKEY        = os.environ['FDKEY']
FB_KEY       = os.environ['FIREBASE_KEY']
FB_PID       = os.environ.get('FIREBASE_PID', 'worldcup2026-4d178')
FB_BASE      = f'https://firestore.googleapis.com/v1/projects/{FB_PID}/databases/(default)/documents'
FB_BATCH     = f'https://firestore.googleapis.com/v1/projects/{FB_PID}/databases/(default):batchWrite'
FB_QUERY     = f'https://firestore.googleapis.com/v1/projects/{FB_PID}/databases/(default):runQuery'
FD_BASE      = 'https://api.football-data.org/v4'
FD_HEADERS   = {'X-Auth-Token': FDKEY}

# Match date ranges for each window
# Real WC 2026 dates (verified from FIFA/FOX/ESPN schedule)
# Each window = one matchday cluster. Lock = first kick-off in that window.
WINDOW_DATES = {
    1: ('2026-06-11', '2026-06-17'),   # Group Stage Matchday 1
    2: ('2026-06-18', '2026-06-23'),   # Group Stage Matchday 2
    3: ('2026-06-24', '2026-06-27'),   # Group Stage Matchday 3
    4: ('2026-06-28', '2026-07-07'),   # Round of 32 + Round of 16
    5: ('2026-07-09', '2026-07-11'),   # Quarter-Finals
    6: ('2026-07-14', '2026-07-19'),   # Semis + 3rd place + Final
}

# Lock times (UTC) — when auto-lock fires = first match kick-off
WINDOW_LOCK_TIMES = {
    1: '2026-06-11T21:00:00Z',   # Mexico vs South Africa 3pm ET
    2: '2026-06-18T17:00:00Z',   # First MD2 match 12pm ET
    3: '2026-06-25T01:00:00Z',   # First MD3 match 9pm ET Wed = Thu 1am UTC
    4: '2026-06-28T16:00:00Z',   # R32 starts 12pm ET
    5: '2026-07-09T20:00:00Z',   # QF1 4pm ET
    6: '2026-07-14T20:00:00Z',   # SF1 3pm ET
}

# ── Firestore REST helpers ───────────────────────────────────────────────────
def enc(v):
    if v is None:          return {'nullValue': None}
    if isinstance(v, bool): return {'booleanValue': v}
    if isinstance(v, int):  return {'integerValue': str(v)}
    if isinstance(v, float):return {'doubleValue': v}
    if isinstance(v, str):  return {'stringValue': v}
    if isinstance(v, list): return {'arrayValue': {'values': [enc(x) for x in v]}}
    if isinstance(v, dict): return {'mapValue': {'fields': {k: enc(val) for k, val in v.items()}}}
    return {'stringValue': str(v)}

def dec(v):
    if not v: return None
    if 'nullValue'    in v: return None
    if 'booleanValue' in v: return v['booleanValue']
    if 'integerValue' in v: return int(v['integerValue'])
    if 'doubleValue'  in v: return v['doubleValue']
    if 'stringValue'  in v: return v['stringValue']
    if 'timestampValue' in v: return v['timestampValue']
    if 'arrayValue'   in v: return [dec(x) for x in v['arrayValue'].get('values', [])]
    if 'mapValue'     in v: return {k: dec(val) for k, val in v['mapValue'].get('fields', {}).items()}
    return None

def doc_to_obj(doc):
    if not doc or 'name' not in doc: return None
    obj = {'id': doc['name'].split('/')[-1]}
    obj.update({k: dec(v) for k, v in doc.get('fields', {}).items()})
    return obj

def fs_get(col, doc_id):
    r = requests.get(f'{FB_BASE}/{col}/{doc_id}?key={FB_KEY}')
    if r.status_code == 404: return None
    r.raise_for_status()
    return doc_to_obj(r.json())

def fs_list(col, filters=None, order_by=None, direction='ASCENDING', limit=None):
    sq = {'structuredQuery': {'from': [{'collectionId': col}]}}
    if filters:
        if len(filters) == 1:
            f = filters[0]
            sq['structuredQuery']['where'] = {
                'fieldFilter': {'field': {'fieldPath': f[0]}, 'op': f[1], 'value': enc(f[2])}
            }
        else:
            sq['structuredQuery']['where'] = {
                'compositeFilter': {'op': 'AND', 'filters': [
                    {'fieldFilter': {'field': {'fieldPath': f[0]}, 'op': f[1], 'value': enc(f[2])}}
                    for f in filters
                ]}
            }
    if order_by:
        sq['structuredQuery']['orderBy'] = [{'field': {'fieldPath': order_by}, 'direction': direction}]
    if limit:
        sq['structuredQuery']['limit'] = limit
    r = requests.post(f'{FB_QUERY}?key={FB_KEY}', json=sq)
    r.raise_for_status()
    return [doc_to_obj(row['document']) for row in r.json() if 'document' in row]

def fs_set(col, doc_id, data):
    keys = [k for k in data if not isinstance(data[k], dict) or '_inc' not in data[k]]
    mask = '&'.join(f'updateMask.fieldPaths={k}' for k in data.keys())
    r = requests.patch(
        f'{FB_BASE}/{col}/{doc_id}?key={FB_KEY}&{mask}',
        json={'fields': {k: enc(v) for k, v in data.items() if not (isinstance(v, dict) and '_inc' in v)}}
    )
    r.raise_for_status()

def fs_add(col, data):
    r = requests.post(f'{FB_BASE}/{col}?key={FB_KEY}', json={'fields': {k: enc(v) for k, v in data.items()}})
    r.raise_for_status()
    return r.json()['name'].split('/')[-1]

def fs_batch(ops):
    """ops = list of {op: 'set'|'update'|'delete', col, id, data}"""
    writes = []
    for op in ops:
        name = f'{FB_BASE}/{op["col"]}/{op["id"]}'
        if op['op'] == 'delete':
            writes.append({'delete': name})
            continue
        regular = {k: v for k, v in op.get('data', {}).items() if not (isinstance(v, dict) and '_inc' in v)}
        transforms = [
            {'fieldPath': k, 'increment': {'integerValue': str(v['_inc'])}}
            for k, v in op.get('data', {}).items() if isinstance(v, dict) and '_inc' in v
        ]
        if regular:
            entry = {'update': {'name': name, 'fields': {k: enc(v) for k, v in regular.items()}}}
            if op['op'] == 'update':
                entry['updateMask'] = {'fieldPaths': list(regular.keys())}
            writes.append(entry)
        if transforms:
            writes.append({'transform': {'document': name, 'fieldTransforms': transforms}})
    if not writes:
        return
    # Firestore batchWrite limit is 500 ops per call
    for i in range(0, len(writes), 500):
        chunk = writes[i:i+500]
        r = requests.post(f'{FB_BATCH}?key={FB_KEY}', json={'writes': chunk})
        r.raise_for_status()

def inc(n): return {'_inc': n}

# ── Representative player pool for window store seeding ──
# Mirrors the game's POOL array (id, name, country, pos, rarity)
STORE_POOL = [
    ('br3','Marquinhos','Brazil','DEF','gold'),('br6','Casemiro','Brazil','MID','gold'),
    ('br8','Rodrygo','Brazil','FWD','gold'),('br9','Vinicius Jr','Brazil','FWD','gold'),
    ('fr7','Camavinga','France','MID','gold'),('fr8','Griezmann','France','MID','gold'),
    ('fr9','Mbappé','France','FWD','gold'),('fr10','Dembélé','France','FWD','gold'),
    ('en5','Alexander-Arnold','England','DEF','gold'),('en6','Bellingham','England','MID','gold'),
    ('en7','Rice','England','MID','gold'),('en8','Saka','England','FWD','gold'),
    ('en9','Kane','England','FWD','gold'),('en10','Foden','England','MID','gold'),
    ('de1','Neuer','Germany','GK','gold'),('de4','Kimmich','Germany','MID','gold'),
    ('de5','Musiala','Germany','MID','gold'),('de8','Wirtz','Germany','MID','gold'),
    ('ar1','Dibu Martínez','Argentina','GK','gold'),('ar6','Messi','Argentina','FWD','gold'),
    ('ar7','Lautaro','Argentina','FWD','gold'),('ar8','E. Fernández','Argentina','MID','gold'),
    ('ar11','Julián Álvarez','Argentina','FWD','gold'),
    ('es4','Pedri','Spain','MID','gold'),('es6','Rodri','Spain','MID','gold'),
    ('es7','Yamal','Spain','FWD','gold'),('es2','Dani Olmo','Spain','MID','gold'),
    ('pt5','Bernardo Silva','Portugal','MID','gold'),('pt6','Bruno Fernandes','Portugal','MID','gold'),
    ('pt7','Ronaldo','Portugal','FWD','gold'),('pt8','Rafael Leão','Portugal','FWD','gold'),
    ('nl3','van Dijk','Netherlands','DEF','gold'),('nl4','de Jong F','Netherlands','MID','gold'),
    ('nl5','Gakpo','Netherlands','FWD','gold'),
    ('ma2','Hakimi','Morocco','DEF','gold'),('jp5','Mitoma','Japan','FWD','gold'),
    ('us6','Pulisic','USA','FWD','gold'),('mx11','Giménez S','Mexico','FWD','gold'),
    # Silver tier
    ('br1','Alisson','Brazil','GK','silver'),('br4','Militão','Brazil','DEF','silver'),
    ('fr1','Maignan','France','GK','silver'),('fr3','Koundé','France','DEF','silver'),
    ('fr6','Tchouaméni','France','MID','silver'),('fr11','Thuram','France','FWD','silver'),
    ('en1','Pickford','England','GK','silver'),('en2','Trippier','England','DEF','silver'),
    ('en11','Watkins','England','FWD','silver'),
    ('de2','Rüdiger','Germany','DEF','silver'),('de6','Havertz','Germany','FWD','silver'),
    ('ar4','de Paul','Argentina','MID','silver'),('ar5','Mac Allister','Argentina','MID','silver'),
    ('nl7','Dumfries','Netherlands','DEF','silver'),('nl9','Wijnaldum','Netherlands','MID','silver'),
    ('nl11','Depay','Netherlands','FWD','silver'),
    ('ma7','Aguerd','Morocco','DEF','silver'),('ma10','Amrabat','Morocco','MID','silver'),
    ('sn4','Mané','Senegal','FWD','silver'),('ca10','Buchanan','Canada','FWD','silver'),
]

def seed_window_store(window_num):
    """Seed 20 random players into the window store at 30 bricks each."""
    # Check if already seeded
    existing = fs_list('listings', [['type', 'EQUAL', 'store'], ['windowNum', 'EQUAL', window_num]])
    if existing:
        print(f'  Window store already seeded for window {window_num} ({len(existing)} listings)')
        return
    import random
    pool = list(STORE_POOL)
    random.shuffle(pool)
    # Prioritise gold then silver
    gold = [p for p in pool if p[4] == 'gold']
    silver = [p for p in pool if p[4] == 'silver']
    ordered = (gold + silver)[:20]
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    ops = []
    for pid, name, country, pos, rarity in ordered:
        lid = f'store_w{window_num}_{pid}'
        ops.append({'op': 'set', 'col': 'listings', 'id': lid, 'data': {
            'sellerId': '__store__', 'sellerNick': 'Window Store',
            'cardId': None, 'realPlayerId': pid,
            'cardName': name, 'country': country, 'flag': '', 'pos': pos,
            'rarity': rarity, 'points': 0, 'price': 30,
            'status': 'active', 'type': 'store', 'windowNum': window_num,
            'createdAt': now
        }})
    fs_batch(ops)
    print(f'  ⚡ Window store seeded: {len(ops)} players for window {window_num}')



# ── Football-data.org helpers ────────────────────────────────────────────────
def fd_get_matches(date_from, date_to):
    """Fetch WC 2026 matches between two dates."""
    url = f'{FD_BASE}/competitions/WC/matches'
    r = requests.get(url, headers=FD_HEADERS, params={
        'dateFrom': date_from, 'dateTo': date_to, 'season': 2026
    })
    if r.status_code == 429:
        print('Rate limited, sleeping 60s')
        time.sleep(60)
        return fd_get_matches(date_from, date_to)
    r.raise_for_status()
    return r.json().get('matches', [])

def fd_get_match(match_id):
    """Fetch single match with goals array."""
    r = requests.get(f'{FD_BASE}/matches/{match_id}', headers=FD_HEADERS)
    r.raise_for_status()
    return r.json().get('match', r.json())

def now_utc():
    return datetime.datetime.now(timezone.utc)

def parse_dt(s):
    if not s: return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except:
        return None

# ── Window logic ─────────────────────────────────────────────────────────────
def get_window_for_date(d):
    """Return which game window a given date falls in."""
    ds = d.strftime('%Y-%m-%d')
    for wnum, (wstart, wend) in WINDOW_DATES.items():
        if wstart <= ds <= wend:
            return wnum
    return None

def get_current_window_doc():
    ws = fs_list('windows', order_by='createdAt', direction='DESCENDING', limit=1)
    return ws[0] if ws else None

# ── Main scoring logic ───────────────────────────────────────────────────────
def score_match(fd_match, window_doc):
    """Score a finished match against all player XIs."""
    home_team = fd_match['homeTeam']['name']
    away_team = fd_match['awayTeam']['name']
    home_goals_full = fd_match['score']['fullTime']['home'] or 0
    away_goals_full = fd_match['score']['fullTime']['away'] or 0
    fd_match_id = str(fd_match['id'])

    # Check if already scored
    already = fs_list('matches', [['fdMatchId', 'EQUAL', fd_match_id]])
    if already:
        print(f'  Already scored: {home_team} vs {away_team}')
        return False

    print(f'  Scoring: {home_team} {home_goals_full}–{away_goals_full} {away_team}')

    # Fetch detailed match for goal scorers
    try:
        detail = fd_get_match(fd_match['id'])
        goals = detail.get('goals', [])
    except Exception as e:
        print(f'  Warning: could not fetch match detail: {e}')
        goals = []

    # Build scorer set (lowercase names for fuzzy match)
    scorer_names = set()
    for g in goals:
        if g.get('scorer') and g['scorer'].get('name'):
            scorer_names.add(g['scorer']['name'].lower())

    home_win = home_goals_full > away_goals_full
    away_win = away_goals_full > home_goals_full
    draw = home_goals_full == away_goals_full
    high_score = (home_goals_full + away_goals_full) >= 3

    # Log the match
    match_id = fs_add('matches', {
        'fdMatchId': fd_match_id,
        'homeTeam': home_team,
        'awayTeam': away_team,
        'homeGoals': home_goals_full,
        'awayGoals': away_goals_full,
        'scorers': list(scorer_names),
        'windowId': window_doc['id'],
        'status': 'finished',
        'scoredAt': datetime.datetime.utcnow().isoformat() + 'Z',
        'autoScored': True,
    })

    # Score all player XIs
    all_xis = fs_list('xi')
    ops = []
    update_count = 0

    for xi in all_xis:
        player_id = xi.get('playerId') or xi.get('id')
        slots = xi.get('slots') or {}
        player_pts = 0

        for card_id in slots.values():
            card = fs_get('cards', card_id)
            if not card:
                continue

            country = card.get('country', '')
            pos = card.get('pos', '')
            name = card.get('name', '').lower()

            is_home = country == home_team
            is_away = country == away_team
            if not is_home and not is_away:
                continue

            my_win = (is_home and home_win) or (is_away and away_win)
            my_goals_conceded = away_goals_full if is_home else home_goals_full
            clean_sheet = my_goals_conceded == 0

            pts = 0
            if my_win:       pts += 4
            if draw:         pts += 2
            if high_score and pos == 'FWD': pts += 3
            if high_score and pos == 'MID': pts += 2
            if clean_sheet and pos in ('GK', 'DEF'): pts += 5
            if clean_sheet and pos == 'MID': pts += 2

            # Goal scorer bonus — fuzzy name match
            for scorer in scorer_names:
                name_parts = name.split()
                if scorer == name or any(part in scorer for part in name_parts if len(part) > 3):
                    pts += 8
                    break

            if pts != 0:
                ops.append({'op': 'update', 'col': 'cards', 'id': card_id, 'data': {'points': inc(pts)}})
                player_pts += pts
                update_count += 1

        if player_pts != 0:
            ops.append({'op': 'update', 'col': 'players', 'id': player_id, 'data': {'points': inc(player_pts)}})

    if ops:
        fs_batch(ops)

    print(f'  ✅ Scored {update_count} cards across {len(all_xis)} teams')
    return True

def resolve_predictions(fd_match):
    """Auto-resolve predictions based on match result."""
    home_team = fd_match['homeTeam']['name']
    away_team = fd_match['awayTeam']['name']
    home_g = fd_match['score']['fullTime']['home'] or 0
    away_g = fd_match['score']['fullTime']['away'] or 0
    total_g = home_g + away_g
    high_score = total_g >= 3
    winner = home_team if home_g > away_g else (away_team if away_g > home_g else 'Draw')

    # Get pending predictions for related matches
    pending = fs_list('predictions', [['status', 'EQUAL', 'pending']])
    ops = []
    paid = 0

    for pred in pending:
        match_str = pred.get('match', '')
        pick = pred.get('pick', '')
        pred_type = pred.get('type', '')
        reward = pred.get('reward', 0)
        correct = False

        # Match-specific safe predictions
        if pred_type == 'safe' and (home_team in match_str or away_team in match_str):
            correct = (pick == winner)

        # Brave: 3+ goals
        elif pred_type == 'brave' and '3+' in pred.get('question', '') and (home_team in match_str or away_team in match_str):
            correct = (pick == 'Yes') == high_score

        # Chaos predictions are left pending for manual resolution
        # (too ambiguous to auto-resolve reliably)
        elif pred_type == 'chaos':
            continue

        if correct:
            ops.append({'op': 'update', 'col': 'predictions', 'id': pred['id'], 'data': {'status': 'won'}})
            ops.append({'op': 'update', 'col': 'players', 'id': pred['playerId'], 'data': {'bricks': inc(reward)}})
            paid += 1
        elif pred_type != 'chaos':
            ops.append({'op': 'update', 'col': 'predictions', 'id': pred['id'], 'data': {'status': 'lost'}})

    if ops:
        fs_batch(ops)
    if paid:
        print(f'  💰 Paid out {paid} winning predictions')

def pay_participation_bonus(window_num):
    """Pay +20 bricks to all players with a saved XI."""
    all_players = fs_list('players')
    all_xis = fs_list('xi')
    xi_player_ids = {xi.get('playerId') or xi.get('id') for xi in all_xis}
    ops = [
        {'op': 'update', 'col': 'players', 'id': p['id'], 'data': {'bricks': inc(20)}}
        for p in all_players if p['id'] in xi_player_ids
    ]
    if ops:
        fs_batch(ops)
    print(f'  🧱 Paid participation bonus to {len(ops)} players for window {window_num}')

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    now = now_utc()
    print(f'[{now.strftime("%Y-%m-%d %H:%M")} UTC] Brick Cup scorer running')

    current_window = get_current_window_doc()
    if not current_window:
        print('No window document found — creating window 1')
        fs_add('windows', {'number': 1, 'open': True, 'locked': False,
                           'createdAt': now.isoformat() + 'Z'})
        current_window = get_current_window_doc()

    win_num = current_window.get('number', 1)
    win_locked = current_window.get('locked', False)
    win_id = current_window['id']

    if win_num not in WINDOW_DATES:
        print(f'Window {win_num} has no date range defined — tournament may be over')
        return

    date_from, date_to = WINDOW_DATES[win_num]
    print(f'Current window: {win_num} ({date_from} → {date_to}), locked={win_locked}')

    # Fetch matches for this window
    try:
        matches = fd_get_matches(date_from, date_to)
    except Exception as e:
        print(f'Error fetching matches: {e}')
        return

    if not matches:
        print('No matches found for this window')
        return

    print(f'Found {len(matches)} matches in window {win_num}')

    # Auto-lock window at the scheduled lock time (first kick-off)
    if not win_locked:
        lock_iso = WINDOW_LOCK_TIMES.get(win_num)
        lock_time = parse_dt(lock_iso) if lock_iso else None
        # Also check first match time as fallback
        first_match_time = parse_dt(matches[0].get('utcDate')) if matches else None
        should_lock = (lock_time and now >= lock_time) or (first_match_time and now >= first_match_time)
        if should_lock:
            print(f'Lock time reached — locking window {win_num}')
            fs_set('windows', win_id, {'locked': True, 'open': False})
            win_locked = True

    # Score finished matches
    finished = [m for m in matches if m.get('status') == 'FINISHED']
    scored_any = False
    for match in finished:
        try:
            did_score = score_match(match, current_window)
            if did_score:
                resolve_predictions(match)
                scored_any = True
                time.sleep(2)  # Rate limit courtesy delay
        except Exception as e:
            print(f'  Error scoring match {match.get("id")}: {e}')

    # Check if all matches in window are finished → open next window
    all_statuses = [m.get('status') for m in matches]
    all_done = all(s == 'FINISHED' for s in all_statuses)

    if all_done and win_locked:
        next_win_num = win_num + 1
        if next_win_num in WINDOW_DATES:
            # Check if next window already exists
            existing_next = fs_list('windows', [['number', 'EQUAL', next_win_num]])
            if not existing_next:
                print(f'All matches done — opening window {next_win_num}')
                fs_add('windows', {
                    'number': next_win_num, 'open': True, 'locked': False,
                    'createdAt': now.isoformat() + 'Z'
                })
                pay_participation_bonus(next_win_num)
                seed_window_store(next_win_num)
        else:
            print('Tournament complete! No more windows to open.')

    print('Done.')

if __name__ == '__main__':
    main()

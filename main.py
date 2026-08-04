
import os
import json
import time
import threading
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
API_FOOTBALL_KEY = os.environ.get('API_FOOTBALL_KEY', '')

API_BASE = 'https://v3.football.api-sports.io'
HEADERS = {'x-apisports-key': API_FOOTBALL_KEY}

POLL_INTERVAL = 180  # 3 minuti

# File persistenza
SILENCED_FILE = 'silenced_matches.json'
FAVORITES_FILE = 'favorite_matches.json'

# Stato runtime
matches_state = {}      # fixture_id -> last_stats dict
silenced_matches = {}   # fixture_id -> {score_home, score_away, minute, timestamp}
favorite_matches = set()  # fixture_id

# ─────────────────────────────────────────────────────────────
# PERSISTENZA
# ─────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def init_persistence():
    global silenced_matches, favorite_matches
    silenced_matches = load_json(SILENCED_FILE, {})
    fav = load_json(FAVORITES_FILE, [])
    favorite_matches = set(str(x) for x in fav)

def save_silenced():
    save_json(SILENCED_FILE, silenced_matches)

def save_favorites():
    save_json(FAVORITES_FILE, list(favorite_matches))

# ─────────────────────────────────────────────────────────────
# API-FOOTBALL
# ─────────────────────────────────────────────────────────────
def api_get(endpoint, params=None):
    try:
        r = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS, params=params, timeout=30)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        print(f"API error: {e}")
        return {}

def get_live_fixtures():
    """Recupera partite in corso, escluse femminili/giovanili/amatoriali/amichevoli."""
    data = api_get('fixtures', {'live': 'all'})
    fixtures = data.get('response', []) or []
    filtered = []
    excluded_keywords = ['women', 'female', 'u15', 'u16', 'u17', 'u18', 'u19', 'u20',
                         'amateur', 'amateurs', 'friendlies', 'friendly', 'reserves',
                         'reserve', 'youth', 'junior']
    for f in fixtures:
        league = f.get('league', {})
        league_name = league.get('name', '').lower()
        if any(k in league_name for k in excluded_keywords):
            continue
        if f.get('fixture', {}).get('status', {}).get('short') in ('PST', 'CAN', 'ABD'):
            continue
        filtered.append(f)
    return filtered

def get_fixture_stats(fixture_id):
    data = api_get('fixtures/statistics', {'fixture': fixture_id})
    stats = {}
    for team_stats in data.get('response', []) or []:
        team = team_stats.get('team', {})
        team_name = team.get('name', 'Squadra')
        stats[team_name] = {}
        for s in team_stats.get('statistics', []) or []:
            t = s.get('type', '')
            v = s.get('value')
            if v is None:
                v = 0
            try:
                v = int(v)
            except (ValueError, TypeError):
                try:
                    v = float(v)
                except (ValueError, TypeError):
                    v = 0
            stats[team_name][t] = v
    return stats

def get_fixture_events(fixture_id):
    data = api_get('fixtures/events', {'fixture': fixture_id})
    goals = []
    for ev in data.get('response', []) or []:
        if ev.get('type') == 'Goal':
            goals.append({
                'minute': ev.get('time', {}).get('elapsed', 0),
                'extra': ev.get('time', {}).get('extra', 0) or 0,
                'player': ev.get('player', {}).get('name', 'N/D'),
                'team': ev.get('team', {}).get('name', ''),
                'team_id': ev.get('team', {}).get('id', 0)
            })
    return sorted(goals, key=lambda x: (x['minute'], x.get('extra', 0)))

def get_fixture_by_team_name(team_query):
    """Cerca partita live per nome squadra (parziale)."""
    fixtures = get_live_fixtures()
    team_query_lower = team_query.lower()
    matches = []
    for f in fixtures:
        home = f.get('teams', {}).get('home', {}).get('name', '').lower()
        away = f.get('teams', {}).get('away', {}).get('name', '').lower()
        if team_query_lower in home or team_query_lower in away:
            matches.append(f)
    return matches

# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────
def tg_api(method, payload=None, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=30)
        else:
            r = requests.post(url, json=payload, timeout=30)
        return r.json()
    except Exception as e:
        print(f"Telegram error: {e}")
        return {}

def send_message(text, chat_id=None, reply_markup=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        return
    tg_api('sendMessage', {'chat_id': cid, 'text': text, 'parse_mode': 'HTML',
                           'reply_markup': reply_markup})

def send_photo(caption, photo_bytes, filename, chat_id=None, reply_markup=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        return
    files = {'photo': (filename, photo_bytes, 'image/png')}
    payload = {'chat_id': cid, 'caption': caption, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    tg_api('sendPhoto', payload, files)

# ─────────────────────────────────────────────────────────────
# GRAFICO
# ─────────────────────────────────────────────────────────────
def build_bar_chart(team_names, values_home, values_away):
    """Grafico a barre orizzontali comparative."""
    labels = ['Tiri Totali', 'Tiri in Porta', 'Corner']
    def safe(lst, idx):
        return int(lst[idx]) if idx < len(lst) else 0
    h = [safe(values_home, i) for i in range(3)]
    a = [safe(values_away, i) for i in range(3)]

    fig, ax = plt.subplots(figsize=(6, 3))
    y = range(len(labels))
    height = 0.35
    ax.barh([i - height/2 for i in y], h, height, label=team_names[0], color='#2ecc71')
    ax.barh([i + height/2 for i in y], a, height, label=team_names[1], color='#e74c3c')
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels)
    ax.legend(loc='lower right', fontsize=8)
    ax.set_xlim(0, max(max(h + a + [1])) * 1.2)
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf

# ─────────────────────────────────────────────────────────────
# FIAMME
# ─────────────────────────────────────────────────────────────
# Soglie: (minimo_per_🔥, minimo_per_🔥🔥)
# Total Shots:  +2→🔥, +3→🔥, +4→🔥🔥
# Shots on Goal: +2→🔥, +3→🔥🔥
# Corners: nessuna fiamma
FIRE_THRESHOLDS = {
    'Total Shots':    (2, 4),
    'Shots on Goal':  (2, 3),
    'Shots off Goal': (2, 3),
    'Corners':        (999, 999),
}

def get_fire_suffix(delta, metric_name):
    low, high = FIRE_THRESHOLDS.get(metric_name, (999, 999))
    if delta >= high:
        return '🔥🔥'
    elif delta >= low:
        return '🔥'
    return ''

def compute_delta(old_stats, new_stats, team_name, metric):
    old = old_stats.get(team_name, {}).get(metric, 0)
    new = new_stats.get(team_name, {}).get(metric, 0)
    return new - old

# ─────────────────────────────────────────────────────────────
# FORMATTAZIONE STATISTICHE
# ─────────────────────────────────────────────────────────────
def format_stats_delta(old_stats, new_stats):
    teams = list(new_stats.keys())
    if len(teams) < 2:
        return "Nessun dato"
    t1, t2 = teams[0], teams[1]
    metrics = ['Total Shots', 'Shots on Goal', 'Corners']
    label_map = {
        'Total Shots': 'Tiri totali',
        'Shots on Goal': 'Tiri in porta',
        'Corners': 'Corner'
    }
    lines = []
    for m in metrics:
        v1 = int(new_stats[t1].get(m, 0))
        v2 = int(new_stats[t2].get(m, 0))
        d1 = compute_delta(old_stats, new_stats, t1, m)
        d2 = compute_delta(old_stats, new_stats, t2, m)
        fire1 = get_fire_suffix(d1, m)
        fire2 = get_fire_suffix(d2, m)
        label = label_map.get(m, m)
        line = f"• {label}: {v1}{fire1} ({d1:+d}) - {v2}{fire2} ({d2:+d})"
        lines.append(line)
    return "\n".join(lines)

def format_simple_stats(new_stats):
    teams = list(new_stats.keys())
    if len(teams) < 2:
        return "Nessun dato"
    t1, t2 = teams[0], teams[1]
    metrics = ['Total Shots', 'Shots on Goal', 'Corners']
    label_map = {
        'Total Shots': 'Tiri totali',
        'Shots on Goal': 'Tiri in porta',
        'Corners': 'Corner'
    }
    lines = []
    for m in metrics:
        v1 = int(new_stats[t1].get(m, 0))
        v2 = int(new_stats[t2].get(m, 0))
        label = label_map.get(m, m)
        lines.append(f"• {label}: {v1} - {v2}")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# TASTIERA INLINE
# ─────────────────────────────────────────────────────────────
def build_inline_keyboard(fixture_id, is_favorite):
    buttons = []
    fav_text = "❌ Rimuovi dai preferiti" if is_favorite else "⭐ Aggiungi ai preferiti"
    buttons.append([{"text": fav_text, "callback_data": f"fav:{fixture_id}"}])
    buttons.append([{"text": "🔕 Silenzia questa partita", "callback_data": f"sil:{fixture_id}"}])
    return {"inline_keyboard": buttons}

# ─────────────────────────────────────────────────────────────
# LOGICA NOTIFICHE
# ─────────────────────────────────────────────────────────────
def should_notify_live(fixture_id, stats, minute):
    if str(fixture_id) in silenced_matches:
        return False
    if favorite_matches and str(fixture_id) not in favorite_matches:
        return False
    teams = list(stats.keys())
    if len(teams) < 2:
        return False
    t1, t2 = teams[0], teams[1]
    total_shots = stats[t1].get('Total Shots', 0) + stats[t2].get('Total Shots', 0)
    diff_shots = abs(stats[t1].get('Total Shots', 0) - stats[t2].get('Total Shots', 0))
    if diff_shots >= 3:
        return True
    if minute <= 25 and total_shots >= 6:
        return True
    if total_shots >= 4:
        last = matches_state.get(str(fixture_id), {})
        last_time = last.get('last_forced_notify', 0)
        if minute - last_time >= 30:
            return True
    return False

def send_live_notification(fixture, stats, minute, is_first=False):
    fid = str(fixture['fixture']['id'])
    home = fixture['teams']['home']['name']
    away = fixture['teams']['away']['name']
    league = fixture['league']['name']
    score_h = fixture['goals']['home'] if fixture['goals']['home'] is not None else 0
    score_a = fixture['goals']['away'] if fixture['goals']['away'] is not None else 0

    events = get_fixture_events(fixture['fixture']['id'])
    first_goal = events[0] if events else None
    last_goal = events[-1] if events else None

    old = matches_state.get(fid, {})
    old_stats = old.get('stats', {})
    if is_first or not old_stats:
        stats_text = format_simple_stats(stats)
    else:
        stats_text = format_stats_delta(old_stats, stats)

    caption = (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🏆 {league}\n"
        f"⏱️ Minuto: {minute}' | Stato: LIVE\n\n"
        f"🔢 Risultato: {score_h} - {score_a}\n\n"
    )
    if first_goal:
        extra = f"+{first_goal['extra']}" if first_goal.get('extra') else ""
        caption += f"🥇 Primo gol: {first_goal['minute']}{extra}' ({first_goal['player']})\n"
    if last_goal and len(events) > 1:
        extra = f"+{last_goal['extra']}" if last_goal.get('extra') else ""
        caption += f"⚡ Ultimo gol: {last_goal['minute']}{extra}' ({last_goal['player']})\n"
    if first_goal or last_goal:
        caption += "\n"

    caption += f"🔥 Statistiche {'attuali' if is_first else 'ultimi 15 min'}:\n{stats_text}\n\n"
    caption += f"🟢 Verde = {home}\n🔴 Rosso = {away}"

    teams = list(stats.keys())
    if len(teams) >= 2:
        vals = [
            [stats[teams[0]].get('Total Shots', 0), stats[teams[0]].get('Shots on Goal', 0),
             stats[teams[0]].get('Corners', 0)],
            [stats[teams[1]].get('Total Shots', 0), stats[teams[1]].get('Shots on Goal', 0),
             stats[teams[1]].get('Corners', 0)]
        ]
        chart = build_bar_chart([home, away], vals[0], vals[1])
    else:
        chart = None

    is_fav = fid in favorite_matches
    keyboard = build_inline_keyboard(fid, is_fav)

    if chart:
        send_photo(caption, chart, f"match_{fid}.png", reply_markup=keyboard)
    else:
        send_message(caption, reply_markup=keyboard)

def send_final_notification(fixture, stats):
    fid = str(fixture['fixture']['id'])
    home = fixture['teams']['home']['name']
    away = fixture['teams']['away']['name']
    league = fixture['league']['name']
    score_h = fixture['goals']['home'] if fixture['goals']['home'] is not None else 0
    score_a = fixture['goals']['away'] if fixture['goals']['away'] is not None else 0

    sil = silenced_matches.get(fid)
    if sil:
        events = get_fixture_events(fixture['fixture']['id'])
        sil_minute = sil.get('minute', 'N/D')
        sil_score_h = sil.get('score_home', 0)
        sil_score_a = sil.get('score_away', 0)
        goals_after = [g for g in events if g['minute'] > sil_minute]
        diff_h = score_h - sil_score_h
        diff_a = score_a - sil_score_a

        extra_parts = []
        if diff_h > 0:
            extra_parts.append(f"+{diff_h}🏠")
        if diff_a > 0:
            extra_parts.append(f"+{diff_a}✈️")
        extra = " " + " ".join(extra_parts) if extra_parts else ""

        gol_text = ""
        if goals_after:
            gol_list = []
            for g in goals_after:
                ex = f"+{g['extra']}" if g.get('extra') else ""
                icon = "✈️" if g['team_id'] == fixture['teams']['away']['id'] else "🏠"
                gol_list.append(f"{g['minute']}{ex}'{icon}")
            gol_text = "\n⏱️ Gol dopo: " + ", ".join(gol_list)

        text = (
            f"🏁 <b>{home} vs {away}</b>\n"
            f"🏆 {league}\n"
            f"🏁 Risultato finale: {score_h} - {score_a}{extra}\n"
            f"🔕 Silenziato al {sil_minute}'{gol_text}"
        )
        send_message(text)
    else:
        events = get_fixture_events(fixture['fixture']['id'])
        first_goal = events[0] if events else None
        last_goal = events[-1] if events else None
        stats_text = format_simple_stats(stats)
        caption = (
            f"🏁 <b>{home} vs {away}</b>\n"
            f"🏆 {league}\n"
            f"🔢 Risultato finale: {score_h} - {score_a}\n\n"
        )
        if first_goal:
            extra = f"+{first_goal['extra']}" if first_goal.get('extra') else ""
            caption += f"🥇 Primo gol: {first_goal['minute']}{extra}' ({first_goal['player']})\n"
        if last_goal and len(events) > 1:
            extra = f"+{last_goal['extra']}" if last_goal.get('extra') else ""
            caption += f"⚡ Ultimo gol: {last_goal['minute']}{extra}' ({last_goal['player']})\n"
        caption += f"\n📊 Statistiche finali:\n{stats_text}\n\n"
        caption += f"🟢 Verde = {home}\n🔴 Rosso = {away}"

        teams = list(stats.keys())
        if len(teams) >= 2:
            vals = [
                [stats[teams[0]].get('Total Shots', 0), stats[teams[0]].get('Shots on Goal', 0),
                 stats[teams[0]].get('Corners', 0)],
                [stats[teams[1]].get('Total Shots', 0), stats[teams[1]].get('Shots on Goal', 0),
                 stats[teams[1]].get('Corners', 0)]
            ]
            chart = build_bar_chart([home, away], vals[0], vals[1])
            send_photo(caption, chart, f"final_{fid}.png")
        else:
            send_message(caption)

# ─────────────────────────────────────────────────────────────
# POLLING PRINCIPALE
# ─────────────────────────────────────────────────────────────
def process_fixture(fixture):
    fid = str(fixture['fixture']['id'])
    status = fixture.get('fixture', {}).get('status', {})
    short = status.get('short', '')
    minute = status.get('elapsed', 0)

    # Partita finita
    if short in ('FT', 'AET', 'PEN'):
        if fid in matches_state or fid in silenced_matches:
            stats = get_fixture_stats(fixture['fixture']['id'])
            send_final_notification(fixture, stats)
            matches_state.pop(fid, None)
        return

    # Partita non live
    if short not in ('1H', '2H', 'HT', 'ET'):
        return

    stats = get_fixture_stats(fixture['fixture']['id'])
    if not stats:
        return

    # Anti-spam: controlla se stats totali sono identiche al ciclo precedente
    prev = matches_state.get(fid, {})
    prev_stats = prev.get('stats', {})
    if prev_stats == stats:
        return

    is_first = fid not in matches_state

    if should_notify_live(fixture['fixture']['id'], stats, minute):
        send_live_notification(fixture, stats, minute, is_first=is_first)
        teams = list(stats.keys())
        total_shots = 0
        if len(teams) >= 2:
            total_shots = stats[teams[0]].get('Total Shots', 0) + stats[teams[1]].get('Total Shots', 0)
        last_forced = prev.get('last_forced_notify', 0)
        if total_shots >= 4 and (minute - last_forced >= 30 or is_first):
            last_forced = minute
        matches_state[fid] = {
            'stats': stats,
            'minute': minute,
            'last_forced_notify': last_forced
        }
    else:
        matches_state[fid] = {
            'stats': stats,
            'minute': minute,
            'last_forced_notify': prev.get('last_forced_notify', 0)
        }

def polling_loop():
    while True:
        try:
            fixtures = get_live_fixtures()
            for f in fixtures:
                try:
                    process_fixture(f)
                except Exception as e:
                    print(f"Error processing fixture {f['fixture']['id']}: {e}")
            # Pulisci partite non più live dallo stato
            live_ids = {str(f['fixture']['id']) for f in fixtures}
            for fid in list(matches_state.keys()):
                if fid not in live_ids:
                    matches_state.pop(fid, None)
        except Exception as e:
            print(f"Polling error: {e}")
        time.sleep(POLL_INTERVAL)

# ─────────────────────────────────────────────────────────────
# CALLBACK HANDLER (THREAD)
# ─────────────────────────────────────────────────────────────
def handle_callback(update):
    callback = update.get('callback_query', {})
    data = callback.get('data', '')
    msg = callback.get('message', {})
    chat_id = msg.get('chat', {}).get('id')
    msg_id = msg.get('message_id')

    if data.startswith('sil:'):
        fid = data.split(':')[1]
        fixture = None
        fixtures = get_live_fixtures()
        for f in fixtures:
            if str(f['fixture']['id']) == fid:
                fixture = f
                break
        if fixture:
            score_h = fixture['goals']['home'] if fixture['goals']['home'] is not None else 0
            score_a = fixture['goals']['away'] if fixture['goals']['away'] is not None else 0
            minute = fixture.get('fixture', {}).get('status', {}).get('elapsed', 0)
            silenced_matches[fid] = {
                'score_home': score_h,
                'score_away': score_a,
                'minute': minute,
                'timestamp': datetime.now().isoformat()
            }
            save_silenced()
            tg_api('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': 'Partita silenziata ✅'})
            tg_api('editMessageReplyMarkup', {'chat_id': chat_id, 'message_id': msg_id})
        else:
            tg_api('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': 'Partita non trovata ❌'})

    elif data.startswith('fav:'):
        fid = data.split(':')[1]
        if fid in favorite_matches:
            favorite_matches.discard(fid)
            text = "Rimossa dai preferiti ❌"
        else:
            favorite_matches.add(fid)
            text = "Aggiunta ai preferiti ⭐"
        save_favorites()
        tg_api('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': text})
        is_fav = fid in favorite_matches
        keyboard = build_inline_keyboard(fid, is_fav)
        tg_api('editMessageReplyMarkup', {'chat_id': chat_id, 'message_id': msg_id, 'reply_markup': keyboard})

def callback_loop():
    offset = 0
    while True:
        try:
            updates = tg_api('getUpdates', {'offset': offset, 'limit': 10})
            for u in updates.get('result', []):
                offset = u['update_id'] + 1
                if 'callback_query' in u:
                    handle_callback(u)
                elif 'message' in u:
                    handle_command(u['message'])
        except Exception as e:
            print(f"Callback error: {e}")
        time.sleep(2)

# ─────────────────────────────────────────────────────────────
# COMANDI MANUALI
# ─────────────────────────────────────────────────────────────
def handle_command(message):
    text = message.get('text', '')
    chat_id = message.get('chat', {}).get('id')
    if not text:
        return
    cmd = text.split()
    command = cmd[0].lower()
    args = cmd[1:] if len(cmd) > 1 else []

    if command == '/help':
        help_text = (
            "📋 <b>Comandi disponibili:</b>\n"
            "/help - Mostra questo messaggio\n"
            "/status &lt;squadra&gt; - Info live su una partita specifica\n"
            "/favorites - Lista partite preferite\n"
            "/clearfavorites - Svuota lista preferiti\n"
            "/silenced - Lista partite silenziate\n"
            "/live - Mostra tutte le partite live trovate"
        )
        send_message(help_text, chat_id)

    elif command == '/status':
        if not args:
            send_message("⚠️ Usa: /status &lt;nome squadra&gt;", chat_id)
            return
        query = " ".join(args)
        matches = get_fixture_by_team_name(query)
        if not matches:
            send_message(f"❌ Nessuna partita live trovata per '{query}'", chat_id)
            return
        for m in matches:
            fid = str(m['fixture']['id'])
            home = m['teams']['home']['name']
            away = m['teams']['away']['name']
            minute = m.get('fixture', {}).get('status', {}).get('elapsed', 0)
            score_h = m['goals']['home'] if m['goals']['home'] is not None else 0
            score_a = m['goals']['away'] if m['goals']['away'] is not None else 0
            stats = get_fixture_stats(m['fixture']['id'])
            stats_text = format_simple_stats(stats) if stats else "Nessun dato"
            events = get_fixture_events(m['fixture']['id'])
            last_goal = events[-1] if events else None
            last_text = ""
            if last_goal:
                ex = f"+{last_goal['extra']}" if last_goal.get('extra') else ""
                last_text = f"\n⚡ Ultimo gol: {last_goal['minute']}{ex}' ({last_goal['player']})"
            msg = (
                f"⚽ <b>{home} vs {away}</b>\n"
                f"⏱️ {minute}' | {score_h}-{score_a}{last_text}\n\n"
                f"📊 Stats:\n{stats_text}"
            )
            send_message(msg, chat_id)

    elif command == '/favorites':
        if not favorite_matches:
            send_message("⭐ Nessuna partita preferita.", chat_id)
            return
        lines = ["⭐ <b>Partite preferite:</b>"]
        fixtures = get_live_fixtures()
        fav_map = {str(f['fixture']['id']): f for f in fixtures}
        for fid in favorite_matches:
            f = fav_map.get(fid)
            if f:
                home = f['teams']['home']['name']
                away = f['teams']['away']['name']
                minute = f.get('fixture', {}).get('status', {}).get('elapsed', '?')
                lines.append(f"• {home} vs {away} ({minute}')")
            else:
                lines.append(f"• ID {fid} (non live)")
        send_message("\n".join(lines), chat_id)

    elif command == '/clearfavorites':
        favorite_matches.clear()
        save_favorites()
        send_message("🗑️ Lista preferiti svuotata.", chat_id)

    elif command == '/silenced':
        if not silenced_matches:
            send_message("🔕 Nessuna partita silenziata.", chat_id)
            return
        lines = ["🔕 <b>Partite silenziate:</b>"]
        for fid, info in silenced_matches.items():
            lines.append(f"• ID {fid} al {info.get('minute','?')}'")
        send_message("\n".join(lines), chat_id)

    elif command == '/live':
        fixtures = get_live_fixtures()
        if not fixtures:
            send_message("❌ Nessuna partita live trovata al momento.", chat_id)
            return
        lines = [f"⚽ <b>Partite live trovate: {len(fixtures)}</b>"]
        for f in fixtures:
            home = f['teams']['home']['name']
            away = f['teams']['away']['name']
            league = f['league']['name']
            minute = f.get('fixture', {}).get('status', {}).get('elapsed', '?')
            score_h = f['goals']['home'] if f['goals']['home'] is not None else 0
            score_a = f['goals']['away'] if f['goals']['away'] is not None else 0
            lines.append(f"• {home} {score_h}-{score_a} {away} ({league}, {minute}')")
        send_message("\n".join(lines[:20]), chat_id)

# ─────────────────────────────────────────────────────────────
# HEALTH CHECK SERVER (Render)
# ─────────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(('0.0.0.0', 10000), HealthHandler)
    server.serve_forever()

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("=== AVVIO BOT ===")
    print(f"Token presente: {bool(TELEGRAM_BOT_TOKEN)}")
    print(f"Chat ID presente: {bool(TELEGRAM_CHAT_ID)}")
    print(f"API Key presente: {bool(API_FOOTBALL_KEY)}")

    init_persistence()
    print(f"Preferiti: {favorite_matches}")
    print(f"Silenziati: {list(silenced_matches.keys())}")

    # Test rapido Telegram
    me = tg_api('getMe')
    if me and me.get('ok'):
        print(f"Telegram OK: @{me['result']['username']}")
        send_message("🤖 <b>Trading Live Bot avviato!</b>\nBot online e pronto.")
    else:
        print(f"Telegram ERRORE: {me}")

    t_polling = threading.Thread(target=polling_loop, daemon=True)
    t_polling.start()
    print("Thread polling avviato")

    t_callback = threading.Thread(target=callback_loop, daemon=True)
    t_callback.start()
    print("Thread callback avviato")

    start_health_server()
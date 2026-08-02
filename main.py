
import os
import sys
import json
import time
import threading
import requests
import traceback

# Matplotlib setup headless
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
# LOGGING
# ─────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)

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
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Errore salvataggio {path}: {e}")

def init_persistence():
    global silenced_matches, favorite_matches
    silenced_matches = load_json(SILENCED_FILE, {})
    fav = load_json(FAVORITES_FILE, [])
    favorite_matches = set(str(x) for x in fav)
    log(f"Persistenza caricata. Silenziati: {len(silenced_matches)}, Preferiti: {len(favorite_matches)}")

def save_silenced():
    save_json(SILENCED_FILE, silenced_matches)

def save_favorites():
    save_json(FAVORITES_FILE, list(favorite_matches))

# ─────────────────────────────────────────────────────────────
# SAFE ACCESS
# ─────────────────────────────────────────────────────────────
def safe_goals(fixture):
    g = fixture.get('goals') or {}
    return g.get('home') or 0, g.get('away') or 0

def safe_fixture_id(fixture):
    return str((fixture.get('fixture') or {}).get('id', 0))

def safe_status(fixture):
    return (fixture.get('fixture') or {}).get('status') or {}

def safe_league_name(fixture):
    return (fixture.get('league') or {}).get('name', 'N/D')

def safe_team_name(fixture, side):
    return ((fixture.get('teams') or {}).get(side) or {}).get('name', 'N/D')

def safe_team_id(fixture, side):
    return ((fixture.get('teams') or {}).get(side) or {}).get('id', 0)

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
        if r.status_code != 200:
            log(f"Telegram {method} HTTP {r.status_code}: {r.text[:300]}")
            return {}
        data = r.json()
        if data is None:
            return {}
        if not data.get('ok'):
            log(f"Telegram {method} API error: {data.get('description')}")
        return data
    except Exception as e:
        log(f"Telegram error {method}: {e}")
        return {}

def send_message(text, chat_id=None, reply_markup=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        log("send_message: nessun chat_id")
        return
    payload = {'chat_id': cid, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    result = tg_api('sendMessage', payload)
    if result and result.get('ok'):
        log(f"Messaggio inviato a {cid}")
    else:
        log(f"Invio messaggio fallito: {result}")

def send_photo(caption, photo_bytes, filename, chat_id=None, reply_markup=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        log("send_photo: nessun chat_id")
        return
    files = {'photo': (filename, photo_bytes, 'image/png')}
    payload = {'chat_id': cid, 'caption': caption, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    result = tg_api('sendPhoto', payload, files)
    if result and result.get('ok'):
        log(f"Foto inviata a {cid}")
    else:
        log(f"Invio foto fallito: {result}")

# ─────────────────────────────────────────────────────────────
# API-FOOTBALL
# ─────────────────────────────────────────────────────────────
def api_get(endpoint, params=None):
    try:
        r = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS, params=params, timeout=30)
        if r.status_code != 200:
            log(f"API {endpoint} HTTP {r.status_code}: {r.text[:300]}")
            return {}
        data = r.json()
        if data is None:
            return {}
        return data
    except Exception as e:
        log(f"API error {endpoint}: {e}")
        return {}

def get_live_fixtures():
    log("Chiamata API fixtures/live=all...")
    data = api_get('fixtures', {'live': 'all'})
    fixtures = (data.get('response') or []) if isinstance(data, dict) else []
    log(f"API ha restituito {len(fixtures)} partite totali")
    filtered = []
    excluded_keywords = ['women', 'female', 'u15', 'u16', 'u17', 'u18', 'u19', 'u20',
                         'amateur', 'amateurs', 'friendlies', 'friendly', 'reserves',
                         'reserve', 'youth', 'junior']
    for f in fixtures:
        if not isinstance(f, dict):
            continue
        league_name = (f.get('league') or {}).get('name', '').lower()
        if any(k in league_name for k in excluded_keywords):
            continue
        status_short = (f.get('fixture') or {}).get('status', {}).get('short', '')
        if status_short in ('PST', 'CAN', 'ABD'):
            continue
        filtered.append(f)
    log(f"Dopo filtri: {len(filtered)} partite")
    return filtered

def get_fixture_stats(fixture_id):
    data = api_get('fixtures/statistics', {'fixture': fixture_id})
    items = (data.get('response') or []) if isinstance(data, dict) else []
    stats = {}
    for team_stats in items:
        if not isinstance(team_stats, dict):
            continue
        team = team_stats.get('team') or {}
        team_name = team.get('name') or 'Squadra'
        stats[team_name] = {}
        for s in team_stats.get('statistics') or []:
            if not isinstance(s, dict):
                continue
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
    items = (data.get('response') or []) if isinstance(data, dict) else []
    goals = []
    for ev in items:
        if not isinstance(ev, dict):
            continue
        if ev.get('type') == 'Goal':
            tm = ev.get('time') or {}
            pl = ev.get('player') or {}
            team = ev.get('team') or {}
            goals.append({
                'minute': tm.get('elapsed', 0),
                'extra': tm.get('extra', 0) or 0,
                'player': pl.get('name') or 'N/D',
                'team': team.get('name') or '',
                'team_id': team.get('id', 0)
            })
    return sorted(goals, key=lambda x: (x['minute'], x.get('extra', 0)))

def get_fixture_by_team_name(team_query):
    fixtures = get_live_fixtures()
    team_query_lower = team_query.lower()
    matches = []
    for f in fixtures:
        home = safe_team_name(f, 'home').lower()
        away = safe_team_name(f, 'away').lower()
        if team_query_lower in home or team_query_lower in away:
            matches.append(f)
    return matches

# ─────────────────────────────────────────────────────────────
# GRAFICO
# ─────────────────────────────────────────────────────────────
def build_bar_chart(team_names, values_home, values_away):
    try:
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
    except Exception as e:
        log(f"Errore grafico: {e}")
        return None

# ─────────────────────────────────────────────────────────────
# FIAMME
# ─────────────────────────────────────────────────────────────
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
    old = (old_stats.get(team_name) or {}).get(metric, 0)
    new = (new_stats.get(team_name) or {}).get(metric, 0)
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
        v1 = int((new_stats[t1] or {}).get(m, 0))
        v2 = int((new_stats[t2] or {}).get(m, 0))
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
        v1 = int((new_stats[t1] or {}).get(m, 0))
        v2 = int((new_stats[t2] or {}).get(m, 0))
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
    total_shots = (stats[t1] or {}).get('Total Shots', 0) + (stats[t2] or {}).get('Total Shots', 0)
    diff_shots = abs((stats[t1] or {}).get('Total Shots', 0) - (stats[t2] or {}).get('Total Shots', 0))
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
    try:
        fid = safe_fixture_id(fixture)
        home = safe_team_name(fixture, 'home')
        away = safe_team_name(fixture, 'away')
        league = safe_league_name(fixture)
        score_h, score_a = safe_goals(fixture)

        events = get_fixture_events((fixture.get('fixture') or {}).get('id', 0))
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
        chart = None
        if len(teams) >= 2:
            vals = [
                [(stats[teams[0]] or {}).get('Total Shots', 0), (stats[teams[0]] or {}).get('Shots on Goal', 0),
                 (stats[teams[0]] or {}).get('Corners', 0)],
                [(stats[teams[1]] or {}).get('Total Shots', 0), (stats[teams[1]] or {}).get('Shots on Goal', 0),
                 (stats[teams[1]] or {}).get('Corners', 0)]
            ]
            chart = build_bar_chart([home, away], vals[0], vals[1])

        is_fav = fid in favorite_matches
        keyboard = build_inline_keyboard(fid, is_fav)

        if chart:
            send_photo(caption, chart, f"match_{fid}.png", reply_markup=keyboard)
        else:
            send_message(caption, reply_markup=keyboard)
    except Exception as e:
        log(f"Errore send_live_notification: {e}\n{traceback.format_exc()}")

def send_final_notification(fixture, stats):
    try:
        fid = safe_fixture_id(fixture)
        home = safe_team_name(fixture, 'home')
        away = safe_team_name(fixture, 'away')
        league = safe_league_name(fixture)
        score_h, score_a = safe_goals(fixture)

        sil = silenced_matches.get(fid)
        if sil:
            events = get_fixture_events((fixture.get('fixture') or {}).get('id', 0))
            sil_minute = sil.get('minute', 'N/D')
            sil_score_h = sil.get('score_home', 0)
            sil_score_a = sil.get('score_away', 0)
            goals_after = []
            if isinstance(sil_minute, int):
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
                    icon = "✈️" if g['team_id'] == safe_team_id(fixture, 'away') else "🏠"
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
            events = get_fixture_events((fixture.get('fixture') or {}).get('id', 0))
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
                    [(stats[teams[0]] or {}).get('Total Shots', 0), (stats[teams[0]] or {}).get('Shots on Goal', 0),
                     (stats[teams[0]] or {}).get('Corners', 0)],
                    [(stats[teams[1]] or {}).get('Total Shots', 0), (stats[teams[1]] or {}).get('Shots on Goal', 0),
                     (stats[teams[1]] or {}).get('Corners', 0)]
                ]
                chart = build_bar_chart([home, away], vals[0], vals[1])
                if chart:
                    send_photo(caption, chart, f"final_{fid}.png")
                else:
                    send_message(caption)
            else:
                send_message(caption)
    except Exception as e:
        log(f"Errore send_final_notification: {e}\n{traceback.format_exc()}")

# ─────────────────────────────────────────────────────────────
# POLLING PRINCIPALE
# ─────────────────────────────────────────────────────────────
def process_fixture(fixture):
    fid = safe_fixture_id(fixture)
    if fid == '0':
        return
    status = safe_status(fixture)
    short = status.get('short', '')
    minute = status.get('elapsed', 0) or 0

    # Partita finita
    if short in ('FT', 'AET', 'PEN'):
        if fid in matches_state or fid in silenced_matches:
            log(f"Partita {fid} finita, invio notifica finale")
            stats = get_fixture_stats((fixture.get('fixture') or {}).get('id', 0))
            send_final_notification(fixture, stats)
            matches_state.pop(fid, None)
        return

    if short not in ('1H', '2H', 'HT', 'ET'):
        return

    stats = get_fixture_stats((fixture.get('fixture') or {}).get('id', 0))
    if not stats:
        return

    # Anti-spam
    prev = matches_state.get(fid, {})
    prev_stats = prev.get('stats', {})
    if prev_stats == stats:
        return

    is_first = fid not in matches_state

    if should_notify_live((fixture.get('fixture') or {}).get('id', 0), stats, minute):
        log(f"Notifica live per {fid} al minuto {minute}")
        send_live_notification(fixture, stats, minute, is_first=is_first)
        teams = list(stats.keys())
        total_shots = 0
        if len(teams) >= 2:
            total_shots = (stats[teams[0]] or {}).get('Total Shots', 0) + (stats[teams[1]] or {}).get('Total Shots', 0)
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
                    log(f"Error processing fixture: {e}\n{traceback.format_exc()}")
            live_ids = {safe_fixture_id(f) for f in fixtures}
            for fid in list(matches_state.keys()):
                if fid not in live_ids:
                    matches_state.pop(fid, None)
        except Exception as e:
            log(f"Polling error: {e}\n{traceback.format_exc()}")
        time.sleep(POLL_INTERVAL)

# ─────────────────────────────────────────────────────────────
# CALLBACK HANDLER
# ─────────────────────────────────────────────────────────────
def handle_callback(update):
    try:
        callback = update.get('callback_query', {}) or {}
        data = callback.get('data', '')
        msg = callback.get('message', {}) or {}
        chat_id = msg.get('chat', {}).get('id')
        msg_id = msg.get('message_id')

        if data.startswith('sil:'):
            fid = data.split(':')[1]
            fixture = None
            fixtures = get_live_fixtures()
            for f in fixtures:
                if safe_fixture_id(f) == fid:
                    fixture = f
                    break
            if fixture:
                score_h, score_a = safe_goals(fixture)
                minute = safe_status(fixture).get('elapsed', 0) or 0
                silenced_matches[fid] = {
                    'score_home': score_h,
                    'score_away': score_a,
                    'minute': minute,
                    'timestamp': datetime.now().isoformat()
                }
                save_silenced()
                tg_api('answerCallbackQuery', {'callback_query_id': callback.get('id'), 'text': 'Partita silenziata ✅'})
                tg_api('editMessageReplyMarkup', {'chat_id': chat_id, 'message_id': msg_id})
            else:
                tg_api('answerCallbackQuery', {'callback_query_id': callback.get('id'), 'text': 'Partita non trovata ❌'})

        elif data.startswith('fav:'):
            fid = data.split(':')[1]
            if fid in favorite_matches:
                favorite_matches.discard(fid)
                text = "Rimossa dai preferiti ❌"
            else:
                favorite_matches.add(fid)
                text = "Aggiunta ai preferiti ⭐"
            save_favorites()
            tg_api('answerCallbackQuery', {'callback_query_id': callback.get('id'), 'text': text})
            is_fav = fid in favorite_matches
            keyboard = build_inline_keyboard(fid, is_fav)
            tg_api('editMessageReplyMarkup', {'chat_id': chat_id, 'message_id': msg_id, 'reply_markup': keyboard})
    except Exception as e:
        log(f"Callback error: {e}\n{traceback.format_exc()}")

def callback_loop():
    offset = 0
    while True:
        try:
            updates = tg_api('getUpdates', {'offset': offset, 'limit': 10})
            items = (updates.get('result') or []) if isinstance(updates, dict) else []
            for u in items:
                offset = u['update_id'] + 1
                if 'callback_query' in u:
                    handle_callback(u)
                elif 'message' in u:
                    handle_command(u['message'])
        except Exception as e:
            log(f"Callback loop error: {e}\n{traceback.format_exc()}")
        time.sleep(2)

# ─────────────────────────────────────────────────────────────
# COMANDI MANUALI
# ─────────────────────────────────────────────────────────────
def handle_command(message):
    try:
        text = message.get('text', '')
        chat_id = message.get('chat', {}).get('id')
        if not text:
            return
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        if command == '/help':
            help_text = (
                "📋 <b>Comandi disponibili:</b>\n"
                "/help - Mostra questo messaggio\n"
                "/status &lt;squadra&gt; - Info live su una partita specifica\n"
                "/favorites - Lista partite preferite\n"
                "/clearfavorites - Svuota lista preferiti\n"
                "/silenced - Lista partite silenziate\n"
                "/live - Mostra tutte le partite live trovate\n"
                "/test - Test invio notifica"
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
                fid = safe_fixture_id(m)
                home = safe_team_name(m, 'home')
                away = safe_team_name(m, 'away')
                minute = safe_status(m).get('elapsed', 0) or 0
                score_h, score_a = safe_goals(m)
                stats = get_fixture_stats((m.get('fixture') or {}).get('id', 0))
                stats_text = format_simple_stats(stats) if stats else "Nessun dato"
                events = get_fixture_events((m.get('fixture') or {}).get('id', 0))
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
            fav_map = {safe_fixture_id(f): f for f in fixtures}
            for fid in favorite_matches:
                f = fav_map.get(fid)
                if f:
                    home = safe_team_name(f, 'home')
                    away = safe_team_name(f, 'away')
                    minute = safe_status(f).get('elapsed', '?')
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
                home = safe_team_name(f, 'home')
                away = safe_team_name(f, 'away')
                league = safe_league_name(f)
                minute = safe_status(f).get('elapsed', '?')
                score_h, score_a = safe_goals(f)
                lines.append(f"• {home} {score_h}-{score_a} {away} ({league}, {minute}')")
            send_message("\n".join(lines[:20]), chat_id)  # max 20 per messaggio

        elif command == '/test':
            send_message("🧪 <b>Test notifica OK!</b>\nIl bot è attivo e risponde ai comandi.", chat_id)
            # Prova anche a inviare un grafico di test
            try:
                chart = build_bar_chart(['Squadra A', 'Squadra B'], [5, 3, 2], [2, 1, 1])
                if chart:
                    send_photo("📊 Grafico di test", chart, "test.png", chat_id)
            except Exception as e:
                send_message(f"Errore grafico test: {e}", chat_id)

    except Exception as e:
        log(f"Command error: {e}\n{traceback.format_exc()}")

# ─────────────────────────────────────────────────────────────
# HEALTH CHECK SERVER
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
    log("Health server avviato sulla porta 10000")
    server.serve_forever()

# ─────────────────────────────────────────────────────────────
# TEST ALL'AVVIO
# ─────────────────────────────────────────────────────────────
def startup_tests():
    log("=== TEST DI AVVIO ===")

    # Test 1: Variabili d'ambiente
    ok = True
    if not TELEGRAM_BOT_TOKEN:
        log("❌ TELEGRAM_BOT_TOKEN mancante!")
        ok = False
    else:
        log(f"✅ TELEGRAM_BOT_TOKEN presente ({len(TELEGRAM_BOT_TOKEN)} chars)")

    if not TELEGRAM_CHAT_ID:
        log("❌ TELEGRAM_CHAT_ID mancante!")
        ok = False
    else:
        log(f"✅ TELEGRAM_CHAT_ID = {TELEGRAM_CHAT_ID}")

    if not API_FOOTBALL_KEY:
        log("❌ API_FOOTBALL_KEY mancante!")
        ok = False
    else:
        log(f"✅ API_FOOTBALL_KEY presente ({len(API_FOOTBALL_KEY)} chars)")

    if not ok:
        log("=== CONFIGURAZIONE INCOMPLETA ===")
        return False

    # Test 2: Telegram
    log("Test connessione Telegram...")
    me = tg_api('getMe')
    if me and me.get('ok'):
        bot_name = me.get('result', {}).get('username', 'N/D')
        log(f"✅ Telegram OK - Bot: @{bot_name}")
    else:
        log(f"❌ Telegram fallito: {me}")
        return False

    # Test 3: Invio messaggio di avvio
    log("Invio messaggio di avvio...")
    send_message(
        f"🤖 <b>Trading Live Bot avviato!</b>\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n"
        f"📡 Connessione OK\n"
        f"Usa /help per i comandi."
    )

    # Test 4: API-Football
    log("Test connessione API-Football...")
    test_data = api_get('status')
    if test_data and test_data.get('response'):
        account = test_data.get('response', {}).get('account', {})
        log(f"✅ API-Football OK - Piano: {account.get('subscription', 'N/D')}")
    else:
        log(f"⚠️ API-Football status: {test_data}")

    # Test 5: Partite live
    log("Test ricerca partite live...")
    fixtures = get_live_fixtures()
    if fixtures:
        log(f"✅ Trovate {len(fixtures)} partite live")
        # Logga le prime 3
        for f in fixtures[:3]:
            home = safe_team_name(f, 'home')
            away = safe_team_name(f, 'away')
            league = safe_league_name(f)
            minute = safe_status(f).get('elapsed', '?')
            log(f"   • {home} vs {away} ({league}, {minute}')")
    else:
        log("⚠️ Nessuna partita live trovata (potrebbe essere normale)")

    log("=== FINE TEST ===")
    return True

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    log("=== AVVIO BOT ===")
    init_persistence()
    startup_tests()

    t_polling = threading.Thread(target=polling_loop, daemon=True)
    t_polling.start()
    log("Thread polling avviato")

    t_callback = threading.Thread(target=callback_loop, daemon=True)
    t_callback.start()
    log("Thread callback avviato")

    start_health_server()
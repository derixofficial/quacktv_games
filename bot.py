#!/usr/bin/env python3
"""
Telegram Games Bot (base implementation)
- Uses python-telegram-bot v13 (synchronous)
- Simple SQLite persistence

Edit BOT_TOKEN below or set the BOT_TOKEN env var.
"""

import os
import logging
import sqlite3
import threading
import random
import time
from functools import wraps
from uuid import uuid4
import json

from telegram import (Bot, Update, InlineKeyboardButton,
                      InlineKeyboardMarkup, ParseMode)
from telegram.ext import (Updater, CommandHandler, MessageHandler,
                          Filters, CallbackQueryHandler, CallbackContext,
                          ChatMemberHandler)

# ===== CONFIG =====
BOT_TOKEN = os.environ.get('BOT_TOKEN') or "8501850469:AAFHQEmD6WTakgGGMl2WLVrBStUcwD7Ztgs"
# Staff admin IDs (bot staff), edit as needed
STAFF_ADMINS = [8030914400, 7235105154, 5116732881]
# Points: win = 5 points; 1 point = 20 QuackPoints (conversion)
POINTS_PER_WIN = 5
QUACKPOINTS_PER_POINT = 20

DB_PATH = 'bot_data.db'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory pending actions for private flows: {user_id: {'action': ..., 'group_id': ...}}
pending = {}
# Active timers for Parole a Blocchi: {game_id: threading.Timer}
timers = {}

# ===== DB helpers =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY,
            title TEXT,
            stored_at INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            type TEXT,
            group_id INTEGER,
            admin_id INTEGER,
            secret TEXT,
            state TEXT,
            metadata TEXT,
            created_at INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS points (
            user_id INTEGER,
            group_id INTEGER,
            points INTEGER,
            PRIMARY KEY (user_id, group_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS wins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            group_id INTEGER,
            points INTEGER,
            ts INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            group_id INTEGER,
            ts INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            text TEXT,
            data TEXT,
            ts INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def db_exec(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    res = None
    if fetch:
        res = c.fetchall()
    conn.commit()
    conn.close()
    return res


def log_event(event_type, text, data=None):
    try:
        payload = json.dumps(data, ensure_ascii=False) if data is not None else None
        db_exec('INSERT INTO logs (type, text, data, ts) VALUES (?, ?, ?, ?)', (event_type, text, payload, int(time.time())))
    except Exception:
        logger.exception('Failed to write log event')

# ===== util =====
def restricted_to_staff(fn):
    @wraps(fn)
    def wrapped(update: Update, context: CallbackContext, *a, **k):
        user_id = update.effective_user.id
        if user_id not in STAFF_ADMINS:
            update.effective_message.reply_text("Accesso negato: comando riservato allo staff del bot.")
            return
        return fn(update, context, *a, **k)
    return wrapped

# ===== Handlers =====

def start(update: Update, context: CallbackContext):
    user = update.effective_user
    bot: Bot = context.bot
    if update.effective_chat.type == 'private':
        # Build invite link (will be filled once bot username known)
        me = bot.get_me()
        invite_url = f"https://t.me/{me.username}?startgroup=true"
        text = (
            "🤖✨ Ciao {name}! Sono QuackTV Games, il tuo compagno di giochi per gruppo!\n\n"
            "Aggiungimi a un gruppo per iniziare a giocare e creare partite con i tuoi amici! 🎉🎮\n\n"
            "Usa i pulsanti qui sotto per invitarmi o per configurare un gruppo dove vuoi giocare."
        ).format(name=user.first_name)
        kb = [
            [InlineKeyboardButton('➕ Aggiungimi al gruppo!', url=invite_url)],
            [InlineKeyboardButton('▶️ Inizia a giocare!', callback_data='inicia_start')]
        ]
        update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        update.message.reply_text("Ciao! Aggiungimi in privato per le istruzioni: scrivi /start in privato.")


def chat_member_update(update: Update, context: CallbackContext):
    # Called when chat member updated — detect bot added to group
    result = update.chat_member
    new = result.new_chat_member
    chat = update.effective_chat
    bot_user = context.bot.get_me()
    if new.user and new.user.id == bot_user.id:
        # Bot status changed in this chat
        logger.info(f"Bot status changed in {chat.id}: {new.status}")
        # Save group
        db_exec('INSERT OR REPLACE INTO groups (id, title, stored_at) VALUES (?, ?, ?)',
                (chat.id, chat.title or '', int(time.time())))
        # Ask to make admin
        try:
            context.bot.send_message(chat.id, "Ciao! Mettimi Amministratore per far sì che tutto funzioni correttamente! 🙏")
            log_event('bot_added', f'Bot added to group {chat.id}', {'title': chat.title})
        except Exception as e:
            logger.exception(e)
            log_event('error', 'chat_member_update send_message failed', {'exception': str(e)})

# Callback to show group list and game menu
def callback_query(update: Update, context: CallbackContext):
    q = update.callback_query
    data = q.data
    user = q.from_user
    bot = context.bot
    q.answer()
    # pagination for logs
    if data and data.startswith('logs:'):
        # format logs: logs:page
        try:
            page = int(data.split(':')[1])
        except Exception:
            page = 1
        per = 10
        offset = (page-1)*per
        rows = db_exec('SELECT id, type, text, ts FROM logs ORDER BY id DESC LIMIT ? OFFSET ?', (per, offset), fetch=True)
        if not rows:
            q.edit_message_text('Nessun log.')
            return
        lines = [f"{r[0]} | {r[1]} | {r[2]} | {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[3]))}" for r in rows]
        kb = []
        if page > 1:
            kb.append(InlineKeyboardButton('⬅️ Indietro', callback_data=f'logs:{page-1}'))
        kb.append(InlineKeyboardButton('➡️ Avanti', callback_data=f'logs:{page+1}'))
        q.edit_message_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup([kb]))
        return
    if data == 'inicia_start':
        # Show groups where user is admin (from stored groups)
        groups = db_exec('SELECT id, title FROM groups', fetch=True)
        buttons = []
        for gid, title in groups:
            try:
                admins = bot.get_chat_administrators(gid)
                is_admin = any(a.user.id == user.id for a in admins)
                if is_admin:
                    buttons.append([InlineKeyboardButton(f"{title or gid}", callback_data=f'select_group:{gid}')])
            except Exception:
                continue
        if not buttons:
            q.edit_message_text("Non risultano gruppi configurati in cui sei admin. Assicurati di avere aggiunto il bot al gruppo.")
            return
        kb = buttons + [[InlineKeyboardButton('🔙 Annulla', callback_data='cancel')]]
        q.edit_message_text('Seleziona il gruppo su cui vuoi iniziare a giocare:', reply_markup=InlineKeyboardMarkup(kb))
    elif data and data.startswith('select_group:'):
        gid = int(data.split(':', 1)[1])
        # Show game menu
        kb = [
            [InlineKeyboardButton('🕵️ Indovina Chi', callback_data=f'game:start:indovinachi:{gid}')],
            [InlineKeyboardButton('🔤 Parole a Blocchi', callback_data=f'game:start:blocchi:{gid}')],
            [InlineKeyboardButton('⚡ Fast Game', callback_data=f'game:start:fast:{gid}')],
            [InlineKeyboardButton('🔙 Indietro', callback_data='inicia_start')]
        ]
        q.edit_message_text('Scegli il gioco:', reply_markup=InlineKeyboardMarkup(kb))
    elif data and data.startswith('game:start:'):
        parts = data.split(':')
        gtype = parts[2]
        gid = int(parts[3])
        # record pending action for this admin in private
        pending[user.id] = {'action': f'set_word_{gtype}', 'group_id': gid}
        bot.send_message(user.id, f"Hai scelto *{gtype}*. Inviami la parola segreta in questo chat privato.",
                         parse_mode=ParseMode.MARKDOWN)
        q.edit_message_text('Controlla la tua chat privata per continuare.')
    elif data == 'cancel':
        q.edit_message_text('Operazione annullata.')
    elif data and data.startswith('logspartite:'):
        try:
            page = int(data.split(':')[1])
        except Exception:
            page = 1
        per = 8
        offset = (page-1)*per
        rows = db_exec('SELECT id, type, group_id, admin_id, created_at FROM games ORDER BY created_at DESC LIMIT ? OFFSET ?', (per, offset), fetch=True)
        if not rows:
            q.edit_message_text('Nessuna partita trovata.')
            return
        lines = [f"{r[0]} | {r[1]} | group:{r[2]} | admin:{r[3]} | {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[4]))}" for r in rows]
        kb = []
        if page > 1:
            kb.append(InlineKeyboardButton('⬅️ Indietro', callback_data=f'logspartite:{page-1}'))
        kb.append(InlineKeyboardButton('➡️ Avanti', callback_data=f'logspartite:{page+1}'))
        q.edit_message_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup([kb]))
        return

# Receive text in private for flows
def private_message(update: Update, context: CallbackContext):
    user = update.effective_user
    txt = update.message.text.strip()
    if user.id in pending:
        action = pending[user.id]['action']
        gid = pending[user.id]['group_id']
        if action.startswith('set_word_'):
            gtype = action.split('_')[-1]
            if gtype == 'indovinachi':
                start_indovinachi(context.bot, user.id, gid, txt)
                update.message.reply_text('Partita Iniziata! Digita /guida per visualizzare tutte le informazioni riguardo le partite!')
            elif gtype == 'fast':
                start_fastgame(context.bot, user.id, gid, txt)
                update.message.reply_text('Fast Game iniziato!')
            elif gtype == 'blocchi':
                start_blocchi(context.bot, user.id, gid, txt)
                update.message.reply_text('Partita Parole a Blocchi iniziata!')
            pending.pop(user.id, None)
        else:
            update.message.reply_text('Azione non riconosciuta. Usa /start per ricominciare.')
    elif txt and user.id in STAFF_ADMINS and pending.get(user.id, {}).get('action') == 'annuncio_confirm':
        # fallback if flow used a confirm flag
        content = txt
        try:
            context.bot.send_message('@QuackTVUpdates', content)
            update.message.reply_text('Annuncio inviato al canale.')
            log_event('announcement', content, {'by': user.id})
        except Exception as e:
            update.message.reply_text('Errore nell\'invio dell\'annuncio.')
            log_event('error', 'announcement failed', {'exception': str(e)})
        pending.pop(user.id, None)
    else:
        update.message.reply_text("Nessuna azione in corso. Usa /start per iniziare.")

# ===== Game logic =====

def gen_game_id():
    return f"#{random.randint(10000, 99999)}"

def start_indovinachi(bot: Bot, admin_id: int, group_id: int, word: str):
    gid = group_id
    game_id = gen_game_id()
    secret = word.strip().lower()
    created = int(time.time())
    db_exec('INSERT INTO games (id, type, group_id, admin_id, secret, state, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (game_id, 'indovinachi', gid, admin_id, secret, 'active', '', created))
    # Post in group
    msg = bot.send_message(gid, f"🔔 Nuova partita di Indovina Chi! Gli indizi verranno pubblicati durante la partita.\nID Partita: {game_id}")
    try:
        bot.pin_chat_message(gid, msg.message_id, disable_notification=True)
        # Optionally unpin to match "pin silenzioso then delete pin"
        bot.unpin_chat_message(gid)
    except Exception:
        pass
    log_event('game_created', 'indovinachi', {'game_id': game_id, 'group_id': gid, 'admin_id': admin_id})

def start_fastgame(bot: Bot, admin_id: int, group_id: int, word: str):
    game_id = gen_game_id()
    secret = word.strip().lower()
    created = int(time.time())
    db_exec('INSERT INTO games (id, type, group_id, admin_id, secret, state, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (game_id, 'fast', group_id, admin_id, secret, 'active', '', created))
    bot.send_message(group_id, f"⚡ Fast Game iniziato! Primo che scrive la parola vince. Parola: *?*",
                     parse_mode=ParseMode.MARKDOWN)
    log_event('game_created', 'fast', {'game_id': game_id, 'group_id': group_id, 'admin_id': admin_id})

def start_blocchi(bot: Bot, admin_id: int, group_id: int, word: str):
    game_id = gen_game_id()
    secret = word.strip().lower()
    # Reveal one random letter
    positions = list(range(len(secret)))
    if secret:
        reveal_pos = random.choice(positions)
        display = ''.join([ch if i==reveal_pos else '_' for i,ch in enumerate(secret)])
    else:
        display = ''
    created = int(time.time())
    db_exec('INSERT INTO games (id, type, group_id, admin_id, secret, state, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (game_id, 'blocchi', group_id, admin_id, secret, 'active', display, created))
    bot.send_message(group_id, f"🔤 Partita di Parole a Blocchi iniziata: {display}")
    log_event('game_created', 'blocchi', {'game_id': game_id, 'group_id': group_id, 'admin_id': admin_id})

# Indizio command (private by admin)
def indizio(update: Update, context: CallbackContext):
    user = update.effective_user
    args = context.args
    if not args:
        update.message.reply_text("Uso: /indizio [ID] descrizione del'indizio")
        return
    gid = args[0]
    desc = ' '.join(args[1:])
    row = db_exec('SELECT group_id FROM games WHERE id=?', (gid,), fetch=True)
    if not row:
        update.message.reply_text('Partita non trovata.')
        return
    group_id = row[0][0]
    try:
        context.bot.send_message(group_id, f"💡 Indizio per {gid}: {desc}")
        update.message.reply_text('Indizio inviato al gruppo.')
        log_event('indizio_sent', desc, {'game_id': gid, 'by': user.id})
    except Exception as e:
        update.message.reply_text('Errore nell'inviare l\'indizio.')
        log_event('error', 'indizio send failed', {'exception': str(e)})

# Detect guesses in group
def group_message(update: Update, context: CallbackContext):
    txt = (update.message.text or '').strip().lower()
    if not txt:
        return
    gid = update.effective_chat.id
    user = update.effective_user
    # Log this message for tie-breakers (only while there are active games)
    active = db_exec('SELECT 1 FROM games WHERE group_id=? AND state="active" LIMIT 1', (gid,), fetch=True)
    if active:
        try:
            db_exec('INSERT INTO messages (user_id, group_id, ts) VALUES (?, ?, ?)', (user.id, gid, int(time.time())))
        except Exception:
            pass
    # Check active games in this group
    rows = db_exec('SELECT id, type, secret, metadata FROM games WHERE group_id=? AND state="active"', (gid,), fetch=True)
    for gid_game, gtype, secret, metadata in rows:
        if gtype == 'indovinachi':
            if txt == (secret or '').lower():
                # Win
                award_win(user.id, gid, context.bot)
                context.bot.send_message(gid, f"🎉 {user.first_name} ha indovinato la parola! La partita {gid_game} è conclusa.")
                db_exec('UPDATE games SET state=? WHERE id=?', ('finished', gid_game))
        elif gtype == 'fast':
            if txt == (secret or '').lower():
                award_win(user.id, gid, context.bot)
                context.bot.send_message(gid, f"⚡ {user.first_name} ha vinto il Fast Game! Parola corretta.")
                db_exec('UPDATE games SET state=? WHERE id=?', ('finished', gid_game))
        elif gtype == 'blocchi':
            # metadata stores display
            display = metadata
            secret_word = secret
            if len(txt) == 1 and txt.isalpha():
                letter = txt
                new_display = list(display)
                changed = False
                for i,ch in enumerate(secret_word):
                    if ch == letter and new_display[i] == '_':
                        new_display[i] = letter
                        changed = True
                if changed:
                    display = ''.join(new_display)
                    db_exec('UPDATE games SET metadata=? WHERE id=?', (display, gid_game))
                    context.bot.send_message(gid, f"{display}")
                    # Check reveal count
                    unrevealed = display.count('_')
                    if unrevealed <= 1 and gid_game not in timers:
                        # start 30s timer
                        t = threading.Timer(30.0, finish_blocchi, args=(gid_game, context.bot))
                        timers[gid_game] = t
                        t.start()
                # else ignore

def finish_blocchi(game_id, bot: Bot):
    row = db_exec('SELECT group_id, secret, state FROM games WHERE id=?', (game_id,), fetch=True)
    if not row:
        return
    group_id, secret, state = row[0]
    if state != 'active':
        return
    bot.send_message(group_id, f"⏱ Tempo scaduto! La parola era: {secret}")
    db_exec('UPDATE games SET state=? WHERE id=?', ('finished', game_id))
    timers.pop(game_id, None)

def award_win(user_id, group_id, bot: Bot):
    # record win (history)
    ts = int(time.time())
    try:
        db_exec('INSERT INTO wins (user_id, group_id, points, ts) VALUES (?, ?, ?, ?)', (user_id, group_id, POINTS_PER_WIN, ts))
    except Exception:
        pass
    # update cumulative points per group
    row = db_exec('SELECT points FROM points WHERE user_id=? AND group_id=?', (user_id, group_id), fetch=True)
    if row:
        pts = row[0][0] + POINTS_PER_WIN
        db_exec('UPDATE points SET points=? WHERE user_id=? AND group_id=?', (pts, user_id, group_id))
    else:
        pts = POINTS_PER_WIN
        db_exec('INSERT INTO points (user_id, group_id, points) VALUES (?, ?, ?)', (user_id, group_id, pts))
    # Log win
    try:
        log_event('win', f'user {user_id} won in {group_id}', {'user_id': user_id, 'group_id': group_id, 'new_points': pts})
    except Exception:
        pass

# Commands

def guida(update: Update, context: CallbackContext):
    text = (
        "**Guida completa**\n\n"
        "*Indovina Chi*:\n- Admin crea partita da privato -> parola segreta.\n- Admin invia indizi con /indizio [ID] testo.\n- Il primo che scrive esattamente la parola vince.\n\n"
        "*Parole a Blocchi*:\n- Admin imposta parola. Una lettera è svelata. I partecipanti possono inviare singole lettere per rivelare. Quando rimane 1 lettera non svelata parte un timer di 30s.\n\n"
        "*Fast Game*:\n- Admin imposta parola. Primo che scrive la parola vince.\n\n"
        "*Comandi*:\n- /guida: questa guida\n- /stop [ID]: ferma una partita (admin di gruppo)\n- /indizio [ID] testo: invia un indizio (admin)\n- /partite: (staff) mostra partite attive\n"
    )
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

@restricted_to_staff
def partite(update: Update, context: CallbackContext):
    rows = db_exec('SELECT id, type, group_id, created_at FROM games WHERE state="active"', fetch=True)
    if not rows:
        update.message.reply_text('Nessuna partita attiva.')
        return
    msg_lines = ['Partite attive:']
    for gid, gtype, group_id, created in rows:
        try:
            chat = context.bot.get_chat(group_id)
            title = chat.title or group_id
        except Exception:
            title = str(group_id)
        link = f"https://t.me/c/{abs(group_id)}/"
        msg_lines.append(f"- {gid} ({gtype}) in {title} — Entra nel gruppo: {link}")
    update.message.reply_text('\n'.join(msg_lines))


@restricted_to_staff
def logs_command(update: Update, context: CallbackContext):
    # show first page of logs with navigation
    per = 10
    page = 1
    offset = 0
    rows = db_exec('SELECT id, type, text, ts FROM logs ORDER BY id DESC LIMIT ? OFFSET ?', (per, offset), fetch=True)
    if not rows:
        update.message.reply_text('Nessun log disponibile.')
        return
    lines = [f"{r[0]} | {r[1]} | {r[2]} | {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[3]))}" for r in rows]
    kb = [
        [InlineKeyboardButton('➡️ Avanti', callback_data='logs:2')]
    ]
    update.message.reply_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup(kb))


@restricted_to_staff
def logspartite_command(update: Update, context: CallbackContext):
    per = 8
    page = 1
    offset = 0
    rows = db_exec('SELECT id, type, group_id, admin_id, created_at FROM games ORDER BY created_at DESC LIMIT ? OFFSET ?', (per, offset), fetch=True)
    if not rows:
        update.message.reply_text('Nessuna partita trovata.')
        return
    lines = [f"{r[0]} | {r[1]} | group:{r[2]} | admin:{r[3]} | {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[4]))}" for r in rows]
    kb = [
        [InlineKeyboardButton('➡️ Avanti', callback_data='logspartite:2')]
    ]
    update.message.reply_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup(kb))


def annuncio_command(update: Update, context: CallbackContext):
    user = update.effective_user
    # only the primary admin can use /annuncio
    if user.id != 8030914400:
        update.message.reply_text('Accesso negato: comando riservato.')
        return
    # start announcement flow
    pending[user.id] = {'action': 'annuncio_confirm'}
    update.message.reply_text('Scrivi il messaggio da inviare al canale @QuackTVUpdates:')


def classifica(update: Update, context: CallbackContext):
    # Show leaderboard. If in group, show group leaderboard. If in private, optional arg group_id.
    args = context.args
    chat = update.effective_chat
    if chat.type in ('group', 'supergroup') and not args:
        group_id = chat.id
    elif args:
        try:
            group_id = int(args[0])
        except Exception:
            update.message.reply_text('Uso: /classifica [group_id]')
            return
    else:
        update.message.reply_text('Specifica il gruppo con /classifica [group_id] quando usi in privato.')
        return
    rows = db_exec('SELECT user_id, points FROM points WHERE group_id=? ORDER BY points DESC LIMIT 20', (group_id,), fetch=True)
    if not rows:
        update.message.reply_text('Nessuna classifica disponibile per questo gruppo.')
        return
    lines = [f"Classifica per il gruppo {group_id}:"]
    rank = 1
    for user_id, pts in rows:
        quack = pts * QUACKPOINTS_PER_POINT
        try:
            user = context.bot.get_chat(user_id)
            name = user.first_name
        except Exception:
            name = str(user_id)
        lines.append(f"{rank}. {name}: {pts} punti ({quack} QuackPoints)")
        rank += 1
    update.message.reply_text('\n'.join(lines))


def weekly_champion_and_announce(bot: Bot):
    now = int(time.time())
    cutoff = now - 7 * 24 * 3600
    rows = db_exec('SELECT user_id, SUM(points) FROM wins WHERE ts>=? GROUP BY user_id ORDER BY SUM(points) DESC', (cutoff,), fetch=True)
    if not rows:
        return
    top_points = rows[0][1]
    candidates = [r[0] for r in rows if r[1] == top_points]
    if len(candidates) == 1:
        champion = candidates[0]
    else:
        # tie-breaker: count messages during the week
        best = None
        best_msgs = -1
        for uid in candidates:
            cnt_row = db_exec('SELECT COUNT(*) FROM messages WHERE user_id=? AND ts>=?', (uid, cutoff), fetch=True)
            cnt = cnt_row[0][0] if cnt_row else 0
            if cnt > best_msgs:
                best_msgs = cnt
                best = uid
        champion = best or candidates[0]
    # prepare announce
    quack = top_points * QUACKPOINTS_PER_POINT
    try:
        user = bot.get_chat(champion)
        name = user.first_name
    except Exception:
        name = str(champion)
    text = f"🏆 Campione settimanale: {name}!\nPunti: {top_points} ({quack} QuackPoints)"
    # send to all known groups
    groups = db_exec('SELECT id FROM groups', fetch=True)
    for g in groups:
        try:
            bot.send_message(g[0], text)
        except Exception:
            continue

# /stop
def stop_game(update: Update, context: CallbackContext):
    user = update.effective_user
    args = context.args
    if not args:
        update.message.reply_text('Usage: /stop [ID]')
        return
    gid = args[0]
    row = db_exec('SELECT admin_id, group_id, state FROM games WHERE id=?', (gid,), fetch=True)
    if not row:
        update.message.reply_text('Partita non trovata.')
        return
    admin_id, group_id, state = row[0]
    # Only group admins or bot staff can stop
    try:
        admins = context.bot.get_chat_administrators(group_id)
        is_admin = any(a.user.id == user.id for a in admins)
    except Exception:
        is_admin = False
    if user.id != admin_id and user.id not in STAFF_ADMINS and not is_admin:
        update.message.reply_text('Non hai i permessi per fermare questa partita.')
        return
    db_exec('UPDATE games SET state=? WHERE id=?', ('finished', gid))
    update.message.reply_text('Partita fermata.')

# ===== Main =====

def main():
    init_db()
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    dp.add_handler(CallbackQueryHandler(callback_query))

    dp.add_handler(MessageHandler(Filters.private & Filters.text & ~Filters.command, private_message))
    dp.add_handler(MessageHandler(Filters.group & Filters.text & ~Filters.command, group_message))

    dp.add_handler(CommandHandler('indizio', indizio))
    dp.add_handler(CommandHandler('guida', guida))
    dp.add_handler(CommandHandler('partite', partite))
    dp.add_handler(CommandHandler('classifica', classifica))
    dp.add_handler(CommandHandler('logs', logs_command))
    dp.add_handler(CommandHandler('logspartite', logspartite_command))
    dp.add_handler(CommandHandler('annuncio', annuncio_command))
    dp.add_handler(CommandHandler('stop', stop_game))

    updater.start_polling()
    logger.info('Bot avviato')
    # Annuncia il campione settimanale all'avvio
    try:
        weekly_champion_and_announce(updater.bot)
    except Exception:
        logger.exception('Errore durante l\'annuncio del campione settimanale')
    # Annuncio di avvio/manutenzione sul canale @QuackTVUpdates
    try:
        startup_text = (
            "✅ Bot attivo! Segui @QuackTVUpdates per aggiornamenti, manutenzioni e fix.\n"
            "Se hai bisogno di supporto scrivi in privato al bot."
        )
        updater.bot.send_message('@QuackTVUpdates', startup_text)
        log_event('startup', 'sent startup announcement to channel')
    except Exception as e:
        logger.exception('Errore invio annuncio startup')
        log_event('error', 'startup announce failed', {'exception': str(e)})
    updater.idle()

if __name__ == '__main__':
    main()

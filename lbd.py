import os
import time
import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

logger = logging.getLogger("TanyaBot.LBD")

# Indian Standard Time (IST)
IST = timezone(timedelta(hours=5, minutes=30))
TTL_30_DAYS = 2592000  # 30 Days in seconds (30 * 86400)
SPAM_COOLDOWN_SEC = 20

_redis_client = None

# Game Configurations
GAMES_META: Dict[str, Dict[str, str]] = {
    "ttt": {
        "title": "Tic-Tac-Toe (X vs O)",
        "short": "Tic-Tac-Toe",
        "icon": "❌⭕",
        "reward_desc": "Stake Pot Winnings"
    },
    "numguess": {
        "title": "3-Digit Number Hunt",
        "short": "Number Guess",
        "icon": "🔢",
        "reward_desc": "1,000 – 2,000 Coins / Win"
    },
    "scramble": {
        "title": "Scrambled Word Rush",
        "short": "Word Scramble",
        "icon": "🔤",
        "reward_desc": "300 Coins / Win"
    },
    "wordle": {
        "title": "Wordle Tile Master",
        "short": "Wordle Tile",
        "icon": "🟩",
        "reward_desc": "200 Coins / Win"
    }
}

# Lua Script: Aggregates Wins, Coins, and Player Names across N daily Redis hashes atomically on Redis server
LUA_AGGREGATE_LBD = """
local stats = {}
local names_key = KEYS[1]
for i = 2, #KEYS do
    local data = redis.call('HGETALL', KEYS[i])
    for j = 1, #data, 2 do
        local field = data[j]
        local val = tonumber(data[j+1]) or 0
        local sep = string.find(field, ':')
        if sep then
            local uid = string.sub(field, 1, sep - 1)
            local metric = string.sub(field, sep + 1)
            if not stats[uid] then
                stats[uid] = {w = 0, c = 0}
            end
            if metric == 'w' then
                stats[uid].w = stats[uid].w + val
            elseif metric == 'c' then
                stats[uid].c = stats[uid].c + val
            end
        end
    end
end

local result = {}
for uid, sc in pairs(stats) do
    if sc.w > 0 then
        local nm = redis.call('HGET', names_key, uid) or 'Player'
        table.insert(result, {uid, sc.w, sc.c, nm})
    end
end

table.sort(result, function(a, b)
    if a[2] ~= b[2] then
        return a[2] > b[2]
    end
    return a[3] > b[3]
end)

local top10 = {}
for i = 1, math.min(10, #result) do
    table.insert(top10, result[i])
end
return cjson.encode(top10)
"""


def set_redis_client(client):
    """Sets or updates the shared async Redis/Valkey client from bot.py."""
    global _redis_client
    if client is not None:
        _redis_client = client


def get_redis_client():
    """Resolves the Redis/Valkey client from memory or environment variables."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    try:
        import notification
        for attr in ["valkey_client", "redis_client", "valkey", "redis", "r", "cache", "client", "vk"]:
            c = getattr(notification, attr, None)
            if c is not None and hasattr(c, "pipeline"):
                _redis_client = c
                return _redis_client
    except Exception:
        pass

    redis_url = (
        os.getenv("VALKEY_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("VALKEY_URI")
        or os.getenv("REDIS_URI")
    )
    if redis_url:
        try:
            import redis.asyncio as aioredis
            _redis_client = aioredis.from_url(redis_url, decode_responses=True)
            return _redis_client
        except Exception:
            try:
                import valkey.asyncio as aiovalkey
                _redis_client = aiovalkey.from_url(redis_url, decode_responses=True)
                return _redis_client
            except Exception:
                pass
    return None


async def record_game_win(
    game_key: str,
    chat_id: int,
    user_id: int,
    first_name: str,
    coins_won: int,
    redis_client=None
):
    """
    Records a player's win and coins earned directly into Redis/Valkey with a 30-Day TTL.
    Uses daily bucket keys so 7-day and 30-day windows remain 100% accurate without using Server RAM or Postgres.
    """
    client = redis_client or get_redis_client()
    if not client or game_key not in GAMES_META:
        return

    day_str = datetime.now(IST).strftime("%Y-%m-%d")
    gc_key = f"lbd:{game_key}:gc:{chat_id}:{day_str}"
    glob_key = f"lbd:{game_key}:glob:{day_str}"
    names_key = "lbd:player_names"

    clean_name = (first_name or "Player").strip()[:28]
    uid_str = str(user_id)

    try:
        pipe = client.pipeline(transaction=False)
        # Update Group-specific daily bucket
        pipe.hincrby(gc_key, f"{uid_str}:w", 1)
        pipe.hincrby(gc_key, f"{uid_str}:c", int(coins_won))
        pipe.expire(gc_key, TTL_30_DAYS)

        # Update Global daily bucket
        pipe.hincrby(glob_key, f"{uid_str}:w", 1)
        pipe.hincrby(glob_key, f"{uid_str}:c", int(coins_won))
        pipe.expire(glob_key, TTL_30_DAYS)

        # Store latest player first_name for blue clickable mention
        pipe.hset(names_key, uid_str, clean_name)
        pipe.expire(names_key, TTL_30_DAYS)

        await pipe.execute()
    except Exception as e:
        logger.error(f"Failed to record leaderboard win in Redis for {game_key}: {e}")


def get_game_end_lbd_markup(
    game_key: str,
    chat_id: int,
    existing_markup: Optional[InlineKeyboardMarkup] = None
) -> InlineKeyboardMarkup:
    """
    Creates or appends a '🏆 View Game Leaderboard' button at the end of a game.
    Works for standalone text messages as well as Tic-Tac-Toe's finished 3x3 board.
    """
    btn = InlineKeyboardButton(
        "🏆 View Game Leaderboard",
        callback_data=f"lbd_v:{game_key}:30:gc:{chat_id}"
    )
    if existing_markup and existing_markup.inline_keyboard:
        rows = [list(r) for r in existing_markup.inline_keyboard]
        rows.append([btn])
        return InlineKeyboardMarkup(rows)
    return InlineKeyboardMarkup([[btn]])


def build_game_selection_menu(chat_id: int, chat_title: str = "This Group") -> Tuple[str, InlineKeyboardMarkup]:
    """Builds the main /top_players game selection menu."""
    safe_title = html.escape(chat_title or "Community Hub")
    text = (
        f"🏆 <b>ARENA HALL OF FAME</b> ✨\n"
        f"<blockquote>🏛️ <b>Group:</b> <b>{safe_title}</b>\n"
        f"📊 <b>Live Window:</b> <code>Last 7 &amp; 30 Days</code>\n"
        f"⚡ <i>Select a game below to inspect its top champions, win counts, and total coins earned!</i></blockquote>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌⭕ Tic-Tac-Toe", callback_data=f"lbd_v:ttt:30:gc:{chat_id}"),
            InlineKeyboardButton("🔢 Number Guess", callback_data=f"lbd_v:numguess:30:gc:{chat_id}")
        ],
        [
            InlineKeyboardButton("🔤 Word Scramble", callback_data=f"lbd_v:scramble:30:gc:{chat_id}"),
            InlineKeyboardButton("🟩 Wordle Tile", callback_data=f"lbd_v:wordle:30:gc:{chat_id}")
        ]
    ])
    return text, keyboard


def build_leaderboard_keyboard(game_key: str, days: int, scope: str, chat_id: int) -> InlineKeyboardMarkup:
    """Builds interactive filter buttons (7d / 30d, This GC / Overall, Back)."""
    d30_label = "📅 Last 30 Days •" if days == 30 else "📅 Last 30 Days"
    d7_label = "⚡ Last 7 Days •" if days == 7 else "⚡ Last 7 Days"

    gc_label = "🏠 This GC •" if scope == "gc" else "🏠 This GC"
    glob_label = "🌐 Overall •" if scope == "glob" else "🌐 Overall"

    # Encode current active state into callback prefix `lbd_noop` if button is already selected
    cb_d30 = "lbd_same" if days == 30 else f"lbd_v:{game_key}:30:{scope}:{chat_id}"
    cb_d7 = "lbd_same" if days == 7 else f"lbd_v:{game_key}:7:{scope}:{chat_id}"
    cb_gc = "lbd_same" if scope == "gc" else f"lbd_v:{game_key}:{days}:gc:{chat_id}"
    cb_glob = "lbd_same" if scope == "glob" else f"lbd_v:{game_key}:{days}:glob:{chat_id}"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(d30_label, callback_data=cb_d30),
            InlineKeyboardButton(d7_label, callback_data=cb_d7)
        ],
        [
            InlineKeyboardButton(gc_label, callback_data=cb_gc),
            InlineKeyboardButton(glob_label, callback_data=cb_glob)
        ],
        [
            InlineKeyboardButton("🔙 Back to Games", callback_data=f"lbd_menu:{chat_id}")
        ]
    ])


async def fetch_and_format_leaderboard(
    game_key: str,
    days: int,
    scope: str,
    chat_id: int,
    chat_title: str = "This Group"
) -> Tuple[str, InlineKeyboardMarkup]:
    """
    Fetches cached rankings or aggregates them directly inside Redis/Valkey,
    then formats the leaderboard message with blue clickable user mentions.
    """
    meta = GAMES_META.get(game_key, GAMES_META["ttt"])
    keyboard = build_leaderboard_keyboard(game_key, days, scope, chat_id)
    client = get_redis_client()

    if not client:
        err_text = (
            f"{meta['icon']} <b>{meta['title'].upper()}</b>\n"
            f"<blockquote>⚠️ <b>Leaderboard cache is syncing...</b>\n"
            f"Please try again in a few seconds!</blockquote>"
        )
        return err_text, keyboard

    now_ist = datetime.now(IST)
    date_keys = []
    for i in range(days):
        d_str = (now_ist - timedelta(days=i)).strftime("%Y-%m-%d")
        if scope == "gc":
            date_keys.append(f"lbd:{game_key}:gc:{chat_id}:{d_str}")
        else:
            date_keys.append(f"lbd:{game_key}:glob:{d_str}")

    top_players = []
    try:
        import json
        raw_res = await client.eval(LUA_AGGREGATE_LBD, len(date_keys) + 1, "lbd:player_names", *date_keys)
        if raw_res:
            parsed = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
            if isinstance(parsed, list):
                top_players = parsed
    except Exception as e:
        logger.error(f"Redis Lua leaderboard aggregation error: {e}")

    scope_badge = f"🏠 {html.escape(chat_title[:24])}" if scope == "gc" else "🌐 Global Network (All GCs)"
    time_badge = "Last 30 Days" if days == 30 else "Last 7 Days"

    if not top_players:
        empty_text = (
            f"{meta['icon']} <b>{meta['title'].upper()}</b> 🏆\n"
            f"<blockquote>📍 <b>Scope:</b> <b>{scope_badge}</b>\n"
            f"🗓️ <b>Timeframe:</b> <code>{time_badge}</code>\n"
            f"🎁 <b>Reward Pool:</b> <code>{meta['reward_desc']}</code></blockquote>\n\n"
            f"<blockquote>🏜️ <b>No victories recorded in this bracket yet!</b>\n"
            f"Win a round of <b>{meta['short']}</b> to claim the 🥇 <b>#1 Spot</b> on the board!</blockquote>"
        )
        return empty_text, keyboard

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    rows_formatted = []

    for idx, item in enumerate(top_players[:10]):
        uid = int(item[0])
        wins = int(item[1])
        coins = int(item[2])
        raw_name = str(item[3] or "Player").strip()
        safe_name = html.escape(raw_name[:20])

        # Blue clickable Telegram mention
        blue_mention = f'<b><a href="tg://user?id={uid}">{safe_name}</a></b>'
        medal = medals[idx] if idx < len(medals) else f"#{idx + 1}"
        win_word = "Win" if wins == 1 else "Wins"

        rows_formatted.append(
            f"{medal} {blue_mention} — <b>{wins:,} {win_word}</b>  <i>(+{coins:,} 🪙)</i>"
        )

    leaderboard_body = "\n".join(rows_formatted)
    final_text = (
        f"{meta['icon']} <b>{meta['title'].upper()} — TOP 10</b> 🏆\n"
        f"<blockquote>📍 <b>Scope:</b> <b>{scope_badge}</b>\n"
        f"🗓️ <b>Window:</b> <code>{time_badge}</code>  •  🏅 <b>Ranked By:</b> <code>Total Wins</code></blockquote>\n\n"
        f"{leaderboard_body}\n\n"
        f"<blockquote>💡 <i>Tap the filters below to switch between 7D / 30D or Group vs Global rankings.</i></blockquote>"
    )
    return final_text, keyboard


async def cmd_top_players(update: Update, context: CallbackContext):
    """Command handler for /top_players in Group Chats."""
    msg = update.message
    if not msg:
        return

    if msg.chat.type not in ["group", "supergroup"]:
        await msg.reply_text(
            "<blockquote>⚠️ <b>Group Chat Command!</b>\n\n"
            "Use <code>/top_players</code> inside any <b>Group Chat</b> to check game leaderboards! 🏆</blockquote>",
            parse_mode=ParseMode.HTML
        )
        return

    text, keyboard = build_game_selection_menu(msg.chat_id, msg.chat.title or "This Group")
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def handle_lbd_callback(update: Update, context: CallbackContext) -> bool:
    """
    Handles all `lbd_` callback queries.
    Enforces a strict 20-second Redis anti-spam lock on identical button clicks to protect cache load.
    """
    query: Optional[CallbackQuery] = update.callback_query
    if not query or not query.data:
        return False

    data = query.data
    if not data.startswith("lbd_"):
        return False

    msg = query.message
    if not msg:
        try:
            await query.answer()
        except Exception:
            pass
        return True

    # 1. Instant ignore if clicking the already-active filter tab
    if data == "lbd_same":
        try:
            await query.answer("⚡ This view is already open!", show_alert=False)
        except Exception:
            pass
        return True

    client = get_redis_client()

    # 2. 20-Second Anti-Spam Protection in Redis per (chat_id + message_id + target_state)
    if client:
        spam_lock_key = f"lbd:lock:{msg.chat_id}:{msg.message_id}:{data}"
        try:
            is_allowed = await client.set(spam_lock_key, "1", ex=SPAM_COOLDOWN_SEC, nx=True)
            if not is_allowed:
                try:
                    await query.answer(
                        "⏳ Please wait a few seconds before clicking the same button again!",
                        show_alert=False
                    )
                except Exception:
                    pass
                return True
        except Exception as e:
            logger.debug(f"Redis anti-spam check error: {e}")

    # 3. Back to Game Selection Menu (`lbd_menu:<chat_id>`)
    if data.startswith("lbd_menu:"):
        parts = data.split(":")
        target_cid = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else msg.chat_id
        chat_title = msg.chat.title or "This Group"
        text, keyboard = build_game_selection_menu(target_cid, chat_title)
        try:
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception:
            pass
        try:
            await query.answer()
        except Exception:
            pass
        return True

    # 4. Open Specific Game Leaderboard (`lbd_v:<game>:<days>:<scope>:<chat_id>`)
    if data.startswith("lbd_v:"):
        parts = data.split(":")
        if len(parts) < 5:
            try:
                await query.answer()
            except Exception:
                pass
            return True

        game_key = parts[1]
        days = int(parts[2]) if parts[2].isdigit() else 30
        scope = parts[3] if parts[3] in ("gc", "glob") else "gc"
        target_cid = int(parts[4]) if parts[4].lstrip("-").isdigit() else msg.chat_id
        chat_title = msg.chat.title or "This Group"

        text, keyboard = await fetch_and_format_leaderboard(
            game_key=game_key,
            days=days,
            scope=scope,
            chat_id=target_cid,
            chat_title=chat_title
        )

        # If clicked from a Game-End victory message (which shouldn't overwrite the victory result),
        # check if the message is already a leaderboard card or a game win card
        current_text = msg.text or msg.caption or ""
        is_already_lbd_card = ("TOP 10" in current_text or "ARENA HALL OF FAME" in current_text or "No victories recorded" in current_text)

        if is_already_lbd_card:
            try:
                await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception:
                pass
        else:
            # Clicked from Game Over / Winner message -> Send fresh leaderboard message below it
            try:
                await msg.reply_text(text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception:
                pass

        try:
            await query.answer()
        except Exception:
            pass
        return True

    return False

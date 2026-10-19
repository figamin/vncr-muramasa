"""
Web Console for Muramasa (VNCRbot)
Provides a browser-based interface for monitoring logs and managing the database
"""
import os
import asyncio
import aiosqlite
from flask import Flask, render_template, jsonify, request, send_from_directory, Response
from datetime import datetime
from pathlib import Path
import logging
from collections import deque
import psutil
import platform
import json
import re
import threading
import time
import subprocess
import sys
import shutil
import hmac
import secrets
import discord

from utils.database import Database, parse_approver_ids as _parse_approver_ids
from utils import hikaru_user_transfer
from utils.message_filter import apply_rules, compile_rules, validate_pattern
from utils.vndb_client import VNDBClient
from utils.theme_rules import (  # vendored rules_json contract
    validate_rules,
    rules_conflicts,
    rules_summary,
)

def is_running_under_pm2():
    """Check if the current process is running under PM2"""
    # PM2 sets these environment variables
    return bool(os.environ.get('PM2_HOME') or os.environ.get('pm_id') or os.environ.get('PM2_PROCESS_NAME'))

def get_pm2_process_name():
    """Get the PM2 process name if running under PM2"""
    return os.environ.get('name') or os.environ.get('PM2_PROCESS_NAME') or 'Muramasa'

# Configuration
DB_PATH = "data/vnbot.db"
LOG_FILE = "bot.log"
MAX_LOG_LINES = 5000
DEFAULT_PORT = 5000


def _console_port() -> int:
    """Port to serve on. Overridable so a local dev run can sit beside the deployed
    console instead of fighting it for 5000. Container
    runs leave it unset and keep the default the compose port-publish expects."""
    raw = (os.getenv("WEB_CONSOLE_PORT") or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    if raw:
        print(f"Ignoring invalid WEB_CONSOLE_PORT {raw!r}; using {DEFAULT_PORT}.")
    return DEFAULT_PORT


PORT = _console_port()
# Random per-process fallback so no known key ships in source. Basic Auth below
# does not use Flask sessions, but this stays sane if a session feature is added.
SECRET_KEY = os.getenv("WEB_CONSOLE_SECRET") or secrets.token_hex(32)

# Console login (HTTP Basic Auth). Password has no default: if unset, the console
# refuses to serve (see run_web_console) rather than silently running wide-open.
WEB_CONSOLE_USERNAME = os.getenv("WEB_CONSOLE_USERNAME", "admin")
WEB_CONSOLE_PASSWORD = os.getenv("WEB_CONSOLE_PASSWORD")

app = Flask(__name__)
app.secret_key = SECRET_KEY


@app.before_request
def require_console_login():
    """Gate every route behind HTTP Basic Auth. The browser replays the
    Authorization header on the same-origin fetch() calls automatically, so the
    whole console (one HTML page + all /api routes) is covered by this one check."""
    auth = request.authorization
    if (
        auth
        and WEB_CONSOLE_PASSWORD
        and hmac.compare_digest(auth.username or "", WEB_CONSOLE_USERNAME)
        and hmac.compare_digest(auth.password or "", WEB_CONSOLE_PASSWORD)
    ):
        return None
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Muramasa Console"'},
    )

# Setup logger
logger = logging.getLogger(__name__)

# Store recent logs in memory
log_buffer = deque(maxlen=MAX_LOG_LINES)
console_buffer = deque(maxlen=MAX_LOG_LINES)  # Full console output including print statements

def load_recent_logs_from_file():
    """Load recent logs from the log file into the buffer on startup"""
    if not os.path.exists(LOG_FILE):
        return

    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            # Read last N lines efficiently
            lines = deque(f, maxlen=MAX_LOG_LINES)

        # Track last known timestamp for continuation lines (tracebacks, etc.)
        last_timestamp = None
        last_level = 'INFO'

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Try to parse log format: "timestamp - level - message"
            try:
                # Shape: "<date> <time>,<ms> - <logger> - <LEVEL> - <message>"
                parts = line.split(' - ', 3)
                if len(parts) >= 3:
                    timestamp = parts[0].split(',')[0]  # Remove milliseconds
                    level = parts[1] if len(parts) > 1 else 'INFO'
                    message = parts[-1] if len(parts) > 2 else line

                    # Update last known timestamp/level for continuation lines
                    last_timestamp = timestamp
                    last_level = level

                    log_buffer.append({
                        'timestamp': timestamp,
                        'level': level,
                        'message': message
                    })
                else:
                    # Fallback for continuation lines (tracebacks, multi-line output)
                    # Use the last known timestamp instead of datetime.now()
                    log_buffer.append({
                        'timestamp': last_timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'level': last_level,
                        'message': line
                    })
            except Exception as e:
                logger.debug(f"Could not parse log line: {e}")
                continue

        logger.info(f"Loaded {len(log_buffer)} recent log entries from {LOG_FILE}")
    except Exception as e:
        logger.error(f"Failed to load logs from file: {e}")

# Load historical logs on startup
load_recent_logs_from_file()

# Track bot start time
bot_start_time = datetime.now()

# Bot instance reference (will be set by main.py)
bot_instance = None

# Shared DB handle for the message word-filter management routes. Reads/writes the
# same vnbot.db the bot uses, so console edits go live on the next message.
filter_db = Database()

# JavaScript's MAX_SAFE_INTEGER is 2^53 - 1
JS_MAX_SAFE_INTEGER = 9007199254740991

# Username cache to avoid rate limiting
username_cache = {}  # {user_id: username}
username_cache_lock = threading.Lock()

# Process monitoring
process = psutil.Process()
process_cpu_percent = 0.0
process_cpu_avg = 0.0

def cpu_monitor_loop():
    """Background loop to keep process CPU usage updated"""
    global process_cpu_percent, process_cpu_avg
    try:
        process.cpu_percent(interval=None)  # Prime measurement
    except Exception as exc:
        logger.warning(f"Failed to prime CPU monitor: {exc}")
    while True:
        try:
            current = process.cpu_percent(interval=1.0)
            process_cpu_percent = current
            if process_cpu_avg == 0.0:
                process_cpu_avg = current
            else:
                # Exponential moving average to prevent instant drop to zero
                process_cpu_avg = (process_cpu_avg * 0.7) + (current * 0.3)
        except Exception as exc:
            logger.warning(f"CPU monitor error: {exc}")
            process_cpu_percent = 0.0
            process_cpu_avg = 0.0
            time.sleep(1.0)

cpu_monitor_thread = threading.Thread(target=cpu_monitor_loop, daemon=True)
cpu_monitor_thread.start()

def safe_json_value(value):
    """Convert large integers to strings to preserve precision in JavaScript"""
    if isinstance(value, int) and abs(value) > JS_MAX_SAFE_INTEGER:
        return str(value)
    return value

def safe_json_row(row_dict):
    """Convert a row dict to have safe JSON values"""
    return {k: safe_json_value(v) for k, v in row_dict.items()}

def safe_json_recursive(obj):
    """Recursively convert all large integers to strings in nested dicts/lists.

    This prevents JavaScript JSON.parse() from losing precision on Discord IDs
    which exceed JavaScript's MAX_SAFE_INTEGER (2^53-1).
    """
    if isinstance(obj, dict):
        return {k: safe_json_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [safe_json_recursive(item) for item in obj]
    elif isinstance(obj, int) and abs(obj) > JS_MAX_SAFE_INTEGER:
        return str(obj)
    else:
        return obj

class ConsoleCapture:
    """Capture stdout/stderr for full console output"""
    def __init__(self, original_stream, capture_buffer):
        self.original_stream = original_stream
        # Not `self.buffer`: that name shadows a text stream's binary `.buffer`
        # attribute, which libraries (e.g. yt-dlp's write_string) call `.write`
        # on. A deque there raises "'deque' object has no attribute 'write'".
        self._capture_buffer = capture_buffer

    def write(self, text):
        # Write to original stream
        self.original_stream.write(text)
        self.original_stream.flush()

        # Capture to buffer (skip empty lines and newlines)
        text = text.rstrip()
        if text:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self._capture_buffer.append({
                'timestamp': timestamp,
                'message': text
            })
    
    def flush(self):
        self.original_stream.flush()
    
    def isatty(self):
        return self.original_stream.isatty()
    
    def fileno(self):
        """Return the file descriptor"""
        return self.original_stream.fileno()
    
    def __getattr__(self, name):
        """Delegate any other attributes to the original stream"""
        return getattr(self.original_stream, name)

class WebConsoleLogHandler(logging.Handler):
    """Custom log handler that stores logs in memory for web display"""
    def emit(self, record):
        log_entry = {
            'timestamp': datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S'),
            'level': record.levelname,
            'message': self.format(record)
        }
        log_buffer.append(log_entry)

# Setup logging
def setup_logging():
    """Setup logging to capture bot logs"""
    handler = WebConsoleLogHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    
    # Add handler to root logger to catch all logs
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    
    # Capture stdout and stderr for full console output
    sys.stdout = ConsoleCapture(sys.stdout, console_buffer)
    sys.stderr = ConsoleCapture(sys.stderr, console_buffer)

@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('console.html')

@app.route('/api/logs')
def get_logs():
    """Get recent logs"""
    limit = request.args.get('limit', 100, type=int)
    mode = request.args.get('mode', 'filtered')  # 'filtered' or 'full'
    
    if mode == 'full':
        # Merge both log_buffer and console_buffer, sort by timestamp
        all_entries = []
        
        # Add filtered logs with their level
        for log in log_buffer:
            all_entries.append({
                'timestamp': log['timestamp'],
                'level': log['level'],
                'message': log['message'],
                'type': 'log'
            })
        
        # Add console output without level
        for console in console_buffer:
            all_entries.append({
                'timestamp': console['timestamp'],
                'message': console['message'],
                'type': 'console'
            })
        
        # Sort by timestamp
        all_entries.sort(key=lambda x: x['timestamp'])
        
        return jsonify(all_entries[-limit:])
    else:
        # Return only filtered logs
        return jsonify(list(log_buffer)[-limit:])

@app.route('/api/console')
def get_console():
    """Get full console output (including print statements)"""
    limit = request.args.get('limit', 100, type=int)
    return jsonify(list(console_buffer)[-limit:])

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear all log buffers"""
    try:
        log_buffer.clear()
        console_buffer.clear()
        return jsonify({'success': True, 'message': 'Logs cleared'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

def run_async(coro, timeout=10):
    """Helper to run async code in sync context using the bot's event loop

    timeout is per call because the default suits a query or two, while a route that
    drives Discord can spend far longer: an approval alone performs a dozen HTTP calls
    (role edits, a thread post, a channel post, a DM, a message edit) and would otherwise
    be cut off mid-way, leaving the roles granted and nobody told.
    """
    if bot_instance and not bot_instance.is_closed() and hasattr(bot_instance, 'loop') and bot_instance.loop:
        # Use the bot's existing event loop via run_coroutine_threadsafe
        future = asyncio.run_coroutine_threadsafe(coro, bot_instance.loop)
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Error running coroutine in bot loop: {e}", exc_info=True)
            raise  # Re-raise the exception instead of returning None
    else:
        # Fallback if bot not available - create new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

async def fetch_user_async(user_id_int):
    """Async function to fetch Discord user with caching"""
    if not bot_instance or bot_instance.is_closed():
        return None
    
    # Check cache first
    with username_cache_lock:
        if user_id_int in username_cache:
            cached_result = username_cache[user_id_int]
            # Return cached result even if it's a special marker like "NOT_FOUND"
            if cached_result == "NOT_FOUND":
                return "NOT_FOUND"
            elif cached_result == "FORBIDDEN":
                return "FORBIDDEN"
            elif isinstance(cached_result, str):
                # Return a mock user object with the cached username
                class CachedUser:
                    def __init__(self, username):
                        self.name = username
                        self.global_name = None
                return CachedUser(cached_result)
    
    # First try cached bot user (fastest)
    user = bot_instance.get_user(user_id_int)
    if user:
        username = getattr(user, 'global_name', None) or user.name
        with username_cache_lock:
            username_cache[user_id_int] = username
        return user
    
    # Fetch from Discord API
    try:
        user = await bot_instance.fetch_user(user_id_int)
        if user:
            username = getattr(user, 'global_name', None) or user.name
            with username_cache_lock:
                username_cache[user_id_int] = username
        return user
    except discord.NotFound:
        with username_cache_lock:
            username_cache[user_id_int] = "NOT_FOUND"
        return "NOT_FOUND"
    except discord.Forbidden:
        with username_cache_lock:
            username_cache[user_id_int] = "FORBIDDEN"
        return "FORBIDDEN"  
    except Exception as e:
        logger.debug(f"Error fetching user {user_id_int}: {e}")
        return None

async def fetch_usernames_batch(user_ids):
    """Fetch usernames for multiple users in parallel with rate limiting"""
    if not bot_instance or bot_instance.is_closed():
        return {uid: f'User {uid}' for uid in user_ids}
    
    # Remove duplicates
    unique_ids = list(set(user_ids))
    
    # Process in smaller batches to avoid rate limits
    BATCH_SIZE = 10  # Fetch 10 users at a time
    results = {}
    
    for i in range(0, len(unique_ids), BATCH_SIZE):
        batch = unique_ids[i:i + BATCH_SIZE]
        
        # Fetch all users in this batch in parallel
        fetch_tasks = [fetch_user_async(uid) for uid in batch]
        users = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        
        # Process results
        for user_id, user in zip(batch, users):
            if isinstance(user, Exception):
                logger.debug(f"Exception fetching user {user_id}: {user}")
                results[user_id] = f'User {user_id}'
            elif user == "NOT_FOUND":
                results[user_id] = f'Deleted User ({user_id})'
            elif user == "FORBIDDEN":
                results[user_id] = f'Private User ({user_id})'
            elif user and hasattr(user, 'name'):
                username = getattr(user, 'global_name', None) or user.name
                results[user_id] = username
            else:
                results[user_id] = f'User {user_id}'
        
        # Small delay between batches to avoid rate limits
        if i + BATCH_SIZE < len(unique_ids):
            await asyncio.sleep(0.5)
    
    return results

async def fetch_multiple_users(user_ids):
    """Fetch multiple users in parallel"""
    tasks = [fetch_user_async(int(uid) if isinstance(uid, str) else uid) for uid in user_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Convert results to dict mapping user_id -> username
    username_map = {}
    for user_id, result in zip(user_ids, results):
        if isinstance(result, Exception):
            username_map[str(user_id)] = f'User {user_id}'
        elif result == "NOT_FOUND":
            username_map[str(user_id)] = f'Deleted User ({user_id})'
        elif result == "FORBIDDEN":
            username_map[str(user_id)] = f'Private User ({user_id})'
        elif result and hasattr(result, 'name'):
            # Use global_name if available, otherwise username
            username = getattr(result, 'global_name', None) or result.name
            username_map[str(user_id)] = username
        else:
            username_map[str(user_id)] = f'User {user_id}'
    
    return username_map

def get_discord_username_cached_only(user_id):
    """Get Discord username from cache only - fast, no API calls"""
    try:
        user_id_int = int(user_id) if isinstance(user_id, str) else user_id
        
        if not bot_instance or bot_instance.is_closed():
            return None
        
        # Only check cache - no API calls
        user = bot_instance.get_user(user_id_int)
        if user:
            # Use global_name (display name) if available, otherwise username
            username = getattr(user, 'global_name', None) or user.name
            return username
        
        return None
            
    except ValueError:
        return None
    except Exception as e:
        logger.debug(f"Error getting cached username for {user_id}: {e}")
        return None

def get_discord_username(user_id):
    """Get Discord username - tries cache first, then API (can be slow)"""
    try:
        user_id_int = int(user_id) if isinstance(user_id, str) else user_id
        
        if not bot_instance or bot_instance.is_closed():
            return f'User {user_id}'
        
        # Run the async fetch in the proper event loop context
        try:
            user = run_async(fetch_user_async(user_id_int))
        except TimeoutError:
            logger.debug(f"Timeout fetching user {user_id}")
            return f'User {user_id} (Timeout)'
        except Exception as e:
            logger.debug(f"Exception fetching user {user_id}: {e}")
            return f'User {user_id}'
        
        if user == "NOT_FOUND":
            return f'Deleted User ({user_id})'
        elif user == "FORBIDDEN":
            return f'Private User ({user_id})'
        elif user and hasattr(user, 'name'):
            # Use global_name (display name) if available, otherwise username
            username = getattr(user, 'global_name', None) or user.name
            return username
        elif user is None:
            # Fetch returned None
            return f'User {user_id}'
        else:
            return f'User {user_id}'
            
    except ValueError:
        return f'Invalid ID: {user_id}'
    except Exception as e:
        logger.debug(f"Error getting username for {user_id}: {e}")
        return f'User {user_id}'

@app.route('/api/db/tables')
def get_tables():
    """Get list of all database tables"""
    async def _get_tables():
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") as cursor:
                    tables = [row[0] for row in await cursor.fetchall()]
            return jsonify({'success': True, 'tables': tables})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_tables())

@app.route('/api/db/query', methods=['POST'])
def execute_query():
    """Execute a database query (SELECT only for safety)"""
    async def _execute_query():
        try:
            query = request.json.get('query', '').strip()
            
            # Safety check - only allow SELECT queries
            if not query.upper().startswith('SELECT'):
                return jsonify({
                    'success': False, 
                    'error': 'Only SELECT queries are allowed for safety. Use database management tools for modifications.'
                })
            
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query) as cursor:
                    rows = await cursor.fetchall()
                    columns = [description[0] for description in cursor.description] if cursor.description else []
                    # Convert large integers to strings to preserve precision in JavaScript
                    data = [safe_json_row(dict(row)) for row in rows]
            
            return jsonify({
                'success': True,
                'columns': columns,
                'data': data,
                'row_count': len(data)
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_execute_query())

@app.route('/api/db/table/<table_name>')
def get_table_data(table_name):
    """Get data from a specific table"""
    async def _get_table_data():
        try:
            # The table name is interpolated into the SQL below, so it must name an existing table
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': 'Table not found'})
                
                # Get table schema
                async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                    schema = await cursor.fetchall()
                    columns = [col[1] for col in schema]
                    column_types = {col[1]: col[2] for col in schema}
                
                # Get parameters
                limit = request.args.get('limit', 100, type=int)
                offset = request.args.get('offset', 0, type=int)
                sort_column = request.args.get('sort_column', '')
                sort_direction = request.args.get('sort_direction', 'asc')
                search_term = request.args.get('search', '')
                search_column = request.args.get('search_column', '')
                
                # Build query with optional filtering and sorting
                query = f"SELECT * FROM {table_name}"
                params = []
                
                # Add WHERE clause for search
                if search_term:
                    if search_column and search_column in columns:
                        query += f" WHERE {search_column} LIKE ?"
                        params.append(f"%{search_term}%")
                    else:
                        # Search across all columns
                        conditions = [f"{col} LIKE ?" for col in columns]
                        query += f" WHERE ({' OR '.join(conditions)})"
                        params.extend([f"%{search_term}%"] * len(columns))
                
                # Add ORDER BY clause
                if sort_column and sort_column in columns:
                    direction = 'DESC' if sort_direction.lower() == 'desc' else 'ASC'
                    query += f" ORDER BY {sort_column} {direction}"
                
                # Add LIMIT and OFFSET
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                
                # Get table data
                db.row_factory = aiosqlite.Row
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    # Convert large integers to strings to preserve precision in JavaScript
                    data = [safe_json_row(dict(row)) for row in rows]
                
                # Get total count (with search filter if applicable)
                count_query = f"SELECT COUNT(*) FROM {table_name}"
                count_params = []
                if search_term:
                    if search_column and search_column in columns:
                        count_query += f" WHERE {search_column} LIKE ?"
                        count_params.append(f"%{search_term}%")
                    else:
                        conditions = [f"{col} LIKE ?" for col in columns]
                        count_query += f" WHERE ({' OR '.join(conditions)})"
                        count_params.extend([f"%{search_term}%"] * len(columns))
                
                async with db.execute(count_query, count_params) as cursor:
                    total = (await cursor.fetchone())[0]
            
            return jsonify({
                'success': True,
                'table': table_name,
                'columns': columns,
                'column_types': column_types,
                'data': data,
                'total': total,
                'limit': limit,
                'offset': offset
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_table_data())

@app.route('/api/db/backup', methods=['POST'])
def create_backup():
    """Create a database backup"""
    async def _create_backup():
        try:
            backup_dir = Path("data/backups")
            backup_dir.mkdir(exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"manual_backup_{timestamp}.db"
            
            async with aiosqlite.connect(DB_PATH) as source:
                async with aiosqlite.connect(str(backup_path)) as backup:
                    await source.backup(backup)
            
            return jsonify({
                'success': True,
                'backup_file': str(backup_path),
                'message': f'Backup created successfully: {backup_path.name}'
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_create_backup())

@app.route('/api/db/backups')
def list_backups():
    """List available backup files"""
    try:
        backup_dir = Path("data/backups")
        if not backup_dir.exists():
            return jsonify({'success': True, 'backups': []})
        
        backups = []
        for backup_file in backup_dir.glob("*.db"):
            if backup_file.is_file():
                stat = backup_file.stat()
                backups.append({
                    'filename': backup_file.name,
                    'path': str(backup_file),
                    'size_mb': round(stat.st_size / (1024 * 1024), 2),
                    'created': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                })
        
        # Sort by creation time (newest first)
        backups.sort(key=lambda b: b['created'], reverse=True)
        
        return jsonify({'success': True, 'backups': backups})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/db/restore', methods=['POST'])
def restore_backup():
    """Restore database from a backup file"""
    async def _restore_backup():
        try:
            data = request.json
            backup_filename = data.get('filename')
            
            if not backup_filename:
                return jsonify({'success': False, 'error': 'Backup filename required'})

            # Confine to the backups dir and require a .db file; a filename with
            # path components (../, absolute path) must not resolve outside it.
            backup_dir = Path("data/backups")
            backup_path = (backup_dir / backup_filename).resolve()
            if backup_path.parent != backup_dir.resolve() or backup_path.suffix.lower() != '.db':
                return jsonify({'success': False, 'error': 'Invalid backup filename'})

            if not backup_path.exists():
                return jsonify({'success': False, 'error': 'Backup file not found'})
            
            # Create a backup of current database before restoring
            current_backup_path = Path("data/backups") / f"pre_restore_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            async with aiosqlite.connect(DB_PATH) as source:
                async with aiosqlite.connect(str(current_backup_path)) as backup:
                    await source.backup(backup)
            
            # Restore from the selected backup
            async with aiosqlite.connect(str(backup_path)) as source:
                async with aiosqlite.connect(DB_PATH) as target:
                    await source.backup(target)
            
            # Schedule bot restart after restoration to ensure proper database reload
            def delayed_restart():
                time.sleep(2)  # Give time for response to be sent
                logger.info("Initiating bot restart after database restore")
                try:
                    if bot_instance and not bot_instance.is_closed():
                        loop = bot_instance.loop
                        if loop and not loop.is_closed():
                            asyncio.run_coroutine_threadsafe(bot_instance.close(), loop).result(timeout=5)

                        time.sleep(2)  # Give Flask time to finish

                        # Check if running under PM2
                        if is_running_under_pm2():
                            pm2_name = get_pm2_process_name()
                            logger.info(f"Running under PM2, using 'pm2 restart {pm2_name}'")
                            subprocess.run(['pm2', 'restart', pm2_name], check=True)
                            os._exit(0)
                        else:
                            # Restart the entire Python process (minimized on Windows)
                            if platform.system() == "Windows":
                                startupinfo = subprocess.STARTUPINFO()
                                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                                startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE - minimize without focus
                                subprocess.Popen([sys.executable] + sys.argv, startupinfo=startupinfo)
                            else:
                                subprocess.Popen([sys.executable] + sys.argv)

                            time.sleep(1)
                            os._exit(0)
                except Exception as e:
                    logger.error(f"Error during post-restore restart: {e}")
            
            # Start restart in background thread
            threading.Thread(target=delayed_restart, daemon=False).start()
            
            return jsonify({
                'success': True,
                'message': f'Database restored from {backup_filename}. Previous database backed up as {current_backup_path.name}. Bot is restarting...',
                'restart_initiated': True
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_restore_backup())

@app.route('/api/stats')
def get_stats():
    """Get database statistics"""
    async def _get_stats():
        try:
            stats = {}
            async with aiosqlite.connect(DB_PATH) as db:
                # Get row counts for main tables
                tables = ['vn_pool', 'user_completions', 'immersion_activities']
                for table in tables:
                    try:
                        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                            count = (await cursor.fetchone())[0]
                            stats[table] = count
                    except:
                        stats[table] = 0
                
                # Get database file size
                db_size = os.path.getsize(DB_PATH)
                stats['db_size_mb'] = round(db_size / (1024 * 1024), 2)
            
            return jsonify({'success': True, 'stats': stats})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_stats())

@app.route('/api/info')
def get_info():
    """Get bot and system information"""
    try:
        # Calculate uptime
        uptime = datetime.now() - bot_start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
        
        # Check if bot is connected
        bot_running = False
        if bot_instance:
            try:
                bot_running = not bot_instance.is_closed() and bot_instance.is_ready()
            except:
                bot_running = False
        
        info = {
            'uptime': uptime_str,
            'uptime_seconds': uptime.total_seconds(),
            'start_time': bot_start_time.strftime('%Y-%m-%d %H:%M:%S'),
            'memory_mb': round(process.memory_info().rss / (1024 * 1024), 2),
            'cpu_percent': round(process_cpu_avg, 2),
            'cpu_percent_instant': round(process_cpu_percent, 2),
            'python_version': platform.python_version(),
            'platform': platform.system() + ' ' + platform.release(),
            'console_port': PORT,
            'bot_running': bot_running
        }
        
        return jsonify({'success': True, 'info': info})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/guilds')
def get_guilds():
    """Get information about connected Discord servers (main bot and sub-bots)"""
    try:
        guild_map = {}

        def add_guild_entry(guild_data, prefer_existing=False):
            """Add guild data while preventing duplicates by bot type"""
            guild_id = guild_data.get('id')
            if guild_id is not None and not isinstance(guild_id, str):
                guild_id = str(guild_id)
                guild_data['id'] = guild_id
            key = (guild_id, guild_data.get('bot_type') or 'Unknown Bot')
            if key in guild_map and prefer_existing:
                return
            guild_map[key] = guild_data
        
        # Get main bot guilds
        if bot_instance and bot_instance.guilds:
            for guild in bot_instance.guilds:
                # Count non-bot members
                human_count = sum(1 for member in guild.members if not member.bot)
                
                # Count online users (all users including bots with status != offline)
                # Status can be: online, idle, dnd (do not disturb), offline, streaming
                # Note: Presence intent must be enabled in Discord Developer Portal for this to work
                try:
                    total_online = sum(1 for member in guild.members 
                                     if str(member.status) != 'offline')
                    
                    # Count online human users only (excluding bots)
                    human_online = sum(1 for member in guild.members 
                                     if not member.bot and str(member.status) != 'offline')
                except Exception as e:
                    logger.warning(f"Could not get online count for guild {guild.name}: {e}. Presence intent may not be enabled.")
                    total_online = 0
                    human_online = 0
                
                guild_data = {
                    'id': str(guild.id),
                    'name': guild.name,
                    'member_count': guild.member_count,
                    'human_count': human_count,
                    'online_count': total_online,
                    'human_online_count': human_online,
                    'owner_id': str(guild.owner_id) if guild.owner_id else None,
                    'created_at': guild.created_at.strftime('%Y-%m-%d %H:%M:%S') if guild.created_at else None,
                    'text_channels': len(guild.text_channels),
                    'voice_channels': len(guild.voice_channels),
                    'roles': len(guild.roles) - 1,  # Subtract 1 to exclude @everyone role
                    'icon_url': str(guild.icon.url) if guild.icon else None,
                    'features': guild.features if hasattr(guild, 'features') else [],
                    'bot_type': 'Main Bot'
                }
                add_guild_entry(guild_data)
        
        # Check for sub-bot guild info from shared data file
        sub_bot_data_path = Path(__file__).parent / 'data' / 'sub_bot_guilds.json'
        if sub_bot_data_path.exists():
            try:
                with open(sub_bot_data_path, 'r') as f:
                    sub_bot_guilds = json.load(f)
                    for bot_name, guilds in sub_bot_guilds.items():
                        for guild in guilds:
                            guild_copy = dict(guild)
                            guild_copy['bot_type'] = bot_name
                            # Ensure numeric fields are ints when possible
                            try:
                                guild_copy['member_count'] = int(guild_copy.get('member_count', 0))
                            except (TypeError, ValueError):
                                guild_copy['member_count'] = 0
                            add_guild_entry(guild_copy, prefer_existing=True)
            except Exception as e:
                logger.warning(f"Failed to load sub-bot guild data: {e}")
        
        if not guild_map:
            return jsonify({
                'success': True, 
                'guilds': [],
                'total': 0,
                'total_members': 0,
                'message': 'Bot not connected to any servers'
            })
        
        # Sort by member count descending
        all_guilds = sorted(guild_map.values(), key=lambda x: x.get('member_count', 0), reverse=True)
        
        return jsonify({
            'success': True,
            'guilds': all_guilds,
            'total': len(all_guilds),
            'total_members': sum(g.get('member_count', 0) for g in all_guilds),
            'total_humans': sum(g.get('human_count', 0) for g in all_guilds),
            'total_online': sum(g.get('online_count', 0) for g in all_guilds),
            'total_human_online': sum(g.get('human_online_count', 0) for g in all_guilds)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/control/stop', methods=['POST'])
def stop_bot():
    """Stop the bot gracefully"""
    try:
        if not bot_instance:
            return jsonify({'success': False, 'error': 'Bot instance not available'})

        # Schedule bot shutdown in a separate thread to allow response to be sent
        def shutdown():
            time.sleep(0.5)  # Brief delay to let response be sent
            logger.info("Web console requested bot shutdown")
            try:
                # Check if running under PM2 - use pm2 stop to prevent auto-restart
                if is_running_under_pm2():
                    pm2_name = get_pm2_process_name()
                    logger.info(f"Running under PM2, using 'pm2 stop {pm2_name}'")
                    subprocess.run(['pm2', 'stop', pm2_name], check=True)
                else:
                    loop = bot_instance.loop
                    if loop and not loop.is_closed():
                        asyncio.run_coroutine_threadsafe(bot_instance.close(), loop)
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")

        threading.Thread(target=shutdown, daemon=True).start()
        return jsonify({'success': True, 'message': 'Bot shutdown initiated'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/control/restart', methods=['POST'])
def restart_bot():
    """Restart the entire bot process"""
    try:
        if not bot_instance:
            return jsonify({'success': False, 'error': 'Bot instance not available'})

        # Schedule process restart in a separate thread
        def restart_process():
            try:
                time.sleep(1)  # Brief delay to let response be sent
                logger.info("Web console requested bot restart - restarting process...")

                # Close the bot gracefully first
                loop = bot_instance.loop
                if loop and not loop.is_closed():
                    try:
                        asyncio.run_coroutine_threadsafe(bot_instance.close(), loop).result(timeout=5)
                    except:
                        pass

                time.sleep(2)  # Give Flask time to finish

                # Check if running under PM2 - use PM2 restart to avoid duplicate processes
                if is_running_under_pm2():
                    pm2_name = get_pm2_process_name()
                    logger.info(f"Running under PM2, using 'pm2 restart {pm2_name}'")
                    subprocess.run(['pm2', 'restart', pm2_name], check=True)
                    # PM2 will handle the restart, just exit this process
                    os._exit(0)
                else:
                    # Not under PM2 - use the subprocess method
                    logger.info(f"Restarting with: {sys.executable} {' '.join(sys.argv)}")

                    if platform.system() == "Windows":
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE - minimize without focus
                        subprocess.Popen([sys.executable] + sys.argv, startupinfo=startupinfo)
                    else:
                        subprocess.Popen([sys.executable] + sys.argv)

                    # Exit current process after new one starts
                    time.sleep(1)
                    os._exit(0)
            except Exception as e:
                logger.error(f"Error during restart: {e}")

        threading.Thread(target=restart_process, daemon=False).start()
        return jsonify({'success': True, 'message': 'Bot restart initiated'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/control/start', methods=['POST'])
def start_bot():
    """Start the bot by restarting the process"""
    try:
        # If bot is already running, don't restart
        if bot_instance and not bot_instance.is_closed():
            return jsonify({'success': False, 'error': 'Bot is already running'})

        # Restart the entire process to start the bot
        def restart_process():
            try:
                time.sleep(1)
                logger.info("Web console requested bot start - restarting process...")

                # Check if running under PM2
                if is_running_under_pm2():
                    pm2_name = get_pm2_process_name()
                    logger.info(f"Running under PM2, using 'pm2 restart {pm2_name}'")
                    subprocess.run(['pm2', 'restart', pm2_name], check=True)
                    os._exit(0)
                else:
                    # Not under PM2 - use the subprocess method
                    if platform.system() == "Windows":
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE - minimize without focus
                        subprocess.Popen([sys.executable] + sys.argv, startupinfo=startupinfo)
                    else:
                        subprocess.Popen([sys.executable] + sys.argv)

                    # Exit current process after new one starts
                    time.sleep(1)
                    os._exit(0)
            except Exception as e:
                logger.error(f"Error during start: {e}")
        
        threading.Thread(target=restart_process, daemon=False).start()
        return jsonify({'success': True, 'message': 'Bot start initiated'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/db/insert', methods=['POST'])
def insert_row():
    """Insert a new row into a table"""
    async def _insert_row():
        try:
            data = request.json
            table = data.get('table')
            row_data = data.get('data', {})
            
            if not table:
                return jsonify({'success': False, 'error': 'Table name required'})
            
            # Validate table name
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': 'Table not found'})
                
                # Get column types from table schema
                async with db.execute(f"PRAGMA table_info({table})") as cursor:
                    schema = await cursor.fetchall()
                    column_types = {col[1]: col[2] for col in schema}  # name -> type
                
                # Build INSERT query
                columns = []
                values = []
                
                for k, v in row_data.items():
                    if k.lower() == 'id' or v is None or v == '':
                        continue
                    
                    # Convert value based on column type
                    col_type = column_types.get(k, '').upper()
                    try:
                        if 'INT' in col_type:
                            # For INTEGER columns, keep as string if it's a large number
                            # aiosqlite has issues with large integers, but SQLite handles string->int conversion
                            if isinstance(v, str):
                                # Validate it's a valid integer
                                int(v)  # This will raise ValueError if invalid
                                # Keep as string - SQLite will convert it
                            else:
                                v = str(v) if isinstance(v, int) else v
                        elif 'REAL' in col_type or 'FLOAT' in col_type or 'DOUBLE' in col_type:
                            v = float(v) if isinstance(v, str) else v
                        # TEXT/BLOB types keep as-is
                    except (ValueError, TypeError) as e:
                        return jsonify({'success': False, 'error': f'Invalid value for column {k}: {str(e)}'})
                    
                    columns.append(k)
                    values.append(v)
                
                if not columns:
                    return jsonify({'success': False, 'error': 'No data to insert'})

                # Identifiers are interpolated into SQL, so reject any column not
                # in the table schema.
                unknown = [c for c in columns if c not in column_types]
                if unknown:
                    return jsonify({'success': False, 'error': f"Unknown column(s): {', '.join(unknown)}"})

                placeholders = ','.join(['?' for _ in columns])
                column_names = ','.join(columns)
                query = f"INSERT INTO {table} ({column_names}) VALUES ({placeholders})"
                
                await db.execute(query, values)
                await db.commit()
            
            return jsonify({'success': True, 'message': 'Row inserted successfully'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_insert_row())

@app.route('/api/db/update', methods=['POST'])
def update_row():
    """Update an existing row in a table"""
    async def _update_row():
        try:
            data = request.json
            table = data.get('table')
            row_data = data.get('data', {})
            id_column = data.get('id_column', 'id')
            id_value = data.get('id_value')
            
            if not table or not id_value:
                return jsonify({'success': False, 'error': 'Table name and ID required'})
            
            # Validate table name
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': 'Table not found'})
                
                # Get column types from table schema
                async with db.execute(f"PRAGMA table_info({table})") as cursor:
                    schema = await cursor.fetchall()
                    column_types = {col[1]: col[2] for col in schema}  # name -> type
                
                # Build UPDATE query (exclude id column from updates)
                updates = []
                values = []
                
                for k, v in row_data.items():
                    if k == id_column:
                        continue
                    # Column name is interpolated into SQL; reject unknown identifiers.
                    if k not in column_types:
                        return jsonify({'success': False, 'error': f'Unknown column: {k}'})

                    # Convert value based on column type
                    if v is not None and v != '':
                        col_type = column_types.get(k, '').upper()
                        try:
                            if 'INT' in col_type:
                                # For INTEGER columns, keep as string if it's a large number
                                # aiosqlite has issues with large integers, but SQLite handles string->int conversion
                                if isinstance(v, str):
                                    # Validate it's a valid integer
                                    int(v)  # This will raise ValueError if invalid
                                    # Keep as string - SQLite will convert it
                                else:
                                    v = str(v) if isinstance(v, int) else v
                            elif 'REAL' in col_type or 'FLOAT' in col_type or 'DOUBLE' in col_type:
                                v = float(v) if isinstance(v, str) else v
                            # TEXT/BLOB types keep as-is
                        except (ValueError, TypeError) as e:
                            return jsonify({'success': False, 'error': f'Invalid value for column {k}: {str(e)}'})
                    
                    updates.append(f"{k} = ?")
                    values.append(v)
                
                if not updates:
                    return jsonify({'success': False, 'error': 'No data to update'})

                # id_column is interpolated into the WHERE clause; must be a real column.
                if id_column not in column_types:
                    return jsonify({'success': False, 'error': f'Unknown id column: {id_column}'})

                # Convert id_value to string if it's an integer (for aiosqlite compatibility)
                if isinstance(id_value, int):
                    id_value = str(id_value)
                values.append(id_value)
                query = f"UPDATE {table} SET {', '.join(updates)} WHERE {id_column} = ?"
                
                await db.execute(query, values)
                await db.commit()
            
            return jsonify({'success': True, 'message': 'Row updated successfully'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_update_row())

@app.route('/api/db/delete', methods=['POST'])
def delete_row():
    """Delete a row from a table"""
    async def _delete_row():
        try:
            data = request.json
            table = data.get('table')
            id_column = data.get('id_column', 'id')
            id_value = data.get('id_value')
            
            if not table or not id_value:
                return jsonify({'success': False, 'error': 'Table name and ID required'})
            
            # Validate table name
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': 'Table not found'})

                # id_column is interpolated into the WHERE clause; must be a real column.
                async with db.execute(f"PRAGMA table_info({table})") as cursor:
                    valid_columns = {col[1] for col in await cursor.fetchall()}
                if id_column not in valid_columns:
                    return jsonify({'success': False, 'error': f'Unknown id column: {id_column}'})

                # Delete the row
                query = f"DELETE FROM {table} WHERE {id_column} = ?"
                await db.execute(query, (id_value,))
                await db.commit()
            
            return jsonify({'success': True, 'message': 'Row deleted successfully'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_delete_row())

# Log Manager API Endpoints

@app.route('/api/logs/activities')
def get_activity_logs():
    """Get activity logs with search and filtering"""
    async def _get_activity_logs():
        try:
            # Get parameters
            page = request.args.get('page', 1, type=int)
            limit = request.args.get('limit', 50, type=int)
            search = request.args.get('search', '').strip()
            user_filter = request.args.get('user_filter', '').strip()
            activity_filter = request.args.get('activity_filter', '').strip()
            type_filter = request.args.get('type_filter', '').strip()
            status_filter = request.args.get('status_filter', '').strip()
            
            offset = (page - 1) * limit
            
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                
                # Build base query without username initially
                base_query = """
                    SELECT 
                        ia.id,
                        ia.user_id,
                        ia.name,
                        ia.duration,
                        ia.media_type,
                        ia.primary_type,
                        ia.date,
                        ia.comment,
                        ia.deleted_at,
                        ia.meta
                    FROM immersion_activities ia
                """
                
                conditions = []
                params = []
                
                # Add search conditions
                if search:
                    conditions.append("(ia.name LIKE ? OR ia.comment LIKE ?)")
                    params.extend([f"%{search}%", f"%{search}%"])
                
                # Add user filter - handle both user ID and username search
                if user_filter:
                    user_ids_to_filter = []
                    
                    # First, check if it's a direct user ID match
                    if user_filter.isdigit():
                        user_ids_to_filter.append(user_filter)
                    
                    # Search through the username cache
                    with username_cache_lock:
                        for user_id, username in username_cache.items():
                            if isinstance(username, str) and username not in ["NOT_FOUND", "FORBIDDEN"]:
                                if user_filter.lower() in username.lower():
                                    user_ids_to_filter.append(str(user_id))
                    
                    # If bot is available, also search through bot's cached users
                    if bot_instance and not bot_instance.is_closed():
                        try:
                            # Search through bot's cached users
                            for user in bot_instance.users:
                                if (user_filter.lower() in user.name.lower() or 
                                    (hasattr(user, 'display_name') and user.display_name and 
                                     user_filter.lower() in user.display_name.lower()) or
                                    (hasattr(user, 'global_name') and user.global_name and
                                     user_filter.lower() in user.global_name.lower())):
                                    user_id_str = str(user.id)
                                    if user_id_str not in user_ids_to_filter:
                                        user_ids_to_filter.append(user_id_str)
                        except Exception as e:
                            logger.warning(f"Error searching users by username: {e}")
                    
                    # Also search for partial user ID match if not a pure number
                    if not user_filter.isdigit():
                        conditions.append("ia.user_id LIKE ?")
                        params.append(f"%{user_filter}%")
                    
                    # Add user ID filter if any users matched
                    if user_ids_to_filter:
                        placeholders = ','.join(['?' for _ in user_ids_to_filter])
                        if len(conditions) > 0 and not user_filter.isdigit():
                            # Combine with OR if a user_id LIKE condition is already present
                            conditions[-1] = f"(ia.user_id LIKE ? OR ia.user_id IN ({placeholders}))"
                            params.extend(user_ids_to_filter)
                        else:
                            conditions.append(f"ia.user_id IN ({placeholders})")
                            params.extend(user_ids_to_filter)
                
                # Add activity filter (reading/listening)
                if activity_filter:
                    if activity_filter == "reading":
                        conditions.append("ia.primary_type = 'reading'")
                    elif activity_filter == "listening":
                        conditions.append("ia.primary_type = 'listening'")
                
                # Add type filter
                if type_filter:
                    if type_filter == "manual":
                        conditions.append("ia.media_type IS NULL")
                    else:
                        conditions.append("ia.media_type = ?")
                        params.append(type_filter)
                
                # Add status filter
                if status_filter == "active":
                    conditions.append("ia.deleted_at IS NULL")
                elif status_filter == "undone":
                    conditions.append("ia.deleted_at IS NOT NULL")
                
                # Build final query
                where_clause = ""
                if conditions:
                    where_clause = " WHERE " + " AND ".join(conditions)
                
                # Get total count
                count_query = f"SELECT COUNT(*) FROM ({base_query}{where_clause})"
                async with db.execute(count_query, params) as cursor:
                    total = (await cursor.fetchone())[0]
                
                # Get paginated results
                query = f"{base_query}{where_clause} ORDER BY ia.date DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    logs = [safe_json_row(dict(row)) for row in rows]
                
                # Fetch actual Discord usernames using bot instance - parallel fetching
                logger.debug(f"Resolving usernames for {len(logs)} logs")
                
                # Collect all unique user IDs
                user_ids = list(set(log.get('user_id') for log in logs if log.get('user_id')))
                
                if user_ids and bot_instance and not bot_instance.is_closed():
                    # Fetch all usernames in parallel
                    username_map = await fetch_multiple_users(user_ids)
                    
                    # Apply usernames to logs
                    for log in logs:
                        user_id = log.get('user_id')
                        if user_id:
                            log['username'] = username_map.get(str(user_id), f'User {user_id}')
                        else:
                            log['username'] = 'Unknown User'
                else:
                    # No user IDs or bot not available
                    for log in logs:
                        user_id = log.get('user_id')
                        log['username'] = f'User {user_id}' if user_id else 'Unknown User'
                
                return jsonify({
                    'success': True,
                    'logs': logs,
                    'total': total,
                    'page': page,
                    'limit': limit
                })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_activity_logs())

@app.route('/api/logs/activities/edit', methods=['POST'])
def edit_activity_log():
    """Edit an activity log"""
    async def _edit_activity_log():
        try:
            data = request.json
            log_id = data.get('id')
            
            if not log_id:
                return jsonify({'success': False, 'error': 'Log ID required'})
            
            # Extract editable fields
            updates = {}
            if 'name' in data:
                updates['name'] = data['name']
            if 'duration' in data:
                updates['duration'] = int(data['duration'])
            if 'media_type' in data:
                updates['media_type'] = data['media_type']
            if 'primary_type' in data:
                updates['primary_type'] = data['primary_type']
            if 'date' in data:
                # Convert ISO date string to datetime
                updates['date'] = data['date']
            if 'comment' in data:
                updates['comment'] = data['comment']
            
            # Handle metadata fields
            meta_updates = {}
            if 'characters' in data:
                # VN character count
                meta_updates['characters'] = int(data['characters']) if data['characters'] else None
            if 'pages' in data:
                # Book/manga page count
                meta_updates['pages'] = int(data['pages']) if data['pages'] else None
            if 'episodes' in data:
                # Anime episode count
                meta_updates['episodes'] = int(data['episodes']) if data['episodes'] else None
            
            # If there are metadata updates OR duration changed, fetch current data to recalculate reading speed
            if meta_updates or 'duration' in updates:
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("SELECT meta, duration, media_type FROM immersion_activities WHERE id = ?", (log_id,)) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            import json
                            current_meta = json.loads(row['meta'] or '{}')
                            
                            # Get the duration and media type (use updated values if provided, otherwise use current)
                            duration_seconds = updates.get('duration', row['duration'])
                            media_type = updates.get('media_type', row['media_type'])
                            
                            # Update meta with new values
                            for key, value in meta_updates.items():
                                if value is None:
                                    # Remove the key if value is None
                                    current_meta.pop(key, None)
                                else:
                                    current_meta[key] = value
                            
                            # Recalculate reading speeds based on media type
                            if duration_seconds and duration_seconds > 0:
                                hours = duration_seconds / 3600
                                
                                # For VNs and books with character count
                                characters = current_meta.get('characters')
                                if characters:
                                    characters_per_hour = characters / hours
                                    current_meta['reading_speed'] = characters_per_hour
                                    if media_type == 'book':
                                        # Books store separate characters_per_hour
                                        current_meta['characters_per_hour'] = characters_per_hour
                                elif 'characters' in meta_updates and meta_updates['characters'] is None:
                                    # If characters were removed, also remove related fields
                                    current_meta.pop('reading_speed', None)
                                    current_meta.pop('characters_per_hour', None)
                                
                                # For books and manga with page count
                                pages = current_meta.get('pages')
                                if pages:
                                    pages_per_hour = pages / hours
                                    if media_type == 'book':
                                        # Books store both pages_per_hour separately
                                        current_meta['pages_per_hour'] = pages_per_hour
                                        # If no characters, use pages as primary reading_speed
                                        if not characters:
                                            current_meta['reading_speed'] = pages_per_hour
                                    elif media_type == 'manga':
                                        # Manga uses reading_speed for pages
                                        current_meta['reading_speed'] = pages_per_hour
                                elif 'pages' in meta_updates and meta_updates['pages'] is None:
                                    # If pages were removed, also remove related fields
                                    current_meta.pop('pages_per_hour', None)
                                    # Only remove reading_speed if no characters either
                                    if not characters:
                                        current_meta.pop('reading_speed', None)
                            
                            updates['meta'] = json.dumps(current_meta)
            
            if not updates:
                return jsonify({'success': False, 'error': 'No data to update'})
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Build update query
                set_clauses = [f"{key} = ?" for key in updates.keys()]
                values = list(updates.values())
                values.append(log_id)
                
                query = f"UPDATE immersion_activities SET {', '.join(set_clauses)} WHERE id = ?"
                await db.execute(query, values)
                await db.commit()
                
                # Check if any rows were affected
                if db.total_changes == 0:
                    return jsonify({'success': False, 'error': 'Log not found'})
            
            return jsonify({'success': True, 'message': 'Activity log updated successfully'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_edit_activity_log())

@app.route('/api/logs/activities/undo', methods=['POST'])
def undo_activity_log():
    """Mark an activity log as undone (soft delete)"""
    async def _undo_activity_log():
        try:
            data = request.json
            log_id = data.get('id')
            
            if not log_id:
                return jsonify({'success': False, 'error': 'Log ID required'})
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Update deleted_at timestamp
                query = "UPDATE immersion_activities SET deleted_at = datetime('now') WHERE id = ? AND deleted_at IS NULL"
                await db.execute(query, (log_id,))
                await db.commit()
                
                if db.total_changes == 0:
                    return jsonify({'success': False, 'error': 'Log not found or already undone'})
            
            return jsonify({'success': True, 'message': 'Activity log marked as undone'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_undo_activity_log())

@app.route('/api/logs/activities/restore', methods=['POST'])
def restore_activity_log():
    """Restore an undone activity log"""
    async def _restore_activity_log():
        try:
            data = request.json
            log_id = data.get('id')
            
            if not log_id:
                return jsonify({'success': False, 'error': 'Log ID required'})
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Clear deleted_at timestamp
                query = "UPDATE immersion_activities SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL"
                await db.execute(query, (log_id,))
                await db.commit()
                
                if db.total_changes == 0:
                    return jsonify({'success': False, 'error': 'Log not found or not undone'})
            
            return jsonify({'success': True, 'message': 'Activity log restored successfully'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_restore_activity_log())

@app.route('/api/logs/activities/add', methods=['POST'])
def add_activity_log():
    """Add a new activity log"""
    async def _add_activity_log():
        try:
            data = request.json
            
            # Required fields
            user_id = data.get('user_id')
            name = data.get('name', '').strip()
            duration = data.get('duration', 0)
            
            if not user_id or not name:
                return jsonify({'success': False, 'error': 'User ID and activity name are required'})
            
            # Optional fields with defaults
            media_type = data.get('media_type', 'visual_novel')
            primary_type = data.get('primary_type', 'reading')
            comment = data.get('comment', '')
            date = data.get('date')
            
            # Use current datetime if no date provided
            if not date:
                from datetime import datetime
                date = datetime.now().isoformat()
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Insert new activity log
                query = """
                    INSERT INTO immersion_activities 
                    (user_id, name, duration, media_type, primary_type, date, comment) 
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """
                await db.execute(query, (user_id, name, duration, media_type, primary_type, date, comment))
                await db.commit()
            
            return jsonify({'success': True, 'message': 'Activity log added successfully'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add_activity_log())

@app.route('/api/logs/activities/transfer', methods=['POST'])
def transfer_activity_logs():
    """Transfer all activity logs from one user ID to another"""
    async def _transfer():
        try:
            data = request.json
            old_user_id = data.get('old_user_id', '').strip()
            new_user_id = data.get('new_user_id', '').strip()

            # Validation
            if not old_user_id or not new_user_id:
                return jsonify({'success': False, 'error': 'Both old and new user IDs are required'})

            if old_user_id == new_user_id:
                return jsonify({'success': False, 'error': 'Old and new user IDs cannot be the same'})

            # Validate IDs are numeric (Discord snowflakes)
            if not old_user_id.isdigit() or not new_user_id.isdigit():
                return jsonify({'success': False, 'error': 'User IDs must be numeric Discord IDs'})

            async with aiosqlite.connect(DB_PATH) as db:
                # Count logs to transfer (including soft-deleted ones for data integrity)
                async with db.execute(
                    "SELECT COUNT(*) FROM immersion_activities WHERE user_id = ?",
                    (old_user_id,)
                ) as cursor:
                    total_count = (await cursor.fetchone())[0]

                # Count active logs for display
                async with db.execute(
                    "SELECT COUNT(*) FROM immersion_activities WHERE user_id = ? AND deleted_at IS NULL",
                    (old_user_id,)
                ) as cursor:
                    active_count = (await cursor.fetchone())[0]

                if total_count == 0:
                    return jsonify({'success': False, 'error': f'No logs found for user ID {old_user_id}'})

                # Perform the transfer
                await db.execute(
                    "UPDATE immersion_activities SET user_id = ? WHERE user_id = ?",
                    (new_user_id, old_user_id)
                )
                await db.commit()

            return jsonify({
                'success': True,
                'message': f'Successfully transferred {active_count} active logs ({total_count} total) from {old_user_id} to {new_user_id}'
            })
        except Exception as e:
            logger.error(f"Error transferring logs: {e}")
            return jsonify({'success': False, 'error': str(e)})

    return run_async(_transfer())

@app.route('/api/logs/activities/export')
def export_activity_logs():
    """Download one user's immersion logs in the same formats /log_export produces."""
    async def _export():
        user_id = request.args.get('user_id', '').strip()
        export_format = request.args.get('format', 'jsonl').strip().lower()
        include_undone = request.args.get('include_undone', '').lower() in ('1', 'true', 'yes', 'on')

        if not user_id.isdigit() or len(user_id) > 25:
            return jsonify({'success': False, 'error': 'User ID must be a numeric Discord ID'})

        if export_format not in ('jsonl', 'text'):
            return jsonify({'success': False, 'error': 'Format must be jsonl or text'})

        query = "SELECT * FROM immersion_activities WHERE user_id = ?"
        if not include_undone:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY date DESC"

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, (user_id,)) as cursor:
                rows = await cursor.fetchall()

        activities = []
        for row in rows:
            activity = dict(row)
            meta = activity.get('meta')
            if isinstance(meta, str):
                try:
                    activity['meta'] = json.loads(meta)
                except (TypeError, ValueError):
                    activity['meta'] = {}
            elif not isinstance(meta, dict):
                activity['meta'] = {}
            activities.append(activity)

        if not activities:
            return jsonify({'success': False, 'error': f'No logs found for user ID {user_id}'})

        from utils.data_transfer import export_manager

        # No cooldown here: the console is already gated, and an admin export
        # must not eat the subject's own /log_export budget.
        if export_format == 'text':
            buffer = await export_manager.export_activities_text(
                activities=activities,
                user_id=user_id,
                enforce_rate_limit=False
            )
            filename = export_manager.get_export_filename(user_id, format='text')
            mimetype = 'text/plain'  # Flask appends charset=utf-8
        else:
            buffer = await export_manager.export_activities_json(
                activities=activities,
                user_id=user_id,
                compressed=True,
                enforce_rate_limit=False
            )
            filename = export_manager.get_export_filename(user_id, compressed=True, format='jsonl')
            mimetype = 'application/gzip'

        # filename is built from the digits-only user_id plus a timestamp, so it
        # cannot carry quotes or newlines into the header.
        return Response(
            buffer.getvalue(),
            mimetype=mimetype,
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'X-Log-Count': str(len(activities))
            }
        )

    try:
        return run_async(_export())
    except Exception as e:
        logger.error(f"Error exporting logs: {e}")
        return jsonify({'success': False, 'error': str(e)})

# Blacklist Management API Endpoints

@app.route('/api/blacklist')
def get_blacklist():
    """Get all blacklisted users"""
    async def _get_blacklist():
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                
                # Check if blacklist table exists, if not create it
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_blacklist (
                        user_id TEXT PRIMARY KEY,
                        username TEXT,
                        reason TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Check if created_at column exists, if not add it
                try:
                    async with db.execute("PRAGMA table_info(user_blacklist)") as cursor:
                        columns = await cursor.fetchall()
                        has_created_at = any(col[1] == 'created_at' for col in columns)
                        has_blacklisted_by = any(col[1] == 'blacklisted_by' for col in columns)
                        
                        if not has_created_at:
                            # SQLite doesn't allow non-constant defaults in ALTER TABLE
                            # Use NULL default, application will set value on insert
                            await db.execute("ALTER TABLE user_blacklist ADD COLUMN created_at DATETIME")
                            await db.commit()

                        if not has_blacklisted_by:
                            await db.execute("ALTER TABLE user_blacklist ADD COLUMN blacklisted_by TEXT")
                            await db.commit()
                except Exception as e:
                    logger.warning(f"Could not add columns to user_blacklist: {e}")
                
                # Get all blacklisted users - check what columns exist first
                blacklist = []
                try:
                    # Check table structure
                    async with db.execute("PRAGMA table_info(user_blacklist)") as cursor:
                        columns_info = await cursor.fetchall()
                        available_columns = [col[1] for col in columns_info]
                    
                    # Build SELECT query based on available columns
                    select_columns = ['user_id']
                    if 'username' in available_columns:
                        select_columns.append('username')
                    if 'reason' in available_columns:
                        select_columns.append('reason')
                    if 'blacklisted_by' in available_columns:
                        select_columns.append('blacklisted_by')
                    if 'created_at' in available_columns:
                        select_columns.append('created_at')
                    
                    # Add NULL placeholders for missing columns
                    if 'username' not in available_columns:
                        select_columns.append('NULL as username')
                    if 'reason' not in available_columns:
                        select_columns.append('NULL as reason')
                    if 'blacklisted_by' not in available_columns:
                        select_columns.append('NULL as blacklisted_by')
                    if 'created_at' not in available_columns:
                        select_columns.append('NULL as created_at')
                    
                    select_query = f"SELECT {', '.join(select_columns)} FROM user_blacklist"
                    if 'created_at' in available_columns:
                        select_query += " ORDER BY created_at DESC"
                    
                    async with db.execute(select_query) as cursor:
                        rows = await cursor.fetchall()
                        blacklist = [safe_json_row(dict(row)) for row in rows]

                except Exception as e:
                    logger.error(f"Error loading blacklist: {e}")
                    # Ultimate fallback - just get user_id
                    try:
                        async with db.execute("SELECT user_id, NULL as username, NULL as reason, NULL as blacklisted_by, NULL as created_at FROM user_blacklist") as cursor:
                            rows = await cursor.fetchall()
                            blacklist = [safe_json_row(dict(row)) for row in rows]
                    except Exception as e2:
                        logger.error(f"Ultimate fallback failed: {e2}")
                        blacklist = []
                
                # Collect all user IDs from blacklist entries
                user_ids_to_fetch = set()
                for entry in blacklist:
                    user_id = entry.get('user_id')
                    if user_id:
                        try:
                            user_ids_to_fetch.add(int(user_id))
                        except (ValueError, TypeError):
                            pass
                
                # Batch fetch all usernames
                if user_ids_to_fetch:
                    await fetch_usernames_batch(list(user_ids_to_fetch))
                
                # Update with actual Discord usernames from cache
                for entry in blacklist:
                    user_id = entry.get('user_id')
                    if user_id:
                        # Check cache for username
                        with username_cache_lock:
                            cached_username = username_cache.get(int(user_id))
                        
                        if cached_username:
                            entry['username'] = cached_username
                        elif not entry.get('username'):
                            # No cached username and no stored username
                            entry['username'] = f'User {user_id}'
                        # A stored username with no fresh one is kept as-is
                
                return jsonify({
                    'success': True,
                    'blacklist': blacklist
                })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_blacklist())

@app.route('/api/blacklist/add', methods=['POST'])
def add_to_blacklist():
    """Add a user to the blacklist"""
    async def _add_to_blacklist():
        try:
            data = request.json
            user_id = data.get('user_id', '').strip()
            reason = data.get('reason', '').strip()
            
            if not user_id:
                return jsonify({'success': False, 'error': 'User ID required'})
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Create table if it doesn't exist
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_blacklist (
                        user_id TEXT PRIMARY KEY,
                        username TEXT,
                        reason TEXT,
                        blacklisted_by TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Try to get actual Discord username (cache only for speed)
                username = get_discord_username_cached_only(user_id)
                # If not in cache, username will be None and stored as NULL in database
                
                # Check what columns exist before inserting
                async with db.execute("PRAGMA table_info(user_blacklist)") as cursor:
                    columns_info = await cursor.fetchall()
                    available_columns = [col[1] for col in columns_info]
                
                # Default blacklisted_by to web console admin
                blacklisted_by = "Web Console Admin"
                
                # Build insert query based on available columns
                insert_columns = ['user_id']
                insert_values = [user_id]
                
                if 'username' in available_columns:
                    insert_columns.append('username')
                    insert_values.append(username)
                    
                if 'reason' in available_columns:
                    insert_columns.append('reason')
                    insert_values.append(reason)
                    
                if 'blacklisted_by' in available_columns:
                    insert_columns.append('blacklisted_by')
                    insert_values.append(blacklisted_by)

                if 'created_at' in available_columns:
                    from datetime import datetime
                    insert_columns.append('created_at')
                    insert_values.append(datetime.now().isoformat())

                # Create the INSERT query
                columns_str = ', '.join(insert_columns)
                placeholders = ', '.join(['?' for _ in insert_values])
                
                await db.execute(
                    f"INSERT OR REPLACE INTO user_blacklist ({columns_str}) VALUES ({placeholders})",
                    insert_values
                )
                await db.commit()
                
                return jsonify({'success': True, 'message': f'User {user_id} added to blacklist'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add_to_blacklist())

@app.route('/api/blacklist/remove', methods=['POST'])
def remove_from_blacklist():
    """Remove a user from the blacklist"""
    async def _remove_from_blacklist():
        try:
            data = request.json
            user_id = data.get('user_id', '').strip()
            
            if not user_id:
                return jsonify({'success': False, 'error': 'User ID required'})
            
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM user_blacklist WHERE user_id = ?", (user_id,))
                await db.commit()
                
                if db.total_changes == 0:
                    return jsonify({'success': False, 'error': 'User not found in blacklist'})
                
                return jsonify({'success': True, 'message': f'User {user_id} removed from blacklist'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_remove_from_blacklist())

@app.route('/api/kizuna/links')
def list_kizuna_links():
    """List all kizuna ↔ discord links with resolved usernames."""
    async def _list():
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id, kizuna_username, discord_user_id, created_at "
                    "FROM kizuna_links ORDER BY created_at DESC"
                ) as cursor:
                    rows = await cursor.fetchall()
            links = []
            for row in rows:
                link_id, kizuna_username, discord_user_id, created_at = row
                username = get_discord_username_cached_only(discord_user_id)
                links.append({
                    'id': link_id,
                    'kizuna_username': kizuna_username,
                    'discord_user_id': discord_user_id,
                    'username': username,
                    'created_at': created_at,
                })
            return jsonify({'success': True, 'links': links})
        except Exception as e:
            logger.error(f"Error listing kizuna links: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_list())


@app.route('/api/kizuna/links/add', methods=['POST'])
def add_kizuna_link_route():
    """Admin add: link a kizuna username to a discord user (overrides allowed
    only by removing the existing row first via the delete endpoint)."""
    async def _add():
        try:
            data = request.json or {}
            kizuna_username = (data.get('kizuna_username') or '').strip().casefold()
            discord_user_id = (data.get('discord_user_id') or '').strip()
            if not kizuna_username or not discord_user_id:
                return jsonify({'success': False, 'error': 'kizuna_username and discord_user_id are required'})
            if not discord_user_id.isdigit():
                return jsonify({'success': False, 'error': 'discord_user_id must be a numeric Discord ID'})
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO kizuna_links (kizuna_username, discord_user_id) VALUES (?, ?)",
                        (kizuna_username, discord_user_id),
                    )
                    await db.commit()
            except aiosqlite.IntegrityError:
                return jsonify({
                    'success': False,
                    'error': f'Kizuna user "{kizuna_username}" is already linked to another account. '
                             f'Delete the existing link first.',
                })
            return jsonify({'success': True, 'message': f'Linked {kizuna_username} → {discord_user_id}'})
        except Exception as e:
            logger.error(f"Error adding kizuna link: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add())


@app.route('/api/kizuna/links/edit', methods=['POST'])
def edit_kizuna_link_route():
    """Update kizuna_username and/or discord_user_id for an existing link."""
    async def _edit():
        try:
            data = request.json or {}
            link_id = data.get('id')
            kizuna_username = (data.get('kizuna_username') or '').strip().casefold()
            discord_user_id = (data.get('discord_user_id') or '').strip()
            if link_id is None:
                return jsonify({'success': False, 'error': 'id is required'})
            if not kizuna_username or not discord_user_id:
                return jsonify({'success': False, 'error': 'kizuna_username and discord_user_id are required'})
            if not discord_user_id.isdigit():
                return jsonify({'success': False, 'error': 'discord_user_id must be a numeric Discord ID'})
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute(
                        "UPDATE kizuna_links SET kizuna_username = ?, discord_user_id = ? WHERE id = ?",
                        (kizuna_username, discord_user_id, int(link_id)),
                    )
                    await db.commit()
                    if cursor.rowcount == 0:
                        return jsonify({'success': False, 'error': 'Link not found'})
            except aiosqlite.IntegrityError:
                return jsonify({
                    'success': False,
                    'error': f'Kizuna user "{kizuna_username}" is already linked to another account.',
                })
            return jsonify({'success': True, 'message': 'Link updated'})
        except Exception as e:
            logger.error(f"Error editing kizuna link: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_edit())


@app.route('/api/kizuna/links/delete', methods=['POST'])
def delete_kizuna_link_route():
    """Delete a kizuna link by row id."""
    async def _delete():
        try:
            data = request.json or {}
            link_id = data.get('id')
            if link_id is None:
                return jsonify({'success': False, 'error': 'id is required'})
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "DELETE FROM kizuna_links WHERE id = ?", (int(link_id),)
                )
                await db.commit()
                if cursor.rowcount == 0:
                    return jsonify({'success': False, 'error': 'Link not found'})
            return jsonify({'success': True, 'message': 'Link deleted'})
        except Exception as e:
            logger.error(f"Error deleting kizuna link: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_delete())


# ---------------------------------------------------------------------------
# Message word-filter management
# ---------------------------------------------------------------------------

@app.route('/api/message-filters/rules')
def mf_list_rules():
    """List all filter rules."""
    async def _list():
        try:
            rules = await filter_db.get_message_filter_rules()
            return jsonify({'success': True, 'rules': rules})
        except Exception as e:
            logger.error(f"Error listing message filter rules: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_list())


@app.route('/api/message-filters/rules/add', methods=['POST'])
def mf_add_rule():
    """Add a rule (validated before storing)."""
    async def _add():
        try:
            data = request.json or {}
            pattern = (data.get('pattern') or '').strip()
            replacement = data.get('replacement') or ''
            is_regex = bool(data.get('is_regex'))
            ignore_case = bool(data.get('ignore_case', True))
            whole_word = bool(data.get('whole_word', False))
            match_plurals = bool(data.get('match_plurals', False))
            if not pattern:
                return jsonify({'success': False, 'error': 'Pattern is required'})
            ok, error = validate_pattern(pattern, is_regex, replacement, ignore_case, whole_word, match_plurals)
            if not ok:
                return jsonify({'success': False, 'error': error})
            rule_id = await filter_db.add_message_filter_rule(
                pattern, replacement, is_regex=is_regex, ignore_case=ignore_case,
                whole_word=whole_word, match_plurals=match_plurals, created_by='web-console',
            )
            if rule_id is None:
                return jsonify({'success': False, 'error': 'Failed to add rule'})
            return jsonify({'success': True, 'id': rule_id})
        except Exception as e:
            logger.error(f"Error adding message filter rule: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add())


@app.route('/api/message-filters/rules/edit', methods=['POST'])
def mf_edit_rule():
    """Edit an existing rule (validated before storing)."""
    async def _edit():
        try:
            data = request.json or {}
            rule_id = data.get('id')
            if rule_id is None:
                return jsonify({'success': False, 'error': 'id is required'})
            pattern = (data.get('pattern') or '').strip()
            replacement = data.get('replacement') or ''
            is_regex = bool(data.get('is_regex'))
            ignore_case = bool(data.get('ignore_case', True))
            whole_word = bool(data.get('whole_word', False))
            match_plurals = bool(data.get('match_plurals', False))
            if not pattern:
                return jsonify({'success': False, 'error': 'Pattern is required'})
            ok, error = validate_pattern(pattern, is_regex, replacement, ignore_case, whole_word, match_plurals)
            if not ok:
                return jsonify({'success': False, 'error': error})
            updated = await filter_db.update_message_filter_rule(
                int(rule_id), pattern=pattern, replacement=replacement,
                is_regex=is_regex, ignore_case=ignore_case, whole_word=whole_word,
                match_plurals=match_plurals,
            )
            if not updated:
                return jsonify({'success': False, 'error': 'Rule not found'})
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error editing message filter rule: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_edit())


@app.route('/api/message-filters/rules/delete', methods=['POST'])
def mf_delete_rule():
    """Delete a rule by id."""
    async def _delete():
        try:
            data = request.json or {}
            rule_id = data.get('id')
            if rule_id is None:
                return jsonify({'success': False, 'error': 'id is required'})
            removed = await filter_db.delete_message_filter_rule(int(rule_id))
            if not removed:
                return jsonify({'success': False, 'error': 'Rule not found'})
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error deleting message filter rule: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_delete())


@app.route('/api/message-filters/rules/toggle', methods=['POST'])
def mf_toggle_rule():
    """Enable/disable one rule."""
    async def _toggle():
        try:
            data = request.json or {}
            rule_id = data.get('id')
            if rule_id is None:
                return jsonify({'success': False, 'error': 'id is required'})
            enabled = bool(data.get('enabled'))
            updated = await filter_db.update_message_filter_rule(int(rule_id), enabled=enabled)
            if not updated:
                return jsonify({'success': False, 'error': 'Rule not found'})
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error toggling message filter rule: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_toggle())


@app.route('/api/message-filters/settings', methods=['GET', 'POST'])
def mf_settings():
    """Get or update the global toggle and mod-log channel."""
    if request.method == 'GET':
        async def _get():
            try:
                settings = await filter_db.get_message_filter_settings()
                # Discord IDs exceed JS's safe-integer range, so send as a string;
                # a raw number would be rounded by the browser and no longer match
                # the channel <option>, making the picker appear to reset.
                if settings.get('log_channel_id') is not None:
                    settings['log_channel_id'] = str(settings['log_channel_id'])
                return jsonify({'success': True, 'settings': settings})
            except Exception as e:
                logger.error(f"Error getting message filter settings: {e}")
                return jsonify({'success': False, 'error': str(e)})
        return run_async(_get())

    async def _set():
        try:
            data = request.json or {}
            if 'enabled' in data:
                await filter_db.set_message_filter_enabled(bool(data.get('enabled')))
            if 'log_channel_id' in data:
                raw = data.get('log_channel_id')
                channel_id = int(raw) if raw not in (None, '', 'none') else None
                await filter_db.set_message_filter_log_channel(channel_id)
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error updating message filter settings: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_set())


@app.route('/api/message-filters/excluded-roles')
def mf_list_excluded_roles():
    """List exempt role ids."""
    async def _list():
        try:
            role_ids = await filter_db.get_message_filter_excluded_roles()
            return jsonify({'success': True, 'role_ids': [str(r) for r in role_ids]})
        except Exception as e:
            logger.error(f"Error listing excluded roles: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_list())


@app.route('/api/message-filters/excluded-roles/add', methods=['POST'])
def mf_add_excluded_role():
    """Exempt a role from filtering."""
    async def _add():
        try:
            data = request.json or {}
            raw = str(data.get('role_id') or '').strip()
            if not raw.isdigit():
                return jsonify({'success': False, 'error': 'role_id must be numeric'})
            await filter_db.add_message_filter_excluded_role(int(raw), added_by='web-console')
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error adding excluded role: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add())


@app.route('/api/message-filters/excluded-roles/remove', methods=['POST'])
def mf_remove_excluded_role():
    """Remove a role from the exempt list."""
    async def _remove():
        try:
            data = request.json or {}
            raw = str(data.get('role_id') or '').strip()
            if not raw.isdigit():
                return jsonify({'success': False, 'error': 'role_id must be numeric'})
            await filter_db.remove_message_filter_excluded_role(int(raw))
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error removing excluded role: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_remove())


@app.route('/api/message-filters/excluded-channels')
def mf_list_excluded_channels():
    """List channel ids where the whole pipeline (filter + media review) is skipped."""
    async def _list():
        try:
            channel_ids = await filter_db.get_message_filter_excluded_channels()
            return jsonify({'success': True, 'channel_ids': [str(c) for c in channel_ids]})
        except Exception as e:
            logger.error(f"Error listing excluded channels: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_list())


@app.route('/api/message-filters/excluded-channels/add', methods=['POST'])
def mf_add_excluded_channel():
    """Skip the whole pipeline in a channel."""
    async def _add():
        try:
            data = request.json or {}
            raw = str(data.get('channel_id') or '').strip()
            if not raw.isdigit():
                return jsonify({'success': False, 'error': 'channel_id must be numeric'})
            await filter_db.add_message_filter_excluded_channel(int(raw), added_by='web-console')
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error adding excluded channel: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add())


@app.route('/api/message-filters/excluded-channels/remove', methods=['POST'])
def mf_remove_excluded_channel():
    """Resume the pipeline in a channel."""
    async def _remove():
        try:
            data = request.json or {}
            raw = str(data.get('channel_id') or '').strip()
            if not raw.isdigit():
                return jsonify({'success': False, 'error': 'channel_id must be numeric'})
            await filter_db.remove_message_filter_excluded_channel(int(raw))
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error removing excluded channel: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_remove())


@app.route('/api/message-filters/logs')
def mf_logs():
    """Recent filtered-message audit log."""
    async def _logs():
        try:
            logs = await filter_db.get_message_filter_logs(limit=100)
            # Stringify snowflake IDs so the browser shows them exactly (see settings note).
            for entry in logs:
                for key in ('channel_id', 'message_id', 'author_id'):
                    if entry.get(key) is not None:
                        entry[key] = str(entry[key])
            return jsonify({'success': True, 'logs': logs})
        except Exception as e:
            logger.error(f"Error getting message filter logs: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_logs())


@app.route('/api/message-filters/test', methods=['POST'])
def mf_test():
    """Dry-run against some text. If a draft rule is supplied (the one being
    composed in the Add form), preview that rule; otherwise apply the saved
    enabled rules."""
    async def _test():
        try:
            data = request.json or {}
            text = data.get('text') or ''
            draft = data.get('draft') or {}
            draft_pattern = (draft.get('pattern') or '').strip()
            if draft_pattern:
                is_regex = bool(draft.get('is_regex'))
                ignore_case = bool(draft.get('ignore_case', True))
                whole_word = bool(draft.get('whole_word', False))
                match_plurals = bool(draft.get('match_plurals', False))
                replacement = draft.get('replacement') or ''
                ok, error = validate_pattern(draft_pattern, is_regex, replacement, ignore_case, whole_word, match_plurals)
                if not ok:
                    return jsonify({'success': False, 'error': error})
                rows = [{'id': 0, 'pattern': draft_pattern, 'replacement': replacement,
                         'is_regex': int(is_regex), 'ignore_case': int(ignore_case),
                         'whole_word': int(whole_word), 'match_plurals': int(match_plurals)}]
                scope = 'draft'
            else:
                rows = await filter_db.get_message_filter_rules(enabled_only=True)
                scope = 'saved'
            compiled = compile_rules(rows)
            cleaned, matched = apply_rules(text, compiled)
            return jsonify({'success': True, 'cleaned': cleaned, 'matched': matched,
                            'changed': bool(matched) and cleaned != text, 'scope': scope})
        except Exception as e:
            logger.error(f"Error testing message filter: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_test())


@app.route('/api/message-filters/meta')
def mf_meta():
    """Text channels and roles across the bot's guild(s) for the console pickers.

    Each entry carries its guild id as well as its name. A tab that edits per-guild state
    has to be able to narrow the pickers to one guild: offering every guild's channels in
    one list lets a setting be pointed at a channel the guild it belongs to cannot see.
    """
    channels = []
    roles = []
    guilds = []
    try:
        if bot_instance and bot_instance.guilds:
            for guild in bot_instance.guilds:
                guilds.append({'id': str(guild.id), 'name': guild.name})
                for ch in guild.text_channels:
                    channels.append({'id': str(ch.id), 'name': f'#{ch.name}',
                                     'guild': guild.name, 'guild_id': str(guild.id)})
                for role in guild.roles:
                    if role.is_default():
                        continue
                    roles.append({'id': str(role.id), 'name': role.name,
                                  'guild': guild.name, 'guild_id': str(guild.id)})
    except Exception as e:
        logger.error(f"Error building message filter meta: {e}")
    return jsonify({'success': True, 'channels': channels, 'roles': roles,
                    'guilds': guilds})


# ---------------------------------------------------------------------------
# Onboarding: settings, application queue, and decisions
# ---------------------------------------------------------------------------

# The guild config keys this tab surfaces alongside the onboarding_settings table. They
# live in config.json because the welcome message and the verification cog read them
# from there, predating this tab.
ONBOARDING_CONFIG_KEYS = (
    'verification_enabled',
    'welcome_channel',
    'verification_channel',
    'announcements_channel',
    'events_channel',
    'resources_channel',
)


def _onboarding_guild_id():
    """The guild this tab manages, as (guild_id, error).

    The console serves one bot, and muramasa runs in a single guild, so the configured
    guild is taken rather than asked for. A request may still name one explicitly.

    Failures come back as a message rather than an exception. The tab parses every
    response as JSON, so anything raised here would reach the operator as a parse error
    with the reason only in the server log.
    """
    requested = request.args.get('guild_id')
    if not requested and request.method == 'POST':
        # Silent, because a body that is not JSON raises an HTTPException otherwise.
        requested = (request.get_json(silent=True) or {}).get('guild_id')
    if requested:
        try:
            guild_id = int(requested)
        except (TypeError, ValueError):
            return None, 'Invalid guild id'
        # Refused rather than written blind. Settings are keyed by guild id, so an id the
        # bot is not in would create a row nothing ever reads, which reads back as saved.
        # Only checked while the bot is up; with it down there is nothing to check against
        # and the read-only view is still worth serving.
        if bot_instance and bot_instance.guilds:
            if not any(g.id == guild_id for g in bot_instance.guilds):
                return None, 'The bot is not in that server'
        return guild_id, None
    config = load_config()
    if config is None:
        return None, 'config.json could not be read. See the bot log for the reason.'
    for gid in (config.get('guilds') or {}):
        try:
            return int(gid), None
        except (TypeError, ValueError):
            continue
    return None, 'No guild configured'


def _onboarding_async(coro, timeout=10):
    """run_async for the onboarding routes, with failures rendered as JSON.

    run_async re-raises whatever the coroutine hit, and a call that outruns its timeout
    is not cancelled: it keeps going on the bot's loop. Both need saying in the response
    rather than in a 500 page the tab cannot parse.
    """
    try:
        return run_async(coro, timeout=timeout)
    except TimeoutError:
        return jsonify({'success': False, 'error':
                        f'The bot did not answer within {timeout}s. It may still be '
                        'working; reload before trying again.'})
    except Exception as e:
        # run_async has already logged the traceback for anything the bot's loop raised.
        logger.error(f"Error running onboarding request: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/onboarding/settings', methods=['GET', 'POST'])
def onboarding_settings():
    """Read or write the onboarding settings plus the adjacent guild config keys."""
    guild_id, guild_error = _onboarding_guild_id()
    if guild_id is None:
        return jsonify({'success': False, 'error': guild_error})

    if request.method == 'GET':
        async def _get():
            try:
                settings = dict(await filter_db.get_onboarding_settings(guild_id))
                # Discord ids exceed JS's safe-integer range, so they go out as strings
                # or the browser rounds them and the picker appears to reset.
                for key in ('entry_channel_id', 'entry_message_id', 'review_channel_id',
                            'quiz_channel_id', 'moderator_role_id'):
                    if settings.get(key) is not None:
                        settings[key] = str(settings[key])
                config = load_config()
                guild_config = (config.get('guilds') or {}).get(str(guild_id), {})
                return jsonify({
                    'success': True,
                    'guild_id': str(guild_id),
                    'settings': settings,
                    'config': {k: guild_config.get(k) for k in ONBOARDING_CONFIG_KEYS},
                    'funnel': await filter_db.get_onboarding_funnel_counts(guild_id),
                })
            except Exception as e:
                logger.error(f"Error getting onboarding settings: {e}")
                return jsonify({'success': False, 'error': str(e)})
        return _onboarding_async(_get())

    async def _set():
        try:
            data = request.json or {}
            fields = {}
            for key in ('entry_channel_id', 'review_channel_id', 'quiz_channel_id',
                        'moderator_role_id'):
                if key in data:
                    raw = data.get(key)
                    fields[key] = int(raw) if raw not in (None, '', 'none') else None
            if 'approver_ids' in data:
                # Normalised through the same parser the bot reads with, so whatever the
                # box is left in becomes exactly the list that decides applications, and
                # an unparseable entry is dropped here rather than silently at read time.
                fields['approver_ids'] = ','.join(
                    str(i) for i in _parse_approver_ids(data.get('approver_ids'))
                )
            # The same prerequisites /manage_onboarding enforces. Without them the
            # flow accepts applicants whose passes have nowhere to be reviewed: the
            # applicant is told they are under review and the card is never posted.
            current = await filter_db.get_onboarding_settings(guild_id)
            after = dict(current)
            after.update(fields)
            if 'auto_approve_join_requests' in data:
                fields['auto_approve_join_requests'] = (
                    1 if data.get('auto_approve_join_requests') else 0
                )
            if 'enabled' in data:
                fields['enabled'] = 1 if data.get('enabled') else 0
                after['enabled'] = fields['enabled']
            if after.get('enabled'):
                if not after.get('entry_message_id'):
                    return jsonify({'success': False, 'error':
                                    'Post the entry message before enabling the flow.'})
                if not after.get('review_channel_id'):
                    return jsonify({'success': False, 'error':
                                    'Set a review channel before enabling the flow, '
                                    'or approvals have nowhere to land.'})
                if not _parse_approver_ids(after.get('approver_ids')):
                    return jsonify({'success': False, 'error':
                                    'Add at least one approver before enabling the flow, '
                                    'or applications queue up with nobody able to decide '
                                    'them.'})
            if 'reapply_cooldown_days' in data:
                raw = data.get('reapply_cooldown_days')
                fields['reapply_cooldown_days'] = max(0, int(raw or 0))
            if fields:
                await filter_db.update_onboarding_settings(guild_id, **fields)

            # The config.json half. Written only for keys actually present, so a partial
            # save cannot blank a channel the form did not send.
            supplied = {k: data[k] for k in ONBOARDING_CONFIG_KEYS if k in data}
            if supplied:
                config = load_config()
                guild_config = config.setdefault('guilds', {}).setdefault(str(guild_id), {})
                for key, value in supplied.items():
                    if key == 'verification_enabled':
                        guild_config[key] = bool(value)
                    elif value in (None, '', 'none'):
                        guild_config.pop(key, None)
                    else:
                        guild_config[key] = str(value)
                save_config(config)
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error updating onboarding settings: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return _onboarding_async(_set())


@app.route('/api/onboarding/queue')
def onboarding_queue():
    """Applications still moving through the flow, newest stage first."""
    guild_id, guild_error = _onboarding_guild_id()
    if guild_id is None:
        return jsonify({'success': False, 'error': guild_error})

    async def _get():
        try:
            rows = []
            for status in ('awaiting_review', 'awaiting_quiz'):
                for app_row in await filter_db.get_applications_by_status(guild_id, status):
                    entry = dict(app_row)
                    for key in ('user_id', 'thread_id', 'review_message_id'):
                        if entry.get(key) is not None:
                            entry[key] = str(entry[key])
                    # Resolved here rather than in the browser so the tab does not need
                    # its own Discord lookup; absent when the bot is down.
                    entry['display_name'] = None
                    if bot_instance and not bot_instance.is_closed():
                        guild = bot_instance.get_guild(guild_id)
                        member = guild.get_member(int(app_row['user_id'])) if guild else None
                        if member is not None:
                            entry['display_name'] = str(member)
                    rows.append(entry)
            return jsonify({
                'success': True,
                'applications': rows,
                'funnel': await filter_db.get_onboarding_funnel_counts(guild_id),
                'orphaned': await filter_db.count_applications_without_card(guild_id),
                'bot_online': bool(bot_instance and not bot_instance.is_closed()),
            })
        except Exception as e:
            logger.error(f"Error getting onboarding queue: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return _onboarding_async(_get())


@app.route('/api/onboarding/decide', methods=['POST'])
def onboarding_decide():
    """Approve or reject an application from the console.

    The only console route that mutates Discord state. It refuses outright when the bot
    is unavailable rather than letting run_async fall back to a fresh event loop, which
    would drive discord.py objects bound to a loop that is no longer running.
    """
    if not bot_instance or bot_instance.is_closed():
        return jsonify({'success': False, 'error': 'Bot is not running'})

    data = request.get_json(silent=True) or {}
    try:
        app_id = int(data.get('app_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid application id'})
    action = data.get('action')
    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'error': 'Unknown action'})
    note = str(data.get('note') or '')[:400]
    guild_id, guild_error = _onboarding_guild_id()
    if guild_id is None:
        return jsonify({'success': False, 'error': guild_error})

    async def _decide():
        try:
            cog = bot_instance.get_cog('OnboardingCog')
            if cog is None:
                return jsonify({'success': False, 'error': 'Onboarding cog not loaded'})
            guild = bot_instance.get_guild(guild_id)
            if guild is None:
                return jsonify({'success': False, 'error': 'Bot is not in that guild'})
            # decided_by records the bot's own id, since a console decision has no
            # Discord user behind it. The label is what the review card shows; it must
            # not go anywhere the applicant reads, and decision_note is the applicant's.
            decider = bot_instance.user.id if bot_instance.user else 0
            if action == 'approve':
                ok, message = await cog.approve_application(
                    guild, app_id, decider, decider_label='the web console'
                )
            else:
                ok, message = await cog.reject_application(
                    guild, app_id, decider, note, decider_label='the web console'
                )
            return jsonify({'success': bool(ok), 'message': message,
                            'error': None if ok else message})
        except Exception as e:
            logger.error(f"Error deciding onboarding application: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)})
    # A decision drives a dozen Discord calls, well past the default cap.
    return _onboarding_async(_decide(), timeout=60)


# ---------------------------------------------------------------------------
# Media flag / review management
# ---------------------------------------------------------------------------

@app.route('/api/media-flags/settings', methods=['GET', 'POST'])
def mf2_settings():
    if request.method == 'GET':
        async def _get():
            try:
                s = await filter_db.get_media_flag_settings()
                if s.get('log_channel_id') is not None:
                    s['log_channel_id'] = str(s['log_channel_id'])  # snowflake -> string (JS precision)
                return jsonify({'success': True, 'settings': s})
            except Exception as e:
                logger.error(f"Error getting media flag settings: {e}")
                return jsonify({'success': False, 'error': str(e)})
        return run_async(_get())

    async def _set():
        try:
            data = request.json or {}
            if 'enabled' in data:
                await filter_db.set_media_flag_enabled(bool(data.get('enabled')))
            if 'log_channel_id' in data:
                raw = data.get('log_channel_id')
                cid = int(raw) if raw not in (None, '', 'none') else None
                await filter_db.set_media_flag_log_channel(cid)
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error updating media flag settings: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_set())


def _mf2_list_roles(getter):
    async def _l():
        try:
            return jsonify({'success': True, 'role_ids': [str(r) for r in await getter()]})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_l())

def _mf2_role_mutate(fn):
    async def _m():
        try:
            raw = str((request.json or {}).get('role_id') or '').strip()
            if not raw.isdigit():
                return jsonify({'success': False, 'error': 'role_id must be numeric'})
            await fn(int(raw))
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_m())

@app.route('/api/media-flags/excluded-roles')
def mf2_list_excluded():
    return _mf2_list_roles(filter_db.get_media_flag_excluded_roles)

@app.route('/api/media-flags/excluded-roles/add', methods=['POST'])
def mf2_add_excluded():
    return _mf2_role_mutate(lambda rid: filter_db.add_media_flag_excluded_role(rid, added_by='web-console'))

@app.route('/api/media-flags/excluded-roles/remove', methods=['POST'])
def mf2_remove_excluded():
    return _mf2_role_mutate(filter_db.remove_media_flag_excluded_role)

@app.route('/api/media-flags/ignored-roles')
def mf2_list_ignored():
    return _mf2_list_roles(filter_db.get_media_flag_ignored_roles)

@app.route('/api/media-flags/ignored-roles/add', methods=['POST'])
def mf2_add_ignored():
    return _mf2_role_mutate(lambda rid: filter_db.add_media_flag_ignored_role(rid, added_by='web-console'))

@app.route('/api/media-flags/ignored-roles/remove', methods=['POST'])
def mf2_remove_ignored():
    return _mf2_role_mutate(filter_db.remove_media_flag_ignored_role)

@app.route('/api/media-flags/optout-users')
def mf2_list_optouts():
    """Members exempt from media reposting, with a display name resolved from the
    bot's cache where it knows them (the row itself only stores the id)."""
    async def _l():
        try:
            rows = await filter_db.get_media_flag_optout_users()
            items = []
            for r in rows:
                uid = int(r['user_id'])
                name = None
                try:
                    if bot_instance:
                        user = bot_instance.get_user(uid)
                        name = (user.display_name or user.name) if user else None
                except Exception:
                    name = None
                items.append({'user_id': str(uid), 'name': name,
                              'added_by': r.get('added_by'), 'added_at': r.get('added_at')})
            return jsonify({'success': True, 'users': items})
        except Exception as e:
            logger.error(f"Error getting media flag opt-out users: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_l())


def _mf2_user_mutate(fn):
    async def _m():
        try:
            raw = str((request.json or {}).get('user_id') or '').strip()
            if not raw.isdigit():
                return jsonify({'success': False, 'error': 'user_id must be numeric'})
            await fn(int(raw))
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_m())


@app.route('/api/media-flags/optout-users/add', methods=['POST'])
def mf2_add_optout():
    return _mf2_user_mutate(lambda uid: filter_db.add_media_flag_optout_user(uid, added_by='web-console'))


@app.route('/api/media-flags/optout-users/remove', methods=['POST'])
def mf2_remove_optout():
    return _mf2_user_mutate(filter_db.remove_media_flag_optout_user)


@app.route('/api/media-flags/logs')
def mf2_logs():
    async def _l():
        try:
            items = await filter_db.get_media_flag_logs(limit=100)
            for it in items:
                for k in ('source_channel_id', 'source_message_id', 'repost_channel_id',
                          'repost_message_id', 'author_id', 'report_message_id'):
                    if it.get(k) is not None:
                        it[k] = str(it[k])  # snowflake -> string (JS number precision)
            return jsonify({'success': True, 'items': items})
        except Exception as e:
            logger.error(f"Error getting media flag logs: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_l())



# =============================================================================
# Hikaru Bot (VN Completion Tracker) API Endpoints
# =============================================================================

def get_hikaru_db_path():
    """Get the Hikaru bot database path from config"""
    try:
        config_path = Path(__file__).parent / 'config.json'
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                external_dbs = config.get('external_databases', {})
                hikaru_config = external_dbs.get('hikaru', {})
                if hikaru_config.get('enabled', False):
                    db_path = hikaru_config.get('db_path', '')
                    if db_path and Path(db_path).exists():
                        return db_path
        return None
    except Exception as e:
        logger.error(f"Error loading Hikaru config: {e}")
        return None

async def _hikaru_table_missing(db, table: str) -> bool:
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ) as cur:
        return await cur.fetchone() is None

def get_hikaru_log_path():
    """Get the Hikaru bot log file path from config"""
    try:
        config_path = Path(__file__).parent / 'config.json'
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                external_dbs = config.get('external_databases', {})
                hikaru_config = external_dbs.get('hikaru', {})
                if hikaru_config.get('enabled', False):
                    log_path = hikaru_config.get('log_path', '')
                    if log_path:
                        return log_path
        return None
    except Exception as e:
        logger.error(f"Error loading Hikaru log config: {e}")
        return None

def get_hikaru_control_config():
    """Read the Hikaru control section. Expected schema in config.json:

        external_databases.hikaru.control = {
            "docker_container": "hikaru",            # tried first (Docker deployments)
            "local_start_command": ["python", "main.py"],  # fallback (running without Docker)
            "local_working_dir": "<path to the hikaru checkout>"
        }

    Returns ``{}`` when missing; callers must handle that as "not configured"
    and surface a config_hint to the user.
    """
    try:
        config_path = Path(__file__).parent / 'config.json'
        if not config_path.exists():
            return {}
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        hikaru_cfg = cfg.get('external_databases', {}).get('hikaru', {})
        if not hikaru_cfg.get('enabled', False):
            return {}
        return hikaru_cfg.get('control', {}) or {}
    except Exception as e:
        logger.error(f"Error loading Hikaru control config: {e}")
        return {}

def _hikaru_docker_available(container_name):
    """True iff `docker` is on PATH and the named container exists. Tried
    first by every control endpoint so production (Docker on Linux) uses
    the container and dev (no Docker) falls through to the local-process path.
    """
    if not container_name or not shutil.which('docker'):
        return False
    try:
        # `docker inspect` returns 0 if the container exists (running OR stopped),
        # nonzero otherwise. Its output is not needed.
        result = subprocess.run(
            ['docker', 'inspect', '--type=container', container_name],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug(f"docker inspect probe failed: {e}")
        return False

def _hikaru_docker_running(container_name):
    """True iff the named container is in 'running' state. Used by the
    status endpoint so the toggle UI can decide between Stop/Start labels.
    """
    if not container_name or not shutil.which('docker'):
        return False
    try:
        result = subprocess.run(
            ['docker', 'inspect', '-f', '{{.State.Running}}', container_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == 'true'
    except (subprocess.SubprocessError, OSError):
        return False

def _find_local_hikaru_processes(start_command, working_dir):
    """Find python processes whose cmdline matches the configured hikaru
    start command AND whose cwd is the configured working_dir. Used on
    setups without Docker to stop a running hikaru.

    Matching by both cmdline AND cwd avoids accidentally killing an
    unrelated `python main.py` running in some other project tree.
    """
    if not start_command or not working_dir:
        return []
    try:
        target_dir = str(Path(working_dir).resolve()).lower()
    except Exception:
        return []
    cmd_tokens = start_command if isinstance(start_command, list) else [start_command]
    cmd_marker = cmd_tokens[-1] if cmd_tokens else ''  # usually "main.py"
    matches = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
        try:
            info = proc.info
            cmdline = info.get('cmdline') or []
            if not any(cmd_marker in (str(t) or '') for t in cmdline):
                continue
            cwd = info.get('cwd') or ''
            if not cwd:
                continue
            if str(Path(cwd).resolve()).lower() != target_dir:
                continue
            matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return matches

def _spawn_local_hikaru(start_command, working_dir):
    """Spawn a detached hikaru process so it survives muramasa restarts.

    Windows: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` runs hikaru
    with no console window at all (silent restart matches muramasa's UX
    when restarted from the web console: no popup terminal). Stdout /
    stderr go to DEVNULL since the new process has no console; runtime
    output is observed via hikaru's configured log file (which the
    console's Hikaru Bot logs tab already streams).

    Linux: ``start_new_session=True`` puts hikaru in its own process
    group so it's not tied to the muramasa web console's lifetime.
    Linux production should normally hit the docker_container path
    instead of this fallback.
    """
    if not start_command or not working_dir:
        raise ValueError("start_command and working_dir required")
    # The start command comes from config.json, which the console can edit, so
    # running it requires an explicit env opt-in. Production uses the Docker
    # control path and never reaches this branch.
    if os.getenv("ALLOW_LOCAL_HIKARU_SPAWN", "").strip().lower() not in ("1", "true", "yes"):
        raise ValueError(
            "Local hikaru spawn is disabled. Set ALLOW_LOCAL_HIKARU_SPAWN=1 to allow "
            "running a config-defined start command (the Docker control path does not need this)."
        )
    cmd = start_command if isinstance(start_command, list) else [start_command]
    cwd = str(Path(working_dir).resolve())
    if platform.system() == "Windows":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            cmd, cwd=cwd,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    return subprocess.Popen(
        cmd, cwd=cwd,
        start_new_session=True,
    )

def _parse_log_lines(lines):
    """Parse ' - ' formatted log lines into [{timestamp, level, message}],
    carrying the last timestamp/level onto continuation lines (tracebacks, etc.)."""
    logs = []
    last_timestamp = None
    last_level = 'INFO'
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Format: "timestamp - name - level - message" (or 3-part without name)
        try:
            parts = line.split(' - ', 3)
            if len(parts) >= 4:
                timestamp = parts[0].split(',')[0]  # drop milliseconds
                level = parts[2]
                message = parts[3]
                last_timestamp, last_level = timestamp, level
            elif len(parts) >= 3:
                timestamp = parts[0].split(',')[0]
                level = parts[1]
                message = parts[2]
                last_timestamp, last_level = timestamp, level
            else:
                timestamp = last_timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                level = last_level
                message = line
            logs.append({'timestamp': timestamp, 'level': level, 'message': message})
        except Exception:
            logs.append({
                'timestamp': last_timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'level': last_level,
                'message': line,
            })
    return logs


def _bot_log_response(log_path, bot_label, config_key):
    """Shared JSON response for a flat bot log file (hikaru, ichijou)."""
    limit = request.args.get('limit', 100, type=int)
    if not log_path:
        return jsonify({
            'success': False,
            'error': f'{bot_label} log path not configured',
            'config_hint': f'Set {config_key} in config.json',
        })
    # The path is config-controlled, so only .log files are read; token, env,
    # config and database files never reach the response.
    if Path(log_path).suffix.lower() != '.log':
        return jsonify({
            'success': False,
            'error': 'Refusing to read a non-.log file path',
        })
    if not Path(log_path).exists():
        return jsonify({
            'success': False,
            'error': f'Log file not found: {log_path}',
            'config_hint': f'Make sure the {bot_label} bot has been started at least once to create the log file',
        })
    with open(log_path, 'r', encoding='utf-8') as f:
        lines = deque(f, maxlen=limit)  # read last N lines efficiently
    logs = _parse_log_lines(lines)
    return jsonify({'success': True, 'logs': logs, 'log_path': log_path, 'count': len(logs)})


@app.route('/api/vnclub/logs')
def get_hikaru_logs():
    """Get Hikaru bot logs from log file"""
    try:
        return _bot_log_response(get_hikaru_log_path(), 'Hikaru', 'external_databases.hikaru.log_path')
    except Exception as e:
        logger.error(f"Error reading Hikaru logs: {e}")
        return jsonify({'success': False, 'error': str(e)})


def get_ichijou_log_path():
    """Get the Ichijou (vnclub) bot log file path from config"""
    try:
        config_path = Path(__file__).parent / 'config.json'
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                ichijou_config = config.get('external_databases', {}).get('ichijou', {})
                if ichijou_config.get('enabled', False):
                    return ichijou_config.get('log_path', '') or None
        return None
    except Exception as e:
        logger.error(f"Error loading Ichijou log config: {e}")
        return None


@app.route('/api/ichijou/logs')
def get_ichijou_logs():
    """Get Ichijou (vnclub) bot logs from log file"""
    try:
        return _bot_log_response(get_ichijou_log_path(), 'Ichijou', 'external_databases.ichijou.log_path')
    except Exception as e:
        logger.error(f"Error reading Ichijou logs: {e}")
        return jsonify({'success': False, 'error': str(e)})

# ----- Hikaru process control (Restart / Stop / Start) -----
# Two backends in priority order:
#   1. Docker: `docker_container` from config.
#   2. Local subprocess: `local_start_command` + `local_working_dir`;
#      fallback for setups where the operator runs hikaru in a separate
#      console.

def _hikaru_control_unconfigured():
    return jsonify({
        'success': False,
        'error': 'Hikaru control not configured',
        'config_hint': (
            "Add a 'control' section under external_databases.hikaru in "
            "config.json with either 'docker_container' (Docker deployments) or "
            "'local_start_command' + 'local_working_dir' (running without Docker)."
        ),
    })

@app.route('/api/hikaru/control/status')
def hikaru_control_status():
    """Report whether hikaru is currently running. Drives the Stop/Start
    toggle button label in the UI."""
    cfg = get_hikaru_control_config()
    if not cfg:
        return _hikaru_control_unconfigured()
    container = cfg.get('docker_container')
    if _hikaru_docker_available(container):
        running = _hikaru_docker_running(container)
        return jsonify({
            'success': True,
            'running': running,
            'mode': 'docker',
            'container': container,
        })
    # Fall through to local-process detection
    procs = _find_local_hikaru_processes(
        cfg.get('local_start_command'), cfg.get('local_working_dir'),
    )
    return jsonify({
        'success': True,
        'running': len(procs) > 0,
        'mode': 'local',
        'pids': [p.pid for p in procs],
    })

@app.route('/api/hikaru/control/stop', methods=['POST'])
def hikaru_control_stop():
    """Stop hikaru. Docker path: `docker stop <container>`. Local path:
    psutil terminate with kill-fallback after 5s grace period."""
    cfg = get_hikaru_control_config()
    if not cfg:
        return _hikaru_control_unconfigured()
    container = cfg.get('docker_container')
    if _hikaru_docker_available(container):
        try:
            subprocess.run(['docker', 'stop', container], check=True, timeout=30)
            return jsonify({'success': True, 'mode': 'docker',
                            'message': f'Stopped container {container}'})
        except (subprocess.SubprocessError, OSError) as e:
            return jsonify({'success': False, 'mode': 'docker',
                            'error': f'docker stop failed: {e}'})
    # Local fallback
    procs = _find_local_hikaru_processes(
        cfg.get('local_start_command'), cfg.get('local_working_dir'),
    )
    if not procs:
        return jsonify({'success': False, 'mode': 'local',
                        'error': 'No matching hikaru process found.'})
    stopped = []
    for proc in procs:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
            stopped.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning(f"Failed to stop pid {proc.pid}: {e}")
    return jsonify({'success': True, 'mode': 'local', 'stopped_pids': stopped})

@app.route('/api/hikaru/control/start', methods=['POST'])
def hikaru_control_start():
    """Start hikaru. Docker path: `docker start <container>`. Local path:
    spawn the configured command in a new detached process / console."""
    cfg = get_hikaru_control_config()
    if not cfg:
        return _hikaru_control_unconfigured()
    container = cfg.get('docker_container')
    if _hikaru_docker_available(container):
        try:
            subprocess.run(['docker', 'start', container], check=True, timeout=30)
            return jsonify({'success': True, 'mode': 'docker',
                            'message': f'Started container {container}'})
        except (subprocess.SubprocessError, OSError) as e:
            return jsonify({'success': False, 'mode': 'docker',
                            'error': f'docker start failed: {e}'})
    # Local fallback: refuse if already running so duplicates are not spawned.
    existing = _find_local_hikaru_processes(
        cfg.get('local_start_command'), cfg.get('local_working_dir'),
    )
    if existing:
        return jsonify({'success': False, 'mode': 'local',
                        'error': f'Hikaru already running (pid {existing[0].pid}).'})
    try:
        proc = _spawn_local_hikaru(
            cfg.get('local_start_command'), cfg.get('local_working_dir'),
        )
        return jsonify({'success': True, 'mode': 'local', 'pid': proc.pid})
    except (ValueError, OSError, FileNotFoundError) as e:
        return jsonify({'success': False, 'mode': 'local',
                        'error': f'Failed to spawn hikaru: {e}'})

@app.route('/api/hikaru/control/restart', methods=['POST'])
def hikaru_control_restart():
    """Restart hikaru. Docker: `docker restart`. Local: stop + spawn."""
    cfg = get_hikaru_control_config()
    if not cfg:
        return _hikaru_control_unconfigured()
    container = cfg.get('docker_container')
    if _hikaru_docker_available(container):
        try:
            subprocess.run(['docker', 'restart', container], check=True, timeout=60)
            return jsonify({'success': True, 'mode': 'docker',
                            'message': f'Restarted container {container}'})
        except (subprocess.SubprocessError, OSError) as e:
            return jsonify({'success': False, 'mode': 'docker',
                            'error': f'docker restart failed: {e}'})
    # Local fallback: terminate any existing process, then spawn anew.
    procs = _find_local_hikaru_processes(
        cfg.get('local_start_command'), cfg.get('local_working_dir'),
    )
    for proc in procs:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        proc = _spawn_local_hikaru(
            cfg.get('local_start_command'), cfg.get('local_working_dir'),
        )
        return jsonify({'success': True, 'mode': 'local', 'pid': proc.pid})
    except (ValueError, OSError, FileNotFoundError) as e:
        return jsonify({'success': False, 'mode': 'local',
                        'error': f'Failed to spawn hikaru: {e}'})

@app.route('/api/vnclub/status')
def get_vnclub_status():
    """Check Hikaru database connection status and get stats"""
    async def _get_vnclub_status():
        try:
            db_path = get_hikaru_db_path()

            if not db_path:
                # Check if configured but path invalid
                try:
                    config_path = Path(__file__).parent / 'config.json'
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                        hikaru_config = config.get('external_databases', {}).get('hikaru', {})
                        if not hikaru_config.get('enabled', False):
                            return jsonify({
                                'success': False,
                                'connected': False,
                                'error': 'Hikaru database not enabled in config.json',
                                'config_hint': 'Set external_databases.hikaru.enabled to true and provide a valid db_path'
                            })
                        elif not hikaru_config.get('db_path'):
                            return jsonify({
                                'success': False,
                                'connected': False,
                                'error': 'Hikaru database path not configured',
                                'config_hint': 'Set external_databases.hikaru.db_path to the database file path'
                            })
                        else:
                            return jsonify({
                                'success': False,
                                'connected': False,
                                'error': f"Database file not found: {hikaru_config.get('db_path')}",
                                'config_hint': 'Check that the path is correct and the file exists'
                            })
                except:
                    return jsonify({
                        'success': False,
                        'connected': False,
                        'error': 'Hikaru database not configured'
                    })

            async with aiosqlite.connect(db_path) as db:
                # Get stats
                stats = {}

                # Total reading logs
                async with db.execute("SELECT COUNT(*) FROM reading_logs") as cursor:
                    stats['total_logs'] = (await cursor.fetchone())[0]

                # Unique users
                async with db.execute("SELECT COUNT(DISTINCT user_id) FROM reading_logs") as cursor:
                    stats['unique_users'] = (await cursor.fetchone())[0]

                # Total points awarded
                async with db.execute("SELECT COALESCE(SUM(points), 0) FROM reading_logs") as cursor:
                    stats['total_points'] = (await cursor.fetchone())[0]

                # Monthly VN titles count
                try:
                    async with db.execute("SELECT COUNT(*) FROM vn_titles") as cursor:
                        stats['vn_titles'] = (await cursor.fetchone())[0]
                except:
                    stats['vn_titles'] = 0

                # Get distinct servers
                async with db.execute("SELECT COUNT(DISTINCT logged_in_guild) FROM reading_logs WHERE logged_in_guild IS NOT NULL") as cursor:
                    stats['servers'] = (await cursor.fetchone())[0]

                return jsonify({
                    'success': True,
                    'connected': True,
                    'db_path': db_path,
                    'stats': stats
                })
        except Exception as e:
            return jsonify({
                'success': False,
                'connected': False,
                'error': str(e)
            })
    return run_async(_get_vnclub_status())

# Hikaru stores each rating as typed plus the scale it was typed on. Rows
# without the column (a database hikaru has not migrated yet) are out of 5.
HIKARU_RATING_SCALES = (5, 10, 100)
HIKARU_LEGACY_RATING_SCALE = 5
# Hikaru's scale for members with no saved preference.
HIKARU_DEFAULT_RATING_SCALE = 10


async def _hikaru_has_rating_scale(db) -> bool:
    async with db.execute("PRAGMA table_info(reading_logs)") as cursor:
        return any(row[1] == 'rating_scale' for row in await cursor.fetchall())


def _parse_hikaru_whole_number(value, field):
    """An optional whole number sent as a JSON number or a digit string."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError(f'{field} must be a whole number')
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f'{field} must be a whole number')


def _check_hikaru_rating(rating, scale):
    if scale not in HIKARU_RATING_SCALES:
        raise ValueError('rating_scale must be 5, 10 or 100')
    if rating is not None and not 1 <= rating <= scale:
        raise ValueError(f'user_rating must be between 1 and {scale} on the {scale}-point scale')


# A VN id as typed: "v" plus digits, bare digits, or a vndb.org page URL for that VN.
_HIKARU_VNDB_ID_RE = re.compile(
    r"https?://(?:www\.)?vndb\.org/[vV]([1-9][0-9]{0,8})(?:[/?#].*)?"
    r"|[vV]?([1-9][0-9]{0,8})"
)


def _parse_hikaru_vndb_id(value):
    """An optional VN id in the "v<digits>" form hikaru's lookups expect."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == '':
        return None
    if isinstance(value, str):
        match = _HIKARU_VNDB_ID_RE.fullmatch(value)
        if match:
            return 'v' + (match.group(1) or match.group(2))
    raise ValueError('vndb_id must be a VNDB VN id like v17, or its vndb.org URL')


async def _hikaru_member_scale(db, user_id):
    """The member's saved scale, else hikaru's default."""
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_settings'") as cursor:
        if not await cursor.fetchone():
            return HIKARU_DEFAULT_RATING_SCALE
    async with db.execute("SELECT rating_scale FROM user_settings WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
    if row and row[0] in HIKARU_RATING_SCALES:
        return row[0]
    return HIKARU_DEFAULT_RATING_SCALE


@app.route('/api/vnclub/completions')
def get_vnclub_completions():
    """Get Hikaru reading logs with filtering and pagination"""
    async def _get_completions():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            # Get parameters
            page = request.args.get('page', 1, type=int)
            limit = request.args.get('limit', 50, type=int)
            search = request.args.get('search', '').strip()
            user_filter = request.args.get('user_filter', '').strip()
            month_filter = request.args.get('month_filter', '').strip()
            reason_filter = request.args.get('reason_filter', '').strip()
            guild_filter = request.args.get('guild_filter', '').strip()

            offset = (page - 1) * limit

            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row

                # Detect optional `completed_at` column (added by hikaru's
                # day-precision migration). Older DBs don't have it; those
                # rows surface NULL so the UI can fall back to
                # `reward_month` for display.
                async with db.execute("PRAGMA table_info(reading_logs)") as cursor:
                    log_cols = {row['name'] for row in await cursor.fetchall()}
                completed_at_select = ", completed_at" if 'completed_at' in log_cols else ", NULL AS completed_at"
                has_rating_scale = 'rating_scale' in log_cols
                rating_scale_select = (", rating_scale" if has_rating_scale
                                       else f", {HIKARU_LEGACY_RATING_SCALE} AS rating_scale")

                # Build query
                base_query = f"""
                    SELECT
                        log_id,
                        user_id,
                        vndb_id,
                        user_rating,
                        reward_reason,
                        reward_month,
                        points,
                        comment,
                        logged_in_guild
                        {completed_at_select}
                        {rating_scale_select}
                    FROM reading_logs
                """

                conditions = []
                params = []

                if search:
                    conditions.append("(vndb_id LIKE ? OR comment LIKE ?)")
                    params.extend([f"%{search}%", f"%{search}%"])

                if user_filter:
                    conditions.append("CAST(user_id AS TEXT) LIKE ?")
                    params.append(f"%{user_filter}%")

                if month_filter:
                    conditions.append("reward_month = ?")
                    params.append(month_filter)

                if reason_filter:
                    conditions.append("reward_reason = ?")
                    params.append(reason_filter)

                if guild_filter:
                    conditions.append("CAST(logged_in_guild AS TEXT) = ?")
                    params.append(guild_filter)

                where_clause = ""
                if conditions:
                    where_clause = " WHERE " + " AND ".join(conditions)

                # Get total count
                count_query = f"SELECT COUNT(*) FROM reading_logs{where_clause}"
                async with db.execute(count_query, params) as cursor:
                    total = (await cursor.fetchone())[0]

                # Get paginated results
                query = f"{base_query}{where_clause} ORDER BY reward_month DESC, log_id DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])

                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    logs = [safe_json_row(dict(row)) for row in rows]

                # Resolve usernames - cache first, trigger background fetch for uncached
                user_ids = list(set(log.get('user_id') for log in logs if log.get('user_id')))
                username_map = {}
                uncached_ids = []

                if user_ids and bot_instance and not bot_instance.is_closed():
                    try:
                        for uid in user_ids:
                            uid_int = int(uid) if isinstance(uid, str) else uid
                            # Check the custom cache first
                            with username_cache_lock:
                                if uid_int in username_cache:
                                    cached = username_cache[uid_int]
                                    if cached and cached not in ("NOT_FOUND", "FORBIDDEN"):
                                        username_map[uid_int] = cached
                                        continue
                                    elif cached == "NOT_FOUND":
                                        username_map[uid_int] = 'Deleted User'
                                        continue
                                    elif cached == "FORBIDDEN":
                                        username_map[uid_int] = 'Private User'
                                        continue
                            # Check discord.py's internal cache
                            user = bot_instance.get_user(uid_int)
                            if user:
                                username = getattr(user, 'global_name', None) or user.name
                                username_map[uid_int] = username
                                with username_cache_lock:
                                    username_cache[uid_int] = username
                            else:
                                uncached_ids.append(uid_int)

                        # Apply usernames to logs
                        for log in logs:
                            user_id = log.get('user_id')
                            if user_id:
                                uid_int = int(user_id) if isinstance(user_id, str) else user_id
                                log['username'] = username_map.get(uid_int, f'User {user_id}')

                        # Trigger background fetch for uncached users (non-blocking)
                        if uncached_ids and hasattr(bot_instance, 'loop') and bot_instance.loop:
                            asyncio.run_coroutine_threadsafe(
                                fetch_usernames_batch(uncached_ids),
                                bot_instance.loop
                            )
                    except Exception as e:
                        logger.debug(f"Could not resolve usernames: {e}")
                        for log in logs:
                            log['username'] = f"User {log.get('user_id', 'Unknown')}"
                else:
                    for log in logs:
                        log['username'] = f"User {log.get('user_id', 'Unknown')}"

                # Get distinct guilds for filter dropdown (convert to strings to avoid JS precision loss)
                async with db.execute("SELECT DISTINCT logged_in_guild FROM reading_logs WHERE logged_in_guild IS NOT NULL ORDER BY logged_in_guild") as cursor:
                    guilds = [str(row[0]) for row in await cursor.fetchall()]

                # Get distinct months for filter
                async with db.execute("SELECT DISTINCT reward_month FROM reading_logs ORDER BY reward_month DESC") as cursor:
                    months = [row[0] for row in await cursor.fetchall()]

                return jsonify({
                    'success': True,
                    'logs': logs,
                    'total': total,
                    'page': page,
                    'limit': limit,
                    'guilds': guilds,
                    'months': months,
                    'rating_scales': list(HIKARU_RATING_SCALES) if has_rating_scale
                    else [HIKARU_LEGACY_RATING_SCALE],
                })
        except Exception as e:
            logger.error(f"Error fetching Hikaru completions: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_completions())

@app.route('/api/vnclub/usernames', methods=['GET'])
def get_vnclub_usernames():
    """Get usernames for specific user IDs (lightweight endpoint for polling)"""
    async def _get_usernames():
        try:
            user_ids_param = request.args.get('ids', '')
            if not user_ids_param:
                return jsonify({'success': True, 'usernames': {}})

            user_ids = [uid.strip() for uid in user_ids_param.split(',') if uid.strip()]
            if not user_ids:
                return jsonify({'success': True, 'usernames': {}})

            username_map = {}

            if bot_instance and not bot_instance.is_closed():
                for uid in user_ids:
                    try:
                        uid_int = int(uid)
                        # Check the custom cache
                        with username_cache_lock:
                            if uid_int in username_cache:
                                cached = username_cache[uid_int]
                                if cached and cached not in ("NOT_FOUND", "FORBIDDEN"):
                                    username_map[uid] = cached
                                elif cached == "NOT_FOUND":
                                    username_map[uid] = 'Deleted User'
                                elif cached == "FORBIDDEN":
                                    username_map[uid] = 'Private User'
                                continue
                        # Check discord.py's internal cache
                        user = bot_instance.get_user(uid_int)
                        if user:
                            username = getattr(user, 'global_name', None) or user.name
                            username_map[uid] = username
                            with username_cache_lock:
                                username_cache[uid_int] = username
                    except (ValueError, TypeError):
                        pass

            return jsonify({'success': True, 'usernames': username_map})
        except Exception as e:
            logger.error(f"Error fetching usernames: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_usernames())

@app.route('/api/vnclub/completions/add', methods=['POST'])
def add_vnclub_completion():
    """Add a new Hikaru reading log entry"""
    async def _add_completion():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.json

            # Required fields
            user_id = data.get('user_id')
            reward_reason = data.get('reward_reason')
            reward_month = data.get('reward_month')
            points = data.get('points')

            if not all([user_id, reward_reason, reward_month, points is not None]):
                return jsonify({'success': False, 'error': 'Missing required fields: user_id, reward_reason, reward_month, points'})

            # Optional fields
            comment = data.get('comment') or None
            logged_in_guild = data.get('logged_in_guild') or None
            try:
                vndb_id = _parse_hikaru_vndb_id(data.get('vndb_id'))
                user_rating = _parse_hikaru_whole_number(data.get('user_rating'), 'user_rating')
                rating_scale = _parse_hikaru_whole_number(data.get('rating_scale'), 'rating_scale')
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400

            async with aiosqlite.connect(db_path) as db:
                has_rating_scale = await _hikaru_has_rating_scale(db)
                if not has_rating_scale:
                    if rating_scale not in (None, HIKARU_LEGACY_RATING_SCALE):
                        return jsonify({'success': False, 'error': "Hikaru's database has no rating scales yet; ratings are out of 5."}), 400
                    rating_scale = HIKARU_LEGACY_RATING_SCALE
                elif rating_scale is None:
                    rating_scale = await _hikaru_member_scale(db, user_id)
                try:
                    _check_hikaru_rating(user_rating, rating_scale)
                except ValueError as e:
                    return jsonify({'success': False, 'error': str(e)}), 400

                if has_rating_scale:
                    await db.execute("""
                        INSERT INTO reading_logs (user_id, vndb_id, user_rating, rating_scale, reward_reason, reward_month, points, comment, logged_in_guild)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (user_id, vndb_id, user_rating, rating_scale, reward_reason, reward_month, points, comment, logged_in_guild))
                else:
                    await db.execute("""
                        INSERT INTO reading_logs (user_id, vndb_id, user_rating, reward_reason, reward_month, points, comment, logged_in_guild)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (user_id, vndb_id, user_rating, reward_reason, reward_month, points, comment, logged_in_guild))
                await db.commit()

                return jsonify({
                    'success': True,
                    'message': 'Completion log added successfully'
                })
        except Exception as e:
            logger.error(f"Error adding Hikaru completion: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add_completion())

@app.route('/api/vnclub/completions/edit', methods=['POST'])
def edit_vnclub_completion():
    """Edit an existing Hikaru reading log entry"""
    async def _edit_completion():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.json
            log_id = data.get('log_id')

            if not log_id:
                return jsonify({'success': False, 'error': 'log_id is required'})

            # Build update query from provided fields
            updates = {}
            allowed_fields = ['vndb_id', 'user_rating', 'rating_scale', 'reward_reason', 'reward_month', 'points', 'comment', 'logged_in_guild']

            for field in allowed_fields:
                if field in data:
                    updates[field] = data[field] if data[field] != '' else None

            try:
                if 'vndb_id' in updates:
                    updates['vndb_id'] = _parse_hikaru_vndb_id(updates['vndb_id'])
                if 'user_rating' in updates:
                    updates['user_rating'] = _parse_hikaru_whole_number(updates['user_rating'], 'user_rating')
                if 'rating_scale' in updates:
                    updates['rating_scale'] = _parse_hikaru_whole_number(updates['rating_scale'], 'rating_scale')
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            # The column is NOT NULL, so a blank scale means "keep the stored one".
            if 'rating_scale' in updates and updates['rating_scale'] is None:
                del updates['rating_scale']

            if not updates:
                return jsonify({'success': False, 'error': 'No fields to update'})

            async with aiosqlite.connect(db_path) as db:
                if 'user_rating' in updates or 'rating_scale' in updates:
                    has_rating_scale = await _hikaru_has_rating_scale(db)
                    if not has_rating_scale and 'rating_scale' in updates:
                        if updates.pop('rating_scale') != HIKARU_LEGACY_RATING_SCALE:
                            return jsonify({'success': False, 'error': "Hikaru's database has no rating scales yet; ratings are out of 5."}), 400
                        if not updates:
                            return jsonify({'success': False, 'error': 'No fields to update'})
                    scale_select = "rating_scale" if has_rating_scale else str(HIKARU_LEGACY_RATING_SCALE)
                    async with db.execute(
                        f"SELECT user_rating, {scale_select} FROM reading_logs WHERE log_id = ?", (log_id,)
                    ) as cursor:
                        stored = await cursor.fetchone()
                    if stored is None:
                        return jsonify({'success': False, 'error': 'Log entry not found'})
                    # Validated as the pair the row will hold after the update.
                    final_rating = updates['user_rating'] if 'user_rating' in updates else stored[0]
                    final_scale = updates['rating_scale'] if 'rating_scale' in updates else stored[1]
                    try:
                        _check_hikaru_rating(final_rating, final_scale)
                    except ValueError as e:
                        return jsonify({'success': False, 'error': str(e)}), 400

                set_clauses = [f"{field} = ?" for field in updates.keys()]
                params = list(updates.values()) + [log_id]

                query = f"UPDATE reading_logs SET {', '.join(set_clauses)} WHERE log_id = ?"
                await db.execute(query, params)
                await db.commit()

                if db.total_changes == 0:
                    return jsonify({'success': False, 'error': 'Log entry not found'})

                return jsonify({
                    'success': True,
                    'message': 'Completion log updated successfully'
                })
        except Exception as e:
            logger.error(f"Error editing Hikaru completion: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_edit_completion())

@app.route('/api/vnclub/completions/delete', methods=['POST'])
def delete_vnclub_completion():
    """Delete a Hikaru reading log entry"""
    async def _delete_completion():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.json
            log_id = data.get('log_id')

            if not log_id:
                return jsonify({'success': False, 'error': 'log_id is required'})

            async with aiosqlite.connect(db_path) as db:
                await db.execute("DELETE FROM reading_logs WHERE log_id = ?", (log_id,))
                await db.commit()

                if db.total_changes == 0:
                    return jsonify({'success': False, 'error': 'Log entry not found'})

                return jsonify({
                    'success': True,
                    'message': 'Completion log deleted successfully'
                })
        except Exception as e:
            logger.error(f"Error deleting Hikaru completion: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_delete_completion())

# -----------------------------------------------------------------------------
# Hikaru account transfer (see utils/hikaru_user_transfer.py for the rules).
# The database work runs in the request thread, not on the bot loop, so a slow
# request can never be abandoned by run_async's timeout while it commits.
# -----------------------------------------------------------------------------

HIKARU_TRANSFER_SNAPSHOT_DIR = Path(__file__).parent / 'data' / 'backups' / 'hikaru_transfers'


def _hikaru_transfer_db():
    db_path = get_hikaru_db_path()
    if not db_path:
        raise hikaru_user_transfer.TransferError('Hikaru database not configured or not found.')
    return db_path


def _hikaru_transfer_body():
    # get_json without force: only a real application/json request is accepted.
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise hikaru_user_transfer.TransferError('Expected a JSON body.')
    return data


async def _lookup_transfer_account(user_id):
    if not bot_instance or bot_instance.is_closed():
        return {'status': 'unavailable'}
    user = bot_instance.get_user(user_id)
    if user is None:
        try:
            user = await bot_instance.fetch_user(user_id)
        except discord.NotFound:
            return {'status': 'not_found'}
        except Exception:
            return {'status': 'unavailable'}
    return {
        'status': 'ok',
        'name': getattr(user, 'global_name', None) or user.name,
        'handle': user.name,
        'bot': bool(user.bot),
    }


def _check_transfer_accounts(src, dst):
    """Resolve both ids on Discord. The target must be a real, non-bot account,
    so a mistyped id can never receive someone's records."""
    async def _both():
        return await asyncio.gather(_lookup_transfer_account(src), _lookup_transfer_account(dst))

    try:
        source, target = run_async(_both(), timeout=20)
    except Exception:
        logger.exception("Hikaru transfer account lookup failed")
        raise hikaru_user_transfer.TransferError(
            'Could not reach Discord to confirm the accounts. Nothing was changed.')
    if target['status'] == 'not_found':
        raise hikaru_user_transfer.TransferError('The target id is not a Discord account. Check it for typos.')
    if target['status'] != 'ok':
        raise hikaru_user_transfer.TransferError(
            'Could not confirm the target account with Discord right now. Nothing was changed.')
    if target['bot']:
        raise hikaru_user_transfer.TransferError('The target id belongs to a bot.')
    if source.get('bot'):
        raise hikaru_user_transfer.TransferError('The source id belongs to a bot.')
    return {'source': source, 'target': target}


def _transfer_ids(data):
    src = hikaru_user_transfer.parse_account_id(data.get('source_id'))
    dst = hikaru_user_transfer.parse_account_id(data.get('target_id'))
    if src == dst:
        raise hikaru_user_transfer.TransferError('The two account ids are the same.')
    return src, dst


def _transfer_id(data):
    value = data.get('transfer_id')
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise hikaru_user_transfer.TransferError('Invalid transfer id.')
    return value


def _transfer_token(data):
    token = data.get('token')
    if not isinstance(token, str) or len(token) != 64:
        raise hikaru_user_transfer.TransferError('Missing preview token. Preview first.')
    return token


def _transfer_response(action, fn, write=False):
    try:
        return jsonify({'success': True, **fn()})
    except hikaru_user_transfer.TransferError as e:
        return jsonify({'success': False, 'error': str(e)})
    except Exception:
        logger.exception("Hikaru transfer %s failed", action)
        if write:
            error = ('Unexpected error. Check Recent transfers before retrying: a transfer '
                     'is either fully applied and listed there, or not applied at all.')
        else:
            error = 'Unexpected error; see the console log. Nothing was changed.'
        return jsonify({'success': False, 'error': error})


@app.route('/api/vnclub/user_transfer/preview', methods=['POST'])
def hikaru_transfer_preview():
    """Show what moving one account's hikaru records to another would do. Writes nothing."""
    def run():
        db_path = _hikaru_transfer_db()
        src, dst = _transfer_ids(_hikaru_transfer_body())
        accounts = _check_transfer_accounts(src, dst)
        plan = hikaru_user_transfer.preview_transfer(db_path, src, dst)
        plan['accounts'] = accounts
        return {'plan': plan}
    return _transfer_response('preview', run)


@app.route('/api/vnclub/user_transfer/apply', methods=['POST'])
def hikaru_transfer_apply():
    """Apply a previewed transfer; refused unless the preview token still matches."""
    def run():
        db_path = _hikaru_transfer_db()
        data = _hikaru_transfer_body()
        src, dst = _transfer_ids(data)
        token = _transfer_token(data)
        _check_transfer_accounts(src, dst)
        return {'result': hikaru_user_transfer.apply_transfer(
            db_path, src, dst, token, HIKARU_TRANSFER_SNAPSHOT_DIR)}
    return _transfer_response('apply', run, write=True)


@app.route('/api/vnclub/user_transfer/history')
def hikaru_transfer_history():
    def run():
        return {'transfers': hikaru_user_transfer.list_transfers(_hikaru_transfer_db())}
    return _transfer_response('history', run)


@app.route('/api/vnclub/user_transfer/undo_preview', methods=['POST'])
def hikaru_transfer_undo_preview():
    def run():
        db_path = _hikaru_transfer_db()
        transfer_id = _transfer_id(_hikaru_transfer_body())
        return {'plan': hikaru_user_transfer.preview_undo(db_path, transfer_id)}
    return _transfer_response('undo preview', run)


@app.route('/api/vnclub/user_transfer/undo', methods=['POST'])
def hikaru_transfer_undo():
    def run():
        db_path = _hikaru_transfer_db()
        data = _hikaru_transfer_body()
        transfer_id = _transfer_id(data)
        token = _transfer_token(data)
        return {'result': hikaru_user_transfer.apply_undo(
            db_path, transfer_id, token, HIKARU_TRANSFER_SNAPSHOT_DIR)}
    return _transfer_response('undo', run, write=True)

# -----------------------------------------------------------------------------
# Hikaru VNDB account links. Hikaru owns the sync: its background loop picks up
# any link whose synced_at is NULL or older than a day, so a refresh here only
# clears synced_at and never calls VNDB itself. The tables come from a hikaru
# migration and may not exist yet on an older database.
# -----------------------------------------------------------------------------

_VNDB_LINK_TABLES = ('vndb_links', 'vndb_ulist')
_VNDB_LINKS_MISSING_NOTE = 'VNDB link tables not present yet (hikaru migration has not run)'


def _vndb_link_user_id():
    """Parse user_id from a JSON body; (user_id, None) or (None, error)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, 'Expected a JSON body.'
    try:
        return hikaru_user_transfer.parse_account_id(data.get('user_id')), None
    except hikaru_user_transfer.TransferError as e:
        return None, str(e)


async def _vndb_link_tables_missing(db) -> bool:
    for table in _VNDB_LINK_TABLES:
        if await _hikaru_table_missing(db, table):
            return True
    return False


@app.route('/api/vnclub/vndb_links')
def get_vnclub_vndb_links():
    """Every linked VNDB account with counts from its cached public list."""
    async def _get():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                if await _vndb_link_tables_missing(db):
                    return jsonify({'success': True, 'available': False, 'links': [],
                                    'guild_names': {}, 'note': _VNDB_LINKS_MISSING_NOTE})
                has_users = not await _hikaru_table_missing(db, 'users')
                name_col = 'u.user_name' if has_users else 'NULL'
                users_join = ('LEFT JOIN users u ON u.discord_user_id = l.user_id '
                              if has_users else '')
                async with db.execute(
                    f"SELECT l.user_id, l.vndb_uid, l.vndb_username, l.linked_in_guild, "
                    f"l.linked_at, l.synced_at, l.sync_error, {name_col} AS user_name, "
                    f"COUNT(e.vndb_id) AS entries, "
                    f"COALESCE(SUM(CASE WHEN e.is_finished THEN 1 ELSE 0 END), 0) AS finished, "
                    f"COUNT(e.vote) AS votes, "
                    f"COUNT(NULLIF(e.notes, '')) AS notes "
                    f"FROM vndb_links l "
                    f"LEFT JOIN vndb_ulist e ON e.user_id = l.user_id "
                    f"{users_join}"
                    f"GROUP BY l.user_id "
                    f"ORDER BY l.linked_at DESC, l.user_id"
                ) as cursor:
                    rows = await cursor.fetchall()
            links = []
            for r in rows:
                link = dict(r)
                # Ids always travel as strings; the browser would round a snowflake.
                link['user_id'] = str(link['user_id'])
                if link['linked_in_guild'] is not None:
                    link['linked_in_guild'] = str(link['linked_in_guild'])
                links.append(link)
            known = get_guild_names()
            guild_ids = {l['linked_in_guild'] for l in links if l['linked_in_guild']}
            return jsonify({
                'success': True,
                'available': True,
                'links': links,
                'guild_names': {g: known[g] for g in guild_ids if g in known},
            })
        except Exception as e:
            logger.error(f"Error fetching VNDB links: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get())


@app.route('/api/vnclub/vndb_links/refresh', methods=['POST'])
def refresh_vnclub_vndb_link():
    """Queue one link for hikaru's next sync pass by clearing synced_at."""
    user_id, error = _vndb_link_user_id()
    if error:
        return jsonify({'success': False, 'error': error}), 400

    async def _refresh():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                if await _vndb_link_tables_missing(db):
                    return jsonify({'success': False, 'error': _VNDB_LINKS_MISSING_NOTE})
                async with db.execute(
                    "UPDATE vndb_links SET synced_at = NULL, sync_error = NULL WHERE user_id = ?",
                    (user_id,),
                ) as cursor:
                    updated = cursor.rowcount > 0
                await db.commit()
            if not updated:
                return jsonify({'success': False, 'error': 'No VNDB link for that user.'}), 404
            logger.info("console queued VNDB list refresh for user=%s", user_id)
            return jsonify({
                'success': True,
                'message': 'Refresh queued. Hikaru will re-sync this list within about 15 minutes.',
            })
        except Exception as e:
            logger.error(f"Error refreshing VNDB link: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_refresh())


@app.route('/api/vnclub/vndb_links/remove', methods=['POST'])
def remove_vnclub_vndb_link():
    """Unlink one account and drop its cached list, as hikaru's own unlink does."""
    user_id, error = _vndb_link_user_id()
    if error:
        return jsonify({'success': False, 'error': error}), 400

    async def _remove():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                if await _vndb_link_tables_missing(db):
                    return jsonify({'success': False, 'error': _VNDB_LINKS_MISSING_NOTE})
                async with db.execute("DELETE FROM vndb_ulist WHERE user_id = ?", (user_id,)) as cursor:
                    entries = cursor.rowcount
                async with db.execute("DELETE FROM vndb_links WHERE user_id = ?", (user_id,)) as cursor:
                    removed = cursor.rowcount > 0
                if not removed:
                    await db.rollback()
                    return jsonify({'success': False, 'error': 'No VNDB link for that user.'}), 404
                await db.commit()
            logger.info("console removed VNDB link for user=%s (%d cached list entries)", user_id, entries)
            return jsonify({'success': True, 'message': 'VNDB link removed.'})
        except Exception as e:
            logger.error(f"Error removing VNDB link: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_remove())


@app.route('/api/vnclub/user_settings')
def get_vnclub_user_settings():
    """Members who chose a rating scale. NULL or no row means hikaru's default."""
    async def _get():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                if await _hikaru_table_missing(db, 'user_settings'):
                    return jsonify({'success': True, 'available': False, 'settings': [], 'counts': {},
                                    'note': 'user_settings table not present yet (hikaru migration has not run)'})
                has_users = not await _hikaru_table_missing(db, 'users')
                name_col = 'u.user_name' if has_users else 'NULL'
                users_join = ('LEFT JOIN users u ON u.discord_user_id = s.user_id '
                              if has_users else '')
                async with db.execute(
                    f"SELECT s.user_id, s.rating_scale, s.updated_at, {name_col} AS user_name "
                    f"FROM user_settings s {users_join}"
                    f"WHERE s.rating_scale IS NOT NULL "
                    f"ORDER BY s.rating_scale, s.user_id"
                ) as cursor:
                    rows = await cursor.fetchall()
            settings = []
            counts = {}
            for r in rows:
                row = dict(r)
                row['user_id'] = str(row['user_id'])
                settings.append(row)
                key = str(row['rating_scale'])
                counts[key] = counts.get(key, 0) + 1
            return jsonify({'success': True, 'available': True, 'settings': settings, 'counts': counts})
        except Exception as e:
            logger.error(f"Error fetching user_settings: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get())

@app.route('/api/vnclub/vn_titles')
def get_vnclub_vn_titles():
    """Get all Hikaru pool entries (monthly/seasonal/special).

    Returns the `id`, `guild_id` and `status` fields when the schema has
    them. The JSON keys are additive, so clients that ignore those fields
    render as before.
    """
    async def _get_vn_titles():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row

                # Detect whether the newer columns exist so this works against
                # older DBs too. Hikaru's own migration adds them on
                # next bot start, but the web console may be hit before that.
                cols = set()
                async with db.execute("PRAGMA table_info(vn_titles)") as cursor:
                    cols = {row['name'] for row in await cursor.fetchall()}

                pieces = ["vn_titles.vndb_id", "start_month", "end_month",
                          "is_monthly_points", "created_at"]
                if 'id' in cols:
                    pieces.insert(0, "vn_titles.id AS pool_id")
                if 'guild_id' in cols:
                    pieces.append("guild_id")
                if 'status' in cols:
                    pieces.append("status")

                async with db.execute(f"""
                    SELECT {', '.join(pieces)}
                    FROM vn_titles
                    ORDER BY start_month DESC, vn_titles.rowid DESC
                """) as cursor:
                    rows = await cursor.fetchall()
                    titles = [safe_json_row(dict(row)) for row in rows]

                return jsonify({
                    'success': True,
                    'titles': titles,
                    'has_pool_id': 'id' in cols,
                    'has_guild_id': 'guild_id' in cols,
                    'has_status': 'status' in cols,
                })
        except Exception as e:
            logger.error(f"Error fetching VN titles: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_vn_titles())

@app.route('/api/vnclub/vn_titles/add', methods=['POST'])
def add_vnclub_vn_title():
    """Add a pool entry (monthly/seasonal/special) for hikaru.

    Optional fields:
      - status: 'monthly' (default), 'seasonal', or 'special'
      - guild_id: server ID; NULL/omitted = legacy/global

    Schema-aware: only populates newer columns when they exist (older
    hikaru DBs fall back to the old INSERT shape).
    """
    async def _add_vn_title():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.json
            vndb_id = data.get('vndb_id')
            start_month = data.get('start_month')
            end_month = data.get('end_month')
            is_monthly_points = data.get('is_monthly_points', 10)
            status = (data.get('status') or 'monthly').strip().lower()
            guild_id = data.get('guild_id')
            if guild_id in ('', None):
                guild_id = None
            else:
                try:
                    guild_id = int(guild_id)
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'error': 'guild_id must be an integer'})

            if not all([vndb_id, start_month, end_month]):
                return jsonify({'success': False, 'error': 'Missing required fields: vndb_id, start_month, end_month'})
            if status not in ('monthly', 'seasonal', 'special'):
                return jsonify({'success': False, 'error': "status must be 'monthly', 'seasonal', or 'special'"})

            async with aiosqlite.connect(db_path) as db:
                async with db.execute("PRAGMA table_info(vn_titles)") as cursor:
                    cols = {row[1] for row in await cursor.fetchall()}

                columns = ["vndb_id", "start_month", "end_month", "is_monthly_points"]
                values = [vndb_id, start_month, end_month, is_monthly_points]
                if 'guild_id' in cols:
                    columns.append("guild_id")
                    values.append(guild_id)
                if 'status' in cols:
                    columns.append("status")
                    values.append(status)

                placeholders = ", ".join(["?"] * len(values))
                await db.execute(
                    f"INSERT INTO vn_titles ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                await db.commit()

                return jsonify({
                    'success': True,
                    'message': f"Pool entry added ({status})"
                })
        except Exception as e:
            logger.error(f"Error adding VN title: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add_vn_title())

@app.route('/api/vnclub/vn_titles/delete', methods=['POST'])
def delete_vnclub_vn_title():
    """Delete a pool entry. Prefer `pool_id` (the row's id) so admins can
    remove a specific entry: hikaru allows the same VN to live in the
    pool multiple times for different periods. Falls back to deleting all
    rows for a given `vndb_id` when `pool_id` isn't supplied (legacy clients).
    """
    async def _delete_vn_title():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.json or {}
            pool_id = data.get('pool_id')
            vndb_id = data.get('vndb_id')

            if pool_id is None and not vndb_id:
                return jsonify({'success': False, 'error': 'pool_id or vndb_id is required'})

            async with aiosqlite.connect(db_path) as db:
                if pool_id is not None:
                    try:
                        pool_id_int = int(pool_id)
                    except (TypeError, ValueError):
                        return jsonify({'success': False, 'error': 'pool_id must be an integer'})
                    cursor = await db.execute(
                        "DELETE FROM vn_titles WHERE id = ?", (pool_id_int,),
                    )
                else:
                    cursor = await db.execute(
                        "DELETE FROM vn_titles WHERE vndb_id = ?", (vndb_id,),
                    )
                await db.commit()

                if cursor.rowcount == 0:
                    return jsonify({'success': False, 'error': 'No matching pool entry found'})

                return jsonify({
                    'success': True,
                    'message': f"Removed {cursor.rowcount} pool entry(ies)",
                })
        except Exception as e:
            logger.error(f"Error deleting VN title: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_delete_vn_title())

@app.route('/api/vnclub/vn_titles/edit', methods=['POST'])
def edit_vnclub_vn_title():
    """Edit a pool entry. Prefers `pool_id` (the row's id); falls back to
    matching by `vndb_id` for back-compat (which updates EVERY row with
    that vndb_id when there are multiple entries; admins should pass
    pool_id to target a single entry).
    """
    async def _edit_title():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.get_json() or {}
            pool_id = data.get('pool_id')
            vndb_id = data.get('vndb_id')
            if pool_id is None and not vndb_id:
                return jsonify({'success': False, 'error': 'pool_id or vndb_id is required'})

            # Build dynamic update query
            updates = []
            params = []

            if 'start_month' in data:
                updates.append('start_month = ?')
                params.append(data['start_month'])
            if 'end_month' in data:
                updates.append('end_month = ?')
                params.append(data['end_month'])
            if 'is_monthly_points' in data:
                updates.append('is_monthly_points = ?')
                params.append(int(data['is_monthly_points']))
            if 'status' in data and data['status']:
                status = str(data['status']).strip().lower()
                if status not in ('monthly', 'seasonal', 'special'):
                    return jsonify({'success': False, 'error': "status must be 'monthly', 'seasonal', or 'special'"})
                updates.append('status = ?')
                params.append(status)
            if 'guild_id' in data:
                gid = data.get('guild_id')
                if gid in ('', None):
                    updates.append('guild_id = NULL')
                else:
                    try:
                        updates.append('guild_id = ?')
                        params.append(int(gid))
                    except (TypeError, ValueError):
                        return jsonify({'success': False, 'error': 'guild_id must be an integer'})

            if not updates:
                return jsonify({'success': False, 'error': 'No fields to update'})

            if pool_id is not None:
                try:
                    pool_id_int = int(pool_id)
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'error': 'pool_id must be an integer'})
                params.append(pool_id_int)
                query = f"UPDATE vn_titles SET {', '.join(updates)} WHERE id = ?"
            else:
                params.append(vndb_id)
                query = f"UPDATE vn_titles SET {', '.join(updates)} WHERE vndb_id = ?"

            async with aiosqlite.connect(db_path) as db:
                await db.execute(query, params)
                await db.commit()

            return jsonify({'success': True, 'message': 'Monthly VN updated successfully'})
        except Exception as e:
            logger.error(f"Error editing VN title: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_edit_title())


@app.route('/api/vnclub/cycles')
def get_vnclub_cycles():
    """Read-only listing of hikaru's nomination/voting cycles.

    Returns active (phase != 'closed') cycles, plus the most recent N closed
    ones for context. Per-cycle nominee + vote counts are computed in SQL so
    one round-trip is enough. Empty list when the cycle tables don't exist
    (older hikaru DBs that haven't run the cycle migration yet).
    """
    async def _get_cycles():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            include_closed = request.args.get('include_closed', '5')
            try:
                closed_limit = max(0, min(50, int(include_closed)))
            except ValueError:
                closed_limit = 5

            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row

                # Detect tables/cols. Schema evolved in two waves:
                # (1) cycle tables were added by the cycle migration;
                # (2) vn_nominations was DROPPED by the unify migration;
                # nominations now live in vn_titles with status='nominated'.
                # Both legacy + post-unify DBs are supported here.
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('vn_cycles','vn_nominations','vn_votes','vn_titles')"
                ) as cursor:
                    tables = {row['name'] for row in await cursor.fetchall()}
                if 'vn_cycles' not in tables:
                    return jsonify({'success': True, 'cycles': [], 'note': 'vn_cycles table not present yet'})

                async with db.execute("PRAGMA table_info(vn_cycles)") as cursor:
                    cycle_cols = {row['name'] for row in await cursor.fetchall()}
                kind_select = "kind" if 'kind' in cycle_cols else "'monthly' AS kind"
                end_select = "target_end_month" if 'target_end_month' in cycle_cols else "target_month AS target_end_month"
                # Newer columns added after the original cycle migration:
                # surface them when present, fall back to NULL otherwise so
                # callers always see a stable shape.
                vote_ui_select = "vote_ui" if 'vote_ui' in cycle_cols else "NULL AS vote_ui"
                allowed_role_select = (
                    "allowed_role_id" if 'allowed_role_id' in cycle_cols
                    else "NULL AS allowed_role_id"
                )
                closes_at_select = "closes_at" if 'closes_at' in cycle_cols else "NULL AS closes_at"

                # Active cycles first (any phase != closed), then most recent
                # `closed_limit` closed ones.
                async with db.execute(f"""
                    SELECT id, guild_id, phase, {kind_select}, target_month, {end_select},
                           vote_choice_mode, vote_winner_count,
                           announcement_channel_id, announcement_message_id,
                           opened_at, closed_at,
                           {vote_ui_select}, {allowed_role_select}, {closes_at_select}
                    FROM vn_cycles
                    WHERE phase != 'closed'
                    ORDER BY opened_at DESC
                """) as cursor:
                    active_rows = [dict(row) for row in await cursor.fetchall()]

                async with db.execute(f"""
                    SELECT id, guild_id, phase, {kind_select}, target_month, {end_select},
                           vote_choice_mode, vote_winner_count,
                           announcement_channel_id, announcement_message_id,
                           opened_at, closed_at,
                           {vote_ui_select}, {allowed_role_select}, {closes_at_select}
                    FROM vn_cycles
                    WHERE phase = 'closed'
                    ORDER BY closed_at DESC
                    LIMIT ?
                """, (closed_limit,)) as cursor:
                    closed_rows = [dict(row) for row in await cursor.fetchall()]

                cycles = active_rows + closed_rows

                # Annotate each cycle with nominee/vote counts. Source for
                # nominee_count: vn_titles (post-unify) or vn_nominations
                # (legacy DBs that haven't run _unify_nominations_into_vn_titles
                # yet). Detect once here so the per-row loop is cheap.
                use_vn_titles_for_nominees = False
                if 'vn_titles' in tables:
                    async with db.execute("PRAGMA table_info(vn_titles)") as cur:
                        vn_titles_cols = {r['name'] for r in await cur.fetchall()}
                    use_vn_titles_for_nominees = (
                        'cycle_id' in vn_titles_cols and 'status' in vn_titles_cols
                    )
                for c in cycles:
                    if use_vn_titles_for_nominees:
                        async with db.execute(
                            "SELECT COUNT(*) FROM vn_titles "
                            "WHERE cycle_id = ? AND status = 'nominated'",
                            (c['id'],),
                        ) as cur:
                            c['nominee_count'] = (await cur.fetchone())[0]
                    elif 'vn_nominations' in tables:
                        async with db.execute(
                            "SELECT COUNT(*) FROM vn_nominations WHERE cycle_id = ?",
                            (c['id'],),
                        ) as cur:
                            c['nominee_count'] = (await cur.fetchone())[0]
                    else:
                        c['nominee_count'] = 0
                    if 'vn_votes' in tables:
                        async with db.execute(
                            "SELECT COUNT(*) FROM vn_votes WHERE cycle_id = ?",
                            (c['id'],),
                        ) as cur:
                            c['vote_count'] = (await cur.fetchone())[0]
                    else:
                        c['vote_count'] = 0

                return jsonify({
                    'success': True,
                    'cycles': [safe_json_row(c) for c in cycles],
                })
        except Exception as e:
            logger.error(f"Error fetching cycles: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_cycles())


@app.route('/api/vnclub/cycles/<int:cycle_id>/nominations')
def get_vnclub_cycle_nominations(cycle_id: int):
    """List all nominations for a cycle, with current vote counts.

    Post-unify hikaru DBs store nominations in `vn_titles` with
    `status='nominated'` and `cycle_id` pointing at the cycle. Legacy
    DBs that haven't run the unify migration still use `vn_nominations`.
    Both are supported here; the response shape is identical so the
    frontend doesn't have to branch.
    """
    async def _get():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                # Decide source: vn_titles (post-unify) or vn_nominations (legacy).
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('vn_titles','vn_nominations')"
                ) as cursor:
                    tables = {row['name'] for row in await cursor.fetchall()}
                use_vn_titles = False
                if 'vn_titles' in tables:
                    async with db.execute("PRAGMA table_info(vn_titles)") as cur:
                        cols = {r['name'] for r in await cur.fetchall()}
                    use_vn_titles = 'cycle_id' in cols and 'status' in cols and 'nominator_user_id' in cols
                if use_vn_titles:
                    async with db.execute("""
                        SELECT vt.id AS nomination_id, vt.vndb_id,
                               COALESCE(vt.title_cache, vt.vndb_id) AS title,
                               vt.nominator_user_id AS user_id, vt.guild_id,
                               vt.created_at,
                               COALESCE((
                                   SELECT COUNT(*) FROM vn_votes v
                                   WHERE v.cycle_id = vt.cycle_id
                                     AND v.nomination_id = vt.id
                               ), 0) AS votes
                        FROM vn_titles vt
                        WHERE vt.cycle_id = ?
                          AND vt.status IN ('nominated', 'monthly', 'seasonal', 'special')
                        ORDER BY votes DESC, vt.created_at ASC
                    """, (cycle_id,)) as cursor:
                        rows = [safe_json_row(dict(r)) for r in await cursor.fetchall()]
                elif 'vn_nominations' in tables:
                    async with db.execute("""
                        SELECT n.id AS nomination_id, n.vndb_id, n.title, n.user_id, n.guild_id,
                               n.created_at,
                               COALESCE((
                                   SELECT COUNT(*) FROM vn_votes v
                                   WHERE v.cycle_id = n.cycle_id AND v.nomination_id = n.id
                               ), 0) AS votes
                        FROM vn_nominations n
                        WHERE n.cycle_id = ?
                        ORDER BY votes DESC, n.created_at ASC
                    """, (cycle_id,)) as cursor:
                        rows = [safe_json_row(dict(r)) for r in await cursor.fetchall()]
                else:
                    rows = []
                return jsonify({'success': True, 'nominations': rows})
        except Exception as e:
            logger.error(f"Error fetching cycle nominations: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get())


@app.route('/api/vnclub/cycles/<int:cycle_id>/edit', methods=['POST'])
def edit_vnclub_cycle(cycle_id: int):
    """Edit a hikaru voting cycle's metadata fields. Mirrors the bot's
    /manage_voting workflow but only for fields that have NO Discord
    side-effect (no message posts/edits). Phase changes that would
    require Discord interaction (open_voting, close_voting, post winner
    banner) stay bot-only; see /set_phase below for the safe subset.

    Editable fields (any subset; missing fields stay unchanged):
      - target_month        TEXT  YYYY-MM
      - target_end_month    TEXT  YYYY-MM
      - vote_choice_mode    TEXT  'single' | 'multi'
      - vote_winner_count   INT   1-10
      - vote_ui             TEXT  'dropdown' | 'buttons' | null
      - allowed_role_id     INT   role snowflake or null to clear
      - closes_at           TEXT  'YYYY-MM-DD HH:MM:SS' (UTC) or null

    The newer fields (`vote_ui`, `allowed_role_id`, `closes_at`) only
    update if the corresponding columns exist on `vn_cycles`; older
    DBs that haven't run the relevant migrations get a clean error
    rather than a SQL exception.
    """
    async def _edit_cycle():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.get_json() or {}
            updates = []
            params = []

            if 'target_month' in data:
                updates.append('target_month = ?')
                params.append(str(data['target_month']))
            if 'target_end_month' in data:
                updates.append('target_end_month = ?')
                params.append(str(data['target_end_month']))
            if 'vote_choice_mode' in data and data['vote_choice_mode']:
                mode = str(data['vote_choice_mode']).strip().lower()
                if mode not in ('single', 'multi'):
                    return jsonify({'success': False, 'error': "vote_choice_mode must be 'single' or 'multi'"})
                updates.append('vote_choice_mode = ?')
                params.append(mode)
            if 'vote_winner_count' in data:
                try:
                    wc = int(data['vote_winner_count'])
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'error': 'vote_winner_count must be an integer'})
                if wc < 1 or wc > 10:
                    return jsonify({'success': False, 'error': 'vote_winner_count must be 1-10'})
                updates.append('vote_winner_count = ?')
                params.append(wc)

            # Detect column presence once before applying the newer-field
            # updates so callers using a pre-migration DB get a clear
            # "column missing" error instead of a SQLite syntax failure.
            new_cols_to_check: list[str] = []
            for key in ('vote_ui', 'allowed_role_id', 'closes_at'):
                if key in data:
                    new_cols_to_check.append(key)
            cycle_cols: set[str] = set()
            if new_cols_to_check:
                async with aiosqlite.connect(db_path) as db_check:
                    db_check.row_factory = aiosqlite.Row
                    async with db_check.execute("PRAGMA table_info(vn_cycles)") as cur:
                        cycle_cols = {row['name'] for row in await cur.fetchall()}
                missing = [c for c in new_cols_to_check if c not in cycle_cols]
                if missing:
                    return jsonify({
                        'success': False,
                        'error': f"Column(s) {missing} don't exist on this DB; "
                                 f"run hikaru's migrations first.",
                    })

            if 'vote_ui' in data:
                val = data['vote_ui']
                if val is None or (isinstance(val, str) and val.strip() == ''):
                    updates.append('vote_ui = NULL')
                else:
                    val = str(val).strip().lower()
                    if val not in ('dropdown', 'buttons'):
                        return jsonify({'success': False, 'error': "vote_ui must be 'dropdown' or 'buttons'"})
                    updates.append('vote_ui = ?')
                    params.append(val)
            if 'allowed_role_id' in data:
                val = data['allowed_role_id']
                if val is None or (isinstance(val, str) and val.strip() == ''):
                    updates.append('allowed_role_id = NULL')
                else:
                    try:
                        rid = int(val)
                    except (TypeError, ValueError):
                        return jsonify({'success': False, 'error': 'allowed_role_id must be an integer (role snowflake) or empty'})
                    updates.append('allowed_role_id = ?')
                    params.append(rid)
            if 'closes_at' in data:
                val = data['closes_at']
                if val is None or (isinstance(val, str) and val.strip() == ''):
                    updates.append('closes_at = NULL')
                else:
                    s = str(val).strip().replace('T', ' ')[:19]
                    # Light validation: must parse as YYYY-MM-DD HH:MM:SS.
                    try:
                        from datetime import datetime as _dt
                        _dt.strptime(s, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        return jsonify({'success': False, 'error': "closes_at must be 'YYYY-MM-DD HH:MM:SS' (UTC) or empty"})
                    updates.append('closes_at = ?')
                    params.append(s)

            if not updates:
                return jsonify({'success': False, 'error': 'No fields to update'})

            params.append(cycle_id)
            query = f"UPDATE vn_cycles SET {', '.join(updates)} WHERE id = ?"

            async with aiosqlite.connect(db_path) as db:
                async with db.execute("SELECT id FROM vn_cycles WHERE id = ?", (cycle_id,)) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': f'Cycle #{cycle_id} not found'})
                await db.execute(query, params)
                await db.commit()

            return jsonify({'success': True, 'message': f'Cycle #{cycle_id} updated successfully'})
        except Exception as e:
            logger.error(f"Error editing cycle: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_edit_cycle())


@app.route('/api/vnclub/cycles/<int:cycle_id>/cancel', methods=['POST'])
def cancel_vnclub_cycle(cycle_id: int):
    """Cancel a cycle: drop pending nominations + close the cycle in one
    transaction. Equivalent to the bot's /manage_voting action:cancel.

    No Discord side-effects (no banner edit / message disable). Use the
    bot if you also want the original announcement message updated.
    """
    async def _cancel_cycle():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            async with aiosqlite.connect(db_path) as db:
                async with db.execute("SELECT phase FROM vn_cycles WHERE id = ?", (cycle_id,)) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    return jsonify({'success': False, 'error': f'Cycle #{cycle_id} not found'})
                if row[0] == 'closed':
                    return jsonify({'success': False, 'error': f'Cycle #{cycle_id} is already closed'})

                # Atomic: delete pending nominations, then close. Either both
                # commit or both roll back.
                try:
                    await db.execute(
                        "DELETE FROM vn_titles WHERE cycle_id = ? AND status = 'nominated'",
                        (cycle_id,),
                    )
                    await db.execute(
                        "UPDATE vn_cycles SET phase = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (cycle_id,),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

            return jsonify({
                'success': True,
                'message': f'Cycle #{cycle_id} cancelled (pending nominations removed).',
            })
        except Exception as e:
            logger.error(f"Error cancelling cycle: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_cancel_cycle())


@app.route('/api/vnclub/cycles/<int:cycle_id>/set_phase', methods=['POST'])
def set_vnclub_cycle_phase(cycle_id: int):
    """Restricted phase set: only `nominating` ↔ `closed_nominating`.

    Anything involving 'voting' or 'closed' has Discord side-effects
    (post vote message, post winner banner, edit announcements, depromote
    winners) and stays bot-only. Use /api/vnclub/cycles/<id>/cancel if you
    just want to terminate the cycle without those side-effects.
    """
    SAFE_PHASES = ('nominating', 'closed_nominating')

    async def _set_phase():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            data = request.get_json() or {}
            phase = (data.get('phase') or '').strip().lower()
            if phase not in SAFE_PHASES:
                return jsonify({
                    'success': False,
                    'error': (
                        f"phase must be one of {SAFE_PHASES}. "
                        "Phases involving 'voting' or 'closed' need Discord side-effects "
                        "and must be done from the bot via /manage_voting."
                    ),
                }), 400

            async with aiosqlite.connect(db_path) as db:
                async with db.execute("SELECT phase FROM vn_cycles WHERE id = ?", (cycle_id,)) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    return jsonify({'success': False, 'error': f'Cycle #{cycle_id} not found'}), 404
                current_phase = row[0]
                # Block transitions that originate from voting/closed since
                # those would skip Discord side-effects.
                if current_phase not in SAFE_PHASES:
                    return jsonify({
                        'success': False,
                        'error': (
                            f"Cycle is in phase '{current_phase}'. Only cycles "
                            f"in {SAFE_PHASES} can be rewound from the web. "
                            "Use the bot for transitions to/from voting/closed."
                        ),
                    }), 400

                await db.execute(
                    "UPDATE vn_cycles SET phase = ? WHERE id = ?",
                    (phase, cycle_id),
                )
                await db.commit()

            return jsonify({
                'success': True,
                'message': f'Cycle #{cycle_id} phase set to {phase}.',
            })
        except Exception as e:
            logger.error(f"Error setting cycle phase: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_set_phase())


# ====================================================================
# Voting moderation + per-guild voting settings (DB-only mirrors of
# hikaru's /manage_voting → Manage votes / Settings sub-panels).
# Discord-side actions stay bot-only; everything below is pure SQLite.
# ====================================================================


@app.route('/api/vnclub/cycles/<int:cycle_id>/votes')
def get_vnclub_cycle_votes(cycle_id: int):
    """List every individual vote in a cycle for moderation purposes.

    Joined to vn_titles so the response carries the nominee title, which
    saves the frontend an N+1 round-trip. Returns an empty list when the cycle
    has no votes (or vn_votes / vn_titles aren't set up yet).
    """
    async def _get():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('vn_votes','vn_titles')"
                ) as cursor:
                    tables = {row['name'] for row in await cursor.fetchall()}
                if 'vn_votes' not in tables:
                    return jsonify({'success': True, 'votes': []})
                # Detect title source: vn_titles (post-unify) is preferred.
                title_select = "v.nomination_id AS title"
                join_clause = ""
                if 'vn_titles' in tables:
                    async with db.execute("PRAGMA table_info(vn_titles)") as cur:
                        cols = {r['name'] for r in await cur.fetchall()}
                    if 'title_cache' in cols and 'vndb_id' in cols:
                        title_select = "COALESCE(vt.title_cache, vt.vndb_id) AS title"
                        join_clause = "LEFT JOIN vn_titles vt ON vt.id = v.nomination_id"
                async with db.execute(f"""
                    SELECT v.id AS vote_id, v.user_id, v.nomination_id,
                           {title_select},
                           v.created_at
                    FROM vn_votes v
                    {join_clause}
                    WHERE v.cycle_id = ?
                    ORDER BY v.created_at DESC
                """, (cycle_id,)) as cursor:
                    rows = [safe_json_row(dict(r)) for r in await cursor.fetchall()]
                return jsonify({'success': True, 'votes': rows})
        except Exception as e:
            logger.error(f"Error fetching cycle votes: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get())


@app.route('/api/vnclub/cycles/<int:cycle_id>/votes/<int:vote_id>/delete', methods=['POST'])
def delete_vnclub_vote(cycle_id: int, vote_id: int):
    """Remove a single vote row: admin moderation for troll/duplicate votes.

    The cycle_id in the URL is a sanity check: rejects deletions against
    a vote that doesn't belong to the named cycle, so a stale URL can't
    accidentally yank an unrelated row.
    """
    async def _delete():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT cycle_id, user_id FROM vn_votes WHERE id = ?",
                    (vote_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    return jsonify({'success': False, 'error': f'Vote #{vote_id} not found'}), 404
                if row['cycle_id'] != cycle_id:
                    return jsonify({
                        'success': False,
                        'error': (
                            f"Vote #{vote_id} belongs to cycle "
                            f"#{row['cycle_id']}, not #{cycle_id}."
                        ),
                    }), 400
                await db.execute("DELETE FROM vn_votes WHERE id = ?", (vote_id,))
                await db.commit()
            return jsonify({
                'success': True,
                'message': f'Vote #{vote_id} (user {row["user_id"]}) removed.',
            })
        except Exception as e:
            logger.error(f"Error deleting vote: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_delete())


@app.route('/api/vnclub/guild_settings')
def get_vnclub_guild_settings():
    """List every guild_settings row: per-guild defaults that hikaru's
    /manage_voting → Settings sub-panel manages.

    Schema:
      guild_id INTEGER PRIMARY KEY
      default_voting_role_id INTEGER (nullable)
      default_vote_ui TEXT (nullable, 'dropdown' or 'buttons')
      updated_at TIMESTAMP

    Returns an empty list when the table doesn't exist yet (legacy DBs
    pre-dating the guild_settings migration).
    """
    async def _get():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_settings'"
                ) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': True, 'settings': [], 'note': 'guild_settings table not present yet'})
                async with db.execute("PRAGMA table_info(guild_settings)") as cur:
                    cols = {r['name'] for r in await cur.fetchall()}
                ui_select = "default_vote_ui" if 'default_vote_ui' in cols else "NULL AS default_vote_ui"
                async with db.execute(f"""
                    SELECT guild_id, default_voting_role_id, {ui_select}, updated_at
                    FROM guild_settings
                    ORDER BY guild_id
                """) as cursor:
                    rows = [safe_json_row(dict(r)) for r in await cursor.fetchall()]
                return jsonify({'success': True, 'settings': rows})
        except Exception as e:
            logger.error(f"Error fetching guild_settings: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get())


@app.route('/api/vnclub/guild_settings/edit', methods=['POST'])
def edit_vnclub_guild_settings():
    """Upsert a guild's voting defaults (default_voting_role_id +
    default_vote_ui). Body shape:

        {
          "guild_id": <int>,                 # required
          "default_voting_role_id": <int|null>,  # optional
          "default_vote_ui": "dropdown" | "buttons" | null  # optional
        }

    Only fields explicitly present in the body are touched: pass
    `null` to clear, omit to leave alone. Mirrors hikaru's UPSERT
    pattern in DatabaseQueries.UPSERT_DEFAULT_VOTING_ROLE etc.
    """
    async def _edit():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            data = request.get_json() or {}
            try:
                gid = int(data.get('guild_id'))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'guild_id is required and must be an integer'}), 400

            # Validate vote_ui early so bad input is rejected before
            # touching the DB.
            ui_present = 'default_vote_ui' in data
            ui_value = None
            if ui_present:
                raw = data['default_vote_ui']
                if raw is None or (isinstance(raw, str) and raw.strip() == ''):
                    ui_value = None
                else:
                    raw = str(raw).strip().lower()
                    if raw not in ('dropdown', 'buttons'):
                        return jsonify({'success': False, 'error': "default_vote_ui must be 'dropdown', 'buttons', or null"}), 400
                    ui_value = raw

            role_present = 'default_voting_role_id' in data
            role_value = None
            if role_present:
                raw = data['default_voting_role_id']
                if raw is None or (isinstance(raw, str) and raw.strip() == ''):
                    role_value = None
                else:
                    try:
                        role_value = int(raw)
                    except (TypeError, ValueError):
                        return jsonify({'success': False, 'error': 'default_voting_role_id must be an integer or null'}), 400

            if not role_present and not ui_present:
                return jsonify({'success': False, 'error': 'No fields to update'}), 400

            async with aiosqlite.connect(db_path) as db:
                # Confirm the table + the optional default_vote_ui column.
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_settings'"
                ) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': "guild_settings table doesn't exist; run hikaru migrations first"}), 400
                async with db.execute("PRAGMA table_info(guild_settings)") as cur:
                    cols = {r['name'] for r in await cur.fetchall()}
                has_ui_col = 'default_vote_ui' in cols
                if ui_present and not has_ui_col:
                    return jsonify({'success': False, 'error': "default_vote_ui column doesn't exist; run hikaru migrations first"}), 400

                # Ensure a row exists for this guild before patching the
                # individual fields. INSERT-OR-IGNORE keeps the existing
                # values when the row is already there.
                await db.execute(
                    "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
                    (gid,),
                )
                sets: list[str] = []
                params: list = []
                if role_present:
                    if role_value is None:
                        sets.append('default_voting_role_id = NULL')
                    else:
                        sets.append('default_voting_role_id = ?')
                        params.append(role_value)
                if ui_present:
                    if ui_value is None:
                        sets.append('default_vote_ui = NULL')
                    else:
                        sets.append('default_vote_ui = ?')
                        params.append(ui_value)
                sets.append('updated_at = CURRENT_TIMESTAMP')
                params.append(gid)
                await db.execute(
                    f"UPDATE guild_settings SET {', '.join(sets)} WHERE guild_id = ?",
                    params,
                )
                await db.commit()

            return jsonify({'success': True, 'message': f'Settings for guild {gid} updated.'})
        except Exception as e:
            logger.error(f"Error editing guild_settings: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_edit())


@app.route('/api/vnclub/guild_managers')
def get_vnclub_guild_managers():
    """List per-guild manager grants: the users/roles authorized to run
    hikaru's /manage_* commands inside a single guild. Bot-host operators
    (AUTHORIZED_USERS env) are unconditional and not stored here.

    Schema (set by hikaru's migrations):
      guild_id        INTEGER  NOT NULL
      principal_type  TEXT     NOT NULL  CHECK(principal_type IN ('user','role'))
      principal_id    INTEGER  NOT NULL
      added_by_user_id INTEGER (nullable; web-console-issued grants
                                may omit this, since reaching the
                                console already requires its login)
      added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      PRIMARY KEY (guild_id, principal_type, principal_id)

    Optional query param ``guild_id`` narrows to one guild. Returns an
    empty list (with a note) when the table doesn't exist yet; same
    legacy-DB safety net as guild_settings.
    """
    async def _get():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            guild_filter_raw = request.args.get('guild_id', '').strip()
            guild_filter: int | None = None
            if guild_filter_raw:
                try:
                    guild_filter = int(guild_filter_raw)
                except ValueError:
                    return jsonify({'success': False, 'error': 'guild_id must be an integer'}), 400
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_managers'"
                ) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': True, 'managers': [], 'note': 'guild_managers table not present yet'})
                query = (
                    "SELECT guild_id, principal_type, principal_id, "
                    "added_by_user_id, added_at FROM guild_managers"
                )
                params: tuple = ()
                if guild_filter is not None:
                    query += " WHERE guild_id = ?"
                    params = (guild_filter,)
                query += " ORDER BY guild_id, added_at"
                async with db.execute(query, params) as cursor:
                    rows = [safe_json_row(dict(r)) for r in await cursor.fetchall()]
                return jsonify({'success': True, 'managers': rows})
        except Exception as e:
            logger.error(f"Error fetching guild_managers: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get())


def _validate_guild_manager_payload(data: dict) -> tuple[int, str, int, int | None] | tuple[None, str]:
    """Parse + validate the JSON body shared by /add and /delete.

    Returns ``(guild_id, principal_type, principal_id, added_by_user_id)``
    on success, or ``(None, error_message)`` on validation failure.
    ``added_by_user_id`` is optional and defaults to None.
    """
    try:
        gid = int(data.get('guild_id'))
    except (TypeError, ValueError):
        return (None, 'guild_id is required and must be an integer')
    ptype_raw = data.get('principal_type')
    if not isinstance(ptype_raw, str) or ptype_raw not in ('user', 'role'):
        return (None, "principal_type must be 'user' or 'role'")
    try:
        pid = int(data.get('principal_id'))
    except (TypeError, ValueError):
        return (None, 'principal_id is required and must be an integer')
    added_by_raw = data.get('added_by_user_id')
    added_by: int | None = None
    if added_by_raw is not None and not (isinstance(added_by_raw, str) and added_by_raw.strip() == ''):
        try:
            added_by = int(added_by_raw)
        except (TypeError, ValueError):
            return (None, 'added_by_user_id must be an integer or null')
    return (gid, ptype_raw, pid, added_by)


@app.route('/api/vnclub/guild_managers/add', methods=['POST'])
def add_vnclub_guild_manager():
    """Grant manager permission to a user or role inside a single guild.

    INSERT-OR-IGNORE so re-adding an existing grant is a silent no-op,
    matching hikaru's ``/manage_managers action:add`` semantics. The
    composite primary key on ``(guild_id, principal_type, principal_id)``
    is what backs that behaviour at the DB level.
    """
    async def _add():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            data = request.get_json() or {}
            parsed = _validate_guild_manager_payload(data)
            if parsed[0] is None:
                return jsonify({'success': False, 'error': parsed[1]}), 400
            gid, ptype, pid, added_by = parsed
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_managers'"
                ) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': "guild_managers table doesn't exist; run hikaru migrations first"}), 400
                async with db.execute(
                    "INSERT OR IGNORE INTO guild_managers "
                    "(guild_id, principal_type, principal_id, added_by_user_id) "
                    "VALUES (?, ?, ?, ?)",
                    (gid, ptype, pid, added_by),
                ) as cursor:
                    inserted = cursor.rowcount > 0
                await db.commit()
            return jsonify({
                'success': True,
                'inserted': inserted,
                'message': (
                    f'Manager grant added: guild {gid}, {ptype} {pid}.'
                    if inserted else
                    f'Already a manager: guild {gid}, {ptype} {pid} (no-op).'
                ),
            })
        except Exception as e:
            logger.error(f"Error adding guild_manager: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_add())


@app.route('/api/vnclub/guild_managers/delete', methods=['POST'])
def delete_vnclub_guild_manager():
    """Revoke a manager grant by composite key (guild_id, principal_type,
    principal_id). ``deleted`` in the response is False when the row
    didn't exist (idempotent; matches the add endpoint's shape)."""
    async def _delete():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
            data = request.get_json() or {}
            parsed = _validate_guild_manager_payload(data)
            if parsed[0] is None:
                return jsonify({'success': False, 'error': parsed[1]}), 400
            gid, ptype, pid, _added_by = parsed
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_managers'"
                ) as cursor:
                    if not await cursor.fetchone():
                        return jsonify({'success': False, 'error': "guild_managers table doesn't exist; run hikaru migrations first"}), 400
                async with db.execute(
                    "DELETE FROM guild_managers "
                    "WHERE guild_id = ? AND principal_type = ? AND principal_id = ?",
                    (gid, ptype, pid),
                ) as cursor:
                    deleted = cursor.rowcount > 0
                await db.commit()
            return jsonify({
                'success': True,
                'deleted': deleted,
                'message': (
                    f'Manager grant removed: guild {gid}, {ptype} {pid}.'
                    if deleted else
                    f'No matching grant for guild {gid}, {ptype} {pid} (already absent).'
                ),
            })
        except Exception as e:
            logger.error(f"Error deleting guild_manager: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_delete())


# --- Nomination themes (writes hikaru's theme_templates / theme_assignments) ---
#
# created_by_user_id is left NULL on every write here. The console authenticates
# with one shared Basic Auth account and holds no Discord identity, so a NULL
# marks "written from the console" while hikaru's own editor stores the manager's
# Discord id. Each write also leaves a log line.

def _validate_theme_template_payload(data: dict):
    """Returns (guild_id:int, name:str, rules:dict) or (None, error). The rules
    dict is the canonical form, ready to serialise."""
    if not isinstance(data, dict):
        return None, "invalid body"
    gid = str(data.get('guild_id') or '').strip()
    name = (data.get('name') or '').strip()
    if not gid.isdigit():
        return None, "guild_id must be a numeric snowflake"
    if not name:
        return None, "name is required"
    if len(name) > 80:
        return None, "name too long (max 80)"
    try:
        canonical = validate_rules(data.get('rules') or {})
    except ValueError as e:
        return None, f"invalid rules: {e}"
    return int(gid), name, canonical


def _theme_rules_view(rules_json) -> dict:
    """Read-side render of a stored ruleset: {summary, valid, conflicts}.
    Runs the vendored engine so the console describes a ruleset the same way
    hikaru's gate reads it. A stored ruleset that no longer validates is
    reported rather than shown as unrestricted."""
    try:
        canonical = validate_rules(json.loads(rules_json or '{}'))
    except (ValueError, TypeError) as e:
        return {'summary': f'Unreadable rules: {e}', 'valid': False, 'conflicts': []}
    return {'summary': rules_summary(canonical), 'valid': True,
            'conflicts': rules_conflicts(canonical)}


# Matches the cap on hikaru's own assign modal.
THEME_LABEL_MAX = 80

# Anime-season month map, kept in lockstep with hikaru's ANIME_SEASONS.
_SEASON_MONTHS = {"winter": (1, 2, 3), "spring": (4, 5, 6), "summer": (7, 8, 9), "fall": (10, 11, 12)}


def _resolve_theme_period(kind: str, target_month: str):
    """(start_month, end_month) or (None, error). monthly: start==end.
    seasonal: the 3-month window containing target_month."""
    import re
    if not re.match(r"^\d{4}-\d{2}$", target_month or ""):
        return None, "target_month must be YYYY-MM"
    year, month = int(target_month[:4]), int(target_month[5:7])
    if not (1 <= month <= 12):
        return None, "target_month has an invalid month"
    if kind == "monthly":
        return (target_month, target_month), None
    if kind == "seasonal":
        for season, months in _SEASON_MONTHS.items():
            if month in months:
                return (f"{year:04d}-{months[0]:02d}", f"{year:04d}-{months[-1]:02d}"), None
        return None, "could not resolve season"
    return None, "kind must be monthly or seasonal"


@app.route('/api/vndb/tags', methods=['GET'])
def vndb_search_tags():
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({'success': True, 'results': []})

    async def _search():
        async with VNDBClient() as vndb:
            return await vndb.search_tags(query)
    try:
        results = run_async(_search())
        out = [{'id': t.get('id'), 'name': t.get('name'), 'category': t.get('category')}
               for t in (results or []) if t.get('id')]
        return jsonify({'success': True, 'results': out})
    except Exception as e:
        logger.error("vndb tag search failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vndb/producers', methods=['GET'])
def vndb_search_producers():
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({'success': True, 'results': []})

    async def _search():
        async with VNDBClient() as vndb:
            return await vndb.search_producers(query)
    try:
        results = run_async(_search())
        # VNDB's romaji `name`, matching what hikaru's picker stores, so the same
        # producer reads the same whichever editor wrote the rule.
        out = [{'id': p.get('id'), 'name': (p.get('name') or p.get('original'))}
               for p in (results or []) if p.get('id')]
        return jsonify({'success': True, 'results': out})
    except Exception as e:
        logger.error("vndb producer search failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/theme/guilds', methods=['GET'])
def vnclub_theme_guilds():
    db_path = get_hikaru_db_path()
    if not db_path:
        return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

    async def _get():
        import aiosqlite
        guilds = set()
        async with aiosqlite.connect(db_path) as db:
            # Union of guild_ids the bot knows about, so the picker isn't empty
            # before any theme exists. Tables may be absent on a fresh hikaru DB.
            for tbl, col in (("vn_titles", "guild_id"), ("vn_cycles", "guild_id"),
                             ("theme_templates", "guild_id"), ("theme_assignments", "guild_id")):
                if await _hikaru_table_missing(db, tbl):
                    continue
                async with db.execute(
                    f"SELECT DISTINCT {col} FROM {tbl} WHERE {col} IS NOT NULL"
                ) as cur:
                    for row in await cur.fetchall():
                        guilds.add(str(row[0]))
        return sorted(guilds)
    try:
        return jsonify({'success': True, 'guilds': run_async(_get())})
    except Exception as e:
        logger.error("theme guilds failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/theme/templates', methods=['GET'])
def vnclub_theme_templates():
    guild_id = (request.args.get('guild_id') or '').strip()
    db_path = get_hikaru_db_path()
    if not db_path:
        return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
    if not guild_id.isdigit():
        return jsonify({'success': False, 'error': 'guild_id required'}), 400

    async def _get():
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            if await _hikaru_table_missing(db, "theme_templates"):
                return None
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name, rules_json, created_by_user_id, updated_at FROM theme_templates "
                "WHERE guild_id = ? ORDER BY name COLLATE NOCASE", (int(guild_id),)
            ) as cur:
                rows = [safe_json_row(dict(r)) for r in await cur.fetchall()]
            for row in rows:
                row.update(_theme_rules_view(row.get('rules_json')))
            return rows
    try:
        rows = run_async(_get())
        if rows is None:
            return jsonify({'success': False, 'error': "theme_templates table doesn't exist; run hikaru migrations first"}), 400
        return jsonify({'success': True, 'templates': rows})
    except Exception as e:
        logger.error("theme templates failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/theme/assignments', methods=['GET'])
def vnclub_theme_assignments():
    guild_id = (request.args.get('guild_id') or '').strip()
    db_path = get_hikaru_db_path()
    if not db_path:
        return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
    if not guild_id.isdigit():
        return jsonify({'success': False, 'error': 'guild_id required'}), 400

    async def _get():
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            if await _hikaru_table_missing(db, "theme_assignments"):
                return None
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT kind, start_month, end_month, label, rules_json, source_template_id, "
                "created_by_user_id, updated_at "
                "FROM theme_assignments WHERE guild_id = ? ORDER BY start_month DESC, kind", (int(guild_id),)
            ) as cur:
                rows = [safe_json_row(dict(r)) for r in await cur.fetchall()]
            for row in rows:
                row.update(_theme_rules_view(row.get('rules_json')))
            return rows
    try:
        rows = run_async(_get())
        if rows is None:
            return jsonify({'success': False, 'error': "theme_assignments table doesn't exist; run hikaru migrations first"}), 400
        return jsonify({'success': True, 'assignments': rows})
    except Exception as e:
        logger.error("theme assignments failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/theme/templates/save', methods=['POST'])
def save_vnclub_theme_template():
    data = request.get_json(silent=True) or {}
    parsed = _validate_theme_template_payload(data)
    if parsed[0] is None:
        return jsonify({'success': False, 'error': parsed[1]}), 400
    guild_id, name, canonical = parsed
    rules_json = json.dumps(canonical)
    # Rules that validate but can never be satisfied: reported back so the
    # manager sees them at save time, not on the first blocked nomination.
    conflicts = rules_conflicts(canonical)
    template_id = data.get('id')  # None => insert, else update
    db_path = get_hikaru_db_path()
    if not db_path:
        return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

    async def _save():
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            if await _hikaru_table_missing(db, "theme_templates"):
                return {'success': False, 'error': "theme_templates table doesn't exist; run hikaru migrations first", 'code': 400}
            try:
                if template_id is None:
                    async with db.execute(
                        "INSERT INTO theme_templates (guild_id, name, rules_json, created_by_user_id) "
                        "VALUES (?, ?, ?, NULL)", (guild_id, name, rules_json)
                    ) as cur:
                        new_id = cur.lastrowid
                    await db.commit()
                    logger.info("console created theme template %r (guild=%s, id=%s)", name, guild_id, new_id)
                    return {'success': True, 'id': new_id, 'conflicts': conflicts,
                            'message': f'Created template {name}'}
                else:
                    async with db.execute(
                        "UPDATE theme_templates SET name = ?, rules_json = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND guild_id = ?", (name, rules_json, int(template_id), guild_id)
                    ) as cur:
                        updated = cur.rowcount > 0
                    await db.commit()
                    if not updated:
                        return {'success': False, 'error': 'Template not found (it may have been deleted).', 'code': 400}
                    logger.info("console updated theme template %r (guild=%s, id=%s)", name, guild_id, template_id)
                    return {'success': True, 'id': int(template_id), 'conflicts': conflicts,
                            'message': f'Updated template {name}'}
            except aiosqlite.IntegrityError:
                return {'success': False, 'error': f'A template named "{name}" already exists in this server.', 'code': 400}
    try:
        result = run_async(_save())
        code = result.pop('code', None)
        return (jsonify(result), code) if code else jsonify(result)
    except Exception as e:
        logger.error("save theme template failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/theme/templates/delete', methods=['POST'])
def delete_vnclub_theme_template():
    data = request.get_json(silent=True) or {}
    gid = str(data.get('guild_id') or '').strip()
    tid = data.get('id')
    db_path = get_hikaru_db_path()
    if not db_path:
        return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
    if not gid.isdigit() or tid is None:
        return jsonify({'success': False, 'error': 'guild_id and id required'}), 400

    async def _del():
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            if await _hikaru_table_missing(db, "theme_templates"):
                return False
            async with db.execute(
                "DELETE FROM theme_templates WHERE id = ? AND guild_id = ?", (int(tid), int(gid))
            ) as cur:
                deleted = cur.rowcount > 0
            await db.commit()
            return deleted
    try:
        deleted = run_async(_del())
        if deleted:
            logger.info("console deleted theme template id=%s (guild=%s)", tid, gid)
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        logger.error("delete theme template failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/theme/assignments/set', methods=['POST'])
def set_vnclub_theme_assignment():
    data = request.get_json(silent=True) or {}
    gid = str(data.get('guild_id') or '').strip()
    kind = (data.get('kind') or '').strip().lower()
    target_month = (data.get('target_month') or '').strip()
    template_id = data.get('template_id')
    label_override = (data.get('label') or '').strip()
    db_path = get_hikaru_db_path()
    if not db_path:
        return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
    if not gid.isdigit():
        return jsonify({'success': False, 'error': 'guild_id required'}), 400
    if kind not in ('monthly', 'seasonal'):
        return jsonify({'success': False, 'error': "kind must be monthly or seasonal"}), 400
    # The label heads the pool embed, whose page budget is what is left after
    # it. Hikaru's own assign modal caps the field at the same length.
    if len(label_override) > THEME_LABEL_MAX:
        return jsonify({'success': False,
                        'error': f'label must be {THEME_LABEL_MAX} characters or fewer'}), 400
    if not str(template_id or '').strip().isdigit():
        return jsonify({'success': False, 'error': 'pick a template to apply'}), 400
    window, err = _resolve_theme_period(kind, target_month)
    if window is None:
        return jsonify({'success': False, 'error': err}), 400
    start_month, end_month = window

    async def _set():
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            for tbl in ("theme_templates", "theme_assignments"):
                if await _hikaru_table_missing(db, tbl):
                    return {'success': False, 'error': f"{tbl} table doesn't exist; run hikaru migrations first", 'code': 400}
            # Snapshot the template's CURRENT rules onto the period.
            async with db.execute(
                "SELECT name, rules_json FROM theme_templates WHERE id = ? AND guild_id = ?",
                (int(template_id), int(gid))
            ) as cur:
                tpl = await cur.fetchone()
            if not tpl:
                return {'success': False, 'error': 'template not found', 'code': 400}
            tpl_name, stored_rules = tpl
            # The snapshot lands on a live gate, which validates on read: a
            # ruleset that no longer parses is rejected here instead of
            # blocking every nomination for the period.
            try:
                canonical = validate_rules(json.loads(stored_rules or '{}'))
            except (ValueError, TypeError) as e:
                return {'success': False, 'code': 400,
                        'error': f'Template "{tpl_name}" has unusable rules ({e}). Edit and re-save it first.'}
            rules_json = json.dumps(canonical)
            conflicts = rules_conflicts(canonical)
            label = label_override or tpl_name
            await db.execute(
                "INSERT INTO theme_assignments "
                "(guild_id, kind, start_month, end_month, label, rules_json, source_template_id, created_by_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(guild_id, kind, start_month, end_month) DO UPDATE SET "
                "label = excluded.label, rules_json = excluded.rules_json, "
                "source_template_id = excluded.source_template_id, updated_at = CURRENT_TIMESTAMP",
                (int(gid), kind, start_month, end_month, label, rules_json, int(template_id))
            )
            await db.commit()
            logger.info("console applied theme %r (template %s) to guild=%s %s %s..%s",
                        label, template_id, gid, kind, start_month, end_month)
            return {'success': True, 'conflicts': conflicts, 'template_name': tpl_name,
                    'message': f'Applied "{label}" to {kind} {start_month}..{end_month}'}
    try:
        result = run_async(_set())
        code = result.pop('code', None)
        return (jsonify(result), code) if code else jsonify(result)
    except Exception as e:
        logger.error("set theme assignment failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/theme/assignments/delete', methods=['POST'])
def delete_vnclub_theme_assignment():
    data = request.get_json(silent=True) or {}
    gid = str(data.get('guild_id') or '').strip()
    kind = (data.get('kind') or '').strip().lower()
    start_month = (data.get('start_month') or '').strip()
    end_month = (data.get('end_month') or '').strip()
    db_path = get_hikaru_db_path()
    if not db_path:
        return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})
    if not gid.isdigit() or kind not in ('monthly', 'seasonal') or not start_month or not end_month:
        return jsonify({'success': False, 'error': 'guild_id, kind, start_month, end_month required'}), 400
    # Both bounds reach a log line, so hold them to the same YYYY-MM shape the
    # rest of the theme endpoints resolve to.
    import re
    if not all(re.match(r"^\d{4}-\d{2}$", m) for m in (start_month, end_month)):
        return jsonify({'success': False, 'error': 'start_month and end_month must be YYYY-MM'}), 400

    async def _del():
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            if await _hikaru_table_missing(db, "theme_assignments"):
                return False
            async with db.execute(
                "DELETE FROM theme_assignments WHERE guild_id = ? AND kind = ? AND start_month = ? AND end_month = ?",
                (int(gid), kind, start_month, end_month)
            ) as cur:
                deleted = cur.rowcount > 0
            await db.commit()
            return deleted
    try:
        deleted = run_async(_del())
        if deleted:
            logger.info("console cleared theme for guild=%s %s %s..%s", gid, kind, start_month, end_month)
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        logger.error("delete theme assignment failed: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/vnclub/leaderboard')
def get_vnclub_leaderboard():
    """Get Hikaru leaderboard"""
    async def _get_leaderboard():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            month_filter = request.args.get('month', '').strip()
            guild_filter = request.args.get('guild', '').strip()
            limit = request.args.get('limit', 50, type=int)

            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row

                # Build query
                query = """
                    SELECT
                        user_id,
                        SUM(points) as total_points,
                        COUNT(*) as completion_count
                    FROM reading_logs
                """

                conditions = []
                params = []

                if month_filter:
                    conditions.append("reward_month = ?")
                    params.append(month_filter)

                if guild_filter:
                    conditions.append("CAST(logged_in_guild AS TEXT) = ?")
                    params.append(guild_filter)

                if conditions:
                    query += " WHERE " + " AND ".join(conditions)

                query += " GROUP BY user_id ORDER BY total_points DESC LIMIT ?"
                params.append(limit)

                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    leaderboard = [safe_json_row(dict(row)) for row in rows]

                # Try to resolve usernames (use cache only to avoid timeout)
                user_ids = [entry['user_id'] for entry in leaderboard]
                if user_ids and bot_instance and not bot_instance.is_closed():
                    try:
                        username_map = {}
                        for uid in user_ids:
                            uid_int = int(uid) if isinstance(uid, str) else uid

                            # First check the custom cache
                            if uid_int in username_cache:
                                cached = username_cache[uid_int]
                                if cached and cached not in ("NOT_FOUND", "FORBIDDEN"):
                                    username_map[str(uid)] = cached
                                    continue

                            # Then check discord.py's internal cache (guild members)
                            user = bot_instance.get_user(uid_int)
                            if user:
                                username = getattr(user, 'global_name', None) or user.name
                                username_map[str(uid)] = username
                                # Also add to the custom cache for future lookups
                                with username_cache_lock:
                                    username_cache[uid_int] = username

                        for entry in leaderboard:
                            user_id = entry.get('user_id')
                            if user_id:
                                entry['username'] = username_map.get(str(user_id), f'User {user_id}')
                    except Exception as e:
                        logger.debug(f"Could not resolve leaderboard usernames: {e}")
                        for entry in leaderboard:
                            entry['username'] = f"User {entry.get('user_id', 'Unknown')}"
                else:
                    for entry in leaderboard:
                        entry['username'] = f"User {entry.get('user_id', 'Unknown')}"

                return jsonify({
                    'success': True,
                    'leaderboard': leaderboard,
                    'month_filter': month_filter or 'All Time',
                    'guild_filter': guild_filter or 'All Servers'
                })
        except Exception as e:
            logger.error(f"Error fetching leaderboard: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_leaderboard())

@app.route('/api/vnclub/user_stats/<user_id>')
def get_vnclub_user_stats(user_id):
    """Get stats for a specific Hikaru user"""
    async def _get_user_stats():
        try:
            db_path = get_hikaru_db_path()
            if not db_path:
                return jsonify({'success': False, 'error': 'Hikaru database not configured or not found'})

            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row

                # Get user stats
                async with db.execute("""
                    SELECT
                        COUNT(*) as total_entries,
                        COALESCE(SUM(points), 0) as total_points,
                        COUNT(CASE WHEN reward_reason = 'As Monthly VN' THEN 1 END) as monthly_entries,
                        COUNT(DISTINCT vndb_id) as unique_vns
                    FROM reading_logs
                    WHERE user_id = ?
                """, (user_id,)) as cursor:
                    stats = dict(await cursor.fetchone())

                # Get recent activity
                async with db.execute("""
                    SELECT reward_month, vndb_id, points, reward_reason
                    FROM reading_logs
                    WHERE user_id = ?
                    ORDER BY reward_month DESC
                    LIMIT 10
                """, (user_id,)) as cursor:
                    recent = [safe_json_row(dict(row)) for row in await cursor.fetchall()]

                # Try to get username
                username = f"User {user_id}"
                if bot_instance and not bot_instance.is_closed():
                    try:
                        user_map = await fetch_multiple_users([int(user_id)])
                        username = user_map.get(str(user_id), username)
                    except:
                        pass

                return jsonify({
                    'success': True,
                    'user_id': user_id,
                    'username': username,
                    'stats': stats,
                    'recent_activity': recent
                })
        except Exception as e:
            logger.error(f"Error fetching user stats: {e}")
            return jsonify({'success': False, 'error': str(e)})
    return run_async(_get_user_stats())

# =============================================================================
# Config Editor API Endpoints
# =============================================================================

CONFIG_PATH = Path(__file__).parent / 'config.json'

def load_config():
    """Load the config.json file"""
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return None

def save_config(config_data):
    """Save the config.json file with backup"""
    try:
        # Create backup first
        if CONFIG_PATH.exists():
            backup_dir = Path(__file__).parent / 'data' / 'config_backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = backup_dir / f'config_backup_{timestamp}.json'
            shutil.copy2(CONFIG_PATH, backup_path)

            # Keep only last 20 config backups
            backups = sorted(backup_dir.glob('config_backup_*.json'), reverse=True)
            for old_backup in backups[20:]:
                old_backup.unlink()

        # Save new config
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)

        # The bot reads its config from memory, loaded once at startup, so writing the
        # file alone leaves the running process on the old values and a save appears to
        # do nothing until a restart. Doing it here means no caller can forget it.
        if bot_instance:
            bot_instance.config = config_data
        return True
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return False

def get_sub_bot_configs():
    """Load all sub-bot config files"""
    sub_bot_configs = {}
    sub_bots_dir = Path(__file__).parent / 'sub-bots'
    if sub_bots_dir.exists():
        for bot_dir in sub_bots_dir.iterdir():
            if bot_dir.is_dir():
                config_file = bot_dir / 'config.json'
                if config_file.exists():
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            sub_bot_configs[bot_dir.name] = {
                                'path': str(config_file),
                                'config': json.load(f)
                            }
                    except Exception as e:
                        logger.error(f"Error loading sub-bot config {config_file}: {e}")
    return sub_bot_configs

def get_guild_names():
    """Get guild names from the bot instance and sub-bot data"""
    guild_names = {}

    # Get from main bot instance
    if bot_instance and not bot_instance.is_closed():
        for guild in bot_instance.guilds:
            guild_names[str(guild.id)] = guild.name

    # Also get from sub-bot guild data file
    sub_bot_data_path = Path(__file__).parent / 'data' / 'sub_bot_guilds.json'
    if sub_bot_data_path.exists():
        try:
            with open(sub_bot_data_path, 'r', encoding='utf-8') as f:
                sub_bot_guilds = json.load(f)
                for bot_name, guilds in sub_bot_guilds.items():
                    for guild_data in guilds:
                        guild_id = str(guild_data.get('id', ''))
                        guild_name = guild_data.get('name', '')
                        if guild_id and guild_name and guild_id not in guild_names:
                            guild_names[guild_id] = guild_name
        except Exception as e:
            logger.warning(f"Failed to load sub-bot guild names: {e}")

    return guild_names

# Keys whose values are secrets: redacted in GET responses and preserved (not
# clobbered) when a redacted config is saved back.
_SECRET_KEY_PARTS = ("token", "secret", "password", "auth_token", "api_key", "apikey")
_REDACTED = "__REDACTED__"

# Config keys that must not be set through the console API: they drive server-side
# file reads or process spawning. Edit config.json directly if these ever need
# to change.
_FORBIDDEN_CONFIG_SEGMENTS = {
    "log_path", "db_path", "control", "local_start_command",
    "local_working_dir", "docker_container", "secret_key",
}


def _is_secret_key(key):
    k = str(key).lower()
    return any(p in k for p in _SECRET_KEY_PARTS)


def _redact_secrets(obj):
    """Replace secret leaf values with a sentinel so the config view never
    discloses live credentials (any key whose name contains a token, secret,
    password or API-key part)."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if (_is_secret_key(k) and isinstance(v, str) and v) else _redact_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_secrets(v) for v in obj]
    return obj


def _restore_redacted(incoming, existing):
    """Swap _REDACTED placeholders back to the real on-disk value so a redacted
    config round-tripped through save does not overwrite live secrets."""
    if isinstance(incoming, dict):
        existing = existing if isinstance(existing, dict) else {}
        out = {}
        for k, v in incoming.items():
            if v == _REDACTED:
                if k in existing:
                    out[k] = existing[k]
                # else drop the placeholder rather than persist it
            else:
                out[k] = _restore_redacted(v, existing.get(k))
        return out
    if isinstance(incoming, list):
        return [_restore_redacted(v, None) for v in incoming]
    return incoming


@app.route('/api/config')
def get_config():
    """Get the current config with guild names and sub-bot configs"""
    try:
        config = load_config()
        if config is None:
            return jsonify({'success': False, 'error': 'Failed to load config'})

        # Get guild names from bot instance
        guild_names = get_guild_names()

        # Load sub-bot configs
        sub_bot_configs = get_sub_bot_configs()

        # Convert all large integers to strings to prevent JavaScript precision loss
        # This is critical for Discord IDs which exceed JS MAX_SAFE_INTEGER
        return jsonify({
            'success': True,
            'config': safe_json_recursive(_redact_secrets(config)),
            'guild_names': guild_names,
            'sub_bot_configs': safe_json_recursive(_redact_secrets(sub_bot_configs))
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/save', methods=['POST'])
def save_config_endpoint():
    """Save the entire config"""
    try:
        data = request.json
        config = data.get('config')

        if config is None:
            return jsonify({'success': False, 'error': 'No config data provided'})

        # Validate JSON structure
        if not isinstance(config, dict):
            return jsonify({'success': False, 'error': 'Config must be a JSON object'})

        # The config view redacts secrets; swap any sentinels back to the real
        # on-disk values so saving the UI's view never clobbers live credentials.
        config = _restore_redacted(config, load_config() or {})

        if save_config(config):
            # Also update the bot's in-memory config so changes take effect immediately
            if bot_instance:
                bot_instance.config = config
            return jsonify({
                'success': True,
                'message': 'Config saved and applied successfully.'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to save config'})
    except json.JSONDecodeError as e:
        return jsonify({'success': False, 'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/update', methods=['POST'])
def update_config_partial():
    """Update specific config keys without replacing the entire config"""
    try:
        data = request.json
        path = data.get('path')  # e.g., "guilds.123456.welcome_channel"
        value = data.get('value')

        if not path:
            return jsonify({'success': False, 'error': 'Path is required'})

        config = load_config()
        if config is None:
            return jsonify({'success': False, 'error': 'Failed to load config'})

        # Navigate to the path and set the value
        keys = path.split('.')
        # Block keys that feed server-side file reads or process spawning.
        if any(k.strip().lower() in _FORBIDDEN_CONFIG_SEGMENTS for k in keys):
            return jsonify({'success': False, 'error': 'This config key cannot be edited from the console for security reasons; edit config.json directly.'})
        if value == _REDACTED:
            return jsonify({'success': False, 'error': 'Refusing to write a redacted placeholder value'})
        current = config
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]

        current[keys[-1]] = value

        if save_config(config):
            # Also update the bot's in-memory config so changes take effect immediately
            if bot_instance:
                bot_instance.config = config
            return jsonify({
                'success': True,
                'message': f'Updated {path} successfully'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to save config'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/delete', methods=['POST'])
def delete_config_key():
    """Delete a specific config key"""
    try:
        data = request.json
        path = data.get('path')

        if not path:
            return jsonify({'success': False, 'error': 'Path is required'})

        config = load_config()
        if config is None:
            return jsonify({'success': False, 'error': 'Failed to load config'})

        # Navigate to the parent and delete the key
        keys = path.split('.')
        current = config
        for key in keys[:-1]:
            if key not in current:
                return jsonify({'success': False, 'error': f'Path not found: {path}'})
            current = current[key]

        if keys[-1] not in current:
            return jsonify({'success': False, 'error': f'Key not found: {keys[-1]}'})

        del current[keys[-1]]

        if save_config(config):
            # Also update the bot's in-memory config so changes take effect immediately
            if bot_instance:
                bot_instance.config = config
            return jsonify({
                'success': True,
                'message': f'Deleted {path} successfully'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to save config'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/backups')
def get_config_backups():
    """Get list of config backups"""
    try:
        backup_dir = Path(__file__).parent / 'data' / 'config_backups'
        if not backup_dir.exists():
            return jsonify({'success': True, 'backups': []})

        backups = []
        for backup_file in sorted(backup_dir.glob('config_backup_*.json'), reverse=True):
            stat = backup_file.stat()
            backups.append({
                'filename': backup_file.name,
                'size_kb': round(stat.st_size / 1024, 2),
                'created': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            })

        return jsonify({
            'success': True,
            'backups': backups
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/restore', methods=['POST'])
def restore_config_backup():
    """Restore a config backup"""
    try:
        data = request.json
        filename = data.get('filename')

        if not filename:
            return jsonify({'success': False, 'error': 'Filename is required'})

        backup_dir = Path(__file__).parent / 'data' / 'config_backups'
        # Confine to the config-backups dir (reject ../ and absolute paths).
        backup_path = (backup_dir / filename).resolve()
        if backup_path.parent != backup_dir.resolve() or backup_path.suffix.lower() != '.json':
            return jsonify({'success': False, 'error': 'Invalid backup filename'})

        if not backup_path.exists():
            return jsonify({'success': False, 'error': 'Backup file not found'})

        # Load the backup to validate it
        with open(backup_path, 'r', encoding='utf-8') as f:
            backup_config = json.load(f)

        # Save current config as backup first, then restore
        if save_config(backup_config):
            # Also update the bot's in-memory config so changes take effect immediately
            if bot_instance:
                bot_instance.config = backup_config
            return jsonify({
                'success': True,
                'message': f'Restored config from {filename} and applied.'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to restore config'})
    except json.JSONDecodeError:
        return jsonify({'success': False, 'error': 'Backup file contains invalid JSON'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/add-guild', methods=['POST'])
def add_guild_config():
    """Add a new guild configuration with default values"""
    try:
        data = request.json
        guild_id = data.get('guild_id')

        if not guild_id:
            return jsonify({'success': False, 'error': 'Guild ID is required'})

        # Validate guild ID format
        try:
            int(guild_id)
        except ValueError:
            return jsonify({'success': False, 'error': 'Guild ID must be a number'})

        config = load_config()
        if config is None:
            return jsonify({'success': False, 'error': 'Failed to load config'})

        if 'guilds' not in config:
            config['guilds'] = {}

        if str(guild_id) in config['guilds']:
            return jsonify({'success': False, 'error': 'Guild already exists in config'})

        # Add default guild config
        config['guilds'][str(guild_id)] = {
            'welcome_channel': None,
            'news_channel': None,
            'verification_enabled': False,
            'verification_channel': None,
            'highlights_enabled': False,
            'highlights_channel': None,
            'highlights_threshold': 3
        }

        if save_config(config):
            # Also update the bot's in-memory config so changes take effect immediately
            if bot_instance:
                bot_instance.config = config
            return jsonify({
                'success': True,
                'message': f'Added guild {guild_id} with default settings'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to save config'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/add-external-db', methods=['POST'])
def add_external_db():
    """Add a new external database configuration"""
    try:
        data = request.json
        name = data.get('name', '').strip().lower().replace(' ', '_')
        db_path = data.get('db_path', '')
        log_path = data.get('log_path', '')
        enabled = data.get('enabled', True)

        if not name:
            return jsonify({'success': False, 'error': 'Database name is required'})

        if not db_path:
            return jsonify({'success': False, 'error': 'Database path is required'})

        config = load_config()
        if config is None:
            return jsonify({'success': False, 'error': 'Failed to load config'})

        # Ensure external_databases section exists
        if 'external_databases' not in config:
            config['external_databases'] = {}

        # Check if already exists
        if name in config['external_databases']:
            return jsonify({'success': False, 'error': f'External database "{name}" already exists'})

        # Add the new external database
        db_config = {
            'enabled': enabled,
            'db_path': db_path
        }
        if log_path:
            db_config['log_path'] = log_path

        config['external_databases'][name] = db_config

        if save_config(config):
            # Also update the bot's in-memory config so changes take effect immediately
            if bot_instance:
                bot_instance.config = config
            return jsonify({
                'success': True,
                'message': f'External database "{name}" added successfully'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to save config'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/config/sub-bot/save', methods=['POST'])
def save_sub_bot_config():
    """Save a sub-bot config file"""
    try:
        data = request.json
        bot_name = data.get('bot_name')
        config = data.get('config')

        if not bot_name or config is None:
            return jsonify({'success': False, 'error': 'bot_name and config are required'})

        # bot_name becomes a directory segment; reject separators / dots so the
        # resolved path stays a config.json directly inside sub-bots/.
        if '/' in bot_name or '\\' in bot_name or '.' in bot_name:
            return jsonify({'success': False, 'error': 'Invalid bot_name'})

        sub_bots_dir = Path(__file__).parent / 'sub-bots'
        config_path = (sub_bots_dir / bot_name / 'config.json').resolve()
        if config_path.parent.parent != sub_bots_dir.resolve():
            return jsonify({'success': False, 'error': 'Invalid bot_name'})

        if not config_path.exists():
            return jsonify({'success': False, 'error': f'Sub-bot config not found: {bot_name}'})

        # Restore redacted secret sentinels against the on-disk values (mirrors
        # /api/config/save) so the console's redacted view can't overwrite a real secret.
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                existing_sub = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing_sub = {}
        config = _restore_redacted(config, existing_sub if isinstance(existing_sub, dict) else {})

        # Create backup first
        backup_dir = Path(__file__).parent / 'data' / 'config_backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = backup_dir / f'{bot_name}_config_backup_{timestamp}.json'
        shutil.copy2(config_path, backup_path)

        # Save new config
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

        return jsonify({
            'success': True,
            'message': f'Saved {bot_name} config. Restart required for changes to take effect.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ---------------------------------------------------------------------------
# Message purge API (drives utils.purge_service.PurgeJobManager on the bot loop)
# ---------------------------------------------------------------------------

def _get_purge_manager():
    return getattr(bot_instance, 'purge_manager', None) if bot_instance else None


def _schedule_purge_coro(coro):
    """Fire-and-forget a coroutine on the bot loop, logging any exception."""
    async def _wrapped():
        try:
            await coro
        except Exception as e:
            logger.error(f"Purge console task failed: {e}")

    if bot_instance and not bot_instance.is_closed() and getattr(bot_instance, 'loop', None):
        asyncio.run_coroutine_threadsafe(_wrapped(), bot_instance.loop)
        return True
    coro.close()
    return False


@app.route('/api/purge/status')
def purge_status():
    """Current/last purge job snapshot."""
    mgr = _get_purge_manager()
    if not mgr:
        return jsonify({'success': False, 'error': 'Purge engine not available (bot offline?)'})
    return jsonify({'success': True, 'job': safe_json_recursive(mgr.get_status())})


@app.route('/api/purge/dry_run', methods=['POST'])
def purge_dry_run():
    """Start a dry-run scan. Returns immediately; poll /api/purge/status for progress."""
    import re
    from utils.purge_service import PurgeFilters, parse_purge_date, parse_channel_ids

    mgr = _get_purge_manager()
    if not mgr:
        return jsonify({'success': False, 'error': 'Purge engine not available (bot offline?)'})
    if mgr.is_busy():
        return jsonify({'success': False, 'error': 'A purge job is already running.'})

    data = request.json or {}
    # target_user_id is optional: a purge can be scoped by channel/date/content
    # alone (mirrors the /manage_purge slash command). Boundedness is enforced below.
    target_raw = str(data.get('target_user_id', '')).strip()
    target_user_id = None
    if target_raw:
        try:
            target_user_id = int(target_raw)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'target_user_id must be numeric.'})
    directed_raw = str(data.get('directed_at', '')).strip()
    directed_at_user_id = None
    if directed_raw:
        try:
            directed_at_user_id = int(directed_raw)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'directed_at must be numeric.'})
    try:
        guild_id = int(str(data.get('guild_id', '')).strip())
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'A numeric guild_id is required.'})

    channel_id = str(data.get('channel_id', '')).strip()
    scope = int(channel_id) if channel_id.isdigit() else 'guild'

    # Cap regex length: this pattern is applied per message, so an overlong
    # pattern can make matching needlessly slow.
    regex_pattern = data.get('regex') or None
    if regex_pattern is not None and len(str(regex_pattern)) > 200:
        return jsonify({'success': False, 'error': 'Regex is too long (max 200 characters).'})

    try:
        filters = PurgeFilters(
            target_user_id=target_user_id,
            directed_at_user_id=directed_at_user_id,
            contains=(data.get('contains') or None),
            regex=regex_pattern,
            has_link=bool(data.get('has_link')),
            has_file=bool(data.get('has_file')),
            include_pinned=bool(data.get('include_pinned')),
            after=parse_purge_date(data.get('after')),
            before=parse_purge_date(data.get('before')),
            include_nsfw=bool(data.get('include_nsfw', True)),
            oldest_first=bool(data.get('oldest_first')),
            excluded_channel_ids=parse_channel_ids(data.get('exclude_channels')),
            delete_private_threads=bool(data.get('delete_private_threads', True)),
            skip_private_threads=bool(data.get('skip_private_threads')),
        )
    except re.error as e:
        return jsonify({'success': False, 'error': f'Invalid regex: {e}'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)})

    from utils.purge_service import purge_is_bounded
    if not purge_is_bounded(scope, filters):
        return jsonify({'success': False, 'error': (
            'Add at least one filter so the purge is bounded: a channel, a user, '
            'a date range, or a content filter.')})

    scheduled = _schedule_purge_coro(mgr.run_dry_run(
        scope=scope, guild_id=guild_id, filters=filters,
        requested_by=0, requested_via='console',
    ))
    if not scheduled:
        return jsonify({'success': False, 'error': 'Bot loop unavailable.'})
    return jsonify({'success': True, 'message': 'Dry-run started. Poll status for results.'})


@app.route('/api/purge/confirm', methods=['POST'])
def purge_confirm():
    """Confirm the pending preview and start deletion in the background."""
    mgr = _get_purge_manager()
    if not mgr:
        return jsonify({'success': False, 'error': 'Purge engine not available (bot offline?)'})
    job_id = (request.json or {}).get('job_id')
    if not mgr.can_confirm(job_id):
        return jsonify({'success': False, 'error': 'No confirmable preview (expired or already running?).'})
    scheduled = _schedule_purge_coro(mgr.run_delete(job_id))
    if not scheduled:
        return jsonify({'success': False, 'error': 'Bot loop unavailable.'})
    return jsonify({'success': True, 'message': 'Deletion started.'})


@app.route('/api/purge/cancel', methods=['POST'])
def purge_cancel():
    """Cancel the active purge (abandon preview, or stop a running scan/delete)."""
    from utils.purge_service import PurgeStatus

    mgr = _get_purge_manager()
    if not mgr:
        return jsonify({'success': False, 'error': 'Purge engine not available (bot offline?)'})
    status = mgr.get_status()
    job_id = status.get('id')
    state = status.get('status')
    if state == PurgeStatus.AWAITING:
        ok = mgr.abandon(job_id)
    elif state in (PurgeStatus.SCANNING, PurgeStatus.DELETING):
        ok = mgr.cancel(job_id)
    else:
        ok = False
    return jsonify({'success': ok, 'message': 'Cancellation requested.' if ok else 'Nothing running to cancel.'})


def run_web_console(host=None, port=PORT):
    """Start the web console server"""
    # Local only unless WEB_CONSOLE_HOST says otherwise; the Docker setup sets it so
    # the published port can reach the container.
    host = host or os.getenv("WEB_CONSOLE_HOST", "127.0.0.1")
    # Fail closed: without a password the console would be an unauthenticated
    # admin panel, so don't serve at all. The bot keeps running regardless.
    if not WEB_CONSOLE_PASSWORD:
        print("⚠️  Web console disabled: set WEB_CONSOLE_PASSWORD in .env to enable it.")
        logger.warning("Web console disabled: WEB_CONSOLE_PASSWORD is not set.")
        return

    setup_logging()

    # Create templates directory if it doesn't exist
    templates_dir = Path(__file__).parent / 'templates'
    templates_dir.mkdir(exist_ok=True)
    
    # Create the HTML template if it doesn't exist
    create_console_template(templates_dir)
    
    print(f"🌐 Web Console starting at http://{host}:{port}")
    print(f"📊 Access the dashboard at http://localhost:{port}")
    
    # Disable Flask's default logging to reduce noise
    import logging as flask_logging
    log = flask_logging.getLogger('werkzeug')
    log.setLevel(flask_logging.ERROR)
    
    # Allow port reuse to handle restarts better
    import socket
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

def create_console_template(templates_dir):
    """Create the HTML template for the console"""
    template_path = templates_dir / 'console.html'
    
    if not template_path.exists():
        html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Muramasa Web Console</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 20px;
        }
        .header {
            background: #2d2d30;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { color: #5cb3ff; }
        .stats {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: #2d2d30;
            padding: 15px 20px;
            border-radius: 8px;
            flex: 1;
            border-left: 4px solid #5cb3ff;
        }
        .stat-card h3 { color: #858585; font-size: 12px; margin-bottom: 5px; }
        .stat-card .value { font-size: 24px; font-weight: bold; color: #5cb3ff; }
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        .tab {
            background: #2d2d30;
            padding: 12px 24px;
            border-radius: 8px 8px 0 0;
            cursor: pointer;
            transition: background 0.3s;
        }
        .tab:hover { background: #3e3e42; }
        .tab.active { background: #007acc; color: white; }
        .content {
            background: #2d2d30;
            padding: 20px;
            border-radius: 0 8px 8px 8px;
            min-height: 400px;
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        #logs {
            background: #1e1e1e;
            border: 1px solid #3e3e42;
            border-radius: 4px;
            padding: 15px;
            max-height: 600px;
            overflow-y: auto;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 13px;
        }
        .log-entry {
            padding: 4px 0;
            border-bottom: 1px solid #2d2d30;
        }
        .log-entry:last-child { border-bottom: none; }
        .log-timestamp { color: #858585; }
        .log-level { 
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: bold;
            margin: 0 8px;
        }
        .log-level.INFO { background: #264f78; color: #4fc3f7; }
        .log-level.WARNING { background: #5a5a2d; color: #ffd54f; }
        .log-level.ERROR { background: #5a2d2d; color: #ff6b6b; }
        .log-level.DEBUG { background: #2d3e50; color: #95a5a6; }
        .log-message { color: #d4d4d4; }
        .btn {
            background: #007acc;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            transition: background 0.3s;
        }
        .btn:hover { background: #005a9e; }
        .btn-secondary {
            background: #3e3e42;
        }
        .btn-secondary:hover { background: #505050; }
        textarea, input, select {
            background: #1e1e1e;
            border: 1px solid #3e3e42;
            color: #d4d4d4;
            padding: 10px;
            border-radius: 4px;
            width: 100%;
            margin: 10px 0;
            font-family: 'Consolas', monospace;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }
        th, td {
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #3e3e42;
        }
        th {
            background: #1e1e1e;
            font-weight: bold;
            color: #5cb3ff;
        }
        tr:hover { background: #3e3e42; }
        .controls {
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
        }
        #queryResult {
            margin-top: 15px;
            max-height: 500px;
            overflow: auto;
        }
        .success { color: #4caf50; }
        .error { color: #f44336; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 Muramasa Web Console</h1>
        <button class="btn" onclick="refreshAll()">🔄 Refresh All</button>
    </div>

    <div class="stats" id="stats">
        <div class="stat-card">
            <h3>VN POOL ENTRIES</h3>
            <div class="value" id="stat-vn_pool">-</div>
        </div>
        <div class="stat-card">
            <h3>USER COMPLETIONS</h3>
            <div class="value" id="stat-user_completions">-</div>
        </div>
        <div class="stat-card">
            <h3>IMMERSION ACTIVITIES</h3>
            <div class="value" id="stat-immersion_activities">-</div>
        </div>
        <div class="stat-card">
            <h3>DATABASE SIZE</h3>
            <div class="value" id="stat-db_size">-</div>
        </div>
    </div>

    <div class="tabs">
        <div class="tab active" onclick="switchTab('logs')">📋 Logs</div>
        <div class="tab" onclick="switchTab('database')">🗄️ Database</div>
        <div class="tab" onclick="switchTab('query')">⚡ Query</div>
    </div>

    <div class="content">
        <div id="logs-content" class="tab-content active">
            <div class="controls">
                <button class="btn" onclick="loadLogs()">🔄 Refresh Logs</button>
                <button class="btn btn-secondary" onclick="clearLogsDisplay()">🗑️ Clear Display</button>
            </div>
            <div id="logs"></div>
        </div>

        <div id="database-content" class="tab-content">
            <div class="controls">
                <select id="tableSelect" onchange="loadTableData()">
                    <option value="">Select a table...</option>
                </select>
                <button class="btn" onclick="createBackup()">💾 Create Backup</button>
                <button class="btn btn-secondary" onclick="showBackupManager()">📁 Load Backup</button>
            </div>
            <div id="backupManager" style="display: none; margin: 20px 0; padding: 20px; background: #1e1e1e; border-radius: 8px;">
                <h3>Backup Manager</h3>
                <div id="backupList"></div>
                <button class="btn btn-secondary" onclick="hideBackupManager()">Cancel</button>
            </div>
            <div id="tableData"></div>
        </div>

        <div id="query-content" class="tab-content">
            <h3>Execute Query (SELECT only)</h3>
            <textarea id="queryInput" rows="6" placeholder="SELECT * FROM vn_pool LIMIT 10;"></textarea>
            <button class="btn" onclick="executeQuery()">▶️ Execute Query</button>
            <div id="queryResult"></div>
        </div>
    </div>

    <script>
        let logAutoRefresh = null;

        function switchTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            
            event.target.classList.add('active');
            document.getElementById(tab + '-content').classList.add('active');

            if (tab === 'logs') {
                loadLogs();
                startLogAutoRefresh();
            } else {
                stopLogAutoRefresh();
            }

            if (tab === 'database') {
                loadTables();
            }
        }

        function startLogAutoRefresh() {
            if (!logAutoRefresh) {
                logAutoRefresh = setInterval(loadLogs, 2000);
            }
        }

        function stopLogAutoRefresh() {
            if (logAutoRefresh) {
                clearInterval(logAutoRefresh);
                logAutoRefresh = null;
            }
        }

        async function loadLogs() {
            try {
                const response = await fetch('/api/logs?limit=100');
                const logs = await response.json();
                
                const logsDiv = document.getElementById('logs');
                logsDiv.innerHTML = logs.map(log => `
                    <div class="log-entry">
                        <span class="log-timestamp">[${log.timestamp}]</span>
                        <span class="log-level ${log.level}">${log.level}</span>
                        <span class="log-message">${escapeHtml(log.message)}</span>
                    </div>
                `).join('');
                
                logsDiv.scrollTop = logsDiv.scrollHeight;
            } catch (error) {
                console.error('Error loading logs:', error);
            }
        }

        function clearLogsDisplay() {
            document.getElementById('logs').innerHTML = '<p style="color: #858585;">Logs cleared. New logs will appear here...</p>';
        }

        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const result = await response.json();
                
                if (result.success) {
                    document.getElementById('stat-vn_pool').textContent = result.stats.vn_pool || 0;
                    document.getElementById('stat-user_completions').textContent = result.stats.user_completions || 0;
                    document.getElementById('stat-immersion_activities').textContent = result.stats.immersion_activities || 0;
                    document.getElementById('stat-db_size').textContent = result.stats.db_size_mb + ' MB';
                }
            } catch (error) {
                console.error('Error loading stats:', error);
            }
        }

        async function loadTables() {
            try {
                const response = await fetch('/api/db/tables');
                const result = await response.json();
                
                if (result.success) {
                    const select = document.getElementById('tableSelect');
                    select.innerHTML = '<option value="">Select a table...</option>' +
                        result.tables.map(t => `<option value="${t}">${t}</option>`).join('');
                }
            } catch (error) {
                console.error('Error loading tables:', error);
            }
        }

        async function loadTableData() {
            const tableName = document.getElementById('tableSelect').value;
            if (!tableName) return;

            try {
                const response = await fetch(`/api/db/table/${tableName}`);
                const result = await response.json();
                
                if (result.success) {
                    let html = `<p>Showing ${result.data.length} of ${result.total} rows</p>`;
                    html += '<table><thead><tr>';
                    result.columns.forEach(col => html += `<th>${col}</th>`);
                    html += '</tr></thead><tbody>';
                    
                    result.data.forEach(row => {
                        html += '<tr>';
                        result.columns.forEach(col => {
                            let value = row[col];
                            if (value === null) value = '<i>NULL</i>';
                            else if (typeof value === 'string' && value.length > 100) {
                                value = value.substring(0, 100) + '...';
                            }
                            html += `<td>${escapeHtml(String(value))}</td>`;
                        });
                        html += '</tr>';
                    });
                    
                    html += '</tbody></table>';
                    document.getElementById('tableData').innerHTML = html;
                } else {
                    document.getElementById('tableData').innerHTML = `<p class="error">Error: ${result.error}</p>`;
                }
            } catch (error) {
                document.getElementById('tableData').innerHTML = `<p class="error">Error: ${error.message}</p>`;
            }
        }

        async function executeQuery() {
            const query = document.getElementById('queryInput').value.trim();
            if (!query) return;

            try {
                const response = await fetch('/api/db/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query })
                });
                const result = await response.json();
                
                const resultDiv = document.getElementById('queryResult');
                
                if (result.success) {
                    let html = `<p class="success">✓ Query executed successfully (${result.row_count} rows)</p>`;
                    
                    if (result.data.length > 0) {
                        html += '<table><thead><tr>';
                        result.columns.forEach(col => html += `<th>${col}</th>`);
                        html += '</tr></thead><tbody>';
                        
                        result.data.forEach(row => {
                            html += '<tr>';
                            result.columns.forEach(col => {
                                let value = row[col];
                                if (value === null) value = '<i>NULL</i>';
                                html += `<td>${escapeHtml(String(value))}</td>`;
                            });
                            html += '</tr>';
                        });
                        
                        html += '</tbody></table>';
                    }
                    
                    resultDiv.innerHTML = html;
                } else {
                    resultDiv.innerHTML = `<p class="error">✗ Error: ${result.error}</p>`;
                }
            } catch (error) {
                document.getElementById('queryResult').innerHTML = `<p class="error">✗ Error: ${error.message}</p>`;
            }
        }

        async function createBackup() {
            if (!confirm('Create a database backup?')) return;

            try {
                const response = await fetch('/api/db/backup', { method: 'POST' });
                const result = await response.json();
                
                if (result.success) {
                    alert('✓ ' + result.message);
                    if (document.getElementById('backupManager').style.display !== 'none') {
                        loadBackups(); // Refresh backup list if manager is open
                    }
                } else {
                    alert('✗ Error: ' + result.error);
                }
            } catch (error) {
                alert('✗ Error: ' + error.message);
            }
        }

        async function showBackupManager() {
            document.getElementById('backupManager').style.display = 'block';
            await loadBackups();
        }

        function hideBackupManager() {
            document.getElementById('backupManager').style.display = 'none';
        }

        async function loadBackups() {
            try {
                const response = await fetch('/api/db/backups');
                const result = await response.json();
                
                const backupListDiv = document.getElementById('backupList');
                
                if (result.success && result.backups.length > 0) {
                    let html = '<table style="margin: 15px 0;"><thead><tr><th>Filename</th><th>Size</th><th>Created</th><th>Actions</th></tr></thead><tbody>';
                    
                    result.backups.forEach(backup => {
                        html += `<tr>
                            <td>${escapeHtml(backup.filename)}</td>
                            <td>${backup.size_mb} MB</td>
                            <td>${backup.created}</td>
                            <td>
                                <button class="btn" style="padding: 5px 10px; font-size: 12px;" onclick="restoreBackup('${backup.filename}')">🔄 Restore</button>
                            </td>
                        </tr>`;
                    });
                    
                    html += '</tbody></table>';
                    backupListDiv.innerHTML = html;
                } else if (result.success) {
                    backupListDiv.innerHTML = '<p style="color: #858585; margin: 15px 0;">No backup files found.</p>';
                } else {
                    backupListDiv.innerHTML = `<p class="error">Error loading backups: ${result.error}</p>`;
                }
            } catch (error) {
                document.getElementById('backupList').innerHTML = `<p class="error">Error: ${error.message}</p>`;
            }
        }

        async function restoreBackup(filename) {
            if (!confirm(`⚠️ WARNING: This will replace the current database with the backup "${filename}".\n\nThe current database will be backed up before restoration.\n\nAre you sure you want to continue?`)) {
                return;
            }

            try {
                const response = await fetch('/api/db/restore', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename })
                });
                const result = await response.json();
                
                if (result.success) {
                    alert('✓ ' + result.message);
                    hideBackupManager();
                    refreshAll(); // Refresh all data after restore
                } else {
                    alert('✗ Error: ' + result.error);
                }
            } catch (error) {
                alert('✗ Error: ' + error.message);
            }
        }

        function refreshAll() {
            loadStats();
            loadLogs();
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text == null ? '' : String(text);
            // Also escape quotes so the result is safe in quoted HTML
            // attributes, not just in element text.
            return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        // Initialize
        loadStats();
        loadLogs();
        startLogAutoRefresh();
        setInterval(loadStats, 10000); // Refresh stats every 10 seconds
    </script>
</body>
</html>"""
        template_path.write_text(html_content, encoding='utf-8')

if __name__ == '__main__':
    # Try to get bot instance if running standalone
    try:
        import main
        if hasattr(main, 'bot') and main.bot:
            bot_instance = main.bot
            logger.info("Successfully connected to bot instance for username resolution")
    except Exception as e:
        logger.warning(f"Could not connect to bot instance: {e}")
        logger.info("Username resolution will show User IDs instead of usernames")
    
    run_web_console()

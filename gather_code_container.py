import asyncio
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import discord
import socketio


CHANNEL_ID = 1568728866803941416
# How often to poll for new messages (seconds).
POLL_INTERVAL = 1
# Optional: set to a specific message ID to check once and exit.
MESSAGE_ID = None
# DonutNova redemption endpoint (confirmed from site bundle: POST {"code": "..."}).
REDEEM_URL = "https://donutnova.com/api/codes/redeem"
USER_URL = "https://donutnova.com/api/user"
WITHDRAW_URL = "https://donutnova.com/api/withdraw"
AUTO_WITHDRAW_ON_BALANCE_INCREASE = True
MIN_WITHDRAW_AMOUNT = 100000
WITHDRAW_CHECK_INTERVAL = 300  # seconds
WITHDRAW_TOTAL_FILE = Path(__file__).with_name("withdraw_total.json")
# Word list for unscrambling DonutNova chat minigames.
WORDS_PATH = Path(__file__).with_name("words_alpha.txt")
EXTRA_UNSCRAMBLE_WORDS = ["donutnova", "pickaxe"]
# Human-like unscramble behavior.
UNSCRAMBLE_SKIP_CHANCE = 0.33
UNSCRAMBLE_DELAY_MIN = 1.4
UNSCRAMBLE_DELAY_MAX = 1.4


_WORD_INDEX: dict[str, list[str]] | None = None


def load_word_index() -> dict[str, list[str]]:
    global _WORD_INDEX
    if _WORD_INDEX is not None:
        return _WORD_INDEX

    index: dict[str, list[str]] = {}
    if WORDS_PATH.exists():
        with WORDS_PATH.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                word = line.strip().lower()
                if not word.isalpha():
                    continue
                key = "".join(sorted(word))
                index.setdefault(key, []).append(word)

    for word in EXTRA_UNSCRAMBLE_WORDS:
        cleaned = re.sub(r"[^A-Za-z]", "", word).lower()
        if not cleaned:
            continue
        key = "".join(sorted(cleaned))
        index.setdefault(key, [])
        if cleaned not in index[key]:
            index[key].insert(0, cleaned)

    _WORD_INDEX = index
    return index


def unscramble_word(scrambled: str) -> str | None:
    letters = re.sub(r"[^A-Za-z]", "", scrambled).lower()
    if not letters:
        return None
    index = load_word_index()
    matches = index.get("".join(sorted(letters)), [])
    if not matches:
        return None
    # Prefer exact same length matches (already guaranteed) and common short output.
    return matches[0]


def parse_minigame_reward(content: str) -> float | None:
    m = re.search(r"wins\s+([0-9][0-9,]*\.?[0-9]*)\s*([KMB])?", content, re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").upper()
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return num * mult


def extract_code_blocks(text: str | None) -> list[str]:
    """Return fenced code block contents from markdown text."""
    if not text:
        return []

    blocks: list[str] = []
    for m in re.finditer(r"```(?:([A-Za-z0-9_+-]+)\n)?([\s\S]*?)```", text):
        content = m.group(2)
        if content.strip():
            blocks.append(content.strip("\n"))
    return blocks


def walk_components(components) -> list[dict]:
    """Flatten nested component trees into a list of dict-like items."""
    flat = []
    for comp in components or []:
        if isinstance(comp, dict):
            data = comp
            children = data.get("components") or data.get("children") or []
        else:
            data = getattr(comp, "to_dict", lambda: {"repr": repr(comp)})()
            children = getattr(comp, "children", None) or getattr(comp, "components", None) or []

        flat.append(data)
        flat.extend(walk_components(children))

        accessory = data.get("accessory") if isinstance(data, dict) else getattr(comp, "accessory", None)
        if accessory:
            flat.extend(walk_components([accessory]))

    return flat


def get_donutnova_cookies() -> list[str]:
    raw = os.environ.get("DONUTNOVA_COOKIES", "")
    cookies: list[str] = []
    if raw.strip():
        for part in re.split(r"[\n|]+", raw):
            c = part.strip()
            if c:
                cookies.append(c)
    if not cookies:
        raw_single = os.environ.get("DONUTNOVA_COOKIE", "")
        if raw_single.strip():
            for part in re.split(r"[\n|]+", raw_single):
                c = part.strip()
                if c:
                    cookies.append(c)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in cookies:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def donutnova_headers(cookie: str | None = None) -> dict[str, str] | None:
    token = os.environ.get("DONUTNOVA_TOKEN")
    if cookie is None:
        cookies = get_donutnova_cookies()
        cookie = cookies[0] if cookies else None
    if not token and not cookie:
        return None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    return headers


def get_donutnova_user(cookie: str | None = None) -> dict:
    headers = donutnova_headers(cookie=cookie)
    if not headers:
        return {"ok": False, "error": "Missing DONUTNOVA_TOKEN/DONUTNOVA_COOKIE/DONUTNOVA_COOKIES"}
    req = urllib.request.Request(USER_URL, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {"ok": True, "payload": payload}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return {"ok": False, "status": e.code, "payload": payload}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_withdraw_recipient() -> str:
    return (
        os.environ.get("DONUTNOVA_WITHDRAW_RECIPIENT")
        or os.environ.get("WITHDRAW_RECIPIENT")
        or "ignCocktail"
    ).strip() or "ignCocktail"


def withdraw_amount(username: str, recipient: str, amount: float, cookie: str | None = None) -> dict:
    headers = donutnova_headers(cookie=cookie)
    if not headers:
        return {"ok": False, "error": "Missing DONUTNOVA_TOKEN/DONUTNOVA_COOKIE/DONUTNOVA_COOKIES"}
    body = json.dumps({"username": username, "recipient": recipient, "amount": amount}).encode("utf-8")
    req = urllib.request.Request(WITHDRAW_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {"ok": True, "status": resp.status, "payload": payload}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return {"ok": False, "status": e.code, "payload": payload}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_ACCOUNT_NAMES: dict[str, str] = {}
_ACCOUNT_NAMES_LOCK = threading.Lock()


def get_account_name(cookie: str | None, fallback: str = "account") -> str:
    if not cookie:
        return fallback
    with _ACCOUNT_NAMES_LOCK:
        cached = _ACCOUNT_NAMES.get(cookie)
    if cached:
        return cached
    user = get_donutnova_user(cookie=cookie)
    name = fallback
    if user.get("ok"):
        n = str(user.get("payload", {}).get("username", "")).strip()
        if n:
            name = n
    with _ACCOUNT_NAMES_LOCK:
        _ACCOUNT_NAMES[cookie] = name
    return name


_WITHDRAW_LOCKS: dict[str, threading.Lock] = {}
_WITHDRAW_LOCKS_GUARD = threading.Lock()
_LAST_KNOWN_BALANCE_BY_COOKIE: dict[str, float | None] = {}
def _load_withdraw_total() -> float:
    try:
        data = json.loads(WITHDRAW_TOTAL_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return float(data.get("total", 0) or 0)
        return float(data)
    except FileNotFoundError:
        return 0.0
    except Exception:
        return 0.0


def _save_withdraw_total(total: float) -> None:
    tmp = WITHDRAW_TOTAL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"total": total}, indent=2), encoding="utf-8")
    tmp.replace(WITHDRAW_TOTAL_FILE)


_TOTAL_WITHDRAWN = _load_withdraw_total()
_TOTAL_WITHDRAWN_LOCK = threading.Lock()


def _withdraw_lock_for(cookie: str) -> threading.Lock:
    with _WITHDRAW_LOCKS_GUARD:
        lock = _WITHDRAW_LOCKS.get(cookie)
        if lock is None:
            lock = threading.Lock()
            _WITHDRAW_LOCKS[cookie] = lock
        return lock


def auto_withdraw_if_increased(cookie: str | None = None) -> None:
    global _TOTAL_WITHDRAWN
    if not AUTO_WITHDRAW_ON_BALANCE_INCREASE:
        return
    cookies = [cookie] if cookie else get_donutnova_cookies()
    if not cookies:
        return
    if cookie is None and len(cookies) > 1:
        for c in cookies:
            threading.Thread(target=auto_withdraw_if_increased, args=(c,), daemon=True).start()
        return

    cookie = cookies[0]
    lock = _withdraw_lock_for(cookie)
    if not lock.acquire(blocking=False):
        return
    try:
        user = get_donutnova_user(cookie=cookie)
        if not user.get("ok"):
            print(f"[withdraw] user fetch failed: {user}", flush=True)
            return
        payload = user["payload"]
        username = str(payload.get("username", "")).strip()
        balance = float(payload.get("balance", 0) or 0)

        prev = _LAST_KNOWN_BALANCE_BY_COOKIE.get(cookie)
        _LAST_KNOWN_BALANCE_BY_COOKIE[cookie] = balance

        if prev is None:
            return
        if balance <= prev:
            return
        if balance < MIN_WITHDRAW_AMOUNT:
            return
        if not username:
            print("[withdraw] missing username", flush=True)
            return

        recipient = get_withdraw_recipient()
        result = withdraw_amount(username, recipient, balance, cookie=cookie)
        if result.get("ok"):
            withdrawn = float(result.get("payload", {}).get("amount", balance) or 0)
            with _TOTAL_WITHDRAWN_LOCK:
                _TOTAL_WITHDRAWN += withdrawn
                total = _TOTAL_WITHDRAWN
                try:
                    _save_withdraw_total(total)
                except Exception as e:
                    print(f"[withdraw] failed to save total: {e}", flush=True)
            print(f"[withdraw:{username}] {withdrawn:.0f} withdrawn to {recipient} (total: {total:.0f})", flush=True)
            _LAST_KNOWN_BALANCE_BY_COOKIE[cookie] = 0.0
        else:
            status = result.get("status")
            err_payload = result.get("payload") or {}
            err = str(err_payload.get("error", "")) if isinstance(err_payload, dict) else ""
            detail = err or result.get("error") or "unknown error"
            print(f"[withdraw:{username}] failed ({status}): {detail}", flush=True)
            if "insufficient balance" in err.lower():
                _LAST_KNOWN_BALANCE_BY_COOKIE[cookie] = 0.0
                return
            # Allow retry on next balance check/update.
            _LAST_KNOWN_BALANCE_BY_COOKIE[cookie] = prev
            wait_s = 30
            m_wait = re.search(r"wait\s+(?:(\d+)m\s*)?(\d+)s", err, re.IGNORECASE)
            if m_wait:
                mins = int(m_wait.group(1) or 0)
                secs = int(m_wait.group(2) or 0)
                wait_s = max(1, mins * 60 + secs + 1)
            if status == 429 or "cooldown" in err.lower() or "wait" in err.lower():
                print(f"[withdraw:{username}] retrying in {wait_s}s", flush=True)
                threading.Timer(wait_s, auto_withdraw_if_increased, args=(cookie,)).start()
    finally:
        lock.release()


def gather_message(message: discord.Message) -> dict:
    """Extract code content from a message."""
    code_blocks: list[str] = []

    for embed in message.embeds:
        data = embed.to_dict()
        texts = [
            data.get("title"),
            data.get("description"),
            *[f.get("name") for f in data.get("fields", [])],
            *[f.get("value") for f in data.get("fields", [])],
            (data.get("footer") or {}).get("text"),
        ]
        for text in texts:
            code_blocks.extend(extract_code_blocks(text))

    for comp in walk_components(getattr(message, "components", None) or []):
        text = comp.get("content")
        if text is None and isinstance(comp.get("data"), dict):
            text = comp["data"].get("content")
        code_blocks.extend(extract_code_blocks(text))

    seen = set()
    deduped = []
    for block in code_blocks:
        if block not in seen:
            seen.add(block)
            deduped.append(block)

    return {
        "message_id": message.id,
        "code_blocks": deduped,
    }


def is_globally_unusable_code_result(result: dict) -> bool:
    """Permanent failures where retrying the same code is pointless for all accounts."""
    status = result.get("status")
    payload = result.get("payload") or {}
    error = ""
    if isinstance(payload, dict):
        error = str(payload.get("error", "")).lower()
    if status == 410 and "expired" in error:
        return True
    if "already fully used" in error:
        return True
    if "fully used up" in error:
        return True
    return False


def is_account_done_code_result(result: dict) -> bool:
    """Result means this account cannot redeem this code again, but others may."""
    if result.get("ok"):
        return True
    if is_globally_unusable_code_result(result):
        return True
    payload = result.get("payload") or {}
    error = ""
    if isinstance(payload, dict):
        error = str(payload.get("error", "")).lower()
    if "already redeemed" in error:
        return True
    return False


def redeem_code(code: str, cookie: str | None = None) -> dict:
    """Redeem a DonutNova code. Requires DONUTNOVA_TOKEN or DONUTNOVA_COOKIE(S)."""
    headers = donutnova_headers(cookie=cookie)
    if not headers:
        return {"ok": False, "error": "Set DONUTNOVA_TOKEN or DONUTNOVA_COOKIE(S) to redeem codes."}

    body = json.dumps({"code": code}).encode("utf-8")
    req = urllib.request.Request(REDEEM_URL, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {"ok": True, "status": resp.status, "payload": payload}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return {"ok": False, "status": e.code, "payload": payload}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_REDEEM_FIRST_INDEX = 0


async def redeem_and_record(
    code: str,
    message_id: int,
    unusable_codes: set[str],
    in_progress: set[str],
    seen_message_ids: set[int],
) -> None:
    try:
        global _REDEEM_FIRST_INDEX
        cookies = get_donutnova_cookies()
        targets: list[str | None] = cookies if cookies else [None]
        if len(targets) > 1:
            start = _REDEEM_FIRST_INDEX % len(targets)
            _REDEEM_FIRST_INDEX += 1
            rotated = targets[start:] + targets[:start]
        else:
            rotated = targets
        any_transient_failure = False
        any_rate_limit = False
        globally_unusable = False

        items = []
        for offset, cookie in enumerate(rotated):
            i = (targets.index(cookie) + 1) if cookie in targets else (offset + 1)
            items.append((i, cookie))

        results = await asyncio.gather(*(asyncio.to_thread(redeem_code, code, cookie) for _, cookie in items))

        for (i, cookie), result in zip(items, results):
            status = result.get("status")
            payload = result.get("payload") or {}
            err = str(payload.get("error", "")) if isinstance(payload, dict) else ""
            if result.get("ok"):
                name = await asyncio.to_thread(get_account_name, cookie, f"account {i}")
                print(f"{name} used code: {code}", flush=True)
            else:
                name = await asyncio.to_thread(get_account_name, cookie, f"account {i}")
                detail = err or result.get("error") or "unknown error"
                print(f"{name} code {code} failed ({status}): {detail}", flush=True)

            if is_globally_unusable_code_result(result):
                globally_unusable = True
            if status == 429 or "too many attempts" in err.lower() or "try again in" in err.lower():
                any_rate_limit = True
            if not is_account_done_code_result(result):
                any_transient_failure = True

        if globally_unusable or not any_transient_failure:
            unusable_codes.add(code)
        else:
            # Transient failure on at least one account: allow retry.
            # Accounts that already redeemed will return account-done on retry.
            if any_rate_limit:
                threading.Timer(60.0, seen_message_ids.discard, args=(message_id,)).start()
            else:
                seen_message_ids.discard(message_id)
    finally:
        in_progress.discard(code)


def schedule_code_redemptions(
    message: discord.Message,
    unusable_codes: set[str],
    in_progress: set[str],
    seen_message_ids: set[int],
) -> None:
    """Schedule redemption immediately for any new codes in the message."""
    report = gather_message(message)
    for code in report["code_blocks"]:
        if code in unusable_codes or code in in_progress:
            continue
        in_progress.add(code)
        print(code, flush=True)
        asyncio.create_task(
            redeem_and_record(code, message.id, unusable_codes, in_progress, seen_message_ids)
        )


def start_donutnova_unscrambler() -> None:
    """Listen for DonutNova chat unscramble minigames and answer automatically."""
    cookies = get_donutnova_cookies()
    token = os.environ.get("DONUTNOVA_TOKEN")
    if not cookies and not token:
        return
    if not cookies:
        cookies = [None]  # token-only fallback

    load_word_index()
    solved_ids: set[str] = set()
    solved_lock = threading.Lock()
    assignment_lock = threading.Lock()
    msg_assignments: dict[str, int] = {}
    turn = {"i": 0}
    unscramble_re = re.compile(r"Unscramble\s+([A-Za-z]+)", re.IGNORECASE)

    def run_for_cookie(cookie: str | None, idx: int, total: int) -> None:
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        label = get_account_name(cookie, f"account{idx + 1}")

        while True:
            sio = socketio.Client(logger=False, engineio_logger=False, reconnection=True)

            @sio.event
            def connect(sio=sio, cookie=cookie, label=label):
                print(f"[unscramble:{label}] connected to donutnova socket", flush=True)
                sio.emit("chat:join")
                threading.Thread(target=auto_withdraw_if_increased, args=(cookie,), daemon=True).start()

            @sio.on("chat:message")
            def on_chat_message(data=None, sio=sio, idx=idx, total=total, label=label):
                recv_t = time.perf_counter()
                if not isinstance(data, dict):
                    return
                if data.get("type") != "minigame":
                    return
                msg_id = str(data.get("id", ""))
                content = str(data.get("content", ""))
                m = unscramble_re.search(content)
                if not m:
                    return
                scrambled = m.group(1)
                answer = unscramble_word(scrambled)
                solve_t = time.perf_counter()
                if not answer:
                    with solved_lock:
                        if msg_id and msg_id in solved_ids:
                            return
                        if msg_id:
                            solved_ids.add(msg_id)
                    print(f"[unscramble:{label}] no match for {scrambled}", flush=True)
                    return

                if msg_id:
                    with assignment_lock:
                        if msg_id in msg_assignments:
                            assigned = msg_assignments[msg_id]
                        else:
                            with solved_lock:
                                if msg_id in solved_ids:
                                    return
                                solved_ids.add(msg_id)
                            assigned = turn["i"] % total
                            turn["i"] += 1
                            msg_assignments[msg_id] = assigned
                    if assigned != idx:
                        return
                else:
                    with solved_lock:
                        solved_ids.add(f"noid:{scrambled}:{answer}:{time.time()}")

                reward = parse_minigame_reward(content)
                guaranteed = reward is not None and reward >= 2_000_000
                if not guaranteed and UNSCRAMBLE_SKIP_CHANCE > 0 and random.random() < UNSCRAMBLE_SKIP_CHANCE:
                    print(f"[unscramble:{label}] skipping {scrambled} -> {answer} to look human", flush=True)
                    return

                delay = random.uniform(UNSCRAMBLE_DELAY_MIN, UNSCRAMBLE_DELAY_MAX) if UNSCRAMBLE_DELAY_MAX > 0 else 0.0
                payload = {"content": answer, "anon": False}

                if delay <= 0:
                    try:
                        sio.emit("chat:send", payload)
                        emit_t = time.perf_counter()
                        print(
                            f"[unscramble:{label}] {scrambled} -> {answer} "
                            f"(solve={(solve_t-recv_t)*1000:.1f}ms emit={(emit_t-solve_t)*1000:.1f}ms total={(emit_t-recv_t)*1000:.1f}ms)",
                            flush=True,
                        )
                    except Exception as e:
                        print(f"[unscramble:{label}] send error: {e}", flush=True)
                    return

                def send_answer(sio=sio, payload=payload, scrambled=scrambled, answer=answer, delay=delay, label=label):
                    time.sleep(delay)
                    try:
                        sio.emit("chat:send", payload)
                        print(f"[unscramble:{label}] {scrambled} -> {answer} (sent after {delay:.1f}s)", flush=True)
                    except Exception as e:
                        print(f"[unscramble:{label}] send error: {e}", flush=True)

                threading.Thread(target=send_answer, daemon=True).start()

            @sio.on("chat:error")
            def on_chat_error(data=None, label=label):
                print(f"[unscramble:{label}] chat:error {data}", flush=True)

            @sio.on("rain:start")
            def on_rain_start(data=None, sio=sio, label=label):
                try:
                    sio.emit("rain:join", {})
                    print(f"[rain:{label}] joined rain: {data}", flush=True)
                except Exception as e:
                    print(f"[rain:{label}] join error: {e}", flush=True)

            @sio.on("rain:joined")
            def on_rain_joined(data=None, label=label):
                print(f"[rain:{label}] join confirmed: {data}", flush=True)

            @sio.on("balance:update")
            def on_balance_update(data=None, cookie=cookie):
                threading.Thread(target=auto_withdraw_if_increased, args=(cookie,), daemon=True).start()

            try:
                sio.connect(
                    "https://donutnova.com",
                    transports=["websocket"],
                    headers=headers,
                )
                sio.wait()
            except Exception as e:
                print(f"[unscramble:{label}] socket error: {e}", flush=True)
            finally:
                try:
                    sio.disconnect()
                except Exception:
                    pass

            time.sleep(5)

    for idx, cookie in enumerate(cookies):
        threading.Thread(
            target=run_for_cookie,
            args=(cookie, idx, len(cookies)),
            name=f"donutnova-unscrambler-{idx+1}",
            daemon=True,
        ).start()


class GatherClient(discord.Client):
    async def on_ready(self):
        seen_message_ids: set[int] = set()
        # Codes that are redeemed or permanently unusable (expired / fully used).
        already_fully_used_codes: set[str] = set()
        in_progress: set[str] = set()

        try:
            channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)

            if MESSAGE_ID:
                message = await channel.fetch_message(MESSAGE_ID)
                schedule_code_redemptions(message, already_fully_used_codes, in_progress, seen_message_ids)
                while in_progress:
                    await asyncio.sleep(0.01)
                return

            # Prime with the current latest message so we only react to new ones.
            async for message in channel.history(limit=1):
                seen_message_ids.add(message.id)

            while not self.is_closed():
                async for message in channel.history(limit=5):
                    if message.id in seen_message_ids:
                        continue
                    seen_message_ids.add(message.id)
                    schedule_code_redemptions(message, already_fully_used_codes, in_progress, seen_message_ids)
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            await self.close()


def load_dotenv_file(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file without overriding existing env vars."""
    env_path = path or Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def start_periodic_withdraw_checker() -> None:
    if not AUTO_WITHDRAW_ON_BALANCE_INCREASE:
        return

    def run():
        while True:
            time.sleep(WITHDRAW_CHECK_INTERVAL)
            try:
                auto_withdraw_if_increased()
            except Exception as e:
                print(f"[withdraw] periodic check error: {e}", flush=True)

    threading.Thread(target=run, name="withdraw-checker", daemon=True).start()


def main():
    load_dotenv_file()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env or your environment before running this script.")

    start_periodic_withdraw_checker()
    start_donutnova_unscrambler()
    client = GatherClient(chunk_guilds_at_startup=False)
    client.run(token)


if __name__ == "__main__":
    main()

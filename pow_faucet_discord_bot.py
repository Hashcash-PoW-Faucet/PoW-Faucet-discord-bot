import os
import re
import json
import time
from typing import Dict, Any, Optional
import discord
from discord import app_commands
import aiohttp


# ---------------------------
# Config
# ---------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()

FAUCET_API_BASE = os.environ.get("FAUCET_API_BASE", "http://127.0.0.1:8000").rstrip("/")
FAUCET_SENDER_SECRET = os.environ.get("FAUCET_SENDER_SECRET", "").strip()  # Secret of the funded faucet/source account
FAUCET_AMOUNT = int(os.environ.get("FAUCET_AMOUNT", "5"))
COOLDOWN_SECONDS = int(os.environ.get("FAUCET_COOLDOWN_SECONDS", str(24 * 3600)))

DATA_FILE = os.environ.get("FAUCET_BOT_DATA", "discord_pow_faucet.json")
LOCK_FILE = DATA_FILE + ".lock"

# Optional: speed up slash-command sync by limiting to one guild during development
GUILD_ID = os.environ.get("GUILD_ID", "").strip()

# Optional: restrict commands to one channel
ALLOWED_CHANNEL_ID = os.environ.get("ALLOWED_CHANNEL_ID", "").strip()

# Faucet address format (derived from sha256(secret).hexdigest()[:40])
ADDR_RE = re.compile(r"^[0-9a-fA-F]{40}$")


# ---------------------------
# Utilities
# ---------------------------
def now_ts() -> int:
    return int(time.time())


def channel_allowed(interaction: discord.Interaction) -> bool:
    if not ALLOWED_CHANNEL_ID:
        return True
    try:
        return interaction.channel_id == int(ALLOWED_CHANNEL_ID)
    except Exception:
        return False


def normalize_addr(addr: str) -> str:
    addr = (addr or "").strip()
    if not ADDR_RE.match(addr):
        raise ValueError("bad address format")
    return addr.lower()


# ---------------------------
# Minimal file lock (Linux)
# ---------------------------
class FileLock:
    def __init__(self, path: str):
        self.path = path
        self._fd = None

    def acquire(self):
        import fcntl
        self._fd = open(self.path, "a+")
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)

    def release(self):
        import fcntl
        if self._fd:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


# ---------------------------
# Persistence
# ---------------------------
def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: Dict[str, Any]) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def get_user(data: Dict[str, Any], discord_user_id: int) -> Optional[Dict[str, Any]]:
    return data.get("users", {}).get(str(discord_user_id))


def set_user(data: Dict[str, Any], discord_user_id: int, rec: Dict[str, Any]) -> None:
    data.setdefault("users", {})[str(discord_user_id)] = rec


# ---------------------------
# API calls (matches your FastAPI app.py)
# ---------------------------
async def api_get_me(session: aiohttp.ClientSession, secret: str) -> Dict[str, Any]:
    url = f"{FAUCET_API_BASE}/me"
    headers = {"Authorization": f"Bearer {secret}"}
    async with session.get(url, headers=headers, timeout=20) as r:
        txt = await r.text()
        if r.status != 200:
            raise RuntimeError(f"/me failed ({r.status}): {txt}")
        return json.loads(txt)


async def api_transfer(
    session: aiohttp.ClientSession,
    sender_secret: str,
    to_address: str,
    amount: int
) -> Dict[str, Any]:
    url = f"{FAUCET_API_BASE}/transfer"
    headers = {"Authorization": f"Bearer {sender_secret}", "Content-Type": "application/json"}
    payload = {"to_address": to_address, "amount": amount}
    async with session.post(url, headers=headers, json=payload, timeout=30) as r:
        txt = await r.text()
        if r.status != 200:
            raise RuntimeError(f"/transfer failed ({r.status}): {txt}")
        return json.loads(txt)


# ---------------------------
# Discord bot
# ---------------------------
intents = discord.Intents.default()


class PowFaucetBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        # Bonus protection: resolve faucet sender address at startup via /me
        self.sender_address: Optional[str] = None

    async def setup_hook(self):
        # Resolve sender address once (best effort)
        if FAUCET_SENDER_SECRET:
            try:
                async with aiohttp.ClientSession() as session:
                    me = await api_get_me(session, FAUCET_SENDER_SECRET)
                    addr = me.get("account_id")
                    if isinstance(addr, str) and ADDR_RE.match(addr):
                        self.sender_address = addr.lower()
            except Exception:
                # Best-effort only. If it fails, we continue without this protection.
                self.sender_address = None

        # Slash command sync
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = PowFaucetBot()


# ---------------------------
# Commands
# ---------------------------
@bot.tree.command(
    name="register_address",
    description="Register your faucet address (40 hex). You are responsible that it exists / was created via PoW signup."
)
@app_commands.describe(address="Your faucet address (40 hex characters)")
async def register_address(interaction: discord.Interaction, address: str):
    if not channel_allowed(interaction):
        await interaction.response.send_message("This command is not allowed in this channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        addr = normalize_addr(address)
    except Exception:
        await interaction.followup.send(
            "Invalid address format. Expected **40 hex characters** (0-9, a-f).",
            ephemeral=True
        )
        return

    # Bonus protection: do not allow registering the faucet sender address as target
    if bot.sender_address and addr == bot.sender_address:
        await interaction.followup.send(
            "That address is the **faucet source/sender** address. Please register your own address.",
            ephemeral=True
        )
        return

    with FileLock(LOCK_FILE):
        data = load_data()
        rec = get_user(data, interaction.user.id) or {}
        rec["address"] = addr
        rec["registered_at"] = now_ts()
        rec.setdefault("last_claim_at", 0)
        set_user(data, interaction.user.id, rec)
        save_data(data)

    note = (
        "Registered ✅\n"
        f"Stored address: `{addr}`\n"
        "Use `/claim` once every 24h.\n"
        "Note: If the address does not exist on the faucet yet, `/claim` will fail (unknown recipient)."
    )
    await interaction.followup.send(note, ephemeral=True)


@bot.tree.command(name="claim", description="Claim faucet credits (5) once every 24 hours.")
async def claim(interaction: discord.Interaction):
    if not channel_allowed(interaction):
        await interaction.response.send_message("This command is not allowed in this channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    if not FAUCET_SENDER_SECRET:
        await interaction.followup.send("Bot misconfigured: `FAUCET_SENDER_SECRET` missing.", ephemeral=True)
        return

    with FileLock(LOCK_FILE):
        data = load_data()
        rec = get_user(data, interaction.user.id)

    if not rec or not rec.get("address"):
        await interaction.followup.send("Not registered. Use `/register_address <address>` first.", ephemeral=True)
        return

    to_addr = rec["address"]

    # Bonus protection: do not allow claiming to sender address
    if bot.sender_address and to_addr == bot.sender_address:
        await interaction.followup.send(
            "Safety check: your registered target address equals the faucet sender address. "
            "Please register your own address again.",
            ephemeral=True
        )
        return

    # Cooldown
    last_claim = int(rec.get("last_claim_at", 0) or 0)
    t = now_ts()
    if last_claim and t < last_claim + COOLDOWN_SECONDS:
        rem = (last_claim + COOLDOWN_SECONDS) - t
        h = rem // 3600
        m = (rem % 3600) // 60
        await interaction.followup.send(f"Cooldown ⏳ Try again in ~{h}h {m}m.", ephemeral=True)
        return

    # Transfer credits using your existing /transfer endpoint
    try:
        async with aiohttp.ClientSession() as session:
            resp = await api_transfer(session, FAUCET_SENDER_SECRET, to_addr, FAUCET_AMOUNT)
    except Exception as e:
        msg = str(e)

        # Friendly hint for the most common user error:
        # target address has not been created (or user mistyped it).
        if "unknown recipient address" in msg or "(404)" in msg:
            await interaction.followup.send(
                "Claim failed: your address is **unknown** to the faucet server.\n"
                "Make sure you created the account via PoW signup and you entered the correct 40-hex address.",
                ephemeral=True
            )
            return

        # Insufficient faucet credits is also possible (400 insufficient credits)
        if "insufficient credits" in msg or "(400)" in msg:
            await interaction.followup.send(
                "Claim failed: faucet source has insufficient credits right now. Please try later.",
                ephemeral=True
            )
            return

        await interaction.followup.send(f"Claim failed: `{msg}`", ephemeral=True)
        return

    # Persist cooldown only on success
    with FileLock(LOCK_FILE):
        data2 = load_data()
        rec2 = get_user(data2, interaction.user.id) or {}
        rec2["address"] = to_addr
        rec2["last_claim_at"] = t
        set_user(data2, interaction.user.id, rec2)
        save_data(data2)

    from_credits = resp.get("from_credits")
    to_credits = resp.get("to_credits")

    await interaction.followup.send(
        f"Claim successful ✅ Sent **{FAUCET_AMOUNT} credits** to `{to_addr}`.\n"
        f"Source credits: `{from_credits}` | Your credits: `{to_credits}`",
        ephemeral=True
    )


@bot.tree.command(name="whoami", description="Show your registered address and cooldown status.")
async def whoami(interaction: discord.Interaction):
    if not channel_allowed(interaction):
        await interaction.response.send_message("This command is not allowed in this channel.", ephemeral=True)
        return

    with FileLock(LOCK_FILE):
        data = load_data()
        rec = get_user(data, interaction.user.id)

    if not rec or not rec.get("address"):
        await interaction.response.send_message("Not registered. Use `/register_address <address>`.", ephemeral=True)
        return

    addr = rec["address"]
    last_claim = int(rec.get("last_claim_at", 0) or 0)
    t = now_ts()

    if not last_claim or t >= last_claim + COOLDOWN_SECONDS:
        cd = "ready ✅"
    else:
        rem = (last_claim + COOLDOWN_SECONDS) - t
        cd = f"~{rem//3600}h {(rem%3600)//60}m remaining"

    sender_info = ""
    if bot.sender_address:
        sender_info = f"\nFaucet sender address (resolved): `{bot.sender_address}`"

    await interaction.response.send_message(
        f"Your registered address: `{addr}`\nCooldown: {cd}{sender_info}",
        ephemeral=True
    )


def main():
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
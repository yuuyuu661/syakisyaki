# Python 3.11 / discord.py 2.4.x / aiosqlite
# ✅ JP+EN UI（locale_str）/ ギルド即時同期（copy→sync）/ 再登録クリアオプション
# ✅ 掲示板は固定チャンネルにのみ出力（個別の公開メッセージなし）
# ✅ サービスチケットの管理用コマンド（ユーザー・サービス名・減らす枚数）追加

import os
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple, List, Callable, Awaitable

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

JST = timezone(timedelta(hours=9))

# =============================
# 🔧 環境変数
# =============================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
GUILD_IDS = [int(x.strip()) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip().isdigit()]

# コマンド再登録クリア（古い定義を掃除してから同期）…必要な時だけ 1
FORCE_REBUILD_CMDS = os.getenv("FORCE_REBUILD_CMDS", "0") == "1"

# 権限ロール（数値ID）
BALANCE_AUDIT_ROLE_ID = int(os.getenv("BALANCE_AUDIT_ROLE_ID", "0") or 0)
ADJUST_ROLE_ID = int(os.getenv("ADJUST_ROLE_ID", "0") or 0)

# 通貨名（表示用）
CURRENCY_NAME = os.getenv("CURRENCY_NAME", "円")

# 固定掲示板チャンネル
TICKET_BOARD_CHANNEL_ID = int(os.getenv("TICKET_BOARD_CHANNEL_ID", "0") or 0)
RESULT_BOARD_CHANNEL_ID = int(os.getenv("RESULT_BOARD_CHANNEL_ID", "0") or 0)

# DB パス
DB_PATH = os.getenv("DB_PATH", "data.sqlite3")

# =============================
# 🧱 DB 初期化
# =============================
INIT_SQL = r"""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS balances (
  guild_id INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  balance  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS tickets (
  guild_id INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  label    TEXT NOT NULL,
  count    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id, label)
);

CREATE TABLE IF NOT EXISTS boards (
  guild_id   INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  kind       TEXT NOT NULL, -- 'ticket' | 'contract_result'
  message_id INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id, kind)
);

CREATE TABLE IF NOT EXISTS contracts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id    INTEGER NOT NULL,
  initiator   INTEGER NOT NULL,
  opponent    INTEGER NOT NULL,
  content     TEXT NOT NULL,
  status      TEXT NOT NULL, -- 'pending'|'accepted'|'declined'|'closed'
  created_at  TEXT NOT NULL,
  accepted_at TEXT
);
"""

# =============================
# 🛠️ ユーティリティ
# =============================
def jst_now_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

async def ensure_balance(db: aiosqlite.Connection, guild_id: int, user_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO balances (guild_id, user_id, balance) VALUES (?, ?, 0)",
        (guild_id, user_id)
    )

async def get_balance(db: aiosqlite.Connection, guild_id: int, user_id: int) -> int:
    await ensure_balance(db, guild_id, user_id)
    cur = await db.execute("SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def add_balance(db: aiosqlite.Connection, guild_id: int, user_id: int, delta: int) -> int:
    await ensure_balance(db, guild_id, user_id)
    await db.execute("UPDATE balances SET balance = balance + ? WHERE guild_id=? AND user_id=?", (delta, guild_id, user_id))
    cur = await db.execute("SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def add_ticket(db: aiosqlite.Connection, guild_id: int, user_id: int, label: str, n: int = 1) -> int:
    await db.execute(
        "INSERT OR IGNORE INTO tickets (guild_id, user_id, label, count) VALUES (?, ?, ?, 0)",
        (guild_id, user_id, label)
    )
    await db.execute(
        "UPDATE tickets SET count = count + ? WHERE guild_id=? AND user_id=? AND label=?",
        (n, guild_id, user_id, label)
    )
    cur = await db.execute(
        "SELECT count FROM tickets WHERE guild_id=? AND user_id=? AND label=?",
        (guild_id, user_id, label)
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def set_ticket(db: aiosqlite.Connection, guild_id: int, user_id: int, label: str, count: int) -> int:
    await db.execute(
        "INSERT OR REPLACE INTO tickets (guild_id, user_id, label, count) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, label, count)
    )
    cur = await db.execute(
        "SELECT count FROM tickets WHERE guild_id=? AND user_id=? AND label=?",
        (guild_id, user_id, label)
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def fetch_ticket_summary(db: aiosqlite.Connection, guild_id: int) -> Dict[int, Dict[str, int]]:
    cur = await db.execute("SELECT user_id, label, count FROM tickets WHERE guild_id=? ORDER BY user_id, label", (guild_id,))
    out: Dict[int, Dict[str, int]] = {}
    async for user_id, label, count in cur:
        out.setdefault(int(user_id), {})[label] = int(count)
    return out

async def upsert_board(db: aiosqlite.Connection, guild_id: int, channel_id: int, kind: str, message_id: int) -> None:
    await db.execute(
        "INSERT INTO boards (guild_id, channel_id, kind, message_id) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(guild_id, channel_id, kind) DO UPDATE SET message_id=excluded.message_id",
        (guild_id, channel_id, kind, message_id)
    )

async def get_board_message_id(db: aiosqlite.Connection, guild_id: int, channel_id: int, kind: str) -> Optional[int]:
    cur = await db.execute("SELECT message_id FROM boards WHERE guild_id=? AND channel_id=? AND kind=?", (guild_id, channel_id, kind))
    row = await cur.fetchone()
    return int(row[0]) if row else None

# =============================
# 🤖 Bot セットアップ
# =============================
intents = discord.Intents.default()
intents.members = True
intents.message_content = False

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("yenbot")

class YenBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self.db: Optional[aiosqlite.Connection] = None

    async def setup_hook(self) -> None:
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.executescript(INIT_SQL)
        await self.db.commit()

        # --- スラッシュ即時反映: グローバル→ギルドコピー + ギルド同期 ---
        if GUILD_IDS:
            if FORCE_REBUILD_CMDS:
                for gid in GUILD_IDS:
                    obj = discord.Object(id=gid)
                    try:
                        self.tree.clear_commands(guild=obj)
                        cleared = await self.tree.sync(guild=obj)
                        logger.info(f"Cleared {len(cleared)} commands from guild {gid}")
                    except Exception as e:
                        logger.exception(e)
            for gid in GUILD_IDS:
                obj = discord.Object(id=gid)
                try:
                    self.tree.copy_global_to(guild=obj)
                    synced = await self.tree.sync(guild=obj)
                    logger.info(f"Synced {len(synced)} commands to guild {gid}")
                except Exception as e:
                    logger.exception(e)
        else:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} global commands")

bot = YenBot()
ls = app_commands.locale_str  # JP/EN ローカライズ
def em_title(t: str) -> discord.Embed:
    return discord.Embed(title=t, color=0x2ecc71, timestamp=datetime.now(JST))

# =============================
# 🔔 固定掲示板ユーティリティ
# =============================
async def _get_fixed_channel(guild: discord.Guild, channel_id: int) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(channel_id)
    return ch if isinstance(ch, discord.TextChannel) else None

async def ensure_board_message(guild: discord.Guild, kind: str) -> Optional[discord.Message]:
    """固定チャンネルに掲示板メッセージを作成/取得"""
    assert bot.db is not None
    channel_id = TICKET_BOARD_CHANNEL_ID if kind == "ticket" else RESULT_BOARD_CHANNEL_ID
    if not channel_id:
        return None
    ch = await _get_fixed_channel(guild, channel_id)
    if not ch:
        return None

    msg_id = await get_board_message_id(bot.db, guild.id, ch.id, kind)
    if msg_id:
        try:
            return await ch.fetch_message(msg_id)
        except discord.NotFound:
            pass  # 消えていれば新規作成

    # 初期メッセージ
    title = "サービスチケット掲示板（自動更新） / Ticket Board (auto-update)" if kind == "ticket" else "勝負結果掲示板（自動更新） / Result Board (auto-update)"
    e = em_title(title)
    e.description = "ここに内容が自動更新されます。\nThis message is automatically updated."
    msg = await ch.send(embed=e)
    await upsert_board(bot.db, guild.id, ch.id, kind, msg.id)
    await bot.db.commit()
    return msg

# =============================
# 💱 送金（/send → 表示名: 送金 / Send）
# =============================
@bot.tree.command(
    name=ls("send", ja="送金"),
    description=ls("Send currency to a user", ja="ユーザーへ送金します")
)
@app_commands.describe(
    user="送金先 / Recipient",
    amount="金額（整数）/ Amount (int)",
    note="一言（任意）/ Note (optional)"
)
async def send(inter: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 10_000_000], note: Optional[str] = None):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    if user.bot:
        await inter.response.send_message("Bot へは送金できません / Cannot send to bots.", ephemeral=True)
        return

    async with bot.db.execute("BEGIN"):
        sender_bal = await get_balance(bot.db, guild.id, inter.user.id)
        if sender_bal < amount:
            await inter.response.send_message(f"残高不足 / Insufficient balance: {sender_bal}{CURRENCY_NAME}", ephemeral=True)
            await bot.db.execute("ROLLBACK")
            return
        await add_balance(bot.db, guild.id, inter.user.id, -amount)
        await add_balance(bot.db, guild.id, user.id, amount)
        await bot.db.commit()

    # 公開メッセージは出さない → 最小限のエフェメラルのみ
    await inter.response.send_message("送金しました / Sent.", ephemeral=True)

# =============================
# 🧾 残高確認（/balance → 残高確認 / Balance）
# =============================
@bot.tree.command(
    name=ls("balance", ja="残高確認"),
    description=ls("Check your or someone's balance", ja="自分または特定ユーザーの残高を確認します")
)
@app_commands.describe(user="対象（未指定なら自分）/ Target (self if omitted)")
async def balance(inter: discord.Interaction, user: Optional[discord.Member] = None):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    target = user or inter.user
    if target.id != inter.user.id:
        if BALANCE_AUDIT_ROLE_ID and isinstance(inter.user, discord.Member):
            if discord.utils.get(inter.user.roles, id=BALANCE_AUDIT_ROLE_ID) is None:
                await inter.response.send_message("権限がありません / No permission.", ephemeral=True)
                return
        else:
            await inter.response.send_message("権限がありません / No permission.", ephemeral=True)
            return

    bal = await get_balance(bot.db, guild.id, target.id)
    e = em_title("残高 / Balance")
    e.add_field(name="ユーザー / User", value=target.mention, inline=True)
    e.add_field(name="残高 / Amount", value=f"{bal}{CURRENCY_NAME}", inline=True)
    await inter.response.send_message(embed=e, ephemeral=True)

# =============================
# 🧮 金額調整（/adjust → 金額調整 / Adjust）
# =============================
@bot.tree.command(
    name=ls("adjust", ja="金額調整"),
    description=ls("Adjust balance (admin)", ja="管理者が残高を調整します（例: +100, -50）")
)
@app_commands.describe(user="対象ユーザー / Target user", delta="+N または -N / +N or -N")
async def adjust(inter: discord.Interaction, user: discord.Member, delta: str):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    if ADJUST_ROLE_ID and isinstance(inter.user, discord.Member):
        if discord.utils.get(inter.user.roles, id=ADJUST_ROLE_ID) is None:
            await inter.response.send_message("権限がありません / No permission.", ephemeral=True)
            return
    else:
        await inter.response.send_message("権限がありません / No permission.", ephemeral=True)
        return

    m = re.fullmatch(r"([+-])(\d+)", delta.strip())
    if not m:
        await inter.response.send_message("形式エラー: +100 や -50 / Format: +100 or -50", ephemeral=True)
        return
    sign, num = m.group(1), int(m.group(2))
    amount = num if sign == "+" else -num

    new_bal = await add_balance(bot.db, guild.id, user.id, amount)
    await bot.db.commit()

    e = em_title("残高調整 / Adjust")
    e.add_field(name="対象 / User", value=user.mention, inline=True)
    e.add_field(name="変動 / Δ", value=f"{amount:+d}{CURRENCY_NAME}", inline=True)
    e.add_field(name="新残高 / New", value=f"{new_bal}{CURRENCY_NAME}", inline=True)
    await inter.response.send_message(embed=e, ephemeral=True)

# =============================
# 🎟️ サービス作成（/service_create）
# =============================
class ServiceButton(discord.ui.Button):
    def __init__(self, label: str, price: int):
        super().__init__(style=discord.ButtonStyle.primary, label=f"{label} ({price}{CURRENCY_NAME})")
        self.raw_label = label
        self.price = price

    async def callback(self, inter: discord.Interaction):
        # 個別に公開メッセージは出さない（ephemeral最小限）＋固定掲示板のみ更新
        assert bot.db is not None
        guild = inter.guild
        assert guild is not None

        bal = await get_balance(bot.db, guild.id, inter.user.id)
        if bal < self.price:
            await inter.response.send_message(f"残高不足 / Insufficient: {bal}{CURRENCY_NAME}", ephemeral=True)
            return

        await add_balance(bot.db, guild.id, inter.user.id, -self.price)
        await add_ticket(bot.db, guild.id, inter.user.id, self.raw_label, 1)
        await bot.db.commit()

        await inter.response.send_message("購入完了 / Purchased.", ephemeral=True)
        await update_ticket_board()  # 固定チャンネルの掲示板を更新

class ServiceView(discord.ui.View):
    def __init__(self, pairs: List[Tuple[str, int]]):
        super().__init__(timeout=None)
        for label, price in pairs:
            self.add_item(ServiceButton(label, price))

@bot.tree.command(
    name=ls("service_create", ja="サービス作成"),
    description=ls("Create service panel (ticket vending)", ja="サービス販売パネルを作成（チケット付与）")
)
@app_commands.describe(
    title="サービスのタイトル / Panel title",
    description="サービスの紹介 / Description",
    label1="ボタン1 文言 / Button1 label", price1="ボタン1 金額 / Button1 price",
    label2="ボタン2 文言 / Button2 label", price2="ボタン2 金額 / Button2 price",
    label3="ボタン3 文言 / Button3 label", price3="ボタン3 金額 / Button3 price",
    label4="ボタン4 文言 / Button4 label", price4="ボタン4 金額 / Button4 price",
    label5="ボタン5 文言 / Button5 label", price5="ボタン5 金額 / Button5 price",
    label6="ボタン6 文言 / Button6 label", price6="ボタン6 金額 / Button6 price",
)
async def service_create(
    inter: discord.Interaction,
    title: str,
    description: str,
    label1: Optional[str] = None, price1: Optional[int] = None,
    label2: Optional[str] = None, price2: Optional[int] = None,
    label3: Optional[str] = None, price3: Optional[int] = None,
    label4: Optional[str] = None, price4: Optional[int] = None,
    label5: Optional[str] = None, price5: Optional[int] = None,
    label6: Optional[str] = None, price6: Optional[int] = None,
):
    pairs: List[Tuple[str, int]] = []
    for lab, pr in [(label1, price1), (label2, price2), (label3, price3), (label4, price4), (label5, price5), (label6, price6)]:
        if lab and pr and pr > 0:
            pairs.append((lab.strip(), int(pr)))
    if not pairs:
        await inter.response.send_message("少なくとも1つのボタンが必要です / At least 1 button required.", ephemeral=True)
        return

    e = em_title(f"{title}")
    e.description = description
    await inter.response.send_message(embed=e, view=ServiceView(pairs))

# =============================
# 🧾 固定：チケット掲示板（自動更新）
# =============================
async def update_ticket_board():
    """固定チャンネルのチケット掲示板を最新化"""
    assert bot.db is not None
    # ギルド1つ運用前提（複数対応ならループで）
    for gid in GUILD_IDS:
        guild = bot.get_guild(gid)
        if not guild:
            continue
        msg = await ensure_board_message(guild, "ticket")
        if not msg:
            continue
        summary = await fetch_ticket_summary(bot.db, guild.id)
        e = em_title("サービスチケット掲示板（自動更新） / Ticket Board")
        if not summary:
            e.description = "まだチケットの購入はありません。\nNo purchases yet."
        else:
            lines = []
            for uid, labels in summary.items():
                member = guild.get_member(uid)
                name = member.mention if member else f"<@{uid}>"
                for lbl, cnt in labels.items():
                    lines.append(f"{name} | {lbl} | {cnt}")
            e.description = "\n".join(lines)[:4000]
        try:
            await msg.edit(embed=e)
        except Exception:
            logger.exception("Failed to edit ticket board")

@bot.tree.command(
    name=ls("setup_ticket_board", ja="チケット掲示板作成"),
    description=ls("Create/refresh ticket board (fixed channel)", ja="固定チャンネルにチケット掲示板を作成/再作成します")
)
async def setup_ticket_board(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    await update_ticket_board()
    await inter.followup.send("チケット掲示板を用意/更新しました（固定CH）。", ephemeral=True)

# =============================
# 🛠 管理: サービスチケット調整（減らす枚数）
# =============================
@bot.tree.command(
    name=ls("service_ticket_adjust", ja="サービスチケット調整"),
    description=ls("Adjust service tickets for a user", ja="ユーザーのサービスチケット枚数を調整します")
)
@app_commands.describe(
    user="ユーザー / User",
    service="サービス名（ラベル）/ Service label",
    dec="減らす枚数（正数）/ Decrease count (positive)"
)
async def service_ticket_adjust(inter: discord.Interaction, user: discord.Member, service: str, dec: app_commands.Range[int, 1, 10_000]):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    if ADJUST_ROLE_ID and isinstance(inter.user, discord.Member):
        if discord.utils.get(inter.user.roles, id=ADJUST_ROLE_ID) is None:
            await inter.response.send_message("権限がありません / No permission.", ephemeral=True)
            return
    else:
        await inter.response.send_message("権限がありません / No permission.", ephemeral=True)
        return

    # 現在値を取得 → 減算（マイナスは0で止める）
    cur = await bot.db.execute(
        "SELECT count FROM tickets WHERE guild_id=? AND user_id=? AND label=?",
        (guild.id, user.id, service)
    )
    row = await cur.fetchone()
    current = int(row[0]) if row else 0
    new_count = max(0, current - int(dec))
    await set_ticket(bot.db, guild.id, user.id, service, new_count)
    await bot.db.commit()

    await inter.response.send_message(f"調整完了: {user.mention} / {service} / {current} → {new_count}", ephemeral=True)
    await update_ticket_board()

# =============================
# 🤝 契約（提案/承諾/拒否/タイムアウト）
# =============================
class ContractView(discord.ui.View):
    def __init__(self, initiator_id: int, opponent_id: int, contract_id: int, timeout_seconds: int = 300):
        super().__init__(timeout=timeout_seconds)
        self.initiator_id = initiator_id
        self.opponent_id = opponent_id
        self.contract_id = contract_id

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        return inter.user.id == self.opponent_id

    @discord.ui.button(label="承諾 / Accept", style=discord.ButtonStyle.success)
    async def accept(self, inter: discord.Interaction, btn: discord.ui.Button):
        assert bot.db is not None
        await bot.db.execute("UPDATE contracts SET status='accepted', accepted_at=? WHERE id=?", (jst_now_str(), self.contract_id))
        await bot.db.commit()
        # 公開アナウンスは出さない
        await inter.response.edit_message(view=None)
        await inter.followup.send("契約を承諾しました / Accepted.", ephemeral=True)

    @discord.ui.button(label="拒否 / Decline", style=discord.ButtonStyle.danger)
    async def decline(self, inter: discord.Interaction, btn: discord.ui.Button):
        assert bot.db is not None
        await bot.db.execute("UPDATE contracts SET status='declined' WHERE id=?", (self.contract_id,))
        await bot.db.commit()
        await inter.response.edit_message(view=None)
        await inter.followup.send("契約を拒否しました / Declined.", ephemeral=True)

    async def on_timeout(self) -> None:
        assert bot.db is not None
        await bot.db.execute("UPDATE contracts SET status='declined' WHERE id=? AND status='pending'", (self.contract_id,))
        await bot.db.commit()

@bot.tree.command(
    name=ls("contract", ja="契約"),
    description=ls("Propose a duel contract", ja="勝負契約を相手に提示します（5分以内に承諾/拒否）")
)
@app_commands.describe(opponent="相手 / Opponent", content="勝負の内容 / Content")
async def contract(inter: discord.Interaction, opponent: discord.Member, content: str):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    if opponent.bot or opponent.id == inter.user.id:
        await inter.response.send_message("相手ユーザーの指定が不正 / Invalid opponent.", ephemeral=True)
        return

    created_at = jst_now_str()
    await bot.db.execute(
        "INSERT INTO contracts (guild_id, initiator, opponent, content, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (guild.id, inter.user.id, opponent.id, content, created_at)
    )
    await bot.db.commit()

    cur = await bot.db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    contract_id = int(row[0])

    e = em_title("契約の提案 / Contract Proposal")
    e.description = (
        f"**{inter.user.mention}** vs **{opponent.mention}**\n\n"
        f"**勝負内容 / Content:**\n{content}\n\n"
        f"5分以内に承諾または拒否してください / Accept or decline within 5 minutes."
    )
    view = ContractView(inter.user.id, opponent.id, contract_id)
    await inter.response.send_message(embed=e, view=view)

    async def timeout_task():
        await asyncio.sleep(305)
        cur2 = await bot.db.execute("SELECT status FROM contracts WHERE id=?", (contract_id,))
        r = await cur2.fetchone()
        if r and r[0] == 'pending':
            await bot.db.execute("UPDATE contracts SET status='declined' WHERE id=?", (contract_id,))
            await bot.db.commit()
    bot.loop.create_task(timeout_task())

# =============================
# ✅ 契約終了（相手の承認→固定“勝負結果掲示板”にのみ反映）
# =============================
class ResultConfirmView(discord.ui.View):
    def __init__(self, confirmer_id: int, on_confirm: Callable[[discord.Interaction], Awaitable[None]]):
        super().__init__(timeout=300)
        self.confirmer_id = confirmer_id
        self.on_confirm = on_confirm

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        return inter.user.id == self.confirmer_id

    @discord.ui.button(label="承認 / Approve", style=discord.ButtonStyle.success)
    async def approve(self, inter: discord.Interaction, btn: discord.ui.Button):
        await self.on_confirm(inter)

@bot.tree.command(
    name=ls("contract_close", ja="契約終了"),
    description=ls("Submit duel result", ja="勝負の結果を申請します（相手の承認が必要）")
)
@app_commands.describe(opponent="勝負相手 / Opponent", result="あなたの結果 / Your result")
@app_commands.choices(result=[
    app_commands.Choice(name=ls("win", ja="勝利"), value="win"),
    app_commands.Choice(name=ls("lose", ja="敗北"), value="lose"),
])
async def contract_close(inter: discord.Interaction, opponent: discord.Member, result: app_commands.Choice[str]):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    cur = await bot.db.execute(
        "SELECT id, content FROM contracts "
        "WHERE guild_id=? AND ((initiator=? AND opponent=?) OR (initiator=? AND opponent=?)) "
        "AND status='accepted' ORDER BY id DESC LIMIT 1",
        (guild.id, inter.user.id, opponent.id, opponent.id, inter.user.id)
    )
    row = await cur.fetchone()
    if not row:
        await inter.response.send_message("承諾済みの契約が見つかりません / No accepted contract found.", ephemeral=True)
        return
    cid, content = int(row[0]), str(row[1])

    confirmer_id = opponent.id

    async def on_confirm(confirm_inter: discord.Interaction):
        await bot.db.execute("UPDATE contracts SET status='closed' WHERE id=?", (cid,))
        await bot.db.commit()
        await confirm_inter.response.edit_message(view=None)
        # 公開アナウンスは出さず、固定“勝負結果掲示板”のみ更新
        await append_result_board(content, inter.user, opponent, result.value)
        await confirm_inter.followup.send("結果を確定し掲示板を更新しました / Result confirmed.", ephemeral=True)

    e = em_title("勝負結果の確認 / Result Confirmation")
    result_ja = "勝利" if result.value == "win" else "敗北"
    e.description = (
        f"**勝負内容 / Content**\n{content}\n\n"
        f"申請者 / Submitter: {inter.user.mention} → 結果 / Result: **{result_ja}**\n"
        f"相手 / Opponent: {opponent.mention} の承認が必要です / Needs opponent approval."
    )
    await inter.response.send_message(embed=e, view=ResultConfirmView(confirmer_id, on_confirm))

async def append_result_board(content: str, user_a: discord.Member, user_b: discord.Member, result: str):
    """固定チャンネルの勝負結果掲示板に追記（先頭に積む）"""
    assert bot.db is not None
    for gid in GUILD_IDS:
        guild = bot.get_guild(gid)
        if not guild:
            continue
        msg = await ensure_board_message(guild, "contract_result")
        if not msg:
            continue

        symbol = "🏆" if result == "win" else "⚑"
        line = f"{jst_now_str()} — {user_a.mention} vs {user_b.mention} → {user_a.mention} {('勝利' if result=='win' else '敗北')} {symbol}\n内容 / Content: {content}"

        e = em_title("勝負結果掲示板（自動更新） / Result Board")
        old = msg.embeds[0].description if msg.embeds else ""
        new_desc = (line + "\n" + (old or "")).strip()
        e.description = new_desc[:4000]
        try:
            await msg.edit(embed=e)
        except Exception:
            logger.exception("Failed to edit result board")

@bot.tree.command(
    name=ls("setup_result_board", ja="勝負結果掲示板作成"),
    description=ls("Create/refresh result board (fixed channel)", ja="固定チャンネルに勝負結果掲示板を作成/再作成します")
)
async def setup_result_board(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    # 初期行だけ作っておく
    for gid in GUILD_IDS:
        guild = bot.get_guild(gid)
        if not guild:
            continue
        await ensure_board_message(guild, "contract_result")
    await inter.followup.send("勝負結果掲示板を用意/更新しました（固定CH）。", ephemeral=True)

# =============================
# 🟢 起動時ログ
# =============================
@bot.event
async def on_ready():
    try:
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = bot.get_guild(gid)
                if guild:
                    cmds = await bot.tree.fetch_commands(guild=guild)
                    names = [c.name for c in cmds]
                    logger.info(f"Guild {gid} commands: {len(cmds)} -> {names}")
                else:
                    logger.warning(f"Guild {gid} not found in cache.")
        else:
            cmds = await bot.tree.fetch_commands()
            names = [c.name for c in cmds]
            logger.info(f"Global commands: {len(cmds)} -> {names}")
    except Exception:
        logger.exception("Failed to fetch commands on_ready")

# =============================
# 🚀 起動
# =============================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("環境変数 DISCORD_TOKEN が未設定です / DISCORD_TOKEN missing")
    bot.run(DISCORD_TOKEN)

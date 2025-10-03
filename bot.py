# Python 3.11 / discord.py 2.4.x / aiosqlite
# ✅ 日本語UI（locale_str）/ ギルド即時同期 / 再登録クリアのオプション付き

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

# コマンド再登録クリア（古い定義を掃除してから同期）…必要な時だけ 1 に
FORCE_REBUILD_CMDS = os.getenv("FORCE_REBUILD_CMDS", "0") == "1"

# 権限ロール（数値ID）
BALANCE_AUDIT_ROLE_ID = int(os.getenv("BALANCE_AUDIT_ROLE_ID", "0") or 0)
ADJUST_ROLE_ID = int(os.getenv("ADJUST_ROLE_ID", "0") or 0)

# 通貨名（表示用）
CURRENCY_NAME = os.getenv("CURRENCY_NAME", "円")

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
intents.members = True        # メンバー参照が必要
intents.message_content = False  # スラッシュコマンドには不要

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("yenbot")

class YenBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self.db: Optional[aiosqlite.Connection] = None

    async def setup_hook(self) -> None:
        # DB
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.executescript(INIT_SQL)
        await self.db.commit()

        # --- スラッシュ即時反映: グローバル→ギルドコピー + ギルド同期 ---
        if GUILD_IDS:
            # クリアオプション（古い定義の掃除）
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
            # グローバル同期（反映に時間がかかる）
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} global commands")

bot = YenBot()
ls = app_commands.locale_str  # 日本語UI用ローカライズ

def em_title(t: str) -> discord.Embed:
    return discord.Embed(title=t, color=0x2ecc71, timestamp=datetime.now(JST))

# =============================
# 💱 送金（UI名: 送金）
# =============================
@bot.tree.command(name=ls("send", ja="送金"), description=ls("Send currency", ja="ユーザーへ送金します"))
@app_commands.describe(user="送金先ユーザー", amount="金額（整数）", note="一言（任意）")
async def send(inter: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 10_000_000], note: Optional[str] = None):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    if user.bot:
        await inter.response.send_message("Bot へは送金できません。", ephemeral=True)
        return

    async with bot.db.execute("BEGIN"):
        sender_bal = await get_balance(bot.db, guild.id, inter.user.id)
        if sender_bal < amount:
            await inter.response.send_message(f"残高不足です。現在残高: {sender_bal}{CURRENCY_NAME}", ephemeral=True)
            await bot.db.execute("ROLLBACK")
            return
        await add_balance(bot.db, guild.id, inter.user.id, -amount)
        await add_balance(bot.db, guild.id, user.id, amount)
        await bot.db.commit()

    e = em_title("送金が完了しました")
    e.add_field(name="送金者", value=inter.user.mention, inline=True)
    e.add_field(name="受取人", value=user.mention, inline=True)
    e.add_field(name="金額", value=f"{amount}{CURRENCY_NAME}", inline=True)
    if note:
        e.add_field(name="一言", value=note, inline=False)
    await inter.response.send_message(embed=e)

# =============================
# 🧾 残高確認（UI名: 残高確認）
# =============================
@bot.tree.command(name=ls("balance", ja="残高確認"), description=ls("Check balance", ja="自分または特定ユーザーの残高を確認します"))
@app_commands.describe(user="対象ユーザー（未指定なら自分）")
async def balance(inter: discord.Interaction, user: Optional[discord.Member] = None):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    target = user or inter.user
    if target.id != inter.user.id:
        if BALANCE_AUDIT_ROLE_ID and isinstance(inter.user, discord.Member):
            if discord.utils.get(inter.user.roles, id=BALANCE_AUDIT_ROLE_ID) is None:
                await inter.response.send_message("他ユーザーの残高参照権限がありません。", ephemeral=True)
                return
        else:
            await inter.response.send_message("他ユーザーの残高参照権限がありません。", ephemeral=True)
            return

    bal = await get_balance(bot.db, guild.id, target.id)
    e = em_title("残高確認")
    e.add_field(name="ユーザー", value=target.mention, inline=True)
    e.add_field(name="残高", value=f"{bal}{CURRENCY_NAME}", inline=True)
    await inter.response.send_message(embed=e, ephemeral=(target.id == inter.user.id))

# =============================
# 🧮 金額調整（UI名: 金額調整）
# =============================
@bot.tree.command(name=ls("adjust", ja="金額調整"), description=ls("Adjust balance (admin)", ja="管理者が残高を調整します（例: +100, -50）"))
@app_commands.describe(user="対象ユーザー", delta="+N または -N の形式")
async def adjust(inter: discord.Interaction, user: discord.Member, delta: str):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    if ADJUST_ROLE_ID and isinstance(inter.user, discord.Member):
        if discord.utils.get(inter.user.roles, id=ADJUST_ROLE_ID) is None:
            await inter.response.send_message("金額調整の権限がありません。", ephemeral=True)
            return
    else:
        await inter.response.send_message("金額調整の権限がありません。", ephemeral=True)
        return

    m = re.fullmatch(r"([+-])(\d+)", delta.strip())
    if not m:
        await inter.response.send_message("形式エラー: +100 や -50 のように指定してください。", ephemeral=True)
        return
    sign, num = m.group(1), int(m.group(2))
    amount = num if sign == "+" else -num

    new_bal = await add_balance(bot.db, guild.id, user.id, amount)
    await bot.db.commit()

    e = em_title("残高調整")
    e.add_field(name="対象", value=user.mention, inline=True)
    e.add_field(name="変動", value=f"{amount:+d}{CURRENCY_NAME}", inline=True)
    e.add_field(name="新残高", value=f"{new_bal}{CURRENCY_NAME}", inline=True)
    await inter.response.send_message(embed=e)

# =============================
# 🎟️ サービス作成（UI名: サービス作成）
# =============================
class ServiceButton(discord.ui.Button):
    def __init__(self, label: str, price: int):
        super().__init__(style=discord.ButtonStyle.primary, label=f"{label} ({price}{CURRENCY_NAME})")
        self.raw_label = label
        self.price = price

    async def callback(self, inter: discord.Interaction):
        assert bot.db is not None
        guild = inter.guild
        assert guild is not None

        bal = await get_balance(bot.db, guild.id, inter.user.id)
        if bal < self.price:
            await inter.response.send_message(f"残高が足りません。現在 {bal}{CURRENCY_NAME}", ephemeral=True)
            return
        await add_balance(bot.db, guild.id, inter.user.id, -self.price)
        new_count = await add_ticket(bot.db, guild.id, inter.user.id, self.raw_label, 1)
        await bot.db.commit()

        await inter.response.send_message(
            f"{self.raw_label} のチケットを1枚取得しました（所持: {new_count}）。残高 {bal - self.price}{CURRENCY_NAME}",
            ephemeral=True
        )

        await update_ticket_board_message(inter.channel)  # type: ignore

class ServiceView(discord.ui.View):
    def __init__(self, pairs: List[Tuple[str, int]]):
        super().__init__(timeout=None)
        for label, price in pairs:
            self.add_item(ServiceButton(label, price))

@bot.tree.command(name=ls("service_create", ja="サービス作成"), description=ls("Create service panel", ja="サービス販売パネルを作成"))
@app_commands.describe(
    title="サービスのタイトル",
    description="サービスの紹介",
    label1="ボタン1 文言", price1="ボタン1 金額",
    label2="ボタン2 文言", price2="ボタン2 金額",
    label3="ボタン3 文言", price3="ボタン3 金額",
    label4="ボタン4 文言", price4="ボタン4 金額",
    label5="ボタン5 文言", price5="ボタン5 金額",
    label6="ボタン6 文言", price6="ボタン6 金額",
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
        await inter.response.send_message("少なくとも1つのボタンを指定してください。", ephemeral=True)
        return

    e = em_title(title)
    e.description = description
    await inter.response.send_message(embed=e, view=ServiceView(pairs))

# =============================
# 🧾 チケット掲示板（自動更新）
# =============================
async def update_ticket_board_message(channel: discord.abc.Messageable):
    assert bot.db is not None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    guild = channel.guild

    msg_id = await get_board_message_id(bot.db, guild.id, channel.id, "ticket")
    summary = await fetch_ticket_summary(bot.db, guild.id)

    e = em_title("サービスチケット掲示板（自動更新）")
    if not summary:
        e.description = "まだチケットの購入はありません。"
    else:
        lines = []
        for uid, labels in summary.items():
            member = guild.get_member(uid)
            name = member.mention if member else f"<@{uid}>"
            label_str = " / ".join([f"{lbl}:{cnt}" for lbl, cnt in labels.items()])
            lines.append(f"{name}: {label_str}")
        e.description = "\n".join(lines)[:4000]

    try:
        if msg_id:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=e)
        else:
            msg = await channel.send(embed=e)
            await upsert_board(bot.db, guild.id, channel.id, "ticket", msg.id)
            await bot.db.commit()
    except discord.Forbidden:
        logger.warning("掲示板更新に失敗: 権限不足")
    except discord.HTTPException:
        logger.exception("掲示板更新に失敗: HTTPException")

@bot.tree.command(name=ls("setup_ticket_board", ja="チケット掲示板作成"), description=ls("Create ticket board", ja="このチャンネルにチケット掲示板を作成 / 再作成"))
async def setup_ticket_board(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    await update_ticket_board_message(inter.channel)  # type: ignore
    await inter.followup.send("チケット掲示板を用意しました（以後、自動更新）。", ephemeral=True)

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

    @discord.ui.button(label="承諾", style=discord.ButtonStyle.success)
    async def accept(self, inter: discord.Interaction, btn: discord.ui.Button):
        assert bot.db is not None
        await bot.db.execute("UPDATE contracts SET status='accepted', accepted_at=? WHERE id=?", (jst_now_str(), self.contract_id))
        await bot.db.commit()
        await inter.response.edit_message(view=None)
        await inter.channel.send(f"✅ 契約が承諾されました。<@{self.initiator_id}> vs <@{self.opponent_id}>")

    @discord.ui.button(label="拒否", style=discord.ButtonStyle.danger)
    async def decline(self, inter: discord.Interaction, btn: discord.ui.Button):
        assert bot.db is not None
        await bot.db.execute("UPDATE contracts SET status='declined' WHERE id=?", (self.contract_id,))
        await bot.db.commit()
        await inter.response.edit_message(view=None)
        await inter.channel.send(f"❌ 契約が拒否されました。<@{self.initiator_id}> vs <@{self.opponent_id}>")

    async def on_timeout(self) -> None:
        assert bot.db is not None
        await bot.db.execute("UPDATE contracts SET status='declined' WHERE id=? AND status='pending'", (self.contract_id,))
        await bot.db.commit()

@bot.tree.command(name=ls("contract", ja="契約"), description=ls("Propose a contract", ja="勝負契約を相手に提示します（5分以内に承諾/拒否）"))
@app_commands.describe(opponent="相手ユーザー", content="勝負の内容")
async def contract(inter: discord.Interaction, opponent: discord.Member, content: str):
    assert bot.db is not None
    guild = inter.guild
    assert guild is not None

    if opponent.bot or opponent.id == inter.user.id:
        await inter.response.send_message("相手ユーザーの指定が不正です。", ephemeral=True)
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

    e = em_title("契約の提案")
    e.description = f"**{inter.user.mention}** vs **{opponent.mention}**\n\n**勝負内容:**\n{content}\n\n5分以内に承諾または拒否してください。"
    view = ContractView(inter.user.id, opponent.id, contract_id)
    await inter.response.send_message(embed=e, view=view)

    async def timeout_task():
        await asyncio.sleep(305)
        cur2 = await bot.db.execute("SELECT status FROM contracts WHERE id=?", (contract_id,))
        r = await cur2.fetchone()
        if r and r[0] == 'pending':
            await bot.db.execute("UPDATE contracts SET status='declined' WHERE id=?", (contract_id,))
            await bot.db.commit()
            try:
                await inter.channel.send(f"⌛ タイムアウト: 契約は拒否扱いになりました。（ID: {contract_id}）")
            except Exception:
                pass
    bot.loop.create_task(timeout_task())

# =============================
# ✅ 契約終了（相手の承認が必要）
# =============================
class ResultConfirmView(discord.ui.View):
    def __init__(self, confirmer_id: int, on_confirm: Callable[[discord.Interaction], Awaitable[None]]):
        super().__init__(timeout=300)
        self.confirmer_id = confirmer_id
        self.on_confirm = on_confirm

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        return inter.user.id == self.confirmer_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, inter: discord.Interaction, btn: discord.ui.Button):
        await self.on_confirm(inter)

@bot.tree.command(name=ls("contract_close", ja="契約終了"), description=ls("Submit duel result", ja="勝負の結果を申請します（相手の承認が必要）"))
@app_commands.describe(opponent="勝負相手", result="あなたの結果")
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
        await inter.response.send_message("承諾済みの契約が見つかりません。", ephemeral=True)
        return
    cid, content = int(row[0]), str(row[1])

    confirmer_id = opponent.id

    async def on_confirm(confirm_inter: discord.Interaction):
        await bot.db.execute("UPDATE contracts SET status='closed' WHERE id=?", (cid,))
        await bot.db.commit()
        await confirm_inter.response.edit_message(view=None)
        await confirm_inter.channel.send("✅ 勝負結果が確定しました。掲示板を更新します。")
        await append_result_board(confirm_inter.channel, inter.user, opponent, content, result.value)

    e = em_title("勝負結果の確認")
    result_ja = "勝利" if result.value == "win" else "敗北"
    e.description = (
        f"**勝負内容**\n{content}\n\n"
        f"申請者: {inter.user.mention} → 結果: **{result_ja}**\n"
        f"相手: {opponent.mention} の承認が必要です。"
    )
    await inter.response.send_message(embed=e, view=ResultConfirmView(confirmer_id, on_confirm))

async def append_result_board(channel: discord.abc.Messageable, user_a: discord.Member, user_b: discord.Member, content: str, result: str):
    assert bot.db is not None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    guild = channel.guild

    msg_id = await get_board_message_id(bot.db, guild.id, channel.id, "contract_result")

    symbol = "🏆" if result == "win" else "⚑"
    line = f"{jst_now_str()} — {user_a.mention} vs {user_b.mention} → {user_a.mention} {('勝利' if result=='win' else '敗北')} {symbol}\n内容: {content}"

    e = em_title("勝負結果掲示板（自動更新）")

    try:
        if msg_id:
            msg = await channel.fetch_message(msg_id)
            old = msg.embeds[0].description if msg.embeds else ""
            new_desc = (line + "\n" + (old or "")).strip()
            e.description = new_desc[:4000]
            await msg.edit(embed=e)
        else:
            e.description = line
            msg = await channel.send(embed=e)
            await upsert_board(bot.db, guild.id, channel.id, "contract_result", msg.id)
            await bot.db.commit()
    except Exception:
        logger.exception("勝負結果掲示板の更新に失敗")

@bot.tree.command(name=ls("setup_result_board", ja="勝負結果掲示板作成"), description=ls("Create result board", ja="このチャンネルに勝負結果掲示板を作成 / 再作成"))
async def setup_result_board(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    e = em_title("勝負結果掲示板（自動更新）")
    e.description = "ここに勝負結果が時系列で追記されます。"
    msg = await inter.channel.send(embed=e)  # type: ignore
    assert bot.db is not None
    await upsert_board(bot.db, inter.guild.id, inter.channel.id, "contract_result", msg.id)  # type: ignore
    await bot.db.commit()
    await inter.followup.send("勝負結果掲示板を用意しました。", ephemeral=True)

# =============================
# 🟢 起動時の確認ログ
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

# 🛠 デバッグ: このチャンネルでコマンドが見えるはずか？ を診断
@bot.tree.command(name=ls("debug", ja="デバッグ"), description=ls("Debug command visibility", ja="コマンド可視性を診断します"))
async def debug(inter: discord.Interaction):
    # 1) ギルドに何コマンド登録済みか
    guild_cmds = await bot.tree.fetch_commands(guild=inter.guild)  # type: ignore
    names = [c.name for c in guild_cmds]

    # 2) このチャンネルの権限（@everyone 目線）
    everyone = inter.guild.default_role  # type: ignore
    ch_perms_everyone = inter.channel.permissions_for(everyone)  # type: ignore

    # 3) Bot 自身の権限
    me_member = inter.guild.me  # type: ignore
    ch_perms_me = inter.channel.permissions_for(me_member)  # type: ignore

    def yn(b: bool) -> str:
        return "✅" if b else "❌"

    # discord.py 2.x Permissions フラグ
    can_use_app_cmds_everyone = getattr(ch_perms_everyone, "use_application_commands", False)
    can_use_app_cmds_me = getattr(ch_perms_me, "use_application_commands", False)
    can_send = ch_perms_me.send_messages and ch_perms_me.read_messages and ch_perms_me.read_message_history
    can_embed = ch_perms_me.embed_links

    e = discord.Embed(title="デバッグ: コマンド可視性", color=0x95a5a6)
    e.add_field(name="登録コマンド数", value=str(len(guild_cmds)), inline=True)
    e.add_field(name="登録コマンド一覧", value=", ".join(names) or "(なし)", inline=False)
    e.add_field(name="このチャンネルの @everyone", value=f"Use Application Commands: {yn(can_use_app_cmds_everyone)}", inline=False)
    e.add_field(name="このチャンネルの Bot 権限", value=(
        f"Use Application Commands: {yn(can_use_app_cmds_me)}\n"
        f"Send Messages: {yn(can_send)} / Embed Links: {yn(can_embed)}"
    ), inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)
# =============================
# 🚀 起動
# =============================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("環境変数 DISCORD_TOKEN が未設定です")
    bot.run(DISCORD_TOKEN)


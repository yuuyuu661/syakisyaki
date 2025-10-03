import os
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple, List, Callable


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
user_id INTEGER NOT NULL,
balance INTEGER NOT NULL DEFAULT 0,
PRIMARY KEY (guild_id, user_id)
);


CREATE TABLE IF NOT EXISTS tickets (
guild_id INTEGER NOT NULL,
user_id INTEGER NOT NULL,
label TEXT NOT NULL,
count INTEGER NOT NULL DEFAULT 0,
PRIMARY KEY (guild_id, user_id, label)
);


CREATE TABLE IF NOT EXISTS boards (
guild_id INTEGER NOT NULL,
channel_id INTEGER NOT NULL,
kind TEXT NOT NULL, -- 'ticket' | 'contract_result'
message_id INTEGER NOT NULL,
PRIMARY KEY (guild_id, channel_id, kind)
);


CREATE TABLE IF NOT EXISTS contracts (
id INTEGER PRIMARY KEY AUTOINCREMENT,
guild_id INTEGER NOT NULL,
initiator INTEGER NOT NULL,
opponent INTEGER NOT NULL,
content TEXT NOT NULL,
status TEXT NOT NULL, -- 'pending'|'accepted'|'declined'|'closed'
created_at TEXT NOT NULL,
accepted_at TEXT
);
"""


# =============================
# 🛠️ ユーティリティ
# =============================


def jst_now_str() -> str:
return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


await inter.followup.send("勝負結果掲示板を用意しました。", ephemeral=True)
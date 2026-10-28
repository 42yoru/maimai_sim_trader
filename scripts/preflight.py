"""开盘前自检：确认妙想、行情、群会话都就绪。

**仅开发者本地自检，不属于插件运行时。** 它会以只读方式打开宿主实例的
``data/MaiBot.db`` 查 ``chat_sessions``，以此确认配置里的群号确实存在；
插件本身不碰宿主数据库，运行时一律走 ``chat.get_group_streams`` 能力。
这段 SQL 只是给开发者省事的离线检查，宿主库结构一变它就会报错。

实例目录默认按「插件目录的上两级」推断，可用环境变量覆盖：

    set MAIBOT_DIR=E:\\YeYe\\maibot && python scripts/preflight.py

不打印任何密钥。用法（在插件目录下）：

    python scripts/preflight.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import asyncio
import os
import sqlite3
import sys
import tomllib

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MABOT_DIR = Path(os.environ.get("MAIBOT_DIR") or PLUGIN_DIR.parents[1])
sys.path.insert(0, str(PLUGIN_DIR))

from mx_client import MxApiError, MxClient  # noqa: E402
from quotes import TencentQuoteClient  # noqa: E402
from watchlist import WatchlistStore  # noqa: E402

RESULTS: List[Tuple[str, bool, str]] = []


def _money(value: Any) -> str:
    """把妙想返回的金额字段转成可读文本；缺失或不是数字时不崩，只如实显示。"""

    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return f"{value!r}"


def record(name: str, ok: bool, detail: str) -> None:
    """记一条检查结果。"""

    RESULTS.append((name, ok, detail))


def load_config() -> Dict[str, Any]:
    """读取插件配置。"""

    with open(PLUGIN_DIR / "config.toml", "rb") as handle:
        return tomllib.load(handle)


def check_group_session(group_id: str, platform: str) -> None:
    """确认 MaiBot 里真有这个群的会话（直接读宿主库，仅本地自检用）。"""

    db_path = MABOT_DIR / "data" / "MaiBot.db"
    if not db_path.exists():
        record("群会话", False, f"找不到数据库 {db_path}；可用环境变量 MAIBOT_DIR 指定实例目录")
        return
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT session_id, group_name, account_id FROM chat_sessions WHERE group_id = ? AND platform = ?",
            (group_id, platform),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        record("群会话", False, f"查询 chat_sessions 失败（{exc}）；宿主库结构可能变了，忽略此项")
        return
    finally:
        conn.close()
    if not rows:
        record("群会话", False, f"数据库里没有 {platform} 群 {group_id}")
        return
    if len(rows) > 1:
        detail = "、".join(f"{row[0][:8]}…({row[1]})" for row in rows)
        record("群会话", False, f"命中 {len(rows)} 条，无法消歧：{detail}")
        return
    session_id, group_name, account_id = rows[0]
    record("群会话", True, f"{session_id[:8]}… 「{group_name}」 账号 {account_id}")


async def main() -> None:
    config = load_config()
    mx_section = config.get("mx")
    if not isinstance(mx_section, dict):
        record("妙想 Key", False, "config.toml 里没有 [mx] 段，先按 config.toml.example 补上")
        return
    api_key = str(mx_section.get("api_key") or "").strip()
    if not api_key:
        record("妙想 Key", False, "[mx] api_key 为空；也可以改用环境变量 MX_APIKEY")
        return

    mx = MxClient(api_key, base_url=str(mx_section.get("base_url") or ""))
    quotes = TencentQuoteClient()
    try:
        try:
            balance = await mx.get_balance()
            record(
                "妙想账户",
                True,
                "「{}」总资产 {} 可用 {}".format(
                    balance.get("accName") or "(未命名)",
                    _money(balance.get("totalAssets")),
                    _money(balance.get("availBalance")),
                ),
            )
        except MxApiError as exc:
            record("妙想账户", False, f"{exc}（code={exc.code}）")
            return

        try:
            holdings = await mx.get_positions()
            record("妙想持仓", True, f"{len(holdings)} 只" + ("（空仓）" if not holdings else ""))
        except MxApiError as exc:
            record("妙想持仓", False, str(exc))

        pool = WatchlistStore(PLUGIN_DIR / "data" / "watchlist.json").load()
        try:
            snapshot = await quotes.fetch([item.code for item in pool])
            missing = [item.code for item in pool if item.code not in snapshot]
            record("腾讯行情", not missing, f"{len(snapshot)}/{len(pool)} 只" + (f"，缺 {missing}" if missing else ""))
        except Exception as exc:
            record("腾讯行情", False, str(exc))

        notify = config.get("notify") if isinstance(config.get("notify"), dict) else {}
        for group_id in notify.get("group_ids") or []:
            check_group_session(str(group_id), str(notify.get("platform") or "qq"))

        if notify.get("private_enabled"):
            record("私聊播报", True, f"已启用：{notify.get('private_user_ids')}")
        else:
            record("私聊播报", True, "已实现但关闭（private_enabled = false）")
    finally:
        await mx.close()
        await quotes.close()


def print_report() -> int:
    """打印结果，返回退出码。"""

    print("=" * 60)
    failures = 0
    for name, ok, detail in RESULTS:
        mark = "OK  " if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {name:<10} {detail}")
    print("=" * 60)
    if failures:
        print(f"{failures} 项未通过，开盘前先处理。")
        return 1
    print("全部就绪，等开盘。")
    return 0


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(print_report())

"""运行状态：上次看盘、当日节奏、最近几轮记录。

只放「跨轮需要记住」的东西，行情和账户每次都现查，绝不在本地缓存钱。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import json
import os
from datetime import date
from pathlib import Path

STATE_VERSION = 1
MAX_ROUND_RECORDS = 24
MAX_ROUND_ACTIONS = 6
HISTORY_IN_PROMPT = 8


class StateError(RuntimeError):
    """状态文件损坏或版本不符。"""


@dataclass
class RoundRecord:
    """一轮决策留下的结论，供后续轮次参考。

    只记「当时怎么想、做了什么」，行情每次现查，避免长在状态里过期。
    """

    at: str = ""
    """决策时间，形如 `10-24 14:20`。"""

    mood: str = ""
    """当时的心情。"""

    reasoning: str = ""
    """当时的判断理由。"""

    actions: List[str] = field(default_factory=list)
    """这一轮实际执行/被拦下的动作，一行一条。"""

    def to_dict(self) -> Dict[str, Any]:
        """序列化。"""

        return {
            "at": self.at,
            "mood": self.mood,
            "reasoning": self.reasoning,
            "actions": list(self.actions),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RoundRecord":
        """反序列化；缺字段按空处理，类型不对直接报错。"""

        actions = raw.get("actions") or []
        if not isinstance(actions, list):
            raise StateError("round_records.actions 不是数组")
        return cls(
            at=str(raw.get("at") or ""),
            mood=str(raw.get("mood") or ""),
            reasoning=str(raw.get("reasoning") or ""),
            actions=[str(item) for item in actions],
        )

    def to_prompt_block(self) -> str:
        """渲染成给模型看的小块：时间、心情、判断、动作。"""

        head = f"{self.at}｜心情：{self.mood or '平静'}" if self.at else f"心情：{self.mood or '平静'}"
        lines = [head]
        if self.reasoning:
            lines.append(f"判断：{self.reasoning}")
        lines.append("动作：" + ("；".join(self.actions) if self.actions else "没动"))
        return "\n".join(lines)


@dataclass
class RuntimeState:
    """插件运行状态。"""

    trade_date: str = ""
    check_count: int = 0
    trade_count: int = 0
    next_check_at: float = 0.0
    last_mood: str = ""
    last_reasoning: str = ""
    empty_since: str = ""
    round_records: List[RoundRecord] = field(default_factory=list)
    reported_dates: List[str] = field(default_factory=list)

    def roll_date(self, today: str) -> bool:
        """跨日就重置当日计数，返回是否发生了重置。"""

        if self.trade_date == today:
            return False
        self.trade_date = today
        self.check_count = 0
        self.trade_count = 0
        self.last_mood = ""
        self.last_reasoning = ""
        return True

    def track_position_state(self, *, has_position: bool, today: str) -> None:
        """记录空仓起点：一旦建仓就清掉，重新空仓则重新计时。"""

        if has_position:
            self.empty_since = ""
        elif not self.empty_since:
            self.empty_since = today

    def empty_days(self, today: str) -> int:
        """已经连续空仓几个自然日（含今天）。"""

        if not self.empty_since:
            return 0
        try:
            start = date.fromisoformat(self.empty_since)
            current = date.fromisoformat(today)
        except ValueError:
            return 0
        return max(0, (current - start).days + 1)

    def record_round(self, *, record: RoundRecord, trade_count: int) -> None:
        """记下一轮的结论，供后续 prompt 使用。"""

        self.check_count += 1
        self.trade_count += trade_count
        self.last_mood = record.mood
        self.last_reasoning = record.reasoning
        self.round_records.append(record)
        self.round_records = self.round_records[-MAX_ROUND_RECORDS:]

    def mark_reported(self, today: str) -> None:
        """记下今天已经播报过，避免重复发收盘复盘。"""

        if today not in self.reported_dates:
            self.reported_dates.append(today)
            self.reported_dates = self.reported_dates[-7:]

    def to_dict(self) -> Dict[str, Any]:
        """序列化。"""

        return {
            "version": STATE_VERSION,
            "trade_date": self.trade_date,
            "check_count": self.check_count,
            "trade_count": self.trade_count,
            "next_check_at": self.next_check_at,
            "last_mood": self.last_mood,
            "last_reasoning": self.last_reasoning,
            "empty_since": self.empty_since,
            "round_records": [record.to_dict() for record in self.round_records],
            "reported_dates": list(self.reported_dates),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RuntimeState":
        """反序列化；字段类型不对就报错。

        老状态文件只有 `recent_notes` 这一串一行记录，这里顺手迁移成结构化记录，
        避免升级后历史直接消失。
        """

        version = raw.get("version")
        if version != STATE_VERSION:
            raise StateError(f"状态文件版本不匹配（期望 {STATE_VERSION}，实际 {version}）")
        raw_records = raw.get("round_records")
        if raw_records is None:
            # 兼容旧格式：`14:31 没动` 这种一行记录本来就没有判断理由，
            # 直接放进 actions，别硬凑成「判断：14:31 没动 / 动作：没动」
            legacy_notes = raw.get("recent_notes") or []
            if not isinstance(legacy_notes, list):
                raise StateError("recent_notes 不是数组")
            records = [
                RoundRecord(actions=[str(item).strip()])
                for item in legacy_notes
                if str(item).strip()
            ]
        else:
            if not isinstance(raw_records, list):
                raise StateError("round_records 不是数组")
            records = [RoundRecord.from_dict(item) for item in raw_records]
        return cls(
            trade_date=str(raw.get("trade_date") or ""),
            check_count=int(raw.get("check_count") or 0),
            trade_count=int(raw.get("trade_count") or 0),
            next_check_at=float(raw.get("next_check_at") or 0.0),
            last_mood=str(raw.get("last_mood") or ""),
            last_reasoning=str(raw.get("last_reasoning") or ""),
            empty_since=str(raw.get("empty_since") or ""),
            round_records=records,
            reported_dates=[str(item) for item in (raw.get("reported_dates") or [])],
        )


class StateStore:
    """状态落盘，原子写。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._state: RuntimeState | None = None

    @property
    def path(self) -> Path:
        """状态文件路径。"""

        return self._path

    def load(self) -> RuntimeState:
        """读取状态；文件不存在就是全新状态。"""

        if self._state is not None:
            return self._state
        if not self._path.exists():
            self._state = RuntimeState()
            return self._state
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(f"状态文件不是合法 JSON: {self._path}") from exc
        if not isinstance(raw, dict):
            raise StateError(f"状态文件根节点必须是对象: {self._path}")
        self._state = RuntimeState.from_dict(raw)
        return self._state

    def save(self) -> None:
        """原子写回。"""

        if self._state is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self._state.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._path)


__all__ = [
    "HISTORY_IN_PROMPT",
    "MAX_ROUND_ACTIONS",
    "MAX_ROUND_RECORDS",
    "STATE_VERSION",
    "RoundRecord",
    "RuntimeState",
    "StateError",
    "StateStore",
]

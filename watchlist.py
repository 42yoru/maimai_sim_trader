"""股票池：可增删、原子落盘的候选列表。

池子是麦麦自己的东西——她可以在决策里加人、踢人，主人也能通过命令改。
默认给 20 只沪深300 里的熟面孔，风格分散。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import json
import os
import re
import time
from pathlib import Path

POOL_VERSION = 1

# 沪深股票代码统一是六位数字；不校验的话，池子里混进一个错码就会在每轮取行情时抛错
CODE_PATTERN = re.compile(r"^\d{6}$")


def is_valid_code(code: str) -> bool:
    """判断是不是六位数字代码。"""

    return bool(CODE_PATTERN.fullmatch(str(code).strip()))


DEFAULT_POOL: tuple[tuple[str, str], ...] = (
    ("000333", "美的集团"),
    ("000858", "五粮液"),
    ("000651", "格力电器"),
    ("600887", "伊利股份"),
    ("601888", "中国中免"),
    ("600036", "招商银行"),
    ("601318", "中国平安"),
    ("600030", "中信证券"),
    ("601601", "中国太保"),
    ("002475", "立讯精密"),
    ("601138", "工业富联"),
    ("300274", "阳光电源"),
    ("002415", "海康威视"),
    ("601899", "紫金矿业"),
    ("600309", "万华化学"),
    ("600660", "福耀玻璃"),
    ("600276", "恒瑞医药"),
    ("000538", "云南白药"),
    ("600900", "长江电力"),
    ("601088", "中国神华"),
)


class WatchlistError(RuntimeError):
    """股票池文件损坏或格式非法。"""


@dataclass(frozen=True)
class PoolItem:
    """池子里的一只票。"""

    code: str
    name: str
    added_at: float
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化成落盘结构。"""

        return {"code": self.code, "name": self.name, "added_at": self.added_at, "note": self.note}


class WatchlistStore:
    """股票池落盘与增删。

    首次加载时若文件不存在，用默认池初始化并写盘；文件存在但损坏则直接抛错，
    不做静默清空，避免把麦麦攒下来的池子悄悄抹掉。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: Optional[List[PoolItem]] = None

    @property
    def path(self) -> Path:
        """池子文件路径。"""

        return self._path

    def _load_file(self) -> Optional[List[PoolItem]]:
        """从磁盘读池子；不存在返回 None。"""

        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WatchlistError(f"股票池文件不是合法 JSON: {self._path}") from exc
        if not isinstance(raw, dict):
            raise WatchlistError(f"股票池文件根节点必须是对象: {self._path}")
        version = raw.get("version")
        if version != POOL_VERSION:
            raise WatchlistError(f"股票池文件版本不匹配（期望 {POOL_VERSION}，实际 {version}）")
        rows = raw.get("items")
        if not isinstance(rows, list):
            raise WatchlistError("股票池文件的 items 不是列表")
        items: List[PoolItem] = []
        for row in rows:
            if not isinstance(row, dict):
                raise WatchlistError("股票池条目不合法")
            code = str(row.get("code") or "").strip()
            name = str(row.get("name") or "").strip()
            if not code or not name:
                raise WatchlistError(f"股票池条目缺少 code/name: {row}")
            items.append(
                PoolItem(
                    code=code,
                    name=name,
                    added_at=float(row.get("added_at") or 0.0),
                    note=str(row.get("note") or ""),
                )
            )
        return items

    def load(self) -> List[PoolItem]:
        """加载池子；不存在时用默认池初始化。"""

        if self._items is None:
            loaded = self._load_file()
            if loaded is None:
                now = time.time()
                loaded = [PoolItem(code=code, name=name, added_at=now, note="默认池") for code, name in DEFAULT_POOL]
                self._items = loaded
                self._save()
            else:
                self._items = loaded
        return list(self._items)

    def all(self) -> List[PoolItem]:
        """当前池子（保持落盘顺序）。"""

        return self.load()

    def codes(self) -> List[str]:
        """池子里所有代码，方便拼 prompt。"""

        return [item.code for item in self.load()]

    def get(self, code: str) -> Optional[PoolItem]:
        """按代码查一只票。"""

        for item in self.load():
            if item.code == code:
                return item
        return None

    def find_by_name(self, text: str) -> Optional[PoolItem]:
        """按名字模糊查一只票。

        先找完全相同的，再找包含关系的；包含关系命中多只时返回 None 要求用户写代码。
        """

        keyword = " ".join(str(text or "").split())
        if not keyword:
            return None
        items = self.load()
        exact = [item for item in items if item.name == keyword]
        if len(exact) == 1:
            return exact[0]
        partial = [item for item in items if keyword in item.name or item.name in keyword]
        if len(partial) == 1:
            return partial[0]
        return None

    def add(self, code: str, name: str, *, note: str = "") -> bool:
        """加一只票；已存在则返回 False。

        代码必须是六位数字：池子里混进错码，每轮取行情都会抛错，
        后台循环会一直失败到有人手动删掉它。
        """

        normalized = code.strip()
        if not normalized:
            raise ValueError("股票代码不能为空")
        if not is_valid_code(normalized):
            raise ValueError(f"股票代码必须是六位数字，收到 {code!r}")
        items = self.load()
        if any(item.code == normalized for item in items):
            return False
        items.append(PoolItem(code=normalized, name=name.strip() or normalized, added_at=time.time(), note=note))
        self._items = items
        self._save()
        return True

    def remove(self, code: str) -> Optional[PoolItem]:
        """踢掉一只票；不在池子里返回 None。"""

        normalized = code.strip()
        items = self.load()
        target = next((item for item in items if item.code == normalized), None)
        if target is None:
            return None
        self._items = [item for item in items if item.code != normalized]
        self._save()
        return target

    def _save(self) -> None:
        """原子写：先写临时文件再替换，避免插件被杀时留下半份状态。"""

        if self._items is None:
            return
        payload = {
            "version": POOL_VERSION,
            "items": [item.to_dict() for item in self._items],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._path)


__all__ = ["DEFAULT_POOL", "POOL_VERSION", "PoolItem", "WatchlistError", "WatchlistStore"]

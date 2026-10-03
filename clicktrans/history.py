"""history.json 读写 —— 历史记录与翻译缓存是同一份数据。

一份数据两个用途：
1. 用户可见的历史记录（离线时唯一可用的功能）
2. 翻译缓存（相同原文 + 语言对直接复用，省一次 API 调用）

写入三条铁律，缺一不可：
1. 原子写：先写 .tmp 再 os.replace，否则断电会留下半截 JSON 导致历史全丢
2. 限流合并：1 秒内的多次写入合成一次，避免连续框选时频繁磁盘 IO
3. 滚动淘汰：超过 limit 条删最旧的，但 pinned 项跳过
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1
_WRITE_DEBOUNCE_S = 1.0
_WS_RE = re.compile(r"\s+")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize(text: str) -> str:
    """去重用的归一化：折叠所有空白。"""
    return _WS_RE.sub(" ", text or "").strip()


@dataclass
class HistoryItem:
    id: str
    ts: str
    src_lang: str
    tgt_lang: str
    engine: str
    source_text: str
    target_text: str
    pinned: bool = False
    pending: bool = False  # 离线时只识别未翻译，等联网补译

    def preview(self, width: int = 60) -> str:
        one_line = normalize(self.source_text)
        return one_line if len(one_line) <= width else one_line[: width - 1] + "…"

    @property
    def direction(self) -> str:
        return f"{self.src_lang.upper()} → {self.tgt_lang.upper()}"


class HistoryStore:
    """线程安全。所有写操作最终都会落到磁盘，但带 1 秒去抖。"""

    def __init__(self, path: Path, limit: int = 500, dedupe: bool = True):
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self.dedupe = bool(dedupe)
        self._items: list[HistoryItem] = []
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._dirty = False
        self.load()

    # ---------------------------------------------------------------- 读取

    def load(self) -> None:
        with self._lock:
            self._items = []
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                # 文件坏了就备份一份再重来，不要让用户彻底失去数据
                try:
                    self.path.replace(self.path.with_suffix(".json.corrupt"))
                except Exception:
                    pass
                return

            items = raw.get("items") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                return
            known = {f for f in HistoryItem.__dataclass_fields__}
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                clean = {k: v for k, v in entry.items() if k in known}
                clean.setdefault("id", uuid.uuid4().hex[:12])
                clean.setdefault("ts", _now_iso())
                clean.setdefault("src_lang", "en")
                clean.setdefault("tgt_lang", "zh")
                clean.setdefault("engine", "")
                clean.setdefault("source_text", "")
                clean.setdefault("target_text", "")
                try:
                    self._items.append(HistoryItem(**clean))
                except TypeError:
                    continue

    # ---------------------------------------------------------------- 查询

    def all(self) -> list[HistoryItem]:
        """按时间倒序。"""
        with self._lock:
            return sorted(self._items, key=lambda i: i.ts, reverse=True)

    def stats(self) -> tuple[int, int, int]:
        """返回 (总条数, 普通条数, 配额)。置顶不占配额，所以要分开报。"""
        with self._lock:
            pinned = sum(1 for i in self._items if i.pinned)
            return len(self._items), len(self._items) - pinned, self.limit

    def find(self, source_text: str, src_lang: str, tgt_lang: str) -> HistoryItem | None:
        """命中条件：归一化原文相同 + 语言对相同 + 已有译文。"""
        if not self.dedupe:
            return None
        key = normalize(source_text)
        if not key:
            return None
        with self._lock:
            for item in sorted(self._items, key=lambda i: i.ts, reverse=True):
                if item.pending or not item.target_text:
                    continue
                if item.src_lang != src_lang or item.tgt_lang != tgt_lang:
                    continue
                if normalize(item.source_text) == key:
                    return item
        return None

    def search(self, query: str = "", direction: str = "") -> list[HistoryItem]:
        """纯字符串匹配，全量在内存里，没有延迟。"""
        q = normalize(query).lower()
        with self._lock:
            results = []
            for item in self.all():
                if direction and direction != "all":
                    if f"{item.src_lang}-{item.tgt_lang}" != direction:
                        continue
                if q:
                    haystack = f"{item.source_text}\n{item.target_text}".lower()
                    if q not in haystack:
                        continue
                results.append(item)
            return results

    def pendings(self) -> list[HistoryItem]:
        with self._lock:
            return [i for i in self._items if i.pending]

    # ---------------------------------------------------------------- 写入

    def add(
        self,
        source_text: str,
        target_text: str,
        src_lang: str,
        tgt_lang: str,
        engine: str = "",
        pending: bool = False,
    ) -> HistoryItem:
        item = HistoryItem(
            id=f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}",
            ts=_now_iso(),
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            engine=engine,
            source_text=source_text,
            target_text=target_text,
            pending=pending,
        )
        with self._lock:
            self._items.append(item)
            self._evict()
            self._touch()
        return item

    def update_target(self, item_id: str, target_text: str, engine: str = "") -> bool:
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    item.target_text = target_text
                    item.pending = False
                    if engine:
                        item.engine = engine
                    item.ts = _now_iso()
                    self._touch()
                    return True
        return False

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.id != item_id]
            changed = len(self._items) != before
            if changed:
                self._touch()
            return changed

    def set_pinned(self, item_id: str, pinned: bool) -> bool:
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    item.pinned = pinned
                    self._touch()
                    return True
        return False

    def clear(self, keep_pinned: bool = True) -> int:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.pinned] if keep_pinned else []
            self._evict()
            self._touch()
            return before - len(self._items)

    def _evict(self) -> None:
        """滚动淘汰最旧的普通条目。

        语义是「置顶项不占配额」：置顶的永远留，普通条目另外保留 limit 条。
        所以文件上限是 limit + 用户置顶的数量——置顶是用户的主动行为，
        让它撑大文件是合理的，不该被静默删掉。
        """
        pinned = [i for i in self._items if i.pinned]
        movable = sorted((i for i in self._items if not i.pinned), key=lambda i: i.ts)
        if len(movable) > self.limit:
            movable = movable[-self.limit :]
        self._items = pinned + movable

    # -------------------------------------------------------------- 落盘

    def _touch(self) -> None:
        self._dirty = True
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(_WRITE_DEBOUNCE_S, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def flush(self) -> None:
        """原子写。退出前务必手动调一次。"""
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            payload = {
                "version": SCHEMA_VERSION,
                "items": [asdict(i) for i in sorted(self._items, key=lambda x: x.ts)],
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except Exception:
                self._dirty = True  # 下次再试

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.flush()


def export_markdown(items: Iterable[HistoryItem]) -> str:
    """导出成 Markdown，方便贴进笔记或做成复习材料。"""
    lines: list[str] = []
    for item in items:
        lines.append(f"## {item.direction}　{item.ts}")
        lines.append("")
        lines.append("**原文**")
        lines.append("")
        lines.append(item.source_text)
        lines.append("")
        lines.append("**译文**")
        lines.append("")
        lines.append(item.target_text or "（未翻译）")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)

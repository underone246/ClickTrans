"""history.json 的读写、去重、滚动淘汰。"""

from __future__ import annotations

import json

from clicktrans.history import HistoryStore, export_markdown, normalize


def make_store(tmp_path, limit=500, dedupe=True, name="history.json"):
    return HistoryStore(tmp_path / name, limit=limit, dedupe=dedupe)


class TestBasic:
    def test_add_and_find(self, tmp_path):
        store = make_store(tmp_path)
        store.add("Hello world", "你好世界", "en", "zh", engine="test")
        hit = store.find("Hello world", "en", "zh")
        assert hit is not None
        assert hit.target_text == "你好世界"
        store.close()

    def test_find_is_language_pair_sensitive(self, tmp_path):
        store = make_store(tmp_path)
        store.add("Hello", "你好", "en", "zh")
        assert store.find("Hello", "en", "zh") is not None
        assert store.find("Hello", "en", "ja") is None
        store.close()

    def test_find_ignores_whitespace_differences(self, tmp_path):
        store = make_store(tmp_path)
        store.add("Hello   world\n\nfoo", "译文", "en", "zh")
        assert store.find("Hello world foo", "en", "zh") is not None
        store.close()

    def test_pending_items_are_not_cache_hits(self, tmp_path):
        """离线暂存的条目没有译文，不能被当成缓存命中。"""
        store = make_store(tmp_path)
        store.add("Hello", "", "en", "zh", pending=True)
        assert store.find("Hello", "en", "zh") is None
        assert len(store.pendings()) == 1
        store.close()

    def test_dedupe_can_be_disabled(self, tmp_path):
        store = make_store(tmp_path, dedupe=False)
        store.add("Hello", "你好", "en", "zh")
        assert store.find("Hello", "en", "zh") is None
        store.close()

    def test_update_target_clears_pending(self, tmp_path):
        store = make_store(tmp_path)
        item = store.add("Hello", "", "en", "zh", pending=True)
        assert store.update_target(item.id, "你好", engine="test")
        assert store.pendings() == []
        assert store.find("Hello", "en", "zh") is not None
        store.close()


class TestEviction:
    def test_rolling_eviction_keeps_newest(self, tmp_path):
        store = make_store(tmp_path, limit=5)
        for i in range(12):
            store.add(f"text-{i}", f"译文-{i}", "en", "zh")
        assert len(store.all()) == 5
        texts = {item.source_text for item in store.all()}
        assert texts == {f"text-{i}" for i in range(7, 12)}
        store.close()

    def test_pinned_survives_eviction(self, tmp_path):
        store = make_store(tmp_path, limit=3)
        pinned = store.add("keep-me", "保留", "en", "zh")
        store.set_pinned(pinned.id, True)
        for i in range(10):
            store.add(f"text-{i}", f"译文-{i}", "en", "zh")

        texts = {item.source_text for item in store.all()}
        assert "keep-me" in texts
        # 置顶不占配额，所以另外还能留 3 条
        assert len(store.all()) == 4
        store.close()

    def test_clear_keeps_pinned(self, tmp_path):
        store = make_store(tmp_path)
        pinned = store.add("keep", "保留", "en", "zh")
        store.set_pinned(pinned.id, True)
        store.add("drop", "删除", "en", "zh")

        removed = store.clear(keep_pinned=True)
        assert removed == 1
        assert [i.source_text for i in store.all()] == ["keep"]
        store.close()

    def test_clear_all(self, tmp_path):
        store = make_store(tmp_path)
        store.add("a", "甲", "en", "zh")
        store.add("b", "乙", "en", "zh")
        assert store.clear(keep_pinned=False) == 2
        assert store.all() == []
        store.close()


class TestPersistence:
    def test_writes_valid_json_atomically(self, tmp_path):
        path = tmp_path / "history.json"
        store = HistoryStore(path)
        store.add("Hello", "你好", "en", "zh")
        store.close()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert len(payload["items"]) == 1
        # 临时文件必须已经被 os.replace 掉
        assert not (tmp_path / "history.json.tmp").exists()

    def test_reload_from_disk(self, tmp_path):
        path = tmp_path / "history.json"
        first = HistoryStore(path)
        first.add("Hello", "你好", "en", "zh")
        first.close()

        second = HistoryStore(path)
        assert second.find("Hello", "en", "zh") is not None
        second.close()

    def test_missing_file_is_fine(self, tmp_path):
        store = HistoryStore(tmp_path / "nope.json")
        assert store.all() == []
        store.close()

    def test_corrupt_file_is_quarantined(self, tmp_path):
        """坏文件不能直接丢掉 —— 先备份成 .corrupt 再重来。"""
        path = tmp_path / "history.json"
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")

        store = HistoryStore(path)
        assert store.all() == []
        assert (tmp_path / "history.json.corrupt").exists()
        store.close()

    def test_unknown_fields_are_ignored(self, tmp_path):
        """向前兼容：未来版本加的字段不该让旧版本崩溃。"""
        path = tmp_path / "history.json"
        path.write_text(
            json.dumps(
                {
                    "version": 99,
                    "items": [
                        {
                            "id": "x",
                            "ts": "2026-01-01T00:00:00+08:00",
                            "src_lang": "en",
                            "tgt_lang": "zh",
                            "engine": "m",
                            "source_text": "hello",
                            "target_text": "你好",
                            "some_future_field": {"nested": True},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        store = HistoryStore(path)
        assert len(store.all()) == 1
        assert store.all()[0].source_text == "hello"
        store.close()


class TestSearch:
    def test_search_matches_both_sides(self, tmp_path):
        store = make_store(tmp_path)
        store.add("alpha", "阿尔法", "en", "zh")
        store.add("beta", "贝塔", "en", "zh")
        store.add("gamma", "伽马", "ja", "zh")

        assert len(store.search("alpha")) == 1
        assert len(store.search("贝塔")) == 1
        assert len(store.search("")) == 3
        assert len(store.search("", "ja-zh")) == 1
        store.close()

    def test_search_is_case_insensitive(self, tmp_path):
        store = make_store(tmp_path)
        store.add("Hello World", "你好世界", "en", "zh")
        assert len(store.search("hello")) == 1
        store.close()


class TestNormalize:
    def test_collapses_whitespace(self):
        assert normalize("  a \n\n b\t\tc  ") == "a b c"

    def test_empty(self):
        assert normalize("   ") == ""


class TestExport:
    def test_markdown_contains_both_sides(self, tmp_path):
        store = make_store(tmp_path)
        store.add("hello", "你好", "en", "zh")
        markdown = export_markdown(store.all())
        assert "hello" in markdown
        assert "你好" in markdown
        assert "EN → ZH" in markdown
        store.close()

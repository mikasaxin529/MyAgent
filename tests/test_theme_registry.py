"""M1 主题即插即用：theme_registry 注册表测试。

覆盖：
1. 目录扫描：四主题齐全（default 首位），meta 段完整解析
2. match_theme：词长降序匹配（青绿不误捕）、多命中取最长词、无命中 None
3. 即插即用：临时目录写入新主题 JSON → list_themes 自动感知（mtime 缓存失效）
4. theme_chip_labels / themes_hint_for_prompt 派生文案
5. 兜底：损坏 JSON 跳过、缺 meta 段仍可用、themes 目录不可读返回 default

运行：
    PYTHONIOENCODING=utf-8 pytest tests/test_theme_registry.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))

from aidraft.agenthub.yuwen import theme_registry as tr  # noqa: E402


@pytest.fixture
def registry_dir(tmp_path, monkeypatch):
    """把注册表指到临时主题目录：预置 default + fresh-blue，测试自由增删。"""
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "default.json").write_text(json.dumps({
        "name": "default",
        "meta": {"display": "暖橙", "keywords": ["默认", "橙色"],
                 "swatch": ["ED7D31"], "tags": ["通用"]},
    }, ensure_ascii=False), encoding="utf-8")
    (themes / "fresh-blue.json").write_text(json.dumps({
        "name": "fresh-blue",
        "meta": {"display": "青蓝", "keywords": ["青蓝", "蓝色"],
                 "swatch": ["2E7BB5"], "tags": ["清新"]},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(tr, "_THEMES_DIR", themes)
    # 每个用例强制重扫（mtime 缓存对同秒写入的判别不可靠，测试直接失效缓存）
    monkeypatch.setattr(tr, "_cache", {"fingerprint": None, "themes": []})
    yield themes
    # monkeypatch fixture 结束自动还原 _THEMES_DIR 与 _cache


class TestScan:
    def test_scan_basic(self, registry_dir):
        names = [r["name"] for r in tr.list_themes()]
        assert names == ["default", "fresh-blue"]

    def test_records_meta_parsed(self, registry_dir):
        fb = next(r for r in tr.list_themes() if r["name"] == "fresh-blue")
        assert fb["display"] == "青蓝"
        assert "青蓝" in fb["keywords"]
        # 英文名自动追加进 keywords（"换成 fresh-blue" 也能命中）
        assert "fresh-blue" in fb["keywords"]
        assert fb["swatch"] == ["2E7BB5"]
        assert fb["tags"] == ["清新"]

    def test_missing_meta_fallback(self, registry_dir):
        """缺 meta 段的主题仍可用：display=name、keywords=[name]。"""
        (registry_dir / "bare.json").write_text(json.dumps({"name": "bare"}),
                                                encoding="utf-8")
        tr._cache["fingerprint"] = None
        bare = next(r for r in tr.list_themes() if r["name"] == "bare")
        assert bare["display"] == "bare"
        assert bare["keywords"] == ["bare"]

    def test_corrupt_json_skipped(self, registry_dir):
        (registry_dir / "broken.json").write_text("{not json", encoding="utf-8")
        tr._cache["fingerprint"] = None
        names = [r["name"] for r in tr.list_themes()]
        assert "broken" not in names
        assert names == ["default", "fresh-blue"]

    def test_default_always_first_and_present(self, registry_dir):
        (registry_dir / "default.json").unlink()
        tr._cache["fingerprint"] = None
        names = [r["name"] for r in tr.list_themes()]
        assert names[0] == "default"


class TestMatch:
    def test_keyword_match(self, registry_dir):
        assert tr.match_theme("换成青蓝主题") == "fresh-blue"
        assert tr.match_theme("用默认吧") == "default"

    def test_english_name_match(self, registry_dir):
        assert tr.match_theme("switch to fresh-blue please") == "fresh-blue"

    def test_no_match(self, registry_dir):
        assert tr.match_theme("这课讲水彩画") is None
        assert tr.match_theme("") is None

    def test_longest_keyword_wins(self, registry_dir):
        """两个主题都命中时取最长命中词——词长降序语义。

        "青蓝"含"蓝"，若 warm-green 注册了"蓝"字关键词，青蓝(fresh-blue)
        与蓝(warm-green)同时命中，取词长的 fresh-blue。
        """
        (registry_dir / "warm-green.json").write_text(json.dumps({
            "name": "warm-green",
            "meta": {"display": "墨绿", "keywords": ["绿", "蓝", "墨绿"]},
        }, ensure_ascii=False), encoding="utf-8")
        tr._cache["fingerprint"] = None
        assert tr.match_theme("换成青蓝") == "fresh-blue"
        assert tr.match_theme("换成蓝") == "warm-green"
        assert tr.match_theme("墨绿好") == "warm-green"


class TestPlugAndPlay:
    def test_new_theme_auto_discovered(self, registry_dir):
        """即插即用核心契约：放一个新 JSON，不改任何代码即进全链路。"""
        (registry_dir / "sakura.json").write_text(json.dumps({
            "name": "sakura",
            "meta": {"display": "樱花", "keywords": ["樱花", "粉色"],
                     "swatch": ["FF9EC4"], "tags": ["春日"]},
        }, ensure_ascii=False), encoding="utf-8")
        tr._cache["fingerprint"] = None
        # 扫描 / 匹配 / 文案派生全链路感知
        assert "sakura" in tr.theme_names()
        assert tr.match_theme("换成樱花主题") == "sakura"
        assert tr.theme_display("sakura") == "樱花"
        assert "樱花" in tr.themes_hint_for_prompt()
        # 主进程节点侧值域（_page.THEMES 动态派生）同步感知
        from aidraft.agenthub.yuwen.nodes._page import _merge_meta
        meta = _merge_meta({"meta": {"theme": "sakura"}}, {})
        assert meta["theme"] == "sakura"
        # schema 值域是渲染子进程 import 时扫的，主进程不重扫——不在此断言

    def test_cache_invalidates_on_change(self, registry_dir):
        """mtime 缓存：目录变了自动重扫（同进程运行期热加主题）。"""
        assert tr.match_theme("樱花") is None
        (registry_dir / "sakura.json").write_text(json.dumps({
            "name": "sakura", "meta": {"display": "樱花", "keywords": ["樱花"]},
        }, ensure_ascii=False), encoding="utf-8")
        # 不手动失效缓存，靠 fingerprint 差异触发重扫。同秒写入 mtime 相同
        # 的极端情形下 fingerprint 不变——这里改一下已有文件保证目录有变化
        (registry_dir / "fresh-blue.json").write_text(json.dumps({
            "name": "fresh-blue",
            "meta": {"display": "青蓝", "keywords": ["青蓝", "蓝色"]},
        }, ensure_ascii=False), encoding="utf-8")
        assert tr.match_theme("樱花") == "sakura"


class TestDerived:
    def test_chip_labels_top3_non_default(self, registry_dir):
        (registry_dir / "t3.json").write_text(json.dumps(
            {"name": "t3", "meta": {"display": "三", "keywords": []}}),
            encoding="utf-8")
        (registry_dir / "t4.json").write_text(json.dumps(
            {"name": "t4", "meta": {"display": "四", "keywords": []}}),
            encoding="utf-8")
        tr._cache["fingerprint"] = None
        labels = tr.theme_chip_labels()
        assert labels == ["换青蓝主题", "换三主题", "换四主题"]  # 最多 3 个非 default

    def test_hint_format(self, registry_dir):
        hint = tr.themes_hint_for_prompt()
        assert "default=暖橙" in hint
        assert "fresh-blue=青蓝（清新）" in hint

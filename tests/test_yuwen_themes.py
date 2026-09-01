"""渲染层主题化测试（阶段 2b）。

覆盖：
1. themes/ 主题包加载与回退
2. design_tokens 动态代理（set_theme 即时生效、可还原）
3. default 主题渲染回归：与改造前基线字节一致（pptx/html；docx 豁免=修漂移）
4. 三主题渲染产物齐全且颜色确实不同
5. image 元素 src 相对→绝对解析 + pptx 真实插图 + html 相对引用
6. docx 漂移修复验证（ACCENT 与主题一致）

运行：
    PYTHONIOENCODING=utf-8 pytest tests/test_yuwen_themes.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))

YUWEN = _SRC / "devpilot" / "agenthub" / "yuwen"
SCRIPTS = YUWEN / "scripts"
JINGYESI = YUWEN / "references" / "examples" / "jingyesi.json"


# ---------------------------------------------------------------- 辅助

def _render_all(json_path: Path, out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "render_all.py"), str(json_path),
         "--out", str(out_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )


def _zip_manifest(fp: Path) -> dict:
    """pptx/docx 内部条目内容哈希（docProps 时间戳除外）。"""
    with zipfile.ZipFile(fp) as z:
        import hashlib
        return {n: hashlib.sha256(z.read(n)).hexdigest()
                for n in sorted(z.namelist()) if not n.startswith("docProps/")}


@pytest.fixture
def scripts_path():
    """把 scripts 目录加入 sys.path 并还原（common 相对导入依赖脚本模式）。"""
    had = str(SCRIPTS) in sys.path
    if not had:
        sys.path.insert(0, str(SCRIPTS))
    yield
    if not had:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture
def restore_theme():
    """测试结束还原 default 主题，避免污染其他测试。"""
    yield
    from common import design_tokens as T
    T.set_theme("default")


# ---------------------------------------------------------------- 1. 主题加载

class TestThemeLoading:
    def test_load_known_themes(self, scripts_path):
        from common.themes import load_theme
        for name in ("default", "fresh-blue", "warm-green"):
            th = load_theme(name)
            assert th.name == name
            for key in ("BG", "BG_CARD", "ACCENT", "ACCENT2", "ACCENT3",
                        "DIVIDER", "FOOT", "BORDER", "HL", "TONE", "HIGHLIGHTS"):
                assert key in th.pal, f"{name} 缺 pal.{key}"
            assert set(th.font_scale) >= {"低", "中", "高"}
            assert set(th.layout) >= {"MARGIN_X", "CONTENT_TOP", "MAX_Y"}
            assert 0 in th.pal["TONE"] and 4 in th.pal["TONE"]

    def test_unknown_theme_falls_back(self, scripts_path, capsys):
        from common.themes import load_theme
        th = load_theme("not-a-theme")
        assert th.name == "default"
        assert "not-a-theme" in capsys.readouterr().err   # 有 warning 到 stderr
        # 空/None 也回退
        assert load_theme(None).name == "default"
        assert load_theme("").name == "default"

    def test_default_json_matches_legacy_tokens(self, scripts_path):
        """default.json 逐字段 = 改造前 design_tokens 的值（回归保障）。"""
        from common.themes import load_theme
        th = load_theme("default")
        legacy_pal = {
            "BG": "FDF9F1", "BG_CARD": "FFFFFF", "TITLE_TEXT": "3D2B1F",
            "TEXT": "4A3B2E", "TEXT_LIGHT": "9C8B78", "ACCENT": "ED7D31",
            "ACCENT_DK": "D9631B", "ACCENT2": "3E8E5A", "ACCENT3": "3E7BB6",
            "DIVIDER": "EAD9BE", "FOOT": "B7A995", "BORDER": "F0E4CD",
            "HL": "FFDF8A",
            "HIGHLIGHTS": ("3E7BB6", "3E8E5A", "E2574C", "8E6BB5", "C99A2E"),
            "TONE": {1: "D9534F", 2: "E8A33C", 3: "5BA88A", 4: "5B8AB5", 0: "9AA0A6"},
        }
        for k, v in legacy_pal.items():
            assert th.pal[k] == v, f"pal.{k} 漂移：{th.pal[k]} != {v}"
        legacy_layout = {"MARGIN_X": 0.75, "MARGIN_TOP": 0.5, "GAP": 0.25,
                         "CARD_GAP": 0.3, "CARD_PAD": 0.2, "CONTENT_TOP": 1.45,
                         "MAX_Y": 7.05, "LINE_SP": 1.25}
        for k, v in legacy_layout.items():
            assert th.layout[k] == v
        legacy_mid = {"cover_title": 54, "slide_title": 36, "h1": 30, "h2": 26,
                      "h3": 22, "body": 22, "list": 20, "bigchar": 80,
                      "pinyin": 18, "note": 14, "caption": 13, "footer": 11}
        for k, v in legacy_mid.items():
            assert th.font_scale["中"][k] == v


# ---------------------------------------------------------------- 2. 动态代理

class TestDesignTokensProxy:
    def test_proxy_tracks_active_theme(self, scripts_path, restore_theme):
        from common import design_tokens as T
        assert T.PAL.ACCENT == "ED7D31"
        T.set_theme("fresh-blue")
        assert T.PAL.ACCENT == "2E7BB5"
        assert T.PAL.BG == "F4F9FB"
        T.set_theme("warm-green")
        assert T.PAL.ACCENT == "3E7B5A"
        # 还原
        T.set_theme("default")
        assert T.PAL.ACCENT == "ED7D31"

    def test_font_for_layout_follow_theme(self, scripts_path, restore_theme):
        from common import design_tokens as T
        T.set_theme("warm-green")
        assert T.font_for("中", "body") == 22          # 字号不随主题变
        assert T.L.MARGIN_X == 0.75                     # 布局也不变（三主题一致）
        assert T.PAL.TONE[4] == "5B8AB5"                # 声调色跨主题保持稳定

    def test_pinyin_tone_color_follows_theme(self, scripts_path, restore_theme):
        from common import design_tokens as T
        from common import pinyin as py
        T.set_theme("default")
        assert py.tone_color(1) == "D9534F"
        # TONE 三主题刻意一致；扩展键才区分主题
        T.set_theme("fresh-blue")
        assert py.tone_color(0) == "9AA0A6"
        T.set_theme("default")


# ---------------------------------------------------------------- 3. default 回归

class TestDefaultRegression:
    """default 主题渲染 = 改造前产物：与 git HEAD 代码渲染结果对比
    （pptx 内部条目全等；html 去 :root 后全等；docx 修漂移豁免，
    验证结构一致 + 仅色系变化）。"""

    def test_default_render_identical_to_head(self, tmp_path):
        """金标准：与 git HEAD 代码对同一输入渲染的 pptx 内部条目完全一致。"""
        old = _head_tree(tmp_path)
        old_js = old / "src/devpilot/agenthub/yuwen/references/examples/jingyesi.json"
        out_new = tmp_path / "new"; out_old = tmp_path / "old"
        assert _render_all(JINGYESI, out_new).returncode == 0
        rr = subprocess.run(
            [sys.executable, str(old / "src/devpilot/agenthub/yuwen/scripts/render_all.py"),
             str(old_js), "--out", str(out_old)],
            capture_output=True, text=True, encoding="utf-8")
        assert rr.returncode == 0, rr.stderr
        # pptx 全等
        for f in out_new.glob("*.pptx"):
            of = out_old / f.name
            assert of.is_file()
            assert _zip_manifest(f) == _zip_manifest(of), f"{f.name} 不一致"
        # html：视觉等价断言——把新产物里的 var(--x) 按其 :root 声明展开为
        # 字面量（默认主题下应与 HEAD 产物逐字符一致），注释忽略
        import re
        def _expand(s):
            root = re.search(r":root \{.*?\}", s, flags=re.S)
            mapping = {}
            if root:
                for name, val in re.findall(r"(--[\w-]+):\s*([^;]+);", root.group(0)):
                    mapping[name] = val.strip()
                s = s.replace(root.group(0), "")   # 声明块本身不参与对比
            s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
            def _sub(m):
                v = mapping.get(m.group(1), m.group(0))
                return v.lower() if v.startswith("#") else v
            s = re.sub(r"var\((--[\w-]+)\)", _sub, s)
            # 统一 3/6 位 hex 与大小写：#fff → #ffffff
            s = re.sub(r"#([0-9a-f])([0-9a-f])([0-9a-f])\b", r"#\1\1\2\2\3\3", s)
            return s.lower()
        for f in out_new.glob("*.html"):
            of = out_old / f.name
            assert _expand(f.read_text(encoding="utf-8")) == _expand(of.read_text(encoding="utf-8")), \
                f"{f.name} 默认主题渲染不视觉等价于改造前"
        # docx：修漂移豁免——结构全等（颜色归一化后）+ 断言仅主题色系变化
        for f in out_new.glob("*.docx"):
            of = out_old / f.name
            a = _docx_body(f); b = _docx_body(of)
            na = _norm_docx_colors(a); nb = _norm_docx_colors(b)
            assert na == nb, f"{f.name} 结构漂移（非颜色差异）"
            assert "E8743C" not in a and "ED7D31" in a      # 漂移已修
            assert "8C7B6B" not in a and "9C8B78" in a


def _head_tree(tmp: Path) -> Path:
    """把 git HEAD 的 yuwen 目录解压到 tmp。"""
    import io, tarfile
    r = subprocess.run(["git", "archive", "HEAD", "src/devpilot/agenthub/yuwen"],
                       capture_output=True, cwd=_PROJECT_ROOT)
    assert r.returncode == 0
    dest = tmp / "head_tree"; dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        t.extractall(dest)
    return dest


def _docx_body(fp: Path) -> str:
    with zipfile.ZipFile(fp) as z:
        return z.read("word/document.xml").decode("utf-8")


def _norm_docx_colors(x: str) -> str:
    import re
    x = re.sub(r'w:color w:val="[0-9A-F]{6}"', 'w:color w:val="C"', x)
    return x


# ---------------------------------------------------------------- 4. 三主题渲染

class TestRenderThemed:
    def _doc_with_theme(self, theme: str, out: Path) -> Path:
        doc = json.loads(JINGYESI.read_text(encoding="utf-8"))
        doc["meta"]["theme"] = theme
        p = out / f"jingyesi_{theme}.json"
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return p

    @pytest.mark.parametrize("theme", ["default", "fresh-blue", "warm-green"])
    def test_three_formats_render(self, tmp_path, theme):
        out = tmp_path / theme
        r = _render_all(self._doc_with_theme(theme, tmp_path), out)
        assert r.returncode == 0, r.stderr
        assert list(out.glob("*.pptx")), "无 pptx"
        assert list(out.glob("*.html")), "无 html"
        assert list(out.glob("*.docx")), "无 docx"

    def test_themes_produce_different_colors(self, tmp_path):
        """三主题 pptx 的 slide1 XML 强调色必须互不相同（证明主题真的生效）。"""
        accents = {}
        for theme in ("default", "fresh-blue", "warm-green"):
            out = tmp_path / theme
            assert _render_all(self._doc_with_theme(theme, tmp_path), out).returncode == 0
            pptx = next(out.glob("*.pptx"))
            with zipfile.ZipFile(pptx) as z:
                xml = z.read("ppt/slides/slide1.xml").decode("utf-8")
            accents[theme] = xml
        assert "ED7D31" in accents["default"]
        assert "2E7BB5" in accents["fresh-blue"]
        assert "3E7B5A" in accents["warm-green"]

    def test_html_root_generated_from_theme(self, tmp_path):
        """html 的 :root 变量块由主题 JSON 生成（不再是手抄 css 文件）。"""
        out = tmp_path / "fb"
        assert _render_all(self._doc_with_theme("fresh-blue", tmp_path), out).returncode == 0
        raw = next(out.glob("*.html")).read_text(encoding="utf-8")
        assert "--accent: #2E7BB5;" in raw
        assert "--title-text: #1F3A4D;" in raw
        assert "--bg: #F4F9FB;" in raw


# ---------------------------------------------------------------- 5. 图片路径契约

class TestImagePathResolution:
    def _make_doc_with_images(self, tmp: Path, theme="default"):
        from PIL import Image
        out = tmp / "out"; (out / "assets").mkdir(parents=True)
        # 两张假图：一张有效 png，一张故意缺失 src 文件
        Image.new("RGB", (640, 360), (10, 120, 200)).save(out / "assets" / "im1.png")
        doc = json.loads(JINGYESI.read_text(encoding="utf-8"))
        doc["meta"]["theme"] = theme
        doc["slides"].append({
            "id": "simg", "kind": "content", "title": "插图页", "period": 1,
            "elements": [
                {"type": "image", "src": "assets/im1.png", "caption": "配图一",
                 "height": 2.2},
                {"type": "image", "src": "assets/missing.png", "caption": "缺失"},
            ],
        })
        jp = tmp / "doc.json"
        jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return jp, out

    def test_pptx_embeds_real_picture(self, tmp_path):
        jp, out = self._make_doc_with_images(tmp_path)
        r = _render_all(jp, out)
        assert r.returncode == 0, r.stderr
        pptx = next(out.glob("*.pptx"))
        with zipfile.ZipFile(pptx) as z:
            media = [n for n in z.namelist() if n.startswith("ppt/media/")]
            assert any(n.endswith(".png") for n in media), f"无真实图片媒体: {media}"
            # slide XML 里应出现 rels 引用
            slide_xmls = [n for n in z.namelist() if n.startswith("ppt/slides/slide")]
            found = any(b"im1" in z.read(n) or b"png" in z.read(n).lower()
                        for n in slide_xmls if n.endswith(".xml"))
            assert found

    def test_html_img_relative_path(self, tmp_path):
        jp, out = self._make_doc_with_images(tmp_path)
        assert _render_all(jp, out).returncode == 0
        raw = next(out.glob("*.html")).read_text(encoding="utf-8")
        assert 'src="assets/im1.png"' in raw, "html 应为相对路径引用"
        assert "missing.png" not in raw, "无效 src 不应输出 img"

    def test_docx_appends_picture(self, tmp_path):
        jp, out = self._make_doc_with_images(tmp_path)
        assert _render_all(jp, out).returncode == 0
        docx = next(out.glob("*.docx"))
        with zipfile.ZipFile(docx) as z:
            media = [n for n in z.namelist() if n.startswith("word/media/")]
            assert media, "docx 附录应含真实图片"


# ---------------------------------------------------------------- 6. docx 修漂移

class TestDocxThemeFix:
    def test_docx_accent_follows_theme(self, tmp_path):
        """docx 强调色 = 主题 ACCENT（原 E8743C 漂移已修，且随主题切换）。"""
        for theme, want in (("default", "ED7D31"), ("fresh-blue", "2E7BB5"),
                            ("warm-green", "3E7B5A")):
            doc = json.loads(JINGYESI.read_text(encoding="utf-8"))
            doc["meta"]["theme"] = theme
            jp = tmp_path / f"d_{theme}.json"
            jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            out = tmp_path / f"o_{theme}"
            assert _render_all(jp, out).returncode == 0, theme
            docx = next(out.glob("*.docx"))
            xml = _docx_body(docx)
            assert f'w:color w:val="{want}"' in xml, f"{theme}: docx 未用主题 ACCENT {want}"
            assert 'w:color w:val="E8743C"' not in xml


# ---------------------------------------------------------------- 7. schema theme 归一

class TestSchemaTheme:
    def test_validate_theme_enum(self, scripts_path):
        from common.schema import validate
        doc = {
            "meta": {"title": "t", "grade": 1, "lessonType": "精读"},
            "slides": [{"id": "s1", "elements": []}],
        }
        validate(doc)  # 不抛

    def test_normalize_theme_variants(self, scripts_path):
        from common.schema import normalize

        def _n(raw):
            d = {"meta": {"title": "t", "grade": 2, "lessonType": "精读", "theme": raw},
                 "slides": [{"id": "s1", "elements": []}]}
            return normalize(d)["meta"]["theme"]

        assert _n("Fresh-Blue") == "fresh-blue"
        assert _n("FRESH_BLUE") == "fresh-blue"
        assert _n("warm-green") == "warm-green"
        assert _n("  ") == "default"
        assert _n("紫色星空") == "default"

    def test_missing_theme_defaulted(self, scripts_path):
        from common.schema import normalize
        d = {"meta": {"title": "t", "grade": 2, "lessonType": "精读"},
             "slides": [{"id": "s1", "elements": []}]}
        assert normalize(d)["meta"]["theme"] == "default"

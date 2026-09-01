"""渲染层 v2 新版式测试（任务 #22：对标商业课件）。

覆盖：
1. mint-green 主题加载：配色断言 + TONE 跨主题一致 + numbered_header 布局键
2. 新版式三格式渲染：toc / challenge / scene-strip / background 全出血封面
   （pptx slide XML 结构标记 + html 新版式 class）
3. 编号章节头主题开关：mint 启用、default 不启用
4. default 老路径零回归：HEAD 版 jingyesi.json 用新代码渲染 = HEAD 代码渲染产物
   （zip 内部条目全等）

运行：
    PYTHONIOENCODING=utf-8 pytest tests/test_yuwen_render_v2.py -q
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
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
    with zipfile.ZipFile(fp) as z:
        import hashlib
        return {n: hashlib.sha256(z.read(n)).hexdigest()
                for n in sorted(z.namelist()) if not n.startswith("docProps/")}


def _slide_xmls(pptx: Path) -> list[str]:
    with zipfile.ZipFile(pptx) as z:
        names = sorted((n for n in z.namelist()
                        if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
                       key=lambda n: int(n[len("ppt/slides/slide"):-len(".xml")]))
        return [z.read(n).decode("utf-8") for n in names]


@pytest.fixture
def scripts_path():
    had = str(SCRIPTS) in sys.path
    if not had:
        sys.path.insert(0, str(SCRIPTS))
    yield
    if not had:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture
def restore_theme():
    yield
    from common import design_tokens as T
    T.set_theme("default")


@pytest.fixture(scope="module")
def v2_doc_file(tmp_path_factory):
    """含全部新版式要素的课程 JSON：toc / scene-strip / challenge / 全出血封面图。

    基于工作区 jingyesi.json（#23 已追加 toc/scene-strip/challenge 三页），
    补齐有效 src 让真实嵌图/全出血路径被触发。
    """
    tmp = tmp_path_factory.mktemp("v2doc")
    from PIL import Image
    img = tmp / "scene.png"
    Image.new("RGB", (1600, 900), (24, 94, 84)).save(img)
    strip = tmp / "fourgrid.png"
    Image.new("RGB", (1200, 800), (60, 120, 110)).save(strip)
    doc = json.loads(JINGYESI.read_text(encoding="utf-8"))
    for s in doc["slides"]:
        if s["kind"] == "cover":
            s["elements"].append(
                {"type": "image", "src": str(img), "background": True})
        elif s["kind"] == "toc":
            for el in s["elements"]:
                if el["type"] == "image":
                    el["src"] = str(img)
        elif s["kind"] == "scene-strip":
            for el in s["elements"]:
                if el["type"] == "scene-strip":
                    el["src"] = str(strip)
    jp = tmp / "v2.json"
    jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return jp


# ---------------------------------------------------------------- 1. mint 主题加载

class TestMintTheme:
    def test_mint_loads(self, scripts_path):
        from common.themes import load_theme
        th = load_theme("mint-green")
        assert th.name == "mint-green"
        for key in ("BG", "BG_CARD", "ACCENT", "ACCENT2", "ACCENT3",
                    "DIVIDER", "FOOT", "BORDER", "HL", "TONE", "HIGHLIGHTS"):
            assert key in th.pal, f"mint-green 缺 pal.{key}"
        assert th.pal["BG"] == "F3F4F6"          # 冷灰白底
        assert th.pal["ACCENT"] == "2DD4BF"      # 青绿主强调
        assert th.pal["ACCENT_DK"] == "14B8A6"
        assert th.pal["TITLE_TEXT"] == "1F2937"
        assert th.pal["DIVIDER"] == "E5E7EB"

    def test_mint_tone_cross_theme(self, scripts_path):
        """声调五色跨主题一致（schema 契约，test_yuwen_themes 同断言）。"""
        from common.themes import load_theme
        want = {1: "D9534F", 2: "E8A33C", 3: "5BA88A", 4: "5B8AB5", 0: "9AA0A6"}
        assert load_theme("mint-green").pal["TONE"] == want

    def test_mint_numbered_header_layout(self, scripts_path):
        """numbered_header 主题开关：mint=True，其余三主题=False。"""
        from common.themes import load_theme
        assert load_theme("mint-green").layout.get("numbered_header") is True
        for name in ("default", "fresh-blue", "warm-green"):
            assert load_theme(name).layout.get("numbered_header") is False

    def test_mint_layout_font_copy_default(self, scripts_path):
        """mint 的 font_scale/layout 数值 = default（任务约定抄值，仅开关键 True）。"""
        from common.themes import load_theme
        d, m = load_theme("default"), load_theme("mint-green")
        assert m.font_scale == d.font_scale
        assert m.layout == {**d.layout, "numbered_header": True}

    def test_proxy_switch_mint(self, scripts_path, restore_theme):
        from common import design_tokens as T
        T.set_theme("mint-green")
        assert T.PAL.ACCENT == "2DD4BF"
        assert T.L.numbered_header is True
        T.set_theme("default")
        assert T.L.get("numbered_header", False) is False

    def test_theme_enum_everywhere(self, scripts_path):
        """schema 枚举 / 词表 / 前端显示名全链路含 mint-green。"""
        from common.schema import LESSON_THEMES, normalize
        assert "mint-green" in LESSON_THEMES
        d = {"meta": {"title": "t", "grade": 2, "lessonType": "精读",
                      "theme": "Mint_Green"},
             "slides": [{"id": "s1", "elements": []}]}
        assert normalize(d)["meta"]["theme"] == "mint-green"
        sys.path.insert(0, str(_SRC))
        from devpilot.agenthub.yuwen.nodes._page import THEMES
        assert "mint-green" in THEMES
        from devpilot.agenthub.yuwen.nodes.confirm import _THEME_MAP
        # "青绿" 不被 warm-green 误捕：映射顺序即优先级
        pairs = dict((k, t) for kws, t in _THEME_MAP for k in kws)
        assert pairs["青绿"] == "mint-green"
        from devpilot.agenthub.yuwen import prompts
        for const in (prompts.SYSTEM_GEN_OUTLINE, prompts.META_CONTRACT,
                      prompts.SYSTEM_EDIT_OUTLINE):
            assert "mint-green" in const


# ---------------------------------------------------------------- 2. 新版式三格式渲染

class TestRenderV2:
    @pytest.mark.parametrize("theme", ["default", "mint-green"])
    def test_all_formats_render(self, tmp_path, v2_doc_file, theme):
        doc = json.loads(v2_doc_file.read_text(encoding="utf-8"))
        doc["meta"]["theme"] = theme
        jp = tmp_path / "doc.json"
        jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / theme
        r = _render_all(jp, out)
        assert r.returncode == 0, r.stderr
        assert list(out.glob("*.pptx")), "无 pptx"
        assert list(out.glob("*.html")), "无 html"
        assert list(out.glob("*.docx")), "无 docx"

    def test_pptx_structure_markers(self, tmp_path, v2_doc_file):
        """mint 主题 pptx 的 slide XML 含编号头/闯关卡/四格/目录/全出血遮罩标记。"""
        doc = json.loads(v2_doc_file.read_text(encoding="utf-8"))
        doc["meta"]["theme"] = "mint-green"
        jp = tmp_path / "doc.json"
        jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "pptx"
        assert _render_all(jp, out).returncode == 0
        xmls = _slide_xmls(next(out.glob("*.pptx")))
        allx = "\n".join(xmls)
        # 编号章节头：slide2（cover 后第一个内容页 s02）有独立 <a:t>01</a:t>
        # （toc 条目圆点也有 "01"，故断言精确到内容页所在 slide）
        assert "<a:t>01</a:t>" in xmls[1], "mint 编号章节头缺失"
        # challenge：徽章文本 + 正确项星标
        assert "第一关 · 填一填" in allx
        assert "★" in allx
        # scene-strip：caption 文字
        assert "床前明月光——诗人床边洒满月光" in allx
        # toc：目录大字
        assert "CONTENTS" in allx
        # 全出血封面：渐变遮罩 + 媒体图
        assert "gradFill" in xmls[0], "封面渐变遮罩缺失"
        assert "2DD4BF" in allx, "mint 主题强调色未生效"
        with zipfile.ZipFile(next(out.glob("*.pptx"))) as z:
            assert any(n.startswith("ppt/media/") for n in z.namelist())

    def test_default_no_numbered_header(self, tmp_path, v2_doc_file):
        """default 主题渲染同一文档：编号头不启用，但 toc/challenge/scene 照常。"""
        doc = json.loads(v2_doc_file.read_text(encoding="utf-8"))
        doc["meta"]["theme"] = "default"
        jp = tmp_path / "doc.json"
        jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "def"
        assert _render_all(jp, out).returncode == 0
        xmls = _slide_xmls(next(out.glob("*.pptx")))
        allx = "\n".join(xmls)
        # 编号头只看内容页（toc 页条目圆点合法地含 "01"）
        assert "<a:t>01</a:t>" not in xmls[1], "default 不应启用编号章节头"
        assert "第一关 · 填一填" in allx          # 新元素不分主题，全生效
        assert "CONTENTS" in allx

    def test_html_v2_classes(self, tmp_path, v2_doc_file):
        """HTML 新版式 class 齐全；编号头按主题开关。"""
        for theme, want_num in (("mint-green", True), ("default", False)):
            doc = json.loads(v2_doc_file.read_text(encoding="utf-8"))
            doc["meta"]["theme"] = theme
            jp = tmp_path / f"doc_{theme}.json"
            jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            out = tmp_path / theme
            assert _render_all(jp, out).returncode == 0
            raw = next(out.glob("*.html")).read_text(encoding="utf-8")
            assert 'class="elem-challenge"' in raw
            assert "ch-badge" in raw
            assert "ch-opt correct" in raw, "正确答案子卡高亮缺失"
            assert 'class="elem-scene"' in raw and "ss-caps" in raw
            assert "toc-wrap" in raw and "toc-figure" in raw
            assert "cover-bg" in raw, "全出血封面 div 缺失"
            body = raw.split("</style>", 1)[1]   # CSS 规则文本不算模板输出
            assert ('class="num-header"' in body) is want_num
            assert "--success: " in raw           # 主题语义色进 :root

    def test_background_placeholder_degrades(self, tmp_path, v2_doc_file):
        """background 图 src 缺失 → 封面退化为暖白底原版式，不崩。"""
        doc = json.loads(v2_doc_file.read_text(encoding="utf-8"))
        for s in doc["slides"]:
            if s["kind"] == "cover":
                s["elements"] = [e for e in s["elements"]
                                 if not e.get("background")]
        jp = tmp_path / "doc_nobg.json"
        jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "nobg"
        assert _render_all(jp, out).returncode == 0
        xmls = _slide_xmls(next(out.glob("*.pptx")))
        assert "gradFill" not in xmls[0], "无图时不应出现遮罩"


# ---------------------------------------------------------------- 3. default 老路径零回归

class TestLegacyPathZeroRegression:
    """工作区代码渲染 git HEAD 版老 jingyesi.json（无新元素）的 pptx，
    必须与 HEAD 代码渲染产物 zip 条目全等——证明新代码对老内容零侵入。

    区别于 test_yuwen_themes.TestDefaultRegression（对比 HEAD 代码 ×
    工作区新 JSON，#23 追加示例页后预期红）：这里锁的是渲染器本身。"""

    def test_legacy_json_pptx_identical(self, tmp_path):
        r = subprocess.run(["git", "archive", "HEAD", "src/devpilot/agenthub/yuwen"],
                           capture_output=True, cwd=_PROJECT_ROOT)
        assert r.returncode == 0
        head = tmp_path / "head"; head.mkdir()
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
            t.extractall(head)
        old_js = (head / "src/devpilot/agenthub/yuwen"
                  / "references/examples/jingyesi.json")
        out_new = tmp_path / "new"; out_old = tmp_path / "old"
        assert _render_all(old_js, out_new).returncode == 0
        rr = subprocess.run(
            [sys.executable,
             str(head / "src/devpilot/agenthub/yuwen/scripts/render_all.py"),
             str(old_js), "--out", str(out_old)],
            capture_output=True, text=True, encoding="utf-8")
        assert rr.returncode == 0, rr.stderr
        for f in out_new.glob("*.pptx"):
            of = out_old / f.name
            assert of.is_file()
            assert _zip_manifest(f) == _zip_manifest(of), \
                f"{f.name} 老内容渲染漂移（新代码侵入 default 老路径）"

    def test_scene_strip_without_src_placeholder(self, tmp_path):
        """scene-strip 无 src（生图前基线态）：pptx 占位面板 + html 占位 div，不崩。"""
        doc = {
            "meta": {"title": "占位测试", "grade": 2, "lessonType": "精读",
                     "theme": "default", "periods": 1},
            "slides": [
                {"id": "s1", "kind": "cover", "title": "占位", "period": 1,
                 "elements": []},
                {"id": "s2", "kind": "scene-strip", "title": "四格", "period": 1,
                 "elements": [{"type": "scene-strip", "scenes": [
                     {"caption": f"格{i}"} for i in range(1, 5)]}]},
            ],
        }
        jp = tmp_path / "ph.json"
        jp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "o"
        r = _render_all(jp, out)
        assert r.returncode == 0, r.stderr
        assert "待生图回填" in "\n".join(_slide_xmls(next(out.glob("*.pptx"))))
        raw = next(out.glob("*.html")).read_text(encoding="utf-8")
        assert "ss-ph" in raw and 'ss-img"><img' not in raw

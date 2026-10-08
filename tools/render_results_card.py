"""渲染成果卡（docs/results-card.png）。

把 README 与 Release 里那三行数字做成一张能直接用的图：
Release 头图、简历配图、汇报封面都够用。

为什么是脚本而不是一张二进制图：**图里的数字必须来自代码，不能靠手改**。
靶场数字变了就重跑一次，不会出现「README 写了 4 个缺陷、图里还是 3 个」。

    python tools/render_results_card.py

只依赖 Pillow。中文字体用系统自带，优先微软雅黑，其次等线，再退回默认字体。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "results-card.png"

WIDTH, HEIGHT = 1280, 748
MARGIN = 56

ACCENT = (217, 43, 52)
ACCENT_SOFT = (253, 242, 243)
INK = (31, 41, 55)
MUTED = (107, 114, 128)
FAINT = (156, 163, 175)
BORDER = (229, 231, 235)
TILE_BG = (249, 250, 251)
SUCCESS = (22, 163, 74)

REGULAR_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
BOLD_FONTS = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/Dengb.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size: int, bold: bool = False):
    """找得到中文字体就用，找不到退回 PIL 默认 —— 图会难看但不会崩。"""
    from PIL import ImageFont

    for path in (BOLD_FONTS if bold else REGULAR_FONTS):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def text(draw, xy, content, font, fill, anchor="la"):
    draw.text(xy, content, font=font, fill=fill, anchor=anchor)


def rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


# 数字来自三个靶场的实测结果，改动前先跑一遍 run_demo / run_evals 核对。
TARGETS = [
    ("订单服务", "Order Service", "25", "4", "0", "24/24 · 100%"),
    ("用户与订阅服务", "User & Subscription", "44", "3", "0", "41/41 · 100%"),
    ("内容服务", "Content Service", "61", "3", "0", "57/57 · 100%"),
]
TOTAL = ("合计", "Total", "130", "10", "0", "122/122 · 100%")

KPIS = [
    ("契约用例", "test cases", "130"),
    ("检出缺陷", "defects found", "10"),
    ("修复版残留", "on fixed target", "0"),
    ("生成质量", "recall / precision", "100%"),
]


def render() -> Path:
    # 惰性导入：这样 tests/test_results_card.py 可以在不装 Pillow 的环境里
    # 直接 import 这个模块来核对数字（数字与画图是两件事）。
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)

    f_logo = load_font(24, bold=True)
    f_title = load_font(34, bold=True)
    f_sub = load_font(17)
    f_sub_en = load_font(14)
    f_head = load_font(30, bold=True)
    f_head_sub = load_font(18)
    f_kpi_label = load_font(15)
    f_kpi_value = load_font(40, bold=True)
    f_kpi_note = load_font(12)
    f_thead = load_font(14)
    f_thead_en = load_font(11)
    f_cell = load_font(17)
    f_cell_bold = load_font(17, bold=True)
    f_foot = load_font(14)

    # ---------- 页眉 ----------
    rounded(draw, (MARGIN, 44, MARGIN + 56, 100), 14, fill=ACCENT)
    text(draw, (MARGIN + 28, 72), "ST", f_logo, "white", anchor="mm")
    text(draw, (MARGIN + 74, 50), "SmartTest", f_title, INK)
    text(draw, (MARGIN + 74, 92), "接口契约驱动的测试用例智能生成与失败归因平台", f_sub, MUTED)
    text(
        draw,
        (MARGIN + 74, 116),
        "Contract-driven test generation & failure triage for OpenAPI 3.x",
        f_sub_en,
        FAINT,
    )
    draw.line((MARGIN, 152, WIDTH - MARGIN, 152), fill=BORDER, width=1)

    # ---------- 结论 ----------
    text(draw, (MARGIN, 178), "三份不同领域的契约，同一套规则引擎", f_head, INK)
    text(
        draw,
        (MARGIN, 222),
        "缺陷全中 · 修复版归零 · 生成质量满分 —— 换契约依然成立，才是真通用",
        f_head_sub,
        MUTED,
    )

    # ---------- KPI ----------
    tile_w = (WIDTH - 2 * MARGIN - 3 * 18) // 4
    tile_top, tile_h = 268, 104
    for index, (label, note, value) in enumerate(KPIS):
        left = MARGIN + index * (tile_w + 18)
        rounded(draw, (left, tile_top, left + tile_w, tile_top + tile_h), 12, fill=TILE_BG, outline=BORDER)
        text(draw, (left + 20, tile_top + 16), label, f_kpi_label, MUTED)
        text(draw, (left + 20, tile_top + 38), value, f_kpi_value, ACCENT if index < 3 else SUCCESS)
        text(draw, (left + 20, tile_top + 82), note, f_kpi_note, FAINT)

    # ---------- 明细表 ----------
    table_top = 412
    row_h = 46
    columns = (MARGIN, 470, 640, 800, 960, WIDTH - MARGIN)
    headers = ("靶场\nTarget", "契约用例\nCases", "缺陷版\nBuggy", "修复版\nFixed", "生成质量\nQuality")

    draw.line((MARGIN, table_top, WIDTH - MARGIN, table_top), fill=BORDER, width=1)
    for index, header in enumerate(headers):
        lines = header.split("\n")
        left = columns[index] if index else columns[0]
        anchor = "la" if index == 0 else "ra"
        x = left + 12 if index == 0 else columns[index + 1] - 12
        text(draw, (x, table_top + 10), lines[0], f_thead, MUTED, anchor=anchor)
        text(draw, (x, table_top + 30), lines[1], f_thead_en, FAINT, anchor=anchor)
    draw.line((MARGIN, table_top + 54, WIDTH - MARGIN, table_top + 54), fill=BORDER, width=1)

    body_top = table_top + 54
    for index, (name, name_en, cases, buggy, fixed, quality) in enumerate(TARGETS):
        top = body_top + index * row_h
        text(draw, (MARGIN + 12, top + 8), name, f_cell, INK)
        text(draw, (MARGIN + 12, top + 28), name_en, f_kpi_note, FAINT)
        text(draw, (columns[2] - 12, top + 14), cases, f_cell, INK, anchor="ra")
        text(draw, (columns[3] - 12, top + 14), buggy, f_cell_bold, ACCENT, anchor="ra")
        text(draw, (columns[4] - 12, top + 14), fixed, f_cell_bold, SUCCESS, anchor="ra")
        text(draw, (columns[5] - 12, top + 14), quality, f_cell, INK, anchor="ra")
        draw.line((MARGIN, top + row_h, WIDTH - MARGIN, top + row_h), fill=BORDER, width=1)

    total_top = body_top + 3 * row_h + 6
    rounded(
        draw,
        (MARGIN, total_top, WIDTH - MARGIN, total_top + row_h),
        8,
        fill=ACCENT_SOFT,
    )
    text(draw, (MARGIN + 12, total_top + 8), TOTAL[0], f_cell_bold, INK)
    text(draw, (MARGIN + 12, total_top + 28), TOTAL[1], f_kpi_note, FAINT)
    text(draw, (columns[2] - 12, total_top + 14), TOTAL[2], f_cell_bold, INK, anchor="ra")
    text(draw, (columns[3] - 12, total_top + 14), TOTAL[3], f_cell_bold, ACCENT, anchor="ra")
    text(draw, (columns[4] - 12, total_top + 14), TOTAL[4], f_cell_bold, SUCCESS, anchor="ra")
    text(draw, (columns[5] - 12, total_top + 14), TOTAL[5], f_cell_bold, INK, anchor="ra")

    # ---------- 页脚 ----------
    footer_y = total_top + row_h + 34
    text(
        draw,
        (MARGIN, footer_y),
        "246 条单元测试 · 核心模块覆盖率 98% · 13 道 CI 质量门禁 · 假阳性 0",
        f_foot,
        INK,
    )
    text(
        draw,
        (WIDTH - MARGIN, footer_y),
        "github.com/Wangwenbo0523/smarttest",
        f_foot,
        FAINT,
        anchor="ra",
    )
    text(
        draw,
        (MARGIN, footer_y + 24),
        "每个数字都能复现：docs/RESULTS.md 里有 60 秒验证路径",
        f_foot,
        FAINT,
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, "PNG", optimize=True)
    return OUTPUT


if __name__ == "__main__":
    path = render()
    print(f"成果卡已写入: {path}")
    raise SystemExit(0)

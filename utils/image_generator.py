# utils/image_generator.py
"""修仙 · 道友档案 图片卡片生成器（完全自包含版）。

历史实现依赖 AstrBot 数据目录下 ``data/xiuxian/`` 里的
``info_img/back.png`` ``line2~4.png`` ``font/font.ttf``，而 README 指向的
「卡图资源包」下载地址已无任何资源，导致图片卡片要么被禁用、要么只能
画出灰底 + 豆腐块文字。

本实现改为：
  * 纯 Pillow 绘制整套国风卡片（渐变、云纹、太极印、回纹边框、进度条），
    不再读取任何外部图片素材；
  * 内置一份精简中文字体 ``assets/fonts/XiuXianSans.otf``（GB2312 常用字
    + 本插件全部专有名词），并自动探测系统中文字体作为备选；
  * 任何环节不可用时返回 None，由调用方自动回退到文本面板，绝不输出乱码图。
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:  # pragma: no cover - 缺少 Pillow 时优雅降级
    HAS_PIL = False

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 便于脱离框架单测
    import logging
    logger = logging.getLogger("xiuxian_image")

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    get_astrbot_data_path = None


# --------------------------------------------------------------------------
# 资源与配色
# --------------------------------------------------------------------------
BUNDLED_FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "XiuXianSans.otf"

# 系统字体候选（内置字体缺失时的兜底，覆盖 Linux / Windows / macOS）
SYSTEM_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]

# 画布
W, H = 1080, 1520

# 配色（深墨蓝绿 + 鎏金）
C_BG_TOP = (9, 19, 27)
C_BG_BOTTOM = (15, 37, 41)
C_GOLD = (228, 194, 116)
C_GOLD_SOFT = (158, 130, 74)
C_GOLD_DARK = (96, 78, 44)
C_CREAM = (242, 237, 220)
C_MUTED = (148, 168, 174)
C_JADE = (120, 208, 190)
C_RED = (214, 102, 94)
C_PANEL = (18, 37, 45)
C_PANEL_EDGE = (62, 96, 100)
C_DIVIDER = (52, 78, 84)


def _resolve_font_path() -> Optional[Path]:
    """按优先级解析可用的中文字体：内置 > 用户自定义 > 系统字体。"""
    if BUNDLED_FONT.exists():
        return BUNDLED_FONT
    # 兼容旧说明：用户若自行放置了 data/xiuxian/font/font.ttf 也可用
    if get_astrbot_data_path is not None:
        try:
            legacy = Path(get_astrbot_data_path()) / "xiuxian" / "font" / "font.ttf"
            if legacy.exists():
                return legacy
        except Exception:
            pass
    for p in SYSTEM_FONT_CANDIDATES:
        if Path(p).exists():
            return Path(p)
    return None


class ImageGenerator:
    """生成「道友档案」图片卡片。"""

    def __init__(self):
        self.has_pil = HAS_PIL
        self._font_path = _resolve_font_path()
        self._font_cache: Dict[int, "ImageFont.FreeTypeFont"] = {}
        if not self.has_pil:
            logger.warning("【修仙插件】未检测到 Pillow，图片卡片将回退为文本。请安装 pip install Pillow")
        elif self._font_path is None:
            logger.warning("【修仙插件】未找到可用的中文字体，图片卡片将回退为文本。")

    # ---------------------------------------------------------------- 基础
    def is_available(self) -> bool:
        """是否具备生成图片卡片的完整条件。"""
        return self.has_pil and self._font_path is not None

    def font(self, size: int):
        """按字号缓存字体对象。"""
        if size not in self._font_cache:
            self._font_cache[size] = ImageFont.truetype(str(self._font_path), size)
        return self._font_cache[size]

    async def generate_user_info_card(self, user_id: str, detail_map: Dict) -> Optional[BytesIO]:
        """异步生成卡片，返回 PNG 字节流；失败返回 None（调用方回退文本）。"""
        if not self.is_available():
            return None
        try:
            return await asyncio.to_thread(self._draw_sync, str(user_id), detail_map or {})
        except Exception as e:  # pragma: no cover
            logger.error(f"【修仙插件】生成图片卡片失败，回退文本：{e}")
            return None

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _text(draw: "ImageDraw.ImageDraw", xy, text, font, fill,
              anchor: str = "la", stroke: int = 0):
        """安全绘制文本（stroke 用于模拟加粗）。"""
        try:
            draw.text(xy, str(text), font=font, fill=fill, anchor=anchor,
                      stroke_width=stroke, stroke_fill=fill)
        except Exception:
            try:
                draw.text(xy, str(text), font=font, fill=fill)
            except Exception:
                pass

    def _fit(self, draw, text: str, font, max_w: int) -> str:
        """按最大宽度截断文本，尾部加省略号。"""
        s = str(text)
        if draw.textlength(s, font=font) <= max_w:
            return s
        while s and draw.textlength(s + "…", font=font) > max_w:
            s = s[:-1]
        return (s + "…") if s else "…"

    def _panel(self, img, box, radius=18, fill=C_PANEL, edge=C_PANEL_EDGE, width=2):
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, radius=radius, fill=fill, outline=edge, width=width)
        # 内侧细金线
        x0, y0, x1, y1 = box
        d.rounded_rectangle((x0 + 6, y0 + 6, x1 - 6, y1 - 6), radius=max(1, radius - 6),
                            outline=(*C_GOLD_DARK, 90) if img.mode == "RGBA" else C_GOLD_DARK,
                            width=1)

    def _background(self, height: int = H) -> "Image.Image":
        """垂直渐变 + 细微星尘 + 双层边框 + 四角回纹。

        ``height`` 可自定义：背包卡片物品数量不固定，需要按内容动态增高画布。
        """
        img = Image.new("RGBA", (W, height), C_BG_TOP + (255,))
        d = ImageDraw.Draw(img)
        for y in range(height):
            t = y / (height - 1)
            # 中段略微提亮，营造「灵气上浮」的层次
            glow = 12 * math.sin(math.pi * t)
            c = (
                int(C_BG_TOP[0] + (C_BG_BOTTOM[0] - C_BG_TOP[0]) * t + glow * 0.35),
                int(C_BG_TOP[1] + (C_BG_BOTTOM[1] - C_BG_TOP[1]) * t + glow * 0.7),
                int(C_BG_TOP[2] + (C_BG_BOTTOM[2] - C_BG_TOP[2]) * t + glow),
                255,
            )
            d.line([(0, y), (W, y)], fill=c)

        # 顶部柔光晕
        glow_layer = Image.new("RGBA", (W, 420), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        for r in range(260, 0, -6):
            alpha = int(9 * (1 - r / 260))
            gd.ellipse((W // 2 - r, 60 - r // 2, W // 2 + r, 60 + r // 2),
                       fill=(*C_GOLD, alpha))
        img.alpha_composite(glow_layer, (0, 0))

        # 星尘（固定种子，保证同一张卡片稳定）
        rnd = random.Random(20261112)
        for _ in range(90):
            x, y = rnd.randint(40, W - 40), rnd.randint(40, max(41, height - 40))
            r = rnd.choice([1, 1, 1, 2])
            a = rnd.randint(25, 90)
            d.ellipse((x - r, y - r, x + r, y + r), fill=(*C_GOLD, a))

        # 外框：双层金线
        d.rectangle((22, 22, W - 23, height - 23), outline=C_GOLD_SOFT, width=2)
        d.rectangle((32, 32, W - 33, height - 33), outline=(*C_GOLD_DARK, 160), width=1)

        # 四角回纹
        self._corner(d, 32, 32, 1, 1)
        self._corner(d, W - 32, 32, -1, 1)
        self._corner(d, 32, height - 32, 1, -1)
        self._corner(d, W - 32, height - 32, -1, -1)
        return img

    @staticmethod
    def _corner(d, x, y, sx, sy):
        """角落 L 型回纹装饰。"""
        L, gap = 54, 9
        for k, col in ((0, C_GOLD), (1, C_GOLD_DARK)):
            off = k * gap
            d.line([(x + sx * (L - off), y + sy * off),
                    (x + sx * off, y + sy * off),
                    (x + sx * off, y + sy * (L - off))], fill=col, width=2)
        d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=C_GOLD)

    def _taiji(self, img, cx, cy, r):
        """简化太极印记（纯几何绘制，不依赖特殊符号字体）。"""
        d = ImageDraw.Draw(img)
        d.ellipse((cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6),
                  outline=C_GOLD_SOFT, width=2)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*C_PANEL, 255),
                  outline=C_GOLD, width=2)
        # 阴阳两半
        d.pieslice((cx - r + 4, cy - r + 4, cx + r - 4, cy + r - 4),
                   90, 270, fill=C_GOLD)
        d.pieslice((cx - r + 4, cy - r + 4, cx + r - 4, cy + r - 4),
                   270, 90, fill=(*C_PANEL, 255))
        rr = (r - 4) / 2
        d.ellipse((cx - rr, cy - r + 4, cx + rr, cy + r - 4 - 2 * rr + (r - 4)),
                  fill=C_GOLD)
        # 上面这段容易算错，改用标准画法
        # 覆盖重画：上半小圆（金底黑点）、下半小圆（黑底金点）
        r2 = (r - 4) / 2
        d.ellipse((cx - r2, cy - (r - 4), cx + r2, cy), fill=C_GOLD)
        d.ellipse((cx - r2, cy, cx + r2, cy + (r - 4)), fill=(*C_PANEL, 255))
        d.ellipse((cx - r2 / 2.6, cy - (r - 4) / 2 - r2 / 2.6,
                   cx + r2 / 2.6, cy - (r - 4) / 2 + r2 / 2.6), fill=(*C_PANEL, 255))
        d.ellipse((cx - r2 / 2.6, cy + (r - 4) / 2 - r2 / 2.6,
                   cx + r2 / 2.6, cy + (r - 4) / 2 + r2 / 2.6), fill=C_GOLD)

    def _section(self, img, box, title: str, subtitle: str = ""):
        """绘制一个带标题的分区面板，返回内容区起始 y。"""
        x0, y0, x1, y1 = box
        self._panel(img, box)
        d = ImageDraw.Draw(img)
        f_t = self.font(30)
        # 标题左侧菱形装饰
        cx, cy = x0 + 40, y0 + 40
        d.polygon([(cx, cy - 9), (cx + 9, cy), (cx, cy + 9), (cx - 9, cy)], fill=C_GOLD)
        self._text(d, (x0 + 62, y0 + 40), title, f_t, C_GOLD, anchor="lm", stroke=1)
        if subtitle:
            self._text(d, (x1 - 30, y0 + 40), subtitle, self.font(19), C_MUTED, anchor="rm")
        d.line([(x0 + 30, y0 + 70), (x1 - 30, y0 + 70)], fill=C_DIVIDER, width=1)
        return y0 + 86

    def _kv_grid(self, img, x0, y0, width, pairs: Sequence[Tuple[str, str]],
                 cols: int = 2, row_h: int = 52, value_colors=None):
        """两列键值网格：整行分隔线 + 列分隔线，避免相邻列文字粘连。"""
        d = ImageDraw.Draw(img)
        f_k, f_v = self.font(23), self.font(25)
        col_w = width // cols
        rows = math.ceil(len(pairs) / cols)
        # 行分隔线（横跨整行）
        for r in range(1, rows):
            yy = y0 + r * row_h - 6
            d.line([(x0 + 10, yy), (x0 + width - 10, yy)],
                   fill=(*C_DIVIDER, 130), width=1)
        # 列分隔线（竖线，拉开左右两列的视觉距离）
        for c in range(1, cols):
            xx = x0 + c * col_w
            d.line([(xx, y0 + 4), (xx, y0 + rows * row_h - 16)],
                   fill=(*C_DIVIDER, 110), width=1)
        for i, (k, v) in enumerate(pairs):
            col, row = i % cols, i // cols
            x = x0 + col * col_w
            y = y0 + row * row_h
            self._text(d, (x + 20, y + row_h // 2), k, f_k, C_MUTED, anchor="lm")
            vc = (value_colors or {}).get(k, C_CREAM)
            val = self._fit(d, v, f_v, col_w - 170)
            self._text(d, (x + col_w - 24, y + row_h // 2), val, f_v, vc, anchor="rm")

    def _stat_box(self, img, x, y, w, h, label, value, color=C_CREAM):
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((x, y, x + w, y + h), radius=12,
                            fill=(13, 30, 36, 235), outline=C_PANEL_EDGE, width=1)
        self._text(d, (x + w // 2, y + 26), label, self.font(21), C_MUTED, anchor="ma")
        val = self._fit(d, value, self.font(36), w - 24)
        self._text(d, (x + w // 2, y + 62), val, self.font(36), color, anchor="ma", stroke=1)

    # ---------------------------------------------------------------- 主绘制
    def _draw_sync(self, user_id: str, m: Dict) -> BytesIO:
        img = self._background()
        d = ImageDraw.Draw(img)

        def g(key, default=""):
            v = m.get(key, default)
            return default if v is None else v

        # ===== 顶部标题 =====
        self._taiji(img, 132, 132, 46)
        self._text(d, (216, 100), "修 仙 录", self.font(62), C_GOLD, anchor="lm", stroke=2)
        self._text(d, (218, 158), "道友档案  ·  CULTIVATOR PROFILE",
                   self.font(21), C_GOLD_SOFT, anchor="lm")
        d.line([(48, 196), (W - 48, 196)], fill=C_GOLD_DARK, width=1)

        # ===== 身份区 =====
        id_box = (48, 214, W - 48, 402)
        self._panel(img, id_box)
        # 左侧境界徽章
        cx, cy = 148, 308
        d.ellipse((cx - 72, cy - 72, cx + 72, cy + 72),
                  fill=(13, 31, 38, 255), outline=C_GOLD, width=2)
        d.ellipse((cx - 80, cy - 80, cx + 80, cy + 80), outline=C_GOLD_DARK, width=1)
        dao = str(g("道号", "无名道友"))
        badge = dao[:1] if dao else "道"
        self._text(d, (cx, cy), badge, self.font(62), C_GOLD, anchor="mm", stroke=1)
        # 右侧信息
        self._text(d, (246, 262), self._fit(d, dao, self.font(44), 700),
                   self.font(44), C_CREAM, anchor="lm", stroke=1)
        realm = str(g("境界", "凡人"))
        self._text(d, (246, 320), self._fit(d, realm, self.font(30), 700),
                   self.font(30), C_JADE, anchor="lm")
        # 标签胶囊
        tags = [("修炼", str(g("修炼方式", "灵修"))), ("灵根", str(g("灵根", "未知")))]
        tx = 246
        for label, val in tags:
            t = f"{label} · {val}"
            f = self.font(21)
            tw = d.textlength(t, font=f) + 34
            d.rounded_rectangle((tx, 350, tx + tw, 386), radius=17,
                                fill=(26, 52, 58, 255), outline=C_GOLD_DARK, width=1)
            self._text(d, (tx + tw / 2, 368), t, f, C_GOLD, anchor="mm")
            tx += tw + 12
        # 右上角 ID
        self._text(d, (W - 76, 240), f"ID {self._fit(d, user_id, self.font(20), 240)}",
                   self.font(20), C_MUTED, anchor="rm")

        # ===== 修为进度 =====
        px0, px1, py = 48, W - 48, 424
        self._panel(img, (px0, py, px1, py + 112), radius=16)
        self._text(d, (px0 + 28, py + 32), "修 为", self.font(25), C_GOLD, anchor="lm")
        cur = g("修为当前", 0)
        need = g("修为所需", 0)
        try:
            cur_n, need_n = float(cur), float(need)
            ratio = max(0.0, min(1.0, cur_n / need_n)) if need_n > 0 else 0.0
        except (TypeError, ValueError):
            cur_n, need_n, ratio = 0.0, 0.0, 0.0
        exp_text = str(g("修为", f"{int(cur_n):,}/{int(need_n):,}"))
        self._text(d, (px1 - 28, py + 32), f"{exp_text}   ({ratio * 100:.1f}%)",
                   self.font(23), C_CREAM, anchor="rm")
        # 进度条
        bx0, bx1, by = px0 + 30, px1 - 30, py + 68
        bh = 22
        d.rounded_rectangle((bx0, by, bx1, by + bh), radius=bh // 2,
                            fill=(10, 24, 30, 255), outline=C_PANEL_EDGE, width=1)
        fill_w = int((bx1 - bx0) * ratio)
        if fill_w > 4:
            # 金色渐变填充
            for i in range(fill_w):
                t = i / max(1, (bx1 - bx0))
                col = (int(150 + 78 * t), int(120 + 74 * t), int(60 + 56 * t), 255)
                d.line([(bx0 + i, by + 2), (bx0 + i, by + bh - 2)], fill=col)
        d.rounded_rectangle((bx0, by, bx1, by + bh), radius=bh // 2,
                            outline=C_GOLD_SOFT, width=1)

        # ===== 核心数值四宫格 =====
        sy, sh, gap = 554, 108, 14
        sw = (W - 96 - gap * 3) // 4
        stats = [
            ("灵 石", str(g("灵石", 0)), C_GOLD),
            ("战 力", str(g("战力", 0)), C_RED),
            ("寿 元", str(g("寿命", "-")), C_JADE),
            ("突破加成", str(g("突破加成", g("突破状态", "0%"))), C_CREAM),
        ]
        for i, (label, value, col) in enumerate(stats):
            self._stat_box(img, 48 + i * (sw + gap), sy, sw, sh, label, value, col)

        # ===== 修炼属性 =====
        ab = (48, 686, W - 48, 1004)
        ay = self._section(img, ab, "修炼属性", "ATTRIBUTES")
        qi_key = "气血" if str(g("修炼方式", "")) == "体修" else "灵气"
        qi_max_key = "气血上限" if qi_key == "气血" else "灵气上限"
        pairs = [
            ("当前状态", str(g("状态", "空闲"))),
            (qi_key, str(g(qi_key, "-"))),
            ("法 伤", str(g("法伤", 0))),
            ("物 伤", str(g("物伤", 0))),
            ("法 防", str(g("法防", 0))),
            ("物 防", str(g("物防", 0))),
            ("精神力", str(g("精神力", 0))),
            ("攻击力", str(g("攻击力", str(g("物伤", 0))))),
        ]
        self._kv_grid(img, ab[0] + 22, ay, ab[2] - ab[0] - 44, pairs,
                      cols=2, row_h=52,
                      value_colors={"当前状态": C_JADE, "法 伤": C_GOLD, "物 伤": C_GOLD})

        # ===== 装备 + 宗门 =====
        # 高度必须容纳 3 行 × 52px + 标题 86px + 底部留白，否则会被下方贷款框裁切
        eb = (48, 1020, W - 48, 1286)
        ey = self._section(img, eb, "装备 · 宗门", "EQUIP & SECT")
        eq_pairs = [
            ("主修功法", str(g("主修功法", "无"))),
            ("法 器", str(g("法器", "无"))),
            ("防 具", str(g("防具", "无"))),
            ("所在宗门", str(g("所在宗门", "无宗门"))),
            ("宗门职位", str(g("宗门职位", "散修"))),
            ("突破状态", str(g("突破加成", "0%"))),
        ]
        self._kv_grid(img, eb[0] + 22, ey, eb[2] - eb[0] - 44, eq_pairs,
                      cols=2, row_h=52,
                      value_colors={"所在宗门": C_JADE, "主修功法": C_GOLD})

        # ===== 贷款提示（可选） =====
        loan = str(g("贷款信息", "")).strip()
        if loan:
            lb = (48, 1298, W - 48, 1364)
            d.rounded_rectangle(lb, radius=12, fill=(58, 26, 26, 235),
                                outline=C_RED, width=1)
            # 纯几何三角警示标，避免特殊符号在精简字体里变成豆腐块
            tx, ty = lb[0] + 34, lb[1] + 33
            d.polygon([(tx, ty - 13), (tx + 13, ty + 11), (tx - 13, ty + 11)],
                      outline=C_RED, width=2)
            d.line([(tx, ty - 4), (tx, ty + 3)], fill=C_RED, width=2)
            d.ellipse((tx - 2, ty + 6, tx + 2, ty + 9), fill=C_RED)
            self._text(d, (lb[0] + 62, ty),
                       self._fit(d, loan, self.font(23), W - 220),
                       self.font(23), C_RED, anchor="lm")

        # ===== 页脚 =====
        foot_base = 1390 if loan else 1312
        d.line([(48, foot_base), (W - 48, foot_base)], fill=C_GOLD_DARK, width=1)
        self._text(d, (W // 2, foot_base + 30), "大道三千 · 各证其一",
                   self.font(24), C_GOLD_SOFT, anchor="ma")
        ts = time.strftime("%Y-%m-%d %H:%M")
        self._text(d, (W // 2, foot_base + 74), f"档案生成于 {ts}",
                   self.font(18), C_MUTED, anchor="ma")

        out = BytesIO()
        img.convert("RGB").save(out, format="PNG", optimize=True)
        out.seek(0)
        return out

    # ---------------------------------------------------------------- 背包卡片
    async def generate_inventory_card(self, user_id: str, payload: Dict) -> Optional[BytesIO]:
        """异步生成「乾坤囊 · 背包」卡片，返回 PNG 字节流；失败返回 None。

        payload 结构::

            {
              "dao_hao": "某某",
              "ring_name": "基础储物戒",
              "ring_used": 3, "ring_capacity": 20,
              "total_kinds": 8, "total_count": 26,
              "groups": [
                {"title": "丹药背包", "subtitle": "PILLS · 3 种", "color": "jade",
                 "items": [{"name": "炼气丹", "tag": "凡品", "count": 5}, ...]},
                ...
              ]
            }
        """
        if not self.is_available():
            return None
        try:
            return await asyncio.to_thread(self._draw_inventory_sync, str(user_id), payload or {})
        except Exception as e:  # pragma: no cover
            logger.error(f"【修仙插件】生成背包卡片失败：{e}")
            return None

    def _inventory_layout(self, m: Dict):
        """按分组与物品条数计算画布高度和每个分组的纵向区间。"""
        groups = m.get("groups") or []
        cols = 2
        row_h = 50
        y = 440  # 顶部标题 + 身份行 + 统计条 + 储物戒名占用的高度
        boxes = []
        for gp in groups:
            items = gp.get("items") or []
            rows = max(1, math.ceil(len(items) / cols))
            h = 86 + rows * row_h + 22
            boxes.append((y, y + h, gp, rows))
            y += h + 18
        return max(900, y + 130), boxes, cols, row_h

    def _draw_inventory_sync(self, user_id: str, m: Dict) -> BytesIO:
        height, boxes, cols, row_h = self._inventory_layout(m)
        img = self._background(height)
        d = ImageDraw.Draw(img)

        # ===== 顶部标题 =====
        self._taiji(img, 132, 132, 46)
        self._text(d, (216, 100), "乾 坤 囊", self.font(62), C_GOLD, anchor="lm", stroke=2)
        self._text(d, (218, 158), "道友背包  ·  INVENTORY",
                   self.font(21), C_GOLD_SOFT, anchor="lm")
        d.line([(48, 196), (W - 48, 196)], fill=C_GOLD_DARK, width=1)

        dao = str(m.get("dao_hao") or "无名道友")
        self._text(d, (60, 232), self._fit(d, dao, self.font(30), 560),
                   self.font(30), C_CREAM, anchor="lm")
        self._text(d, (W - 60, 232), f"ID {self._fit(d, user_id, self.font(20), 240)}",
                   self.font(20), C_MUTED, anchor="rm")

        # ===== 统计条 =====
        sb = (48, 264, W - 48, 372)
        self._panel(img, sb, radius=16)
        stats = [
            ("物品种类", str(m.get("total_kinds", 0)), C_GOLD),
            ("物品总数", str(m.get("total_count", 0)), C_GOLD),
            ("储物戒格数", f"{m.get('ring_used', 0)}/{m.get('ring_capacity', 0)}", C_JADE),
        ]
        sw = (sb[2] - sb[0] - 40) // 3
        for i, (label, val, col) in enumerate(stats):
            cx = sb[0] + 20 + i * sw + sw // 2
            self._text(d, (cx, sb[1] + 30), label, self.font(21), C_MUTED, anchor="ma")
            self._text(d, (cx, sb[1] + 68), self._fit(d, val, self.font(34), sw - 20),
                       self.font(34), col, anchor="ma", stroke=1)
        self._text(d, (W // 2, 406),
                   f"当前储物戒：{self._fit(d, str(m.get('ring_name') or '基础储物戒'), self.font(21), 700)}",
                   self.font(21), C_GOLD_SOFT, anchor="ma")

        # ===== 分组清单 =====
        palette = {
            "jade": C_JADE, "gold": C_GOLD, "red": C_RED,
            "cream": C_CREAM, "muted": C_MUTED,
        }
        for y0, y1, gp, rows in boxes:
            self._panel(img, (48, y0, W - 48, y1))
            d = ImageDraw.Draw(img)
            accent = palette.get(str(gp.get("color") or "gold"), C_GOLD)
            cx, cy = 88, y0 + 40
            d.polygon([(cx, cy - 9), (cx + 9, cy), (cx, cy + 9), (cx - 9, cy)], fill=accent)
            self._text(d, (110, y0 + 40), str(gp.get("title") or "物品"),
                       self.font(30), accent, anchor="lm", stroke=1)
            self._text(d, (W - 78, y0 + 40), str(gp.get("subtitle") or ""),
                       self.font(19), C_MUTED, anchor="rm")
            d.line([(78, y0 + 70), (W - 78, y0 + 70)], fill=C_DIVIDER, width=1)

            items = gp.get("items") or []
            content_y = y0 + 86
            col_w = (W - 96 - 44) // cols
            if not items:
                self._text(d, (W // 2, content_y + 18), "（空）",
                           self.font(23), C_MUTED, anchor="ma")
            for idx, it in enumerate(items):
                col, row = idx % cols, idx // cols
                x = 48 + 22 + col * col_w
                yy = content_y + row * row_h
                name = str(it.get("name") or "")
                tag = str(it.get("tag") or "").strip()
                label = f"{name} · {tag}" if tag else name
                f_name = self.font(24)
                self._text(d, (x + 8, yy + row_h // 2),
                           self._fit(d, label, f_name, col_w - 120),
                           f_name, C_CREAM, anchor="lm")
                self._text(d, (x + col_w - 12, yy + row_h // 2),
                           f"×{it.get('count', 1)}", self.font(23), accent, anchor="rm")
                d.line([(x + 4, yy + row_h - 6), (x + col_w - 16, yy + row_h - 6)],
                       fill=(*C_DIVIDER, 90), width=1)
            for c in range(1, cols):
                xx = 48 + 22 + c * col_w
                d.line([(xx, content_y + 2), (xx, content_y + rows * row_h - 12)],
                       fill=(*C_DIVIDER, 80), width=1)

        # ===== 页脚 =====
        foot = height - 130
        d.line([(48, foot), (W - 48, foot)], fill=C_GOLD_DARK, width=1)
        self._text(d, (W // 2, foot + 30), "囊括天地 · 万物皆备于我",
                   self.font(24), C_GOLD_SOFT, anchor="ma")
        self._text(d, (W // 2, foot + 74),
                   f"背包生成于 {time.strftime('%Y-%m-%d %H:%M')}",
                   self.font(18), C_MUTED, anchor="ma")

        out = BytesIO()
        img.convert("RGB").save(out, format="PNG", optimize=True)
        out.seek(0)
        return out


_SINGLETON: Optional[ImageGenerator] = None


def get_image_generator() -> ImageGenerator:
    """全局复用一个生成器（避免每张卡片重复探测字体）。"""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = ImageGenerator()
    return _SINGLETON


# 兼容旧调用名
__all__ = ["ImageGenerator", "get_image_generator"]

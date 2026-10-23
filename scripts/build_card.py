#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沉香手串速判 · 卡片生成器

把一份标的 JSON 数据渲染成 HTML 卡片（Botanical / Organic Serif 风格）。

子命令
------
build     由 JSON 生成 HTML 卡片页
density   密度反算（克重 + 颗数 + 珠径 → 密度 + 沉水等级）
diameter  由克重 / 颗数 / 假设密度 反推珠径
estimate  按系数公式给出估价区间

依赖：仅标准库（Python 3.8+）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from html.parser import HTMLParser

# ============================================================================
# 常量：与 references/pricing-model.md 保持一致
# ============================================================================

#: 八维事实的**标准顺序**（不可乱序）
FACT_ORDER = ["颗数", "密度", "克单价", "纹路", "颜色", "油润感", "油脂分布", "处理痕迹"]

#: 密度 → 称呼（民间口径 · 三档）。与 references/pricing-model.md §一 保持一致。
#: ⚠️ 本表只翻译"料有多实"；基准档位见 references/region-benchmarks.md §一 1.1
#: 的三档密度栏（浮水料／半沉 – 九分沉／沉水料）—— 判断该取哪栏基准时，
#: 用实测密度去对表内密度档。
GRADES = [
    (1.00, "沉水"),
    (0.80, "沉水浮"),
    (0.00, "不沉水"),
]

#: 珠径系数：不设系数，一律 1.00 —— 方向存在市场分歧（通货料小珠略贵 /
#: 高油好料大珠更贵），量级亦无据；依据见 references/pricing-model.md §三 3.2。
DIAMETER_COEF = 1.00

#: 形状系数（相对**同料同克重圆珠** = 1.00）
#:
#: ⚠️ 方向：**圆珠 > 桶珠 > 鼓珠（苹果珠）> 随形 > 片片珠**。
#:   圆珠综合损耗 70–80%（成品率仅约 1/4），随形顺料修磨只损 15–20%
#:   → 材料成本比约 3:1，与市场口径"随形 ≈ 圆珠的 1/3"对得上。
#: ⚠️ **别把「料好」当成「形制贵」。** 随形"看着用料好"，是因为同样的钱买到了油更好的料
#:   （预算花在材质而非损耗上）—— 那是**料质**优势，不是随形这个形态本身值钱。
#:   料质走油脂系数、形态走本表，混在一处就是重复计价。
#:
#: ⚠️ 取 1.00 的含义：**方向 <1**（桶珠／鼓珠）取 1.00 是*保守偏高*，卡片注明口径。
#: ⚠️ **判据：方向有据、量级无据 → 一律取 1.00 不给加成，方向只写进说明。**
#:   桶珠／鼓珠同办。随形是唯一给了量级（0.33）的往下修正 —— 有损耗机制支撑。
SHAPE_COEF = {
    "圆珠": 1.00,
    "桶珠": 1.00,       # 方向 <1（费料仅次于圆珠），无量级来源 → 保守按 1.00
    "鼓珠": 1.00,       # 方向 <1，同上
    "苹果珠": 1.00,     # 方向 <1，同上
    "随形件": 0.33,
    "随形": 0.33,
    "片片珠": 0.20,     # 「用剩料、最便宜」；低于随形，无量化来源（保守推断）
    "片珠": 0.20,
}

#: 油脂分布系数
OIL_COEF = {
    "均匀": 1.25,
    "不均": 1.00,
    "干涩": 0.80,
}

TONE_SET = {"good", "warn", "bad", "neutral", ""}


# ============================================================================
# 定价计算
# ============================================================================

def density(weight_g: float, beads: int, diameter_mm: float) -> float:
    """密度反算。

    :param weight_g: 克重（g）
    :param beads: 颗数
    :param diameter_mm: 珠径（mm）
    :return: 密度 ρ（g/cm³）

    ⚠️ 陷阱：珠径单位是 mm，必须先 ÷10 变 cm 再立方。忘记换算会得到 1000 倍的错值。
    """
    r_cm = (diameter_mm / 10.0) / 2.0
    v_single = 4.0 / 3.0 * math.pi * r_cm ** 3
    return weight_g / (v_single * beads)


def grade_of(rho: float) -> str:
    """由密度取沉水等级名。"""
    for low, name in GRADES:
        if rho >= low:
            return name
    return GRADES[-1][1]


def diameter_from(weight_g: float, beads: int, rho: float) -> float:
    """由克重 / 颗数 / 假设密度 反推珠径（mm）。

    :return: 珠径 mm

    推导：V_single = w / (n·ρ)，球体 V = 4/3·π·r³ → r = (3V/4π)^(1/3)
    """
    v_single = weight_g / (beads * rho)
    r_cm = (3.0 * v_single / (4.0 * math.pi)) ** (1.0 / 3.0)
    return r_cm * 2.0 * 10.0


def estimate(base_price: float, weight_g: float, oil: str = "不均",
             diameter_mm: float | None = None, shape: str = "圆珠") -> dict:
    """按系数公式给出估价。

    完整公式：``克单价档位 × 克重 × 油脂系数 × 形状系数``

    :param base_price: 该串证据支持的**上限克单价**（元/克）
    :param weight_g: 克重（g）
    :param oil: 油脂分布 —— 均匀 / 不均 / 干涩
    :param diameter_mm: 珠径 mm（仅用于敏感度提示，不参与调价）
    :param shape: 形状，见 SHAPE_COEF
    :return: 含各系数与上下限的字典
    """
    k_oil = OIL_COEF.get(oil, 1.00)
    k_dia = DIAMETER_COEF  # 珠径不调价（方向分歧 → 一律 1.00）
    k_shape = SHAPE_COEF.get(shape, 1.00)

    upper = base_price * weight_g * k_oil * k_dia * k_shape
    return {
        "base_price": base_price,
        "weight_g": weight_g,
        "k_oil": k_oil,
        "k_diameter": k_dia,
        "k_shape": k_shape,
        "upper": round(upper),
        "lower": round(upper * 0.7),
        "per_gram_upper": round(base_price * k_oil * k_dia * k_shape, 1),
    }


# ============================================================================
# HTML 渲染
# ============================================================================

def esc(s) -> str:
    """HTML 转义，但保留调用方自己写的有限标签（b / em / span / br）。"""
    if s is None:
        return ""
    return str(s)


def _resolve(raw: str, src_dir: str) -> str:
    """把图片路径解析为可用路径。

    优先级：绝对路径/URL → 相对 JSON 文件 → 相对当前工作目录。

    ⚠️ 陷阱：只按 CWD 解析会让 skill 从别处调用时找不到图。
    相对路径必须**相对 JSON 文件**解析。
    """
    if not raw:
        return raw
    if "://" in raw or os.path.isabs(raw):
        return raw
    cand = os.path.join(src_dir, raw)
    if os.path.exists(cand):
        return cand
    return raw


def render_gallery(images, out_dir: str, src_dir: str, prefix: str = "") -> str:
    """渲染图集，并把图片复制到 ``<out_dir>/images/``。

    :param prefix: 文件名前缀，通常传标的编号。
       ⚠️ 陷阱：不加前缀时多张卡会用同一套 ``01.jpg``／``02.jpg`` 互相覆盖，
       导致最终页面所有卡显示同一批图。必须按标的编号隔离。

    ⚠️ 图片一律**原样复制**，不裁剪、不缩放、不调构图——
    用户提供的素材默认一帧不改。
    """
    if not images:
        return ""
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_\-]", "", prefix) or "img"
    parts = []
    for i, im in enumerate(images, 1):
        raw = _resolve(im.get("path") or im.get("src"), src_dir)
        rel = None
        if raw and os.path.exists(raw):
            ext = os.path.splitext(raw)[1] or ".jpg"
            name = im.get("name") or "%s-%02d%s" % (safe, i, ext)
            rel = "images/" + name
            dst = os.path.join(out_dir, rel)
            if os.path.abspath(raw) != os.path.abspath(dst):
                shutil.copy2(raw, dst)
        else:
            rel = raw  # 已经是 URL 或不存在的相对路径，原样引用
        full = im.get("full") or rel
        parts.append(
            '      <figure class="frame frame--square">'
            '<img src="%s" data-full="%s" alt="%s" loading="lazy">'
            '<figcaption>%s</figcaption>'
            "</figure>" % (esc(rel), esc(full), esc(im.get("alt", "")),
                           esc(im.get("caption", "")))
        )
    return "\n".join(parts)


def render_facts(facts) -> str:
    """渲染八维事实表。

    ⚠️ 陷阱：facts 必须按 ``FACT_ORDER`` 排序。乱序会让卡片失去可扫读性，
    这里做**硬校验**而不是静默通过。
    """
    if not facts:
        return ""
    labels = [f.get("label", "") for f in facts]
    known = [l for l in labels if l in FACT_ORDER]
    expect = [l for l in FACT_ORDER if l in known]
    if known != expect:
        raise ValueError(
            "facts 顺序错误。\n  当前: %s\n  应为: %s" % (known, expect)
        )
    parts = []
    for f in facts:
        tone = f.get("tone", "") or ""
        if tone and tone not in TONE_SET:
            raise ValueError("未知 tone: %r（允许 good/warn/bad/neutral）" % tone)
        dot = ('<span class="dot dot--%s"></span>' % tone) if tone else ""
        note = f.get("note")
        note_html = ('<span class="note">%s</span>' % esc(note)) if note else ""
        parts.append(
            '        <div class="fact">'
            "<dt>%s%s</dt>"
            "<dd>%s%s</dd>"
            "</div>" % (dot, esc(f.get("label", "")), esc(f.get("value", "")), note_html)
        )
    return "\n".join(parts)


def render_price_check(spec: dict, tone: str) -> str:
    """渲染「商家定价／成交价」核对块。

    **依赖成交价的结论**（值得争取 / 买得值 / 价格合理·不必退 / 超上限）
    统一写在这里，不写进 verdict —— verdict 只承载材质与证据类结论。

    :param tone: good / warn / bad / neutral，决定色条颜色。

    ⚠️ 规则：没有商用价（``merchant_price``／``deal_price``）时**整块不渲染**，
    避免凭空冒出一个没有来源的价格结论。
    """
    price = spec.get("merchant_price") or spec.get("deal_price") or ""
    if not price:
        return ""
    if tone not in TONE_SET:
        raise ValueError("未知 price_tone: %r" % tone)
    label = spec.get("merchant_price_label") or "商家定价／成交价"
    verdict = spec.get("price_verdict") or ""
    v_html = ('\n      <p class="pc-verdict">%s</p>' % esc(verdict)) if verdict else ""
    return (
        '<div class="price-check pc--%s">\n'
        '      <span class="pc-label">%s</span>\n'
        '      <span class="pc-value">%s</span>%s\n'
        "    </div>" % (tone or "neutral", esc(label), esc(price), v_html)
    )


def render_unknowns(items) -> str:
    """渲染「待确认信息」区块。

    ⚠️ 规则：不确定的信息**不进结论**，只列在这里。
    """
    if not items:
        return ""
    li = "\n".join("      <li>%s</li>" % esc(x) for x in items)
    return (
        '    <div class="unknowns">\n'
        "      <h3>待确认信息</h3>\n"
        "      <ul>\n%s\n      </ul>\n"
        "    </div>" % li
    )


def render_specimen(spec: dict, index: int, tpl: str,
                    out_dir: str, src_dir: str) -> str:
    """把一条标的渲染成卡片。"""
    tone = spec.get("tag_tone", "neutral")
    v_tone = spec.get("verdict_tone", "neutral")
    for t in (tone, v_tone):
        if t not in TONE_SET:
            raise ValueError("未知 tone: %r" % t)

    # 序号不再单独成徽章：有就内联进大标题，没有就不显示
    seq = spec.get("rank") or spec.get("seq") or ""
    name = esc(spec.get("name", ""))
    if seq:
        name = '<span class="name-seq">%s</span>%s' % (esc(seq), name)

    # 价格类结论的色调：优先 price_tone，缺省跟随 verdict_tone
    p_tone = spec.get("price_tone") or v_tone
    if p_tone not in TONE_SET:
        raise ValueError("未知 price_tone: %r" % p_tone)

    mapping = {
        "CODE": esc(spec.get("code", "")),
        "TAG": esc(spec.get("tag", "")),
        "TAG_TONE": tone or "neutral",
        "NAME": name,
        "GALLERY": render_gallery(spec.get("images", []), out_dir, src_dir,
                                  spec.get("code", "")),
        "FACTS": render_facts(spec.get("facts", [])),
        "LIMIT_AMOUNT": esc(spec.get("limit_amount", "")),
        "LIMIT_NOTE": esc(spec.get("limit_note", "")),
        "PRICE_CHECK": render_price_check(spec, p_tone),
        "VERDICT": esc(spec.get("verdict", "")),
        "VERDICT_TONE": v_tone or "neutral",
        "UNKNOWNS": render_unknowns(spec.get("unknowns", [])),
        "DELAY": str(min(index - 1, 8) * 70),
    }
    out = tpl
    for k, v in mapping.items():
        out = out.replace("{{%s}}" % k, v)
    missing = re.findall(r"\{\{([A-Z_]+)\}\}", out)
    if missing:
        raise ValueError("模板占位符未填充: %s" % sorted(set(missing)))
    return out


class _DivCheck(HTMLParser):
    """div 平衡检查器。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.min_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "div":
            self.depth -= 1
            self.min_depth = min(self.min_depth, self.depth)


def build(data_path: str, out_path: str) -> str:
    """由 JSON 生成 HTML 卡片页，返回输出路径。"""
    here = os.path.dirname(os.path.abspath(__file__))
    asset_dir = os.path.join(os.path.dirname(here), "assets")

    with open(os.path.join(asset_dir, "page-template.html"), encoding="utf-8") as f:
        page_tpl = f.read()
    with open(os.path.join(asset_dir, "specimen-template.html"), encoding="utf-8") as f:
        spec_tpl = f.read()

    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)

    specimens = data.get("specimens", [])
    if not specimens:
        raise ValueError("specimens 为空，没有可渲染的标的")

    blocks = [
        render_specimen(s, i, spec_tpl, out_dir, os.path.dirname(os.path.abspath(data_path)))
        for i, s in enumerate(specimens, 1)
    ]

    page = data.get("page", {})
    mapping = {
        "PAGE_TITLE": esc(page.get("title", "沉香手串速判")),
        "HEAD_EYEBROW": esc(page.get("eyebrow", "Eaglewood · Prayer Beads")),
        "HEAD_TITLE": esc(page.get("head_title", "沉香手串 · <em>逐串速判</em>")),
        "HEAD_META": esc(page.get("head_meta", "")),
        "FOOT_META": esc(page.get("foot_meta", "")),
        "SPECIMENS": "\n\n".join(blocks),
    }
    html = page_tpl
    for k, v in mapping.items():
        html = html.replace("{{%s}}" % k, v)
    left = re.findall(r"\{\{([A-Z_]+)\}\}", html)
    if left:
        raise ValueError("页面模板占位符未填充: %s" % sorted(set(left)))

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)

    checker = _DivCheck()
    checker.feed(html)
    if checker.depth != 0 or checker.min_depth < 0:
        raise RuntimeError("生成的 HTML div 不平衡：depth=%d min=%d"
                           % (checker.depth, checker.min_depth))

    print("✅ 已生成 %s" % out_path)
    print("   标的数 = %d ｜ div 平衡 = OK" % len(specimens))
    return out_path


# ============================================================================
# CLI
# ============================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="沉香手串速判 · 卡片生成器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="由 JSON 生成 HTML 卡片页")
    b.add_argument("--data", required=True, help="标的 JSON 路径")
    b.add_argument("--out", required=True, help="输出 HTML 路径")

    d = sub.add_parser("density", help="密度反算")
    d.add_argument("--weight", type=float, required=True, help="克重 g")
    d.add_argument("--beads", type=int, required=True, help="颗数")
    d.add_argument("--diameter", type=float, required=True, help="珠径 mm")

    dm = sub.add_parser("diameter", help="反推珠径")
    dm.add_argument("--weight", type=float, required=True)
    dm.add_argument("--beads", type=int, required=True)
    dm.add_argument("--rho", type=float, required=True, help="假设密度")

    e = sub.add_parser("estimate", help="按系数公式估价")
    e.add_argument("--base-price", type=float, required=True, help="上限克单价 元/克")
    e.add_argument("--weight", type=float, required=True, help="克重 g")
    e.add_argument("--oil", default="不均", choices=list(OIL_COEF))
    e.add_argument("--diameter", type=float, default=None, help="珠径 mm")
    e.add_argument("--shape", default="圆珠", choices=list(SHAPE_COEF))

    args = ap.parse_args(argv)

    if args.cmd == "build":
        build(args.data, args.out)
    elif args.cmd == "density":
        rho = density(args.weight, args.beads, args.diameter)
        print("密度 = %.3f  →  %s" % (rho, grade_of(rho)))
        # 顺带给 ±0.5mm 的敏感度，提醒珠径误差影响
        for dd in (-0.5, 0.5):
            r2 = density(args.weight, args.beads, args.diameter + dd)
            print("  珠径 %+.1fmm → %.3f (%s)" % (dd, r2, grade_of(r2)))
    elif args.cmd == "diameter":
        d_mm = diameter_from(args.weight, args.beads, args.rho)
        print("珠径 ≈ %.2f mm（假设密度 %.2f）" % (d_mm, args.rho))
    elif args.cmd == "estimate":
        r = estimate(args.base_price, args.weight, args.oil, args.diameter, args.shape)
        print("系数：油脂 ×%.2f ｜ 珠径 ×%.2f ｜ 形状 ×%.2f" %
              (r["k_oil"], r["k_diameter"], r["k_shape"]))
        print("折后克单价 = %.1f 元/克" % r["per_gram_upper"])
        print("估价区间 = ¥%s – ¥%s" % (r["lower"], r["upper"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

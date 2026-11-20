"""试跑定义与页面采样模拟。

这里刻意保持无副作用：模板→探针的数据流、试跑定义哈希、抽样结果都只由
纯函数算出，方便领域服务和示例数据共用同一套规则，也方便回查某一轮到底
是按什么定义跑的。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# 演示站点的“真实 DOM”目录：选择器只有命中这里的节点才算有效。
# 生产环境会换成真实抓取结果，这里用固定目录保证试跑可复现。
PAGE_DOM: dict[str, dict[str, dict[str, str]]] = {
    "https://shop.example.com/item/1001": {
        "h1.title": {"字段": "标题", "值": "星海降噪耳机 Pro"},
        "span.price": {"字段": "价格", "值": "¥1,299.00"},
        "div.stock": {"字段": "库存", "值": "128"},
        "div.rating": {"字段": "评分", "值": "4.8"},
        "div.description": {"字段": "描述", "值": "主动降噪，续航 40 小时。"},
    },
}

_CSS_HINT = re.compile(r"^[#.\[\]\w\s>+~:='\"-]+$")


def split_terms(value: Any) -> list[str]:
    """把逗号分隔的字段/选择器串拆开，去掉空白项。"""
    if value is None:
        return []
    return [part.strip() for part in str(value).replace("，", ",").split(",") if part.strip()]


def join_terms(values: list[str]) -> str:
    return ",".join(values)


def compact_selectors(pairs: list[tuple[str, str]]) -> str:
    """按字段顺序拼选择器，保留中间空位以维持配对，去掉尾部空项。"""
    values = [selector for _, selector in pairs]
    while values and not values[-1].strip():
        values.pop()
    return join_terms(values)


def page_catalog(page: str) -> dict[str, dict[str, str]] | None:
    return PAGE_DOM.get((page or "").strip())


def selector_matches(page: str, selector: str) -> bool:
    """判断选择器能否在该采样页面命中 DOM 节点。"""
    selector = (selector or "").strip()
    if not selector:
        return False
    catalog = page_catalog(page)
    if catalog is not None:
        return selector in catalog
    # 未收录页面无法比对真实 DOM，只做形式校验；命中结果会在说明里标注为模拟。
    return bool(_CSS_HINT.fullmatch(selector))


def selector_candidates(page: str) -> dict[str, str]:
    """字段名 → 页面里建议使用的选择器，供待补字段清单给证据。"""
    catalog = page_catalog(page) or {}
    return {item.get("字段", ""): selector for selector, item in catalog.items()}


def suggest_selector(page: str, field: str) -> str:
    suggestion = selector_candidates(page).get(field, "")
    if suggestion:
        return suggestion
    # 未收录页面给出保守的通用建议，并标注需要人工确认。
    return f"[data-field='{field}']"


def pair_fields(fields_spec: Any, selectors_spec: Any) -> list[tuple[str, str]]:
    """按顺序把提取字段与字段选择器配对；选择器不足时保留空串。"""
    fields = split_terms(fields_spec)
    selectors = split_terms(selectors_spec)
    return [
        (field, selectors[index] if index < len(selectors) else "")
        for index, field in enumerate(fields)
    ]


def build_trial_definition(
    *,
    template: dict[str, Any],
    probe: dict[str, Any] | None,
    pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    """构造可复算的试跑定义。字段、选择器、版本、探针任一变化都会改变它。"""
    return {
        "template_id": template.get("id"),
        "template_version": template.get("版本", "v1"),
        "page": (template.get("采样页面") or template.get("适用站点") or "").strip(),
        "probe_id": probe.get("id") if probe else None,
        "fields": [[field, selector] for field, selector in pairs],
    }


def definition_hash(definition: dict[str, Any]) -> str:
    payload = {
        key: definition.get(key)
        for key in ("template_id", "template_version", "page", "probe_id", "fields")
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


def snapshot_provenance(page: str, definition_hash_value: str) -> dict[str, str]:
    """为每轮试跑生成可回查的页面级采样出处（会被写进页面快照模块）。"""
    page_hash = hashlib.md5((page or "").encode("utf-8")).hexdigest()[:8]
    dom_hash = hashlib.sha256(
        f"{page}|{definition_hash_value}|dom".encode("utf-8")
    ).hexdigest()[:16]
    text_hash = hashlib.sha256(
        f"{page}|{definition_hash_value}|text".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "快照ID": f"snap-{page_hash}-{definition_hash_value[:6]}",
        "页面URI": page,
        "DOM哈希": dom_hash,
        "文本哈希": text_hash,
    }


def sample_field(page: str, field: str, selector: str) -> dict[str, Any]:
    """按字段+选择器抽一条采样记录，结果里带上 DOM 定位信息。"""
    selector = (selector or "").strip()
    if not selector:
        return {
            "字段": field,
            "选择器": "",
            "采样值": "",
            "结果": "未采样",
            "原因": "模板未配置该字段的选择器",
            "DOM定位": "",
        }
    if not selector_matches(page, selector):
        return {
            "字段": field,
            "选择器": selector,
            "采样值": "",
            "结果": "未采样",
            "原因": f"选择器 {selector} 在采样页面未匹配到 DOM 节点",
            "DOM定位": "",
        }
    catalog = page_catalog(page) or {}
    node = catalog.get(selector)
    value = node.get("值", "") if node else f"<{selector} 的采样文本>"
    return {
        "字段": field,
        "选择器": selector,
        "采样值": value,
        "结果": "已采样",
        "原因": "",
        "DOM定位": selector,
        "匹配节点": node.get("字段", field) if node else field,
    }


def sample_page(
    *,
    page: str,
    pairs: list[tuple[str, str]],
    snapshot_id: str,
    definition: dict[str, Any] | None = None,
    definition_hash_value: str = "",
    from_probe: bool = True,
) -> dict[str, Any]:
    """跑一遍字段抽样，返回数据行、待补字段清单和页面级诊断素材。

    `evidence` 只作为定位 DOM 的原始出处，带 `验收状态=未验收`，
    任何调用方都不能把它算进正式字段覆盖。
    """
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for field, selector in pairs:
        row = sample_field(page, field, selector)
        row["采样出处"] = snapshot_id
        rows.append(row)
        if row["结果"] != "已采样":
            suggestion = suggest_selector(page, field)
            candidate = sample_field(page, field, suggestion)
            if candidate["结果"] == "已采样":
                evidence.append({
                    "字段": field,
                    "候选选择器": suggestion,
                    "DOM定位": candidate.get("DOM定位", ""),
                    "采样值": candidate.get("采样值", ""),
                    "采样出处": snapshot_id,
                    "验收状态": "未验收",
                })
            missing.append({
                "字段": field,
                "原因": row["原因"],
                "当前选择器": selector,
                "建议选择器": suggestion,
                "建议来源": "页面 DOM 采样目录" if selector_candidates(page) else "通用选择器建议（需人工确认）",
                "采样出处": snapshot_id,
                "验收状态": "未验收",
            })
    covered = [row for row in rows if row["结果"] == "已采样"]
    target = len(rows)
    formal = bool(target) and len(covered) == target
    if formal:
        dataset_type = "正式字段快照"
    elif covered:
        dataset_type = "部分字段数据集"
    else:
        dataset_type = "无有效字段数据集"

    verifiable = [
        "采样页面可达并生成了页面级快照出处",
        f"模板目标字段清单共 {target} 项，其中 {len(covered)} 项已抽样",
        "待补字段已汇总，可据此逐项补选择器",
    ]
    if evidence:
        verifiable.append(f"为 {len(evidence)} 个待补字段给出了 DOM 定位证据（未验收）")
    not_verifiable = []
    if not covered:
        not_verifiable = [
            "字段抽取值：没有任何字段的选择器命中采样页面",
            "选择器有效性：本轮没有可验证的有效选择器",
            "正式字段覆盖：本数据集不能作为字段覆盖依据",
        ]
    elif not formal:
        not_verifiable = [
            f"剩余 {target - len(covered)} 个字段的抽取值",
            "整体正式字段覆盖：本轮仍是部分字段数据集",
        ]
    return {
        "rows": rows,
        "missing": missing,
        "evidence": evidence,
        "covered_fields": [row["字段"] for row in covered],
        "covered": len(covered),
        "target": target,
        "formal": formal,
        "dataset_type": dataset_type,
        "verifiable": verifiable,
        "not_verifiable": not_verifiable,
        "explanation": _explain(dataset_type, target, len(covered), len(evidence) if evidence else 0),
        "provenance": {
            "采样出处": snapshot_id,
            "页面": page,
            "探针ID": (definition or {}).get("probe_id"),
            "试跑定义哈希": definition_hash_value or (definition or {}).get("definition_hash", ""),
            "经由探针入口": from_probe,
        },
    }


def _explain(dataset_type: str, target: int, covered: int, evidence: int) -> str:
    if dataset_type == "正式字段快照":
        return f"{target} 个目标字段全部采样成功，本轮可作为正式字段覆盖依据。"
    if dataset_type == "部分字段数据集":
        return (
            f"{target} 个目标字段中仅 {covered} 个采样成功，本轮只覆盖部分字段，"
            "不能当作已验收的字段覆盖。"
        )
    if target == 0:
        return "模板没有提取字段，本轮只能确认页面可达与快照出处，不能产出任何字段数据。"
    detail = (
        f"{target} 个目标字段都没有采样成功。原因：模板没有配置可用选择器，"
        "或选择器未在采样页面命中 DOM 节点。"
    )
    if evidence:
        detail += f"已附 {evidence} 条 DOM 定位证据，仅供补字段使用，均为未验收。"
    return detail


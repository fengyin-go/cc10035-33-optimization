"""示例场景：同一页面两版模板 + 旧探针采样历史。

这组数据专门用来复现“模板没有有效选择器、试跑返回空数据集”的空态：
* v1 模板已发布且字段选择器完整，旧探针仍挂在 v1 上，并留有正式试跑历史；
* v2 模板是管理员新发布的版本，暂时没有选择器；
* 探针入口按页面解析时优先用最高发布版本，因此会落到 v2 上。

只使用纯函数，避免在 Store 构造期间回头 import 领域服务。
"""
from __future__ import annotations

from typing import Any

from app.trial_defs import (
    build_trial_definition,
    definition_hash,
    sample_page,
    snapshot_provenance,
)

DEMO_PAGE = "https://shop.example.com/item/1001"
DEMO_FIELDS = "标题,价格,库存,评分,描述"
DEMO_SELECTORS = "h1.title,span.price,div.stock,div.rating,div.description"
DEMO_V1_ID = 101
DEMO_V2_ID = 102
DEMO_PROBE_ID = 101


def bootstrap(store: Any) -> None:
    """把演示模板/探针/历史试跑写进内存仓库（幂等：已存在就跳过）。"""
    templates = store.rows("template_rule")
    if any(row.get("id") in (DEMO_V1_ID, DEMO_V2_ID) for row in templates):
        return

    templates.extend([
        {
            "id": DEMO_V1_ID,
            "status": "已发布",
            "pending": False,
            "abnormal": False,
            "模板名称": "商品详情模板",
            "适用站点": DEMO_PAGE,
            "提取字段": DEMO_FIELDS,
            "字段选择器": DEMO_SELECTORS,
            "采样页面": DEMO_PAGE,
            "版本": "v1",
            "校验规则": "价格>0，库存>=0",
            "更新策略": "页面DOM变更后人工复核",
            "待补字段清单": [],
            "正式字段快照": "否",
            "正式字段覆盖": [],
        },
        {
            "id": DEMO_V2_ID,
            "status": "已发布",
            "pending": True,
            "abnormal": False,
            "模板名称": "商品详情模板",
            "适用站点": DEMO_PAGE,
            "提取字段": DEMO_FIELDS,
            "字段选择器": "",
            "采样页面": DEMO_PAGE,
            "版本": "v2",
            "校验规则": "价格>0，库存>=0",
            "更新策略": "页面DOM变更后人工复核",
            "待补字段清单": [],
            "正式字段快照": "否",
            "正式字段覆盖": [],
        },
    ])

    probes = store.rows("probe_selector")
    probes.append({
        "id": DEMO_PROBE_ID,
        "status": "已验证",
        "pending": False,
        "abnormal": False,
        "探针名称": "商品详情-v1探针",
        "目标页面": DEMO_PAGE,
        "选择器列表": DEMO_SELECTORS,
        "验证关键字": "标题",
        "采样频率": "daily",
        "超时秒数": "30",
        "重试次数": "3",
        "来源模板": "商品详情模板",
        "来源模板ID": DEMO_V1_ID,
        "来源模板版本": "v1",
        "采样历史": "T101-R1",
    })

    _seed_v1_history(store)


def _seed_v1_history(store: Any) -> None:
    """v1 模板 + 旧探针的第 1 轮试跑：字段齐全，产出正式字段快照。"""
    template = next(row for row in store.rows("template_rule") if row["id"] == DEMO_V1_ID)
    probe = next(row for row in store.rows("probe_selector") if row["id"] == DEMO_PROBE_ID)
    pairs = [
        (field, selector)
        for field, selector in zip(DEMO_FIELDS.split(","), DEMO_SELECTORS.split(","))
    ]
    definition = build_trial_definition(template=template, probe=probe, pairs=pairs)
    hash_value = definition_hash(definition)
    snapshot = snapshot_provenance(DEMO_PAGE, hash_value)
    dataset = sample_page(
        page=DEMO_PAGE,
        pairs=pairs,
        snapshot_id=snapshot["快照ID"],
        definition=definition,
        definition_hash_value=hash_value,
    )

    snapshots = store.rows("page_snapshot")
    snapshots.append({
        "id": max((int(row.get("id", 0)) for row in snapshots), default=0) + 1,
        "status": "已保存",
        "pending": False,
        "abnormal": False,
        "快照ID": snapshot["快照ID"],
        "页面URI": snapshot["页面URI"],
        "DOM哈希": snapshot["DOM哈希"],
        "文本哈希": snapshot["文本哈希"],
        "抓取时间": "2026-11-19T10:00:00Z",
        "页面大小": "45.2KB",
        "是否变更": "否",
        "来源模板ID": DEMO_V1_ID,
        "试跑定义哈希": hash_value,
        "采样用途": "探针试跑出处",
    })

    trial = {
        "id": 1,
        "status": "已采样",
        "pending": True,
        "abnormal": False,
        "试跑编号": "T101-R1",
        "轮次": 1,
        "来源模板ID": DEMO_V1_ID,
        "模板名称": template["模板名称"],
        "模板版本": "v1",
        "来源模板版本": "v1",
        "探针ID": DEMO_PROBE_ID,
        "探针名称": probe["探针名称"],
        "探针来源类型": "template_probe",
        "历史探针兼容": "否",
        "目标页面": DEMO_PAGE,
        "试跑入口": "探针选择",
        "试跑状态": "已采样",
        "数据集类型": dataset["dataset_type"],
        "试跑定义": definition,
        "试跑定义哈希": hash_value,
        "重试自": None,
        "采样出处": snapshot["快照ID"],
        "采样时间": "2026-11-19T10:00:00Z",
        "目标字段数": dataset["target"],
        "已采字段数": dataset["covered"],
        "已采字段": ",".join(dataset["covered_fields"]),
        "字段数据": dataset["rows"],
        "页面级诊断": dataset["evidence"],
        "诊断验收状态": "",
        "说明": dataset["explanation"],
        "本轮可验证": dataset["verifiable"],
        "本轮不可验证": dataset["not_verifiable"],
        "正式字段快照": "是" if dataset["formal"] else "否",
        "缺字段数": len(dataset["missing"]),
    }
    store.aux_rows("probe_trial").append(trial)

    template.update({
        "待补字段清单": dataset["missing"],
        "最后试跑轮次": "T101-R1",
        "最后试跑定义": definition,
        "最后试跑定义哈希": hash_value,
        "最后采样出处": snapshot["快照ID"],
        "最后试跑数据集类型": dataset["dataset_type"],
        "正式字段快照": "是" if dataset["formal"] else "否",
        "正式字段覆盖": dataset["covered_fields"],
        "正式快照来源轮次": "T101-R1",
        "正式快照采样出处": snapshot["快照ID"],
        "正式快照定义哈希": hash_value,
        "正式快照时间": "2026-11-19T10:00:00Z",
    })

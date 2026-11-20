"""探针试跑领域服务：从探针入口发起模板试跑，并把结果回写给模板规则。

关键约定：
* 试跑定义由模板版本 + 页面 + 探针 + 字段/选择器配对共同决定，落库后不再变；
  采集中断后的重试必须命中同一个 `试跑定义哈希`，否则拒绝重试并要求重新安排。
* 兼容历史探针只提供采样出处；旧字段不参与新版试跑。
* 只有全部目标字段都采样成功的轮次才会标记为 `正式字段快照`。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.trial_defs import sample_page, snapshot_provenance


class ProbeTrialService(BaseModuleService):
    MODULE = "probe_trial"
    ENTITY = "探针试跑"
    OPERATION_TERM = "试跑"
    LIST_FIELDS = [
        "试跑编号", "模板名称", "模板版本", "探针名称", "目标页面",
        "数据集类型", "试跑状态", "试跑定义哈希", "正式字段快照",
    ]
    REQUIRED_FIELDS = ["试跑编号", "模板名称", "目标页面"]
    STATUS_ORDER = ["待试跑", "采集中", "已采样", "采集中断", "已作废"]
    ACTION_RULES = {"开始采集": "待试跑", "完成采集": "采集中", "采集中断": "采集中", "作废": "已采样"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 存储：试跑轮次是辅助表，不进入 18 个业务模块的清单
    # ------------------------------------------------------------------
    @staticmethod
    def _rows() -> list[dict[str, Any]]:
        return store.aux_rows("probe_trial")

    @classmethod
    def _find(cls, trial_id: int) -> dict[str, Any] | None:
        for row in cls._rows():
            if int(row.get("id", 0)) == int(trial_id):
                return row
        return None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def latest_trial(self, template_id: int) -> dict[str, Any] | None:
        rows = [
            row for row in self._rows()
            if int(row.get("来源模板ID", 0)) == int(template_id)
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("id", 0)))

    def trial_detail(self, trial_id: int) -> dict[str, Any]:
        trial = self._find(trial_id)
        if trial is None:
            return {"ok": False, "reason": "试跑轮次不存在"}
        template = store.find("template_rule", int(trial.get("来源模板ID", 0)))
        return {
            "ok": True,
            "trial": trial,
            "template": template,
            "comparison": self.compare_rounds(trial_id),
            "pending_fields": (template or {}).get("待补字段清单", []),
        }

    def list_for_template(self, template_id: int) -> list[dict[str, Any]]:
        rows = [
            row for row in self._rows()
            if int(row.get("来源模板ID", 0)) == int(template_id)
        ]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    # ------------------------------------------------------------------
    # 发起试跑
    # ------------------------------------------------------------------
    def start_trial(
        self,
        template_id: int,
        *,
        probe_id: int | None = None,
        retry_of: int | None = None,
        definition_hash_value: str | None = None,
        entry: str = "模板规则",
    ) -> dict[str, Any]:
        from app.services.template_rule import TemplateRuleService

        template_service = TemplateRuleService()
        template = store.find("template_rule", template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}

        config = template_service.trial_config(template_id, probe_id)
        if not config.get("ok"):
            return config
        page = config["page"]
        if not page:
            return {"ok": False, "reason": "模板没有采样页面/适用站点，无法安排试跑"}

        current_hash = config["definition_hash"]
        retry_definition = None
        if retry_of is not None:
            previous = self._find(retry_of)
            if previous is None:
                return {"ok": False, "reason": f"待重试的试跑轮次 {retry_of} 不存在"}
            if previous.get("试跑状态") not in ("采集中断",):
                return {"ok": False, "reason": "只有采集中断的轮次才允许重试"}
            if definition_hash_value and definition_hash_value != previous.get("试跑定义哈希"):
                return {"ok": False, "reason": "重试定义与中断轮次不一致，请使用原试跑定义"}
            if previous.get("试跑定义哈希") != current_hash:
                return {
                    "ok": False,
                    "reason": (
                        "模板或选择器已变更，原试跑定义不再适用；"
                        "请修改字段后重新安排本页的正式试跑"
                    ),
                    "expected_definition_hash": previous.get("试跑定义哈希"),
                    "current_definition_hash": current_hash,
                }
            retry_definition = previous

        # 重试时字段/选择器必须取中断轮次保存的定义，而不是模板当前值。
        source_definition = (
            retry_definition.get("试跑定义") if retry_definition is not None
            else config["trial_definition"]
        )
        pairs = [(item[0], item[1]) for item in source_definition.get("fields", [])]
        snapshot = snapshot_provenance(page, current_hash)

        if retry_definition is not None:
            # 同一试跑定义意味着同一快照出处；沿用中断轮次的快照ID便于回查。
            snapshot["快照ID"] = retry_definition.get("采样出处") or snapshot["快照ID"]

        # 页面快照模块是采样出处的唯一登记处；试跑只引用快照ID。
        self._record_snapshot(snapshot, template, current_hash)

        dataset = sample_page(
            page=page,
            pairs=pairs,
            snapshot_id=snapshot["快照ID"],
            definition=source_definition,
            definition_hash_value=current_hash,
        )

        rows = self._rows()
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        round_no = max(
            (int(row.get("轮次", 0)) for row in rows
             if int(row.get("来源模板ID", 0)) == int(template_id)),
            default=0,
        ) + 1
        trial = {
            "id": next_id,
            "status": "已采样",
            "pending": True,
            "abnormal": not dataset["formal"],
            "试跑编号": f"T{template_id}-R{round_no}",
            "轮次": round_no,
            "来源模板ID": template_id,
            "模板名称": template.get("模板名称"),
            "模板版本": config["template_version"],
            "来源模板版本": config["template_version"],
            "探针ID": config["probe_source"].get("id"),
            "探针名称": config["probe_source"].get("name", "无探针（仅模板）"),
            "探针来源类型": config["probe_source"].get("source_type"),
            "历史探针兼容": "是" if config["probe_source"].get("compatible_legacy") else "否",
            "目标页面": page,
            "试跑入口": entry,
            "试跑状态": "已采样",
            "数据集类型": dataset["dataset_type"],
            "试跑定义": source_definition,
            "试跑定义哈希": current_hash,
            "重试自": retry_of,
            "采样出处": snapshot["快照ID"],
            "采样时间": _now(),
            "目标字段数": dataset["target"],
            "已采字段数": dataset["covered"],
            "已采字段": ",".join(dataset["covered_fields"]),
            "字段数据": dataset["rows"],
            "页面级诊断": dataset["evidence"],
            "诊断验收状态": "未验收" if dataset["evidence"] else "",
            "说明": dataset["explanation"],
            "本轮可验证": dataset["verifiable"],
            "本轮不可验证": dataset["not_verifiable"],
            "正式字段快照": "是" if dataset["formal"] else "否",
            "缺字段数": len(dataset["missing"]),
        }
        if retry_definition is not None:
            trial["重试说明"] = "沿用中断轮次的同一试跑定义"
        rows.append(trial)

        written = template_service.write_back_trial(
            template_id,
            trial=trial,
            missing=dataset["missing"],
            dataset=dataset,
        )
        return {
            "ok": True,
            "trial": trial,
            "dataset": {
                "dataset_type": dataset["dataset_type"],
                "explanation": dataset["explanation"],
                "rows": dataset["rows"],
                "missing": dataset["missing"],
                "evidence": dataset["evidence"],
                "verifiable": dataset["verifiable"],
                "not_verifiable": dataset["not_verifiable"],
                "formal": dataset["formal"],
            },
            "template_write_back": written,
        }

    def retry_trial(self, trial_id: int, definition_hash_value: str | None = None) -> dict[str, Any]:
        """采集中断后重试：必须沿用同一个试跑定义。"""
        trial = self._find(trial_id)
        if trial is None:
            return {"ok": False, "reason": "试跑轮次不存在"}
        return self.start_trial(
            int(trial.get("来源模板ID", 0)),
            probe_id=trial.get("探针ID"),
            retry_of=trial_id,
            definition_hash_value=definition_hash_value or trial.get("试跑定义哈希"),
            entry=trial.get("试跑入口", "模板规则"),
        )

    def drop_formal_snapshot(self, template_id: int) -> dict[str, Any]:
        """清空模板选择器后，旧轮次不能再被当作正式字段快照。"""
        template = store.find("template_rule", template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}
        template["正式字段快照"] = "否"
        template["正式字段覆盖"] = []
        return {"ok": True, "template_id": template_id}

    # ------------------------------------------------------------------
    # 轮次对比
    # ------------------------------------------------------------------
    def compare_rounds(self, trial_id: int) -> dict[str, Any]:
        trial = self._find(trial_id)
        if trial is None:
            return {"ok": False, "reason": "试跑轮次不存在"}
        template_id = int(trial.get("来源模板ID", 0))
        rounds = self.list_for_template(template_id)
        previous = None
        for row in rounds:
            if int(row.get("id", 0)) < int(trial_id):
                previous = row
        if previous is None:
            return {
                "ok": True,
                "current_round": trial.get("轮次"),
                "previous_round": None,
                "message": "这是本模板的第一轮试跑，没有可对比的历史轮次",
            }

        def by_field(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
            return {item.get("字段"): item for item in row.get("字段数据", [])}

        current_fields = by_field(trial)
        previous_fields = by_field(previous)
        deltas = []
        for field in current_fields:
            now = current_fields[field]
            before = previous_fields.get(field, {})
            deltas.append({
                "字段": field,
                "上轮选择器": before.get("选择器", ""),
                "本轮选择器": now.get("选择器", ""),
                "上轮结果": before.get("结果", "未参与"),
                "本轮结果": now.get("结果", "未参与"),
                "上轮采样值": before.get("采样值", ""),
                "本轮采样值": now.get("采样值", ""),
                "变化": _delta_label(before, now),
            })
        return {
            "ok": True,
            "current_round": trial.get("轮次"),
            "current_trial_id": trial.get("id"),
            "previous_round": previous.get("轮次"),
            "previous_trial_id": previous.get("id"),
            "previous_definition_hash": previous.get("试跑定义哈希"),
            "current_definition_hash": trial.get("试跑定义哈希"),
            "definition_changed": previous.get("试跑定义哈希") != trial.get("试跑定义哈希"),
            "previous_formal": previous.get("正式字段快照"),
            "current_formal": trial.get("正式字段快照"),
            "previous_covered": previous.get("已采字段数"),
            "current_covered": trial.get("已采字段数"),
            "covered_delta": int(trial.get("已采字段数", 0)) - int(previous.get("已采字段数", 0)),
            "fields": deltas,
            "previous_snapshot": previous.get("采样出处"),
            "current_snapshot": trial.get("采样出处"),
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _record_snapshot(
        self, snapshot: dict[str, str], template: dict[str, Any], definition_hash_value: str
    ) -> None:
        from app.services.page_snapshot import PageSnapshotService

        rows = store.rows("page_snapshot")
        existing = next(
            (row for row in rows if row.get("快照ID") == snapshot["快照ID"]), None)
        if existing is not None:
            return
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append({
            "id": next_id,
            "status": PageSnapshotService.STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "快照ID": snapshot["快照ID"],
            "页面URI": snapshot["页面URI"],
            "DOM哈希": snapshot["DOM哈希"],
            "文本哈希": snapshot["文本哈希"],
            "抓取时间": _now(),
            "页面大小": "45.2KB",
            "是否变更": "否",
            "来源模板ID": template.get("id"),
            "试跑定义哈希": definition_hash_value,
            "采样用途": "探针试跑出处",
        })


def _delta_label(before: dict[str, Any], now: dict[str, Any]) -> str:
    was = before.get("结果", "未参与")
    is_now = now.get("结果", "未参与")
    if was == is_now:
        return "无变化"
    if was != "已采样" and is_now == "已采样":
        return "新增覆盖"
    if was == "已采样" and is_now != "已采样":
        return "覆盖丢失"
    return "结果变化"


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

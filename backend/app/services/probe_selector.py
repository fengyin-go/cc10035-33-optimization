"""探针选择领域服务：管理页面元素的自动化探测探针，并作为试跑入口。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.trial_defs import split_terms


class ProbeSelectorService(BaseModuleService):
    MODULE = "probe_selector"
    ENTITY = "探针配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["探针名称", "目标页面", "选择器列表", "验证关键字", "采样频率", "超时秒数", "重试次数"]
    REQUIRED_FIELDS = ["探针名称", "目标页面", "选择器列表"]
    STATUS_ORDER = ["草稿", "探测中", "已验证", "已废弃"]
    ACTION_RULES = {"启动探测": "草稿", "验证探针": "探测中", "废弃探针": "已验证"}
    NEGATIVE_ACTIONS = []

    def trigger_snapshot(self, probe_id: int) -> dict[str, Any]:
        """触发页面快照：探针→快照的跨模块联动。被页面快照消费。"""
        probe = store.find(self.MODULE, probe_id)
        if probe is None:
            return {"ok": False, "reason": "探针不存在"}
        target_page = probe.get("目标页面", "")
        selectors = probe.get("选择器列表", "")

        page_hash = hashlib.md5(target_page.encode()).hexdigest()[:8]
        dom_hash = hashlib.sha256(
            f"{target_page}|{selectors}|{hashlib.md5(b'dummy_dom').hexdigest()[:8]}".encode()
        ).hexdigest()[:16]
        text_hash = hashlib.sha256(
            f"{target_page}|text_content".encode()
        ).hexdigest()[:16]

        return {
            "ok": True,
            "probe_id": probe_id,
            "probe_name": probe.get("探针名称"),
            "target_page": target_page,
            "snapshot_request": {
                "快照ID": f"snap-{page_hash}",
                "页面URI": target_page,
                "DOM哈希": dom_hash,
                "文本哈希": text_hash,
                "抓取时间": "2026-11-19T10:00:00Z",
                "页面大小": "45.2KB",
            },
        }

    def compute_probe_health(self) -> dict[str, Any]:
        """探针健康度计算，供聚合视图和抽取指标消费。"""
        rows = store.rows(self.MODULE)
        valid = sum(1 for r in rows if r.get("status") == "已验证")
        active = sum(1 for r in rows if r.get("status") == "探测中")
        dead = sum(1 for r in rows if r.get("status") == "已废弃")
        total = len(rows)
        return {
            "total_probes": total,
            "valid_probes": valid,
            "active_probes": active,
            "dead_probes": dead,
            "health_ratio": round(valid / total, 2) if total > 0 else 0.0,
        }

    def get_probes_for_page(self, page_uri: str) -> list[dict[str, Any]]:
        """按目标页面过滤探针，并标注每个探针的来源模板版本。"""
        page_uri = (page_uri or "").strip()
        return [
            {"id": r.get("id"), "name": r.get("探针名称"),
             "selectors": r.get("选择器列表"), "status": r.get("status"),
             "probe_template_version": r.get("来源模板版本"),
             "probe_template_id": r.get("来源模板ID"),
             "selectors_count": len(split_terms(r.get("选择器列表")))}
            for r in store.rows(self.MODULE)
            if (r.get("目标页面") or "").strip() == page_uri
        ]

    def describe_probe_source(
        self, probe: dict[str, Any] | None, template: dict[str, Any]
    ) -> dict[str, Any]:
        """说明本轮试跑的探针来源，区分当前模板探针与兼容历史探针。

        历史探针只作为采样出处，旧字段不参与新版试跑；需要补字段时，
        必须由本轮试跑给出 DOM 定位证据，不能直接沿用旧快照。
        """
        template_version = str(template.get("版本") or "v1")
        if probe is None:
            return {
                "id": None,
                "name": "无探针（仅模板）",
                "source_type": "template_only",
                "compatible_legacy": False,
                "probe_template_version": None,
                "message": "该页面没有可用探针，试跑只按模板定义采样",
            }
        probe_version = probe.get("来源模板版本")
        probe_template_id = probe.get("来源模板ID")
        same_version = (
            probe_version == template_version
            and (probe_template_id in (None, template.get("id")))
        )
        return {
            "id": probe.get("id"),
            "name": probe.get("探针名称"),
            "source_type": "template_probe" if same_version else "legacy_probe",
            "compatible_legacy": not same_version,
            "probe_template_version": probe_version,
            "probe_template_id": probe_template_id,
            "probe_selectors": split_terms(probe.get("选择器列表")),
            "message": (
                "探针来自当前模板版本，选择器与本轮试跑定义一致"
                if same_version
                else (
                    f"探针来自历史模板版本 {probe_version or '未知'}，仅保留采样出处；"
                    "旧字段不参与本轮试跑"
                )
            ),
        }

    def run_trial(self, probe_id: int, template_id: int | None = None,
                  retry_of: int | None = None) -> dict[str, Any]:
        """探针选择页的试跑入口：定位模板后发起/重试一轮试跑。"""
        from app.services.probe_trial import ProbeTrialService
        from app.services.template_rule import TemplateRuleService

        probe = store.find(self.MODULE, probe_id)
        if probe is None:
            return {"ok": False, "reason": "探针不存在"}
        page = (probe.get("目标页面") or "").strip()
        template_service = TemplateRuleService()
        template = store.find("template_rule", template_id) if template_id is not None else None
        if template is None:
            template = template_service.resolve_trial_template(page)
        if template is None:
            return {"ok": False, "reason": f"页面 {page} 没有可试跑的模板"}
        trial_service = ProbeTrialService()
        return trial_service.start_trial(
            int(template.get("id")), probe_id=probe_id, retry_of=retry_of, entry="探针选择")

    def page_diagnostics(self, probe_id: int) -> dict[str, Any]:
        """页面级诊断：只回报有实际采样出处的 DOM 定位证据。

        诊断结果永远标记为未验收，不能被当作字段覆盖。
        """
        probe = store.find(self.MODULE, probe_id)
        if probe is None:
            return {"ok": False, "reason": "探针不存在"}
        page = (probe.get("目标页面") or "").strip()
        trials = [
            row for row in store.aux_rows("probe_trial")
            if (row.get("目标页面") or "").strip() == page
        ]
        latest = max(trials, key=lambda row: int(row.get("id", 0))) if trials else None
        evidence = (latest or {}).get("页面级诊断", [])
        usable = [item for item in evidence if item.get("采样出处") and item.get("DOM定位")]
        return {
            "ok": True,
            "probe_id": probe_id,
            "page": page,
            "trial": latest,
            "diagnostics": usable,
            "discarded": len(evidence) - len(usable),
            "acceptance": "未验收",
            "note": (
                "诊断条目只提供 DOM 定位与采样出处，用于补字段；"
                "不得计入已验收字段覆盖"
            ),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["probe_health"] = self.compute_probe_health()
        return base
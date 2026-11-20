"""模板规则领域服务：管理站点级字段抽取模板，并承接探针试跑回写的待补字段。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.trial_defs import (
    build_trial_definition,
    compact_selectors,
    definition_hash,
    pair_fields,
    selector_matches,
    split_terms,
)

# 这两个状态代表模板已经进入可用链路，字段空值必须按错误处理。
VERIFIED_STATUSES = ("已验证", "已发布")


class TemplateRuleService(BaseModuleService):
    MODULE = "template_rule"
    ENTITY = "规则模板"
    OPERATION_TERM = "采集"
    LIST_FIELDS = [
        "模板名称", "适用站点", "提取字段", "字段选择器", "采样页面",
        "版本", "校验规则", "更新策略",
    ]
    REQUIRED_FIELDS = ["模板名称", "适用站点", "提取字段"]
    STATUS_ORDER = ["草稿", "已验证", "已发布", "已失效"]
    ACTION_RULES = {"验证模板": "草稿", "发布模板": "已验证", "标记失效": "已发布"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 模板基础能力
    # ------------------------------------------------------------------
    @staticmethod
    def template_version(template: dict[str, Any]) -> str:
        return str(template.get("版本") or "v1")

    def _version_rank(self, template: dict[str, Any]) -> tuple[int, int]:
        version = self.template_version(template)
        try:
            number = int(version.lstrip("v") or 1)
        except ValueError:
            number = 1
        return number, int(template.get("id", 0))

    def resolve_trial_template(self, page: str) -> dict[str, Any] | None:
        """按页面找当前用于试跑的模板：已发布里版本最高的一版。"""
        page = (page or "").strip()
        if not page:
            return None
        candidates = [
            row for row in store.rows(self.MODULE)
            if page in ((row.get("采样页面") or "").strip(), (row.get("适用站点") or "").strip())
        ]
        if not candidates:
            return None
        published = [row for row in candidates if row.get("status") == "已发布"]
        pool = published or candidates
        return max(pool, key=self._version_rank)

    def template_versions_for_page(self, page: str) -> list[dict[str, Any]]:
        """同一页面的所有可回查版本，供历史排查。"""
        page = (page or "").strip()
        rows = [
            row for row in store.rows(self.MODULE)
            if page in ((row.get("采样页面") or "").strip(), (row.get("适用站点") or "").strip())
        ]
        return sorted(rows, key=self._version_rank, reverse=True)

    def validate_template_config(self, template: dict[str, Any]) -> list[dict[str, Any]]:
        """校验模板字段与选择器。字段为空的已验证/已发布模板必须报错。"""
        issues: list[dict[str, Any]] = []
        status = template.get("status")
        strict = status in VERIFIED_STATUSES
        for field in self.REQUIRED_FIELDS:
            value = str(template.get(field) or "").strip()
            if not value:
                issues.append({
                    "code": "TEMPLATE_REQUIRED_EMPTY",
                    "severity": "error" if strict else "warning",
                    "field": field,
                    "message": f"{status or '草稿'}模板的必填字段「{field}」为空",
                })
        fields = split_terms(template.get("提取字段"))
        selectors = split_terms(template.get("字段选择器"))
        if not fields:
            issues.append({
                "code": "TARGET_FIELDS_EMPTY",
                "severity": "error" if strict else "warning",
                "field": "提取字段",
                "message": "模板未配置提取字段，本轮试跑只能产出无有效字段数据集",
            })
            return issues
        if not selectors:
            # 允许发布暂缺选择器的模板（管理员会先发布再补字段），
            # 但必须明确告知：本轮试跑不会产出可验收字段。
            issues.append({
                "code": "SELECTORS_EMPTY",
                "severity": "warning",
                "field": "字段选择器",
                "message": "模板没有配置任何字段选择器，试跑不会产出可验收字段",
            })
        elif len(selectors) < len(fields):
            issues.append({
                "code": "SELECTORS_PARTIAL",
                "severity": "warning",
                "field": "字段选择器",
                "message": f"{len(fields) - len(selectors)} 个提取字段还没有选择器",
            })
        elif len(selectors) > len(fields):
            issues.append({
                "code": "SELECTORS_EXTRA",
                "severity": "warning",
                "field": "字段选择器",
                "message": f"选择器比提取字段多出 {len(selectors) - len(fields)} 项，多余项不会参与试跑",
            })
        page = (template.get("采样页面") or template.get("适用站点") or "").strip()
        if not page:
            issues.append({
                "code": "SAMPLE_PAGE_EMPTY",
                "severity": "error",
                "field": "采样页面",
                "message": "模板没有采样页面，试跑无法定位采样出处",
            })
        for index, selector in enumerate(selectors):
            if index >= len(fields):
                break
            if not selector:
                continue
            if page and not selector_matches(page, selector):
                issues.append({
                    "code": "SELECTOR_NOT_MATCHED",
                    "severity": "warning",
                    "field": fields[index],
                    "message": f"字段「{fields[index]}」的选择器 {selector} 未在采样页面命中 DOM 节点",
                })
        return issues

    def trial_pairs(self, template: dict[str, Any]) -> list[tuple[str, str]]:
        return pair_fields(template.get("提取字段"), template.get("字段选择器"))

    def validation_issues(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        # 登记时一律从草稿开始，字段缺失按警告提示，不阻断保存。
        draft = dict(values)
        draft.setdefault("status", self.STATUS_ORDER[0])
        return self.validate_template_config(draft)

    def action_issues(self, entry: dict[str, Any], action: str) -> list[dict[str, Any]]:
        """验证/发布前校验：字段为空的已验证模板不允许进入可用状态。"""
        if action not in ("验证模板", "发布模板"):
            return []
        # 动作后的状态即为严格校验目标。
        prospective = dict(entry)
        prospective["status"] = self.ACTION_RULES.get(action, entry.get("status"))
        return self.validate_template_config(prospective)

    def trial_definition(self, template: dict[str, Any], probe: dict[str, Any] | None) -> dict[str, Any]:
        return build_trial_definition(
            template=template, probe=probe, pairs=self.trial_pairs(template))

    # ------------------------------------------------------------------
    # 模板 → 探针：建议探针与试跑配置来源
    # ------------------------------------------------------------------
    def suggest_probes(self, template_id: int) -> dict[str, Any]:
        """根据模板规则推导建议的探针配置，供探针选择模块消费。

        没有有效选择器时直接返回空数组：这是模板到探针链路的停点，
        不能让调用方误以为还需要派发一个“空选择器探针”。
        """
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}
        site = (template.get("适用站点") or "").strip()
        sample_page = (template.get("采样页面") or "").strip()
        page = sample_page or site
        pairs = [
            (field, selector)
            for field, selector in self.trial_pairs(template)
            if selector
        ]

        probes = []
        for i, (field, selector) in enumerate(pairs):
            probes.append({
                "探针名称": f"{template.get('模板名称')}-{self.template_version(template)}-probe-{i + 1}",
                "目标页面": page,
                "选择器列表": selector,
                "验证关键字": field,
                "采样频率": "daily",
                "超时秒数": "30",
                "重试次数": "3",
                "来源模板": template.get("模板名称"),
                "来源模板版本": self.template_version(template),
                "来源模板ID": template.get("id"),
            })
        return {
            "ok": True,
            "template_id": template_id,
            "template_name": template.get("模板名称"),
            "template_version": self.template_version(template),
            "target_page": page,
            "suggested_probe_count": len(probes),
            "probes": probes,
            "reason": "" if probes else "模板没有有效字段选择器，建议探针返回空数组",
        }

    def trial_config(self, template_id: int, probe_id: int | None = None) -> dict[str, Any]:
        """输出本轮试跑的配置来源：模板版本、探针来源、校验问题和定义哈希。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}

        from app.services.probe_selector import ProbeSelectorService

        probe_service = ProbeSelectorService()
        page = (template.get("采样页面") or template.get("适用站点") or "").strip()
        probe = None
        if probe_id is not None:
            probe = store.find("probe_selector", probe_id)
            if probe is None:
                return {"ok": False, "reason": f"探针 {probe_id} 不存在"}
        else:
            page_probes = probe_service.get_probes_for_page(page)
            # 优先取已验证的探针；同一页面可能只剩历史探针，仍作为采样出处。
            probe = next(
                (store.find("probe_selector", item["id"]) for item in page_probes
                 if item.get("status") == "已验证"),
                store.find("probe_selector", page_probes[0]["id"]) if page_probes else None,
            )

        probe_source = probe_service.describe_probe_source(probe, template)
        pairs = self.trial_pairs(template)
        definition = build_trial_definition(template=template, probe=probe, pairs=pairs)
        suggested = self.suggest_probes(template_id)
        issues = self.validate_template_config(template)
        if probe is not None and probe_source["compatible_legacy"]:
            issues.append({
                "code": "LEGACY_PROBE_SOURCE",
                "severity": "info",
                "field": "探针来源",
                "message": (
                    f"探针「{probe.get('探针名称')}」来自历史模板版本 "
                    f"{probe_source.get('probe_template_version') or '未知'}，"
                    "仅作为采样出处，旧字段不参与本轮试跑"
                ),
            })
        selectors = [selector for _, selector in pairs]
        matched = [selector for selector in selectors if selector_matches(page, selector)]
        # can_run 表示“能发起一轮试跑并留下采样出处”，即使字段全空也允许；
        # can_produce_official 才表示本轮能产出正式字段快照。
        can_run = bool(page)
        return {
            "ok": True,
            "template_id": template_id,
            "template_name": template.get("模板名称"),
            "template_version": self.template_version(template),
            "template_status": template.get("status"),
            "page": page,
            "probe_source": probe_source,
            "candidate_probes": probe_service.get_probes_for_page(page),
            "suggested_probes": suggested["probes"],
            "suggested_probe_count": suggested["suggested_probe_count"],
            "suggested_reason": suggested.get("reason", ""),
            "trial_definition": definition,
            "definition_hash": definition_hash(definition),
            "target_fields": [field for field, _ in pairs],
            "selector_count": sum(1 for selector in selectors if selector),
            "matched_selector_count": len(matched),
            "issues": issues,
            "can_run": can_run,
            "can_produce_official": bool(matched) and len(matched) == len(pairs),
            "pending_fields": template.get("待补字段清单", []),
            "versions": [
                {
                    "id": row.get("id"),
                    "version": self.template_version(row),
                    "status": row.get("status"),
                    "has_selectors": bool(split_terms(row.get("字段选择器"))),
                }
                for row in self.template_versions_for_page(page)
            ],
        }

    # ------------------------------------------------------------------
    # 回写待补字段清单 / 补齐选择器
    # ------------------------------------------------------------------
    def update_selectors(
        self,
        template_id: int,
        *,
        field: str | None = None,
        selector: str | None = None,
        selectors: str | None = None,
        target_fields: str | None = None,
    ) -> dict[str, Any]:
        """补齐或清空模板选择器；补一项后即可继续试跑。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}

        before_definition = self.trial_definition(template, None)
        before_hash = definition_hash(before_definition)

        if target_fields is not None:
            template["提取字段"] = target_fields
        if selectors is not None:
            template["字段选择器"] = selectors

        filled = None
        if field is not None and selector is not None:
            pairs = self.trial_pairs(template)
            updated = False
            for index, (name, _) in enumerate(pairs):
                if name == field:
                    pairs[index] = (name, selector.strip())
                    updated = True
                    break
            if not updated:
                pairs.append((field, selector.strip()))
            template["提取字段"] = ",".join(name for name, _ in pairs if name)
            template["字段选择器"] = compact_selectors(pairs)
            filled = {"字段": field, "选择器": selector.strip()}

        # 已补回的选择器从待补清单里移除，其余保留，便于发布人逐项处理。
        current_selectors = dict(self.trial_pairs(template))
        pending = []
        for item in template.get("待补字段清单", []):
            name = item.get("字段")
            if name and current_selectors.get(name):
                continue
            pending.append(item)
        template["待补字段清单"] = pending
        template["字段选择器更新时间"] = _now()
        # 选择器实际变化后，旧的正式字段快照就不能再代表当前模板；
        # 保留来源轮次便于回查，但标记为需要重新安排正式试跑。
        definition_changed = definition_hash(
            self.trial_definition(template, None)) != before_hash
        if definition_changed and template.get("正式字段快照") == "是":
            template["正式快照状态"] = "已过期（选择器已变更，需重新安排正式试跑）"

        from app.services.probe_trial import ProbeTrialService

        trial_service = ProbeTrialService()
        latest = trial_service.latest_trial(template_id)
        return {
            "ok": True,
            "template_id": template_id,
            "filled": filled,
            "definition_changed": definition_changed,
            "字段选择器": template.get("字段选择器"),
            "待补字段清单": pending,
            "can_continue_trial": bool(
                [selector for _, selector in self.trial_pairs(template) if selector]
            ),
            "latest_round": latest.get("试跑编号") if latest else None,
            "trial_config": self.trial_config(template_id),
        }

    def write_back_trial(
        self,
        template_id: int,
        *,
        trial: dict[str, Any],
        missing: list[dict[str, Any]],
        dataset: dict[str, Any],
    ) -> dict[str, Any]:
        """探针把本轮数据集同步给模板：写待补字段清单、记录轮次与正式覆盖。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}

        # 待补清单以本轮结果为准，历史轮次里已补回的字段不重复出现。
        template["待补字段清单"] = missing
        template["最后试跑轮次"] = trial.get("试跑编号")
        template["最后试跑定义"] = trial.get("试跑定义")
        template["最后试跑定义哈希"] = trial.get("试跑定义哈希")
        template["最后采样出处"] = trial.get("采样出处")
        template["最后试跑数据集类型"] = dataset.get("dataset_type")

        if dataset.get("formal"):
            # 正式字段快照只有在全部目标字段采样成功的那一轮才写入。
            template["正式字段快照"] = "是"
            template["正式字段覆盖"] = dataset.get("covered_fields", [])
            template["正式快照来源轮次"] = trial.get("试跑编号")
            template["正式快照采样出处"] = trial.get("采样出处")
            template["正式快照定义哈希"] = trial.get("试跑定义哈希")
            template["正式快照时间"] = trial.get("采样时间")
            template["正式快照状态"] = "有效"
        else:
            # 非正式轮次不得覆盖既有正式快照，只记录“本轮未产出正式快照”。
            template.setdefault("正式字段快照", "否")
            template.setdefault("正式字段覆盖", [])
        return {
            "ok": True,
            "template_id": template_id,
            "pending_fields": missing,
            "pending_count": len(missing),
            "last_round": trial.get("试跑编号"),
            "formal_snapshot": template.get("正式字段快照"),
            "formal_coverage": template.get("正式字段覆盖", []),
            "formal_snapshot_round": template.get("正式快照来源轮次"),
            "round_produced_formal": bool(dataset.get("formal")),
        }

    def pending_fields(self, template_id: int) -> dict[str, Any]:
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}
        from app.services.probe_trial import ProbeTrialService

        latest = ProbeTrialService().latest_trial(template_id)
        return {
            "ok": True,
            "template_id": template_id,
            "template_version": self.template_version(template),
            "pending_fields": template.get("待补字段清单", []),
            "last_round": template.get("最后试跑轮次"),
            "last_definition": template.get("最后试跑定义"),
            "last_dataset_type": template.get("最后试跑数据集类型"),
            "last_snapshot": template.get("最后采样出处"),
            "formal_snapshot": template.get("正式字段快照", "否"),
            "formal_snapshot_status": template.get(
                "正式快照状态", "有效" if template.get("正式字段快照") == "是" else "无"),
            "formal_coverage": template.get("正式字段覆盖", []),
            "formal_snapshot_round": template.get("正式快照来源轮次"),
            "formal_snapshot_source": template.get("正式快照采样出处"),
            "latest_trial": latest,
        }

    # ------------------------------------------------------------------
    # 既有的跨模块消费
    # ------------------------------------------------------------------
    def compute_template_coverage(self) -> dict[str, Any]:
        """计算模板覆盖范围，供抽取指标消费。"""
        rows = store.rows(self.MODULE)
        sites: dict[str, int] = {}
        total_selectors = 0
        total_fields = 0
        for row in rows:
            site = row.get("适用站点", "")
            sites[site] = sites.get(site, 0) + 1
            total_selectors += len(split_terms(row.get("字段选择器")))
            total_fields += len(split_terms(row.get("提取字段")))
        return {
            "template_count": len(rows),
            "site_count": len(sites),
            "total_selectors": total_selectors,
            "total_target_fields": total_fields,
            "published": sum(1 for r in rows if r.get("status") == "已发布"),
        }

    def get_published_templates(self) -> list[dict[str, Any]]:
        """取出所有已发布模板，供质量检查跨模块比对。"""
        return [
            {
                "id": r.get("id"),
                "name": r.get("模板名称"),
                "site": r.get("适用站点"),
                "fields": r.get("提取字段"),
                "selectors": r.get("字段选择器"),
                "sample_page": r.get("采样页面"),
                "version": self.template_version(r),
            }
            for r in store.rows(self.MODULE)
            if r.get("status") == "已发布"
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["template_coverage"] = self.compute_template_coverage()
        return base


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

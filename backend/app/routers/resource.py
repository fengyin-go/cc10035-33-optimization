"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


# ----------------------------------------------------------------------
# 模板 ↔ 探针试跑：领域动作接口
# ----------------------------------------------------------------------
@router.get("/template_rule/{entry_id}/probe-suggestions", response_model=dict)
def template_probe_suggestions(entry_id: int) -> dict[str, Any]:
    from app.services.template_rule import TemplateRuleService

    return TemplateRuleService().suggest_probes(entry_id)


@router.get("/template_rule/{entry_id}/trial-config", response_model=dict)
def template_trial_config(entry_id: int, probe_id: int | None = Query(default=None)) -> dict[str, Any]:
    from app.services.template_rule import TemplateRuleService

    return TemplateRuleService().trial_config(entry_id, probe_id)


@router.get("/template_rule/{entry_id}/pending-fields", response_model=dict)
def template_pending_fields(entry_id: int) -> dict[str, Any]:
    from app.services.template_rule import TemplateRuleService

    return TemplateRuleService().pending_fields(entry_id)


@router.post("/template_rule/{entry_id}/selectors", response_model=dict)
def template_update_selectors(entry_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.template_rule import TemplateRuleService

    return TemplateRuleService().update_selectors(
        entry_id,
        field=payload.get("field"),
        selector=payload.get("selector"),
        selectors=payload.get("selectors"),
        target_fields=payload.get("target_fields"),
    )


@router.get("/template_rule/{entry_id}/trials", response_model=dict)
def template_trials(entry_id: int) -> dict[str, Any]:
    from app.services.probe_trial import ProbeTrialService
    from app.services.template_rule import TemplateRuleService

    rows = ProbeTrialService().list_for_template(entry_id)
    return {
        "items": rows,
        "total": len(rows),
        "template": TemplateRuleService().pending_fields(entry_id),
    }


@router.post("/template_rule/{entry_id}/trial-runs", response_model=dict)
def template_start_trial(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.services.probe_trial import ProbeTrialService

    payload = payload or {}
    return ProbeTrialService().start_trial(
        entry_id,
        probe_id=payload.get("probe_id"),
        retry_of=payload.get("retry_of"),
        definition_hash_value=payload.get("definition_hash"),
        entry=str(payload.get("entry") or "模板规则"),
    )


@router.get("/probe_selector/{entry_id}/trial-readiness", response_model=dict)
def probe_trial_readiness(entry_id: int) -> dict[str, Any]:
    from app.services.probe_selector import ProbeSelectorService
    from app.services.template_rule import TemplateRuleService

    probe = store.find("probe_selector", entry_id)
    if probe is None:
        raise HTTPException(status_code=404, detail=f"探针 {entry_id} 不存在或已归档")
    page = (probe.get("目标页面") or "").strip()
    template = TemplateRuleService().resolve_trial_template(page)
    if template is None:
        return {"ok": False, "reason": f"页面 {page} 没有可试跑的模板"}
    return TemplateRuleService().trial_config(int(template.get("id")), entry_id)


@router.post("/probe_selector/{entry_id}/trial-runs", response_model=dict)
def probe_start_trial(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.services.probe_selector import ProbeSelectorService

    payload = payload or {}
    return ProbeSelectorService().run_trial(
        entry_id,
        template_id=payload.get("template_id"),
        retry_of=payload.get("retry_of"),
    )


@router.get("/probe_selector/{entry_id}/page-diagnostics", response_model=dict)
def probe_page_diagnostics(entry_id: int) -> dict[str, Any]:
    from app.services.probe_selector import ProbeSelectorService

    return ProbeSelectorService().page_diagnostics(entry_id)


@router.get("/probe_trial/{entry_id}/detail", response_model=dict)
def trial_detail(entry_id: int) -> dict[str, Any]:
    from app.services.probe_trial import ProbeTrialService

    return ProbeTrialService().trial_detail(entry_id)


@router.post("/probe_trial/{entry_id}/retry", response_model=dict)
def trial_retry(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.services.probe_trial import ProbeTrialService

    payload = payload or {}
    return ProbeTrialService().retry_trial(entry_id, payload.get("definition_hash"))


@router.get("/probe_trial/{entry_id}/comparison", response_model=dict)
def trial_comparison(entry_id: int) -> dict[str, Any]:
    from app.services.probe_trial import ProbeTrialService

    return ProbeTrialService().compare_rounds(entry_id)

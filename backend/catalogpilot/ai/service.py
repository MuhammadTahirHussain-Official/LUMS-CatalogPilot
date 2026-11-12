"""Proposals, policy and review (FR-25..32, AIR-07/16).

An AI result is stored as a proposal and never applied directly. Policy decides whether a
low-risk, high-confidence proposal on an allow-listed field is auto-approved; everything
else waits for a person. Applying a proposal goes through the catalog service, so it is
versioned, attributed (origin = ai) and reversible like any other edit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..catalog import service as catalog_service
from ..catalog.cleaning import clean_text, sanitize_html
from ..catalog.service import Change
from ..ids import uuid7
from ..models import AIProposal, Catalog, OrgPolicy, Product, ReviewDecision, UsageLedger
from ..security import audit
from . import confidence, gateway, tasks, taxonomy

POLICY_FIELD = {"title": "title", "description": "description_html", "attributes": "attributes",
                "category": "category", "seo": "seo"}


class ReviewError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status


def signals_for(task: str, inp: dict[str, Any], done: gateway.Completion, report: tasks.Report) -> dict[str, float]:
    signals = {"self_consistency": done.self_consistency, "validator_pass_ratio": report.pass_ratio,
               "edit_ratio": report.edit_ratio}
    if task == "category" and done.candidates:
        top = done.candidates[0][1]
        second = done.candidates[1][1] if len(done.candidates) > 1 else 0.0
        chosen = done.output.get("taxonomy_id")
        agrees = 1.0 if chosen == done.candidates[0][0].id else 0.0
        signals.update(retrieval_score=top * agrees, retrieval_margin=max(0.0, top - second) * agrees)
    if task == "title":
        # A title that arrives all upper- or lower-case has lost its casing information
        # (was it "iPhone", "USB", "McKay"?), so a re-cased result is less certain.
        signals["case_ambiguity"] = 1.0 if done.output.get("casing_recovered") else 0.0
    if task == "attributes":
        signals["grounded"] = 1.0 if report.checks.get("grounded") else 0.0
    return signals


def get_policy(db: Session, tenant_id: str) -> OrgPolicy:
    policy = db.execute(select(OrgPolicy)).scalar_one_or_none()
    if policy is None:
        policy = OrgPolicy(tenant_id=tenant_id)
        db.add(policy)
        db.flush()
    return policy


def _current_value(product: Product, task: str, locale: str) -> Any:
    if task == "title":
        return product.title.get(locale) or next(iter(product.title.values()), "")
    if task == "description":
        return product.description_html.get(locale) or next(iter(product.description_html.values()), "")
    if task == "attributes":
        return product.attributes
    if task == "seo":
        return product.seo or {}
    return product.mapped_taxonomy_id


def _proposed_value(task: str, product: Product, out: dict[str, Any]) -> Any:
    if task == "title":
        return clean_text(out["title"], 255)
    if task == "description":
        return sanitize_html(out["description_html"])  # model output is untrusted (SEC-AI-10)
    if task == "attributes":
        # Only add what is missing. A value the seller typed is never overwritten by an extraction.
        have = {a["key"].lower() for a in product.attributes}
        new = [{"key": clean_text(a["key"], 60), "value": clean_text(a["value"], 200), "source": "extracted"}
               for a in out["attributes"] if a["key"].lower() not in have]
        return [*product.attributes, *new] if new else product.attributes
    if task == "seo":
        # Only fill what is empty: search text the seller already wrote is never replaced.
        current = product.seo or {}
        return {**current, "title": current.get("title") or clean_text(out["seo_title"], 70),
                "description": current.get("description") or clean_text(out["seo_description"], 160)}
    return out["taxonomy_id"]


def enhance(db: Session, catalog: Catalog, user_id: str | None, task_names: list[str] | None = None,
            product_ids: list[str] | None = None, limit: int = 5000,
            preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = get_policy(db, catalog.tenant_id)
    wanted = [t for t in (task_names or list(tasks.ALL_TASKS)) if t in tasks.ALL_TASKS]
    prefs = tasks.clean_preferences(preferences)
    if not policy.ai_enabled:
        raise ReviewError("ai_disabled", "AI assistance is turned off for this organization.", 409)
    allowed = [t for t in wanted if POLICY_FIELD[t] in (policy.ai_field_allowlist or [])]
    summary = {"run_id": uuid7(), "products": 0, "proposals": 0, "auto_approved": 0, "pending": 0,
               "discarded": 0, "unchanged": 0, "skipped_tasks": sorted(set(wanted) - set(allowed)), "tokens": 0}
    if gateway.killed():
        summary["ai_paused"] = True
    stmt = select(Product).where(Product.catalog_id == catalog.id, Product.status != "archived").order_by(Product.id)
    if product_ids:
        stmt = stmt.where(Product.id.in_(product_ids[:1000]))
    locale = catalog.default_locale
    auto_changes: list[tuple[AIProposal, Change]] = []
    for product in db.execute(stmt.limit(limit)).scalars().all():
        summary["products"] += 1
        state = catalog_service.product_state(product)
        for task in allowed:
            inp = tasks.minimize(task, state, locale)
            if not inp["title"]:
                continue
            if task == "title":
                inp["preferences"] = prefs
            done = gateway.run_task(task, inp, external_allowed=policy.external_ai_allowed)
            summary["tokens"] += done.tokens
            report = tasks.validate_output(task, inp, done.output, done.candidates)
            if not report.passed:
                summary["discarded"] += 1  # guardrails rejected it: it never becomes a proposal
                continue
            current = _current_value(product, task, locale)
            proposed = _proposed_value(task, product, done.output)
            if proposed is None or proposed == current or not done.output.get("changed", True):
                summary["unchanged"] += 1
                continue
            signals = signals_for(task, inp, done, report)
            score, eligible = confidence.score(task, done.model, signals)
            db.execute(update(AIProposal).where(
                AIProposal.product_id == product.id, AIProposal.task == task, AIProposal.status == "pending"
            ).values(status="superseded"))
            rationale = str(done.output.get("rationale") or "")[:400]
            if task == "category":
                node = taxonomy.by_id(proposed)
                rationale = f"Suggested: {node.path}" if node else rationale
            proposal = AIProposal(
                tenant_id=catalog.tenant_id, product_id=product.id, run_id=summary["run_id"], task=task,
                field_path=tasks.FIELD_OF[task], original_value=current, proposed_value=proposed,
                rationale=rationale, confidence=score, signals=signals, risk=tasks.RISK_OF[task], model=done.model,
                prompt_version=tasks.PROMPT_VERSIONS[task], validator_report=report.as_dict(),
                product_version=product.version,
            )
            db.add(proposal)
            summary["proposals"] += 1
            auto = (policy.ai_mode in ("assisted", "automatic") and proposal.risk == "low" and eligible
                    and score >= policy.auto_approve_threshold)
            if auto:
                auto_changes.append((proposal, _change_for(product, proposal, proposed, locale)))
            else:
                summary["pending"] += 1
    db.flush()
    if auto_changes:
        # Several auto-approved fields of one product are merged into one change.
        merged: dict[str, Change] = {}
        for _proposal, change in auto_changes:
            if change.external_key in merged:
                merged[change.external_key].state.update(change.state)
            else:
                merged[change.external_key] = change
        catalog_service.apply_changes(db, catalog, list(merged.values()), "ai_apply", None)
        for proposal, _ in auto_changes:
            proposal.status = "auto_approved"
            db.add(ReviewDecision(tenant_id=catalog.tenant_id, proposal_id=proposal.id, decided_by=None,
                                  decision="approve", final_value=proposal.proposed_value, reason="policy"))
        summary["auto_approved"] = len(auto_changes)
    if summary["tokens"]:
        db.add(UsageLedger(tenant_id=catalog.tenant_id, metric="ai_tokens", quantity=summary["tokens"],
                           ref=summary["run_id"]))
    audit.record(db, catalog.tenant_id, "ai.enhance", f"catalog:{catalog.id}",
                 {k: summary[k] for k in ("products", "proposals", "auto_approved", "pending", "discarded")})
    db.flush()
    return summary


def _change_for(product: Product, proposal: AIProposal, value: Any, locale: str) -> Change:
    if proposal.task == "title":
        state = {"title": {**product.title, locale: value}}
    elif proposal.task == "description":
        state = {"description_html": {**product.description_html, locale: value}}
    elif proposal.task == "attributes":
        state = {"attributes": value}
    elif proposal.task == "seo":
        state = {"seo": value}
    else:
        state = {"mapped_taxonomy_id": value}
    return Change(product.external_key, state, origin="ai", ref=f"proposal:{proposal.id}", partial=True)


def _validate_edit(task: str, value: Any) -> Any:
    """A person's edit is trusted for meaning but still sanitized and shape-checked."""
    if task == "title":
        text = clean_text(value, 255) if isinstance(value, str) else ""
        if not text:
            raise ReviewError("validation_failed", "The title cannot be empty.")
        return text
    if task == "description":
        if not isinstance(value, str) or len(value) > 65_000:
            raise ReviewError("validation_failed", "The description is not valid.")
        return sanitize_html(value)
    if task == "attributes":
        ok = isinstance(value, list) and len(value) <= 200 and all(
            isinstance(a, dict) and isinstance(a.get("key"), str) and isinstance(a.get("value"), str) for a in value)
        if not ok:
            raise ReviewError("validation_failed", "Attributes must be a list of key/value pairs.")
        return [{"key": clean_text(a["key"], 60), "value": clean_text(a["value"], 200),
                 "source": a.get("source", "explicit")} for a in value if clean_text(a["key"], 60)]
    if task == "seo":
        if not isinstance(value, dict):
            raise ReviewError("validation_failed", "Search text is not valid.")
        return {"title": clean_text(value.get("title"), 70), "description": clean_text(value.get("description"), 160)}
    if value is not None and taxonomy.by_id(str(value)) is None:
        raise ReviewError("validation_failed", "That category does not exist.")
    return value


def decide(db: Session, tenant_id: str, user_id: str, proposal_ids: list[str], decision: str,
           edited_value: Any = None, reason: str | None = None, step_up_fresh: bool = False) -> dict[str, Any]:
    if decision not in ("approve", "reject", "edit"):
        raise ReviewError("validation_failed", "decision must be approve, reject or edit.")
    if not proposal_ids or len(proposal_ids) > 1000:
        raise ReviewError("validation_failed", "Send between 1 and 1,000 proposal ids.")
    if decision == "edit" and len(proposal_ids) != 1:
        raise ReviewError("validation_failed", "Edit one proposal at a time.")
    proposals = db.execute(select(AIProposal).where(AIProposal.id.in_(proposal_ids))).scalars().all()
    result = {"applied": 0, "rejected": 0, "superseded": 0, "already_decided": 0,
              "not_found": len(set(proposal_ids)) - len(proposals)}
    by_catalog: dict[str, list[tuple[AIProposal, Change, Any]]] = {}
    for proposal in proposals:
        if proposal.status != "pending":
            result["already_decided"] += 1  # idempotent: deciding twice changes nothing
            continue
        if decision == "reject":
            proposal.status = "rejected"
            db.add(ReviewDecision(tenant_id=tenant_id, proposal_id=proposal.id, decided_by=user_id,
                                  decision="reject", reason=(reason or "")[:400] or None))
            result["rejected"] += 1
            continue
        if proposal.risk == "high" and not step_up_fresh:
            from ..security.authz import Forbidden
            raise Forbidden("step_up_required", "Please re-authenticate to approve high-risk changes.")
        product = catalog_service.get_product(db, proposal.product_id)
        catalog = catalog_service.get_catalog(db, product.catalog_id)
        current = _current_value(product, proposal.task, catalog.default_locale)
        if current != proposal.original_value:
            # The field changed after the proposal was made; applying it would overwrite newer work.
            proposal.status = "superseded"
            result["superseded"] += 1
            continue
        value = _validate_edit(proposal.task, edited_value) if decision == "edit" else proposal.proposed_value
        by_catalog.setdefault(catalog.id, []).append(
            (proposal, _change_for(product, proposal, value, catalog.default_locale), value))
    for catalog_id, entries in by_catalog.items():
        catalog = catalog_service.get_catalog(db, catalog_id)
        merged: dict[str, Change] = {}
        for _p, change, _v in entries:
            if change.external_key in merged:
                merged[change.external_key].state.update(change.state)
            else:
                merged[change.external_key] = change
        catalog_service.apply_changes(db, catalog, list(merged.values()), "ai_apply", user_id)
        for proposal, _change, value in entries:
            proposal.status = "edited" if decision == "edit" else "approved"
            db.add(ReviewDecision(tenant_id=tenant_id, proposal_id=proposal.id, decided_by=user_id,
                                  decision=decision, final_value=value, reason=(reason or "")[:400] or None))
            result["applied"] += 1
    audit.record(db, tenant_id, f"review.{decision}", "proposals", result)
    db.flush()
    return result


def proposal_view(p: AIProposal) -> dict[str, Any]:
    out = {
        "id": p.id, "product_id": p.product_id, "task": p.task, "field": p.field_path,
        "original": p.original_value, "proposed": p.proposed_value, "rationale": p.rationale,
        "confidence": p.confidence, "risk": p.risk, "status": p.status, "model": p.model,
        "prompt_version": p.prompt_version, "created_at": p.created_at.isoformat(),
    }
    if p.task == "category":
        for key in ("original", "proposed"):
            node = taxonomy.by_id(out[key]) if out[key] else None
            out[f"{key}_label"] = node.path if node else None
    return out


def pending_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(AIProposal.task, func.count()).where(AIProposal.status == "pending")
                      .group_by(AIProposal.task)).all()
    return {task: n for task, n in rows}

"""
Phase 3 Route Constructor - ChatGPT LLM Advisor

Provides aggressive ChatGPT API reasoning for hard constructor cases:
  - Missing route interpretation
  - Duplicate risk assessment
  - Draft improvement suggestions
  - Classification explanations

Modes:
  real_advisory  - calls OpenAI Responses API with structured JSON output
  mock           - deterministic heuristic responses (same output shape)
  dry_run        - logs evidence payload, returns skeleton result
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

CONSTRUCTOR_SYSTEM_PROMPT = """\
You are a senior transit route data analyst working on the DataMind route \
constructor pipeline. Your job is to provide rigorous, conservative \
reasoning about route data quality decisions.

Domain context:
- The pipeline constructs formal transit route records from OpenStreetMap \
relation evidence, stop sequences, and geometry candidates.
- Each route has: name_hint, operator_hint, variant_hint, sector_key, \
stop_count, geometry, and an approval status.
- Routes progress through phases: extraction -> sequence building -> \
geometry construction -> approval -> production.
- Duplicate routes are common because the same physical service can appear \
under slightly different names, operators, or variant labels.

Your responsibilities:
1. INTERPRET MISSING ROUTES: determine whether a gap in coverage is a true \
missing route or already represented under a different name/variant.
2. ASSESS DUPLICATE RISK: compare candidates against the existing catalog \
and flag likely duplicates or opposite-direction variants.
3. SUGGEST IMPROVEMENTS: recommend better hints, flag missing context, \
and identify data quality issues in draft records.
4. EXPLAIN CLASSIFICATIONS: provide clear reasoning for preflight \
classification decisions.

Rules you MUST follow:
- Be conservative. When uncertain, flag for human review rather than \
asserting a conclusion.
- Never overwrite ground truth. Your output is advisory.
- Provide confidence scores between 0.0 and 1.0 that honestly reflect \
your certainty.
- Always include reasoning that traces back to specific evidence fields.
- Flag any evidence gaps that limit your confidence.
- Use the structured JSON output schema exactly as specified.
"""

# ---------------------------------------------------------------------------
# JSON Schemas for structured output (OpenAI Responses API)
# ---------------------------------------------------------------------------

_INTERPRET_MISSING_ROUTE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_truly_missing": {
            "type": "boolean",
            "description": "Whether this route is genuinely absent from the catalog.",
        },
        "already_represented_by": {
            "type": ["string", "null"],
            "description": "If not truly missing, the route_id or name of the existing match.",
        },
        "recommended_action": {
            "type": "string",
            "enum": [
                "create_draft",
                "hold_review",
                "reject_duplicate",
                "merge_with_existing",
                "needs_more_evidence",
            ],
            "description": "Recommended next action.",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in this assessment, 0.0 to 1.0.",
        },
        "duplicate_risk": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
            "description": "Risk that this is a duplicate of an existing route.",
        },
        "hint_improvements": {
            "type": "object",
            "properties": {
                "name_hint": {"type": ["string", "null"]},
                "operator_hint": {"type": ["string", "null"]},
                "variant_hint": {"type": ["string", "null"]},
            },
            "required": ["name_hint", "operator_hint", "variant_hint"],
            "additionalProperties": False,
        },
        "reasoning": {
            "type": "string",
            "description": "Step-by-step reasoning for the assessment.",
        },
        "evidence_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evidence fields that are missing or weak.",
        },
    },
    "required": [
        "is_truly_missing",
        "already_represented_by",
        "recommended_action",
        "confidence",
        "duplicate_risk",
        "hint_improvements",
        "reasoning",
        "evidence_gaps",
    ],
    "additionalProperties": False,
}

_ASSESS_DUPLICATE_RISK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_duplicate": {
            "type": "boolean",
            "description": "Whether the candidate is a duplicate of an existing route.",
        },
        "is_opposite_direction": {
            "type": "boolean",
            "description": "Whether the candidate is the opposite direction of an existing route.",
        },
        "is_variant": {
            "type": "boolean",
            "description": "Whether the candidate is a service variant of an existing route.",
        },
        "best_match_route_id": {
            "type": ["string", "null"],
            "description": "The route_id of the closest existing match, if any.",
        },
        "best_match_name": {
            "type": ["string", "null"],
            "description": "The name of the closest existing match.",
        },
        "similarity_score": {
            "type": "number",
            "description": "How similar the candidate is to the best match, 0.0 to 1.0.",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in this assessment, 0.0 to 1.0.",
        },
        "recommended_action": {
            "type": "string",
            "enum": [
                "proceed_unique",
                "flag_likely_duplicate",
                "flag_opposite_direction",
                "flag_variant",
                "hold_for_review",
            ],
        },
        "reasoning": {
            "type": "string",
            "description": "Step-by-step reasoning for the assessment.",
        },
        "distinguishing_features": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Features that distinguish or link the candidate to existing routes.",
        },
    },
    "required": [
        "is_duplicate",
        "is_opposite_direction",
        "is_variant",
        "best_match_route_id",
        "best_match_name",
        "similarity_score",
        "confidence",
        "recommended_action",
        "reasoning",
        "distinguishing_features",
    ],
    "additionalProperties": False,
}

_SUGGEST_IMPROVEMENTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "quality_rating": {
            "type": "string",
            "enum": ["good", "fair", "poor", "critical"],
            "description": "Overall quality rating of the draft.",
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error"],
                    },
                    "message": {"type": "string"},
                    "suggested_value": {"type": ["string", "null"]},
                },
                "required": ["field", "severity", "message", "suggested_value"],
                "additionalProperties": False,
            },
            "description": "List of issues found in the draft.",
        },
        "missing_context": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Context that is missing and should be provided.",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in these suggestions, 0.0 to 1.0.",
        },
        "reasoning": {
            "type": "string",
            "description": "Overall assessment reasoning.",
        },
    },
    "required": [
        "quality_rating",
        "issues",
        "missing_context",
        "confidence",
        "reasoning",
    ],
    "additionalProperties": False,
}

_EXPLAIN_CLASSIFICATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification_valid": {
            "type": "boolean",
            "description": "Whether the given classification appears correct.",
        },
        "alternative_classification": {
            "type": ["string", "null"],
            "description": "A better classification if the current one seems wrong.",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in this explanation, 0.0 to 1.0.",
        },
        "key_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The most important evidence fields supporting the explanation.",
        },
        "reasoning": {
            "type": "string",
            "description": "Detailed explanation of why the classification was assigned.",
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any risks or caveats about this classification.",
        },
    },
    "required": [
        "classification_valid",
        "alternative_classification",
        "confidence",
        "key_evidence",
        "reasoning",
        "risk_flags",
    ],
    "additionalProperties": False,
}

_INTERPRET_SEQUENCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "interpreted_route_summary": {
            "type": "string",
            "description": "One-paragraph summary of the likely route based on all evidence.",
        },
        "suggested_anchor_stops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "role": {"type": "string", "enum": ["start", "end", "major_intermediate", "terminal"]},
                    "locality": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["description", "role", "locality", "confidence"],
                "additionalProperties": False,
            },
            "description": "Key anchor points the route likely passes through, in likely order.",
        },
        "suggested_intermediate_zones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "zone_name": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["zone_name", "reason", "confidence"],
                "additionalProperties": False,
            },
            "description": "Intermediate zones/corridors the route likely passes through.",
        },
        "suggested_sequence_strategy": {
            "type": "string",
            "enum": [
                "linear_start_to_end",
                "loop_circuit",
                "hub_and_spoke",
                "corridor_follow",
                "locality_chain",
                "uncertain_needs_stops",
            ],
            "description": "The most likely stop-ordering strategy for this route.",
        },
        "improved_name_hint": {"type": ["string", "null"]},
        "improved_operator_hint": {"type": ["string", "null"]},
        "improved_variant_hint": {"type": ["string", "null"]},
        "duplicate_risk": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
        },
        "variant_risk": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
        },
        "confidence": {
            "type": "number",
            "description": "Overall confidence in the sequence interpretation, 0.0 to 1.0.",
        },
        "reasoning": {
            "type": "string",
            "description": "Step-by-step reasoning for the sequence interpretation.",
        },
        "evidence_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Missing evidence that would improve the sequence interpretation.",
        },
        "recommended_next_action": {
            "type": "string",
            "enum": [
                "ready_to_build_sequence",
                "need_more_anchor_stops",
                "need_locality_search",
                "need_operator_confirmation",
                "hold_for_review",
                "reject_insufficient",
            ],
        },
    },
    "required": [
        "interpreted_route_summary",
        "suggested_anchor_stops",
        "suggested_intermediate_zones",
        "suggested_sequence_strategy",
        "improved_name_hint",
        "improved_operator_hint",
        "improved_variant_hint",
        "duplicate_risk",
        "variant_risk",
        "confidence",
        "reasoning",
        "evidence_gaps",
        "recommended_next_action",
    ],
    "additionalProperties": False,
}

_SEQUENCE_DISCOVERY_ADVISORY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "sequence_assessment": {
            "type": "string",
            "description": "Overall assessment of the grounded stop sequence.",
        },
        "express_skip_analysis": {
            "type": "string",
            "description": "Analysis of express/skip patterns in the sequence.",
        },
        "gap_analysis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap_index": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "note": {"type": "string"},
                },
                "required": ["gap_index", "severity", "note"],
                "additionalProperties": False,
            },
            "description": "Analysis of gaps in the sequence.",
        },
        "branch_variant_risk": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
            "description": "Risk that this is a branch or variant of another route.",
        },
        "geographically_implausible_corridor": {
            "type": "boolean",
            "description": "Whether the corridor is geographically implausible for the route family.",
        },
        "failure_reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Reasons the sequence or corridor should be rejected.",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in the overall assessment, 0.0 to 1.0.",
        },
        "recommended_action": {
            "type": "string",
            "enum": ["approve", "review", "block_geography", "reject", "needs_more_evidence"],
            "description": "Recommended next action.",
        },
        "recommended_operator_actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific actions the operator should take.",
        },
        "reasoning_summary": {
            "type": "string",
            "description": "Summary of the reasoning behind the assessment.",
        },
    },
    "required": [
        "sequence_assessment",
        "express_skip_analysis",
        "gap_analysis",
        "branch_variant_risk",
        "geographically_implausible_corridor",
        "failure_reasons",
        "confidence",
        "recommended_action",
        "recommended_operator_actions",
        "reasoning_summary",
    ],
    "additionalProperties": False,
}

_TASK_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "interpret_missing_route": _INTERPRET_MISSING_ROUTE_SCHEMA,
    "assess_duplicate_risk": _ASSESS_DUPLICATE_RISK_SCHEMA,
    "suggest_improvements": _SUGGEST_IMPROVEMENTS_SCHEMA,
    "explain_classification": _EXPLAIN_CLASSIFICATION_SCHEMA,
    "interpret_sequence": _INTERPRET_SEQUENCE_SCHEMA,
    "sequence_discovery_advisory": _SEQUENCE_DISCOVERY_ADVISORY_SCHEMA,
}

# ---------------------------------------------------------------------------
# User prompt templates
# ---------------------------------------------------------------------------

SEQUENCE_DISCOVERY_SYSTEM_PROMPT = CONSTRUCTOR_SYSTEM_PROMPT + """\

You are now evaluating grounded sequence discovery evidence for transit routes \
in Quito / Valle de los Chillos, Ecuador. All data comes from real DB stop \
matches and Valhalla routing — not from text interpretation.

Focus on:
- Geographic plausibility: does the corridor stay within the expected envelope?
- Gap severity: are gaps likely express segments or missing coverage?
- Operator review priorities: what should be checked first?

Valle de los Chillos rules:
- Local feeders must stay geographically within the valley.
- Valle routes may only leave the valley when the seed explicitly names a real \
connector like La Marín, San Francisco, San Roque, Cumandá, or Quitumbe.
- If the corridor is wildly too long, leaves the expected envelope, or inflates \
far beyond the anchor span, treat that as a first-class failure.
"""

# Task-specific system prompts (override CONSTRUCTOR_SYSTEM_PROMPT for certain tasks)
_TASK_SYSTEM_PROMPTS: Dict[str, str] = {
    "sequence_discovery_advisory": SEQUENCE_DISCOVERY_SYSTEM_PROMPT,
}

_INTERPRET_MISSING_ROUTE_PROMPT = """\
Analyze the following evidence for a potentially missing transit route and \
determine whether it is truly missing from the catalog or already \
represented under a different name/variant.

## Evidence

Name hint: {name_hint}
Operator hint: {operator_hint}
Variant hint: {variant_hint}
Sector: {sector_key} ({sector_label})
Stop count: {stop_count}
Approximate bbox: {approximate_bbox}

## Gap context
{gap_context_json}

## Existing catalog routes in this sector (top matches)
{existing_routes_json}

Provide your assessment using the required JSON schema.
"""

_ASSESS_DUPLICATE_RISK_PROMPT = """\
Compare the following route candidate against the existing catalog routes \
and assess whether it is a duplicate, opposite-direction variant, or \
service variant.

## Candidate
{candidate_json}

## Existing catalog routes to compare against
{existing_routes_json}

Provide your assessment using the required JSON schema.
"""

_SUGGEST_IMPROVEMENTS_PROMPT = """\
Review the following draft route record and suggest improvements. \
Look for: missing or weak hints, data quality issues, naming \
inconsistencies, and missing context.

## Draft record
{draft_json}

Provide your suggestions using the required JSON schema.
"""

_EXPLAIN_CLASSIFICATION_PROMPT = """\
Explain why the following route case received the classification shown. \
Assess whether the classification is correct and flag any risks.

## Case evidence
{case_json}

## Assigned classification
{classification}

Provide your explanation using the required JSON schema.
"""

_INTERPRET_SEQUENCE_PROMPT = """\
You are interpreting a transit route to discover its likely stop sequence. \
Use ALL available evidence to infer where this route goes, what stops it \
likely passes through, and what the best stop-ordering strategy would be.

## Route identity
Name hint: {name_hint}
Operator hint: {operator_hint}
Variant hint: {variant_hint}
Sector: {sector_key}

## Gap context (coverage gap that triggered this work)
Start hint: {gap_start_hint}
End hint: {gap_end_hint}
Direction hint: {gap_direction_hint}

## Sequence evidence
Anchor stop IDs (confirmed stops): {anchor_count} stops
Intermediate stop hints: {intermediate_hints_json}
Corridor hints: {corridor_hints_json}
Locality clues: {locality_clues_json}
Must-pass-through areas: {must_pass_through_json}
Total ordered stops so far: {total_ordered_stops}
Sequence notes: {sequence_notes}

## Existing catalog routes in this area (for duplicate/variant check)
{existing_routes_json}

Based on this evidence, provide:
1. A summary of the likely route
2. Suggested anchor stops (key points in likely order)
3. Suggested intermediate zones the route passes through
4. The best sequence-building strategy
5. Improved name/operator/variant hints if the current ones are weak
6. Duplicate and variant risk assessment
7. What evidence is still missing
8. Recommended next action for building the sequence

Provide your interpretation using the required JSON schema.
"""


# ---------------------------------------------------------------------------
# Helper: safe JSON serialization
# ---------------------------------------------------------------------------

def _safe_json(obj: Any, indent: int = 2) -> str:
    """JSON-serialize with fallback for non-serializable objects."""
    try:
        return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)
    except Exception:
        return json.dumps(str(obj))


def _timestamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_confidence(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(f, 4)))


# ---------------------------------------------------------------------------
# Mock heuristics
# ---------------------------------------------------------------------------

def _mock_interpret_missing_route(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic heuristic interpretation for mock/test mode."""
    name_hint = str(evidence.get("name_hint") or "").strip().lower()
    existing = list(evidence.get("existing_catalog_routes") or [])

    # Simple name overlap check
    duplicate_candidate = None
    for route in existing:
        existing_name = str(route.get("name_hint") or route.get("route_name") or "").strip().lower()
        if not existing_name or not name_hint:
            continue
        # Exact or near match
        if name_hint == existing_name or name_hint in existing_name or existing_name in name_hint:
            duplicate_candidate = route
            break

    if duplicate_candidate:
        return {
            "is_truly_missing": False,
            "already_represented_by": str(
                duplicate_candidate.get("route_id")
                or duplicate_candidate.get("route_name")
                or "existing_match"
            ),
            "recommended_action": "reject_duplicate",
            "confidence": 0.72,
            "duplicate_risk": "high",
            "hint_improvements": {
                "name_hint": None,
                "operator_hint": None,
                "variant_hint": None,
            },
            "reasoning": (
                f"Name hint '{name_hint}' has strong overlap with existing catalog route "
                f"'{duplicate_candidate.get('name_hint') or duplicate_candidate.get('route_name')}'. "
                f"Mock heuristic flags as likely duplicate. Operator should verify."
            ),
            "evidence_gaps": [],
        }

    stop_count = 0
    try:
        stop_count = int(evidence.get("stop_count") or 0)
    except (TypeError, ValueError):
        pass

    evidence_gaps = []
    if not name_hint:
        evidence_gaps.append("name_hint is empty")
    if not str(evidence.get("operator_hint") or "").strip():
        evidence_gaps.append("operator_hint is empty")
    if stop_count == 0:
        evidence_gaps.append("stop_count is zero or missing")
    if not evidence.get("approximate_bbox"):
        evidence_gaps.append("approximate_bbox is missing")

    if len(evidence_gaps) >= 3:
        return {
            "is_truly_missing": False,
            "already_represented_by": None,
            "recommended_action": "needs_more_evidence",
            "confidence": 0.35,
            "duplicate_risk": "low",
            "hint_improvements": {
                "name_hint": "Provide a route name or number" if not name_hint else None,
                "operator_hint": "Provide the operator/cooperative name" if not str(evidence.get("operator_hint") or "").strip() else None,
                "variant_hint": None,
            },
            "reasoning": (
                f"Evidence is too sparse ({len(evidence_gaps)} gaps) to make a confident "
                f"determination. Recommend collecting more evidence before creating a draft."
            ),
            "evidence_gaps": evidence_gaps,
        }

    return {
        "is_truly_missing": True,
        "already_represented_by": None,
        "recommended_action": "create_draft",
        "confidence": 0.60,
        "duplicate_risk": "low" if not existing else "medium",
        "hint_improvements": {
            "name_hint": None,
            "operator_hint": None,
            "variant_hint": None,
        },
        "reasoning": (
            f"No strong overlap found with {len(existing)} existing routes in sector. "
            f"Evidence is {'adequate' if not evidence_gaps else 'partial'}. "
            f"Mock heuristic recommends creating a draft for operator review."
        ),
        "evidence_gaps": evidence_gaps,
    }


def _mock_assess_duplicate_risk(
    candidate: Dict[str, Any],
    existing_routes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Deterministic duplicate risk heuristic for mock mode."""
    cand_name = str(candidate.get("name_hint") or candidate.get("route_name") or "").strip().lower()
    cand_operator = str(candidate.get("operator_hint") or "").strip().lower()

    best_match = None
    best_score = 0.0

    for route in existing_routes:
        r_name = str(route.get("name_hint") or route.get("route_name") or "").strip().lower()
        r_operator = str(route.get("operator_hint") or route.get("operator") or "").strip().lower()
        score = 0.0

        if cand_name and r_name:
            if cand_name == r_name:
                score += 0.6
            elif cand_name in r_name or r_name in cand_name:
                score += 0.35
        if cand_operator and r_operator:
            if cand_operator == r_operator:
                score += 0.25
            elif cand_operator in r_operator or r_operator in cand_operator:
                score += 0.12

        # Check for direction indicators
        direction_words = {"ida", "retorno", "vuelta", "regreso", "norte", "sur", "este", "oeste"}
        cand_tokens = set(cand_name.split())
        r_tokens = set(r_name.split())
        direction_diff = (cand_tokens & direction_words) != (r_tokens & direction_words)
        if direction_diff and score >= 0.35:
            score += 0.15  # boost: likely opposite direction

        if score > best_score:
            best_score = score
            best_match = route

    is_duplicate = best_score >= 0.7
    is_opposite = False
    is_variant = False

    if best_match and 0.45 <= best_score < 0.7:
        best_name = str(best_match.get("name_hint") or best_match.get("route_name") or "").strip().lower()
        cand_tokens = set(cand_name.split())
        best_tokens = set(best_name.split())
        direction_words_found = {"ida", "retorno", "vuelta", "regreso"} & (cand_tokens | best_tokens)
        if direction_words_found:
            is_opposite = True
        else:
            is_variant = True

    if is_duplicate:
        action = "flag_likely_duplicate"
    elif is_opposite:
        action = "flag_opposite_direction"
    elif is_variant:
        action = "flag_variant"
    elif best_score >= 0.3:
        action = "hold_for_review"
    else:
        action = "proceed_unique"

    return {
        "is_duplicate": is_duplicate,
        "is_opposite_direction": is_opposite,
        "is_variant": is_variant,
        "best_match_route_id": str(best_match.get("route_id") or "") if best_match else None,
        "best_match_name": str(
            best_match.get("name_hint") or best_match.get("route_name") or ""
        ) if best_match else None,
        "similarity_score": round(best_score, 4),
        "confidence": _clamp_confidence(0.5 + best_score * 0.35),
        "recommended_action": action,
        "reasoning": (
            f"Compared candidate against {len(existing_routes)} existing routes. "
            f"Best similarity score: {best_score:.2f}"
            + (f" with route '{best_match.get('name_hint') or best_match.get('route_name')}'." if best_match else ".")
            + f" Mock heuristic classification: {action}."
        ),
        "distinguishing_features": (
            [f"name_overlap={best_score:.2f}"]
            + (["direction_indicators_differ"] if is_opposite else [])
            + (["same_operator"] if best_match and cand_operator and cand_operator == str(best_match.get("operator_hint") or best_match.get("operator") or "").strip().lower() else [])
        ),
    }


def _mock_suggest_improvements(draft: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic improvement suggestions for mock mode."""
    issues: List[Dict[str, Any]] = []
    missing_context: List[str] = []

    name_hint = str(draft.get("name_hint") or "").strip()
    operator_hint = str(draft.get("operator_hint") or "").strip()
    variant_hint = str(draft.get("variant_hint") or "").strip()
    sector_key = str(draft.get("sector_key") or "").strip()
    stop_count = 0
    try:
        stop_count = int(draft.get("stop_count") or 0)
    except (TypeError, ValueError):
        pass

    if not name_hint:
        issues.append({
            "field": "name_hint",
            "severity": "error",
            "message": "Route name hint is empty. Every route must have an identifiable name.",
            "suggested_value": None,
        })
    elif len(name_hint) < 3:
        issues.append({
            "field": "name_hint",
            "severity": "warning",
            "message": f"Name hint '{name_hint}' is very short and may be ambiguous.",
            "suggested_value": None,
        })

    if not operator_hint:
        issues.append({
            "field": "operator_hint",
            "severity": "warning",
            "message": "Operator hint is empty. Routes should have an operator for disambiguation.",
            "suggested_value": None,
        })

    if not sector_key:
        issues.append({
            "field": "sector_key",
            "severity": "warning",
            "message": "Sector key is unassigned. Route cannot be placed in the geographic catalog.",
            "suggested_value": None,
        })
        missing_context.append("sector assignment")

    if stop_count == 0:
        issues.append({
            "field": "stop_count",
            "severity": "error",
            "message": "Stop count is zero. Route has no stops and cannot be constructed.",
            "suggested_value": None,
        })
        missing_context.append("stop sequence data")
    elif stop_count < 3:
        issues.append({
            "field": "stop_count",
            "severity": "warning",
            "message": f"Stop count is {stop_count}, which is unusually low for a transit route.",
            "suggested_value": None,
        })

    if not draft.get("approximate_bbox"):
        missing_context.append("geographic bounding box")

    if not variant_hint and name_hint:
        # Check if name suggests a variant
        for marker in ["ida", "retorno", "vuelta", "express", "directo", "alterno"]:
            if marker in name_hint.lower():
                issues.append({
                    "field": "variant_hint",
                    "severity": "info",
                    "message": f"Name contains '{marker}' which suggests this is a variant. Consider setting variant_hint.",
                    "suggested_value": marker,
                })
                break

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")

    if error_count >= 2:
        quality = "critical"
    elif error_count >= 1:
        quality = "poor"
    elif warning_count >= 2:
        quality = "fair"
    else:
        quality = "good"

    return {
        "quality_rating": quality,
        "issues": issues,
        "missing_context": missing_context,
        "confidence": _clamp_confidence(0.65 if issues else 0.80),
        "reasoning": (
            f"Draft has {len(issues)} issue(s) ({error_count} error, {warning_count} warning). "
            f"Quality rating: {quality}. "
            + (f"Missing context: {', '.join(missing_context)}. " if missing_context else "")
            + "Mock heuristic analysis."
        ),
    }


def _mock_explain_classification(
    case: Dict[str, Any],
    classification: str,
) -> Dict[str, Any]:
    """Deterministic classification explanation for mock mode."""
    classification = str(classification or "").strip()

    known_classifications = {
        "active_extracted", "active_manual_prod", "extraction_failed",
        "likely_duplicate", "opposite_direction", "needs_review",
        "rejected", "trashed",
    }

    valid = classification.lower() in known_classifications
    evidence_fields = []
    risk_flags = []

    if case.get("name_hint"):
        evidence_fields.append(f"name_hint='{case['name_hint']}'")
    if case.get("operator_hint"):
        evidence_fields.append(f"operator_hint='{case['operator_hint']}'")
    if case.get("sector_key"):
        evidence_fields.append(f"sector_key='{case['sector_key']}'")
    if case.get("canonical_state"):
        evidence_fields.append(f"canonical_state='{case['canonical_state']}'")
    if case.get("stop_count"):
        evidence_fields.append(f"stop_count={case['stop_count']}")

    if classification.lower() in ("likely_duplicate", "opposite_direction"):
        risk_flags.append("Duplicate/direction classification requires manual verification")
    if not evidence_fields:
        risk_flags.append("Very sparse evidence; classification may be unreliable")

    return {
        "classification_valid": valid,
        "alternative_classification": None if valid else "needs_review",
        "confidence": _clamp_confidence(0.55 if valid else 0.30),
        "key_evidence": evidence_fields[:5],
        "reasoning": (
            f"Classification '{classification}' "
            + ("is recognized and consistent with available evidence. " if valid else "is not a recognized classification value. ")
            + f"Based on {len(evidence_fields)} evidence fields. "
            + "Mock heuristic explanation."
        ),
        "risk_flags": risk_flags,
    }


def _mock_interpret_sequence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic sequence interpretation heuristic for mock/test mode."""
    name_hint = str(evidence.get("name_hint") or "").strip()
    operator_hint = str(evidence.get("operator_hint") or "").strip()
    variant_hint = str(evidence.get("variant_hint") or "").strip()
    sector_key = str(evidence.get("sector_key") or "").strip()
    anchor_count = int(evidence.get("anchor_count") or 0)
    intermediate_hints = list(evidence.get("intermediate_stop_hints") or [])
    corridor_hints = list(evidence.get("corridor_hints") or [])
    locality_clues = list(evidence.get("locality_clues") or [])
    must_pass_through = list(evidence.get("must_pass_through") or [])
    total_ordered = int(evidence.get("total_ordered_stops") or 0)
    existing_routes = list(evidence.get("existing_catalog_routes") or [])

    # --- Build suggested anchor stops from name parsing ---
    suggested_anchors: List[Dict[str, Any]] = []
    if name_hint:
        # Try to extract start/end from "A - B" pattern
        for sep in [" - ", " – ", " — ", " a ", " hacia "]:
            if sep in name_hint:
                parts = [p.strip() for p in name_hint.split(sep, 1)]
                if len(parts) == 2 and all(parts):
                    suggested_anchors.append({
                        "description": parts[0],
                        "role": "start",
                        "locality": sector_key or None,
                        "confidence": 0.65,
                    })
                    suggested_anchors.append({
                        "description": parts[1],
                        "role": "end",
                        "locality": sector_key or None,
                        "confidence": 0.65,
                    })
                break

    # Add must-pass-through as major_intermediate
    for area in must_pass_through:
        suggested_anchors.append({
            "description": str(area),
            "role": "major_intermediate",
            "locality": sector_key or None,
            "confidence": 0.50,
        })

    # --- Suggested intermediate zones from corridor + locality ---
    suggested_zones: List[Dict[str, Any]] = []
    for corridor in corridor_hints:
        suggested_zones.append({
            "zone_name": str(corridor),
            "reason": "corridor_hint",
            "confidence": 0.55,
        })
    for loc in locality_clues:
        suggested_zones.append({
            "zone_name": str(loc),
            "reason": "locality_clue",
            "confidence": 0.45,
        })

    # --- Determine strategy ---
    is_loop = any(
        kw in name_hint.lower()
        for kw in ["circular", "loop", "circuito", "anillo"]
    )
    if is_loop:
        strategy = "loop_circuit"
    elif corridor_hints:
        strategy = "corridor_follow"
    elif locality_clues and len(locality_clues) >= 2:
        strategy = "locality_chain"
    elif suggested_anchors and len(suggested_anchors) >= 2:
        strategy = "linear_start_to_end"
    else:
        strategy = "uncertain_needs_stops"

    # --- Duplicate risk ---
    dup_risk = "none"
    var_risk = "none"
    name_lower = name_hint.lower()
    for route in existing_routes:
        r_name = str(
            route.get("name_hint") or route.get("route_name") or ""
        ).strip().lower()
        if r_name and name_lower and (name_lower in r_name or r_name in name_lower):
            dup_risk = "medium"
            break
        if r_name and name_lower:
            # Check variant
            direction_words = {"ida", "retorno", "vuelta", "regreso"}
            c_tokens = set(name_lower.split())
            r_tokens = set(r_name.split())
            base_c = c_tokens - direction_words
            base_r = r_tokens - direction_words
            if base_c == base_r and c_tokens != r_tokens:
                var_risk = "medium"

    # --- Evidence gaps ---
    evidence_gaps: List[str] = []
    if not name_hint:
        evidence_gaps.append("name_hint is empty - cannot parse route endpoints")
    if anchor_count == 0:
        evidence_gaps.append("no confirmed anchor stops")
    if not corridor_hints and not locality_clues:
        evidence_gaps.append("no corridor or locality clues to guide intermediate ordering")
    if not operator_hint:
        evidence_gaps.append("no operator hint for disambiguation")
    if total_ordered == 0:
        evidence_gaps.append("no ordered stops yet")

    # --- Confidence ---
    conf = 0.3
    if suggested_anchors:
        conf += 0.15
    if anchor_count >= 2:
        conf += 0.15
    if intermediate_hints:
        conf += 0.10
    if corridor_hints:
        conf += 0.10
    if locality_clues:
        conf += 0.05
    conf = min(conf, 0.85)

    # --- Next action ---
    if conf >= 0.6 and anchor_count >= 2:
        next_action = "ready_to_build_sequence"
    elif suggested_anchors and not anchor_count:
        next_action = "need_more_anchor_stops"
    elif not locality_clues and not corridor_hints:
        next_action = "need_locality_search"
    elif conf < 0.35:
        next_action = "reject_insufficient"
    else:
        next_action = "hold_for_review"

    return {
        "interpreted_route_summary": (
            f"Route '{name_hint or '(unnamed)'}'"
            + (f" operated by {operator_hint}" if operator_hint else "")
            + (f" ({variant_hint})" if variant_hint else "")
            + f" in sector '{sector_key or 'unassigned'}'."
            + f" Strategy: {strategy}."
            + f" {len(suggested_anchors)} anchor suggestions, {len(suggested_zones)} zone suggestions."
            + " Mock heuristic interpretation."
        ),
        "suggested_anchor_stops": suggested_anchors,
        "suggested_intermediate_zones": suggested_zones,
        "suggested_sequence_strategy": strategy,
        "improved_name_hint": None,
        "improved_operator_hint": None,
        "improved_variant_hint": variant_hint or ("ida" if not variant_hint and suggested_anchors else None),
        "duplicate_risk": dup_risk,
        "variant_risk": var_risk,
        "confidence": round(conf, 4),
        "reasoning": (
            f"Mock heuristic parsed route name for start/end anchors, "
            f"used {len(corridor_hints)} corridor hints and {len(locality_clues)} locality clues "
            f"for intermediate zones. "
            f"Anchor count: {anchor_count}, total ordered: {total_ordered}. "
            f"Evidence gaps: {len(evidence_gaps)}."
        ),
        "evidence_gaps": evidence_gaps,
        "recommended_next_action": next_action,
    }


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------

class ConstructorLLMAdvisor:
    """ChatGPT-powered reasoning for hard Phase 3 route constructor cases."""

    def __init__(
        self,
        *,
        mode: str = "real_advisory",
        model: Optional[str] = None,
        temperature: float = 0.1,
    ) -> None:
        self.mode = str(mode or "real_advisory").strip().lower()
        self.model = str(model or os.getenv("DATAMIND_CHATGPT_MODEL") or DEFAULT_MODEL)
        self.temperature = float(temperature)
        self._call_log: List[Dict[str, Any]] = []

        if self.mode not in ("real_advisory", "mock", "dry_run"):
            logger.warning(
                "ConstructorLLMAdvisor: unrecognized mode '%s', defaulting to 'mock'",
                self.mode,
            )
            self.mode = "mock"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def interpret_missing_route(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        Interpret whether a missing route case is truly missing or already
        represented. Returns structured advisory with action recommendation.

        Evidence keys:
            name_hint, operator_hint, variant_hint, sector_key, sector_label,
            existing_catalog_routes, gap_context, stop_count, approximate_bbox
        """
        evidence = dict(evidence or {})
        task = "interpret_missing_route"

        user_prompt = _INTERPRET_MISSING_ROUTE_PROMPT.format(
            name_hint=evidence.get("name_hint") or "(none)",
            operator_hint=evidence.get("operator_hint") or "(none)",
            variant_hint=evidence.get("variant_hint") or "(none)",
            sector_key=evidence.get("sector_key") or "(unassigned)",
            sector_label=evidence.get("sector_label") or "(unknown)",
            stop_count=evidence.get("stop_count") or 0,
            approximate_bbox=_safe_json(evidence.get("approximate_bbox")),
            gap_context_json=_safe_json(evidence.get("gap_context") or {}),
            existing_routes_json=_safe_json(
                _truncate_routes(evidence.get("existing_catalog_routes") or [])
            ),
        )

        return self._dispatch(
            task=task,
            evidence_payload=evidence,
            user_prompt=user_prompt,
            mock_fn=lambda: _mock_interpret_missing_route(evidence),
        )

    def assess_duplicate_risk(
        self,
        candidate: Dict[str, Any],
        existing_routes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Compare a constructor candidate against the existing catalog.
        Returns duplicate/variant assessment with confidence.
        """
        candidate = dict(candidate or {})
        existing_routes = list(existing_routes or [])
        task = "assess_duplicate_risk"

        evidence_payload = {
            "candidate": candidate,
            "existing_routes_count": len(existing_routes),
            "existing_routes_sample": _truncate_routes(existing_routes),
        }

        user_prompt = _ASSESS_DUPLICATE_RISK_PROMPT.format(
            candidate_json=_safe_json(candidate),
            existing_routes_json=_safe_json(_truncate_routes(existing_routes)),
        )

        return self._dispatch(
            task=task,
            evidence_payload=evidence_payload,
            user_prompt=user_prompt,
            mock_fn=lambda: _mock_assess_duplicate_risk(candidate, existing_routes),
        )

    def suggest_improvements(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """
        Given a draft route record, suggest better hints, missing context,
        and flag quality issues.
        """
        draft = dict(draft or {})
        task = "suggest_improvements"

        user_prompt = _SUGGEST_IMPROVEMENTS_PROMPT.format(
            draft_json=_safe_json(draft),
        )

        return self._dispatch(
            task=task,
            evidence_payload=draft,
            user_prompt=user_prompt,
            mock_fn=lambda: _mock_suggest_improvements(draft),
        )

    def explain_classification(
        self,
        case: Dict[str, Any],
        classification: str,
    ) -> Dict[str, Any]:
        """
        Explain why a case received a given preflight classification.
        """
        case = dict(case or {})
        classification = str(classification or "").strip()
        task = "explain_classification"

        evidence_payload = {
            "case": case,
            "classification": classification,
        }

        user_prompt = _EXPLAIN_CLASSIFICATION_PROMPT.format(
            case_json=_safe_json(case),
            classification=classification,
        )

        return self._dispatch(
            task=task,
            evidence_payload=evidence_payload,
            user_prompt=user_prompt,
            mock_fn=lambda: _mock_explain_classification(case, classification),
        )

    def interpret_sequence(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        Interpret a route's likely stop sequence from available evidence.
        Uses name parsing, corridor/locality clues, anchor stops, and
        intermediate hints to suggest a sequence-building strategy.

        Evidence keys:
            name_hint, operator_hint, variant_hint, sector_key,
            gap_start_hint, gap_end_hint, gap_direction_hint,
            anchor_stop_ids (list), anchor_count (int),
            intermediate_stop_hints (list[dict]),
            corridor_hints (list[str]), locality_clues (list[str]),
            must_pass_through (list[str]), total_ordered_stops (int),
            sequence_notes (str), existing_catalog_routes (list[dict])
        """
        evidence = dict(evidence or {})
        task = "interpret_sequence"

        user_prompt = _INTERPRET_SEQUENCE_PROMPT.format(
            name_hint=evidence.get("name_hint") or "(none)",
            operator_hint=evidence.get("operator_hint") or "(none)",
            variant_hint=evidence.get("variant_hint") or "(none)",
            sector_key=evidence.get("sector_key") or "(unassigned)",
            gap_start_hint=evidence.get("gap_start_hint") or "(unknown)",
            gap_end_hint=evidence.get("gap_end_hint") or "(unknown)",
            gap_direction_hint=evidence.get("gap_direction_hint") or "(unknown)",
            anchor_count=evidence.get("anchor_count") or len(evidence.get("anchor_stop_ids") or []),
            intermediate_hints_json=_safe_json(evidence.get("intermediate_stop_hints") or []),
            corridor_hints_json=_safe_json(evidence.get("corridor_hints") or []),
            locality_clues_json=_safe_json(evidence.get("locality_clues") or []),
            must_pass_through_json=_safe_json(evidence.get("must_pass_through") or []),
            total_ordered_stops=evidence.get("total_ordered_stops") or 0,
            sequence_notes=evidence.get("sequence_notes") or "(none)",
            existing_routes_json=_safe_json(
                _truncate_routes(evidence.get("existing_catalog_routes") or [])
            ),
        )

        return self._dispatch(
            task=task,
            evidence_payload=evidence,
            user_prompt=user_prompt,
            mock_fn=lambda: _mock_interpret_sequence(evidence),
        )

    # ------------------------------------------------------------------
    # Call log access (for audit/debugging)
    # ------------------------------------------------------------------

    @property
    def call_log(self) -> List[Dict[str, Any]]:
        """Return a copy of all advisory calls made during this session."""
        return list(self._call_log)

    def clear_call_log(self) -> None:
        self._call_log.clear()

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        *,
        task: str,
        evidence_payload: Dict[str, Any],
        user_prompt: str,
        mock_fn: Any,
    ) -> Dict[str, Any]:
        """Route to real/mock/dry_run handler and wrap in standard envelope."""
        call_id = str(uuid.uuid4())
        start_ms = time.perf_counter()

        if self.mode == "dry_run":
            result = self._handle_dry_run(
                task=task,
                evidence_payload=evidence_payload,
                user_prompt=user_prompt,
                call_id=call_id,
            )
        elif self.mode == "mock":
            result = self._handle_mock(
                task=task,
                evidence_payload=evidence_payload,
                mock_fn=mock_fn,
                call_id=call_id,
            )
        else:
            # real_advisory
            result = self._handle_real(
                task=task,
                evidence_payload=evidence_payload,
                user_prompt=user_prompt,
                mock_fn=mock_fn,
                call_id=call_id,
            )

        elapsed_ms = int((time.perf_counter() - start_ms) * 1000)
        result["latency_ms"] = elapsed_ms
        result["call_id"] = call_id

        # Store in call log
        self._call_log.append(result)

        logger.info(
            "ConstructorLLMAdvisor [%s] task=%s source=%s confidence=%.2f action=%s latency=%dms",
            call_id[:8],
            task,
            result.get("source", "unknown"),
            result.get("confidence", 0.0),
            result.get("recommended_action", "n/a"),
            elapsed_ms,
        )

        return result

    def _handle_dry_run(
        self,
        *,
        task: str,
        evidence_payload: Dict[str, Any],
        user_prompt: str,
        call_id: str,
    ) -> Dict[str, Any]:
        """Log the call without executing anything."""
        logger.info(
            "ConstructorLLMAdvisor DRY_RUN [%s] task=%s prompt_length=%d",
            call_id[:8],
            task,
            len(user_prompt),
        )
        return {
            "task": task,
            "evidence_payload": evidence_payload,
            "model_output": {},
            "model": self.model,
            "confidence": 0.0,
            "recommended_action": "dry_run_no_action",
            "reasoning": "Dry run mode: no model call made. Evidence payload logged.",
            "source": "dry_run",
            "latency_ms": 0,
            "timestamp": _timestamp_iso(),
            "user_prompt_length": len(user_prompt),
        }

    def _handle_mock(
        self,
        *,
        task: str,
        evidence_payload: Dict[str, Any],
        mock_fn: Any,
        call_id: str,
    ) -> Dict[str, Any]:
        """Return deterministic mock response."""
        try:
            model_output = mock_fn()
        except Exception as exc:
            logger.error(
                "ConstructorLLMAdvisor mock_fn error [%s]: %s",
                call_id[:8],
                exc,
            )
            model_output = {
                "reasoning": f"Mock function error: {exc}",
                "confidence": 0.0,
            }

        return self._wrap_result(
            task=task,
            evidence_payload=evidence_payload,
            model_output=model_output,
            source="mock",
        )

    def _handle_real(
        self,
        *,
        task: str,
        evidence_payload: Dict[str, Any],
        user_prompt: str,
        mock_fn: Any,
        call_id: str,
    ) -> Dict[str, Any]:
        """Call OpenAI Responses API with structured JSON output."""
        schema = _TASK_SCHEMAS.get(task)
        if not schema:
            logger.error("No schema defined for task '%s', falling back to mock.", task)
            return self._handle_mock(
                task=task,
                evidence_payload=evidence_payload,
                mock_fn=mock_fn,
                call_id=call_id,
            )

        try:
            model_output, actual_model, usage = self._call_openai(
                task=task,
                user_prompt=user_prompt,
                schema_name=task,
                schema=schema,
            )
            result = self._wrap_result(
                task=task,
                evidence_payload=evidence_payload,
                model_output=model_output,
                source="openai",
            )
            result["model"] = actual_model
            result["usage"] = usage
            return result

        except Exception as exc:
            logger.warning(
                "ConstructorLLMAdvisor OpenAI call failed [%s], falling back to mock: %s",
                call_id[:8],
                exc,
            )
            result = self._handle_mock(
                task=task,
                evidence_payload=evidence_payload,
                mock_fn=mock_fn,
                call_id=call_id,
            )
            result["source"] = "fallback_mock"
            result["openai_error"] = str(exc)
            return result

    def _call_openai(
        self,
        *,
        task: str,
        user_prompt: str,
        schema_name: str,
        schema: Dict[str, Any],
    ) -> tuple:
        """
        Execute the OpenAI Responses API call. Returns (output_dict, model_str, usage_dict).
        Raises on failure.
        """
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "OpenAI SDK is not installed. Install with: pip install openai"
            ) from e

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set.")

        client = OpenAI(api_key=api_key)

        # Use task-specific system prompt if available
        system_prompt = _TASK_SYSTEM_PROMPTS.get(task, CONSTRUCTOR_SYSTEM_PROMPT)

        messages = [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ]

        text_format = {
            "type": "json_schema",
            "name": schema_name,
            "schema": schema,
            "strict": True,
        }

        # Try both API shapes (Responses API evolved across SDK versions)
        attempts = [
            {
                "model": self.model,
                "input": messages,
                "temperature": self.temperature,
                "tool_choice": "none",
                "text": {"format": text_format},
            },
            {
                "model": self.model,
                "input": messages,
                "temperature": self.temperature,
                "tool_choice": "none",
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    },
                },
            },
        ]

        response = None
        last_err: Optional[Exception] = None

        for payload in attempts:
            try:
                response = client.responses.create(**payload)
                last_err = None
                break
            except TypeError as e:
                last_err = e
            except Exception as e:
                last_err = e
                break

        if response is None:
            raise RuntimeError(f"OpenAI responses.create failed: {last_err}")

        # Extract parsed JSON from response
        parsed = self._extract_parsed_json(response)
        if not isinstance(parsed, dict):
            raise RuntimeError(
                "OpenAI structured output did not return a valid JSON object."
            )

        actual_model = str(getattr(response, "model", self.model) or self.model)
        usage = self._extract_usage(response)

        return parsed, actual_model, usage

    @staticmethod
    def _extract_parsed_json(response: Any) -> Optional[Dict[str, Any]]:
        """Extract parsed JSON dict from an OpenAI Responses API response."""
        output_parsed = getattr(response, "output_parsed", None)
        if isinstance(output_parsed, dict):
            return output_parsed

        output = getattr(response, "output", None)
        if isinstance(output, list):
            for item in output:
                content = getattr(item, "content", None)
                if not isinstance(content, list):
                    continue
                for part in content:
                    parsed = getattr(part, "parsed", None)
                    if isinstance(parsed, dict):
                        return parsed
                    text = getattr(part, "text", None)
                    if isinstance(text, str) and text.strip():
                        try:
                            obj = json.loads(text)
                        except Exception:
                            continue
                        if isinstance(obj, dict):
                            return obj
        return None

    @staticmethod
    def _extract_usage(response: Any) -> Dict[str, Any]:
        """Extract usage stats from response."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            try:
                dumped = usage.model_dump()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
        if isinstance(usage, dict):
            return usage
        return {}

    # ------------------------------------------------------------------
    # Sequence Discovery Advisory (Stage H)
    # ------------------------------------------------------------------

    def advise_sequence_discovery(
        self,
        grounded_evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        LLM advisory for sequence discovery pipeline (Stage H).

        Receives ONLY grounded evidence (DB matches, Valhalla corridor,
        PostGIS intersection results). Does NOT discover stops or geometry.
        Refines and explains.
        """
        task = "sequence_discovery_advisory"
        route_ctx = grounded_evidence.get("route_context", {})
        evidence = grounded_evidence.get("grounded_evidence", {})

        prompt = (
            "You are reviewing a transit route construction for Quito/Valle de los Chillos, Ecuador.\n"
            "The route has been grounded against real DB stops and a Valhalla road corridor.\n\n"
            f"Route: {route_ctx.get('route_name', '?')} ({route_ctx.get('operator', '?')})\n"
            f"Corridor: {evidence.get('corridor_summary', {}).get('total_length_km', '?')} km, "
            f"{evidence.get('sequence_stats', {}).get('total_stops', '?')} stops discovered\n\n"
            f"Expected envelope: {json.dumps(route_ctx.get('expected_geographic_envelope', {}), ensure_ascii=False)}\n"
            f"Grounding confidence: {evidence.get('grounding_confidence', '?')}\n"
            f"Unmatched hints: {evidence.get('unmatched_hints', [])}\n"
            f"Typed token dispatch: {json.dumps(evidence.get('token_types', {}), ensure_ascii=False)}\n"
            f"Gaps: {len(evidence.get('gaps', []))}\n"
            f"Geography metrics: {json.dumps(evidence.get('corridor_summary', {}), ensure_ascii=False)}\n"
            f"Sequence preview: {json.dumps(evidence.get('candidate_sequence_preview', []), ensure_ascii=False)}\n\n"
            "Questions:\n"
            "1. Does this stop sequence make geographic sense inside the expected Valle/connector envelope?\n"
            "2. Are there likely express/skip segments?\n"
            "3. Which gaps are most concerning?\n"
            "4. Do you see potential branch/variant issues?\n"
            "5. What is your confidence this sequence is substantially correct?\n"
            "6. Is the corridor geographically implausible for this route family?\n"
            "7. What should the operator review first?\n\n"
            "Rules:\n"
            "- Valle local feeders must stay geographically local.\n"
            "- Valle routes may leave the valley only when the seed explicitly names a real connector like La Marin, San Francisco, San Roque, Cumanda, or Quitumbe.\n"
            "- If the corridor is wildly too long, leaves the expected envelope, or inflates far beyond the anchor span, treat that as a failure.\n\n"
            "Respond in JSON with keys: sequence_assessment, express_skip_analysis, "
            "gap_analysis (list), branch_variant_risk, geographically_implausible_corridor, "
            "failure_reasons (list), confidence (0-1), recommended_action, "
            "recommended_operator_actions (list), reasoning_summary."
        )

        if self.mode == "mock":
            stats = evidence.get("sequence_stats", {})
            corridor_summary = evidence.get("corridor_summary", {})
            implausible = bool(corridor_summary.get("rejected_for_geographic_implausibility"))
            confidence = 0.12 if implausible else min(0.8, max(0.3, stats.get("total_stops", 0) / 30.0))
            return self._wrap_result(
                task=task,
                evidence_payload=grounded_evidence,
                model_output={
                    "sequence_assessment": (
                        "The grounded corridor is geographically implausible for the expected Valle route family."
                        if implausible
                        else "Sequence grounded against DB stops and Valhalla corridor."
                    ),
                    "express_skip_analysis": (
                        "Operator review should stop here because the corridor left the expected Valle/connector geography."
                        if implausible
                        else "No express patterns detected from grounded evidence."
                    ),
                    "gap_analysis": [
                        {"gap_index": i, "severity": "medium", "note": f"Gap of {g.get('gap_m', 0):.0f}m"}
                        for i, g in enumerate(evidence.get("gaps", [])[:3])
                    ],
                    "branch_variant_risk": "low",
                    "geographically_implausible_corridor": implausible,
                    "failure_reasons": ["geographically_implausible_corridor"] if implausible else [],
                    "confidence": confidence,
                    "recommended_action": "block_geography" if implausible else "review",
                    "recommended_operator_actions": [
                        "Review grounding matches for anchor stops",
                        "Check sequence gaps for missing stops",
                    ],
                    "reasoning_summary": (
                        "Mock advisory blocked the route for geographic implausibility."
                        if implausible
                        else (
                            f"Mock advisory: {stats.get('total_stops', 0)} stops discovered, "
                            f"{len(evidence.get('gaps', []))} gaps detected."
                        )
                    ),
                },
                source="mock",
            )

        if self.mode == "dry_run":
            logger.info("[dry_run] sequence_discovery_advisory payload: %s", json.dumps(grounded_evidence, default=str)[:2000])
            return self._wrap_result(
                task=task,
                evidence_payload=grounded_evidence,
                model_output={
                    "confidence": 0.0,
                    "recommended_action": "dry_run_no_call",
                    "reasoning": "Dry run — no LLM call made.",
                },
                source="dry_run",
            )

        # Real advisory — build mock_fn closure for fallback
        _mock_evidence = evidence
        _mock_corridor_summary = evidence.get("corridor_summary", {})
        _mock_stats = evidence.get("sequence_stats", {})

        def _mock_fallback():
            implausible = bool(_mock_corridor_summary.get("rejected_for_geographic_implausibility"))
            conf = 0.12 if implausible else min(0.8, max(0.3, _mock_stats.get("total_stops", 0) / 30.0))
            return {
                "sequence_assessment": (
                    "Corridor is geographically implausible."
                    if implausible
                    else "Sequence grounded against DB stops and Valhalla corridor."
                ),
                "express_skip_analysis": "No express patterns detected from grounded evidence.",
                "gap_analysis": [],
                "branch_variant_risk": "low",
                "geographically_implausible_corridor": implausible,
                "failure_reasons": ["geographically_implausible_corridor"] if implausible else [],
                "confidence": conf,
                "recommended_action": "block_geography" if implausible else "review",
                "recommended_operator_actions": [
                    "Review grounding matches for anchor stops",
                    "Check sequence gaps for missing stops",
                ],
                "reasoning_summary": f"Fallback mock: {_mock_stats.get('total_stops', 0)} stops.",
            }

        return self._dispatch(
            task=task,
            evidence_payload=grounded_evidence,
            user_prompt=prompt,
            mock_fn=_mock_fallback,
        )

    # ------------------------------------------------------------------
    # Result wrapping
    # ------------------------------------------------------------------

    def _wrap_result(
        self,
        *,
        task: str,
        evidence_payload: Dict[str, Any],
        model_output: Dict[str, Any],
        source: str,
    ) -> Dict[str, Any]:
        """Wrap model output in the standard advisory result envelope."""
        confidence = _clamp_confidence(model_output.get("confidence", 0.0))

        # Extract recommended action from model output (varies by task)
        recommended_action = str(
            model_output.get("recommended_action", "none")
        ).strip()

        reasoning = str(model_output.get("reasoning", "")).strip()

        return {
            "task": task,
            "evidence_payload": evidence_payload,
            "model_output": model_output,
            "model": self.model,
            "confidence": confidence,
            "recommended_action": recommended_action,
            "reasoning": reasoning,
            "source": source,
            "latency_ms": 0,  # overwritten by _dispatch
            "timestamp": _timestamp_iso(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_routes(
    routes: List[Dict[str, Any]],
    max_routes: int = 30,
) -> List[Dict[str, Any]]:
    """
    Truncate and slim down route list to avoid prompt bloat.
    Keep only fields relevant for comparison.
    """
    keep_fields = {
        "route_id", "route_name", "name_hint", "operator_hint",
        "variant_hint", "operator", "sector_key", "sector_label",
        "stop_count", "canonical_state", "direction", "cooperative_hint",
    }
    result = []
    for route in routes[:max_routes]:
        if not isinstance(route, dict):
            continue
        slim = {k: v for k, v in route.items() if k in keep_fields and v is not None}
        if slim:
            result.append(slim)
    return result

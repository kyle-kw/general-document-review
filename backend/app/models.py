"""Shared document and review contracts. Locations always refer to source content."""
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class CoverageWarning(BaseModel):
    code: str
    message: str
    location: dict[str, Any] = Field(default_factory=dict)
    incomplete: bool = True


class Block(BaseModel):
    id: str
    document_id: str
    text: str
    kind: str = "paragraph"
    location: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    id: str
    filename: str
    format: str
    blocks: list[Block] = Field(default_factory=list)
    warnings: list[CoverageWarning] = Field(default_factory=list)


class Evidence(BaseModel):
    document_id: str
    block_id: str
    quote: str
    start: int | None = None
    end: int | None = None
    location: dict[str, Any] = Field(default_factory=dict)
    purpose: Literal["quote", "context"] = "quote"


class AbsenceCoverage(BaseModel):
    """Program-generated search coverage, never a fabricated quotation of absence."""
    document_id: str
    reviewed_span_ids: list[str]
    block_ids: list[str]
    complete: bool
    passes: int = 1


class Fact(BaseModel):
    entity: str
    metric: str
    value: str
    unit: str
    period: str
    scope: str
    claim: str
    evidence: list[Evidence]


class Issue(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    category: Literal["typo", "punctuation", "numeric", "semantic", "custom"]
    scope: Literal["main", "cross"]
    severity: Literal["high", "medium", "low"] = "medium"
    confidence: Literal["confirmed", "needs_confirmation"] = "confirmed"
    title: str
    description: str
    suggestion: str
    evidence: list[Evidence]
    custom_rule_id: str | None = None
    custom_rule_title: str = ""
    finding_kind: Literal["violation", "missing"] = "violation"
    missing_document_id: str | None = None
    absence_coverage: list[AbsenceCoverage] = Field(default_factory=list)
    annotation_status: str = "not_applicable"
    annotation_reason: str = ""

import asyncio
import copy
import json
import re

import pytest

from app.codex_client import AgentError, ReviewCancelled
from app.models import Block, ParsedDocument
from app.pipeline import hierarchy_pairs, run_review, source_chunks
from app.requirements import NormalizedRequirements, NormalizedRule


def document(identity, texts, format="docx"):
    return ParsedDocument(id=identity, filename=identity + "." + format, format=format,
                          blocks=[Block(id=f"b{index}", document_id=identity, text=text,
                                        location={"paragraph": index + 1}) for index, text in enumerate(texts)])


def proposed(category, evidence, title="数值口径不一致"):
    return {"category": category, "severity": "medium", "confidence": "confirmed", "title": title,
            "description": "同一指标在相同时间范围存在不同数值。", "suggestion": "请核对原始数据并统一口径。",
            "evidence": evidence}


class FakeClient:
    context_chars = 48000
    concurrency = 2
    cache_identity = "test-model-v1"

    def __init__(self, numeric=True, invalid=False, reject=False):
        self.calls = []
        self.numeric = numeric
        self.invalid = invalid
        self.reject = reject
        self.active = 0
        self.maximum_active = 0

    async def run_agent(self, role, input, schema):
        role = re.search(r"\[(proofreader|facts|numeric|semantic|cross|verifier)\]", role).group(1)
        self.calls.append((role, copy.deepcopy(input)))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if role == "proofreader":
            issues = []
            for span in input["spans"]:
                if "错别自" in span["text"]:
                    start = span["offset"] + span["text"].index("错别自")
                    issues.append(proposed("typo", [{"document_id": span["document_id"], "block_id": span["block_id"],
                                                     "quote": "错别自", "start": start, "end": start + 3}], "错别字"))
            if self.invalid:
                issues.append(proposed("typo", [{"document_id": input["document_id"], "block_id": "absent",
                                                 "quote": "fabricated", "start": 0, "end": 10}]))
            return {"reviewed_span_ids": input["target_span_ids"], "issues": issues}
        if role == "facts":
            facts = []
            for span in input["spans"]:
                if not re.search(r"\d", span["text"]):
                    continue
                # A fake extractor with faithful source offsets, not a model quality claim.
                quote = span["text"][:min(1000, len(span["text"]))]
                evidence = {"document_id": span["document_id"], "block_id": span["block_id"], "quote": quote,
                            "start": span["offset"], "end": span["offset"] + len(quote)}
                facts.append({"entity": "项目", "metric": "金额", "value": re.search(r"\d+", quote).group(),
                              "unit": "元", "period": "2026", "scope": "项目整体", "claim": quote,
                              "evidence": [evidence]})
            return {"reviewed_span_ids": input["target_span_ids"], "facts": facts}
        if role in {"numeric", "cross"}:
            if role == "cross":
                left, right = input["main_facts"], input["other_facts"]
                pairs = [(a, b) for a in left for b in right]
            else:
                left, right = input["left_facts"], input["right_facts"]
                pairs = [(a, b) for i, a in enumerate(left) for b in (right or left[i + 1:])]
            issues = []
            if self.numeric:
                for a, b in pairs:
                    if a["value"] != b["value"]:
                        evidence = [{k: e[k] for k in ["document_id", "block_id", "quote", "start", "end"]}
                                    for e in [a["evidence"][0], b["evidence"][0]]]
                        issues.append(proposed("numeric", evidence))
            return {"issues": issues}
        if role == "semantic":
            return {"issues": []}
        return {"decisions": [{"candidate_id": item["candidate_id"], "keep": not self.reject,
                               "confidence": "confirmed", "reason": "双方原文支持。"} for item in input["candidates"]]}


async def test_main_and_cross_pipeline_scope_evidence_and_order(tmp_path):
    docs = [document("main", ["这里有错别自。金额100元。", "金额200元。"]),
            document("attachment", ["附件含错别自。金额300元。"], "xlsx")]
    client = FakeClient()
    progress = []
    issues = await run_review(docs, client, tmp_path, lambda *value: progress.append(value), lambda: False)
    assert any(issue.category == "typo" and issue.scope == "main" for issue in issues)
    assert any(issue.scope == "cross" for issue in issues)
    assert not any(issue.category in {"typo", "punctuation"} and issue.scope == "cross" for issue in issues)
    assert all(payload["document_id"] == "main" for role, payload in client.calls if role == "proofreader")
    first_attachment = next(index for index, (role, payload) in enumerate(client.calls)
                            if role == "facts" and payload["document_id"] == "attachment")
    assert any(role == "verifier" for role, _ in client.calls[:first_attachment])
    assert client.maximum_active <= 2
    for issue in issues:
        for evidence in issue.evidence:
            doc = next(doc for doc in docs if doc.id == evidence.document_id)
            block = next(block for block in doc.blocks if block.id == evidence.block_id)
            assert block.text[evidence.start:evidence.end] == evidence.quote
        if issue.scope == "cross":
            assert issue.evidence[0].document_id == "main"
            assert issue.confidence == "needs_confirmation"
            assert "基准有歧义" in issue.description
    assert progress[-1] == ("审查完成，准备导出", 1, 1)
    assert (tmp_path / "verified-main.json").exists()


async def test_checkpoints_reused_but_bound_to_model_and_source(tmp_path):
    docs = [document("main", ["金额100元。", "金额200元。"])]
    client = FakeClient()
    initial = await run_review(docs, client, tmp_path, lambda *_: None, lambda: False)
    calls = len(client.calls)
    cached = await run_review(docs, client, tmp_path, lambda *_: None, lambda: False)
    assert len(client.calls) == calls
    assert [issue.id for issue in initial] == [issue.id for issue in cached]
    client.cache_identity = "test-model-v2"
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False)
    assert len(client.calls) > calls
    calls = len(client.calls)
    docs[0].blocks[0].text = "金额300元。"
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False)
    assert len(client.calls) > calls


def test_long_blocks_covered_with_offsets_and_global_hierarchy():
    doc = document("main", ["甲" * 18000 + "末尾不能丢弃", "第二节金额100元。"])
    chunks = source_chunks(doc, 6000)
    seen = set()
    for chunk in chunks:
        for span in chunk["spans"]:
            block = next(block for block in doc.blocks if block.id == span["block_id"])
            assert block.text[span["offset"]:span["offset"] + len(span["text"])] == span["text"]
            seen.update((block.id, offset) for offset in range(span["offset"], span["offset"] + len(span["text"])))
    assert len(seen) == sum(len(block.text) for block in doc.blocks)
    pairs = list(hierarchy_pairs(9))
    assert len(pairs) == 45
    assert set(pairs) == {(i, j) for i in range(9) for j in range(i, 9)}
    assert (0, 8) in pairs


async def test_long_fact_batches_compare_distant_claims(tmp_path):
    client = FakeClient()
    client.context_chars = 12000
    texts = [f"第{i}项资料。" + "说明" * 220 for i in range(12)]
    docs = [document("main", texts)]
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False)
    numerical = [payload for role, payload in client.calls if role == "numeric"]
    assert any(any(f["evidence"][0]["block_id"] == "b0" for f in call["left_facts"])
               and any(f["evidence"][0]["block_id"] == "b11" for f in call["right_facts"]) for call in numerical)


async def test_invalid_issue_evidence_fails_instead_of_false_zero_result(tmp_path):
    docs = [document("main", ["正常内容。"])]
    with pytest.raises(AgentError, match="无效引文"):
        await run_review(docs, FakeClient(invalid=True), tmp_path, lambda *_: None, lambda: False)


async def test_independent_verifier_can_reject_candidates(tmp_path):
    issues = await run_review([document("main", ["含错别自。金额100元。", "金额200元。"])],
                              FakeClient(reject=True), tmp_path, lambda *_: None, lambda: False)
    assert issues == []


async def test_missing_agent_coverage_fails_instead_of_silent_success(tmp_path):
    class MissingCoverage(FakeClient):
        async def run_agent(self, role, input, schema):
            result = await super().run_agent(role, input, schema)
            if "reviewed_span_ids" in result:
                result["reviewed_span_ids"] = []
            return result

    with pytest.raises(AgentError, match="完整审阅"):
        await run_review([document("main", ["金额100元。"])], MissingCoverage(), tmp_path, lambda *_: None, lambda: False)


async def test_missing_business_fields_are_not_filled_or_salvaged(tmp_path):
    class MissingFields(FakeClient):
        async def run_agent(self, role, input, schema):
            return {"issues": []}

    with pytest.raises(AgentError, match="结构化结果不符合约定"):
        await run_review([document("main", ["正常内容。"] )], MissingFields(), tmp_path, lambda *_: None, lambda: False)


async def test_failed_attachment_is_skipped_without_proofreading(tmp_path):
    docs = [document("main", ["正常内容。"]), document("failed", [])]
    client = FakeClient()
    assert await run_review(docs, client, tmp_path, lambda *_: None, lambda: False) == []
    assert not any(payload.get("document_id") == "failed" for _, payload in client.calls)


async def test_table_labels_preserved_and_run_maps_omitted(tmp_path):
    docs = [document("main", ["100", "200"], "xlsx")]
    for block in docs[0].blocks:
        block.location = {"sheet": "年度预算", "row_header": "研发部", "column_header": "2026预算（万元）",
                          "number_format": "0.00", "runs": [{"text": "private map" * 5000}], "xpath": "long map"}
    client = FakeClient()
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False)
    chunks = [payload for role, payload in client.calls if role == "facts"]
    assert all(span["context"]["column_header"] == "2026预算（万元）" for chunk in chunks for span in chunk["spans"])
    for _, payload in client.calls:
        assert "private map" not in json.dumps(payload)
        assert "long map" not in json.dumps(payload)


async def test_cancelled_graph_makes_no_calls(tmp_path):
    client = FakeClient()
    with pytest.raises(ReviewCancelled):
        await run_review([document("main", ["金额100元。"])], client, tmp_path, lambda *_: None, lambda: True)
    assert client.calls == []


def requirements(*, count=1, kind="required_content", notes="单位解释沿用文件明示口径"):
    result = NormalizedRequirements(notes=[notes] if notes else [], custom_points=[
        NormalizedRule(id=f"custom-{index + 1}", title=f"付款条款{index + 1}",
                       criterion="合同应包含付款条款", kind=kind) for index in range(count)])
    result.digest = result.content_digest()
    return result


class CustomClient(FakeClient):
    def __init__(self, *, independently_found=False, invalid_rule=False, attachment_issue=False):
        super().__init__(numeric=False)
        self.independently_found = independently_found
        self.invalid_rule = invalid_rule
        self.attachment_issue = attachment_issue

    async def run_agent(self, role, input, schema):
        if "[custom_" not in role:
            return await super().run_agent(role, input, schema)
        name = re.search(r"\[(custom_scan|custom_absence|custom_compare)\]", role).group(1)
        self.calls.append((name, copy.deepcopy(input)))
        if name == "custom_compare":
            return {"issues": []}
        rules = []
        for rule in input["review_requirements"]["custom_points"]:
            relevant, satisfied = [], []
            applicable = not all("不适用" in span["text"] for span in input["spans"])
            for span in input["spans"]:
                quote = span["text"][:1000]
                evidence = {"document_id": span["document_id"], "block_id": span["block_id"],
                            "quote": quote, "start": span["offset"], "end": span["offset"] + len(quote)}
                if applicable and ("合同" in quote or "付款" in quote):
                    relevant.append(evidence)
                if "付款条款：" in quote or name == "custom_absence" and self.independently_found and "隐含支付约定" in quote:
                    satisfied.append(evidence)
            issues = []
            if self.attachment_issue and input["document_id"] != "main":
                issues.append({"rule_id": rule["id"], **proposed("semantic", relevant)})
                issues[-1].pop("category")
            rules.append({"rule_id": "custom-9" if self.invalid_rule else rule["id"], "applicable": applicable,
                          "reason": "按文档内容判断", "satisfied_evidence": satisfied,
                          "relevant_evidence": relevant, "issues": issues})
        return {"reviewed_span_ids": input["target_span_ids"], "rules": rules}


async def test_custom_main_missing_is_independently_searched_and_has_no_arbitrary_anchor(tmp_path):
    client = CustomClient()
    issues = await run_review([document("main", ["这是普通叙述。", "下一段依然普通。"])], client, tmp_path,
                              lambda *_: None, lambda: False, requirements=requirements())
    missing = [issue for issue in issues if issue.finding_kind == "missing"]
    assert len(missing) == 1
    assert missing[0].category == "custom"
    assert missing[0].evidence == []
    assert missing[0].absence_coverage[0].passes == 2
    assert missing[0].absence_coverage[0].complete
    assert len(missing[0].absence_coverage[0].block_ids) == 2
    assert any(role == "custom_absence" for role, _ in client.calls)
    assert all(payload["review_requirements"]["notes"] for _, payload in client.calls)


async def test_independent_absence_check_can_find_previously_missed_content(tmp_path):
    issues = await run_review([document("main", ["合同中的隐含支付约定。"])], CustomClient(independently_found=True),
                              tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    assert not any(issue.finding_kind == "missing" for issue in issues)


async def test_required_content_in_later_chunk_prevents_false_global_absence(tmp_path):
    client = CustomClient()
    docs = [document("main", ["合同内容。" * 3000, "付款条款：验收后支付。"])]
    issues = await run_review(docs, client, tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    assert not any(issue.finding_kind == "missing" for issue in issues)
    assert not any(role == "custom_absence" for role, _ in client.calls)


async def test_custom_cross_missing_has_real_main_evidence_and_target_coverage(tmp_path):
    client = CustomClient()
    docs = [document("main", ["付款条款：验收后支付。"]), document("other", ["合同约定尚待补充。"])]
    issues = await run_review(docs, client, tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    missing = [issue for issue in issues if issue.scope == "cross" and issue.finding_kind == "missing"]
    assert len(missing) == 1
    assert missing[0].missing_document_id == "other"
    assert missing[0].absence_coverage[0].document_id == "other"
    assert missing[0].absence_coverage[0].passes == 2
    assert {source.document_id for source in missing[0].evidence} == {"main", "other"}
    assert missing[0].evidence[0].document_id == "main"
    assert missing[0].evidence[1].purpose == "context"


async def test_custom_not_applicable_is_recorded_without_false_missing_or_pass(tmp_path):
    docs = [document("main", ["本文件不适用该项条款。"]), document("other", ["附件也不适用该项条款。"])]
    client = CustomClient()
    issues = await run_review(docs, client, tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    assert not issues
    assert all(any(warning.code == "custom_not_applicable" for warning in doc.warnings) for doc in docs)
    assert not any(role == "custom_absence" for role, _ in client.calls)


async def test_custom_rule_ids_and_attachment_independent_checks_are_rejected(tmp_path):
    with pytest.raises(AgentError, match="所有审查要点"):
        await run_review([document("main", ["合同内容。"])], CustomClient(invalid_rule=True), tmp_path / "id",
                         lambda *_: None, lambda: False, requirements=requirements())
    with pytest.raises(AgentError, match="附件独立问题"):
        await run_review([document("main", ["付款条款：有。"]), document("other", ["合同内容。"])],
                         CustomClient(attachment_issue=True), tmp_path / "scope", lambda *_: None, lambda: False,
                         requirements=requirements())


async def test_custom_absence_with_incomplete_parse_is_needs_confirmation(tmp_path):
    from app.models import CoverageWarning
    docs = [document("main", ["合同约定。"])]
    docs[0].warnings.append(CoverageWarning(code="scan_page", message="扫描页未覆盖"))
    issues = await run_review(docs, CustomClient(), tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    assert issues[0].finding_kind == "missing"
    assert issues[0].confidence == "needs_confirmation"
    assert not issues[0].absence_coverage[0].complete


async def test_distinct_custom_rules_with_same_missing_context_are_not_deduplicated(tmp_path):
    issues = await run_review([document("main", ["合同约定。"])], CustomClient(), tmp_path,
                              lambda *_: None, lambda: False, requirements=requirements(count=2))
    assert {issue.custom_rule_id for issue in issues} == {"custom-1", "custom-2"}
    assert len({issue.id for issue in issues}) == 2


async def test_normalized_requirements_change_invalidates_all_agent_checkpoints(tmp_path):
    docs = [document("main", ["付款条款：已具备。"])]
    client = CustomClient()
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    first = len(client.calls)
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    assert len(client.calls) == first
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False, requirements=requirements(notes="分别核对时间范围"))
    assert len(client.calls) > first


async def test_large_rules_budget_is_deducted_only_once(tmp_path):
    docs = [document("main", ["正常叙述。" * 200 for _ in range(80)])]
    rules = requirements(count=10, kind="content_constraint", notes="口径" * 1000)
    for rule in rules.custom_points:
        rule.criterion = "内容约束" * 100
    rules.digest = rules.content_digest()
    client = CustomClient()
    await run_review(docs, client, tmp_path, lambda *_: None, lambda: False, requirements=rules)
    assert all(len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= client.context_chars - 3000
               for _, payload in client.calls)


def test_overlapping_quotes_are_not_two_independent_consistency_evidences(tmp_path):
    from app.pipeline import ProposedCustomIssue, ProposedIssue, ReviewGraph
    docs = [document("main", ["项目金额100元。"])]
    graph = ReviewGraph(docs, FakeClient(), tmp_path, lambda *_: None, lambda: False, requirements(kind="consistency"))
    quotes = [{"document_id": "main", "block_id": "b0", "quote": "项目金额100元。", "start": 0, "end": 9},
              {"document_id": "main", "block_id": "b0", "quote": "金额100元", "start": 2, "end": 8}]
    with pytest.raises(AgentError, match="无效引文"):
        graph.candidates([ProposedIssue.model_validate(proposed("numeric", quotes))], "main", {"numeric"})
    custom = proposed("numeric", quotes)
    custom.pop("category")
    custom["rule_id"] = "custom-1"
    with pytest.raises(AgentError, match="原文证据"):
        graph.custom_candidates([ProposedCustomIssue.model_validate(custom)], graph.requirements.custom_points[0], "main")


async def test_independent_applicability_disagreement_never_becomes_missing_in_main_or_cross(tmp_path):
    class Disagreement(CustomClient):
        async def run_agent(self, role, input, schema):
            result = await super().run_agent(role, input, schema)
            if "[custom_absence]" in role:
                for rule in result["rules"]:
                    rule.update(applicable=False, relevant_evidence=[], satisfied_evidence=[], issues=[])
            return result

    docs = [document("main", ["普通内容。"]), document("other", ["付款条款：具备。"])]
    issues = await run_review(docs, Disagreement(), tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    assert not any(issue.finding_kind == "missing" for issue in issues)
    assert any(warning.code == "custom_not_applicable" for warning in docs[0].warnings)


async def test_missing_main_cross_result_does_not_invent_main_quotation(tmp_path):
    docs = [document("main", ["普通内容。"]), document("other", ["付款条款：具备。"])]
    issues = await run_review(docs, CustomClient(), tmp_path, lambda *_: None, lambda: False, requirements=requirements())
    cross = next(issue for issue in issues if issue.scope == "cross")
    assert cross.finding_kind == "missing"
    assert cross.missing_document_id == "main"
    assert {evidence.document_id for evidence in cross.evidence} == {"other"}
    assert cross.absence_coverage[0].document_id == "main"
    assert cross.absence_coverage[0].passes == 2


async def test_custom_main_consistency_conflicts_mark_cross_baseline_ambiguous(tmp_path):
    class Conflicting(CustomClient):
        async def run_agent(self, role, input, schema):
            if "[custom_compare]" not in role:
                return await super().run_agent(role, input, schema)
            self.calls.append(("custom_compare", copy.deepcopy(input)))
            sources = input["left_sources"] + input["right_sources"]
            if len(sources) < 2:
                return {"issues": []}
            chosen = [sources[0], sources[-1]]
            evidence = [{key: source[key] for key in ("document_id", "block_id", "quote", "start", "end")}
                        for source in chosen]
            issue = proposed("semantic", evidence, "付款期限不一致")
            issue.pop("category")
            issue["rule_id"] = input["rule"]["id"]
            return {"issues": [issue]}

    docs = [document("main", ["合同付款30天。", "合同付款60天。"]), document("other", ["合同付款90天。"])]
    issues = await run_review(docs, Conflicting(), tmp_path, lambda *_: None, lambda: False,
                              requirements=requirements(kind="consistency"))
    cross = next(issue for issue in issues if issue.scope == "cross")
    assert cross.category == "custom"
    assert cross.confidence == "needs_confirmation"
    assert "基准有歧义" in cross.description

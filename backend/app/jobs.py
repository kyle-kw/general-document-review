"""RQ entry point; jobs receive only the durable review identifier."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import shutil
import time
from uuid import uuid4

from rq import get_current_job

from .config import Settings
from .models import CoverageWarning, ParsedDocument
from .storage import review_directory
from .store import Store, TERMINAL, now

logger = logging.getLogger(__name__)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


async def execute_review(identifier: str, settings: Settings, store: Store) -> None:
    from .codex_client import AgentError, CodexAgentClient, ReviewCancelled
    from .exports import export_report
    from .parsers import ParserCancelled, parse_document
    from .pipeline import PROMPT_VERSION, run_review
    from .sandbox import OFFICECLI_VERSION, officecli_skill_identity
    from .requirements import (RequirementsError, ReviewOptions, initial_requirements_state,
                               normalize_requirements)

    item = store.get(identifier, private=True)
    if item is None or item["status"] in TERMINAL:
        return
    current_job = get_current_job()
    if current_job is not None and current_job.id != item["rq_job_id"]:
        return
    if item["cancel_requested"]:
        store.finish_current(identifier, item["rq_job_id"], settings.retention_days, status="cancelled")
        return
    with store.connect() as db:
        claimed = db.execute("UPDATE reviews SET status='running',stage='starting',updated_at=? WHERE id=? AND rq_job_id=? AND status='queued' AND cancel_requested=0",
                             (now(), identifier, item["rq_job_id"])).rowcount
    if not claimed:
        # Cancellation can arrive after the snapshot above but before the claim.
        # A started RQ job then returns without ever entering the execution loop.
        state = store.get(identifier, private=True)
        if state and state["cancel_requested"]:
            store.finish_current(identifier, item["rq_job_id"], settings.retention_days,
                                 status="cancelled", expected_status="queued")
        return
    directory = review_directory(settings.data_dir, identifier)
    workspace = directory / "work"
    workspace.mkdir(parents=True, exist_ok=True)
    last_check = 0.0
    was_cancelled = False

    def cancelled() -> bool:
        nonlocal last_check, was_cancelled
        if time.monotonic() - last_check > 0.3:
            state = store.get(identifier, private=True)
            was_cancelled = state is None or bool(state["cancel_requested"])
            last_check = time.monotonic()
        return was_cancelled

    def progress(stage: str, current: int, total: int) -> None:
        if cancelled():
            raise ReviewCancelled()
        store.update(identifier, stage=stage, progress_current=current, progress_total=max(1, total))

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(15)
            store.update(identifier, stage=(store.get(identifier, private=True) or {}).get("stage", "running"))

    pulse = asyncio.create_task(heartbeat())
    parsed: list[ParsedDocument] = []
    try:
        options = ReviewOptions.model_validate(item["review_options"])
        requirements_state = initial_requirements_state(options)
        if requirements_state.status != "not_required":
            def requirements_progress(stage: str, current: int, total: int) -> None:
                requirements_state.status = stage
                store.update(identifier, review_requirements=requirements_state.model_dump(mode="json"))
                progress("normalizing_requirements", current, total)

            # No source files, workspace or MCP tools are provided to either
            # compiler turn. Raw user text cannot reach review-agent tools.
            async with CodexAgentClient(settings, workspace / "requirements-codex", cancelled) as compiler:
                requirements_state = await normalize_requirements(
                    options, compiler, workspace / "checkpoints", cancelled, requirements_progress)
        store.update(identifier, review_requirements=requirements_state.model_dump(mode="json"))
        for index, file in enumerate(item["files"]):
            progress("parsing", index, len(item["files"]))
            source = directory / file["path"]
            cache = workspace / f"parsed-{file['id']}.json"
            try:
                with source.open("rb") as stream:
                    fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
                parse_key = hashlib.sha256(f"parser-v2:{fingerprint}:{settings.paddleocr_url}:{settings.paddleocr_batch_pages}".encode()).hexdigest()
                saved = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
                if saved and saved["key"] == parse_key and not any(w.get("incomplete") for w in saved["document"]["warnings"]):
                    document = ParsedDocument.model_validate(saved["document"])
                else:
                    task = asyncio.create_task(parse_document(source, file["id"], file["filename"], settings, workspace / file["id"], cancelled=cancelled))
                    try:
                        while not task.done():
                            if cancelled():
                                task.cancel()
                                raise ReviewCancelled()
                            await asyncio.wait({task}, timeout=0.2)
                        document = await task
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    atomic_json(cache, {"key": parse_key, "document": document.model_dump()})
                if not document.blocks:
                    raise ValueError("未提取到可审查文字，请检查文件或配置 OCR")
                parsed.append(document)
            except (ReviewCancelled, ParserCancelled) as exc:
                raise ReviewCancelled() from exc
            except Exception as exc:
                if index == 0:
                    raise ValueError("主文件解析失败，请检查文件是否有效、未加密且包含可提取文字") from exc
                logger.warning("Reference parse failed for review %s file %s (%s)", identifier, file["id"], type(exc).__name__)
                parsed.append(ParsedDocument(id=file["id"], filename=file["filename"], format=source.suffix[1:], warnings=[
                    CoverageWarning(code="parse_failed", message="此文件解析失败，未完成与主文件的一致性审查")]))
            store.update(identifier, documents=[doc.model_dump() for doc in parsed], warnings=[
                {**warning.model_dump(), "document_id": doc.id, "filename": doc.filename} for doc in parsed for warning in doc.warnings])
        async with CodexAgentClient(settings, workspace / "codex", cancelled,
                                    source_files={file["id"]: directory / file["path"] for file in item["files"]}) as client:
            found = await run_review(parsed, client, workspace / "checkpoints", progress, cancelled,
                                     requirements=requirements_state.normalized)
        store.update(identifier, documents=[doc.model_dump() for doc in parsed], warnings=[
            {**warning.model_dump(), "document_id": doc.id, "filename": doc.filename} for doc in parsed for warning in doc.warnings])
        progress("exporting", 0, 1)
        final_status = "partial" if any(w.incomplete for doc in parsed for w in doc.warnings) else "completed"
        report = {"review_id": identifier, "main_document_id": parsed[0].id, "status": final_status,
                  "attempt": item["attempt"],
                  "review_requirements": requirements_state.normalized.model_dump(mode="json"),
                  "requirements_policy_version": requirements_state.policy_version,
                  "created_at": item["created_at"], "finished_at": now(),
                  "runtime_versions": {"review_prompt": PROMPT_VERSION, "officecli": OFFICECLI_VERSION,
                                       "officecli_skill": officecli_skill_identity()},
                  "scope": {"main": ["typo", "punctuation", "numeric", "semantic"] + (["custom"] if options.custom_points else []),
                            "references": ["numeric", "semantic"] + (["custom"] if options.custom_points else []),
                            "office_images": False, "external_fact_check": False},
                  "documents": [doc.model_dump() for doc in parsed], "issues": [issue.model_dump() for issue in found]}
        staging = directory / f"output-{uuid4().hex}.tmp"
        staging.mkdir()
        try:
            outputs = await asyncio.to_thread(export_report, report, directory / item["files"][0]["path"], staging)
            if cancelled():
                raise ReviewCancelled()
            published = directory / f"output-{item['attempt']}"
            if published.exists():
                # A previous incomplete attempt must not replace registered artifacts.
                published = directory / f"output-{uuid4().hex}"
            staging.rename(published)
            artifacts = [{"id": uuid4().hex, "name": entry["name"], "kind": entry["kind"], "media_type": entry["media_type"],
                          "attempt": item["attempt"],
                          "path": str((published / Path(entry["path"]).name).relative_to(directory))} for entry in outputs]
            store.save_issues(identifier, report["issues"])
            store.finish(identifier, final_status, settings.retention_days, artifacts=artifacts, progress_current=1, progress_total=1)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    except ReviewCancelled:
        store.finish(identifier, "cancelled", settings.retention_days, error=None)
    except RequirementsError as exc:
        store.update(identifier, review_requirements=exc.state.model_dump(mode="json"))
        store.finish(identifier, "failed", settings.retention_days, error=str(exc))
    except AgentError:
        logger.exception("Agent failure in review %s", identifier)
        store.finish(identifier, "failed", settings.retention_days, error="模型审查失败，请检查 LLM 服务、模型配置或上下文预算后重试")
    except ValueError as exc:
        logger.warning("Review %s validation failed (%s)", identifier, type(exc).__name__)
        store.finish(identifier, "failed", settings.retention_days, error=str(exc)[:300])
    except Exception:
        logger.exception("Review %s failed", identifier)
        store.finish(identifier, "failed", settings.retention_days, error="审查执行失败，请检查后端日志后重试")
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


def run_review_job(review_id: str) -> dict:
    settings = Settings()
    store = Store(settings.database_path)
    store.initialize()
    asyncio.run(execute_review(review_id, settings, store))
    item = store.get(review_id, private=True)
    return {"id": review_id, "status": item["status"] if item else "deleted"}

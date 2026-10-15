from pathlib import Path
import secrets
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from redis.exceptions import RedisError
from rq import Queue
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, ValidationError

from .job_queue import get_queue
from .storage import SUPPORTED, remove_review, review_directory, validate_file
from .store import Store, TERMINAL, now
from .requirements import ReviewOptions, initial_requirements_state

router = APIRouter()
COOKIE = "review_browser"


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_options: ReviewOptions | None = None


def store_for(request: Request) -> Store:
    return request.app.state.store


def require_owner(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    owner = store_for(request).owner(token) if 32 <= len(token) <= 128 else None
    if owner is None:
        raise HTTPException(401, "浏览器身份已失效，请刷新页面")
    return owner


def owned(request: Request, identifier: str, owner: str, *, private: bool = False) -> dict:
    item = store_for(request).get(identifier, owner, private=private)
    if item is None:
        raise HTTPException(404, "审查任务不存在")
    return item


@router.post("/session")
def session(request: Request, response: Response) -> dict:
    token = request.cookies.get(COOKIE, "")
    store = store_for(request)
    if not (32 <= len(token) <= 128 and store.owner(token)):
        token = secrets.token_urlsafe(32)
        store.add_owner(token)
    response.set_cookie(COOKIE, token, max_age=31536000, httponly=True, secure=request.app.state.settings.cookie_secure,
                        samesite="strict", path="/")
    return {"ready": True, "retention_days": request.app.state.settings.retention_days}


@router.get("/config")
def public_config(request: Request, owner: str = Depends(require_owner)) -> dict:
    settings = request.app.state.settings
    return {"max_files": settings.max_files, "max_file_mb": settings.max_file_mb,
            "retention_days": settings.retention_days, "ocr_enabled": bool(settings.paddleocr_url),
            "llm_configured": bool(settings.llm_base_url and settings.llm_model and settings.llm_api_key.get_secret_value())}


@router.post("/reviews", status_code=202)
async def create_review(
    request: Request, main_file: Annotated[UploadFile, File()],
    reference_files: Annotated[list[UploadFile] | None, File()] = None,
    review_options: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    owner: str = Depends(require_owner), queue: Queue = Depends(get_queue),
) -> dict:
    settings = request.app.state.settings
    store = store_for(request)
    try:
        options = ReviewOptions.model_validate_json(review_options) if review_options is not None else ReviewOptions()
    except ValidationError as exc:
        raise HTTPException(422, "审查要求格式无效：最多10个要点，每项500字；注意事项最多2000字") from exc
    key = idempotency_key or uuid4().hex
    if len(key) > 128 or not key.strip():
        raise HTTPException(400, "无效的提交标识")
    previous = await run_in_threadpool(store.find_key, owner, key)
    if previous:
        return previous
    uploads = [main_file, *(reference_files or [])]
    if len(uploads) > settings.max_files:
        raise HTTPException(413, f"每个任务最多 {settings.max_files} 份文件")
    identifier = uuid4().hex
    directory = review_directory(settings.data_dir, identifier)
    source_dir = directory / "originals"
    source_dir.mkdir(parents=True)
    files = []
    created = False
    try:
        for index, upload in enumerate(uploads):
            filename = Path((upload.filename or "").replace("\\", "/")).name
            filename = "".join(c for c in filename if ord(c) >= 32)[:220]
            suffix = Path(filename).suffix.lower()
            if suffix not in SUPPORTED:
                raise HTTPException(415, "仅支持 .docx、.xlsx、.pptx、.pdf")
            file_id = uuid4().hex
            path = source_dir / f"{file_id}{suffix}"
            size = 0
            with path.open("wb") as stream:
                while content := await upload.read(1024 * 1024):
                    size += len(content)
                    if size > settings.max_file_mb * 1024 * 1024:
                        raise HTTPException(413, f"单个文件不能超过 {settings.max_file_mb} MiB")
                    await run_in_threadpool(stream.write, content)
            if size == 0:
                raise HTTPException(400, "不能提交空文件")
            await run_in_threadpool(validate_file, path, settings.max_unpacked_mb)
            files.append({"id": file_id, "filename": filename, "is_main": index == 0,
                          "size": size, "path": str(path.relative_to(directory))})
        created = await run_in_threadpool(store.create, identifier, owner, key, files, options.model_dump(mode="json"))
        if not created:
            await run_in_threadpool(remove_review, settings.data_dir, identifier)
            previous = await run_in_threadpool(store.find_key, owner, key)
            if previous is None:
                raise HTTPException(409, "提交冲突，请重试")
            return previous
        try:
            await run_in_threadpool(queue.enqueue, "app.jobs.run_review_job", identifier,
                                    job_id=f"{identifier}-1", job_timeout=settings.rq_job_timeout,
                                    result_ttl=settings.rq_result_ttl, failure_ttl=settings.rq_failure_ttl)
        except (RedisError, ValueError) as exc:
            # Redis may have committed the enqueue before its response was lost.
            # Preserve sources and never overwrite a worker that already claimed it.
            changed = await run_in_threadpool(store.finish_current, identifier, f"{identifier}-1",
                                             settings.retention_days, expected_status="queued",
                                             error="队列提交未确认，请检查后台队列后重试")
            if not changed:
                return await run_in_threadpool(store.get, identifier, owner)
            raise HTTPException(503, "队列提交未确认，任务已保存到审查记录，可稍后重试",
                                headers={"X-Review-Id": identifier}) from exc
        return await run_in_threadpool(store.get, identifier, owner)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        for upload in uploads:
            await upload.close()
        if not created:
            await run_in_threadpool(remove_review, settings.data_dir, identifier)


@router.get("/reviews")
def history(request: Request, owner: str = Depends(require_owner), page: int = Query(1, ge=1),
            page_size: int = Query(20, ge=1, le=100)) -> dict:
    return store_for(request).list(owner, page, page_size)


@router.get("/reviews/{identifier}")
def detail(identifier: str, request: Request, owner: str = Depends(require_owner)) -> dict:
    return owned(request, identifier, owner)


@router.get("/reviews/{identifier}/issues")
def issues(identifier: str, request: Request, owner: str = Depends(require_owner),
           page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)) -> dict:
    owned(request, identifier, owner)
    return store_for(request).issues(identifier, page, page_size)


@router.post("/reviews/{identifier}/cancel")
def cancel(identifier: str, request: Request, owner: str = Depends(require_owner), queue: Queue = Depends(get_queue)) -> dict:
    item = owned(request, identifier, owner, private=True)
    store = store_for(request)
    if item["status"] in TERMINAL:
        return store.get(identifier, owner)
    with store.connect() as db:
        changed = db.execute("UPDATE reviews SET cancel_requested=1,updated_at=? WHERE id=? AND rq_job_id=? AND status IN ('queued','running')",
                             (now(), identifier, item["rq_job_id"])).rowcount
    if not changed:
        return store.get(identifier, owner)
    if item["status"] == "queued":
        try:
            job = queue.fetch_job(item["rq_job_id"])
            # Do not declare cancellation while a worker may already be executing.
            status = job.get_status(refresh=True) if job else None
            if job is None or (status and status.value in {"queued", "deferred", "scheduled", "canceled"}):
                if job:
                    job.cancel()
                store.finish_current(identifier, item["rq_job_id"], request.app.state.settings.retention_days,
                                     status="cancelled", expected_status="queued")
        except RedisError:
            # Durable cancellation is still observed when Redis returns.
            pass
    return store.get(identifier, owner)


@router.post("/reviews/{identifier}/retry", status_code=202)
def retry(identifier: str, request: Request, payload: Annotated[RetryRequest | None, Body()] = None,
          owner: str = Depends(require_owner), queue: Queue = Depends(get_queue)) -> dict:
    store = store_for(request)
    item = owned(request, identifier, owner, private=True)
    if item["status"] not in TERMINAL:
        raise HTTPException(409, "任务停止后才能修改要求或重试")
    settings = request.app.state.settings
    # Serialize competing retries with an optimistic state transition.
    job_id = f"{identifier}-{item['attempt'] + 1}"
    options = payload.review_options if payload and payload.review_options is not None else ReviewOptions.model_validate(item["review_options"])
    with store.connect() as db:
        changed = db.execute("""UPDATE reviews SET status='queued',stage='queued',cancel_requested=0,
            attempt=attempt+1,rq_job_id=?,error=NULL,finished_at=NULL,expires_at=NULL,updated_at=?,
            review_options=?,review_requirements=?
            WHERE id=? AND attempt=? AND status IN ('completed','failed','partial','cancelled')""",
            (job_id, now(), options.model_dump_json(), initial_requirements_state(options).model_dump_json(),
             identifier, item["attempt"])).rowcount
    if not changed:
        raise HTTPException(409, "任务状态已变化，请刷新")
    try:
        queue.enqueue("app.jobs.run_review_job", identifier, job_id=job_id, job_timeout=settings.rq_job_timeout,
                      result_ttl=settings.rq_result_ttl, failure_ttl=settings.rq_failure_ttl)
    except (RedisError, ValueError) as exc:
        changed = store.finish_current(identifier, job_id, settings.retention_days, expected_status="queued",
                                       error="后台队列提交未确认，请重试")
        if not changed:
            return store.get(identifier, owner)
        raise HTTPException(503, "后台队列不可用，请稍后重试") from exc
    return store.get(identifier, owner)


@router.delete("/reviews/{identifier}", status_code=204)
def delete(identifier: str, request: Request, owner: str = Depends(require_owner)) -> Response:
    item = owned(request, identifier, owner)
    if item["status"] not in TERMINAL:
        raise HTTPException(409, "请先取消任务，等待停止后再删除")
    if not store_for(request).delete_terminal(identifier, lambda: remove_review(request.app.state.settings.data_dir, identifier)):
        raise HTTPException(409, "任务状态已变化，请刷新后重试")
    return Response(status_code=204)


def download_path(request: Request, identifier: str, owner: str, file_id: str, collection: str) -> FileResponse:
    item = owned(request, identifier, owner, private=True)
    file = next((entry for entry in item[collection] if entry["id"] == file_id), None)
    if file is None:
        raise HTTPException(404, "文件不存在")
    directory = review_directory(request.app.state.settings.data_dir, identifier).resolve()
    path = (directory / file["path"]).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise HTTPException(404, "文件不存在或已过期")
    return FileResponse(path, filename=file.get("name", file.get("filename")),
                        media_type=file.get("media_type", "application/octet-stream"),
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/reviews/{identifier}/files/{file_id}")
def original(identifier: str, file_id: str, request: Request, owner: str = Depends(require_owner)) -> FileResponse:
    return download_path(request, identifier, owner, file_id, "files")


@router.get("/reviews/{identifier}/artifacts/{artifact_id}")
def artifact(identifier: str, artifact_id: str, request: Request, owner: str = Depends(require_owner)) -> FileResponse:
    return download_path(request, identifier, owner, artifact_id, "artifacts")

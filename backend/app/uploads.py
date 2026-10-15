"""Bound multipart uploads while receiving them, before temporary-file writes."""
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .config import Settings

MAX_HEADER_BYTES = 16 * 1024
MAX_OPTIONS_BYTES = 128 * 1024

UPLOAD_BODY = {
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["main_file"], "properties": {
            "main_file": {"type": "string", "format": "binary"},
            "reference_files": {"type": "array", "items": {"type": "string", "format": "binary"}},
            "review_options": {"type": "string"},
        },
    }}}},
}


class UploadLimitError(MultiPartException):
    pass


@dataclass
class ReviewUpload:
    main_file: UploadFile
    reference_files: list[UploadFile]
    review_options: str | None


class BoundedMultipartParser(MultiPartParser):
    def __init__(self, request: Request, stream: AsyncIterator[bytes], settings: Settings):
        # Counts are checked below so all configured-limit failures return 413.
        super().__init__(request.headers, stream, max_files=float("inf"), max_fields=float("inf"),
                         max_part_size=MAX_OPTIONS_BYTES)
        self.file_limit = settings.max_file_mb * 1024 * 1024
        self.file_count_limit = settings.max_files
        self.part_bytes = 0
        self.header_bytes = 0
        self.main_seen = False
        self.complete = False

    def on_end(self) -> None:
        super().on_end()
        self.complete = True

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self.part_bytes = 0
        self.header_bytes = 0

    def _check_header(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > MAX_HEADER_BYTES:
            raise UploadLimitError("文件上传头信息超过大小限制")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._check_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._check_header(end - start)
        super().on_header_value(data, start, end)

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        name = self._current_part.field_name
        if self._current_files > self.file_count_limit:
            raise UploadLimitError(f"每个任务最多 {self.file_count_limit} 份文件")
        if self._current_fields > 1:
            raise UploadLimitError("审查要求字段数量超过限制")
        if self._current_part.file is not None:
            if name not in {"main_file", "reference_files"}:
                raise MultiPartException("无效的上传文件字段")
            if name == "main_file":
                if self.main_seen:
                    raise MultiPartException("只能提交一个主文件")
                self.main_seen = True
        elif name != "review_options":
            raise MultiPartException("无效的上传表单字段")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.part_bytes += end - start
        if self._current_part.file is not None and self.part_bytes > self.file_limit:
            raise UploadLimitError(f"单个文件不能超过 {self.file_limit // (1024 * 1024)} MiB")
        super().on_part_data(data, start, end)


@asynccontextmanager
async def review_form(request: Request, settings: Settings) -> AsyncIterator[ReviewUpload]:
    limit = settings.max_files * (settings.max_file_mb * 1024 * 1024 + MAX_HEADER_BYTES + 1024) + MAX_OPTIONS_BYTES
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError as exc:
            raise HTTPException(400, "无效的请求大小") from exc
        if declared < 0:
            raise HTTPException(400, "无效的请求大小")
        if declared > limit:
            raise HTTPException(413, "上传请求超过总大小限制")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(422, "请通过文件上传表单提交审查")

    async def bounded_stream() -> AsyncIterator[bytes]:
        size = 0
        async with aclosing(request.stream()) as incoming:
            async for chunk in incoming:
                size += len(chunk)
                if size > limit:
                    raise UploadLimitError("上传请求超过总大小限制")
                yield chunk

    parser = BoundedMultipartParser(request, bounded_stream(), settings)
    form = None
    try:
        try:
            async with aclosing(parser.stream):
                form = await parser.parse()
        except UploadLimitError as exc:
            raise HTTPException(413, exc.message) from exc
        except (MultiPartException, ValueError) as exc:
            raise HTTPException(400, getattr(exc, "message", "无效的文件上传表单")) from exc
        if not parser.complete:
            raise HTTPException(400, "上传内容不完整，请重新提交")
        main = form.get("main_file")
        if not isinstance(main, UploadFile):
            raise HTTPException(422, "请选择一个主文件")
        references = form.getlist("reference_files")
        if any(not isinstance(value, UploadFile) for value in references):
            raise HTTPException(422, "无效的对照文件")
        options = form.get("review_options")
        yield ReviewUpload(main, [value for value in references if isinstance(value, UploadFile)],
                           options if isinstance(options, str) else None)
    finally:
        if form is not None:
            await form.close()
        # Close unfinished parts too (e.g. a truncated final multipart boundary).
        for temporary in parser._files_to_close_on_error:
            temporary.close()

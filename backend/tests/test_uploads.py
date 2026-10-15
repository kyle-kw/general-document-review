from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path

from docx import Document
import fakeredis
from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
from rq import Queue
from rq.serializers import JSONSerializer
from starlette.requests import ClientDisconnect, Request

from app.config import Settings
from app.job_queue import get_queue
from app.main import create_app
from app.uploads import MAX_HEADER_BYTES, review_form


@pytest.fixture
def application(tmp_path: Path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", max_file_mb=1, max_files=2)
    application = create_app(settings)
    connection = fakeredis.FakeRedis()
    monkeypatch.setattr("app.maintenance.make_connection", lambda *a, **k: connection)
    application.dependency_overrides[get_queue] = lambda: Queue("upload-test", connection=connection, serializer=JSONSerializer)
    return application


@pytest.fixture
def temporary_files(monkeypatch):
    import app.uploads as uploads
    original = uploads.BoundedMultipartParser.on_headers_finished
    files = []

    def track(parser):
        try:
            return original(parser)
        finally:
            if parser._current_part.file is not None:
                files.append(parser._current_part.file.file)

    monkeypatch.setattr(uploads.BoundedMultipartParser, "on_headers_finished", track)
    return files


async def test_unauthenticated_upload_does_not_read_or_spool_body(application, temporary_files):
    reads = []

    async def body() -> AsyncIterator[bytes]:
        reads.append(True)
        yield b"x" * (4 * 1024 * 1024)

    with TestClient(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver") as client:
            response = await client.post("/api/v1/reviews", content=body(),
                                         headers={"Content-Type": "multipart/form-data; boundary=audit"})
    assert response.status_code == 401
    assert not reads and not temporary_files


@pytest.mark.parametrize("declared_length", [None, "1"])
async def test_chunked_file_limit_stops_reading_and_cleans_partial_upload(application, temporary_files, declared_length):
    reads = []

    async def body() -> AsyncIterator[bytes]:
        yield b'--audit\r\nContent-Disposition: form-data; name="main_file"; filename="main.docx"\r\n\r\n'
        for index in range(64):
            reads.append(index)
            yield b"x" * (64 * 1024)
        yield b"\r\n--audit--\r\n"

    with TestClient(application) as browser:
        browser.post("/api/v1/session")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver",
                                     cookies=dict(browser.cookies)) as client:
            headers = {"Content-Type": "multipart/form-data; boundary=audit"}
            if declared_length is not None:
                headers["Content-Length"] = declared_length
            response = await client.post("/api/v1/reviews", content=body(), headers=headers)
        assert browser.get("/api/v1/reviews").json()["total"] == 0
    assert response.status_code == 413
    assert len(reads) <= 17
    assert temporary_files and all(file.closed for file in temporary_files)
    assert not list(application.state.settings.data_dir.glob("reviews/*"))


async def test_chunked_request_total_is_bounded_even_after_multipart_ends(application, temporary_files):
    reads = []

    async def body() -> AsyncIterator[bytes]:
        yield b'--audit\r\nContent-Disposition: form-data; name="main_file"; filename="main.docx"\r\n\r\nx\r\n--audit--\r\n'
        for index in range(64):
            reads.append(index)
            yield b"x" * (64 * 1024)

    with TestClient(application) as browser:
        browser.post("/api/v1/session")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver",
                                     cookies=dict(browser.cookies)) as client:
            response = await client.post("/api/v1/reviews", content=body(),
                                         headers={"Content-Type": "multipart/form-data; boundary=audit"})
        assert browser.get("/api/v1/reviews").json()["total"] == 0
    assert response.status_code == 413
    assert len(reads) < 40
    assert temporary_files and all(file.closed for file in temporary_files)


async def test_declared_request_limit_rejects_without_consuming_body(application, temporary_files):
    reads = []

    async def body() -> AsyncIterator[bytes]:
        reads.append(True)
        yield b"unused"

    with TestClient(application) as browser:
        browser.post("/api/v1/session")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver",
                                     cookies=dict(browser.cookies)) as client:
            response = await client.post("/api/v1/reviews", content=body(), headers={
                "Content-Type": "multipart/form-data; boundary=audit", "Content-Length": str(8 * 1024 * 1024)})
    assert response.status_code == 413
    assert not reads and not temporary_files


def test_excess_file_count_and_header_size_stop_before_file_storage(application, temporary_files):
    with TestClient(application) as client:
        client.post("/api/v1/session")
        response = client.post("/api/v1/reviews", files=[("main_file", ("main.docx", b"x")),
            ("reference_files", ("one.docx", b"x")), ("reference_files", ("two.docx", b"x"))])
        assert response.status_code == 413
        assert all(file.closed for file in temporary_files)
        temporary_files.clear()
        response = client.post("/api/v1/reviews", content=(
            b'--audit\r\nContent-Disposition: form-data; name="main_file"; filename="' + b"x" * MAX_HEADER_BYTES
            + b'.docx"\r\n\r\nx\r\n--audit--\r\n'), headers={"Content-Type": "multipart/form-data; boundary=audit"})
        assert response.status_code in {400, 413}  # The multipart library may reject headers even earlier.
        assert not temporary_files


@pytest.mark.parametrize("disconnect", [False, True])
async def test_incomplete_or_disconnected_form_closes_unfinished_files(tmp_path, temporary_files, disconnect):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    messages = iter([
        {"type": "http.request", "more_body": disconnect, "body": (
            b'--audit\r\nContent-Disposition: form-data; name="main_file"; filename="main.docx"\r\n\r\npartial')},
        {"type": "http.disconnect"},
    ])

    async def receive():
        return next(messages)

    request = Request({"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=audit")]}, receive)
    with pytest.raises(ClientDisconnect if disconnect else HTTPException):
        async with review_form(request, settings):
            pytest.fail("An incomplete upload cannot be accepted")
    assert temporary_files and all(file.closed for file in temporary_files)


@pytest.mark.parametrize("tail", [
    b'\r\n--audit\r\nContent-Disposition: form-data; name="reference_files"; filename="ref.docx"\r\n\r\npartial',
    b"\r\n--audit\r\n",
])
async def test_complete_main_does_not_hide_a_truncated_attachment_or_end_boundary(tmp_path, temporary_files, tail):
    body = b'--audit\r\nContent-Disposition: form-data; name="main_file"; filename="main.docx"\r\n\r\nmain' + tail

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=audit")]}, receive)
    with pytest.raises(HTTPException, match="上传内容不完整") as error:
        async with review_form(request, Settings(_env_file=None, data_dir=tmp_path)):
            pytest.fail("A truncated request must never silently drop an attachment")
    assert error.value.status_code == 400
    assert temporary_files and all(file.closed for file in temporary_files)


def test_multipart_api_schema_and_maximum_escaped_unicode_options(application):
    import json
    options = {"custom_points": ["\U0001f642" * 500] * 10, "review_notes": "\U0001f642" * 2000}
    document = Document()
    document.add_paragraph("Valid source")
    stream = BytesIO()
    document.save(stream)
    with TestClient(application) as client:
        client.post("/api/v1/session")
        response = client.post("/api/v1/reviews", files={"main_file": ("main.docx", stream.getvalue())},
                               data={"review_options": json.dumps(options)})
        assert response.status_code == 202, response.text
        assert response.json()["review_options"] == options
        schema = client.get("/openapi.json").json()["paths"]["/api/v1/reviews"]["post"]["requestBody"]
        assert schema["content"]["multipart/form-data"]["schema"]["required"] == ["main_file"]

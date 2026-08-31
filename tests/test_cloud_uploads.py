from __future__ import annotations

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.core.config import Settings
from app.models import BackgroundTask, Resource
from app.services.cloud_uploads import (
    ConnectorAuthRequired,
    _result_event,
    queue_upload_tasks,
    scan_local_files,
    upload_progress,
)
from app.services.resources import create_resource


def test_scan_matches_existing_resource_and_filters_formats(db_session, tmp_path):
    create_resource(db_session, {"title": "小王子", "publish_status": "draft"})
    db_session.commit()
    (tmp_path / "小王子.epub").write_bytes(b"epub-test")
    (tmp_path / "ignore.jpg").write_bytes(b"image")
    settings = Settings(
        app_env="test",
        local_storage_root=tmp_path / "runtime",
        cloud_upload_source_roots=str(tmp_path),
    )

    files = scan_local_files(db_session, str(tmp_path), settings)

    assert len(files) == 1
    assert files[0].name == "小王子.epub"
    assert files[0].resource_title == "小王子"


def test_queue_creates_draft_and_prevents_same_file_duplicate(db_session, tmp_path):
    book = tmp_path / "新书 - 测试作者.epub"
    book.write_bytes(b"ebook")
    settings = Settings(
        app_env="test",
        local_storage_root=tmp_path / "runtime",
        cloud_upload_source_roots=str(tmp_path),
    )

    first = queue_upload_tasks(
        db_session,
        [str(book)],
        ["baidu", "quark"],
        auto_create_resource=True,
        publish_after_upload=False,
        settings=settings,
    )
    second = queue_upload_tasks(
        db_session,
        [str(book)],
        ["baidu", "quark"],
        auto_create_resource=True,
        publish_after_upload=False,
        settings=settings,
    )

    assert first.queued == 2
    assert first.created_resources == 1
    assert second.queued == 0
    assert second.skipped == 2
    resource = db_session.scalar(select(Resource).where(Resource.title == "新书"))
    assert resource is not None
    assert resource.author == "测试作者"
    assert resource.publish_status == "draft"
    tasks = list(db_session.scalars(select(BackgroundTask).where(BackgroundTask.task_type == "cloud_upload")))
    assert {task.payload["provider_code"] for task in tasks} == {"baidu", "quark"}


def test_upload_progress_counts_only_latest_batch(db_session, tmp_path):
    first_book = tmp_path / "第一本.epub"
    second_book = tmp_path / "第二本.epub"
    first_book.write_bytes(b"one")
    second_book.write_bytes(b"two")
    settings = Settings(
        app_env="test",
        local_storage_root=tmp_path / "runtime",
        cloud_upload_source_roots=str(tmp_path),
    )

    queue_upload_tasks(
        db_session,
        [str(first_book)],
        ["baidu"],
        auto_create_resource=True,
        publish_after_upload=False,
        settings=settings,
    )
    queue_upload_tasks(
        db_session,
        [str(second_book)],
        ["baidu", "quark"],
        auto_create_resource=True,
        publish_after_upload=False,
        settings=settings,
    )

    progress = upload_progress(db_session)
    assert progress is not None
    assert progress.total == 2
    assert progress.pending == 2
    assert progress.percent == 0
    assert progress.active is True

    tasks = list(db_session.scalars(select(BackgroundTask).where(BackgroundTask.task_type == "cloud_upload")))
    assert len(tasks) == 3
    tasks[0].status = "completed"
    tasks[1].status = "completed"
    tasks[2].status = "failed"
    db_session.commit()

    progress = upload_progress(db_session)
    assert progress.total == 2
    assert progress.completed == 1
    assert progress.failed == 1
    assert progress.percent == 50
    assert progress.active is False
    assert progress.scope_label == "上一轮进度"


def test_quark_result_parser_identifies_missing_authorization():
    output = '{"code":-1408,"msg":"未完成授权认证","action":"upload","type":"result","data":{}}'
    try:
        _result_event(output)
    except ConnectorAuthRequired as exc:
        assert "未完成授权认证" in str(exc)
    else:
        raise AssertionError("应识别为等待授权")


def test_admin_upload_page_can_scan_local_folder(admin_client, tmp_path):
    (tmp_path / "本地测试.epub").write_bytes(b"test")
    page = admin_client.get("/admin/uploads")
    assert page.status_code == 200
    assert "上传文件并自动生成链接" in page.text
    token = BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf_token"]')["value"]

    response = admin_client.post(
        "/admin/uploads/scan",
        data={"csrf_token": token, "source_path": str(tmp_path)},
    )

    assert response.status_code == 200
    assert "本地测试.epub" in response.text
    assert "将建立草稿" in response.text

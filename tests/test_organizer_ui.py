"""离屏 Qt 回归测试；只用临时书库、假凭据，不运行夸克授权或真实上传。"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("boto3")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QDialog, QListWidget, QMenu
from PySide6.QtTest import QTest

from test_organizer import library, make_epub
from ebook_organizer import ui
from ebook_organizer.engine import scan
from ebook_organizer.safeio import sha256_file


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(library, qt_app, monkeypatch):
    class Credentials:
        def __init__(self, *args): pass
        def get(self, *args): return None
    monkeypatch.setattr(ui, "Credentials", Credentials)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    win = ui.MainWindow(library[0])
    win.show(); qt_app.processEvents()
    yield win
    if win.worker and win.worker.isRunning():
        win.cancel(); win.worker.wait(5000)
    qt_app.processEvents(); win.close(); win.deleteLater(); qt_app.processEvents()


def finish(qt_app, win):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        qt_app.processEvents()
        if (not win.worker or not win.worker.isRunning()) and win.table.isEnabled():
            return
        QTest.qWait(5)
    pytest.fail("后台任务未按时完成")


def test_all_columns_search_and_buttons(window, qt_app):
    win = window
    bid = win.model.rows[0]["book_id"]
    assert win.model.data(win.model.index(0, 10)) == bid
    for column in range(win.model.columnCount()):
        win.model.data(win.model.index(0, column))
    win.search.setText("不存在的书名"); qt_app.processEvents()
    assert win.proxy.rowCount() == 0
    win.search.setText(bid); qt_app.processEvents()
    assert win.proxy.rowCount() == 1
    labels = {b.text() for b in win.findChildren(QPushButton)}
    assert {"删除选中图书", "选择书库并扫描", "导出选中图书", "暂停 / 继续", "取消当前任务"} <= labels
    assert win.del_button.isVisible()
    assert win.delete_shortcut.context() == Qt.WidgetWithChildrenShortcut


def test_titles_dialog_edits_and_undo(window, qt_app, monkeypatch):
    bid = window.ws.books()[0]['book_id']
    window.ws.edit(bid, {'title': '历史：长副标题', 'subtitle': ''})
    monkeypatch.setattr(window, 'organize_ids', lambda: [bid])
    class Dialog(QDialog):
        def exec(self):
            self.findChild(ui.QComboBox).setCurrentIndex(1)
            assert self.findChild(ui.QTableWidget).item(0, 2).text() == '历史'
            return QDialog.Accepted
    monkeypatch.setattr(ui, 'QDialog', Dialog)
    window.edit_titles(); finish(qt_app, window)
    assert window.ws.book(bid)['metadata']['title'] == '历史'
    ui.batch_edit.undo(window.ws)
    assert window.ws.book(bid)['metadata']['title'] == '历史：长副标题'


def test_large_title_editor_save_cancel_and_empty(window, monkeypatch):
    class Dialog(QDialog):
        def exec(self):
            assert self.width() >= 900
            old = self.findChild(ui.QTextEdit, 'original_title')
            new = self.findChild(ui.QTextEdit, 'edited_title')
            assert old.isReadOnly() and old.toPlainText() == '很长的原书名' * 30
            new.setPlainText('')
            save = next(b for b in self.findChildren(QPushButton) if b.text() == '保存到预览')
            save.click()
            assert self.result() != QDialog.Accepted
            new.setPlainText('精简书名\n第二行'); save.click()
            return self.result()
    monkeypatch.setattr(ui, 'QDialog', Dialog)
    assert window.edit_title_text('很长的原书名' * 30, '原值') == '精简书名 第二行'
    monkeypatch.setattr(Dialog, 'exec', lambda self: QDialog.Rejected)
    assert window.edit_title_text('原书名', '原值') is None


def test_title_double_click_opens_large_editor(window, monkeypatch, qt_app):
    bid = window.ws.books()[0]['book_id']
    monkeypatch.setattr(window, 'organize_ids', lambda: [bid])
    calls = []
    def edit(original, current, parent):
        calls.append((original, current)); return '大窗口修改结果'
    monkeypatch.setattr(window, 'edit_title_text', edit)
    class Dialog(QDialog):
        def exec(self):
            grid = self.findChild(ui.QTableWidget)
            assert grid.editTriggers() == ui.QAbstractItemView.NoEditTriggers
            assert grid.verticalHeader().defaultSectionSize() >= 54
            grid.cellDoubleClicked.emit(0, 2)
            assert grid.item(0, 2).text() == '大窗口修改结果'
            return QDialog.Accepted
    monkeypatch.setattr(ui, 'QDialog', Dialog)
    window.edit_titles(); finish(qt_app, window)
    assert calls and window.ws.book(bid)['metadata']['title'] == '大窗口修改结果'


def test_preview_after_column_manual_edit(window, monkeypatch):
    book = window.ws.books()[0]
    plan = ui.batch_edit.preview([book], {'title': '初次修改'})
    class Dialog(QDialog):
        def exec(self):
            if self.windowTitle().startswith('预览：'):
                grid = self.findChild(ui.QTableWidget)
                grid.cellDoubleClicked.emit(0, 2)
                assert '人工最终标题' in grid.item(0, 2).text()
                return QDialog.Accepted
            edit = self.findChild(ui.QTextEdit, 'edit_title'); edit.setPlainText('人工最终标题')
            next(b for b in self.findChildren(QPushButton) if b.text()=='保存到预览').click()
            return self.result()
    monkeypatch.setattr(ui, 'QDialog', Dialog)
    result = window.review_updates([book], plan)
    assert result[0]['changes']['title'] == '人工最终标题'
    assert window.ws.book(book['book_id'])['metadata']['title'] == book['metadata']['title']


def test_large_preview_is_paginated(window, monkeypatch):
    book = window.ws.books()[0]
    books=[]; updates=[]
    for i in range(250):
        current=dict(book); current['book_id']=f'B{i}'; current['metadata']=dict(book['metadata'],title=f'书{i}')
        books.append(current)
        updates.append({'book_id':f'B{i}','revision':current['revision'],'title':f'书{i}','before':{'title':f'书{i}'},'changes':{'title':f'新{i}'}})
    class Dialog(QDialog):
        def exec(self):
            grid=self.findChild(ui.QTableWidget)
            assert grid.rowCount()==100
            next_button=next(b for b in self.findChildren(QPushButton) if b.text()=='下一页')
            next_button.click(); assert grid.rowCount()==100
            next_button.click(); assert grid.rowCount()==50
            grid.item(0,0).setCheckState(Qt.Unchecked)
            return QDialog.Accepted
    monkeypatch.setattr(ui,'QDialog',Dialog)
    result=window.review_updates(books,updates)
    assert len(result)==249 and all(item['book_id']!='B200' for item in result)


def test_description_filter_selection_and_checked_color(window, qt_app):
    window.f_status.setCurrentText('状态:缺少简介'); qt_app.processEvents()
    assert window.proxy.rowCount() == 1
    window.table.selectRow(0); qt_app.processEvents()
    assert '已选中 1 本' in window.count_label.text()
    window.f_status.setCurrentText('状态:已有简介'); qt_app.processEvents()
    assert window.proxy.rowCount() == 0
    dialog = QDialog(window); layout = ui.QVBoxLayout(dialog)
    grid = ui.QTableWidget(1,2)
    for col in range(2): grid.setItem(0,col,ui.QTableWidgetItem('示例'))
    grid.item(0,0).setCheckState(Qt.Checked)
    window.highlight_checks(grid, layout)
    assert grid.item(0,1).background().color().name() == '#d1eddd'
    grid.item(0,0).setCheckState(Qt.Unchecked)
    assert grid.item(0,1).background().color().name() == '#ffffff'


def test_read_only_task_does_not_reload_thousands(window, qt_app, monkeypatch):
    reloads=[]
    monkeypatch.setattr(window,'reload',lambda:reloads.append(True))
    window.start('只读导出',lambda control,progress: 1,lambda result:None,refresh=False)
    finish(qt_app,window)
    assert not reloads


def test_category_dialog_generates_and_confirms(window, qt_app, monkeypatch):
    bid = window.ws.books()[0]['book_id']
    window.ws.edit(bid, {'title': 'Python编程', 'main_category': '', 'subcategory': ''})
    monkeypatch.setattr(window, 'organize_ids', lambda: [bid])
    monkeypatch.setattr(window, 'ask', lambda *a: True)
    class Dialog(QDialog):
        def exec(self):
            for button in self.findChildren(QPushButton):
                if button.text() == '为勾选行生成关键词建议': button.click()
            grid = self.findChild(ui.QTableWidget)
            assert grid.cellWidget(0, 2).currentText() == '计算机互联网'
            grid.item(0, 3).setText('编程开发')
            return QDialog.Accepted
    monkeypatch.setattr(ui, 'QDialog', Dialog)
    window.edit_categories(); finish(qt_app, window)
    meta = window.ws.book(bid)['metadata']
    assert meta['main_category'] == '计算机互联网'
    assert meta['subcategory'] == '编程开发'
    assert meta['classification_status'] == 'confirmed'


def test_export_renamed_is_real_file_without_touching_original(library, tmp_path):
    from ebook_organizer.engine import export_snapshot
    ws, _, original = library
    checksum = sha256_file(original)
    bid = ws.books()[0]['book_id']
    ws.edit(bid, {'title': '精简书名', 'author': '作者', 'main_category': '文学', 'subcategory': '小说'})
    target = export_snapshot(ws, [bid], tmp_path / 'renamed')
    output = target / '网盘上传' / '文学' / '小说' / '精简书名 - 作者.epub'
    assert output.is_file() and sha256_file(output) == checksum
    assert original.is_file() and sha256_file(original) == checksum


def test_batch_dialog_changes_only_language(window, qt_app, monkeypatch):
    from PySide6.QtWidgets import QCheckBox, QComboBox
    ws=window.ws; bid=ws.books()[0]['book_id']
    ws.edit(bid,{'language':'zho'}); window.reload()
    before=ws.book(bid)['metadata'].copy()
    def accept(dialog):
        if dialog.windowTitle()=='批量编辑：只改勾选字段':
            combo=next(c for c in dialog.findChildren(QComboBox) if c.findData('filtered')>=0)
            combo.setCurrentIndex(combo.findData('filtered'))
            next(c for c in dialog.findChildren(QCheckBox) if c.text().startswith('规范已有中文')).setChecked(True)
        return QDialog.Accepted
    monkeypatch.setattr(QDialog,'exec',accept)
    window.bulk_edit(); finish(qt_app,window)
    after=ws.book(bid)['metadata']
    assert after=={**before,'language':'zh-CN'}
    assert window.table.isEnabled()


def test_saving_author_does_not_confirm_inferred_category(window, qt_app):
    ws=window.ws; bid=ws.books()[0]['book_id']
    ws.edit(bid,{'classification_status':'needs_review','language':'zho'}); window.reload()
    window.table.selectRow(0); window.show_selected()
    assert window.fields['language'].text()=='中文'
    window.fields['author'].setText('人工修改作者'); window.save_book()
    assert ws.book(bid)['metadata']['classification_status']=='needs_review'


def test_delete_filtered_selection_refreshes_and_preserves_originals(window, library, qt_app, monkeypatch):
    ws, source, original = library
    other = make_epub(source / "other.epub", title="选中删除的书", body="另一内容")
    before = {p: sha256_file(p) for p in (original, other)}
    scan(ws, source); window.reload()
    window.search.setText("选中删除"); qt_app.processEvents()
    assert window.proxy.rowCount() == 1
    window.table.selectRow(0)
    deleted = window.selected_ids()
    monkeypatch.setattr(window, "ask", lambda *args: True)
    window.delete_selected(); finish(qt_app, window)
    assert not ws.book(deleted[0]) and len(ws.books()) == 1
    assert window.proxy.rowCount() == 0 and window.current_id is None
    assert all(sha256_file(p) == checksum for p, checksum in before.items())
    assert list((ws.root / "backups").glob("before-remove-*.sqlite3"))


def test_empty_scope_and_delete_without_selection_do_nothing(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "start", lambda *args: started.append(args))
    window.start_pipeline(False, [])
    window.delete_selected()
    assert not started


def test_context_menu_selects_clicked_row(window, library, qt_app, monkeypatch):
    ws, source, _ = library
    make_epub(source / "another.epub", title="另一书", body="不相同")
    scan(ws, source); window.reload(); qt_app.processEvents()
    window.table.selectRow(0)
    class TestMenu(QMenu):
        def exec(self, *args): return None
    monkeypatch.setattr(ui, "QMenu", TestMenu)
    target = window.proxy.index(1, 0)
    window.on_table_context_menu(window.table.visualRect(target).center())
    assert window.table.selectionModel().selectedRows()[0].row() == 1


def test_quark_folder_id_uses_item_data_without_parsing_display(window, monkeypatch):
    class TestDialog(QDialog):
        def exec(self):
            self.findChild(QListWidget).setCurrentRow(1)
            return QDialog.Accepted
    monkeypatch.setattr(ui, "QDialog", TestDialog)
    window.show_quark_folders([("有 空格　的目录", "folder-123")])
    assert window.settings_fields["quark_parent"].text() == "folder-123"


def test_success_callback_can_start_next_task(window, qt_app):
    completed = []
    window.start("第一步", lambda *args: 1,
                 lambda result: window.start("第二步", lambda *args: result + 1, completed.append))
    until = time.monotonic() + 5
    while time.monotonic() < until and not completed:
        qt_app.processEvents(); QTest.qWait(5)
    assert completed == [2]


def test_table_import_preview_then_submit_uses_background_tasks(window, library, qt_app, monkeypatch, tmp_path):
    ws, _, _ = library
    bid = ws.books()[0]["book_id"]
    path = tmp_path / "fill.csv"
    path.write_text(f"编号,出版社\n{bid},回填测试出版社\n", encoding="utf-8")
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *args: (str(path), "CSV"))
    monkeypatch.setattr(ui.QInputDialog, "getItem", lambda *args: ("只补空字段（推荐）", True))
    monkeypatch.setattr(window, "ask", lambda *args: True)
    window.import_table()
    finish(qt_app, window)
    assert ws.book(bid)["metadata"]["publisher"] == "回填测试出版社"

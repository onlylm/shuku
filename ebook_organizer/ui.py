from __future__ import annotations

import csv
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QLockFile, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QKeySequence, QPixmap, QAction, QShortcut, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter, QTabWidget, QTableView, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QScrollArea)
from PySide6.QtWidgets import QInputDialog

from .connections import Credentials, SiteClient, quark_connector, quark_list_folders, upload_book, upload_cover
from .covers import make_cover
from .engine import DEFAULT_RULES, export_snapshot, scan
from .pipeline import run_full_pipeline, format_summary
from .safeio import Cancelled, Control, bounded_read
from .workspace import Workspace, now
from . import batch_edit
from . import descriptions, booklist


STATUS = {"passed": "检测通过", "warning": "有提示", "failed": "异常", "blocked": "已阻止", "confirmed": "已确认", "pending": "待处理", "suggested": "待确认建议", "running": "进行中", "succeeded": "已完成", "cancelled": "已取消", "interrupted": "已中断"}
STATUS["needs_review"] = "待人工核对"

# 设计语言：柔和浅色表面 + 克制品牌绿 + 语义状态色（成功绿 / 警告橙 / 错误红）。
# 参考 awesome-design-md 中 Airtable（友好结构化数据、克制主色 CTA）与 Notion（圆角 8px 按钮 / 12px 卡片、胶囊状态徽章、语义状态色）。
C = {
    "canvas": "#f5f7f4", "surface": "#ffffff", "surface_soft": "#eef2ee", "surface_alt": "#e7efe9",
    "primary": "#1f7d54", "primary_press": "#176343", "on_primary": "#ffffff",
    "ink": "#1d2922", "body": "#3c473e", "muted": "#727d73", "hairline": "#d8e0d8", "border": "#c2cdc4",
    "success": "#1a9e54", "success_bg": "#e4f6ec",
    "warning": "#d98324", "warning_bg": "#fcf1de",
    "error": "#d64545", "error_bg": "#fbe7e7",
    "info": "#2563c9", "info_bg": "#e7eefb",
}
RADIUS = "btn:8px; card:12px; input:8px; badge:9999px"


class BookModel(QAbstractTableModel):
    headers = ["书名", "作者", "主分类", "子分类", "ISBN", "检测", "分类", "封面", "网盘", "网站", "编号"]

    def __init__(self):
        super().__init__()
        if os.name == "nt":
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows) or not 0 <= index.column() < len(self.headers):
            return None
        book = self.rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            meta = book["metadata"]
            st = book.get("_status", {})
            if col == 7:
                return "已有" if st.get("cover") else "—"
            if col == 8:
                return "已分享" if st.get("netdisk") else "—"
            if col == 9:
                return "已同步" if st.get("site") else "—"
            mapping = {
                0: meta.get("title"),
                1: meta.get("author"),
                2: meta.get("main_category"),
                3: meta.get("subcategory"),
                4: meta.get("isbn"),
                5: "已排除" if book["excluded"] else STATUS.get(book["status"], book["status"]),
                6: STATUS.get(meta.get("classification_status"), "待处理"),
                10: book["book_id"],
            }
            return str(mapping.get(col) or "—")
        if role == Qt.ForegroundRole and col in (7, 8, 9):
            ok = book.get("_status", {}).get(["cover", "netdisk", "site"][col - 7])
            return QColor(C["success"] if ok else C["muted"])
        if role == Qt.ToolTipRole:
            return book["book_id"] + "\n" + "\n".join(item["message"] for item in book["issues"])
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class BookFilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.f = {"cover": "all", "isbn": "all", "author": "all", "publisher": "all", "category": "", "status": ""}

    def set_criteria(self, **kw):
        self.f.update(kw)
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not super().filterAcceptsRow(row, parent):
            return False
        book = self.sourceModel().rows[row]
        st = book.get("_status", {})
        f = self.f
        if f["cover"] == "yes" and not st.get("cover"):
            return False
        if f["cover"] == "no" and st.get("cover"):
            return False
        if f["isbn"] == "yes" and not str(book["metadata"].get("isbn") or "").strip():
            return False
        if f["isbn"] == "no" and str(book["metadata"].get("isbn") or "").strip():
            return False
        if f["author"] == "yes" and not str(book["metadata"].get("author") or "").strip():
            return False
        if f["author"] == "no" and str(book["metadata"].get("author") or "").strip():
            return False
        if f["publisher"] == "yes" and not str(book["metadata"].get("publisher") or "").strip():
            return False
        if f["publisher"] == "no" and str(book["metadata"].get("publisher") or "").strip():
            return False
        if f["category"] and book["metadata"].get("main_category") != f["category"]:
            return False
        if f["status"] == "netdisk" and not st.get("netdisk"):
            return False
        if f["status"] == "site" and not st.get("site"):
            return False
        if f["status"] == "excluded" and not book["excluded"]:
            return False
        if f["status"] == "failed" and book["status"] not in ("failed", "blocked"):
            return False
        if f["status"] == "pending" and (st.get("netdisk") or st.get("site")):
            return False
        if f['status'] == 'needs_review' and book['metadata'].get('classification_status') == 'confirmed': return False
        if f['status'] == 'needs_rights' and book['metadata'].get('rights_review_status') == 'confirmed': return False
        if f['status'] == 'no_description' and str(book['metadata'].get('description') or '').strip(): return False
        if f['status'] == 'has_description' and not str(book['metadata'].get('description') or '').strip(): return False
        return True


class Worker(QThread):
    progress = Signal(str)
    success = Signal(object)
    failure = Signal(str)

    def __init__(self, function):
        super().__init__()
        self.function, self.control = function, Control()

    def run(self):
        try:
            self.success.emit(self.function(self.control, self.progress.emit))
        except Cancelled as exc:
            self.failure.emit(str(exc))
        except Exception as exc:
            self.failure.emit(batch_edit.error_message(exc))


class MainWindow(QMainWindow):
    def __init__(self, workspace: Workspace):
        super().__init__()
        self.ws = workspace
        self.lock = QLockFile(str(workspace.root / "organizer.lock"))
        if not self.lock.tryLock(100):
            raise ValueError("此工作区已被另一个整理软件打开，请先关闭另一个窗口")
        self.ws.recover()
        self.credentials = Credentials(workspace.setting("workspace_id"))
        self.worker = None
        self.actions = []
        self.preview = None
        self.current_id = None
        self._elapsed = 0
        self.setWindowTitle("电子书整理工作台 0.4.2（资料补提取版）")
        icon_path = Path(__file__).resolve().parent / 'assets' / 'ebook-logo.png'
        if icon_path.exists(): self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1500, 920)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.build_library()
        self.build_tasks()
        self.build_settings()
        self.build_preview()
        self.build_pipeline()
        self.statusBar().showMessage("工作区：" + str(workspace.root) + "　｜　原书库只读")
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{font-family:'Microsoft YaHei';font-size:13px;background:{C['canvas']};color:{C['ink']};}}
            QTabWidget::pane {{border:0;}} QTabBar::tab {{padding:13px 22px;background:{C['surface_soft']};color:{C['body']};border-top-left-radius:8px;border-top-right-radius:8px;}}
            QTabBar::tab:selected {{background:{C['primary']};color:white;}}
            QGroupBox {{border:1px solid {C['hairline']};border-radius:12px;margin-top:14px;padding:16px 12px 12px;background:{C['surface']};}}
            QGroupBox::title {{subcontrol-origin:margin;left:12px;padding:0 6px;color:{C['muted']};}}
            QPushButton {{background:{C['primary']};color:white;padding:9px 14px;border:0;border-radius:8px;font-weight:500;}}
            QPushButton:hover {{background:{C['primary_press']};}} QPushButton:disabled {{background:{C['border']};color:#fff;}}
            QPushButton.alternate {{background:{C['surface']};color:{C['ink']};border:1px solid {C['border']};}}
            QPushButton.alternate:hover {{background:{C['surface_soft']};}}
            QLineEdit,QTextEdit,QComboBox,QTableView {{background:white;border:1px solid {C['border']};border-radius:8px;padding:5px;}}
            QHeaderView::section {{background:{C['surface_soft']};padding:9px;border:0;border-bottom:2px solid {C['hairline']};color:{C['body']};}}
            QTableView::item {{padding:6px;border-bottom:1px solid {C['hairline']};}}
            QTableView::item:selected, QTableView::item:selected:!active {{background:#a6dfbd;color:#103e29;border-bottom:1px solid #278456;}}
            QCheckBox::indicator {{width:20px;height:20px;}}
            QLabel {{color:{C['body']};}} QLabel.title {{font-size:15px;font-weight:600;color:{C['ink']};}}
            QProgressBar {{border:0;border-radius:8px;background:{C['surface_soft']};height:18px;text-align:center;color:{C['ink']};}}
            QProgressBar::chunk {{background:{C['primary']};border-radius:8px;}}
        """)
        self.reload()

    def button(self, label, callback, layout, style="primary"):
        button = QPushButton(label)
        button.clicked.connect(callback)
        if style == "alternate":
            button.setProperty("class", "alternate")
            button.setStyleSheet(f"background:{C['surface']};color:{C['ink']};border:1px solid {C['border']};padding:9px 14px;border-radius:8px;font-weight:500;")
        elif style == "error":
            button.setProperty("class", "error")
            button.setStyleSheet(f"background:{C['error']};color:{C['on_primary']};border:none;padding:9px 14px;border-radius:8px;font-weight:600;")
        layout.addWidget(button)
        self.actions.append(button)
        return button

    def build_library(self):
        page = QWidget(); layout = QVBoxLayout(page)
        intro = QLabel("选择书库扫描后，用上方筛选器挑出「资料齐全」的书，一键全自动上传（分类→版权→封面→网盘→网站）。资料不全的点开右侧补充，或导入表格批量回填。")
        intro.setWordWrap(True); layout.addWidget(intro)
        self.model = BookModel()
        self.proxy = BookFilterProxy(); self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(-1); self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        file_bar = QHBoxLayout()
        self.button("选择书库并扫描", self.choose_scan, file_bar)
        self.button("继续上次扫描", self.resume_scan, file_bar, "alternate")
        self.button("导出选中图书", self.export_selected, file_bar, "alternate")
        self.del_button = self.button("删除选中图书", self.delete_selected, file_bar, "error")
        file_bar.addStretch(); layout.addLayout(file_bar)
        # 筛选栏
        filter_bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("搜索书名 / 作者 / 分类 / ISBN")
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(lambda *_: self.update_count())
        filter_bar.addWidget(self.search, 2)
        self.f_cover = QComboBox(); self.f_cover.addItems(["封面:全部", "封面:有", "封面:无"])
        self.f_isbn = QComboBox(); self.f_isbn.addItems(["ISBN:全部", "ISBN:有", "ISBN:无"])
        self.f_author = QComboBox(); self.f_author.addItems(["作者:全部", "作者:有", "作者:无"])
        self.f_publisher = QComboBox(); self.f_publisher.addItems(["出版社:全部", "出版社:有", "出版社:无"])
        self.f_category = QComboBox(); self.f_category.addItem("分类:全部")
        self.f_status = QComboBox(); self.f_status.addItems(["状态:全部", "状态:已传网盘", "状态:已同步网站", "状态:已排除", "状态:异常", "状态:待处理", "状态:待核对分类", "状态:待确认版权", "状态:缺少简介", "状态:已有简介"])
        for w, key in ((self.f_cover, "cover"), (self.f_isbn, "isbn"), (self.f_author, "author"), (self.f_publisher, "publisher")):
            w.currentIndexChanged.connect(lambda *_: self._apply_filters())
        self.f_category.currentTextChanged.connect(lambda *_: self._apply_filters())
        self.f_status.currentTextChanged.connect(lambda *_: self._apply_filters())
        for w in (self.f_cover, self.f_isbn, self.f_author, self.f_publisher, self.f_category, self.f_status):
            filter_bar.addWidget(w)
        layout.addLayout(filter_bar)
        # 批量操作栏
        batch_bar = QHBoxLayout()
        self.button("全选筛选结果", self.select_all_filtered, batch_bar, "alternate")
        self.button("上传选中/筛选(全自动)", lambda: self.upload_selected_or_filtered(False), batch_bar)
        self.button("一键上传全部已筛选(全自动)", lambda: self.start_pipeline(False, self.all_filtered_ids()), batch_bar)
        self.button("导入表格回填资料", self.import_table, batch_bar, "alternate")
        self.button("导出已上传清单", self.export_uploaded, batch_bar, "alternate")
        layout.addLayout(batch_bar)
        organize_bar = QHBoxLayout()
        self.button('批量精简书名 / 去副标题', self.edit_titles, organize_bar, 'alternate')
        self.button('批量分类（可逐本调整）', self.edit_categories, organize_bar, 'alternate')
        self.button('批量版权 / 语言', self.bulk_edit, organize_bar, 'alternate')
        self.button('导出改名文件（保留原书）', self.export_renamed, organize_bar, 'alternate')
        self.button('撤销上一批资料修改', self.undo_batch, organize_bar, 'alternate')
        layout.addLayout(organize_bar)
        info_bar = QHBoxLayout()
        self.button('批量补提取作者 / 出版社 / 简介', self.extract_descriptions, info_bar, 'alternate')
        self.button('一键导出书单 CSV', self.export_booklist, info_bar, 'alternate')
        self.button('导出 AI 补全模板', self.export_ai_template, info_bar, 'alternate')
        layout.addLayout(info_bar)
        splitter = QSplitter(); layout.addWidget(splitter, 1)
        self.table = QTableView(); self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True); self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column, width in enumerate([0, 100, 85, 80, 120, 80, 80, 48, 48, 48, 110]):
            if column:
                self.table.setColumnWidth(column, width)
        self.table.selectionModel().selectionChanged.connect(self.show_selected)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.update_count())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_context_menu)
        self.delete_shortcut = QShortcut(QKeySequence.Delete, self.table, activated=self.delete_selected)
        self.delete_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        splitter.addWidget(self.table)
        detail = QWidget(); detail_layout = QVBoxLayout(detail)
        self.cover = QLabel("封面预览"); self.cover.setAlignment(Qt.AlignCenter); self.cover.setMinimumHeight(190)
        detail_layout.addWidget(self.cover)
        form = QFormLayout(); self.fields = {}
        for key, title in [("title", "书名"), ("subtitle", "副标题"), ("author", "作者"), ("translator", "译者"), ("publisher", "出版社"), ("publish_year", "出版年份"), ("language", "语言代码"), ("isbn", "ISBN"), ("main_category", "主分类"), ("subcategory", "子分类"), ("source_reference", "授权 / 来源说明")]:
            edit = QLineEdit(); self.fields[key] = edit; form.addRow(title, edit)
        self.category_suggestions = QComboBox()
        form.addRow("分类建议", self.category_suggestions)
        suggestion_button = QPushButton("填入选中的分类建议"); suggestion_button.clicked.connect(self.apply_suggestion)
        self.actions.append(suggestion_button); form.addRow("", suggestion_button)
        confirm_category = QPushButton('人工核对无误：确认本书分类')
        confirm_category.clicked.connect(self.confirm_category)
        self.actions.append(confirm_category); form.addRow('', confirm_category)
        self.rights = QComboBox()
        for title, code in [("尚未确认", ""), ("已获授权", "authorized"), ("公版", "public_domain"), ("开放许可", "open_license")]:
            self.rights.addItem(title, code)
        form.addRow("版权状态", self.rights); detail_layout.addLayout(form)
        self.description = QTextEdit(); self.description.setPlaceholderText("图书简介"); self.description.setMinimumHeight(140)
        detail_layout.addWidget(self.description)
        editbar = QHBoxLayout()
        self.button("保存此书", self.save_book, editbar)
        self.button("撤销上次修改", self.undo_book, editbar)
        self.button("选择封面", self.select_cover, editbar)
        detail_layout.addLayout(editbar)
        self.button("批量编辑 / 标题清理（先预览）", self.bulk_edit, detail_layout)
        self.button("撤销上次整批修改", self.undo_batch, detail_layout, "alternate")
        self.details = QTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(120)
        detail_layout.addWidget(self.details)
        detail_scroll = QScrollArea(); detail_scroll.setWidgetResizable(True); detail_scroll.setWidget(detail)
        splitter.addWidget(detail_scroll); splitter.setSizes([1000, 430])
        bottom = QHBoxLayout()
        self.provider = QComboBox(); self.provider.addItem("夸克网盘", "quark"); self.provider.addItem("百度网盘", "baidu"); bottom.addWidget(self.provider)
        self.button("上传选中封面到 CF", lambda: self.cloud_action("cover"), bottom, "alternate")
        self.button("上传选中图书到网盘", lambda: self.cloud_action("book"), bottom, "alternate")
        self.button("网站同步预检", self.site_preview, bottom, "alternate")
        self.button("封面＋网盘上传并预检", lambda: self.cloud_action("all"), bottom, "alternate")
        self.button("手工回填分享链接", self.manual_link, bottom, "alternate")
        layout.addLayout(bottom)
        self.count_label = QLabel(); layout.addWidget(self.count_label)
        self.tabs.addTab(page, "书库整理")

    def build_tasks(self):
        page = QWidget(); layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        controls = QHBoxLayout()
        self.pause_button = QPushButton("暂停 / 继续"); self.pause_button.clicked.connect(self.pause)
        self.cancel_button = QPushButton("取消当前任务"); self.cancel_button.clicked.connect(self.cancel)
        controls.addWidget(self.pause_button); controls.addWidget(self.cancel_button); controls.addStretch()
        self.pause_button.setEnabled(False); self.cancel_button.setEnabled(False)
        layout.addLayout(controls)
        self.button("刷新任务和异常", self.reload_tasks, bar, "alternate")
        self.button("重试上次云端任务", self.retry_cloud, bar, "alternate")
        self.button("继续上次流水线", self.retry_pipeline, bar, "alternate")
        self.button("备份工作区资料", self.backup, bar, "alternate")
        self.button("恢复到新工作区", self.restore_backup, bar, "alternate")
        self.button("打开工作区", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ws.root))), bar, "alternate")
        self.button("切换工作区", self.switch_workspace, bar, "alternate")
        layout.addLayout(bar)
        # 进度区
        prog = QGroupBox("任务进度")
        pl = QVBoxLayout(prog)
        self.task_status = QLabel("空闲"); self.task_status.setStyleSheet(f"font-weight:600;color:{C['ink']};")
        self.elapsed_label = QLabel("")
        top = QHBoxLayout(); top.addWidget(self.task_status); top.addStretch(); top.addWidget(self.elapsed_label)
        pl.addLayout(top)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 0)
        pl.addWidget(self.progress_bar)
        layout.addWidget(prog)
        self.progress_log = QTextEdit(); self.progress_log.setReadOnly(True)
        # 防止数千本任务把所有历史行永久保存在富文本控件中。
        self.progress_log.document().setMaximumBlockCount(600)
        layout.addWidget(self.progress_log, 1)
        self.task_history = QTextEdit(); self.task_history.setReadOnly(True); layout.addWidget(self.task_history, 1)
        self.tabs.addTab(page, "任务与异常")
        self.timer = QTimer(); self.timer.timeout.connect(self._tick)

    def build_settings(self):
        page = QWidget(); layout = QVBoxLayout(page); form = QFormLayout()
        # 授权状态面板
        self.auth_status = QLabel("尚未检测。请在下方点击「检查夸克授权」「检查网站」后查看状态。")
        self.auth_status.setWordWrap(True); self.auth_status.setStyleSheet(f"background:{C['info_bg']};border:1px solid {C['hairline']};border-radius:8px;padding:10px;color:{C['ink']};")
        layout.addWidget(self.auth_status)
        self.settings_fields = {}
        labels = [("site_url", "网站地址"), ("site_id", "网站编号"), ("r2_account", "R2 账户编号"), ("r2_bucket", "R2 存储桶"), ("r2_public", "公开图片域名（HTTPS）"), ("quark_parent", "夸克目标目录编号（根目录填0）"), ("quark_cli", "夸克官方连接器文件路径（可留空自动查找）"), ("connector_runtime", "连接器运行目录（可留空）"), ("baidu_root", "百度目标目录")]
        settings = self.config()
        for key, label in labels:
            field = QLineEdit(str(settings.get(key) or "")); self.settings_fields[key] = field; form.addRow(label, field)
        self.secret_fields = {}
        for key, label in [("site_token", "网站同步授权"), ("r2_key", "R2 Access Key ID"), ("r2_secret", "R2 Secret Access Key"), ("baidu_token", "百度授权令牌")]:
            field = QLineEdit(); field.setEchoMode(QLineEdit.Password); field.setPlaceholderText("留空保留已保存的系统凭据")
            self.secret_fields[key] = field; form.addRow(label, field)
        layout.addLayout(form)
        layout.addWidget(QLabel("凭据仅保存到 Windows 系统凭据库；不可用时仅本次运行有效。不要填入聊天或普通表格。"))
        bar = QHBoxLayout()
        self.button("保存连接设置", self.save_settings, bar)
        self.button("检查网站并读取分类", self.site_info, bar)
        self.button("检查夸克授权", self.quark_check, bar)
        self.button("选择夸克目录", self.quark_pick_folder, bar, "alternate")
        self.button("安装官方夸克连接器", self.install_quark, bar, "alternate")
        self.button("主动授权夸克", self.quark_authorize, bar, "alternate")
        layout.addLayout(bar)
        layout.addWidget(QLabel("分类关键词规则（已有匹配分类目录优先；关键词只作建议，人工确认后使用）"))
        self.rules = QTextEdit(); self.rules.setPlainText(json.dumps(self.ws.setting("category_rules", DEFAULT_RULES), ensure_ascii=False, indent=2)); self.rules.setMinimumHeight(120); layout.addWidget(self.rules)
        self.button("保存分类规则", self.save_rules, layout, "alternate")
        layout.addWidget(QLabel('本地分类与网站分类对应（JSON，例如 {"历史文化/中国历史": ["历史人文", "中国历史"]}）'))
        self.category_mapping = QTextEdit(); self.category_mapping.setMinimumHeight(90)
        self.category_mapping.setPlainText(json.dumps(self.ws.setting("site_category_mapping", {}), ensure_ascii=False, indent=2))
        layout.addWidget(self.category_mapping)
        self.button("保存网站分类对应", self.save_mapping, layout, "alternate")
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
        self.tabs.addTab(scroll, "连接与分类设置")

    def build_preview(self):
        page = QWidget(); layout = QVBoxLayout(page)
        layout.addWidget(QLabel("先查看对应关系，再提交选中行。同名候选不自动绑定；选择“新建独立版本”不会合并旧书。"))
        self.preview_table = QTableWidget(0, 4); self.preview_table.setHorizontalHeaderLabels(["提交", "书名", "处理方式 / 对应资源", "提示"])
        self.preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.preview_table.itemSelectionChanged.connect(self.show_preview_details)
        layout.addWidget(self.preview_table, 1)
        self.preview_details = QTextEdit(); self.preview_details.setReadOnly(True); self.preview_details.setMaximumHeight(180); layout.addWidget(self.preview_details)
        bar = QHBoxLayout()
        self.overwrite = QCheckBox("明确覆盖目标的非空资料（默认仅补空）")
        self.publish = QCheckBox("链接检测通过后自动发布（不会恢复已归档资源）")
        bar.addWidget(self.overwrite); bar.addWidget(self.publish); layout.addLayout(bar)
        self.button("确认并提交选中项", self.site_commit, layout)
        self.button("恢复上次预检 / 重试提交", self.restore_preview, layout, "alternate")
        self.tabs.addTab(page, "网站同步预检")

    def config(self):
        return self.ws.setting("connections", {"site_url": "http://127.0.0.1:8001", "site_id": "jingye-local", "baidu_root": "电子书库"})

    def build_pipeline(self):
        page = QWidget(); layout = QVBoxLayout(page)
        info = QLabel("上传流水线：核对分类和来源 → 封面上 R2 → 传夸克或百度 → 同步网站；发布需要单独勾选。\n"
                      "分类与版权默认由你确认；资料不齐、同名候选和未发布结果会明确列出。已有分享复用，改过的资料重新同步。\n"
                      "在「书库整理」页用筛选器挑好资料齐全的书，点「一键上传选中 / 全部已筛选」即可。")
        info.setWordWrap(True); layout.addWidget(info)
        form = QFormLayout()
        self.pipeline_provider = QComboBox(); self.pipeline_provider.addItem("夸克网盘", "quark"); self.pipeline_provider.addItem("百度网盘", "baidu")
        form.addRow("目标网盘", self.pipeline_provider)
        self.pipeline_rights = QComboBox()
        for title, code in [("已获授权 (authorized)", "authorized"), ("公版 (public_domain)", "public_domain"), ("开放许可 (open_license)", "open_license")]:
            self.pipeline_rights.addItem(title, code)
        form.addRow("本批版权类别（仅勾选确认时应用）", self.pipeline_rights)
        self.pipeline_source = QLineEdit(); self.pipeline_source.setPlaceholderText("填写已核对的授权或来源，不自动代填"); form.addRow("来源说明（批量确认用）", self.pipeline_source)
        self.pipeline_auto_class = QCheckBox("生成关键词分类建议（需人工确认，不直接上传）"); form.addRow(self.pipeline_auto_class)
        self.pipeline_auto_rights = QCheckBox("我已核对授权，允许将上述版权和来源用于本批"); form.addRow(self.pipeline_auto_rights)
        self.pipeline_publish = QCheckBox("同步时链接校验通过后自动发布"); form.addRow(self.pipeline_publish)
        self.pipeline_force = QCheckBox("重新校验封面并同步网站（不会重复上传已有分享的书）"); form.addRow(self.pipeline_force)
        self.pipeline_overwrite = QCheckBox("明确覆盖网站的非空资料（默认仅补空）"); form.addRow(self.pipeline_overwrite)
        self.pipeline_batch = QSpinBox(); self.pipeline_batch.setRange(1, 500); self.pipeline_batch.setValue(20); form.addRow("网站每批本数", self.pipeline_batch)
        self.pipeline_limit = QSpinBox(); self.pipeline_limit.setRange(0, 100000); self.pipeline_limit.setValue(0); self.pipeline_limit.setSpecialValueText("全部"); form.addRow("本数限制（0=全部，冒烟用）", self.pipeline_limit)
        layout.addLayout(form)
        bar = QHBoxLayout()
        self.button("▶ 开始全自动流水线（全书）", lambda: self.start_pipeline(False), bar)
        self.button("仅预演 (dry-run)", lambda: self.start_pipeline(True), bar, "alternate")
        layout.addLayout(bar)
        layout.addWidget(QLabel("任务中心可暂停/取消，完成当前网络请求后在检查点停下；继续时复用已确认的分享。\n请先配置连接并用少量图书验证。重新同步默认仅补空，更新已有资料需明确勾选覆盖。"))
        self.tabs.addTab(page, "全自动流水线")

    def start_pipeline(self, dry, book_ids=None, resume=None):
        if book_ids is not None and not book_ids:
            QMessageBox.information(self, "没有可处理的书", "当前选择或筛选结果为空，未执行全书操作。")
            return
        config = self.config()
        opts = dict(provider=self.pipeline_provider.currentData(), publish=self.pipeline_publish.isChecked(),
                    auto_classify=self.pipeline_auto_class.isChecked(), auto_rights=self.pipeline_auto_rights.isChecked(),
                    rights_status=self.pipeline_rights.currentData(), source_reference=self.pipeline_source.text().strip(),
                    dry_run=dry, batch=self.pipeline_batch.value(), limit=self.pipeline_limit.value(), force=self.pipeline_force.isChecked(), overwrite=self.pipeline_overwrite.isChecked())
        if resume is not None:
            opts = {**resume, "dry_run": dry}
        if book_ids is not None:
            opts["book_ids"] = book_ids
        if opts.get("auto_rights") and not opts.get("source_reference"):
            self.show_error("请填写批量版权确认的真实来源说明，或取消批量版权确认。")
            return
        scope = f"（明确选择 {len(book_ids)} 本）" if book_ids is not None else "（全书）"
        if not dry and not self.ask("确认真实全自动上传" + scope, "将按上述设置对" + scope + "执行：分类确认、版权申报、封面上 R2、传网盘、同步网站。只处理你确认拥有使用权限的资源。是否继续？"):
            return

        def work(control, progress):
            return run_full_pipeline(self.ws, config, self.credentials, opts, control, progress)

        def done(summary):
            QMessageBox.information(self, "全自动流水线完成", format_summary(summary))
        self.start("全自动流水线" + scope + ("（预演）" if dry else ""), work, done)

    def retry_pipeline(self):
        saved = self.ws.setting("last_pipeline_task")
        if not saved:
            self.show_error("没有可继续的流水线任务")
            return
        self.start_pipeline(False, saved.get("book_ids", []), resume=saved)

    def selected_ids(self):
        return [self.model.rows[self.proxy.mapToSource(index).row()]["book_id"] for index in self.table.selectionModel().selectedRows()]

    def upload_selected_or_filtered(self, dry):
        ids = self.selected_ids() or self.all_filtered_ids()
        if not ids:
            QMessageBox.information(self, "没有可上传的书", "请先选中书库中的行，或用筛选器筛选后再上传。")
            return
        self.start_pipeline(dry, ids)

    def all_filtered_ids(self):
        ids = []
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            ids.append(self.model.rows[self.proxy.mapToSource(index).row()]["book_id"])
        return ids

    def select_all_filtered(self):
        self.table.selectAll()

    def _apply_filters(self):
        self.proxy.set_criteria(
            cover={"封面:全部": "all", "封面:有": "yes", "封面:无": "no"}[self.f_cover.currentText()],
            isbn={"ISBN:全部": "all", "ISBN:有": "yes", "ISBN:无": "no"}[self.f_isbn.currentText()],
            author={"作者:全部": "all", "作者:有": "yes", "作者:无": "no"}[self.f_author.currentText()],
            publisher={"出版社:全部": "all", "出版社:有": "yes", "出版社:无": "no"}[self.f_publisher.currentText()],
            category="" if self.f_category.currentText() == "分类:全部" else self.f_category.currentText(),
            status={"状态:全部": "", "状态:已传网盘": "netdisk", "状态:已同步网站": "site", "状态:已排除": "excluded", "状态:异常": "failed", "状态:待处理": "pending", "状态:待核对分类":"needs_review", "状态:待确认版权":"needs_rights", "状态:缺少简介":"no_description", "状态:已有简介":"has_description"}[self.f_status.currentText()],
        )
        self.update_count()

    def require_ids(self):
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, "请选择图书", "请在书库列表选择一行或多行。")
        return ids

    def update_count(self):
        if hasattr(self, "count_label"):
            self.count_label.setText(f"书库 {len(self.model.rows)} 本｜筛选后 {self.proxy.rowCount()} 本｜已选中 {len(self.selected_ids())} 本｜Ctrl / Shift 多选；删除不删除原文件")

    def ask(self, title, message):
        return QMessageBox.question(self, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def show_error(self, error):
        self.progress_log.append("⚠ " + error)
        QMessageBox.warning(self, "操作提示", error)

    def start(self, label, function, done=None, refresh=True):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "任务进行中", "请等待当前任务完成，或在任务中心暂停/取消。")
            return
        self.progress_log.append("\n▶ " + label)
        for action in self.actions:
            action.setEnabled(False)
        self.table.setEnabled(False)
        self.pause_button.setEnabled(True); self.cancel_button.setEnabled(True)
        self.task_status.setText(label); self._elapsed = 0; self.elapsed_label.setText("已运行 0s")
        self.progress_bar.setRange(0, 0); self.timer.start(1000)
        self.worker = Worker(function)
        self._refresh_after_task = refresh
        self.worker.progress.connect(self.progress_log.append)
        self.worker.progress.connect(self.statusBar().showMessage)
        self.worker.failure.connect(self.show_error)
        self._task_succeeded = False
        def remember(result):
            self._task_succeeded = True
            self._task_result = result
        self.worker.success.connect(remember)
        self.worker.finished.connect(lambda: self.finished(done))
        self.worker.start()

    def finished(self, done=None):
        self.timer.stop(); self.progress_bar.setRange(0, 1); self.progress_bar.setValue(1)
        self.task_status.setText("空闲")
        for action in self.actions:
            action.setEnabled(True)
        self.table.setEnabled(True)
        self.pause_button.setEnabled(False); self.cancel_button.setEnabled(False)
        if getattr(self, '_refresh_after_task', True):
            self.reload()
        self.reload_tasks()
        self.statusBar().showMessage("任务结束；可在任务中心查看结果。原文件未修改。")
        if self._task_succeeded:
            (done or (lambda result: self.progress_log.append("完成：" + str(result))))(self._task_result)

    def _tick(self):
        self._elapsed += 1
        self.elapsed_label.setText(f"已运行 {self._elapsed}s")

    def pause(self):
        if self.worker and self.worker.isRunning():
            if self.worker.control.running.is_set():
                self.worker.control.running.clear(); self.progress_log.append("将在当前网络操作结束或下一个安全检查点暂停")
            else:
                self.worker.control.running.set(); self.progress_log.append("继续任务")

    def cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.control.cancelled.set(); self.worker.control.running.set()
            self.progress_log.append("已请求取消；不删除已完成的文件或远程内容")

    def reload(self):
        selected = set(self.selected_ids())
        site_id = self.config().get("site_id", "")
        rows = self.ws.books(issues_only=False)
        # 一次读取全部状态，避免几千本书逐条打开数据库导致刷新/删除后长时间卡顿。
        with self.ws.connect() as db:
            results = {(r["book_id"], r["target"]): json.loads(r["data"]) for r in db.execute("SELECT book_id,target,data FROM results")}
        cats = set()
        for b in rows:
            st = {
                "cover": bool(b.get("cover_path")),
                "netdisk": any(results.get((b["book_id"], provider), {}).get("share_url") for provider in ("quark", "baidu")),
                "site": (results.get((b["book_id"], "site:" + site_id), {}).get("status") == "ok"
                         and results.get((b["book_id"], "site:" + site_id), {}).get("revision") == b["revision"]),
            }
            b["_status"] = st
            if b["metadata"].get("main_category"):
                cats.add(b["metadata"]["main_category"])
        self.model.replace(rows)
        # 重建分类筛选
        cur = self.f_category.currentText()
        self.f_category.blockSignals(True)
        self.f_category.clear(); self.f_category.addItem("分类:全部")
        for c in sorted(cats):
            self.f_category.addItem(c)
        if cur in [self.f_category.itemText(i) for i in range(self.f_category.count())]:
            self.f_category.setCurrentText(cur)
        self.f_category.blockSignals(False)
        self._apply_filters()
        if selected:
            from PySide6.QtCore import QItemSelectionModel
            selection_model = self.table.selectionModel()
            selection_model.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            for index, book in enumerate(self.model.rows):
                if book["book_id"] in selected:
                    selection_model.select(self.proxy.mapFromSource(self.model.index(index, 0)), QItemSelectionModel.Select | QItemSelectionModel.Rows)
            self.table.setUpdatesEnabled(True)
            selection_model.blockSignals(False)
            self.show_selected(); self.update_count()
        self.update_count()

    def reload_tasks(self):
        with self.ws.connect() as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50")]
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")]
        lines = []
        for j in jobs:
            lines.append(f"● [{STATUS.get(j['status'], j['status'])}] {j['kind']}　{j['created_at'][:19]}\n   {j['details']}")
        if events:
            lines.append("最近事件：")
            lines += [f"  · {e['message']}" for e in events[:20]]
        self.task_history.setPlainText("\n\n".join(lines) or "暂无历史任务")

    def show_selected(self):
        ids = self.selected_ids()
        if not ids:
            self.current_id = None
            for field in self.fields.values():
                field.clear()
            self.description.clear(); self.details.clear(); self.cover.clear()
            self.cover.setText("请选择图书")
            self.category_suggestions.clear(); self.rights.setCurrentIndex(0)
            return
        self.current_id = ids[0]
        book = self.ws.book(ids[0])
        if not book:
            self.current_id = None
            return
        meta = book["metadata"]
        for key, field in self.fields.items():
            field.setText(batch_edit.language_label(meta.get(key)) if key == "language" else str(meta.get(key) or ""))
        self.description.setPlainText(meta.get("description") or "")
        self.rights.setCurrentIndex(max(0, self.rights.findData(meta.get("copyright_status", ""))))
        self.category_suggestions.clear()
        for item in meta.get("classification_candidates", []):
            self.category_suggestions.addItem(item["name"] + "（" + "、".join(item["evidence"]) + "）", item["name"])
        if book["cover_path"]:
            picture = QPixmap(str(self.ws.root / book["cover_path"]))
            self.cover.setPixmap(picture.scaled(130, 190, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.cover.clear(); self.cover.setText("缺少封面，可点击“选择封面”")
        r2 = self.ws.result(book["book_id"], "r2")
        receipt = self.ws.result(book["book_id"], "site:" + self.config().get("site_id", ""))
        st = {"cover": r2.get("state") == "verified" and r2.get("version") == book["cover_version"],
              "netdisk": any(self.ws.result(book["book_id"], provider).get("share_url") for provider in ("quark", "baidu")),
              "site": receipt.get("status") == "ok" and receipt.get("revision") == book["revision"]}
        self.details.setPlainText(json.dumps({
            "编号": book["book_id"], "字段来源": book["provenance"],
            "封面已传R2": st.get("cover"), "已传网盘": st.get("netdisk"), "已同步网站": st.get("site"),
            "分类依据": meta.get("classification_evidence"), "分类建议": meta.get("classification_candidates", []),
            "提示": book["issues"], "夸克结果": self.ws.result(book["book_id"], "quark"),
            "网站结果": self.ws.result(book["book_id"], "site:" + self.config().get("site_id", "")),
        }, ensure_ascii=False, indent=2))

    def save_book(self):
        if not self.current_id:
            return
        changes = {key: field.text().strip() for key, field in self.fields.items()}
        changes['language'] = batch_edit.normalize_language(changes.get('language'))
        year = changes.get("publish_year")
        if year and (not year.isdigit() or not 1 <= int(year) <= 9999):
            self.show_error("出版年份应为有效整数，不确定时可以留空"); return
        changes["publish_year"] = int(year) if year else None
        previous = self.ws.book(self.current_id)['metadata']
        category_changed = any(changes[k] != previous.get(k, '') for k in ('main_category', 'subcategory'))
        classification = ("confirmed" if changes["main_category"] else "pending") if category_changed else previous.get('classification_status', 'pending')
        changes.update(description=self.description.toPlainText(), copyright_status=self.rights.currentData(), rights_review_status="confirmed" if self.rights.currentData() and changes["source_reference"] else "pending", classification_status=classification)
        if changes['description'] != previous.get('description', ''):
            changes['description_source'] = '人工编辑'
        if not changes["title"]:
            self.show_error("书名不能为空"); return
        try:
            self.ws.edit(self.current_id, changes); self.reload()
        except ValueError as exc:
            self.show_error(str(exc))

    def apply_suggestion(self):
        category = self.category_suggestions.currentData()
        if category:
            self.fields["main_category"].setText(category)
            self.fields["subcategory"].clear()
            self.statusBar().showMessage("分类建议已填入，核对后点击保存，或批量应用到选中图书。")

    def confirm_category(self):
        if not self.current_id: return
        main=self.fields['main_category'].text().strip()
        if not main: self.show_error('请先填写主分类'); return
        if self.ask('人工确认分类','确认本书的主分类和子分类正确？这不是ISBN自动核验。'):
            self.ws.edit(self.current_id,{'main_category':main,'subcategory':self.fields['subcategory'].text().strip(),'classification_status':'confirmed'})
            self.reload()

    def bulk_edit(self):
        dialog = QDialog(self); dialog.setWindowTitle('批量编辑：只改勾选字段'); dialog.resize(650, 550)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('原文件、云端文件不变；空值也会应用，请核对。版权与分类独立修改。'))
        scope = QComboBox(); scope.addItem(f'当前选中：{len(self.selected_ids())} 本', 'selected'); scope.addItem(f'全部筛选结果：{len(self.all_filtered_ids())} 本', 'filtered'); layout.addWidget(scope)
        form=QFormLayout(); layout.addLayout(form); controls={}
        for key,label in [('copyright_status','版权状态'),('source_reference','授权 / 来源说明'),('language','语言'),('main_category','主分类'),('subcategory','子分类')]:
            check=QCheckBox(label)
            if key=='copyright_status':
                edit=QComboBox()
                for name,value in [('尚未确认',''),('已获授权','authorized'),('公版','public_domain'),('开放许可','open_license')]: edit.addItem(name,value)
            else:
                edit=QLineEdit('中文' if key=='language' else '')
            edit.setEnabled(False); check.toggled.connect(edit.setEnabled); form.addRow(check,edit); controls[key]=(check,edit)
        clean=QCheckBox('清理书名中的营销括号（保留册数、版本及普通副标题）'); layout.addWidget(clean)
        subtitle=QCheckBox('清空副标题，并从书名末尾去除完全匹配的副标题'); layout.addWidget(subtitle)
        normalize=QCheckBox('规范已有中文语言代码（不改未知、繁体及其他语言）'); layout.addWidget(normalize)
        review=QCheckBox('将选中图书分类标为待人工核对（保留原分类名称）'); layout.addWidget(review)
        go=QPushButton('下一步：预览变化'); go.clicked.connect(dialog.accept); layout.addWidget(go)
        if dialog.exec()!=QDialog.Accepted: return
        ids=self.all_filtered_ids() if scope.currentData()=='filtered' else self.selected_ids()
        if not ids: self.show_error('此范围没有图书'); return
        fields={k:(e.currentData() if isinstance(e,QComboBox) else e.text().strip()) for k,(c,e) in controls.items() if c.isChecked()}
        try:
            books=[self.ws.book(bid) for bid in ids]
            if not all(books): raise ValueError('书库发生变化，请刷新后重试')
            updates=batch_edit.preview(books,fields,clean=clean.isChecked(),clear_subtitle=subtitle.isChecked(),normalize=normalize.isChecked(),review=review.isChecked())
        except ValueError as exc: self.show_error(str(exc)); return
        if not updates: QMessageBox.information(self,'无需修改','没有字段发生变化。'); return
        updates = self.review_updates(books, updates)
        if not updates: return
        if any(item['changes'].get('copyright_status') for item in updates) and not self.ask('确认版权范围','确认这些图书均属于所选版权类别，且来源说明真实有效？'): return
        self.start('批量修改资料',lambda control,progress: batch_edit.apply(self.ws,updates,control),lambda count: QMessageBox.information(self,'批量修改完成',f'已修改 {count} 本，原文件和云端内容未修改。'))

    def review_updates(self, books, updates):
        updates = [dict(item, changes=dict(item['changes']), before=dict(item.get('before', {}))) for item in updates]
        original_books = {b['book_id']: b for b in books}
        preview=QDialog(self); preview.setWindowTitle(f'预览：{len(updates)} 本将被修改'); preview.resize(1100,650); box=QVBoxLayout(preview)
        box.addWidget(QLabel('双击“修改后”打开大窗口再次编辑；取消勾选的书不提交。“修改前”只读。确认后可撤销本批。'))
        page_size = 100
        page_count = max(1, (len(updates) + page_size - 1) // page_size)
        checked = [True] * len(updates)
        current_page = [0]
        grid=QTableWidget(0,3); grid.setHorizontalHeaderLabels(['图书','修改前','修改后']); grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        field_names={'title':'书名','subtitle':'副标题','author':'作者','publisher':'出版社','description':'简介','description_source':'简介来源','language':'语言','copyright_status':'版权状态','source_reference':'授权/来源','main_category':'主分类','subcategory':'子分类','rights_review_status':'版权核对','classification_status':'分类核对'}
        value_names={'confirmed':'已确认','needs_review':'待人工核对','pending':'待确认','authorized':'已获授权','public_domain':'公版','open_license':'开放许可'}
        def readable(values):
            return '\n'.join(field_names.get(k,k)+'：'+(value_names.get(str(v),str(v)) if v else '（空）') for k,v in values.items())
        grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); box.addWidget(grid)
        nav = QHBoxLayout(); previous = QPushButton('上一页'); following = QPushButton('下一页'); page_label = QLabel(); selected_label = QLabel()
        nav.addWidget(previous); nav.addWidget(following); nav.addWidget(page_label); nav.addStretch(); nav.addWidget(selected_label); box.addLayout(nav)
        loading = [False]
        def refresh_selected(): selected_label.setText(f'已勾选 {sum(checked)} / {len(checked)} 本；当前页浅绿色为勾选，白色为未勾选')
        def changed(cell):
            if loading[0] or cell.column()!=0: return
            absolute = current_page[0]*page_size + cell.row()
            if absolute < len(checked): checked[absolute] = cell.checkState()==Qt.Checked
            chosen = checked[absolute]
            for col in range(grid.columnCount()):
                target = grid.item(cell.row(), col)
                if target:
                    target.setBackground(QColor('#d1eddd' if chosen else '#ffffff'))
                    target.setForeground(QColor('#164d32' if chosen else '#526057'))
            refresh_selected()
        grid.itemChanged.connect(changed)
        def load_page(number):
            loading[0] = True; current_page[0] = max(0,min(number,page_count-1))
            start = current_page[0]*page_size; page = updates[start:start+page_size]
            grid.setRowCount(len(page))
            for row,item in enumerate(page):
                values=[item['title'],readable(item['before']),readable(item['changes'])]
                for col,value in enumerate(values):
                    cell=QTableWidgetItem(value); cell.setToolTip(value); grid.setItem(row,col,cell)
                grid.item(row,0).setCheckState(Qt.Checked if checked[start+row] else Qt.Unchecked)
                for col in range(3):
                    grid.item(row,col).setBackground(QColor('#d1eddd' if checked[start+row] else '#ffffff'))
                    grid.item(row,col).setForeground(QColor('#164d32' if checked[start+row] else '#526057'))
                grid.setRowHeight(row,min(180,max(54,22*max(len(item['changes']),1))))
            page_label.setText(f'第 {current_page[0]+1} / {page_count} 页（每页最多 {page_size} 本）')
            previous.setEnabled(current_page[0]>0); following.setEnabled(current_page[0]<page_count-1)
            loading[0] = False; refresh_selected()
        previous.clicked.connect(lambda: load_page(current_page[0]-1)); following.clicked.connect(lambda: load_page(current_page[0]+1)); load_page(0)
        def edit_row(row, col):
            if col != 2: return
            absolute = current_page[0]*page_size+row
            item = updates[absolute]
            revised = self.edit_update_fields(original_books[item['book_id']], item, field_names, preview)
            if revised is not None:
                updates[absolute] = revised
                grid.item(row, 2).setText(readable(revised['changes']))
                grid.item(row, 2).setToolTip(readable(revised['changes']))
        grid.cellDoubleClicked.connect(edit_row)
        edit_button = QPushButton('大窗口编辑当前行的修改后内容')
        edit_button.clicked.connect(lambda: edit_row(grid.currentRow(), 2) if grid.currentRow() >= 0 else None); box.addWidget(edit_button)
        commit=QPushButton(f'确认修改 {len(updates)} 本'); commit.clicked.connect(preview.accept); box.addWidget(commit)
        if preview.exec()!=QDialog.Accepted: return None
        return [item for index,item in enumerate(updates) if checked[index] and item['changes']]

    def edit_update_fields(self, book, item, labels, parent):
        dialog = QDialog(parent); dialog.setWindowTitle('修改后：人工再次调整'); dialog.resize(950, 720)
        layout = QVBoxLayout(dialog); layout.addWidget(QLabel('仅编辑本批涉及的字段；状态由资料重新计算。保存后回到预览，不立即提交书库。'))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); content = QWidget(); form = QFormLayout(content); scroll.setWidget(content); layout.addWidget(scroll)
        controls = {}
        for key, value in item['changes'].items():
            if key in {'classification_status', 'rights_review_status', 'description_source'}: continue
            old = QTextEdit(); old.setPlainText(str(book['metadata'].get(key) or '')); old.setReadOnly(True); old.setMaximumHeight(100)
            form.addRow(labels.get(key,key) + '（原值）', old)
            if key == 'copyright_status':
                edit = QComboBox()
                for name, data in [('尚未确认',''),('已获授权','authorized'),('公版','public_domain'),('开放许可','open_license')]: edit.addItem(name,data)
                edit.setCurrentIndex(max(0,edit.findData(value)))
            else:
                edit = QTextEdit(); edit.setAcceptRichText(False); edit.setPlainText(str(value or ''))
                edit.setMinimumHeight(240 if key=='description' else 100); edit.setStyleSheet('font-size:18px;padding:8px;')
            edit.setObjectName('edit_' + key); controls[key] = edit; form.addRow(labels.get(key,key) + '（新值）', edit)
        if not controls: layout.addWidget(QLabel('本行仅改变核对状态。如不需要，请返回预览取消勾选。'))
        error = QLabel(); error.setWordWrap(True); layout.addWidget(error)
        result = {}
        def accept():
            fields = {key:(edit.currentData() if isinstance(edit,QComboBox) else edit.toPlainText().strip()) for key,edit in controls.items()}
            for key in fields:
                if key != 'description': fields[key] = ' '.join(str(fields[key]).splitlines())
            if 'description_source' in item['changes']: fields['description_source'] = item['changes']['description_source']
            if fields.get('description') != item['changes'].get('description') and 'description' in fields:
                fields['description_source'] = '人工编辑'
            try:
                revised = batch_edit.preview([book], fields, review=item['changes'].get('classification_status')=='needs_review' and not {'main_category','subcategory'} & fields.keys())
                result['item'] = revised[0] if revised else dict(item, changes={})
                dialog.accept()
            except ValueError as exc: error.setText(str(exc))
        bar = QHBoxLayout(); cancel = QPushButton('取消'); cancel.clicked.connect(dialog.reject); save=QPushButton('保存到预览'); save.clicked.connect(accept); bar.addWidget(cancel); bar.addWidget(save); layout.addLayout(bar)
        return result.get('item') if dialog.exec()==QDialog.Accepted else None

    def highlight_checks(self, grid, layout):
        label = QLabel(); layout.addWidget(label)
        grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        checked_rows = set()
        def refresh(changed=None):
            grid.blockSignals(True)
            for row in ([changed.row()] if changed is not None else range(grid.rowCount())):
                chosen = grid.item(row, 0).checkState()==Qt.Checked
                if chosen: checked_rows.add(row)
                else: checked_rows.discard(row)
                for col in range(grid.columnCount()):
                    cell = grid.item(row, col)
                    if cell:
                        cell.setBackground(QColor('#d1eddd' if chosen else '#ffffff'))
                        cell.setForeground(QColor('#164d32' if chosen else '#526057'))
            grid.blockSignals(False)
            label.setText(f'已勾选 {len(checked_rows)} / {grid.rowCount()} 本；浅绿色为勾选，白色为未勾选。')
        grid.itemChanged.connect(refresh); refresh()

    def extract_descriptions(self):
        ids = self.organize_ids()
        if not ids: return
        def done(result):
            report = QDialog(self); report.setWindowTitle('作者 / 出版社 / 简介提取结果'); report.resize(900, 600)
            layout = QVBoxLayout(report); text = QTextEdit(); text.setReadOnly(True)
            text.setPlainText('\n'.join(row['title']+'：'+row['message'] for row in result['report'])); layout.addWidget(text)
            next_button = QPushButton(f"预览 {len(result['updates'])} 本可回填资料")
            next_button.clicked.connect(report.accept); layout.addWidget(next_button)
            if report.exec()!=QDialog.Accepted or not result['updates']: return
            updates = self.review_updates(result['books'], result['updates'])
            if updates:
                self.start('回填图书资料', lambda control,progress: batch_edit.apply(self.ws, updates, control),
                    lambda count: QMessageBox.information(self,'资料回填完成',f'已回填 {count} 本的缺失作者、出版社或简介，可撤销。网站同步预检后再提交；不会重新上传电子书。'))
        self.start('离线提取作者、出版社和简介', lambda control,progress: descriptions.extract(self.ws, ids, control, progress), done, refresh=False)

    def export_booklist(self):
        ids = self.selected_ids() or self.all_filtered_ids()
        if not ids: self.show_error('没有可导出的图书'); return
        path, _ = QFileDialog.getSaveFileName(self, f'导出 {len(ids)} 本书单（有选中导出选中，否则导出筛选结果）', '图书书单.csv', 'CSV 文件 (*.csv)')
        if not path: return
        if not path.lower().endswith('.csv'): path += '.csv'
        target = Path(path)
        if target.exists() and not self.ask('覆盖书单文件', '目标书单已存在，确认覆盖？'): return
        self.start('导出书单', lambda control,progress: booklist.export(self.ws, ids, target, control, progress),
            lambda count: QMessageBox.information(self,'书单导出完成',f'已导出 {count} 本：{target}\n仅资料，不含电子书文件、本地路径和账号凭据。'), refresh=False)

    def export_ai_template(self):
        ids = self.selected_ids() or self.all_filtered_ids()
        if not ids: self.show_error('没有可导出的图书'); return
        path, _ = QFileDialog.getSaveFileName(self, f'导出 {len(ids)} 本 AI 补全模板', 'AI补全图书资料.csv', 'CSV 文件 (*.csv)')
        if not path: return
        if not path.lower().endswith('.csv'): path += '.csv'
        target = Path(path)
        if target.exists() and not self.ask('覆盖模板', '目标文件已存在，确认覆盖？'): return
        self.start('导出 AI 补全模板', lambda control,progress: booklist.export_ai_template(self.ws,ids,target,control,progress),
            lambda count: QMessageBox.information(self,'AI 模板已导出',f'已导出 {count} 本：{target}\n交给 AI 时要求绝对保留系统编号（BK_…）。完成后用“导入表格回填资料”，软件只按系统编号精确匹配并预览。'), refresh=False)

    def organize_ids(self):
        choice, ok = QInputDialog.getItem(self, '选择处理范围', '仅操作指定范围，不自动处理整个书库',
            [f'当前选中：{len(self.selected_ids())} 本', f'全部筛选结果：{len(self.all_filtered_ids())} 本'], 0, False)
        if not ok: return []
        ids = self.all_filtered_ids() if choice.startswith('全部') else self.selected_ids()
        if not ids: self.show_error('没有图书，请先选中图书或选择全部筛选结果')
        return ids

    def edit_titles(self):
        ids = self.organize_ids()
        if not ids: return
        books = [self.ws.book(bid) for bid in ids]
        dialog = QDialog(self); dialog.setWindowTitle('批量精简书名：预览后可逐行修改'); dialog.resize(1100, 700)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('这里只改书库书名。需要实际文件请再点“导出改名文件”。已上传文件不会重命名。'))
        rule = QComboBox()
        for label, value in [('仅去除已有副标题和营销文字（保守）', ''), ('删除第一个冒号及其后文字', 'colon'), ('删除第一个破折号及其后文字', 'dash'), ('删除第一个冒号或破折号及其后文字', 'both')]: rule.addItem(label, value)
        layout.addWidget(rule)
        layout.addWidget(QLabel('冒号可能属于正式书名，例如“龙脉：千里大运河”。请核对新书名；不想改的取消勾选。'))
        grid = QTableWidget(len(books), 3); grid.setHorizontalHeaderLabels(['应用', '原书名', '新书名（双击打开大窗口）'])
        grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        grid.verticalHeader().setDefaultSectionSize(54)
        grid.setStyleSheet('QTableWidget { font-size: 15px; }')
        grid.setColumnWidth(0, 65)
        for row, book in enumerate(books):
            checked = QTableWidgetItem(); checked.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); checked.setCheckState(Qt.Checked); grid.setItem(row, 0, checked)
            old = QTableWidgetItem(book['metadata'].get('title', '')); old.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable); grid.setItem(row, 1, old)
        def generate():
            for row, book in enumerate(books):
                meta = book['metadata']
                grid.setItem(row, 2, QTableWidgetItem(batch_edit.clean_title(meta.get('title', ''), meta.get('subtitle', ''), True, rule.currentData())))
        generate(); rule.currentIndexChanged.connect(generate)
        def edit_row(row, column):
            if column != 2: return
            updated = self.edit_title_text(grid.item(row, 1).text(), grid.item(row, 2).text(), dialog)
            if updated is not None:
                grid.item(row, 2).setText(updated)
                grid.item(row, 2).setToolTip(updated)
        grid.cellDoubleClicked.connect(edit_row)
        grid.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); grid.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch); layout.addWidget(grid)
        edit_button = QPushButton('大窗口编辑当前行的新书名')
        edit_button.clicked.connect(lambda: edit_row(grid.currentRow(), 2) if grid.currentRow() >= 0 else None)
        layout.addWidget(edit_button)
        self.highlight_checks(grid, layout)
        go = QPushButton('确认应用勾选行（清空对应副标题，可撤销）'); go.clicked.connect(dialog.accept); layout.addWidget(go)
        if dialog.exec() != QDialog.Accepted: return
        try:
            updates = []
            for row, book in enumerate(books):
                if grid.item(row, 0).checkState() == Qt.Checked:
                    updates.extend(batch_edit.preview([book], {'title': grid.item(row, 2).text().strip(), 'subtitle': ''}))
        except ValueError as exc: self.show_error(str(exc)); return
        self.start('精简书名', lambda control, progress: batch_edit.apply(self.ws, updates, control),
            lambda count: QMessageBox.information(self, '书名已更新', f'已修改 {count} 本。点击顶部“导出改名文件”可得到实际改名文件；原书与云端文件不变。'))

    def edit_title_text(self, original, current, parent=None):
        editor = QDialog(parent or self); editor.setWindowTitle('编辑完整书名'); editor.resize(920, 620)
        editor.setMinimumSize(640, 460)
        layout = QVBoxLayout(editor)
        layout.addWidget(QLabel('原书名（完整显示，可选中复制）'))
        old = QTextEdit(); old.setObjectName('original_title'); old.setReadOnly(True); old.setPlainText(original)
        old.setStyleSheet('QTextEdit { font-size: 17px; padding: 12px; }'); layout.addWidget(old, 1)
        layout.addWidget(QLabel('新书名（可换行查看；保存时合并成一行）'))
        new = QTextEdit(); new.setObjectName('edited_title'); new.setAcceptRichText(False); new.setPlainText(current)
        new.setStyleSheet('QTextEdit { font-size: 20px; padding: 12px; }'); layout.addWidget(new, 2)
        hint = QLabel('保存只更新本次预览，最后仍需点击“确认应用勾选行”才能写入书库。'); hint.setWordWrap(True); layout.addWidget(hint)
        bar = QHBoxLayout(); cancel = QPushButton('取消，不修改'); save = QPushButton('保存到预览')
        cancel.clicked.connect(editor.reject)
        def accept():
            if not new.toPlainText().strip():
                hint.setText('书名不能为空，请填写新书名。'); new.setFocus(); return
            editor.accept()
        save.clicked.connect(accept); bar.addWidget(cancel); bar.addWidget(save); layout.addLayout(bar)
        new.setFocus()
        if editor.exec() != QDialog.Accepted: return None
        return ' '.join(new.toPlainText().splitlines()).strip()

    def edit_categories(self):
        ids = self.organize_ids()
        if not ids: return
        books = [self.ws.book(bid) for bid in ids]
        rules = self.ws.setting('category_rules', DEFAULT_RULES)
        categories = sorted(set(rules) | {b['metadata'].get('main_category', '') for b in books})
        dialog = QDialog(self); dialog.setWindowTitle('批量分类：选择、修改、人工确认'); dialog.resize(1150, 720)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('逐本修改主/子分类，或统一填入勾选行。关键词建议不是ISBN核验；不能确定的不要勾选确认。'))
        bar = QHBoxLayout(); main = QComboBox(); main.setEditable(True); main.addItems(categories)
        sub = QLineEdit(); sub.setPlaceholderText('子分类（可留空）'); bar.addWidget(main); bar.addWidget(sub)
        fill = QPushButton('将此分类填入勾选行'); bar.addWidget(fill)
        suggest = QPushButton('为勾选行生成关键词建议'); bar.addWidget(suggest); layout.addLayout(bar)
        grid = QTableWidget(len(books), 5); grid.setHorizontalHeaderLabels(['应用', '书名', '主分类（可编辑）', '子分类（可编辑）', '依据 / 提醒'])
        for row, book in enumerate(books):
            meta = book['metadata']
            checked = QTableWidgetItem(); checked.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); checked.setCheckState(Qt.Checked); grid.setItem(row, 0, checked)
            for col, value in [(1, meta.get('title', '')), (4, meta.get('classification_evidence', '请人工核对'))]:
                cell = QTableWidgetItem(value); cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable); cell.setToolTip(value); grid.setItem(row, col, cell)
            picker = QComboBox(); picker.setEditable(True); picker.addItems(categories); picker.setCurrentText(meta.get('main_category', '')); grid.setCellWidget(row, 2, picker)
            grid.setItem(row, 3, QTableWidgetItem(meta.get('subcategory', '')))
        def populate(use_suggestions=False):
            from .engine import classify
            for row, book in enumerate(books):
                if grid.item(row, 0).checkState() != Qt.Checked: continue
                if use_suggestions:
                    result = classify(Path('book.epub'), Path('.'), book['metadata'], rules)
                    candidates = result.get('classification_candidates', [])
                    if not candidates:
                        grid.item(row, 4).setText('没有明确关键词，保留原分类；请人工指定'); continue
                    grid.cellWidget(row, 2).setCurrentText(candidates[0]['name']); grid.item(row, 3).setText('')
                    grid.item(row, 4).setText('待核对：' + '；'.join(c['name'] + '（' + '、'.join(c['evidence']) + '）' for c in candidates))
                else:
                    grid.cellWidget(row, 2).setCurrentText(main.currentText().strip()); grid.item(row, 3).setText(sub.text().strip())
                    grid.item(row, 4).setText('人工批量指定，请核对')
        fill.clicked.connect(lambda: populate(False)); suggest.clicked.connect(lambda: populate(True))
        grid.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); grid.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        grid.setColumnWidth(0, 50); grid.setColumnWidth(2, 170); layout.addWidget(grid)
        self.highlight_checks(grid, layout)
        go = QPushButton('我已核对：保存并确认勾选行分类'); go.clicked.connect(dialog.accept); layout.addWidget(go)
        if dialog.exec() != QDialog.Accepted: return
        updates = []
        for row, book in enumerate(books):
            if grid.item(row, 0).checkState() != Qt.Checked: continue
            category = grid.cellWidget(row, 2).currentText().strip()
            if not category: self.show_error('《' + book['metadata'].get('title', '') + '》主分类为空，请填写或取消勾选'); return
            updates.extend(batch_edit.preview([book], {'main_category': category, 'subcategory': grid.item(row, 3).text().strip()}))
        if not self.ask('确认分类', '将勾选行标为人工已确认。你已核对这些分类，而不是仅依赖关键词建议？'): return
        self.start('批量分类', lambda control, progress: batch_edit.apply(self.ws, updates, control),
            lambda count: QMessageBox.information(self, '分类完成', f'已更新 {count} 本，可撤销。未改版权、原文件或已有网盘目录。'))

    def export_renamed(self):
        ids = self.organize_ids()
        if not ids: return
        destination = QFileDialog.getExistingDirectory(self, '选择另存目录；按当前书名和分类导出，不覆盖原书')
        if destination:
            self.start('导出改名文件', lambda control, progress: export_snapshot(self.ws, ids, Path(destination), control, progress),
                lambda result: QMessageBox.information(self, '已生成改名文件', str(result) + '\n网盘上传文件夹内是按新书名和分类生成的电子书；原文件未修改。异常文件请查看异常区。'))

    def undo_batch(self):
        if self.ask('撤销上次整批修改','将恢复本批修改前的资料；后续又有编辑的图书会阻止整批撤销。'):
            self.start('撤销批量修改',lambda control,progress: batch_edit.undo(self.ws,control),lambda count: QMessageBox.information(self,'已撤销',f'已恢复 {count} 本资料。'))

    def undo_book(self):
        if self.current_id:
            self.ws.undo(self.current_id); self.reload()

    def exclude_selected(self):
        ids = self.require_ids()
        with self.ws.connect() as db:
            for book_id in ids:
                db.execute("UPDATE books SET excluded=1-excluded WHERE book_id=?", (book_id,))
        self.reload()

    def delete_selected(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "任务进行中", "请先取消或等待当前任务完成，再删除图书。")
            return
        ids = self.require_ids()
        if not ids:
            return
        if not self.ask("确认从软件书库移除", f"将移除明确选中的 {len(ids)} 本图书及其本地上传记录。删除前自动备份资料；电脑原始电子书、网盘文件和网站内容均不删除。\n重新扫描原文件会再次导入；如只想暂停处理，请用“排除”。是否继续？"):
            return
        self.table.clearSelection()
        def work(control, progress):
            progress(f"正在备份资料并移除 {len(ids)} 本图书…")
            return self.ws.delete_books(ids, control)
        def done(result):
            QMessageBox.information(self, "已从软件书库移除", f"已移除 {result['deleted']} 本，原文件和云端内容不变。\n删除前资料备份：\n{result['backup']}")
        self.start("从软件书库移除", work, done)

    def on_table_context_menu(self, pos):
        if self.worker and self.worker.isRunning():
            return
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        if not self.table.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.table.selectRow(index.row())
        source = self.proxy.mapToSource(index)
        book = self.model.rows[source.row()]
        menu = QMenu(self)
        title = book["metadata"].get("title") or book["book_id"]
        menu.addSection(f"《{title}》")
        act_delete = QAction("从工作区移除", self)
        act_delete.triggered.connect(self.delete_selected)
        menu.addAction(act_delete)
        act_edit = QAction("补全/编辑资料", self)
        act_edit.triggered.connect(lambda: self.show_selected())
        menu.addAction(act_edit)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def select_cover(self):
        if not self.current_id:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择此书封面", "", "图片 (*.jpg *.jpeg *.png *.webp)")
        if not path:
            return
        book_id = self.current_id
        def work(control, progress):
            cover, version = make_cover(bounded_read(Path(path), 20 * 1024**2), self.ws.root, book_id)
            with self.ws.connect() as db:
                db.execute("UPDATE books SET cover_path=?,cover_version=?,revision=revision+1,updated_at=? WHERE book_id=?", (cover, version, now(), book_id))
            return "封面已保存到工作区；原图片不变"
        self.start("处理封面", work)

    def choose_scan(self):
        source = QFileDialog.getExistingDirectory(self, "选择 Calibre 导出目录或分类 EPUB 目录")
        if source:
            self.ws.set_setting("last_source", source)
            self.start("扫描书库", lambda control, progress: scan(self.ws, Path(source), control, progress))

    def resume_scan(self):
        source = self.ws.setting("last_source")
        if source:
            self.start("继续扫描（按哈希复用编号）", lambda control, progress: scan(self.ws, Path(source), control, progress))
        else:
            self.choose_scan()

    def export_selected(self):
        ids = self.require_ids()
        if not ids:
            return
        destination = QFileDialog.getExistingDirectory(self, "选择导出父目录；每次创建新快照")
        if destination:
            self.start("分类导出", lambda control, progress: export_snapshot(self.ws, ids, Path(destination), control, progress), lambda result: QMessageBox.information(self, "导出完成", str(result)))

    def export_uploaded(self):
        site_id = self.config().get("site_id", "")
        rows = self.ws.books()
        out = []
        for b in rows:
            quark = self.ws.result(b["book_id"], "quark") or {}
            baidu = self.ws.result(b["book_id"], "baidu") or {}
            site = self.ws.result(b["book_id"], "site:" + site_id) or {}
            if not (quark.get("share_url") or baidu.get("share_url") or site.get("status") == "ok"):
                continue
            m = b["metadata"]
            out.append([m.get("title", ""), m.get("author", ""), m.get("isbn", ""), m.get("main_category", ""), quark.get("share_url", ""), baidu.get("share_url", ""), site.get("resource_url", "") or ""])
        if not out:
            QMessageBox.information(self, "无已上传图书", "当前没有已传网盘或已同步网站的书。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存已上传清单", "已上传清单.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["书名", "作者", "ISBN", "分类", "夸克分享链接", "百度分享链接", "网站资源链接"])
            # 表格中的图书资料按文本导出，不让书名等被 Excel 当作公式执行。
            def text_cell(value):
                value = str(value or "")
                return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
            w.writerows([[text_cell(value) for value in row] for row in out])
        QMessageBox.information(self, "已导出", f"已导出 {len(out)} 本已上传图书到：\n{path}")

    def import_table(self):
        from .table_import import read_table, preview_updates, apply_updates
        path, _ = QFileDialog.getOpenFileName(self, "选择表格（CSV / XLSX）", "", "表格 (*.csv *.xlsx)")
        if not path:
            return
        modes = ["只补空字段（推荐）", "覆盖表格提供的非空字段"]
        mode, accepted = QInputDialog.getItem(self, "选择表格回填方式", "预检后还需确认提交：", modes, 0, False)
        if not accepted:
            return
        overwrite = mode == modes[1]
        def preview_work(control, progress):
            control.check()
            return preview_updates(self.ws.books(), read_table(Path(path)), overwrite=overwrite)
        def confirm(preview):
            details = "\n".join(f"第 {item['row']} 行：{item['message']}" for item in preview["issues"][:20])
            self.progress_log.append(details or "所有非空行预检通过")
            count = len(preview["updates"])
            if not count:
                QMessageBox.information(self, "没有可回填项", details or "表格没有可回填的有效字段")
                return
            if not self.ask("确认表格回填", f"有效行 {preview['total']}，可回填 {count} 本，跳过 {len(preview['issues'])} 行。\n只精确匹配，不自动合并同名版本。方式：{mode}。\n" + details + "\n是否提交？"):
                return
            def work(control, progress):
                control.check()
                return apply_updates(self.ws, preview, control)
            self.start("表格回填", work, lambda count: QMessageBox.information(self, "回填完成", f"已更新 {count} 本；修改在同一事务中提交，可逐本撤销。"))
        self.start("表格预检（不修改图书）", preview_work, confirm)

    def _read_table(self, path):
        from .table_import import read_table
        return read_table(path)

    @staticmethod
    def _norm_title(value):
        from .table_import import normalized
        return normalized(value)

    def save_settings(self):
        self.ws.set_setting("connections", {key: value.text().strip() for key, value in self.settings_fields.items()})
        persistent = True
        for key, field in self.secret_fields.items():
            if field.text():
                persistent = self.credentials.set(key, field.text()) and persistent
                field.clear()
        self.auth_status.setText("设置已保存。" + ("" if persistent else "　普通设置已保存；系统凭据库不可用，新凭据仅在本次运行有效。"))

    def save_rules(self):
        try:
            rules = json.loads(self.rules.toPlainText())
            if not isinstance(rules, dict) or any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(s, str) or not s for s in v) for k, v in rules.items()):
                raise ValueError()
            self.ws.set_setting("category_rules", rules)
            self.auth_status.setText("分类规则已保存；重扫不会覆盖人工锁定字段。")
        except Exception:
            self.show_error("分类规则应为“分类名: [关键词, ...]”的 JSON 对象")

    def save_mapping(self):
        try:
            mapping = json.loads(self.category_mapping.toPlainText())
            if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, list) or not 1 <= len(v) <= 2 or any(not isinstance(s, str) or not s.strip() for s in v) for k, v in mapping.items()):
                raise ValueError()
            self.ws.set_setting("site_category_mapping", mapping)
            self.auth_status.setText("网站分类对应已保存。本地/网盘目录不受影响，网站预检时会显示目标分类。")
        except Exception:
            self.show_error("分类对应格式不正确，请按示例填写")

    def site_info(self):
        config = self.config()
        def work(control, progress):
            client = SiteClient(self.ws, config, self.credentials)
            try:
                result = client.info(); self.ws.set_setting("site_categories", result); return result
            finally:
                client.close()
        def done(result):
            self.auth_status.setText(f"✅ 网站连接正常　网站编号：{result['site_id']}；已读取 {len(result['categories'])} 个分类。请核对本站编号与设置一致。")
        self.start("检查网站", work, done)

    def quark_check(self):
        def work(control, progress):
            info = quark_connector(self.ws, self.config()).get_user_info()
            return info
        def done(info):
            name = info.get("nickname") or info.get("user_name") or info.get("name") or "未知账号"
            cap = info.get("capacity") or info.get("total_space") or ""
            self.auth_status.setText(f"✅ 夸克已授权　账号：{name}　容量：{cap}　运行环境检查通过。")
        self.start("检查夸克授权", work, done)

    def quark_pick_folder(self):
        config = self.config()
        self.start("读取夸克目录", lambda control, progress: quark_list_folders(self.ws, config), self.show_quark_folders)

    def show_quark_folders(self, folders):
        dlg = QDialog(self); dlg.setWindowTitle("选择夸克目标目录"); dlg.resize(420, 320)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("选择目标目录；分类子目录会按需创建："))
        lw = QListWidget()
        for name, fid in [("（根目录）", "0"), *folders]:
            item = QListWidgetItem(f"{name}　{fid}")
            item.setData(Qt.UserRole, str(fid))
            lw.addItem(item)
        v.addWidget(lw)
        bb = QHBoxLayout()
        ok = QPushButton("确定"); ok.clicked.connect(dlg.accept)
        cancel = QPushButton("取消"); cancel.clicked.connect(dlg.reject)
        bb.addWidget(ok); bb.addWidget(cancel); v.addLayout(bb)
        if dlg.exec() == QDialog.Accepted and lw.currentItem():
            fid = lw.currentItem().data(Qt.UserRole)
            self.settings_fields["quark_parent"].setText(fid)
            self.auth_status.setText(f"已填入夸克目标目录编号：{fid}（记得点「保存连接设置」生效）。")

    def install_quark(self):
        if not self.ask("安装官方连接器", "将从夸克官方配置地址下载连接器到本地工作区。不会上传图书或伪造运行环境。是否继续？"):
            return
        def work(control, progress):
            from app.core.config import Settings
            from app.services.cloud_uploads import install_quark_connector
            root = Path(self.config().get("connector_runtime") or self.ws.root / "connector-runtime")
            install_quark_connector(Settings(local_storage_root=root, cloud_upload_worker_enabled=False))
            return "连接器已安装；仍需通过运行环境检查及账号授权。"
        self.start("安装官方连接器", work, lambda text: self.auth_status.setText(text))

    def quark_authorize(self):
        if self.ask("授权夸克", "将调用官方连接器授权流程；本操作不会上传图书。是否继续？"):
            self.start("夸克授权", lambda control, progress: quark_connector(self.ws, self.config()).authorize(), lambda _: self.auth_status.setText("✅ 夸克已授权。"))

    def cloud_action(self, kind, resume=None):
        ids, config, provider = (resume["ids"], self.config(), resume["provider"]) if resume else (self.require_ids(), self.config(), self.provider.currentData())
        if not ids or not self.ask("确认真实上传", f"将处理 {len(ids)} 本选中的图书，向配置的 Cloudflare / 网盘写入对应内容。只处理你确认拥有使用权限的资源。是否继续？"):
            return
        self.ws.set_setting("last_cloud_task", {"kind": kind, "ids": ids, "provider": provider})
        def work(control, progress):
            job = self.ws.start_job("cloud", {"kind": kind, "ids": ids, "provider": provider})
            try:
                for book_id in ids:
                    book = self.ws.book(book_id)
                    if not book or book["excluded"] or book["status"] in {"failed", "blocked"}:
                        raise ValueError("选中图书含已排除或异常文件，请先处理")
                    meta = book["metadata"]
                    if meta.get("rights_review_status") != "confirmed" or not meta.get("source_reference"):
                        raise ValueError("上传前请逐本或批量确认版权类别和来源")
                connector = quark_connector(self.ws, config) if provider == "quark" and kind in {"book", "all"} else None
                for book_id in ids:
                    control.check(); book = self.ws.book(book_id)
                    if kind in {"cover", "all"}:
                        progress("上传并验证封面：" + book["metadata"]["title"])
                        upload_cover(self.ws, book_id, config, self.credentials)
                    if kind in {"book", "all"}:
                        progress("上传网盘：" + book["metadata"]["title"])
                        upload_book(self.ws, book_id, provider, config, self.credentials, control, connector=connector)
                self.ws.finish_job(job, "succeeded", {"count": len(ids), "provider": provider})
                if kind == "all":
                    client = SiteClient(self.ws, config, self.credentials)
                    try:
                        return client.preview(ids)
                    finally:
                        client.close()
                return f"处理完成，共 {len(ids)} 本。上传完成不等于网站已经发布。"
            except Exception as exc:
                self.ws.finish_job(job, "cancelled" if isinstance(exc, Cancelled) else "failed", {"error": batch_edit.error_message(exc), "ids": ids, "kind": kind, "provider": provider})
                raise
        self.start("云端任务", work, self.load_preview if kind == "all" else None)

    def retry_cloud(self):
        previous = self.ws.setting("last_cloud_task")
        if previous:
            self.cloud_action(previous["kind"], resume=previous)
        else:
            self.show_error("没有可以重试的云端任务")

    def manual_link(self):
        from PySide6.QtWidgets import QInputDialog
        if not self.current_id:
            return
        value, ok = QInputDialog.getText(self, "回填独立分享链接", "粘贴这一本书的分享链接（不要使用整类文件夹的公共链接）")
        if not ok or not value:
            return
        try:
            from app.providers import registry
            parsed = registry.recognize(value)
            self.ws.save_result(self.current_id, parsed.provider_code, {"state": "shared", "share_url": parsed.normalized_url, "extract_code": parsed.extract_code, "source": "manual"})
            self.reload(); self.show_selected()
        except ValueError as exc:
            self.show_error(str(exc))

    def site_preview(self):
        ids, config = self.require_ids(), self.config()
        if not ids:
            return
        def work(control, progress):
            client = SiteClient(self.ws, config, self.credentials)
            try:
                return client.preview(ids)
            finally:
                client.close()
        self.start("网站预检（不提交资源）", work, self.load_preview)

    def load_preview(self, result):
        self.preview = result
        rows = result["rows"]
        self.preview_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            selected = QCheckBox(); selected.setChecked(not row["error"] and row["action"] != "choose"); selected.setEnabled(not row["error"])
            self.preview_table.setCellWidget(i, 0, selected)
            self.preview_table.setItem(i, 1, QTableWidgetItem(row["title"]))
            combo = QComboBox()
            if row["action"] == "update":
                candidate = row["candidates"][0]; combo.addItem("更新已绑定：" + candidate["title"], ("update", candidate["id"]))
            else:
                combo.addItem("新建独立版本（草稿）", ("create", None))
                for candidate in row["candidates"]:
                    combo.addItem("绑定：" + candidate["title"] + " / " + (candidate["author"] or "作者未填") + " / " + candidate["resource_code"], ("bind", candidate["id"]))
            self.preview_table.setCellWidget(i, 2, combo)
            self.preview_table.setItem(i, 3, QTableWidgetItem(row["error"] or ("有旧库候选，请核对版本" if row["action"] == "choose" else "可提交")))
        self.tabs.setCurrentIndex(3)

    def show_preview_details(self):
        row = self.preview_table.currentRow()
        if self.preview and 0 <= row < len(self.preview["rows"]):
            self.preview_details.setPlainText(json.dumps(self.preview["rows"][row], ensure_ascii=False, indent=2))

    def restore_preview(self):
        saved = self.ws.setting("last_site_preview")
        if saved and saved["site_id"] == self.config().get("site_id") and saved["site_url"] == self.config().get("site_url", "").rstrip("/"):
            self.load_preview(saved["preview"])
        else:
            self.show_error("没有与当前站点相符的历史预检")

    def site_commit(self):
        if not self.preview:
            return
        choices = []
        for i, row in enumerate(self.preview["rows"]):
            if self.preview_table.cellWidget(i, 0).isChecked():
                action, rid = self.preview_table.cellWidget(i, 2).currentData()
                choices.append({"book_id": row["book_id"], "action": action, "resource_id": rid, "overwrite": self.overwrite.isChecked(), "publish": self.publish.isChecked()})
        if not choices or not self.ask("确认写入网站", f"将提交 {len(choices)} 本图书。自动发布：{'是，链接检测通过才发布' if self.publish.isChecked() else '否，新书保持草稿'}。是否继续？"):
            return
        config, preview = self.config(), self.preview
        def work(control, progress):
            client = SiteClient(self.ws, config, self.credentials)
            try:
                return client.commit(preview, choices, control, progress)
            finally:
                client.close()
        def done(result):
            self.preview_details.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            QMessageBox.information(self, "已收到网站回执", "请查看回执中的逐本结果。同步成功、链接有效与已发布是不同状态。")
        self.start("网站提交", work, done)

    def backup(self):
        from .maintenance import backup_workspace
        destination, _ = QFileDialog.getSaveFileName(self, "选择新备份文件", "整理工作区备份.zip", "备份 (*.zip)")
        if destination:
            self.start("备份工作区", lambda control, progress: backup_workspace(self.ws, Path(destination)))

    def restore_backup(self):
        from .maintenance import restore_workspace
        source, _ = QFileDialog.getOpenFileName(self, "选择工作区备份", "", "备份 (*.zip)")
        if not source:
            return
        parent = QFileDialog.getExistingDirectory(self, "选择恢复父目录（会创建新目录，不覆盖现有数据）")
        if parent:
            destination = Path(parent) / ("恢复工作区_" + now()[:19].replace(":", "-"))
            self.start("恢复备份", lambda control, progress: restore_workspace(Path(source), destination), lambda result: QMessageBox.information(self, "恢复完成", f"恢复位置：{result}\n点击任务中心的“切换工作区”，选择此目录即可打开。原工作区没有被覆盖。"))

    def switch_workspace(self):
        destination = QFileDialog.getExistingDirectory(self, "选择其他或已恢复的工作区目录")
        if not destination:
            return
        from .safeio import filesystem_path
        if filesystem_path(Path(destination).resolve()) == self.ws.root:
            return
        if self.ask("切换工作区", "将打开选中的工作区并关闭当前窗口。当前资料不会删除，未保存的输入不会带入新工作区。是否继续？"):
            executable = sys.executable
            args = ["--workspace", destination]
            if not getattr(sys, "frozen", False):
                pythonw = Path(executable).with_name("pythonw.exe")
                executable = str(pythonw) if pythonw.exists() else executable
                args = ["-m", "ebook_organizer", *args]
            subprocess.Popen([executable, *args], cwd=Path(__file__).resolve().parents[1], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.close()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel(); QMessageBox.information(self, "正在安全停止", "已请求取消，请等当前操作结束后再关闭，以免丢失进度。")
            event.ignore(); return
        self.lock.unlock(); event.accept()

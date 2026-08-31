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
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QKeySequence, QPixmap, QAction, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter, QTabWidget, QTableView, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QScrollArea)

from .connections import Credentials, SiteClient, quark_connector, quark_list_folders, upload_book, upload_cover
from .covers import make_cover
from .engine import DEFAULT_RULES, export_snapshot, scan
from .pipeline import run_full_pipeline, format_summary
from .safeio import Cancelled, Control, bounded_read
from .workspace import Workspace, now


STATUS = {"passed": "检测通过", "warning": "有提示", "failed": "异常", "blocked": "已阻止", "confirmed": "已确认", "pending": "待处理", "suggested": "待确认建议", "running": "进行中", "succeeded": "已完成", "cancelled": "已取消", "interrupted": "已中断"}

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
        if not index.isValid():
            return None
        book = self.rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            meta = book["metadata"]
            st = book.get("_status", {})
            if col == 7:
                return "✓" if st.get("cover") else "—"
            if col == 8:
                return "✓" if st.get("netdisk") else "—"
            if col == 9:
                return "✓" if st.get("site") else "—"
            values = [meta.get("title"), meta.get("author"), meta.get("main_category"), meta.get("subcategory"), meta.get("isbn"),
                      "已排除" if book["excluded"] else STATUS.get(book["status"], book["status"]),
                      STATUS.get(meta.get("classification_status"), "待处理"), book["book_id"]]
            return str(values[col] or "—")
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
        self.f = {"cover": "all", "isbn": "all", "author": "all", "category": "", "status": ""}

    def set_criteria(self, **kw):
        self.f.update(kw)
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        book = self.sourceModel().rows[row]
        st = book.get("_status", {})
        f = self.f
        if f["cover"] == "yes" and not st.get("cover"):
            return False
        if f["cover"] == "no" and st.get("cover"):
            return False
        if f["isbn"] == "yes" and not (book["metadata"].get("isbn") or "").strip():
            return False
        if f["isbn"] == "no" and (book["metadata"].get("isbn") or "").strip():
            return False
        if f["author"] == "yes" and not (book["metadata"].get("author") or "").strip():
            return False
        if f["author"] == "no" and (book["metadata"].get("author") or "").strip():
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
            self.failure.emit(str(exc) if isinstance(exc, ValueError) else f"操作未完成：{type(exc).__name__}。请核对配置、权限及网络后重试。")


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
        self.setWindowTitle("电子书整理工作台")
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
            QTableView::item {{padding:6px;border-bottom:1px solid {C['hairline']};}} QTableView::item:selected {{background:{C['surface_alt']};color:{C['ink']};}}
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
        # 筛选栏
        filter_bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("搜索书名 / 作者 / 分类 / ISBN")
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        filter_bar.addWidget(self.search, 2)
        self.f_cover = QComboBox(); self.f_cover.addItems(["封面:全部", "封面:有", "封面:无"])
        self.f_isbn = QComboBox(); self.f_isbn.addItems(["ISBN:全部", "ISBN:有", "ISBN:无"])
        self.f_author = QComboBox(); self.f_author.addItems(["作者:全部", "作者:有", "作者:无"])
        self.f_category = QComboBox(); self.f_category.addItem("分类:全部")
        self.f_status = QComboBox(); self.f_status.addItems(["状态:全部", "状态:已传网盘", "状态:已同步网站", "状态:已排除", "状态:异常", "状态:待处理"])
        for w, key in ((self.f_cover, "cover"), (self.f_isbn, "isbn"), (self.f_author, "author")):
            w.currentIndexChanged.connect(lambda *_: self._apply_filters())
        self.f_category.currentTextChanged.connect(lambda *_: self._apply_filters())
        self.f_status.currentTextChanged.connect(lambda *_: self._apply_filters())
        for w in (self.f_cover, self.f_isbn, self.f_author, self.f_category, self.f_status):
            filter_bar.addWidget(w)
        layout.addLayout(filter_bar)
        # 批量操作栏
        batch_bar = QHBoxLayout()
        self.button("全选筛选结果", self.select_all_filtered, batch_bar, "alternate")
        self.button("一键上传选中(全自动)", lambda: self.start_pipeline(False, self.selected_ids()), batch_bar)
        self.button("一键上传全部已筛选(全自动)", lambda: self.start_pipeline(False, self.all_filtered_ids()), batch_bar)
        self.button("导入表格回填资料", self.import_table, batch_bar, "alternate")
        self.button("导出已上传清单", self.export_uploaded, batch_bar, "alternate")
        self.del_button = self.button("删除选中", self.delete_selected, batch_bar, "error")
        layout.addLayout(batch_bar)
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
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_context_menu)
        QShortcut(QKeySequence.Delete, self.table, activated=self.delete_selected)
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
        self.rights = QComboBox()
        for title, code in [("尚未确认", ""), ("已获授权", "authorized"), ("公版", "public_domain"), ("开放许可", "open_license")]:
            self.rights.addItem(title, code)
        form.addRow("版权状态", self.rights); detail_layout.addLayout(form)
        self.description = QTextEdit(); self.description.setPlaceholderText("图书简介"); self.description.setMaximumHeight(100)
        detail_layout.addWidget(self.description)
        editbar = QHBoxLayout()
        self.button("保存此书", self.save_book, editbar)
        self.button("撤销上次修改", self.undo_book, editbar)
        self.button("选择封面", self.select_cover, editbar)
        detail_layout.addLayout(editbar)
        self.button("将分类和授权应用到选中图书", self.bulk_edit, detail_layout)
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
        pause = QPushButton("暂停 / 继续"); pause.clicked.connect(self.pause)
        cancel = QPushButton("取消当前任务"); cancel.clicked.connect(self.cancel)
        self.button("刷新任务和异常", self.reload_tasks, bar, "alternate")
        self.button("重试上次云端任务", self.retry_cloud, bar, "alternate")
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
        self.progress_log = QTextEdit(); self.progress_log.setReadOnly(True); layout.addWidget(self.progress_log, 1)
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
        info = QLabel("全自动流水线：对选中的书（或全书），依次跑「分类自动确认 → 版权批量申报 → 封面上 R2 → 传网盘(夸克) → 同步网站」。\n"
                      "已完成的书自动跳过（断点续传）；未过闸门的书记入“待处理”队列，不会中断流程。进度与结果见“任务与异常”页签。\n"
                      "在「书库整理」页用筛选器挑好资料齐全的书，点「一键上传选中 / 全部已筛选」即可。")
        info.setWordWrap(True); layout.addWidget(info)
        form = QFormLayout()
        self.pipeline_provider = QComboBox(); self.pipeline_provider.addItem("夸克网盘", "quark"); self.pipeline_provider.addItem("百度网盘", "baidu")
        form.addRow("目标网盘", self.pipeline_provider)
        self.pipeline_rights = QComboBox()
        for title, code in [("已获授权 (authorized)", "authorized"), ("公版 (public_domain)", "public_domain"), ("开放许可 (open_license)", "open_license")]:
            self.pipeline_rights.addItem(title, code)
        form.addRow("版权默认状态", self.pipeline_rights)
        self.pipeline_source = QLineEdit("自有/已购电子书资源"); form.addRow("来源说明（批量申报用）", self.pipeline_source)
        self.pipeline_auto_class = QCheckBox("自动确认有候选的分类（关键词命中即确认）"); self.pipeline_auto_class.setChecked(True); form.addRow(self.pipeline_auto_class)
        self.pipeline_auto_rights = QCheckBox("对未确认的书批量申报上述版权策略"); self.pipeline_auto_rights.setChecked(True); form.addRow(self.pipeline_auto_rights)
        self.pipeline_publish = QCheckBox("同步时链接校验通过后自动发布"); form.addRow(self.pipeline_publish)
        self.pipeline_force = QCheckBox("忽略已完成标记，强制重做（用于补封面/改元数据）"); form.addRow(self.pipeline_force)
        self.pipeline_batch = QSpinBox(); self.pipeline_batch.setRange(1, 500); self.pipeline_batch.setValue(20); form.addRow("网站每批本数", self.pipeline_batch)
        self.pipeline_limit = QSpinBox(); self.pipeline_limit.setRange(0, 100000); self.pipeline_limit.setValue(0); self.pipeline_limit.setSpecialValueText("全部"); form.addRow("本数限制（0=全部，冒烟用）", self.pipeline_limit)
        layout.addLayout(form)
        bar = QHBoxLayout()
        self.button("▶ 开始全自动流水线（全书）", lambda: self.start_pipeline(False), bar)
        self.button("仅预演 (dry-run)", lambda: self.start_pipeline(True), bar, "alternate")
        layout.addLayout(bar)
        layout.addWidget(QLabel("大量书会跑很久，可随时在“任务与异常”页签暂停/取消；已完成的不会重做。运行前请确认“连接与分类设置”里的 R2 与网站同步授权已配置。"))
        self.tabs.addTab(page, "全自动流水线")

    def start_pipeline(self, dry, book_ids=None):
        config = self.config()
        opts = dict(provider=self.pipeline_provider.currentData(), publish=self.pipeline_publish.isChecked(),
                    auto_classify=self.pipeline_auto_class.isChecked(), auto_rights=self.pipeline_auto_rights.isChecked(),
                    rights_status=self.pipeline_rights.currentData(), source_reference=self.pipeline_source.text().strip() or "自有/已购电子书资源",
                    dry_run=dry, batch=self.pipeline_batch.value(), limit=self.pipeline_limit.value(), force=self.pipeline_force.isChecked())
        if book_ids:
            opts["book_ids"] = book_ids
        scope = f"（选中 {len(book_ids)} 本）" if book_ids else "（全书）"
        if not dry and not self.ask("确认真实全自动上传" + scope, "将按上述设置对" + scope + "执行：分类确认、版权申报、封面上 R2、传网盘、同步网站。只处理你确认拥有使用权限的资源。是否继续？"):
            return

        def work(control, progress):
            return run_full_pipeline(self.ws, config, self.credentials, opts, control, progress)

        def done(summary):
            QMessageBox.information(self, "全自动流水线完成", format_summary(summary))
        self.start("全自动流水线" + scope + ("（预演）" if dry else ""), work, done)

    def selected_ids(self):
        return [self.model.rows[self.proxy.mapToSource(index).row()]["book_id"] for index in self.table.selectionModel().selectedRows()]

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
            category="" if self.f_category.currentText() == "分类:全部" else self.f_category.currentText(),
            status={"状态:全部": "", "状态:已传网盘": "netdisk", "状态:已同步网站": "site", "状态:已排除": "excluded", "状态:异常": "failed", "状态:待处理": "pending"}[self.f_status.currentText()],
        )

    def require_ids(self):
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, "请选择图书", "请在书库列表选择一行或多行。")
        return ids

    def ask(self, title, message):
        return QMessageBox.question(self, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def show_error(self, error):
        self.progress_log.append("⚠ " + error)
        QMessageBox.warning(self, "操作提示", error)

    def start(self, label, function, done=None):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "任务进行中", "请等待当前任务完成，或在任务中心暂停/取消。")
            return
        self.progress_log.append("\n▶ " + label)
        for action in self.actions:
            action.setEnabled(False)
        self.task_status.setText(label); self._elapsed = 0; self.elapsed_label.setText("已运行 0s")
        self.progress_bar.setRange(0, 0); self.timer.start(1000)
        self.worker = Worker(function)
        self.worker.progress.connect(self.progress_log.append)
        self.worker.progress.connect(self.statusBar().showMessage)
        self.worker.failure.connect(self.show_error)
        self.worker.success.connect(done or (lambda result: self.progress_log.append("完成：" + str(result))))
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def finished(self):
        self.timer.stop(); self.progress_bar.setRange(0, 1); self.progress_bar.setValue(1)
        self.task_status.setText("空闲")
        for action in self.actions:
            action.setEnabled(True)
        self.reload(); self.reload_tasks()
        self.statusBar().showMessage("任务结束；可在任务中心查看结果。原文件未修改。")

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
        selected = self.selected_ids()
        site_id = self.config().get("site_id", "")
        rows = self.ws.books(issues_only=False)
        cats = set()
        for b in rows:
            st = {
                "cover": bool(b.get("cover_path")),
                "netdisk": bool((self.ws.result(b["book_id"], "quark") or {}).get("share_url")),
                "site": (self.ws.result(b["book_id"], "site:" + site_id) or {}).get("status") == "ok",
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
        if selected:
            from PySide6.QtCore import QItemSelectionModel
            for index, book in enumerate(self.model.rows):
                if book["book_id"] in selected:
                    self.table.selectionModel().select(self.proxy.mapFromSource(self.model.index(index, 0)), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.count_label.setText(f"当前 {len(rows)} 本唯一内容　｜　筛选后 {self.proxy.rowCount()} 本　｜　按住 Ctrl / Shift 可多选；重复扫描按内容识别，不按书名合并")

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
            return
        self.current_id = ids[0]
        book = self.ws.book(ids[0]); meta = book["metadata"]
        for key, field in self.fields.items():
            field.setText(str(meta.get(key) or ""))
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
        st = book.get("_status", {})
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
        year = changes.get("publish_year")
        if year and (not year.isdigit() or not 1 <= int(year) <= 9999):
            self.show_error("出版年份应为有效整数，不确定时可以留空"); return
        changes["publish_year"] = int(year) if year else None
        changes.update(description=self.description.toPlainText(), copyright_status=self.rights.currentData(), rights_review_status="confirmed" if self.rights.currentData() and changes["source_reference"] else "pending", classification_status="confirmed" if changes["main_category"] else "pending")
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

    def bulk_edit(self):
        ids = self.require_ids()
        if not ids or not self.ask("确认批量修改", "将右侧分类及版权/来源应用到选中的所有图书？请确保这些图书的授权范围一致。"):
            return
        main = self.fields["main_category"].text().strip(); source = self.fields["source_reference"].text().strip()
        for book_id in ids:
            self.ws.edit(book_id, {"main_category": main, "subcategory": self.fields["subcategory"].text().strip(), "classification_status": "confirmed" if main else "pending", "copyright_status": self.rights.currentData(), "source_reference": source, "rights_review_status": "confirmed" if self.rights.currentData() and source else "pending"})
        self.reload()

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
        ids = self.require_ids()
        if not ids:
            return
        if not self.ask("确认从工作区移除", f"将彻底删除选中的 {len(ids)} 本书（含源文件记录、封面与上传结果）。原电子书文件不会被删除。此操作不可撤销，是否继续？"):
            return
        def work(control, progress):
            total = len(ids)
            chunk = 100
            deleted = 0
            for i in range(0, total, chunk):
                control.check()
                batch = ids[i:i + chunk]
                self.ws.delete_books(batch)
                deleted += len(batch)
                progress(f"已删除 {deleted}/{total} 本...")
            return f"已删除 {total} 本书"
        self.start("从工作区移除", work)

    def on_table_context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
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
            net = self.ws.result(b["book_id"], "quark") or {}
            site = self.ws.result(b["book_id"], "site:" + site_id) or {}
            if not (net.get("share_url") or site.get("status") == "ok"):
                continue
            m = b["metadata"]
            out.append([m.get("title", ""), m.get("author", ""), m.get("isbn", ""), m.get("main_category", ""), net.get("share_url", ""), site.get("resource_url", "") or ""])
        if not out:
            QMessageBox.information(self, "无已上传图书", "当前没有已传网盘或已同步网站的书。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存已上传清单", "已上传清单.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["书名", "作者", "ISBN", "分类", "网盘分享链接", "网站资源链接"])
            w.writerows(out)
        QMessageBox.information(self, "已导出", f"已导出 {len(out)} 本已上传图书到：\n{path}")

    def import_table(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择表格（CSV / XLSX）", "", "表格 (*.csv *.xlsx)")
        if not path:
            return
        try:
            rows = self._read_table(Path(path))
        except Exception as exc:
            self.show_error(f"读取表格失败：{exc}")
            return
        if not rows:
            self.show_error("表格为空或无法解析表头")
            return
        headers = rows[0]
        colmap = {}
        aliases = {"title": ("书名", "title", "图书名称", "名称"), "isbn": ("isbn", "ISBN", "书号"), "main_category": ("分类", "主分类", "category", "类别"), "author": ("作者", "author"), "translator": ("译者", "translator"), "publisher": ("出版社", "publisher"), "publish_year": ("出版年", "出版年份", "year"), "language": ("语言", "language"), "subtitle": ("副标题", "subtitle"), "description": ("简介", "描述", "description")}
        for field, names in aliases.items():
            for i, h in enumerate(headers):
                if str(h).strip().lower() in [n.lower() for n in names]:
                    colmap[field] = i; break
        # 建立书名/ISBN 索引
        books = self.ws.books()
        by_isbn = {}
        by_title = {}
        for b in books:
            m = b["metadata"]
            if m.get("isbn"):
                by_isbn[m["isbn"].strip()] = b["book_id"]
            by_title[self._norm_title(m.get("title", ""))] = b["book_id"]
        matched = updated = skipped = 0
        for r in rows[1:]:
            title = (r[colmap["title"]].strip() if "title" in colmap else "")
            isbn = (r[colmap["isbn"]].strip() if "isbn" in colmap else "")
            bid = by_isbn.get(isbn) if isbn else None
            if not bid and title:
                t = self._norm_title(title)
                bid = by_title.get(t)
                if not bid:
                    close = difflib.get_close_matches(t, list(by_title), n=1, cutoff=0.9)
                    if close:
                        bid = by_title[close[0]]
            if not bid:
                skipped += 1; continue
            matched += 1
            changes = {}
            for field in ("isbn", "main_category", "author", "translator", "publisher", "publish_year", "language", "subtitle", "description"):
                if field in colmap and len(r) > colmap[field] and str(r[colmap[field]]).strip():
                    val = str(r[colmap[field]]).strip()
                    if field == "publish_year" and not val.isdigit():
                        continue
                    changes[field] = val
            if not changes:
                continue
            if "main_category" in changes:
                changes["classification_status"] = "confirmed"
            try:
                self.ws.edit(bid, changes); updated += 1
            except ValueError:
                pass
        QMessageBox.information(self, "导入完成", f"表格共 {len(rows)-1} 行。\n匹配到 {matched} 本，已更新 {updated} 本，跳过（未匹配或无有效字段）{skipped} 行。\n匹配依据：ISBN 精确 → 书名精确 → 书名模糊(≥90%)。")
        self.reload()

    def _read_table(self, path: Path):
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig") as f:
                return [row for row in csv.reader(f)]
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        return [[(c.value if c.value is not None else "") for c in row] for row in ws.iter_rows()]

    @staticmethod
    def _norm_title(t):
        return re.sub(r"\s+", "", str(t).strip().lower())

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
        try:
            folders = quark_list_folders(self.ws, config)
        except Exception as exc:
            QMessageBox.information(self, "无法自动列出目录", str(exc))
            return
        dlg = QDialog(self); dlg.setWindowTitle("选择夸克目标目录"); dlg.resize(420, 320)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("选择一本书库顶层文件夹（分类子目录会由程序自动创建）："))
        lw = QListWidget()
        lw.addItem("（根目录，填写 0）")
        for name, fid in folders:
            lw.addItem(f"{name}　{fid}")
        v.addWidget(lw)
        bb = QHBoxLayout()
        ok = QPushButton("确定"); ok.clicked.connect(dlg.accept)
        cancel = QPushButton("取消"); cancel.clicked.connect(dlg.reject)
        bb.addWidget(ok); bb.addWidget(cancel); v.addLayout(bb)
        if dlg.exec() == QDialog.Accepted and lw.currentRow() >= 0:
            text = lw.currentItem().text()
            fid = "0" if lw.currentRow() == 0 else text.rsplit(" ", 1)[-1]
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
                self.ws.finish_job(job, "cancelled" if isinstance(exc, Cancelled) else "failed", {"error": type(exc).__name__, "ids": ids, "kind": kind, "provider": provider})
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
            self.show_selected()
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

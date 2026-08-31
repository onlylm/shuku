"""表格回填：先预检、精确匹配、整批事务；不联网，不自动合并同名版本。"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from itertools import islice

from .epub import isbn_valid
from .safeio import bounded_read


ALIASES = {
    "book_id": ("book_id", "编号", "图书编号", "系统编号"),
    "title": ("书名", "原书名", "title", "图书名称", "名称"),
    "new_title": ("新书名", "精简书名", "AI书名", "new_title"),
    "isbn": ("isbn", "书号"), "author": ("作者", "author"),
    "main_category": ("分类", "主分类", "category", "类别"),
    "subcategory": ("子分类", "subcategory"), "translator": ("译者", "translator"),
    "publisher": ("出版社", "publisher"), "publish_year": ("出版年", "出版年份", "year"),
    "language": ("语言", "language"), "subtitle": ("副标题", "subtitle"),
    "description": ("简介", "描述", "description"),
    "classification_verification": ("分类核验状态", "classification_verification"),
}


def cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalized(value):
    return re.sub(r"\s+", "", cell(value).casefold())


def read_table(path):
    content = bounded_read(path, 20 * 1024**2)
    if path.suffix.lower() == ".csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("gb18030")
        rows = list(islice(csv.reader(io.StringIO(text)), 20002))
    elif path.suffix.lower() == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            rows = list(islice(wb.active.iter_rows(values_only=True), 20002))
        finally:
            wb.close()
    else:
        raise ValueError("仅支持 CSV / XLSX")
    if len(rows) > 20001:
        raise ValueError("每次最多回填 20000 行，请分批处理")
    return rows


def preview_updates(books, rows, *, overwrite=False):
    if not rows:
        raise ValueError("表格为空")
    columns = {}
    for index, header in enumerate(rows[0]):
        for field, aliases in ALIASES.items():
            if normalized(header) in aliases:
                if field in columns:
                    raise ValueError("表头重复：" + cell(header))
                columns[field] = index
    if not {"book_id", "title", "isbn"} & columns.keys():
        raise ValueError("表格必须包含图书编号、书名或 ISBN 列")
    updates, issues = [], []
    total = 0
    for number, row in enumerate(rows[1:], 2):
        if not any(cell(value) for value in row):
            continue
        total += 1
        data = {key: cell(row[index]) if index < len(row) else "" for key, index in columns.items()}
        try:
            isbn = isbn_valid(data["isbn"]) if data.get("isbn") else ""
            if data.get("isbn") and not isbn:
                raise ValueError("ISBN 校验不通过")
            if data.get("book_id"):
                matches = [b for b in books if b["book_id"] == data["book_id"]]
            else:
                matches = [b for b in books if isbn and isbn_valid(cell(b["metadata"].get("isbn"))) == isbn]
                unique_isbn = len(matches) == 1
                if not matches and data.get("title"):
                    matches = [b for b in books if normalized(b["metadata"].get("title")) == normalized(data["title"])]
                if data.get("author") and not unique_isbn:
                    matches = [b for b in matches if not b["metadata"].get("author") or normalized(b["metadata"]["author"]) == normalized(data["author"])]
                if len(matches) > 1 and data.get("title"):
                    matches = [b for b in matches if normalized(b["metadata"].get("title")) == normalized(data["title"])]
            if len(matches) != 1:
                raise ValueError("同名或同 ISBN 有多个版本，请填写图书编号" if matches else "没有精确匹配；不自动模糊合并")
            book = matches[0]
            if book["excluded"] or book["status"] in {"failed", "blocked"}:
                raise ValueError("图书已排除或检测异常")
            changes = {key: value for key, value in data.items() if value and key not in {"book_id", "title", "new_title", "classification_verification"}}
            # 原书名只用于匹配，只有明确的“新书名”列可以更新标题。
            if data.get('new_title') and data['new_title'] != book['metadata'].get('title'):
                changes['title'] = data['new_title']
            if isbn:
                changes["isbn"] = isbn
            if changes.get("publish_year"):
                year = changes["publish_year"]
                if not year.isdigit() or not 1 <= int(year) <= 9999:
                    raise ValueError("出版年份应为 1–9999 的整数")
                changes["publish_year"] = int(year)
            # 默认仅补空。ISBN 若只是匹配键，不应造成无变化的修改或误覆盖。
            changes = {key: value for key, value in changes.items()
                       if value != book["metadata"].get(key)
                       and (key == 'title' or overwrite or not cell(book["metadata"].get(key)))}
            if changes.get("main_category"):
                changes["classification_status"] = "needs_review" if '推定' in data.get('classification_verification','') else "confirmed"
            if not changes:
                raise ValueError("没有可回填的非空字段")
            updates.append({"book_id": book["book_id"], "title": book["metadata"].get("title"),
                            "revision": book["revision"], "changes": changes, "row": number})
        except ValueError as exc:
            issues.append({"row": number, "message": str(exc)})
    counts = Counter(item["book_id"] for item in updates)
    for item in updates:
        if counts[item["book_id"]] > 1:
            issues.append({"row": item["row"], "message": "多行指向同一本书，已全部跳过，请合并为一行"})
    return {"total": total, "updates": [item for item in updates if counts[item["book_id"]] == 1],
            "issues": sorted(issues, key=lambda item: item["row"])}


def apply_updates(workspace, preview, control=None):
    # 预检后本地资料发生变化时停止，不用旧表格静默覆盖。
    with workspace.connect() as db:
        for item in preview["updates"]:
            if control:
                control.check()
            book = workspace.decode(db.execute("SELECT * FROM books WHERE book_id=?", (item["book_id"],)).fetchone())
            if not book or book["revision"] != item["revision"] or book["excluded"]:
                raise ValueError("预检后书库资料发生变化，请重新选择表格")
            workspace.edit(item["book_id"], item["changes"], connection=db)
        if control:
            control.check()
    return len(preview["updates"])

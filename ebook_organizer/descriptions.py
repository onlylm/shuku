"""离线补提取作者、出版社和简介，不改源文件，不猜测缺失资料。"""
import time
from pathlib import Path
from .epub import inspect_epub
from .safeio import Control, Cancelled, sha256_file, filesystem_path
from . import batch_edit


def extract(workspace, ids, control=None, progress=lambda _: None):
    control = control or Control()
    books, updates, report = [], [], []
    wanted = list(dict.fromkeys(ids))
    wanted_set = set(wanted)
    # 一次读取书库和来源，避免数千本时每本重复开关 SQLite 连接。
    available = {book['book_id']: book for book in workspace.books() if book['book_id'] in wanted_set}
    with workspace.connect() as db:
        rows = db.execute('SELECT book_id,path FROM sources ORDER BY seen_at DESC').fetchall()
    sources = {}
    for row in rows:
        if row['book_id'] in available and row['book_id'] not in sources:
            sources[row['book_id']] = filesystem_path(Path(row['path']))
    total = len(wanted)
    progress(f'开始提取图书资料：共 {total} 本；为保持界面流畅，每25本刷新一次')
    for index, bid in enumerate(wanted, 1):
        control.check()
        book = available.get(bid)
        if not book: continue
        books.append(book)
        title = book['metadata'].get('title', '')
        if index == 1 or index % 25 == 0 or index == total:
            progress(f'提取资料 {index}/{total}：{title}')
            # XML/ZIP 解析属于 CPU 任务；定期让出执行权给 Windows 界面线程。
            time.sleep(0.002)
        reason = ''
        if book['excluded'] or book['status'] in {'failed', 'blocked'}:
            reason = '已排除或检测异常，跳过'
        else:
            try:
                source = sources.get(bid)
                if not source or not source.is_file():
                    raise ValueError('找不到原电子书，请重新扫描')
                if sha256_file(source, control) != book['sha256']:
                    raise ValueError('源文件已改变，请重新扫描')
                result = inspect_epub(source, control)
                if result.status in {'failed', 'blocked'}:
                    reason = '源文件未通过检测，不回填'
                else:
                    changes, messages = {}, []
                    for field, label in [('author', '作者'), ('publisher', '出版社')]:
                        existing = str(book['metadata'].get(field) or '').strip()
                        extracted = str(result.metadata.get(field) or '').strip()
                        if existing:
                            messages.append(label + '已有，保留')
                        elif field in book['locked']:
                            messages.append(label + '已人工清空，保留')
                        elif extracted:
                            changes[field] = extracted
                            messages.append(label + '已提取')
                        else:
                            messages.append('原书无' + label)
                    existing_description = str(book['metadata'].get('description') or '').strip()
                    description = str(result.metadata.get('description') or '').strip()
                    if existing_description:
                        messages.append('简介已有，保留')
                    elif 'description' in book['locked']:
                        messages.append('简介已人工清空，保留')
                    elif len(description) > 30000:
                        messages.append('简介超过30000字，请人工整理')
                    elif description:
                        changes['description'] = description
                        changes['description_source'] = result.provenance.get('description', 'EPUB / 匹配的外部 OPF')
                        messages.append('简介已提取')
                    else:
                        messages.append('原书无简介')
                    if changes:
                        updates.extend(batch_edit.preview([book], changes))
                    reason = '；'.join(messages)
            except Cancelled:
                raise
            except (OSError, ValueError) as exc:
                reason = batch_edit.error_message(exc)
        report.append({'book_id': bid, 'title': title, 'message': reason})
    control.check()
    progress(f'资料提取完成：检查 {len(books)} 本，可回填 {len(updates)} 本')
    return {'books': books, 'updates': updates, 'report': report}

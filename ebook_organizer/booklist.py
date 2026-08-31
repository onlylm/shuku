"""只导出资料书单，不复制电子书，不导出本地路径或凭据。"""
import csv
import io
from .safeio import Control, atomic_bytes


def safe(value):
    text = str(value if value is not None else '')
    return "'" + text if text.startswith("'") or text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


def export(workspace, ids, path, control=None, progress=lambda _: None):
    control = control or Control()
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    writer.writerow(['图书编号', '书名', '副标题', '作者', 'ISBN', '主分类', '子分类', '出版社', '出版年份', '语言', '简介', '简介来源', '版权状态'])
    keys = ['title', 'subtitle', 'author', 'isbn', 'main_category', 'subcategory', 'publisher', 'publish_year', 'language', 'description', 'description_source', 'copyright_status']
    count = 0
    for bid in dict.fromkeys(ids):
        control.check()
        book = workspace.book(bid)
        if not book: raise ValueError('导出期间图书被删除，请重试')
        writer.writerow([safe(bid)] + [safe(book['metadata'].get(k, '')) for k in keys])
        count += 1
    control.check()
    atomic_bytes(path, stream.getvalue().encode('utf-8-sig'))
    progress(f'已导出 {count} 本书单')
    return count


def export_ai_template(workspace, ids, path, control=None, progress=lambda _: None):
    """导出按稳定图书编号回填的 AI 补全模板。"""
    control = control or Control()
    stream = io.StringIO(newline='')
    columns = ['系统编号', '原书名', '副标题', '新书名', '作者', '出版社', '简介', '需要AI处理', '处理规则']
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\r\n'); writer.writeheader()
    count = 0
    for bid in dict.fromkeys(ids):
        control.check(); book = workspace.book(bid)
        if not book: raise ValueError('导出期间图书被删除，请重试')
        meta = book['metadata']; tasks = []
        if not str(meta.get('author') or '').strip(): tasks.append('补作者')
        if not str(meta.get('publisher') or '').strip(): tasks.append('补出版社')
        if not str(meta.get('description') or '').strip(): tasks.append('补简介')
        tasks.append('判断并去除无意义副标题')
        row = {
            '系统编号': bid, '原书名': meta.get('title',''), '副标题': meta.get('subtitle',''),
            '新书名': meta.get('title',''), '作者': meta.get('author',''), '出版社': meta.get('publisher',''),
            '简介': meta.get('description',''), '需要AI处理': '；'.join(tasks),
            '处理规则': '系统编号绝对不改；软件仅按系统编号匹配；不确定的信息留空；新书名保留卷册/版本信息；简介只写可核实内容，不编造',
        }
        writer.writerow({key:safe(value) for key,value in row.items()}); count += 1
    control.check(); atomic_bytes(path, stream.getvalue().encode('utf-8-sig'))
    progress(f'已导出 {count} 本 AI 补全模板')
    return count

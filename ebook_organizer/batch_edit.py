"""可预览的本地元数据修改；不写原书、不调用网络。"""
import json
import re
from .safeio import Control


def language_label(value):
    return '中文' if str(value).lower() in {'zh', 'zho', 'chi', 'zh-cn', 'zh-hans', '中文'} else str(value or '')


def normalize_language(value):
    return 'zh-CN' if language_label(value) == '中文' else str(value or '').strip()


def clean_title(title, subtitle='', remove_subtitle=False, split_separator=''):
    # 只清除含明确营销词的成对括号，保留卷数、版本、普通副标题。
    marketing = re.compile(r'畅销|热销|重磅推荐|联袂推荐|豆瓣.{0,8}\d|万册|万部|百万|限时|随书附赠|口碑|震撼上市|倾情推荐')
    def replace(match):
        value = match.group(0)
        # 混有套装/版本信息时不自动删除，交给预览人工处理。
        return '' if marketing.search(value) and not re.search(r'套装|全\d+册|第.{1,5}[卷版辑]|修订', value) else value
    result = re.sub(r'【[^【】]*】|（[^（）]*）|\([^()]*\)', replace, title)
    if remove_subtitle and subtitle.strip():
        result = re.sub(r'\s*[:：—－-]\s*'+re.escape(subtitle.strip())+r'\s*$', '', result)
    if split_separator:
        separators = {'colon': r'[:：]', 'dash': r'—{1,2}', 'both': r'[:：]|—{1,2}'}
        if split_separator not in separators:
            raise ValueError('未知副标题分隔规则')
        result = re.split(separators[split_separator], result, maxsplit=1)[0]
    return result.strip() or title


def preview(books, fields=None, *, clean=False, clear_subtitle=False, normalize=False, review=False):
    fields = dict(fields or {})
    allowed = {'title', 'subtitle', 'author', 'publisher', 'description', 'description_source', 'main_category', 'subcategory', 'copyright_status', 'source_reference', 'language'}
    if set(fields)-allowed:
        raise ValueError('不支持的批量字段')
    if review and ({'main_category','subcategory'} & fields.keys()):
        raise ValueError('重新核对分类不能与批量设置分类同时使用')
    result=[]
    for book in books:
        meta=book['metadata']; changes=dict(fields)
        if 'language' in changes: changes['language']=normalize_language(changes['language'])
        elif normalize and language_label(meta.get('language'))=='中文': changes['language']='zh-CN'
        if clean or clear_subtitle: changes['title']=clean_title(meta.get('title',''),meta.get('subtitle',''),clear_subtitle)
        if 'title' in changes and not changes['title'].strip(): raise ValueError('书名不能为空')
        if len(changes.get('description', '')) > 30000: raise ValueError('简介不能超过30000字')
        if clear_subtitle: changes['subtitle']=''
        if review: changes['classification_status']='needs_review'
        elif {'main_category','subcategory'} & fields.keys():
            changes['classification_status']='confirmed' if changes.get('main_category',meta.get('main_category')) else 'pending'
        if {'copyright_status','source_reference'} & fields.keys():
            rights=changes.get('copyright_status',meta.get('copyright_status',''))
            source=changes.get('source_reference',meta.get('source_reference',''))
            if rights and not str(source or '').strip():
                raise ValueError('《'+meta.get('title','')+'》缺少授权/来源说明；请同时填写来源或保留为尚未确认')
            changes['rights_review_status']='confirmed' if rights and source else 'pending'
        changes={k:v for k,v in changes.items() if meta.get(k,'')!=v}
        if changes: result.append({'book_id':book['book_id'],'revision':book['revision'],'title':meta.get('title',''),'changes':changes,'before':{k:meta.get(k,'') for k in changes}})
    return result


def apply(workspace, updates, control=None):
    control=control or Control()
    snapshots=[]
    with workspace.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
            raise ValueError('书库还有运行中的任务，请完成后再批量编辑')
        for item in updates:
            control.check()
            book=workspace.decode(db.execute('SELECT * FROM books WHERE book_id=?',(item['book_id'],)).fetchone())
            if not book or book['revision']!=item['revision']:
                raise ValueError('预览后图书发生变化，请重新预览；本批未提交')
            snapshots.append({'book_id':book['book_id'],'metadata':book['metadata'],'locked':book['locked'],'revision':book['revision']+1})
            workspace.edit(book['book_id'],item['changes'],connection=db)
        control.check()
        if snapshots: db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('last_metadata_batch',json.dumps(snapshots,ensure_ascii=False)))
    return len(snapshots)


def undo(workspace, control=None):
    control=control or Control()
    with workspace.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone(): raise ValueError('请先完成运行中的任务')
        row=db.execute("SELECT value FROM settings WHERE key='last_metadata_batch'").fetchone()
        if not row: raise ValueError('没有可撤销的批量修改')
        snapshots=json.loads(row[0])
        for item in snapshots:
            control.check()
            book=workspace.decode(db.execute('SELECT * FROM books WHERE book_id=?',(item['book_id'],)).fetchone())
            if not book or book['revision']!=item['revision']: raise ValueError('批量修改后有图书再次修改或删除，停止整批撤销，避免覆盖新资料')
            db.execute('UPDATE books SET metadata=?,locked=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE book_id=?',(json.dumps(item['metadata'],ensure_ascii=False),json.dumps(item['locked']),item['book_id']))
            db.execute('DELETE FROM edits WHERE id=(SELECT MAX(id) FROM edits WHERE book_id=?)',(item['book_id'],))
        control.check()
        db.execute("DELETE FROM settings WHERE key='last_metadata_batch'")
    return len(snapshots)


def error_message(exc):
    text=str(exc).replace('\r',' ').replace('\n',' ')
    text=re.sub(r'(?i)(cookie["\x27]?\s*[:=]).*',r'\1[已隐藏]',text)
    text=re.sub(r'https?://\S+','[地址已隐藏]',text)
    text=re.sub(r'(?i)(bearer\s+)\S+',r'\1[已隐藏]',text)
    text=re.sub(r'''(?ix)((?:[\w-]*(?:token|secret|password|cookie|authorization|access_key)[\w-]*)["']?\s*[:=]\s*["']?)[^\s,;"']+''',r'\1[已隐藏]',text)
    return (type(exc).__name__+'：'+text)[:1200]

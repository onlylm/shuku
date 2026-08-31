import csv
import json
import pytest
from test_organizer import library
from ebook_organizer import descriptions, booklist, batch_edit
from ebook_organizer.safeio import Control, Cancelled, sha256_file


def test_extract_matching_opf_preview_apply_undo(library):
    ws, _, source = library
    bid = ws.books()[0]['book_id']; original = sha256_file(source)
    source.with_suffix('.opf').write_text('<package><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试图书</dc:title><dc:description>&lt;p&gt;真实简介&lt;/p&gt;</dc:description></metadata></package>', encoding='utf-8')
    result = descriptions.extract(ws, [bid])
    assert result['updates'][0]['changes']['description'] == '真实简介'
    assert not ws.book(bid)['metadata'].get('description')
    batch_edit.apply(ws, result['updates'])
    assert ws.book(bid)['metadata']['description_source'] == '同目录匹配 OPF'
    assert not descriptions.extract(ws, [bid])['updates']
    batch_edit.undo(ws)
    assert not ws.book(bid)['metadata'].get('description')
    assert sha256_file(source) == original


def test_extract_throttles_progress_for_large_batches(tmp_path, monkeypatch):
    from ebook_organizer.workspace import Workspace
    ws = Workspace(tmp_path/'many')
    books = []
    with ws.connect() as db:
        for i in range(250):
            bid=f'B{i}'; meta={'title':f'书{i}','description':''}
            source=tmp_path/f'{i}.epub'; source.write_bytes(str(i).encode())
            db.execute('INSERT INTO books(book_id,sha256,metadata,provenance,issues,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(bid,sha256_file(source),json.dumps(meta),'{}','[]','passed','now','now'))
            db.execute('INSERT INTO sources(path,root,book_id,size,mtime_ns,seen_at) VALUES(?,?,?,?,?,?)',(str(source),str(tmp_path),bid,source.stat().st_size,1,'now'))
            books.append(bid)
    class Result:
        status='passed'; metadata={'description':''}; provenance={}
    monkeypatch.setattr(descriptions,'inspect_epub',lambda *a: Result())
    messages=[]; result=descriptions.extract(ws,books,progress=messages.append)
    assert not result['updates'] and len(messages) <= 14
    assert '250/250' in messages[-2] and '完成' in messages[-1]


def test_existing_locked_missing_and_cancel(library):
    ws, _, _ = library; bid = ws.books()[0]['book_id']
    assert '原书无简介' in descriptions.extract(ws, [bid])['report'][0]['message']
    ws.edit(bid, {'description': ''})
    assert '人工清空' in descriptions.extract(ws, [bid])['report'][0]['message']
    ws.edit(bid, {'description': '我的简介'})
    assert '简介已有' in descriptions.extract(ws, [bid])['report'][0]['message']
    c = Control(); c.cancelled.set()
    with pytest.raises(Cancelled): descriptions.extract(ws, [bid], c)


def test_export_booklist_safe_full_description(library, tmp_path):
    ws, _, _ = library; bid = ws.books()[0]['book_id']
    ws.edit(bid, {'title': '=1+1', 'description': '简介,带逗号\n第二行', 'description_source': '人工编辑'})
    path = tmp_path / 'list.csv'
    assert booklist.export(ws, [bid], path) == 1
    with path.open(encoding='utf-8-sig', newline='') as f: rows = list(csv.DictReader(f))
    assert rows[0]['书名'] == "'=1+1"
    assert rows[0]['简介'] == '简介,带逗号\n第二行'
    assert rows[0]['图书编号'] == bid
    assert '本地路径' not in rows[0]
    c = Control(); c.cancelled.set(); before = path.read_bytes()
    with pytest.raises(Cancelled): booklist.export(ws, [bid], path, c)
    assert path.read_bytes() == before


def test_ai_template_and_precise_reimport(library, tmp_path):
    from ebook_organizer.table_import import read_table, preview_updates, apply_updates
    ws, _, _ = library; book = ws.books()[0]; bid = book['book_id']
    ws.edit(bid, {'author':'', 'publisher':'', 'description':''})
    path = tmp_path/'ai.csv'
    assert booklist.export_ai_template(ws,[bid],path)==1
    with path.open(encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
    assert rows[0]['系统编号']==bid and rows[0]['新书名']==book['metadata']['title']
    assert '补作者' in rows[0]['需要AI处理'] and '系统编号绝对不改' in rows[0]['处理规则']
    rows[0].update({'新书名':'精简后的书名','作者':'AI核实作者','出版社':'AI核实出版社','简介':'AI核实简介'})
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    preview=preview_updates(ws.books(),read_table(path))
    assert preview['updates'][0]['book_id']==bid
    assert preview['updates'][0]['changes']['title']=='精简后的书名'
    apply_updates(ws,preview)
    meta=ws.book(bid)['metadata']
    assert (meta['title'],meta['author'],meta['publisher'],meta['description'])==('精简后的书名','AI核实作者','AI核实出版社','AI核实简介')

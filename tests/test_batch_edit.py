import json
import pytest
from pathlib import Path
from ebook_organizer import batch_edit as batch
from ebook_organizer.engine import classify, DEFAULT_RULES
from ebook_organizer.workspace import Workspace
from ebook_organizer.safeio import Control, Cancelled


def make_books(tmp_path,count=3):
    ws=Workspace(tmp_path/'batch-library')
    with ws.connect() as db:
        for i in range(count):
            meta={'title':f'图书{i}【畅销百万册】','subtitle':'','language':'zho','main_category':f'分类{i%3}','subcategory':'原子类','copyright_status':'','rights_review_status':'pending'}
            db.execute('INSERT INTO books(book_id,sha256,metadata,provenance,issues,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(f'B{i}',str(i),json.dumps(meta),'{}','[]','passed','now','now'))
    return ws


def test_independent_rights_500_and_undo(tmp_path):
    ws=make_books(tmp_path,500); before=ws.books()
    plan=batch.preview(before,{'copyright_status':'public_domain','source_reference':'合成测试'})
    assert all('main_category' not in r['changes'] for r in plan)
    assert batch.apply(ws,plan)==500
    assert all(b['metadata']['rights_review_status']=='confirmed' for b in ws.books())
    assert batch.undo(ws)==500
    assert [b['metadata'] for b in ws.books()]==[b['metadata'] for b in before]
    with pytest.raises(ValueError,match='没有可撤销'): batch.undo(ws)


def test_titles_conservative():
    assert batch.clean_title('龙脉：千里大运河')=='龙脉：千里大运河'
    assert batch.clean_title('书名（全3册）【畅销百万册】')=='书名（全3册）'
    assert batch.clean_title('书名：副标题','副标题',True)=='书名'
    assert batch.clean_title('书名：副标题','别的副标题',True)=='书名：副标题'
    assert batch.clean_title('书名（套装全3册，畅销百万）')=='书名（套装全3册，畅销百万）'


def test_explicit_subtitle_rules_and_empty_title(tmp_path):
    assert batch.clean_title('龙脉：千里大运河', split_separator='colon') == '龙脉'
    assert batch.clean_title('龙脉——千里大运河', split_separator='dash') == '龙脉'
    assert batch.clean_title('套装1-7册', split_separator='both') == '套装1-7册'
    assert batch.clean_title('：不能删成空白', split_separator='colon') == '：不能删成空白'
    ws = make_books(tmp_path, 1)
    with pytest.raises(ValueError, match='不能为空'): batch.preview(ws.books(), {'title': '  '})
    ws.edit('B0', {'title': '书名：副标题', 'subtitle': '副标题'})
    plan = batch.preview(ws.books(), clear_subtitle=True)
    assert plan[0]['changes']['title'] == '书名'


def test_rollback_on_stale_preview(tmp_path):
    ws=make_books(tmp_path); plan=batch.preview(ws.books(),clean=True)
    ws.edit(plan[-1]['book_id'],{'author':'后来编辑'})
    before=ws.books()
    with pytest.raises(ValueError,match='预览后'): batch.apply(ws,plan)
    assert ws.books()==before
    assert ws.setting('last_metadata_batch') is None


def test_undo_refuses_later_changes(tmp_path):
    ws=make_books(tmp_path); batch.apply(ws,batch.preview(ws.books(),clean=True))
    ws.edit('B2',{'author':'新资料'}); before=ws.books()
    with pytest.raises(ValueError,match='再次修改'): batch.undo(ws)
    assert ws.books()==before


def test_cancel_no_partial_writes(tmp_path):
    ws=make_books(tmp_path); before=ws.books(); control=Control(); control.cancelled.set()
    with pytest.raises(Cancelled): batch.apply(ws,batch.preview(before,clean=True),control)
    assert ws.books()==before


def test_language_and_missing_rights(tmp_path):
    ws=make_books(tmp_path)
    assert batch.normalize_language('中文')=='zh-CN'
    assert batch.normalize_language('en')=='en'
    assert batch.language_label('zho')=='中文'
    assert batch.normalize_language('zh-TW')=='zh-TW'
    with pytest.raises(ValueError,match='来源'): batch.preview(ws.books(),{'copyright_status':'authorized'})
    plan=batch.preview(ws.books(),normalize=True)
    assert all(r['changes']=={'language':'zh-CN'} for r in plan)


def test_directory_not_confirmed():
    m=classify(Path('C:/test/生活实用/旅行与运动/book.epub'),Path('C:/test'),{'title':'黑箱：日本之耻'},DEFAULT_RULES)
    assert m['classification_status']=='needs_review'


def test_inferred_csv_not_confirmed(tmp_path):
    from ebook_organizer.table_import import preview_updates
    ws=make_books(tmp_path,1); b=ws.books()[0]
    result=preview_updates([b],[['书名','主分类','分类核验状态'],[b['metadata']['title'],'文学','推定（未核验）']],overwrite=True)
    assert result['updates'][0]['changes']['classification_status']=='needs_review'


def test_error_redacted():
    text=batch.error_message(RuntimeError('timeout token=abc123 secret_access_key=def456 Bearer abcdef https://host/?token=xyz'))
    assert all(v not in text for v in ('abc123','def456','abcdef','xyz'))
    assert 'timeout' in text


def test_quark_stage_error_and_auth_type(tmp_path):
    from app.services.cloud_uploads import ConnectorAuthRequired
    from ebook_organizer.connections import _quark_step
    ws=make_books(tmp_path,1)
    class Fake:
        def _run(self,*args,**kwargs): raise ConnectorAuthRequired('token=secret 未授权')
    with pytest.raises(ConnectorAuthRequired,match='创建分类目录失败'):
        _quark_step(Fake(),['create-folder'],'创建分类目录',ws,'B0')
    with ws.connect() as db:
        messages=' '.join(r[0] for r in db.execute('select message from events'))
    assert 'secret' not in messages

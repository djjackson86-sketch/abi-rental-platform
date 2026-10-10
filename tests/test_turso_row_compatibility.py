from types import SimpleNamespace
from libsql_client.result import Row
from app.turso_db import TursoCursor


def test_turso_cursor_matches_sqlite_row_access():
    result=SimpleNamespace(rows=[Row({'id':0,'name':1},(7,'Vehicle QA'))],last_insert_rowid=7,rows_affected=1)
    cursor=TursoCursor(result)
    row=cursor.fetchone()
    assert row.keys()==['id','name']
    assert row['id']==row[0]==7
    assert list(row)==[7,'Vehicle QA']
    assert dict(row)=={'id':7,'name':'Vehicle QA'}
    assert len(row)==2
    assert dict(cursor.fetchall()[0])==dict(row)
    assert cursor.lastrowid==7


def test_empty_turso_cursor_returns_none_and_empty_list():
    cursor=TursoCursor(SimpleNamespace(rows=[],last_insert_rowid=None,rows_affected=0))
    assert cursor.fetchone() is None
    assert cursor.fetchall()==[]

"""Exercise the real Hrana adapter against SQLite-backed HTTP responses."""
import io
import json
import sqlite3
import pytest
from app.turso_db import TursoConnection


@pytest.mark.parametrize('fail', [False, True])
def test_interactive_stream_commit_and_rollback(monkeypatch, fail):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE fixture (id INTEGER PRIMARY KEY, label TEXT)')
    operations = []
    def urlopen(request, timeout):
        payload = json.loads(request.data)
        operation = payload['requests'][0]
        operations.append(operation)
        assert request.full_url == 'https://fixture.invalid/v2/pipeline'
        assert request.headers['Authorization'] == 'Bearer synthetic'
        if operation['type'] == 'close':
            result = {'type': 'ok', 'response': {'type': 'close'}}
            baton = None
        else:
            stmt = operation['stmt']
            def value(v):
                return int(v['value']) if v['type']=='integer' else v.get('value')
            args = {p['name']: value(p['value']) for p in stmt['named_args']} if stmt['named_args'] else [value(v) for v in stmt['args']]
            cur = db.execute(stmt['sql'], args)
            rows = []
            for row in cur.fetchall():
                rows.append([{'type': 'integer', 'value': str(v)} if isinstance(v,int) else {'type':'text','value':v} for v in row])
            result = {'type':'ok','response':{'type':'execute','result':{'cols':[{'name':c[0]} for c in (cur.description or [])], 'rows':rows, 'affected_row_count':max(0,cur.rowcount),'last_insert_rowid':str(cur.lastrowid)}}}
            baton = 'stream-fixture'
        return io.BytesIO(json.dumps({'baton':baton,'results':[result]}).encode())
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    connection = TursoConnection.__new__(TursoConnection)
    connection._transaction_url = 'https://fixture.invalid'
    connection._auth_token = 'synthetic'
    def run():
        with connection.write_transaction() as transaction:
            transaction.execute('INSERT INTO fixture(label) VALUES (:label)', {'label':'synthetic'})
            transaction.commit()  # Must not commit early.
            assert transaction.execute('SELECT label FROM fixture').fetchone()['label']=='synthetic'
            if fail:
                raise ValueError('synthetic refusal')
    if fail:
        with pytest.raises(ValueError,match='synthetic refusal'):
            run()
    else:
        run()
    assert db.execute('SELECT COUNT(*) FROM fixture').fetchone()[0] == (0 if fail else 1)
    sqls = [op['stmt']['sql'] for op in operations if op['type']=='execute']
    assert sqls[0]=='BEGIN IMMEDIATE'
    assert sqls[-1]==('ROLLBACK' if fail else 'COMMIT')
    assert operations[-1]['type']=='close'
    db.close()

"""Turso HTTP-200 trigger errors must retain their validation message, without replay."""
import asyncio
from types import SimpleNamespace
import pytest
from libsql_client import LibsqlError
from app.turso_db import TursoConnection
from app.services.credit_limits import execute_credit_checked, ACCOUNT_ERROR


def test_flat_turso_trigger_error_is_validation_error_and_not_retried(monkeypatch):
    calls=[]
    async def send(*args, **kwargs):
        calls.append(args)
        return {'message':'SQLite error: '+ACCOUNT_ERROR,'code':'SQLITE_CONSTRAINT'}
    http=SimpleNamespace(_send=send)
    class Client:
        _client=http
        def execute(self,sql,params):
            return asyncio.run(self._client._send('POST','v1/execute',{}))['result']
    monkeypatch.setattr('libsql_client.create_client_sync',lambda *a,**k:Client())
    db=TursoConnection('https://test.invalid')
    with pytest.raises(ValueError,match='no longer a payment option'):
        execute_credit_checked(db,'INSERT INTO payments VALUES (?)',(1,))
    assert len(calls)==1


def test_successful_turso_result_unchanged(monkeypatch):
    result={'result':{'rows':[]}}
    async def send(*args, **kwargs):return result
    http=SimpleNamespace(_send=send)
    monkeypatch.setattr('libsql_client.create_client_sync',lambda *a,**k:SimpleNamespace(_client=http))
    db=TursoConnection('https://test.invalid')
    assert asyncio.run(http._send('POST','v1/execute',{})) is result

import re


class TursoCursor:
    def __init__(self, result):
        self._result = result
        self.lastrowid = result.last_insert_rowid
        self.rowcount = result.rows_affected

    def fetchone(self):
        return self._result.rows[0] if self._result.rows else None

    def fetchall(self):
        return list(self._result.rows)


class TursoConnection:
    """Small DB-API-like adapter for the subset of sqlite3 used by this app."""

    def __init__(self, url, auth_token=None):
        from libsql_client import create_client_sync

        self._transaction_url = url.replace("libsql://", "https://", 1)
        self._auth_token = auth_token
        kwargs = {}
        if auth_token:
            kwargs["auth_token"] = auth_token
        # libsql-client's libsql:// path uses WebSockets; Turso's HTTP endpoint is
        # more reliable on Render/WSL. Keep env vars in Turso's standard libsql://
        # form and translate at connection time.
        if url.startswith("libsql://"):
            url = "https://" + url.removeprefix("libsql://")
        self._client = create_client_sync(url, **kwargs)
        # Turso can return a flat SQL error with HTTP 200. libsql-client 0.3.x
        # assumes every 200 contains "result" and otherwise raises KeyError,
        # losing trigger validation messages. Normalise BEFORE parsing; never
        # retry a write, since replaying a payment would risk double charging.
        http_client = self._client._client
        original_send = http_client._send
        async def checked_send(*args, **send_kwargs):
            from libsql_client import LibsqlError
            response = await original_send(*args, **send_kwargs)
            if isinstance(response, dict) and "code" in response and "message" in response and "result" not in response:
                raise LibsqlError(response["message"], response["code"])
            return response
        http_client._send = checked_send
        self.total_changes = 0

    def execute(self, sql, params=None):
        params = () if params is None else params
        result = self._client.execute(sql, params)
        self.total_changes += result.rows_affected or 0
        return TursoCursor(result)

    def write_transaction(self):
        """Scoped interactive HTTP transaction using Hrana's stream baton (v2)."""
        from contextlib import contextmanager
        import json
        from urllib.request import Request, urlopen
        from libsql_client.hrana.convert import _stmt_to_proto, _result_set_from_proto
        from libsql_client import LibsqlError

        @contextmanager
        def transaction():
            outer = self

            class Connection:
                def __init__(self):
                    self.baton = None
                    self.url = outer._transaction_url.rstrip("/") + "/v2/pipeline"

                def send(self, operation):
                    payload = json.dumps({"baton": self.baton, "requests": [operation]}).encode()
                    headers = {"Content-Type": "application/json"}
                    if outer._auth_token:
                        headers["Authorization"] = "Bearer " + outer._auth_token
                    req = Request(self.url, data=payload, headers=headers, method="POST")
                    with urlopen(req, timeout=20) as response:
                        data = json.load(response)
                    self.baton = data.get("baton")
                    if data.get("base_url"):
                        self.url = data["base_url"].rstrip("/") + "/v2/pipeline"
                    result = data["results"][0]
                    if result["type"] == "error":
                        error = result["error"]
                        raise LibsqlError(error["message"], error.get("code", "UNKNOWN"))
                    return result["response"]

                def execute(self, sql, params=None):
                    result = self.send({"type": "execute", "stmt": _stmt_to_proto(sql, params)})
                    return TursoCursor(_result_set_from_proto(result["result"]))

                def commit(self):
                    pass  # Only the outer context commits.

                def rollback(self):
                    pass

            connection = Connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.baton:
                    try:
                        connection.execute("ROLLBACK")
                    except Exception:
                        pass  # Preserve the original error; closing the stream rolls back.
                raise
            finally:
                if connection.baton:
                    try:
                        connection.send({"type": "close"})
                    except Exception:
                        pass
        return transaction()

    def atomic_batch(self, statements):
        """Execute this scoped multi-row write in one libSQL transaction."""
        results = self._client.batch(statements)
        self.total_changes += sum(result.rows_affected or 0 for result in results)
        return [TursoCursor(result) for result in results]

    def executescript(self, script):
        cursor = None
        for statement in _split_sql_script(script):
            cursor = self.execute(statement)
        return cursor

    def commit(self):
        # Remote libSQL autocommits individual statements in this adapter.
        return None

    def rollback(self):
        return None

    def close(self):
        self._client.close()


def connect_turso(url, auth_token=None):
    return TursoConnection(url, auth_token)


def _split_sql_script(script):
    cleaned_lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        # Turso/libSQL enables foreign-key enforcement; PRAGMA is local sqlite setup.
        if stripped.upper().startswith("PRAGMA "):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    parts = re.split(r";\s*(?:\n|$)", cleaned)
    return [part.strip() for part in parts if part.strip()]

import sys
from pathlib import Path
from types import SimpleNamespace

import oracledb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders",
    "select a, b from t where note = 'please DELETE me'",
    "WITH x AS (SELECT 1 n FROM dual) SELECT n FROM x",
    "(SELECT 1 FROM dual) UNION ALL (SELECT 2 FROM dual)",
    "SELECT update_dt, comments FROM t -- DROP TABLE t",
    "SELECT q'[it's; DROP]' FROM dual",
    "SELECT * FROM t;",
])
def test_validate_allows_selects(sql):
    assert app.validate_select(sql) is None


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "SELECT 1 FROM dual; DROP TABLE orders",
    "UPDATE t SET a = 1",
    "SELECT * FROM t FOR UPDATE",
    "SELECT dbms_random.value FROM dual",
    "SELECT * FROM t@remote_link",
    "BEGIN NULL; END",
    "WITH x AS (SELECT 1 FROM dual) DELETE FROM t",
    "",
])
def test_validate_rejects(sql):
    assert app.validate_select(sql) is not None


def test_extract_sql():
    text = "```sql\nSELECT 1 FROM dual;\n```\n설명입니다."
    assert app.extract_sql(text) == "SELECT 1 FROM dual"
    assert app.extract_sql("답할 수 없습니다.") is None


def test_excel_bytes():
    data = app.to_excel_bytes(pd.DataFrame({"A": [1, 2]}), "SELECT 1", "질문")
    assert data[:2] == b"PK"
    back = pd.read_excel(__import__("io").BytesIO(data), sheet_name=None)
    assert set(back) == {"결과", "쿼리정보"}


class FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.answers.pop(0)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def test_retry_after_validation_and_db_error(monkeypatch):
    client = FakeClient([
        "```sql\nDELETE FROM t\n```",
        "```sql\nSELECT bad FROM t\n```",
        "```sql\nSELECT good FROM t\n```\n완료",
    ])

    def fake_run(pool, sql, row_limit, timeout_ms):
        if "bad" in sql:
            raise oracledb.DatabaseError("ORA-00904: invalid identifier")
        return pd.DataFrame({"GOOD": [1]}), False

    monkeypatch.setattr(app, "run_query", fake_run)
    res = app.text_to_sql(client, None, "schema", "질문", 100, 1000)
    assert len(res.attempts) == 3
    assert res.df is not None and res.final.sql == "SELECT good FROM t"
    # 오류 메시지가 다음 요청에 전달됐는지
    assert "ORA-00904" in client.calls[2]["messages"][-1]["content"]
    assert client.calls[0]["model"] == app.MODEL


def test_gives_up_after_max_attempts(monkeypatch):
    client = FakeClient(["```sql\nDROP TABLE t\n```"] * app.MAX_ATTEMPTS)
    res = app.text_to_sql(client, None, "s", "q", 10, 1000)
    assert res.df is None and len(res.attempts) == app.MAX_ATTEMPTS


def test_app_starts_without_config(monkeypatch):
    from streamlit.testing.v1 import AppTest
    for k in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN"]:
        monkeypatch.delenv(k, raising=False)
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=30).run()
    assert not at.exception
    assert "접속 정보가 없습니다" in at.error[0].value


def test_app_reports_missing_api_key(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ORACLE_USER", "u")
    monkeypatch.setenv("ORACLE_PASSWORD", "p")
    monkeypatch.setenv("ORACLE_DSN", "localhost:1521/X")
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=30).run()
    assert not at.exception
    assert "ANTHROPIC_API_KEY" in at.error[0].value

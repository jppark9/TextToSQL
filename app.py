"""SCM AI - 자연어 질문을 Oracle SELECT 문으로 바꿔 실행하는 Streamlit 앱.

실행: streamlit run app.py
설정: 환경변수 또는 .streamlit/secrets.toml (README.md 참고)
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import oracledb
import pandas as pd
import streamlit as st

MODEL = "claude-opus-5-5"
MAX_ATTEMPTS = 3  # 최초 생성 1회 + 오류 시 재시도 2회
DEFAULT_ROW_LIMIT = 1000
SCHEMA_FILE = Path(__file__).with_name("schema.txt")

SYSTEM_PROMPT = """당신은 Oracle 데이터베이스 SQL 전문가입니다.
사용자의 한국어 질문을 아래 스키마에 맞는 Oracle SQL 조회문 하나로 바꿉니다.

규칙:
- SELECT 또는 WITH ... SELECT 문 하나만 작성합니다. INSERT/UPDATE/DELETE/DDL/PL/SQL은 절대 쓰지 않습니다.
- 스키마에 있는 테이블과 컬럼만 사용합니다. 모르는 이름을 지어내지 않습니다.
- Oracle 문법을 따릅니다 (예: 날짜는 TO_DATE/TRUNC/SYSDATE, 행 제한은 FETCH FIRST n ROWS ONLY).
- 문장 끝에 세미콜론을 붙이지 않습니다.
- 답변은 ```sql 코드 블록 하나와, 그 아래 한두 문장의 한국어 설명으로만 구성합니다.
- 스키마로 답할 수 없는 질문이면 코드 블록 없이 이유만 한국어로 설명합니다.

스키마:
{schema}
"""

# 조회 전용 검증에 쓰는 금지 키워드 (문자열/주석을 제거한 뒤 검사)
FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "RENAME", "GRANT", "REVOKE", "COMMENT", "EXECUTE", "EXEC", "CALL",
    "BEGIN", "DECLARE", "LOCK", "COMMIT", "ROLLBACK", "SAVEPOINT", "PURGE",
    "FLASHBACK", "ANALYZE", "AUDIT", "NOAUDIT",
]
FORBIDDEN_PATTERNS = [
    (r"\bFOR\s+UPDATE\b", "FOR UPDATE(행 잠금)"),
    (r"\bDBMS_\w+", "DBMS_ 패키지 호출"),
    (r"\bUTL_\w+", "UTL_ 패키지 호출"),
    (r"\bSYS\s*\.", "SYS 스키마 접근"),
    (r"@\w+", "DB 링크(@) 사용"),
]


# --------------------------------------------------------------------------
# SQL 추출 / 검증
# --------------------------------------------------------------------------

def extract_sql(text: str) -> str | None:
    """모델 답변에서 ```sql 코드 블록을 꺼낸다. 없으면 None."""
    match = re.search(r"```(?:sql)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    sql = match.group(1).strip()
    return sql.rstrip(";").strip() or None


def _strip_literals_and_comments(sql: str) -> str:
    """키워드 검사 전에 문자열 리터럴, 따옴표 식별자, 주석을 지운다."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    # Oracle q'[...]' 리터럴: 여는 괄호는 짝이 되는 닫는 괄호로 끝난다
    sql = re.sub(r"q'\[.*?\]'|q'\{.*?\}'|q'\(.*?\)'|q'<.*?>'|q'(.).*?\1'", "''", sql,
                 flags=re.DOTALL | re.IGNORECASE)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"[^"]*"', '"x"', sql)
    return sql


def validate_select(sql: str) -> str | None:
    """조회 전용 SQL이면 None, 아니면 거부 사유를 돌려준다."""
    if not sql or not sql.strip():
        return "SQL이 비어 있습니다."
    cleaned = _strip_literals_and_comments(sql).strip().rstrip(";").strip()
    if ";" in cleaned:
        return "SQL 문은 하나만 허용됩니다 (세미콜론으로 여러 문장을 이어 쓸 수 없음)."
    upper = cleaned.upper()
    if not re.match(r"^\(?\s*(SELECT|WITH)\b", upper):
        return "SELECT 또는 WITH로 시작하는 조회문만 허용됩니다."
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return f"허용되지 않는 키워드가 포함되어 있습니다: {kw}"
    for pattern, label in FORBIDDEN_PATTERNS:
        if re.search(pattern, upper):
            return f"허용되지 않는 구문이 포함되어 있습니다: {label}"
    return None


# --------------------------------------------------------------------------
# 설정 / 연결
# --------------------------------------------------------------------------

def get_setting(name: str, default: str | None = None) -> str | None:
    """환경변수를 먼저 보고, 없으면 .streamlit/secrets.toml을 본다."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return st.secrets.get(name, default)
    except Exception:  # secrets.toml이 없으면 예외가 난다
        return default


@st.cache_resource(show_spinner=False)
def get_pool(user: str, password: str, dsn: str) -> oracledb.ConnectionPool:
    # Thin 모드: Oracle Client 설치 없이 동작
    return oracledb.create_pool(user=user, password=password, dsn=dsn, min=1, max=4, increment=1)


@st.cache_resource(show_spinner=False)
def get_client(api_key: str | None) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def load_schema_from_db(pool: oracledb.ConnectionPool, owner: str, tables: list[str]) -> str:
    """ALL_TAB_COLUMNS / ALL_COL_COMMENTS에서 테이블 정의를 읽어 텍스트로 만든다."""
    sql = """
        SELECT c.table_name, c.column_name, c.data_type, cc.comments
          FROM all_tab_columns c
          LEFT JOIN all_col_comments cc
            ON cc.owner = c.owner AND cc.table_name = c.table_name AND cc.column_name = c.column_name
         WHERE c.owner = :owner
    """
    binds: dict[str, str] = {"owner": owner.upper()}
    if tables:
        names = ", ".join(f":t{i}" for i in range(len(tables)))
        sql += f" AND c.table_name IN ({names})"
        binds.update({f"t{i}": t.strip().upper() for i, t in enumerate(tables)})
    sql += " ORDER BY c.table_name, c.column_id"

    with pool.acquire() as conn, conn.cursor() as cur:
        cur.execute(sql, binds)
        rows = cur.fetchall()

    lines: list[str] = []
    current = None
    for table, column, dtype, comment in rows:
        if table != current:
            lines.append(f"\n테이블 {owner.upper()}.{table}")
            current = table
        lines.append(f"  - {column} {dtype}" + (f"  -- {comment}" if comment else ""))
    return "\n".join(lines).strip()


def run_query(pool: oracledb.ConnectionPool, sql: str, row_limit: int, timeout_ms: int) -> tuple[pd.DataFrame, bool]:
    """SELECT를 실행해 최대 row_limit행을 DataFrame으로 돌려준다. (df, 잘림 여부)"""
    with pool.acquire() as conn:
        conn.call_timeout = timeout_ms
        with conn.cursor() as cur:
            cur.arraysize = min(row_limit, 1000)
            cur.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(row_limit + 1)
    truncated = len(rows) > row_limit
    return pd.DataFrame(rows[:row_limit], columns=columns), truncated


# --------------------------------------------------------------------------
# Claude 호출 + 재시도
# --------------------------------------------------------------------------

@dataclass
class Attempt:
    sql: str | None
    explanation: str
    error: str | None = None


@dataclass
class Result:
    question: str
    attempts: list[Attempt] = field(default_factory=list)
    df: pd.DataFrame | None = None
    truncated: bool = False

    @property
    def final(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None


def ask_claude(client: anthropic.Anthropic, schema: str, messages: list[dict]) -> str:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT.format(schema=schema),
        messages=messages,
        output_config={"effort": "medium"},
        # 안전 분류기가 거절하면 서버가 권장 모델로 자동 재시도
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("모델이 이 요청에 대한 답변을 거절했습니다. 질문을 바꿔 다시 시도해 주세요.")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def text_to_sql(client, pool, schema: str, question: str, row_limit: int, timeout_ms: int) -> Result:
    """SQL 생성 → 검증 → 실행. 검증/실행 오류가 나면 오류 내용을 Claude에게 돌려주고 재시도한다."""
    result = Result(question=question)
    messages: list[dict] = [{"role": "user", "content": question}]

    for _ in range(MAX_ATTEMPTS):
        answer = ask_claude(client, schema, messages)
        sql = extract_sql(answer)
        explanation = re.sub(r"```.*?```", "", answer, flags=re.DOTALL).strip()
        attempt = Attempt(sql=sql, explanation=explanation)
        result.attempts.append(attempt)

        if sql is None:  # 스키마로 답할 수 없는 질문 → 재시도하지 않음
            return result

        attempt.error = validate_select(sql)
        if attempt.error is None:
            try:
                result.df, result.truncated = run_query(pool, sql, row_limit, timeout_ms)
                return result
            except oracledb.Error as e:
                attempt.error = f"Oracle 실행 오류: {e}"

        messages += [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": (
                f"위 SQL에서 오류가 났습니다.\n오류: {attempt.error}\n"
                "스키마와 규칙을 다시 확인해 수정한 SELECT 문을 같은 형식으로 답해 주세요."
            )},
        ]
    return result


# --------------------------------------------------------------------------
# 엑셀 내보내기
# --------------------------------------------------------------------------

def to_excel_bytes(df: pd.DataFrame, sql: str, question: str) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="결과", index=False)
        pd.DataFrame({"항목": ["질문", "SQL"], "내용": [question, sql]}).to_excel(
            writer, sheet_name="쿼리정보", index=False
        )
        sheet = writer.sheets["결과"]
        for idx, col in enumerate(df.columns, start=1):
            width = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)]) + 2
            sheet.column_dimensions[sheet.cell(row=1, column=idx).column_letter].width = min(width, 50)
    return buf.getvalue()


# --------------------------------------------------------------------------
# 화면
# --------------------------------------------------------------------------

def render_result(res: Result, key: str) -> None:
    final = res.final
    if final is None:
        return
    if len(res.attempts) > 1:
        with st.expander(f"재시도 기록 ({len(res.attempts) - 1}회)"):
            for i, a in enumerate(res.attempts[:-1], start=1):
                st.markdown(f"**시도 {i}** — {a.error}")
                if a.sql:
                    st.code(a.sql, language="sql")

    if final.sql is None:
        st.warning(final.explanation or "SQL을 만들지 못했습니다.")
        return

    st.code(final.sql, language="sql")
    if final.explanation:
        st.caption(final.explanation)

    if res.df is None:
        st.error(f"{MAX_ATTEMPTS}번 시도했지만 실행하지 못했습니다.\n\n{final.error}")
        return

    msg = f"{len(res.df):,}행"
    if res.truncated:
        msg += " (행 제한에 걸려 일부만 표시)"
    st.success(msg)
    st.dataframe(res.df)
    st.download_button(
        "엑셀 다운로드",
        data=to_excel_bytes(res.df, final.sql, res.question),
        file_name="scm_ai_result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"dl_{key}",
    )


def main() -> None:
    st.set_page_config(page_title="SCM AI 조회", page_icon="📦", layout="wide")
    st.title("SCM AI 데이터 조회")

    api_key = get_setting("ANTHROPIC_API_KEY")
    db_user = get_setting("ORACLE_USER")
    db_password = get_setting("ORACLE_PASSWORD")
    db_dsn = get_setting("ORACLE_DSN")  # 예: 10.0.0.5:1521/ORCLPDB1

    with st.sidebar:
        st.header("설정")
        row_limit = st.number_input("최대 행 수", 10, 100_000, DEFAULT_ROW_LIMIT, step=100)
        timeout_s = st.number_input("쿼리 제한 시간(초)", 5, 600, 60, step=5)

        st.subheader("스키마")
        if "schema" not in st.session_state:
            st.session_state.schema = SCHEMA_FILE.read_text(encoding="utf-8") if SCHEMA_FILE.exists() else ""
        owner = st.text_input("DB 소유자(Owner)", get_setting("ORACLE_SCHEMA_OWNER", db_user or "") or "")
        tables = st.text_input("테이블 목록 (쉼표 구분, 비우면 전체)", get_setting("ORACLE_TABLES", "") or "")
        if st.button("DB에서 스키마 불러오기", disabled=not (db_user and db_password and db_dsn and owner)):
            try:
                pool = get_pool(db_user, db_password, db_dsn)
                table_list = [t for t in tables.split(",") if t.strip()]
                st.session_state.schema = load_schema_from_db(pool, owner, table_list)
                st.success("스키마를 불러왔습니다.")
            except oracledb.Error as e:
                st.error(f"스키마 조회 실패: {e}")
        st.session_state.schema = st.text_area(
            "Claude에게 전달할 스키마 (직접 수정 가능)", st.session_state.schema, height=300
        )

    missing = [n for n, v in [("ORACLE_USER", db_user), ("ORACLE_PASSWORD", db_password), ("ORACLE_DSN", db_dsn)] if not v]
    if missing:
        st.error("DB 접속 정보가 없습니다: " + ", ".join(missing) + "  (README.md의 설정 방법 참고)")
        st.stop()
    if not st.session_state.schema.strip():
        st.info("왼쪽에서 스키마를 불러오거나 schema.txt를 만들어 주세요. 스키마가 있어야 정확한 SQL이 나옵니다.")

    if "history" not in st.session_state:
        st.session_state.history = []

    for i, res in enumerate(st.session_state.history):
        with st.chat_message("user"):
            st.write(res.question)
        with st.chat_message("assistant"):
            render_result(res, key=str(i))

    question = st.chat_input("예: 지난달 창고별 출고 수량 합계를 보여줘")
    if not question:
        return

    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        try:
            with st.spinner("SQL을 만들고 실행하는 중..."):
                res = text_to_sql(
                    get_client(api_key),
                    get_pool(db_user, db_password, db_dsn),
                    st.session_state.schema,
                    question,
                    int(row_limit),
                    int(timeout_s) * 1000,
                )
        except anthropic.AuthenticationError:
            st.error("Anthropic API 키가 올바르지 않습니다.")
            return
        except anthropic.RateLimitError:
            st.error("API 호출 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.")
            return
        except anthropic.APIConnectionError:
            st.error("Anthropic API에 연결할 수 없습니다. 사내망 방화벽/프록시 설정을 확인해 주세요.")
            return
        except anthropic.APIStatusError as e:
            st.error(f"Anthropic API 오류 ({e.status_code}): {e.message}")
            return
        except oracledb.Error as e:
            st.error(f"DB 연결 오류: {e}")
            return
        except RuntimeError as e:
            st.error(str(e))
            return
        st.session_state.history.append(res)
        render_result(res, key=str(len(st.session_state.history) - 1))


if __name__ == "__main__":
    main()

# SCM AI 데이터 조회 (Text-to-SQL)

자연어로 질문하면 Claude가 Oracle **SELECT 문**을 만들고, 앱이 실행해서 표와 엑셀 파일로 보여 줍니다.

## 기능

- **조회 전용 검증**: SELECT / WITH로 시작하는 문장 하나만 실행합니다. INSERT·UPDATE·DELETE·DDL·PL/SQL, `FOR UPDATE`, `DBMS_`/`UTL_` 패키지, DB 링크(`@`)는 차단합니다.
- **오류 시 자동 재시도**: 검증에서 걸리거나 Oracle 오류(예: `ORA-00904`)가 나면 오류 내용을 Claude에게 돌려주고 최대 2번 더 고쳐 씁니다. 재시도 기록은 화면에서 펼쳐 볼 수 있습니다.
- **엑셀 다운로드**: 결과 시트와 질문·SQL을 담은 시트로 `.xlsx` 파일을 만듭니다.
- **행 수·시간 제한**: 사이드바에서 최대 행 수(기본 1,000)와 쿼리 제한 시간(기본 60초)을 정합니다.
- **스키마 불러오기**: 사이드바 버튼으로 `ALL_TAB_COLUMNS`/`ALL_COL_COMMENTS`에서 테이블 정의와 컬럼 주석을 읽어 옵니다. `schema.txt` 파일로 직접 써 둘 수도 있습니다.

> 앱의 검증은 보조 장치입니다. **DB 계정 자체를 SELECT 권한만 있는 읽기 전용 계정으로** 쓰세요.

## 설치 (Windows)

```
mkdir scm-ai
cd scm-ai
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

`oracledb`는 Thin 모드로 동작하므로 Oracle Client를 따로 설치하지 않아도 됩니다.

## 설정

`.streamlit\secrets.toml.example`을 `.streamlit\secrets.toml`로 복사하고 값을 채웁니다. 환경변수로 넣어도 됩니다(환경변수가 우선).

| 이름 | 설명 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API 키 |
| `ORACLE_USER` / `ORACLE_PASSWORD` | 읽기 전용 DB 계정 |
| `ORACLE_DSN` | `호스트:포트/서비스명` (예: `10.0.0.5:1521/ORCLPDB1`) |
| `ORACLE_SCHEMA_OWNER` (선택) | 스키마를 불러올 소유자 |
| `ORACLE_TABLES` (선택) | 쉼표로 구분한 테이블 목록. 비우면 소유자의 전체 테이블 |

스키마는 둘 중 하나로 준비합니다.
1. 앱 사이드바의 **DB에서 스키마 불러오기** 버튼
2. `schema.txt.example`을 참고해 `schema.txt` 작성 (컬럼 설명을 적어 둘수록 SQL이 정확해집니다)

테이블이 아주 많으면 질문에 필요한 테이블만 `ORACLE_TABLES`에 넣는 편이 정확도와 비용 면에서 낫습니다.

## 실행

```
streamlit run app.py
```

브라우저가 열리면 아래 입력창에 질문을 입력합니다. 예: `지난달 창고별 출고 수량 합계를 보여줘`

## 테스트

```
pip install pytest
python -m pytest tests
```

DB와 API 없이 SQL 검증, 재시도 흐름, 엑셀 생성, 앱 시작을 확인합니다.

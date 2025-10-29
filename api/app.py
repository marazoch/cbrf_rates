from pathlib import Path
from typing import Annotated, List, Literal, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic.dataclasses import dataclass
from pydantic import Field

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "daily_csv"
MAX_LIMIT_DEFAULT = 200
DATE_FMT = "%Y-%m-%d"

COL_DATE = "Date"
COL_CODE = "CharCode"
COL_NAME = "Name"
COL_NOMINAL = "Nominal"
COL_VALUE = "Value"
COL_VALUE_PER_UNIT = "ValuePerUnit"
COL_DATETS = "DateTS"

app = FastAPI(
    title="CBR Rates API",
    version="1.0.0",
    description="""
REST API для доступа к курсам валют ЦБ РФ, собранным скрапером.
- Данные читаются из `data/daily_csv/*.csv`
- Поддерживаются фильтры, сортировка, пагинация
- Ограничение размера ответа, чтобы не отдавать очень большие куски данных
""",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_df: Optional[pd.DataFrame] = None


def _load_csv_folder(folder: Path) -> pd.DataFrame:
    if not folder.exists():
        raise FileNotFoundError(f"Data folder not found: {folder}")

    files = sorted(folder.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {folder}")

    use_cols = [COL_CODE, COL_NAME, COL_NOMINAL, COL_VALUE, COL_VALUE_PER_UNIT, COL_DATE]
    dfs: list[pd.DataFrame] = []
    for f in files:
        try:
            d = pd.read_csv(f, dtype=str, usecols=use_cols, encoding="utf-8")
        except Exception:
            d = pd.read_csv(f, dtype=str, encoding="utf-8")
            d = d[[c for c in use_cols if c in d.columns]]
        dfs.append(d)

    df = pd.concat(dfs, ignore_index=True)

    df[COL_DATE] = pd.to_datetime(df[COL_DATE], errors="coerce").dt.strftime(DATE_FMT)
    df[COL_NOMINAL] = pd.to_numeric(df[COL_NOMINAL], errors="coerce")
    df[COL_VALUE_PER_UNIT] = pd.to_numeric(df[COL_VALUE_PER_UNIT], errors="coerce")

    df = df.dropna(subset=[COL_DATE, COL_CODE, COL_VALUE_PER_UNIT]).copy()

    df[COL_CODE] = df[COL_CODE].astype(str).str.strip().str.upper()

    return df


def _ensure_loaded():
    global _df
    if _df is None:
        _df = _load_csv_folder(DATA_DIR)


def _latest_date() -> str:
    _ensure_loaded()
    if _df.empty:
        raise HTTPException(404, detail="No data loaded")
    dates = pd.to_datetime(_df[COL_DATE], errors="coerce")
    return dates.max().strftime(DATE_FMT)


@dataclass
class RateItem:
    date: str = Field(..., description="Дата (YYYY-MM-DD)")
    char_code: str = Field(..., description="Код валюты (USD, EUR, ...)")
    name: Optional[str] = Field(None, description="Название валюты")
    nominal: Optional[int] = Field(None, description="Номинал (сколько единиц в курсе)")
    value_per_unit: float = Field(..., description="Курс в рублях за 1 единицу")


@dataclass
class RatesResponse:
    total: int
    limit: int
    offset: int
    items: List[RateItem]


@dataclass
class DatesResponse:
    dates: List[str]


@dataclass
class RangePoint:
    date: str
    value_per_unit: float


@dataclass
class RangeResponse:
    code: str
    start: str
    end: str
    total: int
    limit: int
    offset: int
    items: List[RangePoint]


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.post("/reload", tags=["system"], status_code=status.HTTP_202_ACCEPTED)
def reload_data():
    global _df
    _df = _load_csv_folder(DATA_DIR)
    return {"status": "reloaded", "rows": int(_df.shape[0])}


@app.get("/dates", response_model=DatesResponse, response_model_exclude_none=True, tags=["data"])
def list_dates(
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT_DEFAULT, description="Сколько последних дат вернуть")] = 50
):
    _ensure_loaded()
    if _df.empty:
        return DatesResponse(dates=[])
    dates = sorted(_df[COL_DATE].unique(), reverse=True)[:limit]
    return DatesResponse(dates=dates)


@app.get(
    "/rates",
    response_model=RatesResponse,
    response_model_exclude_none=True,
    tags=["data"],
    summary="Срез курсов за одну дату (или последнюю)",
)
def get_rates(
        date: Optional[str] = Query(
            None, description="Дата в формате YYYY-MM-DD. Если не указано — берётся последняя доступная."
        ),
        codes: Optional[str] = Query(
            None, description="Список кодов валют через запятую, напр. 'USD,EUR,CNY'."
        ),
        sort_by: Literal["char_code", "name", "value_per_unit"] = Query(
            "char_code", description="Поле сортировки."
        ),
        order: Literal["asc", "desc"] = Query("asc", description="Порядок сортировки."),
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT_DEFAULT)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
):
    _ensure_loaded()

    if date is None:
        date = _latest_date()
    else:
        try:
            pd.to_datetime(date, format=DATE_FMT)
        except Exception:
            raise HTTPException(400, detail="Invalid 'date' format, expected YYYY-MM-DD")

    df = _df[_df[COL_DATE] == date].copy()
    if df.empty:
        raise HTTPException(404, detail=f"No data for date {date}")

    if codes:
        wanted = {c.strip().upper() for c in codes.split(",") if c.strip()}
        df = df[df[COL_CODE].isin(wanted)]

    key_map = {"char_code": COL_CODE, "name": COL_NAME, "value_per_unit": COL_VALUE_PER_UNIT}
    if sort_by == "name":
        df = df.copy()
        df["_NameSort"] = df[COL_NAME].fillna("")
        df = df.sort_values("_NameSort", ascending=(order == "asc"))
        df = df.drop(columns=["_NameSort"])
    else:
        df = df.sort_values(key_map[sort_by], ascending=(order == "asc"))

    total = int(df.shape[0])
    df = df.iloc[offset: offset + limit]

    items = [
        RateItem(
            date=row[COL_DATE],
            char_code=row[COL_CODE],
            name=(row[COL_NAME] if pd.notna(row[COL_NAME]) else None),
            nominal=int(row[COL_NOMINAL]) if pd.notna(row[COL_NOMINAL]) else None,
            value_per_unit=float(row[COL_VALUE_PER_UNIT]),
        )
        for _, row in df.iterrows()
    ]

    return RatesResponse(total=total, limit=limit, offset=offset, items=items)


@app.get(
    "/range",
    response_model=RangeResponse,
    response_model_exclude_none=True,
    tags=["data"],
    summary="Временной ряд для одной валюты за период",
)
def get_range(
        code: str = Query(..., description="Код валюты (например, USD)"),
        start: str = Query(..., description="Начало периода YYYY-MM-DD"),
        end: str = Query(..., description="Конец периода YYYY-MM-DD (включительно)"),
        agg: Optional[Literal["first", "last", "min", "max", "mean"]] = Query(
            None, description="Необязательно: агрегировать по датам (если дубликаты)."
        ),
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT_DEFAULT)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
        order: Literal["asc", "desc"] = Query("asc", description="Порядок по дате."),
):
    _ensure_loaded()

    code = code.strip().upper()
    try:
        start_dt = pd.to_datetime(start, format=DATE_FMT)
        end_dt = pd.to_datetime(end, format=DATE_FMT)
    except Exception:
        raise HTTPException(400, detail="Invalid 'start'/'end' format, expected YYYY-MM-DD")

    if end_dt < start_dt:
        raise HTTPException(400, detail="'end' must be >= 'start'")

    df = _df[_df[COL_CODE] == code].copy()
    if df.empty:
        raise HTTPException(404, detail=f"Unknown currency code '{code}'")

    df[COL_DATETS] = pd.to_datetime(df[COL_DATE], errors="coerce")
    df = df[(df[COL_DATETS] >= start_dt) & (df[COL_DATETS] <= end_dt)]

    if df.empty:
        return RangeResponse(code=code, start=start, end=end, total=0, limit=limit, offset=offset, items=[])

    df = df.sort_values([COL_DATETS])

    if agg:
        agg_map = {"first": "first", "last": "last", "min": "min", "max": "max", "mean": "mean"}
        df = (
            df.groupby(COL_DATE, as_index=False, sort=True)[COL_VALUE_PER_UNIT]
            .agg(agg_map[agg])
            .rename(columns={COL_VALUE_PER_UNIT: COL_VALUE_PER_UNIT})
        )
        df[COL_DATETS] = pd.to_datetime(df[COL_DATE], errors="coerce")

    df = df.sort_values(COL_DATETS, ascending=(order == "asc"))

    total = int(df.shape[0])
    df = df.iloc[offset: offset + limit]

    items = [RangePoint(date=row[COL_DATE], value_per_unit=float(row[COL_VALUE_PER_UNIT])) for _, row in df.iterrows()]
    return RangeResponse(code=code, start=start, end=end, total=total, limit=limit, offset=offset, items=items)

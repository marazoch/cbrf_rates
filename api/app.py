import os
from pathlib import Path
from typing import Annotated, List, Literal, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Body, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "daily_csv"
MAX_LIMIT_DEFAULT = 200
DATE_FMT = "%Y-%m-%d"

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

    dfs = []
    use_cols = ["NumCode", "CharCode", "Nominal", "Name", "Value", "ValuePerUnit", "Date"]
    for f in files:
        try:
            d = pd.read_csv(f, dtype=str, usecols=use_cols)
        except Exception:
            d = pd.read_csv(f, dtype=str)
            d = d[[c for c in use_cols if c in d.columns]]
        dfs.append(d)

    df = pd.concat(dfs, ignore_index=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime(DATE_FMT)
    df["Nominal"] = pd.to_numeric(df["Nominal"], errors="coerce")
    df["ValuePerUnit"] = pd.to_numeric(df["ValuePerUnit"], errors="coerce")
    df = df.dropna(subset=["Date", "CharCode", "ValuePerUnit"]).copy()
    return df


def _ensure_loaded():
    global _df
    if _df is None:
        _df = _load_csv_folder(DATA_DIR)


def _latest_date() -> str:
    _ensure_loaded()
    dates = pd.to_datetime(_df["Date"], errors="coerce")
    return dates.max().strftime(DATE_FMT)


class RateItem(BaseModel):
    date: str = Field(..., description="Дата (YYYY-MM-DD)")
    char_code: str = Field(..., description="Код валюты (USD, EUR, ...)")
    name: Optional[str] = Field(None, description="Название валюты")
    nominal: Optional[int] = Field(None, description="Номинал (сколько единиц в курсе)")
    value_per_unit: float = Field(..., description="Курс в рублях за 1 единицу")

class RatesResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[RateItem]

class DatesResponse(BaseModel):
    dates: List[str]

class RangePoint(BaseModel):
    date: str
    value_per_unit: float

class RangeResponse(BaseModel):
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
    """Ручная перезагрузка из CSV (вызвать после нового прогона скрапера)."""
    global _df
    _df = _load_csv_folder(DATA_DIR)
    return {"status": "reloaded", "rows": int(_df.shape[0])}

@app.get("/dates", response_model=DatesResponse, tags=["data"])
def list_dates():
    """Список доступных дат (новейшие первыми)."""
    _ensure_loaded()
    dates = sorted(_df["Date"].unique(), reverse=True)
    return DatesResponse(dates=dates)

@app.get(
    "/rates",
    response_model=RatesResponse,
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
    """
    Возвращает **небольшой** срез курсов за одну дату.
    Примеры:
    - `/rates?date=2025-09-29&codes=USD,EUR`
    - `/rates?codes=USD,EUR&sort_by=value_per_unit&order=desc&limit=20`
    """
    _ensure_loaded()

    # дата
    if date is None:
        date = _latest_date()
    else:
        # валидация формата
        try:
            pd.to_datetime(date, format=DATE_FMT)
        except Exception:
            raise HTTPException(400, detail="Invalid 'date' format, expected YYYY-MM-DD")

    df = _df[_df["Date"] == date].copy()
    if df.empty:
        raise HTTPException(404, detail=f"No data for date {date}")

    # фильтр по кодам
    if codes:
        wanted = {c.strip().upper() for c in codes.split(",") if c.strip()}
        df = df[df["CharCode"].str.upper().isin(wanted)]

    # сортировка
    key_map = {
        "char_code": "CharCode",
        "name": "Name",
        "value_per_unit": "ValuePerUnit",
    }
    df = df.sort_values(key_map[sort_by], ascending=(order == "asc"))

    total = int(df.shape[0])
    df = df.iloc[offset : offset + limit]

    items = [
        RateItem(
            date=row["Date"],
            char_code=row["CharCode"],
            name=row.get("Name"),
            nominal=int(row["Nominal"]) if pd.notna(row["Nominal"]) else None,
            value_per_unit=float(row["ValuePerUnit"]),
        )
        for _, row in df.iterrows()
    ]

    return RatesResponse(total=total, limit=limit, offset=offset, items=items)

@app.get(
    "/range",
    response_model=RangeResponse,
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
    """
    Возвращает временной ряд `date -> value_per_unit` для одной валюты.
    - `agg` нужен на всякий случай, если в данных по одной дате несколько строк.
    - Результат **пагинируется**, максимум `MAX_LIMIT_DEFAULT` точек за ответ.
    """
    _ensure_loaded()

    code = code.strip().upper()
    try:
        start_dt = pd.to_datetime(start, format=DATE_FMT)
        end_dt = pd.to_datetime(end, format=DATE_FMT)
    except Exception:
        raise HTTPException(400, detail="Invalid 'start'/'end' format, expected YYYY-MM-DD")

    if end_dt < start_dt:
        raise HTTPException(400, detail="'end' must be >= 'start'")

    df = _df[_df["CharCode"].str.upper() == code].copy()
    if df.empty:
        raise HTTPException(404, detail=f"Unknown currency code '{code}'")

    df["DateTS"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df[(df["DateTS"] >= start_dt) & (df["DateTS"] <= end_dt)]

    if df.empty:
        return RangeResponse(code=code, start=start, end=end, total=0, limit=limit, offset=offset, items=[])

    # чистим и агрегируем по дате при необходимости
    if agg:
        aggs = {
            "first": "first",
            "last": "last",
            "min": "min",
            "max": "max",
            "mean": "mean",
        }
        df = (
            df.groupby("Date", as_index=False)["ValuePerUnit"]
            .agg(aggs[agg])
            .rename(columns={"ValuePerUnit": "ValuePerUnit"})
        )
        df["DateTS"] = pd.to_datetime(df["Date"], errors="coerce")


    df = df.sort_values("DateTS", ascending=(order == "asc"))

    total = int(df.shape[0])
    df = df.iloc[offset : offset + limit]

    items = [
        RangePoint(date=row["Date"], value_per_unit=float(row["ValuePerUnit"]))
        for _, row in df.iterrows()
    ]
    return RangeResponse(code=code, start=start, end=end, total=total, limit=limit, offset=offset, items=items)

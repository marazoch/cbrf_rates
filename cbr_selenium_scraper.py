import argparse
import datetime as dt
from datetime import datetime as _dt
import time
from pathlib import Path
from typing import List, Optional

import matplotlib.pyplot as plt
import pandas as pd
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://www.cbr.ru/currency_base/daily/"

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "raw"
DAILY_DIR = DATA_DIR / "daily_csv"
RAW_DIR.mkdir(parents=True, exist_ok=True)
DAILY_DIR.mkdir(parents=True, exist_ok=True)


def build_url_for_date(d: dt.date) -> str:
    dstr = d.strftime("%d.%m.%Y")
    return f"{BASE_URL}?UniDbQuery.Posted=True&UniDbQuery.To={dstr}"


def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,1000")
    opts.add_argument("--lang=ru-RU,ru")
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    return driver


def fetch_html_for_date(d: dt.date, headless: bool = True) -> str:
    url = build_url_for_date(d)
    driver = make_driver(headless=headless)
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.data")))
        time.sleep(0.4)
        html = driver.page_source
        (RAW_DIR / f"{d.isoformat()}.html").write_text(html, encoding="utf-8")
        return html
    finally:
        driver.quit()


def parse_table(html: str, d: dt.date) -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.data")
    if table is None:
        return pd.DataFrame(columns=["NumCode", "CharCode", "Nominal", "Name", "Value", "ValuePerUnit", "Date"])

    rows = []
    for tr in table.select("tr")[1:]:
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if len(tds) != 5:
            continue
        numcode, charcode, nominal, name, value = tds
        value_norm = value.replace(",", ".")
        try:
            nominal_i = int(nominal)
            value_f = float(value_norm)
            per_unit = value_f / nominal_i if nominal_i else None
        except Exception:
            per_unit = None
        rows.append({
            "NumCode": numcode,
            "CharCode": charcode,
            "Nominal": nominal,
            "Name": name,
            "Value": value,
            "ValuePerUnit": per_unit,
            "Date": d.isoformat(),
        })
    return pd.DataFrame(rows)


def collect_from_today(days: int, headless: bool) -> List[pd.DataFrame]:
    end = dt.date.today()
    dfs: List[pd.DataFrame] = []
    for offset in range(days):
        d = end - dt.timedelta(days=offset)
        try:
            html = fetch_html_for_date(d, headless=headless)
            df = parse_table(html, d)
            if not df.empty:
                out = DAILY_DIR / f"{d.isoformat()}.csv"
                df.to_csv(out, index=False, encoding="utf-8")
                dfs.append(df)
                print(f"[{d}] rows: {len(df)} → {out}")
            else:
                print(f"[{d}] No data")
        except Exception as e:
            print(f"[{d}] download error: {e}")
    return dfs


def merge_wide(dfs: List[pd.DataFrame], pick: Optional[List[str]]) -> pd.DataFrame:
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    if pick:
        df = df[df["CharCode"].isin(pick)]
    wide = df.pivot_table(index="Date", columns="CharCode", values="ValuePerUnit", aggfunc="first")
    wide = wide.sort_index()
    return wide


def calendarize_and_ffill(wide: pd.DataFrame) -> pd.DataFrame:
    """
    Builds a complete calendar based on wide dates and pulls the values forward (ffill),
    so that weekends (holidays) have the rates of the previous working day.
    """
    if wide.empty:
        return wide
    idx = pd.to_datetime(wide.index)
    full = pd.date_range(idx.min(), idx.max(), freq="D")
    out = (wide.reindex(full.strftime("%Y-%m-%d"))
           .apply(pd.to_numeric, errors="coerce")
           .ffill())
    out.index = out.index.astype(str)
    return out


def select_top_movers(wide: pd.DataFrame, k: int = 8) -> pd.DataFrame:
    if wide.shape[1] <= k:
        return wide
    rel = wide.pct_change().dropna(how="all", axis=0)
    movers = rel.std(numeric_only=True).sort_values(ascending=False).head(k).index
    return wide[movers]


def _unique_plot_path(kind: str, wide: pd.DataFrame) -> Path:
    idx = pd.to_datetime(wide.index)
    start = idx.min().date().isoformat()
    end = idx.max().date().isoformat()
    dcount = idx.normalize().nunique()
    ts = _dt.now().strftime("%Y%m%d-%H%M%S")
    fname = f"cbr_plot_{kind}_d{dcount}_{start}_{end}_{ts}.png"
    out = DATA_DIR / fname
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def plot_wide(
        wide: pd.DataFrame,
        out_png: Path,
        mode: str = "absolute",  # 'absolute', 'delta', 'indexed'
        top_k: int = 8,
        force_all: bool = False
) -> None:
    if wide.empty:
        print("No data for plot.")
        return

    data = calendarize_and_ffill(wide.copy())

    if not force_all and data.shape[1] > top_k:
        data = select_top_movers(data, k=top_k)

    data = data.sort_index().apply(pd.to_numeric, errors="coerce")
    x = pd.to_datetime(data.index)

    if mode.lower() == "indexed":
        base = data.iloc[0]
        data = data.divide(base) * 100.0
        y_label = "Index (first calendar day = 100)"
        title = "CBRF rates — indexed"
        line_kwargs = dict(linewidth=1.4, markersize=3.2, marker="o", alpha=0.95)

    elif mode.lower() == "delta":
        base = data.iloc[0]
        data = data.subtract(base)
        y_label = "delta RUB vs first day"
        title = "CBRF rates — delta RUB to first calendar day"
        line_kwargs = dict(linewidth=1.6, alpha=0.95)

    else:
        y_label = "RUB per 1 unit"
        title = "CBRF rates (RUB per 1)"
        line_kwargs = dict(linewidth=1.5, alpha=0.95)

    fig, ax = plt.subplots(figsize=(12, 6.8), constrained_layout=True)
    for col in data.columns:
        ax.plot(x, data[col], label=col, **line_kwargs)

    ax.set_title(title, pad=10)
    ax.set_xlabel("Date")
    ax.set_ylabel(y_label)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate(rotation=28, ha="right")
    ax.margins(x=0.02, y=0.08)

    n_series = data.shape[1]
    ncol = 6 if n_series > 40 else 5 if n_series > 28 else 4 if n_series > 20 else 3
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.16),
        ncol=ncol,
        frameon=True,
        fontsize=8
    )
    fig.subplots_adjust(bottom=0.24 if n_series > 12 else 0.18)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved chart: {out_png}")


def main():
    ap = argparse.ArgumentParser(description="CBRF scraper: from today back N days")
    ap.add_argument("--days", type=int, default=7, help="How many days to collect starting from today (default 7)")
    ap.add_argument("--headless", action="store_true", default=False, help="Run browser headless")
    ap.add_argument("--pick", nargs="*", default=None, help="Which currencies to plot (e.g. USD EUR CNY)")
    ap.add_argument("--all", dest="force_all", action="store_true", default=False,
                    help="Plot all currencies even if there are many")
    ap.add_argument("--indexed", action="store_true", help="Additionally save an indexed chart")
    ap.add_argument("--delta", action="store_true", help="Additionally save a delta RUB chart (vs first calendar day)")
    args = ap.parse_args()

    dfs = collect_from_today(days=args.days, headless=args.headless)
    if not dfs:
        print("No data collected.")
        return

    wide = merge_wide(dfs, pick=args.pick)
    out_csv = DATA_DIR / "week_merged.csv"
    wide.to_csv(out_csv, encoding="utf-8")
    print(f"Saved merged table: {out_csv}")

    abs_png = _unique_plot_path("absolute", wide)
    plot_wide(wide, abs_png, mode="absolute", top_k=8, force_all=args.force_all)

    if args.delta:
        dlt_png = _unique_plot_path("delta", wide)
        plot_wide(wide, dlt_png, mode="delta", top_k=8, force_all=args.force_all)

    if args.indexed:
        idx_png = _unique_plot_path("indexed", wide)
        plot_wide(wide, idx_png, mode="indexed", top_k=8, force_all=args.force_all)


if __name__ == "__main__":
    main()

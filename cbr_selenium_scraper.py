import argparse
import datetime as dt
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

def today_url() -> str:
    return build_url_for_date(dt.date.today())

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
        return pd.DataFrame(columns=["NumCode","CharCode","Nominal","Name","Value","ValuePerUnit","Date"])

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

def select_top_movers(wide: pd.DataFrame, k: int = 8) -> pd.DataFrame:
    if wide.shape[1] <= k:
        return wide

    rel = wide.pct_change().dropna()
    movers = rel.std().sort_values(ascending=False).head(k).index
    return wide[movers]

def plot_wide(
    wide: pd.DataFrame,
    out_png: Path,
    mode: str = "indexed",   # 'indexed' или 'absolute'
    top_k: int = 8,
    force_all: bool = False
) -> None:
    if wide.empty:
        print("No data for plot.")
        return

    data = wide.copy()
    if not force_all and data.shape[1] > top_k:
        data = select_top_movers(data, k=top_k)

    x = pd.to_datetime(data.index)

    if mode.lower() == "indexed":
        base = data.iloc[0]
        data = (data.divide(base) * 100.0)
        y_label = "Index (day1 = 100)"
        title = "CBRF rates - indexed (day 1 = 100)"
    else:
        y_label = "RUB for 1 unit"
        title = "Rates CBRF (rub/1 unit)"

    fig, ax = plt.subplots(figsize=(11, 5.5))
    for col in data.columns:
        ax.plot(x, data[col], marker="o", linewidth=1.8, markersize=4, alpha=0.9, label=col)

    ax.set_title(title, pad=12)
    ax.set_xlabel("Data")
    ax.set_ylabel(y_label)
    ax.grid(True, alpha=0.3)
    ax.margins(x=0.02, y=0.08)
    fig.autofmt_xdate(rotation=30, ha="right")

    ax.legend(
        loc="upper left",
        bbox_to_anchor=(1.02, 1.0),
        borderaxespad=0.0,
        frameon=True
    )

    fig.tight_layout(rect=[0, 0, 0.82, 1])
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"[INFO] Plot saved: {out_png}")

def main():
    ap = argparse.ArgumentParser(description="Scraper CBRF: for current date back to N days")
    ap.add_argument("--days", type=int, default=7, help="How many days to collect starting from today (default 7)")
    ap.add_argument("--headless", action="store_true", default=False, help="Without GUI browser")
    ap.add_argument("--pick", nargs="*", default=None, help="Which currencies to withdraw")
    args = ap.parse_args()

    dfs = collect_from_today(days=args.days, headless=args.headless)
    if not dfs:
        print("No data recieved.")
        return

    wide = merge_wide(dfs, pick=args.pick)
    out_csv = DATA_DIR / "week_merged.csv"
    wide.to_csv(out_csv, encoding="utf-8")
    print(f"[INFO] Merged table saved: {out_csv}")

    out_png = DATA_DIR / "week_plot.png"
    plot_wide(wide, out_png)

if __name__ == "__main__":
    main()

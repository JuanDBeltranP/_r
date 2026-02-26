from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from zoneinfo import ZoneInfo
import pandas as pd
import pdfplumber

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request


# -------------------------
# CONFIG
# -------------------------
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
LONDON_TZ = ZoneInfo("Europe/London")


# -------------------------
# OAuth auth helper
# -------------------------
def get_drive_service(credentials_json_path: str, token_path: str):
    creds = None
    token_file = Path(token_path)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_json_path, SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds)


# -------------------------
# Drive helpers
# -------------------------
def find_folder_id_by_name(service, folder_name: str) -> str:
    q = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{folder_name}' "
        "and trashed=false"
    )
    resp = service.files().list(q=q, fields="files(id, name)").execute()
    folders = resp.get("files", [])
    if not folders:
        raise FileNotFoundError(f"No folder found named '{folder_name}' in Drive.")
    return folders[0]["id"]


def _to_rfc3339(dt: datetime) -> str:
    dt_utc = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.isoformat().replace("+00:00", "Z")


def _parse_rfc3339(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def list_recent_pdfs_in_folder(service, folder_id: str, after_dt: datetime) -> List[Dict]:
    after_rfc = _to_rfc3339(after_dt)
    q = (
        f"'{folder_id}' in parents and "
        "mimeType='application/pdf' and trashed=false and "
        f"(createdTime >= '{after_rfc}' or modifiedTime >= '{after_rfc}')"
    )

    files: List[Dict] = []
    page_token: Optional[str] = None

    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken, files(id, name, createdTime, modifiedTime, md5Checksum, size)",
            orderBy="modifiedTime desc",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return files


def download_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


# -------------------------
# Filename date parsing
# -------------------------
SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _norm(s: str) -> str:
    return (
        s.lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    )


def parse_report_date_from_filename(name: str) -> Optional[datetime]:
    """
    Supports:
      - 2026-02-23T14-04 ...
      - ... 23-02-2026
      - ... al 23 de Febrero de 2026
    If multiple dates exist, returns the latest.
    """
    s = _norm(Path(name).stem)
    found: List[datetime] = []

    # YYYY-MM-DD
    for m in re.finditer(r"\b(20\d{2})[-_\.](\d{2})[-_\.](\d{2})\b", s):
        y, mo, d = map(int, m.groups())
        try:
            found.append(datetime(y, mo, d, tzinfo=LONDON_TZ))
        except ValueError:
            pass

    # DD-MM-YYYY
    for m in re.finditer(r"\b(\d{2})[-_\.](\d{2})[-_\.](20\d{2})\b", s):
        d, mo, y = map(int, m.groups())
        try:
            found.append(datetime(y, mo, d, tzinfo=LONDON_TZ))
        except ValueError:
            pass

    # 'al 23 de febrero de 2026' (with or without 'al')
    for m in re.finditer(r"\b(?:al\s+)?(\d{1,2})\s*de\s*([a-z]+)\s*de\s*(20\d{2})\b", s):
        d = int(m.group(1))
        month_name = m.group(2)
        y = int(m.group(3))
        mo = SPANISH_MONTHS.get(month_name)
        if not mo:
            continue
        try:
            found.append(datetime(y, mo, d, tzinfo=LONDON_TZ))
        except ValueError:
            pass

    return max(found) if found else None


# -------------------------
# PDF extraction
# -------------------------
def extract_exportaciones_table_last_page_from_bytes(
    pdf_bytes: bytes,
    x_tolerance_words: float = 2.0,
    crop_top_offset: float = 36.0,
    crop_bottom_offset: float = 2.0,
) -> pd.DataFrame:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            raise ValueError("PDF has 0 pages")

        page = pdf.pages[-1]
        words = page.extract_words(x_tolerance=x_tolerance_words)
        if not words:
            raise ValueError("No words extracted from last page")

        header_idx = next(
            (
                i
                for i, (w, nxt) in enumerate(zip(words, words[1:]))
                if "Exportaciones" in (w.get("text") or "")
                and "Mensuales" in (nxt.get("text") or "")
            ),
            None,
        )
        if header_idx is None:
            raise ValueError("Pattern 'Exportaciones Mensuales' not found on last page")

        header_box = words[header_idx]
        total_box = next((w for w in words[header_idx + 1 :] if "Total" in (w.get("text") or "")), None)
        if total_box is None:
            raise ValueError("'Total' not found after 'Exportaciones Mensuales'")

        bbox = (
            page.bbox[0],
            header_box["bottom"] + crop_top_offset,
            page.bbox[2],
            total_box["top"] - crop_bottom_offset,
        )
        cropped = page.crop(bbox)

        table_settings = {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "text_y_tolerance": 5,
            "text_x_tolerance": 7,
            "snap_x_tolerance": 15,
            "snap_y_tolerance": 8,
        }

        tables = cropped.extract_tables(table_settings)

    if not tables or not tables[0]:
        raise ValueError("No table extracted from cropped region")

    return pd.DataFrame(tables[0]).dropna(axis=1, how="all")


def promote_header_row_if_needed(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        return df

    first_row = df.iloc[0].astype(str).str.lower().tolist()
    header_keywords = ["month", "mes", "bags", "price", "average", "promedio", "precio", "percent", "porcent"]
    if any(any(k in cell for k in header_keywords) for cell in first_row):
        df.columns = df.iloc[0].astype(str).tolist()
        df = df.iloc[1:].reset_index(drop=True)

    df.columns = [str(c).strip() for c in df.columns]
    return df


def clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = promote_header_row_if_needed(df)
    df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)
    df = df.replace({"#DIV/0!": pd.NA, "": pd.NA, None: pd.NA})

    for col in df.columns:
        if re.search(r"percent|porcent|%", str(col), flags=re.IGNORECASE):
            s = df[col].astype(str).str.replace("%", "", regex=False).str.replace(",", "", regex=False)
            df[col] = pd.to_numeric(s, errors="coerce") / 100.0
    return df


# -------------------------
# Dedupe + processed log
# -------------------------
def load_processed_log(log_path: Path) -> pd.DataFrame:
    if log_path.exists():
        return pd.read_csv(log_path)
    return pd.DataFrame(columns=["file_id", "md5Checksum", "name", "processed_at_london"])


def append_processed_log(log_path: Path, rows: List[Dict]) -> None:
    df_new = pd.DataFrame(rows)
    if log_path.exists():
        df_old = pd.read_csv(log_path)
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new
    df_all.drop_duplicates(subset=["file_id"], keep="last").to_csv(log_path, index=False)


def dedupe_by_md5_keep_newest(files: List[Dict]) -> List[Dict]:
    kept = []
    seen = set()
    for f in sorted(files, key=lambda x: x.get("modifiedTime", ""), reverse=True):
        md5 = f.get("md5Checksum")
        if md5:
            if md5 in seen:
                continue
            seen.add(md5)
        kept.append(f)
    return kept


# -------------------------
# Historical append helpers
# -------------------------
def append_to_historical_csv(historical_path: Path, new_df: pd.DataFrame) -> pd.DataFrame:
    """
    Append new_df into historical_path and dedupe.
    Dedupe strategy: exact-row dedupe across all columns.
    (Safe when schema is variable / unknown.)
    """
    if historical_path.exists():
        old = pd.read_csv(historical_path)
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df.copy()

    combined = combined.drop_duplicates()
    combined.to_csv(historical_path, index=False, encoding="utf-8")
    return combined


# -------------------------
# Parallel worker
# -------------------------
def process_one_pdf(pdf_bytes: bytes, meta: Dict, run_ts: str) -> pd.DataFrame:
    raw = extract_exportaciones_table_last_page_from_bytes(pdf_bytes)
    df = clean_table(raw)

    report_name = Path(meta["name"]).stem

    df.insert(0, "run_timestamp_london", run_ts)
    df.insert(1, "report_name", report_name)
    df.insert(2, "file_id", meta["id"])
    df.insert(3, "md5Checksum", meta.get("md5Checksum"))
    df.insert(4, "drive_createdTime", meta["createdTime"])
    df.insert(5, "drive_modifiedTime", meta["modifiedTime"])
    return df


# -------------------------
# MAIN run (single cron-triggerable run)
# -------------------------
def run_once(
    folder_name: str,
    repo_root: Path,
    credentials_json: Path,
    token_json: Path,
    drive_days_window: int = 7,      # broad Drive filter
    report_days_window: int = 3,     # strict filename report-date filter (fallback to modifiedTime)
    max_workers: int = 8,            # pdf parsing concurrency
):
    service = get_drive_service(str(credentials_json), str(token_json))
    folder_id = find_folder_id_by_name(service, folder_name)

    out_latest = repo_root / "outputs" / "latest"
    out_hist = repo_root / "outputs" / "historical"
    logs_dir = repo_root / "logs"
    out_latest.mkdir(parents=True, exist_ok=True)
    out_hist.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    processed_log = out_hist / "processed_files.csv"
    historical_csv = out_hist / "honduras_reports_historical.csv"

    # If historical file does NOT exist → full backfill
    if not historical_csv.exists():
        print("🔁 First run detected: full historical backfill.")
        pdfs = list_recent_pdfs_in_folder(service, folder_id, after_dt=datetime(2000, 1, 1, tzinfo=timezone.utc))
        report_days_window = 10000  # effectively disable report cutoff
    else:
        now_utc = datetime.now(timezone.utc)
        after_dt = now_utc - timedelta(days=drive_days_window)
        pdfs = list_recent_pdfs_in_folder(service, folder_id, after_dt=after_dt)


    pdfs = dedupe_by_md5_keep_newest(pdfs)

    processed_df = load_processed_log(processed_log)
    processed_ids = set(processed_df["file_id"].astype(str).tolist()) if not processed_df.empty else set()
    processed_md5 = set(processed_df["md5Checksum"].dropna().astype(str).tolist()) if not processed_df.empty else set()

    now_london = datetime.now(LONDON_TZ)
    report_cutoff = (now_london - timedelta(days=report_days_window)).replace(hour=0, minute=0, second=0, microsecond=0)

    preflight_rows: List[Dict] = []
    eligible: List[Dict] = []

    for f in pdfs:
        file_id = f["id"]
        name = f["name"]
        md5 = f.get("md5Checksum")
        created_l = _parse_rfc3339(f["createdTime"]).astimezone(LONDON_TZ)
        modified_l = _parse_rfc3339(f["modifiedTime"]).astimezone(LONDON_TZ)

        reason = None
        if file_id in processed_ids or (md5 and md5 in processed_md5):
            reason = "already_processed"

        report_dt = parse_report_date_from_filename(name)
        if report_dt is None:
            report_dt = modified_l.replace(hour=0, minute=0, second=0, microsecond=0)

        if reason is None and report_dt < report_cutoff:
            reason = "old_report_date"

        preflight_rows.append(
            {
                "file_id": file_id,
                "name": name,
                "md5Checksum": md5,
                "drive_created_london": created_l.isoformat(),
                "drive_modified_london": modified_l.isoformat(),
                "parsed_report_date_london": report_dt.isoformat(),
                "eligible": reason is None,
                "skip_reason": reason or "",
            }
        )

        if reason is None:
            eligible.append(f)

    preflight_df = pd.DataFrame(preflight_rows)
    preflight_df.to_csv(out_latest / "preflight_latest.csv", index=False, encoding="utf-8")

    if not eligible:
        (out_latest / "honduras_reports_latest.csv").write_text("", encoding="utf-8")
        print("No eligible PDFs to process this run.")
        return 0

    all_dfs: List[pd.DataFrame] = []
    errors: List[Dict] = []
    processed_rows_to_add: List[Dict] = []
    run_ts = datetime.now(LONDON_TZ).isoformat()

    # 1) Download sequentially (more stable for Drive)
    downloads: List[Dict] = []
    for f in eligible:
        try:
            pdf_bytes = download_file_bytes(service, f["id"])
            downloads.append({"meta": f, "bytes": pdf_bytes})
        except Exception as e:
            errors.append(
                {
                    "report_name": Path(f["name"]).stem,
                    "file": f["name"],
                    "file_id": f["id"],
                    "error": f"download_failed: {e}",
                }
            )
            print(f"❌ download {f['name']}: {e}")

    if not downloads:
        if errors:
            pd.DataFrame(errors).to_csv(out_latest / "honduras_reports_errors_latest.csv", index=False, encoding="utf-8")
        raise RuntimeError("All eligible PDFs failed during download.")

    # 2) Parse/extract in parallel
    max_workers = max(1, min(int(max_workers), (os.cpu_count() or 4) * 2))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(process_one_pdf, item["bytes"], item["meta"], run_ts): item["meta"] for item in downloads}

        for fut in as_completed(futures):
            meta = futures[fut]
            name = meta["name"]
            report_name = Path(name).stem
            try:
                df = fut.result()
                all_dfs.append(df)

                processed_rows_to_add.append(
                    {
                        "file_id": meta["id"],
                        "md5Checksum": meta.get("md5Checksum"),
                        "name": name,
                        "processed_at_london": run_ts,
                    }
                )

                print(f"✅ {name}: {len(df)} rows")
            except Exception as e:
                errors.append({"report_name": report_name, "file": name, "file_id": meta["id"], "error": str(e)})
                print(f"❌ {name}: {e}")

    if processed_rows_to_add:
        append_processed_log(processed_log, processed_rows_to_add)

    if not all_dfs:
        if errors:
            pd.DataFrame(errors).to_csv(out_latest / "honduras_reports_errors_latest.csv", index=False, encoding="utf-8")
        raise RuntimeError("All eligible PDFs failed in this run.")

    latest_df = pd.concat(all_dfs, ignore_index=True)

    # Save latest batch (this run)
    latest_df.to_csv(out_latest / "honduras_reports_latest.csv", index=False, encoding="utf-8")

    # Append into historical (deduped)
    _ = append_to_historical_csv(historical_csv, latest_df)

    # Save errors if any
    if errors:
        pd.DataFrame(errors).to_csv(out_latest / "honduras_reports_errors_latest.csv", index=False, encoding="utf-8")

    print(f"Saved latest + appended historical. Historical file: {historical_csv}")
    return 0


if __name__ == "__main__":
    # Repo root (assuming this file lives in scripts/)
    REPO_ROOT = Path(__file__).resolve().parents[1]

    # ---- OPTION B: hardcode external credential paths (outside repo) ----
    # Change these two to your actual absolute paths:
    CREDENTIALS_JSON = Path("/Users/juanbeltran/Developer/Honduras_coffee/credentials.json")
    TOKEN_JSON = Path("/Users/juanbeltran/Developer/Honduras_coffee/token.json")

    raise SystemExit(
        run_once(
            folder_name="Honduras Reports",
            repo_root=REPO_ROOT,
            credentials_json=CREDENTIALS_JSON,
            token_json=TOKEN_JSON,
            drive_days_window=7,
            report_days_window=3,
            max_workers=8,
        )
    )

print("Completed!")
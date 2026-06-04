from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

import pandas as pd
import pdfplumber
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine

from pydantic import BaseModel
from backend.models import UploadRecord, SateiItem


class SateiItemUpdate(BaseModel):
    asset_id: str | None = None
    serial: str | None = None
    maker: str | None = None
    category: str | None = None
    model_name: str | None = None
    model_number: str | None = None
    device_type: str | None = None
    condition: str | None = None
    cpu: str | None = None
    memory: str | None = None
    storage: str | None = None
    storage_serial: str | None = None
    weight: str | None = None
    remarks: str | None = None
    price: int | None = None
    status: str | None = None


POSTGRES_URL = os.getenv("POSTGRES_URL", "sqlite:///./ai_satei.db")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
ALLOWED_EXTENSIONS = {".csv", ".xls", ".xlsx", ".pdf"}
KNOWN_MAKERS = [
    "Dell",
    "dynabook",
    "HP",
    "NEC",
    "Panasonic",
    "Microsoft",
    "VAIO",
    "BUFFALO",
    "ELECOM",
    "CiscoSYSTEMS",
    "Cisco",
    "corega",
    "RATOC",
    "Raritan",
    "サンワサプライ",
    "アライドテレシス",
    "ネクストコム",
]
ASSET_ID_PATTERNS = [
    re.compile(r"\b\d{2}[DX]\d{3}\b"),
    re.compile(r"\bPC-[A-Z]{2}-\d{5}\b"),
    re.compile(r"\bIKI-\d{5}\b"),
    re.compile(r"ハイキ-\d{2}-\d{2}-\d{3}"),
]
CATEGORY_PATTERN = re.compile(r"\b\d{2}\s*[.．]\s*[^\t\r\n　 ]+")
YEN_PATTERN = re.compile(r"([0-9０-９][0-9０-９,，]{2,})\s*円")
KEYWORD_PRICE_PATTERN = re.compile(
    r"(?:価格|金額|査定|見積|買取|卸)[^0-9０-９]{0,20}"
    r"([0-9０-９][0-9０-９,，]{2,})"
)

engine = create_engine(POSTGRES_URL, pool_pre_ping=True)

app = FastAPI(title="AI Satei API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def init_db() -> None:
    for attempt in range(30):
        try:
            SQLModel.metadata.create_all(engine)
            return
        except OperationalError:
            if attempt == 29:
                raise
            sleep(1)


@app.on_event("startup")
def on_startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/upload")
async def upload(file: UploadFile) -> dict[str, object]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename is required")

    extension = Path(file.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="only CSV, Excel, and PDF files are supported",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="file is empty")

    target = UPLOAD_DIR / Path(file.filename).name
    target.write_bytes(content)

    # 1. パースしてアイテム候補を抽出
    parsed_items = parse_file_to_items(extension, content)

    # 2. 初期サマリーの計算
    total_price = sum(item["price"] for item in parsed_items if item["price"] is not None)
    priced_count = sum(1 for item in parsed_items if item["price"] is not None)
    summary = f"{len(parsed_items)} items, {priced_count} priced, estimated total {total_price:,} yen"

    record = UploadRecord(
        filename=target.name,
        content_type=file.content_type,
        size_bytes=len(content),
        summary=summary,
    )

    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)

        # SateiItem の保存
        satei_items = []
        for p_item in parsed_items:
            db_item = SateiItem(
                upload_record_id=record.id,
                asset_id=p_item.get("asset_id"),
                serial=p_item.get("serial"),
                maker=p_item.get("maker"),
                category=p_item.get("category"),
                model_name=p_item.get("model_name"),
                model_number=p_item.get("model_number"),
                device_type=p_item.get("device_type"),
                condition=p_item.get("condition"),
                cpu=p_item.get("cpu"),
                memory=p_item.get("memory"),
                storage=p_item.get("storage"),
                storage_serial=p_item.get("storage_serial"),
                weight=p_item.get("weight"),
                remarks=p_item.get("remarks"),
                original_price=p_item.get("original_price"),
                price=p_item.get("price"),
                raw_text=p_item.get("raw_text"),
            )
            session.add(db_item)
            satei_items.append(db_item)
        session.commit()
        
        # 登録後の全アイテムを読み出す
        for item in satei_items:
            session.refresh(item)

        # レスポンス用にdict化してシリアライズ
        items_list = [item.dict() for item in satei_items]

    # 集計データを作成
    categories = Counter(item["category"] for item in parsed_items if item["category"])
    makers = Counter(item["maker"] for item in parsed_items if item["maker"])

    return {
        "id": record.id,
        "filename": record.filename,
        "size_bytes": record.size_bytes,
        "summary": record.summary,
        "status": "ok",
        "record_count": len(parsed_items),
        "priced_count": priced_count,
        "estimated_total": total_price,
        "category_counts": dict(categories),
        "maker_counts": dict(makers),
        "items": items_list,
    }


@app.post("/upload-text")
async def upload_text(
    text: str = Form(...),
    filename: str | None = Form(default=None),
) -> dict[str, object]:
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    requested_name = Path(filename or "pasted-text.txt").name
    if Path(requested_name).suffix.lower() != ".txt":
        requested_name = f"{requested_name}.txt"

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    target = UPLOAD_DIR / f"{timestamp}-{requested_name}"
    content = text.encode("utf-8")
    target.write_bytes(content)

    # 1. パースしてアイテム候補を抽出
    parsed_items = parse_text_to_items(text)

    # 2. 初期サマリーの計算
    total_price = sum(item["price"] for item in parsed_items if item["price"] is not None)
    priced_count = sum(1 for item in parsed_items if item["price"] is not None)
    summary = f"{len(parsed_items)} items, {priced_count} priced, estimated total {total_price:,} yen"

    record = UploadRecord(
        filename=target.name,
        content_type="text/plain; charset=utf-8",
        size_bytes=len(content),
        summary=summary,
    )

    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)

        # SateiItem の保存
        satei_items = []
        for p_item in parsed_items:
            db_item = SateiItem(
                upload_record_id=record.id,
                asset_id=p_item.get("asset_id"),
                serial=p_item.get("serial"),
                maker=p_item.get("maker"),
                category=p_item.get("category"),
                model_name=p_item.get("model_name"),
                model_number=p_item.get("model_number"),
                device_type=p_item.get("device_type"),
                condition=p_item.get("condition"),
                cpu=p_item.get("cpu"),
                memory=p_item.get("memory"),
                storage=p_item.get("storage"),
                storage_serial=p_item.get("storage_serial"),
                weight=p_item.get("weight"),
                remarks=p_item.get("remarks"),
                original_price=p_item.get("original_price"),
                price=p_item.get("price"),
                raw_text=p_item.get("raw_text"),
            )
            session.add(db_item)
            satei_items.append(db_item)
        session.commit()

        for item in satei_items:
            session.refresh(item)

        items_list = [item.dict() for item in satei_items]

    categories = Counter(item["category"] for item in parsed_items if item["category"])
    makers = Counter(item["maker"] for item in parsed_items if item["maker"])

    return {
        "id": record.id,
        "filename": record.filename,
        "size_bytes": record.size_bytes,
        "summary": record.summary,
        "status": "ok",
        "record_count": len(parsed_items),
        "priced_count": priced_count,
        "estimated_total": total_price,
        "category_counts": dict(categories),
        "maker_counts": dict(makers),
        "items": items_list,
    }


def parse_text_to_items(text: str) -> list[dict[str, any]]:
    items = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    # 簡易的にテーブル形式か判定
    is_table = False
    for line in lines[:5]:
        parts = line.split("\t")
        if len(parts) >= 2 and any(p.strip().isdigit() for p in parts[:3]):
            is_table = True
            break
        if len(parts) >= 3 and any(any(h in p for h in ["No", "カテゴリ", "管理番号", "メーカー", "価格"]) for p in parts):
            is_table = True
            break

    if is_table:
        header_map = {}
        header_row_index = -1
        
        # 最初の3行からヘッダー行を特定する
        for i, line in enumerate(lines[:3]):
            parts = [p.replace('"', '').strip() for p in line.split("\t")]
            if any(any(h in p for h in ["No", "カテゴリ", "管理番号", "メーカー", "価格", "シリアル", "モデル"]) for p in parts):
                header_row_index = i
                for idx, p in enumerate(parts):
                    p_clean = p.lower()
                    if any(x in p_clean for x in ["no.", "no"]):
                        header_map["no"] = idx
                    elif any(x in p_clean for x in ["管理番号", "管理", "資産"]):
                        header_map["asset_id"] = idx
                    elif any(x in p_clean for x in ["シリアルナンバー", "本体シリアル", "シリアル"]):
                        header_map["serial"] = idx
                    elif any(x in p_clean for x in ["カテゴリ", "分類"]):
                        header_map["category"] = idx
                    elif any(x in p_clean for x in ["メーカー", "製造元"]):
                        header_map["maker"] = idx
                    elif any(x in p_clean for x in ["機種"]):
                        header_map["model_name"] = idx
                    elif any(x in p_clean for x in ["モデル型番", "型番"]):
                        header_map["model_number"] = idx
                    elif any(x in p_clean for x in ["タイプ"]):
                        header_map["device_type"] = idx
                    elif any(x in p_clean for x in ["状態", "故障"]):
                        header_map["condition"] = idx
                    elif any(x in p_clean for x in ["cpu"]):
                        header_map["cpu"] = idx
                    elif any(x in p_clean for x in ["メモリ"]):
                        header_map["memory"] = idx
                    elif any(x in p_clean for x in ["ストレージシリアル"]):
                        header_map["storage_serial"] = idx
                    elif any(x in p_clean for x in ["ストレージ"]):
                        header_map["storage"] = idx
                    elif any(x in p_clean for x in ["重量"]):
                        header_map["weight"] = idx
                    elif any(x in p_clean for x in ["備考", "メモ"]):
                        header_map["remarks"] = idx
                    elif any(x in p_clean for x in ["推定卸価格", "卸価格", "査定", "価格", "金額"]):
                        header_map["price"] = idx
                break

        start_row = header_row_index + 1 if header_row_index != -1 else 0
        
        for line in lines[start_row:]:
            parts = [p.replace('"', '').strip() for p in line.split("\t")]
            if len(parts) < 2:
                continue

            if header_map:
                asset_id = parts[header_map["asset_id"]] if "asset_id" in header_map and header_map["asset_id"] < len(parts) else None
                serial = parts[header_map["serial"]] if "serial" in header_map and header_map["serial"] < len(parts) else None
                maker = parts[header_map["maker"]] if "maker" in header_map and header_map["maker"] < len(parts) else None
                category = parts[header_map["category"]] if "category" in header_map and header_map["category"] < len(parts) else None
                model_name = parts[header_map["model_name"]] if "model_name" in header_map and header_map["model_name"] < len(parts) else None
                model_number = parts[header_map["model_number"]] if "model_number" in header_map and header_map["model_number"] < len(parts) else None
                device_type = parts[header_map["device_type"]] if "device_type" in header_map and header_map["device_type"] < len(parts) else None
                condition = parts[header_map["condition"]] if "condition" in header_map and header_map["condition"] < len(parts) else None
                cpu = parts[header_map["cpu"]] if "cpu" in header_map and header_map["cpu"] < len(parts) else None
                memory = parts[header_map["memory"]] if "memory" in header_map and header_map["memory"] < len(parts) else None
                storage = parts[header_map["storage"]] if "storage" in header_map and header_map["storage"] < len(parts) else None
                storage_serial = parts[header_map["storage_serial"]] if "storage_serial" in header_map and header_map["storage_serial"] < len(parts) else None
                weight = parts[header_map["weight"]] if "weight" in header_map and header_map["weight"] < len(parts) else None
                remarks = parts[header_map["remarks"]] if "remarks" in header_map and header_map["remarks"] < len(parts) else None
                
                price = None
                if "price" in header_map and header_map["price"] < len(parts):
                    price_str = parts[header_map["price"]]
                    normalized = normalize_digits(price_str).replace(",", "").replace("円", "")
                    if normalized.isdigit():
                        price = int(normalized)
            else:
                # フォールバック
                no_index = next((idx for idx, p in enumerate(parts[:3]) if p.isdigit()), None)
                if no_index is None:
                    continue

                asset_id = parts[no_index]
                category = parts[no_index + 1] if len(parts) > no_index + 1 else None
                serial = model_name = model_number = device_type = condition = cpu = memory = storage = storage_serial = weight = remarks = None

                maker = None
                for p in parts:
                    for km in KNOWN_MAKERS:
                        if km.lower() in p.lower():
                            maker = km
                            break
                    if maker:
                        break

                price = None
                price_str = next((p for p in reversed(parts) if p), "")
                normalized = normalize_digits(price_str).replace(",", "").replace("円", "")
                if normalized.isdigit():
                    price = int(normalized)

            if asset_id == "": asset_id = None
            if serial == "": serial = None
            if maker == "": maker = None
            if category == "": category = None
            if model_name == "": model_name = None
            if model_number == "": model_number = None
            if device_type == "": device_type = None
            if condition == "": condition = None
            if cpu == "": cpu = None
            if memory == "": memory = None
            if storage == "": storage = None
            if storage_serial == "": storage_serial = None
            if weight == "": weight = None
            if remarks == "": remarks = None

            # メーカーを補完
            if not maker:
                for p in parts:
                    for km in KNOWN_MAKERS:
                        if km.lower() in p.lower():
                            maker = km
                            break
                    if maker:
                        break

            if asset_id or maker or category or price or serial or model_name or model_number or cpu or memory or storage:
                items.append({
                    "asset_id": asset_id,
                    "serial": serial,
                    "maker": maker,
                    "category": category,
                    "model_name": model_name,
                    "model_number": model_number,
                    "device_type": device_type,
                    "condition": condition,
                    "cpu": cpu,
                    "memory": memory,
                    "storage": storage,
                    "storage_serial": storage_serial,
                    "weight": weight,
                    "remarks": remarks,
                    "original_price": price,
                    "price": price,
                    "raw_text": line,
                })
    else:
        # 非構造化テキスト
        for line in lines:
            normalized_line = normalize_digits(line)
            asset_id = None
            for pattern in ASSET_ID_PATTERNS:
                matches = pattern.findall(normalized_line)
                if matches:
                    asset_id = matches[0]
                    break

            category = None
            cat_matches = CATEGORY_PATTERN.findall(normalized_line)
            if cat_matches:
                category = re.sub(r"\s+", " ", cat_matches[0])

            maker = None
            lower_line = normalized_line.lower()
            for km in KNOWN_MAKERS:
                if km.lower() in lower_line:
                    maker = km
                    break

            price = None
            prices = [
                normalize_price(match)
                for match in YEN_PATTERN.findall(normalized_line)
                + KEYWORD_PRICE_PATTERN.findall(normalized_line)
            ]
            valid_prices = [p for p in prices if p is not None]
            if valid_prices:
                price = valid_prices[0]

            if asset_id or maker or category or price:
                items.append({
                    "asset_id": asset_id,
                    "serial": None,
                    "maker": maker,
                    "category": category,
                    "model_name": None,
                    "model_number": None,
                    "device_type": None,
                    "condition": None,
                    "cpu": None,
                    "memory": None,
                    "storage": None,
                    "storage_serial": None,
                    "weight": None,
                    "remarks": None,
                    "original_price": price,
                    "price": price,
                    "raw_text": line,
                })
    return items


def parse_dataframe_to_items(df: pd.DataFrame) -> list[dict[str, any]]:
    asset_cols = ["no", "id", "管理", "資産", "コード", "番号", "銘柄", "シリアル"]
    serial_cols = ["シリアルナンバー", "本体シリアル", "シリアル", "serial"]
    maker_cols = ["メーカー", "ブランド", "製造"]
    cat_cols = ["カテゴリ", "分類", "品名", "商品", "タイプ", "機種名", "機種"]
    model_name_cols = ["機種", "機種名", "model_name"]
    model_number_cols = ["モデル型番", "型番", "モデル", "model_number"]
    device_type_cols = ["タイプ", "種別", "区分", "device_type"]
    condition_cols = ["状態", "故障", "ランク", "condition"]
    cpu_cols = ["cpu", "プロセッサ"]
    memory_cols = ["メモリ", "memory", "ram"]
    storage_cols = ["ストレージ", "storage", "hdd", "ssd"]
    storage_serial_cols = ["ストレージシリアル", "storage_serial"]
    weight_cols = ["重量", "重さ", "weight"]
    remarks_cols = ["備考", "メモ", "remarks", "note"]
    price_cols = ["価格", "金額", "査定", "見積", "単価", "値", "推定卸価格"]

    col_map = {}
    for col in df.columns:
        col_lower = str(col).lower()
        if any(x in col_lower for x in asset_cols) and "asset_id" not in col_map:
            col_map["asset_id"] = col
        elif any(x in col_lower for x in serial_cols) and "serial" not in col_map:
            col_map["serial"] = col
        elif any(x in col_lower for x in maker_cols) and "maker" not in col_map:
            col_map["maker"] = col
        elif any(x in col_lower for x in cat_cols) and "category" not in col_map:
            col_map["category"] = col
        elif any(x in col_lower for x in model_name_cols) and "model_name" not in col_map:
            col_map["model_name"] = col
        elif any(x in col_lower for x in model_number_cols) and "model_number" not in col_map:
            col_map["model_number"] = col
        elif any(x in col_lower for x in device_type_cols) and "device_type" not in col_map:
            col_map["device_type"] = col
        elif any(x in col_lower for x in condition_cols) and "condition" not in col_map:
            col_map["condition"] = col
        elif any(x in col_lower for x in cpu_cols) and "cpu" not in col_map:
            col_map["cpu"] = col
        elif any(x in col_lower for x in memory_cols) and "memory" not in col_map:
            col_map["memory"] = col
        elif any(x in col_lower for x in storage_cols) and "storage" not in col_map:
            col_map["storage"] = col
        elif any(x in col_lower for x in storage_serial_cols) and "storage_serial" not in col_map:
            col_map["storage_serial"] = col
        elif any(x in col_lower for x in weight_cols) and "weight" not in col_map:
            col_map["weight"] = col
        elif any(x in col_lower for x in remarks_cols) and "remarks" not in col_map:
            col_map["remarks"] = col
        elif any(x in col_lower for x in price_cols) and "price" not in col_map:
            col_map["price"] = col

    items = []
    for _, row in df.iterrows():
        asset_id = str(row[col_map["asset_id"]]).strip() if "asset_id" in col_map else None
        serial = str(row[col_map["serial"]]).strip() if "serial" in col_map else None
        maker = str(row[col_map["maker"]]).strip() if "maker" in col_map else None
        category = str(row[col_map["category"]]).strip() if "category" in col_map else None
        model_name = str(row[col_map["model_name"]]).strip() if "model_name" in col_map else None
        model_number = str(row[col_map["model_number"]]).strip() if "model_number" in col_map else None
        device_type = str(row[col_map["device_type"]]).strip() if "device_type" in col_map else None
        condition = str(row[col_map["condition"]]).strip() if "condition" in col_map else None
        cpu = str(row[col_map["cpu"]]).strip() if "cpu" in col_map else None
        memory = str(row[col_map["memory"]]).strip() if "memory" in col_map else None
        storage = str(row[col_map["storage"]]).strip() if "storage" in col_map else None
        storage_serial = str(row[col_map["storage_serial"]]).strip() if "storage_serial" in col_map else None
        weight = str(row[col_map["weight"]]).strip() if "weight" in col_map else None
        remarks = str(row[col_map["remarks"]]).strip() if "remarks" in col_map else None

        price = None
        if "price" in col_map:
            raw_p = row[col_map["price"]]
            if pd.notna(raw_p):
                normalized = normalize_digits(str(raw_p)).replace(",", "").replace("円", "")
                if normalized.split(".")[0].isdigit():
                    price = int(float(normalized))

        if not maker:
            for cell_val in row.values:
                cell_str = str(cell_val).lower()
                for km in KNOWN_MAKERS:
                    if km.lower() in cell_str:
                        maker = km
                        break
                if maker:
                    break

        if pd.isna(asset_id) or asset_id == "nan": asset_id = None
        if pd.isna(serial) or serial == "nan": serial = None
        if pd.isna(maker) or maker == "nan": maker = None
        if pd.isna(category) or category == "nan": category = None
        if pd.isna(model_name) or model_name == "nan": model_name = None
        if pd.isna(model_number) or model_number == "nan": model_number = None
        if pd.isna(device_type) or device_type == "nan": device_type = None
        if pd.isna(condition) or condition == "nan": condition = None
        if pd.isna(cpu) or cpu == "nan": cpu = None
        if pd.isna(memory) or memory == "nan": memory = None
        if pd.isna(storage) or storage == "nan": storage = None
        if pd.isna(storage_serial) or storage_serial == "nan": storage_serial = None
        if pd.isna(weight) or weight == "nan": weight = None
        if pd.isna(remarks) or remarks == "nan": remarks = None

        raw_text = "\t".join([str(val) for val in row.values if pd.notna(val)])

        if asset_id or maker or category or price:
            items.append({
                "asset_id": asset_id,
                "maker": maker,
                "category": category,
                "original_price": price,
                "price": price,
                "raw_text": raw_text,
            })
    return items


def parse_file_to_items(extension: str, content: bytes) -> list[dict[str, any]]:
    items = []
    try:
        if extension == ".csv":
            df = pd.read_csv(io.BytesIO(content))
            items = parse_dataframe_to_items(df)
        elif extension in {".xls", ".xlsx"}:
            df = pd.read_excel(io.BytesIO(content))
            items = parse_dataframe_to_items(df)
        elif extension == ".pdf":
            text_lines = []
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        text_lines.append(text)
            full_text = "\n".join(text_lines)
            items = parse_text_to_items(full_text)
    except Exception as e:
        print(f"Error parsing file: {e}")
    return items


def normalize_digits(value: str) -> str:
    return value.translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))


def normalize_price(value: str) -> int | None:
    normalized = normalize_digits(value).replace(",", "")
    if normalized.isdigit():
        return int(normalized)
    return None


@app.get("/upload-records", response_model=None)
def get_upload_records() -> list[dict[str, any]]:
    with Session(engine) as session:
        from sqlmodel import select
        statement = select(UploadRecord).order_by(UploadRecord.created_at.desc())
        results = session.exec(statement).all()
        # レスポンス用に各レコードのdict化
        records_list = []
        for r in results:
            d = r.dict()
            # 日時をISO形式文字列に変換
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            records_list.append(d)
        return records_list


@app.get("/upload-records/{record_id}/items", response_model=None)
def get_satei_items(record_id: int) -> list[dict[str, any]]:
    with Session(engine) as session:
        from sqlmodel import select
        statement = select(SateiItem).where(SateiItem.upload_record_id == record_id).order_by(SateiItem.id)
        results = session.exec(statement).all()
        return [item.dict() for item in results]


@app.put("/satei-items/{item_id}", response_model=None)
def update_satei_item(item_id: int, updated: SateiItemUpdate) -> dict[str, any]:
    with Session(engine) as session:
        item = session.get(SateiItem, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        
        if updated.asset_id is not None:
            item.asset_id = updated.asset_id
        if updated.serial is not None:
            item.serial = updated.serial
        if updated.maker is not None:
            item.maker = updated.maker
        if updated.category is not None:
            item.category = updated.category
        if updated.model_name is not None:
            item.model_name = updated.model_name
        if updated.model_number is not None:
            item.model_number = updated.model_number
        if updated.device_type is not None:
            item.device_type = updated.device_type
        if updated.condition is not None:
            item.condition = updated.condition
        if updated.cpu is not None:
            item.cpu = updated.cpu
        if updated.memory is not None:
            item.memory = updated.memory
        if updated.storage is not None:
            item.storage = updated.storage
        if updated.storage_serial is not None:
            item.storage_serial = updated.storage_serial
        if updated.weight is not None:
            item.weight = updated.weight
        if updated.remarks is not None:
            item.remarks = updated.remarks
        if updated.price is not None:
            item.price = updated.price
        if updated.status is not None:
            item.status = updated.status
            
        session.add(item)
        session.commit()
        session.refresh(item)
        
        recalculate_record_summary(item.upload_record_id, session)
        
        return item.dict()


def recalculate_record_summary(record_id: int, session: Session) -> None:
    record = session.get(UploadRecord, record_id)
    if not record:
        return
        
    from sqlmodel import select
    statement = select(SateiItem).where(SateiItem.upload_record_id == record_id)
    items = session.exec(statement).all()
    
    total_price = sum(item.price for item in items if item.price is not None)
    priced_count = sum(1 for item in items if item.price is not None)
    
    summary = f"{len(items)} items, {priced_count} priced, estimated total {total_price:,} yen"
    record.summary = summary
    session.add(record)
    session.commit()

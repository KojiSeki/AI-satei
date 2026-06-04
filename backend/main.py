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
    maker: str | None = None
    category: str | None = None
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
                asset_id=p_item["asset_id"],
                maker=p_item["maker"],
                category=p_item["category"],
                original_price=p_item["original_price"],
                price=p_item["price"],
                raw_text=p_item["raw_text"],
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
                asset_id=p_item["asset_id"],
                maker=p_item["maker"],
                category=p_item["category"],
                original_price=p_item["original_price"],
                price=p_item["price"],
                raw_text=p_item["raw_text"],
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

    if is_table:
        for line in lines:
            parts = [p.strip() for p in line.split("\t")]
            no_index = next((idx for idx, p in enumerate(parts[:3]) if p.isdigit()), None)
            if no_index is None:
                continue

            asset_id = parts[no_index]
            category = parts[no_index + 1] if len(parts) > no_index + 1 else None

            # メーカーをKNOWN_MAKERSから探す
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

            items.append({
                "asset_id": asset_id,
                "maker": maker,
                "category": category,
                "original_price": price,
                "price": price,
                "raw_text": line,
            })
    else:
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
                    "maker": maker,
                    "category": category,
                    "original_price": price,
                    "price": price,
                    "raw_text": line,
                })
    return items


def parse_dataframe_to_items(df: pd.DataFrame) -> list[dict[str, any]]:
    asset_cols = ["no", "id", "管理", "資産", "コード", "番号", "銘柄"]
    maker_cols = ["メーカー", "ブランド", "製造"]
    cat_cols = ["カテゴリ", "分類", "品名", "商品", "タイプ", "機種名"]
    price_cols = ["価格", "金額", "査定", "見積", "単価", "値"]

    col_map = {}
    for col in df.columns:
        col_lower = str(col).lower()
        if any(x in col_lower for x in asset_cols) and "asset_id" not in col_map:
            col_map["asset_id"] = col
        elif any(x in col_lower for x in maker_cols) and "maker" not in col_map:
            col_map["maker"] = col
        elif any(x in col_lower for x in cat_cols) and "category" not in col_map:
            col_map["category"] = col
        elif any(x in col_lower for x in price_cols) and "price" not in col_map:
            col_map["price"] = col

    items = []
    for _, row in df.iterrows():
        asset_id = str(row[col_map["asset_id"]]).strip() if "asset_id" in col_map else None
        maker = str(row[col_map["maker"]]).strip() if "maker" in col_map else None
        category = str(row[col_map["category"]]).strip() if "category" in col_map else None

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

        if pd.isna(asset_id) or asset_id == "nan":
            asset_id = None
        if pd.isna(maker) or maker == "nan":
            maker = None
        if pd.isna(category) or category == "nan":
            category = None

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
        if updated.maker is not None:
            item.maker = updated.maker
        if updated.category is not None:
            item.category = updated.category
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

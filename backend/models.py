from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class UploadRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    filename: str
    content_type: str | None = None
    size_bytes: int
    status: str = "stored"
    summary: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SateiItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    upload_record_id: int = Field(foreign_key="uploadrecord.id", index=True)
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
    original_price: int | None = None
    price: int | None = None
    raw_text: str | None = None
    status: str = "pending"  # "pending", "completed"


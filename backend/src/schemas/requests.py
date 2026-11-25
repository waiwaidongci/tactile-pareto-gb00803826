from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


class Import(BaseModel):
    # 旧版合并 CSV 仍只要求 name/csv；分开交单时 name 可缺省并由班次/类型补全。
    model_config = ConfigDict(extra='allow')

    name: str = Field(default='', max_length=180)
    csv: str | None = Field(default=None, max_length=1_000_000)
    rows: list[dict] | None = None
    records: list[dict] | None = None
    shift: str | None = Field(default=None, max_length=80)
    shift_code: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, max_length=80)
    shift_id: int | None = None
    kind: str | None = None
    form_type: str | None = None
    doc_type: str | None = None
    type: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=120)
    request_key: str | None = Field(default=None, max_length=120)


class Run(BaseModel):
    model_config = ConfigDict(extra='allow')

    dataset_id: int | None = Field(default=None, gt=0)
    shift_id: int | None = None
    shift: str | None = Field(default=None, max_length=80)
    shift_code: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, max_length=80)
    options: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=120)
    request_key: str | None = Field(default=None, max_length=120)


class Review(BaseModel):
    decision: Literal['approved', 'returned']
    note: str = Field(min_length=2, max_length=1000)


class Delivery(BaseModel):
    recipient: str = Field(min_length=2, max_length=100)
    cadence: Literal['once', 'daily']
    scheduled_at: datetime | None = None

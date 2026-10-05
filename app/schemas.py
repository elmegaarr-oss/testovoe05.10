from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime
import uuid

class PaymentCreate(BaseModel):
    amount: float = Field(..., gt=0)
    currency: str = Field(..., min_length=3, max_length=3)
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    webhook_url: str

class PaymentResponse(BaseModel):
    payment_id: uuid.UUID
    status: str
    created_at: datetime

    class Config:
        from_attributes = True

class PaymentDetailResponse(BaseModel):
    id: uuid.UUID
    amount: float
    currency: str
    description: Optional[str]
    # Фикс: подменяем имя для клиента, забирая данные из payment_metadata
    metadata: Optional[Dict[str, Any]] = Field(None, alias="payment_metadata")
    status: str
    idempotency_key: str
    webhook_url: str
    created_at: datetime
    processed_at: Optional[datetime]

    class Config:
        from_attributes = True
        populate_by_name = True

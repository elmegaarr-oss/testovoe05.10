import uuid
from fastapi import APIRouter, Header, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import Payment, OutboxEvent
from app.schemas import PaymentCreate, PaymentResponse, PaymentDetailResponse

router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])

async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    from app.config import settings
    if x_api_key != settings.API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key")
    return x_api_key

@router.post("", response_model=PaymentResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_payment(
    payload: PaymentCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    # 1. Защита от дубликатов по Idempotency key
    stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
    result = await db.execute(stmt)
    existing_payment = result.scalar_one_or_none()
    
    if existing_payment:
        return PaymentResponse(
            payment_id=existing_payment.id,
            status=existing_payment.status,
            created_at=existing_payment.created_at
        )

    # 2. Создание сущностей в одной атомарной ACID-транзакции
    payment_id = uuid.uuid4()
    new_payment = Payment(
        id=payment_id,
        amount=payload.amount,
        currency=payload.currency,
        description=payload.description,
        payment_metadata=payload.metadata, # Мапим в корректное поле БД
        idempotency_key=idempotency_key,
        webhook_url=payload.webhook_url,
        status="pending"
    )
    
    outbox_event = OutboxEvent(
        event_type="payment.created",
        payload={
            "payment_id": str(payment_id),
            "amount": float(payload.amount),
            "currency": str(payload.currency),
            "webhook_url": str(payload.webhook_url)
        }
    )
    
    db.add(new_payment)
    db.add(outbox_event)
    await db.flush()
    await db.commit()
    
    return PaymentResponse(
        payment_id=new_payment.id,
        status=new_payment.status,
        created_at=new_payment.created_at
    )

@router.get("/{payment_id}", response_model=PaymentDetailResponse)
async def get_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    stmt = select(Payment).where(Payment.id == payment_id)
    result = await db.execute(stmt)
    payment = result.scalar_one_or_none()
    
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
        
    return payment

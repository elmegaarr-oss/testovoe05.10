import os

# Описание структуры и контента всех файлов проекта
project_files = {
    # 1. ЗАВИСИМОСТИ
    "requirements.txt": """
fastapi>=0.110.0
uvicorn>=0.28.0
pydantic>=2.6.0
pydantic-settings>=2.2.0
sqlalchemy>=2.0.28
asyncpg>=0.29.0
alembic>=1.13.1
faststream[rabbit]>=0.4.7
httpx>=0.27.0
tenacity>=8.2.3
""",

    # 2. НАСТРОЙКИ ПРИЛОЖЕНИЯ
    "app/config.py": """
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "payment_db"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASSWORD: str = "guest"
    
    API_KEY: str = "secret-token-123"

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
""",

    # 3. БАЗА ДАННЫХ И МОДЕЛИ
    "app/database.py": """
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

engine = create_async_engine(settings.database_url, echo=True)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with async_session() as session:
        yield session
""",

    "app/models.py": """
import uuid
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import String, Numeric, DateTime, Boolean
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class PaymentStatus(str, PyEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=True)
    status: Mapped[PaymentStatus] = mapped_column(String, default=PaymentStatus.PENDING)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    webhook_url: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
""",

    # 4. СХЕМЫ ВАЛИДАЦИИ
    "app/schemas.py": """
from pydantic import BaseModel, Field, HttpUrl
from typing import Dict, Any, Optional
from datetime import datetime
import uuid
from app.models import PaymentStatus

class PaymentCreate(BaseModel):
    amount: float = Field(..., gt=0, description="Сумма платежа")
    currency: str = Field(..., min_length=3, max_length=3, description="Валюта (RUB, USD, EUR)")
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    webhook_url: str = Field(..., description="URL для уведомления о результате")

class PaymentResponse(BaseModel):
    payment_id: uuid.UUID
    status: PaymentStatus
    created_at: datetime

    class Config:
        from_attributes = True

class PaymentDetailResponse(BaseModel):
    id: uuid.UUID
    amount: float
    currency: str
    description: Optional[str]
    metadata_json: Optional[Dict[str, Any]] = Field(None, alias="metadata")
    status: PaymentStatus
    idempotency_key: str
    webhook_url: str
    created_at: datetime
    processed_at: Optional[datetime]

    class Config:
        from_attributes = True
        populate_by_name = True
""",

    # 5. МАРШРУТЫ API
    "app/api.py": """
from fastapi import APIRouter, Header, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import Payment, OutboxEvent, PaymentStatus
from app.schemas import PaymentCreate, PaymentResponse, PaymentDetailResponse
import uuid

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
    # 1. Проверяем дубликаты по Idempotency key
    stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
    result = await db.execute(stmt)
    existing_payment = result.scalar_one_or_none()
    
    if existing_payment:
        return PaymentResponse(
            payment_id=existing_payment.id,
            status=existing_payment.status,
            created_at=existing_payment.created_at
        )

    # 2. Создаем платеж и Outbox событие в одной ACID-транзакции
    payment_id = uuid.uuid4()
    new_payment = Payment(
        id=payment_id,
        amount=payload.amount,
        currency=payload.currency,
        description=payload.description,
        metadata_json=payload.metadata,
        idempotency_key=idempotency_key,
        webhook_url=payload.webhook_url,
        status=PaymentStatus.PENDING
    )
    
    outbox_event = OutboxEvent(
        event_type="payment.created",
        payload={
            "payment_id": str(payment_id),
            "amount": float(payload.amount),
            "currency": payload.currency,
            "webhook_url": payload.webhook_url
        }
    )
    
    db.add(new_payment)
    db.add(outbox_event)
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
""",

    "app/main.py": """
from fastapi import FastAPI
from app.api import router

app = FastAPI(title="Async Payment Processing Service")
app.include_router(router)

@app.get("/health")
def health_check():
    return {"status": "ok"}
""",

    # 6. РАБОТА С ОЧЕРЕДЯМИ И OUTBOX РЕАЛИЗАЦИЯ
    "app/worker.py": """
import asyncio
import random
from datetime import datetime
import httpx
from faststream import FastStream
from faststream.rabbit import RabbitBroker, RabbitQueue, RabbitExchange, ExchangeType
from sqlalchemy import select
from app.config import settings
from app.database import async_session
from app.models import Payment, OutboxEvent, PaymentStatus
from tenacity import retry, stop_after_attempt, wait_exponential

# Конфигурация RabbitMQ брокера
broker = RabbitBroker(f"amqp://{settings.RABBITMQ_USER}:{settings.RABBITMQ_PASSWORD}@{settings.RABBITMQ_HOST}:{settings.RABBITMQ_PORT}/")
app = FastStream(broker)

# Настройка основного Exchange, Очереди и DLQ
dlq_exchange = RabbitExchange("payments.dlq.exchange", type=ExchangeType.DIRECT)
dlq_queue = RabbitQueue("payments.dlq", exchange=dlq_exchange)

main_exchange = RabbitExchange("payments.exchange", type=ExchangeType.DIRECT)
main_queue = RabbitQueue(
    "payments.new", 
    exchange=main_exchange,
    arguments={
        "x-dead-letter-exchange": "payments.dlq.exchange",
        "x-dead-letter-routing-key": "payments.dlq"
    }
)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def send_webhook_with_retry(url: str, data: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=data, timeout=5.0)
        response.raise_for_status()

@broker.subscriber(main_queue)
async def process_payment_event(msg: dict):
    payment_id = msg["payment_id"]
    webhook_url = msg["webhook_url"]
    
    # 1. Эмуляция долгой обработки платежа (2-5 сек)
    await asyncio.sleep(random.uniform(2, 5))
    
    # Распределение: 90% успех, 10% ошибка
    final_status = PaymentStatus.SUCCEEDED if random.random() < 0.9 else PaymentStatus.FAILED
    
    async with async_session() as session:
        async with session.begin():
            stmt = select(Payment).where(Payment.id == payment_id)
            res = await session.execute(stmt)
            payment = res.scalar_one_or_none()
            
            if payment:
                payment.status = final_status
                payment.processed_at = datetime.utcnow()
                await session.commit()
    
    # 2. Отправка Webhook уведомления с повторными попытками
    # 2. Отправка Webhook уведомления с повторными попытками
    webhook_payload = {
        "payment_id": payment_id,
        "status": final_status.value,
        "amount": msg["amount"],
        "currency": msg["currency"],
        "processed_at": datetime.utcnow().isoformat()
    }
    
    try:
        await send_webhook_with_retry(webhook_url, webhook_payload)
    except Exception as e:
        print(f"Failed to deliver webhook for payment {payment_id} after 3 attempts: {e}")
        # Выбрасываем исключение, чтобы FastStream/RabbitMQ мог отправить сообщение в DLQ после исчерпания лимитов
        raise e

# Периодическая задача для паттерна Outbox (Эмуляция реле)
async def outbox_publisher():
    while True:
        await asyncio.sleep(1) # Опрос раз в секунду
        async with async_session() as session:
            stmt = select(OutboxEvent).where(OutboxEvent.processed == False).limit(10)
            res = await session.execute(stmt)
            events = res.scalars().all()
            
            for event in events:
                try:
                    await broker.publish(
                        message=event.payload,
                        queue=main_queue,
                        exchange=main_exchange
                    )
                    event.processed = True
                    await session.commit()
                except Exception as e:
                    print(f"Error publishing outbox event {event.id}: {e}")

@app.after_startup
async def start_outbox_loop():
    asyncio.create_task(outbox_publisher())
""",

    # 7. КОНТЕЙНЕРИЗАЦИЯ
    "Dockerfile": """
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
""",

    "docker-compose.yml": """
version: '3.8'

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: payment_db
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  rabbitmq:
    image: rabbitmq:3-management-alpine
    ports:
      - "5672:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: guest
      RABBITMQ_DEFAULT_PASS: guest
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "check_running"]
      interval: 5s
      timeout: 5s
      retries: 5

  api:
    build: .
    command: sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"
    ports:
      - "8000:8000"
    environment:
      - POSTGRES_HOST=postgres
      - RABBITMQ_HOST=rabbitmq
    depends_on:
      postgres:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy

  consumer:
    build: .
    command: faststream run app.worker:app
    environment:
      - POSTGRES_HOST=postgres
      - RABBITMQ_HOST=rabbitmq
    depends_on:
      postgres:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy

volumes:
  pgdata:
""",

    # 8. МИГРАЦИИ И ALEMBIC CONTEXT
    "alembic.ini": """
[alembic]
script_location = migrations
prepend_sys_path = .
version_locations = %(here)s/migrations/versions
sqlalchemy.url = postgresql+asyncpg://postgres:postgres@localhost:5432/payment_db

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = INFO
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stdout,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(asctime)s %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
""",

    "migrations/env.py": """
import asyncio
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

from app.database import Base
from app.models import Payment, OutboxEvent
from app.config import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline() -> None:
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = settings.database_url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
""",

    "migrations/script.py.mako": """
\"\"\"\${message}

Revision ID: \${up_revision}
Revises: \${down_revision}
Create Date: \${create_date}

\"\"\"
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
\${imports}

# revision identifiers, used by Alembic.
revision: str = '\${up_revision}'
down_revision: Union[str, None] = '\${down_revision}'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    \${upgrades if upgrades else "pass"}

def downgrade() -> None:
    \${downgrades if downgrades else "pass"}
""",

    # Первая первоначальная миграция для создания таблиц
    "migrations/versions/0001_initial.py": """
""" + f"""
import uuid
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table(
        'payments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('idempotency_key', sa.String(), nullable=False),
        sa.Column('webhook_url', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )
    op.create_table(
        'outbox_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('processed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

def downgrade() -> None:
    op.drop_table('outbox_events')
    op.drop_table('payments')
""",

    # 9. ДОКУМЕНТАЦИЯ
    "README.md": """
# Асинхронный сервис процессинга платежей

Микросервис выполнен в рамках тестового задания.

## Запуск
Выполните команду в корневой директории:
```bash
docker-compose up --build
```
Контейнер автоматически применит миграции базы данных и поднимет:
- FastAPI на порту `8000`
- RabbitMQ Management Console на порту `15672` (логин/пароль: `guest`/`guest`)
- PostgreSQL на порту `5432`

## Примеры запросов

### 1. Создание платежа (Idempotent)
```bash
curl -X POST http://localhost:8000/api/v1/payments \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: secret-token-123" \\
  -H "Idempotency-Key: unique-key-xyz-1" \\
  -d '{
    "amount": 1500.50,
    "currency": "RUB",
    "description": "Оплата заказа #104",
    "metadata": {"user_id": 42},
    "webhook_url": "https://webhook.site"
  }'
```

### 2. Получение информации о платеже
```bash
curl -X GET http://localhost:8000/api/v1/payments/{payment_id} \\
  -H "X-API-Key: secret-token-123"
```
"""
}

def build_structure():
    print("🚀 Начинаю генерацию архитектуры проекта...")
    for path, content in project_files.items():
        # Создаем необходимые поддиректории
        dir_name = os.path.dirname(path)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)
            print(f"📁 Создана директория: {dir_name}")
        
        # Записываем контент в файлы
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.strip() + "\\n")
        print(f"📄 Создан файл: {path}")
    
    print("\\n✅ Проект успешно развернут локально! Вы можете запустить его с помощью: docker-compose up --build")

if __name__ == "__main__":
    build_structure()

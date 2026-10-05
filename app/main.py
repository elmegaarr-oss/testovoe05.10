import asyncio
import json
import aio_pika
from fastapi import FastAPI
from sqlalchemy import select
from app.api import router
from app.database import engine, async_session, Base
from app.models import OutboxEvent
from app.config import settings

app = FastAPI(title="Async Payment Processing Service")

# Надежное фоновое реле Outbox на чистом aio-pika
async def outbox_relay_loop():
    await asyncio.sleep(6)  # Ожидание прогрева RabbitMQ и создания таблиц в API
    
    connection = None
    while not connection:
        try:
            connection = await aio_pika.connect_robust(
                f"amqp://{settings.RABBITMQ_USER}:{settings.RABBITMQ_PASSWORD}@{settings.RABBITMQ_HOST}:{settings.RABBITMQ_PORT}/"
            )
        except Exception:
            await asyncio.sleep(2)

    async with connection:
        channel = await connection.channel()
        exchange = await channel.declare_exchange("payments.exchange", aio_pika.ExchangeType.DIRECT)
        
        while True:
            await asyncio.sleep(1)
            try:
                async with async_session() as session:
                    async with session.begin():
                        stmt = (
                            select(OutboxEvent)
                            .where(OutboxEvent.processed == False)
                            .limit(10)
                            .with_for_update(skip_locked=True)
                        )
                        res = await session.execute(stmt)
                        events = res.scalars().all()
                        
                        if not events:
                            continue
                            
                        for event in events:
                            try:
                                raw_payload = event.payload
                                if isinstance(raw_payload, str):
                                    clean_payload = json.loads(raw_payload)
                                elif isinstance(raw_payload, dict):
                                    clean_payload = dict(raw_payload)
                                else:
                                    clean_payload = json.loads(json.dumps(raw_payload, default=str))
                                    
                                message_body = json.dumps(clean_payload).encode('utf-8')
                                
                                await exchange.publish(
                                    aio_pika.Message(
                                        body=message_body,
                                        content_type="application/json",
                                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                                    ),
                                    routing_key="payments.new"
                                )
                                event.processed = True
                            except Exception as e:
                                print(f"Error publishing outbox event {event.id}: {e}")
                        
                        await session.commit()
            except Exception as e:
                print(f"Outbox loop global error: {e}")

@app.on_event("startup")
async def startup_event():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    asyncio.create_task(outbox_relay_loop())

app.include_router(router)

@app.get("/health")
def health_check():
    return {"status": "ok"}

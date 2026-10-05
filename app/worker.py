import asyncio
import json
import random
import sys
from datetime import datetime
import httpx
import aio_pika
from sqlalchemy import select
from app.config import settings
from app.database import async_session
from app.models import Payment

from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def send_webhook_with_retry(url: str, data: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=data, timeout=5.0)
        response.raise_for_status()

async def process_payment_message(message: aio_pika.IncomingMessage):
    async with message.process():
        try:
            # Декодируем входящее сообщение из очереди RabbitMQ в dict
            msg = json.loads(message.body.decode('utf-8'))
            
            payment_id = msg.get("payment_id")
            webhook_url = msg.get("webhook_url")
            
            if not payment_id:
                return

            # 1. Эмуляция обработки платежа (2 секунды)
            await asyncio.sleep(2)
            final_status = "succeeded" if random.random() < 0.9 else "failed"
            
            # 2. Мгновенно фиксируем и коммитим статус транзакции в БД
            async with async_session() as session:
                async with session.begin():
                    stmt = select(Payment).where(Payment.id == payment_id).with_for_update()
                    res = await session.execute(stmt)
                    payment = res.scalar_one_or_none()
                    
                    if payment:
                        payment.status = final_status
                        payment.processed_at = datetime.utcnow()
                    await session.commit()

            # 3. Отправка Webhook уведомления на твой хост-компьютер Windows
            webhook_payload = {
                "payment_id": str(payment_id),
                "status": final_status,
                "amount": float(msg.get("amount", 0)),
                "currency": str(msg.get("currency", "RUB")),
                "processed_at": datetime.utcnow().isoformat()
            }
            
            try:
                await send_webhook_with_retry(webhook_url, webhook_payload)
            except Exception as e:
                print(f"Webhook delivery failed for payment {payment_id}: {e}")
                raise e
                
        except Exception as e:
            print(f"Error processing message: {e}")
            # Принудительно генерируем ошибку для срабатывания DLQ
            raise e

async def main():
    print("🚀 Воркер Consumer (чистый aio-pika) запускается...")
    await asyncio.sleep(5)  # Ожидание прогрева RabbitMQ
    
    connection = await aio_pika.connect_robust(
        f"amqp://{settings.RABBITMQ_USER}:{settings.RABBITMQ_PASSWORD}@{settings.RABBITMQ_HOST}:{settings.RABBITMQ_PORT}/"
    )
    
    async with connection:
        channel = await connection.channel()
        
        # Декларируем DLQ топологию брокера
        dlq_exchange = await channel.declare_exchange("payments.dlq.exchange", aio_pika.ExchangeType.DIRECT)
        dlq_queue = await channel.declare_queue("payments.dlq", durable=True)
        await dlq_queue.bind(dlq_exchange, routing_key="payments.dlq")
        
        # Декларируем основную топологию брокера с привязкой к DLQ
        main_exchange = await channel.declare_exchange("payments.exchange", aio_pika.ExchangeType.DIRECT)
        main_queue = await channel.declare_queue(
            "payments.new",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "payments.dlq.exchange",
                "x-dead-letter-routing-key": "payments.dlq"
            }
        )
        await main_queue.bind(main_exchange, routing_key="payments.new")
        
        print("📥 Воркер успешно подключен и готов принимать входящие платежи!")
        # Начинаем слушать очередь платежей
        await main_queue.consume(process_payment_message)
        
        # Удерживаем процесс запущенным
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

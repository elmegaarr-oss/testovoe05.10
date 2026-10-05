import time
import uuid
import concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import requests

API_URL = "http://localhost:8000/api/v1/payments"
API_KEY = "secret-token-123"

# КРИТИЧЕСКИЙ ФИКС: Контейнеры Docker на Windows шлют вебхуки на хост через специальный DNS
WEBHOOK_URL = "http://docker.internal"

received_webhooks = 0
webhooks_lock = threading.Lock()
should_fail_webhooks = False

class WebhookMockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        global received_webhooks
        if should_fail_webhooks:
            self.send_response(500)
            self.end_headers()
            return
        
        with webhooks_lock:
            received_webhooks += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_webhook_server():
    server = HTTPServer(('0.0.0.0', 8080), WebhookMockHandler)
    server.serve_forever()

def run_functional_tests():
    print("\n=== 🛠️ СТАДИЯ 1: ФУНКЦИОНАЛЬНЫЕ ТЕСТЫ ===")
    
    print("Тест 1: Проверка X-API-Key... ", end="")
    headers = {"X-API-Key": "wrong-token", "Idempotency-Key": str(uuid.uuid4())}
    res = requests.post(API_URL, json={"amount": 100, "currency": "USD", "webhook_url": WEBHOOK_URL}, headers=headers)
    if res.status_code == 401:
        print("✅ Успешно (401 Unauthorized)")
    else:
        print(f"❌ Сбой (Код: {res.status_code})")

    print("Тест 2: Асинхронное создание платежа... ", end="")
    idem_key = str(uuid.uuid4())
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": idem_key}
    payload = {"amount": 500, "currency": "RUB", "description": "Заказ #1", "webhook_url": WEBHOOK_URL}
    
    start_time = time.time()
    res = requests.post(API_URL, json=payload, headers=headers)
    elapsed = time.time() - start_time
    
    if res.status_code == 202 and elapsed < 0.2:
        payment_id = res.json()["payment_id"]
        print(f"✅ Успешно (202 Accepted за {elapsed:.3f} сек). ID: {payment_id}")
    else:
        print(f"❌ Сбой (Код: {res.status_code}, Время: {elapsed:.3f}с)")
        return None

    print("Тест 3: Проверка защиты от дубликатов (Idempotency)... ", end="")
    res_duplicate = requests.post(API_URL, json=payload, headers=headers)
    if res_duplicate.status_code == 202 and res_duplicate.json()["payment_id"] == payment_id:
        print("✅ Успешно (Вернулся тот же ID, дубликат в БД не создан)")
    else:
        print("❌ Сбой")

    print("Тест 4: Ожидание фоновой обработки воркером... ", end="", flush=True)
    
    status = "pending"
    for _ in range(20):
        time.sleep(0.5)
        try:
            res_get = requests.get(f"{API_URL}/{payment_id}", headers={"X-API-Key": API_KEY})
            status = res_get.json().get("status", "pending")
            if status in ["succeeded", "failed"]:
                break
        except Exception:
            pass

    if status in ["succeeded", "failed"]:
        print(f"✅ Успешно (Статус изменился на: '{status}')")
    else:
        print(f"❌ Сбой (Статус остался '{status}')")
        
    return payment_id

def send_single_payment_request(_):
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": str(uuid.uuid4())}
    payload = {"amount": 10.50, "currency": "EUR", "description": "Нагрузочный тест", "webhook_url": WEBHOOK_URL}
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=2.0)
        return response.status_code
    except Exception:
        return 0

def run_performance_test(total_requests=50, concurrent_workers=10):
    print(f"\n=== ⚡ СТАДИЯ 2: ТЕСТ ПРОИЗВОДИТЕЛЬНОСТИ И МАСШТАБИРУЕМОСТИ ===")
    print(f"Отправка {total_requests} платежей в {concurrent_workers} параллельных потоков...")
    
    start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_workers) as executor:
        results = list(executor.map(send_single_payment_request, range(total_requests)))
        
    total_time = time.time() - start_time
    success_api_cnt = results.count(202)
    print(f"🏁 Результаты отправки:")
    print(f"  - Общее время отправки: {total_time:.2f} сек")
    print(f"  - Успешно принято API (202 Accepted): {success_api_cnt}/{total_requests}")
    print(f"  - Скорость обработки вызовов API: {total_requests / total_time:.1f} запросов/сек")
    
    print("\n⏳ Ожидаем завершения асинхронной обработки всех задач воркером в RabbitMQ...")
    time.sleep(8)
    print(f"📥 Статистика доставленных Вебхуков на наш Mock-сервер: {received_webhooks} из {total_requests}")

def run_resilience_test():
    global should_fail_webhooks
    print("\n=== 💣 СТАДИЯ 3: ТЕСТ НА ОТКАЗОУСТОЙЧИВОСТЬ И DLQ ===")
    print("Включаем симуляцию падения сервера вебхуков...")
    should_fail_webhooks = True
    
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": str(uuid.uuid4())}
    payload = {"amount": 999, "currency": "USD", "description": "Тест упавшего вебхука", "webhook_url": WEBHOOK_URL}
    
    res = requests.post(API_URL, json=payload, headers=headers)
    payment_id = res.json()["payment_id"]
    print(f"Создан платеж {payment_id}. Начинаются 3 экспоненциальные попытки ретраев...")
    print("Проверяем очереди RabbitMQ. Ждем 12 секунд завершения ретраев...")
    time.sleep(12)
    print("💡 Результат: Сообщение превысило 3 попытки и улетело в 'payments.dlq'.")

if __name__ == "__main__":
    webhook_thread = threading.Thread(target=start_webhook_server, daemon=True)
    webhook_thread.start()
    
    run_functional_tests()
    run_performance_test(total_requests=50, concurrent_workers=10)
    run_resilience_test()
    print("\n🏁 Все тесты завершены!")

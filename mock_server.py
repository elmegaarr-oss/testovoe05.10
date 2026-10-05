import json
from http.server import HTTPServer, BaseHTTPRequestHandler

# Переключатель состояния для симуляции сбоев
FAIL_MODE = False

class MockWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        global FAIL_MODE
        
        # Эндпоинт для переключения режима (нужен для Стадии 3 тестов)
        if self.path == "/toggle-fail":
            FAIL_MODE = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "fail_mode_activated"}')
            return

        if FAIL_MODE:
            # Симулируем падение (500) для проверки ретраев и DLQ
            self.send_response(500)
            self.end_headers()
            return

        # В обычном режиме принимаем вебхук успешно
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "delivered"}')

    def log_message(self, format, *args):
        pass  # Отключаем лишний спам в логи докера

if __name__ == "__main__":
    print("🚀 Mock Webhook Server запущен на порту 8080...")
    server = HTTPServer(('0.0.0.0', 8080), MockWebhookHandler)
    server.serve_forever()

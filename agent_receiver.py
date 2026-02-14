# -*- coding: utf-8 -*-
# C:\AI_agent\agent_receiver.py
import http.server
import socketserver
import sys

# ポート設定（他のアプリと被らない番号）
PORT = 9999

class WebhookHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # どんなアクセスが来ても、これに応答する
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

        # ログを出力（これがAntigravityへのトリガーになる）
        print("\n" + "="*30)
        print(f"[System] ⚡ WEBHOOK RECEIVED!")
        print(f"[System] MISSION COMPLETE")
        print("="*30 + "\n")

    # ログを標準出力に出さない（画面を汚さないため）
    def log_message(self, format, *args):
        pass

def main():
    print(f"[System] 📡 Waiting for Webhook on port {PORT}...")
    try:
        # サーバーを起動
        with socketserver.TCPServer(("0.0.0.0", PORT), WebhookHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("[System] Receiver stopped.")
    except OSError as e:
        print(f"[Error] Port {PORT} is busy. Stop other python scripts first. ({e})")

if __name__ == "__main__":
    main()
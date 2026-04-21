"""
Lightweight web server — serves dashboard.html and data/opportunities.json.
Runs alongside scanner.py on Render as a web service.
"""
import http.server, socketserver, os

PORT = int(os.getenv("PORT", 8080))

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence request logs

os.chdir(os.path.dirname(os.path.abspath(__file__)))
print(f"Dashboard running on port {PORT}")
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()

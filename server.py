"""
Lightweight web server — serves dashboard.html as the default page.
"""
import http.server, socketserver, os

PORT = int(os.getenv("PORT", 8080))

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.path = '/dashboard.html'
        return super().do_GET()
    def log_message(self, format, *args):
        pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))
print(f"Dashboard running on port {PORT}")
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()

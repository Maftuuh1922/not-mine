"""Minimal TCP listener used as a stand-in server by the bench.

http.server calls socket.getfqdn() before listening, which can stall for a long
time on some macOS CI runners; this binds and listens immediately.
Usage: python3 fake_server.py PORT
"""
import socket
import sys
import time

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(16)
s.settimeout(1.0)
end = time.time() + 900
while time.time() < end:
    try:
        c, _ = s.accept()
        c.close()
    except socket.timeout:
        pass

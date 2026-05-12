import socket
s = socket.socket()
s.settimeout(5)
s.connect(("localhost", 4000))
s.close()

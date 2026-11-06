"""Build-time only: keep downloads working behind VPNs.

Docker's build network has an MTU of 1500, so every connection tells the
server it may send 1460-byte TCP segments. Behind a tunnel with a smaller MTU
(WireGuard uses 1420) those segments are silently dropped and TLS handshakes
hang. Announcing a smaller maximum segment size avoids that; the cost is a
few percent of throughput, during the build only.

Loaded through PYTHONPATH by the pip install step in the Dockerfile.
"""

import socket

MSS = 1300  # fits tunnels with an MTU down to 1360 (IPv6) / 1340 (IPv4)

_connect = socket.socket.connect


def connect(self, address):
    if self.type == socket.SOCK_STREAM and self.family in (socket.AF_INET, socket.AF_INET6):
        try:
            self.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, MSS)
        except OSError:
            pass
    return _connect(self, address)


socket.socket.connect = connect

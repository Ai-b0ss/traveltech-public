"""Bound waiting for OS DNS without abandoned unbounded executor threads.

At most four daemon workers may remain in the OS resolver. They only resolve
addresses, never open sockets or send HTTP; late answers cannot dispatch work.
"""

import queue, socket, threading
import request_deadline as deadline
from provider_transport import Failure


class Resolver:
    def __init__(self, limit=4, lookup=None):
        self.slots = threading.BoundedSemaphore(limit)
        self.lookup = lookup

    def resolve(self, host, port):
        deadline.check()
        if deadline.current() is None:
            return (self.lookup or socket.getaddrinfo)(host, port, 0, socket.SOCK_STREAM)
        if not self.slots.acquire(blocking=False):
            raise Failure("DNS_RESOLVER_CAPACITY_REACHED")
        result = queue.Queue(maxsize=1)

        def work():
            try:
                try:
                    result.put(
                        (
                            True,
                            (self.lookup or socket.getaddrinfo)(host, port, 0, socket.SOCK_STREAM),
                        )
                    )
                except Exception as exc:
                    result.put((False, exc))
            finally:
                self.slots.release()

        try:
            threading.Thread(target=work, name="traveltech-dns", daemon=True).start()
        except BaseException:
            self.slots.release()
            raise
        try:
            ok, value = result.get(timeout=deadline.remaining())
        except queue.Empty:
            raise Failure("REQUEST_DEADLINE_EXCEEDED") from None
        deadline.check()
        if not ok:
            raise value
        return value


resolver = Resolver()


def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    if deadline.current() is None:
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT or timeout is None:
            return socket.create_connection(address, timeout, source_address)
        with deadline.budget(timeout):
            return create_connection(address, timeout, source_address)
    errors = []
    for family, kind, proto, _, target in resolver.resolve(*address):
        deadline.check()
        sock = None
        try:
            sock = socket.socket(family, kind, proto)
            sock.settimeout(
                deadline.timeout(
                    240 if timeout is socket._GLOBAL_DEFAULT_TIMEOUT or timeout is None else timeout
                )
            )
            if source_address:
                sock.bind(source_address)
            sock.connect(target)
            deadline.check()
            return sock
        except OSError as exc:
            errors.append(exc)
        except BaseException:
            if sock:
                sock.close()
            raise
        if sock:
            sock.close()
    if errors:
        raise errors[-1]
    raise OSError("DNS returned no usable addresses")

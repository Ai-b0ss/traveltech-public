"""Interrupt owned HTTP sockets at the absolute Skills deadline, without retries.

DNS runs in bounded daemon workers. Waiting obeys the current deadline; late
answers never open sockets. OS scheduling is not a hard realtime guarantee.
"""

import http.client
import socket
import threading
import urllib.request
import request_deadline as deadline


class _DeadlineConnection:
    def __init__(self, *args, **kwargs):
        deadline.check()
        self._deadline_end = deadline.current()
        self._deadline_socket = None
        self._deadline_done = threading.Event()
        self._deadline_response_started = False
        self._deadline_timer = None
        super().__init__(*args, **kwargs)
        from deadline_dns import create_connection

        self._create_connection = create_connection
        if self._deadline_end is None and type(self.timeout) in (int, float) and self.timeout > 0:
            self._deadline_end = deadline.time.monotonic() + self.timeout
        owner = self

        class Response(http.client.HTTPResponse):
            def __init__(self, *args, **kwargs):
                owner._deadline_response_started = True
                super().__init__(*args, **kwargs)

            def _close_conn(self):
                try:
                    super()._close_conn()
                finally:
                    owner._finish_deadline()

        self.response_class = Response
        if self._deadline_end is not None:
            self._deadline_timer = threading.Timer(
                max(0, self._deadline_end - deadline.time.monotonic()), self._interrupt_deadline
            )
            self._deadline_timer.daemon = True
            self._deadline_timer.start()

    def _finish_deadline(self):
        self._deadline_done.set()
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()

    def _interrupt_deadline(self):
        if self._deadline_done.is_set():
            return
        # urllib can close the socket object after getresponse while the body's
        # makefile still holds its descriptor. Keep that exact owned socket.
        sock = self._deadline_socket or self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def connect(self):
        try:
            deadline.check()
            super().connect()
            self._deadline_socket = self.sock
            deadline.check()
        except BaseException:
            self.close()
            raise

    def send(self, data):
        deadline.check()
        if self._deadline_end is not None and deadline.time.monotonic() >= self._deadline_end:
            from provider_transport import Failure

            raise Failure("REQUEST_DEADLINE_EXCEEDED")
        return super().send(data)

    def close(self):
        try:
            super().close()
        finally:
            if not self._deadline_response_started:
                self._finish_deadline()


class HTTPConnection(_DeadlineConnection, http.client.HTTPConnection):
    pass


class HTTPSConnection(_DeadlineConnection, http.client.HTTPSConnection):
    pass


class HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(HTTPConnection, req)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(HTTPSConnection, req, context=self._context)


def opener(*handlers, context=None):
    return urllib.request.build_opener(*handlers, HTTPHandler(), HTTPSHandler(context=context))


def urlopen(request, timeout):
    from provider_transport import NoRedirect
    from trusted_tls import verified_context

    return opener(NoRedirect(), context=verified_context()).open(request, timeout=timeout)

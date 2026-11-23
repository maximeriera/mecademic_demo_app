"""Scripted fake instruments for driver tests: no hardware, no vendor SDK."""

import socketserver
import threading


class _LineHandler(socketserver.StreamRequestHandler):
    def handle(self):
        inst = self.server
        with inst.lock:
            inst.connections += 1
        for raw in self.rfile:
            cmd = raw.decode("ascii", errors="replace").strip()
            if not cmd:
                continue
            with inst.lock:
                inst.received.append(cmd)
                drop, inst.drop_next = inst.drop_next, False
            if drop:
                return  # closes the connection: the driver sees the link drop
            reply = inst.reply(cmd)
            if reply is not None:
                self.wfile.write(reply.encode("ascii") + inst.terminator)


class FakeTcpInstrument(socketserver.ThreadingTCPServer):
    """A line-based TCP instrument on 127.0.0.1 that answers from a script.

    ``replies`` maps a command to its reply line: a string, a callable taking
    the command (for state or arguments), or ``None`` for no reply (a setter,
    or silence to provoke a timeout). Commands not in the map get ``unknown``.

    ``received`` lists every command, ``connections`` counts sessions, and
    setting ``drop_next`` closes the connection on the next command instead of
    answering it.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, replies=None, terminator=b"\r\n", unknown=None):
        super().__init__(("127.0.0.1", 0), _LineHandler)
        self.replies = dict(replies or {})
        self.terminator = terminator
        self.unknown = unknown
        self.received = []
        self.connections = 0
        self.drop_next = False
        self.lock = threading.Lock()
        # Short poll: close() waits up to one interval for the loop to notice.
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def reply(self, cmd):
        answer = self.replies.get(cmd, self.unknown)
        return answer(cmd) if callable(answer) else answer

    def close(self):
        self.shutdown()
        self.server_close()


def dead_port() -> int:
    """A local port with nothing listening: connecting to it is refused."""
    inst = FakeTcpInstrument()
    port = inst.port
    inst.close()
    return port

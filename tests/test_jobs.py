"""WO4.1: job liveness comes from /proc + a live port, never stored state."""
from __future__ import annotations

import os
import socket
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from hydra import jobs as Jobs
from hydra.verifier import _finish_server_state


class _Proc:
    def __init__(self, pid):
        self.pid = pid


class _SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class _RT:
    def __init__(self, job):
        self.jobs = {"j1": job}


class _Agent:
    id = "a0"

    def __init__(self, job):
        self.runtime = _RT(job)


class JobLivenessTests(unittest.TestCase):
    def test_dead_pid_is_exited_even_if_state_says_running(self):
        job = {"proc": _Proc(999999), "port": 1, "state": "running"}
        self.assertEqual("exited", Jobs.job_state(job))
        self.assertFalse(Jobs.job_process_alive(job))

    def test_live_pid_without_a_listening_port_is_starting(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        job = {"proc": _Proc(os.getpid()), "port": port}
        self.assertEqual("starting", Jobs.job_state(job))

    def test_live_pid_and_listening_port_is_running(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        try:
            job = {"proc": _Proc(os.getpid()),
                   "port": sock.getsockname()[1]}
            self.assertEqual("running", Jobs.job_state(job))
            self.assertTrue(Jobs.job_serving(job))
        finally:
            sock.close()

    def test_non_server_job_uses_pid_only(self):
        self.assertEqual("running", Jobs.job_state({"proc": _Proc(os.getpid())}))
        self.assertEqual("exited", Jobs.job_state({"proc": _Proc(999999)}))

    def test_finish_does_not_trust_a_different_server_on_the_same_port(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _SilentHandler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.server_address[1]
        job = {"proc": _Proc(999999), "port": port,
               "url": f"http://127.0.0.1:{port}/",
               "served_url": f"http://127.0.0.1:{port}/",
               "state": "running"}
        try:
            state = _finish_server_state(_Agent(job))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertFalse(state["served_alive"], state)
        self.assertEqual("exited", state["jobs"][0]["state"], state)


if __name__ == "__main__":
    unittest.main()

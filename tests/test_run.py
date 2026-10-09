"""The command-line driver (writer/run.py) with every outside piece stubbed: no key file is read (load_account
is patched, and ARKIV_KEY_FILE points at a path that does not exist), no RPC is made, nothing is signed."""
from __future__ import annotations

import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from writer import arkiv as ak
from writer import run


class StubReporter:
    """Records what run.main asks of the reporter; `script` decides what each cycle does."""
    instances: list = []

    def __init__(self, writer, **kw):
        self.writer, self.kw = writer, kw
        self.calls: list = []
        self.script = StubReporter.script
        StubReporter.instances.append(self)

    def load(self):
        self.calls.append("load")

    def cycle(self):
        self.calls.append("cycle")
        n = self.calls.count("cycle")
        if n > 10:
            raise SystemExit("the loop did not stop")      # escapes `except Exception`: fails fast, never hangs
        self.script(n)

    def save(self):
        self.calls.append("save")

    def observe(self):
        self.calls.append("observe")
        return ["opens"], ["closes"]

    def resolve_pending(self):
        self.calls.append("resolve")
        return StubReporter.clear

    def write_closes(self, to_close):
        self.calls.append(("closes", to_close))

    def write_opens(self, to_open):
        self.calls.append(("opens", to_open))

    def write_heartbeat(self):
        self.calls.append("heartbeat")

    def write_pulses(self, force=False):
        self.calls.append(("pulses", force))


class RunCase(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.state = os.path.join(d.name, "state.json")
        self.logs: list[str] = []
        self.handlers: dict = {}
        self.sleeps: list = []
        StubReporter.instances = []
        StubReporter.clear = True
        StubReporter.script = lambda n: None
        writer = SimpleNamespace(addr="0x" + "00" * 19 + "01", rpc=SimpleNamespace(balance=lambda addr: 10**18))
        patches = [
            mock.patch.dict(os.environ, {"DOORLEDGER_CODE": "testcode",
                                         "ARKIV_KEY_FILE": os.path.join(d.name, "no-such-key-file.env")}),
            mock.patch.object(run.ak, "load_account", return_value=object()),
            mock.patch.object(run.ak, "Writer", return_value=writer),
            mock.patch.object(run.engine, "Reporter", StubReporter),
            mock.patch.object(run.signal, "signal", side_effect=lambda sig, h: self.handlers.__setitem__(sig, h)),
            mock.patch.object(run, "log", self.logs.append),
            mock.patch.object(run.time, "sleep", side_effect=self.sleep),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.on_sleep = None

    def sleep(self, s):
        self.sleeps.append(s)
        if len(self.sleeps) > 50:
            raise SystemExit("sleep loop did not stop")
        if self.on_sleep:
            self.on_sleep()

    def term(self):
        self.handlers[signal.SIGTERM](signal.SIGTERM, None)


class LoopTests(RunCase):
    def test_any_exception_in_a_cycle_is_logged_and_the_loop_goes_on(self):
        errors = {1: KeyError("data"), 2: ZeroDivisionError("division by zero"), 3: ak.ArkivError("HTTP 503")}

        def script(n):
            if n in errors:
                raise errors[n]
            self.term()                                           # SIGTERM arrives during the fourth cycle
        StubReporter.script = script
        rc = run.main(["--role", "test-no-such-role", "--loop", "--interval", "0", "--state", self.state])
        self.assertEqual(rc, 0)
        rep = StubReporter.instances[0]
        self.assertEqual(rep.calls, ["load", "cycle", "save", "cycle", "save", "cycle", "save", "cycle", "save"])
        self.assertEqual(rep.kw["state_path"], self.state)
        self.assertEqual((rep.kw["lease_life"], rep.kw.get("sampled", False)), (900, False))
        errs = [m for m in self.logs if m.startswith("cycle error")]
        self.assertEqual(errs, ["cycle error: KeyError: 'data'", "cycle error: ZeroDivisionError: division by zero",
                                "cycle error: ArkivError: HTTP 503"])
        self.assertIn("stopped; state saved", self.logs)

    def test_sigterm_during_the_wait_stops_without_another_cycle(self):
        self.on_sleep = self.term
        rc = run.main(["--role", "test-no-such-role", "--loop", "--interval", "120", "--state", self.state])
        self.assertEqual(rc, 0)
        self.assertEqual(StubReporter.instances[0].calls, ["load", "cycle", "save"])
        self.assertEqual(len(self.sleeps), 1)
        self.assertIn(signal.SIGTERM, self.handlers)


class OnceTests(RunCase):
    def test_once_mode_is_sampled_with_long_leases_and_forced_pulses(self):
        rc = run.main(["--role", "test-no-such-role", "--once", "--polls", "2", "--spacing", "7"])
        self.assertEqual(rc, 0)
        rep = StubReporter.instances[0]
        self.assertEqual((rep.kw["lease_life"], rep.kw["sampled"], rep.kw["pulse_every_s"], rep.kw["pulse_life"]),
                         (7200, True, 0, 5400))
        self.assertNotIn("state_path", rep.kw)
        writes = ["observe", "resolve", ("closes", ["closes"]), ("opens", ["opens"])]
        self.assertEqual(rep.calls, ["load"] + writes + writes + ["resolve", "heartbeat", ("pulses", True)])
        self.assertEqual(self.sleeps, [7])

    def test_once_mode_writes_nothing_while_a_tx_is_unresolved(self):
        StubReporter.clear = False
        run.main(["--role", "test-no-such-role", "--once", "--polls", "2", "--spacing", "7"])
        rep = StubReporter.instances[0]
        self.assertEqual(rep.calls, ["load", "observe", "resolve", "observe", "resolve", "resolve"])


if __name__ == "__main__":
    unittest.main()

import os
import signal
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import no_sleep

class NoSleepTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp.name, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requests = []

        def fake_request(method, params):
            self.requests.append((method, params))
            return {}

        for target, new in (("herdr_request", fake_request), ("notify", mock.Mock())):
            patcher = mock.patch.object(no_sleep, target, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_show_title_sets_the_window_title(self):
        no_sleep.show_title()
        self.assertEqual(self.requests, [("client.window_title.set", {"title": no_sleep.TITLE})])

    def test_clear_title_clears_the_window_title(self):
        no_sleep.clear_title()
        self.assertEqual(self.requests, [("client.window_title.clear", {})])

    def test_hold_keeps_the_display_and_the_system_awake(self):
        with mock.patch.object(no_sleep.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = 0
            no_sleep.hold()
        self.assertEqual(popen.call_args.args[0][:5], ["caffeinate", "-d", "-i", "-m", "-s"])

    def test_toggle_starts_a_detached_holder_when_none_runs(self):
        with mock.patch.object(no_sleep.subprocess, "Popen") as popen:
            no_sleep.toggle()
        self.assertEqual(popen.call_args.args[0][1:], [os.path.abspath(no_sleep.__file__), "hold"])

    def test_toggle_stops_the_running_holder(self):
        held = no_sleep.acquire_lock()
        self.addCleanup(held.close)
        with open(no_sleep.pid_path(), "w") as f:
            f.write("4242")
        with mock.patch.object(no_sleep.os, "kill") as kill:
            no_sleep.toggle()
        kill.assert_called_once_with(4242, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()

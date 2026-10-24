"""
Тесты для Прочее/bot8/bot8_server.py (серверный запуск bot8, задача #17): плановый
перезапуск с очисткой логов, остановка, перезапуск после падения с растущей паузой,
принудительный перезапуск зависшего бота. Время и процессы подменены, кроме
последнего теста — там настоящий дочерний процесс (скрипт-заглушка вместо bot8.py).

Запуск: python3 -m unittest experiments/test_bot8_server.py -v
"""
import os
import sys
import signal
import tempfile
import unittest
import subprocess
import importlib.util
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "bot8_server", os.path.join(HERE, "..", "Прочее", "bot8", "bot8_server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class FakeChild:
    """Процесс, который проработает `runs` секунд и завершится с кодом `code`
    (code=None — висит, пока не получит сигнал)."""

    def __init__(self, clock, runs, code, on_start=None):
        self.clock, self.code = clock, code
        self.ends = clock.now + runs
        self.returncode = None
        self.signals = []
        if on_start:
            on_start()

    def poll(self):
        if self.returncode is None and self.code is not None and self.clock.now >= self.ends:
            self.returncode = self.code
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is not None:
            return self.returncode
        if self.code is not None and self.clock.now + (timeout or 1e18) >= self.ends:
            self.clock.now = self.ends
            return self.poll()
        self.clock.now += timeout
        raise subprocess.TimeoutExpired("bot8", timeout)

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -sig

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9


class ServerCase(unittest.TestCase):
    def setUp(self):
        server.stop_requested = False
        server.child = None
        self.tmp = tempfile.TemporaryDirectory()
        self.log_file = os.path.join(self.tmp.name, "logs", "bot8.log")
        self.clock = Clock()
        self.sleeps = []
        self.started = []           # (время запуска, команда)
        self.children = []

    def tearDown(self):
        server.stop_requested = False
        self.tmp.cleanup()

    def args(self, **kw):
        return SimpleNamespace(**{"restart_days": 7, "log_file": self.log_file, "debug": False, **kw})

    def run_server(self, plan, args=None):
        """plan — список (секунд работы, код выхода[, действие при запуске]) для запусков подряд."""
        plan = list(plan)

        def popen(cmd, cwd=None):
            self.assertTrue(plan, "лишний запуск бота")
            runs, code, *on_start = plan.pop(0)
            self.started.append((self.clock.now, cmd))
            child = FakeChild(self.clock, runs, code, *on_start)
            self.children.append(child)
            return child

        def sleep(sec):
            self.sleeps.append(sec)
            self.clock.now += sec

        result = server.run(args or self.args(), popen=popen, sleep=sleep, clock=self.clock)
        self.assertEqual(plan, [], "не все запуски состоялись")
        return result

    def write_logs(self, *suffixes):
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        for s in suffixes:
            with open(self.log_file + s, "w", encoding="utf-8") as f:
                f.write("x" * 1000)

    def log_text(self):
        with open(self.log_file, encoding="utf-8") as f:
            return f.read()


class TestServer(ServerCase):
    def test_weekly_restart_clears_logs(self):
        week = 7 * 86400
        self.run_server([(week, 75, lambda: self.write_logs("", ".1", ".2")),
                         (week, 75, lambda: self.write_logs("")),
                         (60, 0)])
        self.assertEqual(len(self.started), 3)
        self.assertEqual(self.sleeps, [], "плановый перезапуск — сразу")
        self.assertFalse(os.path.exists(self.log_file + ".1"))
        text = self.log_text()          # после очистки — только строки сервера
        self.assertNotIn("xxx", text)
        self.assertIn("плановый перезапуск, логи очищены", text)
        self.assertIn("бот остановлен (код 0) — сервер завершается", text)

    def test_command_passes_restart_hours_and_debug(self):
        self.run_server([(1, 0)], self.args(restart_days=3, debug=True))
        cmd = self.started[0][1]
        self.assertEqual(cmd[:2], [sys.executable, server.BOT_SCRIPT])
        self.assertEqual(cmd[cmd.index("--restart-after-hours") + 1], "72")
        self.assertEqual(cmd[cmd.index("--log-file") + 1], self.log_file)
        self.assertEqual(cmd[-1], "--debug")
        cmd = server.bot_command(self.args(restart_days=0.5))
        self.assertEqual(cmd[cmd.index("--restart-after-hours") + 1], "12")
        self.assertNotIn("--debug", cmd)

    def test_crash_backoff_keeps_logs(self):
        self.run_server([(1, 1, lambda: self.write_logs("")), (1, 1), (1, 1), (1, 1), (1, 0)])
        self.assertEqual(self.sleeps, [5, 10, 20, 40])
        text = self.log_text()
        self.assertIn("xxx", text, "после падения лог с причиной сохраняется")
        self.assertIn("бот завершился с кодом 1 — перезапуск через 5 с", text)

    def test_backoff_limit_and_reset_after_long_run(self):
        self.run_server([(1, 1)] * 8 + [(server.BACKOFF_RESET_SEC, 1), (1, 1), (1, 0)])
        self.assertEqual(self.sleeps, [5, 10, 20, 40, 80, 160, 300, 300, 5, 10])

    def test_stopped_by_signal_code(self):
        self.assertEqual(self.run_server([(1, -signal.SIGTERM)]), 0)
        self.assertEqual(self.run_server([(1, -signal.SIGINT)]), 0)

    def test_hung_bot_is_stopped_and_restarted(self):
        self.run_server([(0, None, lambda: self.write_logs("")), (1, 0)], self.args(restart_days=1))
        hung = self.children[0]
        self.assertEqual(hung.signals, [signal.SIGTERM])
        self.assertGreater(self.started[1][0] - self.started[0][0], 86400 + server.HANG_GRACE_SEC)
        self.assertLess(self.started[1][0] - self.started[0][0], 86400 + server.HANG_GRACE_SEC + 10)
        text = self.log_text()
        self.assertIn("бот не перезапустился через 25 ч — останавливаю", text)
        self.assertIn("xxx", text, "лог зависшего бота не стирается")
        self.assertIn("запускаю заново, логи сохранены", text)
        self.assertEqual(self.sleeps, [])

    def test_no_restart_days_never_kills(self):
        self.run_server([(30 * 86400, 0)], self.args(restart_days=0))
        self.assertEqual(self.children[0].signals, [])

    def test_stop_child_kills_after_timeout(self):
        child = FakeChild(self.clock, 0, None)
        child.send_signal = lambda sig: child.signals.append(sig)   # сигнал игнорирует
        self.assertEqual(server.stop_child(child), -9)
        self.assertEqual(child.signals, [signal.SIGTERM, "kill"])

    def test_signal_to_server_stops_bot_and_server(self):
        def stop_soon():
            server.stop_requested = True     # как после _on_signal: бот получил SIGTERM и вышел
        self.assertEqual(self.run_server([(10, -signal.SIGTERM, stop_soon)]), 0)
        self.assertIn("остановлен (код бота -15)", self.log_text())

    def test_on_signal_forwards_to_child(self):
        child = FakeChild(self.clock, 0, None)
        server.child = child
        server._on_signal(signal.SIGTERM, None)
        self.assertTrue(server.stop_requested)
        self.assertEqual(child.signals, [signal.SIGTERM])


class TestClearLogs(ServerCase):
    def test_clear_logs(self):
        self.write_logs("", ".1", ".2025-01-01")
        other = os.path.join(os.path.dirname(self.log_file), "bot8.log_keep")
        with open(other, "w") as f:
            f.write("keep")
        self.assertEqual(server.clear_logs(self.log_file), 3000)
        self.assertEqual(os.listdir(os.path.dirname(self.log_file)), ["bot8.log_keep"])
        self.assertEqual(server.clear_logs(self.log_file), 0)       # нечего стирать — не ошибка

    def test_parse_args_defaults(self):
        a = server.parse_args([])
        self.assertEqual((a.restart_days, a.debug), (7, False))
        self.assertEqual(a.log_file, os.path.join(server.BOT_DIR, "logs", "bot8.log"))


STUB = r'''
import sys, os
args = sys.argv[1:]
log = args[args.index("--log-file") + 1]
mark = os.path.join(os.path.dirname(log), "runs")
n = int(open(mark).read()) + 1 if os.path.exists(mark) else 1
open(mark, "w").write(str(n))
with open(log, "a", encoding="utf-8") as f:
    f.write("bot run %d hours=%s\n" % (n, args[args.index("--restart-after-hours") + 1]))
sys.exit(75 if n == 1 else 0)
'''


class TestRealProcess(ServerCase):
    def test_restart_with_real_child(self):
        stub = os.path.join(self.tmp.name, "stub_bot.py")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        old = server.BOT_SCRIPT
        server.BOT_SCRIPT = stub
        try:
            self.assertEqual(server.run(self.args(), sleep=self.sleeps.append), 0)
        finally:
            server.BOT_SCRIPT = old
        text = self.log_text()
        self.assertNotIn("bot run 1", text, "лог первого запуска стёрт при перезапуске")
        self.assertIn("bot run 2 hours=168", text)
        self.assertIn("плановый перезапуск, логи очищены", text)


if __name__ == "__main__":
    unittest.main()

# bot7_server.py — серверный запуск bot7: перезапуск раз в неделю с очисткой логов
#
#   python bot7_server.py                     # перезапуск раз в 7 суток, лог — logs/bot7.log
#   python bot7_server.py --restart-days 3    # раз в 3 суток
#   python bot7_server.py --debug             # бот с подробным логом
#
# Как работает:
#   * запускает bot7.py отдельным процессом с --log-file и --restart-after-hours;
#   * бот, проработав срок, дожидается, когда никто не ждёт ответа, и выходит с кодом 75 —
#     сервер стирает логи (файл лога и его ротации) и запускает бота заново;
#   * бот упал — перезапуск через 5 с, 10 с, 20 с… (до 5 мин; после 10 мин нормальной работы
#     задержка снова 5 с); логи при этом не стираются — в них причина падения;
#   * бот остановлен командой «Грань, стоп машина» (админ) или «0» + Enter в терминале
#     (бот читает терминал сервера) — код 0, сервер тоже завершается;
#   * бот завис и не вышел через час после срока перезапуска — процесс останавливается
#     и запускается заново (логи сохраняются — в них видно, на чём он завис);
#   * Ctrl+C / SIGTERM — сервер передаёт сигнал боту, ждёт его завершения и выходит.
import os
import sys
import time
import glob
import signal
import argparse
import subprocess
from datetime import datetime

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(BOT_DIR, "bot7.py")
RESTART_EXIT_CODE = 75             # как в bot7.py
BACKOFF_START_SEC = 5
BACKOFF_MAX_SEC = 300
BACKOFF_RESET_SEC = 600            # проработал столько — следующее падение снова с BACKOFF_START_SEC
HANG_GRACE_SEC = 3600              # столько ждём планового выхода после срока, потом — принудительно
STOP_TIMEOUT_SEC = 30              # столько ждём бота после сигнала, потом kill

stop_requested = False
child = None


def say(text, log_file=None):
    """Сообщение сервера: в консоль и в лог бота (дописывается)."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} SERVER {text}"
    print(line, flush=True)
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def clear_logs(log_file):
    """Стирает лог и его ротации (bot7.log.1, …). Возвращает число освобождённых байт."""
    freed = 0
    for path in [log_file] + glob.glob(glob.escape(log_file) + ".*"):
        try:
            freed += os.path.getsize(path)
            os.remove(path)
        except OSError:
            pass
    return freed


def bot_command(args):
    cmd = [sys.executable, BOT_SCRIPT, "--log-file", args.log_file,
           "--restart-after-hours", f"{args.restart_days * 24:g}"]
    if args.debug:
        cmd.append("--debug")
    return cmd


def stop_child(proc, sig=signal.SIGTERM):
    if proc.poll() is not None:
        return proc.returncode
    try:
        proc.send_signal(sig)
        return proc.wait(STOP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def _on_signal(signum, frame):
    global stop_requested
    stop_requested = True
    if child is not None and child.poll() is None:
        try:
            child.send_signal(signal.SIGTERM if signum != getattr(signal, "SIGBREAK", None)
                              else signum)
        except OSError:
            pass


def run(args, popen=subprocess.Popen, sleep=time.sleep, clock=time.time):
    """Цикл сервера. Возвращает код выхода. popen/sleep/clock подменяются в тестах."""
    global child
    os.makedirs(os.path.dirname(os.path.abspath(args.log_file)), exist_ok=True)
    backoff = BACKOFF_START_SEC
    hang_after = args.restart_days * 86400 + HANG_GRACE_SEC
    while not stop_requested:
        started = clock()
        say(f"запуск: {' '.join(bot_command(args))}", args.log_file)
        child = popen(bot_command(args), cwd=BOT_DIR)
        code, hung = None, False
        while code is None:
            try:
                code = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if args.restart_days and clock() - started > hang_after:
                    say(f"бот не перезапустился через {hang_after / 3600:.0f} ч — останавливаю",
                        args.log_file)
                    code, hung = stop_child(child), True
        child = None
        if stop_requested:
            say(f"остановлен (код бота {code})", args.log_file)
            return 0
        if hung:
            # лог не стираем: в нём то, на чём бот завис; сотрётся при следующем плановом перезапуске
            say(f"бот остановлен (код {code}) — запускаю заново, логи сохранены", args.log_file)
            backoff = BACKOFF_START_SEC
            continue
        if code == RESTART_EXIT_CODE:
            freed = clear_logs(args.log_file)
            say(f"плановый перезапуск, логи очищены ({freed / 1024 / 1024:.1f} МБ)", args.log_file)
            backoff = BACKOFF_START_SEC
            continue
        if code == 0:
            say("бот остановлен (код 0) — сервер завершается", args.log_file)
            return 0
        if code < 0 and -code in (signal.SIGTERM, signal.SIGINT):
            say(f"бот остановлен сигналом {-code} — сервер завершается", args.log_file)
            return 0
        if clock() - started >= BACKOFF_RESET_SEC:
            backoff = BACKOFF_START_SEC
        say(f"бот завершился с кодом {code} — перезапуск через {backoff} с", args.log_file)
        sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_SEC)
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Серверный запуск bot7 с плановым перезапуском")
    ap.add_argument("--restart-days", type=float, default=7,
                    help="перезапускать бота и стирать логи раз в столько суток (0 — не перезапускать)")
    ap.add_argument("--log-file", default=os.path.join(BOT_DIR, "logs", "bot7.log"),
                    help="файл лога бота")
    ap.add_argument("--debug", action="store_true", help="бот с подробным логом")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

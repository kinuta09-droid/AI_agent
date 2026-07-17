# -*- coding: utf-8 -*-
"""
プロセス間ファイルロック (Windows msvcrt.locking ベース)

trade_log.json は bot(/crypto)・scheduler(22:30監査)・watcher(15分毎) の
3プロセスが read-modify-write するため、load→mutate→save の系列を
プロセス間で直列化する必要がある (save_json の原子性だけでは
lost-update は防げない)。

lockfile 方式 (O_EXCL) ではなく msvcrt.locking を使う理由:
プロセスが強制終了してもOSがハンドルと共にロックを解放するため、
stale ロックの検出・回収ロジックが不要になる (pid_guard で学んだ
「check→act の非原子性」の穴も構造的に存在しない)。
"""
import os
import time
import msvcrt


class FileLockTimeout(Exception):
    """ロック取得がタイムアウトした (他プロセスが保持し続けている)。"""


class FileLock:
    """対象ファイルに対応する .lock ファイルの先頭1バイトを排他ロックする。

    with FileLock(TRADE_LOG_PATH):
        logs = load_json(TRADE_LOG_PATH, default=[])
        ...mutate...
        save_json(TRADE_LOG_PATH, logs)

    対象ファイル自体ではなく別ファイルをロックするのは、save_json が
    os.replace で対象を差し替えるため、対象のハンドルを掴んだままだと
    replace が失敗する (Windows は開いているファイルを置換できない) ため。
    """

    def __init__(self, target_path: str, timeout: float = 10.0,
                 poll_interval: float = 0.1):
        self.lock_path = f"{target_path}.lock"
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fh = None

    def __enter__(self):
        directory = os.path.dirname(self.lock_path) or '.'
        os.makedirs(directory, exist_ok=True)
        self._fh = open(self.lock_path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise FileLockTimeout(
                        f"ロック取得タイムアウト ({self.timeout}s): {self.lock_path}")
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fh is not None:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # unlock失敗してもclose時にOSが解放する
            self._fh.close()
            self._fh = None
        return False

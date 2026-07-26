# -*- coding: utf-8 -*-
"""
lib/pid_guard.py — プロセス重複起動ガード (アトミック版)

なぜ独立モジュールなのか:
  bot_discord.py は discord / gemini / numpy を import するため、ガードを
  あちらに置いたままではユニットテストから import できない。本モジュールは
  標準ライブラリのみに依存し、単体でテストできる。

なぜ O_EXCL なのか:
  旧実装 (bot_discord._check_existing_bot → _write_pid) は「存在チェック」と
  「書き込み」が別操作で、しかも重量級 import が終わった後に実行されていた。
  そのため数秒差で起動した2プロセスが、両方とも「生存インスタンスなし」と
  判定して両方 PID ファイルを書けてしまう (後勝ち)。
  結果: Discord Bot が2重にゲートウェイ接続し、1回のスラッシュコマンドが
  2本の実行として並走 → logs/skill_observations.jsonl に同一秒の重複記録。
  (2026-07-17 実障害)
  os.open(O_CREAT|O_EXCL) はファイル生成の成否判定と生成自体が OS レベルで
  不可分なので、何プロセスが同時に殺到しても成功するのはただ1つになる。

なぜブートセッション ID を併記するのか:
  PID は再起動をまたいで一意ではない。電源断やシャットダウンで release() が
  走らないと PID ファイルが残り、次のブートでその PID を別プロセス (特に
  照会が ACCESS_DENIED になるシステムプロセス) が持っていると
  is_process_alive が「生存」と誤答する。設計上ここは安全側 = 生存に倒すため、
  acquire() は永久に False を返し、Bot が起動しない。
  main.py のスーパーバイザは「即 exit」を poll() で拾って5秒後に再起動するだけ
  なので、これは無言の再起動ループになり、heartbeat 監視も発動しない
  (実体が上がらないので stale 判定に到達しない)。
  そこで PID と一緒に「そのブートの起動時刻」を記録し、値が現在のブートと
  食い違うファイルは PID の生死を問わず stale とみなす。
  前ブートの PID がたまたま再利用されていても誤って「生存」と読まない。

なぜハンドルを開いたまま保持するのか:
  「read_owner で stale と判定 → os.remove」も、読んだ後・消す前に別プロセスが
  ロックを取り直すと、生きている所有者のロックを消してしまう別の TOCTOU になる。
  そこで所有者は O_EXCL で開いた fd をプロセス終了まで閉じない。Windows は
  開いているファイルの削除を拒否する (PermissionError) ため、
  「削除できる ⇔ 所有者が死んでいる」が OS 保証の不変条件になり、
  stale の引き継ぎが原子的になる。プロセスが強制終了されても OS がハンドルを
  閉じるので、残ったファイルは次回起動時に stale として正しく回収できる。

公開 API:
    acquire(pid_file, pid=None, is_alive=None, boot=None) -> bool
    release(pid_file, pid=None)                           -> bool
    read_owner(pid_file)                                  -> int | None
    read_record(pid_file)                                 -> (int|None, float|None)
    is_process_alive(pid)                                 -> bool
    boot_id()                                             -> float | None
"""
import os
import time

# 取得済みロックの fd を保持する (pid_file の絶対パス -> fd)。
# 閉じた瞬間に他プロセスが削除できてしまうので release() まで開いたままにする。
_HELD = {}

# stale ファイルを掃除してから取り直す試行回数。
# 「掃除 → 別プロセスに先を越される」が連続する確率は極めて低いので 3 で十分。
_STALE_RETRY_LIMIT = 3

# O_EXCL でファイルを作ってから PID を書き込むまでの一瞬、ファイルは空になる。
# この窓を覗いた他プロセスが「壊れている = stale」と誤判定して所有者のロックを
# 消してしまうと重複起動を許すため、空のときだけ短くリトライして読み直す。
_EMPTY_READ_RETRIES = 3
_EMPTY_READ_DELAY = 0.02

# 記録されたブート時刻と現在の推定値がこの秒数を超えてずれていたら別ブートとみなす。
# 推定値は「壁時計 - 起動からの経過時間」なので、ブート直後の NTP 同期などで
# 数十秒ずれうる。一方、再起動をまたぐと前回稼働時間ぶん (通常は時間単位) ずれる
# ため、この幅なら「同一ブートを別物と誤判定する」ことなく再起動を検出できる。
_BOOT_TOLERANCE = 120


def boot_id():
    """現在のブートセッションの識別子 (= OS 起動時刻の推定 epoch 秒)。

    「壁時計 - 起動からの経過時間」で求める。同一ブート中はほぼ一定、
    再起動すると必ず変わる。取得できない環境では None (= 機能を無効化し、
    従来どおり PID の生死判定だけで stale を決める) を返す。
    """
    try:
        if os.name == 'nt':
            import ctypes
            # GetTickCount64: 起動からの経過ミリ秒 (49.7日で溢れる GetTickCount と違い64bit)
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            uptime = kernel32.GetTickCount64() / 1000.0
        else:
            # CLOCK_BOOTTIME はサスペンド時間も含む起動からの経過秒
            uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
        return time.time() - uptime
    except Exception:
        return None


def _read_raw(pid_file, retries, delay):
    """PID ファイルの生テキストを返す。無い/読めない/空のままなら None。

    空文字は「所有者が O_EXCL で作った直後・書き込む直前」の可能性があるため
    リトライする (ここで諦めると生存中のロックを stale と誤判定してしまう)。
    """
    for attempt in range(retries):
        try:
            with open(pid_file, 'r', encoding='utf-8') as f:
                raw = f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None
        if raw:
            return raw
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def read_record(pid_file, _retries=_EMPTY_READ_RETRIES, _delay=_EMPTY_READ_DELAY):
    """(所有者 PID, 記録されたブート ID) を返す。

    フォーマットは1行目に PID、2行目に (あれば) ブート ID。
    ブート ID を持たない旧フォーマットのファイルとも互換 (その場合は None)。
    PID が数字でない = 破損は回復の見込みがないので即 None (= stale 扱い)。
    """
    raw = _read_raw(pid_file, _retries, _delay)
    if raw is None:
        return None, None
    lines = raw.splitlines()
    try:
        pid = int(lines[0].strip())
    except (ValueError, IndexError):
        return None, None
    boot = None
    if len(lines) > 1:
        try:
            boot = float(lines[1].strip())
        except ValueError:
            boot = None  # 壊れたブート ID は「無い」扱い → 従来の生死判定に委ねる
    return pid, boot


def read_owner(pid_file, _retries=_EMPTY_READ_RETRIES, _delay=_EMPTY_READ_DELAY):
    """PID ファイルの所有者 PID を返す。無い/空のまま/壊れている場合は None。"""
    return read_record(pid_file, _retries, _delay)[0]


def is_previous_boot(recorded_boot, current_boot, tolerance=_BOOT_TOLERANCE):
    """記録されたブート ID が「前回のブート」のものなら True。

    どちらかが不明なときは False (= 判定しない) を返す。ここで True に倒すと
    ブート ID を持たない旧ファイルを無条件 stale にしてしまい、重複起動ガードが
    素通りする。不明なら従来どおり PID の生死判定に委ねるのが安全側。
    """
    if recorded_boot is None or current_boot is None:
        return False
    return abs(current_boot - recorded_boot) > tolerance


def is_process_alive(pid):
    """PID のプロセスが生存しているか (Windows)。

    照会権限が無いだけのケース (ERROR_ACCESS_DENIED) を「死亡」と誤判定すると
    重複起動を許してしまうため、判定に迷ったら「生存」側に倒す。
    死亡プロセスの照会は ERROR_INVALID_PARAMETER(87) になる。
    """
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return True

    import ctypes
    ERROR_ACCESS_DENIED = 5
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # QUERY_INFORMATION より要求権限が緩い

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


def acquire(pid_file, pid=None, is_alive=None, boot=None):
    """PID ロックを原子的に取得する。

    Args:
        pid:      記録する PID (既定 = 自プロセス)。テストの並行検証用に注入可能。
        is_alive: 生存判定関数 (既定 = is_process_alive)。同上。
        boot:     ブート ID (既定 = boot_id())。None を明示注入すると
                  ブート判定を無効化できる (旧挙動の再現・テスト用)。

    Returns:
        True  … 取得成功。呼び出し元は終了時に release() すること。
        False … 他の生存インスタンスが所有中、または安全に取得できなかった。
                呼び出し元は起動を中止すること。
    """
    me = os.getpid() if pid is None else int(pid)
    alive = is_process_alive if is_alive is None else is_alive
    my_boot = boot_id() if boot is None else boot
    key = os.path.abspath(pid_file)

    for _ in range(_STALE_RETRY_LIMIT):
        try:
            fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner, owner_boot = read_record(pid_file)
            if owner == me and not is_previous_boot(owner_boot, my_boot):
                return True          # 既に自分が所有している (再入)
            # 前ブートの残骸は PID の生死を問わず stale。PID は再起動で再利用
            # されるため、ここで alive() に聞くと他人のプロセスを自分の
            # 生存インスタンスと誤認して永久に起動できなくなる。
            if is_previous_boot(owner_boot, my_boot):
                owner = None
            if owner is not None and alive(owner):
                return False         # 他インスタンスが生存中 → 起動中止
            # stale (プロセス死亡 or 内容破損) → 掃除して取り直す
            try:
                os.remove(pid_file)
            except FileNotFoundError:
                pass                 # 別プロセスが先に掃除した → そのまま再試行
            except OSError:
                # 所有者が fd を開いたままなら Windows は削除を拒否する。
                # = 所有者は生きているので安全側に倒す (read_owner が空を
                #   読んだ直後の取得競合もここで弾かれる)。
                return False
            continue
        except OSError:
            return False
        else:
            # PID とブート ID は1回の write で書く。読み手が「PID だけ書けて
            # ブート ID がまだ」の中間状態を観測すると、前ブートの残骸を
            # 生存ロックと誤認しうるため分割書き込みにしない。
            record = str(me) if my_boot is None else f"{me}\n{my_boot!r}"
            try:
                os.write(fd, record.encode('utf-8'))
            except OSError:
                # 書けなかったロックを残すと誰も掃除できないので撤去する
                os.close(fd)
                try:
                    os.remove(pid_file)
                except OSError:
                    pass
                return False
            _HELD[key] = fd          # release() まで閉じない (上の docstring 参照)
            return True
    return False


def release(pid_file, pid=None):
    """自分が所有者のときだけ PID ファイルを削除する。

    無条件削除にしないのは、他インスタンスのロックを消すと次回起動で
    ガードが素通りし、まさに防ぎたい重複起動を自ら招くため。
    """
    me = os.getpid() if pid is None else int(pid)
    key = os.path.abspath(pid_file)
    if read_owner(pid_file, _retries=1) != me:
        return False
    fd = _HELD.pop(key, None)
    if fd is not None:
        try:
            os.close(fd)             # 閉じるまで Windows は削除させない
        except OSError:
            pass
    try:
        os.remove(pid_file)
        return True
    except OSError:
        return False

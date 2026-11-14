#!/usr/bin/env python3
"""KiFS journal / CoW 自测：断电注入 + 恢复一致性。

用法: python3 tools/test_journal.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MKIFS = os.path.join(HERE, "mkifs.py")
PASS = 0
FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok - %s" % msg)
    else:
        FAIL += 1
        print("  ❌ - %s" % msg)


def run(args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, MKIFS] + args, capture_output=True, env=e)


def read_file(img, path):
    out = os.path.join(tempfile.gettempdir(), "kifs-journal-read.bin")
    r = run(["read", img, path, "--out", out])
    if r.returncode != 0:
        return r.returncode, r.stdout
    return r.returncode, open(out, "rb").read()


def main():
    global PASS, FAIL
    work = tempfile.mkdtemp(prefix="kifs-journal-")
    base = os.path.join(work, "base.img")

    print("== 基础: format + 写初始文件 ==")
    ok(run(["format", base]).returncode == 0, "format 4MiB")
    ok(run(["write", base, "/f", "--data", "OLD-OLD-OLD"]).returncode == 0, "写 /f = OLD")
    ok(run(["fsck", base]).returncode == 0, "初始 fsck 通过")

    # CoW：同一文件重写，extent 起点必须换新块
    big_old = bytes(range(256)) * 50          # 12800 B
    big_new = bytes((i + 1) & 0xFF for i in range(12800))
    p_old = os.path.join(work, "old.bin")
    p_new = os.path.join(work, "new.bin")
    open(p_old, "wb").write(big_old)
    open(p_new, "wb").write(big_new)
    img = os.path.join(work, "cow.img")
    shutil.copy(base, img)
    ok(run(["write", img, "/big", "--from", p_old]).returncode == 0, "写 /big (12800B)")
    st = run(["stat", img, "/big"]).stdout.decode()
    start_old = int(re.search(r"extents\s*:\s*\[\((\d+),", st).group(1))
    ok(run(["write", img, "/big", "--from", p_new]).returncode == 0, "重写 /big (CoW)")
    st = run(["stat", img, "/big"]).stdout.decode()
    start_new = int(re.search(r"extents\s*:\s*\[\((\d+),", st).group(1))
    ok(start_new != start_old, "CoW: 新数据写到新块 (旧 %d → 新 %d)" % (start_old, start_new))
    _, data = read_file(img, "/big")
    ok(data == big_new, "CoW: 重写后内容正确")
    ok(run(["fsck", img]).returncode == 0, "CoW: fsck 通过")
    j = run(["journal", img]).stdout.decode()
    ok("state=END" in j and "state=OPEN" not in j, "journal: 所有事务已 END")

    print("== 断电注入 ==")
    cases = [
        ("after_begin", "OLD-OLD-OLD"),     # 只有 BEGIN → 丢弃
        ("after_records", "OLD-OLD-OLD"),   # 数据记录写了但没 COMMIT → 丢弃
        ("after_commit", "NEW-NEW-NEW"),    # COMMIT 后崩 → 重放
        ("after_apply", "NEW-NEW-NEW"),     # 应用后崩 → 幂等重放
        ("torn", "OLD-OLD-OLD"),            # 最后一条记录撕裂 → 丢弃
    ]
    for point, expect in cases:
        img = os.path.join(work, "crash-%s.img" % point)
        shutil.copy(base, img)
        r = run(["write", img, "/f", "--data", "NEW-NEW-NEW"], {"KIFS_CRASH": point})
        ok(r.returncode == 2, "%s: 进程按注入点退出 (rc=%d)" % (point, r.returncode))
        rc, data = read_file(img, "/f")   # open 时自动 recover
        ok(rc == 0 and data.decode() == expect,
           "%s: 恢复后内容 = %r (实际 %r)" % (point, expect, data))
        ok(run(["fsck", img]).returncode == 0, "%s: 恢复后 fsck 通过" % point)
        j = run(["journal", img]).stdout.decode()
        ok("state=OPEN" not in j, "%s: 无未提交悬挂事务" % point)

    print("\n%d 通过, %d 失败" % (PASS, FAIL))
    if FAIL:
        sys.exit(1)
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

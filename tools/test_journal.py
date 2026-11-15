#!/usr/bin/env python3
"""KiFS v2 journal / CoW 自测：5 种断电注入 × 两种块大小 + 恢复一致性。

用法: python3 tools/test_journal.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
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


def read_file(img, path, name="read.bin"):
    out = os.path.join(tempfile.gettempdir(), "kifs-journal-" + name)
    r = run(["read", img, path, "--out", out])
    if r.returncode != 0:
        return r.returncode, b""
    return r.returncode, open(out, "rb").read()


def ext_start(stat_out):
    m = re.search(r"extents\s*:\s*\[\((\d+),", stat_out)
    return int(m.group(1)) if m else None


def suite(work, bs, mib):
    tag = "[%dK]" % (bs // 1024)
    base = os.path.join(work, "base-%d.img" % bs)
    print("== %s 基础: format %dMiB / 写初始文件 ==" % (tag, mib))
    ok(run(["format", base, "--size-mib", str(mib), "--block-size", str(bs)]).returncode == 0,
       "%s format" % tag)
    ok(run(["write", base, "/f", "--data", "OLD-OLD-OLD"]).returncode == 0, "%s 写 /f" % tag)
    ok(run(["fsck", base]).returncode == 0, "%s 初始 fsck" % tag)

    # --- CoW：同文件重写，extent 起点必须换新块 ---
    n = bs * 3 + 17
    big_old = bytes(range(256)) * (n // 256 + 1)
    big_old = big_old[:n]
    big_new = bytes((i + 1) & 0xFF for i in range(n))
    p_old = os.path.join(work, "old-%d.bin" % bs)
    p_new = os.path.join(work, "new-%d.bin" % bs)
    open(p_old, "wb").write(big_old)
    open(p_new, "wb").write(big_new)
    img = os.path.join(work, "cow-%d.img" % bs)
    shutil.copy(base, img)
    ok(run(["write", img, "/big", "--from", p_old]).returncode == 0, "%s 写 /big (%dB)" % (tag, n))
    start_old = ext_start(run(["stat", img, "/big"]).stdout.decode())
    ok(run(["write", img, "/big", "--from", p_new]).returncode == 0, "%s CoW 重写 /big" % tag)
    start_new = ext_start(run(["stat", img, "/big"]).stdout.decode())
    ok(start_old is not None and start_new is not None and start_new != start_old,
       "%s CoW 换新块 (%s → %s)" % (tag, start_old, start_new))
    rc, data = read_file(img, "/big", "cow-%d.bin" % bs)
    ok(rc == 0 and data == big_new, "%s CoW 重写内容正确" % tag)
    ok(run(["fsck", img]).returncode == 0, "%s CoW 后 fsck" % tag)
    j = run(["journal", img]).stdout.decode()
    ok("state=END" in j and "state=OPEN" not in j, "%s journal 无 OPEN" % tag)

    # --- 断电注入：写大文件（多块 + 位图 + record 多处元数据） ---
    new_payload = (b"NEW-NEW-NEW!" * (n // 12 + 1))[:n]
    p_crash = os.path.join(work, "crash-new-%d.bin" % bs)
    open(p_crash, "wb").write(new_payload)
    cases = [
        ("after_begin", "OLD"),      # 只有 BEGIN → 丢弃
        ("after_records", "OLD"),    # 数据片写了但无 COMMIT → 丢弃
        ("after_commit", "NEW"),     # COMMIT 后崩 → 重放
        ("after_apply", "NEW"),      # 应用后崩 → 幂等重放
        ("torn", "OLD"),             # 最后一条记录撕裂 → 丢弃
    ]
    print("== %s 断电注入 ==" % tag)
    for point, expect in cases:
        cimg = os.path.join(work, "crash-%s-%d.img" % (point, bs))
        shutil.copy(base, cimg)
        r = run(["write", cimg, "/f", "--from", p_crash], {"KIFS_CRASH": point})
        ok(r.returncode == 2, "%s %s: 按注入点退出 rc=2 (实际 %d)"
           % (tag, point, r.returncode))
        rc, data = read_file(cimg, "/f", "crash-%s-%d.bin" % (point, bs))
        got = "NEW" if data.startswith(b"NEW") else ("OLD" if data.startswith(b"OLD") else "?")
        ok(rc == 0 and got == expect,
           "%s %s: 恢复后内容为 %s (期望 %s)" % (tag, point, got, expect))
        ok(run(["fsck", cimg]).returncode == 0, "%s %s: 恢复后 fsck" % (tag, point))
        j = run(["journal", cimg]).stdout.decode()
        ok("state=OPEN" not in j, "%s %s: 无悬挂 OPEN 事务" % (tag, point))
        # 恢复后还能继续写
        ok(run(["write", cimg, "/after", "--data", "post-crash"]).returncode == 0,
           "%s %s: 恢复后可继续写" % (tag, point))
        ok(run(["fsck", cimg]).returncode == 0, "%s %s: 续写后 fsck" % (tag, point))

    # --- 释放块的断电注入：重写成内联（CoW 分配 + 释放旧块） ---
    cimg = os.path.join(work, "crash-free-%d.img" % bs)
    shutil.copy(base, cimg)
    ok(run(["write", cimg, "/big", "--from", p_old]).returncode == 0, "%s 准备 /big" % tag)
    r = run(["write", cimg, "/big", "--data", "tiny"], {"KIFS_CRASH": "after_commit"})
    ok(r.returncode == 2, "%s after_commit(缩为内联) rc=2" % tag)
    rc, data = read_file(cimg, "/big", "free-%d.bin" % bs)
    ok(rc == 0 and data == b"tiny", "%s 重放后 /big 变内联内容" % tag)
    ok(run(["fsck", cimg]).returncode == 0, "%s 释放块后 fsck（free_blocks 一致）" % tag)


def inprocess_recover(work):
    """同进程恢复：Image() 构造里重放后，superblock 必须刷新为盘上新值。"""
    import mkifs
    bs = 4096
    base = os.path.join(work, "inproc-base.img")
    ok(run(["format", base, "--size-mib", "4", "--block-size", str(bs)]).returncode == 0,
       "[4K] inproc format")
    ok(run(["write", base, "/f", "--data", "OLD"]).returncode == 0, "[4K] inproc 初始写")
    payload = os.path.join(work, "inproc.bin")
    open(payload, "wb").write(b"s" * 9000)
    r = run(["write", base, "/f", "--from", payload], {"KIFS_CRASH": "after_commit"})
    ok(r.returncode == 2, "[4K] inproc after_commit rc=2")
    img = mkifs.Image(base)                      # 挂载即重放
    ok(img.sb.data_next == 8 and img.sb.free_blocks == 951,
       "[4K] 同进程恢复后 superblock 已刷新 (data_next=%d free=%d)"
       % (img.sb.data_next, img.sb.free_blocks))
    img.write_file("/g", b"after-recover")
    img.flush()
    ok(run(["fsck", base]).returncode == 0, "[4K] 同进程恢复+续写后 fsck")


def main():
    work = tempfile.mkdtemp(prefix="kifs-v2-journal-")
    try:
        for bs, mib in ((4096, 4), (16384, 64)):
            suite(work, bs, mib)
        inprocess_recover(work)
        print("\n%d 通过, %d 失败" % (PASS, FAIL))
        if FAIL:
            sys.exit(1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

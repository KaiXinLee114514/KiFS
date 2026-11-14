#!/usr/bin/env python3
"""mkifs.py 自测：format/mkdir/write/read/ls/stat/rm/fsck + 损坏检测。

用法: python3 tools/test_mkifs.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MK = [sys.executable, os.path.join(HERE, "mkifs.py")]
T = 0


def run(*args, expect=0):
    global T
    T += 1
    r = subprocess.run(MK + [str(a) for a in args], capture_output=True, text=True)
    if r.returncode != expect:
        print(f"❌ [{T}] mkifs {' '.join(map(str, args))} -> {r.returncode} (expect {expect})")
        print(r.stdout, r.stderr)
        sys.exit(1)
    print(f"✅ [{T}] {' '.join(map(str, args))}")
    return r


def main():
    d = tempfile.mkdtemp(prefix="kifs-test-")
    img = os.path.join(d, "test.img")
    try:
        run("format", img, "--size-mib", "4")
        run("fsck", img)
        run("mkdir", img, "/docs")
        run("mkdir", img, "/docs/sub")
        run("write", img, "/docs/a.txt", "--data", "inline!")
        big = os.path.join(d, "big.bin")
        data = bytes(range(256)) * 50  # 12800 B，跨 4 块
        open(big, "wb").write(data)
        run("write", img, "/docs/big.bin", "--from", big)
        out = os.path.join(d, "out.bin")
        run("read", img, "/docs/big.bin", "--out", out)
        assert open(out, "rb").read() == data, "大文件回读不一致"
        print("✅ 大文件回读一致")
        r = run("ls", img, "/docs")
        assert "big.bin" in r.stdout and "a.txt" in r.stdout
        r = run("stat", img, "/docs/big.bin")
        assert "extents" in r.stdout and "[]" not in r.stdout.split("extents")[1].splitlines()[0]
        run("fsck", img)
        run("write", img, "/ro.txt", "--data", "ro", "--perm", "2044")
        run("write", img, "/ro.txt", "--data", "x", expect=1)   # 只读文件拒绝写
        run("rm", img, "/docs/a.txt")
        run("fsck", img)
        # 损坏检测：翻一个位图位
        bad = os.path.join(d, "bad.img")
        blob = bytearray(open(img, "rb").read())
        sb_off = 4096
        data_start = int.from_bytes(blob[sb_off + 20:sb_off + 24], "little")
        blob[data_start * 4096] ^= 0x01
        open(bad, "wb").write(blob)
        r = run("fsck", bad, expect=1)
        assert "位图" in r.stdout
        print(f"\n🎉 全部 {T} 项自测通过")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()

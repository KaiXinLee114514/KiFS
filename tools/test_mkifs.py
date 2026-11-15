#!/usr/bin/env python3
"""mkifs.py 自测（KiFS v2）：format/mkdir/write/read/ls/stat/rm/fsck + 损坏检测。

覆盖 4 KiB 与 16 KiB 两种块大小，以及 u64 大小 / extent 编码 / record 上限 /
稀疏大卷 format。

用法: python3 tools/test_mkifs.py
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MK = [sys.executable, os.path.join(HERE, "mkifs.py")]
T = 0


def run(*args, expect=0):
    global T
    T += 1
    r = subprocess.run(MK + [str(a) for a in args], capture_output=True, text=True)
    label = " ".join(map(str, args))
    if r.returncode != expect:
        print(f"❌ [{T}] mkifs {label} -> {r.returncode} (expect {expect})")
        print(r.stdout, r.stderr)
        sys.exit(1)
    print(f"✅ [{T}] {label}")
    return r


def check(cond, msg):
    global T
    T += 1
    if not cond:
        print(f"❌ [{T}] {msg}")
        sys.exit(1)
    print(f"✅ [{T}] {msg}")


def info_field(out, key):
    for line in out.splitlines():
        if line.strip().startswith(key):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"info 输出缺少 {key}:\n{out}")


def case_block_size(d, bs, size_mib):
    """同一套功能在给定 block_size 上跑一遍。"""
    tag = f"bs={bs}"
    img = os.path.join(d, f"t{bs}-{size_mib}.img")
    run("format", img, "--size-mib", size_mib, "--block-size", bs)
    total = size_mib * 1024 * 1024 // bs
    r = run("info", img)
    free0 = int(r.stdout.split("free_blocks")[1].split()[0])
    check(f"block_size : {bs}" in r.stdout, f"{tag} info block_size")
    check(f"total_blocks: {total} " in r.stdout, f"{tag} info total_blocks={total}")
    check(f"record@{3 + (total + 8 * bs - 1) // (8 * bs)}" in r.stdout,
          f"{tag} record 表位置")
    run("fsck", img)

    run("mkdir", img, "/docs")
    run("mkdir", img, "/docs/sub")
    # 内联写
    run("write", img, "/docs/a.txt", "--data", "inline! kifs v2")
    out = os.path.join(d, "a.out")
    run("read", img, "/docs/a.txt", "--out", out)
    check(open(out, "rb").read() == b"inline! kifs v2", f"{tag} 内联回读一致")
    # 恰好 92 B 仍然内联，93 B 落 extent
    run("write", img, "/edge92", "--data", "x" * 92)
    run("write", img, "/edge93", "--data", "y" * 93)
    r = run("stat", img, "/edge92")
    check("inline_len : 92" in r.stdout and "extents    : []" in r.stdout, f"{tag} 92B 内联")
    r = run("stat", img, "/edge93")
    check("blocks     : 1" in r.stdout and "inline_len : 0" in r.stdout
          and re.search(r"extents\s*: \[\(\d+, 1\)\]", r.stdout),
          f"{tag} 93B 走 extent")
    # 跨块大文件 + 回读
    data = bytes(range(256)) * (bs * 5 // 256 + 1)
    big = os.path.join(d, "big.bin")
    open(big, "wb").write(data)
    run("write", img, "/docs/big.bin", "--from", big)
    outb = os.path.join(d, "big.out")
    run("read", img, "/docs/big.bin", "--out", outb)
    check(open(outb, "rb").read() == data, f"{tag} {len(data)}B 大文件回读一致")
    r = run("stat", img, "/docs/big.bin")
    check(f"size       : {len(data)} 字节" in r.stdout, f"{tag} stat size")
    check(f"blocks     : {(len(data) + bs - 1) // bs}" in r.stdout, f"{tag} stat blocks")
    r = run("ls", img, "/docs")
    check("big.bin" in r.stdout and "a.txt" in r.stdout, f"{tag} ls 列表")
    run("fsck", img)
    # CoW 重写（大小变化）+ 回读
    data2 = b"Z" * (bs * 3 + 7)
    big2 = os.path.join(d, "big2.bin")
    open(big2, "wb").write(data2)
    run("write", img, "/docs/big.bin", "--from", big2)
    run("read", img, "/docs/big.bin", "--out", outb)
    check(open(outb, "rb").read() == data2, f"{tag} CoW 重写回读一致")
    run("fsck", img)
    # 只读
    run("write", img, "/ro.txt", "--data", "ro", "--perm", "2044")
    run("write", img, "/ro.txt", "--data", "x", expect=1)
    # 删除 + 回收
    run("rm", img, "/docs/big.bin")
    run("rm", img, "/edge92")
    run("rm", img, "/edge93")
    run("fsck", img)
    r = run("info", img)
    check(f"free_blocks {free0}" in r.stdout, f"{tag} 删除后 free_blocks 回到 {free0}")


def case_big_16k(d):
    """64 GiB / 16 KiB：大卷元数据容量与稀疏 format。"""
    img = os.path.join(d, "big64g.img")
    run("format", img, "--size-mib", "65536", "--block-size", "16384")
    total = 65536 * 1024 * 1024 // 16384
    r = run("info", img)
    check(f"total_blocks: {total} " in r.stdout, "64GiB total_blocks=4194304")
    check("record@35(65536 槽/512 块)" in r.stdout, "64GiB record_count=65536")
    check("block_size : 16384" in r.stdout, "64GiB block_size")
    # 稀疏：64 GiB 镜像不应真占盘
    st = os.stat(img)
    check(st.st_blocks * 512 < 64 * 1024 * 1024, "64GiB format 只写元数据（<64MiB 实际占用）")
    run("mkdir", img, "/bigdir")
    payload = os.urandom(40000)
    src = os.path.join(d, "p.bin")
    open(src, "wb").write(payload)
    run("write", img, "/bigdir/p.bin", "--from", src)
    out = os.path.join(d, "p.out")
    run("read", img, "/bigdir/p.bin", "--out", out)
    check(open(out, "rb").read() == payload, "64GiB 文件回读一致")
    run("fsck", img)


def case_meta(d):
    """u64 / v2 record 编码 / record-cap / 损坏检测。"""
    import mkifs
    # --- Record 编解码：u64 size/blocks + u64 extent start ---
    r = mkifs.Record()
    r.oid, r.type, r.perm = 77, mkifs.TYPE_FILE, 0o644
    r.size = (5 << 32) + 12345
    r.blocks = (7 << 32) + 9
    r.extents = [((1 << 33) + 17, 0xFFFF0000)]
    r.n_ext, r.n_ext_total = 1, 1
    blob = r.pack()
    check(len(blob) == 128, "record 打包恰为 128 B")
    q = mkifs.Record.unpack(blob)
    check(q.size == r.size and q.blocks == r.blocks, "u64 size/blocks 编解码")
    check(q.extents == r.extents, "extent(start u64,count u32) 12 B 编解码")
    check(blob[8:12] == struct.pack("<I", 12345) and
          blob[100:104] == struct.pack("<I", 5), "size_lo@8 / size_hi@100 偏移")
    check(blob[12:16] == struct.pack("<I", 9) and
          blob[104:108] == struct.pack("<I", 7), "blocks_lo@12 / blocks_hi@104 偏移")
    check(struct.unpack_from("<QI", blob, 36) == ((1 << 33) + 17, 0xFFFF0000),
          "extent@36 = 12 B/项")
    r2 = mkifs.Record()
    r2.oid, r2.type, r2.size, r2.inline_len, r2.inline_data = 78, 1, 92, 92, b"q" * 92
    q2 = mkifs.Record.unpack(r2.pack())
    check(q2.inline_data == b"q" * 92 and q2.size == 92, "内联数据覆盖 36..128")

    img = os.path.join(d, "meta.img")
    run("format", img, "--size-mib", "4")
    # record-cap 覆盖
    capimg = os.path.join(d, "cap.img")
    run("format", capimg, "--size-mib", "4", "--record-cap", "100")
    r = run("info", capimg)
    check("record@4(100 槽/4 块)" in r.stdout, "--record-cap 100 → 4 块 record 表")
    run("format", capimg, "--size-mib", "4", "--record-cap", "1048577", expect=1)
    # block-size 非法
    run("format", capimg, "--size-mib", "4", "--block-size", "8192", expect=2)

    # u64 大小：伪造一个 4 GiB+ 的 record，stat 应正确显示（fsck 会拒绝，仅验解析）
    run("write", img, "/f", "--data", "hi")
    m = mkifs.Image(img)
    parent_oid, name = m.resolve("/f")
    parent = m.read_record(parent_oid)
    oid = m.dir_lookup(parent, name)
    rec = m.read_record(oid)
    rec.size = (1 << 32) + 123
    rec.blocks = (2 << 32) + 1
    rec.inline_len = 0
    rec.extents = [(5, 1)]
    rec.n_ext = rec.n_ext_total = 1
    m.write_record(rec)
    m.flush()
    out = run("stat", img, "/f")
    check(f"size       : {(1 << 32) + 123} 字节" in out.stdout, "stat 显示 u64 size")
    check(f"blocks     : {(2 << 32) + 1}" in out.stdout, "stat 显示 u64 blocks")
    run("fsck", img, expect=1)          # 伪造的 size 与 extent 不一致，应被抓住

    # --- 损坏检测 ---
    clean = os.path.join(d, "clean.img")
    run("format", clean, "--size-mib", "4")
    run("mkdir", clean, "/x")
    run("write", clean, "/x/y", "--data", "hello")

    # 1) superblock 版本 → v1（probe 层拒绝）
    bad = os.path.join(d, "badver.img")
    blob = bytearray(open(clean, "rb").read())
    bs = 4096
    struct.pack_into("<I", blob, bs + 4, 0x00010000)
    struct.pack_into("<I", blob, bs + 108, zlib.crc32(bytes(blob[bs:bs + 108])) & 0xFFFFFFFF)
    open(bad, "wb").write(blob)
    r = run("info", bad, expect=1)
    check("v1" in (r.stdout + r.stderr), "v1 镜像被明确拒绝并提示重格式化")

    # 2) superblock 校验和损坏
    bad2 = os.path.join(d, "badcrc.img")
    blob = bytearray(open(clean, "rb").read())
    blob[bs + 100] ^= 0xFF
    open(bad2, "wb").write(blob)
    run("fsck", bad2, expect=1)

    # 3) superblock 备份不一致
    bad3 = os.path.join(d, "badbak.img")
    blob = bytearray(open(clean, "rb").read())
    blob[2 * bs + 40] ^= 0xFF
    open(bad3, "wb").write(blob)
    r = run("fsck", bad3, expect=1)
    check("备份" in r.stdout, "fsck 抓到 superblock 备份不一致")

    # 4) 位图：把一个空闲数据块标成已用
    bad4 = os.path.join(d, "badbit.img")
    blob = bytearray(open(clean, "rb").read())
    data_start = struct.unpack_from("<I", blob, bs + 20)[0]
    total_blocks = struct.unpack_from("<I", blob, bs + 12)[0]
    bitmap_blocks = (total_blocks + 8 * bs - 1) // (8 * bs)
    blob[data_start * bs + bitmap_blocks * bs - 1] |= 0x80
    open(bad4, "wb").write(blob)
    r = run("fsck", bad4, expect=1)
    check("位图" in r.stdout, "fsck 抓到位图翻转")

    # 5) record 类型损坏 → 目录项指向坏 record
    bad5 = os.path.join(d, "badrec.img")
    blob = bytearray(open(clean, "rb").read())
    record_start = struct.unpack_from("<I", blob, bs + 40)[0]
    blob[record_start * bs + 2 * 128 + 4] = 0      # oid 2 (目录 /x) 置空
    open(bad5, "wb").write(blob)
    run("fsck", bad5, expect=1)


def case_symlink(d):
    """type=3 符号链接：创建/readlink/ls/stat/当文件读写报错/1024B 上限/dangling。"""
    tag = "symlink"
    img = os.path.join(d, "sym.img")
    run("format", img, "--size-mib", "4")
    run("mkdir", img, "/dir")
    run("write", img, "/dir/target.txt", "--data", "hello symlink")

    # 短目标（中文 + emoji）内联存储
    target = "/dir/目标-🙂.txt"
    run("symlink", img, "/link", "--target", target)
    r = run("readlink", img, "/link")
    check(r.stdout.strip() == target, f"{tag} readlink 往返一致（中文/emoji）")
    r = run("stat", img, "/link")
    check("type       : symlink" in r.stdout, f"{tag} stat type=symlink")
    check("target     : %s" % target in r.stdout, f"{tag} stat 显示 target")
    check("size       : %d 字节" % len(target.encode()) in r.stdout,
          f"{tag} stat size=目标字节数")
    check("inline_len : %d" % len(target.encode()) in r.stdout, f"{tag} 短目标走 inline")

    # ls：类型前缀 l + " -> target"
    r = run("ls", img, "/")
    line = [l for l in r.stdout.splitlines() if l.endswith(" link -> %s" % target)]
    check(bool(line) and line[0].startswith("l "), f"{tag} ls 显示 l 前缀与 -> target")

    # dangling：目标不存在也合法
    run("symlink", img, "/dangling", "--target", "/no/such/file")
    r = run("readlink", img, "/dangling")
    check(r.stdout.strip() == "/no/such/file", f"{tag} dangling 链接 readlink 正常")

    # 指向目录
    run("symlink", img, "/dirlink", "--target", "/dir")
    r = run("readlink", img, "/dirlink")
    check(r.stdout.strip() == "/dir", f"{tag} 指向目录的链接 readlink")

    # 当普通文件读写 → 明确报错
    r = run("read", img, "/link", expect=1)
    check("符号链接" in (r.stdout + r.stderr), f"{tag} read 符号链接报错")
    r = run("write", img, "/link", "--data", "x", expect=1)
    check("符号链接" in (r.stdout + r.stderr), f"{tag} write 符号链接报错")

    # 目标长度上限：1024 B 允许（走 extent），1025 B 拒绝
    long_ok = "/" + "a" * 1023
    run("symlink", img, "/long-ok", "--target", long_ok)
    r = run("stat", img, "/long-ok")
    check("blocks     : 1" in r.stdout and "inline_len : 0" in r.stdout,
          f"{tag} 1024B 目标走 extent")
    long_bad = "/" + "b" * 1024
    r = run("symlink", img, "/long-bad", "--target", long_bad, expect=1)
    check("太长" in (r.stdout + r.stderr), f"{tag} 1025B 目标被拒绝")

    # 同名拒绝
    r = run("symlink", img, "/link", "--target", "/x", expect=1)
    check("已存在" in (r.stdout + r.stderr), f"{tag} 重名 symlink 被拒绝")

    # unlink 只删链接，不动目标
    run("rm", img, "/link")
    run("readlink", img, "/link", expect=1)
    out = os.path.join(d, "sym-target.out")
    run("read", img, "/dir/target.txt", "--out", out)
    check(open(out, "rb").read() == b"hello symlink", f"{tag} 删除链接后目标文件不受影响")

    r = run("fsck", img)
    check("符号链接" in r.stdout, f"{tag} fsck 通过并报告符号链接数量")


def main():
    d = tempfile.mkdtemp(prefix="kifs-v2-test-")
    try:
        for bs, mib in ((4096, 4), (4096, 64), (16384, 64), (16384, 256)):
            case_block_size(d, bs, mib)
        case_big_16k(d)
        case_meta(d)
        case_symlink(d)
        print(f"\n🎉 全部 {T} 项自测通过（4 KiB + 16 KiB）")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()

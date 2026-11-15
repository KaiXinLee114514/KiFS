#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KiFS Image 块级/区间 API 自测（不需要 FUSE，可在任何 Linux/macOS 上跑）。

覆盖：create_file、inline↔extent 转换、随机 offset 写（与内存模型逐字节比对）、
truncate 向下/向上/回 inline、多 extent + ext_index 表、read_at 随机读、
mkdir/chmod(保留 hide/ro 高位)/rename/unlink/rmdir，最后用 mkifs.py fsck
验证结构，并在重新打开镜像后核对内容持久化。

用法：
    python3 linux/test_image_api.py
"""

import errno
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MKIFS = os.path.join(ROOT, "tools", "mkifs.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mkifs  # noqa: E402

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok   %s" % msg)
    else:
        print("  FAIL %s" % msg)
        FAIL.append(msg)


def format_img(path, size_mib, bs):
    subprocess.check_call(
        [sys.executable, MKIFS, "format", path, "--size-mib", str(size_mib),
         "--block-size", str(bs)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fsck(path):
    p = subprocess.run([sys.executable, MKIFS, "fsck", path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check(p.returncode == 0, "fsck %s" % os.path.basename(path))
    if p.returncode != 0:
        print(p.stdout.decode("utf-8", "replace"))
    return p.returncode == 0


def write_model(img, rec, model, off, data):
    img.write_at(rec, off, data)
    end = off + len(data)
    if end > len(model):
        model.extend(b"\0" * (end - len(model)))
    model[off:end] = data


def verify_read(img, rec, model, tag):
    ok = True
    n = len(model)
    # 整段分块读
    step = 65536
    for off in range(0, n, step):
        want = bytes(model[off:off + step])
        got = img.read_at(rec, off, step)
        if got != want:
            check(False, "%s 整段读不一致 @%d" % (tag, off))
            ok = False
            break
    # 随机读
    rng = random.Random(len(model) ^ 0x5A5A)
    for _ in range(30):
        off = rng.randrange(0, n + 10)
        ln = rng.randrange(0, 5000)
        got = img.read_at(rec, off, ln)
        want = bytes(model[off:off + ln])
        if got != want:
            check(False, "%s 随机读不一致 off=%d n=%d" % (tag, off, ln))
            ok = False
            break
    check(ok, "%s 读回逐字节一致（size=%d）" % (tag, n))


def one_image(tmp, bs, size_mib):
    img_path = os.path.join(tmp, "t%d.img" % bs)
    format_img(img_path, size_mib, bs)
    print("[block_size=%d size=%dMiB]" % (bs, size_mib))
    img = mkifs.Image(img_path)

    # --- inline 写 ---
    rec = img.create_file("/a")
    img.write_at(rec, 0, b"hello")
    img.write_record(rec)
    img.flush()
    check(rec.size == 5 and rec.inline_len == 5, "inline 写入")
    check(img.read_file_at("/a", 0, 100) == b"hello", "inline 读取")

    # --- 跨 inline 增长 + 随机写 ---
    model = bytearray(b"hello")
    rng = random.Random(bs * 7919)
    for i in range(70):
        off = rng.randrange(0, 400000)
        n = rng.randrange(1, 30000)
        data = rng.randbytes(n)
        write_model(img, rec, model, off, data)
    tag = "随机写 %d 次" % 70
    check(rec.size == len(model), "%s 后 size 一致（%d）" % (tag, rec.size))
    verify_read(img, rec, model, tag)

    # --- 制造碎片，触发 ext_index 表（>3 段） ---
    nblk = max(2, len(model) // bs)
    for i in range(40):
        off = rng.randrange(0, nblk - 1) * bs + 1     # 每次命中不同块，必然切片
        data = rng.randbytes(1)
        write_model(img, rec, model, off, data)
    exts = img.rec_extents(rec)
    check(len(exts) > 3 and rec.ext_index != 0,
          "多 extent 使用 ext_index 表（%d 段, table@%d）" % (len(exts), rec.ext_index))
    img.write_record(rec)
    img.flush()
    verify_read(img, rec, model, "ext_index")

    # --- truncate 向下（非块对齐）---
    cut = 12345
    img.truncate_at(rec, cut)
    del model[cut:]
    img.write_record(rec)
    img.flush()
    verify_read(img, rec, model, "truncate 向下")
    check(rec.size == cut, "truncate 后 size=%d" % rec.size)

    # --- truncate 向上（补零）---
    img.truncate_at(rec, cut + 300000)
    model.extend(b"\0" * 300000)
    img.write_record(rec)
    img.flush()
    verify_read(img, rec, model, "truncate 向上")

    # --- truncate 到 inline ---
    img.truncate_at(rec, 40)
    model = model[:40]
    check(rec.inline_len == 40 and rec.blocks == 0, "truncate 回 inline")
    img.write_record(rec)
    img.flush()
    verify_read(img, rec, model, "truncate 回 inline")

    # --- 关闭重开，内容持久化 ---
    img.close()
    img = mkifs.Image(img_path)
    rec = img.record_at("/a")
    check(rec is not None and rec.size == 40, "重开后 record 仍在")
    check(img.read_file_at("/a", 0, 100) == bytes(model), "重开后内容一致")

    # --- 目录操作 ---
    img.mkdir_at("/d")
    img.mkdir_at("/d/sub")
    r2 = img.create_file("/d/f", perm=0o664)
    img.write_at(r2, 0, b"xyz")
    img.write_record(r2)
    # chmod 保留高位
    r2.perm |= mkifs.PERM_RO
    img.write_record(r2)
    img.chmod_at("/d/f", 0o600)
    r2 = img.record_at("/d/f")
    check((r2.perm & 0o777) == 0o600 and (r2.perm & mkifs.PERM_RO),
          "chmod 改低 9 位、保留 ro 高位")
    # 同父 / 跨父 rename
    img.rename_at("/d/f", "/d/g")
    check(img.record_at("/d/f") is None and img.record_at("/d/g") is not None,
          "同父 rename")
    img.rename_at("/d/g", "/d/sub/g2")
    check(img.record_at("/d/g") is None and img.record_at("/d/sub/g2") is not None,
          "跨父 rename")
    check(img.record_at("/d/sub/g2").parent == img.record_at("/d/sub").oid,
          "rename 后 parent 已更新")
    # 重命名到已存在的空目标
    img.create_file("/d/empty")
    img.rename_at("/d/sub/g2", "/d/empty")
    check(img.record_at("/d/sub/g2") is None and img.record_at("/d/empty").type == mkifs.TYPE_FILE,
          "rename 覆盖空目标")
    # unlink / rmdir
    img.unlink_at("/d/empty")
    check(img.record_at("/d/empty") is None, "unlink")
    try:
        img.rmdir_at("/d")
        check(False, "rmdir 非空目录应失败")
    except OSError as e:
        check(e.errno == errno.ENOTEMPTY, "rmdir 非空目录返回 ENOTEMPTY")
    img.rmdir_at("/d/sub")
    img.rmdir_at("/d")
    check(img.record_at("/d") is None, "rmdir 空目录")
    # 路径语义
    try:
        img.record_at("/a/b")
        check(False, "文件下取路径应 ENOTDIR")
    except NotADirectoryError:
        check(True, "文件下取路径返回 ENOTDIR")
    check(img.record_at("/nope") is None, "不存在的路径返回 None")

    info = img.statfs_info()
    check(info["blocks"] > 0 and info["free_blocks"] >= 0, "statfs_info 字段")
    img.write_record(rec)
    img.flush()
    img.close()
    fsck(img_path)

    # --- 重开镜像再 fsck，然后清理 ---
    img = mkifs.Image(img_path)
    check(img.read_file_at("/a", 0, 100) == bytes(model), "最终重开内容一致")
    img.close()
    fsck(img_path)


def main():
    tmp = tempfile.mkdtemp(prefix="kifs-api-")
    try:
        for bs, size_mib in ((4096, 4), (16384, 64)):
            one_image(tmp, bs, size_mib)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAIL:
        print("\n❌ %d 项失败" % len(FAIL))
        return 1
    print("\n🎉 Image 块级 API 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

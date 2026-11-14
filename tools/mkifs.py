#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mkifs.py — KiFS (Kai File System) 宿主编译器 / 检查器  [Phase A]

纯 Python、单文件，按 docs/kifs.md 的字节级布局生成与操作 4 MiB 测试镜像。
它不依赖内核，作用是：
  1. 先跑通磁盘格式，再照它写内核驱动；
  2. 做 fsck 的「标准答案」，双实现互相校验。

用法:
    python3 tools/mkifs.py format IMG [--size-mib 4] [--label KKIFS]
    python3 tools/mkifs.py mkdir  IMG /PATH
    python3 tools/mkifs.py write  IMG /PATH (--data TEXT | --from FILE) [--perm 644]
    python3 tools/mkifs.py read   IMG /PATH [--out FILE]
    python3 tools/mkifs.py ls     IMG [/PATH]
    python3 tools/mkifs.py stat   IMG /PATH
    python3 tools/mkifs.py rm     IMG /PATH
    python3 tools/mkfs.py fsck   IMG
    python3 tools/mkifs.py info   IMG

v0 简化（见 docs/kifs.md §12 Phase A）：
  - 文件数据按「连续 extent」分配（不支持碎片多段），目录内容也走同样分配。
  - Journal 只预留空间，不写事务（Phase C 再做）。
  - 目录线性扫描（Subindex 预留）。
  - 单进程、无并发锁；iolock 位先只做展示。
"""

import argparse
import os
import struct
import sys
import time
import uuid
import zlib

MAGIC = b"KIFS"
VERSION = 0x00010000
BLOCK = 4096
RECORD_SIZE = 128
INLINE_MAX = 92
INLINE_OFF = 36
JOURNAL_BLOCKS_DEFAULT = 64
LABEL_LEN = 32
OWNER_LEN = 16

# 权限位（按 docs/kifs.md §9：bit0-2 owner rwx，bit3-5 group，bit6-8 other）
PERM_HIDE = 1 << 9
PERM_RO = 1 << 10
PERM_IOLOCK = 1 << 11
PERM_OWNER_RW = 0b011          # r=bit0 w=bit1
PERM_GROUP_R = 1 << 3
PERM_OTHER_R = 1 << 6
PERM_FILE_DEFAULT = PERM_OWNER_RW | PERM_GROUP_R | PERM_OTHER_R
PERM_DIR_DEFAULT = 0b111 | (0b101 << 3) | (0b101 << 6)

SUPER_STRUCT = struct.Struct("<4s14I16s32sI")   # 112 B，后接 400 B reserved
REC_HEAD = struct.Struct("<IBBHIIIIIIBBH")     # 前 36 B
EXT_STRUCT = struct.Struct("<IHH")             # 12 B（start u32, count u16, pad）
DIRENT_HEAD = struct.Struct("<IBBH")           # 8 B
DIRENT_MIN = 8

TYPE_FILE, TYPE_DIR = 1, 2

# journal（见 docs/kifs.md §8）
JRN_MAGIC = b"KJRN"
JRN_HDR = 20
JRN_PAYLOAD = BLOCK - JRN_HDR          # 4076
JRN_STRUCT = struct.Struct("<4sIHHIHH")  # magic, seq, flags, offset, target, len, crc16
JRN_VALID, JRN_BEGIN, JRN_COMMIT, JRN_END = 1, 2, 4, 8


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def jrn_pack(seq, flags, offset, target, payload=b""):
    payload = bytes(payload)[:JRN_PAYLOAD]
    return JRN_STRUCT.pack(JRN_MAGIC, seq, flags, offset, target,
                           len(payload), crc16(payload)) + payload.ljust(JRN_PAYLOAD, b"\0")


def jrn_unpack(block):
    if len(block) < JRN_HDR:
        return None
    magic, seq, flags, offset, target, ln, crc = JRN_STRUCT.unpack_from(block, 0)
    if magic != JRN_MAGIC or not (flags & JRN_VALID) or seq == 0 or ln > JRN_PAYLOAD:
        return None
    payload = block[JRN_HDR:JRN_HDR + ln]
    if crc16(payload) != crc:
        return None
    return dict(seq=seq, flags=flags, offset=offset, target=target, payload=payload)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def align4(n):
    return (n + 3) & ~3


def default_owner():
    return os.environ.get("USER", os.environ.get("USERNAME", "kai"))[:15]


def perm_str(perm):
    """16 位 perm → rwxrwxrwx 3 位 hide/ro/iolock。"""
    out = []
    for shift in (0, 3, 6):
        g = (perm >> shift) & 0b111
        out.append(("r" if g & 1 else "-") + ("w" if g & 2 else "-") + ("x" if g & 4 else "-"))
    flags = ("1" if perm & PERM_HIDE else "0") + ("1" if perm & PERM_RO else "0") \
        + ("1" if perm & PERM_IOLOCK else "0")
    return "".join(out) + flags


def parse_perm(s):
    """支持 '644' / 'rw-r--r--' / 'rwxr-xr-x110' 三种输入。返回 (perm, extra_flag_bits)。"""
    bits = 0
    if s.isdigit():
        v = int(s, 8)
        bits = ((1 if v & 0o400 else 0)) | ((2 if v & 0o200 else 0)) | ((4 if v & 0o100 else 0))
        bits |= (((1 if v & 0o040 else 0)) | ((2 if v & 0o020 else 0)) | ((4 if v & 0o010 else 0))) << 3
        bits |= (((1 if v & 0o004 else 0)) | ((2 if v & 0o002 else 0)) | ((4 if v & 0o001 else 0))) << 6
        bits |= v & 0o7000            # bit9=hide, bit10=ro, bit11=iolock
        return bits
    s = s.replace(" ", "")
    if len(s) >= 9:
        for i, ch in enumerate("rwxrwxrwx"):
            group = i // 3
            bit = {"r": 1, "w": 2, "x": 4}[ch]
            if s[i] != "-":
                bits |= bit << (group * 3)
        tail = s[9:]
        if len(tail) >= 3 and set(tail[:3]) <= {"0", "1"}:
            if tail[0] == "1":
                bits |= PERM_HIDE
            if tail[1] == "1":
                bits |= PERM_RO
            if tail[2] == "1":
                bits |= PERM_IOLOCK
        return bits
    raise SystemExit("权限格式看不懂: %r（支持 644 / rw-r--r-- / rwxr-xr-x110）" % s)


# ---------------------------------------------------------------------------
# Superblock
# ---------------------------------------------------------------------------
class Super(object):
    def __init__(self):
        self.magic = MAGIC
        self.version = VERSION
        self.block_size = BLOCK
        self.total_blocks = 0
        self.part_lba = 0
        self.data_start = 3          # 位图起始块
        self.data_next = 0           # 下一个空闲数据块
        self.meta_next = 0           # 元数据向上游标（向下生长）
        self.journal_start = 0
        self.journal_blocks = JOURNAL_BLOCKS_DEFAULT
        self.record_start = 0
        self.record_count = 0
        self.root_oid = 1
        self.free_blocks = 0
        self.flags = 1               # clean
        self.volume_uuid = uuid.uuid4().bytes
        self.label = b"KKIFS"
        self.checksum = 0

    def pack(self):
        head = struct.pack("<4s14I16s32s", self.magic, self.version, self.block_size,
                           self.total_blocks, self.part_lba, self.data_start,
                           self.data_next, self.meta_next, self.journal_start,
                           self.journal_blocks, self.record_start, self.record_count,
                           self.root_oid, self.free_blocks, self.flags,
                           self.volume_uuid, self.label.ljust(LABEL_LEN, b"\0"))
        crc = zlib.crc32(head) & 0xFFFFFFFF
        return head + struct.pack("<I", crc) + b"\0" * (512 - len(head) - 4)

    @classmethod
    def unpack(cls, data):
        f = list(SUPER_STRUCT.unpack_from(data, 0))
        s = cls()
        (s.magic, s.version, s.block_size, s.total_blocks, s.part_lba,
         s.data_start, s.data_next, s.meta_next, s.journal_start,
         s.journal_blocks, s.record_start, s.record_count, s.root_oid,
         s.free_blocks, s.flags, s.volume_uuid, s.label, s.checksum) = f
        s.label = s.label.split(b"\0")[0]
        return s

    def checksum_ok(self, data):
        head = data[:struct.calcsize("<4s14I16s32s")]
        return (zlib.crc32(head) & 0xFFFFFFFF) == self.checksum


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------
class Record(object):
    def __init__(self):
        self.oid = 0
        self.type = 0
        self.flags = 0
        self.perm = PERM_FILE_DEFAULT
        self.size = 0
        self.blocks = 0
        self.parent = 0
        self.ctime = 0
        self.mtime = 0
        self.kw_index = 0
        self.n_ext = 0
        self.inline_len = 0
        self.inline_off = INLINE_OFF
        self.extents = []            # [(start, count)]
        self.ext_index = 0
        self.n_ext_total = 0
        self.next_free_oid = 0
        self.owner = default_owner().encode()
        self.inline_data = b""

    def pack(self):
        head = REC_HEAD.pack(self.oid, self.type, self.flags, self.perm, self.size,
                             self.blocks, self.parent, self.ctime, self.mtime,
                             self.kw_index, self.n_ext, self.inline_len,
                             self.inline_off)
        body = b""
        for (st, cnt) in (self.extents + [(0, 0)] * 3)[:3]:
            body += EXT_STRUCT.pack(st, cnt, 0)
        body = body.ljust(36, b"\0")                       # 36..72 extents
        body += struct.pack("<III", self.ext_index, self.n_ext_total,
                            self.next_free_oid)            # 72..84
        body += self.owner.ljust(OWNER_LEN, b"\0")         # 84..100
        body += b"\0" * 28                                 # 100..128
        if self.inline_len:
            data = self.inline_data[:INLINE_MAX].ljust(INLINE_MAX, b"\0")
            buf = bytearray(head + body)
            buf[INLINE_OFF:INLINE_OFF + INLINE_MAX] = data
            return bytes(buf)
        return head + body

    @classmethod
    def unpack(cls, data):
        r = cls()
        (r.oid, r.type, r.flags, r.perm, r.size, r.blocks, r.parent,
         r.ctime, r.mtime, r.kw_index, r.n_ext, r.inline_len,
         r.inline_off) = REC_HEAD.unpack_from(data, 0)
        exts = []
        if not r.inline_len:
            for i in range(3):
                st, cnt, _pad = EXT_STRUCT.unpack_from(data, 36 + i * 12)
                if cnt:
                    exts.append((st, cnt))
        r.extents = exts
        r.ext_index, r.n_ext_total, r.next_free_oid = struct.unpack_from("<III", data, 72)
        r.owner = data[84:100].split(b"\0")[0]
        if r.inline_len:
            r.inline_data = data[r.inline_off:r.inline_off + r.inline_len]
        return r


# ---------------------------------------------------------------------------
# 镜像
# ---------------------------------------------------------------------------
class Image(object):
    def __init__(self, path):
        self.path = path
        if not os.path.exists(path):
            raise SystemExit("镜像不存在: %s" % path)
        self.blob = bytearray(open(path, "rb").read())
        self.sb = Super.unpack(self.blob[BLOCK:BLOCK + 512])
        self._txn_deltas = []
        self.recover()                       # 挂载即重放未完成事务

    # ---- 基础块访问 ----
    def block(self, n):
        return self.blob[n * BLOCK:(n + 1) * BLOCK]

    def put_block(self, n, data):
        """元数据块写入（进 journal 事务）。"""
        assert len(data) <= BLOCK
        self.blob[n * BLOCK:n * BLOCK + BLOCK] = data.ljust(BLOCK, b"\0")
        self.mark(n, 0, BLOCK)

    def write_data_block(self, n, data):
        """CoW 数据块：直接落盘，不进 journal（提交前对新块是孤儿，安全）。"""
        chunk = bytes(data).ljust(BLOCK, b"\0")[:BLOCK]
        self.blob[n * BLOCK:(n + 1) * BLOCK] = chunk
        with open(self.path, "r+b") as fh:
            fh.seek(n * BLOCK)
            fh.write(chunk)

    def save(self):
        """直接整盘落盘（仅 format 用；正常路径走 flush()）。"""
        open(self.path, "wb").write(self.blob)

    # ---- journal / 事务 ----
    def mark(self, block, offset, length):
        if 0 <= block < self.sb.total_blocks and length > 0:
            self._txn_deltas.append((block, offset, bytes(self.blob[block * BLOCK + offset:
                                                                 block * BLOCK + offset + length])))

    def _jrn_blocks(self):
        base = self.sb.journal_start
        return [jrn_unpack(self.blob[(base + i) * BLOCK:(base + i + 1) * BLOCK])
                for i in range(self.sb.journal_blocks)]

    def _jrn_state(self):
        blocks = self._jrn_blocks()
        txns = {}
        for i, b in enumerate(blocks):
            if not b:
                continue
            t = txns.setdefault(b["seq"], {"begin": None, "commit": None, "end": None, "recs": []})
            if b["flags"] & JRN_BEGIN:
                t["begin"] = i
            elif b["flags"] & JRN_COMMIT:
                t["commit"] = i
            elif b["flags"] & JRN_END:
                t["end"] = i
            else:
                t["recs"].append((b["target"], b["offset"], b["payload"], i))
        return txns, blocks

    def recover(self):
        """重放已 COMMIT 但未 END 的完整事务（幂等）；丢弃未提交的残缺事务。返回重放数。"""
        txns, _blocks = self._jrn_state()
        n = 0
        for seq in sorted(txns):
            t = txns[seq]
            if t["end"] is not None:
                continue
            if t["begin"] is None or t["commit"] is None:
                # 残缺事务（崩在 BEGIN/记录阶段）：未应用过，直接标 END 回收槽位
                slot = t["begin"] if t["begin"] is not None else t["commit"]
                if slot is not None:
                    data = jrn_pack(seq, JRN_VALID | JRN_END, 0, 0, b"")
                    off = (self.sb.journal_start + slot) * BLOCK
                    with open(self.path, "r+b") as fh:
                        fh.seek(off)
                        fh.write(data)
                    self.blob[off:off + BLOCK] = data
                continue
            with open(self.path, "r+b") as fh:
                for target, off, payload, _slot in sorted(t["recs"]):
                    if target < 0 or target >= self.sb.total_blocks:
                        continue
                    fh.seek(target * BLOCK + off)
                    fh.write(payload)
                    self.blob[target * BLOCK + off:target * BLOCK + off + len(payload)] = payload
                data = jrn_pack(seq, JRN_VALID | JRN_END, 0, 0, b"")
                off = (self.sb.journal_start + t["begin"]) * BLOCK
                fh.seek(off)
                fh.write(data)
                self.blob[off:off + BLOCK] = data
            n += 1
        return n

    def flush(self):
        """把本事务的元数据增量写入 journal 并应用。"""
        deltas = self._txn_deltas
        self._txn_deltas = []
        sb_new = self.sb.pack()
        if bytes(self.blob[BLOCK:BLOCK + 112]) != sb_new[:112]:
            for bk in (1, 2, self.sb.total_blocks - 1):
                deltas.append((bk, 0, sb_new))
        if deltas:
            self._jrn_commit(deltas)

    def _jrn_commit(self, deltas):
        crash = os.environ.get("KIFS_CRASH", "")
        txns, blocks = self._jrn_state()
        seq = (max(txns) + 1) if txns else 1
        used = [i for i, b in enumerate(blocks) if b]
        n = self.sb.journal_blocks
        idx = (max(used) + 1) % n if used else 0
        # 大增量按 JRN_PAYLOAD 切片
        recs = []
        for (block, off, payload) in deltas:
            payload = bytes(payload)
            for i in range(0, len(payload), JRN_PAYLOAD):
                recs.append((block, off + i, payload[i:i + JRN_PAYLOAD]))
        if len(recs) + 3 > n:
            raise SystemExit("journal 满（%d 条记录 > %d 块）" % (len(recs) + 3, n))
        start = idx
        fh = open(self.path, "r+b")

        def w(flags, offset, target, payload=b""):
            nonlocal idx
            data = jrn_pack(seq, JRN_VALID | flags, offset, target, payload)
            off = (self.sb.journal_start + idx) * BLOCK
            fh.seek(off)
            fh.write(data)
            self.blob[off:off + BLOCK] = data
            idx = (idx + 1) % n

        w(JRN_BEGIN, 0, 0)
        if crash == "after_begin":
            fh.flush(); fh.close(); raise SystemExit(2)
        for (block, off, payload) in recs:
            w(0, off, block, payload)
        if crash == "after_records":
            fh.flush(); fh.close(); raise SystemExit(2)
        if crash == "torn":
            # 写坏最后一条记录的 payload，模拟撕裂写
            last = (self.sb.journal_start + ((idx - 1) % n)) * BLOCK
            fh.seek(last + JRN_HDR)
            fh.write(b"\xff")
            fh.flush(); fh.close(); raise SystemExit(2)
        w(JRN_COMMIT, 0, 0)
        if crash == "after_commit":
            fh.flush(); fh.close(); raise SystemExit(2)
        for (block, off, payload) in recs:
            fh.seek(block * BLOCK + off)
            fh.write(payload)
            self.blob[block * BLOCK + off:block * BLOCK + off + len(payload)] = payload
        if crash == "after_apply":
            fh.flush(); fh.close(); raise SystemExit(2)
        # END 覆盖写回 BEGIN 槽
        data = jrn_pack(seq, JRN_VALID | JRN_END, 0, 0, b"")
        off = (self.sb.journal_start + start) * BLOCK
        fh.seek(off)
        fh.write(data)
        self.blob[off:off + BLOCK] = data
        fh.flush()
        fh.close()

    # ---- 位图 ----
    def _bitmap_block(self):
        return self.sb.data_start

    def bit_get(self, blk):
        b = self.blob[self._bitmap_block() * BLOCK + blk // 8]
        return (b >> (blk % 8)) & 1

    def bit_set(self, blk, val=1):
        off = self._bitmap_block() * BLOCK + blk // 8
        if val:
            self.blob[off] |= (1 << (blk % 8))
        else:
            self.blob[off] &= ~(1 << (blk % 8)) & 0xFF
        self.mark(self._bitmap_block(), blk // 8, 1)

    # ---- Record ----
    def rec_off(self, oid):
        return self.sb.record_start * BLOCK + oid * RECORD_SIZE

    def read_record(self, oid):
        if oid <= 0 or oid >= self.sb.record_count:
            return None
        data = self.blob[self.rec_off(oid):self.rec_off(oid) + RECORD_SIZE]
        if len(data) < RECORD_SIZE:
            return None
        r = Record.unpack(data)
        return r if r.type else None

    def mark_span(self, byte_off, length):
        """把一段字节范围拆成若干 mark（可能跨块）。"""
        while length > 0:
            block = byte_off // BLOCK
            off = byte_off % BLOCK
            n = min(length, BLOCK - off)
            self.mark(block, off, n)
            byte_off += n
            length -= n

    def write_record(self, r):
        data = r.pack()
        self.blob[self.rec_off(r.oid):self.rec_off(r.oid) + RECORD_SIZE] = data[:RECORD_SIZE]
        self.mark_span(self.sb.record_start * BLOCK + r.oid * RECORD_SIZE, RECORD_SIZE)

    def alloc_oid(self):
        for oid in range(1, self.sb.record_count):
            if self.read_record(oid) is None:
                return oid
        raise SystemExit("record 表满了")

    # ---- 数据块 ----
    def alloc_blocks(self, count):
        """从 data_next 向上找连续 count 块，失败返回 None。"""
        start = self.sb.data_next
        limit = self.sb.meta_next
        run = 0
        for blk in range(start, limit):
            if self.bit_get(blk):
                run = 0
                continue
            run += 1
            if run == count:
                first = blk - count + 1
                for b in range(first, first + count):
                    self.bit_set(b, 1)
                if first == self.sb.data_next:
                    self.sb.data_next = first + count
                self.sb.free_blocks -= count
                return first
        return None

    def free_blocks(self, start, count):
        for b in range(start, start + count):
            self.bit_set(b, 0)
        self.sb.free_blocks += count
        self.sb.data_next = min(self.sb.data_next, start)

    # ---- 文件内容 ----
    def read_content(self, rec):
        if rec.inline_len:
            return bytes(rec.inline_data)
        out = b""
        for (st, cnt) in rec.extents:
            out += bytes(self.blob[st * BLOCK:(st + cnt) * BLOCK])
        return out[:rec.size]

    def write_content(self, rec, data):
        """CoW 写：数据块永远写新块（提交前是孤儿、安全），旧块在本事务内释放。"""
        n = len(data)
        old_inline = rec.inline_len
        old_extents = list(rec.extents)
        if n <= INLINE_MAX:
            # inline：先释放旧数据块，再改 record（record 更新走 journal）
            if old_extents:
                for (st, cnt) in old_extents:
                    self.free_blocks(st, cnt)
            rec.size = n
            rec.inline_len = n
            rec.inline_data = data
            rec.n_ext = 0
            rec.n_ext_total = 0
            rec.blocks = 0
            rec.extents = []
            return
        blocks = (n + BLOCK - 1) // BLOCK
        # 1) 旧块仍占用时分配新块（保证不复用旧块 → CoW）
        start = self.alloc_blocks(blocks)
        if start is None:
            raise SystemExit("盘满（连续 %d 块分配不到）" % blocks)
        # 2) 新数据块直接落盘（不进 journal；提交前是孤儿）
        for i in range(blocks):
            chunk = data[i * BLOCK:(i + 1) * BLOCK]
            self.write_data_block(start + i, chunk)
        # 3) 释放旧数据块（位图变更走 journal）
        if old_extents:
            for (st, cnt) in old_extents:
                self.free_blocks(st, cnt)
        rec.size = n
        rec.inline_len = 0
        rec.inline_data = b""
        rec.extents = [(start, blocks)]
        rec.n_ext = 1
        rec.n_ext_total = 1
        rec.blocks = blocks
        _ = old_inline  # 旧 inline 无块可释放

    def record_free_data(self, rec):
        if rec.extents:
            for (st, cnt) in rec.extents:
                self.free_blocks(st, cnt)
        rec.extents = []
        rec.n_ext = 0
        rec.n_ext_total = 0
        rec.inline_len = 0
        rec.inline_data = b""

    # ---- 路径 ----
    def resolve(self, path, create_parents=False):
        """返回 (parent_oid, name)；path 必须是绝对或相对根。"""
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            return (self.sb.root_oid, "")
        oid = self.sb.root_oid
        for p in parts[:-1]:
            rec = self.read_record(oid)
            if rec is None or rec.type != TYPE_DIR:
                raise SystemExit("不是目录: %s" % p)
            nxt = self.dir_lookup(rec, p)
            if nxt is None:
                if create_parents:
                    raise SystemExit("父目录不存在: /%s" % "/".join(parts[:-1]))
                raise SystemExit("目录不存在: %s" % p)
            oid = nxt
        return (oid, parts[-1])

    def dir_entries(self, drec):
        data = self.read_content(drec)
        out = []
        pos = 0
        while pos + DIRENT_MIN <= len(data):
            oid, typ, nlen, elen = DIRENT_HEAD.unpack_from(data, pos)
            if elen == 0:
                break
            name = data[pos + DIRENT_MIN:pos + DIRENT_MIN + nlen].decode("utf-8", "replace")
            out.append((name, oid, typ, elen))
            pos += elen
        return out

    def dir_lookup(self, drec, name):
        for (n, oid, typ, elen) in self.dir_entries(drec):
            if n == name:
                return oid
        return None

    def dir_set_entries(self, drec, entries):
        """entries = [(name, oid, type)] → 序列化并按名字排序写回。"""
        entries = sorted(entries, key=lambda e: e[0].encode("utf-8"))
        blob = b""
        for (name, oid, typ) in entries:
            nb = name.encode("utf-8")
            elen = align4(DIRENT_MIN + len(nb))
            blob += DIRENT_HEAD.pack(oid, typ, len(nb), elen) + nb
            blob += b"\0" * (elen - DIRENT_MIN - len(nb))
        self.write_content(drec, blob)

    def dir_add(self, drec, name, oid, typ):
        entries = [(n, o, t) for (n, o, t, _e) in self.dir_entries(drec)]
        if any(n == name for (n, _o, _t) in entries):
            raise SystemExit("已存在同名项: %s" % name)
        entries.append((name, oid, typ))
        self.dir_set_entries(drec, entries)

    def dir_remove(self, drec, name):
        entries = [(n, o, t) for (n, o, t, _e) in self.dir_entries(drec) if n != name]
        self.dir_set_entries(drec, entries)

    # ---- 高级操作 ----
    def mkdir(self, path, perm=PERM_DIR_DEFAULT):
        parent_oid, name = self.resolve(path)
        if not name:
            raise SystemExit("不能创建根 /")
        parent = self.read_record(parent_oid)
        if self.dir_lookup(parent, name) is not None:
            raise SystemExit("已存在: %s" % path)
        oid = self.alloc_oid()
        now = int(time.time())
        r = Record()
        r.oid, r.type, r.perm = oid, TYPE_DIR, perm
        r.parent, r.ctime, r.mtime = parent_oid, now, now
        r.owner = default_owner().encode()
        self.write_record(r)
        self.dir_add(parent, name, oid, TYPE_DIR)
        self.write_record(parent)
        return oid

    def write_file(self, path, data, perm=None):
        parent_oid, name = self.resolve(path)
        if not name:
            raise SystemExit("不能写根 /")
        parent = self.read_record(parent_oid)
        oid = self.dir_lookup(parent, name)
        now = int(time.time())
        if oid is not None:
            rec = self.read_record(oid)
            if rec.type != TYPE_FILE:
                raise SystemExit("目标是目录: %s" % path)
            if rec.perm & PERM_RO:
                raise SystemExit("文件是只读的 (ro): %s" % path)
            self.write_content(rec, data)
            rec.mtime = now
            if perm is not None:
                rec.perm = perm
            self.write_record(rec)
            return oid
        oid = self.alloc_oid()
        r = Record()
        r.oid, r.type, r.parent = oid, TYPE_FILE, parent_oid
        r.perm = perm if perm is not None else PERM_FILE_DEFAULT
        r.ctime = r.mtime = now
        r.owner = default_owner().encode()
        self.write_record(r)
        self.write_content(r, data)
        self.write_record(r)
        self.dir_add(parent, name, oid, TYPE_FILE)
        self.write_record(parent)
        return oid

    def read_file(self, path):
        parent_oid, name = self.resolve(path)
        parent = self.read_record(parent_oid)
        oid = self.dir_lookup(parent, name) if name else None
        if oid is None:
            raise SystemExit("找不到: %s" % path)
        rec = self.read_record(oid)
        if rec.type != TYPE_FILE:
            raise SystemExit("是目录: %s" % path)
        return self.read_content(rec)

    def rm(self, path):
        parent_oid, name = self.resolve(path)
        parent = self.read_record(parent_oid)
        oid = self.dir_lookup(parent, name) if name else None
        if oid is None:
            raise SystemExit("找不到: %s" % path)
        rec = self.read_record(oid)
        if rec.type == TYPE_DIR:
            if self.dir_entries(rec):
                raise SystemExit("目录非空: %s" % path)
        if rec.perm & PERM_RO:
            raise SystemExit("文件是只读的 (ro): %s" % path)
        self.record_free_data(rec)
        rec.type = 0
        self.write_record(rec)
        self.dir_remove(parent, name)
        self.write_record(parent)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_format(args):
    size = args.size_mib * 1024 * 1024
    if size % BLOCK:
        raise SystemExit("size 必须是 4 KiB 的倍数")
    total = size // BLOCK
    sb = Super()
    sb.total_blocks = total
    sb.journal_blocks = JOURNAL_BLOCKS_DEFAULT
    sb.journal_start = total - 1 - sb.journal_blocks
    sb.data_start = 3
    bitmap_blocks = (total + 8 * BLOCK - 1) // (8 * BLOCK)
    sb.record_start = sb.data_start + bitmap_blocks
    sb.record_count = min(max(16, total // 64), 4096)
    record_blocks = (sb.record_count * RECORD_SIZE + BLOCK - 1) // BLOCK
    sb.data_next = sb.record_start + record_blocks
    sb.meta_next = sb.journal_start
    sb.label = args.label.encode()[:LABEL_LEN - 1]
    blob = bytearray(size)
    # 引导块留空（0），superblock 主/备份
    blob[BLOCK:BLOCK + 512] = sb.pack()
    blob[2 * BLOCK:2 * BLOCK + 512] = sb.pack()
    blob[(total - 1) * BLOCK:(total - 1) * BLOCK + 512] = sb.pack()
    # 位图：把引导/superblock/位图/record 表/元数据/日志区全部标已用
    img = Image.__new__(Image)
    img.blob = blob
    img.sb = sb
    img.path = args.img
    img._txn_deltas = []
    for b in range(0, sb.data_next):
        img.bit_set(b, 1)
    for b in range(sb.meta_next, total):
        img.bit_set(b, 1)
    sb.free_blocks = sum(1 for b in range(sb.data_next, sb.meta_next) if not img.bit_get(b))
    # 根目录 record
    root = Record()
    root.oid, root.type, root.perm = 1, TYPE_DIR, PERM_DIR_DEFAULT
    root.parent = 0
    root.ctime = root.mtime = int(time.time())
    root.owner = default_owner().encode()
    img.write_record(root)
    blob[BLOCK:BLOCK + 512] = sb.pack()
    blob[2 * BLOCK:2 * BLOCK + 512] = sb.pack()
    blob[(total - 1) * BLOCK:(total - 1) * BLOCK + 512] = sb.pack()
    open(args.img, "wb").write(blob)
    print("✅ 格式化完成: %s" % args.img)
    print("   块大小 %d B / 总块 %d / 数据区 %d-%d / record %d 个 / journal %d 块" %
          (BLOCK, total, sb.data_next, sb.meta_next - 1, sb.record_count, sb.journal_blocks))
    print("   UUID %s / 卷标 %s" % (uuid.UUID(bytes=sb.volume_uuid), sb.label.decode()))


def open_img(args):
    return Image(args.img)


def cmd_mkdir(args):
    img = open_img(args)
    img.mkdir(args.path, perm=parse_perm(args.perm) if args.perm else PERM_DIR_DEFAULT)
    img.flush()
    print("✅ mkdir %s" % args.path)


def cmd_write(args):
    if args.data is not None and args.src is not None:
        raise SystemExit("--data 和 --from 只能给一个")
    if args.data is not None:
        data = args.data.encode()
    elif args.src is not None:
        data = open(args.src, "rb").read()
    else:
        data = sys.stdin.buffer.read()
    img = open_img(args)
    img.write_file(args.path, data, perm=parse_perm(args.perm) if args.perm else None)
    img.flush()
    print("✅ write %s (%d 字节)" % (args.path, len(data)))


def cmd_read(args):
    img = open_img(args)
    data = img.read_file(args.path)
    if args.out:
        open(args.out, "wb").write(data)
        print("✅ 读出 %d 字节 → %s" % (len(data), args.out))
    else:
        sys.stdout.buffer.write(data)
        if not data.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")


def cmd_ls(args):
    img = open_img(args)
    path = args.path or "/"
    parent_oid, name = img.resolve(path)
    parent = img.read_record(parent_oid)
    if not name:
        parent = img.read_record(img.sb.root_oid)
    if name:
        oid = img.dir_lookup(parent, name)
        if oid is None:
            raise SystemExit("找不到: %s" % path)
        parent = img.read_record(oid)
        if parent is None or parent.type != TYPE_DIR:
            raise SystemExit("不是目录: %s" % path)
    entries = img.dir_entries(parent)
    print("d %s %-8s %s" % ("dir", "0", "."))
    for (n, oid, typ, _e) in entries:
        rec = img.read_record(oid)
        if rec is None:
            print("❌ %s → 坏 record %d" % (n, oid))
            continue
        kind = "d" if rec.type == TYPE_DIR else "-"
        show = n if not (rec.perm & PERM_HIDE) else "." + n
        print("%s %s %-8s %s" % (kind, perm_str(rec.perm) + rec.owner.decode("utf-8", "replace"),
                                 str(rec.size) if rec.type == TYPE_FILE else "dir", show))


def cmd_stat(args):
    img = open_img(args)
    parent_oid, name = img.resolve(args.path)
    parent = img.read_record(parent_oid)
    oid = img.dir_lookup(parent, name) if name else 1
    if oid is None:
        raise SystemExit("找不到: %s" % args.path)
    rec = img.read_record(oid)
    print("oid        : %d" % rec.oid)
    print("type       : %s" % ("dir" if rec.type == TYPE_DIR else "file"))
    print("perm       : %s%s" % (perm_str(rec.perm), rec.owner.decode("utf-8", "replace")))
    print("size       : %d 字节" % rec.size)
    print("blocks     : %d" % rec.blocks)
    print("inline_len : %d" % rec.inline_len)
    print("extents    : %s" % rec.extents)
    print("parent     : %d" % rec.parent)
    print("ctime/mtime: %d / %d" % (rec.ctime, rec.mtime))


def cmd_rm(args):
    img = open_img(args)
    img.rm(args.path)
    img.flush()
    print("✅ rm %s" % args.path)


def cmd_info(args):
    img = open_img(args)
    sb = img.sb
    print("magic      : %s" % sb.magic.decode(errors="replace"))
    print("version    : 0x%08X" % sb.version)
    print("block_size : %d" % sb.block_size)
    print("total_blocks: %d (%.1f MiB)" % (sb.total_blocks, sb.total_blocks * BLOCK / 1048576.0))
    print("layout     : bitmap@%d record@%d(+%d) data_next=%d meta_next=%d journal=%d+%d" %
          (sb.data_start, sb.record_start, sb.record_count, sb.data_next,
           sb.meta_next, sb.journal_start, sb.journal_blocks))
    print("root_oid   : %d / free_blocks %d" % (sb.root_oid, sb.free_blocks))
    print("uuid       : %s" % uuid.UUID(bytes=sb.volume_uuid))
    print("label      : %s flags=%d checksum_ok=%s" %
          (sb.label.decode(errors="replace"), sb.flags, sb.checksum_ok(img.block(1))))


def cmd_fsck(args):
    img = open_img(args)
    sb = img.sb
    problems = []
    owner = {}
    used = {}
    rec_blocks = (sb.record_count * RECORD_SIZE + BLOCK - 1) // BLOCK
    data_area = sb.record_start + rec_blocks      # 数据块区真正起点（固定区到此为止）

    def take(blk, what, need=1):
        for b in range(blk, blk + need):
            if b in used:
                problems.append("块 %d 被重复占用: %s 和 %s" % (b, used[b], what))
            used[b] = what

    # superblock
    if sb.magic != MAGIC:
        problems.append("superblock magic 不对: %r" % sb.magic)
    if sb.version != VERSION:
        problems.append("版本不对: 0x%X" % sb.version)
    if not sb.checksum_ok(img.block(1)):
        problems.append("superblock 校验和不对")
    for bk in (2, sb.total_blocks - 1):
        if img.block(bk)[:112] != img.block(1)[:112]:
            problems.append("superblock 备份 @块 %d 与主副本不一致" % bk)
    # journal：open 时已 recover，这里只应剩「未提交的旧事务」；有未重放的完整事务才是问题
    txns, _jblocks = img._jrn_state()
    pending = [s for s, t in txns.items() if t["commit"] is not None and t["end"] is None]
    if pending:
        problems.append("journal 有 %d 个已提交未重放事务: %s" % (len(pending), pending))
    # 固定区占用
    for b in range(0, data_area):
        take(b, "固定区")
    for b in range(sb.meta_next, sb.total_blocks):
        take(b, "元数据/日志区")

    # records + 数据块
    for oid in range(1, sb.record_count):
        rec = img.read_record(oid)
        if rec is None:
            continue
        owner[oid] = rec
        if rec.type not in (TYPE_FILE, TYPE_DIR):
            problems.append("oid %d 未知类型 %d" % (oid, rec.type))
            continue
        if rec.oid != oid:
            problems.append("oid %d 槽里的 oid=%d 不一致" % (oid, rec.oid))
        if rec.type == TYPE_FILE:
            cap = INLINE_MAX if rec.inline_len else rec.blocks * BLOCK
            if rec.size > cap:
                problems.append("oid %d size=%d 超出容量 %d" % (oid, rec.size, cap))
            if rec.inline_len and rec.blocks:
                problems.append("oid %d 既 inline 又占块" % oid)
            if rec.inline_len and rec.n_ext:
                problems.append("oid %d 既 inline 又有 extent" % oid)
        if not rec.inline_len:
            for (st, cnt) in rec.extents:
                if st < data_area or st + cnt > sb.meta_next:
                    problems.append("oid %d extent %d+%d 越界" % (oid, st, cnt))
                else:
                    take(st, "oid %d" % oid, cnt)
        if rec.parent:
            p = owner.get(rec.parent)
            if p is None and rec.parent != 0:
                if img.read_record(rec.parent) is None:
                    problems.append("oid %d parent=%d 不存在" % (oid, rec.parent))

    # 目录项
    seen = set([1])
    visited = set()
    stack = [1]
    while stack:
        d = stack.pop()
        if d in visited:
            problems.append("目录 oid %d 被重复遍历（疑似环/重复引用）" % d)
            continue
        visited.add(d)
        drec = img.read_record(d)
        if drec is None or drec.type != TYPE_DIR:
            problems.append("目录 oid %d 不存在/不是目录" % d)
            continue
        data = img.read_content(drec)
        pos = 0
        while pos + DIRENT_MIN <= len(data):
            oid, typ, nlen, elen = DIRENT_HEAD.unpack_from(data, pos)
            if elen == 0:
                break
            name = data[pos + DIRENT_MIN:pos + DIRENT_MIN + nlen].decode("utf-8", "replace")
            if elen < align4(DIRENT_MIN + nlen):
                problems.append("目录 oid %d 项 %s elen=%d 太小" % (d, name, elen))
            if pos + elen > len(data):
                problems.append("目录 oid %d 项 %s 越界" % (d, name))
                break
            child = img.read_record(oid)
            if child is None:
                problems.append("目录 oid %d 项 %s 指向不存在的 record %d" % (d, name, oid))
            else:
                if child.parent != d:
                    problems.append("oid %d 的 parent=%d 与目录 oid %d 不符" % (oid, child.parent, d))
                if child.type != typ:
                    problems.append("目录项 %s 的 type 与 record 不符" % name)
                seen.add(oid)
                if child.type == TYPE_DIR:
                    stack.append(oid)
            pos += elen

    # 位图一致性
    for b in range(data_area, sb.meta_next):
        on = img.bit_get(b)
        if on and b not in used:
            problems.append("位图标记块 %d 已用，但没有文件占用" % b)
        if (not on) and b in used:
            problems.append("块 %d 被占用但位图没标记" % b)
    for b in range(0, data_area):
        if not img.bit_get(b):
            problems.append("固定区块 %d 位图未标记" % b)
    for b in range(sb.meta_next, sb.total_blocks):
        if not img.bit_get(b):
            problems.append("元数据区块 %d 位图未标记" % b)
    real_free = sum(1 for b in range(data_area, sb.meta_next) if not img.bit_get(b))
    if real_free != sb.free_blocks:
        problems.append("free_blocks=%d 实际空闲=%d" % (sb.free_blocks, real_free))

    # 孤立 record
    for oid in owner:
        if oid not in seen:
            problems.append("oid %d 未被任何目录项引用（孤立）" % oid)

    if problems:
        print("❌ fsck 发现 %d 个问题:" % len(problems))
        for p in problems[:50]:
            print("   - %s" % p)
        return 1
    print("✅ fsck 通过: %d 个 record, %d 个块在用, 空闲 %d 块" %
          (len(owner), len(used), real_free))
    return 0


def cmd_journal(args):
    img = open_img(args)
    txns, _blocks = img._jrn_state()
    print("journal: %d 块, %d 个事务" % (img.sb.journal_blocks, len(txns)))
    for seq in sorted(txns):
        t = txns[seq]
        state = "END" if t["end"] is not None else ("COMMIT" if t["commit"] is not None else "OPEN")
        print("  seq=%d state=%s records=%d" % (seq, state, len(t["recs"])))
    if not txns:
        print("  （空）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="KiFS 宿主工具 (Phase A)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("format"); p.add_argument("img")
    p.add_argument("--size-mib", type=int, default=4); p.add_argument("--label", default="KKIFS")
    p.set_defaults(func=cmd_format)

    p = sub.add_parser("mkdir"); p.add_argument("img"); p.add_argument("path")
    p.add_argument("--perm", default=None); p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser("write"); p.add_argument("img"); p.add_argument("path")
    p.add_argument("--data", default=None); p.add_argument("--from", dest="src", default=None)
    p.add_argument("--perm", default=None); p.set_defaults(func=cmd_write)

    p = sub.add_parser("read"); p.add_argument("img"); p.add_argument("path")
    p.add_argument("--out", default=None); p.set_defaults(func=cmd_read)

    p = sub.add_parser("ls"); p.add_argument("img"); p.add_argument("path", nargs="?", default=None)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("stat"); p.add_argument("img"); p.add_argument("path")
    p.set_defaults(func=cmd_stat)

    p = sub.add_parser("journal"); p.add_argument("img")
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("rm"); p.add_argument("img"); p.add_argument("path")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("info"); p.add_argument("img"); p.set_defaults(func=cmd_info)

    p = sub.add_parser("fsck"); p.add_argument("img"); p.set_defaults(func=cmd_fsck)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

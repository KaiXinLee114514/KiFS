#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mkifs.py — KiFS v2 (Kai File System) 宿主编译器 / 检查器

纯 Python、单文件，按 docs/kifs-v2.md 的字节级布局生成与操作 KiFS **v2** 镜像：
  - 块大小 4096 / 16384（`--block-size`，默认 4096 用于 CI 小镜像，大卷用 16384）
  - u64 文件大小 / u64 块数（size_lo+size_hi、blocks_lo+blocks_hi）
  - extent = (start u64, count u32)，12 B/项
  - record 表上限 1<<20（`--record-cap` 覆盖）
  - 大卷 format 只写元数据区（bitmap + record 表 + superblock + journal 区），数据区留稀疏空洞

它不依赖内核，作用是：
  1. 先跑通磁盘格式，再照它写内核驱动；
  2. 做 fsck 的「标准答案」，双实现互相校验。

用法:
    python3 tools/mkifs.py format IMG [--size-mib 4] [--block-size 4096|16384]
                                     [--label KKIFS] [--record-cap N]
    python3 tools/mkifs.py mkdir  IMG /PATH
    python3 tools/mkifs.py write  IMG /PATH (--data TEXT | --from FILE) [--perm 644]
    python3 tools/mkifs.py read   IMG /PATH [--out FILE]
    python3 tools/mkifs.py ls     IMG [/PATH]
    python3 tools/mkifs.py stat   IMG /PATH
    python3 tools/mkifs.py rm     IMG /PATH
    python3 tools/mkifs.py fsck   IMG
    python3 tools/mkifs.py info   IMG
    python3 tools/mkifs.py journal IMG

v2 简化（见 docs/kifs.md §12 Phase A）：
  - 文件数据按「连续 extent」分配（写入不会产生多段）；读取/fsck 兼容 ext_index 表。
  - 目录线性扫描（Subindex 预留）。
  - 单进程、无并发锁；iolock 位先只做展示。
  - 旧 v1 格式不再支持（version != 0x00020000 直接拒绝）。
"""

import argparse
import errno
import os
import stat
import struct
import sys
import time
import uuid
import zlib

MAGIC = b"KIFS"
VERSION = 0x00020000
BLOCK_SIZES = (4096, 16384)
DEFAULT_BLOCK = 4096
RECORD_SIZE = 128
RECORD_MAX = 1 << 20
INLINE_MAX = 92
INLINE_OFF = 36
JOURNAL_BLOCKS_DEFAULT = 64
LABEL_LEN = 32
OWNER_LEN = 16
CACHE_BLOCKS = 512          # 块缓存上限（脏块不会被驱逐）

# 权限位（按 docs/kifs.md §9：bit0-2 owner rwx，bit3-5 group，bit6-8 other）
PERM_HIDE = 1 << 9
PERM_RO = 1 << 10
PERM_IOLOCK = 1 << 11
PERM_OWNER_RW = 0b011          # r=bit0 w=bit1
PERM_GROUP_R = 1 << 3
PERM_OTHER_R = 1 << 6
PERM_FILE_DEFAULT = PERM_OWNER_RW | PERM_GROUP_R | PERM_OTHER_R
PERM_DIR_DEFAULT = 0b111 | (0b101 << 3) | (0b101 << 6)
PERM_SYMLINK_DEFAULT = 0o777

# 符号链接目标上限（UTF-8 字节数，不含结尾 NUL）
SYMLINK_TARGET_MAX = 1024

SUPER_STRUCT = struct.Struct("<4s14I16s32sI")   # 112 B，后接 400 B reserved
REC_HEAD = struct.Struct("<IBBHIIIIIIBBH")      # 前 36 B（v2）
EXT_STRUCT = struct.Struct("<QI")               # 12 B：start u64, count u32（v2）
DIRENT_HEAD = struct.Struct("<IBBH")            # 8 B
DIRENT_MIN = 8

TYPE_FILE, TYPE_DIR, TYPE_SYMLINK = 1, 2, 3

# journal（见 docs/kifs.md §8）
JRN_MAGIC = b"KJRN"
JRN_HDR = 20
JRN_STRUCT = struct.Struct("<4sIHHIHH")  # magic, seq, flags, offset, target, len, crc16
JRN_VALID, JRN_BEGIN, JRN_COMMIT, JRN_END = 1, 2, 4, 8

# 块号是 u32：总块数最多 2^32-1（64 TiB 的 16 KiB 卷恰好是 2^32 块，差 1 块）
MAX_TOTAL_BLOCKS = 0xFFFFFFFF


def ceil_div(a, b):
    return (a + b - 1) // b


def bitmap_blocks_for(total_blocks, block_size):
    """位图占用的块数：1 bit/块，ceil(total / (8*block_size))。"""
    return ceil_div(total_blocks, 8 * block_size)


def record_count_for(total_blocks):
    """v2 record 上限公式：min(max(16, total//64), 1<<20)。"""
    return min(max(16, total_blocks // 64), RECORD_MAX)


def record_blocks_for(record_count, block_size):
    return ceil_div(record_count * RECORD_SIZE, block_size)


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def jrn_pack(seq, flags, offset, target, payload=b"", block_size=DEFAULT_BLOCK):
    payload_max = block_size - JRN_HDR
    payload = bytes(payload)[:payload_max]
    return (JRN_STRUCT.pack(JRN_MAGIC, seq, flags, offset, target,
                            len(payload), crc16(payload))
            + payload.ljust(payload_max, b"\0"))


def jrn_unpack(block, block_size=DEFAULT_BLOCK):
    payload_max = block_size - JRN_HDR
    if len(block) < JRN_HDR:
        return None
    magic, seq, flags, offset, target, ln, crc = JRN_STRUCT.unpack_from(block, 0)
    if magic != JRN_MAGIC or not (flags & JRN_VALID) or seq == 0 or ln > payload_max:
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
    """支持 '644' / 'rw-r--r--' / 'rwxr-xr-x110' 三种输入。返回 perm 位。"""
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


def set_bit_range(buf, base, lo, hi):
    """buf 覆盖块号 [base, base+8*len(buf))；把其中的 [lo, hi) 置 1。"""
    lo = max(lo, base)
    hi = min(hi, base + 8 * len(buf))
    if lo >= hi:
        return
    b0 = (lo - base) // 8
    b1 = (hi - base) // 8
    if b0 == b1:
        m0 = (lo - base) - b0 * 8
        m1 = (hi - base) - b0 * 8
        buf[b0] |= (((1 << m1) - 1) ^ ((1 << m0) - 1)) & 0xFF
        return
    buf[b0] |= (0xFF << ((lo - base) - b0 * 8)) & 0xFF
    for i in range(b0 + 1, b1):
        buf[i] = 0xFF
    rem = (hi - base) - b1 * 8
    if rem:
        buf[b1] |= (1 << rem) - 1


# ---------------------------------------------------------------------------
# Superblock
# ---------------------------------------------------------------------------
class Super(object):
    def __init__(self):
        self.magic = MAGIC
        self.version = VERSION
        self.block_size = DEFAULT_BLOCK
        self.total_blocks = 0
        self.part_lba = 0
        self.data_start = 3          # 位图起始块
        self.data_next = 0           # 下一个空闲数据块（向上游标）
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
# Record（128 B，v2）
# ---------------------------------------------------------------------------
class Record(object):
    def __init__(self):
        self.oid = 0
        self.type = 0
        self.flags = 0
        self.perm = PERM_FILE_DEFAULT
        self.size = 0                # u64
        self.blocks = 0              # u64
        self.parent = 0
        self.ctime = 0
        self.mtime = 0
        self.kw_index = 0
        self.n_ext = 0
        self.inline_len = 0
        self.inline_off = INLINE_OFF
        self.extents = []            # [(start u64, count u32)]
        self.ext_index = 0
        self.n_ext_total = 0
        self.next_free_oid = 0
        self.owner = default_owner().encode()
        self.inline_data = b""

    def pack(self):
        head = REC_HEAD.pack(self.oid, self.type, self.flags, self.perm,
                             self.size & 0xFFFFFFFF, self.blocks & 0xFFFFFFFF,
                             self.parent, self.ctime, self.mtime, self.kw_index,
                             self.n_ext, self.inline_len, self.inline_off)
        body = b""
        for (st, cnt) in (list(self.extents) + [(0, 0)] * 3)[:3]:
            body += EXT_STRUCT.pack(st & 0xFFFFFFFFFFFFFFFF, cnt & 0xFFFFFFFF)
        body = body.ljust(36, b"\0")                        # 36..72 extents
        body += struct.pack("<III", self.ext_index, self.n_ext_total,
                            self.next_free_oid)             # 72..84
        body += self.owner[:OWNER_LEN].ljust(OWNER_LEN, b"\0")   # 84..100
        body += struct.pack("<II", self.size >> 32, self.blocks >> 32)  # 100..108
        body += b"\0" * 20                                  # 108..128
        if self.inline_len:
            data = self.inline_data[:INLINE_MAX].ljust(INLINE_MAX, b"\0")
            buf = bytearray(head + body)
            buf[INLINE_OFF:INLINE_OFF + INLINE_MAX] = data
            return bytes(buf)
        return head + body

    @classmethod
    def unpack(cls, data):
        r = cls()
        (r.oid, r.type, r.flags, r.perm, size_lo, blocks_lo, r.parent,
         r.ctime, r.mtime, r.kw_index, r.n_ext, r.inline_len,
         r.inline_off) = REC_HEAD.unpack_from(data, 0)
        exts = []
        if not r.inline_len:
            for i in range(3):
                st, cnt = EXT_STRUCT.unpack_from(data, 36 + i * 12)
                if cnt:
                    exts.append((st, cnt))
        r.extents = exts
        r.ext_index, r.n_ext_total, r.next_free_oid = struct.unpack_from("<III", data, 72)
        r.owner = data[84:100].split(b"\0")[0]
        size_hi, blocks_hi = struct.unpack_from("<II", data, 100)
        if r.inline_len:
            # 内联数据覆盖 36..128：extents/ext_index/n_ext_total/owner/size_hi/blocks_hi
            # 这些槽位都是文件内容，不能当元数据解析
            r.inline_data = data[r.inline_off:r.inline_off + r.inline_len]
            r.size = r.inline_len
            r.blocks = 0
            r.extents = []
            r.ext_index = 0
            r.n_ext = 0
            r.n_ext_total = 0
            r.next_free_oid = 0
            r.owner = b""
        else:
            r.size = size_lo | (size_hi << 32)
            r.blocks = blocks_lo | (blocks_hi << 32)
        return r


# ---------------------------------------------------------------------------
# 镜像（按需读 + 块缓存；不整盘载入内存）
# ---------------------------------------------------------------------------
def device_size(path):
    """镜像文件大小，或块设备的字节数（BLKGETSIZE64）。"""
    st = os.stat(path)
    if stat.S_ISBLK(st.st_mode):
        import array
        import fcntl
        try:
            with open(path, "rb") as f:
                buf = array.array("Q", [0])
                fcntl.ioctl(f.fileno(), 0x80081272, buf, True)
                return buf[0]
        except OSError as e:
            raise SystemExit("读块设备大小失败: %s" % e)
    return st.st_size


class Image(object):
    def __init__(self, path, offset=0):
        self.path = path
        self.offset = int(offset or 0)
        if self.offset < 0:
            raise SystemExit("offset 不能为负: %d" % self.offset)
        if not os.path.exists(path):
            raise SystemExit("镜像不存在: %s" % path)
        self.fh = open(path, "r+b")
        self.dev_size = self._device_size()
        if self.offset + 512 > self.dev_size:
            raise SystemExit("offset %d 超出设备大小 %d" % (self.offset, self.dev_size))
        self.bs = None
        self._cache = {}
        self._dirty = set()
        self._txn_deltas = []
        self._used_recs = None
        self.sb = self._probe_super()
        self.bs = self.sb.block_size
        if self.offset + self.sb.total_blocks * self.bs > self.dev_size:
            raise SystemExit(
                "KiFS 卷 [%d, %d) 超出设备大小 %d（offset 不对？）" %
                (self.offset, self.offset + self.sb.total_blocks * self.bs,
                 self.dev_size))
        self.recover()                       # 挂载即重放未完成事务

    def _device_size(self):
        return device_size(self.path)

    def close(self):
        try:
            self.fh.close()
        except OSError:
            pass

    # ---- 底层 I/O ----
    def _pread(self, off, n):
        self.fh.seek(self.offset + off)
        return self.fh.read(n)

    def _disk_write(self, off, data):
        """直接落盘（journal 应用 / 数据块），并同步已缓存的块。"""
        self.fh.seek(self.offset + off)
        self.fh.write(data)
        if self.bs is None:
            return
        blk = off // self.bs
        end = off + len(data)
        while blk * self.bs < end:
            b = self._cache.get(blk)
            if b is not None:
                b0 = max(blk * self.bs, off)
                b1 = min((blk + 1) * self.bs, end)
                b[b0 - blk * self.bs:b1 - blk * self.bs] = data[b0 - off:b1 - off]
            blk += 1

    def _evict(self, keep):
        while len(self._cache) > CACHE_BLOCKS:
            victim = None
            for k in self._cache:
                if k != keep and k not in self._dirty:
                    victim = k
                    break
            if victim is None:
                break
            del self._cache[victim]

    def _cache_load(self, blk):
        b = self._cache.get(blk)
        if b is not None:
            return b
        b = bytearray(self._pread(blk * self.bs, self.bs))
        self._cache[blk] = b
        self._evict(blk)
        return b

    def _read_bytes(self, off, n, use_cache=True):
        out = bytearray()
        while n > 0:
            blk = off // self.bs
            boff = off - blk * self.bs
            take = min(n, self.bs - boff)
            if use_cache:
                b = self._cache_load(blk)
            else:
                b = self._pread(blk * self.bs, self.bs)
            out += bytes(b[boff:boff + take])
            off += take
            n -= take
        return bytes(out)

    def _write_bytes(self, off, data):
        """只改内存缓存（元数据），flush 时走 journal 落盘。"""
        data = bytes(data)
        while data:
            blk = off // self.bs
            boff = off - blk * self.bs
            take = min(len(data), self.bs - boff)
            b = self._cache_load(blk)
            b[boff:boff + take] = data[:take]
            self._dirty.add(blk)
            off += take
            data = data[take:]

    def block(self, n):
        return self._read_bytes(n * self.bs, self.bs)

    def put_block(self, n, data):
        """元数据块写入（进 journal 事务）。"""
        chunk = bytes(data).ljust(self.bs, b"\0")[:self.bs]
        self._write_bytes(n * self.bs, chunk)
        self.mark(n, 0, self.bs)

    def write_data_block(self, n, data):
        """CoW 数据块：直接落盘，不进 journal（提交前是新块，孤儿安全）。"""
        chunk = bytes(data).ljust(self.bs, b"\0")[:self.bs]
        self._disk_write(n * self.bs, chunk)
        self._cache[n] = bytearray(chunk)
        self._dirty.discard(n)
        self._evict(n)

    # ---- superblock 探测 ----
    def _probe_super(self):
        """v2 superblock 在块 1，字节偏移 = block_size：4K→4096，16K→16384。"""
        for off in BLOCK_SIZES:
            raw = self._pread(off, 512)
            if len(raw) < 112:
                continue
            cand = Super.unpack(raw)
            if (cand.magic == MAGIC and cand.version == VERSION
                    and cand.block_size == off and cand.checksum_ok(raw)):
                return cand
        raise SystemExit(
            "不是 KiFS v2 镜像：偏移 4096/16384 均未找到合法 superblock\n"
            "（v1 格式已不再支持，请用 mkifs.py format 重新生成）")

    # ---- journal / 事务 ----
    def mark(self, block, offset, length):
        """记录 after-image 增量（调用前数据已在缓存里）。"""
        if 0 <= block < self.sb.total_blocks and length > 0:
            self._txn_deltas.append(
                (block, offset, self._read_bytes(block * self.bs + offset, length)))

    def mark_span(self, byte_off, length):
        """把一段字节范围拆成若干 mark（可能跨块）。"""
        while length > 0:
            block = byte_off // self.bs
            off = byte_off % self.bs
            n = min(length, self.bs - off)
            self.mark(block, off, n)
            byte_off += n
            length -= n

    def _jrn_blocks(self):
        base = self.sb.journal_start
        return [jrn_unpack(self.block(base + i), self.bs)
                for i in range(self.sb.journal_blocks)]

    def _jrn_state(self):
        blocks = self._jrn_blocks()
        txns = {}
        for i, b in enumerate(blocks):
            if not b:
                continue
            t = txns.setdefault(b["seq"], {"begin": None, "commit": None,
                                           "end": None, "recs": []})
            if b["flags"] & JRN_BEGIN:
                t["begin"] = i
            elif b["flags"] & JRN_COMMIT:
                t["commit"] = i
            elif b["flags"] & JRN_END:
                t["end"] = i
            else:
                t["recs"].append((b["target"], b["offset"], b["payload"], i))
        return txns, blocks

    def _jrn_write_slot(self, slot, data):
        self._disk_write((self.sb.journal_start + slot) * self.bs, data)

    def recover(self):
        """重放已 COMMIT 但未 END 的完整事务（幂等）；丢弃未提交的残缺事务。"""
        txns, _blocks = self._jrn_state()
        n = 0
        for seq in sorted(txns):
            t = txns[seq]
            if t["end"] is not None:
                continue
            slots = []
            if t["begin"] is not None:
                slots.append(t["begin"])
            slots += [s for (_tg, _o, _p, s) in t["recs"]]
            if t["commit"] is not None:
                slots.append(t["commit"])
            if t["begin"] is None or t["commit"] is None:
                for s in slots:                       # 残缺事务整段作废
                    self._jrn_write_slot(s, bytes(self.bs))
                continue
            for target, off, payload, _slot in sorted(t["recs"], key=lambda r: r[3]):
                if target < 0 or target >= self.sb.total_blocks:
                    continue
                self._disk_write(target * self.bs + off, payload)
            self._jrn_write_slot(t["begin"], jrn_pack(seq, JRN_VALID | JRN_END, 0, 0,
                                                      b"", self.bs))
            for s in slots:                           # 只留一个 END 标记
                if s != t["begin"]:
                    self._jrn_write_slot(s, bytes(self.bs))
            n += 1
        if n:
            self.fh.flush()
            # 已提交事务里的 superblock 覆盖了盘上旧值：重读，别用挂载时的陈旧副本
            self.sb = Super.unpack(self._pread(self.bs, 512))
        return n

    def _collect_deltas(self):
        """合并本事务的 after-image：同位置取最后一次，相邻区间拼成一条 journal 记录。

        位图按字节 mark，一次大分配会产生成百上千个 1 B 增量；不合并会瞬间打满
        64 块的 journal（16 KiB 块写 76 MB 就是 4695 条）。合并后位图连续段
        通常只剩一条记录。
        """
        seen = {}
        for (block, off, val) in self._txn_deltas:
            seen[(block, off)] = val
        merged = []
        for (block, off), val in sorted(seen.items()):
            if (merged and merged[-1][0] == block
                    and merged[-1][1] + len(merged[-1][2]) == off
                    and len(merged[-1][2]) + len(val) <= self.bs - JRN_HDR):
                merged[-1] = (block, merged[-1][1], merged[-1][2] + val)
            else:
                merged.append((block, off, val))
        return merged

    def flush(self):
        """把本事务的元数据增量写入 journal 并应用。"""
        deltas = self._collect_deltas()
        self._txn_deltas = []
        sb_new = self.sb.pack()
        if self._read_bytes(self.bs, 112) != sb_new[:112]:
            for bk in (1, 2, self.sb.total_blocks - 1):
                deltas.append((bk, 0, sb_new))
        if deltas:
            self._jrn_commit(deltas)
        self.fh.flush()

    def sync(self):
        """flush 事务并 fsync 到设备（FUSE fsync 用）。"""
        self.flush()
        try:
            os.fsync(self.fh.fileno())
        except OSError:
            pass

    def reload(self):
        """丢弃未提交的内存事务，回到盘上已提交状态（写失败时回滚用）。"""
        self._cache.clear()
        self._dirty.clear()
        self._txn_deltas = []
        self._used_recs = None
        self.sb = Super.unpack(self._pread(self.bs, 512))

    def sync(self):
        """flush 事务并 fsync 到设备（FUSE fsync 用）。"""
        self.flush()
        try:
            os.fsync(self.fh.fileno())
        except OSError:
            pass

    def reload(self):
        """丢弃未提交的内存事务，回到盘上已提交状态（写失败时回滚用）。"""
        self._cache.clear()
        self._dirty.clear()
        self._txn_deltas = []
        self._used_recs = None
        self.sb = Super.unpack(self._pread(self.bs, 512))

    def sync(self):
        """flush 事务并 fsync 到设备（FUSE fsync 用）。"""
        self.flush()
        try:
            os.fsync(self.fh.fileno())
        except OSError:
            pass

    def reload(self):
        """丢弃未提交的内存事务，回到盘上已提交状态（写失败时回滚用）。"""
        self._cache.clear()
        self._dirty.clear()
        self._txn_deltas = []
        self._used_recs = None
        self.sb = Super.unpack(self._pread(self.bs, 512))

    def _jrn_commit(self, deltas):
        crash = os.environ.get("KIFS_CRASH", "")
        txns, blocks = self._jrn_state()
        seq = (max(txns) + 1) if txns else 1
        used = [i for i, b in enumerate(blocks) if b]
        n = self.sb.journal_blocks
        idx = (max(used) + 1) % n if used else 0
        payload_max = self.bs - JRN_HDR
        recs = []
        for (block, off, payload) in deltas:
            payload = bytes(payload)
            for i in range(0, len(payload), payload_max):
                recs.append((block, off + i, payload[i:i + payload_max]))
        if len(recs) + 3 > n:
            raise SystemExit("journal 满（%d 条记录 > %d 块）" % (len(recs) + 3, n))
        start = idx

        def w(flags, offset, target, payload=b""):
            nonlocal idx
            data = jrn_pack(seq, JRN_VALID | flags, offset, target, payload, self.bs)
            self._jrn_write_slot(idx, data)
            idx = (idx + 1) % n

        w(JRN_BEGIN, 0, 0)
        if crash == "after_begin":
            self.fh.flush()
            raise SystemExit(2)
        for (block, off, payload) in recs:
            w(0, off, block, payload)
        if crash == "after_records":
            self.fh.flush()
            raise SystemExit(2)
        if crash == "torn":
            last = (self.sb.journal_start + ((idx - 1) % n)) * self.bs
            self._disk_write(last + JRN_HDR, b"\xff")
            self.fh.flush()
            raise SystemExit(2)
        w(JRN_COMMIT, 0, 0)
        if crash == "after_commit":
            self.fh.flush()
            raise SystemExit(2)
        for (block, off, payload) in recs:
            self._disk_write(block * self.bs + off, payload)
            self._dirty.discard(block)
        if crash == "after_apply":
            self.fh.flush()
            raise SystemExit(2)
        # END 覆盖写回 BEGIN 槽
        self._jrn_write_slot(start, jrn_pack(seq, JRN_VALID | JRN_END, 0, 0, b"", self.bs))
        self.fh.flush()

    # ---- 位图 ----
    def _bitmap_off(self):
        return self.sb.data_start * self.bs

    def bit_get(self, blk):
        b = self._read_bytes(self._bitmap_off() + blk // 8, 1)[0]
        return (b >> (blk % 8)) & 1

    def bits_set_range(self, lo, n, val=1):
        """批量把位图 [lo, lo+n) 置 val：按字节合并写，比逐 bit 快几十倍。"""
        while n > 0:
            byte_idx = lo // 8
            bit0 = lo % 8
            take = min(n, 8 - bit0)
            off = self._bitmap_off() + byte_idx
            b = self._read_bytes(off, 1)[0]
            mask = ((1 << take) - 1) << bit0
            nb = (b | mask) if val else (b & ~mask & 0xFF)
            if nb != b:
                self._write_bytes(off, bytes([nb]))
                self.mark(self.sb.data_start, byte_idx, 1)
            lo += take
            n -= take

    def bit_set(self, blk, val=1):
        off = self._bitmap_off() + blk // 8
        b = self._read_bytes(off, 1)[0]
        if val:
            nb = b | (1 << (blk % 8))
        else:
            nb = b & ~(1 << (blk % 8)) & 0xFF
        if nb != b:
            self._write_bytes(off, bytes([nb]))
            self.mark(self.sb.data_start, blk // 8, 1)

    def bits_value(self, lo, n):
        """读位图 [lo, lo+n) 的 n 个 bit，返回整数（bit i 对应块 lo+i）。"""
        b0 = lo // 8
        b1 = ceil_div(lo + n, 8)
        raw = self._read_bytes(self._bitmap_off() + b0, b1 - b0, use_cache=False)
        v = int.from_bytes(raw, "little") >> (lo - b0 * 8)
        return v & ((1 << n) - 1)

    def count_bits(self, lo, hi):
        total = 0
        step = 1 << 20
        while lo < hi:
            n = min(step, hi - lo)
            total += self.bits_value(lo, n).bit_count()
            lo += n
        return total

    # ---- Record ----
    def rec_off(self, oid):
        return self.sb.record_start * self.bs + oid * RECORD_SIZE

    def read_record(self, oid):
        if oid <= 0 or oid >= self.sb.record_count:
            return None
        data = self._read_bytes(self.rec_off(oid), RECORD_SIZE)
        if len(data) < RECORD_SIZE:
            return None
        r = Record.unpack(data)
        return r if r.type else None

    def write_record(self, r):
        data = r.pack()
        self._write_bytes(self.rec_off(r.oid), data)
        self.mark_span(self.rec_off(r.oid), RECORD_SIZE)

    def rec_extents(self, rec):
        """返回记录的全部 extent（含 ext_index 表，12 B/项）。"""
        if rec.inline_len:
            return []
        if rec.ext_index and rec.n_ext_total > 3:
            raw = self._read_bytes(rec.ext_index * self.bs, rec.n_ext_total * 12)
            exts = []
            for i in range(rec.n_ext_total):
                st, cnt = EXT_STRUCT.unpack_from(raw, i * 12)
                if cnt:
                    exts.append((st, cnt))
            return exts
        return list(rec.extents)

    def ext_index_blocks(self, rec):
        """ext_index 表占用的块数（表项 12 B）。"""
        if rec.ext_index and rec.n_ext_total > 3:
            return ceil_div(rec.n_ext_total * 12, self.bs)
        return 0

    def alloc_oid(self):
        step = 4096
        oid = 1
        while oid < self.sb.record_count:
            n = min(step, self.sb.record_count - oid)
            # 走块缓存：同一事务里刚分配（尚未 flush）的 record 也要能看到
            raw = self._read_bytes(self.rec_off(oid), n * RECORD_SIZE)
            for i in range(n):
                if raw[i * RECORD_SIZE + 4] == 0:      # type == 0 → 空槽
                    return oid + i
            oid += n
        raise SystemExit("record 表满了（record_count=%d）" % self.sb.record_count)

    # ---- 数据块 ----
    def alloc_blocks(self, count):
        """从 data_next 向上找连续 count 块，失败返回 None。"""
        if count <= 0:
            return None
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
        out = bytearray()
        for (st, cnt) in self.rec_extents(rec):
            out += self._read_bytes(st * self.bs, cnt * self.bs, use_cache=False)
        return bytes(out[:rec.size])

    def write_content(self, rec, data):
        """CoW 写：数据块永远写新块（提交前是孤儿、安全），旧块在本事务内释放。"""
        n = len(data)
        old_extents = list(self.rec_extents(rec))
        if n <= INLINE_MAX:
            for (st, cnt) in old_extents:
                self.free_blocks(st, cnt)
            rec.size = n
            rec.blocks = 0
            rec.inline_len = n
            rec.inline_data = data
            rec.n_ext = 0
            rec.n_ext_total = 0
            rec.extents = []
            rec.ext_index = 0
            return
        blocks = ceil_div(n, self.bs)
        if blocks > 0xFFFFFFFF:
            raise SystemExit("文件太大：%d 块超出 u32 extent 上限" % blocks)
        # 1) 旧块仍占用时分配新块（保证不复用旧块 → CoW）
        start = self.alloc_blocks(blocks)
        if start is None:
            raise SystemExit("盘满（连续 %d 块分配不到）" % blocks)
        # 2) 新数据块直接落盘（不进 journal；提交前是孤儿）
        for i in range(blocks):
            self.write_data_block(start + i, data[i * self.bs:(i + 1) * self.bs])
        # 3) 释放旧数据块（位图变更走 journal）
        for (st, cnt) in old_extents:
            self.free_blocks(st, cnt)
        rec.size = n
        rec.blocks = blocks
        rec.inline_len = 0
        rec.inline_data = b""
        rec.extents = [(start, blocks)]
        rec.n_ext = 1
        rec.n_ext_total = 1
        rec.ext_index = 0

    def record_free_data(self, rec):
        for (st, cnt) in self.rec_extents(rec):
            self.free_blocks(st, cnt)
        if self.ext_index_blocks(rec):
            self.free_blocks(rec.ext_index, self.ext_index_blocks(rec))
        rec.extents = []
        rec.n_ext = 0
        rec.n_ext_total = 0
        rec.inline_len = 0
        rec.inline_data = b""
        rec.ext_index = 0
        rec.blocks = 0

    # ---- 路径 ----
    def resolve(self, path):
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

    def symlink(self, path, target, perm=PERM_SYMLINK_DEFAULT):
        """创建符号链接（type=3）：内容为目标路径的 UTF-8 字节，允许 dangling。"""
        if not isinstance(target, str):
            raise SystemExit("符号链接目标必须是字符串")
        try:
            data = target.encode("utf-8")
        except UnicodeEncodeError:
            raise SystemExit("符号链接目标不是合法 UTF-8 字符串: %s" % path)
        if b"\x00" in data:
            raise SystemExit("符号链接目标不能包含 NUL: %s" % path)
        if not data:
            raise SystemExit("符号链接目标不能为空: %s" % path)
        if len(data) > SYMLINK_TARGET_MAX:
            raise SystemExit("符号链接目标太长: %d 字节（上限 %d 字节）"
                             % (len(data), SYMLINK_TARGET_MAX))
        parent_oid, name = self.resolve(path)
        if not name:
            raise SystemExit("不能创建根 /")
        parent = self.read_record(parent_oid)
        if self.dir_lookup(parent, name) is not None:
            raise SystemExit("已存在: %s" % path)
        now = int(time.time())
        oid = self.alloc_oid()
        r = Record()
        r.oid, r.type, r.perm = oid, TYPE_SYMLINK, perm
        r.parent, r.ctime, r.mtime = parent_oid, now, now
        r.owner = default_owner().encode()
        self.write_record(r)
        self.write_content(r, data)
        self.write_record(r)
        self.dir_add(parent, name, oid, TYPE_SYMLINK)
        self.write_record(parent)
        return oid

    def readlink(self, path):
        """读取符号链接目标（不跟随链接；目标可以不存在）。"""
        parent_oid, name = self.resolve(path)
        parent = self.read_record(parent_oid)
        oid = self.dir_lookup(parent, name) if name else None
        if oid is None:
            raise SystemExit("找不到: %s" % path)
        rec = self.read_record(oid)
        if rec.type != TYPE_SYMLINK:
            raise SystemExit("不是符号链接: %s" % path)
        data = self.read_content(rec)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SystemExit("符号链接目标不是合法 UTF-8: %s" % path)

    def write_file(self, path, data, perm=None):
        parent_oid, name = self.resolve(path)
        if not name:
            raise SystemExit("不能写根 /")
        parent = self.read_record(parent_oid)
        oid = self.dir_lookup(parent, name)
        now = int(time.time())
        if oid is not None:
            rec = self.read_record(oid)
            if rec.type == TYPE_SYMLINK:
                raise SystemExit("是符号链接: %s" % path)
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
        if rec.type == TYPE_SYMLINK:
            raise SystemExit("是符号链接: %s" % path)
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
        rec.size = 0
        rec.inline_len = 0
        rec.inline_data = b""
        self.write_record(rec)
        self.dir_remove(parent, name)
        self.write_record(parent)

    # ------------------------------------------------------------------
    # 块级 / 区间 API（FUSE 驱动用；调用方负责 at 方法之后 flush()）
    #
    # 设计要点：
    #   * read_at 只读请求覆盖的块，不整文件进内存；
    #   * write_at 是「增量 CoW」：只给被修改的块分配新块，未改动的 extent
    #     原样保留并共享；旧块在新块写完之后才释放，提交前断电不会破坏旧数据；
    #   * 顺序追加（off == rec.size）走快速路径：只写 EOF 之后的字节（不改变
    #     任何已提交字节），并尝试把最后一个 extent 向后扩，避免产生碎片；
    #   * extent > 3 段时写 ext_index 表（新表块 CoW 分配，旧表块释放）。
    # ------------------------------------------------------------------
    def path_parts(self, path):
        return [p for p in (path or "/").replace("\\", "/").split("/") if p]

    def record_at(self, path):
        """按路径取 record；不存在返回 None，路径中间不是目录抛 ENOTDIR。"""
        oid = self.sb.root_oid
        for p in self.path_parts(path):
            rec = self.read_record(oid)
            if rec is None or rec.type != TYPE_DIR:
                raise NotADirectoryError(errno.ENOTDIR, "不是目录: %s" % p)
            nxt = self.dir_lookup(rec, p)
            if nxt is None:
                return None
            oid = nxt
        return self.read_record(oid)

    def parent_record(self, path):
        """返回 (parent_rec, name)；根路径抛 EISDIR（不能对根操作）。"""
        parts = self.path_parts(path)
        if not parts:
            raise IsADirectoryError(errno.EISDIR, "是根目录")
        oid = self.sb.root_oid
        for p in parts[:-1]:
            rec = self.read_record(oid)
            if rec is None or rec.type != TYPE_DIR:
                raise NotADirectoryError(errno.ENOTDIR, "不是目录: %s" % p)
            nxt = self.dir_lookup(rec, p)
            if nxt is None:
                raise FileNotFoundError(errno.ENOENT, "目录不存在: %s" % p)
            oid = nxt
        parent = self.read_record(oid)
        if parent is None or parent.type != TYPE_DIR:
            raise NotADirectoryError(errno.ENOTDIR, "父路径不是目录")
        return parent, parts[-1]

    def used_records(self):
        """已用 record 槽数（statfs 的 f_files 用；结果缓存，写入时失效）。"""
        if self._used_recs is not None:
            return self._used_recs
        n = 0
        step = 4096
        oid = 1
        while oid < self.sb.record_count:
            k = min(step, self.sb.record_count - oid)
            raw = self._read_bytes(self.rec_off(oid), k * RECORD_SIZE)
            for i in range(k):
                if raw[i * RECORD_SIZE + 4]:
                    n += 1
            oid += k
        self._used_recs = n
        return n

    def statfs_info(self):
        sb = self.sb
        used = self.used_records()
        return {"block_size": self.bs, "blocks": sb.total_blocks,
                "free_blocks": sb.free_blocks,
                "files": sb.record_count, "free_files": max(0, sb.record_count - used)}

    # ---- 读 ----
    def read_at(self, rec, off, n):
        """读文件 [off, off+n)，只碰覆盖到的块；返回实际读到的字节。"""
        if off < 0 or n < 0:
            raise OSError(errno.EINVAL, "偏移/长度为负")
        if n == 0 or off >= rec.size:
            return b""
        n = min(n, rec.size - off)
        if rec.inline_len:
            return bytes(rec.inline_data[off:off + n])
        out = bytearray(n)
        pos = 0
        for (st, cnt) in self.rec_extents(rec):
            e0 = pos
            e1 = pos + cnt * self.bs
            pos = e1
            a = max(off, e0)
            b = min(off + n, e1)
            if a < b:
                out[a - off:b - off] = self._read_bytes(
                    st * self.bs + (a - e0), b - a, use_cache=False)
        return bytes(out)

    def read_file_at(self, path, off, n):
        rec = self.record_at(path)
        if rec is None:
            raise FileNotFoundError(errno.ENOENT, "找不到: %s" % path)
        if rec.type != TYPE_FILE:
            raise IsADirectoryError(errno.EISDIR, "是目录: %s" % path)
        return self.read_at(rec, off, n)

    # ---- 写（增量 CoW） ----
    def _split_extents(self, exts, lo, hi):
        """把逻辑块区间 [lo,hi) 从 extent 列表里切出来。

        返回 (head, tail, free_ranges)：head/tail 是保留的前后片段（含物理块号），
        free_ranges 是旧的、将被替换的物理块区间。
        """
        head, tail, free_ranges = [], [], []
        pos = 0
        for (st, cnt) in exts:
            e0, e1 = pos, pos + cnt
            pos = e1
            if e0 < lo:
                take = min(e1, lo) - e0
                if take > 0:
                    head.append((st, take))
            if e1 > hi:
                cut = max(e0, hi) - e0
                tail.append((st + cut, e1 - max(e0, hi)))
            f0, f1 = max(e0, lo), min(e1, hi)
            if f0 < f1:
                free_ranges.append((st + (f0 - e0), f1 - f0))
        return head, tail, free_ranges

    def _read_old_bytes(self, exts, off, n):
        """按逻辑偏移从 extent 列表读 n 字节（用于读-改-写被覆盖的旧块）。"""
        out = bytearray(n)
        pos = 0
        for (st, cnt) in exts:
            e0, e1 = pos, pos + cnt * self.bs
            pos = e1
            a = max(off, e0)
            b = min(off + n, e1)
            if a < b:
                out[a - off:b - off] = self._read_bytes(
                    st * self.bs + (a - e0), b - a, use_cache=False)
        return bytes(out)

    def _set_inline(self, rec, data):
        rec.size = len(data)
        rec.blocks = 0
        rec.inline_len = len(data)
        rec.inline_off = INLINE_OFF
        rec.inline_data = bytes(data)
        rec.n_ext = 0
        rec.n_ext_total = 0
        rec.extents = []
        rec.ext_index = 0

    def _set_extents(self, rec, exts, size):
        """合并相邻 extent 并写回 record；>3 段时同时写 ext_index 表（CoW）。"""
        merged = []
        for (st, cnt) in exts:
            if cnt <= 0:
                continue
            if merged and merged[-1][0] + merged[-1][1] == st:
                merged[-1] = (merged[-1][0], merged[-1][1] + cnt)
            else:
                merged.append((st, cnt))
        old_idx = rec.ext_index
        old_idx_n = self.ext_index_blocks(rec)
        rec.size = size
        rec.blocks = sum(c for (_s, c) in merged)
        rec.inline_len = 0
        rec.inline_data = b""
        if len(merged) > 3:
            nblk = ceil_div(len(merged) * 12, self.bs)
            start = self.alloc_blocks(nblk)
            if start is None:
                raise OSError(errno.ENOSPC, "盘满（ext_index 表 %d 块）" % nblk)
            blob = b"".join(EXT_STRUCT.pack(st, cnt) for (st, cnt) in merged)
            for i in range(nblk):
                self.write_data_block(start + i, blob[i * self.bs:(i + 1) * self.bs])
            if old_idx and old_idx_n:
                self.free_blocks(old_idx, old_idx_n)
            rec.ext_index = start
            rec.extents = merged[:3]
            rec.n_ext = 3
            rec.n_ext_total = len(merged)
        else:
            if old_idx and old_idx_n:
                self.free_blocks(old_idx, old_idx_n)
            rec.ext_index = 0
            rec.extents = merged
            rec.n_ext = len(merged)
            rec.n_ext_total = len(merged)

    def _try_append(self, rec, data):
        """顺序追加快速路径：只写 EOF 之后的字节 + 尝试原地扩最后一个 extent。

        返回 True 表示已处理；False 表示条件不满足，调用方走通用 CoW 路径。
        安全性：写入的字节全部 >= 已提交的 size，崩溃时旧内容（含 size）不变。
        """
        old_size = rec.size
        if old_size <= INLINE_MAX or rec.inline_len:
            return False
        exts = [(st, cnt) for (st, cnt) in self.rec_extents(rec)]
        if not exts:
            return False
        st, cnt = exts[-1]
        if old_size % self.bs:
            room = self.bs - (old_size % self.bs)
            k = min(room, len(data))
            self._disk_write((st + cnt - 1) * self.bs + (old_size % self.bs), data[:k])
            old_size += k
            data = data[k:]
            rec.size = old_size
        if not data:
            self._set_extents(rec, exts, old_size)
            return True
        n_new = ceil_div(len(data), self.bs)
        base = st + cnt
        if base != self.sb.data_next:
            return False                      # 尾部不连续 → 走 CoW 路径（避免碎片）
        if base + n_new > self.sb.meta_next:
            return False
        if self.bits_value(base, n_new):
            return False                      # 有块已占用
        # 数据块先落盘（提交前是孤儿，安全）——整段一次写，别按 16K 一哆嗦
        self._disk_write(base * self.bs, data)
        for b in range(base, base + n_new):
            self._cache.pop(b, None)
            self._dirty.discard(b)
        # 再改位图/record（走 journal）——位图按字节批量置位
        self.bits_set_range(base, n_new, 1)
        self.sb.data_next = base + n_new
        self.sb.free_blocks -= n_new
        exts[-1] = (st, cnt + n_new)
        self._set_extents(rec, exts, old_size + len(data))
        return True

    def write_at(self, rec, off, data):
        """把 data 写到文件 [off, off+len)（写洞自动补零），返回新 size 相关字段。"""
        if off < 0:
            raise OSError(errno.EINVAL, "偏移为负")
        data = bytes(data)
        if not data:
            return
        end = off + len(data)
        old_size = rec.size
        new_size = max(old_size, end)
        if off == old_size and self._try_append(rec, data):
            return
        if new_size <= INLINE_MAX:
            buf = bytearray(rec.inline_data if rec.inline_len else b"")
            if len(buf) < new_size:
                buf += bytes(new_size - len(buf))
            buf[off:end] = data
            if not rec.inline_len:
                for (st, cnt) in self.rec_extents(rec):
                    self.free_blocks(st, cnt)
                rec.blocks = 0
            self._set_inline(rec, bytes(buf[:new_size]))
            return
        old_inline = bytes(rec.inline_data) if rec.inline_len else b""
        exts = [] if rec.inline_len else [(st, cnt) for (st, cnt) in self.rec_extents(rec)]
        old_blocks = ceil_div(old_size, self.bs) if old_size else 0
        cover_lo = min(off, old_size) // self.bs
        cover_hi = max(old_blocks, ceil_div(end, self.bs))
        n_mid = cover_hi - cover_lo
        start = self.alloc_blocks(n_mid)
        if start is None:
            raise OSError(errno.ENOSPC, "盘满（连续 %d 块分配不到）" % n_mid)
        for i in range(n_mid):
            base = (cover_lo + i) * self.bs
            chunk = bytearray(self.bs)
            if base < old_size:
                seglen = min(self.bs, old_size - base)
                if rec.inline_len:
                    chunk[:seglen] = old_inline[base:base + seglen]
                else:
                    chunk[:seglen] = self._read_old_bytes(exts, base, seglen)
            a = max(base, off)
            z = min(base + self.bs, end)
            if a < z:
                chunk[a - base:z - base] = data[a - off:z - off]
            self.write_data_block(start + i, bytes(chunk))
        if rec.inline_len or not exts:
            head, tail, free_ranges = [], [], []
        else:
            head, tail, free_ranges = self._split_extents(exts, cover_lo, cover_hi)
        # 先改 record（>3 段时会分配新 ext_index 表），再释放旧块：
        # 若先释放，新表块可能复用刚释放的旧数据块，提交前断电会破坏旧文件
        self._set_extents(rec, head + [(start, n_mid)] + tail, new_size)
        for (fst, fcnt) in free_ranges:
            self.free_blocks(fst, fcnt)

    def truncate_at(self, rec, size):
        if size < 0:
            raise OSError(errno.EINVAL, "大小为负")
        old = rec.size
        if size == old:
            return
        if size < old:
            if size <= INLINE_MAX:
                data = self.read_at(rec, 0, size)
                for (st, cnt) in self.rec_extents(rec):
                    self.free_blocks(st, cnt)
                self._set_inline(rec, data)
                return
            exts = self.rec_extents(rec)
            keep_blocks = ceil_div(size, self.bs)
            kept, freed, pos = [], [], 0
            for (st, cnt) in exts:
                if pos >= keep_blocks:
                    freed.append((st, cnt))
                elif pos + cnt <= keep_blocks:
                    kept.append((st, cnt))
                else:
                    k = keep_blocks - pos
                    kept.append((st, k))
                    freed.append((st + k, cnt - k))
                pos += cnt
            self._set_extents(rec, kept, size)      # 先建新索引表，再释放旧块
            for (fst, fcnt) in freed:
                self.free_blocks(fst, fcnt)
            return
        # 向上扩展：KiFS 没有空洞（fsck 要求 size <= blocks*block_size），补零分配
        chunk = 1 << 20
        while rec.size < size:
            n = min(chunk, size - rec.size)
            self.write_at(rec, rec.size, b"\0" * n)

    # ---- 命名空间操作（FUSE 用；抛 OSError 子类，errno 语义与 POSIX 对齐） ----
    def _new_record(self, typ, parent_oid, perm):
        oid = self.alloc_oid()
        r = Record()
        r.oid, r.type = oid, typ
        r.parent = parent_oid
        r.perm = perm
        r.ctime = r.mtime = int(time.time())
        r.owner = default_owner().encode()
        self.write_record(r)
        self._used_recs = None
        return r

    def create_file(self, path, perm=PERM_FILE_DEFAULT):
        parent, name = self.parent_record(path)
        if not name:
            raise IsADirectoryError(errno.EISDIR, "不能写根 /")
        if self.dir_lookup(parent, name) is not None:
            raise FileExistsError(errno.EEXIST, "已存在: %s" % path)
        r = self._new_record(TYPE_FILE, parent.oid, perm)
        self.dir_add(parent, name, r.oid, TYPE_FILE)
        self.write_record(parent)
        return r

    def mkdir_at(self, path, perm=PERM_DIR_DEFAULT):
        parent, name = self.parent_record(path)
        if not name:
            raise FileExistsError(errno.EEXIST, "根目录已存在")
        if self.dir_lookup(parent, name) is not None:
            raise FileExistsError(errno.EEXIST, "已存在: %s" % path)
        r = self._new_record(TYPE_DIR, parent.oid, perm)
        self.dir_add(parent, name, r.oid, TYPE_DIR)
        self.write_record(parent)
        return r

    def _clear_record(self, rec):
        self.record_free_data(rec)
        rec.type = 0
        rec.size = 0
        rec.blocks = 0
        rec.inline_len = 0
        rec.inline_data = b""
        rec.extents = []
        rec.ext_index = 0
        rec.n_ext = rec.n_ext_total = 0
        self.write_record(rec)
        self._used_recs = None

    def unlink_at(self, path):
        parent, name = self.parent_record(path)
        oid = self.dir_lookup(parent, name)
        if oid is None:
            raise FileNotFoundError(errno.ENOENT, "找不到: %s" % path)
        rec = self.read_record(oid)
        if rec is None:
            raise FileNotFoundError(errno.ENOENT, "找不到: %s" % path)
        if rec.type == TYPE_DIR:
            raise IsADirectoryError(errno.EISDIR, "是目录: %s" % path)
        self._clear_record(rec)
        self.dir_remove(parent, name)
        self.write_record(parent)

    def rmdir_at(self, path):
        parent, name = self.parent_record(path)
        oid = self.dir_lookup(parent, name)
        if oid is None:
            raise FileNotFoundError(errno.ENOENT, "找不到: %s" % path)
        rec = self.read_record(oid)
        if rec is None or rec.type != TYPE_DIR:
            raise NotADirectoryError(errno.ENOTDIR, "不是目录: %s" % path)
        if self.dir_entries(rec):
            raise OSError(errno.ENOTEMPTY, "目录非空: %s" % path)
        self._clear_record(rec)
        self.dir_remove(parent, name)
        self.write_record(parent)

    def _is_ancestor(self, anc_oid, oid):
        """oid 是否等于 anc_oid 或位于其子树内（沿 parent 上溯，带环保护）。"""
        seen = set()
        while oid and oid not in seen:
            if oid == anc_oid:
                return True
            seen.add(oid)
            rec = self.read_record(oid)
            if rec is None:
                return False
            oid = rec.parent
        return False

    def rename_at(self, old, new):
        if old == new:
            return
        op, on = self.parent_record(old)
        oid = self.dir_lookup(op, on)
        if oid is None:
            raise FileNotFoundError(errno.ENOENT, "找不到: %s" % old)
        orec = self.read_record(oid)
        np, nn = self.parent_record(new)
        if not nn:
            raise OSError(errno.EBUSY, "不能重命名到根 /")
        if orec.type == TYPE_DIR and self._is_ancestor(oid, np.oid):
            raise OSError(errno.EINVAL, "不能把目录移动到自己的子树: %s" % new)
        existing = self.dir_lookup(np, nn)
        if existing == oid:
            return                                  # 源和目标指向同一对象
        if existing is not None:
            target = self.read_record(existing)
            if target is None:
                raise FileNotFoundError(errno.ENOENT, "目标不存在: %s" % new)
            if orec.type == TYPE_DIR and target.type != TYPE_DIR:
                raise NotADirectoryError(errno.ENOTDIR, "目标不是目录: %s" % new)
            if orec.type != TYPE_DIR and target.type == TYPE_DIR:
                raise IsADirectoryError(errno.EISDIR, "目标是目录: %s" % new)
            if target.type == TYPE_DIR and self.dir_entries(target):
                raise OSError(errno.ENOTEMPTY, "目标目录非空: %s" % new)
            self._clear_record(target)
            if op.oid != np.oid:
                self.dir_remove(np, nn)
        if op.oid == np.oid:
            entries = [(n, o, t) for (n, o, t, _e) in self.dir_entries(op)
                       if n not in (on, nn)]
            entries.append((nn, oid, orec.type))
            self.dir_set_entries(op, entries)
            self.write_record(op)
        else:
            self.dir_remove(op, on)
            self.write_record(op)
            self.dir_add(np, nn, oid, orec.type)
            self.write_record(np)
        orec.parent = np.oid
        orec.ctime = int(time.time())
        self.write_record(orec)

    def chmod_at(self, path, mode):
        rec = self.record_at(path)
        if rec is None:
            raise FileNotFoundError(errno.ENOENT, "找不到: %s" % path)
        rec.perm = (rec.perm & ~0o777) | (mode & 0o777)
        self.write_record(rec)

    def utimens_at(self, path, mtime=None, ctime=None):
        rec = self.record_at(path)
        if rec is None:
            raise FileNotFoundError(errno.ENOENT, "找不到: %s" % path)
        if mtime is not None:
            rec.mtime = int(mtime)
        if ctime is not None:
            rec.ctime = int(ctime)
        self.write_record(rec)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_format(args):
    bs = args.block_size
    if bs not in BLOCK_SIZES:
        raise SystemExit("block_size 只支持 %s" % (BLOCK_SIZES,))
    size = args.size_mib * 1024 * 1024
    if size <= 0:
        raise SystemExit("size 必须为正")
    if size % bs:
        raise SystemExit("size 必须是 block_size 的整数倍")
    total = size // bs
    if total > MAX_TOTAL_BLOCKS:
        raise SystemExit(
            "总块数 %d 超出 u32 块号范围（最大 %d 块 = 64 TiB - %d B）"
            % (total, MAX_TOTAL_BLOCKS, bs))
    if total < 64:
        raise SystemExit("镜像太小（至少 64 块）")

    sb = Super()
    sb.block_size = bs
    sb.total_blocks = total
    sb.journal_blocks = JOURNAL_BLOCKS_DEFAULT
    sb.journal_start = total - 1 - sb.journal_blocks     # 最后一块留给 superblock 备份 2
    sb.data_start = 3
    bitmap_blocks = bitmap_blocks_for(total, bs)
    sb.record_start = sb.data_start + bitmap_blocks
    if args.record_cap is not None:
        if not (2 <= args.record_cap <= RECORD_MAX):
            raise SystemExit("--record-cap 必须在 [2, %d]" % RECORD_MAX)
        sb.record_count = args.record_cap
    else:
        sb.record_count = record_count_for(total)
    record_blocks = record_blocks_for(sb.record_count, bs)
    sb.data_next = sb.record_start + record_blocks
    sb.meta_next = sb.journal_start
    if sb.data_next > sb.meta_next:
        raise SystemExit("镜像太小：元数据区（bitmap+record）放不下")
    sb.label = args.label.encode()[:LABEL_LEN - 1]
    sb.free_blocks = sb.meta_next - sb.data_next

    root = Record()
    root.oid, root.type, root.perm = 1, TYPE_DIR, PERM_DIR_DEFAULT
    root.parent = 0
    root.ctime = root.mtime = int(time.time())
    root.owner = default_owner().encode()
    root_packed = root.pack()

    # 卷在设备内的字节偏移（--offset-lba / --offset）：块设备/带前缀的镜像用
    vol_off = volume_offset(args)
    exists = os.path.exists(args.img)
    is_blk = exists and stat.S_ISBLK(os.stat(args.img).st_mode)
    if is_blk or vol_off > 0:
        if not exists:
            raise SystemExit("带 offset 的 format 要求目标已存在（先 truncate 出空间）: %s" % args.img)
        dev_size = device_size(args.img)
        if vol_off + size > dev_size:
            raise SystemExit("卷 [%d, %d) 超出设备大小 %d"
                             % (vol_off, vol_off + size, dev_size))
    mode = "r+b" if (vol_off > 0 or is_blk) else "wb"
    written = [0]
    with open(args.img, mode) as fh:
        if vol_off == 0 and not is_blk:
            fh.truncate(size)                 # 数据区留稀疏空洞（块设备/带前缀卷不动）
        zero = bytes(1 << 20)

        def w(off, data):
            fh.seek(vol_off + off)
            fh.write(data)
            written[0] += len(data)

        # 1) 位图：固定区 [0, data_next) + 元数据/日志区 [meta_next, total) 已用
        for i in range(bitmap_blocks):
            base = i * bs * 8
            buf = bytearray(bs)
            set_bit_range(buf, base, 0, sb.data_next)
            set_bit_range(buf, base, sb.meta_next, total)
            w((sb.data_start + i) * bs, buf)
        # 2) record 表：清零 + 根目录 record
        left = record_blocks * bs
        off = sb.record_start * bs
        while left:
            n = min(len(zero), left)
            w(off, zero[:n])
            off += n
            left -= n
        w(sb.record_start * bs + RECORD_SIZE, root_packed)
        # 3) journal 区清零（块头，全部无效 → 空日志）
        left = sb.journal_blocks * bs
        off = sb.journal_start * bs
        while left:
            n = min(len(zero), left)
            w(off, zero[:n])
            off += n
            left -= n
        # 4) superblock 三副本
        packed = sb.pack()
        for bk in (1, 2, total - 1):
            w(bk * bs, packed)

    cap_gib = size / float(1 << 30)
    print("✅ 格式化完成: %s" % args.img)
    print("   v2 / 块大小 %d B / 总块 %d / 容量 %.2f GiB" % (bs, total, cap_gib))
    print("   布局: bitmap@%d(+%d) record@%d(+%d 槽 / %d 块) data=%d..%d journal@%d(+%d)" %
          (sb.data_start, bitmap_blocks, sb.record_start, sb.record_count,
           record_blocks, sb.data_next, sb.meta_next - 1,
           sb.journal_start, sb.journal_blocks))
    print("   UUID %s / 卷标 %s" % (uuid.UUID(bytes=sb.volume_uuid),
                                    sb.label.decode(errors="replace")))
    print("   实际写入 %.2f MiB（仅元数据区；数据区为稀疏空洞）" % (written[0] / 1048576.0))
    return 0


def volume_offset(args):
    """--offset-lba / --offset（卷在设备内的起始字节）。"""
    if getattr(args, "offset_lba", None) is not None:
        return args.offset_lba * 512
    if getattr(args, "offset", None) is not None:
        return args.offset
    return 0


def open_img(args):
    return Image(args.img, offset=volume_offset(args))


def _find_fuse_python():
    """返回能 `from fuse import FUSE` 的 python；优先仓库 .venv，其次当前解释器。

    系统里可能装着 Debian 的老 fusepy（只有 Fuse 没有 FUSE），必须测真导入。
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    venv_py = os.path.join(os.path.dirname(here), ".venv", "bin", "python")
    if os.path.exists(venv_py):
        cands.append(venv_py)
    cands.append(sys.executable)
    for py in cands:
        try:
            rc = subprocess.call(
                [py, "-c", "from fuse import FUSE, FuseOSError, Operations"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            rc = 1
        if rc == 0:
            return py
    return None


def cmd_mount(args):
    here = os.path.dirname(os.path.abspath(__file__))
    drv = os.path.abspath(os.path.join(here, "..", "linux", "fuse_kifs.py"))
    if not os.path.exists(drv):
        raise SystemExit("找不到 FUSE 驱动: %s" % drv)
    py = _find_fuse_python()
    if py is None:
        raise SystemExit(
            "缺少 fusepy。请先：\n"
            "  python3 -m venv %s/.venv\n"
            "  %s/.venv/bin/pip install -r %s/linux/requirements.txt\n"
            "然后重试（或在 .venv 里运行本脚本）。"
            % (os.path.dirname(here), os.path.dirname(here), os.path.dirname(here)))
    argv = [py, drv, args.img, args.mountpoint]
    if getattr(args, "offset_lba", None) is not None:
        argv += ["--offset-lba", str(args.offset_lba)]
    if getattr(args, "offset", None) is not None:
        argv += ["--offset", str(args.offset)]
    for flag in ("read_only", "show_hidden", "foreground", "debug"):
        if getattr(args, flag, False):
            argv.append("--" + flag.replace("_", "-"))
    if getattr(args, "log", None):
        argv += ["--log", args.log]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(py, argv)


def cmd_mkdir(args):
    img = open_img(args)
    img.mkdir(args.path, perm=parse_perm(args.perm) if args.perm else PERM_DIR_DEFAULT)
    img.flush()
    print("✅ mkdir %s" % args.path)
    return 0


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
    return 0


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
        sys.stdout.buffer.flush()
    return 0


def cmd_symlink(args):
    img = open_img(args)
    perm = parse_perm(args.perm) if args.perm else PERM_SYMLINK_DEFAULT
    img.symlink(args.path, args.target, perm=perm)
    img.flush()
    print("✅ symlink %s -> %s" % (args.path, args.target))
    return 0


def cmd_readlink(args):
    img = open_img(args)
    print(img.readlink(args.path))
    return 0


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
        kind = {TYPE_DIR: "d", TYPE_FILE: "-", TYPE_SYMLINK: "l"}.get(rec.type, "?")
        show = n if not (rec.perm & PERM_HIDE) else "." + n
        size = "dir" if rec.type == TYPE_DIR else str(rec.size)
        line = "%s %s %-8s %s" % (kind, perm_str(rec.perm) + rec.owner.decode("utf-8", "replace"),
                                  size, show)
        if rec.type == TYPE_SYMLINK:
            line += " -> %s" % img.read_content(rec).decode("utf-8", "replace")
        print(line)
    return 0


def cmd_stat(args):
    img = open_img(args)
    parent_oid, name = img.resolve(args.path)
    parent = img.read_record(parent_oid)
    oid = img.dir_lookup(parent, name) if name else 1
    if oid is None:
        raise SystemExit("找不到: %s" % args.path)
    rec = img.read_record(oid)
    print("oid        : %d" % rec.oid)
    typ_name = {TYPE_DIR: "dir", TYPE_FILE: "file", TYPE_SYMLINK: "symlink"}.get(rec.type, "unknown")
    print("type       : %s" % typ_name)
    if rec.type == TYPE_SYMLINK:
        print("target     : %s" % img.read_content(rec).decode("utf-8", "replace"))
    print("perm       : %s%s" % (perm_str(rec.perm), rec.owner.decode("utf-8", "replace")))
    print("size       : %d 字节" % rec.size)
    print("blocks     : %d" % rec.blocks)
    print("inline_len : %d" % rec.inline_len)
    print("extents    : %s" % (img.rec_extents(rec),))
    print("ext_index  : %d (n_ext_total=%d)" % (rec.ext_index, rec.n_ext_total))
    print("parent     : %d" % rec.parent)
    print("ctime/mtime: %d / %d" % (rec.ctime, rec.mtime))
    return 0


def cmd_rm(args):
    img = open_img(args)
    img.rm(args.path)
    img.flush()
    print("✅ rm %s" % args.path)
    return 0


def cmd_info(args):
    img = open_img(args)
    sb = img.sb
    bs = sb.block_size
    bitmap_blocks = bitmap_blocks_for(sb.total_blocks, bs)
    rec_blocks = record_blocks_for(sb.record_count, bs)
    print("magic      : %s" % sb.magic.decode(errors="replace"))
    print("version    : 0x%08X (v2)" % sb.version)
    print("block_size : %d" % bs)
    print("total_blocks: %d (%.3f GiB)" %
          (sb.total_blocks, sb.total_blocks * bs / float(1 << 30)))
    print("layout     : bitmap@%d(+%d) record@%d(%d 槽/%d 块) data_next=%d meta_next=%d journal=%d+%d"
          % (sb.data_start, bitmap_blocks, sb.record_start, sb.record_count,
             rec_blocks, sb.data_next, sb.meta_next, sb.journal_start, sb.journal_blocks))
    print("root_oid   : %d / free_blocks %d" % (sb.root_oid, sb.free_blocks))
    print("uuid       : %s" % uuid.UUID(bytes=sb.volume_uuid))
    print("label      : %s flags=%d checksum_ok=%s" %
          (sb.label.decode(errors="replace"), sb.flags, sb.checksum_ok(img.block(1))))
    return 0


def cmd_fsck(args):
    img = open_img(args)
    sb = img.sb
    bs = sb.block_size
    problems = []
    used = {}
    owner = {}
    claimed = [0]           # extent 占用的数据块数（含 ext_index 表）
    nsym = 0                # 符号链接数（type=3）

    def bad(msg):
        problems.append(msg)

    def take(blk, what, need=1):
        for b in range(blk, blk + need):
            if b in used:
                bad("块 %d 被重复占用: %s 和 %s" % (b, used[b], what))
            used[b] = what
        claimed[0] += need

    # ---- superblock ----
    if sb.magic != MAGIC:
        bad("superblock magic 不对: %r" % sb.magic)
    if sb.version != VERSION:
        bad("版本不是 v2: 0x%08X（期望 0x%08X）" % (sb.version, VERSION))
    if bs not in BLOCK_SIZES:
        bad("block_size 非法: %d（只支持 4096/16384）" % bs)
        return _fsck_report(problems, 0, 0, 0)
    if not sb.checksum_ok(img.block(1)):
        bad("superblock 校验和不对")
    for bk in (2, sb.total_blocks - 1):
        if img.block(bk)[:112] != img.block(1)[:112]:
            bad("superblock 备份 @块 %d 与主副本不一致" % bk)
    try:
        avail = img.dev_size - img.offset
        is_blk = stat.S_ISBLK(os.stat(img.path).st_mode)
        if img.offset == 0 and not is_blk:
            if avail != sb.total_blocks * bs:
                bad("镜像文件大小 %d 与 total_blocks*block_size=%d 不符" %
                    (avail, sb.total_blocks * bs))
        elif avail < sb.total_blocks * bs:
            bad("设备可用 %d 字节 < total_blocks*block_size=%d" %
                (avail, sb.total_blocks * bs))
    except OSError:
        pass

    # ---- 布局 & 区间不重叠 ----
    bitmap_blocks = bitmap_blocks_for(sb.total_blocks, bs)
    rec_blocks = record_blocks_for(sb.record_count, bs)
    fixed_end = sb.record_start + rec_blocks          # 固定元数据区末尾（数据区起点）
    if sb.data_start != 3:
        bad("data_start 应为 3，实际 %d" % sb.data_start)
    if sb.record_start != sb.data_start + bitmap_blocks:
        bad("record_start=%d 与 bitmap 末尾 %d 不符" %
            (sb.record_start, sb.data_start + bitmap_blocks))
    if not (1 <= sb.record_count <= RECORD_MAX):
        bad("record_count=%d 超出 [1, %d]" % (sb.record_count, RECORD_MAX))
    if sb.data_next < fixed_end:
        bad("data_next=%d < 固定元数据区末尾 %d" % (sb.data_next, fixed_end))
    if sb.data_next > sb.meta_next:
        bad("data_next=%d > meta_next=%d" % (sb.data_next, sb.meta_next))
    if sb.meta_next > sb.journal_start:
        bad("meta_next=%d > journal_start=%d" % (sb.meta_next, sb.journal_start))
    if sb.journal_start < fixed_end:
        bad("journal 区与 record 表重叠（journal_start=%d < %d）" % (sb.journal_start, fixed_end))
    if sb.journal_start + sb.journal_blocks > sb.total_blocks - 1:
        bad("journal 区越界（%d+%d > %d）" %
            (sb.journal_start, sb.journal_blocks, sb.total_blocks - 1))
    if sb.journal_blocks != JOURNAL_BLOCKS_DEFAULT:
        bad("journal_blocks=%d 不是 %d" % (sb.journal_blocks, JOURNAL_BLOCKS_DEFAULT))

    # ---- journal：open 时已 recover，只应剩「未提交的残缺事务」 ----
    txns, _jblocks = img._jrn_state()
    pending = [s for s, t in txns.items() if t["commit"] is not None and t["end"] is None]
    if pending:
        bad("journal 有 %d 个已提交未重放事务: %s" % (len(pending), pending))

    # ---- record 表（分块流式扫描，1M 槽也不用逐条 seek） ----
    step = 4096
    oid = 1
    while oid < sb.record_count:
        n = min(step, sb.record_count - oid)
        raw = img._read_bytes(img.rec_off(oid), n * RECORD_SIZE, use_cache=False)
        for i in range(n):
            base = i * RECORD_SIZE
            if raw[base + 4] == 0:                    # type == 0 → 空槽
                continue
            rec = Record.unpack(raw[base:base + RECORD_SIZE])
            o = oid + i
            owner[o] = rec
            if rec.type not in (TYPE_FILE, TYPE_DIR, TYPE_SYMLINK):
                bad("oid %d 未知类型 %d" % (o, rec.type))
                continue
            if rec.oid != o:
                bad("oid %d 槽里的 oid=%d 不一致" % (o, rec.oid))
            if rec.inline_len:
                if rec.inline_len > INLINE_MAX:
                    bad("oid %d inline_len=%d > %d" % (o, rec.inline_len, INLINE_MAX))
                if rec.inline_off < INLINE_OFF or rec.inline_off + rec.inline_len > RECORD_SIZE:
                    bad("oid %d inline 区间 [%d,%d) 越界" %
                        (o, rec.inline_off, rec.inline_off + rec.inline_len))
                if rec.size != rec.inline_len:
                    bad("oid %d inline size=%d 与 inline_len=%d 不一致" %
                        (o, rec.size, rec.inline_len))
                if rec.blocks:
                    bad("oid %d inline 但 blocks=%d" % (o, rec.blocks))
            else:
                exts = img.rec_extents(rec)
                if not exts and rec.size:
                    bad("oid %d size=%d 但没有 extent" % (o, rec.size))
                total_cnt = sum(c for (_s, c) in exts)
                if total_cnt != rec.blocks:
                    bad("oid %d blocks=%d 与 extent 合计=%d 不一致" %
                        (o, rec.blocks, total_cnt))
                if rec.size > rec.blocks * bs:
                    bad("oid %d size=%d 超出 blocks=%d × %d 容量" %
                        (o, rec.size, rec.blocks, bs))
                if rec.n_ext_total != len(exts):
                    bad("oid %d n_ext_total=%d 与实际 extent 数=%d 不一致" %
                        (o, rec.n_ext_total, len(exts)))
                if rec.n_ext != min(rec.n_ext_total, 3):
                    bad("oid %d n_ext=%d 与 n_ext_total=%d 不一致" %
                        (o, rec.n_ext, rec.n_ext_total))
                if rec.n_ext_total > 3 and not rec.ext_index:
                    bad("oid %d n_ext_total=%d > 3 但 ext_index=0" % (o, rec.n_ext_total))
                for (st, cnt) in exts:
                    if cnt == 0:
                        bad("oid %d 有 count=0 的 extent" % o)
                    elif st < fixed_end or st + cnt > sb.meta_next:
                        bad("oid %d extent %d+%d 越界（数据区 %d..%d）" %
                            (o, st, cnt, fixed_end, sb.meta_next))
                    else:
                        take(st, "oid %d" % o, cnt)
                nidx = img.ext_index_blocks(rec)
                if nidx:
                    if rec.ext_index < fixed_end or rec.ext_index + nidx > sb.meta_next:
                        bad("oid %d ext_index=%d+%d 越界" % (o, rec.ext_index, nidx))
                    else:
                        take(rec.ext_index, "oid %d ext_index" % o, nidx)
            if rec.type == TYPE_SYMLINK:
                nsym += 1
                if rec.size > SYMLINK_TARGET_MAX:
                    bad("oid %d 符号链接目标 %d 字节 > 上限 %d" %
                        (o, rec.size, SYMLINK_TARGET_MAX))
                try:
                    data = img.read_content(rec)
                except Exception as e:
                    bad("oid %d 符号链接目标读取失败: %s" % (o, e))
                    data = b""
                if b"\x00" in data:
                    bad("oid %d 符号链接目标含 NUL" % o)
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    bad("oid %d 符号链接目标不是合法 UTF-8" % o)
            if rec.parent:
                p = img.read_record(rec.parent)
                if p is None or p.type != TYPE_DIR:
                    bad("oid %d parent=%d 不存在或不是目录" % (o, rec.parent))
        oid += n

    # ---- 目录项 ----
    seen = set([1])
    visited = set()
    stack = [sb.root_oid]
    if sb.root_oid != 1:
        bad("root_oid=%d 不是 1" % sb.root_oid)
    while stack:
        d = stack.pop()
        if d in visited:
            bad("目录 oid %d 被重复遍历（疑似环/重复引用）" % d)
            continue
        visited.add(d)
        drec = img.read_record(d)
        if drec is None or drec.type != TYPE_DIR:
            bad("目录 oid %d 不存在/不是目录" % d)
            continue
        data = img.read_content(drec)
        if len(data) != drec.size:
            bad("目录 oid %d 内容长度 %d != size %d" % (d, len(data), drec.size))
        pos = 0
        last_name = None
        while pos + DIRENT_MIN <= len(data):
            o, typ, nlen, elen = DIRENT_HEAD.unpack_from(data, pos)
            if elen == 0:
                break
            name = data[pos + DIRENT_MIN:pos + DIRENT_MIN + nlen].decode("utf-8", "replace")
            if elen < align4(DIRENT_MIN + nlen):
                bad("目录 oid %d 项 %s elen=%d 太小" % (d, name, elen))
            if pos + elen > len(data):
                bad("目录 oid %d 项 %s 越界" % (d, name))
                break
            if last_name is not None and name.encode() < last_name.encode():
                bad("目录 oid %d 项 %s 未按名字排序" % (d, name))
            last_name = name
            child = img.read_record(o)
            if child is None:
                bad("目录 oid %d 项 %s 指向不存在的 record %d" % (d, name, o))
            else:
                if child.parent != d:
                    bad("oid %d 的 parent=%d 与目录 oid %d 不符" % (o, child.parent, d))
                if child.type != typ:
                    bad("目录项 %s 的 type 与 record 不符" % name)
                seen.add(o)
                if child.type == TYPE_DIR:
                    stack.append(o)
            pos += elen

    # ---- 位图一致性（按位区间计数，15 TiB 也是秒级） ----
    if fixed_end <= sb.data_next <= sb.meta_next <= sb.total_blocks:
        if img.count_bits(0, fixed_end) != fixed_end:
            bad("位图: 固定元数据区 [0,%d) 存在未标记的块" % fixed_end)
        beyond = bitmap_blocks * bs * 8 - sb.total_blocks
        if beyond and img.bits_value(sb.total_blocks, beyond):
            bad("位图: 超出 total_blocks 的 %d 个 bit 未清零" % beyond)
        if img.count_bits(sb.meta_next, sb.total_blocks) != sb.total_blocks - sb.meta_next:
            bad("位图: 元数据/日志区 [%d,%d) 存在未标记的块" % (sb.meta_next, sb.total_blocks))
        marked = img.count_bits(fixed_end, sb.meta_next)
        if marked != claimed[0]:
            bad("位图: 数据区标记 %d 块，实际占用 %d 块" % (marked, claimed[0]))
        # 数据区里被标为已用、但没有 record 认领的块（逐块只在非零块上迭代）
        step_bits = 1 << 20
        lo = fixed_end
        while lo < sb.meta_next:
            n = min(step_bits, sb.meta_next - lo)
            v = img.bits_value(lo, n)
            while v:
                lsb = v & -v
                blk = lo + lsb.bit_length() - 1
                if blk not in used:
                    bad("位图标记块 %d 已用，但没有文件占用" % blk)
                v &= v - 1
            lo += n
        real_free = (sb.meta_next - fixed_end) - marked
        if real_free != sb.free_blocks:
            bad("free_blocks=%d 与位图实际空闲=%d 不符" % (sb.free_blocks, real_free))
    else:
        real_free = -1

    # ---- 孤立 record ----
    for o in sorted(owner):
        if o not in seen:
            bad("oid %d 未被任何目录项引用（孤立）" % o)

    return _fsck_report(problems, len(owner), len(used), real_free, nsym)


def _fsck_report(problems, nrec, nused, free, nsym=0):
    if problems:
        print("❌ fsck 发现 %d 个问题:" % len(problems))
        for p in problems[:50]:
            print("   - %s" % p)
        if len(problems) > 50:
            print("   ...（其余 %d 项省略）" % (len(problems) - 50))
        return 1
    sym_txt = ("（其中 %d 个符号链接）" % nsym) if nsym else ""
    print("✅ fsck 通过: %d 个 record%s, %d 个块在用, 空闲 %d 块"
          % (nrec, sym_txt, nused, free))
    return 0


def cmd_journal(args):
    img = open_img(args)
    txns, _blocks = img._jrn_state()
    print("journal: %d 块, %d 个事务 (block_size=%d)" %
          (img.sb.journal_blocks, len(txns), img.bs))
    for seq in sorted(txns):
        t = txns[seq]
        state = "END" if t["end"] is not None else ("COMMIT" if t["commit"] is not None else "OPEN")
        print("  seq=%d state=%s records=%d" % (seq, state, len(t["recs"])))
    if not txns:
        print("  （空）")
    return 0


def add_offset_args(p):
    """给命令加 --offset-lba / --offset（卷在块设备/包裹文件内的起始位置）。"""
    g = p.add_mutually_exclusive_group()
    g.add_argument("--offset-lba", type=int, default=None)
    g.add_argument("--offset", type=int, default=None)


def main():
    ap = argparse.ArgumentParser(description="KiFS v2 宿主工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("format")
    p.add_argument("img")
    p.add_argument("--size-mib", type=int, default=4)
    p.add_argument("--block-size", type=int, default=DEFAULT_BLOCK,
                   choices=list(BLOCK_SIZES))
    p.add_argument("--label", default="KKIFS")
    p.add_argument("--record-cap", type=int, default=None)
    add_offset_args(p)
    p.set_defaults(func=cmd_format)

    p = sub.add_parser("mount")
    p.add_argument("img"); p.add_argument("mountpoint")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--offset-lba", type=int, default=None)
    g.add_argument("--offset", type=int, default=None)
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--show-hidden", action="store_true")
    p.add_argument("--foreground", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--log", default=None)
    p.set_defaults(func=cmd_mount)

    p = sub.add_parser("mkdir"); p.add_argument("img"); p.add_argument("path")
    p.add_argument("--perm", default=None); add_offset_args(p); p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser("write"); p.add_argument("img"); p.add_argument("path")
    p.add_argument("--data", default=None); p.add_argument("--from", dest="src", default=None)
    p.add_argument("--perm", default=None); add_offset_args(p); p.set_defaults(func=cmd_write)

    p = sub.add_parser("read"); p.add_argument("img"); p.add_argument("path")
    p.add_argument("--out", default=None); add_offset_args(p); p.set_defaults(func=cmd_read)

    p = sub.add_parser("symlink"); p.add_argument("img"); p.add_argument("path")
    p.add_argument("--target", required=True, help="链接指向的目标路径（不解析是否存在）")
    p.add_argument("--perm", default=None); add_offset_args(p); p.set_defaults(func=cmd_symlink)

    p = sub.add_parser("readlink"); p.add_argument("img"); p.add_argument("path")
    add_offset_args(p); p.set_defaults(func=cmd_readlink)

    p = sub.add_parser("ls"); p.add_argument("img"); p.add_argument("path", nargs="?", default=None)
    add_offset_args(p); p.set_defaults(func=cmd_ls)

    p = sub.add_parser("stat"); p.add_argument("img"); p.add_argument("path")
    add_offset_args(p); p.set_defaults(func=cmd_stat)

    p = sub.add_parser("journal"); p.add_argument("img")
    add_offset_args(p); p.set_defaults(func=cmd_journal)

    p = sub.add_parser("rm"); p.add_argument("img"); p.add_argument("path")
    add_offset_args(p); p.set_defaults(func=cmd_rm)

    p = sub.add_parser("info"); p.add_argument("img")
    add_offset_args(p); p.set_defaults(func=cmd_info)

    p = sub.add_parser("fsck"); p.add_argument("img")
    add_offset_args(p); p.set_defaults(func=cmd_fsck)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

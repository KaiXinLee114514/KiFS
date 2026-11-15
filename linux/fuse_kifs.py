#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fuse_kifs.py — KiFS v2 的 Linux FUSE 驱动（单线程，读写走 Image 的 journal+CoW）

挂载：
    .venv/bin/python linux/fuse_kifs.py IMG_OR_DEV /mnt/kifs
    .venv/bin/python linux/fuse_kifs.py IMG_OR_DEV /mnt/kifs --offset-lba 22528
    .venv/bin/python linux/fuse_kifs.py IMG_OR_DEV /mnt/kifs --offset 11534336
    .venv/bin/python linux/fuse_kifs.py IMG_OR_DEV /mnt/kifs --read-only

卸载：
    fusermount3 -u /mnt/kifs

`tools/mkifs.py mount ...` 是同一入口的包装（会自动找仓库 .venv 里的 python）。

实现说明：
  * 每个写操作（write/create/mkdir/unlink/rmdir/rename/truncate/chmod…）
    都是一次 journal 事务：改完内存里的 record/位图后调用 Image.flush()；
    数据块在记录 COMMIT 之前直接落盘（新块是孤儿，断电安全）。
  * 文件读写按块/区间进行，不会把大文件整个读进内存；
    随机写只复制被修改的块（增量 CoW），顺序追加只写 EOF 之后的字节。
  * 权限位：perm 低 9 位映射到 st_mode 的 rwx；高位 hide/ro/iolock 不映射到
    setuid/setgid/sticky。ro 位与 --read-only 挂载都映射为只读；hide 项在
    readdir/getattr 里不可见；iolock 目前只作为「单线程 + 全局锁」的占位语义。
  * 所有文件 st_uid/st_gid 报为挂载用户（KiFS 只存 owner 名字，没有 uid/gid）。
"""

import argparse
import errno
import fcntl
import functools
import logging
import os
import stat
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mkifs  # noqa: E402

try:
    from fuse import FUSE, FuseOSError, Operations
except (ImportError, AttributeError) as _e:  # pragma: no cover - 缺依赖时触发
    sys.stderr.write(
        "无法导入 fusepy 3.x（%s）。系统自带的 Debian fusepy 2.x 不兼容。\n"
        "请先建 venv 并安装依赖：\n"
        "  python3 -m venv %s/.venv\n"
        "  %s/.venv/bin/pip install -r %s/requirements.txt\n"
        % (_e, ROOT, ROOT, HERE))
    raise SystemExit(1)

log = logging.getLogger("kifs.fuse")
PERM_READ = 0b001
PERM_WRITE = 0b010
PERM_EXEC = 0b100


def _rev3(g):
    """KiFS 组内位序 r=1,w=2,x=4 ↔ POSIX 组内位序 r=4,w=2,x=1。"""
    return ((g & 1) << 2) | (g & 2) | ((g & 4) >> 2)


def perm_to_mode(perm):
    """KiFS 16 位 perm 的低 9 位 → POSIX st_mode 的 rwxrwxrwx。"""
    m = 0
    for group in range(3):                      # 0=owner,1=group,2=other
        g = (perm >> (group * 3)) & 0b111
        m |= _rev3(g) << ((2 - group) * 3)
    return m


def mode_to_perm(mode):
    """POSIX rwxrwxrwx → KiFS perm 的低 9 位（高位 hide/ro/iolock 由调用方保留）。"""
    p = 0
    for group in range(3):
        g = (mode >> ((2 - group) * 3)) & 0b111
        p |= _rev3(g) << (group * 3)
    return p


def _locked(fn):
    """所有 FUSE 回调串行化；出错时回滚未提交的内存事务。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._mutex:
            try:
                return fn(self, *args, **kwargs)
            except FuseOSError:
                # FuseOSError 是 OSError 子类，必须先于 OSError 捕获：
                # 正常的 ENOENT/EACCES 等控制流异常不能回滚未提交事务！
                raise
            except OSError:
                self._rollback()
                raise
            except SystemExit as e:      # mkifs 内部用 SystemExit 报告致命错误
                self._rollback()
                log.error("KiFS 内部错误: %s", e)
                raise FuseOSError(errno.EIO)
            except Exception:
                self._rollback()
                log.exception("FUSE 操作 %s 异常", fn.__name__)
                raise FuseOSError(errno.EIO)
    return wrapper


class KifsFS(Operations):
    def __init__(self, image, offset=0, read_only=False, allow_hidden=False):
        self.read_only = bool(read_only)
        self.allow_hidden = bool(allow_hidden)
        self._dirty = False          # 延迟 journal 提交（写路径只打标记）
        self._dirty_bytes = 0        # 累积脏字节，超过阈值强制提交一次
        self._wb = None              # 顺序写合并缓冲: (rec, start_off, bytearray)
        self.uid = os.getuid()
        self.gid = os.getgid()
        self._mutex = threading.RLock()
        self.img = mkifs.Image(image, offset=offset)
        # 同镜像只允许一个写者（FUSE 挂载期间独占）
        try:
            fcntl.flock(self.img.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("镜像已被其他进程锁住（是否已挂载/正在写）: %s" % image)
        self.bs = self.img.bs
        log.info("挂载 KiFS: %s offset=%d block=%d total_blocks=%d",
                 image, self.img.offset, self.bs, self.img.sb.total_blocks)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _rollback(self):
        try:
            self.img.reload()
            self.img.recover()
        except Exception:
            log.exception("回滚失败（磁盘可能需要在下次挂载时 recover）")

    def _lookup(self, path):
        rec = self.img.record_at(path)
        if rec is not None and not self.allow_hidden and (rec.perm & mkifs.PERM_HIDE):
            return None
        return rec

    def _need(self, path, typ=None):
        rec = self._lookup(path)
        if rec is None:
            raise FuseOSError(errno.ENOENT)
        if typ is not None and rec.type != typ:
            if typ == mkifs.TYPE_DIR:
                raise FuseOSError(errno.ENOTDIR)
            raise FuseOSError(errno.EISDIR)
        return rec

    def _need_dir(self, path):
        return self._need(path, mkifs.TYPE_DIR)

    def _need_file(self, path):
        """文件操作专用：符号链接不给当普通文件读写（EINVAL），目录 EISDIR。"""
        rec = self._need(path)
        if rec.type == mkifs.TYPE_SYMLINK:
            raise FuseOSError(errno.EINVAL)
        if rec.type != mkifs.TYPE_FILE:
            raise FuseOSError(errno.EISDIR)
        return rec

    def _check_ro(self):
        if self.read_only:
            raise FuseOSError(errno.EROFS)

    def _check_read(self, rec):
        if rec.type == mkifs.TYPE_DIR:
            return
        if not (rec.perm & PERM_READ):
            raise FuseOSError(errno.EACCES)

    def _check_write(self, rec):
        self._check_ro()
        if rec.perm & mkifs.PERM_RO:
            raise FuseOSError(errno.EROFS)
        if not (rec.perm & PERM_WRITE):
            raise FuseOSError(errno.EACCES)

    def _check_dir_write(self, parent):
        self._check_ro()
        if parent.perm & mkifs.PERM_RO:
            raise FuseOSError(errno.EROFS)
        if not (parent.perm & PERM_WRITE) or not (parent.perm & PERM_EXEC):
            raise FuseOSError(errno.EACCES)

    def _attrs(self, rec):
        if rec.type == mkifs.TYPE_SYMLINK:
            # 符号链接的权限位恒为 0777（Linux 忽略其实际权限，不随只读挂载降级）
            return {
                "st_mode": stat.S_IFLNK | 0o777,
                "st_nlink": 1,
                "st_size": rec.size,          # 目标路径的字节数
                "st_uid": self.uid,
                "st_gid": self.gid,
                "st_ino": rec.oid,
                "st_atime": float(rec.mtime),
                "st_mtime": float(rec.mtime),
                "st_ctime": float(rec.ctime or rec.mtime),
                "st_blocks": 0,
                "st_blksize": self.bs,
            }
        isdir = rec.type == mkifs.TYPE_DIR
        mode = (stat.S_IFDIR if isdir else stat.S_IFREG) | perm_to_mode(rec.perm)
        if self.read_only:
            mode &= ~0o222
        return {
            "st_mode": mode,
            "st_nlink": 2 if isdir else 1,
            "st_size": rec.size,
            "st_uid": self.uid,
            "st_gid": self.gid,
            "st_ino": rec.oid,
            "st_atime": float(rec.mtime),
            "st_mtime": float(rec.mtime),
            "st_ctime": float(rec.ctime or rec.mtime),
            "st_blocks": (rec.blocks * self.bs + 511) // 512,
            "st_blksize": self.bs,
        }

    def _flush(self):
        self.img.flush()

    def _commit_if_dirty(self):
        """把累积的元数据增量一次性提交 journal。

        write() 不再逐次提交（那是 0.4 MiB/s 的元凶）；
        数据块在 write_at 里已直接落盘，元数据（位图/record）这里一次提交。
        """
        if self._dirty:
            self.img.flush()
            self._dirty = False
            self._dirty_bytes = 0

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    def _wb_flush(self):
        """把顺序写合并缓冲整段提交：一次 write_at，避免 4 KiB 一哆嗦。"""
        wb = self._wb
        if not wb:
            return
        self._wb = None
        rec, start, buf, _fh = wb
        if not buf:
            return
        self.img.write_at(rec, start, bytes(buf))
        rec.mtime = int(time.time())
        self.img.write_record(rec)
        self._dirty = True
        self._dirty_bytes += len(buf)
        if self._dirty_bytes >= 128 * 1024 * 1024:
            self._commit_if_dirty()

    @_locked
    def getattr(self, path, fh=None):
        self._wb_flush()
        rec = self._need(path)
        return self._attrs(rec)

    @_locked
    def access(self, path, amode):
        rec = self._need(path)
        if amode & os.W_OK:
            if rec.type == mkifs.TYPE_DIR:
                self._check_dir_write(rec)
            else:
                self._check_write(rec)
        if amode & os.R_OK:
            self._check_read(rec)
        if amode & os.X_OK:
            if not (rec.perm & PERM_EXEC):
                raise FuseOSError(errno.EACCES)
        return 0

    @_locked
    def statfs(self, path):
        self._wb_flush()
        info = self.img.statfs_info()
        return {
            "f_bsize": info["block_size"],
            "f_frsize": info["block_size"],
            "f_blocks": info["blocks"],
            "f_bfree": info["free_blocks"],
            "f_bavail": info["free_blocks"],
            "f_files": info["files"],
            "f_ffree": info["free_files"],
            "f_favail": info["free_files"],
            "f_namemax": 255,
        }

    @_locked
    def readdir(self, path, fh):
        self._wb_flush()
        rec = self._need_dir(path)
        names = ["."]
        if path != "/":
            names.append("..")
        for (name, oid, typ, _elen) in self.img.dir_entries(rec):
            child = self.img.read_record(oid)
            if child is None:
                continue
            if not self.allow_hidden and (child.perm & mkifs.PERM_HIDE):
                continue
            if child.type == mkifs.TYPE_SYMLINK:
                # 带 attrs 的项才能让内核拿到 DT_LNK（ls -F/ls -l 首字符为 l）
                names.append((name, {"st_mode": stat.S_IFLNK | 0o777,
                                     "st_ino": oid}, 0))
            else:
                names.append(name)
        return names

    @_locked
    def opendir(self, path):
        self._need_dir(path)
        return 0

    @_locked
    def releasedir(self, path, fh):
        return 0

    @_locked
    def chmod(self, path, mode):
        self._check_ro()
        self.img.chmod_at(path, mode_to_perm(mode & 0o777))
        self._flush()
        return 0

    @_locked
    def chown(self, path, uid, gid):
        self._need(path)
        if uid not in (-1, self.uid) or gid not in (-1, self.gid):
            raise FuseOSError(errno.EPERM)
        return 0

    @_locked
    def utimens(self, path, times=None):
        self._check_ro()
        now = time.time()
        _atime, mtime = times if times else (now, now)
        self.img.utimens_at(path, mtime=mtime)
        self._flush()
        return 0

    # ------------------------------------------------------------------
    # 打开 / 读写
    # ------------------------------------------------------------------
    @_locked
    def open(self, path, flags):
        self._wb_flush()
        rec = self._need_file(path)
        if (flags & os.O_ACCMODE) != os.O_RDONLY:
            self._check_write(rec)
        else:
            self._check_read(rec)
        return rec.oid          # 用 oid 当 fh，后续读写直接按 oid 查 record，省去路径解析

    @_locked
    def create(self, path, mode, fi=None):
        parent, _name = self.img.parent_record(path)
        self._check_dir_write(parent)
        self.img.create_file(path, perm=mode_to_perm(mode & 0o777))
        self._flush()
        return 0

    @_locked
    def mknod(self, path, mode, dev):
        if not stat.S_ISREG(mode):
            raise FuseOSError(errno.EPERM)
        return self.create(path, mode & 0o777)

    @_locked
    def read(self, path, size, offset, fh):
        self._wb_flush()
        rec = self.img.read_record(fh) if fh else self._need_file(path)
        self._check_read(rec)
        return self.img.read_at(rec, offset, size)

    @_locked
    def write(self, path, data, offset, fh):
        wb = self._wb
        if wb is not None and fh and wb[3] == fh and wb[1] + len(wb[2]) == offset:
            wb[2].extend(data)                 # 热路径：同一 fh 顺序写，只记缓冲
        else:
            self._wb_flush()
            rec = self.img.read_record(fh) if fh else self._need_file(path)
            self._check_write(rec)
            self._wb = (rec, offset, bytearray(data), fh)
        if len(self._wb[2]) >= 4 * 1024 * 1024:  # 每 4 MiB 提交一次，限制内存
            self._wb_flush()
        return len(data)

    @_locked
    def truncate(self, path, length, fh=None):
        self._wb_flush()
        rec = self.img.read_record(fh) if fh else self._need_file(path)
        self._check_write(rec)
        self.img.truncate_at(rec, length)
        rec.mtime = int(time.time())
        self.img.write_record(rec)
        self._flush()
        return 0

    @_locked
    def flush(self, path, fh):
        self._wb_flush()
        self._commit_if_dirty()
        return 0

    @_locked
    def fsync(self, path, datasync, fh):
        self._wb_flush()
        self._commit_if_dirty()
        self.img.flush()
        try:
            os.fsync(self.img.fh.fileno())
        except OSError:
            pass
        return 0

    @_locked
    def fsyncdir(self, path, datasync, fh):
        return self.fsync(path, datasync, fh)

    @_locked
    def release(self, path, fh):
        self._wb_flush()
        self._commit_if_dirty()
        return 0

    def destroy(self, path=None):
        try:
            self._wb_flush()
            self._commit_if_dirty()
        except Exception:
            log.exception("destroy 提交 journal 失败")

    # ------------------------------------------------------------------
    # 命名空间
    # ------------------------------------------------------------------
    @_locked
    def mkdir(self, path, mode):
        parent, _name = self.img.parent_record(path)
        self._check_dir_write(parent)
        self.img.mkdir_at(path, perm=mode_to_perm(mode & 0o777))
        self._flush()
        return 0

    @_locked
    def unlink(self, path):
        self._wb_flush()
        parent, _name = self.img.parent_record(path)
        self._check_dir_write(parent)
        self.img.unlink_at(path)
        self._flush()
        return 0

    @_locked
    def rmdir(self, path):
        parent, _name = self.img.parent_record(path)
        self._check_dir_write(parent)
        self.img.rmdir_at(path)
        self._flush()
        return 0

    @_locked
    def rename(self, old, new):
        self._wb_flush()
        if self.read_only:
            raise FuseOSError(errno.EROFS)
        op, _on = self.img.parent_record(old)
        np, _nn = self.img.parent_record(new)
        self._check_dir_write(op)
        self._check_dir_write(np)
        self.img.rename_at(old, new)
        self._flush()
        return 0

    @_locked
    def link(self, target, source):
        raise FuseOSError(errno.EOPNOTSUPP)

    @_locked
    def readlink(self, path):
        rec = self._need(path)
        if rec.type != mkifs.TYPE_SYMLINK:
            raise FuseOSError(errno.EINVAL)
        data = self.img.read_content(rec)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise FuseOSError(errno.EIO)

    @_locked
    def symlink(self, target, source):
        # fusepy 的参数顺序：target=新链接路径，source=链接指向的目标路径
        # （底层 C: symlink(source, target)，见 fusepy Operations.symlink 文档）
        try:
            tlen = len(source.encode("utf-8"))
        except UnicodeEncodeError:
            raise FuseOSError(errno.EINVAL)
        if not source:
            raise FuseOSError(errno.ENOENT)
        if tlen > mkifs.SYMLINK_TARGET_MAX:
            raise FuseOSError(errno.ENAMETOOLONG)
        parent, name = self.img.parent_record(target)
        self._check_dir_write(parent)
        if self.img.dir_lookup(parent, name) is not None:
            raise FuseOSError(errno.EEXIST)
        self.img.symlink(target, source)
        self._flush()
        return 0

    @_locked
    def getxattr(self, path, name, position=0):
        self._need(path)
        raise FuseOSError(errno.ENODATA)

    @_locked
    def listxattr(self, path):
        self._need(path)
        return []

    @_locked
    def setxattr(self, path, name, value, options, position=0):
        raise FuseOSError(errno.EOPNOTSUPP)

    @_locked
    def removexattr(self, path, name):
        raise FuseOSError(errno.EOPNOTSUPP)

    def destroy(self, path):
        with self._mutex:
            try:
                self.img.close()
            except Exception:
                pass


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="KiFS v2 FUSE 驱动（挂载镜像文件或块设备）")
    ap.add_argument("image", help="KiFS 镜像文件，或含 KiFS 卷的块设备")
    ap.add_argument("mountpoint", help="挂载点目录（需已存在）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--offset-lba", type=int, default=None,
                   help="KiFS 卷在设备内的起始 LBA（512B 扇区）")
    g.add_argument("--offset", type=int, default=None,
                   help="KiFS 卷在设备内的起始字节偏移")
    ap.add_argument("--read-only", action="store_true", help="只读挂载")
    ap.add_argument("--show-hidden", action="store_true",
                    help="显示带 hide 权限位的条目（默认隐藏）")
    ap.add_argument("--foreground", action="store_true", help="前台运行（调试）")
    ap.add_argument("--debug", action="store_true", help="前台 + FUSE 调试输出")
    ap.add_argument("--log", default=None, help="日志文件（daemon 化后 stderr 会丢失）")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not os.path.isdir(args.mountpoint):
        sys.stderr.write("挂载点不存在或不是目录: %s\n" % args.mountpoint)
        return 2
    offset = 0
    if args.offset_lba is not None:
        offset = args.offset_lba * 512
    elif args.offset is not None:
        offset = args.offset
    if offset < 0:
        sys.stderr.write("offset 不能为负\n")
        return 2
    if args.log:
        logging.basicConfig(filename=args.log, level=logging.DEBUG,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO,
                            format="%(levelname)s %(name)s: %(message)s")
    try:
        fs = KifsFS(args.image, offset=offset, read_only=args.read_only,
                    allow_hidden=args.show_hidden)
    except OSError as e:
        sys.stderr.write("打开镜像失败: %s\n" % e)
        return 1
    except SystemExit as e:
        sys.stderr.write("初始化失败: %s\n" % e)
        return 1
    kwargs = {
        "fsname": args.image,
        "subtype": "kifs",
        "nothreads": True,
        "default_permissions": True,
        "foreground": bool(args.foreground or args.debug),
        "debug": bool(args.debug),
    }
    if args.read_only:
        kwargs["ro"] = True
    try:
        FUSE(fs, args.mountpoint, **kwargs)
    except RuntimeError as e:
        sys.stderr.write("挂载失败: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

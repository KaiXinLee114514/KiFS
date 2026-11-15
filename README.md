# KiFS (Kai File System)

一个以「对象 + 倒排关键词索引」为核心的块文件系统（块大小 4 / 16 KiB）—— 目录只是视图，语义查询是一等公民。

> 状态：**v0.5 · 格式 v2 + 符号链接**（2026-11-15）——宿主工具支持 16 KiB 块、u64 文件大小、
> 1M record、单卷 64 TiB、大卷稀疏 format，以及 record `type=3` 符号链接。测试：
> `tools/test_mkifs.py` 228 项、`tools/test_journal.py` 111 项（4K+16K）。
> 本仓库 `kernel/` 下的汇编驱动已同步 v2（读写/journal/CoW），但尚未专门支持
> 符号链接（显示行为见 [kernel/README.md](kernel/README.md)）。
> 设计稿：[docs/kifs.md](docs/kifs.md) · v2 规格：[docs/kifs-v2.md](docs/kifs-v2.md) · 集成指南：[docs/integration.md](docs/integration.md)
> Linux 可用 FUSE 直接挂载（读写走 journal+CoW）：[linux/README.md](linux/README.md)

## 设计速览

- 盘头 Superblock（带备份）→ 块位图 / Record 表 / 数据块向上长
- 关键词串池 / Subindex / Journal（redo）从盘尾向下长，双游标
- Record 128 B：oid/type/flags/perm(16 位)/size(u64)/时间/parent/kw_index/owner + 小文件内联 ≤92 B + 最多 3 个内联 extent
- 符号链接：`type=3`，目标路径（≤1024 B，允许 dangling）复用内联/extent 存储；`ln -s`/`readlink` 可用
- v2：`size_lo/size_hi` 与 `blocks_lo/blocks_hi` 拼 u64；extent 每项 12 B `(start u64, count u32)`；
  `record_count = min(max(16, total_blocks//64), 1<<20)`
- 权限展示：`rwxrwxrwx111kai`（9 位 rwx + 3 位 hide/ro/iolock + 用户名；hide/ro/iolock 都是 perm 位）
- 目录 = 有序 UTF-8 目录项；关键词 = 串池 + 倒排 OID 数组
- 断电一致性：BEGIN → BLOCK → COMMIT → 落盘 → END 的环形 journal
- 查询示例：`"Last Summer family photos"` → 时间区间 + 类型 + keyword 的结构化过滤

## 项目结构

```
kifs/
├── docs/
│   ├── kifs.md           设计稿（磁盘布局/Record/目录/journal/权限/关键词/路线）
│   └── integration.md    如何把 kernel/kifs.asm 集成进一个 32 位内核
├── kernel/
│   ├── kifs.asm          KiFS 内核驱动（x86 nasm 平铺，Phase C1 读写+journal+CoW）
│   └── README.md         驱动说明（命令/内存布局/写入语义/测试）
├── tools/
│   ├── mkifs.py          宿主工具（v2）：format/mount/mkdir/write/read/symlink/readlink/ls/stat/rm/info/fsck/journal
│   ├── test_mkifs.py     宿主自测 228 项（4 KiB + 16 KiB，含符号链接）
│   └── test_journal.py   断电注入自测 111 项（4K+16K × 断电点 + 符号链接创建/删除）
├── linux/
│   ├── fuse_kifs.py      FUSE 驱动（挂载镜像/块设备，读写走 Image 的 journal+CoW）
│   ├── test_fuse.sh      端到端测试：真挂载→POSIX 操作→卸载→fsck+字节比对
│   ├── test_image_api.py Image 块级 API 自测（inline/随机写/ext_index/truncate/符号链接）
│   ├── requirements.txt  fusepy>=3.0（装进仓库 .venv）
│   └── README.md         依赖/挂载卸载/权限/限制/排障
└── README.md
```

参考实现分两层：宿主 Python（`tools/mkifs.py`，字节级权威）与内核汇编（`kernel/kifs.asm`，与之逐字节一致）。

## 路线图

| Phase | 内容 | 状态 |
|---|---|---|
| A | `tools/mkifs.py` 宿主（format/mkdir/write/read/symlink/readlink/ls/stat/rm/info/fsck/journal）+ 测试镜像 | ✅ v2 + 符号链接完成（**16 KiB 块 / u64 / 1M record / 大卷 / type=3**）|
| B | 内核只读驱动（挂载 /MNT/KIFS） | ✅ B0：只读驱动 + `kifs info/ls/cat/stat`（KKernel `kifs-phase-b` 分支）|
| C | 内核写入 + journal + 断电注入测试 | ✅ C1：内核写入 + journal + CoW（`kifs-phase-c` 分支，48 项断言）；C2 真机 kill -9 压测待做 |
| D | keywords 倒排 + 语义查询 | 未开始 |
| E | 快照/CoW、Subindex、压缩 | 未开始 |
| F | Linux FUSE 挂载（免内核模块，读写走 journal+CoW） | ✅ `linux/fuse_kifs.py` + `linux/test_fuse.sh` |

## 宿主原型快速上手

```bash
# 4 KiB 块小镜像（CI/快速）
python3 tools/mkifs.py format /tmp/kifs.img --size-mib 4
# 16 KiB 块（大卷；64 GiB 只要 ~10 MiB 元数据写入）
python3 tools/mkifs.py format /tmp/kifs16.img --size-mib 65536 --block-size 16384
python3 tools/mkifs.py mkdir  /tmp/kifs.img /docs
python3 tools/mkifs.py write  /tmp/kifs.img /docs/hello.txt --data "hello kifs"
python3 tools/mkifs.py read   /tmp/kifs.img /docs/hello.txt
python3 tools/mkifs.py ls     /tmp/kifs.img /docs
python3 tools/mkifs.py symlink /tmp/kifs.img /docs/link --target hello.txt
python3 tools/mkifs.py readlink /tmp/kifs.img /docs/link
python3 tools/mkifs.py info   /tmp/kifs.img      # 布局/版本/record_count
python3 tools/mkifs.py fsck   /tmp/kifs.img
```

`format` 选项：`--size-mib N`、`--block-size 4096|16384`、`--record-cap N`（≤1M）、
`--label X`。format 只写元数据区，大卷是稀疏文件（15 TiB 卷实写 ~249 MiB）。

自测：

```bash
python3 tools/test_mkifs.py     # 228 项：4K/16K 功能 + u64/extent/record 上限/大卷/符号链接
python3 tools/test_journal.py   # 111 项：4K+16K × 断电注入 + CoW + 符号链接创建/删除
```

内核侧（KKernel 参考仓库的 `kifs-phase-c` 分支）：

```bash
make test-kifs        # 48 项：内核读写/journal/reboot 持久化/崩溃重放/宿主交叉验证
```

## Linux 挂载（FUSE）

不需要内核模块：用户态 FUSE 把 KiFS 镜像或块设备挂成普通目录，所有写入走
`tools/mkifs.py` 的 journal+CoW（与宿主工具同一套代码，掉电可重放）。

```bash
# 一次性：装依赖（fusepy 3.x；Debian 自带的 2.x 不兼容）
python3 -m venv .venv
.venv/bin/pip install -r linux/requirements.txt

# 造卷 → 挂载 → 用 → 卸载 → 校验
tools/mkifs.py format /tmp/kifs.img --size-mib 4
tools/mkifs.py mount  /tmp/kifs.img /mnt/kifs
ls -la /mnt/kifs && cp bigfile /mnt/kifs/ && sync
fusermount3 -u /mnt/kifs
tools/mkifs.py fsck /tmp/kifs.img
```

- **块设备**：`sudo tools/mkifs.py format /dev/sdb1 --size-mib N` 后
  `sudo tools/mkifs.py mount /dev/sdb1 /mnt/kifs`（整盘就是卷，需要 root）；
  盘上有分区表/别的头时 `format`/`mount` 都加 `--offset-lba 22528`（内核驱动的固定
  分区）或 `--offset BYTES`。
- **只读/hide/offline 卷**：`--read-only`、`--show-hidden`、`--offset` 等选项与限制
  （单线程、不支持稀疏文件、盘满 ENOSPC、ext_index 碎片化）见 [linux/README.md](linux/README.md)。
- **测试**：`linux/test_fuse.sh` —— 4K/4MiB 与 16K/64MiB 两张镜像，挂载→
  mkdir/cp/dd/rename/chmod/truncate/append→卸载→`fsck` 必过 + 离线字节比对；
  另有 `[符号链接]` 小节与 root 下的 loop 块设备路径。

## 内核驱动快速上手

- 复制 `kernel/kifs.asm` 进内核，`%include` 在 ATA 驱动之后，启动时 `call kifs_init`（没有卷就静默跳过）。
- 命令：`kifs info / ls / cat / stat / mkdir / write / rm / journal`。
- 固定分区 LBA 22528（11 MiB），不挂 VFS；写入走 journal（KJRN+CRC16）与 CoW。
- **v2 格式**：从 superblock 读 `block_size`（4K/16K）、16 KiB 块缓冲、LBA48 寻址、
  Record 的 `size_hi/blocks_hi`、extent `(u64,u32)`、journal payload=`block_size-20`
  （内核侧已在 `kifs-v2` 分支落地）。
- 详细接入与依赖见 [docs/integration.md](docs/integration.md) 和 [kernel/README.md](kernel/README.md)。

## 为什么不开源（暂时）

本项目计划在合适的时间点作为独立仓库公开。在此之前只在本地迭代。

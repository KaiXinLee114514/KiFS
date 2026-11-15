# KiFS (Kai File System)

一个以「对象 + 倒排关键词索引」为核心的块文件系统（块大小 4 / 16 KiB）—— 目录只是视图，语义查询是一等公民。

> 状态：**v0.4 · 格式 v2**（2026-11-15）——宿主工具已升级 v2：16 KiB 块、u64 文件大小、
> 1M record、单卷 64 TiB、大卷稀疏 format。测试：`tools/test_mkifs.py` 192 项、
> `tools/test_journal.py` 91 项（4K+16K）。本仓库 `kernel/` 下的汇编驱动仍是 v1 格式，
> 需要按 v2 规格升级（见 [docs/integration.md](docs/integration.md)）。
> 设计稿：[docs/kifs.md](docs/kifs.md) · v2 规格：[docs/kifs-v2.md](docs/kifs-v2.md) · 集成指南：[docs/integration.md](docs/integration.md)

## 设计速览

- 盘头 Superblock（带备份）→ 块位图 / Record 表 / 数据块向上长
- 关键词串池 / Subindex / Journal（redo）从盘尾向下长，双游标
- Record 128 B：oid/type/flags/perm(16 位)/size(u64)/时间/parent/kw_index/owner + 小文件内联 ≤92 B + 最多 3 个内联 extent
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
│   ├── mkifs.py          宿主工具（v2）：format/mkdir/write/read/ls/stat/rm/info/fsck/journal
│   ├── test_mkifs.py     宿主自测 192 项（4 KiB + 16 KiB）
│   └── test_journal.py   断电注入自测 91 项（4K+16K × after_begin/records/commit/apply/torn）
└── README.md
```

参考实现分两层：宿主 Python（`tools/mkifs.py`，字节级权威）与内核汇编（`kernel/kifs.asm`，与之逐字节一致）。

## 路线图

| Phase | 内容 | 状态 |
|---|---|---|
| A | `tools/mkifs.py` 宿主（format/mkdir/write/read/ls/stat/rm/info/fsck/journal）+ 测试镜像 | ✅ v2 完成（**16 KiB 块 / u64 / 1M record / 大卷**）|
| B | 内核只读驱动（挂载 /MNT/KIFS） | ✅ B0：只读驱动 + `kifs info/ls/cat/stat`（KKernel `kifs-phase-b` 分支）|
| C | 内核写入 + journal + 断电注入测试 | ✅ C1：内核写入 + journal + CoW（`kifs-phase-c` 分支，48 项断言）；C2 真机 kill -9 压测待做 |
| D | keywords 倒排 + 语义查询 | 未开始 |
| E | 快照/CoW、Subindex、压缩 | 未开始 |

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
python3 tools/mkifs.py info   /tmp/kifs.img      # 布局/版本/record_count
python3 tools/mkifs.py fsck   /tmp/kifs.img
```

`format` 选项：`--size-mib N`、`--block-size 4096|16384`、`--record-cap N`（≤1M）、
`--label X`。format 只写元数据区，大卷是稀疏文件（15 TiB 卷实写 ~249 MiB）。

自测：

```bash
python3 tools/test_mkifs.py     # 192 项：4K/16K 功能 + u64/extent/record 上限/大卷
python3 tools/test_journal.py   # 91 项：4K+16K × 5 种断电注入 + CoW
```

内核侧（KKernel 参考仓库的 `kifs-phase-c` 分支）：

```bash
make test-kifs        # 48 项：内核读写/journal/reboot 持久化/崩溃重放/宿主交叉验证
```

## 内核驱动快速上手

- 复制 `kernel/kifs.asm` 进内核，`%include` 在 ATA 驱动之后，启动时 `call kifs_init`（没有卷就静默跳过）。
- 命令：`kifs info / ls / cat / stat / mkdir / write / rm / journal`。
- 固定分区 LBA 22528（11 MiB），不挂 VFS；写入走 journal（KJRN+CRC16）与 CoW。
- **v2 变更（内核侧待跟进）**：从 superblock 读 `block_size`（4K/16K）、16 KiB 块缓冲、
  LBA48 寻址、Record 的 `size_hi/blocks_hi`、extent `(u64,u32)`、journal payload=`block_size-20`。
- 详细接入与依赖见 [docs/integration.md](docs/integration.md) 和 [kernel/README.md](kernel/README.md)。

## 为什么不开源（暂时）

本项目计划在合适的时间点作为独立仓库公开。在此之前只在本地迭代。

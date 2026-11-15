# KiFS (Kai File System)

一个以「对象 + 倒排关键词索引」为核心的 4 KiB 块文件系统 —— 目录只是视图，语义查询是一等公民。

> 状态：**v0.3**（2026-11-15）——宿主原型 A（journal+CoW+断电注入）与内核驱动 C1（读写 + journal + CoW，48 项交叉验证）均已完成。
> 设计稿：[docs/kifs.md](docs/kifs.md) · 内核驱动：[kernel/README.md](kernel/README.md) · 集成指南：[docs/integration.md](docs/integration.md)

## 设计速览

- 盘头 Superblock（带备份）→ 块位图 / Record 表 / 数据块向上长
- 关键词串池 / Subindex / Journal（redo）从盘尾向下长，双游标
- Record 128 B：oid/type/flags/perm(16 位)/size/时间/parent/kw_index/owner + 小文件内联 ≤92 B + 最多 3 个内联 extent
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
│   ├── mkifs.py          宿主原型：format/mkdir/write/read/ls/stat/rm/info/fsck/journal
│   ├── test_mkifs.py     宿主自测 15 项
│   └── test_journal.py   断电注入自测 29 项（after_begin/records/commit/apply/torn）
└── README.md
```

参考实现分两层：宿主 Python（`tools/mkifs.py`，字节级权威）与内核汇编（`kernel/kifs.asm`，与之逐字节一致）。

## 路线图

| Phase | 内容 | 状态 |
|---|---|---|
| A | `tools/mkifs.py` 宿主原型（format/mkdir/write/read/ls/stat/rm/info/fsck/journal）+ 测试镜像 | ✅ 完成（连续 extent / 目录线性 / **journal redo + CoW**）|
| B | 内核只读驱动（挂载 /MNT/KIFS） | ✅ B0：只读驱动 + `kifs info/ls/cat/stat`（KKernel `kifs-phase-b` 分支）|
| C | 内核写入 + journal + 断电注入测试 | ✅ C1：内核写入 + journal + CoW（`kifs-phase-c` 分支，48 项断言）；C2 真机 kill -9 压测待做 |
| D | keywords 倒排 + 语义查询 | 未开始 |
| E | 快照/CoW、Subindex、压缩 | 未开始 |

## 宿主原型快速上手

```bash
python3 tools/mkifs.py format /tmp/kifs.img --size-mib 4
python3 tools/mkifs.py mkdir  /tmp/kifs.img /docs
python3 tools/mkifs.py write  /tmp/kifs.img /docs/hello.txt --data "hello kifs"
python3 tools/mkifs.py read   /tmp/kifs.img /docs/hello.txt
python3 tools/mkifs.py ls     /tmp/kifs.img /docs
python3 tools/mkifs.py fsck   /tmp/kifs.img
```

自测：

```bash
python3 tools/test_mkifs.py     # 15 项宿主流程自测
python3 tools/test_journal.py   # 29 项断电注入自测（journal+CoW）
```

内核侧（KKernel 参考仓库的 `kifs-phase-c` 分支）：

```bash
make test-kifs        # 48 项：内核读写/journal/reboot 持久化/崩溃重放/宿主交叉验证
```

## 内核驱动快速上手

- 复制 `kernel/kifs.asm` 进内核，`%include` 在 ATA 驱动之后，启动时 `call kifs_init`（没有卷就静默跳过）。
- 命令：`kifs info / ls / cat / stat / mkdir / write / rm / journal`。
- 固定分区 LBA 22528（11 MiB），不挂 VFS；写入走 journal（KJRN+CRC16）与 CoW。
- 详细接入与依赖见 [docs/integration.md](docs/integration.md) 和 [kernel/README.md](kernel/README.md)。

## 为什么不开源（暂时）

本项目计划在合适的时间点作为独立仓库公开。在此之前只在本地迭代。

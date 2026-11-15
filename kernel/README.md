# KiFS 内核驱动（kifs.asm）

`kernel/kifs.asm` 是 KiFS 在 32 位保护模式内核里的实现（x86，nasm `-f bin` 平铺内核，无分页依赖）。
当前为 **Phase C1（v1 格式）**：只读 + 写入（journal + CoW），已通过 48 项内核侧/宿主侧交叉验证。

> ⚠️ **格式 v2（2026-11-15）**：宿主 `tools/mkifs.py` 已升级到 v2（`version=0x00020000`，
> 支持 4096/16384 块、u64 size、extent `(u64,u32)`、1M record）。本目录的 v1 驱动读到
> v2 镜像会因版本不符拒绝；升级要点见 [docs/integration.md](../docs/integration.md) 顶部
> 与 [docs/kifs-v2.md](../docs/kifs-v2.md) §5。

## 定位

- 磁盘布局固定在 **LBA 22528（11 MiB）** 起的连续分区，不解析分区表、不挂 VFS。
  盘上没有合法 KiFS 卷时 `kifs_init` **静默返回**（`kifs_ok=0`），不影响其他文件系统。
- 与 FAT16 各读各的分区，互不影响；KiFS 卷由宿主工具 [`tools/mkifs.py`](../tools/mkifs.py) 或内核自身写入。
- 依赖宿主内核提供：ATA PIO 读写、shell 命令表/打印、管道 stdin（`kifs write` 无文本参数时从管道读）。

## 命令行

| 命令 | 说明 |
|---|---|
| `kifs info` | superblock：magic/version/块大小/总块数/root_oid/free_blocks/卷标/journal |
| `kifs ls [路径]` | 列目录（省略 = 根） |
| `kifs cat <路径>` | 打印文件（内联 / extent） |
| `kifs stat <路径>` | oid/类型/权限/大小/块数/inline_len/extent/父 oid |
| `kifs mkdir <路径>` | 建目录 |
| `kifs write <路径> [文本]` | 写文件（inline 或 extent；无文本参数时读管道 stdin） |
| `kifs rm <路径>` | 删文件 / 空目录 |
| `kifs journal` | 列 journal 事务状态（END/COMMIT/OPEN） |

示例：
```
kifs mkdir /w
kifs write /w/a.txt hello kernel
kifs cat README.TXT | kifs write /w/copy.bin     # 管道写入多块文件
kifs journal
```

## 内存布局（与内核 0x3C0000 区共用；0x3C0000–0x3C2FFF 已被 UHCI/USB 占用）

| 范围 | 用途 |
|---|---|
| 0x3C3000–0x3C5FFF | 块缓冲 / 目录内容缓冲 / record / 路径缓冲 |
| 0x3C6000–0x3C7FFF | 位图缓存 + journal 块构建 / 目标块暂存 |
| 0x3C8000–0x3CFFFF | 写入数据 / 目录内容 staging（32 KiB） |
| 0x3D2000–0x3D32FF | superblock 影子 / delta 表 / 事务表 |

## 写入语义

- 元数据（Record 128 B / 位图字节 / Superblock 三副本）走 journal 事务：
  `BEGIN → 数据片记录 → COMMIT → 应用 → END`（20 B 头 `KJRN` + CRC-16-CCITT + 4076 B payload，64 槽环形）。
- **CoW**：文件数据块写新块（直接 ATA），旧块在事务内从位图释放；断电时新块是孤儿，旧数据/旧元数据始终完整。
- 挂载（`kifs_init`）时重放完整事务（BEGIN+COMMIT、CRC 全有效；幂等），残缺事务回收槽位。

## 测试

- 内核侧：KKernel 仓库 `make test-kifs`（`tests/qemu_test.py --kifs`，48 项断言：读写/jonrnal/reboot 持久化/崩溃重放/宿主交叉验证）。
- 宿主交叉验证：测试后从镜像 dd 出 LBA 22528 起 4 MiB，用 `tools/mkifs.py fsck` + `read` 做字节级比对。
- 宿主自测：`tools/test_mkifs.py`（15 项）、`tools/test_journal.py`（29 项断电注入）。

## 已知偏差 / TODO

- `kifs_now` 恒 0：新建/修改文件的 ctime/mtime 写 0（宿主 fsck 不检查）。
- `kifs write <path> <text>` 的文本参数受内核 shell 行缓冲 128 字符限制；大文件请用管道 stdin。
- journal 64 槽环形复用，与宿主 mkifs.py 行为一致。
- C2（真机断电注入压测）未做。

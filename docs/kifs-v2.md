# KiFS v2 格式规格（草案，Block 16 KiB / u64 文件大小 / 大卷）

> 目标：单卷最大 **64 TiB**（16 KiB 块 × u32 块号）、单文件最大 = 卷大小、文件数上限 1M。
> v2 与 v1 **不兼容**（测试镜像需重格式化）。VERSION = `0x00020000`。

## 1. 全局
- `block_size`：4096 或 **16384**（v2 必支持 16 KiB；4 KiB 仅为小镜像/兼容测试保留）。
- 块号：u32（0 .. 2^32-1 块）→ 4 KiB 时 16 TiB，16 KiB 时 **64 TiB**。
- 扇区/块换算：`block_sectors = block_size / 512`（4K=8，16K=32）。
- Superblock 布局不变（112 B，`<4s14I16s32sI`），仅 `version=0x00020000`。
- 位图：1 bit/块，`bitmap_blocks = ceil(total_blocks / (8*block_size))`，从块 3 起。
- 布局不变：块 0 引导；块 1 superblock 主；块 2 superblock 备；块 3 位图；其后 record 表；数据区从 `data_next`（向上）；元数据 `meta_next`（向下）；盘尾 journal（64 块）；末块 superblock 备份二。

## 2. Record（128 B，不变长）
| 偏移 | 大小 | 字段 | v2 说明 |
|---:|---:|---|---|
| 0 | 4 | oid | |
| 4 | 1 | type | |
| 5 | 1 | flags | |
| 6 | 2 | perm | 同 v1（16 位） |
| 8 | 4 | size_lo | 文件字节数低 32 位 |
| 12 | 4 | blocks_lo | 占用块数低 32 位 |
| 16 | 4 | parent | |
| 20 | 4 | ctime | |
| 24 | 4 | mtime | |
| 28 | 4 | kw_index | |
| 32 | 1 | n_ext | 内联 extent 个数 0..3 |
| 33 | 1 | inline_len | |
| 34 | 2 | inline_off | 36 |
| 36 | 36 | extents[3] | **每项 12 B：start u64（块号）+ count u32（块数）**（不再是 u16） |
| 72 | 4 | ext_index | block 号（u32） |
| 76 | 4 | n_ext_total | |
| 80 | 4 | next_free_oid | |
| 84 | 16 | owner | |
| 100 | 4 | size_hi | **文件大小高 32 位** |
| 104 | 4 | blocks_hi | **占用块数高 32 位** |
| 108 | 20 | reserved | 填 0 |

- `size = size_lo | (size_hi << 32)`；`blocks` 同理。
- 内联文件：`inline_len ≤ 92`，数据在 record[inline_off..]。
- extent：`(start, count)`，`start` 为分区内块号 u64（48 位 LBA 足够）；单 extent 最大 `count=2^32-1` 块 → 16 KiB 块时 = 64 TiB。
- `record_count = min(max(16, total_blocks // 64), 1<<20)`；Record 表占 `ceil(record_count*128/block_size)` 块。

## 3. 目录
- 目录项不变：`oid u32 | type u8 | name_len u8 | entry_len u16` + name + 4 对齐；按名排序。
- 单目录内容仍用 extent（当前实现单段连续）。

## 4. Journal（不变格式）
- 头 20 B：`magic 'KJRN' | seq u32 | flags u16 | offset u16 | target u32 | len u16 | crc16 u16`；
- payload = `block_size - 20`（4K→4076，16K→16364）。
- 事务 BEGIN → 数据片记录 → COMMIT → 应用 → END；恢复规则同 v1。

## 5. 实现要求（v2）
### 宿主 `tools/mkifs.py`
- `format IMG --size-mib N [--block-size 4096|16384] [--label X] [--record-cap N]`
  - 默认 block 4096（CI 小镜像），**大卷用 16384**（18T/64T 推荐）。
  - format 只写元数据区（bitmap/record/superblock/journal 头），数据区不写。
- 所有偏移/大小按 `block_size` 计算（不许再写死 4096）。
- size/blocks 用 u64 读写；extent 用 (u64,u32)。
- `journal` 子命令、断电注入 `KIFS_CRASH` 保持。

### 内核 `kernel/ata.asm`
- 新增 **LBA48** 读写：`ata_read_sectors48` / `ata_write_sectors48`（edx:eax = 64 位 LBA、ecx = 扇区数、edi/esi = 缓冲；返回 eax=0/-1）。
  - 命令 0x24/0x34；写 0x1F2..0x1F5 低 24 位、再写高 24 位；0x1F6 bit6=1。
- 保留原 LBA28 接口（FAT16/boot 路径不动）。
- 保持「每次命令 ≤16 扇区」策略，失败清数据口。

### 内核 `kernel/kifs.asm`
- 支持 v2：校验 `version == 0x00020000`；从 `block_size` 读块大小（4096/16384），校验 512 对齐。
- 块缓冲扩到最大 16 KiB（0x3C6000–0x3FFFFF 区域，pmem 池从 0x400000 起，别越界）。
- 块读写走 LBA48（`part_lba + block*block_sectors` 64 位计算）；journal payload 按块大小。
- Record 解析读 size_hi/blocks_hi；extent 解析 (u64 start,u32 count)。
- `kifs write` 单文件上限仍按内核缓冲（32 KiB）限制，但 **cat/stat 支持大文件**（流式，超大显示 size 数字即可）。
- 命令与行为兼容现有测试（test-kifs 断言按 v2 镜像更新）。

## 6. 测试
- 宿主 `test_mkifs.py`：4K 与 16K 各一套；u64 size 边界（写一个 >4 GiB 的稀疏逻辑文件？宿主测试可用小数据 + 伪造 size 字段验证解析）。
- 宿主 `test_journal.py`：5 种断电注入照旧（新增 16K 块）。
- 内核 `test-kifs`：v2 镜像（16K 块）读写；**大卷测试**：200 GiB 稀疏盘 QEMU 挂载，KiFS 分区放在 LBA 22528、容量 180 GiB，写入位于 >128 GiB 偏移的文件并读回（验证 LBA48）。
- C2 断电注入（QEMU kill -9）在 v2 镜像上重跑。

## 7. 实现偏差记录（宿主 v2，2026-11-15）

以下是规范落进 `tools/mkifs.py` 时明确下来的细节/偏差，实现以本节为准：

1. **64 TiB 差一块**：`total_blocks` 是 u32，最大 `2^32-1` 块；精确 64 TiB（67108864 MiB）
   需要 `2^32` 块，`format` 会拒绝（报「总块数 4294967296 超出 u32 块号范围」）。
   16 KiB 块实际单卷上限 = 64 TiB - 16 KiB；4 KiB 块 = 16 TiB - 4 KiB。
2. **内联数据覆盖 record 尾部字段**：`inline_off=36`、上限 92 B 时数据占 36..128，
   因此内联文件的 `ext_index`/`n_ext_total`/`owner`/`size_hi`/`blocks_hi` 不可解析；
   内联时 `size = inline_len`、`blocks = 0`、`extents = []`。fsck 与内核都按此规则。
3. **`data_start` 语义**：v2 固定为 3，指**位图起始块**；record 表紧随其后。
4. **record 表块数**：`ceil(record_count*128/block_size)`；`--record-cap N` 的 N 直接是槽数
   （上限 `1<<20`），不看卷大小。
5. **fsck 新增检查**（宿主已实现）：`version==0x00020000`、`block_size∈{4096,16384}`、
   位图/record 表/journal 区不重叠、`data_start==3`、superblock 三副本一致、
   `size`/`blocks` 与 extent 声明一致（`blocks == sum(count)`）、位图标记块数 =
   所有 extent 块数、目录项 type 与 record 一致、孤立 record。
6. **journal 满**：宿主把同事务相邻增量合并（位图连续字节合并成 1 条），
   64 块 journal 足以覆盖单次大文件写；仍超限时报错退出（v1 行为保留）。
7. **format 只写元数据**：位图块、record 表块、journal 64 个块头（BEGIN 槽清零/写 0）、
   3 份 superblock；数据区保持稀疏空洞。实测 15 TiB/16K 卷写 249 MiB、0.22 s。

# KiFS 内核驱动（kifs.asm）

`kernel/kifs.asm` 是 KiFS 在 32 位保护模式内核里的实现（x86，nasm `-f bin` 平铺内核，无分页依赖）。
当前为 **v2（2026-11-15）**：只读 + 写入（journal + CoW），支持
`block_size` 4096/16384（从 superblock 读）、u64 文件大小/块数、12 B extent、LBA48。
KKernel 的 `kifs-v2` 分支已用它通过 4 KiB / 16 KiB / LBA48 高地址三组内核侧 + 宿主侧交叉验证。

## 定位

- 磁盘布局默认固定在 **LBA 22528（11 MiB）** 起的连续分区，不解析分区表、不挂 VFS。
  分区 LBA 是编译期常量 `KIFS_PART_LBA`，可用 `nasm -DKIFS_PART_LBA=...` 覆盖
  （`test-kifs-big` 用它把卷放到 300000000，验证 64 位 LBA）。
- 块大小不写死：挂载时依次探 4 KiB / 16 KiB，从 superblock 读 `block_size` 并校验。
  块 I/O 走 **LBA48**：`64 位 LBA = KIFS_PART_LBA + block * block_sectors`。
- 盘上没有合法 KiFS 卷时 `kifs_init` **静默返回**（`kifs_ok=0`），不影响其他文件系统。
- 与 FAT16 各读各的分区，互不影响；KiFS 卷由宿主工具 [`tools/mkifs.py`](../tools/mkifs.py) 或内核自身写入。
- 依赖宿主内核提供：ATA PIO 读写（含 LBA48 版本）、shell 命令表/打印、管道 stdin。

> 限制：位图缓存目前只装 **一个块**，因此单卷最多 `8 * block_size` 个块
> （4 KiB → 32768 块/128 MiB；16 KiB → 131072 块/2 GiB）。更大的卷要改成多块位图，
> 当前未实现（宿主工具能格式化，内核会拒绝挂载）。

## 命令行

| 命令 | 说明 |
|---|---|
| `kifs info` | superblock：magic/version/块大小/总块数/root_oid/free_blocks/卷标/journal |
| `kifs ls [路径]` | 列目录（省略 = 根） |
| `kifs cat <路径>` | 打印文件（内联 / extent，流式，u64 大小） |
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

## v2 磁盘格式要点

- 版本 `0x00020000`；superblock 前 112 B 布局与 v1 相同
  （`block_size@8`、`total_blocks@12`、`data_start@20`、`record_start@40`、
  `record_count@44`、`free_blocks@52`、`label@76`、`crc32@108`）。
- Record 128 B：`size_lo@8` + `size_hi@100`、`blocks_lo@12` + `blocks_hi@104` 拼 u64；
  3 个 extent 从 `@36` 起、每个 12 B：`start u64@+0`、`count u32@+8`；`n_ext@32`、`inline_len@33`。
- **内联文件**：`inline_len > 0` 时 record 36..127 全是数据（最多 92 B），
  `size = inline_len`、`blocks = 0`、extent 为空；此时不能解析 `owner/size_hi/blocks_hi`。
- 记录表大小 `ceil(record_count*128/block_size)`；`record_count = min(max(16,total_blocks//64), 1<<20)`。
- 位图从块 3 开始；journal 固定最后 64 块，header 仍是 20 B，payload = `block_size - 20`
  （4 KiB → 4076，16 KiB → 16364）。

## 内存布局（与内核 0x3C0000 区共用；0x3C0000–0x3C2FFF 已被 UHCI/USB 占用）

| 范围 | 用途 |
|---|---|
| 0x3C6000–0x3C9FFF | 16 KiB 块缓冲 |
| 0x3CA000–0x3CDFFF | journal 块构建 / 目标块暂存（16 KiB） |
| 0x3D2000–0x3D32FF | 目录内容缓冲 / record / 路径缓冲 |
| 0x3E0000–0x3E3FFF | 位图缓存（16 KiB） |
| 0x3E4000–0x3EBFFF | 写入数据 / 目录内容 staging（32 KiB） |
| 0x3EC000–0x3ED1FF | superblock 影子 / delta 表 / 事务表 |

0x3D0000–0x3D1FFF 是页池位图、0x3C0000–0x3C2FFF 是 USB，**不要覆盖**；0x3C3000–0x3C5FFF 现在空闲。

## 写入语义

- 元数据（Record 128 B / 位图字节 / Superblock 三副本）走 journal 事务：
  `BEGIN → 数据片记录 → COMMIT → 应用 → END`（20 B 头 `KJRN` + CRC-16-CCITT + `block_size-20` B payload，64 槽环形）。
- **CoW**：文件数据块写新块（直接 ATA），旧块在事务内从位图释放；断电时新块是孤儿，旧数据/旧元数据始终完整。
- 16 KiB 块 = 32 个扇区，ATA 一次命令最多 16 扇区，所以块读写会拆成两个 chunk；
  每个 chunk 发命令前都要等 BSY 清零。

### LBA48 的两个坑（升级时踩过）

1. ATA LBA48 任务文件要 **先写高字节、再写低字节**：`0x1F2`(count 高) → `0x1F5`(LBA47:40) →
   `0x1F4`(LBA39:32) → `0x1F3`(LBA31:24)，然后再写低组 `0x1F2/0x1F3/0x1F4/0x1F5`。
   反过来先写低组时 QEMU 会返回 `status=0x41`（ERR+BSY），命令被拒。
2. 多 chunk 时，上一个 chunk 的最后一个扇区写完后驱动器还会忙一小会儿；下一条命令前
   必须等 BSY 清零，否则偶发 ATA 错误（16 KiB 块恰好两个 chunk，最容易撞上）。

## 测试

- 内核侧（KKernel `kifs-v2`）：
  - `make test-kifs`：4 KiB 块、4 MiB 卷，49 项断言（读写/journal/reboot 持久化/崩溃重放/宿主交叉验证）。
  - `make test-kifs16`：16 KiB 块、64 MiB 卷（128 MiB 盘），20 项断言；验证块大小从 superblock 读、record 128 条/块。
  - `make test-kifs-big`：`-DKIFS_PART_LBA=300000000` + 200 GiB 稀疏盘，卷在 **>128 GiB** 的 LBA，
    21 项断言；验证 LBA48 高地址的 read/write/cat/ls/stat/journal。
  - `make test-kifs-crash`：真断电注入（SIGKILL 循环）。
- 宿主交叉验证：测试后按真实分区 LBA 从镜像抽取卷，用 `tools/mkifs.py fsck` + `read` 做字节级比对。
- 宿主自测：`tools/test_mkifs.py`（228 项，4K+16K，含符号链接）、`tools/test_journal.py`
  （111 项断电注入，含符号链接创建/删除）。

## 已知偏差 / TODO

- `kifs_now` 恒 0：新建/修改文件的 ctime/mtime 写 0（宿主 fsck 不检查）。
- `kifs write <path> <text>` 的文本参数受内核 shell 行缓冲 128 字符限制；大文件请用管道 stdin。
- journal 64 槽环形复用，与宿主 mkifs.py 行为一致。
- 单块位图限制（见上）。
- **符号链接（type=3）**：本期未改内核驱动，宿主工具/FUSE 已支持（见
  [docs/kifs.md](../docs/kifs.md) §6.1）。参考驱动遇到 type=3 时的显示行为：
  `kifs ls` 把链接当普通项显示 `-`（不显示 `-> target`）、大小显示目标字节数；
  `kifs stat` 类型显示 `file`；`kifs cat` 报「不是文件」且**不跟随链接**。
- C2（真机断电注入压测）未做。

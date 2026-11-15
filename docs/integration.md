# 把 KiFS 集成进一个 32 位内核

本文以 KKernel（https://github.com/KaiXinLee114514/KKernel）为参考，说明 `kernel/kifs.asm` 的接入方式。
驱动本身是**自包含的 nasm 平铺代码**，不需要分页/虚拟内存支持，只需要宿主提供下面几个原语。

> **状态（2026-11-15）**：`kernel/kifs.asm` 已是 **v2** 实现：`version=0x00020000`、
> 从 superblock 读 `block_size`（4096/16384）、u64 文件大小/块数、extent `(start u64,count u32)`、
> journal payload = `block_size-20`，块 I/O 走 **LBA48**。KKernel 的 `kifs-v2` 分支用它通过了
> 4 KiB / 16 KiB / LBA48 高地址三组测试。旧 v1 卷需要宿主工具重新格式化或迁移。

## 1. 依赖的宿主原语

| 原语 | 参考实现（KKernel） | 说明 |
|---|---|---|
| ATA PIO 扇区读 | `kernel/ata.asm` 的 `ata_read_sectors` | LBA28，FAT16/引导仍用 |
| ATA PIO 扇区写 | `kernel/ata.asm` 的 `ata_write_sectors` | LBA28，修过寄存器保护 bug（FAT 提交 `b04e29a`） |
| **ATA LBA48 读** | `kernel/ata.asm` 的 `ata_read_sectors48` | `edx:eax` = 64 位 LBA，`ecx` = 扇区数，`edi` = 目标 |
| **ATA LBA48 写** | `kernel/ata.asm` 的 `ata_write_sectors48` | 同上，`esi` = 源；KiFS v2 的块 I/O 只用这两个 |
| 命令行框架 | `kernel/shell.asm` | 命令表 + 参数指针 + `term_print`/`term_putc`/`term_set_color` |
| 管道 stdin（可选） | `kernel/pipe.asm` 的 `j_pipe_read`/API 17 | `kifs write` 无文本参数时从管道读 |
| 初始化调用 | `kernel/kmain.asm` | 启动时 `call kifs_init`（在 ATA 初始化之后） |

LBA48 版本至少要支持 **每次命令 ≤16 扇区** 的分块、写后 FLUSH，并保持和 LBA28 版一致的失败返回（0 / -1）。

## 2. 接入步骤

1. 把 `kernel/kifs.asm` 复制到内核源码目录。
2. 在 `kernel/start.asm`（或等价的总 `%include` 文件）里，**在 ATA 驱动之后、shell 之前**加：
   ```
   %include "kifs.asm"
   ```
3. 在启动路径（如 `kmain.asm`）里，ATA 初始化完之后调用：
   ```
   call kifs_init
   ```
   `kifs_init` 会从 LBA 22528 起探 superblock；没有合法卷时静默返回（`kifs_ok=0`），不影响启动。
4. 在 shell 命令表里注册 `kifs` 命令（参考 KKernel 的 `cmd_table` 与 `cmd_kifs`），帮助文本加一行。
5. 确认内存布局：驱动使用 **0x3C6000–0x3CDFFF、0x3D2000–0x3D32FF、0x3E0000–0x3ED1FF**
   （16 KiB 块缓冲/journal、目录/record、位图、写入 staging、superblock/delta/事务表），
   不要与其他模块重叠（KKernel 里 0x3C0000–0x3C2FFF 是 UHCI/USB，0x3D0000–0x3D1FFF 是页池位图；
   注释里列出了每段用途）。
6. 如果内核有“页池常驻页数”之类的测试基线（KKernel 的 `expected_free_pages()`），确认新缓冲是否计入——KiFS 缓冲是静态内存，不是页池分配，通常不需要改。

分区 LBA 默认 22528，可用 `nasm -DKIFS_PART_LBA=...` 覆盖（`kifs.asm` 里用 `%ifndef` 包着）。

## 3. 准备磁盘镜像

两选一：

**A. 宿主生成（推荐开发期）**：用 `tools/mkifs.py` 造卷，`dd` 到磁盘镜像的分区 LBA。
v2 推荐 16 KiB 块（CI 小镜像可用 4 KiB）：

```bash
# 4 KiB 块小镜像（4 MiB）
python3 tools/mkifs.py format /tmp/kifs-part.img --size-mib 4
# 或 16 KiB 块（64 MiB 起，大卷更省元数据）
python3 tools/mkifs.py format /tmp/kifs-part.img --size-mib 64 --block-size 16384
python3 tools/mkifs.py mkdir  /tmp/kifs-part.img /docs
python3 tools/mkifs.py write  /tmp/kifs-part.img /docs/hello.txt --data "hello kifs"
dd if=/tmp/kifs-part.img of=build/hd.img bs=512 seek=22528 conv=notrunc
```

大卷（LBA48 测试）示例：`-DKIFS_PART_LBA=300000000` 编译内核，把卷 `dd` 到
200 GiB 稀疏盘的 300000000 号扇区（>128 GiB，32 位 LBA 必溢出，只能走 LBA48）。
注意内核位图缓存只有一块，单卷最多 `8*block_size` 个块（4K→128 MiB，16K→2 GiB）；
更大的卷需要先实现多块位图。

**B. 内核自己写**：卷是空的也行——`kifs_init` 需要合法 superblock，可先用宿主 format 一次，之后内核侧的
`kifs mkdir/write/rm` 会走 journal+CoW 自行维护。

> 注意：`mkifs.py` 的 superblock 主副本在分区块 **1**（块 0 是引导块留空）、`label@76`——设计文档
> docs/kifs.md 早期版本写的是块 0/`label@92`，以 `mkifs.py` 实现为准（驱动按此实现）。

## 4. 测试

- 宿主机自测：`python3 tools/test_mkifs.py`（192 项，4K+16K）、`python3 tools/test_journal.py`（91 项断电注入，4K+16K）。
- 内核侧（KKernel 参考）：
  - `make test-kifs` → 49 项断言：4 KiB 块，内核写路径（mkdir/write/cat/stat/ls/journal/rm）、
    reboot 持久化、崩溃盘重放/回收、宿主交叉验证。
  - `make test-kifs16` → 20 项断言：16 KiB 块 / 64 MiB 卷，同样的读写 + reboot + 宿主 fsck。
  - `make test-kifs-big` → 21 项断言：LBA48 高地址（卷在 >128 GiB 的 LBA），读写/cat/ls/stat/journal + 宿主 fsck。
  - `make test-kifs-crash` → 真断电注入压测。
  - **宿主交叉验证**：测试结束按真实分区 LBA 从镜像抽出卷 → `mkifs.py fsck` + `read` 字节比对。

### LBA48 集成坑（KKernel 升级记录）

1. ATA LBA48 任务文件要**先写高字节再写低字节**：`0x1F2`(count 高) → `0x1F5`(LBA47:40) →
   `0x1F4`(LBA39:32) → `0x1F3`(LBA31:24)，再写低组；先低后高时 QEMU 返回 `status=0x41`（ERR+BSY）。
2. 16 KiB 块 = 32 扇区，分两个 ATA 命令（每次 ≤16）；下一个 chunk 发命令前要等 BSY 清零，
   否则偶发 ATA 错误。

## 5. 已知偏差

- 时间戳：内核侧 `kifs_now` 恒 0（ctime/mtime 写 0），宿主 fsck 不检查。
- `kifs write <path> <text>` 文本参数受内核 shell 行缓冲限制（KKernel 为 128 字符），大文件用管道。
- journal 64 槽环形复用，与宿主工具一致；payload 长度 = `block_size-20`（v2）。
- v2 内联 record：`inline_len>0` 时 record 36..128 全是文件内容，`size=inline_len`、`blocks=0`，
  不要解析 owner/size_hi/extent。
- 内核位图缓存只有一块：单卷上限 `8*block_size` 个块。

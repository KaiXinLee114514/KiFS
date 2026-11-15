# 把 KiFS 集成进一个 32 位内核

本文以 KKernel（https://github.com/KaiXinLee114514/KKernel）为参考，说明 `kernel/kifs.asm` 的接入方式。
驱动本身是**自包含的 nasm 平铺代码**，不需要分页/虚拟内存支持，只需要宿主提供下面几个原语。

> ⚠️ **格式 v2（2026-11-15）**：本仓库 `kernel/kifs.asm` 目前仍是 **v1** 实现，
> 只认 `version=0x00010000` 与 4096 B 块。宿主 `mkifs.py` 现在默认生成 **v2**
> （`0x00020000`）镜像，v1 驱动会拒绝。内核侧需要按
> [docs/kifs-v2.md](kifs-v2.md) 升级：读 `block_size`、16 KiB 块缓冲、LBA48、
> Record 的 `size_hi/blocks_hi`、extent `(start u64,count u32)`、journal payload=块大小-20。
> 在升级完成前，内核测试要自己造 v1 镜像，或把 `MKIFS_VERSION` 之类的旧参数留一份。

## 1. 依赖的宿主原语

| 原语 | 参考实现（KKernel） | 说明 |
|---|---|---|
| ATA PIO 扇区读 | `kernel/ata.asm` 的 `ata_read_sectors` | 读 LBA 开始的若干 512 B 扇区 |
| ATA PIO 扇区写 | `kernel/ata.asm` 的 `ata_write_sectors` | 修过寄存器保护 bug（FAT 提交 `b04e29a`），照现有调用约定 |
| 命令行框架 | `kernel/shell.asm` | 命令表 + 参数指针 + `term_print`/`term_putc`/`term_set_color` |
| 管道 stdin（可选） | `kernel/pipe.asm` 的 `j_pipe_read`/API 17 | `kifs write` 无文本参数时从管道读 |
| 初始化调用 | `kernel/kmain.asm` | 启动时 `call kifs_init`（在 ATA 初始化之后） |

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
   `kifs_init` 会校验 LBA 22528 处的 superblock；没有合法卷时静默返回（`kifs_ok=0`），不影响启动。
4. 在 shell 命令表里注册 `kifs` 命令（参考 KKernel 的 `cmd_table` 与 `cmd_kifs`），帮助文本加一行。
5. 确认内存布局：驱动使用 **0x3C3000–0x3CFFFF 和 0x3D2000–0x3D32FF**，不要与其他模块重叠
   （KKernel 里 0x3C0000–0x3C2FFF 是 UHCI/USB 占用；按自身内核布局调整常量即可，注释里列出了每段用途）。
6. 如果内核有“页池常驻页数”之类的测试基线（KKernel 的 `expected_free_pages()`），确认新缓冲是否计入——KiFS 缓冲是静态内存，不是页池分配，通常不需要改。

## 3. 准备磁盘镜像

两选一：

**A. 宿主生成（推荐开发期）**：用 `tools/mkifs.py` 造卷，`dd` 到磁盘镜像的 LBA 22528。
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

大卷（LBA48 测试）示例：`--size-mib 15728640 --block-size 16384` 生成 15 TiB 稀疏卷，
实盘只占 ~250 MiB；内核要用 LBA48 才能寻址超过 128 GiB 的偏移。

**B. 内核自己写**：卷是空的也行——`kifs_init` 需要合法 superblock，可先用宿主 format 一次，之后内核侧的
`kifs mkdir/write/rm` 会走 journal+CoW 自行维护。

> 注意：`mkifs.py` 的 superblock 主副本在分区块 **1**（块 0 是引导块留空）、`label@76`——设计文档
> docs/kifs.md 早期版本写的是块 0/`label@92`，以 `mkifs.py` 实现为准（驱动按此实现）。

## 4. 测试

- 宿主机自测：`python3 tools/test_mkifs.py`（192 项，4K+16K）、`python3 tools/test_journal.py`（91 项断电注入，4K+16K）。
- 内核侧（KKernel 参考）：`make test-kifs` → 48 项断言，包括：
  - 内核写路径（mkdir/write/cat/stat/ls/journal/rm）
  - reboot 后持久化
  - 崩溃盘制造 → 挂载重放已提交事务 / 回收残缺事务
  - **宿主交叉验证**：测试结束 dd 出 LBA 22528 起分区 → `mkifs.py fsck` + `read` 字节比对

## 5. 已知偏差

- 时间戳：内核侧 `kifs_now` 恒 0（ctime/mtime 写 0），宿主 fsck 不检查。
- `kifs write <path> <text>` 文本参数受内核 shell 行缓冲限制（KKernel 为 128 字符），大文件用管道。
- journal 64 槽环形复用，与宿主工具一致；payload 长度 = `block_size-20`（v2）。
- v2 内联 record：`inline_len>0` 时 record 36..128 全是文件内容，`size=inline_len`、`blocks=0`，
  不要解析 owner/size_hi/extent。

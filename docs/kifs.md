# KiFS 设计稿（Kai File System）

> 版本 0.1 · 2026-11-14 · 状态：设计定稿（尚未实现）
>
> 一句话：KiFS 是一个以「对象 + 倒排关键词索引」为核心的 4K 块文件系统——
> 目录只是视图，语义查询是一等公民。

---

## 1. 目标与非目标

### 目标（v1 要有的）
- 文件 / 目录 / 读写，4 KiB 块，支持大文件（extent）
- 小文件内联（不占数据块）
- 断电一致性（redo journal）
- 每文件权限位：`rwxrwxrwx` + `hide` / `ro` 等属性位
- 关键词倒排索引（keywords 由外部 / AI 写入），支持语义查询
- 磁盘布局简单到可以写两份独立实现：`tools/mkifs.py`（宿主工具）与内核驱动，
  双实现互相校验

### 非目标（v1 不做，格式预留）
- 快照 / 写时复制（后续 Phase E）
- 多用户 / ACL / 配额（v1 只有单用户视角，权限位仅 honor）
- 压缩、加密、网络文件系统

---

## 2. 术语

| 术语 | 含义 |
|---|---|
| **OID** | Object ID，32 位对象编号，0 保留 |
| **Record** | 元数据记录（相当于 inode），按 OID 索引 |
| **Superindex** | 顶层索引：OID → Record（固定表，元数据区） |
| **Subindex** | 大目录的分级名字索引（v1 不实现，格式预留） |
| **Extent** | 一段连续的数据块 `{start, count}` |
| **Journal** | 盘尾的 redo 环形日志 |
| **Keyword** | 关键词串 → 倒排 OID 表 |

---

## 3. 磁盘布局

```
偏移（块号）        内容                              增长方向
----------------------------------------------------------------
块 0             KKernel 引导扇区（KiFS 不碰）
块 1             Superblock 主副本（512 B）
块 2             Superblock 备份
块 3 起           块位图（1 bit / 块）                ↓ 向上
                  Record 表（固定槽，128 B × N）        ↓ 向上
                  数据块区（文件内容）                 ↓ 向上
                  ~~~~~~~ 空闲空间 ~~~~~~~
                  关键词串池 + 倒排表                  ↑ 向下
                  Subindex 区（预留）                  ↑ 向下
                  Journal（固定预留，环形）             ↑ 向下
最后一块          Superblock 备份 2
```

- 两个游标：`data_next` 从盘头往上长，`meta_next` 从盘尾往下长；`data_next >= meta_next` 即满盘。
- 全部**小端**。
- 块大小固定 4096 B；盘按块编号寻址（`lba = part_lba + block * 8`）。

---

## 4. Superblock（512 B）

| 偏移 | 大小 | 字段 | 说明 |
|---:|---:|---|---|
| 0 | 4 | `magic` | `'KIFS'` = `0x5346494B`（小端读） |
| 4 | 4 | `version` | `0x0001_0000`（v1.0） |
| 8 | 4 | `block_size` | 4096 |
| 12 | 4 | `total_blocks` | 分区总块数 |
| 16 | 4 | `part_lba` | 本分区起始 LBA（供宿主工具用） |
| 20 | 4 | `data_start` | 数据/位图区起始块号 |
| 24 | 4 | `data_next` | 下一个空闲数据块（向上游标） |
| 28 | 4 | `meta_next` | 下一个空闲元数据块（向下游标） |
| 32 | 4 | `journal_start` | Journal 起始块号 |
| 36 | 4 | `journal_blocks` | Journal 预留块数 |
| 40 | 4 | `record_start` | Record 表起始块号 |
| 44 | 4 | `record_count` | Record 槽数 |
| 48 | 4 | `root_oid` | 根目录 OID = 1 |
| 52 | 4 | `free_blocks` | 空闲数据块数（冗余计数，可重算） |
| 56 | 4 | `flags` | 0x1=clean，0x2=需要 fsck |
| 60 | 16 | `volume_uuid` | 卷 UUID（v4 随机） |
| 76 | 32 | `label` | 卷标，UTF-8，NUL 结尾 |
| 108 | 4 | `checksum` | 前 108 B 的 CRC32（0 表示不校验） |
| 112 | 400 | `reserved` | 填 0 |

- 块 1 主副本，块 2 备份；盘尾倒数第 1 块再存一份（防头部损坏）。
- 校验和覆盖 `[0, 108)`；superblock 每次只整体写（<单扇区），天然原子。

---

## 5. Record（元数据记录，128 B × `record_count`）

Record 表放在块 1 之后固定区域：`record_start` 起，槽号即 OID。

| 偏移 | 大小 | 字段 | 说明 |
|---:|---:|---|---|
| 0 | 4 | `oid` | 自身 OID（冗余校验，应等于槽号） |
| 4 | 1 | `type` | 1=file，2=dir，3=symlink（预留） |
| 5 | 1 | `flags` | 0x01=system，其余预留（hide/ro/iolock 统一放 `perm` 里） |
| 6 | 2 | `perm` | 16 位权限/属性字：bit0-8 = `rwxrwxrwx`，bit9 = hide，bit10 = ro，bit11 = iolock，bit12-15 预留 |
| 8 | 4 | `size` | 文件字节数 |
| 12 | 4 | `blocks` | 实际占用数据块数 |
| 16 | 4 | `parent` | 父目录 OID（根为 0） |
| 20 | 4 | `ctime` | 创建时间（Unix 秒，v1 用 RTC 近似） |
| 24 | 4 | `mtime` | 修改时间 |
| 28 | 4 | `kw_index` | 关键词倒排引用（0=无） |
| 32 | 1 | `n_ext` | 内联 extent 个数（0..4） |
| 33 | 1 | `inline_len` | 内联文件长度 0..92 |
| 34 | 2 | `inline_off` | 内联数据在记录内的偏移（默认 36） |
| 36 | 36 | `extents[3]` | 每个 extent：`start u32 + count u16`，最多 3 个内联 |
| 72 | 4 | `ext_index` | 超出内联的 extent 表所在块号（0=无） |
| 76 | 4 | `n_ext_total` | extent 总数 |
| 80 | 4 | `next_free_oid` | 预留（链式扩展用） |
| 84 | 16 | `owner` | 所有者用户名（UTF-8，NUL 填充，最多 15 字符；v1 单用户也记录） |
| 100 | 28 | `reserved` | 填 0 |

- 内联上限 92 B（v1），≤92 B 的文件直接躺在 record 里。
- 文件 > 92 B：用 extent 段；≤3 段全部内联，更多则 `ext_index` 指向数据区里的一张
  `u32 u16` 数组块。
- Record 表在 format 时固定 `record_count`（默认盘大小的 1/64，上限 4096）。

---

## 6. 目录格式

目录 = 它的数据块里紧凑存放的有序目录项（按名字字节序）：

| 偏移 | 大小 | 字段 |
|---:|---:|---|
| 0 | 4 | `oid` |
| 4 | 1 | `type` |
| 5 | 1 | `name_len` |
| 6 | 2 | `entry_len`（本项总长，4 对齐） |
| 8 | … | `name`（UTF-8，不 NUL 结尾，`name_len` 字节） |
| … | 0-3 | 补齐到 4 字节 |

- 查找：顺序扫描（v1）；目录大于 64 KiB 后启用 Subindex 分级（预留）。
- `entry_len - 8 - name_len` 的尾部字节为 0。
- 目录项 `type` 与 record 的 `type` 应一致，不一致视为损坏（fsck 报）。
- 根目录（OID=1）的 `parent=0`，名字为空。

---

## 7. 块分配（位图 + extent）

- **块位图**：1 bit / 块，从 `data_start` 块起的固定区域；bit=1 已用。
- 分配：**next-fit**，从 `data_next` 向后找连续空闲段；到盘尾回绕一次。
- 释放：清位图位 + `free_blocks` 计数。
- extent 合并：相邻 extent 自动合并；不移动数据。
- 小文件内联（≤92 B）不占块。
- 数据区与元数据区之间的空闲块由两个游标共同维护。

---

## 8. Journal（redo，盘尾固定预留）

- 预留 `journal_blocks`（默认 64 块 = 256 KiB），环形。
- 块头（每块开头 12 B）：`magic 'KJRN' (4) + seq u32 (4) + flags u16 (2) + count u16 (2)`。
- 记录（可变长、按 4 对齐）：

| 字段 | 大小 | 说明 |
|---|---:|---|
| `type` | 1 | 1=BEGIN，2=BLOCK，3=COMMIT |
| `len` | 2 | 负载长度 |
| `target_block` | 4 | 要写的块号（BLOCK） |
| `offset` | 2 | 块内偏移 |
| `data_blocks` | 2 | （运行期）负载占几块 |
| `payload` | … | 要写的字节 |

- 协议：
  1. 写 BEGIN（带事务 seq）
  2. 写所有 BLOCK 记录（含要落盘的数据/元数据块内容）
  3. 写 COMMIT
  4. 真正写目标块
  5. 写 END / 更新 head 游标
- 挂载时：从头扫，若发现完整 BEGIN…COMMIT 段 → 重放（目标块写回）；不完整段丢弃。
- 副作用：v1 每次写文件至少多写一遍 journal；速度换安全，后续可选 `barrier=0` 关闭。

---

## 9. 权限与属性（`rwxrwxrwx-hide-ro-iolock-owner`）

权限字 `perm` 是 16 位，布局：

| 位 | 字段 | 说明 |
|---:|---|---|
| bit0-2 | `rwx`（owner） | 所有者读/写/执行 |
| bit3-5 | `rwx`（group） | 同组读/写/执行（v1 预留） |
| bit6-8 | `rwx`（other） | 其他读/写/执行 |
| bit9 | `hide` | ls 默认不列，`ls -a` 可见 |
| bit10 | `ro` | 只读：写/删/改名返回 `EACCES` |
| bit11 | `iolock` | 被锁：运行期独占标记（见下） |
| bit12-15 | 预留 | 填 0 |

配合 Record 里的 `owner`（16 B 用户名，如 `kai`），完整展示格式（紧凑式，固定 12 字符头 + 用户名）：

```
rwxrwxrwx111kai
```

- 前 9 字符 = `rwxrwxrwx`
- 接着 3 个数字位 = `hide ro iolock`（顺序固定，1=置位，0=未置位）
- 余下即 `owner` 用户名

例：`rw-r--r--010kai` = owner 可读写、其他只读、ro=1、iolock=0、owner=kai。
（`iolock` 是运行期状态位，展示/调试用）

语义：
- **hide**：默认目录列表不显示；显式路径访问仍然可以。
- **ro**：只读；v1 无用户系统也会 honor（写 → `EACCES`）。
- **iolock**：每文件一个锁位。打开/写入时置位，关闭时清位；掉电遗留的 iolock
  由 fsck 清掉。v1 单进程写，主要用于将来多 shell / 多线程并发时的互斥，
  以及 `ls -l` 展示「这个文件正被占用」。
- 非 owner 的访问检查 v1 不做（单用户），但字段先留好。
- **cache 组**（运行期 RAM，不落盘）：数据缓存 200 块 + 元数据缓存 50 块（待定），
  LRU 替换；类似 FAT16 的 `FAT_BUF`/`fat_cache_lba`，但按块管理。

---

## 10. 关键词与查询（KiFS 的灵魂）

- **关键词串池**：唯一字符串按字典序存放在元数据区（向下生长），每串带 refcount。
- **倒排表**：`keyword → 有序 OID 数组（u32 升序）`，存储在元数据区；删除时懒标记。
- 写入 API：创建/更新文件时附带 `keywords[]`；也允许事后 `tag` 命令追加。
- **查询**（外部解析器 → 内核结构化 API）：
  - `"Last Summer family photos"` →
    `{time:[2025-06-01, 2025-09-30], type:image/*, keywords:[family]}`
  - 内核先做关键词交集（OID 数组求交），再按 record 的时间范围 / 类型过滤。
- 终局：**目录只是视图**；所有对象都在 OID 空间里，目录树只是其中一种索引。
  这正是我们聊 WinFS 时说的「倒排索引才是核心」。

---

## 11. 与 FAT16 对比

| 项目 | FAT16（现状） | KiFS（设计） |
|---|---|---|
| 分配 | 簇链（FAT 表） | 位图 + extent |
| 小文件 | 至少 1 簇 | 内联 ≤92 B |
| 长名 | VFAT LFN | 目录项直接 UTF-8 名 |
| 断电一致性 | 无 | redo journal |
| 权限 | 无 | perm + hide/ro |
| 查询 | 无 | 关键词倒排 + 语义查询 |
| 目录 | 扁平目录项 | 有序目录项（Subindex 预留） |
| 实现成本 | 低 | 中（但宿主 + 内核双实现可互验） |

**迁移路径**：先做 `tools/mkifs.py` 生成镜像 + `fsck` 校验；再给 KKernel 加
**只读挂载**（`mount KIFS` → `/MNT/KIFS`）；最后加写入 + journal。FAT16 继续保留
（引导盘 + 兼容），KiFS 作为第二分区。

---

## 12. 实现路线（版本裁剪）

- **Phase A（宿主原型，不碰内核）**：`tools/mkifs.py`
  - format / mkdir / write / read / ls / 小文件内联 / extent 分配 / fsck
  - Python 单文件，输出 4 MiB 测试镜像；与设计表逐字节对齐
- **Phase B（内核只读）**：`kernel/kifs.asm`
  - mount（校验 superblock + journal 重放判定）、ls、cat、按关键词过滤的只读查询
  - 挂载点 `/MNT/KIFS`（`/MNT` 目录已存在）
- **Phase C（内核写入）**：写文件 + extent 分配 + journal + 断电注入测试
- **Phase D（关键词）**：tag 命令 + 倒排表构建/查询；语义查询解析器可放外部工具
- **Phase E（后续）**：快照/CoW、Subindex 树、压缩、加密

---

## 13. 常量速查

| 项 | 值 |
|---|---|
| magic | `b'KIFS'` |
| 版本 | `0x0001_0000` |
| 块大小 | 4096 |
| record 大小 | 128 |
| perm 字 | 16 位：rwxrwxrwx + hide + ro + iolock |
| owner | 16 B 用户名（UTF-8，NUL 填充） |
| 内联上限 | 92 B |
| 内联 extent 上限 | 3 个 |
| 目录项头 | 8 B，名字 UTF-8，4 对齐 |
| journal 头 | `b'KJRN'` + seq + flags + count |
| 默认 journal | 64 块（256 KiB） |
| 默认 record 槽 | 盘块数/64，上限 4096 |

---

## 14. 未决问题（实现前再定）

1. Record 表固定槽 vs 动态分配（固定简单但浪费；v1 先固定）
2. 目录项排序用字节序还是大小写不敏感序
3. extent 索引块（超过 3 段）是否也走 journal
4. journal checkpoint / head-tail 回收策略（v1：满则强制落盘+重置）
5. keywords 删除/更新的空间回收
6. KiFS 与 FAT16 的挂载原语（`mount` 命令 + VFS 层抽象）
7. 断电测试怎么做：QEMU `-snapshot` + 杀进程 + 重新挂载 fsck
8. owner 直存用户名（v1）vs 以后改 uid 表（需要用户系统）
9. group/other 的 rwx 在单用户 v1 是否只展示不强制（倾向只展示）

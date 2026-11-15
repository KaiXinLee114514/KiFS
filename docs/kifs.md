# KiFS 设计稿（Kai File System）

> 版本 0.1 · 2026-11-14 · 状态：设计定稿（v2 已实现；v2.1 增加符号链接）
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

- 两个游标：`data_next` 从盘头往上长，`meta_next` 从盘尾往下长；`data_next > meta_next` 即满盘。
- 全部**小端**。
- 块大小 `block_size` ∈ {4096, 16384}（v2）：`block_sectors = block_size / 512`（4K=8，16K=32）；
  盘按块编号寻址 `lba = part_lba + block * block_sectors`（v2 内核需 LBA48）。
- `bitmap_blocks = ceil(total_blocks / (8*block_size))`，Record 表紧随位图，
  占 `ceil(record_count*128/block_size)` 块；`data_start` 指位图起始块（=3）。
- 块号是 u32：4 KiB 块时单卷最大 16 TiB，16 KiB 块时 64 TiB - 16 KiB（`total_blocks` 装不下 2^32）。

---

## 4. Superblock（512 B）

| 偏移 | 大小 | 字段 | 说明 |
|---:|---:|---|---|
| 0 | 4 | `magic` | `'KIFS'` = `0x5346494B`（小端读） |
| 4 | 4 | `version` | `0x0002_0000`（v2.0；v1 镜像不再支持） |
| 8 | 4 | `block_size` | 4096 / 16384 |
| 12 | 4 | `total_blocks` | 分区总块数（u32，最大 `2^32-1`） |
| 16 | 4 | `part_lba` | 本分区起始 LBA（供宿主工具用） |
| 20 | 4 | `data_start` | 位图起始块号（v2 固定为 3） |
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
- superblock 位置随块大小移动：4 KiB 块在字节 4096，16 KiB 块在字节 16384；探测时
  两者都试，版本、块大小、CRC 都对上才算数。

---

## 5. Record（元数据记录，128 B × `record_count`）

Record 表放在位图之后：`record_start` 起，槽号即 OID。v2 用 `size_lo/size_hi` +
`blocks_lo/blocks_hi` 拼 u64，extent 改为 `(start u64, count u32)`。

| 偏移 | 大小 | 字段 | 说明 |
|---:|---:|---|---|
| 0 | 4 | `oid` | 自身 OID（冗余校验，应等于槽号） |
| 4 | 1 | `type` | 1=file，2=dir，3=symlink（v2.1 已实现） |
| 5 | 1 | `flags` | 0x01=system，其余预留 |
| 6 | 2 | `perm` | 16 位权限/属性字：bit0-8 = `rwxrwxrwx`，bit9 = hide，bit10 = ro，bit11 = iolock |
| 8 | 4 | `size_lo` | 文件字节数低 32 位 |
| 12 | 4 | `blocks_lo` | 占用块数低 32 位 |
| 16 | 4 | `parent` | 父目录 OID（根为 0） |
| 20 | 4 | `ctime` | 创建时间（Unix 秒） |
| 24 | 4 | `mtime` | 修改时间 |
| 28 | 4 | `kw_index` | 关键词倒排引用（0=无） |
| 32 | 1 | `n_ext` | 内联 extent 个数（0..3） |
| 33 | 1 | `inline_len` | 内联文件长度 0..92 |
| 34 | 2 | `inline_off` | 内联数据在记录内的偏移（默认 36） |
| 36 | 36 | `extents[3]` | **每项 12 B：`start u64 + count u32`**，最多 3 个内联 |
| 72 | 4 | `ext_index` | 超出内联的 extent 表所在块号（0=无；表项同样 12 B/项） |
| 76 | 4 | `n_ext_total` | extent 总数 |
| 80 | 4 | `next_free_oid` | 预留（链式扩展用） |
| 84 | 16 | `owner` | 所有者用户名（UTF-8，NUL 填充，最多 15 字符） |
| 100 | 4 | `size_hi` | 文件字节数高 32 位 |
| 104 | 4 | `blocks_hi` | 占用块数高 32 位 |
| 108 | 20 | `reserved` | 填 0 |

- `size = size_lo | (size_hi << 32)`；`blocks = blocks_lo | (blocks_hi << 32)`。单文件最大 16 EiB（格式上限），实际受卷大小限制。
- extent：`start` 为分区内块号 u64（LBA48 足够），`count` u32；单 extent 最大
  `2^32-1` 块 → 16 KiB 块时 64 TiB。
- 内联上限 92 B（`inline_off=36`，数据覆盖 36..128）。**注意**：内联时
  `extents`/`ext_index`/`n_ext_total`/`owner`/`size_hi`/`blocks_hi` 这些槽位都是文件内容，
  不能解析；内联文件的 size 一律等于 `inline_len`，blocks=0，owner 无记录。
- Record 表在 format 时固定：`record_count = min(max(16, total_blocks // 64), 1<<20)`，
  `--record-cap N` 可覆盖（N ≤ 1M）。1M 槽 = 128 MiB 表，对应 64 GiB 以上卷不再增长。
- 数据块区里 `ext_index` 表（>3 段时）按 12 B/项存放，也走位图/CoW。

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

- 查找：顺序扫描；目录大于 64 KiB 后启用 Subindex 分级（预留）。
- `entry_len - 8 - name_len` 的尾部字节为 0。
- 目录项 `type` 与 record 的 `type` 应一致，不一致视为损坏（fsck 报）。
- 根目录（OID=1）的 `parent=0`，名字为空。

### 6.1 符号链接（type=3，v2.1）

- **存储**：record 的 `type=3`；内容就是目标路径的 UTF-8 字节（不含结尾 NUL），
  `size` = 目标字节数；内容完全复用文件内容存储——≤92 B 内联，更长为 extent
  （宿主一次写一个连续 extent）。因为内联会覆盖 record 尾部字段，内联符号链接的
  `owner` 为空，与内联文件一致。
- **目标上限**：1024 B；超过即拒绝（宿主工具报错，FUSE 返回 `ENAMETOOLONG`）。
- **语义**：目标可以是绝对或相对路径，**允许 dangling**（指向不存在的对象）；
  文件系统层不做路径解析/跟随——`readlink` 只读出原始字节，跟随由 VFS（Linux
  FUSE 挂载）完成。因此目录项里只要 `type` 一致即可，fsck 也不会去解析目标。
- **权限**：默认 `perm=0777`；FUSE 里 `st_mode` 恒为 `S_IFLNK|0777`，`st_size` 为
  目标字节数，`st_nlink=1`。`chmod` 对符号链接本身没有意义（VFS 通常先跟随）。
- **删除**：`unlink` 只删除链接 record + 目录项，不触碰目标对象。
- **防御**：即使 VFS 已解析，驱动仍对 `open`/`read`/`write`/`truncate` 直接指向
  符号链接的请求返回 `EINVAL`；`readdir` 对 type=3 项上报 `DT_LNK`。
- **fsck**：接受 type=3，并检查目标 ≤1024 B、无 NUL、是合法 UTF-8；dangling 不算错误。
- **FAT16 对比**：FAT 目录项没有「链接」类型，无法表达 `ln -s`；KiFS 直接复用
  record + 目录项 + 内容存储，代价是 1 个 record 槽（目标 ≤92 B 时不占数据块）。
- **宿主命令**：`python3 tools/mkifs.py symlink IMG /path --target TARGET [--perm ...]`、
  `python3 tools/mkifs.py readlink IMG /path`；`ls` 显示 `l` 前缀和 `-> target`，
  `stat` 显示 `type: symlink` 与 `target:`。

---

## 7. 块分配（位图 + extent）

- **块位图**：1 bit / 块，从 `data_start` 块起，占 `ceil(total_blocks/(8*block_size))` 块；bit=1 已用。
- 分配：**next-fit**，从 `data_next` 向后找连续空闲段；释放后 `data_next` 回退到最小释放块。
- 释放：清位图位 + `free_blocks` 计数。
- extent 合并：相邻 extent 自动合并；不移动数据。
- 小文件内联（≤92 B）不占块。
- 数据区与元数据区之间的空闲块由两个游标共同维护；fsck 校验位图标记总数 = 所有 extent 声明块数。
- 宿主实现一次写只用一个连续 extent（数据区够大即可）；读取/fsck 兼容 `ext_index` 多段。

---

## 8. Journal（redo，盘尾固定预留）

- 预留 `journal_blocks`（默认 64 块；4 KiB 块 = 256 KiB，16 KiB 块 = 1 MiB），环形；块号 `journal_start + i`。
- **一个 journal 块承载一条记录**（简化：逐块 CRC、易做断电注入）：

| 偏移 | 大小 | 字段 | 说明 |
|---:|---:|---|---|
| 0 | 4 | `magic` | `'KJRN'` |
| 4 | 4 | `seq` | 事务序号，单调递增 |
| 8 | 2 | `flags` | bit0 VALID，bit1 BEGIN，bit2 COMMIT，bit3 END |
| 10 | 2 | `offset` | 数据片在目标块内偏移（BEGIN/COMMIT/END 为 0） |
| 12 | 4 | `target_block` | 目标块号（BEGIN/COMMIT/END 为 0） |
| 16 | 2 | `len` | payload 长度（≤ block_size-20：4K→4076，16K→16364） |
| 18 | 2 | `crc16` | CRC-16/CCITT（poly 0x1021，init 0xFFFF）覆盖 payload |
| 20 | block_size-20 | `payload` | 数据片 |

- 大块拆分：一个目标块/连续区间的数据按 `block_size-20` 切片，逐片记录（同 seq、同 target、offset 递增）；恢复时按 (target, offset) 排序应用。
- 宿主实现会把同事务里相邻的 after-image 增量合并成一条记录（位图连续字节尤其重要，
  否则一次大分配就能写满 64 块 journal）；合并后仍满足上述格式。
- 事务协议：
  1. `BEGIN(seq)` 写一个 journal 块
  2. 逐条写数据片记录（看情况 1~N 块）
  3. `COMMIT(seq)` 写一个 journal 块
  4. **应用**：按记录把数据写回目标块（顺序无关，重放幂等）
  5. `END(seq)`：覆盖写回 BEGIN 那个 journal 槽（flags=VALID|END），表示已应用完成
- 打开镜像/挂载时：扫全部 journal 块 → 按 seq 聚合；**只有 BEGIN+COMMIT 完整、且所有数据片 CRC16 有效的事务才重放**；不完整/撕裂事务直接丢弃。
  - 「已应用但没写 END」时崩溃 → 下次打开再应用同样内容，幂等无害。
  - 「只有 COMMIT 没有 END」的完整事务一律重放（幂等），保证崩溃恢复不需要额外 checkpoint 指针。
- **CoW 写路径（v1）**：
  - 文件数据块不原地改：分配新块、写完新数据后才提交元数据事务；旧块在事务里从位图释放。
  - 元数据只走 journal 增量：Record 128 B、位图变化的字节、Superblock 112 B（主+块 2+末块备份）。
  - 因此断电时：新数据块 = 孤儿（位图未标、fsck 回收）；旧数据/旧元数据始终完整。
- 断电注入（宿主原型）：`KIFS_CRASH=after_begin|after_records|after_commit|after_apply|torn` 在对应阶段直接退出/写坏记录，验证恢复。
- 副作用：每次写至少多写 2+ 个 journal 块；速度换安全，后续可加 `barrier=0`。

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
| 符号链接 | 无（目录项没有链接类型） | type=3，目标复用文件内容存储（≤1024 B） |
| 实现成本 | 低 | 中（但宿主 + 内核双实现可互验） |

**迁移路径**：先做 `tools/mkifs.py` 生成镜像 + `fsck` 校验；再给 KKernel 加
**只读挂载**（`mount KIFS` → `/MNT/KIFS`）；最后加写入 + journal。FAT16 继续保留
（引导盘 + 兼容），KiFS 作为第二分区。

---

## 12. 实现路线（版本裁剪）

- **Phase A（宿主原型，不碰内核）**：`tools/mkifs.py`
  - format / mkdir / write / read / ls / 小文件内联 / extent 分配 / fsck
  - **A2（已完成，2026-11-15）**：journal redo + CoW 写入路径 + 断电注入自测
  - **A3 = v2（已完成，2026-11-15）**：16 KiB 块、u64 文件大小、extent `(u64,u32)`、
    record 上限 1M、大卷稀疏 format（15 TiB 只写 ~249 MiB 元数据）。
    自测：`tools/test_mkifs.py` 192 项（4K+16K）、`tools/test_journal.py` 91 项
    （4K+16K × 5 种断电注入）。
  - **A3.1 = 符号链接（已完成，2026-11-15）**：record type=3，目标路径复用
    inline/extent 存储、上限 1024 B、允许 dangling；`mkifs symlink/readlink` +
    FUSE `symlink/readlink/getattr/readdir`。自测：`test_mkifs.py` 228 项、
    `test_journal.py` 111 项（新增符号链接创建/删除断电注入）。
  - Python 单文件；CI 用 4 KiB 块小镜像，大卷用 16 KiB 块；与设计表逐字节对齐
- **Phase B（内核只读）**：`kernel/kifs.asm`
  - **B0（已完成，2026-11-14，v1 格式）**：KKernel 里的只读驱动 + `kifs` 命令组 ——
    `kifs info` / `ls [路径]` / `cat <路径>` / `stat <路径>`；superblock 校验
    （魔数 / 版本 / CRC32）、目录项逐段遍历、内联 + extent 读取。**固定 LBA 22528**、
    只读；宿主镜像 dd 到 16 MB 盘 FAT16 分区之后自测（`make test-kifs`）
  - **B1（v2，待做）**：按 v2 规格升级内核驱动：从 superblock 读 `block_size`、
    16 KiB 块缓冲、LBA48、u64 size/extent。⚠️ v1 驱动读到 v2 镜像会因版本不符拒绝，
    升级前 `make test-kifs` 需继续用 v1 镜像或同步升级
  - B2：mount（校验 superblock + journal 重放判定）、按关键词过滤的只读查询、挂载点 `/MNT/KIFS`
- **Phase C（内核写入）**：写文件 + extent 分配 + journal + 断电注入测试
- **Phase D（关键词）**：tag 命令 + 倒排表构建/查询；语义查询解析器可放外部工具
- **Phase E（后续）**：快照/CoW、Subindex 树、压缩、加密

---

## 13. 常量速查

| 项 | 值 |
|---|---|
| magic | `b'KIFS'` |
| 版本 | `0x0002_0000`（v2；v1 不再支持） |
| 块大小 | 4096 / 16384（默认 4096，大卷 16384） |
| record 大小 | 128 |
| record 上限 | `min(max(16, total_blocks//64), 1<<20)`，`--record-cap` 可覆盖（≤1M） |
| perm 字 | 16 位：rwxrwxrwx + hide + ro + iolock |
| owner | 16 B 用户名（UTF-8，NUL 填充） |
| 内联上限 | 92 B（数据占 record 36..128） |
| 内联 extent | 3 个，每项 12 B：`start u64 + count u32` |
| 文件大小 | u64：`size_lo@8` + `size_hi@100`；blocks 同理 `@12/@104` |
| 符号链接目标 | ≤1024 B UTF-8（无 NUL，允许 dangling），默认 perm 0777 |
| 目录项头 | 8 B，名字 UTF-8，4 对齐 |
| journal 头 | 20 B：`'KJRN'` + seq u32 + flags u16 + offset u16 + target u32 + len u16 + crc16 |
| 默认 journal | 64 块（4K=256 KiB，16K=1 MiB） |
| 单卷上限 | 4K 块 16 TiB；16K 块 64 TiB - 16 KiB（u32 total_blocks） |

---

## 14. 未决问题（实现前再定）

1. Record 表固定槽 vs 动态分配（固定简单但浪费；v2 固定，上限 1M）
2. 目录项排序用字节序还是大小写不敏感序
3. extent 索引块（超过 3 段）是否也走 journal（当前：ext_index 表按 12 B/项，走位图/CoW）
4. journal checkpoint / head-tail 回收策略（当前：满则报错退出；宿主已把相邻增量合并以省记录）
5. keywords 删除/更新的空间回收
6. KiFS 与 FAT16 的挂载原语（`mount` 命令 + VFS 层抽象）
7. 断电测试怎么做：QEMU `-snapshot` + 杀进程 + 重新挂载 fsck
8. owner 直存用户名 vs 以后改 uid 表（需要用户系统）
9. group/other 的 rwx 在单用户环境是否只展示不强制（倾向只展示）
10. 内联数据覆盖 record 尾部字段（owner/size_hi 等），是否值得把内联数据挪到独立区域

---

## 15. 内核驱动注意事项（v2）

- **分区位置**：沿用 B0 的 `KIFS_PART_LBA = 22528`；但 v2 镜像不再限制 4 MiB，
  大卷测试需 LBA48 和更大的盘镜像。宿主生成命令示例：
  - CI 小镜像：`python3 tools/mkifs.py format disk.img --size-mib 4`
  - 16K 测试卷：`python3 tools/mkifs.py format disk.img --size-mib 64 --block-size 16384`
  - 大卷：`python3 tools/mkifs.py format disk.img --size-mib 15728640 --block-size 16384`
    （15 TiB 稀疏，只占 ~250 MiB 实盘）
- **superblock 在分区块 1**：块 0 是引导块留空，主副本在块 1、备份在块 2 与最后一块。
  位置随块大小移动（4K → 字节 4096，16K → 字节 16384）；校验魔数 /
  `version == 0x00020000` / `block_size ∈ {4096,16384}` / 前 108 字节 CRC32。
- **读块**：`lba = part_lba + block * (block_size/512)` 用 64 位算，走 LBA48。
- **Record 解析**：`size = size_lo | (size_hi<<32)`，blocks 同理；extent 每项 12 B
  `(start u64, count u32)`；`n_ext` 为内联段数，超过 3 段读 `ext_index` 表（12 B/项）。
- **内联 record 的 36..128 字节被内联数据覆盖**：`inline_len > 0` 时不要解析
  extent 区 / owner / size_hi / blocks_hi，size 一律取 `inline_len`，blocks=0。
- **journal**：payload = `block_size-20`（4K→4076，16K→16364）；只读驱动可只看
  `flags` clean 位，不做重放。
- **不校验 owner 权限 / 不做 hide 过滤**（单用户）；`perm` 展示 `rwxrwxrwx` + 三位 + owner。
- **静默失败**：盘上没 KiFS 卷（魔数 / 版本 / CRC 不对、读盘失败）时初始化直接返回，
  不影响原有 FAT / 软盘启动路径。

---

## 16. 实现记录：v2 宿主工具（2026-11-15）

- **代码**：`tools/mkifs.py` 全部按 v2 重写；`tools/test_mkifs.py`、`tools/test_journal.py` 同步。
- **布局**：块大小参数化；`bitmap@3`、record 表紧随、`data_next` 向上、`meta_next` 向下、
  盘尾 64 块 journal、末块 super 备份 2；format 只写元数据区（大卷 = 位图+record 表+journal 头）。
- **Record v2**：`size_lo/size_hi` + `blocks_lo/blocks_hi` 拼 u64；extent `(u64,u32)` 12 B；
  `record_count = min(max(16, total_blocks//64), 1<<20)`，`--record-cap` 覆盖。
- **写路径**：元数据增量先合并（同位置取最后值、相邻区间拼接）再进 journal；
  数据块 CoW；恢复时残缺事务清槽、完整事务重放并写 END，重读 superblock。
- **fsck 新增检查**：version==2、block_size∈{4096,16384}、位图/record/journal 区不重叠、
  位图标记数与所有 extent 声明块数一致、size/blocks 与 extent 总块数一致、
  目录项类型一致、孤立 record、superblock 三副本一致。
- **实测**：64 GiB/16K format 写 9.5 MiB、15 TiB/16K format 写 249 MiB（0.22s）、
  15 TiB fsck 0.54s；192 + 91 项自测通过（v2 时点；加入符号链接后为 228 + 111）。
- **历史**：A2（v1 journal/CoW）记录已由 v2 取代；`KIFS_CRASH` 五种注入点语义不变。

---

## 17. 实现记录：Phase A2（journal redo + CoW，v1 历史，2026-11-15）

- **代码**：`tools/mkifs.py`（`Image.recover` / `Image.flush` / `Image._jrn_commit` / `mark` / `mark_span` / `write_data_block`），`tools/test_journal.py`。
- **写路径**：所有元数据变更（Record 128 B、位图字节、Superblock ×3）先进 `_txn_deltas`；`flush()` 摊平成 journal 记录并提交；数据块（文件/目录内容）永远写新块（CoW），旧块在事务内释放——提交前新块是孤儿，fsck 会认为未用。
- **恢复**：挂载即 `recover()`；重放 BEGIN+COMMIT 完整且 CRC 全有效的事务（幂等，再写 END 覆盖 BEGIN 槽）；残缺事务（崩在 BEGIN/记录阶段）直接写 END 回收槽位，不重放。
- **注入点**：`KIFS_CRASH=after_begin|after_records|after_commit|after_apply|torn`（torn 会在 COMMIT 前写坏最后一条记录的 payload CRC）。
- **验证结论**：after_begin/after_records/torn → 内容保持旧值（事务丢弃）；after_commit/after_apply → 内容为新值（重放幂等）；所有场景重开 fsck 通过、无悬挂事务；CoW 重写实测新旧 extent 起点不同，旧块被正确释放。
- **注意**：本节是 v1 时代记录，v2 的 journal 格式与注入点语义不变，但 payload 长度与块大小相关。

---

## 18. 实现记录：符号链接 type=3（v2.1，2026-11-15）

- **格式**：record `type=3`；`size` = 目标路径 UTF-8 字节数，内容复用 `write_content`
  的 inline（≤92 B）/ extent 路径；目标上限 1024 B，允许 dangling，不做文件系统层解析。
  目录项 `type` 同步为 3，`unlink` 走普通非目录分支（只删链接自身）。
- **宿主工具**（`tools/mkifs.py`）：新增 `TYPE_SYMLINK=3`、`PERM_SYMLINK_DEFAULT=0o777`、
  `SYMLINK_TARGET_MAX=1024`；`Image.symlink/readlink`；`write_file`/`read_file` 遇到
  符号链接明确报「是符号链接」；`ls` 用 `l` 前缀并追加 `-> target`，`stat` 增加
  `type: symlink` 与 `target:`；fsck 接受 type=3，检查目标长度/NUL/UTF-8 并统计数量；
  CLI 增加 `symlink` / `readlink`。
- **FUSE**（`linux/fuse_kifs.py`）：`getattr` 对 type=3 返回 `S_IFLNK|0777`、`st_size`=
  目标字节数、`st_nlink=1`；`symlink` 注意 fusepy 参数顺序为 `(target=新链接路径,
  source=目标路径)`；`readlink` 返回目标字符串；`readdir` 对 type=3 上报 `DT_LNK`；
  `open/read/write/truncate` 遇到符号链接防御性返回 `EINVAL`；超长目标
  `ENAMETOOLONG`、重名 `EEXIST`。
- **测试**：`tools/test_mkifs.py` 新增 36 项（中文/emoji、dangling、指向目录、
  1024/1025 B 边界、重名、unlink 不动目标、fsck 计数）；`tools/test_journal.py`
  新增符号链接创建（after_records 丢弃 / after_commit 重放）与删除重放共 20 项；
  `linux/test_fuse.sh` 增加 `[符号链接]` 小节；`linux/test_image_api.py` 增加
  内联/长目标块级 API 断言。
- **文档**：`docs/kifs.md`（本节 + §6.1 + 类型表/常量表/FAT 对比）、`README.md`、
  `linux/README.md`、`kernel/README.md`（参考驱动的显示行为：`ls` 仍显示 `-`，
  `stat` 显示 file，`cat` 报非文件，且不跟随链接；内核驱动本期不改）。

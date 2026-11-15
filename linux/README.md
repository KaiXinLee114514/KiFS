# KiFS on Linux — FUSE 驱动

把 KiFS v2 卷挂成普通目录：`ls`/`cp`/`dd`/`mv`/`rm` 直接可用，所有写入都走
`tools/mkifs.py` 的 **journal + CoW** 路径（掉电可重放），不需要内核模块。

- 驱动：`linux/fuse_kifs.py`（Python + [fusepy](https://github.com/fusepy/fusepy) 3.x）
- 入口：`mkifs mount IMG_OR_DEV /mnt/kifs`（自动找仓库 `.venv` 里的 Python）
- 支持：镜像**文件**（含稀疏大卷）和**块设备**（真机裸分区；需 root）
- 测试：`linux/test_fuse.sh`（端到端挂载 + fsck + 字节比对）

## 1. 依赖

```bash
# Debian/Ubuntu
sudo apt install fuse3 libfuse3-3 python3-venv
ls -l /dev/fuse            # crw-rw-rw- 即可；未加载时 sudo modprobe fuse

# 仓库内建 venv（fusepy 必须是 3.x；Debian 自带的 python3-fuse 是 2.x，不兼容）
cd /path/to/kifs
python3 -m venv .venv
.venv/bin/pip install -r linux/requirements.txt
```

不需要 root 也能挂载**自己拥有**的镜像文件（`fusermount3` 走 setuid 辅助程序）。
块设备挂载需要 root（见 §4）。想允许其他用户访问挂载点，需要
`/etc/fuse.conf` 里打开 `user_allow_other` 并加 `-o allow_other`（默认不支持）。

## 2. 挂载 / 卸载

```bash
# 造一个卷（4 KiB 块、4 MiB；块大小也可以 16384）
tools/mkifs.py format /tmp/kifs.img --size-mib 4

# 挂载（后台 daemon；命令返回后即可使用）
tools/mkifs.py mount /tmp/kifs.img /mnt/kifs

# 也可以直接调驱动，加日志/前台调试
.venv/bin/python linux/fuse_kifs.py /tmp/kifs.img /mnt/kifs --log /tmp/fuse.log
.venv/bin/python linux/fuse_kifs.py /tmp/kifs.img /mnt/kifs --foreground --debug

ls -la /mnt/kifs
cp file /mnt/kifs/            # 真正写入 KiFS 卷
sync                          # 或 fsync 单个文件
fusermount3 -u /mnt/kifs      # 卸载（新版 fuse3；老系统用 fusermount -u）
```

`umount /mnt/kifs` 在 root 下同样可用；普通用户用 `fusermount3 -u`。
卸载后可以随时 `tools/mkifs.py fsck /tmp/kifs.img` 验证。

常用选项：

| 选项 | 说明 |
|---|---|
| `--offset N` / `--offset-lba N` | KiFS 卷在设备内的字节偏移 / 512 B 扇区偏移（分区镜像、整盘前有分区表时用） |
| `--read-only` | 只读挂载（内核挂载表也是 `ro`，写入返回 EROFS） |
| `--show-hidden` | 显示带 hide 权限位的条目（默认对 `ls` 隐藏） |
| `--foreground` / `--debug` | 前台运行 / 带 FUSE 调试输出 |
| `--log FILE` | 日志写文件（daemon 化后 stderr 会丢，排障时用这个） |

卸载前请退出所有打开该目录的进程/`cd`；否则会 `Device or resource busy`，
可用 `fusermount3 -uz` 强制卸载（不推荐，可能丢未 fsync 的数据）。

## 3. 支持的操作与权限

已接入的 FUSE 操作：`readdir`、`getattr`、`access`、`open`、`read`、`write`、
`create`、`mknod`、`mkdir`、`unlink`、`rmdir`、`rename`、`truncate`、`chmod`、
`chown`（实现为 no-op/校验）、`utimens`、`statfs`、`flush`、`fsync`、`fsyncdir`、
`release`、`destroy`、`symlink`、`readlink`。

符号链接：`ln -s 目标 链接` 写入 record `type=3`，目标最长 1024 B（UTF-8，允许
dangling），`ls -l` 显示 `l` 与 `-> 目标`，`readlink` 原样返回目标；解析由 VFS
完成，驱动不跟随。`unlink` 只删链接本身；对链接直接 `open/read/write/truncate`
防御性返回 `EINVAL`（正常路径已被 VFS 解析）。超长目标 `ENAMETOOLONG`、重名
`EEXIST`。

KiFS 的 16 位 `perm` 低 9 位是 **r=1、w=2、x=4**（与 POSIX 的 r=4、w=2、x=1 相反），
驱动负责双向转换，`ls -l` 看到的是正常 rwx。高 3 位是特殊位：

- `hide`（`1<<9`）：默认在 `readdir`/`getattr` 里不可见；`--show-hidden` 才显示。
- `ro`（`1<<10`）：该文件/目录只读（写入返回 EROFS，`ls` 权限里去掉写位）。
- `iolock`（`1<<11`）：KiFS 没有并发写语义，驱动本身就是**单线程 + 全局锁**；
  该位目前只作为占位/文档语义，不额外加锁。

`--read-only` 挂载会让整个文件系统只读（内核 `MS_RDONLY`），与单文件 `ro` 位叠加。
所有文件 `st_uid/st_gid` 报为挂载用户（KiFS 只存 owner 名字，没有 uid/gid）。
KiFS 没有硬链接/扩展属性：`link` 返回 `ENOTSUP`，xattr 的 set/remove 返回
`ENOTSUP`、get/list 返回空；符号链接支持 `type=3`（见本节）。

## 4. 块设备（裸分区 / 整盘）

KiFS 卷可以直接放在块设备上（例如 18 T HDD 的裸分区）。**需要 root**：普通用户
对 `/dev/sdX` 通常没有写权限，`fusermount3` 也帮不了。

```bash
# 整盘/整分区就是 KiFS（卷从 LBA 0 开始）；format 需要显式大小（MiB，向下取整）
SIZE_MIB=$(( $(sudo blockdev --getsize64 /dev/sdb1) / 1048576 ))
sudo tools/mkifs.py format /dev/sdb1 --size-mib "$SIZE_MIB"
sudo tools/mkifs.py mount  /dev/sdb1 /mnt/kifs

# 盘上有分区表/别的头，KiFS 卷在固定 LBA 22528（11 MiB）——内核驱动的布局
sudo tools/mkifs.py format /dev/sdb --offset-lba 22528 --size-mib 65536
sudo tools/mkifs.py mount  /dev/sdb /mnt/kifs --offset-lba 22528
# 或者用字节偏移（--offset 11534336）

fusermount3 -u /mnt/kifs    # root 下也可用 umount /mnt/kifs
```

`format` 对**块设备**不会 `truncate`（也不允许卷越界）；带 `--offset` 时要求目标
**已存在**且足够大（普通文件先 `truncate -s 16M disk.img` 再 format）。

> ⚠️ 在真盘上 format 会写 superblock/位图，**先确认设备路径**（`lsblk`）。
> 块设备的 format/挂载路径需要 root，本机测试脚本只覆盖镜像文件与 loop（root 时）。
> 挂载时驱动会对镜像/设备加独占 `flock`，同一个卷不会被两个进程同时打开写。

`--offset`/`--offset-lba` 也可以用于镜像文件：例如 `dd` 出来的带前缀的卷，
或测试脚本里的「包裹卷」场景（前缀字节不会被驱动碰）。

## 5. 限制（重要）

- **单线程**：fusepy `nothreads=True`，所有请求在一个线程里串行处理（KiFS 的
  journal/位图没有并发写设计）。大文件顺序读写没问题，随机小 IO 并发差。
- **不支持稀疏文件**：KiFS 要求 `size <= blocks * block_size`（fsck 会报错），
  所以 `truncate` 变大时驱动按 1 MiB 分块**补零写入**——大文件 `truncate -s 100G`
  会真的占盘。`cp --sparse=always` 也一样。
- **盘满返回 ENOSPC**：`write`/`truncate` 向上扩容时若分配不到连续块会失败；
  `cp` 中途失败会看到 ENOSPC，已写部分保留。
- **extent 限制**：record 内联 3 个 extent；超过 3 段时驱动自动分配 ext_index
  表块（读写都支持），但极端碎片化下每次改写都要 CoW 一整段，性能会下降。
- **journal 容量**：每个写操作是一次事务，journal 固定 64 块（BEGIN/COMMIT 占 2
  块，最多约 61 条 after-image 记录，每条 ≤ `block_size-20` 字节，连续区间会合并）。
  数据块在 COMMIT 前就直写落盘（新块是孤儿，断电安全），journal 里只有位图/record/
  superblock 等元数据，所以正常写不会碰到上限；`truncate` 向上按 1 MiB 分块是为了
  让元数据增量保持在一次事务内。
- **没有 mmap 写回**：FUSE 默认不启用 writeback cache，`mmap(MAP_SHARED)` 的
  写不保证落盘（读可以）。需要持久化请用 `write`/`fsync`。
- **崩溃恢复**：挂载时自动重放 journal（`Image.recover()`）；如果镜像在别的
  机器/进程手里被写坏，驱动会在写失败时 `reload + recover` 一次并把错误返回给
  应用（EIO）。
- **扩展属性/ACL/硬链接**：不支持；符号链接支持 `type=3`（目标 ≤1024 B，允许
  dangling，见 §3）。
- 大卷（15 TiB 稀疏镜像）format 只写元数据（~249 MiB），挂载后 `df` 显示的是
  KiFS 的 `statfs` 数字（总块数/空闲块数），不是宿主文件系统。

## 6. 测试

```bash
# 端到端：4 KiB/4 MiB + 16 KiB/64 MiB 两张镜像
linux/test_fuse.sh

# 保留现场（失败时本来就会保留）
linux/test_fuse.sh --keep
```

脚本会：`format` → 挂载 → `ls/mkdir/cp/dd/rename/chmod/truncate(下/上)/append/fsync`
→ 卸载 → `mkifs fsck` 必过 → 离线 `mkifs read` 与源文件逐字节比对；再测只读挂载、
hide/ro 权限位、`--offset` 包裹卷。以 root 运行且装了 `losetup` 时，额外把镜像挂成
loop 块设备再跑一遍块设备路径（真机裸分区不会自动碰）。

只测 Image 的块级 API（不需要 FUSE）：

```bash
.venv/bin/python linux/test_image_api.py     # 4K/4MiB + 16K/64MiB
```

## 7. 排障

| 现象 | 原因/处理 |
|---|---|
| `无法导入 fusepy 3.x` | 没建 `.venv` 或装的是系统 fusepy 2.x → §1 |
| `mount failed: Permission denied` | 镜像不属于当前用户，或挂载点被占用；块设备需 root |
| `Transport endpoint is not connected` | 驱动进程崩了/被 kill → `fusermount3 -uz /mnt/kifs` 后看 `--log` |
| `Device or resource busy` | 有进程占用挂载点 → `lsof +D /mnt/kifs`、退出 `cd` |
| 写入 `ENOSPC` | 卷满（`df` 看 free blocks）或连续块不够 → `fsck`/删文件 |
| `fsck` 报错 | 先卸载；别手动改镜像；驱动每次写都有 journal，正常不会坏 |
| 权限显示成 `dr-xr-xrwx` 之类 | 旧版驱动的位序 bug，已修；升级到最新 `linux/fuse_kifs.py` |

## 8. 一键挂载脚本 `linux/kifs-mount.sh`

日常使用（尤其是 USB 实体盘会重新枚举成 sda/sdb/sdc）推荐用这个脚本，不用每次手动
拼 `mount` 命令或找我：

```bash
# 一次性安装到 PATH（当前用户）
ln -sf /Files/Project/kifs/linux/kifs-mount.sh ~/.local/bin/kifs-mount

kifs-mount            # 挂载（默认，等价 kifs-mount mount）
kifs-mount umount     # 卸载
kifs-mount restart    # 先卸再挂（换驱动/换盘后常用）
kifs-mount status     # 看设备/挂载/驱动/空间
kifs-mount data       # 用 udisks 挂载 DATA(exFAT) 分区
```

脚本做的事：

- **按分区名找设备**：优先 `/dev/disk/by-partlabel/kifs`（稳定，与 sda/sdb 无关），
  其次 `/dev/kifs`（udev 规则创建），最后扫描分区找 `KIFS` 魔数。
- 处理 USB 刚插入/重新枚举：`KIFS_WAIT=15` 默认等 15 秒。
- 自动清理陈旧挂载与残留 `fuse_kifs.py` 进程（`fusermount3 -uz` + kill）。
- 后台起驱动 + 等挂载点就绪；日志落在 `~/.local/state/kifs/`。

可调环境变量：`KIFS_REPO`、`KIFS_MNT`、`KIFS_DEV`、`KIFS_WAIT`、`KIFS_LOG_DIR`、`KIFS_PY`。

> 实体盘权限：udev 规则 `/etc/udev/rules.d/70-kifs.rules` 按 USB-SATA 桥序列号
> `235678C218CA` 给 `sd?1` 设 `OWNER=kai MODE=0600`，插到任何 USB 口都能直接读写。
> 需要我改成 systemd user 服务开机/登录自动挂载的话，说一声。

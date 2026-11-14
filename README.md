# KiFS (Kai File System)

一个以「对象 + 倒排关键词索引」为核心的 4 KiB 块文件系统 —— 目录只是视图，语义查询是一等公民。

> 状态：设计定稿（v0.2，2026-11-15）；Phase A 宿主原型（`tools/mkifs.py`）已完成——含 journal redo + CoW 写入路径 + 断电注入自测（29 项）；内核只读驱动 B0 已有独立分支实现，内核写入未开始。
> 设计稿见 [docs/kifs.md](docs/kifs.md)。

## 设计速览

- 盘头 Superblock（带备份）→ 块位图 / Record 表 / 数据块向上长
- 关键词串池 / Subindex / Journal（redo）从盘尾向下长，双游标
- Record 128 B：oid/type/flags/perm(16 位)/size/时间/parent/kw_index/owner + 小文件内联 ≤92 B + 最多 3 个内联 extent
- 权限展示：`rwxrwxrwx111kai`（9 位 rwx + 3 位 hide/ro/iolock + 用户名；hide/ro/iolock 都是 perm 位）
- 目录 = 有序 UTF-8 目录项；关键词 = 串池 + 倒排 OID 数组
- 断电一致性：BEGIN → BLOCK → COMMIT → 落盘 → END 的环形 journal
- 查询示例：`"Last Summer family photos"` → 时间区间 + 类型 + keyword 的结构化过滤

## 路线图

| Phase | 内容 | 状态 |
|---|---|---|
| A | `tools/mkifs.py` 宿主原型（format/mkdir/write/read/ls/stat/rm/info/fsck/journal）+ 测试镜像 | ✅ 完成（连续 extent / 目录线性 / **journal redo + CoW**）|
| B | 内核只读驱动（挂载 /MNT/KIFS） | B0 分支完成（只读驱动 + kifs 命令组，未合入）|
| C | 内核写入 + journal + 断电注入测试 | 宿主侧已完成（journal+CoW+断电注入），内核侧未开始 |
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

自测（临时目录里跑完整流程，含损坏检测）：

```bash
python3 tools/test_mkifs.py
```

## 为什么不开源（暂时）

本项目计划在合适的时间点作为独立仓库公开。在此之前只在本地迭代。

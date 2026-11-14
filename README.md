# KiFS (Kai File System)

一个以「对象 + 倒排关键词索引」为核心的 4 KiB 块文件系统 —— 目录只是视图，语义查询是一等公民。

> 状态：设计定稿（v0.1，2026-11-14），实现未开始。
> 设计稿见 [docs/kifs.md](docs/kifs.md)。

## 设计速览

- 盘头 Superblock（带备份）→ 块位图 / Record 表 / 数据块向上长
- 关键词串池 / Subindex / Journal（redo）从盘尾向下长，双游标
- Record 128 B：oid/type/flags/perm/size/时间/parent/kw_index + 小文件内联 ≤92 B + 最多 3 个内联 extent
- 目录 = 有序 UTF-8 目录项；关键词 = 串池 + 倒排 OID 数组
- 断电一致性：BEGIN → BLOCK → COMMIT → 落盘 → END 的环形 journal
- 查询示例：`"Last Summer family photos"` → 时间区间 + 类型 + keyword 的结构化过滤

## 路线图

| Phase | 内容 | 状态 |
|---|---|---|
| A | `tools/mkifs.py` 宿主原型（format/mkdir/write/read/ls/fsck）+ 测试镜像 | 未开始 |
| B | 内核只读驱动（挂载 /MNT/KIFS） | 未开始 |
| C | 内核写入 + journal + 断电注入测试 | 未开始 |
| D | keywords 倒排 + 语义查询 | 未开始 |
| E | 快照/CoW、Subindex、压缩 | 未开始 |

## 为什么不开源（暂时）

本项目计划在合适的时间点作为独立仓库公开。在此之前只在本地迭代。

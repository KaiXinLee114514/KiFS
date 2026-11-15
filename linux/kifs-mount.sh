#!/usr/bin/env bash
# =============================================================================
#  kifs-mount — KiFS 一键挂载 / 卸载（FUSE 用户态驱动）
#
#  用法:
#    kifs-mount [mount]     挂载（默认）
#    kifs-mount umount      卸载（别名 unmount/u）
#    kifs-mount restart     先卸再挂
#    kifs-mount status      查看设备/挂载/驱动/空间状态
#    kifs-mount data        用 udisks 挂载 DATA(exFAT) 分区并打印路径
#
#  可调环境变量:
#    KIFS_REPO     仓库目录         (默认 /Files/Project/kifs)
#    KIFS_MNT      挂载点           (默认 /mnt/kifs)
#    KIFS_DEV      设备路径         (默认自动找 /dev/disk/by-partlabel/kifs)
#    KIFS_WAIT     等设备出现的秒数 (默认 15)
#    KIFS_LOG_DIR  日志目录         (默认 ~/.local/state/kifs)
#    KIFS_PY       python 解释器    (默认 $KIFS_REPO/.venv/bin/python)
#
#  设备识别: 优先 /dev/disk/by-partlabel/kifs（分区名 kifs 稳定），
#            其次 /dev/kifs（udev 规则创建），最后扫描分区的 KIFS 魔数。
# =============================================================================
set -u

REPO="${KIFS_REPO:-/Files/Project/kifs}"
MNT="${KIFS_MNT:-/mnt/kifs}"
DEV_OVERRIDE="${KIFS_DEV:-}"
WAIT_SECS="${KIFS_WAIT:-15}"
LOG_DIR="${KIFS_LOG_DIR:-$HOME/.local/state/kifs}"
PY="${KIFS_PY:-$REPO/.venv/bin/python}"

bye()  { echo "kifs-mount: $*" >&2; exit 1; }
ok()   { echo "✅ $*"; }
info() { echo "ℹ️  $*"; }

# ---------- 设备发现 ----------
find_dev_py() {
    # 最后兜底：扫常见分区设备，找偏移 4096 或 16384 处的 "KIFS" 魔数
    "$PY" - <<'PYEOF' 2>/dev/null
import glob, os, sys
cands = sorted(set(glob.glob("/dev/sd*[0-9]") + glob.glob("/dev/nvme[0-9]n[0-9]p[0-9]")))
for p in cands:
    try:
        fd = os.open(p, os.O_RDONLY)
    except OSError:
        continue
    try:
        for off in (4096, 16384):
            os.lseek(fd, off, 0)
            if os.read(fd, 4) == b"KIFS":
                print(p)
                sys.exit(0)
    finally:
        os.close(fd)
sys.exit(1)
PYEOF
}

find_dev() {
    if [ -n "$DEV_OVERRIDE" ]; then
        [ -b "$DEV_OVERRIDE" ] || bye "KIFS_DEV=$DEV_OVERRIDE 不是块设备"
        echo "$DEV_OVERRIDE"; return 0
    fi
    local deadline=$(( $(date +%s) + WAIT_SECS ))
    while :; do
        if [ -b /dev/disk/by-partlabel/kifs ]; then
            readlink -f /dev/disk/by-partlabel/kifs; return 0
        fi
        if [ -b /dev/kifs ]; then
            readlink -f /dev/kifs; return 0
        fi
        [ "$(date +%s)" -ge "$deadline" ] && break
        sleep 0.5
    done
    find_dev_py || return 1
}

# ---------- 状态工具 ----------
is_mounted() { mountpoint -q "$MNT" 2>/dev/null; }

stale_pids() {
    ps -eo pid=,args= 2>/dev/null \
      | grep -F 'fuse_kifs.py' \
      | grep -F "$MNT" \
      | grep -v grep \
      | awk '{print $1}'
}

cleanup_stale() {
    local pids
    pids=$(stale_pids)
    if [ -n "$pids" ]; then
        info "清理残留驱动进程: $pids"
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null
    fi
    fusermount3 -uz "$MNT" 2>/dev/null || true
}

# ---------- 子命令 ----------
do_mount() {
    if is_mounted; then
        ok "已挂载: $(findmnt -n -o SOURCE,TARGET "$MNT")"
        df -h "$MNT" | tail -1
        return 0
    fi
    [ -x "$PY" ] || bye "找不到 venv python: $PY（先在 $REPO 里建 .venv 并装 fusepy）"
    [ -f "$REPO/linux/fuse_kifs.py" ] || bye "缺少 $REPO/linux/fuse_kifs.py"

    local dev
    dev=$(find_dev) || bye "找不到 KiFS 分区（可插好 USB 盘重试，或设 KIFS_WAIT=30）"

    mkdir -p "$MNT" "$LOG_DIR" || bye "无法创建 $MNT 或 $LOG_DIR"
    cleanup_stale
    local ts log out
    ts=$(date +%Y%m%d-%H%M%S)
    log="$LOG_DIR/fuse-$ts.log"
    out="$LOG_DIR/fuse-$ts.out"

    setsid "$PY" "$REPO/linux/fuse_kifs.py" "$dev" "$MNT" --log "$log" \
        >"$out" 2>&1 </dev/null &

    local i
    for i in $(seq 1 100); do
        is_mounted && break
        sleep 0.1
    done
    if is_mounted; then
        ok "已挂载 $dev -> $MNT"
        df -h "$MNT" | tail -1
        info "日志: $log"
    else
        echo "❌ 挂载失败，输出尾部:" >&2
        tail -n 15 "$out" 2>/dev/null >&2
        exit 1
    fi
}

do_umount() {
    if is_mounted; then
        sync
        if ! fusermount3 -u "$MNT" 2>/dev/null; then
            info "普通卸载失败（可能还有进程占用），尝试 lazy 卸载"
            fusermount3 -uz "$MNT" 2>/dev/null || bye "卸载失败: $MNT"
        fi
        ok "已卸载 $MNT"
    else
        cleanup_stale
        info "$MNT 原本就未挂载"
    fi
}

do_status() {
    echo "repo   : $REPO"
    echo "python : $PY $([ -x "$PY" ] && echo '(ok)' || echo '(缺失!)')"
    if [ -b /dev/disk/by-partlabel/kifs ]; then
        echo "device : /dev/disk/by-partlabel/kifs -> $(readlink -f /dev/disk/by-partlabel/kifs)"
    elif [ -b /dev/kifs ]; then
        echo "device : /dev/kifs -> $(readlink -f /dev/kifs)"
    else
        echo "device : (未找到 KiFS 分区)"
    fi
    if is_mounted; then
        echo "mount  : ✅ $(findmnt -n -o SOURCE,FSTYPE,SIZE "$MNT")"
        df -h "$MNT" | tail -1
    else
        echo "mount  : ❌ 未挂载"
    fi
    local pids
    pids=$(stale_pids)
    if [ -n "$pids" ]; then
        if is_mounted; then echo "驱动 pid: $pids"; else echo "残留驱动 pid: $pids（可 umount 清理）"; fi
    fi
    echo "日志目录: $LOG_DIR"
}

do_data() {
    local dev="${1:-}"
    if [ -z "$dev" ]; then
        dev=$(lsblk -pno NAME,LABEL | awk '$2=="DATA"{print $1; exit}')
    fi
    [ -n "$dev" ] || bye "找不到 label=DATA 的 exFAT 分区"
    udisksctl mount -b "$dev" || bye "挂载 $dev 失败"
}

case "${1:-mount}" in
    mount|m)        do_mount ;;
    umount|unmount|u) do_umount ;;
    restart|r)      do_umount; do_mount ;;
    status|s)       do_status ;;
    data)           do_data "${2:-}" ;;
    -h|--help|help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)              bye "未知子命令: $1（用 mount/umount/restart/status/data）" ;;
esac

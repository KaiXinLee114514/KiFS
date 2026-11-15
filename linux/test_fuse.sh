#!/usr/bin/env bash
# KiFS FUSE 端到端测试：真实挂载 → 一连串 POSIX 操作 → 卸载 → fsck + 字节比对。
#
# 用法：
#   linux/test_fuse.sh              # 4 KiB/4 MiB 与 16 KiB/64 MiB 两张镜像
#   linux/test_fuse.sh --keep       # 失败/成功后都保留临时目录（方便看现场）
#
# 需要：
#   * Linux，/dev/fuse 可用
#   * fusermount3（或 fusermount）
#   * 仓库 .venv 里装好 fusepy（linux/requirements.txt）
#
# 以 root 运行且系统有 losetup 时，额外跑一遍 loop 块设备挂载测试；
# 真实 18T HDD 裸分区需要 root，测试脚本不自动碰真实设备。
set -u

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$HERE")
PY="$ROOT/.venv/bin/python"
DRIVER="$HERE/fuse_kifs.py"
MKIFS=("$PY" "$ROOT/tools/mkifs.py")
FUSERMOUNT=$(command -v fusermount3 || command -v fusermount || true)
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

TMP=$(mktemp -d "${TMPDIR:-/tmp}/kifs-fuse.XXXXXX")
MNT=""
LOOPDEV=""
FAILED=0

log()  { printf '%s\n' "$*"; }
ok()   { printf '  ok   %s\n' "$*"; }

diag() {
    log "---- 诊断 ----"
    log "临时目录: $TMP"
    if [ -n "$MNT" ]; then
        log "挂载点状态: $(mountpoint -q "$MNT" && echo 已挂载 || echo 未挂载)"
        log "mount 表项: $(awk -v m="$MNT" '$2==m {print $3, $4}' /proc/mounts 2>/dev/null)"
        log "---- ls -la ----"
        ls -la "$MNT" 2>&1 | head -30
        log "---- find -maxdepth 2 ----"
        find "$MNT" -maxdepth 2 -ls 2>&1 | head -30
        df -h "$MNT" 2>&1 | sed 's/^/  df: /'
    fi
    if [ -f "$TMP/fuse.stderr" ]; then
        log "---- driver stderr ----"
        tail -20 "$TMP/fuse.stderr"
    fi
    if [ -f "$TMP/fuse.log" ]; then
        log "---- fuse.log (tail -40) ----"
        tail -40 "$TMP/fuse.log"
    fi
    if command -v dmesg >/dev/null 2>&1; then
        log "---- dmesg (tail -10，可能无权限) ----"
        dmesg 2>/dev/null | tail -10 || true
    fi
}

die() {
    log "❌ $*"
    FAILED=1
    diag
    exit 1
}

cleanup() {
    if [ -n "$MNT" ] && mountpoint -q "$MNT" 2>/dev/null; then
        "$FUSERMOUNT" -u "$MNT" 2>/dev/null || "$FUSERMOUNT" -uz "$MNT" 2>/dev/null || true
        sleep 0.2
    fi
    if [ -n "$LOOPDEV" ]; then
        losetup -d "$LOOPDEV" 2>/dev/null || true
    fi
    if [ "$KEEP" -eq 1 ] || [ "$FAILED" -ne 0 ]; then
        log "现场保留在: $TMP"
    else
        rm -rf "$TMP"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------- 前置检查
[ -c /dev/fuse ] || die "没有 /dev/fuse（内核 FUSE 未加载？）"
[ -n "$FUSERMOUNT" ] || die "找不到 fusermount3/fusermount（安装 fuse3）"
if [ ! -x "$PY" ]; then
    die "缺少 $PY；先建 venv：python3 -m venv $ROOT/.venv && $ROOT/.venv/bin/pip install -r $HERE/requirements.txt"
fi
"$PY" -c 'from fuse import FUSE, FuseOSError, Operations' 2>/dev/null \
    || die "$PY 里没有可用的 fusepy 3.x（pip install -r $HERE/requirements.txt）"

# ---------------------------------------------------------------- 工具函数
_start_mount() {                       # _start_mount MNT 命令...
    local mnt=$1
    shift
    mkdir -p "$mnt"
    "$@" >"$TMP/fuse.stdout" 2>"$TMP/fuse.stderr" &
    local pid=$!
    local i
    for i in $(seq 1 200); do
        if mountpoint -q "$mnt"; then
            MNT=$mnt
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.05
    done
    MNT=$mnt
    local rc=0
    wait "$pid" 2>/dev/null || rc=$?
    log "挂载失败（命令退出码 $rc），driver 输出："
    cat "$TMP/fuse.stdout" "$TMP/fuse.stderr" 2>/dev/null | tail -30
    return 1
}

_start_mount() {                       # _start_mount MNT 命令...
    local mnt=$1
    shift
    mkdir -p "$mnt"
    "$@" >"$TMP/fuse.stdout" 2>"$TMP/fuse.stderr" &
    local pid=$!
    local i
    for i in $(seq 1 200); do
        if mountpoint -q "$mnt"; then
            MNT=$mnt
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.05
    done
    MNT=$mnt
    local rc=0
    wait "$pid" 2>/dev/null || rc=$?
    log "挂载失败（命令退出码 $rc），driver 输出："
    cat "$TMP/fuse.stdout" "$TMP/fuse.stderr" 2>/dev/null | tail -30
    return 1
}

mount_kifs() {                         # mount_kifs IMG MNT [额外参数...]
    local img=$1 mnt=$2
    shift 2
    _start_mount "$mnt" "$PY" "$DRIVER" "$img" "$mnt" --log "$TMP/fuse.log" "$@" \
        || return 1
    ok "挂载 $img -> $mnt $*"
    return 0
}

mount_cli() {                          # mount_cli IMG MNT [额外参数...]（走 mkifs mount）
    local img=$1 mnt=$2
    shift 2
    _start_mount "$mnt" "${MKIFS[@]}" mount "$img" "$mnt" --log "$TMP/fuse.log" "$@" \
        || return 1
    ok "mkifs mount $img -> $mnt $*"
    return 0
}

unmount_kifs() {                       # unmount_kifs MNT
    local mnt=$1 i
    "$FUSERMOUNT" -u "$mnt" 2>/dev/null || true
    for i in $(seq 1 100); do
        mountpoint -q "$mnt" || break
        sleep 0.05
    done
    if mountpoint -q "$mnt"; then
        log "普通卸载超时，尝试 lazy unmount"
        "$FUSERMOUNT" -uz "$mnt" 2>/dev/null || true
        sleep 0.2
    fi
    mountpoint -q "$mnt" && { MNT=$mnt; return 1; }
    MNT=""
    ok "卸载 $mnt"
    return 0
}

fsck_img() {                           # fsck_img IMG [额外参数...]
    local img=$1
    shift
    local out
    out=$("${MKIFS[@]}" fsck "$img" "$@" 2>&1) || {
        log "$out"
        die "fsck 失败: $img $*"
    }
    ok "fsck: $out"
}

read_out() {                           # read_out IMG PATH OUT [额外参数...]
    local img=$1 path=$2 out=$3
    shift 3
    "${MKIFS[@]}" read "$img" "$path" --out "$out" "$@" >/dev/null \
        || die "mkifs read $img $path 失败"
}

size_of() { stat -c '%s' "$1"; }

expect_size() {                        # expect_size PATH BYTES
    local got
    got=$(size_of "$1")
    [ "$got" = "$2" ] || die "大小不对: $1 = $got，期望 $2"
}

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

# ---------------------------------------------------------------- 单张镜像流程
test_image() {
    local bs=$1 size_mib=$2 tag=$3
    local img="$TMP/$tag.img" mnt="$TMP/$tag.mnt"
    local src="$TMP/$tag.src" extra="$TMP/$tag.extra"
    local cp_kb dd_mib trunc_down trunc_up file_bytes dd_bytes
    if [ "$size_mib" -lt 8 ]; then       # 小镜像：缩小文件，留出 CoW 余量
        cp_kb=768; dd_mib=1; trunc_down=300000; trunc_up=900000
    else
        cp_kb=2560; dd_mib=3; trunc_down=100000; trunc_up=1234567
    fi
    file_bytes=$((cp_kb * 1024))
    dd_bytes=$((dd_mib * 1024 * 1024))

    log "=== [$tag] ${size_mib} MiB / block=${bs}B ==="
    "${MKIFS[@]}" format "$img" --size-mib "$size_mib" --block-size "$bs" >/dev/null \
        || die "format 失败"

    mount_kifs "$img" "$mnt" || die "挂载失败"

    # 1. 空根目录
    [ "$(ls -A "$mnt" | wc -l)" -eq 0 ] || die "新镜像根目录应为空"
    ok "readdir 空根目录"

    # 2. 目录树
    mkdir -p "$mnt/d1/d2" || die "mkdir -p 失败"
    [ -d "$mnt/d1/d2" ] || die "mkdir 后目录不存在"
    ok "mkdir -p d1/d2"

    # 3. 小文件（inline）
    printf 'hello kifs %s\n' "$tag" >"$mnt/d1/hello.txt" || die "写小文件失败"
    cmp -s <(printf 'hello kifs %s\n' "$tag") "$mnt/d1/hello.txt" || die "小文件内容不一致"
    ok "小文件写/读"

    # 4. cp 主机文件
    head -c "$file_bytes" /dev/urandom >"$src"
    cp "$src" "$mnt/d1/cp.bin" || die "cp 失败"
    cmp -s "$src" "$mnt/d1/cp.bin" || die "cp 后字节不一致"
    ok "cp $((file_bytes / 1024)) KiB + cmp"

    # 5. dd 直接写挂载点
    dd if=/dev/urandom of="$mnt/d1/dd.bin" bs=1M count="$dd_mib" status=none || die "dd 失败"
    local dd_sha
    dd_sha=$(sha_of "$mnt/d1/dd.bin")
    ok "dd ${dd_mib} MiB"

    # 6. rename（同目录、跨目录）
    mv "$mnt/d1/dd.bin" "$mnt/d1/d2/dd.bin" || die "同目录 rename 失败"
    [ ! -e "$mnt/d1/dd.bin" ] || die "rename 后旧路径仍存在"
    mv "$mnt/d1/d2/dd.bin" "$mnt/dd2.bin" || die "跨目录 rename 失败"
    [ ! -e "$mnt/d1/d2/dd.bin" ] || die "跨目录 rename 后旧路径仍存在"
    [ "$(sha_of "$mnt/dd2.bin")" = "$dd_sha" ] || die "rename 后内容变了"
    ok "rename 同目录/跨目录 + 内容不变"

    # 7. chmod
    chmod 640 "$mnt/d1/cp.bin" || die "chmod 失败"
    [ "$(stat -c '%a' "$mnt/d1/cp.bin")" = "640" ] \
        || die "chmod 后 mode=$(stat -c '%a' "$mnt/d1/cp.bin")，期望 640"
    ok "chmod 640"

    # 8. truncate 向下
    truncate -s "$trunc_down" "$mnt/d1/cp.bin" || die "truncate 向下失败"
    expect_size "$mnt/d1/cp.bin" "$trunc_down"
    cmp -s <(head -c "$trunc_down" "$src") "$mnt/d1/cp.bin" || die "truncate 后前缀不一致"
    ok "truncate 向下 $trunc_down"

    # 9. truncate 向上（补零）
    truncate -s "$trunc_up" "$mnt/d1/cp.bin" || die "truncate 向上失败"
    expect_size "$mnt/d1/cp.bin" "$trunc_up"
    cmp -s -n "$trunc_down" "$src" "$mnt/d1/cp.bin" || die "truncate 向上后前缀不一致"
    [ "$(tail -c +$((trunc_down + 1)) "$mnt/d1/cp.bin" | tr -d '\0' | wc -c)" -eq 0 ] \
        || die "truncate 向上后尾部不是零"
    ok "truncate 向上 $trunc_up（尾部补零）"

    # 10. 追加（顺序追加快速路径）
    head -c 65536 /dev/urandom >"$extra"
    cat "$extra" >>"$mnt/dd2.bin" || die "append 失败"
    expect_size "$mnt/dd2.bin" $((dd_bytes + 65536))
    local append_sha
    append_sha=$(sha_of "$mnt/dd2.bin")
    ok "append 64 KiB"

    # 11. fsync / flush
    "$PY" - "$mnt/d1/cp.bin" <<'PYEOF' || die "fsync 失败"
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
os.fsync(fd)
os.close(fd)
PYEOF
    sync
    ok "fsync + sync"

    # 12. statfs
    "$PY" - "$mnt" "$bs" <<'PYEOF' || die "statfs 检查失败"
import os, sys
st = os.statvfs(sys.argv[1])
bs = int(sys.argv[2])
assert st.f_bsize == bs, (st.f_bsize, bs)
assert st.f_blocks > 0 and st.f_bfree <= st.f_blocks, (st.f_blocks, st.f_bfree)
print("  ok   statvfs f_bsize=%d blocks=%d free=%d" % (st.f_bsize, st.f_blocks, st.f_bfree))
PYEOF

    # 13. unlink / rmdir（含非空目录必须失败）
    rm "$mnt/d1/hello.txt" || die "unlink 失败"
    [ ! -e "$mnt/d1/hello.txt" ] || die "unlink 后文件仍存在"
    rmdir "$mnt/d1" 2>/dev/null && die "rmdir 非空目录居然成功"
    rmdir "$mnt/d1/d2" || die "rmdir 空目录失败"
    [ ! -e "$mnt/d1/d2" ] || die "rmdir 后目录仍存在"
    ok "unlink + rmdir（非空目录被拒）"

    # 14. 卸载前把内容抄到主机，卸载后逐字节比对
    cp "$mnt/dd2.bin" "$TMP/$tag.dd2.expect" || die "读回 dd2.bin 失败"
    cp "$mnt/d1/cp.bin" "$TMP/$tag.cp.expect" || die "读回 cp.bin 失败"
    [ "$(sha_of "$TMP/$tag.dd2.expect")" = "$append_sha" ] || die "dd2.bin 读回 sha 变了"

    unmount_kifs "$mnt" || die "卸载失败"

    # 15. fsck + 离线读字节比对
    fsck_img "$img"
    read_out "$img" /dd2.bin "$TMP/$tag.dd2.out"
    cmp -s "$TMP/$tag.dd2.out" "$TMP/$tag.dd2.expect" || die "离线读 dd2.bin 字节不一致"
    read_out "$img" /d1/cp.bin "$TMP/$tag.cp.out"
    cmp -s "$TMP/$tag.cp.out" "$TMP/$tag.cp.expect" || die "离线读 cp.bin 字节不一致"
    expect_size "$TMP/$tag.cp.out" "$trunc_up"
    ok "离线 fsck + read 字节比对"

    # 16. 重新挂载，验证持久化，再追加一次（覆盖「卸载重挂后 append」）
    mount_kifs "$img" "$mnt" || die "重新挂载失败"
    [ "$(sha_of "$mnt/dd2.bin")" = "$append_sha" ] || die "重挂后 dd2.bin sha 变了"
    expect_size "$mnt/d1/cp.bin" "$trunc_up"
    head -c 32768 /dev/urandom >"$extra"
    cat "$extra" >>"$mnt/d1/cp.bin" || die "重挂后 append 失败"
    expect_size "$mnt/d1/cp.bin" $((trunc_up + 32768))
    cp "$mnt/d1/cp.bin" "$TMP/$tag.cp2.expect" || die "读回 cp.bin（追加后）失败"
    unmount_kifs "$mnt" || die "第二次卸载失败"
    fsck_img "$img"
    read_out "$img" /d1/cp.bin "$TMP/$tag.cp2.out"
    cmp -s "$TMP/$tag.cp2.out" "$TMP/$tag.cp2.expect" || die "重挂 append 后字节不一致"
    ok "卸载重挂 + append 后 fsck/字节比对"

    # 17. 只读挂载
    mount_kifs "$img" "$mnt" --read-only || die "只读挂载失败"
    grep -qE '(^|,)ro(,|$)' <(awk -v m="$mnt" '$2==m {print $4}' /proc/mounts) || die "挂载表里没有 ro"
    cmp -s "$TMP/$tag.cp2.expect" "$mnt/d1/cp.bin" || die "只读挂载读内容不一致"
    echo x >>"$mnt/d1/cp.bin" 2>/dev/null && die "只读挂载竟然允许写"
    mkdir "$mnt/nope" 2>/dev/null && die "只读挂载竟然允许 mkdir"
    ok "只读挂载：可读、写/mkdir 被拒"
    unmount_kifs "$mnt" || die "只读卸载失败"
    fsck_img "$img"
}

# ---------------------------------------------------------------- 符号链接（type=3）
test_symlink() {
    local img="$TMP/sym.img" mnt="$TMP/sym.mnt" long_t line got
    log "=== [符号链接] ==="
    "${MKIFS[@]}" format "$img" --size-mib 4 >/dev/null || die "format 失败"
    "${MKIFS[@]}" mkdir "$img" /dir >/dev/null || die "mkdir /dir 失败"
    "${MKIFS[@]}" write "$img" /dir/target.txt --data "link target content" >/dev/null \
        || die "写 /dir/target.txt 失败"
    long_t="/$(printf 'z%.0s' $(seq 1 199))"

    mount_kifs "$img" "$mnt" || die "挂载失败"
    ln -s target.txt "$mnt/dir/link" || die "ln -s 相对链接失败"
    ln -s /dir "$mnt/dir/dirlink" || die "ln -s 目录链接失败"
    ln -s /nonexistent/x "$mnt/dir/dangling" || die "ln -s dangling 失败"
    ln -s "$long_t" "$mnt/dir/longlink" || die "ln -s 长目标失败"
    [ "$(readlink "$mnt/dir/link")" = "target.txt" ] || die "readlink 结果不对"
    ok "ln -s + readlink"
    [ "$(cat "$mnt/dir/link")" = "link target content" ] || die "cat 跟随链接读目标失败"
    ok "cat 链接（跟随读到目标内容）"
    line=$(ls -l "$mnt/dir" | grep ' link -> target\.txt$') || die "ls -l 未显示 link -> target.txt"
    case "$line" in
        l*) ;;
        *) die "ls -l 首字符不是 l: $line" ;;
    esac
    ok "ls -l 显示 l 前缀 + -> target"
    [ "$(stat -c '%F' "$mnt/dir/link")" = "symbolic link" ] || die "stat -c %F 不是 symbolic link"
    [ "$(stat -c '%a' "$mnt/dir/link")" = "777" ] || die "符号链接 mode 不是 777"
    ok "stat: symbolic link / mode 777"
    [ "$(readlink "$mnt/dir/dangling")" = "/nonexistent/x" ] || die "dangling readlink 失败"
    cat "$mnt/dir/dangling" 2>/dev/null && die "dangling 链接居然能 cat"
    ok "dangling：readlink 正常，cat 报 ENOENT"
    [ "$(ls "$mnt/dir/dirlink/")" = "target.txt" ] || die "目录链接 ls 失败"
    ok "目录链接：ls link/ 正常"
    [ "$(readlink "$mnt/dir/longlink")" = "$long_t" ] || die "长目标 readlink 不一致"
    ok "长目标（200B）readlink"
    rm "$mnt/dir/link" || die "unlink 符号链接失败"
    [ ! -e "$mnt/dir/link" ] || die "unlink 后链接仍存在"
    ok "unlink 符号链接"
    unmount_kifs "$mnt" || die "卸载失败"

    fsck_img "$img"
    got=$("${MKIFS[@]}" readlink "$img" /dir/longlink) || die "离线 readlink 失败"
    [ "$got" = "$long_t" ] || die "离线 readlink 不一致"
    "${MKIFS[@]}" stat "$img" /dir/longlink | grep -q 'type       : symlink' \
        || die "离线 stat type 不是 symlink"
    "${MKIFS[@]}" stat "$img" /dir/longlink | grep -q 'blocks     : 1' \
        || die "离线 stat 长目标未走 extent"
    "${MKIFS[@]}" ls "$img" /dir | grep -q 'l .* longlink -> ' \
        || die "离线 ls 未显示 l / -> target"
    ok "离线 readlink/stat/ls"
    fsck_img "$img"
}

# ---------------------------------------------------------------- 带 hide/ro 位的文件
test_perm_flags() {
    local img="$TMP/perm.img" mnt="$TMP/perm.mnt"
    log "=== [hide/ro 权限位] ==="
    "${MKIFS[@]}" format "$img" --size-mib 4 >/dev/null || die "format 失败"
    "${MKIFS[@]}" write "$img" /hidden --data secret --perm 'rw-r--r--110' >/dev/null
    "${MKIFS[@]}" write "$img" /locked --data readonly --perm 'rw-r--r--010' >/dev/null
    "${MKIFS[@]}" write "$img" /normal --data normal >/dev/null

    mount_kifs "$img" "$mnt" || die "挂载失败"
    ls "$mnt" | grep -qx hidden && die "hide 文件出现在 readdir 里"
    cat "$mnt/hidden" 2>/dev/null && die "hide 文件居然能按名打开"
    [ "$(cat "$mnt/locked")" = "readonly" ] || die "ro 文件读失败"
    echo x >>"$mnt/locked" 2>/dev/null && die "ro 位文件竟然可写"
    unmount_kifs "$mnt" || die "卸载失败"

    mount_kifs "$img" "$mnt" --show-hidden || die "挂载失败"
    [ "$(cat "$mnt/hidden")" = "secret" ] || die "--show-hidden 下读不到 hide 文件"
    unmount_kifs "$mnt" || die "卸载失败"
    fsck_img "$img"
    ok "hide/ro 位映射"
}

# ---------------------------------------------------------------- offset 挂载（包裹文件模拟卷在设备内偏移）
test_offset() {
    local wrap="$TMP/wrap.bin" mnt="$TMP/wrap.mnt" prefix=1048576
    log "=== [--offset 挂载] ==="
    "${MKIFS[@]}" format "$TMP/wrap.vol" --size-mib 4 >/dev/null || die "format 失败"
    head -c "$prefix" /dev/urandom >"$wrap"
    cat "$TMP/wrap.vol" >>"$wrap"
    cp "$wrap" "$TMP/wrap.orig"
    mount_cli "$wrap" "$mnt" --offset "$prefix" || die "offset 挂载失败"
    dd if=/dev/urandom of="$mnt/blob" bs=64K count=8 status=none || die "offset 卷写失败"
    cp "$mnt/blob" "$TMP/blob.expect" || die "offset 卷读回失败"
    unmount_kifs "$mnt" || die "卸载失败"
    cmp -s <(head -c "$prefix" "$wrap") <(head -c "$prefix" "$TMP/wrap.orig") \
        || die "offset 前缀被破坏"
    fsck_img "$wrap" --offset "$prefix"
    read_out "$wrap" /blob "$TMP/blob.out" --offset "$prefix"
    cmp -s "$TMP/blob.out" "$TMP/blob.expect" || die "offset 卷离线读字节不一致"
    "${MKIFS[@]}" fsck "$wrap" >/dev/null 2>&1 && die "错误 offset 的 fsck 应失败"
    ok "offset 挂载 + 前缀保护 + 离线 fsck/read"
}


# ---------------------------------------------------------------- --offset-lba（卷在设备内 LBA 偏移，模拟整盘固定分区）
test_offset_lba() {
    local disk="$TMP/disk.img" mnt="$TMP/lba.mnt" lba=22528 prefix=$((22528 * 512))
    log "=== [--offset-lba 挂载] ==="
    truncate -s 16M "$disk" || die "建盘镜像失败"
    "${MKIFS[@]}" format "$disk" --offset-lba "$lba" --size-mib 4 >/dev/null \
        || die "format --offset-lba 失败"
    mount_cli "$disk" "$mnt" --offset-lba "$lba" || die "offset-lba 挂载失败"
    dd if=/dev/urandom of="$mnt/blob" bs=64K count=4 status=none || die "offset-lba 卷写失败"
    cp "$mnt/blob" "$TMP/lba.expect" || die "offset-lba 卷读回失败"
    unmount_kifs "$mnt" || die "卸载失败"
    fsck_img "$disk" --offset-lba "$lba"
    read_out "$disk" /blob "$TMP/lba.out" --offset-lba "$lba"
    cmp -s "$TMP/lba.out" "$TMP/lba.expect" || die "offset-lba 离线读字节不一致"
    [ "$(head -c "$prefix" "$disk" | tr -d '\0' | wc -c)" -eq 0 ] \
        || die "offset-lba 前缀被破坏"
    "${MKIFS[@]}" fsck "$disk" >/dev/null 2>&1 && die "错误 offset-lba 的 fsck 应失败"
    ok "offset-lba 挂载 + 前缀保护 + 离线 fsck/read"
}

# ---------------------------------------------------------------- root + loop 设备（可选）
test_loop_device() {
    [ "$(id -u)" -eq 0 ] || { log "=== [块设备] 跳过（需要 root） ==="; return 0; }
    command -v losetup >/dev/null 2>&1 || { log "=== [块设备] 跳过（没有 losetup） ==="; return 0; }
    local img="$TMP/loop.img" mnt="$TMP/loop.mnt"
    log "=== [块设备：loop] ==="
    "${MKIFS[@]}" format "$img" --size-mib 16 >/dev/null || die "format 失败"
    truncate -s 16M "$TMP/loopdev.bin" || die "建 loop 后备文件失败"
    LOOPDEV=$(losetup -f --show "$TMP/loopdev.bin") || die "losetup 失败"
    dd if="$img" of="$LOOPDEV" bs=1M conv=fsync status=none || die "写 loop 设备失败"
    mount_cli "$LOOPDEV" "$mnt" || die "块设备挂载失败"
    printf 'loop device\n' >"$mnt/hello" || die "块设备写失败"
    [ "$(cat "$mnt/hello")" = "loop device" ] || die "块设备读写失败"
    unmount_kifs "$mnt" || die "块设备卸载失败"
    fsck_img "$LOOPDEV"
    losetup -d "$LOOPDEV" || true
    LOOPDEV=""
    ok "loop 块设备挂载/写入/fsck"
}

# ---------------------------------------------------------------- main
log "临时目录: $TMP"
test_image 4096 4 4k4m
test_image 16384 64 16k64m
test_perm_flags
test_symlink
test_offset
test_offset_lba
test_loop_device

if [ "$FAILED" -ne 0 ]; then
    log "❌ 有测试失败"
    exit 1
fi
log "🎉 FUSE 端到端测试全部通过"
exit 0

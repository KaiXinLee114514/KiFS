; ============================================================================
;  kernel/kifs.asm — KiFS 只读驱动(Phase B0)
;
;  KiFS 是 KKernel 自己的文件系统(设计文档在 kifs 仓库 docs/kifs.md,
;  宿主工具 tools/mkifs.py 生成/检查镜像、fsck 当标准答案)。Phase B0 只做
;  **内核侧只读挂载**,命令组:
;
;      kifs info              看 superblock:magic/version/块大小/总块数/
;                             root_oid/free_blocks/卷标
;      kifs ls [path]         列目录(省略路径 = 根目录 /)
;      kifs cat <path>        打印文件内容(内联 / 单段多块 extent)
;      kifs stat <path>       oid / 类型 / 权限 / 大小 / 块数 / 内联长度 /
;                             extent / 父 oid
;
;  为什么固定 LBA 而不是自动探测:Phase B0 是"先让内核能读一致格式"的
;  最小闭环,分区位置和宿主测试镜像焊死(22528 = 11 MiB),省掉挂载表、
;  分区表解析和 VFS 层;Phase B1 再谈 /MNT/KIFS。**没挂 VFS,只读**,
;  和 FAT16 命令各读各的分区,互不影响。
;
;  静默原则:普通镜像 / 软盘 / FAT 盘上这个位置没有 KiFS(或校验不过),
;  kifs_init 一声不吭地返回,kifs_ok=0;现有测试一个都不受影响。
;
;  内存(0x3C3000-0x3C5FFF,与 kmain.asm 顶部布局表同步;0x3C2000 之后
;  到 0x3C2FFF 是 usbdisk 的 4 KiB 数据缓冲,别越界):
;      0x3C3000  4 KiB 块缓冲(ATA 读盘目标)
;      0x3C4000  4 KiB 目录内容缓冲
;      0x3C5000  128 B 当前 record
;      0x3C5100  256 B 路径缓冲
;      全局变量在下面数据段(内核映像里,可写)
;
;  磁盘布局(全部小端;权威实现:宿主 tools/mkifs.py):
;    superblock(512 B;主副本在**分区块 1** —— 块 0 是引导块留空,
;      设计文档写的是"块 0",但权威实现 mkifs.py 是块 1,以 py 为准):
;      magic 'KIFS'@0(0x5346494B), version 0x00010000@4, block_size 4096@8,
;      total_blocks@12, data_start@20, data_next@24, meta_next@28,
;      record_start@40, record_count@44, root_oid@48, free_blocks@52,
;      flags@56, uuid@60(16 B), label@76(32 B),
;      checksum = crc32(前 108 字节)@108,其余补 0
;      备份副本在块 2 和最后一块(本驱动只读块 1,不信备份)
;    record 128 B(record 表从块 record_start 起,第 oid 个在 +oid*128):
;      oid@0, type@4(1=文件 2=目录), flags@5, perm@6,
;      size@8, blocks@12, parent@16, ctime@20, mtime@24, kw_index@28,
;      n_ext@32, inline_len@33, inline_off@34,
;      extents@36(3 × 12 B:start u32 / count u16 / pad u16),
;      ext_index@72, n_ext_total@76, next_free_oid@80, owner@84(16 B)
;      内联数据从 inline_off(36)起,最多 92 B;内联时 extent 区不能读
;      —— v0 宿主工具把 36..128 整段留给内联数据,owner/extent 都被盖住
;    目录内容 = 一串目录项:oid u32@0, type u8@4, name_len u8@5,
;      entry_len u16@6,名字 UTF-8@8,entry_len 4 对齐,按名字字节序排
;    perm 16 位:bit0-2 owner rwx,bit3-5 group,bit6-8 other,
;      bit9 hide,bit10 ro,bit11 iolock(只读驱动:只展示,不校验)
;
;  只读:本文件没有一个 ata_write_sectors 调用(故意的)。
; ============================================================================

; ---------------------------------------------------------------------------
;  常量 / 固定缓冲
; ---------------------------------------------------------------------------
KIFS_PART_LBA     equ 22528              ; KiFS 分区固定起点(11 MiB / 8 = LBA 22528)
KIFS_MAGIC        equ 0x5346494B         ; dword 'KIFS'(小端)
KIFS_VERSION      equ 0x00010000
KIFS_BLOCK_SIZE   equ 4096
KIFS_BLOCK_SECTORS equ KIFS_BLOCK_SIZE / 512
KIFS_INLINE_OFF   equ 36                 ; v0 宿主工具固定内联数据偏移
KIFS_INLINE_MAX   equ 92
KIFS_REC_SIZE     equ 128
KIFS_DIR_MAX      equ 4096               ; Phase B0:目录内容最大 4 KiB(单块)
KIFS_CAT_MAX      equ 61440              ; Phase B0:cat 上限 60 KiB(流式,不落大缓冲)

; Phase C1 写入驱动
KIFS_DATA_MAX       equ 32768              ; 单文件上限(32 KiB)
KIFS_BITMAP_BUF     equ 0x3C6000           ; 位图块缓存(4 KiB)
KIFS_JRN_BUF        equ 0x3C7000           ; journal 块构建 / 目标块暂存
KIFS_DATA_BUF       equ 0x3C8000           ; 写入数据 / 目录内容 staging(32 KiB)
KIFS_SB_BUF         equ 0x3D2000           ; superblock 影子(512 B)
KIFS_DELTAS         equ 0x3D2200           ; delta 表 16 × 144 B
KIFS_DELTA_MAX      equ 16
KIFS_REC2_BUF       equ 0x3D2B00           ; 第二条 record 暂存
KIFS_PARENT_BUF     equ 0x3D2B80           ; 父路径(256 B)
KIFS_SB_OLD         equ 0x3D2C80           ; 上次提交的 sb 前 112 B
KIFS_TXN            equ 0x3D2D00           ; 事务表 64 × 20 B
KIFS_TXN_MAX        equ 64
KIFS_JRN_MAGIC      equ 0x4E524A4B         ; 'KJRN'
KIFS_JRN_PAYLOAD    equ 4076               ; 4096 - 20
KIFS_JF_VALID       equ 1
KIFS_JF_BEGIN       equ 2
KIFS_JF_COMMIT      equ 4
KIFS_JF_END         equ 8
KIFS_PERM_FILE      equ 0x4B               ; rw-r--r--
KIFS_PERM_DIR       equ 0x16F              ; rwxr-xr-x
KIFS_PERM_RO        equ 0x400
KIFS_ERR_EXISTS     equ 6
KIFS_ERR_NOTEMPTY   equ 7
KIFS_ERR_RO         equ 8
KIFS_ERR_NOROOT     equ 9
KIFS_ERR_NOSPC      equ 10
KIFS_ERR_TOOBIG     equ 11
KIFS_ERR_RECFULL    equ 12
KIFS_ERR_JRNFULL    equ 13
KIFS_ERR_WRITEERR   equ 14

KIFS_IO_BUF       equ 0x3C3000           ; 4 KiB 块缓冲
KIFS_DIR_BUF      equ 0x3C4000           ; 4 KiB 目录内容缓冲
KIFS_REC_BUF      equ 0x3C5000           ; 128 B 当前 record
KIFS_PATH_BUF     equ 0x3C5100           ; 256 B 路径缓冲

; kifs_err 错误码(命令层翻译成人话)
KIFS_ERR_NONE     equ 0
KIFS_ERR_NOENT    equ 1                  ; 路径不存在
KIFS_ERR_NOTDIR   equ 2                  ; 不是目录
KIFS_ERR_NOTFILE  equ 3                  ; 不是文件
KIFS_ERR_DIRBIG   equ 4                  ; 目录内容超出 4 KiB
KIFS_ERR_IO       equ 5                  ; 读盘失败

; ===========================================================================
;  kifs_crc32:esi = 数据,ecx = 长度 → eax = zlib crc32
;  和宿主 Python 的 zlib.crc32 逐位一致(多项式 0xEDB88320,初值/终值取反)
; ===========================================================================
kifs_crc32:
    push ebx
    push edx
    push ecx
    push esi
    mov eax, 0xFFFFFFFF
.next_byte:
    test ecx, ecx
    jz .done
    movzx edx, byte [esi]
    xor al, dl
    mov ebx, 8
.next_bit:
    shr eax, 1
    jnc .no_xor
    xor eax, 0xEDB88320
.no_xor:
    dec ebx
    jnz .next_bit
    inc esi
    dec ecx
    jmp .next_byte
.done:
    not eax
    pop esi
    pop ecx
    pop edx
    pop ebx
    ret

; ===========================================================================
;  kifs_read_block:eax = 块号 → 读进 KIFS_IO_BUF;返回 eax = 0 / -1
;  块 LBA = 22528 + 块号 × 8(4096 B = 8 扇区)
; ===========================================================================
kifs_read_block:
    pushad
    shl eax, 3                            ; 块号 → 扇区偏移
    add eax, KIFS_PART_LBA
    mov ecx, KIFS_BLOCK_SECTORS
    mov edi, KIFS_IO_BUF
    call ata_read_sectors                 ; eax=0 成功 / -1 失败,其它寄存器不动
    mov [kifs_rb_ret], eax
    popad
    mov eax, [kifs_rb_ret]
    ret

; ===========================================================================
;  kifs_read_record:eax = oid, edi = 128 B 目标 → eax = 0 / -1
;  oid 合法范围 1..record_count-1;record 在块 record_start + oid/32,
;  块内偏移 (oid & 31) * 128
;  (保留寄存器;内部用 KIFS_IO_BUF 做块缓冲,调用者别同时指望它的内容)
; ===========================================================================
kifs_read_record:
    pushad
    mov [kifs_rr_oid], eax
    mov [kifs_rr_dst], edi
    cmp eax, [kifs_record_count]
    jae .fail
    test eax, eax
    jz .fail
    ; 块号 = record_start + oid/32
    mov edx, eax
    shr edx, 5
    add edx, [kifs_record_start]
    mov eax, edx
    call kifs_read_block
    test eax, eax
    jnz .fail
    ; 块内偏移 = (oid & 31) * 128
    mov eax, [kifs_rr_oid]
    and eax, 31
    shl eax, 7
    add eax, KIFS_IO_BUF
    mov esi, eax
    mov edi, [kifs_rr_dst]
    mov ecx, KIFS_REC_SIZE
    cld
    rep movsb
    mov dword [kifs_rr_ret], 0
    jmp .out
.fail:
    mov dword [kifs_rr_ret], -1
.out:
    popad
    mov eax, [kifs_rr_ret]
    ret

; ===========================================================================
;  kifs_read_content:esi = record 指针,edi = 目标,ecx = 最多读多少
;   → eax = 实际读到的字节数 / -1
;  内联:从 inline_off 拷 min(inline_len, max) 字节
;  多块:按 extent 一段段读块、逐块拷贝,总共不超过 min(size, max)
;  (目录内容也走这里;Phase B0 目录 ≤ 4 KiB,所以最多一块)
; ===========================================================================
kifs_read_content:
    pushad
    mov [kifs_rc_rec], esi
    mov [kifs_rc_dst], edi
    mov [kifs_rc_max], ecx
    mov dword [kifs_rc_written], 0
    ; ---- 内联? ----
    movzx eax, byte [esi + 33]            ; inline_len
    test eax, eax
    jz .extents
    ; 内联边界自洽:inline_off + inline_len 不能越过 record
    movzx edx, word [esi + 34]            ; inline_off
    lea ebx, [eax + edx]
    cmp ebx, KIFS_REC_SIZE
    ja .fail
    ; n = min(inline_len, max)
    cmp eax, [kifs_rc_max]
    jbe .inline_n_ok
    mov eax, [kifs_rc_max]
.inline_n_ok:
    mov [kifs_rc_n], eax
    mov ecx, eax
    mov esi, [kifs_rc_rec]
    movzx edx, word [esi + 34]
    add esi, edx
    mov edi, [kifs_rc_dst]
    cld
    rep movsb
    mov eax, [kifs_rc_n]
    jmp .out
.extents:
    ; 目标读取长度 = min(size, max)
    mov esi, [kifs_rc_rec]
    mov eax, [esi + 8]                    ; size
    cmp eax, [kifs_rc_max]
    jbe .size_ok
    mov eax, [kifs_rc_max]
.size_ok:
    mov [kifs_rc_left], eax
    movzx eax, byte [esi + 32]            ; n_ext
    mov [kifs_rc_ne], eax
    mov dword [kifs_rc_exti], 0
    mov eax, [kifs_rc_dst]
    mov [kifs_rc_cur], eax
.ext_loop:
    cmp dword [kifs_rc_left], 0
    je .ext_done
    cmp dword [kifs_rc_ne], 0
    je .ext_done
    ; extent[i] 在 record + 36 + i*12
    mov eax, [kifs_rc_exti]
    imul eax, eax, 12
    mov esi, [kifs_rc_rec]
    lea esi, [esi + eax + 36]
    mov eax, [esi]                        ; start 块
    mov [kifs_rc_blk], eax
    movzx eax, word [esi + 4]             ; 块数
    mov [kifs_rc_bcnt], eax
.blk_loop:
    cmp dword [kifs_rc_left], 0
    je .ext_done
    cmp dword [kifs_rc_bcnt], 0
    je .next_ext
    mov eax, [kifs_rc_blk]
    call kifs_read_block
    test eax, eax
    jnz .fail
    mov ecx, [kifs_rc_left]
    cmp ecx, KIFS_BLOCK_SIZE
    jbe .chunk_ok
    mov ecx, KIFS_BLOCK_SIZE
.chunk_ok:
    mov [kifs_rc_n], ecx
    mov esi, KIFS_IO_BUF
    mov edi, [kifs_rc_cur]
    cld
    rep movsb
    mov eax, [kifs_rc_n]
    add [kifs_rc_cur], eax
    sub [kifs_rc_left], eax
    add [kifs_rc_written], eax
    inc dword [kifs_rc_blk]
    dec dword [kifs_rc_bcnt]
    jmp .blk_loop
.next_ext:
    inc dword [kifs_rc_exti]
    dec dword [kifs_rc_ne]
    jmp .ext_loop
.ext_done:
    mov eax, [kifs_rc_written]
    jmp .out
.fail:
    mov eax, -1
.out:
    mov [kifs_rc_ret], eax
    popad
    mov eax, [kifs_rc_ret]
    ret

; ===========================================================================
;  kifs_resolve:esi = 路径(以 0 结尾,绝对/相对都行,"/" = 根)
;   → eax = oid(0 = 失败,kifs_err 里是原因)
;  从 root_oid 开始逐段走目录;每段 = 字节精确比较(区分大小写)
; ===========================================================================
kifs_resolve:
    pushad
    mov [kifs_rs_path], esi
    mov dword [kifs_err], KIFS_ERR_NONE
    mov eax, [kifs_root_oid]
    mov [kifs_rs_oid], eax
.seg_loop:
    ; 跳过 '/'
    mov esi, [kifs_rs_path]
.slash:
    cmp byte [esi], '/'
    jne .seg_start
    inc esi
    jmp .slash
.seg_start:
    cmp byte [esi], 0
    je .ok                                ; 路径走完了
    ; 本段 [start, end)
    mov [kifs_rs_comp], esi
    mov edi, esi
.scan:
    mov al, [edi]
    test al, al
    jz .scan_end
    cmp al, '/'
    je .scan_end
    inc edi
    jmp .scan
.scan_end:
    mov ecx, edi
    sub ecx, esi
    mov [kifs_rs_complen], ecx
    mov [kifs_rs_path], edi               ; 下一轮从这里继续
    ; ---- 读当前 record:必须是目录且内容 ≤ 4 KiB ----
    mov eax, [kifs_rs_oid]
    mov edi, KIFS_REC_BUF
    call kifs_read_record
    test eax, eax
    jnz .err_noent
    cmp byte [KIFS_REC_BUF + 4], 2        ; TYPE_DIR
    jne .err_notdir
    mov eax, [KIFS_REC_BUF + 8]           ; 目录内容大小
    cmp eax, KIFS_DIR_MAX
    ja .err_notdir
    mov esi, KIFS_REC_BUF
    mov edi, KIFS_DIR_BUF
    mov ecx, KIFS_DIR_MAX
    call kifs_read_content
    test eax, eax
    js .err_noent
    mov [kifs_rs_dirsize], eax
    ; ---- 线性扫目录项 ----
    mov dword [kifs_rs_pos], 0
.entry_loop:
    mov edx, [kifs_rs_pos]
    mov eax, [kifs_rs_dirsize]
    sub eax, edx
    cmp eax, 8                            ; 目录项头 8 B
    jb .err_noent
    lea esi, [KIFS_DIR_BUF + edx]
    movzx ecx, word [esi + 6]             ; entry_len
    test ecx, ecx
    jz .err_noent
    movzx eax, byte [esi + 5]             ; name_len
    cmp eax, [kifs_rs_complen]
    jne .entry_next
    ; 名字逐个字节比
    lea esi, [esi + 8]
    mov edi, [kifs_rs_comp]
    mov ecx, [kifs_rs_complen]
    cld
    repe cmpsb
    jne .entry_next
    ; 命中:oid 进状态,继续下一段
    mov edx, [kifs_rs_pos]
    mov eax, [KIFS_DIR_BUF + edx]         ; 子项 oid
    mov [kifs_rs_oid], eax
    jmp .seg_loop
.entry_next:
    mov edx, [kifs_rs_pos]
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    add edx, ecx
    mov [kifs_rs_pos], edx
    jmp .entry_loop
.err_noent:
    mov dword [kifs_err], KIFS_ERR_NOENT
    mov dword [kifs_rs_ret], 0
    jmp .out
.err_notdir:
    mov dword [kifs_err], KIFS_ERR_NOTDIR
    mov dword [kifs_rs_ret], 0
    jmp .out
.ok:
    mov eax, [kifs_rs_oid]
    mov [kifs_rs_ret], eax
.out:
    popad
    mov eax, [kifs_rs_ret]
    ret

; ===========================================================================
;  kifs_print_perm:eax = 16 位 perm → 打印 9 字符 rwxrwxrwx + 3 位标志数字
;  (hide/ro/iolock 只展示,不强制 —— 只读驱动不校验权限)
; ===========================================================================
kifs_print_perm:
    pushad
    mov [kifs_perm_tmp], eax
    xor ebx, ebx                          ; 组内位移 0/3/6
.group:
    cmp ebx, 9
    jae .flags
    mov eax, [kifs_perm_tmp]
    mov ecx, ebx
    shr eax, cl
    mov ecx, eax
    ; r = bit0
    and ecx, 1
    mov al, '-'
    test ecx, ecx
    jz .put_r
    mov al, 'r'
.put_r:
    call term_putc
    mov eax, [kifs_perm_tmp]
    mov ecx, ebx
    shr eax, cl
    mov ecx, eax
    ; w = bit1
    and ecx, 2
    mov al, '-'
    test ecx, ecx
    jz .put_w
    mov al, 'w'
.put_w:
    call term_putc
    mov eax, [kifs_perm_tmp]
    mov ecx, ebx
    shr eax, cl
    mov ecx, eax
    ; x = bit2
    and ecx, 4
    mov al, '-'
    test ecx, ecx
    jz .put_x
    mov al, 'x'
.put_x:
    call term_putc
    add ebx, 3
    jmp .group
.flags:
    mov ebx, 9                            ; hide / ro / iolock
.flag_loop:
    mov eax, [kifs_perm_tmp]
    mov ecx, ebx
    shr eax, cl
    and al, 1
    add al, '0'
    call term_putc
    inc ebx
    cmp ebx, 12
    jb .flag_loop
    popad
    ret

; ===========================================================================
;  kifs_print_owner:esi = record 指针 → 打印 owner(16 B,遇 0 停)
;  注意:v0 宿主工具在**内联** record 里把 36..128 全拿去放内联数据了,
;  owner@84 会被数据覆盖 —— inline_len > 48 时干脆不打(打了是乱码);
;  非内联 record 的 owner 字段是好的。
; ===========================================================================
kifs_print_owner:
    pushad
    cmp byte [esi + 33], 0                ; inline_len
    je .print
    cmp byte [esi + 33], 48
    ja .out                               ; owner 区被内联数据盖住,不显示
.print:
    lea esi, [esi + 84]
    xor ebx, ebx
.loop:
    cmp ebx, 16
    jae .out
    mov al, [esi + ebx]
    test al, al
    jz .out
    call term_putc
    inc ebx
    jmp .loop
.out:
    popad
    ret

; ===========================================================================
;  kifs_copy_path:把 [usbd_rest] 的路径参数拷进 KIFS_PATH_BUF
;   去尾部空格,最多 255 字节,返回 eax = 长度(0 = 没给路径)
; ===========================================================================
kifs_copy_path:
    pushad
    mov esi, [usbd_rest]
    mov edi, KIFS_PATH_BUF
    xor ecx, ecx
.copy:
    cmp ecx, 255
    jae .trim
    mov al, [esi + ecx]
    test al, al
    jz .trim
    mov [edi + ecx], al
    inc ecx
    jmp .copy
.trim:
    test ecx, ecx
    jz .store
    cmp byte [edi + ecx - 1], ' '
    jne .store
    dec ecx
    jmp .trim
.store:
    mov byte [edi + ecx], 0
    mov [kifs_path_len], ecx
    popad
    mov eax, [kifs_path_len]
    ret

; ===========================================================================
;  kifs_path_default:空路径 → 根目录 "/"
; ===========================================================================
kifs_path_default:
    cmp dword [kifs_path_len], 0
    jne .ret
    mov byte [KIFS_PATH_BUF], '/'
    mov byte [KIFS_PATH_BUF + 1], 0
    mov dword [kifs_path_len], 1
.ret:
    ret

; ===========================================================================
;  kifs_print_err:按 kifs_err 打印一句红色错误
; ===========================================================================
kifs_print_err:
    pushad
    mov al, COL_ERR
    call term_set_color
    mov eax, [kifs_err]
    cmp eax, KIFS_ERR_NOTDIR
    jne .e2
    mov esi, msg_kifs_notdir
    jmp .emit
.e2:
    cmp eax, KIFS_ERR_NOTFILE
    jne .e3
    mov esi, msg_kifs_notfile
    jmp .emit
.e3:
    cmp eax, KIFS_ERR_DIRBIG
    jne .e4
    mov esi, msg_kifs_dirbig
    jmp .emit
.e4:
    cmp eax, KIFS_ERR_IO
    jne .e5
    mov esi, msg_kifs_ioerr
    jmp .emit
.e5:
    cmp eax, KIFS_ERR_EXISTS
    jne .e6
    mov esi, msg_kifs_exists
    jmp .emit
.e6:
    cmp eax, KIFS_ERR_NOTEMPTY
    jne .e7
    mov esi, msg_kifs_notempty
    jmp .emit
.e7:
    cmp eax, KIFS_ERR_RO
    jne .e8
    mov esi, msg_kifs_ro
    jmp .emit
.e8:
    cmp eax, KIFS_ERR_NOROOT
    jne .e9
    mov esi, msg_kifs_noroot
    jmp .emit
.e9:
    cmp eax, KIFS_ERR_NOSPC
    jne .e10
    mov esi, msg_kifs_nospc
    jmp .emit
.e10:
    cmp eax, KIFS_ERR_TOOBIG
    jne .e11
    mov esi, msg_kifs_toobig
    jmp .emit
.e11:
    cmp eax, KIFS_ERR_RECFULL
    jne .e12
    mov esi, msg_kifs_recfull
    jmp .emit
.e12:
    cmp eax, KIFS_ERR_JRNFULL
    jne .e13
    mov esi, msg_kifs_jrnfull
    jmp .emit
.e13:
    cmp eax, KIFS_ERR_WRITEERR
    jne .enoent
    mov esi, msg_kifs_writeerr
    jmp .emit
.enoent:
    mov esi, msg_kifs_notfound
.emit:
    call term_print
    mov al, COL_NORMAL
    call term_set_color
    popad
    ret

; ===========================================================================
;  kifs_print_usage:用法错误(红色)
; ===========================================================================
kifs_print_usage:
    pushad
    mov al, COL_ERR
    call term_set_color
    mov esi, msg_kifs_usage
    call term_print
    mov al, COL_NORMAL
    call term_set_color
    popad
    ret

; ===========================================================================
;  kifs_init:探测 + 校验 superblock。成功 kifs_ok=1,失败**什么都不打印**。
;  普通镜像里这块区域是 0,FAT16/FAT32/软盘测试完全不感知。
; ===========================================================================
kifs_init:
    pushad
    mov byte [kifs_ok], 0
    call kifs_mount_sb
    test eax, eax
    js .out
    ; 有已提交未 END 的事务?重放/回收后重新挂载一次(sb 和位图可能变了)
    call kifs_recover
    test eax, eax
    js .out
    call kifs_mount_sb
    test eax, eax
    js .out
    ; 根 record 必须是目录,否则不算 KiFS
    mov eax, [kifs_root_oid]
    mov edi, KIFS_REC_BUF
    call kifs_read_record
    test eax, eax
    jnz .out
    cmp byte [KIFS_REC_BUF + 4], 2
    jne .out
    mov byte [kifs_ok], 1
.out:
    popad
    ret
; ===========================================================================
;  cmd_kifs — shell 命令入口
;    kifs info / kifs ls [path] / kifs cat <path> / kifs stat <path>
;  没镜像时只打一句提示(规格要求的固定文案),不碰 FAT 状态。
; ===========================================================================
cmd_kifs:
    pushad
    ; 第一个词 = 子命令,后面 = 路径参数
    mov esi, [cmd_arg]
    call usbd_first_word                  ; esi/ecx = 词;[usbd_rest] = 词后的参数
    mov [kifs_word_ptr], esi
    mov [kifs_word_len], ecx
    call kifs_copy_path                   ; 路径 → KIFS_PATH_BUF(去尾空格)
    ; ---- 没镜像? ----
    cmp byte [kifs_ok], 0
    jne .have_image
    mov al, COL_ERR
    call term_set_color
    mov esi, msg_kifs_noimg
    call term_print
    mov al, COL_NORMAL
    call term_set_color
    jmp .done
.have_image:
    ; ---- 没子命令 = info ----
    cmp dword [kifs_word_len], 0
    je .do_info
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_info
    call str_eq
    test eax, eax
    jnz .do_info
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_ls
    call str_eq
    test eax, eax
    jnz .do_ls
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_cat
    call str_eq
    test eax, eax
    jnz .do_cat
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_stat
    call str_eq
    test eax, eax
    jnz .do_stat
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_mkdir
    call str_eq
    test eax, eax
    jnz .do_mkdir
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_write
    call str_eq
    test eax, eax
    jnz .do_write
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_rm
    call str_eq
    test eax, eax
    jnz .do_rm
    mov esi, [kifs_word_ptr]
    mov ecx, [kifs_word_len]
    mov edx, n_kifs_journal
    call str_eq
    test eax, eax
    jnz .do_journal
    call kifs_print_usage
    jmp .done

; ---------------------------------------------------------------------------
;  kifs info
; ---------------------------------------------------------------------------
.do_info:
    mov al, COL_HEADER
    call term_set_color
    mov esi, msg_kifs_info_head
    call term_print
    mov al, COL_NORMAL
    call term_set_color
    ; magic + version + block_size
    mov esi, msg_kifs_magic
    call term_print
    mov eax, [kifs_version]
    call term_print_hex
    mov esi, msg_kifs_blocksize
    call term_print
    mov eax, [kifs_block_size]
    call term_print_dec
    mov al, 10
    call term_putc
    ; total_blocks / root_oid / free_blocks
    mov esi, msg_kifs_total
    call term_print
    mov eax, [kifs_total_blocks]
    call term_print_dec
    mov esi, msg_kifs_root
    call term_print
    mov eax, [kifs_root_oid]
    call term_print_dec
    mov esi, msg_kifs_free
    call term_print
    mov eax, [kifs_free_blocks]
    call term_print_dec
    mov al, 10
    call term_putc
    ; label / flags
    mov esi, msg_kifs_label
    call term_print
    mov esi, kifs_label
    call term_print
    mov esi, msg_kifs_flags
    call term_print
    mov eax, [kifs_flags]
    call term_print_dec
    mov al, 10
    call term_putc
    jmp .done

; ---------------------------------------------------------------------------
;  kifs ls [path]
; ---------------------------------------------------------------------------
.do_ls:
    call kifs_path_default
    mov esi, KIFS_PATH_BUF
    call kifs_resolve
    test eax, eax
    jz .err
    ; 目标 record
    mov edi, KIFS_REC_BUF
    call kifs_read_record
    test eax, eax
    jnz .err_io
    cmp byte [KIFS_REC_BUF + 4], 2        ; 必须是目录
    jne .err_notdir
    mov eax, [KIFS_REC_BUF + 8]
    cmp eax, KIFS_DIR_MAX
    ja .err_dirbig
    mov esi, KIFS_REC_BUF
    mov edi, KIFS_DIR_BUF
    mov ecx, KIFS_DIR_MAX
    call kifs_read_content
    test eax, eax
    js .err_io
    mov [kifs_ls_size], eax
    mov dword [kifs_ls_pos], 0
.ls_loop:
    mov edx, [kifs_ls_pos]
    mov eax, [kifs_ls_size]
    sub eax, edx
    cmp eax, 8
    jb .done                              ; 目录项也走完了
    lea esi, [KIFS_DIR_BUF + edx]
    movzx ecx, word [esi + 6]             ; entry_len
    test ecx, ecx
    jz .done
    ; 子项 record(失败就按 entry_len 跳过,不整条命令翻车)
    mov eax, [KIFS_DIR_BUF + edx]         ; 子项 oid
    mov edi, KIFS_REC_BUF
    call kifs_read_record
    test eax, eax
    jnz .ls_next
    ; 类型 d/-
    mov al, '-'
    cmp byte [KIFS_REC_BUF + 4], 2
    jne .ls_kind
    mov al, 'd'
.ls_kind:
    call term_putc
    mov al, ' '
    call term_putc
    ; 权限 12 字符
    movzx eax, word [KIFS_REC_BUF + 6]
    call kifs_print_perm
    ; owner
    mov esi, KIFS_REC_BUF
    call kifs_print_owner
    mov al, ' '
    call term_putc
    ; 文件大小(目录打 "dir",和宿主 mkifs.py ls 一致)
    cmp byte [KIFS_REC_BUF + 4], 2
    jne .ls_size_file
    mov esi, msg_kifs_word_dir
    call term_print
    jmp .ls_size_done
.ls_size_file:
    mov eax, [KIFS_REC_BUF + 8]
    call term_print_dec
.ls_size_done:
    mov al, ' '
    call term_putc
    ; 名字(直接从目录缓冲里取,bytes 原样打)
    mov edx, [kifs_ls_pos]
    lea esi, [KIFS_DIR_BUF + edx + 8]
    movzx ecx, byte [KIFS_DIR_BUF + edx + 5]   ; name_len
    test ecx, ecx
    jz .ls_name_done
.ls_name_loop:
    mov al, [esi]
    call term_putc
    inc esi
    dec ecx
    jnz .ls_name_loop
.ls_name_done:
    mov al, 10
    call term_putc
.ls_next:
    mov edx, [kifs_ls_pos]
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    test ecx, ecx
    jz .done
    add edx, ecx
    mov [kifs_ls_pos], edx
    jmp .ls_loop

; ---------------------------------------------------------------------------
;  kifs cat <path> — 流式输出,内联直接打,extent 按块读、按字节打
;  (不往内存里整份放,所以 60 KiB 上限只是个"别拿它 cat 大文件"的规矩)
; ---------------------------------------------------------------------------
.do_cat:
    cmp dword [kifs_path_len], 0
    je .usage
    mov esi, KIFS_PATH_BUF
    call kifs_resolve
    test eax, eax
    jz .err
    mov edi, KIFS_REC_BUF
    call kifs_read_record
    test eax, eax
    jnz .err_io
    cmp byte [KIFS_REC_BUF + 4], 1        ; 必须是文件
    jne .err_notfile
    mov eax, [KIFS_REC_BUF + 8]
    cmp eax, KIFS_CAT_MAX
    ja .err_filebig
    mov byte [kifs_cat_last], 0
    mov dword [kifs_cat_printed], 0
    ; ---- 内联?先整段读到 IO 缓冲再逐字节打(最多 92 B)----
    cmp byte [KIFS_REC_BUF + 33], 0
    je .cat_ext
    mov esi, KIFS_REC_BUF
    mov edi, KIFS_IO_BUF
    mov ecx, KIFS_INLINE_MAX
    call kifs_read_content
    test eax, eax
    js .err_io
    mov ecx, eax
    test ecx, ecx
    jz .cat_end
    mov esi, KIFS_IO_BUF
    mov dword [kifs_cat_printed], 1
.cat_inline_loop:
    mov al, [esi]
    mov [kifs_cat_last], al
    call term_putc
    inc esi
    dec ecx
    jnz .cat_inline_loop
    jmp .cat_end
.cat_ext:
    mov eax, [KIFS_REC_BUF + 8]
    mov [kifs_cat_left], eax
    movzx eax, byte [KIFS_REC_BUF + 32]   ; n_ext
    mov [kifs_cat_ne], eax
    mov dword [kifs_cat_exti], 0
.cat_ext_loop:
    cmp dword [kifs_cat_left], 0
    je .cat_end
    cmp dword [kifs_cat_ne], 0
    je .cat_end
    mov eax, [kifs_cat_exti]
    imul eax, eax, 12
    lea esi, [KIFS_REC_BUF + eax + 36]
    mov eax, [esi]                        ; start 块
    mov [kifs_cat_blk], eax
    movzx eax, word [esi + 4]             ; 块数
    mov [kifs_cat_bcnt], eax
.cat_block_loop:
    cmp dword [kifs_cat_left], 0
    je .cat_end
    cmp dword [kifs_cat_bcnt], 0
    je .cat_next_ext
    mov eax, [kifs_cat_blk]
    call kifs_read_block
    test eax, eax
    jnz .err_io
    mov ecx, [kifs_cat_left]
    cmp ecx, KIFS_BLOCK_SIZE
    jbe .cat_chunk_ok
    mov ecx, KIFS_BLOCK_SIZE
.cat_chunk_ok:
    mov [kifs_cat_n], ecx
    mov esi, KIFS_IO_BUF
    mov dword [kifs_cat_printed], 1
.cat_print_loop:
    mov al, [esi]
    mov [kifs_cat_last], al
    call term_putc
    inc esi
    dec ecx
    jnz .cat_print_loop
    mov eax, [kifs_cat_n]
    sub [kifs_cat_left], eax
    inc dword [kifs_cat_blk]
    dec dword [kifs_cat_bcnt]
    jmp .cat_block_loop
.cat_next_ext:
    inc dword [kifs_cat_exti]
    dec dword [kifs_cat_ne]
    jmp .cat_ext_loop
.cat_end:
    ; 内容不是以换行结尾(或空文件)就补一个换行,提示符别粘在内容后面
    cmp dword [kifs_cat_printed], 0
    je .cat_newline
    cmp byte [kifs_cat_last], 10
    je .done
.cat_newline:
    mov al, 10
    call term_putc
    jmp .done

; ---------------------------------------------------------------------------
;  kifs stat <path>
; ---------------------------------------------------------------------------
.do_stat:
    call kifs_path_default
    mov esi, KIFS_PATH_BUF
    call kifs_resolve
    test eax, eax
    jz .err
    mov edi, KIFS_REC_BUF
    call kifs_read_record
    test eax, eax
    jnz .err_io
    ; 标题(带路径)
    mov esi, msg_kifs_stat_head
    call term_print
    mov esi, KIFS_PATH_BUF
    call term_print
    mov al, 10
    call term_putc
    ; oid
    mov esi, msg_kifs_stat_oid
    call term_print
    mov eax, [KIFS_REC_BUF]
    call term_print_dec
    ; type
    mov esi, msg_kifs_stat_type
    call term_print
    cmp byte [KIFS_REC_BUF + 4], 2
    jne .stype_file
    mov esi, msg_kifs_word_dir
    jmp .stype_put
.stype_file:
    mov esi, msg_kifs_word_file
.stype_put:
    call term_print
    mov al, 10
    call term_putc
    ; perm + owner
    mov esi, msg_kifs_stat_perm
    call term_print
    movzx eax, word [KIFS_REC_BUF + 6]
    call kifs_print_perm
    mov esi, KIFS_REC_BUF
    call kifs_print_owner
    mov al, 10
    call term_putc
    ; size
    mov esi, msg_kifs_stat_size
    call term_print
    mov eax, [KIFS_REC_BUF + 8]
    call term_print_dec
    mov esi, msg_kifs_bytes
    call term_print
    ; blocks + inline_len
    mov esi, msg_kifs_stat_blocks
    call term_print
    mov eax, [KIFS_REC_BUF + 12]
    call term_print_dec
    mov esi, msg_kifs_stat_inline
    call term_print
    movzx eax, byte [KIFS_REC_BUF + 33]
    call term_print_dec
    mov al, 10
    call term_putc
    ; extents:n_ext + 每段 (start=.. blocks=..)
    mov esi, msg_kifs_stat_ext
    call term_print
    movzx eax, byte [KIFS_REC_BUF + 32]
    call term_print_dec
    mov dword [kifs_st_i], 0
.st_ext_loop:
    mov ebx, [kifs_st_i]
    movzx eax, byte [KIFS_REC_BUF + 32]
    cmp ebx, eax
    jae .st_ext_done
    mov esi, msg_kifs_stat_ext_one
    call term_print
    mov eax, [kifs_st_i]
    imul eax, eax, 12
    lea esi, [KIFS_REC_BUF + eax + 36]
    mov eax, [esi]
    call term_print_dec
    mov esi, msg_kifs_stat_ext_blocks
    call term_print
    mov eax, [kifs_st_i]
    imul eax, eax, 12
    lea esi, [KIFS_REC_BUF + eax + 40]
    movzx eax, word [esi]
    call term_print_dec
    inc dword [kifs_st_i]
    jmp .st_ext_loop
.st_ext_done:
    mov al, 10
    call term_putc
    ; parent
    mov esi, msg_kifs_stat_parent
    call term_print
    mov eax, [KIFS_REC_BUF + 16]
    call term_print_dec
    mov al, 10
    call term_putc
    jmp .done

.do_mkdir:
    call kifs_cmd_mkdir
    jmp .done
.do_write:
    call kifs_cmd_write
    jmp .done
.do_rm:
    call kifs_cmd_rm
    jmp .done
.do_journal:
    call kifs_cmd_journal
    jmp .done

; ---------------------------------------------------------------------------
;  错误出口(路径解析失败 / 读盘失败 / 类型不对 / 目录太大)
; ---------------------------------------------------------------------------
.err:
    call kifs_print_err
    jmp .done
.err_io:
    mov dword [kifs_err], KIFS_ERR_IO
    jmp .err
.err_notdir:
    mov dword [kifs_err], KIFS_ERR_NOTDIR
    jmp .err
.err_notfile:
    mov dword [kifs_err], KIFS_ERR_NOTFILE
    jmp .err
.err_dirbig:
    mov dword [kifs_err], KIFS_ERR_DIRBIG
    jmp .err
.err_filebig:
    mov al, COL_ERR
    call term_set_color
    mov esi, msg_kifs_filebig
    call term_print
    mov al, COL_NORMAL
    call term_set_color
    jmp .done
.usage:
    call kifs_print_usage
.done:
    popad
    ret

; ===========================================================================
;  Phase C1 — journal + CoW 写入
;  ---------------------------------------------------------------------------
;  事务边界 = 一条 kifs 命令(和宿主 tools/mkifs.py 的 Image.flush 一致):
;    1. 数据块 CoW:先分配新块、直接 ATA 写(提交前是孤儿),再释放旧块;
;       位图改动先只在内存里,提交时整块当一个 delta 进 journal。
;    2. 元数据(record / 位图 / superblock 三副本)只改影子,提交时全部
;       作为 delta 进 journal:BEGIN → 记录(≤4076 B/条)→ COMMIT →
;       应用(read-modify-write)→ END 覆盖写回 BEGIN 槽。
;    3. 挂载时扫全部 journal 块:完整事务(有 BEGIN + COMMIT、无 END)按
;       seq 顺序重放;残缺事务(缺 BEGIN 或 COMMIT)写 END 回收槽位。
;  每个块写完都 FLUSH CACHE(ata_write_sectors 内部做),顺序从保守出发。
; ===========================================================================

; ---------------------------------------------------------------------------
;  kifs_read_block_to:eax = 块号,edi = 4096 B 目标 → eax = 0 / -1
; ---------------------------------------------------------------------------
kifs_read_block_to:
    push ecx
    mov ecx, eax
    shl eax, 3
    add eax, KIFS_PART_LBA
    mov ecx, KIFS_BLOCK_SECTORS
    call ata_read_sectors
    pop ecx
    ret

; ---------------------------------------------------------------------------
;  kifs_write_block:eax = 块号,esi = 4096 B 源 → eax = 0 / -1
; ---------------------------------------------------------------------------
kifs_write_block:
    push ecx
    mov ecx, eax
    shl eax, 3
    add eax, KIFS_PART_LBA
    mov ecx, KIFS_BLOCK_SECTORS
    call ata_write_sectors
    pop ecx
    ret

; ---------------------------------------------------------------------------
;  kifs_crc16:esi = 数据,ecx = 长度 → eax = CRC16-CCITT(poly 0x1021,初值 0xFFFF)
;  与宿主 mkifs.py 的 crc16 逐位一致;空数据 = 0xFFFF。保留所有寄存器。
; ---------------------------------------------------------------------------
kifs_crc16:
    push ebx
    push edx
    push ecx
    push esi
    mov eax, 0xFFFF
.next_byte:
    test ecx, ecx
    jz .done
    movzx ebx, byte [esi]
    shl ebx, 8
    xor eax, ebx
    mov edx, 8
.next_bit:
    test eax, 0x8000
    jz .no_xor
    shl eax, 1
    xor eax, 0x1021
    and eax, 0xFFFF
    jmp .bit_next
.no_xor:
    shl eax, 1
    and eax, 0xFFFF
.bit_next:
    dec edx
    jnz .next_bit
    inc esi
    dec ecx
    jmp .next_byte
.done:
    pop esi
    pop ecx
    pop edx
    pop ebx
    ret

; ---------------------------------------------------------------------------
;  位图缓存(KIFS_BITMAP_BUF):eax = 块号 → eax = 0 / 1
; ---------------------------------------------------------------------------
kifs_bit_get:
    push ecx
    push edx
    mov ecx, eax
    shr eax, 3
    and ecx, 7
    movzx edx, byte [KIFS_BITMAP_BUF + eax]
    shr edx, cl
    mov eax, edx
    and eax, 1
    pop edx
    pop ecx
    ret

; ---------------------------------------------------------------------------
;  kifs_bit_set:eax = 块号,edx = 0/1;改内存位图并置 dirty 标记
;  (整块位图在 flush 时作为一个 4096 B delta 落盘,所以只要一个 dirty 位)
; ---------------------------------------------------------------------------
kifs_bit_set:
    push ebx
    push ecx
    mov ebx, eax
    and ebx, 7
    shr eax, 3
    mov ecx, ebx
    mov bl, 1
    shl bl, cl
    cmp edx, 0
    je .clear
    or [KIFS_BITMAP_BUF + eax], bl
    jmp .mark
.clear:
    not bl
    and [KIFS_BITMAP_BUF + eax], bl
.mark:
    mov byte [kifs_bitmap_dirty], 1
    pop ecx
    pop ebx
    ret

; ---------------------------------------------------------------------------
;  kifs_alloc_blocks:ecx = 连续块数 → eax = 起始块号 / 0(失败)
;  从 data_next 起到 meta_next 找连续空闲(和宿主 alloc_blocks 一致):
;  置位、若 first == data_next 则推进 data_next、free_blocks -= count。
; ---------------------------------------------------------------------------
kifs_alloc_blocks:
    pushad
    mov [kifs_ab_len], ecx
    mov dword [kifs_ab_ret], 0
    mov eax, [kifs_data_next]
    mov [kifs_ab_start], eax
.try:
    mov eax, [kifs_ab_start]
    mov ecx, [kifs_ab_len]
    mov edx, [kifs_meta_next]
    sub edx, ecx
    cmp eax, edx
    ja .fail
    mov dword [kifs_ab_i], 0
.chk:
    mov eax, [kifs_ab_start]
    add eax, [kifs_ab_i]
    call kifs_bit_get
    test eax, eax
    jnz .next
    inc dword [kifs_ab_i]
    mov eax, [kifs_ab_i]
    cmp eax, [kifs_ab_len]
    jb .chk
    ; ---- 找到连续空闲:全部置位 ----
    mov dword [kifs_ab_i], 0
.set:
    mov eax, [kifs_ab_start]
    add eax, [kifs_ab_i]
    mov edx, 1
    call kifs_bit_set
    inc dword [kifs_ab_i]
    mov eax, [kifs_ab_i]
    cmp eax, [kifs_ab_len]
    jb .set
    mov eax, [kifs_ab_start]
    cmp eax, [kifs_data_next]
    jne .no_dn
    add eax, [kifs_ab_len]
    mov [kifs_data_next], eax
.no_dn:
    mov eax, [kifs_ab_len]
    sub [kifs_free_blocks], eax
    mov eax, [kifs_ab_start]
    mov [kifs_ab_ret], eax
    jmp .out
.next:
    inc dword [kifs_ab_start]
    jmp .try
.fail:
    mov dword [kifs_ab_ret], 0
.out:
    popad
    mov eax, [kifs_ab_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_free_blocks_range:eax = 起始块,ecx = 块数;清位、free_blocks += n、
;  data_next = min(data_next, start)
; ---------------------------------------------------------------------------
kifs_free_blocks_range:
    pushad
    mov [kifs_fb_start], eax
    mov [kifs_fb_cnt], ecx
    mov dword [kifs_fb_i], 0
.loop:
    mov eax, [kifs_fb_i]
    cmp eax, [kifs_fb_cnt]
    jae .done
    add eax, [kifs_fb_start]
    xor edx, edx
    call kifs_bit_set
    inc dword [kifs_fb_i]
    jmp .loop
.done:
    mov eax, [kifs_fb_cnt]
    add [kifs_free_blocks], eax
    mov eax, [kifs_fb_start]
    cmp eax, [kifs_data_next]
    jae .out
    mov [kifs_data_next], eax
.out:
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_free_rec_data:esi = record → 释放其 extent 数据块,并清掉
;  n_ext / n_ext_total / inline_len 和 extent 表(与宿主 record_free_data 一致;
;  size/blocks 保留,rm 之后 record type=0 就看不到了)
; ---------------------------------------------------------------------------
kifs_free_rec_data:
    pushad
    mov [kifs_fd_rec], esi
    cmp byte [esi + 33], 0
    jne .clear
    movzx eax, byte [esi + 32]
    mov [kifs_fd_ne], eax
    mov dword [kifs_fd_i], 0
.loop:
    mov eax, [kifs_fd_i]
    cmp eax, [kifs_fd_ne]
    jae .clear
    mov esi, [kifs_fd_rec]
    mov ecx, eax
    imul ecx, ecx, 12
    lea edi, [esi + ecx + 36]
    mov eax, [edi]
    movzx ecx, word [edi + 4]
    test ecx, ecx
    jz .next
    call kifs_free_blocks_range
.next:
    inc dword [kifs_fd_i]
    jmp .loop
.clear:
    mov esi, [kifs_fd_rec]
    mov byte [esi + 32], 0
    mov byte [esi + 33], 0
    mov byte [esi + 76], 0
    lea edi, [esi + 36]
    xor eax, eax
    mov ecx, 9                             ; extents 3 × 12 B
    cld
    rep stosd
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_alloc_oid:找 1..record_count-1 里第一个空闲 record → eax = oid / 0
; ---------------------------------------------------------------------------
kifs_alloc_oid:
    pushad
    mov dword [kifs_ao_ret], 0
    mov ebx, 1
.loop:
    cmp ebx, [kifs_record_count]
    jae .out
    mov eax, ebx
    mov edi, KIFS_REC2_BUF
    call kifs_read_record
    test eax, eax
    jnz .out
    cmp byte [KIFS_REC2_BUF + 4], 0
    je .found
    inc ebx
    jmp .loop
.found:
    mov [kifs_ao_ret], ebx
.out:
    popad
    mov eax, [kifs_ao_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_delta_add_small:eax=目标块, edx=块内偏移, ecx=长度(≤128), esi=数据
;  delta 实参拷进 delta 表(record 128 B 会随后的操作被覆盖,必须当快照)
; ---------------------------------------------------------------------------
kifs_delta_add_small:
    pushad
    mov dword [kifs_das_ret], -1
    cmp ecx, 128
    ja .out
    mov edi, [kifs_delta_n]
    cmp edi, KIFS_DELTA_MAX
    jae .out
    imul edi, edi, 144
    add edi, KIFS_DELTAS
    mov [edi + 0], eax
    mov [edi + 4], edx
    mov [edi + 8], ecx
    mov dword [edi + 12], 0                ; ptr=0 → 数据在 entry+16
    add edi, 16
    cld
    rep movsb
    inc dword [kifs_delta_n]
    mov dword [kifs_das_ret], 0
.out:
    popad
    mov eax, [kifs_das_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_delta_add_block:eax=目标块, ecx=长度(任意), esi=数据指针
;  (位图整块 / superblock 512 B 用外置指针,不拷贝)
; ---------------------------------------------------------------------------
kifs_delta_add_block:
    pushad
    mov dword [kifs_dab_ret], -1
    mov edi, [kifs_delta_n]
    cmp edi, KIFS_DELTA_MAX
    jae .out
    imul edi, edi, 144
    add edi, KIFS_DELTAS
    mov [edi + 0], eax
    mov dword [edi + 4], 0
    mov [edi + 8], ecx
    mov [edi + 12], esi
    inc dword [kifs_delta_n]
    mov dword [kifs_dab_ret], 0
.out:
    popad
    mov eax, [kifs_dab_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_write_record:esi = 128 B record → 记一条 delta(oid → 块/偏移)
; ---------------------------------------------------------------------------
kifs_write_record:
    pushad
    mov dword [kifs_wr_ret], -1
    mov [kifs_wr_rec], esi
    mov eax, [esi]
    cmp eax, [kifs_record_count]
    jae .out
    test eax, eax
    jz .out
    mov edx, eax
    and edx, 31
    shl edx, 7                             ; 块内偏移
    shr eax, 5
    add eax, [kifs_record_start]           ; 所在块
    mov ecx, KIFS_REC_SIZE
    mov esi, [kifs_wr_rec]
    call kifs_delta_add_small
    test eax, eax
    jnz .out
    mov dword [kifs_wr_ret], 0
.out:
    popad
    mov eax, [kifs_wr_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_wc_free_old:释放 write_content 开头快照下来的旧 extent
; ---------------------------------------------------------------------------
kifs_wc_free_old:
    pushad
    mov dword [kifs_wc_i], 0
.loop:
    mov eax, [kifs_wc_i]
    cmp eax, [kifs_wc_oldn]
    jae .done
    mov ecx, [kifs_wc_ocnt + eax * 4]
    mov eax, [kifs_wc_ostart + eax * 4]
    call kifs_free_blocks_range
    inc dword [kifs_wc_i]
    jmp .loop
.done:
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_write_content:esi = record, edi = 数据, ecx = 长度 → eax = 0 / -1
;  ≤92 B 内联(先释放旧 extent);否则连续块 CoW:alloc → 逐块直接写 →
;  释放旧 extent → 改 record(和宿主 write_content 顺序一致)
; ---------------------------------------------------------------------------
kifs_write_content:
    pushad
    mov [kifs_wc_rec], esi
    mov [kifs_wc_data], edi
    mov [kifs_wc_len], ecx
    mov dword [kifs_wc_ret], -1
    ; ---- 先快照旧 extent(改 record 之后 36.. 就是新数据了)----
    mov dword [kifs_wc_oldn], 0
    cmp byte [esi + 33], 0
    jne .old_done
    movzx eax, byte [esi + 32]
    mov [kifs_wc_oldn], eax
    xor ebx, ebx
.old_loop:
    cmp ebx, [kifs_wc_oldn]
    jae .old_done
    mov ecx, ebx
    imul ecx, ecx, 12
    lea edi, [esi + ecx + 36]
    mov eax, [edi]
    mov [kifs_wc_ostart + ebx * 4], eax
    movzx eax, word [edi + 4]
    mov [kifs_wc_ocnt + ebx * 4], eax
    inc ebx
    jmp .old_loop
.old_done:
    mov ecx, [kifs_wc_len]
    cmp ecx, KIFS_INLINE_MAX
    ja .extent
    ; ---- 内联:释放旧块,改写内联区 ----
    call kifs_wc_free_old
    mov esi, [kifs_wc_rec]
    mov eax, [kifs_wc_len]
    mov [esi + 8], eax                     ; size
    mov dword [esi + 12], 0                ; blocks
    mov byte [esi + 32], 0                 ; n_ext
    mov [esi + 33], al                     ; inline_len
    mov word [esi + 34], KIFS_INLINE_OFF
    mov dword [esi + 72], 0                ; ext_index
    mov byte [esi + 76], 0                 ; n_ext_total
    lea edi, [esi + 36]
    mov esi, [kifs_wc_data]
    mov ecx, [kifs_wc_len]
    cld
    rep movsb
    mov ecx, KIFS_INLINE_MAX
    sub ecx, [kifs_wc_len]
    jz .ok
    xor eax, eax
    rep stosb                              ; 补齐到 92 B
    jmp .ok
.extent:
    ; ---- 连续块 CoW ----
    mov eax, [kifs_wc_len]
    add eax, KIFS_BLOCK_SIZE - 1
    shr eax, 12
    mov [kifs_wc_blocks], eax
    mov ecx, eax
    call kifs_alloc_blocks
    test eax, eax
    jz .nospc
    mov [kifs_wc_start], eax
    mov dword [kifs_wc_i], 0
.wr_loop:
    mov eax, [kifs_wc_i]
    cmp eax, [kifs_wc_blocks]
    jae .wr_done
    mov esi, [kifs_wc_data]
    mov ecx, eax
    shl ecx, 12
    add esi, ecx
    mov eax, [kifs_wc_start]
    add eax, [kifs_wc_i]
    call kifs_write_block
    test eax, eax
    jnz .ioerr
    inc dword [kifs_wc_i]
    jmp .wr_loop
.wr_done:
    call kifs_wc_free_old
    mov esi, [kifs_wc_rec]
    mov eax, [kifs_wc_len]
    mov [esi + 8], eax                     ; size
    mov eax, [kifs_wc_blocks]
    mov [esi + 12], eax                    ; blocks
    mov byte [esi + 32], 1                 ; n_ext
    mov byte [esi + 33], 0                 ; inline_len
    mov word [esi + 34], KIFS_INLINE_OFF
    mov eax, [kifs_wc_start]
    mov [esi + 36], eax
    mov eax, [kifs_wc_blocks]
    mov [esi + 40], ax
    mov word [esi + 42], 0
    mov dword [esi + 44], 0
    mov dword [esi + 48], 0                ; extent[1]
    mov dword [esi + 52], 0
    mov dword [esi + 56], 0
    mov dword [esi + 60], 0                ; extent[2]
    mov dword [esi + 64], 0
    mov dword [esi + 68], 0
    mov dword [esi + 72], 0                ; ext_index
    mov byte [esi + 76], 1                 ; n_ext_total
    jmp .ok
.nospc:
    mov dword [kifs_err], KIFS_ERR_NOSPC
    jmp .out
.ioerr:
    mov dword [kifs_err], KIFS_ERR_IO
    jmp .out
.ok:
    mov dword [kifs_wc_ret], 0
.out:
    popad
    mov eax, [kifs_wc_ret]
    ret

; ===========================================================================
;  journal
; ===========================================================================

; ---------------------------------------------------------------------------
;  kifs_jrn_parse:KIFS_IO_BUF 里一个 journal 块 → eax = 1 有效 / 0
;  有效时填 kifs_j_seq / j_flags / j_offset / j_target / j_len
; ---------------------------------------------------------------------------
kifs_jrn_parse:
    pushad
    mov dword [kifs_jp_ret], 0
    cmp dword [KIFS_IO_BUF + 0], KIFS_JRN_MAGIC
    jne .out
    mov eax, [KIFS_IO_BUF + 4]             ; seq
    test eax, eax
    jz .out
    movzx edx, word [KIFS_IO_BUF + 8]
    test edx, KIFS_JF_VALID
    jz .out
    movzx ecx, word [KIFS_IO_BUF + 16]
    cmp ecx, KIFS_JRN_PAYLOAD
    ja .out
    mov esi, KIFS_IO_BUF + 20
    call kifs_crc16
    movzx edx, word [KIFS_IO_BUF + 18]
    cmp eax, edx
    jne .out
    mov eax, [KIFS_IO_BUF + 4]
    mov [kifs_j_seq], eax
    movzx eax, word [KIFS_IO_BUF + 8]
    mov [kifs_j_flags], eax
    movzx eax, word [KIFS_IO_BUF + 10]
    mov [kifs_j_offset], eax
    mov eax, [KIFS_IO_BUF + 12]
    mov [kifs_j_target], eax
    movzx eax, word [KIFS_IO_BUF + 16]
    mov [kifs_j_len], eax
    mov dword [kifs_jp_ret], 1
.out:
    popad
    mov eax, [kifs_jp_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_txn_find:eax = seq → eax = txn 表项指针 / 0(表满)
;  表项 20 B:seq@0, begin@4, commit@8, end@12, recs@16(-1 = 没有)
; ---------------------------------------------------------------------------
kifs_txn_find:
    pushad
    mov [kifs_tf_seq], eax
    mov ecx, [kifs_txn_n]
    mov esi, KIFS_TXN
.loop:
    test ecx, ecx
    jz .new
    cmp dword [esi + 0], eax
    je .found
    add esi, 20
    dec ecx
    jmp .loop
.found:
    mov [kifs_tf_ret], esi
    jmp .out
.new:
    cmp dword [kifs_txn_n], KIFS_TXN_MAX
    jae .full
    mov esi, [kifs_txn_n]
    imul esi, esi, 20
    add esi, KIFS_TXN
    mov eax, [kifs_tf_seq]
    mov [esi + 0], eax
    mov dword [esi + 4], -1
    mov dword [esi + 8], -1
    mov dword [esi + 12], -1
    mov dword [esi + 16], 0
    inc dword [kifs_txn_n]
    mov [kifs_tf_ret], esi
    jmp .out
.full:
    mov dword [kifs_tf_ret], 0
.out:
    popad
    mov eax, [kifs_tf_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_jrn_scan:扫全部 journal 块,建 txn 表(seq 聚合),求 maxseq/最大槽
;   → eax = 0 / -1(读盘失败)
; ---------------------------------------------------------------------------
kifs_jrn_scan:
    pushad
    mov dword [kifs_txn_n], 0
    mov dword [kifs_jrn_maxseq], 0
    mov dword [kifs_jrn_usedmax], -1
    mov dword [kifs_js_i], 0
.loop:
    mov eax, [kifs_js_i]
    cmp eax, [kifs_journal_blocks]
    jae .done
    add eax, [kifs_journal_start]
    call kifs_read_block
    test eax, eax
    jnz .fail
    call kifs_jrn_parse
    test eax, eax
    jz .next
    mov eax, [kifs_js_i]
    mov [kifs_jrn_usedmax], eax
    mov eax, [kifs_j_seq]
    cmp eax, [kifs_jrn_maxseq]
    jbe .no_max
    mov [kifs_jrn_maxseq], eax
.no_max:
    mov eax, [kifs_j_seq]
    call kifs_txn_find
    test eax, eax
    jz .fail
    mov edi, eax
    mov edx, [kifs_j_flags]
    test edx, KIFS_JF_BEGIN
    jz .no_begin
    mov eax, [kifs_js_i]
    mov [edi + 4], eax
    jmp .next
.no_begin:
    test edx, KIFS_JF_COMMIT
    jz .no_commit
    mov eax, [kifs_js_i]
    mov [edi + 8], eax
    jmp .next
.no_commit:
    test edx, KIFS_JF_END
    jz .is_record
    mov eax, [kifs_js_i]
    mov [edi + 12], eax
    jmp .next
.is_record:
    inc dword [edi + 16]
.next:
    inc dword [kifs_js_i]
    jmp .loop
.done:
    mov dword [kifs_js_ret], 0
    jmp .out
.fail:
    mov dword [kifs_js_ret], -1
.out:
    popad
    mov eax, [kifs_js_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_jrn_build_ctrl:eax = seq, edx = flags → 构造 4096 B 控制块到 JRN_BUF
;  (BEGIN / COMMIT / END:offset=target=0,payload 空,crc=0xFFFF)
; ---------------------------------------------------------------------------
kifs_jrn_build_ctrl:
    pushad
    mov [kifs_jb_seq], eax
    mov [kifs_jb_flags], edx
    mov edi, KIFS_JRN_BUF
    xor eax, eax
    mov ecx, KIFS_BLOCK_SIZE / 4
    cld
    rep stosd
    mov dword [KIFS_JRN_BUF + 0], KIFS_JRN_MAGIC
    mov eax, [kifs_jb_seq]
    mov [KIFS_JRN_BUF + 4], eax
    mov eax, [kifs_jb_flags]
    mov [KIFS_JRN_BUF + 8], ax
    mov dword [KIFS_JRN_BUF + 10], 0       ; offset + target 高半
    mov dword [KIFS_JRN_BUF + 12], 0
    mov word [KIFS_JRN_BUF + 16], 0
    mov word [KIFS_JRN_BUF + 18], 0xFFFF
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_jrn_build_rec:eax=seq, edx=offset, ecx=target, esi=payload, ebx=len
; ---------------------------------------------------------------------------
kifs_jrn_build_rec:
    pushad
    mov [kifs_jb_seq], eax
    mov [kifs_jb_off], edx
    mov [kifs_jb_tgt], ecx
    mov [kifs_jb_ptr], esi
    mov [kifs_jb_len], ebx
    mov edi, KIFS_JRN_BUF
    xor eax, eax
    mov ecx, KIFS_BLOCK_SIZE / 4
    cld
    rep stosd
    mov dword [KIFS_JRN_BUF + 0], KIFS_JRN_MAGIC
    mov eax, [kifs_jb_seq]
    mov [KIFS_JRN_BUF + 4], eax
    mov word [KIFS_JRN_BUF + 8], KIFS_JF_VALID
    mov eax, [kifs_jb_off]
    mov [KIFS_JRN_BUF + 10], ax
    mov eax, [kifs_jb_tgt]
    mov [KIFS_JRN_BUF + 12], eax
    mov eax, [kifs_jb_len]
    mov [KIFS_JRN_BUF + 16], ax
    mov esi, [kifs_jb_ptr]
    mov ecx, [kifs_jb_len]
    call kifs_crc16
    mov [KIFS_JRN_BUF + 18], ax
    mov esi, [kifs_jb_ptr]
    mov edi, KIFS_JRN_BUF + 20
    mov ecx, [kifs_jb_len]
    cld
    rep movsb
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_jrn_write_buf:eax = journal 槽号 → 把 JRN_BUF 写到该槽
; ---------------------------------------------------------------------------
kifs_jrn_write_buf:
    add eax, [kifs_journal_start]
    mov esi, KIFS_JRN_BUF
    call kifs_write_block
    ret

; ---------------------------------------------------------------------------
;  kifs_slot_next:提交过程中槽号 +1(环形)
; ---------------------------------------------------------------------------
kifs_slot_next:
    push eax
    inc dword [kifs_jc_slot]
    mov eax, [kifs_jc_slot]
    cmp eax, [kifs_journal_blocks]
    jb .ok
    mov dword [kifs_jc_slot], 0
.ok:
    pop eax
    ret

; ---------------------------------------------------------------------------
;  kifs_jrn_write_end:eax = 槽号, edx = seq → 写 VALID|END 控制块
; ---------------------------------------------------------------------------
kifs_jrn_write_end:
    pushad
    mov [kifs_we_slot], eax
    mov eax, edx
    mov edx, KIFS_JF_VALID | KIFS_JF_END
    call kifs_jrn_build_ctrl
    mov eax, [kifs_we_slot]
    call kifs_jrn_write_buf
    mov [kifs_we_ret], eax
    popad
    mov eax, [kifs_we_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_replay_seq:eax = seq → 重放该事务的全部记录,返回 eax = 0 / -1
;  journal 块读进 IO_BUF(payload 在 +20),目标块读进 JRN_BUF 再 patch 写回。
; ---------------------------------------------------------------------------
kifs_replay_seq:
    pushad
    mov [kifs_rp_seq], eax
    mov dword [kifs_rp_i], 0
.loop:
    mov eax, [kifs_rp_i]
    cmp eax, [kifs_journal_blocks]
    jae .done
    add eax, [kifs_journal_start]
    call kifs_read_block
    test eax, eax
    jnz .fail
    call kifs_jrn_parse
    test eax, eax
    jz .next
    mov eax, [kifs_j_seq]
    cmp eax, [kifs_rp_seq]
    jne .next
    mov eax, [kifs_j_flags]
    and eax, KIFS_JF_BEGIN | KIFS_JF_COMMIT | KIFS_JF_END
    jnz .next                             ; 只重放记录块
    mov eax, [kifs_j_target]
    cmp eax, [kifs_total_blocks]
    jae .next
    mov ecx, [kifs_j_offset]
    cmp ecx, KIFS_BLOCK_SIZE
    jae .next
    mov edx, [kifs_j_len]
    add edx, ecx
    cmp edx, KIFS_BLOCK_SIZE
    ja .next
    mov edi, KIFS_JRN_BUF
    call kifs_read_block_to
    test eax, eax
    jnz .fail
    mov esi, KIFS_IO_BUF + 20
    mov edi, KIFS_JRN_BUF
    add edi, [kifs_j_offset]
    mov ecx, [kifs_j_len]
    cld
    rep movsb
    mov eax, [kifs_j_target]
    mov esi, KIFS_JRN_BUF
    call kifs_write_block
    test eax, eax
    jnz .fail
.next:
    inc dword [kifs_rp_i]
    jmp .loop
.done:
    mov dword [kifs_rp_ret], 0
    jmp .out
.fail:
    mov dword [kifs_rp_ret], -1
.out:
    popad
    mov eax, [kifs_rp_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_jrn_commit → eax = 0 / -1
;  BEGIN → 记录(按 4076 切片,槽号环形)→ COMMIT → 应用 delta → END
; ---------------------------------------------------------------------------
kifs_jrn_commit:
    pushad
    mov dword [kifs_jc_ret], -1
    call kifs_jrn_scan
    test eax, eax
    js .out
    ; ---- 先算需要的记录条数,不够就报 journal 满 ----
    mov dword [kifs_jc_total], 0
    mov dword [kifs_jc_di], 0
.count_loop:
    mov ecx, [kifs_jc_di]
    cmp ecx, [kifs_delta_n]
    jae .count_done
    mov esi, ecx
    imul esi, esi, 144
    add esi, KIFS_DELTAS
    mov eax, [esi + 8]
    test eax, eax
    jz .count_next
    add eax, KIFS_JRN_PAYLOAD - 1
    xor edx, edx
    mov ecx, KIFS_JRN_PAYLOAD
    div ecx
    add [kifs_jc_total], eax
.count_next:
    inc dword [kifs_jc_di]
    jmp .count_loop
.count_done:
    mov eax, [kifs_jc_total]
    add eax, 3
    cmp eax, [kifs_journal_blocks]
    ja .out
    ; ---- seq / 起始槽 ----
    mov eax, [kifs_jrn_maxseq]
    inc eax
    mov [kifs_jc_seq], eax
    mov eax, [kifs_jrn_usedmax]
    inc eax
    xor edx, edx
    div dword [kifs_journal_blocks]
    mov [kifs_jc_slot], edx
    mov [kifs_jc_begin], edx
    ; ---- BEGIN ----
    mov eax, [kifs_jc_seq]
    mov edx, KIFS_JF_VALID | KIFS_JF_BEGIN
    call kifs_jrn_build_ctrl
    mov eax, [kifs_jc_slot]
    call kifs_jrn_write_buf
    test eax, eax
    jnz .out
    call kifs_slot_next
    ; ---- 记录 ----
    mov dword [kifs_jc_di], 0
.delta_loop:
    mov ecx, [kifs_jc_di]
    cmp ecx, [kifs_delta_n]
    jae .records_done
    mov esi, ecx
    imul esi, esi, 144
    add esi, KIFS_DELTAS
    mov [kifs_jc_entry], esi
    mov eax, [esi + 12]
    test eax, eax
    jnz .have_ptr
    lea eax, [esi + 16]
.have_ptr:
    mov [kifs_jc_ptr], eax
    mov dword [kifs_jc_off], 0
.chunk_loop:
    mov esi, [kifs_jc_entry]
    mov eax, [kifs_jc_off]
    mov ecx, [esi + 8]
    sub ecx, eax
    jz .next_delta
    cmp ecx, KIFS_JRN_PAYLOAD
    jbe .chunk_ok
    mov ecx, KIFS_JRN_PAYLOAD
.chunk_ok:
    mov [kifs_jc_chunk], ecx
    mov eax, [kifs_jc_seq]
    mov edx, [esi + 4]
    add edx, [kifs_jc_off]
    mov ecx, [esi + 0]
    mov ebx, [kifs_jc_chunk]
    mov esi, [kifs_jc_ptr]
    add esi, [kifs_jc_off]
    call kifs_jrn_build_rec
    mov eax, [kifs_jc_slot]
    call kifs_jrn_write_buf
    test eax, eax
    jnz .out
    call kifs_slot_next
    mov eax, [kifs_jc_chunk]
    add [kifs_jc_off], eax
    jmp .chunk_loop
.next_delta:
    inc dword [kifs_jc_di]
    jmp .delta_loop
.records_done:
    ; ---- COMMIT ----
    mov eax, [kifs_jc_seq]
    mov edx, KIFS_JF_VALID | KIFS_JF_COMMIT
    call kifs_jrn_build_ctrl
    mov eax, [kifs_jc_slot]
    call kifs_jrn_write_buf
    test eax, eax
    jnz .out
    call kifs_slot_next
    ; ---- 应用(整段 read-modify-write,幂等)----
    mov dword [kifs_jc_di], 0
.apply_loop:
    mov ecx, [kifs_jc_di]
    cmp ecx, [kifs_delta_n]
    jae .apply_done
    mov esi, ecx
    imul esi, esi, 144
    add esi, KIFS_DELTAS
    mov eax, [esi + 12]
    test eax, eax
    jnz .ap_ptr
    lea eax, [esi + 16]
.ap_ptr:
    mov [kifs_jc_ptr], eax
    mov eax, [esi + 0]
    mov [kifs_jc_atgt], eax
    cmp eax, [kifs_total_blocks]
    jae .bad_apply
    mov eax, [esi + 4]
    mov [kifs_jc_aoff], eax
    mov eax, [esi + 8]
    mov [kifs_jc_alen], eax
    cmp eax, KIFS_BLOCK_SIZE
    ja .bad_apply
    mov ebx, [kifs_jc_aoff]
    add ebx, eax
    cmp ebx, KIFS_BLOCK_SIZE
    ja .bad_apply
    mov eax, [kifs_jc_atgt]
    call kifs_read_block
    test eax, eax
    jnz .out
    mov esi, [kifs_jc_ptr]
    mov edi, KIFS_IO_BUF
    add edi, [kifs_jc_aoff]
    mov ecx, [kifs_jc_alen]
    cld
    rep movsb
    mov eax, [kifs_jc_atgt]
    mov esi, KIFS_IO_BUF
    call kifs_write_block
    test eax, eax
    jnz .out
    inc dword [kifs_jc_di]
    jmp .apply_loop
.bad_apply:
    jmp .out
.apply_done:
    ; ---- END 写回 BEGIN 槽 ----
    mov eax, [kifs_jc_seq]
    mov edx, KIFS_JF_VALID | KIFS_JF_END
    call kifs_jrn_build_ctrl
    mov eax, [kifs_jc_begin]
    call kifs_jrn_write_buf
    test eax, eax
    jnz .out
    mov dword [kifs_jc_ret], 0
.out:
    popad
    mov eax, [kifs_jc_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_flush → eax = 0 / -1
;  更新 sb 影子 → 变了就加 3 条 512 B delta(块 1/2/最后一块)→ 位图 dirty
;  就加一条整块 delta → 有 delta 才提交
; ---------------------------------------------------------------------------
kifs_flush:
    pushad
    mov dword [kifs_fl_ret], -1
    mov eax, [kifs_data_next]
    mov [KIFS_SB_BUF + 24], eax
    mov eax, [kifs_free_blocks]
    mov [KIFS_SB_BUF + 52], eax
    mov esi, KIFS_SB_BUF
    mov ecx, 108
    call kifs_crc32
    mov [KIFS_SB_BUF + 108], eax
    mov esi, KIFS_SB_BUF
    mov edi, KIFS_SB_OLD
    mov ecx, 112
    cld
    repe cmpsb
    je .sb_same
    mov eax, 1
    mov ecx, 512
    mov esi, KIFS_SB_BUF
    call kifs_delta_add_block
    test eax, eax
    jnz .out
    mov eax, 2
    mov ecx, 512
    mov esi, KIFS_SB_BUF
    call kifs_delta_add_block
    test eax, eax
    jnz .out
    mov eax, [kifs_total_blocks]
    dec eax
    mov ecx, 512
    mov esi, KIFS_SB_BUF
    call kifs_delta_add_block
    test eax, eax
    jnz .out
    mov esi, KIFS_SB_BUF
    mov edi, KIFS_SB_OLD
    mov ecx, 112
    cld
    rep movsb
.sb_same:
    cmp byte [kifs_bitmap_dirty], 0
    je .no_bitmap
    mov eax, [kifs_data_start]
    mov ecx, KIFS_BLOCK_SIZE
    mov esi, KIFS_BITMAP_BUF
    call kifs_delta_add_block
    test eax, eax
    jnz .out
.no_bitmap:
    cmp dword [kifs_delta_n], 0
    je .nothing
    call kifs_jrn_commit
    test eax, eax
    jnz .out
.nothing:
    mov byte [kifs_bitmap_dirty], 0
    mov dword [kifs_delta_n], 0
    mov dword [kifs_fl_ret], 0
.out:
    popad
    mov eax, [kifs_fl_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_txn_abort:丢弃未提交的改动 —— 重读 superblock + 位图,清 delta 表
; ---------------------------------------------------------------------------
kifs_txn_abort:
    call kifs_mount_sb
    ret

; ===========================================================================
;  挂载 / 恢复
; ===========================================================================

; ---------------------------------------------------------------------------
;  kifs_mount_sb:读块 1、校验、存全局、把 sb 512 B 拷进影子、加载位图
;   → eax = 0 / -1(失败不打印)
; ---------------------------------------------------------------------------
kifs_mount_sb:
    pushad
    mov dword [kifs_ms_ret], -1
    mov byte [kifs_bitmap_dirty], 0
    mov dword [kifs_delta_n], 0
    mov dword [kifs_txn_n], 0
    mov eax, 1
    call kifs_read_block
    test eax, eax
    jnz .out
    cmp dword [KIFS_IO_BUF + 0], KIFS_MAGIC
    jne .out
    cmp dword [KIFS_IO_BUF + 4], KIFS_VERSION
    jne .out
    cmp dword [KIFS_IO_BUF + 8], KIFS_BLOCK_SIZE
    jne .out
    mov esi, KIFS_IO_BUF
    mov ecx, 108
    call kifs_crc32
    cmp eax, [KIFS_IO_BUF + 108]
    jne .out
    mov eax, [KIFS_IO_BUF + 12]            ; total_blocks
    cmp eax, 8
    jb .out
    cmp eax, 32768                         ; 单块位图能描述的上限
    ja .out
    mov [kifs_total_blocks], eax
    mov ebx, [KIFS_IO_BUF + 40]            ; record_start
    cmp ebx, eax
    jae .out
    mov ecx, [KIFS_IO_BUF + 44]            ; record_count
    cmp ecx, 2
    jb .out
    cmp ecx, 4096
    ja .out
    lea edx, [ecx + 31]
    shr edx, 5                             ; record_blocks
    add edx, ebx                           ; data_area
    cmp edx, eax
    jae .out
    mov edx, [KIFS_IO_BUF + 48]            ; root_oid
    test edx, edx
    jz .out
    cmp edx, ecx
    jae .out
    mov edx, [KIFS_IO_BUF + 20]            ; data_start(位图块)
    cmp edx, ebx
    jae .out
    mov eax, [KIFS_IO_BUF + 24]            ; data_next
    mov ecx, [KIFS_IO_BUF + 44]
    add ecx, 31
    shr ecx, 5
    add ecx, [KIFS_IO_BUF + 40]
    cmp eax, ecx
    jb .out
    mov ecx, [KIFS_IO_BUF + 28]            ; meta_next
    cmp eax, ecx
    ja .out
    mov ecx, [KIFS_IO_BUF + 32]            ; journal_start
    mov ebx, [KIFS_IO_BUF + 36]            ; journal_blocks
    test ebx, ebx
    jz .out
    cmp ecx, [KIFS_IO_BUF + 28]
    jb .out
    mov edx, ecx
    add edx, ebx
    mov eax, [KIFS_IO_BUF + 12]
    dec eax                                ; 最后一块是 sb 备份,不能给 journal
    cmp edx, eax
    ja .out
    ; ---- 存全局 ----
    mov eax, [KIFS_IO_BUF + 4]
    mov [kifs_version], eax
    mov eax, [KIFS_IO_BUF + 8]
    mov [kifs_block_size], eax
    mov eax, [KIFS_IO_BUF + 12]
    mov [kifs_total_blocks], eax
    mov eax, [KIFS_IO_BUF + 20]
    mov [kifs_data_start], eax
    mov eax, [KIFS_IO_BUF + 24]
    mov [kifs_data_next], eax
    mov eax, [KIFS_IO_BUF + 28]
    mov [kifs_meta_next], eax
    mov eax, [KIFS_IO_BUF + 32]
    mov [kifs_journal_start], eax
    mov eax, [KIFS_IO_BUF + 36]
    mov [kifs_journal_blocks], eax
    mov eax, [KIFS_IO_BUF + 40]
    mov [kifs_record_start], eax
    mov eax, [KIFS_IO_BUF + 44]
    mov [kifs_record_count], eax
    mov eax, [KIFS_IO_BUF + 48]
    mov [kifs_root_oid], eax
    mov eax, [KIFS_IO_BUF + 52]
    mov [kifs_free_blocks], eax
    mov eax, [KIFS_IO_BUF + 56]
    mov [kifs_flags], eax
    mov esi, KIFS_IO_BUF + 76
    mov edi, kifs_label
    mov ecx, 32
    cld
    rep movsb
    mov byte [kifs_label + 32], 0
    ; ---- sb 影子 + 旧副本 ----
    mov esi, KIFS_IO_BUF
    mov edi, KIFS_SB_BUF
    mov ecx, KIFS_BLOCK_SIZE / 4
    cld
    rep movsd
    mov esi, KIFS_SB_BUF
    mov edi, KIFS_SB_OLD
    mov ecx, 112
    rep movsb
    ; ---- 位图块 → BITMAP_BUF ----
    mov eax, [kifs_data_start]
    call kifs_read_block
    test eax, eax
    jnz .out
    mov esi, KIFS_IO_BUF
    mov edi, KIFS_BITMAP_BUF
    mov ecx, KIFS_BLOCK_SIZE / 4
    cld
    rep movsd
    mov dword [kifs_ms_ret], 0
.out:
    popad
    mov eax, [kifs_ms_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_recover:按 seq 升序处理所有没有 END 的事务
;   完整(BEGIN+COMMIT)→ 重放记录 → END;残缺 → 在 begin(否则 commit)槽写 END
;   → eax = 重放/回收的个数 / -1
; ---------------------------------------------------------------------------
kifs_recover:
    pushad
    mov dword [kifs_rv_count], 0
    call kifs_jrn_scan
    test eax, eax
    js .fail
    mov dword [kifs_rv_last], 0
.outer:
    mov dword [kifs_rv_best], -1
    mov dword [kifs_rv_bestptr], 0
    xor ebx, ebx
.search:
    cmp ebx, [kifs_txn_n]
    jae .search_done
    mov esi, ebx
    imul esi, esi, 20
    add esi, KIFS_TXN
    mov eax, [esi + 0]
    cmp eax, [kifs_rv_last]
    jbe .s_next
    mov ecx, [kifs_rv_best]
    cmp ecx, -1
    je .take
    cmp eax, ecx
    jae .s_next
.take:
    mov [kifs_rv_best], eax
    mov [kifs_rv_bestptr], esi
.s_next:
    inc ebx
    jmp .search
.search_done:
    cmp dword [kifs_rv_best], -1
    je .done
    mov esi, [kifs_rv_bestptr]
    mov [kifs_rv_entry], esi
    cmp dword [esi + 12], -1
    jne .next                          ; 有 END,跳过
    mov eax, [esi + 4]                 ; begin?
    cmp eax, -1
    jne .has_begin
    mov eax, [esi + 8]                 ; 无 begin,commit?
    cmp eax, -1
    je .next
    mov edx, [esi + 0]
    call kifs_jrn_write_end
    inc dword [kifs_rv_count]
    jmp .next
.has_begin:
    cmp dword [esi + 8], -1
    jne .replay
    mov eax, [esi + 4]                 ; 有 begin 无 commit:回收
    mov edx, [esi + 0]
    call kifs_jrn_write_end
    inc dword [kifs_rv_count]
    jmp .next
.replay:
    mov eax, [esi + 0]
    call kifs_replay_seq
    test eax, eax
    js .fail
    mov esi, [kifs_rv_entry]
    mov eax, [esi + 4]
    mov edx, [esi + 0]
    call kifs_jrn_write_end
    inc dword [kifs_rv_count]
.next:
    mov eax, [kifs_rv_best]
    mov [kifs_rv_last], eax
    jmp .outer
.done:
    mov eax, [kifs_rv_count]
    jmp .out
.fail:
    mov eax, -1
.out:
    mov [kifs_rv_ret], eax
    popad
    mov eax, [kifs_rv_ret]
    ret

; ===========================================================================
;  路径 / 目录操作
; ===========================================================================

; ---------------------------------------------------------------------------
;  kifs_split_word:esi = 字符串 → esi/ecx = 第一个词,[kifs_rest] = 词后参数
;  (和 usbd_first_word 同一套语义,但不动 [cmd_arg])
; ---------------------------------------------------------------------------
kifs_split_word:
    push ebx
    push edx
    mov [kifs_sw_ptr], esi
.skip_lead:
    cmp byte [esi], ' '
    jne .start
    inc esi
    jmp .skip_lead
.start:
    mov ebx, esi
.scan:
    mov dl, [esi]
    test dl, dl
    jz .end
    cmp dl, ' '
    je .end
    inc esi
    jmp .scan
.end:
    mov ecx, esi
    sub ecx, ebx
    mov [kifs_sw_ptr], ebx
    mov [kifs_sw_len], ecx
.skip_rest:
    cmp byte [esi], ' '
    jne .rest_done
    inc esi
    jmp .skip_rest
.rest_done:
    mov [kifs_rest], esi
    mov esi, [kifs_sw_ptr]
    mov ecx, [kifs_sw_len]
    pop edx
    pop ebx
    ret

; ---------------------------------------------------------------------------
;  kifs_word_to_path:esi/ecx = 路径词 → 拷进 KIFS_PATH_BUF,设 kifs_path_len
;   → eax = 0 / -1(太长)
; ---------------------------------------------------------------------------
kifs_word_to_path:
    pushad
    mov dword [kifs_wtp_ret], 0
    mov [kifs_wtp_len], ecx
    cmp ecx, 255
    ja .fail
    mov edi, KIFS_PATH_BUF
    cld
    rep movsb
    mov byte [edi], 0
    mov eax, [kifs_wtp_len]
    mov [kifs_path_len], eax
    jmp .out
.fail:
    mov dword [kifs_wtp_ret], -1
.out:
    popad
    mov eax, [kifs_wtp_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_parse_target:解析 KIFS_PATH_BUF → kifs_name_ptr/len + KIFS_PARENT_BUF
;   (父路径;根目录时 PARENT_BUF="/")
;   → eax = 0 / -1(路径是根 / 空)
; ---------------------------------------------------------------------------
kifs_parse_target:
    pushad
    mov dword [kifs_pt_ret], -1
    mov esi, KIFS_PATH_BUF
.skip_lead:
    cmp byte [esi], '/'
    jne .start
    inc esi
    jmp .skip_lead
.start:
    mov [kifs_pt_start], esi
    mov edi, esi
.scan_end:
    cmp byte [edi], 0
    je .trim
    inc edi
    jmp .scan_end
.trim:
    cmp edi, [kifs_pt_start]
    jbe .root
    cmp byte [edi - 1], '/'
    jne .trim_done
    dec edi
    jmp .trim
.trim_done:
    mov edx, edi
.find_slash:
    cmp edx, [kifs_pt_start]
    jbe .no_slash
    cmp byte [edx - 1], '/'
    je .has_slash
    dec edx
    jmp .find_slash
.has_slash:
    mov [kifs_name_ptr], edx
    mov eax, edi
    sub eax, edx
    mov [kifs_name_len], eax
    mov ecx, edx
    sub ecx, [kifs_pt_start]
.trim_parent:
    test ecx, ecx
    jz .parent_root
    mov eax, [kifs_pt_start]
    cmp byte [eax + ecx - 1], '/'
    jne .copy_parent
    dec ecx
    jmp .trim_parent
.copy_parent:
    mov esi, [kifs_pt_start]
    mov edi, KIFS_PARENT_BUF
    mov edx, ecx
    cld
    rep movsb
    mov byte [edi], 0
    mov [kifs_parent_len], edx
    mov dword [kifs_pt_ret], 0
    jmp .out
.no_slash:
    mov eax, [kifs_pt_start]
    mov [kifs_name_ptr], eax
    mov eax, edi
    sub eax, [kifs_pt_start]
    mov [kifs_name_len], eax
.parent_root:
    mov byte [KIFS_PARENT_BUF], '/'
    mov byte [KIFS_PARENT_BUF + 1], 0
    mov dword [kifs_parent_len], 1
    mov dword [kifs_pt_ret], 0
    jmp .out
.root:
    mov dword [kifs_err], KIFS_ERR_NOROOT
.out:
    popad
    mov eax, [kifs_pt_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_load_parent:解析 KIFS_PARENT_BUF → 父 oid + 父 record(REC_BUF) +
;  父目录内容(DIR_BUF,大小 kifs_dir_size) → eax = 0 / -1(kifs_err 已设)
; ---------------------------------------------------------------------------
kifs_load_parent:
    pushad
    mov dword [kifs_lp_ret], -1
    mov esi, KIFS_PARENT_BUF
    call kifs_resolve
    test eax, eax
    jz .out
    mov [kifs_parent_oid], eax
    mov edi, KIFS_REC_BUF
    call kifs_read_record
    test eax, eax
    jnz .io
    cmp byte [KIFS_REC_BUF + 4], 2
    jne .notdir
    mov eax, [KIFS_REC_BUF + 8]
    cmp eax, KIFS_DIR_MAX
    ja .dirbig
    mov esi, KIFS_REC_BUF
    mov edi, KIFS_DIR_BUF
    mov ecx, KIFS_DIR_MAX
    call kifs_read_content
    test eax, eax
    js .io
    mov [kifs_dir_size], eax
    mov dword [kifs_lp_ret], 0
    jmp .out
.io:
    mov dword [kifs_err], KIFS_ERR_IO
    jmp .out
.notdir:
    mov dword [kifs_err], KIFS_ERR_NOTDIR
    jmp .out
.dirbig:
    mov dword [kifs_err], KIFS_ERR_DIRBIG
.out:
    popad
    mov eax, [kifs_lp_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_lookup_in_dir:eax = 目录内容大小, edi = 名字, ecx = 长度
;   (目录内容已在 DIR_BUF)→ eax = oid / 0
; ---------------------------------------------------------------------------
kifs_lookup_in_dir:
    pushad
    mov [kifs_lu_size], eax
    mov [kifs_lu_name], edi
    mov [kifs_lu_nlen], ecx
    mov dword [kifs_lu_pos], 0
    mov dword [kifs_lu_ret], 0
.loop:
    mov edx, [kifs_lu_pos]
    mov eax, [kifs_lu_size]
    sub eax, edx
    cmp eax, 8
    jb .out
    lea esi, [KIFS_DIR_BUF + edx]
    movzx ecx, word [esi + 6]
    test ecx, ecx
    jz .out
    movzx eax, byte [esi + 5]
    cmp eax, [kifs_lu_nlen]
    jne .next
    lea esi, [esi + 8]
    mov edi, [kifs_lu_name]
    mov ecx, [kifs_lu_nlen]
    cld
    repe cmpsb
    jne .next
    mov edx, [kifs_lu_pos]
    mov eax, [KIFS_DIR_BUF + edx]
    mov [kifs_lu_ret], eax
    jmp .out
.next:
    mov edx, [kifs_lu_pos]
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    add edx, ecx
    mov [kifs_lu_pos], edx
    jmp .loop
.out:
    popad
    mov eax, [kifs_lu_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_name_cmp:esi/ecx = 名 1,edi/edx = 名 2 → eax <0 / 0 / >0(字节序)
; ---------------------------------------------------------------------------
kifs_name_cmp:
    push ebx
    push esi
    push edi
    mov [kifs_nc_p1], esi
    mov [kifs_nc_l1], ecx
    mov [kifs_nc_p2], edi
    mov [kifs_nc_l2], edx
    cmp ecx, edx
    jbe .min_ok
    mov ecx, edx
.min_ok:
    mov esi, [kifs_nc_p1]
    mov edi, [kifs_nc_p2]
    test ecx, ecx
    jz .by_len
.byte_loop:
    movzx eax, byte [esi]
    movzx ebx, byte [edi]
    sub eax, ebx
    jnz .done
    inc esi
    inc edi
    dec ecx
    jnz .byte_loop
.by_len:
    mov eax, [kifs_nc_l1]
    sub eax, [kifs_nc_l2]
.done:
    pop edi
    pop esi
    pop ebx
    ret

; ---------------------------------------------------------------------------
;  kifs_put_dirent:edi = 目标, eax = oid, edx = type, esi = 名字,
;   ecx = 名字长度 → eax = entry_len(4 对齐,名字后补 0)
; ---------------------------------------------------------------------------
kifs_put_dirent:
    pushad
    mov [kifs_pd_oid], eax
    mov [kifs_pd_type], edx
    mov [kifs_pd_name], esi
    mov [kifs_pd_nlen], ecx
    lea ebx, [ecx + 11]
    and ebx, ~3
    mov [kifs_pd_elen], ebx
    mov eax, [kifs_pd_oid]
    mov [edi + 0], eax
    mov eax, [kifs_pd_type]
    mov [edi + 4], al
    mov eax, [kifs_pd_nlen]
    mov [edi + 5], al
    mov [edi + 6], bx
    add edi, 8
    mov esi, [kifs_pd_name]
    mov ecx, [kifs_pd_nlen]
    cld
    rep movsb
    mov ecx, [kifs_pd_elen]
    sub ecx, [kifs_pd_nlen]
    sub ecx, 8
    jz .done
    xor eax, eax
    rep stosb
.done:
    mov eax, [kifs_pd_elen]
    mov [kifs_pd_ret], eax
    popad
    mov eax, [kifs_pd_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_dir_add:esi = 目录 record, edi = 名字, ecx = 名字长度,
;   edx = 子 oid, ebx = 子 type → eax = 0 / -1(kifs_err 已设)
;   新内容在 DATA_BUF 里按名字字节序插入,最后 CoW 写回目录 record。
; ---------------------------------------------------------------------------
kifs_dir_add:
    pushad
    mov dword [kifs_da_ret], -1
    mov [kifs_da_rec], esi
    mov [kifs_da_name], edi
    mov [kifs_da_nlen], ecx
    mov [kifs_da_oid], edx
    mov [kifs_da_type], ebx
    mov esi, [kifs_da_rec]
    mov edi, KIFS_DIR_BUF
    mov ecx, KIFS_DIR_MAX
    call kifs_read_content
    test eax, eax
    js .io
    mov [kifs_da_size], eax
    mov ecx, [kifs_da_nlen]
    lea eax, [ecx + 11]
    and eax, ~3
    mov [kifs_da_elen], eax
    add eax, [kifs_da_size]
    cmp eax, KIFS_DIR_MAX
    ja .dirbig
    mov [kifs_da_newsize], eax
    mov dword [kifs_da_pos], 0
    mov dword [kifs_da_out], 0
    mov dword [kifs_da_ins], 0
.loop:
    mov edx, [kifs_da_pos]
    cmp edx, [kifs_da_size]
    jae .after
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    test ecx, ecx
    jz .after
    cmp dword [kifs_da_ins], 0
    jne .copy
    mov esi, [kifs_da_name]
    mov ecx, [kifs_da_nlen]
    lea edi, [KIFS_DIR_BUF + edx + 8]
    movzx edx, byte [KIFS_DIR_BUF + edx + 5]
    call kifs_name_cmp
    test eax, eax
    jge .copy                          ; 新名字 >= 当前项 → 当前项先
    call kifs_dir_emit_new
    mov dword [kifs_da_ins], 1
    jmp .loop
.copy:
    mov edx, [kifs_da_pos]
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    lea esi, [KIFS_DIR_BUF + edx]
    mov edi, [kifs_da_out]
    add edi, KIFS_DATA_BUF
    push ecx
    cld
    rep movsb
    pop ecx
    add [kifs_da_out], ecx
    add [kifs_da_pos], ecx
    jmp .loop
.after:
    cmp dword [kifs_da_ins], 0
    jne .write
    call kifs_dir_emit_new
.write:
    mov esi, [kifs_da_rec]
    mov edi, KIFS_DATA_BUF
    mov ecx, [kifs_da_newsize]
    call kifs_write_content
    test eax, eax
    jnz .out
    mov dword [kifs_da_ret], 0
    jmp .out
.io:
    mov dword [kifs_err], KIFS_ERR_IO
    jmp .out
.dirbig:
    mov dword [kifs_err], KIFS_ERR_DIRBIG
.out:
    popad
    mov eax, [kifs_da_ret]
    ret

; ---------------------------------------------------------------------------
;  kifs_dir_emit_new:把待插入目录项写进 DATA_BUF 的当前输出位置
; ---------------------------------------------------------------------------
kifs_dir_emit_new:
    mov edi, [kifs_da_out]
    add edi, KIFS_DATA_BUF
    mov eax, [kifs_da_oid]
    mov edx, [kifs_da_type]
    mov esi, [kifs_da_name]
    mov ecx, [kifs_da_nlen]
    call kifs_put_dirent
    add [kifs_da_out], eax
    ret

; ---------------------------------------------------------------------------
;  kifs_dir_remove:esi = 目录 record, edi = 名字, ecx = 长度
;   → eax = 0 / -1(kifs_err 已设)
; ---------------------------------------------------------------------------
kifs_dir_remove:
    pushad
    mov dword [kifs_dr_ret], -1
    mov [kifs_dr_rec], esi
    mov [kifs_dr_name], edi
    mov [kifs_dr_nlen], ecx
    mov esi, [kifs_dr_rec]
    mov edi, KIFS_DIR_BUF
    mov ecx, KIFS_DIR_MAX
    call kifs_read_content
    test eax, eax
    js .io
    mov [kifs_dr_size], eax
    mov dword [kifs_dr_pos], 0
    mov dword [kifs_dr_out], 0
    mov dword [kifs_dr_found], 0
.loop:
    mov edx, [kifs_dr_pos]
    cmp edx, [kifs_dr_size]
    jae .write
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    test ecx, ecx
    jz .write
    ; 比较名字
    movzx eax, byte [KIFS_DIR_BUF + edx + 5]
    cmp eax, [kifs_dr_nlen]
    jne .copy
    lea esi, [KIFS_DIR_BUF + edx + 8]
    mov edi, [kifs_dr_name]
    mov ecx, [kifs_dr_nlen]
    cld
    repe cmpsb
    jne .copy
    ; 命中:跳过
    mov dword [kifs_dr_found], 1
    mov edx, [kifs_dr_pos]
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    add [kifs_dr_pos], ecx
    jmp .loop
.copy:
    mov edx, [kifs_dr_pos]
    movzx ecx, word [KIFS_DIR_BUF + edx + 6]
    lea esi, [KIFS_DIR_BUF + edx]
    mov edi, [kifs_dr_out]
    add edi, KIFS_DATA_BUF
    push ecx
    cld
    rep movsb
    pop ecx
    add [kifs_dr_out], ecx
    add [kifs_dr_pos], ecx
    jmp .loop
.write:
    cmp dword [kifs_dr_found], 0
    je .noent
    mov esi, [kifs_dr_rec]
    mov edi, KIFS_DATA_BUF
    mov ecx, [kifs_dr_out]
    call kifs_write_content
    test eax, eax
    jnz .out
    mov dword [kifs_dr_ret], 0
    jmp .out
.noent:
    mov dword [kifs_err], KIFS_ERR_NOENT
    jmp .out
.io:
    mov dword [kifs_err], KIFS_ERR_IO
.out:
    popad
    mov eax, [kifs_dr_ret]
    ret

; ===========================================================================
;  Phase C1 shell 命令:mkdir / write / rm / journal
;  每个命令 = 一个事务:改影子(delta/位图)→ flush 提交。任何一步失败都
;  kifs_txn_abort 丢弃未提交改动(重读 sb + 位图)。
; ===========================================================================

; ---------------------------------------------------------------------------
;  kifs_now:返回一个"时间戳"。kernel 里没有方便的 unix 秒,ctime/mtime
;  写 0(fsck 不校验时间,只保证字段存在)。
; ---------------------------------------------------------------------------
kifs_now:
    xor eax, eax
    ret

; ---------------------------------------------------------------------------
;  kifs_cmd_mkdir:kifs mkdir <path>
; ---------------------------------------------------------------------------
kifs_cmd_mkdir:
    pushad
    call kifs_parse_target
    test eax, eax
    js .err
    call kifs_load_parent
    test eax, eax
    js .err
    mov eax, [kifs_dir_size]
    mov edi, [kifs_name_ptr]
    mov ecx, [kifs_name_len]
    call kifs_lookup_in_dir
    test eax, eax
    jnz .exists
    call kifs_alloc_oid
    test eax, eax
    jz .recfull
    mov [kifs_nw_oid], eax
    ; ---- 新目录 record ----
    mov edi, KIFS_REC2_BUF
    xor eax, eax
    mov ecx, KIFS_REC_SIZE / 4
    cld
    rep stosd
    mov edi, KIFS_REC2_BUF
    mov eax, [kifs_nw_oid]
    mov [edi + 0], eax
    mov byte [edi + 4], 2
    mov byte [edi + 5], 0
    mov word [edi + 6], KIFS_PERM_DIR
    mov dword [edi + 8], 0
    mov dword [edi + 12], 0
    mov eax, [kifs_parent_oid]
    mov [edi + 16], eax
    call kifs_now
    mov [KIFS_REC2_BUF + 20], eax
    mov [KIFS_REC2_BUF + 24], eax
    mov dword [KIFS_REC2_BUF + 28], 0
    mov byte [KIFS_REC2_BUF + 32], 0
    mov byte [KIFS_REC2_BUF + 33], 0
    mov word [KIFS_REC2_BUF + 34], KIFS_INLINE_OFF
    mov dword [KIFS_REC2_BUF + 72], 0
    mov byte [KIFS_REC2_BUF + 76], 0
    mov dword [KIFS_REC2_BUF + 80], 0
    mov dword [KIFS_REC2_BUF + 84], 0x0069616B   ; owner="kai"
    mov esi, KIFS_REC2_BUF
    call kifs_write_record
    test eax, eax
    jnz .writeerr
    ; ---- 父目录项 ----
    mov esi, KIFS_REC_BUF
    mov edi, [kifs_name_ptr]
    mov ecx, [kifs_name_len]
    mov edx, [kifs_nw_oid]
    mov ebx, 2
    call kifs_dir_add
    test eax, eax
    jnz .err
    mov esi, KIFS_REC_BUF
    call kifs_write_record
    test eax, eax
    jnz .writeerr
    call kifs_flush
    test eax, eax
    jnz .writeerr
    ; ---- 成功 ----
    mov al, COL_NORMAL
    call term_set_color
    mov esi, msg_kifs_mkdir_ok
    call term_print
    mov esi, KIFS_PATH_BUF
    call term_print
    mov esi, msg_kifs_oid
    call term_print
    mov eax, [kifs_nw_oid]
    call term_print_dec
    mov esi, msg_kifs_close
    call term_print
    jmp .done
.exists:
    mov dword [kifs_err], KIFS_ERR_EXISTS
    jmp .err
.recfull:
    mov dword [kifs_err], KIFS_ERR_RECFULL
    jmp .err
.writeerr:
    mov dword [kifs_err], KIFS_ERR_WRITEERR
.err:
    call kifs_txn_abort
    call kifs_print_err
.done:
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_cmd_write:kifs write <path> <text>   /   ... | kifs write <path>
;  没有文本参数就从管道读 stdin(最多 32 KiB)
; ---------------------------------------------------------------------------
kifs_cmd_write:
    pushad
    mov esi, [usbd_rest]
    call kifs_split_word
    call kifs_word_to_path
    test eax, eax
    js .toolong
    ; ---- 数据缓冲清零 ----
    mov edi, KIFS_DATA_BUF
    xor eax, eax
    mov ecx, KIFS_DATA_MAX / 4
    cld
    rep stosd
    ; ---- 文本参数?没有就读 stdin ----
    mov esi, [kifs_rest]
    xor ecx, ecx
.len:
    cmp byte [esi + ecx], 0
    je .len_done
    inc ecx
    jmp .len
.len_done:
    test ecx, ecx
    jnz .text
    mov edi, KIFS_DATA_BUF
    mov ecx, KIFS_DATA_MAX
    call pipe_read
    mov ecx, eax
    jmp .have
.text:
    cmp ecx, KIFS_DATA_MAX
    ja .toolong
    mov [kifs_wr_len2], ecx
    mov esi, [kifs_rest]
    mov edi, KIFS_DATA_BUF
    cld
    rep movsb
    mov ecx, [kifs_wr_len2]
.have:
    mov [kifs_wr_len2], ecx
    ; ---- 定位 ----
    call kifs_parse_target
    test eax, eax
    js .err
    call kifs_load_parent
    test eax, eax
    js .err
    mov eax, [kifs_dir_size]
    mov edi, [kifs_name_ptr]
    mov ecx, [kifs_name_len]
    call kifs_lookup_in_dir
    test eax, eax
    jz .create
    ; ---- 覆盖已有文件 ----
    mov [kifs_wr_oid], eax
    mov edi, KIFS_REC2_BUF
    call kifs_read_record
    test eax, eax
    jnz .writeerr
    cmp byte [KIFS_REC2_BUF + 4], 1
    jne .notfile
    test word [KIFS_REC2_BUF + 6], KIFS_PERM_RO
    jnz .ro
    mov esi, KIFS_REC2_BUF
    mov edi, KIFS_DATA_BUF
    mov ecx, [kifs_wr_len2]
    call kifs_write_content
    test eax, eax
    js .err
    call kifs_now
    mov [KIFS_REC2_BUF + 24], eax
    mov esi, KIFS_REC2_BUF
    call kifs_write_record
    test eax, eax
    jnz .writeerr
    jmp .flush
.create:
    ; ---- 新文件 ----
    call kifs_alloc_oid
    test eax, eax
    jz .recfull
    mov [kifs_wr_oid], eax
    mov edi, KIFS_REC2_BUF
    xor eax, eax
    mov ecx, KIFS_REC_SIZE / 4
    cld
    rep stosd
    mov edi, KIFS_REC2_BUF
    mov eax, [kifs_wr_oid]
    mov [edi + 0], eax
    mov byte [edi + 4], 1
    mov byte [edi + 5], 0
    mov word [edi + 6], KIFS_PERM_FILE
    mov eax, [kifs_wr_len2]
    mov [edi + 8], eax
    mov dword [edi + 12], 0
    mov eax, [kifs_parent_oid]
    mov [edi + 16], eax
    call kifs_now
    mov [KIFS_REC2_BUF + 20], eax
    mov [KIFS_REC2_BUF + 24], eax
    mov dword [KIFS_REC2_BUF + 28], 0
    mov byte [KIFS_REC2_BUF + 32], 0
    mov byte [KIFS_REC2_BUF + 33], 0
    mov word [KIFS_REC2_BUF + 34], KIFS_INLINE_OFF
    mov dword [KIFS_REC2_BUF + 72], 0
    mov byte [KIFS_REC2_BUF + 76], 0
    mov dword [KIFS_REC2_BUF + 80], 0
    mov dword [KIFS_REC2_BUF + 84], 0x0069616B   ; owner="kai"
    mov esi, KIFS_REC2_BUF
    mov edi, KIFS_DATA_BUF
    mov ecx, [kifs_wr_len2]
    call kifs_write_content
    test eax, eax
    js .err
    mov esi, KIFS_REC2_BUF
    call kifs_write_record
    test eax, eax
    jnz .writeerr
    mov esi, KIFS_REC_BUF
    mov edi, [kifs_name_ptr]
    mov ecx, [kifs_name_len]
    mov edx, [kifs_wr_oid]
    mov ebx, 1
    call kifs_dir_add
    test eax, eax
    jnz .err
    mov esi, KIFS_REC_BUF
    call kifs_write_record
    test eax, eax
    jnz .writeerr
.flush:
    call kifs_flush
    test eax, eax
    jnz .writeerr
    ; ---- 成功 ----
    mov al, COL_NORMAL
    call term_set_color
    mov esi, msg_kifs_write_ok
    call term_print
    mov eax, [kifs_wr_len2]
    call term_print_dec
    mov esi, msg_kifs_bytes_to
    call term_print
    mov esi, KIFS_PATH_BUF
    call term_print
    mov esi, msg_kifs_oid
    call term_print
    mov eax, [kifs_wr_oid]
    call term_print_dec
    mov esi, msg_kifs_close
    call term_print
    jmp .done
.notfile:
    mov dword [kifs_err], KIFS_ERR_NOTFILE
    jmp .err
.ro:
    mov dword [kifs_err], KIFS_ERR_RO
    jmp .err
.toolong:
    mov dword [kifs_err], KIFS_ERR_TOOBIG
    jmp .err
.recfull:
    mov dword [kifs_err], KIFS_ERR_RECFULL
    jmp .err
.writeerr:
    mov dword [kifs_err], KIFS_ERR_WRITEERR
    jmp .err
.err:
    call kifs_txn_abort
    call kifs_print_err
.done:
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_cmd_rm:kifs rm <path>
; ---------------------------------------------------------------------------
kifs_cmd_rm:
    pushad
    call kifs_parse_target
    test eax, eax
    js .err
    call kifs_load_parent
    test eax, eax
    js .err
    mov eax, [kifs_dir_size]
    mov edi, [kifs_name_ptr]
    mov ecx, [kifs_name_len]
    call kifs_lookup_in_dir
    test eax, eax
    jz .noent
    mov edi, KIFS_REC2_BUF
    call kifs_read_record
    test eax, eax
    jnz .writeerr
    test word [KIFS_REC2_BUF + 6], KIFS_PERM_RO
    jnz .ro
    cmp byte [KIFS_REC2_BUF + 4], 2
    jne .not_dir_check
    ; 目录必须为空
    mov esi, KIFS_REC2_BUF
    mov edi, KIFS_DIR_BUF
    mov ecx, KIFS_DIR_MAX
    call kifs_read_content
    test eax, eax
    js .writeerr
    test eax, eax
    jnz .notempty
.not_dir_check:
    mov esi, KIFS_REC2_BUF
    call kifs_free_rec_data
    mov byte [KIFS_REC2_BUF + 4], 0
    mov esi, KIFS_REC2_BUF
    call kifs_write_record
    test eax, eax
    jnz .writeerr
    mov esi, KIFS_REC_BUF
    mov edi, [kifs_name_ptr]
    mov ecx, [kifs_name_len]
    call kifs_dir_remove
    test eax, eax
    jnz .err
    mov esi, KIFS_REC_BUF
    call kifs_write_record
    test eax, eax
    jnz .writeerr
    call kifs_flush
    test eax, eax
    jnz .writeerr
    ; ---- 成功 ----
    mov al, COL_NORMAL
    call term_set_color
    mov esi, msg_kifs_rm_ok
    call term_print
    mov esi, KIFS_PATH_BUF
    call term_print
    mov al, 10
    call term_putc
    jmp .done
.noent:
    mov dword [kifs_err], KIFS_ERR_NOENT
    jmp .err
.notempty:
    mov dword [kifs_err], KIFS_ERR_NOTEMPTY
    jmp .err
.ro:
    mov dword [kifs_err], KIFS_ERR_RO
    jmp .err
.writeerr:
    mov dword [kifs_err], KIFS_ERR_WRITEERR
.err:
    call kifs_txn_abort
    call kifs_print_err
.done:
    popad
    ret

; ---------------------------------------------------------------------------
;  kifs_cmd_journal:kifs journal —— 列 journal 里的事务(按 seq 升序)
; ---------------------------------------------------------------------------
kifs_cmd_journal:
    pushad
    call kifs_jrn_scan
    test eax, eax
    js .io
    mov esi, msg_kifs_jrn_head
    call term_print
    mov eax, [kifs_journal_blocks]
    call term_print_dec
    mov esi, msg_kifs_jrn_blocks
    call term_print
    mov eax, [kifs_txn_n]
    call term_print_dec
    mov esi, msg_kifs_jrn_txns
    call term_print
    cmp dword [kifs_txn_n], 0
    jne .list
    mov esi, msg_kifs_jrn_empty
    call term_print
    jmp .done
.list:
    mov dword [kifs_jl_last], 0
.outer:
    mov dword [kifs_jl_best], -1
    mov dword [kifs_jl_ptr], 0
    xor ebx, ebx
.search:
    cmp ebx, [kifs_txn_n]
    jae .search_done
    mov esi, ebx
    imul esi, esi, 20
    add esi, KIFS_TXN
    mov eax, [esi + 0]
    cmp eax, [kifs_jl_last]
    jbe .s_next
    mov ecx, [kifs_jl_best]
    cmp ecx, -1
    je .take
    cmp eax, ecx
    jae .s_next
.take:
    mov [kifs_jl_best], eax
    mov [kifs_jl_ptr], esi
.s_next:
    inc ebx
    jmp .search
.search_done:
    cmp dword [kifs_jl_best], -1
    je .done
    mov esi, [kifs_jl_ptr]
    mov [kifs_jl_entry], esi
    mov esi, msg_kifs_jrn_seq
    call term_print
    mov esi, [kifs_jl_entry]
    mov eax, [esi + 0]
    call term_print_dec
    mov esi, msg_kifs_jrn_state
    call term_print
    mov esi, [kifs_jl_entry]
    cmp dword [esi + 12], -1
    jne .st_end
    cmp dword [esi + 8], -1
    jne .st_commit
    mov esi, msg_kifs_jrn_open
    jmp .st_print
.st_end:
    mov esi, msg_kifs_jrn_end
    jmp .st_print
.st_commit:
    mov esi, msg_kifs_jrn_commit
.st_print:
    call term_print
    mov esi, msg_kifs_jrn_recs
    call term_print
    mov esi, [kifs_jl_entry]
    mov eax, [esi + 16]
    call term_print_dec
    mov al, 10
    call term_putc
    mov eax, [kifs_jl_best]
    mov [kifs_jl_last], eax
    jmp .outer
.io:
    mov dword [kifs_err], KIFS_ERR_IO
    call kifs_print_err
.done:
    popad
    ret


; ===========================================================================
;  数据
; ===========================================================================
kifs_ok            db 0
kifs_version       dd 0
kifs_block_size    dd 0
kifs_total_blocks  dd 0
kifs_record_start  dd 0
kifs_record_count  dd 0
kifs_root_oid      dd 0
kifs_free_blocks   dd 0
kifs_flags         dd 0
kifs_label         times 33 db 0
kifs_err           dd 0

; 函数间的小状态(flat 内核里用内存传参,省得跟一堆 helper 的寄存器契约打架)
kifs_rb_ret        dd 0
kifs_rr_oid        dd 0
kifs_rr_dst        dd 0
kifs_rr_ret        dd 0

kifs_rc_rec        dd 0
kifs_rc_dst        dd 0
kifs_rc_max        dd 0
kifs_rc_left       dd 0
kifs_rc_written    dd 0
kifs_rc_ne         dd 0
kifs_rc_exti       dd 0
kifs_rc_cur        dd 0
kifs_rc_n          dd 0
kifs_rc_blk        dd 0
kifs_rc_bcnt       dd 0
kifs_rc_ret        dd 0

kifs_rs_path       dd 0
kifs_rs_oid        dd 0
kifs_rs_comp       dd 0
kifs_rs_complen    dd 0
kifs_rs_dirsize    dd 0
kifs_rs_pos        dd 0
kifs_rs_ret        dd 0

kifs_perm_tmp      dd 0
kifs_word_ptr      dd 0
kifs_word_len      dd 0
kifs_path_len      dd 0

kifs_ls_size       dd 0
kifs_ls_pos        dd 0

kifs_cat_left      dd 0
kifs_cat_ne        dd 0
kifs_cat_exti      dd 0
kifs_cat_blk       dd 0
kifs_cat_bcnt      dd 0
kifs_cat_n         dd 0
kifs_cat_printed   dd 0
kifs_cat_last      db 0

kifs_st_i          dd 0

; ---- Phase C1 状态 ----
kifs_data_start      dd 0
kifs_data_next       dd 0
kifs_meta_next       dd 0
kifs_journal_start   dd 0
kifs_journal_blocks  dd 0
kifs_bitmap_dirty    db 0

kifs_delta_n         dd 0
kifs_das_ret         dd 0
kifs_dab_ret         dd 0

kifs_ab_len          dd 0
kifs_ab_start        dd 0
kifs_ab_i            dd 0
kifs_ab_ret          dd 0

kifs_fb_start        dd 0
kifs_fb_cnt          dd 0
kifs_fb_i            dd 0

kifs_fd_rec          dd 0
kifs_fd_ne           dd 0
kifs_fd_i            dd 0

kifs_ao_ret          dd 0

kifs_wr_rec          dd 0
kifs_wr_ret          dd 0
kifs_wr_len2         dd 0
kifs_wr_oid          dd 0

kifs_wc_rec          dd 0
kifs_wc_data         dd 0
kifs_wc_len          dd 0
kifs_wc_blocks       dd 0
kifs_wc_start        dd 0
kifs_wc_i            dd 0
kifs_wc_ret          dd 0
kifs_wc_oldn         dd 0
kifs_wc_ostart       times 3 dd 0
kifs_wc_ocnt         times 3 dd 0

kifs_ms_ret          dd 0

kifs_j_seq           dd 0
kifs_j_flags         dd 0
kifs_j_offset        dd 0
kifs_j_target        dd 0
kifs_j_len           dd 0
kifs_jp_ret          dd 0

kifs_js_i            dd 0
kifs_js_ret          dd 0
kifs_txn_n           dd 0
kifs_jrn_maxseq      dd 0
kifs_jrn_usedmax     dd 0

kifs_tf_seq          dd 0
kifs_tf_ret          dd 0

kifs_jb_seq          dd 0
kifs_jb_flags        dd 0
kifs_jb_off          dd 0
kifs_jb_tgt          dd 0
kifs_jb_ptr          dd 0
kifs_jb_len          dd 0

kifs_we_slot         dd 0
kifs_we_ret          dd 0

kifs_rp_seq          dd 0
kifs_rp_i            dd 0
kifs_rp_ret          dd 0

kifs_jc_seq          dd 0
kifs_jc_slot         dd 0
kifs_jc_begin        dd 0
kifs_jc_di           dd 0
kifs_jc_off          dd 0
kifs_jc_chunk        dd 0
kifs_jc_total        dd 0
kifs_jc_ptr          dd 0
kifs_jc_entry        dd 0
kifs_jc_aoff         dd 0
kifs_jc_alen         dd 0
kifs_jc_atgt         dd 0
kifs_jc_ret          dd 0

kifs_fl_ret          dd 0

kifs_rv_ret          dd 0
kifs_rv_count        dd 0
kifs_rv_last         dd 0
kifs_rv_best         dd 0
kifs_rv_bestptr      dd 0
kifs_rv_entry        dd 0

kifs_sw_ptr          dd 0
kifs_sw_len          dd 0
kifs_rest            dd 0
kifs_wtp_len         dd 0
kifs_wtp_ret         dd 0

kifs_pt_start        dd 0
kifs_pt_ret          dd 0
kifs_parent_len      dd 0
kifs_name_ptr        dd 0
kifs_name_len        dd 0

kifs_parent_oid      dd 0
kifs_dir_size        dd 0
kifs_lp_ret          dd 0

kifs_lu_size         dd 0
kifs_lu_name         dd 0
kifs_lu_nlen         dd 0
kifs_lu_pos          dd 0
kifs_lu_ret          dd 0

kifs_nc_p1           dd 0
kifs_nc_l1           dd 0
kifs_nc_p2           dd 0
kifs_nc_l2           dd 0

kifs_pd_oid          dd 0
kifs_pd_type         dd 0
kifs_pd_name         dd 0
kifs_pd_nlen         dd 0
kifs_pd_elen         dd 0
kifs_pd_ret          dd 0

kifs_da_rec          dd 0
kifs_da_name         dd 0
kifs_da_nlen         dd 0
kifs_da_oid          dd 0
kifs_da_type         dd 0
kifs_da_size         dd 0
kifs_da_elen         dd 0
kifs_da_newsize      dd 0
kifs_da_pos          dd 0
kifs_da_out          dd 0
kifs_da_ins          dd 0
kifs_da_ret          dd 0

kifs_dr_rec          dd 0
kifs_dr_name         dd 0
kifs_dr_nlen         dd 0
kifs_dr_size         dd 0
kifs_dr_pos          dd 0
kifs_dr_out          dd 0
kifs_dr_found        dd 0
kifs_dr_ret          dd 0

kifs_nw_oid          dd 0

kifs_jl_last         dd 0
kifs_jl_best         dd 0
kifs_jl_ptr          dd 0
kifs_jl_entry        dd 0

; 子命令名(和 shell.asm 的 cmd_table 一起用)
n_kifs_info  db 'info', 0
n_kifs_ls    db 'ls', 0
n_kifs_cat   db 'cat', 0
n_kifs_stat  db 'stat', 0
n_kifs_mkdir db 'mkdir', 0
n_kifs_write db 'write', 0
n_kifs_rm    db 'rm', 0
n_kifs_journal db 'journal', 0

; 文案。错误文案按任务规格逐字固定,测试断言要匹配。
msg_kifs_noimg     db 'kifs: no KiFS image (read-write driver at LBA 22528)', 10, 0
msg_kifs_usage     db 'kifs: usage: kifs info | ls [path] | cat <path> | stat <path> | mkdir <path> | write <path> [text] | rm <path> | journal', 10, 0
msg_kifs_notfound  db 'kifs: path not found', 10, 0
msg_kifs_notdir    db 'kifs: not a directory', 10, 0
msg_kifs_notfile   db 'kifs: not a file', 10, 0
msg_kifs_dirbig    db 'kifs: directory too big (max 4 KiB)', 10, 0
msg_kifs_filebig   db 'kifs: file too big (Phase B0 cat supports <= 60 KiB)', 10, 0
msg_kifs_ioerr     db 'kifs: read error (ATA)', 10, 0

msg_kifs_info_head db 'kifs: KiFS read-write driver (Phase C1), partition at LBA 22528', 10, 0
msg_kifs_magic     db '  magic=KIFS version=', 0
msg_kifs_blocksize db ' block_size=', 0
msg_kifs_total     db '  total_blocks=', 0
msg_kifs_root      db ' root_oid=', 0
msg_kifs_free      db ' free_blocks=', 0
msg_kifs_label     db '  label=', 0
msg_kifs_flags     db ' flags=', 0

msg_kifs_stat_head db 'kifs stat: ', 0
msg_kifs_stat_oid  db '  oid=', 0
msg_kifs_stat_type db ' type=', 0
msg_kifs_stat_perm db '  perm=', 0
msg_kifs_stat_size db '  size=', 0
msg_kifs_bytes     db ' bytes', 0
msg_kifs_stat_blocks db '  blocks=', 0
msg_kifs_stat_inline db ' inline_len=', 0
msg_kifs_stat_ext  db '  extents=', 0
msg_kifs_stat_ext_one db ' (start=', 0
msg_kifs_stat_ext_blocks db ' blocks=', 0
msg_kifs_stat_parent db '  parent=', 0

msg_kifs_word_dir  db 'dir', 0
msg_kifs_word_file db 'file', 0

msg_kifs_exists    db 'kifs: path already exists', 10, 0
msg_kifs_notempty  db 'kifs: directory not empty', 10, 0
msg_kifs_ro        db 'kifs: file is read-only (ro)', 10, 0
msg_kifs_noroot    db 'kifs: cannot operate on root /', 10, 0
msg_kifs_nospc     db 'kifs: no space left (data area full)', 10, 0
msg_kifs_toobig    db 'kifs: file too big (max 32 KiB)', 10, 0
msg_kifs_recfull   db 'kifs: record table full', 10, 0
msg_kifs_jrnfull   db 'kifs: journal full', 10, 0
msg_kifs_writeerr  db 'kifs: write error (ATA)', 10, 0

msg_kifs_mkdir_ok  db 'kifs: mkdir ', 0
msg_kifs_write_ok  db 'kifs: wrote ', 0
msg_kifs_bytes_to  db ' bytes to ', 0
msg_kifs_rm_ok     db 'kifs: removed ', 0
msg_kifs_oid       db ' (oid=', 0
msg_kifs_close     db ')', 10, 0

msg_kifs_jrn_head   db 'journal: ', 0
msg_kifs_jrn_blocks db ' blocks, ', 0
msg_kifs_jrn_txns   db ' transactions', 10, 0
msg_kifs_jrn_empty  db '  (empty)', 10, 0
msg_kifs_jrn_seq    db '  seq=', 0
msg_kifs_jrn_state  db ' state=', 0
msg_kifs_jrn_end    db 'END', 0
msg_kifs_jrn_commit db 'COMMIT', 0
msg_kifs_jrn_open   db 'OPEN', 0
msg_kifs_jrn_recs   db ' records=', 0

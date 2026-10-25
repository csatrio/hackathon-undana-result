"""System telemetry (kind 1) from telemetry_db/system_telemetry.db: derived metrics, their translation, and a plain-language story.

Each row is one host snapshot every ~5 s. Most fields are cumulative kernel counters (cpu jiffies, tcp segments, ...), so rates and
percentages come from the difference between consecutive snapshots of the SAME host. A counter that goes backwards (reboot) is skipped.
"""
import json
import sqlite3
from pathlib import Path

ONLINE_GAP_S = 30  # no snapshot for this long = host considered offline for that stretch
CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")

# ---- translation: derived metrics --------------------------------------------------------------------------------------
# id: (label_en, label_id, unit, description_en, description_id)
DERIVED = {
    "stress": ("Stress score", "Skor stres", "0-100",
               "Worst of CPU, memory, load per core and kernel pressure. Higher = closer to falling over.",
               "Yang terburuk dari CPU, memori, beban per core dan tekanan kernel. Makin tinggi makin dekat ke tumbang."),
    "cpu_pct": ("CPU busy", "CPU terpakai", "%", "Share of CPU time spent working (not idle, not waiting for disk).", "Porsi waktu CPU yang dipakai bekerja (bukan idle, bukan menunggu disk)."),
    "cpu_user_pct": ("CPU user", "CPU user", "%", "CPU time in application code.", "Waktu CPU di kode aplikasi."),
    "cpu_system_pct": ("CPU system", "CPU sistem", "%", "CPU time in the kernel (syscalls, networking).", "Waktu CPU di kernel (syscall, jaringan)."),
    "cpu_iowait_pct": ("CPU waiting for disk", "CPU menunggu disk", "%", "CPU idle only because it waits for disk I/O.", "CPU menganggur karena menunggu I/O disk."),
    "cpu_softirq_pct": ("CPU softirq", "CPU softirq", "%", "CPU time handling network packets in the kernel.", "Waktu CPU memproses paket jaringan di kernel."),
    "cores": ("CPU cores", "Jumlah core", "cores", "Estimated from CPU jiffies per second.", "Perkiraan dari jiffies CPU per detik."),
    "load1": ("Load (1 min)", "Beban (1 mnt)", "procs", "Average number of processes running or waiting to run.", "Rata-rata proses yang berjalan atau mengantre."),
    "load_per_core": ("Load per core", "Beban per core", "x", "Load divided by cores. Above 1 the CPU has a queue; above 2 it is overloaded.", "Beban dibagi core. Di atas 1 ada antrean; di atas 2 kelebihan beban."),
    "mem_used_pct": ("Memory used", "Memori terpakai", "%", "100% minus 'available' memory (what the kernel can still hand out).", "100% dikurangi memori 'available' (yang masih bisa diberikan kernel)."),
    "mem_avail_mb": ("Memory available", "Memori tersedia", "MB", "Memory that can be given to programs without swapping.", "Memori yang bisa diberikan ke program tanpa swap."),
    "mem_anon_gb": ("Program memory", "Memori program", "GB", "Memory held by programs (heap/stack), not cache.", "Memori yang dipegang program (heap/stack), bukan cache."),
    "psi_cpu": ("CPU pressure", "Tekanan CPU", "%", "Share of the last 10 s in which some task waited for CPU. 0 = never waits.", "Porsi 10 detik terakhir saat ada tugas menunggu CPU. 0 = tidak pernah menunggu."),
    "psi_io": ("Disk pressure", "Tekanan disk", "%", "Share of the last 10 s in which some task stalled on disk I/O.", "Porsi 10 detik terakhir saat ada tugas macet menunggu disk."),
    "psi_io_full": ("Disk stall (all tasks)", "Disk macet (semua tugas)", "%", "Share of the last 10 s in which EVERY task was stalled on disk.", "Porsi 10 detik terakhir saat SEMUA tugas macet menunggu disk."),
    "psi_mem": ("Memory pressure", "Tekanan memori", "%", "Share of the last 10 s in which tasks stalled reclaiming memory. Above 0 means RAM is short.", "Porsi 10 detik terakhir saat tugas macet mencari memori. Di atas 0 berarti RAM kurang."),
    "tcp_established": ("Open TCP connections", "Koneksi TCP terbuka", "conns", "Connections currently open (established).", "Koneksi yang sedang terbuka."),
    "tcp_time_wait": ("TCP TIME_WAIT", "TCP TIME_WAIT", "conns", "Closed connections the kernel still keeps for a short while.", "Koneksi yang sudah ditutup tapi masih disimpan kernel sebentar."),
    "tcp_in_segs_ps": ("TCP packets in", "Paket TCP masuk", "/s", "TCP segments received per second.", "Segmen TCP diterima per detik."),
    "tcp_out_segs_ps": ("TCP packets out", "Paket TCP keluar", "/s", "TCP segments sent per second.", "Segmen TCP dikirim per detik."),
    "tcp_retrans_pct": ("TCP retransmits", "TCP kirim ulang", "%", "Share of sent segments that had to be re-sent (lost or too slow). High = congestion or overload.", "Porsi segmen terkirim yang harus dikirim ulang (hilang/terlalu lambat). Tinggi = macet atau kelebihan beban."),
    "tcp_listen_overflows_ps": ("Dropped new connections", "Koneksi baru dibuang", "/s", "Connections refused because the accept queue was full: the server could not keep up.", "Koneksi ditolak karena antrean accept penuh: server tidak sanggup mengimbangi."),
    "tcp_resets_ps": ("Connection resets", "Koneksi di-reset", "/s", "Established connections killed by a reset.", "Koneksi terbuka yang diputus paksa (reset)."),
    "tcp_timeouts_ps": ("TCP timeouts", "TCP timeout", "/s", "Retransmission timeouts: the peer stopped answering.", "Timeout kirim ulang: lawan bicara berhenti menjawab."),
    "tcp_in_errs_ps": ("TCP bad packets", "Paket TCP rusak", "/s", "Segments received with errors (checksum/format).", "Segmen diterima dengan galat (checksum/format)."),
    "ctx_switch_ps": ("Context switches", "Context switch", "/s", "How often the CPU switches between tasks. Very high = many busy threads.", "Seberapa sering CPU berpindah tugas. Sangat tinggi = banyak thread sibuk."),
    "interrupts_ps": ("Interrupts", "Interupsi", "/s", "Hardware interrupts per second (mostly network cards).", "Interupsi perangkat keras per detik (kebanyakan kartu jaringan)."),
    "forks_ps": ("Processes started", "Proses dimulai", "/s", "New processes created per second.", "Proses baru dibuat per detik."),
    "procs_running": ("Runnable processes", "Proses siap jalan", "procs", "Processes ready to run right now.", "Proses yang siap jalan sekarang."),
    "oom_kills": ("OOM kills", "Proses dibunuh OOM", "events", "Processes killed by the kernel because memory ran out.", "Proses dibunuh kernel karena memori habis."),
    "pgscan_direct_ps": ("Memory reclaim scans", "Pemindaian reclaim memori", "pages/s", "Pages scanned when a program had to wait for memory to be freed.", "Halaman yang dipindai saat program harus menunggu memori dibebaskan."),
    "major_faults_ps": ("Major page faults", "Page fault mayor", "/s", "Memory pages that had to be read from disk.", "Halaman memori yang harus dibaca dari disk."),
    "disk_read_kbps": ("Disk read", "Baca disk", "KB/s", "Data paged in from disk.", "Data dibaca dari disk."),
    "disk_write_kbps": ("Disk write", "Tulis disk", "KB/s", "Data paged out to disk.", "Data ditulis ke disk."),
    "ipc": ("Instructions per cycle", "Instruksi per siklus", "x", "CPU efficiency. Low = CPU stalls (cache misses, waiting for memory).", "Efisiensi CPU. Rendah = CPU tersendat (cache miss, menunggu memori)."),
    "cache_miss_pct": ("CPU cache misses", "Cache miss CPU", "%", "Share of cache lookups that went to slow RAM.", "Porsi pencarian cache yang harus ke RAM yang lambat."),
}
DERIVED_GROUPS = {
    "Overview": ["stress"], "CPU": ["cpu_pct", "cpu_user_pct", "cpu_system_pct", "cpu_iowait_pct", "cpu_softirq_pct", "cores", "load1", "load_per_core", "procs_running", "ctx_switch_ps", "interrupts_ps", "forks_ps", "ipc", "cache_miss_pct"],
    "Memory": ["mem_used_pct", "mem_avail_mb", "mem_anon_gb", "psi_mem", "oom_kills", "pgscan_direct_ps", "major_faults_ps"],
    "Pressure": ["psi_cpu", "psi_io", "psi_io_full"], "Disk": ["disk_read_kbps", "disk_write_kbps"],
    "Network": ["tcp_established", "tcp_time_wait", "tcp_in_segs_ps", "tcp_out_segs_ps", "tcp_retrans_pct", "tcp_listen_overflows_ps", "tcp_resets_ps", "tcp_timeouts_ps", "tcp_in_errs_ps"],
}

# ---- translation: raw keys ---------------------------------------------------------------------------------------------
# key: (unit, kind, english, indonesian)   kind: counter (only grows) | gauge (current value) | info
RAW = {
    "cpu.user": ("jiffies", "counter", "CPU time in application code since boot (1 jiffy = 1/clk_tck s).", "Waktu CPU di kode aplikasi sejak boot (1 jiffy = 1/clk_tck detik)."),
    "cpu.nice": ("jiffies", "counter", "CPU time in low-priority (niced) processes.", "Waktu CPU di proses berprioritas rendah."),
    "cpu.system": ("jiffies", "counter", "CPU time in the kernel.", "Waktu CPU di kernel."),
    "cpu.idle": ("jiffies", "counter", "CPU time doing nothing.", "Waktu CPU menganggur."),
    "cpu.iowait": ("jiffies", "counter", "CPU idle while waiting for disk I/O.", "CPU menganggur menunggu I/O disk."),
    "cpu.irq": ("jiffies", "counter", "CPU time serving hardware interrupts.", "Waktu CPU melayani interupsi perangkat keras."),
    "cpu.softirq": ("jiffies", "counter", "CPU time serving software interrupts (network packets).", "Waktu CPU melayani interupsi perangkat lunak (paket jaringan)."),
    "cpu.steal": ("jiffies", "counter", "CPU time taken by the hypervisor for other VMs. Above 0 = noisy neighbour.", "Waktu CPU diambil hypervisor untuk VM lain. Di atas 0 = tetangga berisik."),
    "cpu.context_switches": ("count", "counter", "Times the CPU switched between tasks since boot.", "Berapa kali CPU berpindah tugas sejak boot."),
    "cpu.interrupts": ("count", "counter", "Hardware interrupts since boot.", "Interupsi perangkat keras sejak boot."),
    "cpu.softirqs": ("count", "counter", "Software interrupts since boot.", "Interupsi perangkat lunak sejak boot."),
    "cpu.forks": ("count", "counter", "Processes created since boot.", "Proses dibuat sejak boot."),
    "cpu.procs_running": ("procs", "gauge", "Processes running or ready to run now.", "Proses berjalan atau siap jalan sekarang."),
    "cpu.procs_blocked": ("procs", "gauge", "Processes blocked waiting for I/O now.", "Proses terblokir menunggu I/O sekarang."),
    "load.1m": ("procs", "gauge", "Load average over 1 minute.", "Rata-rata beban 1 menit."),
    "load.5m": ("procs", "gauge", "Load average over 5 minutes.", "Rata-rata beban 5 menit."),
    "load.15m": ("procs", "gauge", "Load average over 15 minutes.", "Rata-rata beban 15 menit."),
    "mem.total": ("bytes", "gauge", "Installed RAM.", "RAM terpasang."),
    "mem.available": ("bytes", "gauge", "RAM that programs can still get without swapping.", "RAM yang masih bisa didapat program tanpa swap."),
    "mem.anon": ("bytes", "gauge", "RAM held by program data (not cache).", "RAM yang dipegang data program (bukan cache)."),
    "mem.cached": ("bytes", "gauge", "RAM used as file cache (reclaimable).", "RAM untuk cache file (bisa diambil kembali)."),
    "mem.slab": ("bytes", "gauge", "RAM used by kernel data structures.", "RAM untuk struktur data kernel."),
    "mem.dirty": ("bytes", "gauge", "Cached data waiting to be written to disk.", "Data cache yang menunggu ditulis ke disk."),
    "mem.writeback": ("bytes", "gauge", "Cached data being written to disk right now.", "Data cache yang sedang ditulis ke disk."),
    "mem.swap_total": ("bytes", "gauge", "Swap space size (0 = no swap: memory exhaustion means OOM kills).", "Ukuran swap (0 = tanpa swap: memori habis berarti proses dibunuh)."),
    "mem.swap_used": ("bytes", "gauge", "Swap space in use.", "Swap yang terpakai."),
    "psi.cpu.some_avg10": ("%", "gauge", "Share of last 10 s some task waited for CPU.", "Porsi 10 detik terakhir ada tugas menunggu CPU."),
    "psi.cpu.some_avg60": ("%", "gauge", "Same, last 60 s.", "Sama, 60 detik terakhir."),
    "psi.cpu.some_avg300": ("%", "gauge", "Same, last 5 min.", "Sama, 5 menit terakhir."),
    "psi.cpu.some_total": ("us", "counter", "Total microseconds some task waited for CPU.", "Total mikrodetik ada tugas menunggu CPU."),
    "psi.cpu.full_avg10": ("%", "gauge", "Share of last 10 s ALL tasks waited for CPU.", "Porsi 10 detik terakhir SEMUA tugas menunggu CPU."),
    "psi.cpu.full_avg60": ("%", "gauge", "Same, last 60 s.", "Sama, 60 detik terakhir."),
    "psi.cpu.full_avg300": ("%", "gauge", "Same, last 5 min.", "Sama, 5 menit terakhir."),
    "psi.cpu.full_total": ("us", "counter", "Total microseconds all tasks waited for CPU.", "Total mikrodetik semua tugas menunggu CPU."),
    "psi.io.some_avg10": ("%", "gauge", "Share of last 10 s some task stalled on disk.", "Porsi 10 detik terakhir ada tugas macet di disk."),
    "psi.io.some_avg60": ("%", "gauge", "Same, last 60 s.", "Sama, 60 detik terakhir."),
    "psi.io.some_avg300": ("%", "gauge", "Same, last 5 min.", "Sama, 5 menit terakhir."),
    "psi.io.some_total": ("us", "counter", "Total microseconds some task stalled on disk.", "Total mikrodetik ada tugas macet di disk."),
    "psi.io.full_avg10": ("%", "gauge", "Share of last 10 s ALL tasks stalled on disk.", "Porsi 10 detik terakhir SEMUA tugas macet di disk."),
    "psi.io.full_avg60": ("%", "gauge", "Same, last 60 s.", "Sama, 60 detik terakhir."),
    "psi.io.full_avg300": ("%", "gauge", "Same, last 5 min.", "Sama, 5 menit terakhir."),
    "psi.io.full_total": ("us", "counter", "Total microseconds all tasks stalled on disk.", "Total mikrodetik semua tugas macet di disk."),
    "psi.memory.some_avg10": ("%", "gauge", "Share of last 10 s some task stalled on memory.", "Porsi 10 detik terakhir ada tugas macet karena memori."),
    "psi.memory.some_avg60": ("%", "gauge", "Same, last 60 s.", "Sama, 60 detik terakhir."),
    "psi.memory.some_avg300": ("%", "gauge", "Same, last 5 min.", "Sama, 5 menit terakhir."),
    "psi.memory.some_total": ("us", "counter", "Total microseconds some task stalled on memory.", "Total mikrodetik ada tugas macet karena memori."),
    "psi.memory.full_avg10": ("%", "gauge", "Share of last 10 s ALL tasks stalled on memory.", "Porsi 10 detik terakhir SEMUA tugas macet karena memori."),
    "psi.memory.full_avg60": ("%", "gauge", "Same, last 60 s.", "Sama, 60 detik terakhir."),
    "psi.memory.full_avg300": ("%", "gauge", "Same, last 5 min.", "Sama, 5 menit terakhir."),
    "psi.memory.full_total": ("us", "counter", "Total microseconds all tasks stalled on memory.", "Total mikrodetik semua tugas macet karena memori."),
    "tcp.established": ("conns", "gauge", "Connections in ESTABLISHED state.", "Koneksi berstatus ESTABLISHED."),
    "tcp.curr_estab": ("conns", "gauge", "Connections currently open (same idea as established).", "Koneksi yang sedang terbuka (mirip established)."),
    "tcp.time_wait": ("conns", "gauge", "Connections in TIME_WAIT (recently closed).", "Koneksi TIME_WAIT (baru ditutup)."),
    "tcp.close_wait": ("conns", "gauge", "Connections the peer closed but the program has not. Many = a program is leaking sockets.", "Koneksi yang ditutup lawan tapi belum oleh program. Banyak = program bocor socket."),
    "tcp.syn_recv": ("conns", "gauge", "Half-open connections (SYN received). Many = SYN flood or overload.", "Koneksi setengah terbuka (SYN diterima). Banyak = SYN flood atau kelebihan beban."),
    "tcp.listen": ("sockets", "gauge", "Sockets waiting for new connections.", "Socket yang menunggu koneksi baru."),
    "tcp.in_segs": ("segs", "counter", "TCP segments received since boot.", "Segmen TCP diterima sejak boot."),
    "tcp.out_segs": ("segs", "counter", "TCP segments sent since boot.", "Segmen TCP dikirim sejak boot."),
    "tcp.retrans_segs": ("segs", "counter", "TCP segments re-sent since boot.", "Segmen TCP dikirim ulang sejak boot."),
    "tcp.in_errs": ("segs", "counter", "TCP segments received with errors.", "Segmen TCP diterima dengan galat."),
    "tcp.out_rsts": ("segs", "counter", "Reset segments sent (we refused or killed connections).", "Segmen reset terkirim (kita menolak/memutus koneksi)."),
    "tcp.estab_resets": ("count", "counter", "Open connections reset.", "Koneksi terbuka yang di-reset."),
    "tcp.attempt_fails": ("count", "counter", "Failed connection attempts.", "Percobaan koneksi yang gagal."),
    "tcp.abort_on_timeout": ("count", "counter", "Connections aborted because the peer stopped answering.", "Koneksi dibatalkan karena lawan berhenti menjawab."),
    "tcp.timeouts": ("count", "counter", "Retransmission timeouts.", "Timeout kirim ulang."),
    "tcp.syn_retrans": ("count", "counter", "SYN packets re-sent (connections slow to start).", "Paket SYN dikirim ulang (koneksi lambat dimulai)."),
    "tcp.listen_overflows": ("count", "counter", "Times the accept queue was full.", "Berapa kali antrean accept penuh."),
    "tcp.listen_drops": ("count", "counter", "Connections dropped because of a full accept queue.", "Koneksi dibuang karena antrean accept penuh."),
    "vmstat.oom_kill": ("count", "counter", "Processes killed because RAM ran out.", "Proses dibunuh karena RAM habis."),
    "vmstat.pgfault": ("count", "counter", "Minor page faults (cheap).", "Page fault minor (murah)."),
    "vmstat.pgmajfault": ("count", "counter", "Major page faults (needed disk).", "Page fault mayor (butuh disk)."),
    "vmstat.pgpgin": ("KB", "counter", "Kilobytes read from disk.", "Kilobyte dibaca dari disk."),
    "vmstat.pgpgout": ("KB", "counter", "Kilobytes written to disk.", "Kilobyte ditulis ke disk."),
    "vmstat.pgscan_direct": ("pages", "counter", "Pages scanned directly by a program waiting for memory.", "Halaman dipindai langsung oleh program yang menunggu memori."),
    "vmstat.pgsteal_direct": ("pages", "counter", "Pages freed that way.", "Halaman dibebaskan dengan cara itu."),
    "vmstat.pswpin": ("pages", "counter", "Pages swapped in.", "Halaman masuk dari swap."),
    "vmstat.pswpout": ("pages", "counter", "Pages swapped out.", "Halaman keluar ke swap."),
    "perf.cycles": ("count", "counter", "CPU cycles executed (system wide).", "Siklus CPU dijalankan (seluruh sistem)."),
    "perf.instructions": ("count", "counter", "CPU instructions executed.", "Instruksi CPU dijalankan."),
    "perf.cache_references": ("count", "counter", "CPU cache lookups.", "Pencarian cache CPU."),
    "perf.cache_misses": ("count", "counter", "Lookups missing the cache (slow RAM access).", "Pencarian yang meleset dari cache (akses RAM lambat)."),
    "perf.branch_instructions": ("count", "counter", "Branch instructions executed.", "Instruksi cabang dijalankan."),
    "perf.branch_misses": ("count", "counter", "Mispredicted branches.", "Cabang yang salah tebak."),
    "perf.context_switches": ("count", "counter", "Context switches seen by perf.", "Context switch menurut perf."),
    "perf.cpu_migrations": ("count", "counter", "Tasks moved between CPU cores.", "Tugas berpindah antar core CPU."),
    "perf.page_faults": ("count", "counter", "Page faults seen by perf.", "Page fault menurut perf."),
    "perf.task_clock": ("ns", "counter", "CPU time consumed by all tasks.", "Waktu CPU yang dipakai semua tugas."),
    "perf.scope": ("text", "info", "Whether perf counts the whole system.", "Apakah perf menghitung seluruh sistem."),
    "proc.cpu_ticks": ("jiffies", "counter", "CPU time of the telemetry agent itself.", "Waktu CPU agen telemetri itu sendiri."),
    "proc.utime": ("jiffies", "counter", "Agent CPU time in user code.", "Waktu CPU agen di kode user."),
    "proc.stime": ("jiffies", "counter", "Agent CPU time in kernel.", "Waktu CPU agen di kernel."),
    "proc.rss": ("bytes", "gauge", "Agent resident memory.", "Memori resident agen."),
    "proc.rss_peak": ("bytes", "gauge", "Agent peak resident memory.", "Puncak memori resident agen."),
    "proc.threads": ("threads", "gauge", "Agent thread count.", "Jumlah thread agen."),
    "proc.disk_read_bytes": ("bytes", "counter", "Bytes the agent read from disk.", "Byte yang dibaca agen dari disk."),
    "proc.disk_write_bytes": ("bytes", "counter", "Bytes the agent wrote to disk.", "Byte yang ditulis agen ke disk."),
    "proc.syscall_read_bytes": ("bytes", "counter", "Bytes the agent read via syscalls.", "Byte yang dibaca agen via syscall."),
    "proc.syscall_write_bytes": ("bytes", "counter", "Bytes the agent wrote via syscalls.", "Byte yang ditulis agen via syscall."),
    "proc.voluntary_ctxt": ("count", "counter", "Agent context switches by choice (waiting).", "Context switch agen karena menunggu."),
    "proc.involuntary_ctxt": ("count", "counter", "Agent context switches forced by the scheduler.", "Context switch agen karena dipaksa scheduler."),
    "proc.minor_faults": ("count", "counter", "Agent minor page faults.", "Page fault minor agen."),
    "proc.major_faults": ("count", "counter", "Agent major page faults.", "Page fault mayor agen."),
    "clk_tck": ("Hz", "info", "Jiffies per second.", "Jiffy per detik."),
    "interval_s": ("s", "info", "Seconds between snapshots.", "Detik antar snapshot."),
    "seq": ("count", "info", "Snapshot sequence number of this host.", "Nomor urut snapshot host ini."),
    "hostname": ("text", "info", "Machine name.", "Nama mesin."),
    "ts_ms": ("ms", "info", "Snapshot time (epoch ms).", "Waktu snapshot (epoch ms)."),
    "ts": ("s", "info", "Snapshot time (epoch s).", "Waktu snapshot (epoch detik)."),
    "available_perf_counters": ("list", "info", "Hardware counters this machine exposes.", "Penghitung perangkat keras yang tersedia di mesin ini."),
}

# ---- stress mood (same thresholds as the FINAL dashboard banner) -------------------------------------------------------
MOODS = [(20, "😎", "Chill", "Santai"), (50, "😊", "Happy", "Senang"), (75, "😐", "Okay", "Biasa"), (90, "😟", "Stressed", "Tertekan"), (101, "😭", "Crying", "Menangis")]


def mood(score):
    for hi, emoji, en, idn in MOODS:
        if score < hi:
            return dict(emoji=emoji, en=en, id=idn)


def flatten(o, prefix=""):
    out = {}
    for k, v in o.items():
        if isinstance(v, dict):
            out.update(flatten(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def derive(prev, cur):
    """Derived metrics for the interval prev -> cur (both flat dicts). None if the interval is unusable (reboot, duplicate)."""
    dt = (cur["ts_ms"] - prev["ts_ms"]) / 1000
    if dt <= 0 or dt > 120:
        return None
    jiff = {k: cur[f"cpu.{k}"] - prev[f"cpu.{k}"] for k in CPU_FIELDS}
    total = sum(jiff.values())
    if total <= 0 or any(v < 0 for v in jiff.values()):
        return None
    d = lambda k: cur[k] - prev[k]
    rate = lambda k: (d(k) / dt) if d(k) >= 0 else None
    hz = cur.get("clk_tck", 100)
    cores = max(1, round(total / (hz * dt)))
    out = {
        "cpu_pct": clamp(100 * (total - jiff["idle"] - jiff["iowait"]) / total),
        "cpu_user_pct": 100 * (jiff["user"] + jiff["nice"]) / total, "cpu_system_pct": 100 * jiff["system"] / total,
        "cpu_iowait_pct": 100 * jiff["iowait"] / total, "cpu_softirq_pct": 100 * (jiff["softirq"] + jiff["irq"]) / total,
        "cores": cores, "load1": cur["load.1m"], "load_per_core": cur["load.1m"] / cores, "procs_running": cur["cpu.procs_running"],
        "mem_used_pct": clamp(100 * (1 - cur["mem.available"] / cur["mem.total"])), "mem_avail_mb": cur["mem.available"] / 1048576,
        "mem_anon_gb": cur["mem.anon"] / 1073741824,
        "psi_cpu": cur["psi.cpu.some_avg10"], "psi_io": cur["psi.io.some_avg10"], "psi_io_full": cur["psi.io.full_avg10"], "psi_mem": cur["psi.memory.some_avg10"],
        "tcp_established": cur["tcp.established"], "tcp_time_wait": cur["tcp.time_wait"],
        "tcp_in_segs_ps": rate("tcp.in_segs"), "tcp_out_segs_ps": rate("tcp.out_segs"),
        "tcp_listen_overflows_ps": rate("tcp.listen_overflows"), "tcp_resets_ps": rate("tcp.estab_resets"),
        "tcp_timeouts_ps": rate("tcp.timeouts"), "tcp_in_errs_ps": rate("tcp.in_errs"),
        "ctx_switch_ps": rate("cpu.context_switches"), "interrupts_ps": rate("cpu.interrupts"), "forks_ps": rate("cpu.forks"),
        "oom_kills": max(0, d("vmstat.oom_kill")), "pgscan_direct_ps": rate("vmstat.pgscan_direct"), "major_faults_ps": rate("vmstat.pgmajfault"),
        "disk_read_kbps": rate("vmstat.pgpgin"), "disk_write_kbps": rate("vmstat.pgpgout"),
    }
    out["tcp_retrans_pct"] = clamp(100 * d("tcp.retrans_segs") / d("tcp.out_segs")) if d("tcp.out_segs") >= 200 and d("tcp.retrans_segs") >= 0 else None  # tiny intervals are noise
    cyc, ins = d("perf.cycles"), d("perf.instructions")
    out["ipc"] = ins / cyc if cyc > 0 and ins >= 0 else None
    refs, miss = d("perf.cache_references"), d("perf.cache_misses")
    out["cache_miss_pct"] = 100 * miss / refs if refs > 0 and miss >= 0 else None
    # stress: worst of cpu, memory, load vs 2x cores, pressure (disk pressure weighted 0.5: a steady disk wait on a healthy box is not dying)
    out["stress"] = max(out["cpu_pct"], out["mem_used_pct"], clamp(out["load_per_core"] * 50), out["psi_cpu"], out["psi_mem"], out["psi_io"] * 0.5)
    return out


NEEDED = ["ts_ms", "clk_tck", "load.1m", "mem.total", "mem.available", "mem.anon", "cpu.procs_running", "cpu.context_switches", "cpu.interrupts", "cpu.forks",
          "psi.cpu.some_avg10", "psi.io.some_avg10", "psi.io.full_avg10", "psi.memory.some_avg10", "tcp.established", "tcp.time_wait", "tcp.in_segs", "tcp.out_segs",
          "tcp.retrans_segs", "tcp.listen_overflows", "tcp.estab_resets", "tcp.timeouts", "tcp.in_errs", "vmstat.oom_kill", "vmstat.pgscan_direct",
          "vmstat.pgmajfault", "vmstat.pgpgin", "vmstat.pgpgout", "perf.cycles", "perf.instructions", "perf.cache_references", "perf.cache_misses"] + [f"cpu.{k}" for k in CPU_FIELDS]


class SystemData:
    """All hosts, derived once. host -> {'t': [ms...], 'm': {metric: [values...]}, 'hostname', 'samples', 'first', 'last'}."""

    def __init__(self, db):
        self.hosts = {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        prev, latest = {}, {}
        for ts, alias, hostname, data in con.execute("SELECT ts_ms, alias, hostname, data FROM system_telemetry ORDER BY alias, ts_ms"):
            flat = flatten(json.loads(data))
            if any(k not in flat for k in NEEDED):
                continue
            cur = {k: flat[k] for k in NEEDED}
            h = self.hosts.setdefault(alias, dict(t=[], m={k: [] for k in list(DERIVED)}, hostname=hostname, samples=0, first=ts, last=ts, raw=None))
            h["samples"] += 1
            h["first"], h["last"], h["raw"] = min(h["first"], ts), max(h["last"], ts), flat
            p = prev.get(alias)
            prev[alias] = cur
            dv = derive(p, cur) if p else None
            if dv is None:
                continue
            h["t"].append(ts)
            for k in DERIVED:
                h["m"][k].append(dv.get(k))
        con.close()

    def series(self, host, metric, bucket_s, agg="mean", lo=None, hi=None):
        h = self.hosts[host]
        size = bucket_s * 1000
        cells = {}
        for ts, v in zip(h["t"], h["m"][metric]):
            if v is None or (lo is not None and ts < lo) or (hi is not None and ts > hi):
                continue
            c = cells.setdefault(ts // size * size, [0.0, 0, v])
            c[0] += v; c[1] += 1; c[2] = max(c[2], v)
        return {t: (c[2] if agg == "max" else (c[0] if metric == "oom_kills" else c[0] / c[1])) for t, c in cells.items()}


# ---- story ---------------------------------------------------------------------------------------------------------------
# (kind, metric, threshold, min_seconds, severity, english template, indonesian template)  {peak} {dur} {avg}
RULES = [
    ("cpu_saturated", "cpu_pct", 90, 60, "high", "CPU was almost fully used (peak {peak:.0f}%, avg {avg:.0f}%).", "CPU hampir penuh terpakai (puncak {peak:.0f}%, rata-rata {avg:.0f}%)."),
    ("memory_tight", "mem_used_pct", 90, 60, "high", "Memory was nearly exhausted (peak {peak:.0f}% used). With no swap, running out means programs get killed.", "Memori hampir habis (puncak {peak:.0f}% terpakai). Tanpa swap, kehabisan berarti program dibunuh."),
    ("cpu_queue", "psi_cpu", 50, 60, "medium", "Tasks were waiting for a CPU more than half of the time (peak {peak:.0f}%).", "Tugas menunggu CPU lebih dari separuh waktu (puncak {peak:.0f}%)."),
    ("disk_stall", "psi_io_full", 50, 60, "medium", "Disk stalled every task for over half of the time (peak {peak:.0f}%).", "Disk membuat semua tugas macet lebih dari separuh waktu (puncak {peak:.0f}%)."),
    ("memory_pressure", "psi_mem", 10, 30, "high", "Tasks were stalling to find memory (peak {peak:.0f}%): RAM was short.", "Tugas macet mencari memori (puncak {peak:.0f}%): RAM kurang."),
    ("connections_dropped", "tcp_listen_overflows_ps", 1, 30, "high", "The server dropped new connections because its queue was full (peak {peak:.0f}/s).", "Server membuang koneksi baru karena antreannya penuh (puncak {peak:.0f}/dtk)."),
    ("retransmits", "tcp_retrans_pct", 5, 60, "medium", "Many packets had to be re-sent (peak {peak:.1f}%): congestion or an overloaded peer.", "Banyak paket harus dikirim ulang (puncak {peak:.1f}%): macet atau lawan kelebihan beban."),
    ("oom", "oom_kills", 1, 0, "critical", "The kernel killed {total:.0f} process(es) because memory ran out.", "Kernel membunuh {total:.0f} proses karena memori habis."),
]
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


def story(sd, host, gap_s=30):
    h = sd.hosts[host]
    events = []
    for kind, metric, thr, min_s, sev, en, idn in RULES:
        vals = h["m"][metric]
        start = last = None
        pk, tot, n, sm = 0.0, 0.0, 0, 0.0

        def close():
            nonlocal start, last, pk, tot, n, sm
            if start is not None and (last - start) / 1000 >= min_s:
                ctx = dict(peak=pk, avg=sm / max(n, 1), dur=(last - start) / 1000, total=tot)
                events.append(dict(host=host, kind=kind, severity=sev, start_ms=start, end_ms=last, duration_s=round((last - start) / 1000 + 5), peak=pk,
                                   text_en=en.format(**ctx), text_id=idn.format(**ctx)))
            start = last = None; pk, tot, n, sm = 0.0, 0.0, 0, 0.0

        for ts, v in zip(h["t"], vals):
            if v is not None and v >= thr:
                if start is not None and (ts - last) / 1000 > gap_s:
                    close()
                if start is None:
                    start = ts
                last = ts; pk = max(pk, v); tot += v; n += 1; sm += v
            elif start is not None and (ts - last) / 1000 > gap_s:
                close()
        close()
    # silent stretches
    for a, b in zip(h["t"], h["t"][1:]):
        if (b - a) / 1000 > gap_s * 2:
            events.append(dict(host=host, kind="offline", severity="info", start_ms=a, end_ms=b, duration_s=round((b - a) / 1000), peak=None,
                               text_en=f"No data for {round((b - a) / 60000, 1)} min (host or its agent was down, or the network dropped).",
                               text_id=f"Tidak ada data selama {round((b - a) / 60000, 1)} menit (host/agen mati atau jaringan putus)."))
    return sorted(events, key=lambda e: e["start_ms"])


def summary(sd, host):
    h = sd.hosts[host]
    pick = lambda m: [v for v in h["m"][m] if v is not None]
    pct = lambda xs, q: sorted(xs)[min(len(xs) - 1, int(len(xs) * q))] if xs else None
    stress = pick("stress")
    out = dict(host=host, hostname=h["hostname"], samples=h["samples"], first_ms=h["first"], last_ms=h["last"], cores=(pct(pick("cores"), 0.5)),
               mem_total_gb=round(h["raw"]["mem.total"] / 1073741824, 1), oom_kills=int(sum(h["m"]["oom_kills"][i] or 0 for i in range(len(h["t"])))),
               swap=h["raw"]["mem.swap_total"] > 0)
    for m in ("cpu_pct", "mem_used_pct", "load_per_core", "psi_cpu", "psi_io", "tcp_established", "tcp_retrans_pct", "stress"):
        xs = pick(m)
        out[m] = dict(avg=sum(xs) / len(xs) if xs else None, p95=pct(xs, 0.95), max=max(xs) if xs else None, now=xs[-1] if xs else None)
    out["overflows_total"] = int(sum((v or 0) * 5 for v in h["m"]["tcp_listen_overflows_ps"]))
    shares = {}
    for s in stress:
        shares[mood(s)["emoji"]] = shares.get(mood(s)["emoji"], 0) + 1
    out["mood_share"] = {k: round(100 * v / len(stress), 1) for k, v in shares.items()} if stress else {}
    out["mood_now"] = mood(out["stress"]["now"]) if stress else None
    return out


def snapshot(sd, host):
    """Latest raw values of a host, each with its translation."""
    raw = sd.hosts[host]["raw"]
    rows = []
    for k, v in raw.items():
        unit, kind, en, idn = RAW.get(k, ("", "info", "(no description yet)", "(belum ada deskripsi)"))
        rows.append(dict(key=k, value=v, unit=unit, kind=kind, en=en, id=idn))
    return sorted(rows, key=lambda r: r["key"])

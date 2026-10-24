# Clean reinstall of dao-smart-nic on CN10K DPU (xeon3-oct-112)

Run on 2026-10-07 as user dao-test. Board: DPU xeon3-oct-112 (CN10KA, aarch64, Ubuntu 22.04). Software: DAO 26.10.0 (branch dao-devel, commit 74ae818), DPDK 25.11.0. Outputs below are from the real run; long ones are abridged and marked.

## 1. Goal and result

Goal: build dao-smart-nic from a clean source copy, install it into its own prefix, launch it on 0002:03:00.0 plus nine SDP VFs, verify it, and put the board back as it was.

Result: passed. The app launched with no errors, linked `0002:03:00.0 <--> 0002:01:00.1`, answered on telnet, and the board was restored (PCI drivers identical to the saved before-state, no stale hugepage files).

## 2. Ground rules

- The board is shared and VPP work must not be interrupted. SmartNIC uses 0002:03:00.0, nine SDP VFs (0002:01:00.1 to 0002:01:01.1) and cores 0-3. VPP uses 0002:02:00.0 (Parts A/B), 0002:04:00.0 (Part C) and cores 4-6.
- Every state-changing step is guarded: it does nothing if a vpp process is running.
- Never touched: \~/dao and its install/, /usr/local/bin/dao-smart-nic, the apt packages, the VPP devices.
- Run the launch in the foreground in terminal 2 and use terminal 1 for checks.

## 3. Board state before the run

- dao-cn10k apt package: not installed. Installed: cpt-firmware-cn10k 24.09.0, dpdk 21.11.9, dpdk-23.11-cn10k 24.08.0, oct-ep-target-cn10k 26.02.0, openssl-engine-1.0.0-cn10k 24.09.2. libdpdk 25.11.0 (pkg-config) belongs to no dpkg package.
- Existing binaries: /usr/local/bin/dao-smart-nic (1.4 MB, Aug 14, root) and \~/dao/install/bin/dao-smart-nic (31 MB, Aug 29). They differ.
- The board rebooted on 2026-10-06 at 11:51:27. After the reboot nothing was bound to vfio-pci, hugepages were 0 in all sizes, and no dao or vpp process was running (only nginx and sshd).
- \~/dao is dirty (local edits and untracked files, see Step 3) and shared with other work, so it was not used for the clean build.
- Tools: meson 1.12.0, ninja 1.10.1, gcc 11.4.0, ccache, pkg-config, aarch64-linux-gnu-gcc.
- RAM 23 GB (14 GB free), hugepage size 512 MB, hugetlbfs mounted on /dev/hugepages.

## 4. Procedure

### Step 1: Inventory (read-only)

Run on: DPU

```
dpkg -l | grep -Ei 'dao|dpdk|oct-ep|cpt-firmware'
ls /etc/apt/sources.list.d/
for p in $(pgrep -f dao-smart-nic); do echo "PID $p"; tr '\0' ' ' </proc/$p/cmdline; echo; readlink /proc/$p/cwd; done
find / -name 'dao-smart-nic' -type f 2>/dev/null
dpdk-devbind.py -s | grep -Ei 'a0f7|a064|vfio'
grep -i huge /proc/meminfo
```

Output (abridged):

```
ii  cpt-firmware-cn10k                24.09.0   arm64   Cryptographic Accelerator Firmware for Marvell Octeon 10
ii  dpdk                              21.11.9-0ubuntu0.22.04.2   arm64   Data Plane Development Kit (runtime)
ii  dpdk-23.11-cn10k                  24.08.0   arm64   DPDK Library for Marvell Octeon 10
ii  oct-ep-target-cn10k               26.02.0   arm64   Octeon-ep agent for Marvell Octeon 10
ii  openssl-engine-1.0.0-cn10k        24.09.2   arm64   DPDK based OpenSSL Engine for Marvell Octeon 10
dao.list  dao.list.v2404.disabled  docker.list  ubuntu-toolchain-r-ubuntu-test-jammy.list
/usr/local/bin/dao-smart-nic
/home/dao-test/dao/install/bin/dao-smart-nic
0002:01:00.0 'Device a065' if= drv=rvu_af unused=vfio-pci
0002:01:00.1 'Device a0f7' if=enP2p1s0v0 drv=rvu_nicvf unused=vfio-pci
... (0002:01:00.2 to 0002:01:01.1: nine SDP VFs a0f7 in total, all drv=rvu_nicvf)
0002:02:00.0 'Device a063' if=enP2p2s0 drv=rvu_nicpf unused=vfio-pci
0002:03:00.0 'Device a063' if=enP2p3s0 drv=rvu_nicpf unused=vfio-pci
0002:04:00.0 'Device a063' if=enP2p4s0 drv=rvu_nicpf unused=vfio-pci
... (other devices omitted)
HugePages_Total:       0
HugePages_Free:        0
Hugepagesize:     524288 kB
```

Verify:

- No `PID ...` block is printed, so dao-smart-nic is not running.
- `dao-cn10k` is absent from the dpkg list, so the existing binaries come from source builds.
- No device shows `drv=vfio-pci` and HugePages\_Total is 0.

### Step 2: Save restore info and check the source tree

Run on: DPU. Only creates \~/smartnic-restore.

```
uptime -s
pgrep -af 'vpp|dao-|nginx|ovs-vswitchd' | grep -v pgrep
mkdir -p ~/smartnic-restore
ls -l /usr/local/bin/dao-smart-nic ~/dao/install/bin/dao-smart-nic
md5sum /usr/local/bin/dao-smart-nic ~/dao/install/bin/dao-smart-nic
cd ~/dao && git branch --show-current && git log -1 --oneline && git status -s | head
ls ~/dao/app/smart-nic/
cp -a ~/dao/app/smart-nic/*.cli ~/smartnic-restore/ && ls ~/smartnic-restore
ls /sys/kernel/mm/hugepages/
cat /sys/kernel/mm/hugepages/hugepages-*/nr_hugepages
```

Output (abridged):

```
2026-10-06 11:51:27
731 nginx: master process /usr/local/nginx/sbin/nginx -p /usr/local/nginx/ -c /opt/async_nginx.conf
14293 sshd: dao-test [priv]
14314 sshd: dao-test@pts/0
-rwxr-xr-x 1 dao-test dao-test 31074832 Aug 29 06:30 /home/dao-test/dao/install/bin/dao-smart-nic
-rwxr-xr-x 1 root     root      1434416 Aug 14 09:20 /usr/local/bin/dao-smart-nic
da31db281b80967b2b2fb838a355c385  /usr/local/bin/dao-smart-nic
e91d8bd915789cbf42f362493748994d  /home/dao-test/dao/install/bin/dao-smart-nic
dao-devel
74ae818 (HEAD -> dao-devel, ... origin/dao-devel, origin/HEAD) lib/pts: fix mseg free marking
 M app/secgw-graph/cli/secgw_cli.c
 M app/secgw-graph/main.c
 M config/arm64_cn10k_linux_gcc
 M lib/flow/meson.build
?? app/secgw-graph/cli/secgw_cli.c.pre-mac-debug
... (head shows 10 lines; there are 11 entries in total, see Step 3)
cli  include  init  meson.build  node  smart-nic.cli  smart-nic-crb106.cli
smart-nic.cli  smart-nic-crb106.cli
hugepages-16777216kB  hugepages-2048kB  hugepages-524288kB
0
0
0
```

Verify:

- Only nginx and sshd are running (no vpp, no dao app).
- Both .cli files are copied into \~/smartnic-restore.
- nr\_hugepages is 0 for all three sizes.

### Step 3: Review the local edits and build tools (read-only)

Run on: DPU. `git diff` opens a pager and blocks the rest of the paste, so set GIT\_PAGER first.

```
export GIT_PAGER=cat PAGER=cat
cd ~/dao
git status -s | wc -l
git status -s lib/flow
ls -l lib/flow/flow_cpt_em.c lib/flow/flow_cpt_em_stub.c
git diff config/arm64_cn10k_linux_gcc lib/flow/meson.build
cat app/smart-nic/smart-nic-crb106.cli
which meson ninja gcc ccache pkg-config aarch64-linux-gnu-gcc
meson --version
pkg-config --modversion libdpdk
meson configure build 2>/dev/null | grep -E '^\s+(prefix|buildtype|prefer_static_build)\s'
```

Output (abridged):

```
11
 M lib/flow/meson.build
?? lib/flow/flow_cpt_em_stub.c
-rw-rw-r-- 1 dao-test dao-test 45037 Jul 13 16:28 lib/flow/flow_cpt_em.c
-rw-rw-r-- 1 dao-test dao-test   459 Jul 14 11:47 lib/flow/flow_cpt_em_stub.c
/home/dao-test/.local/bin/meson
/usr/bin/ninja
/usr/bin/gcc
/usr/bin/ccache
/usr/bin/pkg-config
/usr/bin/aarch64-linux-gnu-gcc
1.12.0
25.11.0
  buildtype             release
  prefix                /home/dao-test/dao/install
  prefer_static_build   true
```

What the git diff showed:

- config/arm64\_cn10k\_linux\_gcc: aarch64-marvell-linux-gnu-{gcc,g++,gcc-ar,strip} replaced by aarch64-linux-gnu-\* (native tools).
- lib/flow/meson.build: builds flow\_cpt\_em\_stub.c (untracked, 459 bytes) instead of flow\_cpt\_em.c (45 KB).

The board-specific CLI (abridged, the original uses 0002:04:00.0):

```
; Board-specific configuration for crb106-pcie (cn10ka_b0)
mempool mempool0 size 8192 buffers 4000 cache 256 numa 0
ethdev 0002:04:00.0 rxq 1 txq 8 mempool0
ethdev 0002:04:00.0 mtu 1700
ethdev 0002:04:00.0 promiscuous on
ethdev 0002:01:00.1 rxq 1 txq 8 mempool0
ethdev 0002:01:00.1 mtu 1700
ethdev 0002:01:00.1 promiscuous on
... same three lines for 0002:01:00.2 to 0002:01:01.1 (nine SDP VFs in total)
graph config coremask 0xf bsz 32 tmo 10 model default pcap_enable 1 num_pcap_pkts 100000 pcap_file /tmp/output.pcap
lcore map port 0002:04:00.0 queue 0 core 1
lcore map port 0002:01:00.2 queue 0 core 2
lcore map port 0002:01:00.1 queue 0 core 3
graph start
pipeline port add 0002:04:00.0
pipeline port add 0002:01:00.2
pipeline port add 0002:01:00.1
pipeline port link 0002:04:00.0 0002:01:00.1
```

Verify: the existing build was configured with the options in Step 4 and is a native build, so the edited cross-file is not needed for a native build.

### Step 4: Make a clean local clone

Run on: DPU. Local clone, no network needed, \~/dao is not modified.

```
grep -m2 -E 'Command line|Build Options' ~/dao/build/meson-logs/meson-log.txt
git clone --branch dao-devel ~/dao ~/dao-clean
cd ~/dao-clean
git log -1 --oneline
git status -s | wc -l
cp -a ~/smartnic-restore/smart-nic-crb106.cli app/smart-nic/
ls app/smart-nic
```

Output (abridged):

```
Build Options: -Denable_apps=secgw-graph -Dplatform=cn10k -Dprefix=/home/dao-test/dao/install -Denable_kmods=false -Dprefer_static_build=true -Dprefer_static=true
Command line: `cc .../testfile.c -o .../output.obj -c ... --print-search-dirs` -> 0
Cloning into '/home/dao-test/dao-clean'...
done.
74ae818 (HEAD -> dao-devel, ... origin/dao-devel, origin/HEAD) lib/pts: fix mseg free marking
0
cli  include  init  meson.build  node  smart-nic.cli  smart-nic-crb106.cli
```

Verify: the clone is at 74ae818, `git status -s | wc -l` prints 0 (before the cp), and smart-nic-crb106.cli is present.

### Step 5: Configure the build

Run on: DPU. Writes only to \~/dao-clean/build.

```
grep -m3 -E 'Build type|C compiler for the (build|host) machine' ~/dao/build/meson-logs/meson-log.txt
pkg-config --variable=pcfiledir libdpdk
cd ~/dao-clean
meson setup build -Denable_apps=smart-nic -Dplatform=cn10k -Dprefix=/home/dao-test/dao-clean/install -Denable_kmods=false -Dprefer_static_build=true -Dprefer_static=true 2>&1 | tail -30
```

Output (abridged):

```
Build type: native build
C compiler for the host machine: /usr/bin/ccache cc (gcc 11.4.0 "cc (Ubuntu 11.4.0-1ubuntu1~22.04.3) 11.4.0")
C compiler for the build machine: /usr/bin/ccache cc (gcc 11.4.0 "cc (Ubuntu 11.4.0-1ubuntu1~22.04.3) 11.4.0")
/usr/lib/aarch64-linux-gnu/pkgconfig
Applications:
        smart-nic,
... (Disabled Modules: card-mgr, crypto-agent, ovs-offload, secgw-graph, virtio-crypto, virtio-l2fwd, virtio-blkio, rdma_graph, and four libraries)
Build targets in project: 30
Data Accelerator Offload 26.10.0
  User defined options
    enable_apps        : smart-nic
    enable_kmods       : false
    platform           : cn10k
    prefer_static      : true
    prefer_static_build: true
    prefix             : /home/dao-test/dao-clean/install
Found ninja-1.10.1 at /usr/bin/ninja
```

Verify: no ERROR line, smart-nic is the only enabled application, and the prefix is the clean one. A meson warning about meson\_version >= 0.61.0 (Wrap files with diff\_files) is harmless.

### Step 6: Compile

Run on: DPU. `-j 8` keeps the load modest on the shared board.

```
dpkg -S /usr/lib/aarch64-linux-gnu/pkgconfig/libdpdk.pc
cd ~/dao-clean
time ninja -C build -j 8 2>&1 | tail -30
find build -name dao-smart-nic -type f -exec ls -l {} \;
```

Output (abridged):

```
dpkg-query: no path found matching pattern /usr/lib/aarch64-linux-gnu/pkgconfig/libdpdk.pc
[107/136] Compiling C object app/dao-smart-nic.p/smart-nic_cli_smc_graph_cli.c.o
...
[115/136] Compiling C object lib/libdao_flow.a.p/flow_flow_cpt_em.c.o
[116/136] Linking static target lib/libdao_flow.a
...
[124/136] Linking target app/dao-smart-nic
...
[136/136] Generating doc/guides/End User Documentation with a custom command

real    0m12.014s
user    0m22.489s
sys     0m1.349s
-rwxrwxr-x 1 dao-test dao-test 31134816 Oct  7 05:29 build/app/dao-smart-nic
```

Verify: ninja reaches \[136/136\] with no error, and the binary exists (31134816 bytes). The real lib/flow/flow\_cpt\_em.c compiled, so the stub was not needed.

### Step 7: Install into the clean prefix and verify the binary

Run on: DPU. Writes only to \~/dao-clean/install.

```
cd ~/dao-clean
ninja -C build install 2>&1 | tail -5
ls -l ~/dao-clean/install/bin/dao-smart-nic
file ~/dao-clean/install/bin/dao-smart-nic
ldd ~/dao-clean/install/bin/dao-smart-nic
md5sum ~/dao-clean/install/bin/dao-smart-nic ~/dao/install/bin/dao-smart-nic
```

Output (abridged):

```
Installing /home/dao-test/dao-clean/build/lib/common/dao_config.h to /home/dao-test/dao-clean/install/.
-rwxr-xr-x 1 dao-test dao-test 31134816 Oct  7 05:29 /home/dao-test/dao-clean/install/bin/dao-smart-nic
.../dao-smart-nic: ELF 64-bit LSB pie executable, ARM aarch64, version 1 (SYSV), dynamically linked, interpreter /lib/ld-linux-aarch64.so.1, ... with debug_info, not stripped
        linux-vdso.so.1
        libm.so.6 => /lib/aarch64-linux-gnu/libm.so.6
        libnl-3.so.200 => /lib/aarch64-linux-gnu/libnl-3.so.200
        libnl-route-3.so.200 => /lib/aarch64-linux-gnu/libnl-route-3.so.200
        libnl-xfrm-3.so.200 => /lib/aarch64-linux-gnu/libnl-xfrm-3.so.200
        libc.so.6 => /lib/aarch64-linux-gnu/libc.so.6
        /lib/ld-linux-aarch64.so.1
14ee288f152335b5bc9d0a10c3835ec6  /home/dao-test/dao-clean/install/bin/dao-smart-nic
e91d8bd915789cbf42f362493748994d  /home/dao-test/dao/install/bin/dao-smart-nic
```

Verify: aarch64 ELF, only libm, libnl and libc are linked dynamically (DPDK and DAO are static), and the md5 differs from the old binary.

### Step 8: Pre-launch checks (read-only)

Run on: DPU

```
mount | grep -i huge
cat /sys/bus/pci/devices/0002:04:00.0/sriov_numvfs /sys/bus/pci/devices/0002:04:00.0/sriov_totalvfs
for d in 0002:04:00.0 0002:01:00.1 0002:01:00.2; do echo $d $(basename $(readlink /sys/bus/pci/devices/$d/driver)) group $(basename $(readlink /sys/bus/pci/devices/$d/iommu_group)); done
ip -br addr show enP2p4s0
ip route | head -3
pgrep -af 'vpp|dao-|ovs-vswitchd|qemu' | grep -v pgrep
systemctl is-active nginx-tls.service
```

Output:

```
hugetlbfs on /dev/hugepages type hugetlbfs (rw,relatime,pagesize=512M)
0
3
0002:04:00.0 rvu_nicpf group 10
0002:01:00.1 rvu_nicvf group 13
0002:01:00.2 rvu_nicvf group 14
enP2p4s0         DOWN
default via 10.68.0.1 dev eth5 proto dhcp src 10.68.0.190 metric 1024
8.8.4.4 via 10.68.0.1 dev eth5 proto dhcp src 10.68.0.190 metric 1024
8.8.8.8 via 10.68.0.1 dev eth5 proto dhcp src 10.68.0.190 metric 1024
14293 sshd: dao-test [priv]
14314 sshd: dao-test@pts/0
active
```

Verify: hugetlbfs is mounted with 512M pages, the default route and SSH go via eth5 (not a data port), and no app is running. The pgrep lines are only sshd, matched because of the dao- in the user name.

### Step 9: Choose the port and create the CLI copy

The original crb106 CLI uses 0002:04:00.0, which VPP Part C also uses. This run used 0002:03:00.0 instead.

Run on: DPU

```
ls /dev/vfio
sudo fuser -v /dev/vfio/* 2>&1 | head
ip -br link show | grep -E 'enP2p[2-6]s0'
for d in 0002:03:00.0 0002:05:00.0 0002:06:00.0; do echo $d $(basename $(readlink /sys/bus/pci/devices/$d/driver)) group $(basename $(readlink /sys/bus/pci/devices/$d/iommu_group)); done
sed 's/0002:04:00.0/0002:03:00.0/g' ~/dao-clean/app/smart-nic/smart-nic-crb106.cli > ~/dao-clean/app/smart-nic/smart-nic-crb106-p03.cli
diff ~/dao-clean/app/smart-nic/smart-nic-crb106.cli ~/dao-clean/app/smart-nic/smart-nic-crb106-p03.cli
```

Output:

```
vfio
enP2p2s0         UP             40:60:5a:02:e6:bb <BROADCAST,MULTICAST,UP,LOWER_UP>
enP2p3s0         DOWN           40:60:5a:02:e6:bc <NO-CARRIER,BROADCAST,MULTICAST,UP>
enP2p4s0         DOWN           40:60:5a:02:e6:bd <NO-CARRIER,BROADCAST,MULTICAST,UP>
enP2p5s0         DOWN           40:60:5a:02:e6:be <NO-CARRIER,BROADCAST,MULTICAST,UP>
enP2p6s0         DOWN           6a:96:fb:63:ee:6f <NO-CARRIER,BROADCAST,MULTICAST,UP>
0002:03:00.0 rvu_nicpf group 9
0002:05:00.0 rvu_nicpf group 11
0002:06:00.0 rvu_nicpf group 12
10,12c10,12
< ethdev 0002:04:00.0 rxq 1 txq 8 mempool0
< ethdev 0002:04:00.0 mtu 1700
< ethdev 0002:04:00.0 promiscuous on
---
> ethdev 0002:03:00.0 rxq 1 txq 8 mempool0
> ethdev 0002:03:00.0 mtu 1700
> ethdev 0002:03:00.0 promiscuous on
42c42
< lcore map port 0002:04:00.0 queue 0 core 1
---
> lcore map port 0002:03:00.0 queue 0 core 1
46c46
< pipeline port add 0002:04:00.0
---
> pipeline port add 0002:03:00.0
49c49
< pipeline port link 0002:04:00.0 0002:01:00.1
---
> pipeline port link 0002:03:00.0 0002:01:00.1
```

Verify: /dev/vfio contains only `vfio` and fuser prints nothing, so no one holds VFIO. The diff shows exactly 6 changed lines, all 0002:04:00.0 to 0002:03:00.0, and the original file is untouched. Link note: after the reboot only enP2p2s0 had carrier, so 0002:03:00.0 will report link down.

### Step 10: Allocate hugepages and bind the devices (guarded)

Changes shared board state. The block does nothing if VPP is running. The run first set 4 pages and then raised it to 6 for VPP headroom, so use 6 directly.

Run on: DPU

```
echo 6 | sudo tee /sys/kernel/mm/hugepages/hugepages-524288kB/nr_hugepages
if pgrep -x vpp >/dev/null; then echo "VPP RUNNING - STOPPED, nothing bound"; else
  dpdk-devbind.py -s | grep -E '0002:03:00.0|0002:01:0[01]\.' > ~/smartnic-restore/pci-before.txt; cat ~/smartnic-restore/pci-before.txt
  sudo dpdk-devbind.py -b vfio-pci 0002:03:00.0 0002:01:00.1 0002:01:00.2 0002:01:00.3 0002:01:00.4 0002:01:00.5 0002:01:00.6 0002:01:00.7 0002:01:01.0 0002:01:01.1
  dpdk-devbind.py -s | grep -E '0002:03:00.0|0002:01:0[01]\.'
fi
grep -E 'HugePages_(Total|Free)' /proc/meminfo
ls /dev/vfio
ip -br addr show eth5
```

Output (abridged):

```
6
0002:01:00.0 'Device a065' if= drv=rvu_af unused=vfio-pci
0002:01:00.1 'Device a0f7' if=enP2p1s0v0 drv=rvu_nicvf unused=vfio-pci
... (0002:01:00.2 to 0002:01:01.1: rvu_nicvf, enP2p1s0v1 to v8)
0002:03:00.0 'Device a063' if=enP2p3s0 drv=rvu_nicpf unused=vfio-pci
0002:01:00.1 'Device a0f7' drv=vfio-pci unused=
... (nine SDP VFs, all drv=vfio-pci)
0002:03:00.0 'Device a063' drv=vfio-pci unused=
0002:01:00.0 'Device a065' if= drv=rvu_af unused=vfio-pci
HugePages_Total:       6
HugePages_Free:        6
13  14  15  16  17  18  19  20  21  9  vfio
eth5             UP             10.68.0.190/22 metric 1024 ...
```

Verify: ten devices show drv=vfio-pci, 0002:01:00.0 stays on rvu\_af (it is the SDP PF, not bound), /dev/vfio shows groups 9 and 13 to 21, HugePages\_Free is 6, and eth5 is still up (SSH is unaffected).

### Step 11: Launch dao-smart-nic

Run on: DPU, terminal 2 (stays in the foreground). `--file-prefix=smartnic` keeps its hugepage files separate from VPP's.

```
pgrep -x vpp >/dev/null && echo "VPP RUNNING - DO NOT LAUNCH" || sudo ~/dao-clean/install/bin/dao-smart-nic -c 0xf --file-prefix=smartnic --huge-dir=/dev/hugepages -a 0002:03:00.0 -a 0002:01:00.1 -a 0002:01:00.2 -a 0002:01:00.3 -a 0002:01:00.4 -a 0002:01:00.5 -a 0002:01:00.6 -a 0002:01:00.7 -a 0002:01:01.0 -a 0002:01:01.1 -- -s /home/dao-test/dao-clean/app/smart-nic/smart-nic-crb106-p03.cli -h 127.0.0.1 -p 50000 --enable-graph-stats
```

Output (abridged):

```
[lcore -1] DAO_INFO: smart-nic application version 26.10.0-25.11.0-74ae818
EAL: Detected CPU lcores: 24
EAL: Detected NUMA nodes: 1
EAL: '-c <coremask>' option is deprecated, and will be removed in a future release
EAL: Detected static linkage of DPDK
EAL: Multi-process socket /var/run/dpdk/smartnic/mp_socket
EAL: Selected IOVA mode 'VA'
EAL: VFIO support initialized
EAL: Using IOMMU type 1 (Type 1)
CNXK: RoC Model: cn10ka_b0 (HW_PLATFORM)
[lcore  0] DAO_WARNING: WARNING! Telnet session can not be accessed with--enable-graph-stats
Port_id = 9 srcmac = 40:60:5a:2:e6:bc
Port_id = 0 srcmac = ae:45:47:61:9b:9f
... (ports 1 to 8, one line each)
[lcore  0] DAO_INFO: Rx node smc_eth_rx-0: is at 19
[lcore  0] DAO_INFO: Tx node smc_eth_tx-0: is at 20
... (nodes 1 to 9 follow)
GRAPH: graph_pcap_file_open():108 pcap filename: /tmp/output.pcap_bqtdTm.pcapng
Launching worker loops....
[lcore  1] DAO_INFO: Lcore 1 port 9 queue 0
[lcore  2] DAO_INFO: Lcore 2 port 1 queue 0
[lcore  3] DAO_INFO: Lcore 3 port 0 queue 0
[lcore  0] DAO_INFO: Starting port 9...
Port 9 Link down
[lcore  0] DAO_INFO: Starting port 1...
Port 1 Link up at 100 Gbps FDX Fixed None
[lcore  0] DAO_INFO: Starting port 0...
Port 0 Link up at 100 Gbps FDX Fixed None
[lcore  0] DAO_INFO: Ports 0002:03:00.0 <--> 0002:01:00.1 linked successfully
(then an empty graph stats table repeats every few seconds)
```

Verification in terminal 1:

```
pgrep -af dao-smart-nic | grep -v pgrep
grep -E 'HugePages_(Total|Free)' /proc/meminfo
telnet 127.0.0.1 50000
```

```
15415 sudo /home/dao-test/dao-clean/install/bin/dao-smart-nic -c 0xf --file-prefix=smartnic ...
15417 sudo /home/dao-test/dao-clean/install/bin/dao-smart-nic -c 0xf --file-prefix=smartnic ...
15418 /home/dao-test/dao-clean/install/bin/dao-smart-nic -c 0xf --file-prefix=smartnic ...
HugePages_Total:       6
HugePages_Free:        5
Trying 127.0.0.1...
Connected to 127.0.0.1.
Escape character is '^]'.

Welcome!

smart-nic>
```

Verify: no ERROR lines, worker loops launched on lcores 1-3, port 9 (0002:03:00.0) is link down (no carrier, expected), ports 0 and 1 are up at 100 Gbps, the pipeline link message appears, SmartNIC uses 1 hugepage (Free 6 to 5), and telnet gives the smart-nic> prompt.

### Step 12: Verify inside telnet

Run on: DPU, terminal 1, at the smart-nic> prompt. Leave telnet with Ctrl+\] then `quit`.

```
ethdev 0002:03:00.0 show
ethdev 0002:03:00.0 stats
graph stats show
```

Output:

```
0002:03:00.0: flags=<DOWN> mtu 1700
        ether 40:60:5A:02:E6:BC rxqueues 1 txqueues 8
        port# 9  speed None
        RX packets 0  bytes 0
        RX errors 0  missed 0  no-mbuf 0
        TX packets 0  bytes 0
        TX errors 0

  ######################## NIC statistics for port 9  ########################
  RX-packets: 0          RX-missed: 0          RX-bytes:  0
  RX-errors: 0
  RX-nombuf:  0
  TX-packets: 0          TX-errors: 0          TX-bytes:  0
  ...

+-------------------------------+---------------+---------------+ ...
|Node                           |calls          |objs           | ...
(empty table)
```

Verify: MTU 1700 and 1 rx / 8 tx queues match the CLI, the MAC matches enP2p3s0 (40:60:5a:02:e6:bc), all counters are 0 (no traffic, no carrier), and `graph stats show` is empty, which is consistent with the app's warning about --enable-graph-stats.

### Step 13: Stop SmartNIC and restore the board

In terminal 1 press Ctrl+\] and type `quit`. In terminal 2 press Ctrl+C. If the process does not exit, run `sudo pkill -x dao-smart-nic`.

Run on: DPU, terminal 1

```
pgrep -x dao-smart-nic || echo "stopped"
sudo dpdk-devbind.py -b rvu_nicpf 0002:03:00.0
sudo dpdk-devbind.py -b rvu_nicvf 0002:01:00.1 0002:01:00.2 0002:01:00.3 0002:01:00.4 0002:01:00.5 0002:01:00.6 0002:01:00.7 0002:01:01.0 0002:01:01.1
dpdk-devbind.py -s | grep -E '0002:03:00.0|0002:01:0[01]\.' > /tmp/pci-after.txt; diff ~/smartnic-restore/pci-before.txt /tmp/pci-after.txt && echo "restored: identical"
sudo rm -f /tmp/output.pcap_*
ls /dev/vfio /dev/hugepages /var/run/dpdk
grep -E 'HugePages_(Total|Free)' /proc/meminfo
ip -br addr show eth5
```

Output:

```
stopped
restored: identical
/dev/hugepages:
smartnicmap_0

/dev/vfio:
vfio
ls: cannot open directory '/var/run/dpdk': Permission denied
HugePages_Total:       6
HugePages_Free:        5
eth5             UP             10.68.0.190/22 metric 1024 ...
```

Verify: the process is gone, the PCI state diff is empty, /dev/vfio holds only `vfio`, and eth5 is up. Two leftovers remain (Step 14): the stale hugepage file smartnicmap\_0 (Free is 5, not 6) and /var/run/dpdk, which needs sudo to list.

### Step 14: Remove SmartNIC's stale files

Run on: DPU

```
sudo fuser -v /dev/hugepages/smartnicmap_0 2>&1
sudo ls /var/run/dpdk
sudo rm -f /dev/hugepages/smartnicmap_0
sudo rm -rf /var/run/dpdk/smartnic
sudo ls /var/run/dpdk /dev/hugepages
grep -E 'HugePages_(Total|Free)' /proc/meminfo
```

Output:

```
e_cpt_engine748  rte  smartnic
/dev/hugepages:

/var/run/dpdk:
e_cpt_engine748  rte
HugePages_Total:       6
HugePages_Free:        6
```

Verify: fuser printed nothing (no holder), /dev/hugepages is empty, only the smartnic folder was removed (e\_cpt\_engine748 and rte belong to other processes and stay), and HugePages\_Free is back to 6.

## 5. Final board state

- SmartNIC is stopped; 0002:03:00.0 is on rvu\_nicpf and the nine SDP VFs are on rvu\_nicvf, identical to the saved before-state.
- Hugepages: 6 x 512 MB, all free (they were 0 after the reboot; left at 6 so VPP can start).
- Untouched: \~/dao, /usr/local/bin/dao-smart-nic, apt packages, VPP devices 0002:02:00.0 and 0002:04:00.0, cores 4-6.
- Left behind: \~/dao-clean (source, build, install), \~/smartnic-restore (pci-before.txt and CLI copies), /tmp/pci-after.txt.

## 6. Findings and gotchas

- `git diff` opens a pager and swallows the rest of a pasted block. Run `export GIT_PAGER=cat PAGER=cat` first. Typing `q` at the shell prints `command not found`.
- /var/run/dpdk needs sudo to list.
- The app warns that telnet cannot be accessed with --enable-graph-stats, yet telnet connected and answered. `graph stats show` returned an empty table.
- EAL prints that `-c <coremask>` is deprecated (use `-l`). It still works.
- A DPDK process leaves /dev/hugepages/\<prefix>map\_0 behind after exit. It pinned 1 page until removed, so always check /dev/hugepages after stopping.
- The local edits in \~/dao (native toolchain in config/arm64\_cn10k\_linux\_gcc and the flow\_cpt\_em\_stub.c swap) were not needed for the smart-nic build: a native build of the committed code, with the real flow\_cpt\_em.c, compiled cleanly.
- The pgrep patterns containing `dao-` also match sshd (the user is dao-test). Harmless.
- Existing \~/dao/build was configured with enable\_apps=secgw-graph only. The clean build enables only smart-nic.

## 7. Differences from the DAO guide (Universal SmartNIC Turnkey App)

- The guide's example binds SDP VFs at 0002:1f:00.1 and 0002:1f:00.2 and creates RPM VFs (device a064) with `echo 2 > .../0002:02:00.0/sriov_numvfs`. On this board the SDP VFs (a0f7) are 0002:01:00.1 to 0002:01:01.1 and already exist, and the Ethernet side is an RPM PF bound directly to vfio-pci with no RPM VFs (sriov\_numvfs stays 0).
- The guide launches with `-c 0xf -a ... -s ./app/smart-nic/smart-nic.cli -h 127.0.0.1 -p 50000 --enable-graph-stats`. This run adds `--file-prefix=smartnic --huge-dir=/dev/hugepages` and uses the board-specific CLI.
- DAO apt packages (dao-cn10k) were not used; the build is from source.

## 8. Open items

- The unmodified crb106 CLI uses 0002:04:00.0, which VPP Part C also uses. SmartNIC and VPP Part C cannot run at the same time with it. The p03 copy avoids the clash.
- 0002:03:00.0 had no carrier, so no traffic test was done. After the reboot only enP2p2s0 (0002:02:00.0) had carrier and 0002:04:00.0 had none, although it linked at 25G before the reboot. Recheck cabling before VPP Part C.
- /usr/local/bin/dao-smart-nic (Aug 14) was not replaced. Decide whether to install the clean binary there.
- VPP baseline pool numbers changed: 512 MB pages free is now 6 (was 23 before the reboot).
- To rerun SmartNIC later: repeat Steps 10, 11, 13 and 14 (the binary and CLI copy already exist).

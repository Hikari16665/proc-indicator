/*
 * proc-events-helper — 极简的 netlink proc connector 读取器
 *
 * 订阅内核 PROC_EVENT_FORK / EXEC / EXIT 事件，每个事件输出一行 JSON 到 stdout，
 * 由 proc-indicator（Python/GTK4）读取。只需要 CAP_NET_ADMIN（用于订阅），
 * 订阅成功后立即丢弃所有 capability，之后只做只读的 /proc 读取。
 *
 * 编译:  gcc -O2 -Wall -o proc-events-helper proc-events-helper.c
 * 授权:  sudo setcap cap_net_admin+ep proc-events-helper
 *
 * 输出示例:
 *   {"e":"ready"}
 *   {"e":"fork","pid":123,"ppid":100,"comm":"bash","k":0,"t":12.345}
 *   {"e":"exec","pid":123,"ppid":100,"comm":"ls","exe":"/usr/bin/ls","cmd":"ls -la","t":12.346}
 *   {"e":"exit","pid":123,"comm":"ls","code":0,"sig":0,"start":1234567,"k":0,"t":12.350}
 *   {"e":"overflow"}      （内核缓冲区溢出，有事件丢失）
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/capability.h>
#include <linux/cn_proc.h>
#include <linux/connector.h>
#include <linux/netlink.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define PF_KTHREAD 0x00200000
#define CMD_MAX 1024

static double now_mono(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static ssize_t read_file(const char *path, char *buf, size_t cap) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t n = read(fd, buf, cap - 1);
    close(fd);
    if (n < 0) return -1;
    buf[n] = 0;
    return n;
}

/* 写出 JSON 字符串（带转义） */
static void json_str(FILE *f, const char *s, size_t len) {
    fputc('"', f);
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') { fputc('\\', f); fputc(c, f); }
        else if (c == '\n') fputs("\\n", f);
        else if (c == '\t') fputs("\\t", f);
        else if (c < 0x20) fprintf(f, "\\u%04x", c);
        else fputc(c, f);
    }
    fputc('"', f);
}

static void put_comm(FILE *f, int pid) {
    char path[64], buf[64];
    snprintf(path, sizeof path, "/proc/%d/comm", pid);
    ssize_t n = read_file(path, buf, sizeof buf);
    if (n > 0) {
        if (buf[n - 1] == '\n') n--;
        fputs(",\"comm\":", f);
        json_str(f, buf, (size_t)n);
    }
}

/* 解析 /proc/pid/stat：ppid(4)、flags(9)、starttime(22) */
static int read_stat(int pid, int *ppid, unsigned long *flags, unsigned long long *start) {
    char path[64], buf[1024];
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    if (read_file(path, buf, sizeof buf) <= 0) return -1;
    char *p = strrchr(buf, ')');
    if (!p) return -1;
    p += 2; /* 指向 state 字段（第 3 个） */
    int field = 3;
    char *save = NULL;
    for (char *tok = strtok_r(p, " ", &save); tok; tok = strtok_r(NULL, " ", &save), field++) {
        if (field == 4 && ppid) *ppid = atoi(tok);
        else if (field == 9 && flags) *flags = strtoul(tok, NULL, 10);
        else if (field == 22) { if (start) *start = strtoull(tok, NULL, 10); break; }
    }
    return 0;
}

static void put_exec_details(FILE *f, int pid) {
    char path[64], buf[CMD_MAX + 1];
    snprintf(path, sizeof path, "/proc/%d/exe", pid);
    ssize_t n = readlink(path, buf, CMD_MAX);
    if (n > 0) { fputs(",\"exe\":", f); json_str(f, buf, (size_t)n); }
    snprintf(path, sizeof path, "/proc/%d/cmdline", pid);
    n = read_file(path, buf, sizeof buf);
    if (n > 0) {
        while (n > 0 && buf[n - 1] == 0) n--;
        for (ssize_t i = 0; i < n; i++) if (buf[i] == 0) buf[i] = ' ';
        fputs(",\"cmd\":", f);
        json_str(f, buf, (size_t)n);
    }
}

static void drop_all_caps(void) {
    struct __user_cap_header_struct hdr = { _LINUX_CAPABILITY_VERSION_3, 0 };
    struct __user_cap_data_struct data[2];
    memset(data, 0, sizeof data);
    if (syscall(SYS_capset, &hdr, data) != 0) perror("capset");
    prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
}

static int subscribe(int sock, enum proc_cn_mcast_op op) {
    struct __attribute__((aligned(NLMSG_ALIGNTO))) {
        struct nlmsghdr nl;
        struct __attribute__((__packed__)) {
            struct cn_msg cn;
            enum proc_cn_mcast_op op;
        } body;
    } msg;
    memset(&msg, 0, sizeof msg);
    msg.nl.nlmsg_len = sizeof msg;
    msg.nl.nlmsg_pid = getpid();
    msg.nl.nlmsg_type = NLMSG_DONE;
    msg.body.cn.id.idx = CN_IDX_PROC;
    msg.body.cn.id.val = CN_VAL_PROC;
    msg.body.cn.len = sizeof(enum proc_cn_mcast_op);
    msg.body.op = op;
    return send(sock, &msg, sizeof msg, 0) < 0 ? -1 : 0;
}

int main(void) {
    prctl(PR_SET_PDEATHSIG, SIGTERM);
    signal(SIGPIPE, SIG_DFL);
    static char outbuf[1 << 16];
    setvbuf(stdout, outbuf, _IOFBF, sizeof outbuf);

    int sock = socket(PF_NETLINK, SOCK_DGRAM | SOCK_CLOEXEC, NETLINK_CONNECTOR);
    if (sock < 0) { perror("socket"); return 2; }
    struct sockaddr_nl sa = { .nl_family = AF_NETLINK, .nl_groups = CN_IDX_PROC, .nl_pid = getpid() };
    if (bind(sock, (struct sockaddr *)&sa, sizeof sa) < 0) { perror("bind"); return 3; }
    int rcv = 4 << 20;
    setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rcv, sizeof rcv);
    setsockopt(sock, SOL_SOCKET, SO_RCVBUFFORCE, &rcv, sizeof rcv); /* 需要 CAP_NET_ADMIN，失败无所谓 */
    if (subscribe(sock, PROC_CN_MCAST_LISTEN) < 0) { perror("subscribe"); return 4; }
    drop_all_caps();

    /* 内核不会对无权限的订阅回错误，只是收不到事件；Python 端会用超时检测 */
    puts("{\"e\":\"ready\"}");
    fflush(stdout);

    char buf[1 << 15] __attribute__((aligned(NLMSG_ALIGNTO)));
    for (;;) {
        ssize_t len = recv(sock, buf, sizeof buf, 0);
        if (len < 0) {
            if (errno == EINTR) continue;
            if (errno == ENOBUFS) { puts("{\"e\":\"overflow\"}"); fflush(stdout); continue; }
            perror("recv");
            return 5;
        }
        if (len == 0) break;
        for (struct nlmsghdr *nh = (struct nlmsghdr *)buf; NLMSG_OK(nh, (size_t)len); nh = NLMSG_NEXT(nh, len)) {
            if (nh->nlmsg_type == NLMSG_NOOP || nh->nlmsg_type == NLMSG_ERROR) continue;
            struct cn_msg *cn = NLMSG_DATA(nh);
            struct proc_event *ev = (struct proc_event *)cn->data;
            double t = now_mono();
            switch (ev->what) {
            case PROC_EVENT_FORK: {
                int pid = ev->event_data.fork.child_pid;
                if (pid != ev->event_data.fork.child_tgid) break; /* 线程，忽略 */
                int ptgid = ev->event_data.fork.parent_tgid;
                unsigned long flags = 0;
                read_stat(pid, NULL, &flags, NULL);
                int k = (ptgid == 2 || pid == 2 || (flags & PF_KTHREAD)) ? 1 : 0;
                printf("{\"e\":\"fork\",\"pid\":%d,\"ppid\":%d,\"k\":%d", pid, ptgid, k);
                put_comm(stdout, pid);
                printf(",\"t\":%.6f}\n", t);
                break;
            }
            case PROC_EVENT_EXEC: {
                int pid = ev->event_data.exec.process_pid;
                if (pid != ev->event_data.exec.process_tgid) break;
                int ppid = 0;
                read_stat(pid, &ppid, NULL, NULL);
                printf("{\"e\":\"exec\",\"pid\":%d,\"ppid\":%d", pid, ppid);
                put_comm(stdout, pid);
                put_exec_details(stdout, pid);
                printf(",\"t\":%.6f}\n", t);
                break;
            }
            case PROC_EVENT_EXIT: {
                int pid = ev->event_data.exit.process_pid;
                if (pid != ev->event_data.exit.process_tgid) break;
                unsigned int st = ev->event_data.exit.exit_code;
                unsigned long flags = 0;
                unsigned long long start = 0;
                read_stat(pid, NULL, &flags, &start);
                int sig = st & 0x7f, code = (st >> 8) & 0xff;
                printf("{\"e\":\"exit\",\"pid\":%d,\"code\":%d,\"sig\":%d,\"core\":%d,\"start\":%llu,\"k\":%d",
                       pid, code, sig, (st & 0x80) ? 1 : 0, start, (flags & PF_KTHREAD) ? 1 : 0);
                put_comm(stdout, pid);
                printf(",\"t\":%.6f}\n", t);
                break;
            }
            default:
                break;
            }
        }
        if (fflush(stdout) == EOF) break;
    }
    return 0;
}

/* fakepad: the Alia Pad gamepad without /dev/uinput (docs/design.md §5), for Docker Desktop
 * on Windows and macOS, whose virtual machine has no uinput.
 *
 * Preloaded into the cart (LD_PRELOAD) only when the supervisor runs without uinput. The
 * supervisor makes a device node (ALIA_FAKEPAD, e.g. /dev/input/event200; a char device the
 * kernel doesn't have) so that SDL finds it in /dev/input like any gamepad; opening it here
 * connects to the supervisor's socket (ALIA_FAKEPAD_SOCKET) instead, which sends struct
 * input_event, one SEQPACKET per report. The evdev ioctls SDL asks are answered with the
 * same device alia-inputd creates on the handheld: name, IDs, buttons and axes, so SDL
 * computes the same GUID and runtime.toml's mapping applies as is.
 * Keep in step with alia_runtime/uinput.py and runtime.toml [gamepad].
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static const char NAME[] = "Alia Pad Gamepad";
static const struct input_id ID = {.bustype = BUS_VIRTUAL, .vendor = 0x1209, .product = 0x0001, .version = 1};
static const int KEYS[] = {BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST, BTN_TL, BTN_TR, BTN_TL2, BTN_TR2,
                           BTN_SELECT, BTN_START, BTN_THUMBL, BTN_THUMBR,
                           BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT};
static const int AXES[] = {ABS_X, ABS_Y, ABS_RX, ABS_RY};
#define AXIS_MIN -32768
#define AXIS_MAX 32767
#define AXIS_FUZZ 256

#define MAX_FDS 16
static int fake_fds[MAX_FDS];
static int nfake;

static int is_fake(int fd) {
    for (int i = 0; i < nfake; i++)
        if (fake_fds[i] == fd) return 1;
    return 0;
}

static void forget(int fd) {
    for (int i = 0; i < nfake; i++)
        if (fake_fds[i] == fd) fake_fds[i] = fake_fds[--nfake];
}

static int fake_path(const char *path) {
    const char *dev = getenv("ALIA_FAKEPAD");
    return dev && path && strcmp(path, dev) == 0;
}

/* opening the node: a connection to the supervisor */
static int fake_open(int flags) {
    const char *sock = getenv("ALIA_FAKEPAD_SOCKET");
    if (!sock || nfake == MAX_FDS) {
        errno = ENODEV;
        return -1;
    }
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | ((flags & O_CLOEXEC) ? SOCK_CLOEXEC : 0), 0);
    if (fd < 0) return -1;
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    strncpy(a.sun_path, sock, sizeof a.sun_path - 1);
    if (connect(fd, (struct sockaddr *)&a, sizeof a) != 0) {
        int e = errno;
        close(fd);
        errno = e == ENOENT ? ENODEV : e;
        return -1;
    }
    if (flags & O_NONBLOCK) fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    fake_fds[nfake++] = fd;
    return fd;
}

#define REAL(name, ret, ...) \
    static ret (*real_##name)(__VA_ARGS__); \
    if (!real_##name) real_##name = dlsym(RTLD_NEXT, #name);

int open(const char *path, int flags, ...) {
    if (fake_path(path)) return fake_open(flags);
    REAL(open, int, const char *, int, ...)
    va_list ap;
    va_start(ap, flags);
    mode_t mode = (flags & (O_CREAT | O_TMPFILE)) ? va_arg(ap, mode_t) : 0;
    va_end(ap);
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    if (fake_path(path)) return fake_open(flags);
    REAL(open64, int, const char *, int, ...)
    va_list ap;
    va_start(ap, flags);
    mode_t mode = (flags & (O_CREAT | O_TMPFILE)) ? va_arg(ap, mode_t) : 0;
    va_end(ap);
    return real_open64(path, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
    if (fake_path(path)) return fake_open(flags);
    REAL(openat, int, int, const char *, int, ...)
    va_list ap;
    va_start(ap, flags);
    mode_t mode = (flags & (O_CREAT | O_TMPFILE)) ? va_arg(ap, mode_t) : 0;
    va_end(ap);
    return real_openat(dirfd, path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    if (fake_path(path)) return fake_open(flags);
    REAL(openat64, int, int, const char *, int, ...)
    va_list ap;
    va_start(ap, flags);
    mode_t mode = (flags & (O_CREAT | O_TMPFILE)) ? va_arg(ap, mode_t) : 0;
    va_end(ap);
    return real_openat64(dirfd, path, flags, mode);
}

/* _FORTIFY_SOURCE's open (no mode argument) */
int __open_2(const char *path, int flags) {
    if (fake_path(path)) return fake_open(flags);
    REAL(__open_2, int, const char *, int)
    return real___open_2(path, flags);
}

int __open64_2(const char *path, int flags) {
    if (fake_path(path)) return fake_open(flags);
    REAL(__open64_2, int, const char *, int)
    return real___open64_2(path, flags);
}

int close(int fd) {
    forget(fd);
    REAL(close, int, int)
    return real_close(fd);
}

static void set_bit(unsigned char *bits, size_t len, int bit) {
    if ((size_t)(bit / 8) < len) bits[bit / 8] |= 1 << (bit % 8);
}

/* the evdev queries, answered with alia-inputd's gamepad */
static int fake_ioctl(unsigned long req, void *arg) {
    unsigned nr = _IOC_NR(req), size = _IOC_SIZE(req);
    if (_IOC_TYPE(req) != 'E') {
        errno = ENOTTY;
        return -1;
    }
    if (req == EVIOCGVERSION) {
        *(int *)arg = EV_VERSION;
        return 0;
    }
    if (req == EVIOCGID) {
        memcpy(arg, &ID, sizeof ID);
        return 0;
    }
    if (_IOC_DIR(req) == _IOC_READ && nr == _IOC_NR(EVIOCGNAME(0))) {
        size_t n = sizeof NAME < size ? sizeof NAME : size;
        memcpy(arg, NAME, n);
        if (n) ((char *)arg)[n - 1] = '\0';
        return (int)n;
    }
    if (_IOC_DIR(req) == _IOC_READ && (nr == _IOC_NR(EVIOCGPHYS(0)) || nr == _IOC_NR(EVIOCGUNIQ(0)))) {
        if (size) ((char *)arg)[0] = '\0';
        return size ? 1 : 0;
    }
    if (_IOC_DIR(req) == _IOC_READ && (nr == _IOC_NR(EVIOCGPROP(0)) || nr == _IOC_NR(EVIOCGKEY(0)) ||
                                        nr == _IOC_NR(EVIOCGLED(0)) || nr == _IOC_NR(EVIOCGSND(0)) ||
                                        nr == _IOC_NR(EVIOCGSW(0)))) {
        memset(arg, 0, size);  /* no properties; nothing held at open (the supervisor sends changes) */
        return (int)size;
    }
    if (_IOC_DIR(req) == _IOC_READ && nr >= 0x20 && nr < 0x20 + EV_CNT) {  /* EVIOCGBIT(type, len) */
        unsigned char *bits = arg;
        memset(bits, 0, size);
        switch (nr - 0x20) {
        case 0:
            set_bit(bits, size, EV_SYN);
            set_bit(bits, size, EV_KEY);
            set_bit(bits, size, EV_ABS);
            break;
        case EV_KEY:
            for (size_t i = 0; i < sizeof KEYS / sizeof *KEYS; i++) set_bit(bits, size, KEYS[i]);
            break;
        case EV_ABS:
            for (size_t i = 0; i < sizeof AXES / sizeof *AXES; i++) set_bit(bits, size, AXES[i]);
            break;
        }
        return (int)size;
    }
    if (_IOC_DIR(req) == _IOC_READ && nr >= 0x40 && nr < 0x40 + ABS_CNT) {  /* EVIOCGABS(axis) */
        struct input_absinfo info = {0};
        for (size_t i = 0; i < sizeof AXES / sizeof *AXES; i++)
            if ((int)(nr - 0x40) == AXES[i])
                info = (struct input_absinfo){.value = 0, .minimum = AXIS_MIN, .maximum = AXIS_MAX, .fuzz = AXIS_FUZZ};
        memcpy(arg, &info, size < sizeof info ? size : sizeof info);
        return 0;
    }
    if (req == EVIOCGRAB) return 0;
    errno = EINVAL;  /* force feedback and the rest: like a gamepad without motors (none yet) */
    return -1;
}

int ioctl(int fd, unsigned long req, ...) {
    va_list ap;
    va_start(ap, req);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (is_fake(fd)) return fake_ioctl(req, arg);
    REAL(ioctl, int, int, unsigned long, ...)
    return real_ioctl(fd, req, arg);
}

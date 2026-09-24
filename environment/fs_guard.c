#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static const char *protected_roots[] = {
    "/logs/verifier",
    "/logs/artifacts",
    NULL
};

static int prefix_match(const char *path, const char *root) {
    size_t n = strlen(root);
    return strncmp(path, root, n) == 0 && (path[n] == '\0' || path[n] == '/');
}

static int path_is_protected_absolute(const char *path) {
    if (!path || path[0] != '/') return 0;
    for (size_t i = 0; protected_roots[i]; ++i) {
        if (prefix_match(path, protected_roots[i])) return 1;
    }
    return 0;
}

static int resolve_base_for_dirfd(int dirfd, char *buf, size_t len) {
    if (dirfd == AT_FDCWD) {
        return getcwd(buf, len) ? 0 : -1;
    }
    char procpath[64];
    if (snprintf(procpath, sizeof(procpath), "/proc/self/fd/%d", dirfd) >= (int)sizeof(procpath)) return -1;
    ssize_t n = readlink(procpath, buf, len - 1);
    if (n < 0 || (size_t)n >= len - 1) return -1;
    buf[n] = '\0';
    return 0;
}

static int normalize_candidate(int dirfd, const char *path, char *out, size_t out_len) {
    if (!path || !*path) return -1;
    if (path[0] == '/') {
        if (snprintf(out, out_len, "%s", path) >= (int)out_len) return -1;
    } else {
        char base[PATH_MAX];
        if (resolve_base_for_dirfd(dirfd, base, sizeof(base)) != 0) return -1;
        if (snprintf(out, out_len, "%s/%s", base, path) >= (int)out_len) return -1;
    }

    /* Resolve an existing parent so symlink aliases into protected trees are covered. */
    char work[PATH_MAX];
    if (snprintf(work, sizeof(work), "%s", out) >= (int)sizeof(work)) return -1;
    char *slash = strrchr(work, '/');
    if (slash && slash != work) {
        char leaf[NAME_MAX + 1];
        if (snprintf(leaf, sizeof(leaf), "%s", slash + 1) >= (int)sizeof(leaf)) return 0;
        *slash = '\0';
        char parent[PATH_MAX];
        if (realpath(work, parent)) {
            size_t pn = strlen(parent), ln = strlen(leaf);
            if (pn + 1 + ln + 1 <= out_len) {
                memcpy(out, parent, pn);
                out[pn] = '/';
                memcpy(out + pn + 1, leaf, ln + 1);
            }
        }
    } else if (slash == work) {
        /* Parent is /. */
    }
    return 0;
}

static int path_is_protected_at(int dirfd, const char *path) {
    char candidate[PATH_MAX];
    if (normalize_candidate(dirfd, path, candidate, sizeof(candidate)) != 0) return 0;
    return path_is_protected_absolute(candidate);
}


static int running_as_task_init(void) {
    static int cached = -1;
    if (cached != -1) return cached;
    char exe[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n < 0) { cached = 0; return cached; }
    exe[n] = '\0';
    const char *base = strrchr(exe, '/');
    base = base ? base + 1 : exe;
    cached = strcmp(base, "task-init") == 0;
    return cached;
}

static int deny_if_protected_at(int dirfd, const char *path) {
    if (running_as_task_init()) return 0;
    if (path_is_protected_at(dirfd, path)) {
        errno = EACCES;
        return 1;
    }
    return 0;
}

static void *next_sym(const char *name) {
    void *p = dlsym(RTLD_NEXT, name);
    if (!p) _exit(127);
    return p;
}

int open(const char *path, int flags, ...) {
    static int (*real_open)(const char *, int, ...) = NULL;
    if (!real_open) real_open = next_sym("open");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode_t mode = (mode_t)va_arg(ap, int); va_end(ap);
        return real_open(path, flags, mode);
    }
    return real_open(path, flags);
}

int open64(const char *path, int flags, ...) {
    static int (*real_open64)(const char *, int, ...) = NULL;
    if (!real_open64) real_open64 = next_sym("open64");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode_t mode = (mode_t)va_arg(ap, int); va_end(ap);
        return real_open64(path, flags, mode);
    }
    return real_open64(path, flags);
}

int openat(int dirfd, const char *path, int flags, ...) {
    static int (*real_openat)(int, const char *, int, ...) = NULL;
    if (!real_openat) real_openat = next_sym("openat");
    if (deny_if_protected_at(dirfd, path)) return -1;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode_t mode = (mode_t)va_arg(ap, int); va_end(ap);
        return real_openat(dirfd, path, flags, mode);
    }
    return real_openat(dirfd, path, flags);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    static int (*real_openat64)(int, const char *, int, ...) = NULL;
    if (!real_openat64) real_openat64 = next_sym("openat64");
    if (deny_if_protected_at(dirfd, path)) return -1;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode_t mode = (mode_t)va_arg(ap, int); va_end(ap);
        return real_openat64(dirfd, path, flags, mode);
    }
    return real_openat64(dirfd, path, flags);
}

FILE *fopen(const char *path, const char *mode) {
    static FILE *(*real_fopen)(const char *, const char *) = NULL;
    if (!real_fopen) real_fopen = next_sym("fopen");
    if (deny_if_protected_at(AT_FDCWD, path)) return NULL;
    return real_fopen(path, mode);
}

FILE *fopen64(const char *path, const char *mode) {
    static FILE *(*real_fopen64)(const char *, const char *) = NULL;
    if (!real_fopen64) real_fopen64 = next_sym("fopen64");
    if (deny_if_protected_at(AT_FDCWD, path)) return NULL;
    return real_fopen64(path, mode);
}

FILE *freopen(const char *path, const char *mode, FILE *stream) {
    static FILE *(*real_freopen)(const char *, const char *, FILE *) = NULL;
    if (!real_freopen) real_freopen = next_sym("freopen");
    if (path && deny_if_protected_at(AT_FDCWD, path)) return NULL;
    return real_freopen(path, mode, stream);
}

int creat(const char *path, mode_t mode) {
    static int (*real_creat)(const char *, mode_t) = NULL;
    if (!real_creat) real_creat = next_sym("creat");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_creat(path, mode);
}

int creat64(const char *path, mode_t mode) {
    static int (*real_creat64)(const char *, mode_t) = NULL;
    if (!real_creat64) real_creat64 = next_sym("creat64");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_creat64(path, mode);
}

int mkdir(const char *path, mode_t mode) {
    static int (*real_mkdir)(const char *, mode_t) = NULL;
    if (!real_mkdir) real_mkdir = next_sym("mkdir");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_mkdir(path, mode);
}

int mkdirat(int dirfd, const char *path, mode_t mode) {
    static int (*real_mkdirat)(int, const char *, mode_t) = NULL;
    if (!real_mkdirat) real_mkdirat = next_sym("mkdirat");
    if (deny_if_protected_at(dirfd, path)) return -1;
    return real_mkdirat(dirfd, path, mode);
}

int unlink(const char *path) {
    static int (*real_unlink)(const char *) = NULL;
    if (!real_unlink) real_unlink = next_sym("unlink");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_unlink(path);
}

int unlinkat(int dirfd, const char *path, int flags) {
    static int (*real_unlinkat)(int, const char *, int) = NULL;
    if (!real_unlinkat) real_unlinkat = next_sym("unlinkat");
    if (deny_if_protected_at(dirfd, path)) return -1;
    return real_unlinkat(dirfd, path, flags);
}

int rmdir(const char *path) {
    static int (*real_rmdir)(const char *) = NULL;
    if (!real_rmdir) real_rmdir = next_sym("rmdir");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_rmdir(path);
}

int rename(const char *oldp, const char *newp) {
    static int (*real_rename)(const char *, const char *) = NULL;
    if (!real_rename) real_rename = next_sym("rename");
    if (deny_if_protected_at(AT_FDCWD, oldp) || deny_if_protected_at(AT_FDCWD, newp)) return -1;
    return real_rename(oldp, newp);
}

int renameat(int oldfd, const char *oldp, int newfd, const char *newp) {
    static int (*real_renameat)(int, const char *, int, const char *) = NULL;
    if (!real_renameat) real_renameat = next_sym("renameat");
    if (deny_if_protected_at(oldfd, oldp) || deny_if_protected_at(newfd, newp)) return -1;
    return real_renameat(oldfd, oldp, newfd, newp);
}

int renameat2(int oldfd, const char *oldp, int newfd, const char *newp, unsigned int flags) {
    static int (*real_renameat2)(int, const char *, int, const char *, unsigned int) = NULL;
    if (!real_renameat2) real_renameat2 = next_sym("renameat2");
    if (deny_if_protected_at(oldfd, oldp) || deny_if_protected_at(newfd, newp)) return -1;
    return real_renameat2(oldfd, oldp, newfd, newp, flags);
}

int symlink(const char *target, const char *linkpath) {
    static int (*real_symlink)(const char *, const char *) = NULL;
    if (!real_symlink) real_symlink = next_sym("symlink");
    if (deny_if_protected_at(AT_FDCWD, linkpath)) return -1;
    return real_symlink(target, linkpath);
}

int symlinkat(const char *target, int newdirfd, const char *linkpath) {
    static int (*real_symlinkat)(const char *, int, const char *) = NULL;
    if (!real_symlinkat) real_symlinkat = next_sym("symlinkat");
    if (deny_if_protected_at(newdirfd, linkpath)) return -1;
    return real_symlinkat(target, newdirfd, linkpath);
}

int link(const char *oldp, const char *newp) {
    static int (*real_link)(const char *, const char *) = NULL;
    if (!real_link) real_link = next_sym("link");
    if (deny_if_protected_at(AT_FDCWD, newp)) return -1;
    return real_link(oldp, newp);
}

int linkat(int oldfd, const char *oldp, int newfd, const char *newp, int flags) {
    static int (*real_linkat)(int, const char *, int, const char *, int) = NULL;
    if (!real_linkat) real_linkat = next_sym("linkat");
    if (deny_if_protected_at(newfd, newp)) return -1;
    return real_linkat(oldfd, oldp, newfd, newp, flags);
}

int truncate(const char *path, off_t length) {
    static int (*real_truncate)(const char *, off_t) = NULL;
    if (!real_truncate) real_truncate = next_sym("truncate");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_truncate(path, length);
}

int chmod(const char *path, mode_t mode) {
    static int (*real_chmod)(const char *, mode_t) = NULL;
    if (!real_chmod) real_chmod = next_sym("chmod");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_chmod(path, mode);
}

int chown(const char *path, uid_t owner, gid_t group) {
    static int (*real_chown)(const char *, uid_t, gid_t) = NULL;
    if (!real_chown) real_chown = next_sym("chown");
    if (deny_if_protected_at(AT_FDCWD, path)) return -1;
    return real_chown(path, owner, group);
}

static const char guard_preload_env[] = "LD_PRELOAD=/usr/local/lib/task-fs-guard.so";

static int is_preload_name(const char *name) {
    return name && strcmp(name, "LD_PRELOAD") == 0;
}

int unsetenv(const char *name) {
    static int (*real_unsetenv)(const char *) = NULL;
    if (!real_unsetenv) real_unsetenv = next_sym("unsetenv");
    if (is_preload_name(name)) return 0;
    return real_unsetenv(name);
}

int setenv(const char *name, const char *value, int overwrite) {
    static int (*real_setenv)(const char *, const char *, int) = NULL;
    if (!real_setenv) real_setenv = next_sym("setenv");
    if (is_preload_name(name)) return 0;
    return real_setenv(name, value, overwrite);
}

int putenv(char *string) {
    static int (*real_putenv)(char *) = NULL;
    if (!real_putenv) real_putenv = next_sym("putenv");
    if (strncmp(string, "LD_PRELOAD=", 11) == 0) return 0;
    return real_putenv(string);
}

int clearenv(void) {
    static int (*real_clearenv)(void) = NULL;
    static int (*real_setenv)(const char *, const char *, int) = NULL;
    if (!real_clearenv) real_clearenv = next_sym("clearenv");
    if (!real_setenv) real_setenv = next_sym("setenv");
    int rc = real_clearenv();
    if (rc == 0) rc = real_setenv("LD_PRELOAD", "/usr/local/lib/task-fs-guard.so", 1);
    return rc;
}

static char **guard_env(char *const envp[]) {
    size_t n = 0;
    int found = 0;
    if (envp) {
        while (envp[n]) {
            if (strncmp(envp[n], "LD_PRELOAD=", 11) == 0) found = 1;
            ++n;
        }
    }
    char **copy = calloc(n + (found ? 1 : 2), sizeof(char *));
    if (!copy) return NULL;
    for (size_t i = 0; i < n; ++i) {
        if (strncmp(envp[i], "LD_PRELOAD=", 11) == 0) copy[i] = (char *)guard_preload_env;
        else copy[i] = envp[i];
    }
    if (!found) copy[n++] = (char *)guard_preload_env;
    copy[n] = NULL;
    return copy;
}

int execve(const char *pathname, char *const argv[], char *const envp[]) {
    static int (*real_execve)(const char *, char *const[], char *const[]) = NULL;
    if (!real_execve) real_execve = next_sym("execve");
    char **env = guard_env(envp);
    if (!env) { errno = ENOMEM; return -1; }
    int rc = real_execve(pathname, argv, env);
    int saved = errno;
    free(env);
    errno = saved;
    return rc;
}

int execveat(int dirfd, const char *pathname, char *const argv[], char *const envp[], int flags) {
    static int (*real_execveat)(int, const char *, char *const[], char *const[], int) = NULL;
    if (!real_execveat) real_execveat = next_sym("execveat");
    char **env = guard_env(envp);
    if (!env) { errno = ENOMEM; return -1; }
    int rc = real_execveat(dirfd, pathname, argv, env, flags);
    int saved = errno;
    free(env);
    errno = saved;
    return rc;
}

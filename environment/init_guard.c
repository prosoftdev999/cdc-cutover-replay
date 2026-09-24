#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static const uid_t AGENT_UID = 10001;
static const gid_t AGENT_GID = 10001;

static int ensure_dir(const char *path, mode_t mode) {
    struct stat st;
    if (lstat(path, &st) == 0) {
        if (!S_ISDIR(st.st_mode)) {
            fprintf(stderr, "task-init: %s is not a directory\n", path);
            return -1;
        }
        return 0;
    }
    if (errno != ENOENT || mkdir(path, mode) != 0) {
        fprintf(stderr, "task-init: cannot create %s: %s\n", path, strerror(errno));
        return -1;
    }
    return 0;
}

static int seal_tree(const char *path) {
    DIR *dir = opendir(path);
    if (!dir) {
        fprintf(stderr, "task-init: cannot open %s: %s\n", path, strerror(errno));
        return -1;
    }

    struct dirent *ent;
    char child[PATH_MAX];
    while ((ent = readdir(dir)) != NULL) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) {
            continue;
        }
        if (snprintf(child, sizeof(child), "%s/%s", path, ent->d_name) >= (int)sizeof(child)) {
            closedir(dir);
            return -1;
        }

        struct stat st;
        if (lstat(child, &st) != 0) {
            continue;
        }
        if (S_ISDIR(st.st_mode)) {
            if (chmod(child, 0755) != 0 || seal_tree(child) != 0 || chown(child, 0, 0) != 0 || chmod(child, 0555) != 0) {
                closedir(dir);
                return -1;
            }
        } else if (S_ISREG(st.st_mode)) {
            if (chown(child, 0, 0) != 0 || chmod(child, 0444) != 0) {
                closedir(dir);
                return -1;
            }
        }
    }
    closedir(dir);
    return 0;
}

static int seal_dir(const char *path) {
    if (ensure_dir(path, 0755) != 0) {
        return -1;
    }
    if (chown(path, 0, 0) != 0 || chmod(path, 0755) != 0) {
        fprintf(stderr, "task-init: cannot prepare %s: %s\n", path, strerror(errno));
        return -1;
    }

    char marker[PATH_MAX];
    if (snprintf(marker, sizeof(marker), "%s/.sealed", path) >= (int)sizeof(marker)) {
        return -1;
    }
    int fd = open(marker, O_WRONLY | O_CREAT, 0444);
    if (fd >= 0) {
        static const char note[] = "grader directory is not agent-writable\n";
        (void)write(fd, note, sizeof(note) - 1);
        close(fd);
        (void)chown(marker, 0, 0);
        (void)chmod(marker, 0444);
    }

    if (seal_tree(path) != 0 || chown(path, 0, 0) != 0 || chmod(path, 0555) != 0) {
        fprintf(stderr, "task-init: cannot seal %s: %s\n", path, strerror(errno));
        return -1;
    }
    return 0;
}

static int prepare_logs(void) {
    if (ensure_dir("/logs", 0755) != 0) {
        return -1;
    }
    if (chown("/logs", 0, 0) != 0 || chmod("/logs", 0755) != 0) {
        fprintf(stderr, "task-init: cannot protect /logs: %s\n", strerror(errno));
        return -1;
    }

    if (ensure_dir("/logs/agent", 0755) != 0) {
        return -1;
    }
    if (chown("/logs/agent", AGENT_UID, AGENT_GID) != 0 || chmod("/logs/agent", 0755) != 0) {
        fprintf(stderr, "task-init: cannot prepare /logs/agent: %s\n", strerror(errno));
        return -1;
    }

    if (seal_dir("/logs/verifier") != 0 || seal_dir("/logs/artifacts") != 0) {
        return -1;
    }
    return 0;
}

static void drop_privileges(void) {
    if (setgroups(0, NULL) != 0 ||
        setresgid(AGENT_GID, AGENT_GID, AGENT_GID) != 0 ||
        setresuid(AGENT_UID, AGENT_UID, AGENT_UID) != 0) {
        fprintf(stderr, "task-init: cannot drop privileges: %s\n", strerror(errno));
        _exit(126);
    }
}

int main(int argc, char **argv) {
    if (geteuid() != 0) {
        fprintf(stderr, "task-init: privileged setup unavailable\n");
        return 126;
    }

    if (prepare_logs() != 0) {
        return 126;
    }
    drop_privileges();

    if (argc < 2) {
        execlp("sleep", "sleep", "infinity", (char *)NULL);
    } else {
        execvp(argv[1], &argv[1]);
    }
    fprintf(stderr, "task-init: exec failed: %s\n", strerror(errno));
    return 127;
}

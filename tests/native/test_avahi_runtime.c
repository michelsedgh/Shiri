/* Compile the actual pinned/patched runtime function against credential mocks. */
#include <assert.h>
#include <errno.h>
#include <grp.h>
#include <pwd.h>
#include <stdio.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define AVAHI_USER "avahi"
#define AVAHI_GROUP "avahi"
#define AVAHI_DAEMON_RUNTIME_DIR "/run/avahi-daemon/"
static struct { int drop_root; } config;
static uid_t current_uid, effective_uid;
static gid_t current_gid;
static struct passwd configured_user, executing_user;
static struct group configured_group, executing_group;
static struct stat runtime_info;
static int by_name_user, by_name_group, by_id_user, by_id_group, ownership_calls;
static int absent_user, absent_group, mkdir_failure, stat_failure, ownership_failure;
static uid_t assigned_uid;
static gid_t assigned_gid;
static mode_t last_umask;

static uid_t mock_getuid(void) { return current_uid; }
static uid_t mock_geteuid(void) { return effective_uid; }
static gid_t mock_getgid(void) { return current_gid; }
static struct passwd *mock_getpwnam(const char *name) {
    (void)name; by_name_user++; return absent_user ? NULL : &configured_user;
}
static struct passwd *mock_getpwuid(uid_t uid) {
    assert(uid == current_uid); by_id_user++; return absent_user ? NULL : &executing_user;
}
static struct group *mock_getgrnam(const char *name) {
    (void)name; by_name_group++; return absent_group ? NULL : &configured_group;
}
static struct group *mock_getgrgid(gid_t gid) {
    assert(gid == current_gid); by_id_group++; return absent_group ? NULL : &executing_group;
}
static int mock_mkdir(const char *path, mode_t mode) {
    (void)path; assert(mode == 0755); errno = mkdir_failure ? EACCES : EEXIST;
    return -1;
}
static int mock_chown(const char *path, uid_t uid, gid_t gid) {
    (void)path; ownership_calls++; assigned_uid = uid; assigned_gid = gid;
    if (ownership_failure) { errno = EPERM; return -1; }
    runtime_info.st_uid = uid; runtime_info.st_gid = gid; return 0;
}
static int mock_stat(const char *path, struct stat *info) {
    (void)path; if (stat_failure) { errno = EACCES; return -1; }
    *info = runtime_info; return 0;
}
static mode_t mock_umask(mode_t mode) { mode_t previous = last_umask; last_umask = mode; return previous; }
#define getuid mock_getuid
#define geteuid mock_geteuid
#define getgid mock_getgid
#define getpwnam mock_getpwnam
#define getpwuid mock_getpwuid
#define getgrnam mock_getgrnam
#define getgrgid mock_getgrgid
#define mkdir mock_mkdir
#define chown mock_chown
#define stat(...) mock_stat(__VA_ARGS__)
#define umask mock_umask
#define avahi_log_error(...) ((void)0)
#include "avahi_runtime.inc"

static void reset(void) {
    current_uid = effective_uid = 201; current_gid = 202; config.drop_root = 0;
    configured_user.pw_uid = 70; configured_group.gr_gid = 71;
    executing_user.pw_uid = 201; executing_group.gr_gid = 202;
    runtime_info.st_mode = S_IFDIR | 0700;
    runtime_info.st_uid = 201; runtime_info.st_gid = 202;
    by_name_user = by_name_group = by_id_user = by_id_group = ownership_calls = 0;
    absent_user = absent_group = mkdir_failure = stat_failure = ownership_failure = 0;
    assigned_uid = assigned_gid = 0; last_umask = 0077;
}

int main(void) {
    reset();
    assert(make_runtime_dir() == 0);
    assert(by_id_user == 1 && by_id_group == 1 && by_name_user == 0 && by_name_group == 0);
    assert(assigned_uid == 201 && assigned_gid == 202 && last_umask == 0077);
    reset(); ownership_failure = 1; /* Capless no-op chown may be refused. */
    assert(make_runtime_dir() == 0 && runtime_info.st_uid == 201);
    reset(); ownership_failure = 1; runtime_info.st_uid = 70;
    assert(make_runtime_dir() == -1 && runtime_info.st_uid == 70);
    reset(); current_uid = effective_uid = 0; config.drop_root = 1;
    assert(make_runtime_dir() == 0 && by_name_user == 1 && by_name_group == 1);
    assert(by_id_user == 0 && by_id_group == 0 && assigned_uid == 70 && assigned_gid == 71);
    reset(); current_uid = effective_uid = 0; config.drop_root = 0;
    assert(make_runtime_dir() == 0 && assigned_uid == 70 && assigned_gid == 71);
    reset(); current_uid = 201; effective_uid = 0;
    assert(make_runtime_dir() == 0 && by_name_user == 1 && assigned_uid == 70);
    reset(); config.drop_root = 1;
    assert(make_runtime_dir() == 0 && assigned_uid == 70 && by_id_user == 0);
    reset(); absent_user = 1;
    assert(make_runtime_dir() == -1 && ownership_calls == 0 && last_umask == 0077);
    reset(); absent_group = 1;
    assert(make_runtime_dir() == -1 && ownership_calls == 0 && last_umask == 0077);
    reset(); mkdir_failure = 1;
    assert(make_runtime_dir() == -1 && ownership_calls == 0 && last_umask == 0077);
    reset(); stat_failure = 1;
    assert(make_runtime_dir() == -1 && ownership_calls == 1 && last_umask == 0077);
    reset(); runtime_info.st_mode = S_IFREG | 0700;
    assert(make_runtime_dir() == -1);
    puts("12 actual Avahi runtime credential/ownership cases passed");
    return 0;
}

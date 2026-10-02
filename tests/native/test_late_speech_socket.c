/* Linux/root-only synthetic credential test. No ALSA, transport or namespace
 * mutation: a private temporary socket and two capability-free test UIDs. */
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
static int injected_chmod_failure, injected_group_mismatch;
static int guarded_fchmodat(int directory, const char *path, mode_t mode, int flags);
static int guarded_fstatat(int directory, const char *path, struct stat *node, int flags);
#define fchmodat guarded_fchmodat
#define fstatat guarded_fstatat
#include SHIRI_SPEECH_SOURCE
#undef fchmodat
#undef fstatat
#include <assert.h>
#include <dirent.h>
#include <poll.h>
#include <signal.h>
#include <grp.h>
#include <linux/capability.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>

static unsigned checks;
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "failed line %d: %s\n", __LINE__, #x); abort(); } ++checks; } while (0)
static const uid_t output_uid = 65534, audio_uid = 65533;
struct observation { uint64_t sequence, refused, admitted; unsigned queued, descriptors; int sample; };

static int guarded_fchmodat(int directory, const char *path, mode_t mode, int flags)
{
  if (injected_chmod_failure) {
    injected_chmod_failure = 0; errno = EACCES; return -1;
  }
  return fchmodat(directory, path, mode, flags);
}
static int guarded_fstatat(int directory, const char *path, struct stat *node, int flags)
{
  int result = fstatat(directory, path, node, flags);
  if (!result && S_ISSOCK(node->st_mode) && injected_group_mismatch) {
    injected_group_mismatch = 0; node->st_gid = (gid_t)(node->st_gid + 1);
  }
  return result;
}

static void setup_failure_cleanup(const char *path)
{
  struct stat before, after; int fd;
  injected_chmod_failure = 1;
  CHECK(shiri_speech_init(path, audio_uid, "b6786543-7eb2-443d-83b1-65b984123a76",
                         "123456789abcdef0123456789abcdef0", 1) < 0);
  CHECK(!injected_chmod_failure && speech_fd < 0 && speech_directory < 0);
  CHECK(lstat(path, &after) < 0 && errno == ENOENT);
  injected_group_mismatch = 1;
  CHECK(shiri_speech_init(path, audio_uid, "b6786543-7eb2-443d-83b1-65b984123a76",
                         "123456789abcdef0123456789abcdef0", 1) < 0);
  CHECK(!injected_group_mismatch && speech_fd < 0 && speech_directory < 0);
  CHECK(lstat(path, &after) < 0 && errno == ENOENT);
  fd = open(path, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600); CHECK(fd >= 0);
  CHECK(fstat(fd, &before) == 0 && close(fd) == 0);
  CHECK(shiri_speech_init(path, audio_uid, "b6786543-7eb2-443d-83b1-65b984123a76",
                         "123456789abcdef0123456789abcdef0", 1) < 0);
  CHECK(lstat(path, &after) == 0 && S_ISREG(after.st_mode) &&
        after.st_dev == before.st_dev && after.st_ino == before.st_ino);
  CHECK(unlink(path) == 0);
}

static unsigned descriptors(void)
{
  DIR *d = opendir("/proc/self/fd"); struct dirent *entry; unsigned count = 0;
  assert(d);
  while ((entry = readdir(d))) if (entry->d_name[0] != '.') ++count;
  closedir(d); return count;
}
static void drop(uid_t uid)
{
  struct __user_cap_header_struct header = { _LINUX_CAPABILITY_VERSION_3, 0 };
  struct __user_cap_data_struct caps[2];
  CHECK(setgroups(0, NULL) == 0);
  CHECK(setresgid(uid, uid, uid) == 0);
  CHECK(setresuid(uid, uid, uid) == 0);
  CHECK(prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0);
  CHECK(getuid() == uid && geteuid() == uid);
  if (uid) {
    memset(caps, 0, sizeof(caps)); CHECK(syscall(SYS_capget, &header, caps) == 0);
    CHECK(!caps[0].effective && !caps[0].permitted && !caps[0].inheritable &&
          !caps[1].effective && !caps[1].permitted && !caps[1].inheritable);
  }
}
static void write_exact(int fd, const void *data, size_t bytes)
{
  const uint8_t *p = data; ssize_t n;
  while (bytes) { n = write(fd, p, bytes); CHECK(n > 0); p += n; bytes -= (size_t)n; }
}
static void read_exact(int fd, void *data, size_t bytes)
{
  uint8_t *p = data; ssize_t n; struct pollfd wait = { fd, POLLIN, 0 };
  while (bytes) {
    CHECK(poll(&wait, 1, 1500) == 1);
    n = read(fd, p, bytes); CHECK(n > 0); p += n; bytes -= (size_t)n;
  }
}
static void put32(uint8_t *p, uint32_t n)
{
  p[0] = (uint8_t)(n >> 24); p[1] = (uint8_t)(n >> 16); p[2] = (uint8_t)(n >> 8); p[3] = (uint8_t)n;
}
static void put64(uint8_t *p, uint64_t n)
{
  put32(p, (uint32_t)(n >> 32)); put32(p + 4, (uint32_t)n);
}
static size_t packet(uint8_t *p, uint64_t sequence)
{
  memset(p, 0, 1040); memcpy(p, "SHRITTS1", 8); p[8] = p[9] = 1; p[11] = 80;
  put32(p + 12, 960); put64(p + 16, sequence);
  CHECK(speech_hex("b6786543-7eb2-443d-83b1-65b984123a76", p + 24, 1) == 0);
  CHECK(speech_hex("123456789abcdef0123456789abcdef0", p + 40, 0) == 0);
  put64(p + 56, speech_now()); put32(p + 64, 480); put32(p + 68, 65536); put32(p + 72, 1);
  for (unsigned i = 0; i < 480; ++i) { p[80 + i * 2] = 0xe8; p[81 + i * 2] = 0x03; }
  return 1040;
}
static void send_packet(const char *path, uid_t uid, uint8_t *packet, size_t bytes, unsigned rights)
{
  pid_t child = fork(); int status;
  CHECK(child >= 0);
  if (!child) {
    struct sockaddr_un address; int fd, passed = -1;
    struct iovec iov = { packet, bytes }; struct msghdr message;
    union { struct cmsghdr align; unsigned char bytes[CMSG_SPACE(32 * sizeof(int))]; } ancillary;
    struct cmsghdr *control;
    drop(uid); fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0); CHECK(fd >= 0);
    memset(&address, 0, sizeof(address)); address.sun_family = AF_UNIX;
    CHECK(strlen(path) < sizeof(address.sun_path)); strcpy(address.sun_path, path);
    CHECK(connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0);
    memset(&message, 0, sizeof(message)); message.msg_iov = &iov; message.msg_iovlen = 1;
    if (rights) {
      CHECK(rights <= 32); passed = open("/dev/null", O_RDONLY | O_CLOEXEC); CHECK(passed >= 0);
      memset(&ancillary, 0, sizeof(ancillary)); message.msg_control = ancillary.bytes;
      message.msg_controllen = CMSG_SPACE(rights * sizeof(int)); control = CMSG_FIRSTHDR(&message);
      control->cmsg_level = SOL_SOCKET; control->cmsg_type = SCM_RIGHTS;
      control->cmsg_len = CMSG_LEN(rights * sizeof(int));
      for (unsigned i = 0; i < rights; ++i) memcpy((uint8_t *)CMSG_DATA(control) + i * sizeof(int), &passed, sizeof(int));
    }
    CHECK(sendmsg(fd, &message, MSG_NOSIGNAL) == (ssize_t)bytes);
    if (passed >= 0) close(passed);
    close(fd); _exit(0);
  }
  CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
}
static struct observation observe(int command, int reply)
{
  struct observation result; char op = 'P'; write_exact(command, &op, 1); read_exact(reply, &result, sizeof(result)); return result;
}

int main(void)
{
  char root[] = "/tmp/shiri-speech-kernel-XXXXXX", directory[108], path[108], old[108];
  int command[2], reply[2], status; char ready; pid_t server;
  uint8_t data[4096]; size_t bytes; struct observation before, after;
  struct stat node; FILE *file;
  CHECK(geteuid() == 0); alarm(12);
  CHECK(mkdtemp(root)); CHECK(chmod(root, 0711) == 0);
  CHECK(snprintf(directory, sizeof(directory), "%s/overlay", root) < (int)sizeof(directory));
  CHECK(mkdir(directory, 0700) == 0); CHECK(chown(directory, output_uid, audio_uid) == 0);
  CHECK(chmod(directory, 02710) == 0);
  CHECK(snprintf(path, sizeof(path), "%s/speech.sock", directory) < (int)sizeof(path));
  CHECK(snprintf(old, sizeof(old), "%s/old.sock", directory) < (int)sizeof(old));
  CHECK(pipe(command) == 0 && pipe(reply) == 0); server = fork(); CHECK(server >= 0);
  if (!server) {
    uint8_t pcm[1920]; char op; struct observation result;
    close(command[1]); close(reply[0]); drop(output_uid);
    CHECK(prctl(PR_SET_PDEATHSIG, SIGTERM, 0, 0, 0) == 0);
    setup_failure_cleanup(path);
    CHECK(shiri_speech_init(path, audio_uid, "b6786543-7eb2-443d-83b1-65b984123a76",
                           "123456789abcdef0123456789abcdef0", 1) == 0);
    ready = 'R'; write_exact(reply[1], &ready, 1);
    while (read(command[0], &op, 1) == 1) {
      if (op == 'X') break;
      CHECK(op == 'P'); shiri_speech_poll(); memset(pcm, 0, sizeof(pcm));
#ifdef SPEECH_RESERVE_NS
      /* Credential observations wait for the declared actual monotonic prime,
       * while all original room/UID/sequence/expiry/descriptor gates remain. */
      uint64_t observed;
      while (speech.running && (observed = speech_now()) < speech.priming_until) {
        CHECK(observed);
        uint64_t remaining = speech.priming_until - observed;
        CHECK(remaining <= SPEECH_RESERVE_NS);
        struct timespec delay = {0, (long)remaining};
        CHECK(nanosleep(&delay, NULL) == 0 || errno == EINTR);
      }
#endif
      shiri_speech_mix(pcm, sizeof(pcm), 480, 48000, 16, 2);
      result.sequence = speech.sequence; result.refused = speech.refused;
      result.admitted = speech.admitted; result.queued = speech.count;
      result.descriptors = descriptors(); result.sample = pcm[0] | ((unsigned)pcm[1] << 8);
      write_exact(reply[1], &result, sizeof(result));
    }
    shiri_speech_deinit(); close(command[0]); close(reply[1]); _exit(0);
  }
  close(command[0]); close(reply[1]); read_exact(reply[0], &ready, 1); CHECK(ready == 'R');
  CHECK(lstat(path, &node) == 0 && S_ISSOCK(node.st_mode) && node.st_uid == output_uid &&
        node.st_gid == audio_uid && (node.st_mode & 07777) == 0660);
  before = observe(command[1], reply[0]); CHECK(before.sequence == 0 && before.sample == 0);
  bytes = packet(data, 1); send_packet(path, 0, data, bytes, 0);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 0 && after.refused == 1 && after.sample == 0);
  bytes = packet(data, 1); send_packet(path, audio_uid, data, bytes, 0);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.admitted == 1 && after.sample == 1000 && !after.queued);
  /* Exact credential UID is insufficient without exact room/launch, freshness
   * and sequence. Every rejection must preserve the program and latest voice. */
  bytes = packet(data, 2); data[24] ^= 1; send_packet(path, audio_uid, data, bytes, 0);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0);
  bytes = packet(data, 2); data[40] ^= 1; send_packet(path, audio_uid, data, bytes, 0);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0);
  bytes = packet(data, 1); send_packet(path, audio_uid, data, bytes, 0);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0);
  bytes = packet(data, 2); put64(data + 56, speech_now() - SPEECH_AGE_NS); send_packet(path, audio_uid, data, bytes, 0);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0);
  bytes = packet(data, 2); memset(data + bytes, 0, sizeof(data) - bytes); send_packet(path, audio_uid, data, sizeof(data), 0);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0);
  bytes = packet(data, 2); send_packet(path, audio_uid, data, bytes, 1);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0 && after.descriptors == before.descriptors);
  bytes = packet(data, 2); send_packet(path, audio_uid, data, bytes, 32);
  after = observe(command[1], reply[0]); CHECK(after.sequence == 1 && after.sample == 0 && after.descriptors == before.descriptors);
  CHECK(rename(path, old) == 0); file = fopen(path, "wb"); CHECK(file);
  CHECK(fwrite("replacement", 1, 11, file) == 11); CHECK(fclose(file) == 0);
  ready = 'X'; write_exact(command[1], &ready, 1); close(command[1]); close(reply[0]);
  CHECK(waitpid(server, &status, 0) == server && WIFEXITED(status) && WEXITSTATUS(status) == 0);
  CHECK(lstat(path, &node) == 0 && S_ISREG(node.st_mode) && node.st_uid == 0);
  CHECK(unlink(path) == 0 && unlink(old) == 0 && rmdir(directory) == 0 && rmdir(root) == 0);
  printf("late-speech-socket: %u parent checks; real UID admission, ancillary FD reaping, exact replacement-safe cleanup passed\n", checks);
#ifdef SPEECH_RESERVE_NS
  printf("explicit20ms voice-only prime; original credential/expiry gates unchanged\n");
#endif
  return 0;
}

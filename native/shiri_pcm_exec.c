/* SPDX-License-Identifier: MIT
 * Unprivileged launcher: deny card-wide ALSA control mutations before exec.
 * Exact DeviceAllow must separately admit only pinned control + playback nodes.
 * ALSA 1.2.6.1 pcm_hw.c opens control even for integer card configuration, using
 * PVERSION and file-local PCM_PREFER_SUBDEVICE. No mixer operation is needed by
 * Shiri's per-session software-volume OwnTone backend.
 *
 * CLI: shiri-pcm-exec -- /absolute/root-owned/daemon ARG...
 * Supported native Linux ABIs: little-endian x86_64 and aarch64.
 * Filter inheritance, NNP, TSYNC, compat/x32 and io_uring fences are mandatory.
 * Primary ABI: Linux v5.15 include/uapi/linux/seccomp.h and sound/asound.h.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define SHIRI_SECCOMP_ALLOW UINT32_C(0x7fff0000)
#define SHIRI_SECCOMP_KILL UINT32_C(0x80000000)
#define SHIRI_SECCOMP_DENY (UINT32_C(0x00050000) | 1u)
#define SHIRI_SECCOMP_MAX 24u

struct shiri_filter_instruction {
  uint16_t code;
  uint8_t yes, no;
  uint32_t value;
};

struct shiri_filter_parameters {
  uint32_t architecture, ioctl_number, ring_setup, ring_enter, ring_register;
  uint32_t version_request, prefer_request;
  uint32_t architecture_offset, number_offset, request_low_offset;
};

static size_t
shiri_pcm_filter(struct shiri_filter_parameters parameters,
                 struct shiri_filter_instruction instructions[SHIRI_SECCOMP_MAX])
{
  size_t count = 0, x32, setup, enter, registration, ioctl, version, prefer, family, deny, allow;
  memset(instructions, 0, SHIRI_SECCOMP_MAX * sizeof(*instructions));
#define EMIT(c, y, n, v) do { \
  instructions[count++] = (struct shiri_filter_instruction){c, y, n, v}; \
} while (0)
  EMIT(0x20, 0, 0, parameters.architecture_offset); /* LD_W_ABS */
  EMIT(0x15, 1, 0, parameters.architecture); /* native ABI or kill */
  EMIT(0x06, 0, 0, SHIRI_SECCOMP_KILL);
  EMIT(0x20, 0, 0, parameters.number_offset);
  x32 = count; EMIT(0x45, 0, 0, UINT32_C(0x40000000)); /* JSET x32 bit */
  setup = count; EMIT(0x15, 0, 0, parameters.ring_setup);
  enter = count; EMIT(0x15, 0, 0, parameters.ring_enter);
  registration = count; EMIT(0x15, 0, 0, parameters.ring_register);
  ioctl = count; EMIT(0x15, 0, 0, parameters.ioctl_number);
  EMIT(0x06, 0, 0, SHIRI_SECCOMP_ALLOW);
  instructions[ioctl].yes = (uint8_t)(count - ioctl - 1);
  EMIT(0x20, 0, 0, parameters.request_low_offset);
  version = count; EMIT(0x15, 0, 0, parameters.version_request);
  prefer = count; EMIT(0x15, 0, 0, parameters.prefer_request);
  EMIT(0x54, 0, 0, UINT32_C(0x0000ff00)); /* AND ioctl type byte */
  family = count; EMIT(0x15, 0, 0, UINT32_C(0x00005500)); /* ALSA control 'U' */
  deny = count; EMIT(0x06, 0, 0, SHIRI_SECCOMP_DENY);
  allow = count; EMIT(0x06, 0, 0, SHIRI_SECCOMP_ALLOW);
  instructions[x32].yes = (uint8_t)(deny - x32 - 1);
  instructions[setup].yes = (uint8_t)(deny - setup - 1);
  instructions[enter].yes = (uint8_t)(deny - enter - 1);
  instructions[registration].yes = (uint8_t)(deny - registration - 1);
  instructions[version].yes = (uint8_t)(allow - version - 1);
  instructions[prefer].yes = (uint8_t)(allow - prefer - 1);
  instructions[family].yes = (uint8_t)(deny - family - 1);
  instructions[family].no = (uint8_t)(allow - family - 1);
#undef EMIT
  return count;
}

static int
shiri_pcm_arguments(int count, char **arguments)
{
  size_t total = 0, length;
  int index;
  if (count < 1 || count > 128 || !arguments || !arguments[0] || arguments[0][0] != '/')
    return 0;
  for (index = 0; index < count; ++index) {
    if (!arguments[index]) return 0;
    length = strnlen(arguments[index], 4097);
    if (length > 4096 || (index == 0 && !length) || total > 65536 - length - 1)
      return 0;
    total += length + 1;
  }
  return 1;
}

#if defined(__linux__) && !defined(SHIRI_PCM_PORTABLE_TEST)
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/time.h>
#include <time.h>
#include <sound/asound.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "PCM ioctl guard requires a reviewed native little-endian Linux ABI"
#elif defined(__x86_64__)
#define SHIRI_NATIVE_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define SHIRI_NATIVE_ARCH AUDIT_ARCH_AARCH64
#else
#error "PCM ioctl guard requires reviewed native x86_64 or aarch64 Linux"
#endif

_Static_assert(SECCOMP_RET_ALLOW == SHIRI_SECCOMP_ALLOW, "seccomp allow ABI");
_Static_assert(SECCOMP_RET_KILL_PROCESS == SHIRI_SECCOMP_KILL, "seccomp kill ABI");
_Static_assert((SECCOMP_RET_ERRNO | EPERM) == SHIRI_SECCOMP_DENY, "seccomp denial ABI");
_Static_assert(_IOC_TYPE(SNDRV_CTL_IOCTL_PVERSION) == 'U', "ALSA control version ABI");
_Static_assert(_IOC_TYPE(SNDRV_CTL_IOCTL_PCM_PREFER_SUBDEVICE) == 'U', "ALSA PCM preference ABI");

static int
install_filter(void)
{
  const struct shiri_filter_parameters parameters = {
    SHIRI_NATIVE_ARCH, __NR_ioctl, __NR_io_uring_setup, __NR_io_uring_enter, __NR_io_uring_register,
    SNDRV_CTL_IOCTL_PVERSION, SNDRV_CTL_IOCTL_PCM_PREFER_SUBDEVICE,
    offsetof(struct seccomp_data, arch), offsetof(struct seccomp_data, nr),
    offsetof(struct seccomp_data, args[1])
  };
  struct shiri_filter_instruction emitted[SHIRI_SECCOMP_MAX];
  struct sock_filter instructions[SHIRI_SECCOMP_MAX] = {0};
  struct sock_fprog program;
  size_t index, count = shiri_pcm_filter(parameters, emitted);
  for (index = 0; index < count; ++index) {
    instructions[index].code = emitted[index].code;
    instructions[index].jt = emitted[index].yes;
    instructions[index].jf = emitted[index].no;
    instructions[index].k = emitted[index].value;
  }
  program.len = (unsigned short)count;
  program.filter = instructions;
  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) return -1;
  if (syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC, &program) != 0)
    return -1;
  return 0;
}

static int
unprivileged(void)
{
  struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
  struct __user_cap_data_struct data[2] = {{0}};
  uid_t real, effective, saved;
  gid_t real_group, effective_group, saved_group;
  if (getresuid(&real, &effective, &saved) || getresgid(&real_group, &effective_group, &saved_group) ||
      !effective || !effective_group || real != effective || saved != effective ||
      real_group != effective_group || saved_group != effective_group ||
      syscall(SYS_capget, &header, data)) return 0;
  return !(data[0].effective | data[0].permitted | data[0].inheritable |
           data[1].effective | data[1].permitted | data[1].inheritable);
}

int
main(int argc, char **argv, char **environment)
{
  int descriptor;
  struct stat info;
  if (argc < 3 || strcmp(argv[1], "--") || !shiri_pcm_arguments(argc - 2, argv + 2)) {
    fputs("Use shiri-pcm-exec -- /absolute/root-owned/daemon ARG...\n", stderr); return 2;
  }
  if (!unprivileged()) {
    fputs("PCM ioctl guard requires the already-unprivileged, capability-free output identity.\n", stderr); return 1;
  }
  descriptor = open(argv[2], O_PATH | O_CLOEXEC | O_NOFOLLOW);
  if (descriptor < 0 || fstat(descriptor, &info) || !S_ISREG(info.st_mode) ||
      info.st_uid != 0 || info.st_nlink != 1 || (info.st_mode & 06022) || !(info.st_mode & 0111)) {
    if (descriptor >= 0) close(descriptor);
    fputs("PCM ioctl guard requires an immutable root-owned regular daemon executable.\n", stderr); return 1;
  }
  if (install_filter()) {
    close(descriptor);
    fputs("PCM ioctl guard could not install the mandatory inherited seccomp filter.\n", stderr); return 1;
  }
  (void)syscall(SYS_execveat, descriptor, "", argv + 2, environment, AT_EMPTY_PATH);
  close(descriptor);
  fputs("PCM ioctl guard could not execute the admitted daemon.\n", stderr);
  return 1;
}
#endif

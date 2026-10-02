/* Opt-in Linux probe: execute only behind shiri-pcm-exec, on an explicitly
 * reserved Loopback playback endpoint. Never writes an ALSA mixer element.
 * Unprotected denial probes use NULL arguments, so a missing filter fails the
 * test before PCM playback and cannot mutate card controls.
 * Compile: cc -std=c11 -Wall -Wextra -Werror -O2 probe_pcm_exec.c pcm_exec_requests.c -lasound -o PROBE
 * Run unprivileged: shiri-pcm-exec -- PROBE /dev/snd/controlCN shiri_test_hw N 1 7
 */
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include "pcm_exec_requests.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static unsigned int checks;

static void
require(int okay, const char *message)
{
  if (!okay) {
    fprintf(stderr, "PCM ioctl enforcement check failed: %s\n", message);
    exit(1);
  }
  ++checks;
}

static void
deny_ioctl(int descriptor, unsigned long request)
{
  errno = 0;
  require(ioctl(descriptor, request, NULL) == -1 && errno == EPERM,
          "card-wide ioctl was not denied by the inherited filter");
}

static unsigned int
number(const char *value)
{
  char *end;
  unsigned long parsed;
  errno = 0;
  parsed = strtoul(value, &end, 10);
  require(*value && !*end && !errno && parsed <= 255 && value[0] != '-', "invalid expected endpoint index");
  return (unsigned int)parsed;
}

int
main(int argc, char **argv)
{
  int descriptor, version = 0, preference, result, status;
  unsigned int card, device, subdevice, block;
  pid_t child;
  snd_pcm_t *pcm = NULL;
  snd_pcm_info_t *info;
  int16_t samples[480 * 2] = {0};
  alarm(8);
  require(geteuid() != 0, "probe must run with the unprivileged output identity");
  require(prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) == 1, "no-new-privileges was not inherited");
  require(prctl(PR_GET_SECCOMP, 0, 0, 0, 0) == 2, "seccomp mode was not inherited");
  if (argc == 3 && !strcmp(argv[1], "--child")) {
    descriptor = (int)number(argv[2]);
    deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_WRITE));
    deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_TLV_WRITE));
    return 0;
  }
  require(argc == 6, "expected CONTROL PCM CARD DEVICE SUBDEVICE");
  card = number(argv[3]); device = number(argv[4]); subdevice = number(argv[5]);
  descriptor = open(argv[1], O_RDWR | O_NOFOLLOW);
  require(descriptor >= 0, "explicit control endpoint was not admitted");
  require(ioctl(descriptor, shiri_control_request(SHIRI_PCM_VERSION), &version) == 0 && version > 0,
          "required control PVERSION was denied");
  preference = (int)subdevice;
  require(ioctl(descriptor, shiri_control_request(SHIRI_PCM_PREFER), &preference) == 0,
          "file-local PCM subdevice preference was denied");
  deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_WRITE));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_TLV_WRITE));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_TLV_COMMAND));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_ADD));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_REMOVE));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_POWER));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_CTL_CARD_INFO));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_PCM_VERSION) ^ (1ul << _IOC_SIZESHIFT));
  deny_ioctl(descriptor, shiri_control_request(SHIRI_PCM_PREFER) ^ (1ul << _IOC_DIRSHIFT));
  require(syscall(SYS_io_uring_setup, 1, NULL) == -1 && errno == EPERM, "io_uring setup bypass was not denied");
  require(syscall(SYS_io_uring_enter, -1, 0, 0, 0, NULL, 0) == -1 && errno == EPERM, "io_uring enter bypass was not denied");
  require(syscall(SYS_io_uring_register, -1, 0, NULL, 0) == -1 && errno == EPERM, "io_uring register bypass was not denied");
  require(syscall(SYS_getpid | 0x40000000u) == -1 && errno == EPERM, "x32 syscall namespace was not denied");
  child = fork();
  require(child >= 0, "cannot test filter inheritance");
  if (!child) {
    char fd_text[32];
    snprintf(fd_text, sizeof(fd_text), "%d", descriptor);
    execl(argv[0], argv[0], "--child", fd_text, (char *)NULL);
    _exit(1);
  }
  require(waitpid(child, &status, 0) == child && status == 0, "filter did not persist through fork+exec");
  close(descriptor);
  result = snd_pcm_open(&pcm, argv[2], SND_PCM_STREAM_PLAYBACK, 0);
  require(result == 0 && pcm, "ALSA PCM open failed behind control ioctl filter");
  snd_pcm_info_alloca(&info);
  require(!snd_pcm_info(pcm, info), "cannot inspect opened PCM");
  require(snd_pcm_info_get_card(info) == (int)card && snd_pcm_info_get_device(info) == device &&
          snd_pcm_info_get_subdevice(info) == subdevice && snd_pcm_info_get_stream(info) == SND_PCM_STREAM_PLAYBACK,
          "opened PCM does not match the explicitly reserved endpoint");
  require(!snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED, 2, 48000, 1, 100000),
          "PCM hardware parameters failed under filter");
  for (block = 0; block < 5; ++block)
    require(snd_pcm_writei(pcm, samples, 480) == 480, "PCM write did not succeed under filter");
  require(!snd_pcm_drop(pcm), "PCM stop failed");
  require(!snd_pcm_close(pcm), "PCM close failed");
  printf("{\"ok\":true,\"uid\":%u,\"checks\":%u,\"card\":%u,\"device\":%u,\"subdevice\":%u,"
         "\"control_mutations_denied_before_pcm\":true,\"inherited_exec\":true,\"frames_written\":2400}\n",
         (unsigned int)geteuid(), checks, card, device, subdevice);
  return 0;
}

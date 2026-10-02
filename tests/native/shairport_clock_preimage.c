/* Private timed PCM ingress. OwnTone retains its existing output scheduler. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "audio.h"
#include "common.h"
#include "shiri_pcm.h"
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static struct shiri_pcm_packet state;
static char *socket_path;
static int peer_uid = -1, fd = -1, connection = -1, gap;
static unsigned int configured;

static uint64_t monotonic_ns(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts)) return 0;
  return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}
static int random_session(uint8_t id[16]) {
  int random_fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  size_t filled = 0;
  if (random_fd < 0) return -1;
  while (filled < 16) {
    ssize_t n = read(random_fd, id + filled, 16 - filled);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) { close(random_fd); return -1; }
    filled += (size_t)n;
  }
  close(random_fd);
  id[6] = (id[6] & 15u) | 64u;
  id[8] = (id[8] & 63u) | 128u;
  return 0;
}
static void parse_group(const char *text, uint8_t id[16]) {
  unsigned int i = 0, half = 0, value = 0;
  memset(id, 0, 16);
  if (!text) return;
  for (; *text; text++) {
    unsigned int n;
    if (*text == '-') continue;
    if (*text >= '0' && *text <= '9') n = (unsigned int)(*text - '0');
    else if (*text >= 'a' && *text <= 'f') n = (unsigned int)(*text - 'a') + 10u;
    else if (*text >= 'A' && *text <= 'F') n = (unsigned int)(*text - 'A') + 10u;
    else goto bad;
    if (i >= 16) goto bad;
    if (!half) { value = n << 4; half = 1; }
    else { id[i++] = (uint8_t)(value | n); half = 0; }
  }
  if (i == 16 && !half) return;
bad:
  memset(id, 0, 16);
}
static void close_route(void) {
  if (fd >= 0) close(fd);
  fd = -1;
}
static int send_control(uint8_t kind, uint32_t value, int await_grant) {
  uint8_t header[SHIRI_PCM_HEADER];
  struct shiri_pcm_packet message = state, answer;
  struct pollfd pfd;
  ssize_t n;
  message.kind = kind;
  message.frames = value;
  message.payload = 0;
  message.presentation = 0;
  shiri_pcm_encode(header, &message);
  pfd.fd = fd; pfd.events = POLLOUT;
  if (fd < 0 || poll(&pfd, 1, 300) != 1 || !(pfd.revents & POLLOUT)) return -1;
  n = send(fd, header, sizeof(header), MSG_DONTWAIT | MSG_NOSIGNAL);
  if (n != (ssize_t)sizeof(header)) return -1;
  if (!await_grant) return 0;
  /* The worker's source-only flush barrier is bounded at five seconds. */
  pfd.events = POLLIN;
  if (poll(&pfd, 1, 6000) != 1 || !(pfd.revents & POLLIN)) return -1;
  n = recv(fd, header, sizeof(header), MSG_DONTWAIT | MSG_TRUNC);
  if (n != (ssize_t)sizeof(header) || shiri_pcm_decode(&answer, header, sizeof(header)) ||
      answer.kind != SHIRI_GRANT || memcmp(answer.session, state.session, 16) ||
      answer.generation != state.generation || shiri_zero(answer.incarnation, 16) || !answer.epoch)
    return -1;
  if (kind != SHIRI_BEGIN &&
      (memcmp(answer.incarnation, state.incarnation, 16) || answer.epoch != state.epoch)) return -1;
  memcpy(state.incarnation, answer.incarnation, 16);
  state.epoch = answer.epoch;
  return 0;
}
static unsigned int percent(double volume) {
  if (!isfinite(volume) || volume <= -30.0) return 0;
  if (volume >= 0.0) return 100;
  return (unsigned int)lround((volume + 30.0) * 100.0 / 30.0);
}
static void session_begin(int number, const char *group, int leader, int ap2, double volume) {
  struct sockaddr_un address;
  int size = 16384, old_cancel;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  close_route();
  memset(&state, 0, sizeof(state));
  state.kind = SHIRI_BEGIN; state.clock = SHIRI_RAW; state.generation = 1;
  state.flags = (ap2 ? SHIRI_PCM_AIRPLAY2 : 0) | (leader ? SHIRI_PCM_GROUP_LEADER : 0);
  parse_group(group, state.group);
  connection = number; gap = 0;
  if (random_session(state.session)) goto fail;
  fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
  if (fd < 0) goto fail;
  memset(&address, 0, sizeof(address)); address.sun_family = AF_UNIX;
  if (strlen(socket_path) >= sizeof(address.sun_path)) goto fail;
  strcpy(address.sun_path, socket_path);
  if (connect(fd, (struct sockaddr *)&address, sizeof(address))) goto fail;
#ifdef SO_PEERCRED
  {
    struct ucred credential;
    socklen_t length = sizeof(credential);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credential, &length) ||
        length != sizeof(credential) || credential.uid != (uid_t)peer_uid) goto fail;
  }
#else
  goto fail; /* This backend requires Linux peer credentials. */
#endif
  if (setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &size, sizeof(size))) goto fail;
  if (send_control(SHIRI_BEGIN, 0, 1) || send_control(SHIRI_VOLUME, percent(volume), 0)) goto fail;
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
  return;
fail:
  warn("Shiri native PCM admission failed; this exact producer remains fenced.");
  close_route();
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
}
static void session_group(int number, const char *group, int leader) {
  int old_cancel;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  if (number == connection && fd >= 0) {
    parse_group(group, state.group);
    state.flags = (state.flags & ~SHIRI_PCM_GROUP_LEADER) | (leader ? SHIRI_PCM_GROUP_LEADER : 0);
  }
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
}
static void session_volume(int number, double volume) {
  int old_cancel;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  if (number == connection && fd >= 0 && send_control(SHIRI_VOLUME, percent(volume), 0))
    close_route();
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
}
static int play_native(int number, void *pcm, int samples, int sample_type, uint32_t rtp, uint64_t playtime) {
  uint8_t message[SHIRI_PCM_HEADER + SHIRI_PCM_MAX_PAYLOAD];
  struct shiri_pcm_packet packet;
  ssize_t n;
  int old_cancel, ret = 0;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  if (number != connection) { ret = ESTALE; goto done; }
  if (samples <= 0 || samples > (int)SHIRI_PCM_MAX_FRAMES || !configured || !pcm) { ret = EINVAL; goto done; }
  if (fd < 0) { ret = EPIPE; goto done; }
  state.sequence++;
  if (sample_type != play_samples_are_timed || !playtime) {
    /* Native lead-in/missing-frame silence has no anchor; don't invent one. */
    state.frame_index += (uint64_t)samples; gap = 1; goto done;
  }
  packet = state;
  packet.kind = SHIRI_PCM; packet.frames = (uint32_t)samples; packet.payload = (uint32_t)samples * 4u;
  packet.rtp = rtp; packet.presentation = playtime;
  packet.flags |= gap ? SHIRI_PCM_GAP : 0;
  packet.mono_before = monotonic_ns();
  packet.raw_sample = get_absolute_time_in_ns();
  packet.mono_after = monotonic_ns();
  shiri_pcm_encode(message, &packet);
  memcpy(message + SHIRI_PCM_HEADER, pcm, packet.payload);
  n = send(fd, message, SHIRI_PCM_HEADER + packet.payload, MSG_DONTWAIT | MSG_NOSIGNAL);
  state.frame_index += (uint64_t)samples;
  if (n == (ssize_t)(SHIRI_PCM_HEADER + packet.payload)) gap = 0;
  else if (n < 0 && (errno == EAGAIN || errno == ENOBUFS)) gap = 1;
  else { ret = EPIPE; close_route(); }
done:
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
  return ret;
}
static void flush_native(int number) {
  int old_cancel;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  if (number == connection && fd >= 0) {
    if (state.generation >= INT64_MAX) close_route();
    else {
      state.generation++; state.sequence = state.frame_index = 0; gap = 0;
      if (send_control(SHIRI_FLUSH, 0, 1)) close_route();
    }
  }
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
}
static void session_end(int number) {
  int old_cancel;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  if (number == connection) {
    if (fd >= 0) send_control(SHIRI_END, 0, 0);
    close_route(); connection = -1;
  }
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
}
static void flush(void) { flush_native(connection); }
static void stop(void) { session_end(connection); }
static int session_retired(int number) {
  uint8_t byte;
  int old_cancel, retired;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  retired = number != connection || fd < 0;
  if (!retired) {
    ssize_t n = recv(fd, &byte, 1, MSG_DONTWAIT | MSG_PEEK);
    retired = n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR);
  }
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
  return retired;
}
static int init(int argc, char **argv) {
  const char *path;
  (void)argv;
  if ((argc < -1 || argc > 0) || !config.cfg || !config_lookup_non_empty_string(config.cfg, "shiri.socket", &path) ||
      !config_lookup_int(config.cfg, "shiri.peer_uid", &peer_uid) || peer_uid < 0)
    die("Shiri backend requires a private socket and exact peer_uid.");
  socket_path = strdup(path);
  if (!socket_path || strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path))
    die("Shiri native PCM socket path is too long.");
  config.audio_backend_buffer_desired_length = 0.15;
  config.audio_backend_latency_offset = 0;
  parse_audio_options("shiri", (1u << SPS_FORMAT_S16_LE), (1u << SPS_RATE_48000), (1u << 2));
  return 0;
}
static int32_t get_configuration(unsigned int channels, unsigned int rate, unsigned int format) {
  (void)channels; (void)rate; (void)format;
  return search_for_suitable_configuration(2, 48000, SPS_FORMAT_S16_LE, NULL);
}
static int configure(int32_t format, char **channel_map) {
  if (channel_map) *channel_map = NULL;
  configured = CHANNELS_FROM_ENCODED_FORMAT(format) == 2 && RATE_FROM_ENCODED_FORMAT(format) == 48000 &&
               FORMAT_FROM_ENCODED_FORMAT(format) == SPS_FORMAT_S16_LE;
  return configured ? 0 : EINVAL;
}
static void deinit(void) { stop(); free(socket_path); socket_path = NULL; }
audio_output audio_shiri = {.name="shiri", .init=init, .deinit=deinit,
  .get_configuration=get_configuration, .configure=configure,
  .session_begin=session_begin, .session_volume=session_volume, .session_group=session_group,
  .play_native=play_native, .session_end=session_end, .session_retired=session_retired,
  .flush_native=flush_native, .stop=stop, .flush=flush};

/* SPDX-License-Identifier: GPL-2.0-or-later
 * Real private-pipe input, actual output delay guard and timestamp assignment.
 * No ALSA library/device or network is used by this scheduling regression. */
#include "input_fixture.inc"

static unsigned invalid_offsets;
#undef DPRINTF
#define DPRINTF(...) do { invalid_offsets++; } while (0)
struct output_device { int offset_ms; const char *name; };
struct alsa_session { uint64_t delay_ms; };
struct alsa_playback_session { struct timespec stamp_pts; };

static uint64_t session_delay(int offset) {
  struct output_device record = {.offset_ms=offset,.name="private scheduling fixture"};
  struct output_device *device=&record;
  struct alsa_session session={0}, *as=&session;
#include "alsa_delay.inc"
  return as->delay_ms;
}
/* Conventional timespec normalization is plumbing, not an output scheduler. */
static struct timespec timespec_add(struct timespec a, struct timespec b) {
  a.tv_sec+=b.tv_sec; a.tv_nsec+=b.tv_nsec;
  if(a.tv_nsec>=1000000000L){a.tv_sec++;a.tv_nsec-=1000000000L;}
  return a;
}
static uint64_t playback_timestamp(struct timespec pts, uint64_t delay) {
  struct alsa_session session={.delay_ms=delay}, *as=&session;
  struct alsa_playback_session playback={0}, *pb=&playback;
  struct timespec delay_ts={0};
#include "alsa_stamp.inc"
  return (uint64_t)pb->stamp_pts.tv_sec*UINT64_C(1000000000)+(uint64_t)pb->stamp_pts.tv_nsec;
}

int main(int argc, char **argv) {
  assert(argc==2); char *end=NULL;
  outputs_buffer_duration_ms=strtoull(argv[1],&end,10);
  assert(end&&!*end&&outputs_buffer_duration_ms>0&&outputs_buffer_duration_ms<=60000);
  pthread_mutex_init(&input_buffer.mutex,NULL);pthread_cond_init(&input_buffer.cond,NULL);
  struct shiri_pcm_owner owner={.epoch=1,.generation=1};
  memset(owner.incarnation,1,16);memset(owner.session,2,16);
  assert(!input_pipe_shiri_seal(1)&&!input_pipe_shiri_arm(&owner,1));
  int fd[2];assert(!pipe(fd)&&!fcntl(fd[0],F_SETFL,O_NONBLOCK));
  struct evbuffer framed={0}, pcm={0};
  struct pipe private_pipe={.fd=fd[0],.framed=&framed,.is_autostarted=true};
  struct input_source source={.input_ctx=&private_pipe,.evbuf=&pcm,.quality={48000,16,2}};
  shiri_source=&source;
  /* Envelope P is the shared native presentation plus Shiri's common 4s relay. */
  struct shiri_pcm_packet packet={.kind=SHIRI_PCM,.clock=SHIRI_MONOTONIC,.epoch=1,.generation=1,
                                 .sequence=1,.presentation=now_ns()+UINT64_C(4000000000)};
  memcpy(packet.incarnation,owner.incarnation,16);memcpy(packet.session,owner.session,16);
  enqueue(fd[1],&packet);assert(!play_framed(&source)&&accepts==1&&!errors);
  uint64_t accepted=(uint64_t)accepted_timestamp.tv_sec*UINT64_C(1000000000)+(uint64_t)accepted_timestamp.tv_nsec;
  assert(accepted==packet.presentation-outputs_buffer_duration_ms*UINT64_C(1000000));
  unsigned mismatches=0;
  for(int offset=-2000;offset<=2000;offset++) {
    uint64_t expected=offset<0 ? packet.presentation-(uint64_t)(-(int64_t)offset)*UINT64_C(1000000)
                               : packet.presentation+(uint64_t)offset*UINT64_C(1000000);
    uint64_t stamp=playback_timestamp(accepted_timestamp,session_delay(offset));
    if(stamp!=expected)mismatches++;
  }
  assert(mismatches==invalid_offsets);
  close(fd[0]);close(fd[1]);pthread_cond_destroy(&input_buffer.cond);pthread_mutex_destroy(&input_buffer.mutex);
  printf("{\"ok\":%s,\"buffer_ms\":%llu,\"offset_cases\":4001,\"ignored_offsets\":%u,"
         "\"horizon_mismatches\":%u,\"input_buffer_subtraction_verified\":true}\n",
         mismatches?"false":"true",(unsigned long long)outputs_buffer_duration_ms,invalid_offsets,mismatches);
  return mismatches?1:0;
}

/* Tests the actual patched final-write and, optionally, control functions. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <limits.h>
#include <errno.h>
#include "pcm_volume.h"

typedef long snd_pcm_sframes_t;
typedef struct { size_t frame_bytes; } snd_pcm_t;
struct media_quality { int bits_per_sample; int channels; };
struct alsa_mixer { int unused; };
struct alsa_extra {
  const char *card_name, *mixer_name, *mixer_device_name;
  bool software_volume;
};
struct alsa_playback_session;
struct alsa_session {
  bool software_volume;
  int volume, state, callback_id;
  uint64_t device_id, delay_ms;
  const char *devname, *mixer_name, *mixer_device_name;
  struct alsa_mixer mixer;
  struct alsa_session *next;
  struct alsa_playback_session *pb;
};
struct alsa_playback_session {
  snd_pcm_t *pcm;
  struct alsa_session *session;
  uint8_t *volume_buf;
  size_t volume_bufsize;
  struct media_quality quality;
};
struct output_device {
  int volume, offset_ms;
  uint64_t id;
  const char *name;
  struct alsa_extra *extra_device_info;
  struct alsa_session *session;
};
static unsigned writes;
static snd_pcm_sframes_t frames;
static const void *submitted;
static uint8_t written[64];
static bool allocation_failure;

static ssize_t snd_pcm_frames_to_bytes(snd_pcm_t *pcm, snd_pcm_sframes_t count) {
  return (ssize_t)((size_t)count * pcm->frame_bytes);
}
static snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *pcm, const void *buffer, snd_pcm_sframes_t count) {
  assert(count >= 0 && (size_t)count * pcm->frame_bytes <= sizeof(written));
  memcpy(written, buffer, (size_t)count * pcm->frame_bytes);
  writes++; frames = count; submitted = buffer;
  return count;
}
static void *test_realloc(void *pointer, size_t size) {
  return allocation_failure ? NULL : realloc(pointer, size);
}
#define realloc test_realloc
#include "pcm_write.inc"
#undef realloc

#ifdef TEST_ALSA_CONTROL
enum { OUTPUT_STATE_CONNECTED, OUTPUT_STATE_STOPPED };
static struct alsa_session *sessions;
static struct output_device *registered;
static struct media_quality alsa_fallback_quality;
static unsigned mixer_opens, mixer_sets, mixer_closes, status_calls;
static unsigned log_calls;
static bool fail_mixer;
static int outputs_quality_subscribe(struct media_quality *quality) { (void)quality; return 0; }
static void outputs_quality_unsubscribe(struct media_quality *quality) { (void)quality; }
static uint64_t outputs_buffer_duration_ms_get(void) { return 2000; }
static void playback_session_remove_all(struct alsa_session *as) { as->pb = NULL; }
static int mixer_open(struct alsa_mixer *mixer, const char *device, const char *name) {
  (void)mixer; (void)device; (void)name; mixer_opens++; return fail_mixer ? -1 : 0;
}
static int volume_set(struct alsa_mixer *mixer, int volume) {
  (void)mixer; (void)volume; mixer_sets++; return 0;
}
static void mixer_close(struct alsa_mixer *mixer, const char *name) { (void)mixer; (void)name; mixer_closes++; }
static void outputs_device_session_add(uint64_t id, struct alsa_session *as) { assert(id == registered->id); registered->session = as; }
static void alsa_status(struct alsa_session *as) { (void)as; status_calls++; }
#define DPRINTF(...) ((void)log_calls++)
#define CHECK_NULL(category, expression) assert((expression) != NULL)
#include "alsa_control.inc"
#endif

int main(void) {
  snd_pcm_t pcm = {4};
  struct alsa_session session = {.software_volume=true, .volume=50};
  struct alsa_playback_session playback = {.pcm=&pcm, .session=&session, .quality={16,2}};
  uint8_t raw[] = {0x40,0x1F,0xC0,0xE0,0xA0,0x0F,0x60,0xF0}; /* 8000,-8000,4000,-4000 */
  uint8_t original[sizeof(raw)], queued[sizeof(raw)];
  uint8_t eighth[] = {0xE8,0x03,0x18,0xFC,0xF4,0x01,0x0C,0xFE};
  memcpy(original,raw,sizeof(raw)); memcpy(queued,raw,sizeof(raw));
  assert(pcm_write(&playback,raw,2)==2 && frames==2 && writes==1);
  assert(submitted==playback.volume_buf && submitted!=raw);
  assert(memcmp(written,eighth,sizeof(raw))==0 && memcmp(raw,original,sizeof(raw))==0);
  uint8_t *scratch = playback.volume_buf;
  session.volume=100;
  assert(pcm_write(&playback,queued,2)==2 && playback.volume_buf==scratch);
  assert(memcmp(written,original,sizeof(raw))==0 && memcmp(queued,original,sizeof(raw))==0);
  /* A second, old-quality playback session drains with the same current gain. */
  struct alsa_playback_session draining = {.pcm=&pcm, .session=&session, .quality={16,2}};
  session.volume=0;
  assert(pcm_write(&draining,queued,2)==2);
  for(size_t i=0;i<sizeof(raw);i++) assert(written[i]==0);
  assert(memcmp(queued,original,sizeof(raw))==0 && draining.volume_buf!=playback.volume_buf);
  unsigned before=writes;
  assert(pcm_write(&playback,NULL,0)==0 && writes==before);
  assert(pcm_write(&playback,raw,-1)==-EINVAL && writes==before);
  assert(pcm_write(&playback,raw,LONG_MAX)==-EINVAL && writes==before);
  playback.quality.channels=0;
  assert(pcm_write(&playback,raw,2)==-EINVAL && writes==before);
  playback.quality.channels=2; playback.quality.bits_per_sample=8;
  assert(pcm_write(&playback,raw,2)==-EINVAL && writes==before);
  playback.quality.bits_per_sample=16;
  pcm.frame_bytes=6;
  assert(pcm_write(&playback,raw,2)==-EINVAL && writes==before);
  pcm.frame_bytes=4;
  struct alsa_playback_session failing = {.pcm=&pcm, .session=&session, .quality={16,2}};
  allocation_failure=true;
  assert(pcm_write(&failing,raw,2)==-ENOMEM && failing.volume_buf==NULL && writes==before);
  allocation_failure=false;
  session.software_volume=false;
  assert(pcm_write(&playback,raw,2)==2 && submitted==raw && memcmp(written,original,sizeof(raw))==0);
  free(playback.volume_buf); free(draining.volume_buf);

#ifdef TEST_ALSA_CONTROL
  struct alsa_extra extra = {.card_name="exact-room-PCM", .software_volume=true};
  struct output_device device = {.volume=150,.id=1,.name="test",.extra_device_info=&extra};
  registered=&device;
  fail_mixer=true; /* Software mode must succeed even with no hardware mixer. */
  assert(alsa_device_start(&device,7)==1 && device.session->volume==100);
  assert(mixer_opens==0 && mixer_sets==0 && mixer_closes==0 && status_calls==1);
  struct alsa_session *same=device.session;
  device.volume=-50;
  assert(alsa_device_volume_set(&device,8)==1 && device.session==same && same->volume==0);
  assert(mixer_opens==0 && mixer_sets==0 && same->delay_ms==2000 && status_calls==2);
  alsa_session_free(same);
  assert(mixer_closes==0);
  device.session=NULL; sessions=NULL;
  int offsets[] = {-2000, -2001, INT_MIN, 0, 2000, INT_MAX};
  uint64_t delays[] = {0, 2000, 2000, 2000, 4000, UINT64_C(2000) + INT_MAX};
  for(unsigned i=0;i<sizeof(offsets)/sizeof(offsets[0]);i++) {
    device.offset_ms=offsets[i];
    unsigned previous_logs=log_calls;
    same=alsa_session_make(&device,9);
    assert(same && same->delay_ms==delays[i]);
    assert(log_calls==previous_logs + (offsets[i] < -2000));
    alsa_session_free(same);
    device.session=NULL; sessions=NULL;
  }
  device.offset_ms=0;
  extra.software_volume=false;
  assert(alsa_session_make(&device,9)==NULL && mixer_opens==1);
  fail_mixer=false;
  assert(alsa_device_start(&device,10)==1 && mixer_opens==2 && mixer_sets==1);
  device.volume=50;
  assert(alsa_device_volume_set(&device,11)==1 && mixer_sets==2);
  alsa_session_free(device.session);
  assert(mixer_closes==2); /* One failed open cleanup and one normal close. */
#endif
  puts("OwnTone actual final-write/control seam passed: private copies, queued/draining live gain, frames, failures and legacy mixer mode");
  return 0;
}

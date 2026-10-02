/* SPDX-License-Identifier: GPL-2.0-or-later
 * Actual ALSA output functions, scripted driver completions, no devices. */
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <time.h>
#include "pcm_volume.h"

#define DPRINTF(...) do {} while (0)
#define ALSA_ERROR_WRITE -1
#define ALSA_ERROR_UNDERRUN -2
#define ALSA_ERROR_SESSION -3
#define SND_PCM_STATE_RUNNING 3
#define SND_PCM_STATE_DRAINING 5
typedef long snd_pcm_sframes_t;
typedef int snd_pcm_state_t;
struct snd_pcm { unsigned width, identifier; int state; };
typedef struct snd_pcm snd_pcm_t;
struct ringbuffer { uint8_t *buffer; size_t size, write_avail, read_avail, write_pos, read_pos; };
struct media_quality { int sample_rate, bits_per_sample, channels; };
enum output_device_state { OUTPUT_STATE_CONNECTED, OUTPUT_STATE_STREAMING, OUTPUT_STATE_FAILED, OUTPUT_STATE_STOPPED };
enum alsa_sync_state { ALSA_SYNC_OK, ALSA_SYNC_AHEAD, ALSA_SYNC_BEHIND };
struct alsa_session;
struct alsa_playback_session {
  snd_pcm_t *pcm; struct alsa_session *session; uint8_t *volume_buf; size_t volume_bufsize;
  int buffer_nsamp; bool startup_done; uint32_t pos; struct media_quality quality; struct timespec last_pts;
  double *latency_history; int sync_resample_step; struct ringbuffer prebuf;
  struct alsa_playback_session *next;
};
struct alsa_session { bool software_volume; int volume, callback_id; uint64_t device_id;
                     enum output_device_state state; struct alsa_playback_session *pb; };
struct output_device { struct alsa_session *session; };
struct output_data { uint8_t *buffer; size_t bufsize; int samples; struct media_quality quality; };
struct output_buffer { struct output_data data[3]; struct timespec pts; };
static bool alsa_sync_disable=true;
static long plan[64], availability=4096;
static unsigned plan_size, plan_index, driver_calls, closed, drains, callbacks, unsubscribes;
static int avail_error, drain_error;
static uint8_t committed[1048576];
static unsigned committed_device[262144];
static size_t committed_bytes, committed_frames;

static ssize_t snd_pcm_frames_to_bytes(snd_pcm_t *pcm, long frames) { return frames*(long)pcm->width; }
static long snd_pcm_bytes_to_frames(snd_pcm_t *pcm, ssize_t bytes) { return bytes/(long)pcm->width; }
static long snd_pcm_writei(snd_pcm_t *pcm, const void *data, long frames) {
  driver_calls++;
  long result=plan_index<plan_size?plan[plan_index++]:frames;
  if(result>0&&result<=frames) {
    size_t bytes=(size_t)result*pcm->width;
    assert(committed_bytes+bytes<=sizeof(committed)&&committed_frames+(size_t)result<=262144);
    memcpy(committed+committed_bytes,data,bytes);committed_bytes+=bytes;
    for(long i=0;i<result;i++)committed_device[committed_frames++]=pcm->identifier;
  }
  return result;
}
static int snd_pcm_avail_delay(snd_pcm_t *pcm, long *avail, long *delay) {
  (void)pcm; *avail=availability; *delay=0; return avail_error;
}
static int snd_pcm_state(snd_pcm_t *pcm) { return pcm->state; }
static int snd_pcm_drain(snd_pcm_t *pcm) { drains++;if(!drain_error)pcm->state=SND_PCM_STATE_DRAINING;return drain_error; }
static int quality_is_equal(const struct media_quality *a,const struct media_quality *b) {
  return a->sample_rate==b->sample_rate&&a->bits_per_sample==b->bits_per_sample&&a->channels==b->channels;
}
static enum alsa_sync_state sync_check(double *drift,double *latency,struct alsa_playback_session *pb,long delay) {
  (void)drift;(void)latency;(void)pb;(void)delay;assert(false);return ALSA_SYNC_OK;
}
static void sync_correct(struct alsa_playback_session *pb,double drift,double latency,struct timespec pts,long delay) {
  (void)pb;(void)drift;(void)latency;(void)pts;(void)delay;assert(false);
}
static void pcm_close(snd_pcm_t *pcm) { assert(pcm);closed++;free(pcm); }
static void outputs_quality_unsubscribe(struct media_quality *quality) { assert(quality->channels==2);unsubscribes++; }
static void outputs_cb(int callback,uint64_t device,enum output_device_state state) {
  assert(callback==71&&device==123&&state==OUTPUT_STATE_CONNECTED&&closed>0);callbacks++;
}
static void alsa_session_cleanup(struct alsa_session *as) { (void)as;assert(false); }
/* @ACTUAL_PCM_WRITE@ */
/* @ACTUAL_LIFECYCLE@ */
/* @ACTUAL_QUEUE@ */

static void script(const long *items,unsigned count) {
  assert(count<=64);if(count)memcpy(plan,items,count*sizeof(*items));plan_size=count;plan_index=0;
}
static void reset_observation(void) {
  committed_bytes=committed_frames=driver_calls=drains=callbacks=closed=unsubscribes=0;
  plan_size=plan_index=0;availability=4096;avail_error=drain_error=0;
}
static struct alsa_playback_session *playback(struct alsa_session *as,unsigned bits,unsigned capacity,unsigned identifier) {
  struct alsa_playback_session *pb=calloc(1,sizeof(*pb));assert(pb);
  pb->pcm=calloc(1,sizeof(*pb->pcm));assert(pb->pcm);
  pb->pcm->width=bits/8*2;pb->pcm->identifier=identifier;pb->pcm->state=SND_PCM_STATE_RUNNING;
  pb->session=as;pb->quality=(struct media_quality){48000,(int)bits,2};
  pb->prebuf.size=(size_t)capacity*pb->pcm->width;
  pb->prebuf.buffer=calloc(1,pb->prebuf.size);assert(pb->prebuf.buffer);
  pb->prebuf.write_avail=pb->prebuf.size;pb->latency_history=calloc(1,sizeof(double));assert(pb->latency_history);
  as->software_volume=true;as->volume=100;as->device_id=123;as->state=OUTPUT_STATE_STREAMING;as->pb=pb;
  return pb;
}
static uint8_t *samples(unsigned first,unsigned frames,unsigned bits) {
  unsigned bytes=bits/8;uint8_t *data=malloc((size_t)frames*bytes*2);assert(data);
  for(unsigned i=0;i<frames;i++)for(unsigned channel=0;channel<2;channel++) {
    int32_t value=(int32_t)(first+i+1)*8*(1<<(bits-16));if(channel)value=-value;
    for(unsigned j=0;j<bytes;j++)data[(i*2+channel)*bytes+j]=(uint8_t)((uint32_t)value>>(j*8));
  }
  return data;
}
static struct output_data data(unsigned first,unsigned frames,unsigned bits) {
  return (struct output_data){.buffer=samples(first,frames,bits),.bufsize=(size_t)frames*(bits/8)*2,
                             .samples=(int)frames,.quality={48000,(int)bits,2}};
}
static int write_block(struct alsa_playback_session *pb,struct output_data block) {
  struct output_buffer obuf={.data={block},.pts={10,0}};
  return playback_write(pb,&obuf);
}
static void verify(unsigned frames,unsigned bits,unsigned gain_after,unsigned gain_volume) {
  size_t bytes=(size_t)frames*(bits/8)*2;uint8_t *expected=malloc(bytes);assert(expected);
  assert(gain_volume==50||gain_volume==100);
  /* Independent exact values: fixture samples are divisible by eight, so
   * cubic50% is exactly value/8 without using the production gain function. */
  for(unsigned i=0;i<frames;i++)for(unsigned channel=0;channel<2;channel++) {
    int32_t value=(int32_t)(i+1)*8*(1<<(bits-16));if(channel)value=-value;
    if(i>=gain_after&&gain_volume==50)value/=8;
    for(unsigned j=0;j<bits/8;j++)expected[(i*2+channel)*(bits/8)+j]=(uint8_t)((uint32_t)value>>(j*8));
  }
  assert(committed_bytes==bytes&&committed_frames==frames&&!memcmp(committed,expected,bytes));
  free(expected);
}
static void zero_tick(struct alsa_playback_session *pb) {
  static uint8_t empty_sample;
  struct output_data empty={.buffer=&empty_sample,.quality=pb->quality};assert(!write_block(pb,empty));
}
static void positive_partial(unsigned bits,bool prebuffer) {
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,bits,2048,1);
  struct output_data first=data(0,960,bits),next=data(960,480,bits);
  uint8_t *original=malloc(first.bufsize);assert(original);memcpy(original,first.buffer,first.bufsize);
  if(prebuffer) {pb->buffer_nsamp=960;availability=0;assert(!write_block(pb,first)&&pb->pos==960);availability=960;}
  long partial[]={576,-EAGAIN};script(partial,2);
  assert(!write_block(pb,prebuffer?next:first));
  assert(pb->pos==(prebuffer?1440u:960u));
  if(!prebuffer) {as.volume=50;assert(!write_block(pb,next));}
  else as.volume=50;
  assert(pb->prebuf.read_avail==(prebuffer?864u:864u)*pb->pcm->width);
  assert(!memcmp(first.buffer,original,first.bufsize));
  script(NULL,0);availability=4096;zero_tick(pb);assert(!pb->prebuf.read_avail);
  verify(1440,bits,576,50);assert(pb->pos==1440);
  free(original);free(first.buffer);free(next.buffer);playback_session_remove_all(&as);assert(!as.pb&&closed==1);
}
static void retry_results(void) {
  const long results[]={0,-EAGAIN,-EINTR};
  for(unsigned i=0;i<3;i++)for(unsigned bits=16;bits<=32;bits+=8) {
    reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,bits,2048,1);
    struct output_data first=data(0,960,bits);script(&results[i],1);
    assert(!write_block(pb,first)&&pb->pos==960&&pb->prebuf.read_avail==first.bufsize&&!committed_bytes);
    script(&results[i],1);zero_tick(pb);assert(pb->prebuf.read_avail==first.bufsize&&!committed_bytes);
    script(NULL,0);zero_tick(pb);verify(960,bits,960,100);free(first.buffer);playback_session_remove_all(&as);
  }
}
static void wrapping_and_bounded_work(void) {
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,16,2048,1);
  pb->prebuf.read_pos=pb->prebuf.write_pos=2000*4;
  struct output_data first=data(0,960,16);availability=0;assert(!write_block(pb,first));
  availability=4096;zero_tick(pb);verify(960,16,960,100);assert(driver_calls==2);
  free(first.buffer);playback_session_remove_all(&as);
  reset_observation();pb=playback(&as,16,2048,1);first=data(0,960,16);availability=0;assert(!write_block(pb,first));
  long ones[16];for(unsigned i=0;i<16;i++)ones[i]=1;script(ones,16);availability=4096;zero_tick(pb);
  assert(driver_calls==8&&committed_frames==8&&pb->prebuf.read_avail==952*4);
  script(NULL,0);zero_tick(pb);verify(960,16,960,100);free(first.buffer);playback_session_remove_all(&as);
}
static void failures_are_not_admissions(void) {
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,16,480,1);
  struct output_data first=data(0,960,16);pb->buffer_nsamp=2000;
  assert(write_block(pb,first)==ALSA_ERROR_WRITE&&pb->pos==0&&!pb->prebuf.read_avail&&!committed_frames);
  pb->buffer_nsamp=0;long too_many[]={961};script(too_many,1);availability=4096;
  assert(write_block(pb,first)==ALSA_ERROR_WRITE&&pb->pos==0&&!pb->prebuf.read_avail);
  long fatal[]={-EIO};script(fatal,1);assert(write_block(pb,first)==ALSA_ERROR_WRITE&&pb->pos==0);
  free(first.buffer);playback_session_remove_all(&as);
  reset_observation();pb=playback(&as,16,2048,1);first=data(0,960,16);availability=0;
  assert(!write_block(pb,first)&&pb->pos==960);availability=4096;script(fatal,1);
  static uint8_t dummy;struct output_data empty={.buffer=&dummy,.quality=pb->quality};
  assert(write_block(pb,empty)==ALSA_ERROR_WRITE&&pb->pos==960&&pb->prebuf.read_avail==first.bufsize);
  free(first.buffer);playback_session_remove_all(&as);
}
static void legacy_volume_and_drain_retry(void) {
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,16,2048,1);
  as.software_volume=false;as.volume=0;struct output_data first=data(0,960,16);long partial[]={576};script(partial,1);
  assert(!write_block(pb,first)&&pb->pos==960&&pb->prebuf.read_avail==384*4);
  as.volume=50;script(NULL,0);zero_tick(pb);verify(960,16,960,100);
  drain_error=-EAGAIN;assert(!playback_drain(pb)&&drains==1&&pb->pcm->state==SND_PCM_STATE_RUNNING);
  drain_error=-EINTR;assert(!playback_drain(pb)&&drains==2&&pb->pcm->state==SND_PCM_STATE_RUNNING);
  drain_error=0;assert(!playback_drain(pb)&&drains==3&&pb->pcm->state==SND_PCM_STATE_DRAINING);
  assert(!playback_drain(pb)&&drains==3);
  pb->pcm->state=1;assert(playback_drain(pb)==ALSA_ERROR_SESSION);
  free(first.buffer);playback_session_remove_all(&as);
}
static void converter_drain_and_source_flush(void) {
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *old=playback(&as,24,2048,1);
  struct output_data first=data(0,960,24);availability=0;assert(!write_block(old,first));
  struct alsa_playback_session *current=playback(&as,32,2048,2);old->next=current;as.pb=old;
  old->sync_resample_step=1;long partial[]={576,-EINTR};script(partial,2);availability=960;
  assert(!playback_drain(old)&&old->prebuf.read_avail==384*6&&!drains);
  assert(as.pb==old&&old->next==current&&current->prebuf.read_avail==0);
  avail_error=-EINTR;assert(!playback_drain(old)&&old->prebuf.read_avail==384*6);avail_error=0;
  script(NULL,0);assert(!playback_drain(old)&&!old->prebuf.read_avail);
  verify(960,24,960,100);assert(!playback_drain(old)&&drains==1);
  struct output_device device={.session=&as};assert(alsa_device_flush(&device,71)==1);
  assert(!as.pb&&closed==2&&callbacks==1&&unsubscribes==1&&as.callback_id==-1);
  free(first.buffer);
  /* A pending source tail is discarded, not replayed into a successor PCM. */
  reset_observation();old=playback(&as,16,2048,1);first=data(0,960,16);long blocked[]={-EAGAIN};script(blocked,1);
  assert(!write_block(old,first)&&old->prebuf.read_avail==first.bufsize&&!committed_frames);
  assert(alsa_device_flush(&device,71)==1&&as.pb==NULL&&closed==1&&callbacks==1);
  current=playback(&as,16,2048,2);struct output_data fresh=data(960,480,16);script(NULL,0);
  assert(!write_block(current,fresh)&&committed_frames==480&&committed_bytes==fresh.bufsize);
  assert(!memcmp(committed,fresh.buffer,fresh.bufsize));
  for(unsigned i=0;i<480;i++)assert(committed_device[i]==2);
  free(first.buffer);free(fresh.buffer);playback_session_remove_all(&as);
}
static void startup_is_not_a_wrapping_counter(void) {
  const unsigned horizons[]={12000,108000}; // 250ms and 2250ms at 48kHz.
  for(unsigned i=0;i<2;i++) {
    reset_observation();struct alsa_session as={0};unsigned horizon=horizons[i];
    struct alsa_playback_session *pb=playback(&as,16,horizon+4096,1);pb->buffer_nsamp=(int)horizon;
    struct output_data initial=data(0,horizon,16),block=data(horizon,960,16);
    // The exact initial horizon is still prebuffered; the next block opens the
    // existing driver path and permanently completes this playback's startup.
    assert(!pb->startup_done&&!write_block(pb,initial)&&!driver_calls&&pb->pos==horizon);
    availability=(long)horizon+4096;
    assert(!write_block(pb,block)&&pb->startup_done&&driver_calls>0&&pb->prebuf.read_avail==0);
    reset_observation();pb->pos=UINT32_MAX-255;
    assert(!write_block(pb,block)&&pb->pos==704&&pb->startup_done&&driver_calls==1);
    assert(committed_frames==960&&!pb->prebuf.read_avail);
    // Even when a newly constructed playback first observes a wide position,
    // the widened comparison refuses a false startup caused by addition wrap.
    pb->startup_done=false;pb->pos=UINT32_MAX-255;reset_observation();
    assert(!write_block(pb,block)&&pb->pos==704&&pb->startup_done&&driver_calls==1);
    // A queued tail remains in order through the counter wrap, and a low
    // wrapped position on a following callback does not restart prebuffering.
    reset_observation();pb->pos=UINT32_MAX-255;
    long partial[]={576};script(partial,1);
    assert(!write_block(pb,block)&&pb->pos==704&&pb->prebuf.read_avail==384*4);
    script(NULL,0);struct output_data next=data(horizon+960,480,16);
    assert(!write_block(pb,next)&&pb->pos==1184&&pb->startup_done&&driver_calls==3);
    assert(committed_frames==1440&&!pb->prebuf.read_avail);
    assert(!memcmp(committed,block.buffer,block.bufsize));
    assert(!memcmp(committed+block.bufsize,next.buffer,next.bufsize));
    // Stay on the actual output path throughout a whole startup-horizon's
    // worth of later callbacks, including every low post-wrap counter value.
    for(unsigned tick=0;tick<=horizon/960;tick++) {
      unsigned before=driver_calls;
      assert(!write_block(pb,block)&&driver_calls==before+1&&pb->startup_done&&!pb->prebuf.read_avail);
    }
    free(initial.buffer);free(block.buffer);free(next.buffer);playback_session_remove_all(&as);
  }
  // Invalid input cannot flip startup state or advance admission accounting.
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,16,2048,1);
  struct output_data bad=data(0,960,16);bad.samples=-1;
  assert(write_block(pb,bad)==ALSA_ERROR_WRITE&&!pb->startup_done&&!pb->pos&&!driver_calls);
  free(bad.buffer);
  // A real source flush destroys the flag with the old playback. Its successor
  // starts a new initial horizon rather than inheriting the retired flag.
  pb->startup_done=true;struct output_device device={.session=&as};assert(alsa_device_flush(&device,71)==1&&!as.pb);
  pb=playback(&as,16,2048,2);pb->buffer_nsamp=960;
  struct output_data first=data(0,960,16),second=data(960,480,16);
  assert(!pb->startup_done&&!write_block(pb,first)&&!pb->startup_done&&!driver_calls&&pb->pos==960);
  assert(!write_block(pb,second)&&pb->startup_done&&driver_calls==2&&committed_frames==1440);
  free(first.buffer);free(second.buffer);playback_session_remove_all(&as);
}
static void preimage(void) {
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,16,2048,1);
  struct output_data first=data(0,960,16);long short_write[]={576};script(short_write,1);
  assert(!write_block(pb,first));size_t direct_lost=960-committed_frames-pb->prebuf.read_avail/4;
  playback_session_remove_all(&as);reset_observation();pb=playback(&as,16,2048,1);
  availability=0;assert(!write_block(pb,first));struct output_data next=data(960,480,16);
  availability=960;script(short_write,1);assert(!write_block(pb,next));
  size_t queued_lost=1440-committed_frames-pb->prebuf.read_avail/4;
  assert(direct_lost==384&&queued_lost==384);
  printf("{\"preimage\":true,\"direct_lost_frames\":%zu,\"queued_lost_frames\":%zu}\n",direct_lost,queued_lost);
  free(first.buffer);free(next.buffer);playback_session_remove_all(&as);
}
static void wrap_preimage(void) {
  reset_observation();struct alsa_session as={0};struct alsa_playback_session *pb=playback(&as,16,120000,1);
  // UINT32_MAX-255 is an actually reachable multiple of the 960-frame tick.
  pb->buffer_nsamp=108000;pb->pos=UINT32_MAX-255;
  struct output_data block=data(0,960,16);assert(!write_block(pb,block));
  assert(pb->pos==704&&!driver_calls&&pb->prebuf.read_avail==block.bufsize);
  printf("{\"wrap_preimage\":true,\"reentered_startup\":true,\"driver_calls\":%u,\"queued_frames\":%zu,\"wrapped_position\":%u}\n",
         driver_calls,pb->prebuf.read_avail/4,pb->pos);
  free(block.buffer);playback_session_remove_all(&as);
}
int main(int argc,char **argv) {
  if(argc==2) {
    if(!strcmp(argv[1],"--preimage"))preimage();
    else {assert(!strcmp(argv[1],"--wrap-preimage"));wrap_preimage();}
    return 0;
  }
  assert(argc==1);
  for(unsigned bits=16;bits<=32;bits+=8) {positive_partial(bits,false);positive_partial(bits,true);}
  retry_results();wrapping_and_bounded_work();failures_are_not_admissions();legacy_volume_and_drain_retry();converter_drain_and_source_flush();startup_is_not_a_wrapping_counter();
  puts("Actual ALSA queue/write/drain/source-flush: positive partials, raw final gain, admission counts, zero/EAGAIN/EINTR, converter tails, wrapped queues, bounded work/capacity, startup counter wrap and no successor replay passed");
  return 0;
}

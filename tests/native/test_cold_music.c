/* Exact player/input bodies; local clocks, memory buffers and output seams. */
#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <pthread.h>
#include <time.h>

#define CHECK(x) do { assert(x); ++checks; } while (0)
#define CHECK_NULL(group,x) assert((x)!=NULL)
#define STOB(samples,bits,channels) ((size_t)(samples)*(bits)/8*(channels))
#define BTOS(bytes,bits,channels) ((bytes)/((bits)/8*(channels)))
#define DATA_KIND_PIPE 7
#define DATA_KIND_FILE 1
#define PLAY_STOPPED 0
#define PLAY_PLAYING 1
#define PLAY_PAUSED 2
#define LISTENER_PLAYER 1
#ifndef TIMER_ABSTIME
#define TIMER_ABSTIME 1
struct itimerspec { struct timespec it_interval, it_value; };
#endif

static unsigned checks, output_calls, flush_calls, seek_calls, abort_calls;
static unsigned metadata_calls, quality_calls, start_next_calls, eof_calls, error_calls;
static unsigned timer_arms, timer_stops, source_zero_reads;
static bool timer_active, resume_pending, mutate_gate_on_empty;
static uint64_t now_ns, gate_operation=3;
static int gate_state=1, player_state, player_flush_pending, pb_timer, pb_timer_fd=42;
static uint64_t timer_expirations=1;
static bool fail_clock, fail_timer, pb_write_recovery;
static bool pb_timer_native, pb_timer_native_anchored, pb_timer_native_waiting_for_pcm, pb_timer_speech_only;
static uint64_t pb_timer_native_operation;
static unsigned pb_write_deficit_max=50;
static struct timespec pb_timer_native_anchor, pb_timer_native_wait_until;
static struct timespec shiri_output_end, armed_anchor;
static bool shiri_output_end_valid;
static struct timespec player_tick_interval={0,10000000};
static int timer_absolute;
struct event { int unused; };
static struct event timer_event;
static struct event *pb_timer_ev=&timer_event;
struct media_quality { unsigned sample_rate,bits_per_sample,channels; };
struct input_metadata { int unused; };
struct input_source { int data_kind; bool open; struct timespec sync_ts; };
struct player_source { int data_kind,id; const char *path; uint32_t item_id; struct player_source *next; };
struct db_queue_item { int unused; };
struct output_device { int unused; };
static struct player_source pipe_source={DATA_KIND_PIPE,25,"silent-framed-fixture",25,NULL};
static uint8_t player_buffer[1920];
static struct {
  struct media_quality quality;
  uint8_t *buffer;
  size_t bufsize,read_deficit,read_deficit_max;
  uint64_t pos;
  struct timespec pts,start_ts;
  struct player_source *reading_now,*playing_now,*source_list;
} pb_session;
typedef void (*input_cb)(void);
/* @INPUT_FLAGS@ */
/* @INPUT_THRESHOLDS@ */

/* A bounded memory evbuffer models only libevent's local buffer operations. */
struct evbuffer { uint8_t data[1200000]; size_t length; };
static struct evbuffer main_buffer, write_buffer;
static size_t evbuffer_get_length(struct evbuffer *b) { return b->length; }
static int evbuffer_add_buffer(struct evbuffer *dest,struct evbuffer *source) {
  assert(dest->length+source->length<=sizeof(dest->data));
  memcpy(dest->data+dest->length,source->data,source->length);
  dest->length+=source->length; source->length=0; return 0;
}
static int evbuffer_remove(struct evbuffer *b,void *dest,size_t size) {
  size_t take=size<b->length?size:b->length;
  if(!take) { ++source_zero_reads; if(mutate_gate_on_empty) { ++gate_operation; mutate_gate_on_empty=false; } }
  if(take) memcpy(dest,b->data,take);
  b->length-=take; memmove(b->data,b->data+take,b->length); return (int)take;
}
/* @INPUT_DEFINITIONS@ */
static struct input_buffer input_buffer;
static struct input_source input_now_reading;
static void *cfg;
static bool framed_configuration=true;
static void *cfg_getsec(void *c,const char *s) { (void)c;(void)s;return cfg; }
static bool cfg_getbool(void *c,const char *s) { (void)c;assert(!strcmp(s,"pipe_framed"));return framed_configuration; }
static bool quality_is_equal(struct media_quality *a,struct media_quality *b) { return !memcmp(a,b,sizeof(*a)); }
static struct input_metadata *metadata_get(struct input_source *source) {
  (void)source; struct input_metadata *m=calloc(1,sizeof(*m)); assert(m);return m;
}
static struct timespec *ts_get(struct input_source *source) {
  struct timespec *t=malloc(sizeof(*t)); assert(t);*t=source->sync_ts;return t;
}
static void input_stop(void) { input_now_reading.open=false; }
static const char *rejection;
static void log_message(const char *format,...) {
  if(!strncmp(format,"Native anchor rejected: reason=",30)) rejection="native_rejection";
}
#define DPRINTF(level,group,...) log_message(__VA_ARGS__)
static int mock_clock(clockid_t clock,struct timespec *ts) {
  assert(clock==CLOCK_MONOTONIC);if(fail_clock)return -1;
  *ts=(struct timespec){(time_t)(now_ns/1000000000),(long)(now_ns%1000000000)};return 0;
}
#define clock_gettime mock_clock
static int timer_settime_local(int timer,int flags,const struct itimerspec *tick,void *old) {
  (void)timer;(void)old;if(fail_timer)return -1;
  ++timer_arms; timer_absolute=flags;armed_anchor=tick->it_value;
  if(!tick->it_value.tv_sec && !tick->it_value.tv_nsec)++timer_stops;
  return 0;
}
#define timer_settime timer_settime_local
static int timer_getoverrun_local(int timer) { (void)timer;return (int)timer_expirations-1; }
#define timer_getoverrun timer_getoverrun_local
#define TFD_TIMER_ABSTIME TIMER_ABSTIME
static int timerfd_settime_local(int fd,int flags,const struct itimerspec *tick,void *old) {
  assert(fd==pb_timer_fd);return timer_settime_local(pb_timer,flags,tick,old);
}
#define timerfd_settime timerfd_settime_local
static int timer_read_local(int fd,void *data,size_t size) {
  assert(fd==pb_timer_fd && size==sizeof(timer_expirations));memcpy(data,&timer_expirations,size);return (int)size;
}
#define read timer_read_local
static int event_add(struct event *event,void *timeout) { (void)timeout;assert(event==pb_timer_ev);timer_active=true;return 0; }
static int event_del(struct event *event) { assert(event==pb_timer_ev);timer_active=false;return 0; }
static int timespec_cmp(struct timespec a,struct timespec b) {
  return a.tv_sec!=b.tv_sec?(a.tv_sec>b.tv_sec?1:-1):a.tv_nsec!=b.tv_nsec?(a.tv_nsec>b.tv_nsec?1:-1):0;
}
static struct timespec timespec_add(struct timespec a,struct timespec b) {
  a.tv_sec+=b.tv_sec;a.tv_nsec+=b.tv_nsec;if(a.tv_nsec>=1000000000){++a.tv_sec;a.tv_nsec-=1000000000;}return a;
}
static struct timespec stamp(uint64_t ns) { return (struct timespec){(time_t)(ns/1000000000),(long)(ns%1000000000)}; }
static int input_pipe_shiri_state(uint64_t *operation) { *operation=gate_operation;return gate_state; }
static void outputs_stop_delayed_cancel(void) {}
static void shiri_speech_poll(void) {}
static bool shiri_speech_bed_tick(void) { return false; }
static void shiri_speech_bed_retire_outputs(void) {}
static void shiri_speech_mix(void *p,int b,int f,unsigned r,unsigned d,unsigned c) { (void)p;(void)b;(void)f;(void)r;(void)d;(void)c; }
static void shiri_speech_mix_ready(bool bed) { assert(!bed); }
static void event_read_error(void) { ++error_calls; }
static void event_read_start_next(void) { ++start_next_calls; }
static void event_read_eof(void) { ++eof_calls;pb_session.reading_now=NULL; }
static void event_read_metadata(struct input_metadata *m) { ++metadata_calls;free(m); }
static void event_read_quality(struct media_quality *q) { ++quality_calls;pb_session.quality=*q;free(q);pb_session.buffer=player_buffer;pb_session.bufsize=sizeof(player_buffer); }
static void event_read_ts(struct timespec *ts) { pb_session.pts=*ts;free(ts); }
static void event_read(int frames) { pb_session.pos+=frames; }
static uint16_t captured[300000];
static struct timespec captured_pts[300000];
static size_t capture_frames;
static void outputs_write(void *pcm,int bytes,int frames,struct media_quality *quality,struct timespec *pts) {
  assert(bytes==frames*4 && quality->sample_rate==48000 && quality->bits_per_sample==16 && quality->channels==2);
  ++output_calls;assert(capture_frames+frames<=sizeof(captured)/sizeof(*captured));
  uint8_t *p=pcm;
  for(int i=0;i<frames;i++) {
    unsigned left=p[4*i]|((unsigned)p[4*i+1]<<8),right=p[4*i+2]|((unsigned)p[4*i+3]<<8);
    CHECK(left==right);captured[capture_frames]=left;
    captured_pts[capture_frames++]=timespec_add(*pts,stamp((uint64_t)i*1000000000/48000));
  }
}
static void pb_abort(void) { ++abort_calls; }
static void device_flush_cb(struct output_device *device,int state) { (void)device;(void)state; }
static int outputs_flush(void (*cb)(struct output_device*,int)) { assert(cb==device_flush_cb);++flush_calls;return 0; }
static void status_update(int state,int listener) { assert(listener==LISTENER_PLAYER);player_state=state; }
static void seek_save(void) { ++seek_calls; }
static struct db_queue_item *db_queue_fetch_byitemid(uint32_t id) { (void)id;assert(!"unexpected source-list replacement");return NULL; }
static int pb_session_start(struct db_queue_item *item,uint32_t seek) { (void)item;(void)seek;assert(!"unexpected session replacement");return -1; }
static void free_queue_item(struct db_queue_item *item,int content) { (void)item;(void)content; }
static void player_playback_start(void) { resume_pending=true; }
static int pb_timer_stop(void);
static int pb_suspend(void);
static int pb_timer_native_prepare(void);
/* @INPUT_FUNCTIONS@ */
/* @PLAYER_FUNCTIONS@ */

static void clear_markers(void) {
  struct marker *m=input_buffer.marker_tail;
  while(m){struct marker *next=m->prev;free(m->data);free(m);m=next;}
  input_buffer.marker_tail=NULL;
}
static void fresh(void) {
  clear_markers(); memset(&main_buffer,0,sizeof(main_buffer));memset(&write_buffer,0,sizeof(write_buffer));
  memset(&pb_session,0,sizeof(pb_session));memset(&input_buffer.cur_write_quality,0,sizeof(input_buffer.cur_write_quality));
  input_buffer.bytes_written=input_buffer.bytes_read=0;input_buffer.full_cb=NULL;input_buffer.evbuf=&main_buffer;
  input_now_reading=(struct input_source){DATA_KIND_PIPE,true,{0}};framed_configuration=true;
  pipe_source.data_kind=DATA_KIND_PIPE;pipe_source.next=NULL;
  pb_session.quality=(struct media_quality){48000,16,2};pb_session.buffer=player_buffer;pb_session.bufsize=sizeof(player_buffer);
  pb_session.read_deficit_max=288000;pb_session.pos=987654;pb_session.start_ts=stamp(31000000000);
  pb_session.pts=stamp(31000000000);pb_session.reading_now=pb_session.playing_now=pb_session.source_list=&pipe_source;
  now_ns=32000000000;gate_operation=3;gate_state=1;player_state=PLAY_PLAYING;
  pb_timer_native=pb_timer_native_anchored=pb_timer_native_waiting_for_pcm=pb_timer_speech_only=false;
  shiri_output_end_valid=false;fail_clock=fail_timer=pb_write_recovery=resume_pending=mutate_gate_on_empty=false;
  output_calls=flush_calls=seek_calls=abort_calls=metadata_calls=quality_calls=start_next_calls=eof_calls=error_calls=0;
  timer_arms=timer_stops=source_zero_reads=0;capture_frames=0;rejection=NULL;timer_expirations=1;
  CHECK(pb_timer_start()==0 && timer_active && pb_timer_native && !pb_timer_native_anchored);
}
static int produce(unsigned frames,unsigned sample_start,uint64_t original_anchor) {
  CHECK(write_buffer.length==0 && frames*4<=sizeof(write_buffer.data));
  for(unsigned i=0;i<frames;i++) {
    unsigned value=1+(sample_start+i)%30000;
    write_buffer.data[4*i]=write_buffer.data[4*i+2]=value;
    write_buffer.data[4*i+1]=write_buffer.data[4*i+3]=value>>8;
  }
  write_buffer.length=frames*4;input_now_reading.sync_ts=stamp(original_anchor);
  struct media_quality quality={48000,16,2};int result=input_write(&write_buffer,&quality,INPUT_FLAG_SYNC);
#ifdef PREIMAGE
  CHECK(result==0 || (result==EAGAIN && resume_pending));
#else
  CHECK(result==0);
#endif
  return result;
}
static void call_tick(uint64_t ns) { now_ns=ns;if(timer_active)playback_cb(pb_timer_fd,0,NULL); }
static void verify_range(size_t from,unsigned frames,unsigned sample_start,uint64_t original_anchor) {
  CHECK(capture_frames>=from+frames);
  for(unsigned i=0;i<frames;i++) {
    CHECK(captured[from+i]==1+(sample_start+i)%30000);
    /* Integer sample conversion may differ by one ns at a block boundary. */
    uint64_t got=(uint64_t)captured_pts[from+i].tv_sec*1000000000+captured_pts[from+i].tv_nsec;
    uint64_t expected=original_anchor+(uint64_t)i*1000000000/48000;
    CHECK(got==expected || got+1==expected);
  }
}
static void first_packet_then_empty(void) {
  fresh();produce(1024,0,32500000000);
  call_tick(32000000000);CHECK(pb_timer_native_anchored && timer_absolute==TIMER_ABSTIME && timespec_cmp(armed_anchor,stamp(32500000000))==0 && !output_calls);
  call_tick(32490000000);CHECK(!output_calls);
  call_tick(32500000000);call_tick(32510000000);call_tick(32520000000);
  CHECK(capture_frames==1024 && pb_session.pos==987654+1024 && !flush_calls);
  verify_range(0,1024,0,32500000000);
  for(unsigned i=3;i<165;i++)call_tick(32500000000+(uint64_t)i*10000000);
}
static void reproduce_or_resume(void) {
  first_packet_then_empty();
#ifdef PREIMAGE
  CHECK(flush_calls==1 && seek_calls==1 && !timer_active && player_state==PLAY_PAUSED && input_buffer.full_cb);
  CHECK(!resume_pending && capture_frames==1024);
  uint64_t first_anchor=now_ns+600000000;
  produce(384,1024,first_anchor);CHECK(!resume_pending && input_buffer.bytes_written-input_buffer.bytes_read==1536);
  for(unsigned i=0;!resume_pending && i<602;i++) {
    now_ns+=10000000;produce(480,1408+480*i,first_anchor+8000000+(uint64_t)i*10000000);
  }
  CHECK(resume_pending && input_buffer.bytes_written-input_buffer.bytes_read>INPUT_BUFFER_NATIVE_THRESHOLD);
  struct timespec original={0};CHECK(input_peek_sync(&original)==1 && timespec_cmp(original,stamp(first_anchor))==0);
  CHECK(now_ns>first_anchor+5000000000 && capture_frames==1024);
  CHECK(pb_timer_start()==0);CHECK(pb_timer_native_prepare()==-1 && !pb_timer_native_anchored);
  CHECK(timespec_cmp(pb_session.pts,original)!=0);
  puts("six-second refill aged an original future marker into strict anchor_late");
#else
  CHECK(!flush_calls && !seek_calls && !abort_calls && timer_active && player_state==PLAY_PLAYING);
  CHECK(!pb_timer_native_waiting_for_pcm && pb_timer_native_anchored && !pb_session.read_deficit && !input_buffer.full_cb);
  uint64_t first_anchor=now_ns+600000000,arrival=now_ns;
  uint64_t old_pos=pb_session.pos;struct timespec old_pts=pb_session.pts;
  produce(384,1024,first_anchor);
  unsigned previous_arms=timer_arms;
  call_tick(arrival);CHECK(pb_timer_native_anchored && !pb_timer_native_waiting_for_pcm && timer_arms==previous_arms);
  CHECK(capture_frames==1408 && pb_session.pos==old_pos+384);
  CHECK(pb_session.read_deficit==384 && pb_timer_native_anchored && !pb_timer_native_waiting_for_pcm);
  verify_range(1024,384,1024,first_anchor);
  call_tick(arrival+10000000);
  CHECK(!pb_session.read_deficit && !pb_timer_native_waiting_for_pcm && pb_timer_native_anchored);
  CHECK(timespec_cmp(pb_session.pts,old_pts)>0 && !flush_calls && !seek_calls && !resume_pending);
  uint64_t p=pb_session.pos;struct timespec pts=pb_session.pts,start=pb_session.start_ts;
  for(unsigned i=0;i<6500;i++)call_tick(first_anchor+20000000+(uint64_t)i*10000000);
  CHECK(timer_active && pb_timer_native_anchored && !flush_calls && !abort_calls);
  CHECK(pb_session.pos==p && timespec_cmp(pb_session.pts,pts)==0 && timespec_cmp(pb_session.start_ts,start)==0);
  puts("cold framed partial prefix and resume preserve every original sample/PTS; no capacity refill, flush or session restart");
#endif
}

#ifndef PREIMAGE
static void steady_framed_blocks(void) {
  fresh();
  const unsigned blocks=576,frames=384;
  const uint64_t arrival=now_ns,anchor=arrival+600000000;
  unsigned supplied=0;uint64_t next_read=anchor;
  /* Independently clocked 8 ms production and 10 ms player consumption. */
  while(supplied<blocks || capture_frames<(size_t)blocks*frames) {
    uint64_t next_write=arrival+(uint64_t)supplied*8000000;
    if(supplied<blocks && next_write<=next_read) {
      now_ns=next_write;produce(frames,supplied*frames,anchor+(uint64_t)supplied*8000000);
      if(!supplied)call_tick(now_ns);
      ++supplied;
    } else {
      call_tick(next_read);next_read+=10000000;
      CHECK(next_read<anchor+7000000000);
    }
  }
  CHECK(capture_frames==(size_t)blocks*frames && pb_session.pos==987654+(uint64_t)blocks*frames);
  verify_range(0,blocks*frames,0,anchor);
  CHECK(input_buffer.bytes_read==input_buffer.bytes_written && !main_buffer.length);
  call_tick(next_read);CHECK(!pb_timer_native_waiting_for_pcm && pb_timer_native_anchored);
  CHECK(timer_active && !flush_calls && !seek_calls && !abort_calls && !resume_pending && !pb_session.read_deficit);
  puts("221184 steady 8 ms framed samples preserved across the existing 10 ms timer with original timestamps");
}

static void controls_and_ownership(void) {
  fresh();produce(384,42,32500000000);
  CHECK(input_write(NULL,NULL,INPUT_FLAG_METADATA)==0);
  call_tick(32000000000);call_tick(32500000000);
  CHECK(quality_calls==1 && capture_frames==384 && pb_session.read_deficit==384 && pb_timer_native_anchored);
  call_tick(32510000000);CHECK(metadata_calls==1 && !pb_session.read_deficit && !pb_timer_native_waiting_for_pcm && pb_timer_native_anchored);
  verify_range(0,384,42,32500000000);
  /* A continuing stream may miss its input-read time but still retain valid
   * future device-output lead. Do not introduce a new cold-anchor gate. */
  produce(480,426,32508000000);uint64_t p=pb_session.pos;
  call_tick(32558000000);CHECK(!abort_calls && pb_timer_native_anchored && pb_session.pos==p+480 && capture_frames==864);
  verify_range(384,480,426,32508000000);
  fresh();produce(480,0,now_ns-1);p=pb_session.pos;size_t bytes=input_buffer.bytes_read;
  call_tick(now_ns);CHECK(abort_calls==1 && !pb_timer_native_anchored && pb_session.pos==p && input_buffer.bytes_read==bytes);

  fresh();produce(480,0,32500000000);call_tick(32000000000);call_tick(32500000000);
  CHECK(input_write(NULL,NULL,INPUT_FLAG_EOF)==0);
  call_tick(32510000000);CHECK(eof_calls==1 && start_next_calls==1 && !pb_timer_native_waiting_for_pcm && pb_timer_native_anchored);
  fresh();produce(480,0,32500000000);call_tick(32000000000);call_tick(32500000000);
  CHECK(input_write(NULL,NULL,INPUT_FLAG_ERROR)==0);
  call_tick(32510000000);CHECK(error_calls==1 && start_next_calls==1 && !pb_timer_native_waiting_for_pcm && pb_timer_native_anchored);

  fresh();produce(480,0,32500000000);call_tick(32000000000);call_tick(32500000000);
  gate_operation++;call_tick(32510000000);CHECK(abort_calls==1 && pb_timer_native_anchored && !pb_timer_native_waiting_for_pcm);
  fresh();produce(480,0,32500000000);call_tick(32000000000);call_tick(32500000000);
  mutate_gate_on_empty=true;call_tick(32510000000);
  CHECK(pb_timer_native_anchored && !pb_timer_native_waiting_for_pcm && pb_session.read_deficit==1920);
  call_tick(32520000000);CHECK(abort_calls==1 && !flush_calls);
  fresh();produce(480,0,32500000000);call_tick(32000000000);call_tick(32500000000);
  gate_state=-1;call_tick(32510000000);CHECK(abort_calls==1 && !flush_calls);
  fresh();produce(480,0,32500000000);call_tick(32000000000);call_tick(32500000000);
  timer_expirations=4;call_tick(32540000000);
  CHECK(!pb_session.read_deficit && pb_timer_native_anchored && timer_active && !flush_calls && !abort_calls);
  timer_expirations=1;produce(480,480,32510000000);call_tick(32550000000);
  CHECK(capture_frames==960 && !abort_calls && !flush_calls);verify_range(480,480,480,32510000000);

  /* Ordinary file/raw inputs retain upstream underrun behavior. */
  fresh();pb_timer_native=false;pipe_source.data_kind=DATA_KIND_FILE;
  input_now_reading.data_kind=DATA_KIND_FILE;CHECK(input_buffer_threshold()==INPUT_BUFFER_THRESHOLD);
  for(unsigned i=0;i<152;i++)call_tick(32000000000+(uint64_t)i*10000000);
  CHECK(flush_calls==1 && !timer_active && !pb_timer_native_waiting_for_pcm && input_buffer.full_cb);
  puts("EOF/error/control/partial read and exact owner fences retained; ordinary file underrun unchanged");
}
#endif

int main(void) {
  CHECK(pthread_mutex_init(&input_buffer.mutex,NULL)==0 && pthread_cond_init(&input_buffer.cond,NULL)==0);
  reproduce_or_resume();
#ifndef PREIMAGE
  steady_framed_blocks();
  controls_and_ownership();
#endif
  clear_markers();CHECK(pthread_cond_destroy(&input_buffer.cond)==0 && pthread_mutex_destroy(&input_buffer.mutex)==0);
  printf("%u exact native cold music checks passed\n",checks);return 0;
}

/* Actual OwnTone timer, first-sample control reads and player callback. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <sys/types.h>
#ifdef __APPLE__
typedef int timer_t;
struct itimerspec { struct timespec it_interval,it_value; };
#define TIMER_ABSTIME 1
#endif
#define DPRINTF(...) ((void)0)
#define DATA_KIND_PIPE 4
#define INPUT_FLAG_ERROR 1
#define INPUT_FLAG_START_NEXT 2
#define INPUT_FLAG_EOF 4
#define INPUT_FLAG_METADATA 8
#define INPUT_FLAG_QUALITY 16
#define INPUT_FLAG_SYNC 32
#define BTOS(n,b,c) ((n)/((b)/8)/(c))
struct media_quality { unsigned sample_rate,bits_per_sample,channels; };
struct input_metadata { int unused; };
struct player_source { int data_kind,id; const char*path; };
static uint8_t pcm[1920];
static struct player_source native_source={DATA_KIND_PIPE,1,"private"};
static struct {
  uint8_t *buffer; size_t bufsize,read_deficit,read_deficit_max;
  struct media_quality quality;
  struct timespec start_ts,pts;
  struct player_source *reading_now,*playing_now;
} pb_session;
static struct timespec player_tick_interval={0,10000000};
static bool pb_timer_native,pb_timer_native_anchored,pb_write_recovery;
static uint64_t pb_timer_native_operation;
static struct timespec pb_timer_native_anchor,pb_timer_native_wait_until;
static int pb_timer_ev,player_flush_pending;
#ifdef HAVE_TIMERFD
static int pb_timer_fd;
#ifndef TFD_TIMER_ABSTIME
#define TFD_TIMER_ABSTIME 1
#endif
#else
static int pb_timer;
#endif
static unsigned pb_write_deficit_max=50;
static int armed_state=1,peek_status,reads,writes,aborts,control_step,timer_calls,flags_seen;
#ifdef SHIRI_TIMER_SPEECH
/* Disabled-overlay boundary only. The real enabled module is tested by its
 * own actual-source fixtures; keep these calls in the extracted player. */
enum { SPEECH_NONE, SPEECH_POLLED, SPEECH_TIMER, SPEECH_INPUT, SPEECH_MIXED, SPEECH_OUTPUT };
static int speech_phase,speech_polls,speech_mixes;
#ifdef SHIRI_TIMER_READY
static int speech_ready;
static void shiri_speech_mix_ready(void){
  assert(speech_phase==SPEECH_OUTPUT&&speech_ready==writes-1);
  speech_ready++;
}
#endif
static void shiri_speech_poll(void){
  assert(speech_phase!=SPEECH_POLLED&&speech_phase!=SPEECH_MIXED);
  speech_polls++;speech_phase=SPEECH_POLLED;
}
static void speech_timer_read(void){assert(speech_phase==SPEECH_POLLED);speech_phase=SPEECH_TIMER;}
static void shiri_speech_mix(void *data,size_t bytes,int frames,unsigned rate,unsigned bits,unsigned channels){
  assert(speech_phase==SPEECH_INPUT);
  assert(data==pcm&&bytes==sizeof(pcm)&&frames==480);
  assert(rate==48000&&bits==16&&channels==2);
  for(size_t i=0;i<bytes;i++)assert(((uint8_t*)data)[i]==7);
  speech_mixes++;speech_phase=SPEECH_MIXED;
  /* An unconfigured overlay must leave this player-owned PCM unchanged. */
}
#else
static void speech_timer_read(void){}
#endif
static uint64_t operation=1;
static struct timespec now={5,0},next_anchor,last_write_pts;
static struct itimerspec timer_seen;
static int fixture_clock(clockid_t id,struct timespec*t){assert(id==CLOCK_MONOTONIC);*t=now;return 0;}
#define clock_gettime fixture_clock
static int fixture_timer(int t,int flags,const struct itimerspec*v,struct itimerspec*o){
  (void)t;(void)o;timer_calls++;flags_seen=flags;timer_seen=*v;return 0;
}
#ifdef HAVE_TIMERFD
#define timerfd_settime fixture_timer
static ssize_t fixture_read(int fd,void*p,size_t n){(void)fd;speech_timer_read();assert(n==sizeof(uint64_t));*((uint64_t*)p)=1;return (ssize_t)n;}
#define read fixture_read
#else
#define timer_settime fixture_timer
static int fixture_overrun(int t){(void)t;speech_timer_read();return 0;}
#define timer_getoverrun fixture_overrun
#endif
static int input_pipe_shiri_state(uint64_t*p){*p=operation;return armed_state;}
static int input_peek_sync(struct timespec*t){*t=next_anchor;return peek_status;}
static int event_add(int e,void*p){(void)e;(void)p;return 0;}
static int event_del(int e){(void)e;return 0;}
static void outputs_stop_delayed_cancel(void){}
static int timespec_cmp(struct timespec a,struct timespec b){
  if(a.tv_sec!=b.tv_sec)return a.tv_sec>b.tv_sec?1:-1;
  return (a.tv_nsec>b.tv_nsec)-(a.tv_nsec<b.tv_nsec);
}
static struct timespec timespec_add(struct timespec a,struct timespec b){
  a.tv_sec+=b.tv_sec;a.tv_nsec+=b.tv_nsec;if(a.tv_nsec>=1000000000){a.tv_sec++;a.tv_nsec-=1000000000;}return a;
}
static int input_read(void*p,size_t n,short*flag,void**data){
#ifdef SHIRI_TIMER_SPEECH
  assert(speech_phase==SPEECH_TIMER||speech_phase==SPEECH_INPUT||speech_phase==SPEECH_OUTPUT);
  speech_phase=SPEECH_INPUT;
#endif
  reads++; *flag=0;*data=NULL;
  if(control_step==0){*flag=INPUT_FLAG_QUALITY;control_step++;return 0;}
  if(control_step==1){*flag=INPUT_FLAG_SYNC;*data=&next_anchor;control_step++;
    assert(p==pcm&&n==sizeof(pcm));memset(p,7,n);return (int)n;}
  return 0;
}
static void event_read(int n){(void)n;if(!pb_session.start_ts.tv_sec){pb_session.start_ts=now;pb_session.pts=now;}}
static void event_read_error(void){}
static void event_read_start_next(void){}
static void event_read_eof(void){}
static void event_read_metadata(struct input_metadata*p){(void)p;}
static void event_read_quality(struct media_quality*p){(void)p;
  pb_session.quality=(struct media_quality){48000,16,2};pb_session.bufsize=sizeof(pcm);pb_session.buffer=pcm;
}
static void event_read_ts(struct timespec*p){pb_session.pts=*p;}
static void outputs_write(void*p,int n,int samples,void*q,struct timespec*pts){
  (void)q;assert(p==pcm&&n==samples*4);assert(timespec_cmp(now,*pts)>=0);
#ifdef SHIRI_TIMER_SPEECH
  assert(speech_phase==SPEECH_MIXED&&speech_mixes==writes+1);speech_phase=SPEECH_OUTPUT;
#endif
  for(int i=0;i<n;i++)assert(((uint8_t*)p)[i]==7);
  writes++;last_write_pts=*pts;
}
static void player_playback_start(void){}
static void input_buffer_full_cb(void(*cb)(void)){(void)cb;}
static int pb_suspend(void){return 0;}
static int pb_timer_stop(void);
static int pb_timer_native_prepare(void);
static void pb_abort(void){aborts++;pb_timer_stop();}

/* @ACTUAL_NATIVE_TIMER@ */

static void reset(void){
  pb_timer_stop();memset(&pb_session,0,sizeof(pb_session));
  pb_session.reading_now=pb_session.playing_now=&native_source;
  pb_session.read_deficit_max=192000;armed_state=1;peek_status=0;operation++;
  control_step=reads=writes=aborts=timer_calls=0;now=(struct timespec){5,0};
#ifdef SHIRI_TIMER_SPEECH
  speech_phase=SPEECH_NONE;speech_polls=speech_mixes=0;
#ifdef SHIRI_TIMER_READY
  speech_ready=0;
#endif
#endif
}
int main(void){
  reset();assert(pb_timer_start()==0);assert(pb_timer_native&&flags_seen==0);
  playback_cb(0,0,NULL);assert(!reads&&!writes);
#ifdef SHIRI_TIMER_SPEECH
  assert(speech_polls==1&&speech_mixes==0);
#endif
  next_anchor=(struct timespec){8,0};peek_status=1;now.tv_nsec=10000000;
  playback_cb(0,0,NULL);assert(!reads&&!writes&&flags_seen==TIMER_ABSTIME);
  assert(timer_seen.it_value.tv_sec==8&&timer_seen.it_interval.tv_nsec==10000000);
  /* A queued stale periodic event cannot read ahead of the absolute anchor. */
  now=(struct timespec){7,999000000};playback_cb(0,0,NULL);assert(!reads&&!writes);
  now=(struct timespec){8,0};playback_cb(0,0,NULL);
  assert(reads==2&&writes==1&&last_write_pts.tv_sec==8&&last_write_pts.tv_nsec==0);
  assert(pb_session.read_deficit==0&&!aborts); /* quality+PCM on SAME tick */
#ifdef SHIRI_TIMER_SPEECH
  assert(speech_polls==4&&speech_mixes==1);
#endif
  /* Same owner flush/new grant must wait on its NEW anchor. */
  reset();next_anchor=(struct timespec){9,0};peek_status=1;
  assert(pb_timer_start()==0);playback_cb(0,0,NULL);assert(!writes&&timer_seen.it_value.tv_sec==9);
  operation++;now=(struct timespec){9,0};playback_cb(0,0,NULL);assert(aborts==1&&!reads&&!writes);
#ifdef SHIRI_TIMER_SPEECH
  assert(speech_polls==2&&speech_mixes==0);
#endif
  assert(!pb_timer_native&&!pb_timer_native_operation); /* no stale timer after END/takeover */
  reset();assert(pb_timer_start()==0);now=(struct timespec){10,0};
  playback_cb(0,0,NULL);assert(aborts==1&&!reads&&!writes); /* bounded missing anchor */
  reset();peek_status=1;next_anchor=(struct timespec){4,0};assert(pb_timer_start()==0);
  playback_cb(0,0,NULL);assert(aborts==1&&!reads&&!writes); /* never reset late P to receive time */
  reset();armed_state=-1;assert(pb_timer_start()<0&&!reads&&!writes);
  reset();armed_state=0;assert(pb_timer_start()==0&&!pb_timer_native&&flags_seen==0);
  assert(timer_seen.it_value.tv_sec==0&&timer_seen.it_value.tv_nsec==10000000);
#ifdef SHIRI_TIMER_SPEECH
#ifdef SHIRI_TIMER_READY
  assert(speech_ready==writes);
#endif
  puts("disabled late speech: poll precedes timer/input; private PCM mix precedes output only after native anchor; no PCM mutation");
#endif
  puts("actual player timer: absolute native anchor, no early writes, same-tick quality, bounded missing/late input, source-operation fencing; raw path unchanged");
  return 0;
}

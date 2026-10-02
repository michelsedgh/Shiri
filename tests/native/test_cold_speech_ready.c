/* Exact extracted player command/callback implementations, controlled outputs. */
#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include <time.h>
#include "shiri_speech_ready.h"
static uint64_t now_ns = UINT64_C(1000000000);
static int fail_clock;
static int mock_clock(clockid_t which, struct timespec *ts) {
  assert(which == CLOCK_MONOTONIC);
  if (fail_clock) return -1;
  ts->tv_sec = (time_t)(now_ns/1000000000); ts->tv_nsec=(long)(now_ns%1000000000); return 0;
}
#define clock_gettime mock_clock
#include SHIRI_SPEECH_SOURCE
static unsigned checks, starts, stops, cancels, flushes, callback_ends, command_calls, foreign_cancels;
static void *evbase_player;
static int event_fail;
struct event { void (*cb)(int,short,void*); void *arg; };
static struct event selected_timer, foreign_timer;
static int event_del(struct event *e) { if(e==&selected_timer)++cancels;else {assert(e==&foreign_timer);++foreign_cancels;} return 0; }
static struct event *evtimer_new(void *base,void (*cb)(int,short,void*),void *arg) {
  struct event *e;(void)base;if(event_fail)return NULL;e=malloc(sizeof(*e));assert(e);e->cb=cb;e->arg=arg;return e;
}
static int evtimer_add(struct event *e,const struct timeval *timeout) {assert(e && timeout->tv_sec==5 && !timeout->tv_usec);return 0;}
static void event_free(struct event *e) {free(e);}
#define CHECK(value) do { assert(value); ++checks; } while (0)
enum output_device_state { OUTPUT_STATE_PASSWORD=-2, OUTPUT_STATE_FAILED=-1, OUTPUT_STATE_STOPPED, OUTPUT_STATE_STARTUP, OUTPUT_STATE_CONNECTED, OUTPUT_STATE_STREAMING };
struct output_device { const char *name;uint64_t id; int type, offset_ms; bool selected, busy, prevent_playback; void *session; enum output_device_state state; struct output_device *next; struct event *stop_timer; };
typedef void (*output_status_cb)(struct output_device *,enum output_device_state);
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
static void mock_log(const char *format, ...) {(void)format;}
#define DPRINTF(level,group,...) mock_log(__VA_ARGS__)
#define player_pmap(cb) "callback"
#define OUTPUTS_MAX_CALLBACKS 64
struct outputs_callback_register {int token;output_status_cb cb;struct output_device *device;bool ready;uint64_t device_id;enum output_device_state state;};
static struct outputs_callback_register outputs_cb_register[OUTPUTS_MAX_CALLBACKS];
static uint32_t outputs_callback_token;
static struct event *outputs_deferredev;
static int callback_add(struct output_device *,output_status_cb);
static int pending_token;
static void event_active(struct event *e,int flags,int n) {(void)e;(void)flags;(void)n;}
static struct output_device *outputs_device_list;
enum command_state { COMMAND_END, COMMAND_PENDING };
#define PLAY_STOPPED 0
#define PLAY_PLAYING 1
static int player_state, pb_timer_native, pb_timer_native_anchored;
static struct { struct { unsigned sample_rate,bits_per_sample,channels; } quality; } pb_session;
static struct shiri_source_state shiri_source_state;
static struct output_device device, foreign;
static void *cmdbase;
static uint64_t startup_delay;
static int startup_bad_status, start_failure, mutate_source_between_commands;
static void (*pending_callback)(struct output_device *,enum output_device_state);
static void device_streaming_cb(struct output_device *d, enum output_device_state s) { (void)d;(void)s; }
static struct output_device *outputs_list(void) { return &device; }
static void __attribute__((unused)) outputs_stop_delayed_cancel(void) { ++cancels;++foreign_cancels; }
static int outputs_device_stop_delayed(struct output_device *d, void (*cb)(struct output_device *,enum output_device_state)) { assert(d==&device && cb==device_streaming_cb);++stops;callback_add(d,cb); return 0; }
static void outputs_device_cb_set(struct output_device *d, void (*cb)(struct output_device *,enum output_device_state)) { assert(d==&device && cb==device_streaming_cb);callback_add(d,cb); }
static int outputs_device_start(struct output_device *d, void (*cb)(struct output_device *,enum output_device_state), bool probe) {
  ++starts; assert(!probe);
  if (start_failure) return -1;
  if (d->session) return 0;
  d->session=&device; d->state=OUTPUT_STATE_STARTUP; pending_callback=cb;pending_token=callback_add(d,cb); return 1;
}
/* @ACTUAL_STATE@ */
static int commands_exec_sync(void *base, enum command_state (*fn)(void *,int *), void *bh, void *arg) {
  int ret=0;(void)base;(void)bh;++command_calls;
  assert(fn(arg,&ret)==COMMAND_END); /* Never retain a missing START command. */
  if(mutate_source_between_commands){++shiri_source_state.last.operation_generation;mutate_source_between_commands=0;}
  return ret;
}
/* @ACTUAL_FUNCTIONS@ */
static struct shiri_speech_request fresh(void) {
  struct shiri_speech_request q;
  shiri_speech_setup_event_clear();memset(outputs_cb_register,0,sizeof(outputs_cb_register));outputs_callback_token=0;
  memset(&q,0,sizeof(q)); memset(&shiri_source_state,0,sizeof(shiri_source_state));
  memset(&shiri_speech_start,0,sizeof(shiri_speech_start)); memset(&shiri_last_mix,0,sizeof(shiri_last_mix));
  memset(&device,0,sizeof(device)); memset(&speech,0,sizeof(speech));
  q.source.owner.incarnation[0]=1; q.source.owner.generation=1; q.source.operation_generation=1;
  q.room[0]=2;q.launch[0]=3;q.speech[0]=4;q.action=SHIRI_SPEECH_PREPARE;
  shiri_source_state.initialized=true; shiri_source_state.last=q.source;
  memcpy(speech.room,q.room,16);memcpy(speech.launch,q.launch,16);speech_fd=999;
  outputs_device_list=&device;device.id=5;device.selected=true;device.type=6;device.state=OUTPUT_STATE_STOPPED;
  memset(&foreign,0,sizeof(foreign));foreign.id=6;foreign.session=&foreign;foreign.state=OUTPUT_STATE_CONNECTED;
  device.next=&foreign;device.stop_timer=&selected_timer;foreign.stop_timer=&foreign_timer;foreign_cancels=0;
  now_ns=1000000000; startup_delay=300000000;fail_clock=startup_bad_status=start_failure=0;
  mutate_source_between_commands=event_fail=0;command_calls=0;pending_callback=NULL; starts=stops=cancels=flushes=callback_ends=0;
  pb_timer_native=pb_timer_native_anchored=player_state=0;
  pb_session.quality.sample_rate=48000;pb_session.quality.bits_per_sample=16;pb_session.quality.channels=2;
  return q;
}
static void finish_startup(void) {
  unsigned i;output_status_cb cb;enum output_device_state state;
  assert(pending_callback);now_ns+=startup_delay;
  outputs_cb(pending_token,device.id,startup_bad_status ? OUTPUT_STATE_FAILED : OUTPUT_STATE_CONNECTED);
  for(i=0;i<ARRAY_SIZE(outputs_cb_register);i++)if(outputs_cb_register[i].ready){
    cb=outputs_cb_register[i].cb;state=outputs_cb_register[i].state;memset(&outputs_cb_register[i],0,sizeof(outputs_cb_register[i]));
    device.state=state;cb(&device,state);
  }
  pending_callback=NULL;
}
static int prepare_all(struct shiri_speech_request *q,struct shiri_speech_reply *r) {
  int ret=player_shiri_speech_ready(q,r);
  if(!ret && q->action==SHIRI_SPEECH_PREPARE && pending_callback){finish_startup();ret=player_shiri_speech_ready(q,r);}
  return ret;
}
static void setup(void) {
  struct shiri_speech_request q=fresh();struct shiri_speech_reply r;
  CHECK(prepare_all(&q,&r)==0 && r.connected && !r.ready);
  CHECK(starts==1 && callback_ends==0 && now_ns==1300000000 && r.prepared_ns==now_ns);
  CHECK(stops==1); /* finite idle transport cleanup even if caller vanishes */
  CHECK(!foreign_cancels); /* prearm never cancels an unrelated retiring output */
  CHECK(outputs_shiri_stop_delayed_cancel(&foreign)<0 && !foreign_cancels);
  q.action=SHIRI_SPEECH_READY;
  CHECK(prepare_all(&q,&r)==0 && !r.ready);
  player_state=PLAY_PLAYING;pb_timer_native=pb_timer_native_anchored=1;
  shiri_speech_mix_ready();
  CHECK(prepare_all(&q,&r)==0 && r.ready && r.mixed_ns==now_ns);
  now_ns+=SHIRI_SPEECH_MIX_AGE_NS;
  CHECK(prepare_all(&q,&r)==0 && !r.ready);
  q.action=SHIRI_SPEECH_PREPARE;
  CHECK(prepare_all(&q,&r)==0 && starts==1); /* lost-ACK retry */
}
static void identities(void) {
  struct shiri_speech_request q;struct shiri_speech_reply r;unsigned i;
  for (i=0;i<7;i++) {
    q=fresh();
    if(i==0)q.room[0]++;
    if(i==1)q.launch[0]++;
    if(i==2)q.source.owner.incarnation[0]++;
    if(i==3)q.source.owner.epoch++;
    if(i==4)q.source.owner.generation++;
    if(i==5)q.source.operation_generation++;
    if(i==6)q.source.owner.session[0]=9;
    CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT && starts==0 && cancels==0);
  }
  q=fresh();CHECK(prepare_all(&q,&r)==0);
  q.speech[0]++;q.action=SHIRI_SPEECH_RELEASE;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT && stops==1 && shiri_speech_start.initialized);
  q.action=SHIRI_SPEECH_PREPARE;
  CHECK(prepare_all(&q,&r)==0);
  q.speech[0]--;q.action=SHIRI_SPEECH_RELEASE;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT && shiri_speech_start.request.speech[0]==5);
}
static void failures(void) {
  struct shiri_speech_request q;struct shiri_speech_reply r;
  q=fresh();startup_delay=SHIRI_SPEECH_SETUP_NS;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_FAILURE && !shiri_speech_start.connected && stops==1);
  q=fresh();startup_bad_status=1;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_FAILURE && !shiri_speech_start.connected);
  q=fresh();start_failure=1;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_FAILURE);
  q=fresh();mutate_source_between_commands=1;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT && stops==0); /* no stop on successor source */
  q=fresh();CHECK(prepare_all(&q,&r)==0);
  device.session=&r;q.action=SHIRI_SPEECH_READY;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_FAILURE);
  q.action=SHIRI_SPEECH_RELEASE;CHECK(prepare_all(&q,&r)==0 && stops==1); /* replacement not stopped */
  q=fresh();CHECK(prepare_all(&q,&r)==0);
  device.offset_ms++;q.action=SHIRI_SPEECH_READY;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_FAILURE);
  q=fresh();CHECK(prepare_all(&q,&r)==0);
  shiri_source_state.faulted=true;q.action=SHIRI_SPEECH_READY;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT);
  q=fresh();fail_clock=1;CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT && starts==0);
}
static void music(void) {
  struct shiri_speech_request q=fresh();struct shiri_speech_reply r;
  q.source.owner.session[0]=8;shiri_source_state.last=q.source;q.action=SHIRI_SPEECH_OBSERVE;
  device.session=&device;device.state=OUTPUT_STATE_CONNECTED;
  CHECK(prepare_all(&q,&r)==0 && !r.ready && !starts && !cancels && !stops);
  player_state=PLAY_PLAYING;pb_timer_native=pb_timer_native_anchored=1;
  shiri_speech_mix_ready();
  CHECK(prepare_all(&q,&r)==0 && r.ready && starts+cancels+stops+flushes==0);
  device.session=&r;CHECK(prepare_all(&q,&r)==0 && !r.ready);
  device.session=&device;shiri_source_state.last.owner.generation++;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT);
  q.source.owner.generation++;CHECK(prepare_all(&q,&r)==0 && !r.ready);
}
static void put32(uint8_t *p,uint32_t n) { p[0]=n>>24;p[1]=n>>16;p[2]=n>>8;p[3]=n; }
static void put64(uint8_t *p,uint64_t n) { put32(p,(uint32_t)(n>>32));put32(p+4,(uint32_t)n); }
static void prefix(void) {
  struct shiri_speech_request q=fresh();struct shiri_speech_reply r;
  uint8_t packet[SPEECH_HEADER+960], pcm[1920], id[16];unsigned i;
  CHECK(speech_hex("01000000000000000000000000000000",id,0)==0 && id[0]==1);
  memset(packet,0,sizeof(packet));memcpy(packet,"SHRITTS1",8);packet[8]=1;packet[9]=1;packet[11]=80;
  put32(packet+12,960);put64(packet+16,1);memcpy(packet+24,q.room,16);memcpy(packet+40,q.launch,16);
  put64(packet+56,now_ns);put32(packet+64,480);put32(packet+68,65536);put32(packet+72,1);
  for(i=0;i<480;i++){packet[80+2*i]=0xd2;packet[81+2*i]=4;}
  /* Exact old behaviour: enqueue before300ms connection, prefix expires at
   * first mutable player mix. Queue/tolerance itself stays unchanged. */
  speech.gain=1;CHECK(speech_receive(&speech,packet,sizeof(packet),now_ns)==0 && speech.count==480);
  now_ns+=300000000;memset(pcm,0,sizeof(pcm));speech_mix(&speech,pcm,sizeof(pcm),480,48000,16,2,now_ns);
  CHECK(speech.expired==480 && speech.count==0);
  for(i=0;i<sizeof(pcm);i++)CHECK(pcm[i]==0);
  q=fresh();CHECK(prepare_all(&q,&r)==0 && r.connected && !r.ready);
  player_state=PLAY_PLAYING;pb_timer_native=pb_timer_native_anchored=1;shiri_speech_mix_ready();
  q.action=SHIRI_SPEECH_READY;CHECK(prepare_all(&q,&r)==0 && r.ready);
  /* New admission starts only after that actual first mix; original receive
   * age is independently bounded by Python, never rewritten as source age. */
  memcpy(packet+24,q.room,16);memcpy(packet+40,q.launch,16);put64(packet+56,now_ns);
  speech.gain=1;CHECK(speech_receive(&speech,packet,sizeof(packet),now_ns)==0);
#ifdef SPEECH_RESERVE_NS
  /* The additive reserve delays consumption only; readiness and the original
   * emitted packet clock/250ms expiry remain exactly the same. */
  CHECK(speech.priming_until==now_ns+SPEECH_RESERVE_NS);
  now_ns+=SPEECH_RESERVE_NS;
#endif
  memset(pcm,0,sizeof(pcm));speech_mix(&speech,pcm,sizeof(pcm),480,48000,16,2,now_ns);
  for(i=0;i<480;i++)CHECK(pcm[i*4]==0xd2 && pcm[i*4+1]==4 && pcm[i*4+2]==0xd2 && pcm[i*4+3]==4);
  CHECK(speech.expired==0 && speech.count==0);
}
static void missing(void) {
  struct shiri_speech_request q=fresh(),other;struct shiri_speech_reply r;
  CHECK(player_shiri_speech_ready(&q,&r)==0 && !r.connected && shiri_speech_start.pending==1);
  CHECK(command_calls==2 && shiri_speech_setup_event);
  other=q;other.speech[0]++;
  CHECK(player_shiri_speech_ready(&other,&r)==SHIRI_SOURCE_CONFLICT);
  /* Real libevent deadline callback, not a client cancellation stand-in. */
  now_ns+=SHIRI_SPEECH_SETUP_NS;
  shiri_speech_setup_event->cb(-1,0,shiri_speech_setup_event->arg);
  CHECK(!shiri_speech_setup_event && shiri_speech_start.failed && !shiri_speech_start.connected);
  CHECK(player_shiri_speech_ready(&q,&r)==SHIRI_SOURCE_FAILURE);
  CHECK(shiri_speech_start.pending==0); /* real stop callback replaced its never-reused START token */
  CHECK(callback_ends==0 && command_calls>=4); /* another player command remained schedulable */
  unsigned token=outputs_callback_token;finish_startup();
  CHECK(shiri_speech_start.pending==0 && shiri_speech_start.failed && outputs_callback_token==token);
  /* After real session retirement a new nonce can start, and the old token
   * is ignored even while the new exact callback is registered. */
  device.session=NULL;device.state=OUTPUT_STATE_STOPPED;now_ns+=1;
  CHECK(player_shiri_speech_ready(&other,&r)==0 && shiri_speech_start.pending==1);
  unsigned new_pending=shiri_speech_start.pending;
  outputs_cb(1,device.id,OUTPUT_STATE_CONNECTED);
  CHECK(shiri_speech_start.pending==new_pending && !shiri_speech_start.connected);
  finish_startup();CHECK(player_shiri_speech_ready(&other,&r)==0 && r.connected);
  q=fresh();event_fail=1;CHECK(player_shiri_speech_ready(&q,&r)==SHIRI_SOURCE_FAILURE && starts==0);
}
int main(void) { setup();identities();failures();music();prefix();missing();shiri_speech_setup_event_clear();speech_fd=-1;printf("%u exact player readiness checks passed\n",checks);
#ifdef SPEECH_RESERVE_NS
printf("explicit20ms voice-only prime; readiness/packet clocks unchanged\n");
#endif
return 0; }

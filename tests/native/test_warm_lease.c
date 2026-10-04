/* Actual native warm lifecycle and delayed-stop code; no socket/audio I/O. */
#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>
#include "shiri_warm.h"
#include "shiri_speech_ready.h"
/* @ACTUAL_SPEECH_STATUS@ */

static unsigned checks;
#define CHECK(v) do { assert(v); ++checks; } while (0)
enum command_state { COMMAND_END, COMMAND_PENDING };
enum { PLAY_STOPPED, PLAY_PAUSED, PLAY_PLAYING };
enum output_device_state { OUTPUT_STATE_STOPPED, OUTPUT_STATE_STARTUP, OUTPUT_STATE_CONNECTED, OUTPUT_STATE_STREAMING, OUTPUT_STATE_FAILED };
struct event { bool pending; struct timeval delay; void (*cb)(int,short,void *); void *arg; };
struct output_device {
  uint64_t id; int type,offset_ms; bool selected,busy,prevent_playback;
  void *session,*shiri_warm_session; uint64_t shiri_session_serial,shiri_warm_serial,shiri_warm_until_ns;
  enum output_device_state state; struct event *stop_timer; struct output_device *next;
};
typedef void (*output_status_cb)(struct output_device *,enum output_device_state);
static void *evbase_player;
static uint64_t observed_ns;
static unsigned starts,stops,delays,timer_changes,callback_changes;
static unsigned start_fail_id;
static bool start_inline;
static bool bed_wanted;
static struct output_device devices[2];
static struct event stop_events[2];
static output_status_cb callbacks[2];
static struct shiri_source_state shiri_source_state;
static struct { bool active; } shiri_source_wait;
static struct { unsigned pending; } shiri_speech_start;
static struct shiri_speech_status media;
static int player_state;
static struct output_device *outputs_list(void) { return devices; }
static struct output_device *outputs_device_get(uint64_t id) { return id>=1 && id<=2 ? &devices[id-1] : NULL; }
static uint64_t outputs_shiri_session_serial;
/* @ACTUAL_OUTPUT_SESSION_ADD@ */
/* @ACTUAL_OUTPUT_SESSION_REMOVE@ */
static uint64_t shiri_speech_ready_now(void) { return observed_ns; }
static bool shiri_speech_ids_match(const uint8_t *room,const uint8_t *launch)
{ return room[0]==1 && launch[0]==2; }
static bool shiri_speech_bed_wanted(void) { return bed_wanted; }
static void shiri_speech_status(struct shiri_speech_status *out) { *out=media; }
static struct event *evtimer_new(void *base,void (*cb)(int,short,void *),void *arg)
{ (void)base; struct event *e=calloc(1,sizeof(*e)); CHECK(e); e->cb=cb; e->arg=arg; return e; }
static int evtimer_add(struct event *e,const struct timeval *delay)
{ CHECK(e); e->pending=true; e->delay=*delay; ++timer_changes; return 0; }
static int event_add(struct event *e,const struct timeval *delay) { return evtimer_add(e,delay); }
static void event_free(struct event *e) { free(e); }
#define EV_TIMEOUT 1
static int event_pending(struct event *e,short what,struct timeval *out)
{ (void)what; (void)out; return e && e->pending; }
static int test_clock_gettime(clockid_t clock,struct timespec *out)
{ CHECK(clock==CLOCK_MONOTONIC); out->tv_sec=observed_ns/UINT64_C(1000000000); out->tv_nsec=observed_ns%UINT64_C(1000000000); return 0; }
#define clock_gettime test_clock_gettime
#define OUTPUTS_STOP_TIMEOUT 10
static struct timeval outputs_stop_timeout={10,0};
static output_status_cb callback_get(struct output_device *device) { return callbacks[device->id-1]; }
static int callback_add(struct output_device *device,output_status_cb callback)
{ callbacks[device->id-1]=callback; ++callback_changes; return 1; }
static int device_state_update(struct output_device *device,int ret)
{ if (ret<0) device->state=OUTPUT_STATE_FAILED; return ret; }
static void backend_cb_set(struct output_device *device,int token) { (void)device; (void)token; }
static int backend_stop(struct output_device *device,int token)
{ (void)token; ++stops; device->state=OUTPUT_STATE_STARTUP; return 1; }
static struct { bool disabled; int (*device_stop)(struct output_device *,int); void (*device_cb_set)(struct output_device *,int); } backend={false,backend_stop,backend_cb_set};
static __typeof__(&backend) outputs[]={&backend};
/* @ACTUAL_OUTPUT_REMAINING@ */
/* @ACTUAL_OUTPUT_STOP@ */
/* @ACTUAL_OUTPUT_DELAYED_STOP@ */
/* @ACTUAL_OUTPUT_STOP_TIMER@ */
/* @ACTUAL_OUTPUT_HOLD@ */
/* @ACTUAL_OUTPUT_RELEASE@ */
static bool outputs_shiri_callback_pending(uint64_t id,output_status_cb cb)
{ return id>=1 && id<=2 && callbacks[id-1]==cb; }
static void outputs_device_cb_set(struct output_device *device,output_status_cb cb)
{ callback_add(device,cb); }
static void device_streaming_cb(struct output_device *device,enum output_device_state state)
{ (void)device; (void)state; }
static int outputs_device_start(struct output_device *device,output_status_cb cb,bool probe)
{
  CHECK(!probe); ++starts; callbacks[device->id-1]=cb;
  if (device->id==start_fail_id) return -1;
  CHECK(outputs_device_session_add(device->id,(void *)(uintptr_t)(100+device->id))==0); device->state=OUTPUT_STATE_STARTUP;
  if (start_inline) {
    callbacks[device->id-1]=NULL; device->state=OUTPUT_STATE_CONNECTED; cb(device,OUTPUT_STATE_CONNECTED); return 0;
  }
  return 1;
}
#define outputs_device_stop_delayed counted_delayed_stop
static int counted_delayed_stop(struct output_device *device,output_status_cb cb)
{ ++delays;
#undef outputs_device_stop_delayed
  return outputs_device_stop_delayed(device,cb);
#define outputs_device_stop_delayed counted_delayed_stop
}
/* @ACTUAL_WARM_LIFECYCLE@ */
#undef outputs_device_stop_delayed

static void reset(void)
{
  shiri_warm_event_clear(); memset(&shiri_warm_state,0,sizeof(shiri_warm_state)); shiri_warm_retired_generation=0;
  memset(devices,0,sizeof(devices)); memset(stop_events,0,sizeof(stop_events)); memset(callbacks,0,sizeof(callbacks));
  memset(&media,0,sizeof(media)); memset(&shiri_source_state,0,sizeof(shiri_source_state));
  shiri_source_state.initialized=true; shiri_source_state.last.operation_generation=1;
  shiri_source_state.last.owner.incarnation[0]=1; shiri_source_state.last.owner.generation=1;
  shiri_source_wait.active=false; shiri_speech_start.pending=0; player_state=PLAY_STOPPED; bed_wanted=false;
  devices[0].id=1; devices[0].selected=true; devices[0].stop_timer=&stop_events[0];
  devices[1].id=2; devices[1].stop_timer=&stop_events[1]; devices[0].next=&devices[1];
  devices[0].shiri_session_serial=1; outputs_shiri_session_serial=1;
  observed_ns=UINT64_C(10000000000); starts=stops=delays=timer_changes=callback_changes=start_fail_id=0; start_inline=false;
}
static struct shiri_warm_param request(unsigned id,uint64_t generation,enum shiri_warm_action action,uint64_t deadline)
{
  struct shiri_warm_param p={0}; p.request.room[0]=1; p.request.launch[0]=2; p.request.warm[0]=id;
  p.request.generation=generation; p.request.action=action; p.request.deadline_ns=deadline; return p;
}
static int run(struct shiri_warm_param *p)
{ int result; CHECK(shiri_warm_command(p,&result)==COMMAND_END); return result; }
static void connected(unsigned index)
{
  struct output_device *device=&devices[index]; output_status_cb cb=callbacks[index];
  CHECK(cb); callbacks[index]=NULL; device->state=OUTPUT_STATE_CONNECTED; cb(device,OUTPUT_STATE_CONNECTED);
}
static void stopped(unsigned index)
{
  struct output_device *device=&devices[index]; output_status_cb cb=callbacks[index];
  CHECK(cb); callbacks[index]=NULL; outputs_device_session_remove(device->id); device->state=OUTPUT_STATE_STOPPED; cb(device,OUTPUT_STATE_STOPPED);
}
static void cold_setup_observe_renew_expire(void)
{
  reset(); struct shiri_warm_param p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(60000000000));
  CHECK(run(&p)==0 && p.reply.pending && !p.reply.connected && starts==1 && stops==0);
  CHECK(shiri_warm_event->delay.tv_sec==3 && !shiri_warm_state.connected);
  observed_ns+=UINT64_C(1000000000); connected(0);
  CHECK(shiri_warm_state.connected && shiri_warm_state.prepared_ns==observed_ns && shiri_warm_state.pending==0);
  CHECK(devices[0].shiri_warm_until_ns==p.request.deadline_ns);
  p=request(3,1,SHIRI_WARM_OBSERVE,0); __typeof__(shiri_warm_state) before=shiri_warm_state;
  struct output_device output_before=devices[0]; unsigned changes=timer_changes,callbacks_before=callback_changes;
  CHECK(run(&p)==0 && p.reply.connected && p.reply.outputs==1 && !p.reply.pending);
  CHECK(!memcmp(&before,&shiri_warm_state,sizeof(before)) && !memcmp(&output_before,&devices[0],sizeof(output_before)));
  CHECK(changes==timer_changes && callbacks_before==callback_changes); /* Strictly passive. */
  p=request(3,1,SHIRI_WARM_DEADLINE,observed_ns+UINT64_C(20000000000));
  CHECK(run(&p)==0 && p.reply.connected && starts==1 && devices[0].shiri_warm_until_ns==p.request.deadline_ns);
  observed_ns=p.request.deadline_ns;
  shiri_warm_timeout(-1,0,(void *)(uintptr_t)1);
  CHECK(shiri_warm_state.terminal && !shiri_warm_state.active && devices[0].shiri_warm_until_ns==0);
  CHECK(starts==1 && stops==0 && delays==1); /* Ordinary finite10s teardown; no reconnect. */
  p=request(3,1,SHIRI_WARM_RELEASE,0); changes=timer_changes;
  CHECK(run(&p)==0 && p.reply.terminal && timer_changes==changes);
  p=request(3,2,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000));
  CHECK(run(&p)==SHIRI_SOURCE_CONFLICT && starts==1); /* Terminal ID cannot resurrect. */
}
static void ordered_cancel_and_late_start(void)
{
  reset(); struct shiri_warm_param p=request(4,2,SHIRI_WARM_RELEASE,0);
  CHECK(run(&p)==0 && p.reply.terminal && starts==0);
  CHECK(run(&p)==0 && p.reply.terminal && starts==0); /* Exact unknown retirement replay. */
  p=request(4,2,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000));
  CHECK(run(&p)==SHIRI_SOURCE_CONFLICT && starts==0); /* Retirement precedes uncertain acquire. */
  p=request(5,3,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000)); CHECK(run(&p)==0 && starts==1);
  p=request(5,3,SHIRI_WARM_RELEASE,0); CHECK(run(&p)==0 && p.reply.pending && stops==1);
  CHECK(callbacks[0]==device_shiri_warm_stop_cb); /* Earlier START token was replaced. */
  p=request(6,4,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000));
  CHECK(run(&p)==SHIRI_SOURCE_CONFLICT && starts==1);
  stopped(0); CHECK(shiri_warm_state.pending==0);
  CHECK(run(&p)==0 && starts==2); connected(0);
  __typeof__(shiri_warm_state) before=shiri_warm_state; struct output_device output_before=devices[0];
  p=request(5,3,SHIRI_WARM_RELEASE,0);
  CHECK(run(&p)==0 && p.reply.terminal && !memcmp(&before,&shiri_warm_state,sizeof(before)));
  CHECK(!memcmp(&output_before,&devices[0],sizeof(output_before)) && stops==1);
}
static void music_voice_and_replaced_sessions_survive(void)
{
  for (unsigned kind=0;kind<7;++kind) {
    reset(); devices[0].session=(void *)101; devices[0].state=OUTPUT_STATE_CONNECTED;
    struct shiri_warm_param p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(60000000000)); CHECK(run(&p)==0 && p.reply.connected && !starts);
    if (kind==0) { shiri_source_state.last.owner.session[0]=8; player_state=PLAY_PAUSED; }
    if (kind==1) player_state=PLAY_PLAYING;
    if (kind==2) media.active=1;
    if (kind==3) media.running=1;
    if (kind==4) media.queue_frames=100;
    if (kind==5) bed_wanted=true;
    if (kind==6) { devices[0].session=(void *)999; devices[0].shiri_warm_session=(void *)999; devices[0].shiri_warm_until_ns=UINT64_MAX; }
    struct shiri_source_state source_before=shiri_source_state; struct shiri_speech_status voice_before=media;
    p=request(3,1,SHIRI_WARM_RELEASE,0); CHECK(run(&p)==0 && !p.reply.connected && p.reply.terminal);
    CHECK(!stops && !delays && !memcmp(&source_before,&shiri_source_state,sizeof(source_before)) && !memcmp(&voice_before,&media,sizeof(voice_before)));
    if (kind==6) CHECK(devices[0].shiri_warm_until_ns==UINT64_MAX && devices[0].shiri_warm_session==(void *)999);
  }
  reset(); struct shiri_warm_param p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000)); CHECK(run(&p)==0 && p.reply.pending);
  shiri_source_state.last.owner.session[0]=9; callbacks[0]=device_streaming_cb; /* Successor source replaced the START token. */
  unsigned changes=callback_changes; shiri_warm_timeout(-1,0,(void *)(uintptr_t)1);
  CHECK(!stops && !delays && callbacks[0]==device_streaming_cb && callback_changes==changes);
}
static void rejection_is_atomic_and_setup_is_bounded(void)
{
  reset(); devices[0].session=(void *)101; devices[0].state=OUTPUT_STATE_CONNECTED;
  struct shiri_warm_param p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+SHIRI_WARM_MAX_NS); CHECK(run(&p)==0 && p.reply.connected);
  for (unsigned kind=0;kind<6;++kind) {
    p=request(3,1,SHIRI_WARM_DEADLINE,observed_ns+UINT64_C(10000000000));
    if (kind==0) p.request.launch[0]=7;
    if (kind==1) p.request.generation=0;
    if (kind==2) p.request.deadline_ns=observed_ns;
    if (kind==3) p.request.deadline_ns=observed_ns+SHIRI_WARM_MAX_NS+1;
    if (kind==4) p.request.warm[0]=9;
    if (kind==5) p.request.action=99;
    __typeof__(shiri_warm_state) before=shiri_warm_state; struct output_device output_before=devices[0];
    CHECK(run(&p)<0 && !memcmp(&before,&shiri_warm_state,sizeof(before)) && !memcmp(&output_before,&devices[0],sizeof(output_before)));
  }
  reset(); p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000)); CHECK(run(&p)==0 && p.reply.pending);
  observed_ns+=SHIRI_WARM_SETUP_NS; shiri_warm_timeout(-1,0,(void *)(uintptr_t)1);
  CHECK(shiri_warm_state.failed && shiri_warm_state.terminal && stops==1 && starts==1);
  stopped(0); CHECK(shiri_warm_state.pending==0);
  shiri_warm_timeout(-1,0,(void *)(uintptr_t)1); CHECK(starts==1 && stops==1);
}
static void exact_hold_and_direct_stop(void)
{
  reset(); struct output_device *d=&devices[0]; d->session=(void *)101; d->state=OUTPUT_STATE_CONNECTED;
  CHECK(outputs_shiri_warm_hold(d,d->session,d->shiri_session_serial,observed_ns+UINT64_C(60000000000))==0 && !timer_changes && !callback_changes);
  CHECK(outputs_device_stop_delayed(d,device_streaming_cb)==1 && d->stop_timer->delay.tv_sec==60);
  unsigned callbacks_before=callback_changes; observed_ns+=UINT64_C(1000000000);
  stop_timer_cb(-1,0,d); CHECK(!stops && callback_changes==callbacks_before && d->stop_timer->delay.tv_sec==59);
  observed_ns=d->shiri_warm_until_ns; stop_timer_cb(-1,0,d);
  CHECK(stops==1 && d->shiri_warm_until_ns==0 && !d->shiri_warm_session);
  reset(); d=&devices[0]; d->session=(void *)101; d->state=OUTPUT_STATE_CONNECTED;
  CHECK(outputs_shiri_warm_hold(d,d->session,d->shiri_session_serial,observed_ns+UINT64_C(60000000000))==0);
  CHECK(outputs_device_stop(d,device_streaming_cb)==1 && stops==1 && !d->shiri_warm_until_ns);
  d->session=(void *)999; d->shiri_warm_session=(void *)999; d->shiri_warm_until_ns=UINT64_MAX;
  outputs_shiri_warm_release(d,(void *)101,1); CHECK(d->shiri_warm_until_ns==UINT64_MAX);
  d->selected=false; CHECK(outputs_shiri_warm_remaining(d)==0);
}
static void expiry_token_retirement_and_session_address_reuse(void)
{
  for (unsigned kind=0;kind<2;++kind) {
    reset(); devices[0].session=(void *)101; devices[0].state=OUTPUT_STATE_CONNECTED;
    struct shiri_warm_param p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(1000000000)); CHECK(run(&p)==0 && p.reply.connected);
    uint64_t original=p.request.deadline_ns; observed_ns=original;
    p=request(3,1,kind ? SHIRI_WARM_DEADLINE : SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000));
    CHECK(run(&p)==SHIRI_SOURCE_CONFLICT && shiri_warm_state.terminal && shiri_warm_state.request.deadline_ns==original);
  }
  reset(); struct shiri_warm_param p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000)); CHECK(run(&p)==0);
  p=request(3,1,SHIRI_WARM_RELEASE,0); CHECK(run(&p)==0 && p.reply.pending);
  callbacks[0]=device_streaming_cb; shiri_source_state.last.owner.session[0]=9;
  __typeof__(shiri_warm_state) before=shiri_warm_state;
  p=request(3,1,SHIRI_WARM_OBSERVE,0); CHECK(run(&p)==0 && !p.reply.pending && !memcmp(&before,&shiri_warm_state,sizeof(before)));
  p=request(3,1,SHIRI_WARM_RELEASE,0); CHECK(run(&p)==0 && !p.reply.pending && shiri_warm_state.pending==0 && stops==1);
  reset(); CHECK(outputs_device_session_add(1,(void *)101)==0); devices[0].state=OUTPUT_STATE_CONNECTED;
  p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000)); CHECK(run(&p)==0 && p.reply.connected);
  uint64_t old_serial=devices[0].shiri_session_serial;
  outputs_device_session_remove(1); CHECK(!devices[0].shiri_warm_session && !devices[0].shiri_warm_until_ns);
  CHECK(outputs_device_session_add(1,(void *)101)==0); devices[0].state=OUTPUT_STATE_CONNECTED;
  CHECK(devices[0].shiri_session_serial>old_serial);
  CHECK(outputs_shiri_warm_hold(&devices[0],devices[0].session,devices[0].shiri_session_serial,observed_ns+UINT64_C(60000000000))==0);
  struct output_device successor=devices[0];
  p=request(3,1,SHIRI_WARM_OBSERVE,0); CHECK(run(&p)==0 && !p.reply.connected && !memcmp(&successor,&devices[0],sizeof(successor)));
  CHECK(outputs_shiri_warm_hold(&devices[0],devices[0].session,old_serial,observed_ns+UINT64_C(30000000000))<0);
  outputs_shiri_warm_release(&devices[0],devices[0].session,old_serial);
  CHECK(!memcmp(&successor,&devices[0],sizeof(successor)));
  shiri_warm_timeout(-1,0,(void *)(uintptr_t)1);
  CHECK(!stops && !delays && !memcmp(&successor,&devices[0],sizeof(successor)));
  outputs_shiri_session_serial=UINT64_MAX;
  CHECK(outputs_device_session_add(1,(void *)999)<0 && !memcmp(&successor,&devices[0],sizeof(successor)));
}
static void inline_partial_failure_and_lost_start(void)
{
  reset(); start_inline=true;
  struct shiri_warm_param p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000));
  CHECK(run(&p)==0 && p.reply.connected && !p.reply.pending && starts==1);
  CHECK(devices[0].shiri_warm_until_ns==p.request.deadline_ns);
  reset(); devices[1].selected=true; start_fail_id=2;
  p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000));
  CHECK(run(&p)==0 && p.reply.terminal && p.reply.pending && starts==2 && stops==1);
  CHECK(!devices[1].session && callbacks[0]==device_shiri_warm_stop_cb);
  stopped(0); CHECK(shiri_warm_state.pending==0);
  reset(); p=request(3,1,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000)); CHECK(run(&p)==0 && p.reply.pending);
  devices[0].session=NULL; callbacks[0]=NULL; /* Network session disappeared without a START receipt. */
  shiri_warm_timeout(-1,0,(void *)(uintptr_t)1);
  CHECK(shiri_warm_state.terminal && shiri_warm_state.pending==0 && stops==0 && starts==1);
  p=request(4,2,SHIRI_WARM_ACQUIRE,observed_ns+UINT64_C(10000000000));
  CHECK(run(&p)==0 && p.reply.pending && starts==2); /* Explicit new lease; timer never retries. */
}
int main(void)
{
  cold_setup_observe_renew_expire(); ordered_cancel_and_late_start(); music_voice_and_replaced_sessions_survive();
  rejection_is_atomic_and_setup_is_bounded(); exact_hold_and_direct_stop(); inline_partial_failure_and_lost_start();
  expiry_token_retirement_and_session_address_reuse(); shiri_warm_event_clear();
  printf("warm1: %u actual-source lifecycle checks; no PCM/socket/audio, finite setup/expiry, exact session holds and media preservation\n",checks);
  return 0;
}

#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <inttypes.h>
#include <stdio.h>
#include <time.h>
#include <errno.h>
#include <stdarg.h>
#include <pthread.h>
#include "shiri_source.h"
#ifdef __APPLE__
struct itimerspec { struct timespec it_interval,it_value; };
#endif
#ifndef TIMER_ABSTIME
#define TIMER_ABSTIME 1
#endif
#define TFD_TIMER_ABSTIME TIMER_ABSTIME
#define HAVE_TIMERFD 1
#define PLAY_PLAYING 1
#define PLAY_PAUSED 2
#define PLAY_STOPPED 3
#define DATA_KIND_PIPE 1
#define E_LOG 0
#define L_PLAYER 0
static char last_reason[512];
static void fixture_log(int level,int area,const char *fmt,...) {va_list ap;(void)level;(void)area;va_start(ap,fmt);vsnprintf(last_reason,sizeof(last_reason),fmt,ap);va_end(ap);}
#define DPRINTF fixture_log
static int pb_timer_fd, pb_timer_ev_storage, *pb_timer_ev=&pb_timer_ev_storage;
static int event_active, timer_calls, timer_failure_call, clock_reads, clock_failure_read;
static int aborts, callback_token, pending_callbacks, command_completions, delayed_stops;
static int output_fail, start_fail, seal_fail, arm_fail, peek_status, peek_advances, starts, arms, flushes, stopped_sessions;
static pthread_mutex_t shiri_gate_lock=PTHREAD_MUTEX_INITIALIZER;
static struct shiri_pcm_owner shiri_owner;
static bool shiri_armed;
static uint64_t shiri_operation;
#define fixture_operation shiri_operation
static int fixture_state, player_state;
static struct timespec fixture_now, next_anchor, peek_returns_at, seen_timer;
static int seen_flags;
static bool pb_timer_native, pb_timer_native_anchored;
static uint64_t pb_timer_native_operation;
static struct timespec pb_timer_native_anchor,pb_timer_native_wait_until,player_tick_interval;
static struct { int data_kind;} pipe_source;
static struct {void *reading_now;struct timespec start_ts,pts; int read_deficit;} pb_session_storage;
struct fixture_pb_session {struct {int data_kind;} *reading_now;struct timespec start_ts,pts; int read_deficit;};
static struct fixture_pb_session pb_session;
static int timespec_cmp(struct timespec a,struct timespec b){return a.tv_sec==b.tv_sec?(a.tv_nsec>b.tv_nsec)-(a.tv_nsec<b.tv_nsec):(a.tv_sec>b.tv_sec)-(a.tv_sec<b.tv_sec);}
static int fixture_clock_gettime(int kind,struct timespec *out){assert(kind==CLOCK_MONOTONIC);clock_reads++;if(clock_reads==clock_failure_read)return -1;*out=fixture_now;return 0;}
#define clock_gettime fixture_clock_gettime
static int timerfd_settime(int fd,int flags,const struct itimerspec *tick,void *old){(void)fd;(void)old;timer_calls++;seen_timer=tick->it_value;seen_flags=flags;if(timer_calls==timer_failure_call)return -1;return 0;}
static int event_add(void *event,void *timeout){(void)event;(void)timeout;event_active=1;return 0;}
static int event_del(void *event){(void)event;event_active=0;return 0;}
static int input_pipe_shiri_state(uint64_t *op){*op=fixture_operation;return shiri_armed?1:-1;}
static int input_peek_sync(struct timespec *anchor){*anchor=next_anchor;if(peek_advances)fixture_now=peek_returns_at;return peek_status;}
static int input_pipe_shiri_seal(uint64_t op){fixture_operation=op;fixture_state=-1;shiri_armed=false;return seal_fail?-1:0;}
static int input_pipe_shiri_arm(const struct shiri_pcm_owner *owner,uint64_t op){arms++;if(arm_fail||op!=fixture_operation||shiri_armed)return -1;fixture_state=1;shiri_armed=true;shiri_owner=*owner;return 0;}
static void input_flush(void *arg){(void)arg;}
static void outputs_resampling_reset(void){}
static void outputs_metadata_purge(void){}
static void outputs_stop_delayed_cancel(void){delayed_stops=0;}
struct output_device{int id;};
enum output_device_state{OUTPUT_STATE_CONNECTED,OUTPUT_STATE_FAILED,OUTPUT_STATE_PASSWORD};
static struct output_device device;
static void device_streaming_cb(struct output_device *device_,enum output_device_state status){(void)device_;(void)status;}
static void outputs_device_stop_delayed(struct output_device *device_,void (*cb)(struct output_device*,enum output_device_state)){(void)device_;(void)cb;delayed_stops++;}
static void outputs_device_cb_set(struct output_device *device_,void (*cb)(struct output_device*,enum output_device_state)){(void)device_;(void)cb;}
static int outputs_shiri_flush(void (*cb)(struct output_device*,enum output_device_state),int *failed){(void)cb;flushes++;*failed=output_fail;pending_callbacks=1;callback_token=100;return 1;}
static int outputs_shiri_start(void (*cb)(struct output_device*,enum output_device_state),int *failed){(void)cb;starts++;*failed=start_fail;return 0;}
static void outputs_stop(void (*cb)(struct output_device*,enum output_device_state)){(void)cb;aborts++;callback_token=101;}
static void *cmdbase;
static void commands_exec_end(void *base_,int result){(void)base_;(void)result;command_completions++;pending_callbacks=0;}
static void pb_session_stop(void){stopped_sessions++;event_active=0;player_state=PLAY_STOPPED;pb_session.reading_now=NULL;}
static int clear_queue_on_stop_disabled=1;
static void db_queue_clear(int all){(void)all;}
enum command_state{COMMAND_END,COMMAND_PENDING};

static struct shiri_source_state shiri_source_state;
static int shiri_source_flush_failed,shiri_source_start_failed;
struct shiri_source_param {
  struct shiri_source_request request;
  bool replay;
  bool restart_outputs;
};
static int
pb_timer_native_prepare(void)
{
  struct itimerspec tick;
  struct timespec now, anchor;
  uint64_t operation;
  int state, ret;
  state = input_pipe_shiri_state(&operation);
  if (state != 1 || operation != pb_timer_native_operation)
    return -1;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    return -1;
  if (pb_timer_native_anchored)
    return timespec_cmp(now, pb_timer_native_anchor) < 0 ? 0 : 1;
  ret = input_peek_sync(&anchor);
  /* The input mutex can wait across the native deadline. Admit the original
   * anchor against a fresh clock after peek, including a missing marker. */
  if (ret < 0 || clock_gettime(CLOCK_MONOTONIC, &now) < 0
      || timespec_cmp(now, pb_timer_native_wait_until) >= 0)
    return -1;
  if (!ret)
    return 0;
  /* Never replace a late native anchor with the receive or timer clock. */
  if (timespec_cmp(anchor, now) <= 0)
    return -1;
  tick.it_interval = player_tick_interval;
  tick.it_value = anchor;
#ifdef HAVE_TIMERFD
  ret = timerfd_settime(pb_timer_fd, TFD_TIMER_ABSTIME, &tick, NULL);
#else
  ret = timer_settime(pb_timer, TIMER_ABSTIME, &tick, NULL);
#endif
  if (ret < 0)
    return -1;
  pb_timer_native_anchor = anchor;
  pb_timer_native_anchored = true;
  /* Control/quality markers must not substitute CLOCK_MONOTONIC now for this
   * first sample's anchor when the session has not read any PCM yet. */
  pb_session.start_ts = anchor;
  pb_session.pts = anchor;
  return 0;
}

static int
pb_timer_start(void)
{
  struct itimerspec tick;
  int ret;

  // The stop timers will be active if we have recently paused, but now that the
  // playback loop has been kicked off, we deactivate them
  outputs_stop_delayed_cancel();

  ret = event_add(pb_timer_ev, NULL);
  if (ret < 0)
    {
      DPRINTF(E_LOG, L_PLAYER, "Could not add playback timer\n");

      return -1;
    }

  pb_timer_native = false;
  pb_timer_native_anchored = false;
  pb_timer_native_operation = 0;
  if (pb_session.reading_now && pb_session.reading_now->data_kind == DATA_KIND_PIPE)
    {
      ret = input_pipe_shiri_state(&pb_timer_native_operation);
      if (ret < 0)
        return -1;
      pb_timer_native = (ret == 1);
      if (pb_timer_native)
        {
          if (clock_gettime(CLOCK_MONOTONIC, &pb_timer_native_wait_until) < 0)
            return -1;
          pb_timer_native_wait_until.tv_sec += 5;
        }
    }
  tick.it_interval = player_tick_interval;
  tick.it_value = player_tick_interval;

#ifdef HAVE_TIMERFD
  ret = timerfd_settime(pb_timer_fd, 0, &tick, NULL);
#else
  ret = timer_settime(pb_timer, 0, &tick, NULL);
#endif
  if (ret < 0)
    {
      DPRINTF(E_LOG, L_PLAYER, "Could not arm playback timer: %s\n", strerror(errno));

      return -1;
    }

  return 0;
}

static int
pb_timer_stop(void)
{
  struct itimerspec tick;
  int ret;

  event_del(pb_timer_ev);
  pb_timer_native = false;
  pb_timer_native_anchored = false;
  pb_timer_native_operation = 0;

  memset(&tick, 0, sizeof(struct itimerspec));

#ifdef HAVE_TIMERFD
  ret = timerfd_settime(pb_timer_fd, 0, &tick, NULL);
#else
  ret = timer_settime(pb_timer, 0, &tick, NULL);
#endif
  if (ret < 0)
    {
      DPRINTF(E_LOG, L_PLAYER, "Could not disarm playback timer: %s\n", strerror(errno));
      return -1;
    }

  return 0;
}

static void
device_shiri_flush_cb(struct output_device *device, enum output_device_state status)
{
  if (!device || status == OUTPUT_STATE_FAILED)
    shiri_source_flush_failed = 1;
  if (device)
    outputs_device_stop_delayed(device, device_streaming_cb);
  commands_exec_end(cmdbase, shiri_source_flush_failed ? SHIRI_SOURCE_FAILURE : 0);
}

static void
device_shiri_start_cb(struct output_device *device, enum output_device_state status)
{
  if (!device || status == OUTPUT_STATE_FAILED || status == OUTPUT_STATE_PASSWORD)
    shiri_source_start_failed = 1;
  else
    outputs_device_cb_set(device, device_streaming_cb);
  commands_exec_end(cmdbase, shiri_source_start_failed ? SHIRI_SOURCE_FAILURE : 0);
}

static void
pb_abort(void)
{
  // Immediate stop of all outputs
  outputs_stop(device_streaming_cb);
  outputs_metadata_purge();

  pb_session_stop();

  if (!clear_queue_on_stop_disabled)
    db_queue_clear(0);
}

static enum command_state
shiri_source_seal_flush(void *arg, int *retval)
{
  struct shiri_source_param *param = arg;
  int result, pending;

  result = shiri_source_check(&shiri_source_state, &param->request);
  if (result <= 0)
    {
      param->replay = (result == SHIRI_SOURCE_REPLAY);
      *retval = result;
      return COMMAND_END;
    }

  shiri_source_begin(&shiri_source_state, &param->request);
  *retval = input_pipe_shiri_seal(param->request.operation_generation);
  if (*retval < 0)
    return COMMAND_END;
  input_flush(NULL);
  outputs_resampling_reset();
  pb_session.read_deficit = 0;
  shiri_source_flush_failed = 0;
  param->restart_outputs = (player_state == PLAY_PLAYING);
  pending = outputs_shiri_flush(device_shiri_flush_cb, &shiri_source_flush_failed);
  outputs_metadata_purge();
  *retval = pending > 0 ? pending : (shiri_source_flush_failed ? SHIRI_SOURCE_FAILURE : 0);
  return pending > 0 ? COMMAND_PENDING : COMMAND_END;
}

static enum command_state
shiri_source_restart(void *arg, int *retval)
{
  int pending;
  (void)arg;
  shiri_source_start_failed = 0;
  outputs_stop_delayed_cancel();
  pending = outputs_shiri_start(device_shiri_start_cb, &shiri_source_start_failed);
  *retval = pending > 0 ? pending : (shiri_source_start_failed ? SHIRI_SOURCE_FAILURE : 0);
  return pending > 0 ? COMMAND_PENDING : COMMAND_END;
}

static enum command_state
shiri_source_arm(void *arg, int *retval)
{
  struct shiri_source_param *param = arg;
  if (!shiri_source_state.initialized || !shiri_source_state.faulted
      || shiri_source_flush_failed || shiri_source_start_failed
      || shiri_source_state.last.operation_generation != param->request.operation_generation
      || !shiri_source_owner_equal(&shiri_source_state.last.owner, &param->request.owner))
    {
      *retval = SHIRI_SOURCE_FAILURE;
      return COMMAND_END;
    }
  *retval = input_pipe_shiri_arm(&param->request.owner, param->request.operation_generation);
  if (*retval == 0)
    shiri_source_state.faulted = false;
  return COMMAND_END;
}

static void original_native_tick_branch(void){int ret;
  if (pb_timer_native)
    {
      ret = pb_timer_native_prepare();
      if (ret < 0)
        {
          DPRINTF(E_LOG, L_PLAYER, "Native input missed or lost its initial presentation anchor\n");
          pb_abort();
          return;
        }
      if (ret == 0)
        return; /* Waiting/rearmed: no PCM or output effects before the anchor. */
    }

}

static void reset(void){
 event_active=timer_calls=clock_reads=aborts=callback_token=pending_callbacks=command_completions=delayed_stops=0;
 timer_failure_call=clock_failure_read=output_fail=start_fail=seal_fail=arm_fail=peek_advances=starts=arms=flushes=stopped_sessions=0;peek_status=1;
 fixture_now=(struct timespec){100,0};next_anchor=(struct timespec){100,750000000};
 player_tick_interval=(struct timespec){0,10000000};fixture_operation=2;fixture_state=1;shiri_armed=true;player_state=PLAY_PLAYING;
 pipe_source.data_kind=DATA_KIND_PIPE;pb_session.reading_now=(void*)&pipe_source;
 memset(&shiri_source_state,0,sizeof(shiri_source_state));shiri_source_state.initialized=true;
 shiri_source_state.last.owner.incarnation[0]=1;shiri_source_state.last.owner.session[0]=2;
 shiri_source_state.last.owner.epoch=1;shiri_source_state.last.owner.generation=1;shiri_source_state.last.operation_generation=2;
 assert(pb_timer_start()==0);pb_timer_native_anchored=true;pb_timer_native_anchor=(struct timespec){99,0};
 last_reason[0]=0;shiri_source_flush_failed=shiri_source_start_failed=0;
}
static struct shiri_source_param next_request(void){struct shiri_source_param p={0};p.request=shiri_source_state.last;p.request.operation_generation++;p.request.owner.generation++;return p;}
static void dispatch_timer(void){if(event_active)original_native_tick_branch();}
static void complete_flush(void){if(pending_callbacks&&callback_token==100)device_shiri_flush_cb(&device,OUTPUT_STATE_CONNECTED);}
int main(void){
 struct shiri_source_param p;int ret;enum command_state state;
 reset();p=next_request();state=shiri_source_seal_flush(&p,&ret);assert(state==COMMAND_PENDING&&ret==1);dispatch_timer();
#ifdef EXPECT_OLD_RACE
 assert(aborts==1&&callback_token==101);complete_flush();assert(command_completions==0&&pending_callbacks==1);
 puts("Original source transition: pending FLUSH overwritten by timer abort; completion lost");return 0;
#else
 assert(aborts==0&&callback_token==100&&!event_active);complete_flush();assert(command_completions==1&&!pending_callbacks);
 assert(shiri_source_restart(NULL,&ret)==COMMAND_END&&ret==0);dispatch_timer();assert(aborts==0&&!event_active);
 assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret==0);assert(event_active&&pb_timer_native_operation==3&&!shiri_source_state.faulted);
 assert(pb_timer_native_prepare()==0&&pb_timer_native_anchored);assert(timespec_cmp(seen_timer,next_anchor)==0&&seen_flags==TFD_TIMER_ABSTIME);
 assert(timespec_cmp(pb_session.pts,next_anchor)==0&&timespec_cmp(pb_session.start_ts,next_anchor)==0);
 fixture_now=next_anchor;assert(pb_timer_native_prepare()==1);assert(aborts==0&&callback_token==100);
 puts("Fixed source transition: pending FLUSH completes, rearm preserves new original PCM anchor");
 reset();p.request=shiri_source_state.last;assert(shiri_source_seal_flush(&p,&ret)==COMMAND_END&&ret==SHIRI_SOURCE_REPLAY);assert(event_active&&timer_calls==1);
 puts("Exact replay leaves original timer and generation unchanged");
 reset();p=next_request();p.request.operation_generation++;assert(shiri_source_seal_flush(&p,&ret)==COMMAND_END&&ret==SHIRI_SOURCE_CONFLICT);assert(event_active&&timer_calls==1&&fixture_operation==2);
 puts("Stale/wrong source request has no timer or gate effects");
 reset();p=next_request();assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();player_state=PLAY_PAUSED;assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret==0);assert(!event_active&&!shiri_source_state.faulted);
 reset();p=next_request();assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();player_state=PLAY_STOPPED;assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret==0);assert(!event_active&&!shiri_source_state.faulted);
 puts("User pause/stop during asynchronous output phase stays stopped");
 reset();p=next_request();assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();p.request.owner.generation++;assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret==SHIRI_SOURCE_FAILURE);assert(!event_active&&shiri_source_state.faulted);
 reset();p=next_request();assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();arm_fail=1;assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret<0);assert(!event_active&&shiri_source_state.faulted);
 reset();p=next_request();assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();timer_failure_call=timer_calls+1;assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret<0);assert(shiri_source_state.faulted&&!event_active&&!shiri_armed);
 puts("Wrong arm, backend arm error and timer rearm error remove event watch and fence input");
 reset();pb_timer_native_anchored=false;next_anchor=fixture_now;assert(pb_timer_native_prepare()<0&&!pb_timer_native_anchored&&strstr(last_reason,"anchor_late"));
 reset();pb_timer_native_anchored=false;peek_status=0;peek_advances=1;peek_returns_at=pb_timer_native_wait_until;assert(pb_timer_native_prepare()<0&&!pb_timer_native_anchored&&strstr(last_reason,"anchor_timeout"));
 reset();pb_timer_native_anchored=false;peek_advances=1;peek_returns_at=next_anchor;assert(pb_timer_native_prepare()<0&&!pb_timer_native_anchored&&strstr(last_reason,"anchor_late"));
 reset();fixture_state=-1;shiri_armed=false;assert(pb_timer_native_prepare()<0&&strstr(last_reason,"source_gate"));
 puts("Late original P, missing-anchor deadline, mutex-stalled anchor and unarmed source remain rejected with distinct reasons");
 reset();p=next_request();timer_failure_call=timer_calls+1;assert(shiri_source_seal_flush(&p,&ret)==COMMAND_END&&ret<0);assert(!event_active&&!shiri_armed&&shiri_source_state.faulted&&flushes==0);
 reset();p=next_request();seal_fail=1;assert(shiri_source_seal_flush(&p,&ret)==COMMAND_END&&ret<0);assert(!event_active&&!shiri_armed&&shiri_source_state.faulted&&flushes==0);
 reset();p=next_request();assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);device_shiri_flush_cb(&device,OUTPUT_STATE_FAILED);assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret<0);assert(!event_active&&!shiri_armed&&shiri_source_state.faulted);
 reset();p=next_request();assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();start_fail=1;assert(shiri_source_restart(NULL,&ret)==COMMAND_END&&ret<0);assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret<0);assert(!event_active&&!shiri_armed&&shiri_source_state.faulted);
 puts("Stop, seal, FLUSH and START failures retain no consuming timer or armed input");
 reset();p=next_request();memset(p.request.owner.session,0,16);p.request.owner.generation=1;assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret==0);assert(player_state==PLAY_STOPPED&&!event_active&&stopped_sessions==1&&!shiri_source_state.faulted);
 p=next_request();p.request.owner.session[0]=3;p.request.owner.epoch++;p.request.owner.generation=1;assert(shiri_source_seal_flush(&p,&ret)==COMMAND_PENDING);complete_flush();assert(p.restart_outputs&&!p.restart_timer);assert(shiri_source_restart(NULL,&ret)==COMMAND_END&&ret==0);assert(starts==1);assert(shiri_source_arm(&p,&ret)==COMMAND_END&&ret==0);assert(!event_active&&shiri_armed&&!shiri_source_state.faulted);
 fixture_now=(struct timespec){101,0};next_anchor=(struct timespec){101,500000000};pb_session.reading_now=(void*)&pipe_source;player_state=PLAY_PLAYING;assert(pb_timer_start()==0);assert(pb_timer_native_operation==p.request.operation_generation);assert(pb_timer_native_prepare()==0&&pb_timer_native_anchored&&timespec_cmp(pb_session.pts,next_anchor)==0);
 puts("END closes native music session; subsequent BEGIN completes full START before fresh pipe timer/original P");
 {struct shiri_pcm_owner foreign=p.request.owner;foreign.generation++;assert(input_pipe_shiri_disarm(&foreign,fixture_operation)<0&&shiri_armed);assert(input_pipe_shiri_disarm(&p.request.owner,fixture_operation+1)<0&&shiri_armed);assert(input_pipe_shiri_disarm(NULL,fixture_operation)<0&&shiri_armed);assert(input_pipe_shiri_disarm(&p.request.owner,fixture_operation)==0&&!shiri_armed);assert(input_pipe_shiri_disarm(&p.request.owner,fixture_operation)==0&&!shiri_armed);}
 puts("Exact input-disarm rejects foreign owner/op and is idempotent for current binding");

 return 0;
#endif
}

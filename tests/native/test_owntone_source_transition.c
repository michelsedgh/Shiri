/* Actual player/source commands with controllable output acknowledgments. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <pthread.h>
#include "shiri_source.h"

#define CHECK_ERR(facility, expression) assert((expression)==0)
#define L_MAIN 0
enum command_state { COMMAND_END, COMMAND_PENDING };
enum output_device_state { OUTPUT_STATE_CONNECTED, OUTPUT_STATE_FAILED, OUTPUT_STATE_PASSWORD };
enum { PLAY_STOPPED, PLAY_PAUSED, PLAY_PLAYING };
typedef enum command_state (*command_function)(void *, int *);
struct output_device { uint64_t id; bool selected; void *session; struct output_device *next; };
typedef void (*output_status_cb)(struct output_device *, enum output_device_state);
static struct output_device devices[3], *outputs_device_list;
static int flush_behavior[3], start_behavior[3];
static int seal_calls, input_flush_calls, flush_calls, start_calls, arm_calls;
static int fail_seal, fail_arm, sealed, pending_count, command_result;
static uint64_t sealed_operation;
static struct { size_t read_deficit; } pb_session;
static int player_state;
static void *cmdbase;
union player_arg { int intval; };
static int volume_calls, current_volume;
static enum command_state volume_set(void *arg, int *retval) {
  union player_arg *value=arg;
  volume_calls++; current_volume=value->intval; *retval=0; return COMMAND_END;
}
static enum command_state volume_generic_bh(void *arg, int *retval) {
  (void)arg; (void)retval; return COMMAND_END;
}
static void device_streaming_cb(struct output_device *device, enum output_device_state status) {
  (void)device; (void)status;
}
static void outputs_device_stop_delayed(struct output_device *d, output_status_cb cb) { (void)d; (void)cb; }
static void outputs_device_cb_set(struct output_device *d, output_status_cb cb) { (void)d; (void)cb; }
static void outputs_metadata_purge(void) {}
static void outputs_stop_delayed_cancel(void) {}
static int input_pipe_shiri_seal(uint64_t operation) {
  seal_calls++; sealed=1;
  if (fail_seal) return -1;
  assert(operation>sealed_operation);
  sealed_operation=operation;
  return 0;
}
static int input_pipe_shiri_arm(const struct shiri_pcm_owner *owner, uint64_t operation) {
  (void)owner; arm_calls++;
  assert(sealed && operation==sealed_operation);
  if (fail_arm) return -1;
  sealed=0;
  return 0;
}
static void input_flush(void *flags) { (void)flags; assert(sealed); input_flush_calls++; }
struct callback { output_status_cb function; struct output_device *device; int state; };
static struct callback callbacks[8];
static int queued;
static int enqueue(struct output_device *d, output_status_cb cb, int behavior) {
  assert(sealed);
  if (behavior<0) return -1;
  if (!behavior) return 0;
  assert(queued<8);
  callbacks[queued++]=(struct callback){cb,d,behavior==2?OUTPUT_STATE_FAILED:behavior==3?OUTPUT_STATE_PASSWORD:OUTPUT_STATE_CONNECTED};
  return 1;
}
static int outputs_device_flush(struct output_device *d, output_status_cb cb) {
  flush_calls++; return enqueue(d,cb,flush_behavior[d->id]);
}
static int outputs_device_start(struct output_device *d, output_status_cb cb, bool probe) {
  (void)probe; start_calls++; return enqueue(d,cb,start_behavior[d->id]);
}
static void commands_exec_end(void *base, int result) {
  (void)base; assert(pending_count>0); pending_count--; command_result=result;
}
static int commands_exec_sync(void *base, command_function top, command_function bottom, void *arg) {
  int result=0, i;
  enum command_state state;
  (void)base;
  queued=0;
  state=top(arg,&result);
  if (state==COMMAND_PENDING) {
    assert(result>0 && result==queued);
    pending_count=result; command_result=result;
    for(i=0;i<queued;i++) callbacks[i].function(callbacks[i].device,callbacks[i].state);
    assert(!pending_count);
    result=command_result;
  }
  if(bottom && result==0) bottom(arg,&result);
  return result;
}

/* @ACTUAL_SOURCE_TRANSITION@ */

static void reset(void) {
  int i;
  memset(&shiri_source_state,0,sizeof(shiri_source_state));
  shiri_source_flush_failed=shiri_source_start_failed=0;
  seal_calls=input_flush_calls=flush_calls=start_calls=arm_calls=0;
  fail_seal=fail_arm=sealed=pending_count=0; sealed_operation=0;
  volume_calls=0; current_volume=50;
  player_state=PLAY_STOPPED; pb_session.read_deficit=900;
  for(i=0;i<3;i++) {
    devices[i]=(struct output_device){(uint64_t)i,true,(void*)1,i<2?&devices[i+1]:NULL};
    flush_behavior[i]=start_behavior[i]=1;
  }
  outputs_device_list=devices;
}
static struct shiri_source_request idle(void) {
  struct shiri_source_request r;
  memset(&r,0,sizeof(r));
  r.owner.incarnation[0]=1;
  r.owner.generation=1; r.operation_generation=1;
  return r;
}
static struct shiri_source_request begin(struct shiri_source_request previous) {
  previous.owner.session[0]=2; previous.owner.epoch++;
  previous.owner.generation=1; previous.operation_generation++;
  return previous;
}
int main(void) {
  struct shiri_source_request r, next, stale;
  int effect_count, i, failure;
  reset(); r=idle();
  assert(player_shiri_source_transition(&r)==0);
  assert(!sealed && !shiri_source_state.faulted && pb_session.read_deficit==0);
  effect_count=seal_calls+input_flush_calls+flush_calls+start_calls+arm_calls;
  assert(player_shiri_source_transition(&r)==0);
  assert(effect_count==seal_calls+input_flush_calls+flush_calls+start_calls+arm_calls);
  player_state=PLAY_PLAYING;
  next=begin(r);
  assert(player_shiri_source_transition(&next)==0);
  assert(start_calls==3 && !sealed);
  assert(player_shiri_source_volume(&next.owner,77)==0);
  assert(volume_calls==1 && current_volume==77);
  assert(player_shiri_source_volume(&r.owner,2)==SHIRI_SOURCE_CONFLICT);
  assert(volume_calls==1);
  stale=next; stale.owner.generation++;
  assert(player_shiri_source_volume(&stale.owner,5)==SHIRI_SOURCE_CONFLICT);
  assert(player_shiri_source_volume(&next.owner,-1)==SHIRI_SOURCE_INVALID);
  assert(player_shiri_source_volume(&next.owner,101)==SHIRI_SOURCE_INVALID);
  assert(volume_calls==1);
  stale=r;
  assert(player_shiri_source_transition(&stale)==SHIRI_SOURCE_CONFLICT);
  stale=next; stale.owner.session[1]=5;
  assert(player_shiri_source_transition(&stale)==SHIRI_SOURCE_CONFLICT);
  stale=next; stale.owner.incarnation[1]=7; stale.operation_generation++;
  assert(player_shiri_source_transition(&stale)==SHIRI_SOURCE_CONFLICT);
  assert(seal_calls==2);
  r=next; r.operation_generation++; r.owner.generation++;
  assert(player_shiri_source_transition(&r)==0); /* Same-owner native seek. */
  assert(r.owner.epoch==1 && !sealed);
  assert(player_shiri_source_volume(&next.owner,10)==SHIRI_SOURCE_CONFLICT);
  assert(player_shiri_source_volume(&r.owner,60)==0);
  assert(volume_calls==2 && current_volume==60);
  r.operation_generation++; memset(r.owner.session,0,16); r.owner.generation=1;
  assert(player_shiri_source_transition(&r)==0); /* Native source ended, idle speech route. */
  assert(!sealed && r.owner.epoch==1);
  assert(player_shiri_source_volume(&r.owner,0)==SHIRI_SOURCE_CONFLICT);
  assert(volume_calls==2);
  /* Every output failure, in any callback order or immediate-return position,
     retains the seal and refuses every successor until backend restart. */
  for(failure=0;failure<6;failure++) for(i=0;i<3;i++) {
    reset(); r=idle(); assert(player_shiri_source_transition(&r)==0);
    player_state=PLAY_PLAYING; next=begin(r);
    if(failure==0) flush_behavior[i]=-1;
    if(failure==1) flush_behavior[i]=2;
    if(failure==2) start_behavior[i]=-1;
    if(failure==3) start_behavior[i]=2;
    if(failure==4) start_behavior[i]=3;
    if(failure==5) fail_arm=1;
    assert(player_shiri_source_transition(&next)<0);
    assert(sealed && shiri_source_state.faulted);
    assert(player_shiri_source_volume(&next.owner,0)==SHIRI_SOURCE_CONFLICT);
    assert(!volume_calls);
    stale=begin(next);
    assert(player_shiri_source_transition(&stale)==SHIRI_SOURCE_FAULTED);
  }
  reset(); fail_seal=1; r=idle();
  assert(player_shiri_source_transition(&r)<0);
  assert(!input_flush_calls && !flush_calls && !arm_calls);
  puts("Actual player source seam: replay, ordering, seek/end and all 18 output failure permutations passed");
  return 0;
}

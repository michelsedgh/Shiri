/* Execute the actual player source-seal command and shared-converter reset.
 * No encoder implementation is mocked as evidence of audio retirement: the
 * separate full-FFmpeg fixture checks its history. This checks admission/order. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "shiri_source.h"
enum command_state { COMMAND_END, COMMAND_PENDING };
enum { PLAY_STOPPED, PLAY_PLAYING };
static struct shiri_source_state shiri_source_state;
/* @ACTUAL_PARAM@ */
static int player_state=PLAY_PLAYING,shiri_source_flush_failed;
static struct { unsigned read_deficit; } pb_session;
static bool outputs_got_new_subscription, sealed, input_cleared;
static unsigned seal_calls,flush_calls,metadata_calls;
static int fail_seal,flush_result;
static int input_pipe_shiri_seal(uint64_t operation){assert(operation);seal_calls++;sealed=true;return fail_seal?-1:0;}
static void input_flush(void *unused){(void)unused;assert(sealed);input_cleared=true;}
static void device_shiri_flush_cb(void){ }
static int outputs_shiri_flush(void(*callback)(void),int *failed){
  assert(callback==device_shiri_flush_cb&&sealed&&input_cleared);flush_calls++;
#ifndef TEST_PREIMAGE
  assert(outputs_got_new_subscription); /* Reset is marked BEFORE output ACKs. */
#endif
  *failed=flush_result<0;return flush_result>0?flush_result:0;
}
static void outputs_metadata_purge(void){metadata_calls++;}
/* @ACTUAL_RESET@ */
/* @ACTUAL_SEAL@ */
static void invoke(struct shiri_source_request request){
  struct shiri_source_param param={.request=request};int result;
  outputs_got_new_subscription=false;sealed=input_cleared=false;
  pb_session.read_deficit=3840;unsigned prior=flush_calls;
  enum command_state state=shiri_source_seal_flush(&param,&result);
  assert(sealed&&input_cleared&&flush_calls==prior+1&&pb_session.read_deficit==0);
#ifdef TEST_PREIMAGE
  assert(!outputs_got_new_subscription);
#else
  assert(outputs_got_new_subscription);
#endif
  assert(state==(flush_result>0?COMMAND_PENDING:COMMAND_END));
  assert(result==(flush_result>0?flush_result:flush_result<0?SHIRI_SOURCE_FAILURE:0));
  /* Completion is the controlled boundary, not a claim of physical Drop. */
  shiri_source_state.faulted=false;
}
int main(void){
  struct shiri_source_request r={.operation_generation=1};r.owner.incarnation[0]=1;r.owner.generation=1;
  invoke(r);r.owner.session[0]=2;r.owner.epoch=1;r.operation_generation++;invoke(r);
  r.owner.generation++;r.operation_generation++;flush_result=2;invoke(r);
  r.owner.session[0]=3;r.owner.epoch++;r.owner.generation=1;r.operation_generation++;flush_result=0;invoke(r);
  memset(r.owner.session,0,16);r.owner.generation=1;r.operation_generation++;invoke(r);
  /* Replays and malformed/stale requests must not reset the current encoder. */
  struct shiri_source_param param={.request=r};int result;unsigned effects=seal_calls+flush_calls+metadata_calls;
  outputs_got_new_subscription=false;assert(shiri_source_seal_flush(&param,&result)==COMMAND_END);
  assert(result==0&&param.replay&&!outputs_got_new_subscription&&effects==seal_calls+flush_calls+metadata_calls);
  param.request.owner.epoch++;assert(shiri_source_seal_flush(&param,&result)==COMMAND_END);
  assert(result<0&&!outputs_got_new_subscription&&effects==seal_calls+flush_calls+metadata_calls);
  param.request=r;param.request.owner.session[0]=4;param.request.owner.epoch++;param.request.operation_generation++;
  sealed=input_cleared=false;unsigned prior=flush_calls;
  fail_seal=1;assert(shiri_source_seal_flush(&param,&result)==COMMAND_END&&result<0&&!outputs_got_new_subscription);
  assert(sealed&&!input_cleared&&flush_calls==prior); /* No flush/reset after failed seal. */
#ifdef TEST_PREIMAGE
  puts("Actual preimage retains converter history across admitted same-format source transitions: reproduced");
#else
  puts("Actual source barrier resets shared converter history only after exact admission/seal; FLUSH/takeover/END/replay/failure cases passed");
#endif
  return 0;
}

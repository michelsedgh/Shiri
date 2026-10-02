#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "shiri_source.h"

static struct shiri_source_request idle(void) {
  struct shiri_source_request r;
  memset(&r,0,sizeof(r)); r.owner.incarnation[0]=1;
  r.owner.generation=1; r.operation_generation=1;
  return r;
}
static void ack(struct shiri_source_state *state, struct shiri_source_request *request) {
  assert(shiri_source_check(state,request)==SHIRI_SOURCE_NEW);
  shiri_source_begin(state,request); assert(state->faulted);
  state->faulted=false;
}
int main(void) {
  struct shiri_source_state state={0};
  struct shiri_source_request r=idle(), bad, next;
  size_t i;
  ack(&state,&r);
  assert(shiri_source_check(&state,&r)==SHIRI_SOURCE_REPLAY);
  next=r; next.operation_generation++; next.owner.epoch=1; next.owner.session[0]=2;
  ack(&state,&next);
  for(i=0;i<16;i++) {
    bad=next; bad.owner.incarnation[i]^=8;
    assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_CONFLICT);
    bad=next; bad.owner.session[i]^=8;
    assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_CONFLICT);
  }
  bad=next; bad.operation_generation++;
  assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_CONFLICT);
  bad=next; bad.operation_generation--; assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_CONFLICT);
  bad=next; bad.operation_generation+=2; assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_CONFLICT);
  r=next; r.operation_generation++; r.owner.generation++;
  ack(&state,&r);
  bad=r; bad.operation_generation++; bad.owner.generation=1;
  assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_CONFLICT);
  next=r; next.operation_generation++; memset(next.owner.session,0,16); next.owner.generation=1;
  ack(&state,&next);
  r=next; r.operation_generation++; r.owner.epoch++; r.owner.session[0]=3;
  ack(&state,&r);
  assert(r.owner.epoch==2);
  state.last.operation_generation=INT64_MAX;
  bad=r; bad.operation_generation=INT64_MAX; assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_REPLAY);
  bad.operation_generation=(uint64_t)INT64_MAX+1; assert(shiri_source_check(&state,&bad)==SHIRI_SOURCE_INVALID);
  state.last=r; state.faulted=true;
  assert(shiri_source_check(&state,&r)==SHIRI_SOURCE_FAULTED);
  puts("Actual source gate: exact ownership, operation/flush epochs, idle speech and overflow boundaries passed");
  return 0;
}

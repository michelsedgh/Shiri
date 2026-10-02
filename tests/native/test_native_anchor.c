/* Keep all five real player functions and the disabled-overlay call order.
 * Only the clock and mutex-peek return boundary are controlled here. */
#define main inherited_timer_matrix
#include "actual_native_timer.c"
#undef main

static unsigned cases;
static void prepare(int peek, struct timespec returned, struct timespec anchor) {
  reset();
  peek_status=peek;next_anchor=anchor;peek_returns_at=returned;peek_advances=true;
  assert(pb_timer_start()==0);
  assert(fixture_clock_reads==1&&timer_calls==1);
}
static void rejected(void) {
  assert(aborts==1&&!reads&&!writes&&!pb_timer_native_anchored);
  assert(!absolute_timer_successes&&!pb_session.start_ts.tv_sec&&!pb_session.pts.tv_sec);
  cases++;
}
int main(int argc,char **argv) {
  assert(inherited_timer_matrix()==0);
  if(argc==2&&!strcmp(argv[1],"preimage")) {
    prepare(1,(struct timespec){5,160000000},(struct timespec){5,150000000});
    now=(struct timespec){5,100000000};
    playback_cb(0,0,NULL);
    assert(now.tv_nsec==160000000&&!aborts&&pb_timer_native_anchored);
    assert(!reads&&!writes&&absolute_timer_successes==1);
    assert(pb_timer_native_anchor.tv_nsec==150000000);
    playback_cb(0,0,NULL);
    assert(reads==2&&writes==1&&!aborts);
    assert(last_write_pts.tv_nsec==150000000&&now.tv_nsec-last_write_pts.tv_nsec==10000000);
    puts("PREIMAGE reproduced: mutex peek crosses exact original native anchor by 10ms; old clock admits late output");
    return 0;
  }
  assert(argc==1);
  /* The 10ms reproduced stall and exact equality never arm an old deadline. */
  for(int delta=0;delta<=2;delta++) {
    prepare(1,(struct timespec){5,150000000+delta*5000000},(struct timespec){5,150000000});
    now=(struct timespec){5,100000000};playback_cb(0,0,NULL);
    assert(peek_calls==1&&fixture_clock_reads==3);rejected();
  }
  /* An original anchor still 1ns ahead is preserved, not rebuilt from now. */
  prepare(1,(struct timespec){5,149999999},(struct timespec){5,150000000});
  now=(struct timespec){5,100000000};playback_cb(0,0,NULL);
  assert(!aborts&&!reads&&!writes&&pb_timer_native_anchored&&absolute_timer_successes==1);
  assert(pb_session.pts.tv_nsec==150000000&&pb_session.start_ts.tv_nsec==150000000);
  assert(fixture_clock_reads==3&&peek_calls==1);cases++;
  now=next_anchor;playback_cb(0,0,NULL);
  assert(writes==1&&!aborts&&peek_calls==1&&fixture_clock_reads==4);cases++;
  /* Missing anchor still refreshes the timeout clock, including the boundary. */
  prepare(0,(struct timespec){9,999999999},(struct timespec){11,0});
  playback_cb(0,0,NULL);assert(!aborts&&!reads&&!writes&&!pb_timer_native_anchored);
  assert(fixture_clock_reads==3&&!absolute_timer_successes);cases++;
  for(int present=0;present<=1;present++) {
    for(int delta=0;delta<=2;delta++) {
      prepare(present,(struct timespec){10,delta},(struct timespec){11,0});
      playback_cb(0,0,NULL);assert(fixture_clock_reads==3);rejected();
    }
  }
  /* Failure before or after the mutex cannot admit or substitute any P. */
  for(int present=0;present<=1;present++) {
    for(unsigned failure=2;failure<=3;failure++) {
      prepare(present,(struct timespec){5,100000000},(struct timespec){6,0});
      clock_failure_read=failure;playback_cb(0,0,NULL);
      assert(fixture_clock_reads==failure&&peek_calls==(failure==3));rejected();
    }
  }
  prepare(-1,(struct timespec){5,100000000},(struct timespec){6,0});
  playback_cb(0,0,NULL);assert(peek_calls==1&&fixture_clock_reads==2);rejected();
  prepare(1,(struct timespec){5,100000000},(struct timespec){6,0});
  timer_failure_call=2;playback_cb(0,0,NULL);rejected();
  /* Nonzero P on an existing session cannot change on failed admission. */
  prepare(1,(struct timespec){5,160000000},(struct timespec){5,150000000});
  pb_session.start_ts=pb_session.pts=(struct timespec){4,123};
  playback_cb(0,0,NULL);assert(aborts==1&&!reads&&!writes&&!absolute_timer_successes);
  assert(pb_session.pts.tv_sec==4&&pb_session.pts.tv_nsec==123);
  assert(pb_session.start_ts.tv_sec==4&&pb_session.start_ts.tv_nsec==123);cases++;
  /* Source operation/state failures occur before clock/peek; no stale timer. */
  for(int state=-1;state<=1;state++) {
    prepare(1,(struct timespec){5,100000000},(struct timespec){6,0});
    if(state==1)operation++;else armed_state=state;
    playback_cb(0,0,NULL);assert(peek_calls==0&&fixture_clock_reads==1);rejected();
  }
  /* Successful anchored checks never peek or need the new second clock. */
  prepare(1,(struct timespec){5,100000000},(struct timespec){6,0});
  playback_cb(0,0,NULL);assert(pb_timer_native_anchored&&peek_calls==1);
  clock_failure_read=5;now=(struct timespec){5,999999999};
  playback_cb(0,0,NULL);assert(!aborts&&!reads&&!writes&&fixture_clock_reads==4&&peek_calls==1);cases++;
  now=(struct timespec){6,0};clock_failure_read=0;
  playback_cb(0,0,NULL);assert(writes==1&&!aborts&&fixture_clock_reads==5&&peek_calls==1);cases++;
  /* Ordinary source behavior has no native peek/extra clock admission. */
  reset();armed_state=0;assert(pb_timer_start()==0&&!pb_timer_native);
  assert(fixture_clock_reads==0&&peek_calls==0&&flags_seen==0);cases++;
  printf("%u actual native anchor cases: fresh post-peek clock failure/equal/past/wait-boundary; original P/source/timers and ordinary branch preserved\n",cases);
  return 0;
}

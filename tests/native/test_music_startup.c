/* Controlled first output-START completion; actual composed timer callback.
 * This qualifies deadline semantics, not measured device startup duration. */
#define main inherited_timer_matrix
#include "test_native_timer.c"
#undef main
int main(void) {
  assert(inherited_timer_matrix() == 0);
  /* The shared first native P exists before admission; callback availability
   * t0 = P - 150 ms. Never rewrite it after GRANT/output START completion. */
  unsigned boundaries=0;
  for(unsigned horizon=750;horizon<=1000;horizon+=250){
    const unsigned buffer=500,lead=150;
    uint64_t budget=(uint64_t)(lead+horizon-buffer)*UINT64_C(1000000);
    uint64_t completions[]={0,UINT64_C(30000000),UINT64_C(150000000),
                            budget-1,budget,budget+1,UINT64_C(1000000000)};
    for(unsigned k=0;k<sizeof(completions)/sizeof(completions[0]);k++){
      uint64_t completion=completions[k];
      reset();
      next_anchor=(struct timespec){5+(time_t)(budget/UINT64_C(1000000000)),
                                 (long)(budget%UINT64_C(1000000000))};
      struct timespec immutable=next_anchor;
      now=(struct timespec){5+(time_t)(completion/UINT64_C(1000000000)),
                           (long)(completion%UINT64_C(1000000000))};
      peek_status=1;
      assert(pb_timer_start()==0);
      playback_cb(0,0,NULL);
      assert(!reads&&!writes);
      if(completion>=budget){
        assert(aborts==1&&!pb_timer_native&&!pb_timer_native_anchored);
        assert(timespec_cmp(next_anchor,immutable)==0); /* no receive-clock rebase */
      }else{
        assert(!aborts&&pb_timer_native_anchored);
        assert(timespec_cmp(timer_seen.it_value,immutable)==0);
        now=immutable;playback_cb(0,0,NULL);
        assert(reads==2&&writes==1&&!aborts);
        assert(timespec_cmp(last_write_pts,immutable)==0);
      }
      boundaries++;
    }
  }
  assert(boundaries==14);
  puts("pre-BEGIN fixed P: H750/B500 400ms and H1000/B500 650ms first-anchor budgets; exact late/equal rejection, no rebase, disabled overlay intact");
  return 0;
}

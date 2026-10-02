/* Compile the actual player tick against a timestamp-bounded 8 ms input. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#define DPRINTF(...) ((void)0)
#ifdef SHIRI_PARTIAL_SPEECH
/* Observe disabled overlay calls in the actual composed callback. Enabled
 * overlay behavior belongs to the separate real shiri_speech module tests. */
enum { SPEECH_NONE, SPEECH_POLLED, SPEECH_TIMER, SPEECH_INPUT, SPEECH_MIXED, SPEECH_OUTPUT };
static int speech_phase;
static unsigned speech_polls,speech_mixes,output_calls;
#ifdef SHIRI_PARTIAL_READY
static unsigned speech_ready;
static void shiri_speech_mix_ready(void){
  assert(speech_phase==SPEECH_OUTPUT&&speech_ready==output_calls-1);
  speech_ready++;
}
#endif
static void speech_timer_read(void){assert(speech_phase==SPEECH_POLLED);speech_phase=SPEECH_TIMER;}
static void shiri_speech_poll(void){
  assert(speech_phase!=SPEECH_POLLED&&speech_phase!=SPEECH_MIXED);
  speech_polls++;speech_phase=SPEECH_POLLED;
}
#else
static void speech_timer_read(void){}
#endif
static int pb_timer;
#define timer_getoverrun fixture_getoverrun
static int timer_getoverrun(int timer) { (void)timer; speech_timer_read(); return 0; }
static unsigned pb_write_deficit_max=50;
static bool pb_write_recovery;
/* Absolute native timer gating is exercised by test_native_timer.c. This
 * fixture isolates catch-up after reads are admitted by that same callback. */
static bool pb_timer_native;
static int pb_timer_native_prepare(void) { return 1; }
static int player_flush_pending;
static int suspend_calls, abort_calls, read_calls;
static unsigned char buffer[1920];
static size_t available, produced, written;
static struct {
  size_t read_deficit, read_deficit_max, bufsize;
  void *buffer;
  struct { unsigned sample_rate,bits_per_sample,channels; } quality;
  struct timespec pts;
} pb_session;
static struct timespec player_tick_interval={0,10000000};
static struct timespec timespec_add(struct timespec a, struct timespec b) {
  a.tv_sec+=b.tv_sec; a.tv_nsec+=b.tv_nsec;
  if(a.tv_nsec>=1000000000L) { a.tv_sec++; a.tv_nsec-=1000000000L; }
  return a;
}
static void pb_abort(void) { abort_calls++; }
static int pb_suspend(void) { suspend_calls++; return 0; }
static void player_playback_start(void) {}
static void input_buffer_full_cb(void (*callback)(void)) { (void)callback; }
static int source_read(int *nbytes, int *nsamples, void *output, size_t maximum) {
  size_t size=available<1536?available:1536; /* 384 actual frames = 8 ms. */
#ifdef SHIRI_PARTIAL_SPEECH
  assert(speech_phase==SPEECH_TIMER||speech_phase==SPEECH_INPUT||speech_phase==SPEECH_OUTPUT);
  speech_phase=SPEECH_INPUT;
#endif
  assert(output==buffer);
  read_calls++;
  assert(size<=maximum && size%4==0);
  memset(output,7,size);
  *nbytes=(int)size; *nsamples=(int)(size/4);
  available-=size;
  return 0;
}
#ifdef SHIRI_PARTIAL_SPEECH
static void shiri_speech_mix(void *pcm,size_t bytes,int frames,unsigned rate,unsigned bits,unsigned channels){
  assert(speech_phase==SPEECH_INPUT);
  assert(pcm==buffer&&bytes>0&&bytes<=sizeof(buffer)&&bytes==(size_t)frames*4);
  assert(rate==48000&&bits==16&&channels==2);
  for(size_t i=0;i<bytes;i++)assert(((unsigned char*)pcm)[i]==7);
  speech_mixes++;speech_phase=SPEECH_MIXED;
  /* Disabled overlay leaves the admitted partial input entirely unchanged. */
}
#endif
static void outputs_write(void *pcm, int nbytes, int nsamples, void *quality, struct timespec *pts) {
  (void)pts;
  assert(pcm==buffer&&quality==&pb_session.quality&&nbytes==nsamples*4);
#ifdef SHIRI_PARTIAL_SPEECH
  assert(speech_phase==SPEECH_MIXED&&speech_mixes==output_calls+1);
  output_calls++;speech_phase=SPEECH_OUTPUT;
#endif
  for(int i=0;i<nbytes;i++)assert(((unsigned char*)pcm)[i]==7);
  written+=(size_t)nbytes;
}

/* @ACTUAL_PLAYBACK_TICK@ */

int main(void) {
  unsigned tick;
  pb_session.bufsize=sizeof(buffer); pb_session.buffer=buffer;
  pb_session.quality.sample_rate=48000;
  pb_session.quality.bits_per_sample=16;pb_session.quality.channels=2;
  pb_session.read_deficit_max=192000;
  for(tick=0;tick<10000;tick++) {
    available+=1920; produced+=1920; /* Exactly one 10 ms player tick. */
    playback_cb(0,0,NULL);
    if(suspend_calls || abort_calls || pb_session.read_deficit>3840 || available>3840) {
      fputs("Timestamp-bounded partial input permanently falls behind the player clock\n",stderr);
      return 1;
    }
  }
  assert(produced==written+available && pb_session.read_deficit==available);
  assert(written>=produced-3840 && read_calls<20000);
#ifdef SHIRI_PARTIAL_SPEECH
  assert(speech_polls==10000&&speech_mixes==output_calls);
  unsigned previous_mixes=speech_mixes;
#endif
  available=0;
  read_calls=0;
  playback_cb(0,0,NULL);
  assert(read_calls==1); /* An empty input breaks promptly, without spinning. */
#ifdef SHIRI_PARTIAL_SPEECH
  assert(speech_polls==10001&&speech_mixes==previous_mixes);
#ifdef SHIRI_PARTIAL_READY
  assert(speech_ready==output_calls);
#endif
  puts("Disabled late speech: one poll before each timer/input, private partial PCM mixed once before each output, empty reads unmixed; bytes unchanged");
#endif
  puts("Actual player partial-read regression: 100 seconds of 8 ms blocks stay bounded on 10 ms ticks; empty input does not spin");
  return 0;
}

/* Actual current native mixer replay of Python worker delivery timestamps.
 * No network, kernel scheduling, physical output or room startup is simulated. */
#define _GNU_SOURCE
#include SHIRI_SPEECH_SOURCE
#include <assert.h>
#include <inttypes.h>

static void put32(uint8_t *p, uint32_t v) { p[0]=v>>24;p[1]=v>>16;p[2]=v>>8;p[3]=v; }
static void put64(uint8_t *p, uint64_t v) { put32(p,v>>32);put32(p+4,v); }
static int read16(const uint8_t *p) { unsigned v=p[0]|((unsigned)p[1]<<8);return v&32768u?(int)v-65536:(int)v; }
static void raw16(uint8_t *p,int v) { unsigned n=v<0?v+65536:v;p[0]=n;p[1]=n>>8; }
static int sample(unsigned frame) { return 100+(int)(frame%10000); }

int main(void) {
  uint64_t arrivals[128], origin=UINT64_C(1000000000), onset=0;
  unsigned frames[128], count=0, total=0, next=0, sent=0, consumed=0, gaps=0, wrong=0, maximum=0;
  while(count<128 && scanf("%" SCNu64 " %u",&arrivals[count],&frames[count])==2) {
    assert(frames[count]>0 && frames[count]<=960);
    assert(!count || arrivals[count]>=arrivals[count-1]);
    total+=frames[count++];
  }
  assert(count && count<128 && arrivals[0]==origin);
  struct speech_state state; memset(&state,0,sizeof(state));state.gain=1;
  uint8_t owner[16]={1}, packet[SPEECH_HEADER+1920], pcm[1920];
  memset(state.room,2,sizeof(state.room));memset(state.launch,3,sizeof(state.launch));
  assert(speech_begin(&state,owner,origin)==0);
  for(unsigned tick=0;tick<400;++tick) {
    uint64_t now=origin+(uint64_t)tick*10000000;
    while(next<count && arrivals[next]<=now) {
      memset(packet,0,sizeof(packet));memcpy(packet,"SHRITTS1",8);
      packet[8]=2;packet[9]=1;packet[11]=SPEECH_HEADER;
      put32(packet+12,frames[next]*2);put64(packet+16,next+1);
      memcpy(packet+24,state.room,16);memcpy(packet+40,state.launch,16);memcpy(packet+80,owner,16);
      put64(packet+56,arrivals[next]);put32(packet+64,frames[next]);
      put32(packet+68,SPEECH_FULL_GAIN);put32(packet+72,1);
      for(unsigned i=0;i<frames[next];++i)raw16(packet+SPEECH_HEADER+2*i,sample(sent+i));
      assert(speech_receive(&state,packet,SPEECH_HEADER+frames[next]*2,now)==0);
      sent+=frames[next++];
      if(state.count>maximum)maximum=state.count;
      if(next==count)assert(speech_finish(&state,owner)==0);
    }
    memset(pcm,0,sizeof(pcm));speech_mix(&state,pcm,sizeof(pcm),480,SPEECH_RATE,16,2,now);
    for(unsigned i=0;i<480;++i) {
      int left=read16(pcm+4*i),right=read16(pcm+4*i+2);
      if(left) {
        if(!onset)onset=now-origin;
        if(left!=sample(consumed)||left!=right)++wrong;
        ++consumed;
      } else if(tick>=2 && consumed<total)++gaps;
    }
  }
  assert(consumed==total && !wrong && !state.count && !state.expired && !state.refused);
  printf("{\"onset_ns\":%" PRIu64 ",\"gap_frames\":%u,\"underflow_frames\":%" PRIu64
         ",\"maximum_queue_frames\":%u,\"ordered_frames\":%u}\n",onset,gaps,state.underflow_frames,maximum,consumed);
  return 0;
}

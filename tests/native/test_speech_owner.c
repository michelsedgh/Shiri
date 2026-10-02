/* Exact v2 media module: ownership is independent of music and output state. */
#define _GNU_SOURCE
#include SHIRI_SPEECH_SOURCE
#include <assert.h>
static unsigned checks;
#define CHECK(v) do { assert(v); ++checks; } while (0)
static const uint64_t now0 = UINT64_C(10000000000);
static const uint8_t owner_a[16] = {1}, owner_b[16] = {2};
static void put32(uint8_t *p, uint32_t v) { p[0]=v>>24;p[1]=v>>16;p[2]=v>>8;p[3]=v; }
static void put64(uint8_t *p, uint64_t v) { put32(p,v>>32);put32(p+4,v); }
static int read16(const uint8_t *p) { unsigned v=p[0]|((unsigned)p[1]<<8);return v&32768u?(int)v-65536:(int)v; }
static void raw16(uint8_t *p,int v) { unsigned n=v<0?v+65536:v;p[0]=n;p[1]=n>>8; }
static struct speech_state fresh(void) {
  struct speech_state s;memset(&s,0,sizeof(s));s.gain=1;
  CHECK(speech_hex("b6786543-7eb2-443d-83b1-65b984123a76",s.room,1)==0);
  CHECK(speech_hex("123456789abcdef0123456789abcdef0",s.launch,0)==0);
  return s;
}
static size_t packet(uint8_t *p,const struct speech_state *s,const uint8_t id[16],
                     uint64_t seq,unsigned frames,int sample,unsigned active,uint64_t now) {
  memset(p,0,SPEECH_HEADER+2*SPEECH_FRAMES);memcpy(p,"SHRITTS1",8);
  p[8]=2;p[9]=frames?1:2;p[11]=SPEECH_HEADER;put32(p+12,frames*2);put64(p+16,seq);
  memcpy(p+24,s->room,16);memcpy(p+40,s->launch,16);memcpy(p+80,id,16);
  put64(p+56,now);put32(p+64,frames);put32(p+68,13107);put32(p+72,active);
  for(unsigned i=0;i<frames;++i)raw16(p+SPEECH_HEADER+2*i,sample);
  return SPEECH_HEADER+2*frames;
}
static void music(uint8_t *p,unsigned frames,int value) { for(unsigned i=0;i<frames*2;++i)raw16(p+2*i,value); }
static void refused(struct speech_state *s,const uint8_t *p,size_t n,uint64_t now) {
  struct speech_state before=*s;CHECK(speech_receive(s,p,n,now)<0);
  before.refused=s->refused;CHECK(!memcmp(&before,s,sizeof(before)));
}
static void stale_owner_and_tail(void) {
  struct speech_state s=fresh(),before;uint8_t p[SPEECH_HEADER+2*SPEECH_FRAMES],pcm[1920];size_t n;
  n=packet(p,&s,owner_a,1,960,100,1,now0);refused(&s,p,n,now0);
  CHECK(speech_begin(&s,owner_a,now0)==0);CHECK(speech_begin(&s,owner_a,now0)==0);
  CHECK(speech_receive(&s,p,n,now0)==0 && s.count==960 && s.priming_until==now0+20000000);
  CHECK(speech_finish(&s,owner_a)==0 && s.count==960 && !s.active && !s.accepting);
  CHECK(speech_begin(&s,owner_a,now0)<0);CHECK(speech_begin(&s,owner_b,now0)==1 && s.count==960);
  n=packet(p,&s,owner_a,2,960,100,1,now0);refused(&s,p,n,now0);
  music(pcm,480,0);speech_mix(&s,pcm,sizeof(pcm),480,48000,16,2,now0+20000000);
  for(unsigned i=0;i<960;++i)CHECK(read16(pcm+2*i)==100);
  CHECK(speech_begin(&s,owner_b,now0+20000000)==1 && s.count==480);
  music(pcm,480,0);speech_mix(&s,pcm,sizeof(pcm),480,48000,16,2,now0+30000000);
  for(unsigned i=0;i<960;++i)CHECK(read16(pcm+2*i)==100);
  CHECK(!s.count && speech_begin(&s,owner_b,now0+30000000)==0);
  n=packet(p,&s,owner_b,2,960,200,1,now0+30000000);
  CHECK(speech_receive(&s,p,n,now0+30000000)==0 && s.priming_until==now0+50000000);
  n=packet(p,&s,owner_a,999,960,100,1,now0+30000000);refused(&s,p,n,now0+30000000);
  n=packet(p,&s,owner_a,999,0,0,0,now0+30000000);refused(&s,p,n,now0+30000000);
  before=s;CHECK(speech_finish(&s,owner_a)<0 && speech_cancel(&s,owner_a)<0);
  CHECK(!memcmp(&before,&s,sizeof(s)) && s.sequence==2 && s.count==960);
  music(pcm,480,0);speech_mix(&s,pcm,sizeof(pcm),480,48000,16,2,now0+50000000);
  for(unsigned i=0;i<960;++i)CHECK(read16(pcm+2*i)==200);
}
static void exact_cancel_restores(void) {
  struct speech_state s=fresh();uint8_t p[SPEECH_HEADER+2*SPEECH_FRAMES],pcm[1920];size_t n;
  CHECK(speech_begin(&s,owner_a,now0)==0);
  n=packet(p,&s,owner_a,1,960,100,1,now0);CHECK(speech_receive(&s,p,n,now0)==0);
  music(pcm,480,10000);speech_mix(&s,pcm,sizeof(pcm),480,48000,16,2,now0+20000000);
  CHECK(fabs(s.gain-.75)<1e-10 && s.count==480);
  CHECK(speech_cancel(&s,owner_a)==0 && !s.count && !s.active && !s.running && !s.lease && !s.priming_until);
  CHECK(speech_cancel(&s,owner_a)==0 && speech_begin(&s,owner_a,now0+21000000)<0);
  int last=7500;
  for(unsigned tick=0;tick<25;++tick) {
    music(pcm,480,10000);speech_mix(&s,pcm,sizeof(pcm),480,48000,16,2,now0+30000000+tick*10000000);
    for(unsigned i=0;i<960;++i){int v=read16(pcm+2*i);CHECK(v>=last && v<=10000);last=v;}
  }
  CHECK(s.gain==1 && last==10000 && s.mixed_frames==480);
  CHECK(speech_begin(&s,owner_b,now0+300000000)==0);
  n=packet(p,&s,owner_a,999,960,100,1,now0+300000000);refused(&s,p,n,now0+300000000);
  s=fresh();CHECK(speech_cancel(&s,owner_a)==0 && speech_begin(&s,owner_a,now0)<0);
}
static void original_guards(void) {
  struct speech_state s=fresh();uint8_t p[SPEECH_HEADER+2*SPEECH_FRAMES];size_t n;
  CHECK(SPEECH_AGE_NS==250000000 && SPEECH_RESERVE_NS==20000000 && SPEECH_QUEUE==12000);
  CHECK(speech_begin(&s,owner_a,now0)==0);
  n=packet(p,&s,owner_a,1,960,32767,1,now0);p[8]=1;refused(&s,p,n,now0);p[8]=2;
  p[11]=80;refused(&s,p,n,now0);p[11]=SPEECH_HEADER;
  put64(p+56,now0-SPEECH_AGE_NS);refused(&s,p,n,now0);
  put64(p+56,now0+SPEECH_FUTURE_NS+1);refused(&s,p,n,now0);
  put64(p+56,now0);put32(p+76,1);refused(&s,p,n,now0);put32(p+76,0);
  p[80]=0;refused(&s,p,n,now0);p[80]=1;
  CHECK(speech_receive(&s,p,n,now0)==0);refused(&s,p,n,now0);
  for(unsigned i=2;i<=12;++i){put64(p+16,i);CHECK(speech_receive(&s,p,n,now0)==0);}
  n=packet(p,&s,owner_a,13,480,1,1,now0);CHECK(speech_receive(&s,p,n,now0)==0 && s.count==12000);
  n=packet(p,&s,owner_a,14,1,1,1,now0);refused(&s,p,n,now0);
  speech_expire(&s,now0+SPEECH_AGE_NS);CHECK(!s.count && s.expired==12000 && !s.active);
}

static void lost_cancel_lease_restores(void) {
  struct speech_state s=fresh();uint8_t p[SPEECH_HEADER+2*SPEECH_FRAMES],pcm[1920];
  CHECK(speech_begin(&s,owner_a,now0)==0);
  size_t n=packet(p,&s,owner_a,1,960,100,1,now0);
  CHECK(speech_receive(&s,p,n,now0)==0);
  /* If authenticated cancellation cannot be delivered, the retired local
   * producer sends no refresh. Original queue/lease expiry restores music. */
  for(unsigned tick=0;tick<50;++tick) {
    music(pcm,480,10000);speech_mix(&s,pcm,sizeof(pcm),480,48000,16,2,now0+tick*10000000);
  }
  CHECK(!s.count && !s.active && s.gain==1 && s.sequence==1 && s.admitted==1);
  CHECK(s.mixed_frames==960 && s.admitted_frames==960);
  for(unsigned i=0;i<960;++i)CHECK(read16(pcm+2*i)==10000);
}

int main(void) {
  stale_owner_and_tail();exact_cancel_restores();original_guards();lost_cancel_lease_restores();
  printf("owner1: %u actual-media checks; stale-ID fence, exact cancel, natural tail, original TTL/reserve\n",checks);
  return 0;
}

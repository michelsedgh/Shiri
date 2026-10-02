/* Actual backend callbacks with controlled native socket grants, no receiver or device. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <stdint.h>
#include "common.h"
#include "audio.h"
#include "shiri_pcm.h"
#ifndef SOCK_CLOEXEC
#define SOCK_CLOEXEC 0
#endif
#ifndef SOCK_NONBLOCK
#define SOCK_NONBLOCK 0
#endif
#ifndef SO_PEERCRED
#define SO_PEERCRED 17
struct ucred { pid_t pid; uid_t uid; gid_t gid; };
#endif
static uint8_t captured[SHIRI_PCM_HEADER+SHIRI_PCM_MAX_PAYLOAD];
static struct shiri_pcm_packet last;
static int sends,deny_send,peer=61000,closed,peer_gone;
static size_t captured_bytes;
static uint64_t raw_fallback=UINT64_C(10000000000);
static unsigned raw_step,raw_step_index;
static int preempt_after_check;
static uint64_t final_raw_monotonic_jump;
static uint64_t send_observed_now;
static uint64_t clock_values[128], raw_values[64];
static size_t clock_count, clock_index, raw_count, raw_index;
static uint64_t default_clock = UINT64_C(9000000000);
static int mock_clock_gettime(clockid_t clock,struct timespec *value){
  uint64_t ns;
  if(clock==CLOCK_MONOTONIC_RAW)ns=get_absolute_time_in_ns();
  else{
    assert(clock==CLOCK_MONOTONIC);
    if(clock_count){assert(clock_index<clock_count);ns=clock_values[clock_index++];}
    else {ns=default_clock;default_clock+=100;}
  }
  if(ns==UINT64_MAX){errno=EIO;return -1;}
  value->tv_sec=(time_t)(ns/UINT64_C(1000000000));
  value->tv_nsec=(long)(ns%UINT64_C(1000000000));return 0;
}
static void clocks(const uint64_t *monotonic,size_t count,const uint64_t *raw,size_t raws){
  assert(count<=128&&raws<=64);
  if(count){
    memcpy(clock_values,monotonic,count*sizeof(*monotonic));
  }
  if(raws){
    memcpy(raw_values,raw,raws*sizeof(*raw));
  }
  clock_count=count;
  raw_count=raws;
  clock_index=raw_index=0;
}
static int mock_socket(int d,int t,int p){assert(d==AF_UNIX);(void)t;(void)p;return 99;}
static int mock_connect(int s,const struct sockaddr*a,socklen_t n){assert(s==99);(void)a;(void)n;return 0;}
static int mock_poll(struct pollfd*f,nfds_t n,int ms){assert(n==1&&ms<=6000);f->revents=f->events;return 1;}
static int mock_getsockopt(int s,int l,int o,void*v,socklen_t*n){assert(s==99&&l==SOL_SOCKET&&o==SO_PEERCRED);struct ucred*u=v;u->uid=(uid_t)peer;*n=sizeof(*u);return 0;}
static int mock_setsockopt(int s,int l,int o,const void*v,socklen_t n){(void)s;(void)l;(void)o;(void)v;(void)n;return 0;}
static ssize_t mock_send(int s,const void*p,size_t n,int f){
  (void)s;(void)f;if(deny_send){deny_send=0;errno=EAGAIN;return -1;}
  assert(n<=sizeof(captured));captured_bytes=n;memcpy(captured,p,n);assert(shiri_pcm_decode(&last,captured,n)==0);if(last.kind==SHIRI_PCM){send_observed_now=preempt_after_check?last.presentation+1:raw_fallback;}sends++;return (ssize_t)n;
}
static ssize_t mock_recv(int s,void*p,size_t n,int f){
  (void)s;(void)f;if(n==1){if(peer_gone)return 0;errno=EAGAIN;return -1;}assert(n==SHIRI_PCM_HEADER);struct shiri_pcm_packet reply=last;
  reply.kind=SHIRI_GRANT;reply.epoch=1;memset(reply.incarnation,7,16);shiri_pcm_encode(p,&reply);return (ssize_t)n;
}
static int mock_open(const char *path,int flags,...){
  assert(!strcmp(path,"/dev/urandom") && flags==(O_RDONLY|O_CLOEXEC|O_NOFOLLOW));return 100;
}
static ssize_t mock_read(int n,void *bytes,size_t count){
  assert(n==100);for(size_t i=0;i<count;i++){((uint8_t *)bytes)[i]=(uint8_t)(i+1u);}return (ssize_t)count;
}
static int mock_close(int n){if(n==100)return 0;if(n==99){closed++;return 0;}return close(n);}
#define open mock_open
#define read mock_read
#define socket mock_socket
#define connect mock_connect
#define poll mock_poll
#define getsockopt mock_getsockopt
#define setsockopt mock_setsockopt
#define send mock_send
#define recv mock_recv
#define close mock_close
#define clock_gettime mock_clock_gettime
#include "audio_shiri.inc"
#undef open
#undef read
#undef socket
#undef connect
#undef poll
#undef getsockopt
#undef setsockopt
#undef send
#undef recv
#undef close
#undef clock_gettime
struct test_config config={.cfg=(void*)1};
int config_lookup_non_empty_string(void*c,const char*k,const char**v){(void)c;(void)k;*v="/private/music.sock";return 1;}
int config_lookup_int(void*c,const char*k,int*v){(void)c;(void)k;*v=61000;return 1;}
uint64_t get_absolute_time_in_ns(void){
  if(raw_count && raw_index<raw_count){
    raw_fallback=raw_values[raw_index++];
    if(raw_index==2 && final_raw_monotonic_jump){default_clock+=final_raw_monotonic_jump;}
    return raw_fallback;
  }
  if(raw_step){raw_fallback=UINT64_C(10000000000)+(uint64_t)raw_step_index++*UINT64_C(1000000000)/SHIRI_PCM_RATE;}
  return raw_fallback;
}
void warn(const char*p,...){(void)p;}
void die(const char*p,...){(void)p;exit(71);}
void parse_audio_options(const char*n,uint32_t f,uint32_t r,uint32_t c){assert(!strcmp(n,"shiri")&&f==(1u<<SPS_FORMAT_S16_LE)&&r==(1u<<SPS_RATE_48000)&&c==(1u<<2));}
int32_t search_for_suitable_configuration(unsigned int c,unsigned int r,unsigned int f,int(*check)(unsigned int,unsigned int,unsigned int)){(void)check;return (int32_t)((c<<25)|((r/2)<<6)|f);}

#define CHECK(condition) do { if (!(condition)) { fprintf(stderr,"FAIL line%d: %s\n",__LINE__,#condition);exit(72); } } while (0)
static unsigned checks;
static int number=700;
static const uint64_t base_raw=UINT64_C(10000000000);
static uint8_t pcm[SHIRI_PCM_MAX_PAYLOAD];

/* Independent sample-calendar oracle: enumerate the original samples, rather
 * than copy the production inverse/ceil arithmetic. */
static uint32_t expired_reference(uint64_t p,uint64_t now,uint32_t frames){
  uint32_t i=0;
  while(i<frames && p+(uint64_t)i*UINT64_C(1000000000)/48000u<=now)i++;
  return i;
}
static void prepare(void){
  clocks(NULL,0,NULL,0);raw_fallback=base_raw;raw_step=raw_step_index=0;
  preempt_after_check=0;send_observed_now=0;final_raw_monotonic_jump=0;default_clock=UINT64_C(9000000000);
  number++;
  audio_shiri.session_begin(number,"01234567-89ab-cdef-0123-456789abcdef",1,1,-15);
  CHECK(fd==99);state.sequence=0;state.frame_index=0;gap=0;
  for(unsigned i=0;i<sizeof(pcm);i++)pcm[i]=(uint8_t)(i*37u+11u);
}
static void raw_script(const uint64_t *raw,size_t count){
  clocks(NULL,0,raw,count);default_clock=UINT64_C(9000000000);
}
static void preserved_fence(struct shiri_pcm_packet before){
  CHECK(fd==99 && state.epoch==before.epoch && state.generation==before.generation);
  CHECK(!memcmp(state.session,before.session,16));
  CHECK(!memcmp(state.incarnation,before.incarnation,16));
  CHECK(!memcmp(state.group,before.group,16));
}
static void future_wire(uint64_t lead,const char *output){
  prepare();const uint64_t raw[]={base_raw,base_raw};raw_script(raw,2);
  struct shiri_pcm_packet before=state;int sent=sends;
  CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,UINT32_MAX-4,base_raw+lead));
  CHECK(sends==sent+1 && last.frames==16 && last.frame_index==0 && last.sequence==1);
  CHECK(last.presentation==base_raw+lead && last.rtp==UINT32_MAX-4 && !(last.flags&SHIRI_PCM_GAP));
  CHECK(last.raw_sample==base_raw && state.frame_index==16 && state.sequence==1 && !gap);
  CHECK(!memcmp(captured+SHIRI_PCM_HEADER,pcm,64));preserved_fence(before);
  if(output){FILE *f=fopen(output,"wb");CHECK(f);CHECK(fwrite(captured,1,captured_bytes,f)==captured_bytes);CHECK(!fclose(f));}
  checks++;
}
static void partial(uint64_t delta,uint32_t frames){
  prepare();uint8_t original[SHIRI_PCM_MAX_PAYLOAD];memcpy(original,pcm,sizeof(pcm));
  const uint64_t raw[]={base_raw,base_raw+delta,base_raw+delta};raw_script(raw,3);
  uint32_t trim=expired_reference(base_raw,base_raw+delta,frames);CHECK(trim<frames);
  struct shiri_pcm_packet before=state;int sent=sends;
  CHECK(!audio_shiri.play_native(number,pcm,(int)frames,play_samples_are_timed,123,base_raw));
  CHECK(sends==sent+1 && last.presentation>base_raw+delta);
  CHECK(last.frames==frames-trim && last.frame_index==trim && last.sequence==1 && last.rtp==123);
  CHECK(last.presentation==base_raw+(uint64_t)trim*UINT64_C(1000000000)/48000u);
  CHECK(last.flags&SHIRI_PCM_GAP);CHECK(last.payload==(frames-trim)*4u);
  CHECK(!memcmp(captured+SHIRI_PCM_HEADER,pcm+trim*4u,last.payload));
  CHECK(!memcmp(original,pcm,sizeof(pcm)));
  CHECK(state.frame_index==frames && state.sequence==1 && !gap);
  preserved_fence(before);checks++;
}
static void original_calendar_after_repeated_trim(void){
  prepare();const uint64_t raw[]={base_raw,base_raw+20833,base_raw+41666,base_raw+41666};raw_script(raw,4);
  CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,7,base_raw));
  CHECK(last.frame_index==3 && last.frames==13 && last.presentation==base_raw+62500);
  CHECK(last.presentation!=base_raw+62499 && state.sequence==1 && state.frame_index==16);
  CHECK(last.raw_sample==base_raw && raw_index==4 && last.flags&SHIRI_PCM_GAP);
  CHECK(!memcmp(captured+SHIRI_PCM_HEADER,pcm+12,52));checks++;
}
static void full_expiry_then_fresh(uint64_t delta,uint32_t frames){
  prepare();const uint64_t raw[]={base_raw,base_raw+delta};raw_script(raw,2);
  struct shiri_pcm_packet before=state;int sent=sends;
  CHECK(expired_reference(base_raw,base_raw+delta,frames)==frames);
  CHECK(!audio_shiri.play_native(number,pcm,(int)frames,play_samples_are_timed,5,base_raw));
  CHECK(sends==sent && state.sequence==1 && state.frame_index==frames && gap);
  preserved_fence(before);
  const uint64_t now[]={base_raw+delta,base_raw+delta};raw_script(now,2);
  CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,9,base_raw+delta+100000));
  CHECK(sends==sent+1 && last.sequence==2 && last.frame_index==frames && last.frames==16);
  CHECK(last.presentation==base_raw+delta+100000 && last.flags&SHIRI_PCM_GAP && !gap);
  CHECK(!memcmp(captured+SHIRI_PCM_HEADER,pcm,64));preserved_fence(before);checks++;
}
static void send_pressure_after_partial(void){
  prepare();const uint64_t raw[]={base_raw,base_raw+63734,base_raw+63734};raw_script(raw,3);
  int sent=sends;deny_send=1;
  CHECK(!audio_shiri.play_native(number,pcm,366,play_samples_are_timed,9,base_raw));
  CHECK(sends==sent && state.frame_index==366 && state.sequence==1 && gap && fd==99);
  const uint64_t fresh[]={base_raw+63734,base_raw+63734};raw_script(fresh,2);
  CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,10,base_raw+1000000));
  CHECK(last.frame_index==366 && last.sequence==2 && last.flags&SHIRI_PCM_GAP && !gap);checks++;
}
static void failed_final_clock(uint64_t final,int after_trim){
  prepare();const uint64_t raw[]={base_raw,after_trim?base_raw+20833:final,final};raw_script(raw,after_trim?3:2);
  struct shiri_pcm_packet before=state;int sent=sends;
  CHECK(audio_shiri.play_native(number,pcm,16,play_samples_are_timed,1,base_raw+(!after_trim?1000000:0))==EIO);
  CHECK(sends==sent && fd<0 && !gap && !clock_order.active);
  CHECK(!memcmp(&before,&state,sizeof(state)));CHECK(audio_shiri.session_retired(number));checks++;
}
static void final_raw_inside_original_deadline(void){
  prepare();const uint64_t raw[]={base_raw,base_raw};raw_script(raw,2);
  final_raw_monotonic_jump=UINT64_C(21000000);
  struct shiri_pcm_packet before=state;int sent=sends;
  CHECK(audio_shiri.play_native(number,pcm,16,play_samples_are_timed,1,base_raw+150000000)==ETIMEDOUT);
  CHECK(raw_index==2 && sends==sent && fd<0 && !clock_order.active);
  CHECK(!memcmp(&before,&state,sizeof(state)));checks++;
}
static void trimming_does_not_reset_deadline(void){
  prepare();const uint64_t raw[]={base_raw,base_raw+20833,base_raw+20833};
  const uint64_t mono[]={9000000000,9000000100,9000000200,9000000300,9000000400,9000000500,9020000000};
  clocks(mono,7,raw,3);struct shiri_pcm_packet before=state;int sent=sends;
  CHECK(audio_shiri.play_native(number,pcm,16,play_samples_are_timed,1,base_raw)==ETIMEDOUT);
  CHECK(raw_index==3 && clock_index==7 && sends==sent && fd<0 && !clock_order.active);
  CHECK(!memcmp(&before,&state,sizeof(state)));checks++;
}
static void finite_progress(void){
  prepare();raw_step=1;raw_step_index=0;
  const uint64_t first[]={base_raw-1};raw_script(first,1);
  int sent=sends;
  CHECK(!audio_shiri.play_native(number,pcm,64,play_samples_are_timed,3,base_raw));
  CHECK(sends==sent && state.sequence==1 && state.frame_index==64 && gap && fd==99);
  CHECK(raw_step_index==64);checks++;
}
static void no_hard_os_delivery_claim(void){
  prepare();const uint64_t raw[]={base_raw,base_raw};raw_script(raw,2);preempt_after_check=1;
  CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,3,base_raw+1));
  CHECK(last.presentation==base_raw+1 && send_observed_now>last.presentation);
  CHECK(fd==99 && state.frame_index==16);checks++;
}
static void ownership_and_flush(void){
  prepare();int sent=sends;
  CHECK(audio_shiri.play_native(number-1,pcm,16,play_samples_are_timed,1,1)==ESTALE);
  CHECK(sends==sent && !raw_index && !clock_index);
  CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,1,0));
  CHECK(sends==sent && state.frame_index==16 && state.sequence==1 && gap && !raw_index);
  audio_shiri.flush_native(number);
  CHECK(state.generation==2 && !state.sequence && !state.frame_index);
  const uint64_t raw[]={base_raw,base_raw};raw_script(raw,2);
  CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,1,base_raw+1000000));
  CHECK(last.generation==2 && last.epoch==1 && last.sequence==1 && !last.frame_index);
  preserved_fence(state);audio_shiri.session_end(number);CHECK(fd<0);checks++;
}
#ifdef HAS_EXPIRED_PREFIX
static void arithmetic_reference(void){
  const uint32_t lengths[]={1,2,366,8192};
  for(unsigned k=0;k<sizeof(lengths)/sizeof(lengths[0]);k++){
    uint32_t frames=lengths[k];uint64_t last_offset=(uint64_t)(frames-1)*UINT64_C(1000000000)/48000u;
    const uint64_t edge[]={0,1,20832,20833,20834,41665,41666,41667,last_offset,last_offset+1,UINT64_MAX-base_raw};
    CHECK(!expired_prefix(base_raw,base_raw-1,frames));
    for(unsigned e=0;e<sizeof(edge)/sizeof(edge[0]);e++)
      CHECK(expired_prefix(base_raw,base_raw+edge[e],frames)==expired_reference(base_raw,base_raw+edge[e],frames));
    for(unsigned i=0;i<frames;i++){
      uint64_t offset=(uint64_t)i*UINT64_C(1000000000)/48000u;
      CHECK(expired_prefix(base_raw,base_raw+offset,frames)==i+1);
      if(offset)CHECK(expired_prefix(base_raw,base_raw+offset-1,frames)==i);
    }
  }
  checks++;
}
#endif
static void save_packet(FILE *file){
  CHECK(fwrite(captured,1,captured_bytes,file)==captured_bytes);
}
static void consumer_wire(const char *path,int skip_whole){
  FILE *file=fopen(path,"wb");CHECK(file);
  if(!skip_whole){
    partial(63734,366);save_packet(file);
    const uint64_t raw[]={base_raw+63734,base_raw+63734};raw_script(raw,2);
    CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,124,base_raw+7625000));
    CHECK(last.sequence==2 && last.frame_index==366 && !(last.flags&SHIRI_PCM_GAP));
    save_packet(file);
  }else{
    future_wire(1000000,NULL);save_packet(file);
    const uint64_t raw[]={base_raw+1645833,base_raw+1645833};raw_script(raw,2);
    int sent=sends;
    CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,5,base_raw+1333333));
    CHECK(sends==sent && state.frame_index==32 && state.sequence==2 && gap);
    raw_script(raw,2);
    CHECK(!audio_shiri.play_native(number,pcm,16,play_samples_are_timed,9,base_raw+1666666));
    CHECK(last.sequence==3 && last.frame_index==32 && last.flags&SHIRI_PCM_GAP);
    save_packet(file);
  }
  CHECK(!fclose(file));
}
int main(int argc,char **argv){
  audio_shiri.init(0,NULL);
  CHECK(!audio_shiri.configure(audio_shiri.get_configuration(2,48000,16),NULL));
  if(argc==3 && !strcmp(argv[1],"--future-wire")){future_wire(150000000,argv[2]);return 0;}
  if(argc==3 && !strcmp(argv[1],"--consumer-wire")){consumer_wire(argv[2],0);return 0;}
  if(argc==3 && !strcmp(argv[1],"--gap-wire")){consumer_wire(argv[2],1);return 0;}
  /* Original source demonstrably exports this already-expired first sample. */
  partial(63734,366);
  future_wire(1,NULL);future_wire(150000000,NULL);
  const uint64_t edge[]={0,20833,20834,41666,41667,312499};
  for(unsigned i=0;i<sizeof(edge)/sizeof(edge[0]);i++)partial(edge[i],16);
  original_calendar_after_repeated_trim();
  full_expiry_then_fresh(0,1);full_expiry_then_fresh(312500,16);
  full_expiry_then_fresh((UINT64_C(365)*UINT64_C(1000000000))/48000u,366);
  full_expiry_then_fresh(UINT64_C(1000000000),16);
  send_pressure_after_partial();
  failed_final_clock(0,0);failed_final_clock(UINT64_MAX,0);failed_final_clock(base_raw-1,0);
  failed_final_clock(0,1);failed_final_clock(UINT64_MAX,1);failed_final_clock(base_raw+20832,1);
  final_raw_inside_original_deadline();trimming_does_not_reset_deadline();
  finite_progress();no_hard_os_delivery_claim();ownership_and_flush();
#ifdef HAS_EXPIRED_PREFIX
  arithmetic_reference();
#endif
  printf("PASS %u publisher deadline cases; enumerated original calendar; admission-time boundary only\n",checks);
  return 0;
}

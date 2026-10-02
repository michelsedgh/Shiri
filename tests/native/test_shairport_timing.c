/* Actual backend callbacks with controlled native socket grants, no receiver or device. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <errno.h>
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
#include "shiri_timing.h"
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
static uint64_t clock_values[32], raw_values[16];
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
  assert(count<=32&&raws<=16);memcpy(clock_values,monotonic,count*sizeof(*monotonic));
  memcpy(raw_values,raw,raws*sizeof(*raw));clock_count=count;raw_count=raws;clock_index=raw_index=0;
}
static int mock_socket(int d,int t,int p){assert(d==AF_UNIX);(void)t;(void)p;return 99;}
static int mock_connect(int s,const struct sockaddr*a,socklen_t n){assert(s==99);(void)a;(void)n;return 0;}
static int mock_poll(struct pollfd*f,nfds_t n,int ms){assert(n==1&&ms<=6000);f->revents=f->events;return 1;}
static int mock_getsockopt(int s,int l,int o,void*v,socklen_t*n){assert(s==99&&l==SOL_SOCKET&&o==SO_PEERCRED);struct ucred*u=v;u->uid=(uid_t)peer;*n=sizeof(*u);return 0;}
static int mock_setsockopt(int s,int l,int o,const void*v,socklen_t n){(void)s;(void)l;(void)o;(void)v;(void)n;return 0;}
static ssize_t mock_send(int s,const void*p,size_t n,int f){
  (void)s;(void)f;if(deny_send){deny_send=0;errno=EAGAIN;return -1;}
  assert(n<=sizeof(captured));memcpy(captured,p,n);assert(shiri_pcm_decode(&last,captured,n)==0);sends++;return (ssize_t)n;
}
static ssize_t mock_recv(int s,void*p,size_t n,int f){
  (void)s;(void)f;if(n==1){if(peer_gone)return 0;errno=EAGAIN;return -1;}assert(n==SHIRI_PCM_HEADER);struct shiri_pcm_packet reply=last;
  reply.kind=SHIRI_GRANT;reply.epoch=1;memset(reply.incarnation,7,16);shiri_pcm_encode(p,&reply);return (ssize_t)n;
}
static int mock_close(int n){if(n==99){closed++;return 0;}return close(n);}
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
  if(raw_count){assert(raw_index<raw_count);return raw_values[raw_index++];}
  return UINT64_C(10000000000);
}
void warn(const char*p,...){(void)p;}
void die(const char*p,...){(void)p;exit(71);}
void parse_audio_options(const char*n,uint32_t f,uint32_t r,uint32_t c){assert(!strcmp(n,"shiri")&&f==(1u<<SPS_FORMAT_S16_LE)&&r==(1u<<SPS_RATE_48000)&&c==(1u<<2));}
int32_t search_for_suitable_configuration(unsigned int c,unsigned int r,unsigned int f,int(*check)(unsigned int,unsigned int,unsigned int)){(void)check;return (int32_t)((c<<25)|((r/2)<<6)|f);}
static void clock_result(const uint64_t *mono,size_t count,const uint64_t *raw,size_t raws,int expected){
  static int number=100;
  uint8_t pcm[64],original[64];memset(pcm,42,sizeof(pcm));memcpy(original,pcm,sizeof(pcm));
  audio_shiri.session_begin(number,"01234567-89ab-cdef-0123-456789abcdef",1,1,-15);
  assert(fd==99);state.sequence=17;state.frame_index=32;gap=1;
  struct shiri_pcm_packet saved=state;int before=sends;
  clocks(mono,count,raw,raws);
  assert(audio_shiri.play_native(number,pcm,16,play_samples_are_timed,UINT32_MAX-4,UINT64_C(10150000000))==expected);
  assert(clock_index==count&&raw_index==raws&&!memcmp(pcm,original,sizeof(pcm)));
  if(expected){
    assert(sends==before&&fd<0&&gap==1&&!memcmp(&saved,&state,sizeof(state)));
    assert(audio_shiri.session_retired(number));
  }else{
    assert(sends==before+1&&fd==99&&state.sequence==18&&state.frame_index==48&&gap==0);
    assert(last.sequence==18&&last.frame_index==32&&last.generation==saved.generation&&last.epoch==saved.epoch);
    assert(!memcmp(last.session,saved.session,16)&&!memcmp(last.incarnation,saved.incarnation,16));
    assert(!memcmp(last.group,saved.group,16)&&last.flags==(saved.flags|SHIRI_PCM_GAP));
    assert(last.presentation==UINT64_C(10150000000)&&last.rtp==UINT32_MAX-4);
    assert(last.mono_before==mono[count-2]&&last.mono_after==mono[count-1]&&last.raw_sample==raw[raws-1]);
    assert(last.mono_after-last.mono_before<=UINT64_C(1000000));
    assert(!memcmp(captured+SHIRI_PCM_HEADER,original,sizeof(original)));
  }
  clock_count=raw_count=0;number++;
}
static void clock_retries(void){
  const uint64_t raw[]={UINT64_C(10000000000),UINT64_C(10001000000),UINT64_C(10002000000),UINT64_C(10003000000)};
  /* The observed 1.347960ms interruption spoils only the first bracket. */
  const uint64_t retry[]={9000000000,9001347960,9001348060,9001348260};
  clock_result(retry,4,raw,2,0);
  const uint64_t fourth[]={9000000000,9001100000,9001100100,9002200100,9002200200,9003300200,9003300300,9003300500};
  clock_result(fourth,8,raw,4,0);
  const uint64_t all_wide[]={9000000000,9001100000,9001100100,9002200100,9002200200,9003300200,9003300300,9004400300};
  clock_result(all_wide,8,raw,4,ETIMEDOUT);
  const uint64_t exact_bracket[]={9000000000,9001000000};clock_result(exact_bracket,2,raw,1,0);
  const uint64_t bracket_over[]={9000000000,9001000001,9001000100,9001000200};clock_result(bracket_over,4,raw,2,0);
  const uint64_t deadline_narrow[]={9000000000,9001100000,9004999900,9005000000};clock_result(deadline_narrow,4,raw,2,ETIMEDOUT);
  const uint64_t before_deadline[]={9000000000,9001100000,9004999800,9004999999};clock_result(before_deadline,4,raw,2,0);
  const uint64_t past_deadline[]={9000000000,9001100000,9005000000,9005000100};clock_result(past_deadline,4,raw,2,ETIMEDOUT);
  const uint64_t wide_deadline[]={9000000000,9005000000};clock_result(wide_deadline,2,raw,1,ETIMEDOUT);
  const uint64_t reversed[]={9000000100,9000000000};clock_result(reversed,2,raw,1,EIO);
  const uint64_t retry_reversed[]={9000000000,9001100000,9001099999,9001100001};clock_result(retry_reversed,4,raw,2,EIO);
  const uint64_t raw_reversed[]={10000000000,9999999999};
  const uint64_t ordered_retry[]={9000000000,9001100000,9001100100,9001100200};clock_result(ordered_retry,4,raw_reversed,2,EIO);
  const uint64_t zero_before[]={0,9000000000};clock_result(zero_before,2,raw,1,EIO);
  const uint64_t zero_after[]={9000000000,0};clock_result(zero_after,2,raw,1,EIO);
  const uint64_t zero_raw[]={0};const uint64_t narrow[]={9000000000,9000000100};clock_result(narrow,2,zero_raw,1,EIO);
  const uint64_t failed_raw[]={UINT64_MAX};clock_result(narrow,2,failed_raw,1,EIO);
  const uint64_t failed_before[]={UINT64_MAX,9000000100};clock_result(failed_before,2,raw,1,EIO);
  const uint64_t failed_after[]={9000000000,UINT64_MAX};clock_result(failed_after,2,raw,1,EIO);
  /* Unsampled lead-in remains intentionally gap-marked; exact stale sessions
   * cannot consume a clock attempt, send media or mutate the current route. */
  audio_shiri.session_begin(200,NULL,0,1,-10);
  uint8_t pcm[64]={0};struct shiri_pcm_packet saved=state;int before=sends;
  clocks(narrow,2,raw,1);
  assert(audio_shiri.play_native(199,pcm,16,play_samples_are_timed,1,1)==ESTALE);
  assert(!clock_index&&!raw_index&&sends==before&&!memcmp(&saved,&state,sizeof(state)));
  assert(audio_shiri.play_native(200,pcm,16,play_samples_are_untimed,1,0)==0);
  assert(!clock_index&&!raw_index&&sends==before&&state.sequence==1&&state.frame_index==16&&gap==1);
  assert(audio_shiri.play_native(200,pcm,16,play_samples_are_timed,1,UINT64_C(10150000000))==0);
  assert(last.sequence==2&&last.frame_index==16&&(last.flags&SHIRI_PCM_GAP));
  clock_count=raw_count=0;audio_shiri.flush_native(200);
  assert(state.generation==2&&state.sequence==0&&state.frame_index==0);
}
int main(int argc,char **argv){
  if(argc==2&&!strcmp(argv[1],"clock-preimage")){
    audio_shiri.init(0,NULL);assert(audio_shiri.configure(audio_shiri.get_configuration(2,48000,16),NULL)==0);
    audio_shiri.session_begin(13,NULL,0,1,-10);int before=sends;
    const uint64_t mono[]={9000000000,9001347960},raw[]={10000000000};clocks(mono,2,raw,1);
    uint8_t pcm[64]={0};assert(audio_shiri.play_native(13,pcm,16,play_samples_are_timed,1,UINT64_C(10150000000))==0);
    assert(sends==before+1&&last.mono_after-last.mono_before==UINT64_C(1347960)&&state.frame_index==16);
    clock_count=raw_count=0;audio_shiri.deinit();puts("preimage: preempted 1.347960ms bracket sent invalid PCM before receiver rejection");return 0;
  }
  if(argc==2){audio_shiri.init(atoi(argv[1]),NULL);audio_shiri.deinit();return 0;}
  /* Pinned parse_options returns argc+1 without a '--' separator, so its
   * ordinary daemon startup invokes the backend with argc=-1. */
  audio_shiri.init(-1,NULL);
  audio_shiri.deinit();
  audio_shiri.init(0,NULL);
  int32_t format=audio_shiri.get_configuration(8,44100,32);
  assert(audio_shiri.configure(format,NULL)==0);
  audio_shiri.session_begin(13,"01234567-89ab-cdef-0123-456789abcdef",1,1,-15);
  assert(fd==99&&last.kind==SHIRI_VOLUME&&last.frames==50);
  uint8_t session[16];memcpy(session,state.session,16);
  assert(!shiri_zero(session,16)&&state.group[0]==1&&state.group[15]==239);
  assert(state.flags==(SHIRI_PCM_AIRPLAY2|SHIRI_PCM_GROUP_LEADER));
  audio_shiri.session_group(13,"f1234567-89ab-cdef-0123-456789abcdef",0);
  assert(state.group[0]==241&&!(state.flags&SHIRI_PCM_GROUP_LEADER));
  audio_shiri.session_group(12,"01234567-89ab-cdef-0123-456789abcdef",1);
  assert(state.group[0]==241&&!memcmp(state.session,session,16));
  int before=sends;audio_shiri.session_volume(12,-3);assert(sends==before);
  uint8_t pcm[64];memset(pcm,42,sizeof(pcm));
  assert(audio_shiri.play_native(13,pcm,16,play_samples_are_timed,UINT32_MAX-4,UINT64_C(10150000000))==0);
  assert(last.kind==SHIRI_PCM&&last.frames==16&&last.rtp==UINT32_MAX-4&&last.presentation==UINT64_C(10150000000));
  assert(last.raw_sample==UINT64_C(10000000000)&&last.mono_after>=last.mono_before);
  assert(last.frame_index==0&&last.sequence==1&&!memcmp(captured+SHIRI_PCM_HEADER,pcm,64));
  deny_send=1;assert(audio_shiri.play_native(13,pcm,16,play_samples_are_timed,12,UINT64_C(10150333333))==0);
  assert(audio_shiri.play_native(13,pcm,16,play_samples_are_timed,28,UINT64_C(10150666666))==0);
  assert(last.flags&SHIRI_PCM_GAP);assert(last.frame_index==32&&last.sequence==3);
  audio_shiri.flush_native(13);assert(state.generation==2&&last.kind==SHIRI_FLUSH);
  assert(audio_shiri.play_native(13,pcm,16,play_samples_are_timed,44,UINT64_C(10151000000))==0);
  assert(last.generation==2&&last.sequence==1&&last.frame_index==0);
  audio_shiri.session_begin(14,"01234567-89ab-cdef-0123-456789abcdef",0,1,-10);
  assert(memcmp(state.session,session,16));before=sends;audio_shiri.session_volume(13,-1);assert(sends==before);
  before=sends;audio_shiri.session_end(13);audio_shiri.flush_native(13);
  assert(audio_shiri.play_native(13,pcm,16,play_samples_are_timed,1,1)==ESTALE&&sends==before);
  assert(!audio_shiri.session_retired(14));peer_gone=1;assert(audio_shiri.session_retired(14));
  audio_shiri.session_end(14);assert(last.kind==SHIRI_END&&fd<0);
  assert(audio_shiri.play_native(13,pcm,16,play_samples_are_timed,1,1)==ESTALE);
  peer=62000;audio_shiri.session_begin(15,NULL,0,1,-10);assert(fd<0); /* wrong peer cannot grant */
  assert(shiri_skipped_time(1000000000,48,48000)==1001000000);
  assert(shiri_skipped_time(1000000000,441,44100)==1010000000);
  assert(shiri_skipped_time(UINT64_MAX-10,1,48000)==UINT64_MAX);
  peer=61000;peer_gone=0;clock_retries();
  audio_shiri.deinit();assert(closed>=3);
  puts("actual native callback/group/session/flush/gap + partial-skip + 4-attempt/5ms clock retry timing passed");return 0;
}

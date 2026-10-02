/* Real OwnTone input_write/wait_buffer_ready/framed-pipe/read functions.
 * Private pipe only; no player, audio device or network is opened. */
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "shiri_pcm.h"
#define INPUT_FLAG_EOF 2
#define INPUT_FLAG_ERROR 4
#define INPUT_FLAG_QUALITY 16
#define INPUT_FLAG_SYNC 32
#define INPUT_FLAG_START_NEXT 64
#define DATA_KIND_PIPE 4
#define DATA_KIND_FILE 1
#define PIPE_READ_MAX 65536
#define STOB(n,b,c) ((n)*((b)/8)*(c))
#define DPRINTF(...) ((void)0)
/* @ACTUAL_THRESHOLD_DEFINITIONS@ */
struct evbuffer { uint8_t *data; size_t size,capacity; };
static size_t evbuffer_get_length(struct evbuffer*b){return b->size;}
static int evbuffer_drain(struct evbuffer*b,size_t n){if(n>b->size)n=b->size;memmove(b->data,b->data+n,b->size-n);b->size-=n;return 0;}
static int evbuffer_remove(struct evbuffer*b,void*p,size_t n){if(n>b->size)n=b->size;memcpy(p,b->data,n);evbuffer_drain(b,n);return (int)n;}
static int evbuffer_copyout(struct evbuffer*b,void*p,size_t n){assert(n<=b->size);memcpy(p,b->data,n);return (int)n;}
static int evbuffer_remove_buffer(struct evbuffer*a,struct evbuffer*b,size_t n){assert(n<=a->size&&b->size+n<=b->capacity);memcpy(b->data+b->size,a->data,n);b->size+=n;evbuffer_drain(a,n);return (int)n;}
static int evbuffer_add_buffer(struct evbuffer*a,struct evbuffer*b){return evbuffer_remove_buffer(b,a,b->size)<0?-1:0;}
static int evbuffer_read(struct evbuffer*b,int fd,int max){assert(b->size+(size_t)max<=b->capacity);ssize_t n=read(fd,b->data+b->size,(size_t)max);if(n>0)b->size+=(size_t)n;return (int)n;}
struct media_quality { int sample_rate,bits_per_sample,channels; };
struct pipe { int fd;bool is_autostarted;struct evbuffer*framed;struct timespec sync_ts;bool sequence_valid;uint64_t sequence,next_frame; };
struct input_source { struct pipe*input_ctx;struct evbuffer*evbuf;struct media_quality quality;int data_kind;bool open; };
struct marker {size_t pos;short flag;void*data;struct marker*prev;};
static struct {pthread_mutex_t mutex;pthread_cond_t cond;struct evbuffer*evbuf;struct marker*marker_tail;
 struct media_quality cur_write_quality,cur_read_quality;size_t bytes_read,bytes_written;}input_buffer;
static struct input_source input_now_reading,*shiri_source;
static struct timespec input_loop_timeout={0,1};
static pthread_mutex_t shiri_gate_lock=PTHREAD_MUTEX_INITIALIZER;
static struct shiri_pcm_owner shiri_owner;
static bool shiri_armed,pipe_framed=true;
static uint64_t shiri_operation,test_now,first_anchor;
static unsigned errors,waiting,full_callbacks;
typedef int cfg_t;
static cfg_t*cfg;
static cfg_t*cfg_getsec(cfg_t*c,const char*section){assert(!strcmp(section,"library"));return c;}
static int cfg_getbool(cfg_t*c,const char*key){(void)c;assert(!strcmp(key,"pipe_framed"));return pipe_framed;}
static int quality_is_equal(const struct media_quality*a,const struct media_quality*b){return !memcmp(a,b,sizeof(*a));}
static int fixture_clock(clockid_t id,struct timespec*t){assert(id==CLOCK_MONOTONIC);t->tv_sec=test_now/1000000000;t->tv_nsec=test_now%1000000000;return 0;}
#define clock_gettime fixture_clock
static struct timespec timespec_reltoabs(struct timespec t){return t;}
static int fixture_timedwait(pthread_cond_t*c,pthread_mutex_t*m,const struct timespec*t){(void)c;(void)m;(void)t;waiting++;return ETIMEDOUT;}
#define pthread_cond_timedwait fixture_timedwait
static void buffer_full_cb(void){full_callbacks++;}
static void input_stop(void){errors++;}
static int input_wait(void){waiting++;return 0;}
static uint64_t outputs_buffer_duration_ms_get(void){return 500;}
static int stop(struct input_source*s){(void)s;shiri_source=NULL;return 0;}
static void markers_set(short flags,size_t len){
 if(flags&INPUT_FLAG_ERROR){errors++;return;}
 if(flags&INPUT_FLAG_SYNC){assert(len>0);struct pipe*p=input_now_reading.input_ctx;
  if(!first_anchor)first_anchor=(uint64_t)p->sync_ts.tv_sec*1000000000+(uint64_t)p->sync_ts.tv_nsec;}
}
/* @ACTUAL_INPUT_FUNCTIONS@ */
/* @ACTUAL_PIPE_FUNCTIONS@ */
static struct evbuffer buffer(size_t capacity){struct evbuffer b={.capacity=capacity,.data=malloc(capacity)};assert(b.data);return b;}
static void reset(struct evbuffer*b){b->size=0;memset(&input_buffer.cur_write_quality,0,sizeof(input_buffer.cur_write_quality));
 input_buffer.bytes_read=input_buffer.bytes_written=first_anchor=errors=waiting=full_callbacks=0;test_now=UINT64_C(100000000000);}
static void packet(int fd,uint64_t frame,uint64_t presentation){uint8_t data[SHIRI_PCM_HEADER+3840];
 struct shiri_pcm_packet p={.kind=SHIRI_PCM,.clock=SHIRI_MONOTONIC,.epoch=1,.generation=1,.sequence=frame/960+1,
  .frame_index=frame,.presentation=presentation,.frames=960,.payload=3840};
 memcpy(p.incarnation,shiri_owner.incarnation,16);memcpy(p.session,shiri_owner.session,16);
 shiri_pcm_encode(data,&p);for(size_t i=SHIRI_PCM_HEADER;i<sizeof(data);i++)data[i]=(uint8_t)(frame/960);
 assert(write(fd,data,sizeof(data))==(ssize_t)sizeof(data));}
static void warmup(uint64_t lead){
 struct evbuffer buffered=buffer(1400000),pending=buffer(65536),framed=buffer(140000);
 reset(&buffered);input_buffer.evbuf=&buffered;
 int fds[2];assert(!pipe(fds)&&!fcntl(fds[0],F_SETFL,O_NONBLOCK));
 struct pipe p={.fd=fds[0],.is_autostarted=true,.framed=&framed};
 input_now_reading=(struct input_source){.input_ctx=&p,.evbuf=&pending,.quality={48000,16,2},.data_kind=DATA_KIND_PIPE,.open=true};
 shiri_source=&input_now_reading;uint64_t operation=shiri_operation+1;
 assert(!input_pipe_shiri_seal(operation));struct shiri_pcm_owner owner={.epoch=1,.generation=1};
 memset(owner.incarnation,1,16);memset(owner.session,2,16);assert(!input_pipe_shiri_arm(&owner,operation));
 uint64_t origin=test_now,anchor=origin+lead+UINT64_C(4000000000)-UINT64_C(500000000),frame=0;
 bool premature=false;
 /* The actual native timer does not consume PCM before this first source
  * anchor. Feed real framed writes at20ms native cadence throughout warmup. */
 while(test_now<anchor){
  if(wait_buffer_ready()<0){premature=true;break;}
  packet(fds[1],frame,origin+lead+UINT64_C(4000000000)+frame*UINT64_C(1000000000)/48000);
  assert(!play_framed(&input_now_reading)&&!pending.size&&!errors);
  frame+=960;test_now+=UINT64_C(20000000);
 }
 assert(first_anchor==anchor);
#ifdef TEST_PREIMAGE
 assert(premature&&test_now-origin>=UINT64_C(2000000000)&&test_now<anchor);
#else
 assert(!premature&&frame>=960*186&&buffered.size==frame*4&&!errors);
 uint8_t pcm[3840];short flags;void*data;
 /* Once the anchor arrives, actual player-facing input_read releases exactly
  * one native block per producer tick. The buffer remains bounded and drains. */
 for(unsigned i=0;i<100;i++){
  assert(input_read(pcm,sizeof(pcm),&flags,&data)==(int)sizeof(pcm));
  for(size_t j=0;j<sizeof(pcm);j++)assert(pcm[j]==(uint8_t)i);
  assert(!wait_buffer_ready());
  packet(fds[1],frame,origin+lead+UINT64_C(4000000000)+frame*UINT64_C(1000000000)/48000);
  assert(!play_framed(&input_now_reading)&&!pending.size&&!errors);
  frame+=960;test_now+=UINT64_C(20000000);
 }
 assert(buffered.size<input_buffer_threshold()+3840);
#endif
 assert(!input_pipe_shiri_seal(operation+1));shiri_source=NULL;
 close(fds[0]);close(fds[1]);free(buffered.data);free(pending.data);free(framed.data);
}
static void limits(void){
 struct evbuffer buffered=buffer(1400000),pending=buffer(65536);reset(&buffered);input_buffer.evbuf=&buffered;
 input_now_reading.data_kind=DATA_KIND_PIPE;pipe_framed=true;
#ifndef TEST_PREIMAGE
 assert(input_buffer_threshold()==1152000);
#endif
 /* Raw PIPE and ordinary FILE stay2s even with the native option present. */
 pipe_framed=false;buffered.size=INPUT_BUFFER_THRESHOLD+4;assert(wait_buffer_ready()<0);
 pending.size=4;assert(input_write(&pending,NULL,0)==EAGAIN&&pending.size==4);
 pipe_framed=true;input_now_reading.data_kind=DATA_KIND_FILE;assert(wait_buffer_ready()<0);
 input_now_reading.data_kind=DATA_KIND_PIPE;
#ifndef TEST_PREIMAGE
 buffered.size=input_buffer_threshold()+4;assert(wait_buffer_ready()<0);
 assert(input_write(&pending,NULL,0)==EAGAIN&&pending.size==4);
 uint8_t readback[3840];short flags;void*data;assert(input_read(readback,sizeof(readback),&flags,&data)==3840);
 assert(!wait_buffer_ready()&&!input_write(&pending,NULL,0)&&!pending.size);
 buffered.size=input_buffer_threshold();pending.size=SHIRI_PCM_MAX_PAYLOAD;
 assert(!input_write(&pending,NULL,0)&&!pending.size);
 assert(buffered.size==input_buffer_threshold()+SHIRI_PCM_MAX_PAYLOAD&&wait_buffer_ready()<0);
 pending.size=4;assert(input_write(&pending,NULL,0)==EAGAIN&&pending.size==4);
#endif
 free(buffered.data);free(pending.data);
}
int main(void){(void)cfg;assert(!pthread_mutex_init(&input_buffer.mutex,NULL));assert(!pthread_cond_init(&input_buffer.cond,NULL));
 warmup(UINT64_C(220000000));warmup(UINT64_C(2000000000));limits();
 pthread_cond_destroy(&input_buffer.cond);pthread_mutex_destroy(&input_buffer.mutex);
#ifdef TEST_PREIMAGE
 puts("Actual original2s input limit stalls before3.72s/5.5s native anchors: reproduced");
#else
 puts("Actual native input/pipe/write/wait/read: both3.72s/5.5s first anchors buffered;6s admission threshold+one32768-byte packet and raw/file2s limits preserved");
#endif
 return 0;}

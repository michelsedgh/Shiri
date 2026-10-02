/* SPDX-License-Identifier: GPL-2.0-or-later
 * Actual patched input/pipe functions, controlled buffers and real private FDs. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#include "shiri_pcm.h"
#define INPUT_FLAG_SYNC 32
#define INPUT_FLAG_QUALITY 16
#define INPUT_FLAG_ERROR 4
#define INPUT_FLAG_EOF 2
#define PIPE_READ_MAX 65536
#define DPRINTF(...) do {} while(0)
struct evbuffer { uint8_t data[200000]; size_t size; };
static size_t evbuffer_get_length(struct evbuffer *b) { return b->size; }
static int evbuffer_drain(struct evbuffer *b,size_t size) {
  if(size>b->size)size=b->size;
  memmove(b->data,b->data+size,b->size-size); b->size-=size; return 0;
}
static int evbuffer_remove(struct evbuffer*b,void*p,size_t n) {
  if(n>b->size)n=b->size;
  memcpy(p,b->data,n); evbuffer_drain(b,n); return (int)n;
}
static int evbuffer_remove_buffer(struct evbuffer*a,struct evbuffer*b,size_t n) {
  assert(n<=a->size&&b->size+n<=sizeof(b->data));
  memcpy(b->data+b->size,a->data,n); b->size+=n; evbuffer_drain(a,n); return (int)n;
}
static int evbuffer_copyout(struct evbuffer*b,void*p,size_t n) {
  assert(n<=b->size); memcpy(p,b->data,n); return (int)n;
}
static int evbuffer_read(struct evbuffer*b,int fd,int max) {
  assert(b->size+(size_t)max<=sizeof(b->data));
  ssize_t n=read(fd,b->data+b->size,(size_t)max); if(n>0)b->size+=(size_t)n; return (int)n;
}
struct media_quality { int sample_rate,bits_per_sample,channels; };
struct pipe { int fd; bool is_autostarted; struct evbuffer *framed; struct timespec sync_ts;
              bool sequence_valid; uint64_t sequence,next_frame; };
struct input_source { struct pipe *input_ctx; struct evbuffer *evbuf; struct media_quality quality; };
struct marker { size_t pos; short flag; void*data; struct marker *prev; };
static struct { pthread_mutex_t mutex; pthread_cond_t cond; struct marker*marker_tail;
                struct evbuffer*evbuf; size_t bytes_read; } input_buffer;
static bool pipe_framed=true, shiri_armed;
static pthread_mutex_t shiri_gate_lock=PTHREAD_MUTEX_INITIALIZER;
static struct shiri_pcm_owner shiri_owner;
static uint64_t shiri_operation;
static struct input_source *shiri_source;
static int accepts,waits,errors,stops,backpressure;
static struct timespec accepted_timestamp;
static int shiri_ts_get(struct timespec*,struct input_source*);
static int input_wait(void) { waits++; return 0; }
static uint64_t outputs_buffer_duration_ms_get(void) { return 2000; }
static int stop(struct input_source*s) { (void)s; stops++; shiri_source=NULL; return 0; }
static int input_write(struct evbuffer*b,struct media_quality*q,short flags) {
  (void)q;
  if(flags&INPUT_FLAG_ERROR){errors++;return 0;}
  if(!b)return 0;
  assert(flags==INPUT_FLAG_SYNC);
  if(backpressure){backpressure=0;return EAGAIN;}
  assert(shiri_ts_get(&accepted_timestamp,shiri_source)==0);
  accepts++; evbuffer_drain(b,b->size); return 0;
}
#include "timed_input.inc"
static uint64_t now_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000000u+(uint64_t)t.tv_nsec;}
static struct marker*mark(size_t pos,short flag,void*data,struct marker*next){
  struct marker*m=calloc(1,sizeof(*m));assert(m);m->pos=pos;m->flag=flag;m->data=data;m->prev=next;return m;
}
static void markers_test(void){
  struct evbuffer buffer={.size=96};uint8_t data[128];short flag;void*tag=NULL;
  int a=11,b=22;
  struct timespec anchor={7,31},copy={0};
  input_buffer.evbuf=&buffer; input_buffer.bytes_read=0;
  input_buffer.marker_tail=mark(0,INPUT_FLAG_QUALITY,&a,mark(0,INPUT_FLAG_SYNC,&anchor,NULL));
  assert(input_peek_sync(&copy)==1&&copy.tv_sec==7&&copy.tv_nsec==31);
  assert(input_buffer.marker_tail->flag==INPUT_FLAG_QUALITY); /* copy, no consumption */
  while(input_buffer.marker_tail){struct marker*m=input_buffer.marker_tail;input_buffer.marker_tail=m->prev;free(m);}
  input_buffer.marker_tail=mark(32,INPUT_FLAG_SYNC,&anchor,NULL);
  assert(input_peek_sync(&copy)<0); /* cannot retime preceding PCM to a later anchor */
  free(input_buffer.marker_tail);input_buffer.marker_tail=NULL;
  assert(input_peek_sync(&copy)<0); /* unanchored PCM is never scheduled */
  buffer.size=0;assert(input_peek_sync(&copy)==0);buffer.size=96;
  input_buffer.evbuf=&buffer; input_buffer.bytes_read=0;
  memset(buffer.data,1,32);memset(buffer.data+32,2,64);
  input_buffer.marker_tail=mark(0,INPUT_FLAG_SYNC,&a,mark(32,INPUT_FLAG_SYNC,&b,NULL));
  assert(input_read(data,sizeof(data),&flag,&tag)==32&&flag==INPUT_FLAG_SYNC&&tag==&a);
  for(int i=0;i<32;i++)assert(data[i]==1);
  assert(input_read(data,sizeof(data),&flag,&tag)==64&&flag==INPUT_FLAG_SYNC&&tag==&b);
  for(int i=0;i<64;i++)assert(data[i]==2);
  assert(!input_buffer.marker_tail);
  /* A future anchor must not timestamp bytes that come before its sample. */
  buffer.size=96;input_buffer.bytes_read=0;
  input_buffer.marker_tail=mark(32,INPUT_FLAG_SYNC,&b,NULL);
  assert(input_read(data,32,&flag,&tag)==32&&flag==0&&input_buffer.marker_tail);
  assert(input_read(data,64,&flag,&tag)==64&&flag==INPUT_FLAG_SYNC&&tag==&b);
  /* Existing quality-marker semantics are preserved before the first anchor. */
  buffer.size=96;input_buffer.bytes_read=0;
  input_buffer.marker_tail=mark(0,INPUT_FLAG_QUALITY,&a,mark(0,INPUT_FLAG_SYNC,&b,NULL));
  assert(input_read(data,128,&flag,&tag)==0&&flag==INPUT_FLAG_QUALITY&&tag==&a);
  assert(input_read(data,128,&flag,&tag)==96&&flag==INPUT_FLAG_SYNC&&tag==&b);
}
static void enqueue(int fd,struct shiri_pcm_packet*p){
  uint8_t data[SHIRI_PCM_HEADER+64];p->payload=64;p->frames=16;
  shiri_pcm_encode(data,p);memset(data+SHIRI_PCM_HEADER,7,64);
  assert(write(fd,data,sizeof(data))==(ssize_t)sizeof(data));
}
int main(void){
  pthread_mutex_init(&input_buffer.mutex,NULL);pthread_cond_init(&input_buffer.cond,NULL);
  markers_test();
  struct shiri_pcm_owner owner={.epoch=1,.generation=1};memset(owner.incarnation,1,16);memset(owner.session,2,16);
  assert(input_pipe_shiri_arm(&owner,0)<0);
  assert(input_pipe_shiri_seal(1)==0);assert(input_pipe_shiri_arm(&owner,1)==0); /* before any source */
  assert(input_pipe_shiri_arm(&owner,1)<0);assert(input_pipe_shiri_seal(1)<0);
  int fds[2];assert(pipe(fds)==0);assert(fcntl(fds[0],F_SETFL,O_NONBLOCK)==0);
  struct evbuffer framed={0},pcm={0};struct pipe p={.fd=fds[0],.framed=&framed,.is_autostarted=true};
  struct input_source source={.input_ctx=&p,.evbuf=&pcm,.quality={48000,16,2}};shiri_source=&source;
  struct shiri_pcm_packet packet={.kind=SHIRI_PCM,.clock=SHIRI_MONOTONIC,.epoch=1,.generation=1,.sequence=1,.presentation=now_ns()+4000000000u};
  memcpy(packet.incarnation,owner.incarnation,16);memcpy(packet.session,owner.session,16);
  enqueue(fds[1],&packet);assert(play_framed(&source)==0&&accepts==1);
  assert((uint64_t)accepted_timestamp.tv_sec*1000000000u+(uint64_t)accepted_timestamp.tv_nsec==packet.presentation-2000000000u);
  packet.sequence=2;packet.frame_index=16;backpressure=1;
  enqueue(fds[1],&packet);assert(play_framed(&source)==0&&accepts==1&&pcm.size==64);
  /* An ownership seal discards pending PCM and previously queued frames. */
  packet.sequence=3;packet.frame_index=32;enqueue(fds[1],&packet);
  assert(input_pipe_shiri_seal(2)==0&&pcm.size==0&&framed.size==0);
  owner.generation=2;assert(input_pipe_shiri_arm(&owner,2)==0);
  enqueue(fds[1],&packet);assert(play_framed(&source)==0&&accepts==1); /* old generation ignored */
  packet.generation=2;packet.sequence=1;packet.frame_index=0;
  enqueue(fds[1],&packet);assert(play_framed(&source)==0&&accepts==2);
  enqueue(fds[1],&packet);assert(play_framed(&source)<0&&errors==1); /* repeated frames rejected */
  shiri_source=&source;packet.sequence=2;packet.frame_index=16;
  assert(input_pipe_shiri_seal(3)==0);owner.generation=3;assert(input_pipe_shiri_arm(&owner,3)==0);
  uint8_t partial[12]={0};assert(write(fds[1],partial,sizeof(partial))==(ssize_t)sizeof(partial));close(fds[1]);
  assert(play_framed(&source)==0);assert(play_framed(&source)<0&&errors==2); /* EOF in header */
  close(fds[0]);pthread_cond_destroy(&input_buffer.cond);pthread_mutex_destroy(&input_buffer.mutex);
  puts("actual timed-pipe/source gate + first-sample marker boundaries passed");return 0;
}

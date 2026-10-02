/* Actual pinned request, poll, controller and SBC-reset seams; no Bluetooth hardware. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#define ARRAYSIZE(a) (sizeof(a)/sizeof(*(a)))
#define debug(...) ((void)0)
#define warn(...) ((void)0)
#define error(...) ((void)0)
#define SPLICE_F_NONBLOCK 2
#define BA_TRANSPORT_PCM_MODE_SINK 1
#define BA_TRANSPORT_PROFILE_A2DP_SOURCE 1
#define A2DP_CODEC_SBC 0
#define BA_TRANSPORT_PCM_SIGNAL_OPEN 0
#define BA_TRANSPORT_PCM_SIGNAL_CLOSE 1
#define BA_TRANSPORT_PCM_SIGNAL_RESUME 3
#define BA_TRANSPORT_PCM_SIGNAL_DRAIN 4
#define BA_TRANSPORT_PCM_SIGNAL_DROP 5
#define BLUEALSA_PCM_CTRL_DRAIN "Drain"
#define BLUEALSA_PCM_CTRL_DROP "Drop"
#define BLUEALSA_PCM_CTRL_PAUSE "Pause"
#define BLUEALSA_PCM_CTRL_RESUME "Resume"
#define G_GNUC_UNUSED __attribute__((unused))
#define G_SOURCE_CONTINUE 1
#define G_SOURCE_REMOVE 0
#define SBC_LE 0
#define DEBUG 0
struct ba_transport { int profile; uint32_t codec; };
struct ba_transport_pcm {
  struct ba_transport *t;
  int mode,fd,pipe[2],rate;
  void *controller;
  bool controller_restricted;
  bool paused,drained,running;
  pthread_mutex_t mutex;
  pthread_cond_t cond;
  /* @ACTUAL_DROP_FIELDS@ */
};
struct io_poll {
  struct { int frames; } asrs;
  bool initiated,tainted,draining;
  int timeout;
  /* @ACTUAL_IO_FIELDS@ */
};
typedef struct { char data[256],*tail; size_t used; } ffb_t;
static void ffb_rewind(ffb_t *f) { f->tail=f->data;f->used=0; }
static void ffb_seek(ffb_t *f, size_t n) { f->used+=n;f->tail=f->data+f->used; }
static size_t ffb_len_in(ffb_t *f) { return sizeof(f->data)-f->used; }
static size_t ffb_blen_in(ffb_t *f) { return ffb_len_in(f); }
#define asrsync_init(s,r) ((void)(s),(void)(r))
static uint32_t ba_transport_get_codec(struct ba_transport *t) { return t->codec; }
static bool ba_transport_pcm_state_check_running(struct ba_transport_pcm *p) { return p->running; }
static int ba_transport_pcm_signal_recv(struct ba_transport_pcm *p) { int s;return read(p->pipe[0],&s,sizeof(s))==sizeof(s)?s:-1; }
static int ba_transport_pcm_signal_send(struct ba_transport_pcm *p,int s) {return write(p->pipe[1],&s,sizeof(s))==sizeof(s)?0:-1;}
static ssize_t io_pcm_read(struct ba_transport_pcm *p, void *b,size_t n) {return read(p->fd,b,n);}
static unsigned async_drop_count,reset_count,write_count,legacy_calls;
static int reset_result;
static bool endless_flush;
static struct { int null_fd,sbc_quality; } config;
static ssize_t mock_splice(int fd, void *a,int dst,void *b,size_t len,unsigned flags) {
  (void)a;(void)b;(void)dst;assert(flags==SPLICE_F_NONBLOCK);
  if(endless_flush) return 128;
  char buffer[1024];return read(fd,buffer,len<sizeof(buffer)?len:sizeof(buffer));
}
#define splice mock_splice
static int ba_transport_pcm_drop(struct ba_transport_pcm *p) {(void)p;async_drop_count++;return 0;}
static int ba_transport_pcm_drain(struct ba_transport_pcm *p) {(void)p;legacy_calls++;return 0;}
static int ba_transport_pcm_pause(struct ba_transport_pcm *p) {legacy_calls++;p->paused=true;return 0;}
static int ba_transport_pcm_resume(struct ba_transport_pcm *p) {legacy_calls++;p->paused=false;return 0;}
static ssize_t io_pcm_flush(struct ba_transport_pcm *p) {char x[1024];while(read(p->fd,x,sizeof(x))>0){}return 0;}
static int sbc_reinit_a2dp(void *s,int flags,const void *c,size_t n) {(void)s;(void)flags;(void)c;(void)n;reset_count++;return reset_result;}
static unsigned sbc_a2dp_get_bitpool(const void *c,int q) {(void)c;(void)q;return 32;}
static void ba_transport_stop_if_no_clients(struct ba_transport *t) {(void)t;}
static int ba_transport_pcm_release(struct ba_transport_pcm *p);
typedef struct { const char *message; } GError;
typedef int GIOCondition;
enum {G_IO_STATUS_NORMAL,G_IO_STATUS_AGAIN,G_IO_STATUS_ERROR,G_IO_STATUS_EOF};
typedef struct { const char *command;char response[32]; } GIOChannel;
static int g_io_channel_read_chars(GIOChannel *ch,char *out,size_t cap,size_t *len,GError **err) {
  (void)err;*len=strlen(ch->command);assert(*len<cap);memcpy(out,ch->command,*len);return G_IO_STATUS_NORMAL;
}
static int g_io_channel_write_chars(GIOChannel *ch,const char *text,int n,size_t *len,void *err) {
  (void)n;(void)err;*len=strlen(text);strcpy(ch->response,text);write_count++;return 0;
}
static void g_io_channel_flush(GIOChannel *ch,void *e) {(void)ch;(void)e;}
static void g_error_free(GError *e) {(void)e;}
/* macOS has no monotonic condition clock; only the fixture adapts its wait. */
static int fixture_cond_timedwait(pthread_cond_t *c,pthread_mutex_t *m,const struct timespec *deadline) {
#ifdef __APPLE__
  struct timespec mono,real;assert(clock_gettime(CLOCK_MONOTONIC,&mono)==0);assert(clock_gettime(CLOCK_REALTIME,&real)==0);
  int64_t remaining=(int64_t)(deadline->tv_sec-mono.tv_sec)*1000000000+deadline->tv_nsec-mono.tv_nsec;
  if(remaining<=0) return ETIMEDOUT;
  real.tv_sec+=remaining/1000000000;real.tv_nsec+=remaining%1000000000;
  if(real.tv_nsec>=1000000000){real.tv_sec++;real.tv_nsec-=1000000000;}
  return pthread_cond_timedwait(c,m,&real);
#else
  return pthread_cond_timedwait(c,m,deadline);
#endif
}
#define pthread_cond_timedwait fixture_cond_timedwait
/* @ACTUAL_REQUEST_FUNCTIONS@ */
#undef pthread_cond_timedwait
/* @ACTUAL_IO_FUNCTIONS@ */
static void g_source_destroy(void *s) {(void)s;}
static void g_source_unref(void *s) {(void)s;}
/* @ACTUAL_RELEASE@ */
/* @ACTUAL_CONTROLLER@ */
typedef int GDBusMethodInvocation;
static bool restricted_open;
static void bluealsa_pcm_open_common(GDBusMethodInvocation *inv,void *pcm,bool restricted){(void)inv;(void)pcm;restricted_open=restricted;}
/* @ACTUAL_OPEN_WRAPPERS@ */
static void codec_drop(struct io_poll *state,struct ba_transport_pcm *t_pcm) {
  struct io_poll io=*state;
  struct {unsigned bitpool;int endian;} sbc={0};
  struct {int unused;} cfg={0},*configuration=&cfg;
  /* @ACTUAL_SBC_RESET@ */
  *state=io;
  (void)sbc;
}
struct request {struct ba_transport_pcm *pcm;int result,error;};
static void *request_thread(void *argument) {struct request *r=argument;r->result=ba_transport_pcm_drop_sync(r->pcm);r->error=errno;return NULL;}
static double seconds(void) {struct timespec x;assert(clock_gettime(CLOCK_MONOTONIC,&x)==0);return x.tv_sec+x.tv_nsec/1e9;}
static int writer_fd;
static struct ba_transport transport={BA_TRANSPORT_PROFILE_A2DP_SOURCE,A2DP_CODEC_SBC};
static void initialize(struct ba_transport_pcm *p) {
  memset(p,0,sizeof(*p));p->t=&transport;p->mode=BA_TRANSPORT_PCM_MODE_SINK;p->running=true;p->rate=48000;
  assert(pthread_mutex_init(&p->mutex,NULL)==0);assert(pthread_mutex_init(&p->drop_sync_mtx,NULL)==0);
  pthread_condattr_t a;assert(pthread_condattr_init(&a)==0);
#ifndef __APPLE__
  assert(pthread_condattr_setclock(&a,CLOCK_MONOTONIC)==0);
#endif
  assert(pthread_cond_init(&p->drop_sync_cond,&a)==0);pthread_condattr_destroy(&a);p->drop_sync_initialized=true;
  int data[2];assert(pipe(data)==0);p->fd=data[0];writer_fd=data[1];
  assert(pipe(p->pipe)==0);assert(pipe(p->drop_sync_pipe)==0);
  assert(fcntl(p->fd,F_SETFL,O_NONBLOCK)==0);assert(fcntl(p->drop_sync_pipe[0],F_SETFL,O_NONBLOCK)==0);
  assert(fcntl(p->drop_sync_pipe[1],F_SETFL,O_NONBLOCK)==0);
  reset_result=0;endless_flush=false;
}
static uint64_t take_token(struct ba_transport_pcm *p) {
  uint64_t token;struct pollfd f={p->drop_sync_pipe[0],POLLIN,0};assert(poll(&f,1,500)==1);
  assert(read(f.fd,&token,sizeof(token))==sizeof(token));return token;
}
static uint64_t pending_token(struct ba_transport_pcm *p) {
  struct pollfd f={p->drop_sync_pipe[0],POLLIN,0};assert(poll(&f,1,500)==1);
  double deadline=seconds()+.5;uint64_t token=0;
  while(!token&&seconds()<deadline) {pthread_mutex_lock(&p->drop_sync_mtx);token=p->drop_sync_active;pthread_mutex_unlock(&p->drop_sync_mtx);if(!token){struct timespec delay={0,1000000};nanosleep(&delay,NULL);}}
  assert(token);return token;
}
static void finish(struct ba_transport_pcm *p) {
  close(p->fd);close(writer_fd);close(p->pipe[0]);close(p->pipe[1]);close(p->drop_sync_pipe[0]);close(p->drop_sync_pipe[1]);
  pthread_mutex_destroy(&p->mutex);pthread_mutex_destroy(&p->drop_sync_mtx);pthread_cond_destroy(&p->drop_sync_cond);
}
static void *encoder_thread(void *argument) {
  struct ba_transport_pcm *p=argument;struct pollfd f={p->drop_sync_pipe[0],POLLIN,0};assert(poll(&f,1,500)==1);
  ffb_t raw={.used=100};raw.tail=raw.data+100;struct io_poll io={.timeout=-1};
  assert(io_poll_and_read_pcm(&io,p,&raw)==-1&&errno==ESTALE);assert(raw.used==0);
  char x;assert(read(p->fd,&x,1)==-1&&errno==EAGAIN);codec_drop(&io,p);assert(io.drop_sync_token==0);return NULL;
}
static void *encoder_retire_legacy(void *argument) {
  struct ba_transport_pcm *p=argument;ffb_t raw={.used=100};raw.tail=raw.data+100;struct io_poll io={.timeout=-1};
  unsigned resets=0;
  do {assert(io_poll_and_read_pcm(&io,p,&raw)==-1&&errno==ESTALE);assert(raw.used==0);resets++;uint64_t token=io.drop_sync_token;codec_drop(&io,p);if(token)break;} while(resets<8);
  assert(resets==3);struct pollfd legacy={p->pipe[0],POLLIN,0};assert(poll(&legacy,1,0)==0);
  assert(write(writer_fd,"new",3)==3);assert(io_poll_and_read_pcm(&io,p,&raw)==3&&raw.used==3);
  return NULL;
}
int main(void) {
  struct ba_transport_pcm p;unsigned cases=0;
  bluealsa_pcm_open(NULL,&p);assert(!restricted_open);cases++;
  bluealsa_pcm_open_restricted(NULL,&p);assert(restricted_open);cases++;
  for(int failed=0;failed<2;failed++) {
    initialize(&p);reset_result=failed?-1:0;p.paused=true;p.controller_restricted=true;assert(write(writer_fd,"old raw PCM",11)==11);
    pthread_t encoder;assert(pthread_create(&encoder,NULL,encoder_thread,&p)==0);
    unsigned resets=reset_count;GIOChannel ch={.command="DropSync"};bluealsa_pcm_controller(&ch,0,&p);
    assert(strcmp(ch.response,failed?"Failed":"OK")==0);assert(reset_count==resets+1&&p.paused);
    pthread_join(encoder,NULL);finish(&p);cases+=2;
  }
  initialize(&p);GIOChannel invalid={.command="DropSyncX"};bluealsa_pcm_controller(&invalid,0,&p);assert(strcmp(invalid.response,"Invalid")==0);
  invalid=(GIOChannel){.command="DropSyn"};bluealsa_pcm_controller(&invalid,0,&p);assert(strcmp(invalid.response,"Invalid")==0);cases+=2;
  unsigned drops=async_drop_count;invalid=(GIOChannel){.command="Drop"};bluealsa_pcm_controller(&invalid,0,&p);
  assert(strcmp(invalid.response,"OK")==0&&async_drop_count==drops+1);cases++;
  const char *forbidden[]={"","D","Drop","DropSyn","DropSyncX","DROPsync","dropSync","Drain","Pause","Resume","Unknown"};
  p.controller_restricted=true;
  for(unsigned i=0;i<ARRAYSIZE(forbidden);i++){invalid=(GIOChannel){.command=forbidden[i]};unsigned before=legacy_calls;
    bluealsa_pcm_controller(&invalid,0,&p);assert(strcmp(invalid.response,"Invalid")==0&&legacy_calls==before&&async_drop_count==drops+1&&!p.paused);cases++;}
  p.controller_restricted=false;
  struct request old={&p,42,0};double started=seconds();assert(ba_transport_pcm_drop_sync(&p)==-1&&errno==ETIMEDOUT);
  assert(seconds()-started<.2);uint64_t old_token=take_token(&p);cases++;
  struct request next={&p,42,0};pthread_t waiter;assert(pthread_create(&waiter,NULL,request_thread,&next)==0);
  uint64_t next_token=take_token(&p);assert(next_token>old_token);ba_transport_pcm_drop_sync_complete(&p,old_token,0);
  pthread_mutex_lock(&p.drop_sync_mtx);assert(p.drop_sync_active==next_token&&p.drop_sync_result==EINPROGRESS);pthread_mutex_unlock(&p.drop_sync_mtx);cases++;
  assert(ba_transport_pcm_drop_sync(&p)==-1&&errno==EBUSY);cases++;
  ba_transport_pcm_drop_sync_complete(&p,next_token,0);pthread_join(waiter,NULL);assert(next.result==0);cases++;
  assert(pthread_create(&waiter,NULL,request_thread,&old)==0);old_token=take_token(&p);ba_transport_pcm_drop_sync_cancel(&p);
  pthread_join(waiter,NULL);assert(old.result==-1&&old.error==ECANCELED);cases++;
  assert(pthread_create(&waiter,NULL,request_thread,&next)==0);next_token=take_token(&p);ba_transport_pcm_drop_sync_complete(&p,old_token,0);
  ba_transport_pcm_drop_sync_complete(&p,next_token,0);pthread_join(waiter,NULL);assert(next.result==0);cases++;
  assert(pthread_create(&waiter,NULL,request_thread,&old)==0);take_token(&p);pthread_cancel(waiter);void *cancelled;pthread_join(waiter,&cancelled);assert(cancelled==PTHREAD_CANCELED);
  assert(pthread_mutex_trylock(&p.drop_sync_mtx)==0);assert(p.drop_sync_active==0);pthread_mutex_unlock(&p.drop_sync_mtx);cases++;
  assert(pthread_create(&waiter,NULL,request_thread,&next)==0);next_token=take_token(&p);
  struct drop_sync_waiter stale={&p,next_token-1};pthread_mutex_lock(&p.drop_sync_mtx);drop_sync_wait_cancel(&stale);
  pthread_mutex_lock(&p.drop_sync_mtx);assert(p.drop_sync_active==next_token);pthread_mutex_unlock(&p.drop_sync_mtx);
  ba_transport_pcm_drop_sync_complete(&p,next_token,0);pthread_join(waiter,NULL);assert(next.result==0);cases++;
  assert(pthread_create(&waiter,NULL,request_thread,&next)==0);next_token=take_token(&p);
  pthread_mutex_lock(&p.drop_sync_mtx);assert(clock_gettime(CLOCK_MONOTONIC,&p.drop_sync_deadline)==0);pthread_mutex_unlock(&p.drop_sync_mtx);
  ba_transport_pcm_drop_sync_complete(&p,next_token,0);pthread_join(waiter,NULL);assert(next.result==-1&&next.error==ETIMEDOUT);cases++;
  assert(pthread_create(&waiter,NULL,request_thread,&next)==0);pending_token(&p);endless_flush=true;
  struct io_poll io={.timeout=-1};ffb_t raw={0};raw.tail=raw.data;started=seconds();
  assert(io_poll_and_read_pcm(&io,&p,&raw)==-1&&errno==ESTALE);codec_drop(&io,&p);pthread_join(waiter,NULL);
  assert(next.result==-1&&next.error==ETIMEDOUT&&seconds()-started<.2);cases++;endless_flush=false;
  assert(pthread_create(&waiter,NULL,request_thread,&next)==0);next_token=take_token(&p);
  pthread_mutex_lock(&p.drop_sync_mtx);assert(p.drop_sync_active==next_token);p.drop_sync_result=0;pthread_cond_broadcast(&p.drop_sync_cond);
  struct timespec stalled={0,170000000};nanosleep(&stalled,NULL);pthread_mutex_unlock(&p.drop_sync_mtx);
  pthread_join(waiter,NULL);assert(next.result==-1&&next.error==ETIMEDOUT);cases++;
  uint64_t junk=0;while(write(p.drop_sync_pipe[1],&junk,sizeof(junk))==sizeof(junk)){}assert(errno==EAGAIN);
  assert(ba_transport_pcm_drop_sync(&p)==-1&&errno==EAGAIN&&p.drop_sync_active==0);cases++;
  while(read(p.drop_sync_pipe[0],&junk,sizeof(junk))==sizeof(junk)){}assert(errno==EAGAIN);
  p.drop_sync_next=UINT64_MAX;assert(ba_transport_pcm_drop_sync(&p)==-1&&errno==EOVERFLOW);cases++;
  p.drop_sync_next=0;transport.codec=1;assert(!ba_transport_pcm_drop_sync_supported(&p));assert(ba_transport_pcm_drop_sync(&p)==-1&&errno==ENOTSUP);cases++;
  transport.codec=0;p.mode=0;assert(!ba_transport_pcm_drop_sync_supported(&p));cases++;p.mode=1;p.running=false;
  assert(ba_transport_pcm_drop_sync(&p)==-1&&errno==ESRCH);cases++;
  p.running=true;assert(pthread_create(&waiter,NULL,request_thread,&next)==0);take_token(&p);
  pthread_mutex_lock(&p.mutex);ba_transport_pcm_release(&p);pthread_mutex_unlock(&p.mutex);
  pthread_join(waiter,NULL);assert(next.result==-1&&next.error==ECANCELED&&p.fd==-1);cases++;
  finish(&p);
  initialize(&p);assert(ba_transport_pcm_drop_sync(&p)==-1&&errno==ETIMEDOUT); /* leave old token queued */
  assert(pthread_create(&waiter,NULL,request_thread,&next)==0);pending_token(&p);
  pthread_t encoder;assert(pthread_create(&encoder,NULL,encoder_thread,&p)==0);
  pthread_join(waiter,NULL);pthread_join(encoder,NULL);assert(next.result==0);cases++;
  finish(&p);
  initialize(&p);int controls[]={BA_TRANSPORT_PCM_SIGNAL_DROP,BA_TRANSPORT_PCM_SIGNAL_CLOSE,BA_TRANSPORT_PCM_SIGNAL_OPEN,BA_TRANSPORT_PCM_SIGNAL_DROP};
  assert(write(p.pipe[1],controls,sizeof(controls))==sizeof(controls));assert(pthread_create(&waiter,NULL,request_thread,&next)==0);pending_token(&p);
  assert(pthread_create(&encoder,NULL,encoder_retire_legacy,&p)==0);pthread_join(waiter,NULL);pthread_join(encoder,NULL);assert(next.result==0);cases++;
  finish(&p);
  initialize(&p);pthread_mutex_lock(&p.mutex);started=seconds();
  assert(pthread_create(&waiter,NULL,request_thread,&next)==0);pthread_join(waiter,NULL);
  assert(next.result==-1&&next.error==ETIMEDOUT&&seconds()-started<.2&&p.drop_sync_next==0);
  pthread_mutex_unlock(&p.mutex);cases++;
  struct timespec deadline;assert(clock_gettime(CLOCK_MONOTONIC,&deadline)==0);deadline.tv_nsec+=30000000;
  if(deadline.tv_nsec>=1000000000){deadline.tv_nsec-=1000000000;deadline.tv_sec++;}
  pthread_mutex_lock(&p.mutex);started=seconds();assert(io_pcm_drop_sync_flush(&p,1,p.fd,&deadline)==ETIMEDOUT);
  assert(seconds()-started<.1);pthread_mutex_unlock(&p.mutex);cases++;
  finish(&p);assert(write_count>=5);printf("%u actual request/poll/codec/controller cases passed; cancellation and delayed token fences hold\n",cases);return 0;
}

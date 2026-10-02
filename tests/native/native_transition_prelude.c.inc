#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <inttypes.h>
#include <stdio.h>
#include <time.h>
#include <errno.h>
#include <stdarg.h>
#include <pthread.h>
#include <sys/time.h>
#include <stdlib.h>
#include "shiri_source.h"
#ifdef __APPLE__
struct itimerspec { struct timespec it_interval,it_value; };
#endif
#ifndef TIMER_ABSTIME
#define TIMER_ABSTIME 1
#endif
#define TFD_TIMER_ABSTIME TIMER_ABSTIME
#ifndef FIXTURE_PORTABLE_TIMER
#define HAVE_TIMERFD 1
#endif
#define PLAY_PLAYING 1
#define PLAY_PAUSED 2
#define PLAY_STOPPED 3
#define DATA_KIND_PIPE 1
#define E_LOG 0
#define L_PLAYER 0
static char last_reason[512];
static void fixture_log(int level,int area,const char *fmt,...) {va_list ap;(void)level;(void)area;va_start(ap,fmt);vsnprintf(last_reason,sizeof(last_reason),fmt,ap);va_end(ap);}
#define DPRINTF fixture_log
static int pb_timer_fd, pb_timer, pb_timer_ev_storage, *pb_timer_ev=&pb_timer_ev_storage;
static int playback_event_active, timer_calls, timer_failure_call, clock_reads, clock_failure_read;
static int aborts, callback_token, pending_callbacks, command_completions, delayed_stops;
static int output_fail, start_fail, seal_fail, arm_fail, peek_status, peek_advances, starts, arms, flushes, stopped_sessions;
static pthread_mutex_t shiri_gate_lock=PTHREAD_MUTEX_INITIALIZER;
static struct shiri_pcm_owner shiri_owner;
static bool shiri_armed;
static uint64_t shiri_operation;
#define fixture_operation shiri_operation
static int fixture_state, player_state;
static struct timespec fixture_now, next_anchor, peek_returns_at, seen_timer;
static int seen_flags;
static bool pb_timer_native, pb_timer_native_anchored, pb_timer_native_waiting_for_pcm;
static uint64_t pb_timer_native_operation;
static struct timespec pb_timer_native_anchor,pb_timer_native_wait_until,player_tick_interval;
static struct { int data_kind;} pipe_source;
static struct {void *reading_now;struct timespec start_ts,pts; int read_deficit;} pb_session_storage;
struct fixture_pb_session {struct {int data_kind;} *reading_now;struct timespec start_ts,pts; int read_deficit;};
static struct fixture_pb_session pb_session;
static int timespec_cmp(struct timespec a,struct timespec b){return a.tv_sec==b.tv_sec?(a.tv_nsec>b.tv_nsec)-(a.tv_nsec<b.tv_nsec):(a.tv_sec>b.tv_sec)-(a.tv_sec<b.tv_sec);}
static int fixture_clock_gettime(int kind,struct timespec *out){assert(kind==CLOCK_MONOTONIC);clock_reads++;if(clock_reads==clock_failure_read)return -1;*out=fixture_now;return 0;}
#define clock_gettime fixture_clock_gettime
static int timerfd_settime(int fd,int flags,const struct itimerspec *tick,void *old){(void)fd;(void)old;timer_calls++;seen_timer=tick->it_value;seen_flags=flags;if(timer_calls==timer_failure_call)return -1;return 0;}
#define timer_settime fixture_timer_settime
static int fixture_timer_settime(int timer,int flags,const struct itimerspec *tick,void *old){return timerfd_settime(timer,flags,tick,old);}
static int event_add(void *event,void *timeout){(void)event;(void)timeout;playback_event_active=1;return 0;}
static int event_del(void *event){(void)event;playback_event_active=0;return 0;}
static int input_pipe_shiri_state(uint64_t *op){*op=fixture_operation;return shiri_armed?1:-1;}
static int input_peek_sync(struct timespec *anchor){*anchor=next_anchor;if(peek_advances)fixture_now=peek_returns_at;return peek_status;}
static int input_pipe_shiri_seal(uint64_t op){fixture_operation=op;fixture_state=-1;shiri_armed=false;return seal_fail?-1:0;}
static int input_pipe_shiri_arm(const struct shiri_pcm_owner *owner,uint64_t op){arms++;if(arm_fail||op!=fixture_operation||shiri_armed)return -1;fixture_state=1;shiri_armed=true;shiri_owner=*owner;return 0;}
static void input_flush(void *arg){(void)arg;}
static void outputs_resampling_reset(void){}
static void outputs_metadata_purge(void){}
static void outputs_stop_delayed_cancel(void){delayed_stops=0;}

#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define E_DBG 1
#define E_WARN 2
#define LISTENER_SPEAKER 1
#define LISTENER_VOLUME 2
#define OUTPUTS_MAX_CALLBACKS 64
struct event {void (*cb)(int,short,void*);void *arg;int active;};
static void *evbase_player;
static int deadline_create_fail,deadline_add_fail,deadline_events;
static struct event *evtimer_new(void *base_,void (*cb)(int,short,void*),void *arg){(void)base_;if(deadline_create_fail)return NULL;struct event *e=calloc(1,sizeof(*e));assert(e);e->cb=cb;e->arg=arg;deadline_events++;return e;}
static int evtimer_add(struct event *e,struct timeval *deadline){assert(deadline->tv_sec==3&&deadline->tv_usec==0);if(deadline_add_fail)return -1;e->active=1;return 0;}
static void event_free(struct event *e){free(e);deadline_events--;}
static void event_active(void *ev,int what,int calls){(void)ev;(void)what;(void)calls;}
static void *outputs_deferredev;
enum output_device_state{OUTPUT_STATE_STOPPED=0,OUTPUT_STATE_STARTUP=1,OUTPUT_STATE_CONNECTED=2,OUTPUT_STATE_STREAMING=3,OUTPUT_STATE_FAILED=-1,OUTPUT_STATE_PASSWORD=-2};
struct output_device {uint64_t id;char *name;int type;char *type_name;enum output_device_state state;int selected,advertised,busy,prevent_playback;void *session,*stop_timer;struct output_device *next;};
typedef void (*output_status_cb)(struct output_device*,enum output_device_state);
static struct output_device devices[2],*outputs_device_list;
static int backend_flush[2],backend_start[2],backend_stops,delayed[2],network_callbacks;
static int device_index(struct output_device *d){return d==&devices[0]?0:1;}
static struct output_device *outputs_device_get(uint64_t id){for(struct output_device*d=outputs_device_list;d;d=d->next)if(d->id==id)return d;return NULL;}
static void outputs_device_remove(struct output_device*d){(void)d;}
static const char *player_pmap(void *cb){(void)cb;return "fixture";}
static int callback_add(struct output_device*,output_status_cb);
static void outputs_device_cb_set(struct output_device *d,output_status_cb cb){assert(callback_add(d,cb)>0);}
static int outputs_device_flush(struct output_device *d,output_status_cb cb){flushes++;assert(callback_add(d,cb)>0);return backend_flush[device_index(d)];}
static int outputs_device_start(struct output_device *d,output_status_cb cb,bool only_probe){(void)only_probe;starts++;assert(callback_add(d,cb)>0);int r=backend_start[device_index(d)];if(r>=0){d->session=d;d->state=r?OUTPUT_STATE_STARTUP:OUTPUT_STATE_CONNECTED;}return r;}
static int outputs_device_stop(struct output_device *d,output_status_cb cb){backend_stops++;assert(callback_add(d,cb)>0);d->state=OUTPUT_STATE_STARTUP;return 1;}
static int outputs_device_stop_delayed(struct output_device*d,output_status_cb cb){delayed[device_index(d)]=1;assert(callback_add(d,cb)>0);return 1;}
static void device_streaming_cb(struct output_device*d,enum output_device_state status){(void)d;(void)status;network_callbacks++;}
static void status_update(int state,int flags){(void)state;(void)flags;}
static void outputs_stop(output_status_cb cb){(void)cb;aborts++;callback_token=101;}
static void pb_session_stop(void){stopped_sessions++;playback_event_active=0;player_state=PLAY_STOPPED;pb_session.reading_now=NULL;}
static int clear_queue_on_stop_disabled=1;
static void db_queue_clear(int all){(void)all;}
enum command_state{COMMAND_END,COMMAND_PENDING};
typedef enum command_state (*command_function)(void*,int*);
struct command {command_function func,func_bh;void *arg;int pending,ret;};
struct commands_base {struct command *current_cmd;};
static struct commands_base command_base,*cmdbase=&command_base;
static struct command logical_cmd;
static void commands_exec_end(struct commands_base *b,int result){assert(b->current_cmd&&b->current_cmd->pending==1);b->current_cmd->ret=result;b->current_cmd=NULL;command_completions++;}

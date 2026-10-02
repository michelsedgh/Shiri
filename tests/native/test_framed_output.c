/* Actual new OwnTone output adapter with controlled player/socket boundary.
 * No device or network is opened. ASan/UBSan cover the unchanged full module. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/time.h>
#ifdef __linux__
#include <sys/random.h>
#else
/* Controlled Linux socket boundary tokens; no host syscall is performed. */
#define SOCK_CLOEXEC (1<<20)
#define SOCK_NONBLOCK (1<<21)
#define SO_PEERCRED 12345
#define GRND_NONBLOCK 1u
struct ucred { pid_t pid; uid_t uid; gid_t gid; };
#endif
#include "shiri_out_wire.h"

struct media_quality { int sample_rate,bits_per_sample,channels,bit_rate; };
enum output_device_state { OUTPUT_STATE_FAILED=-1,OUTPUT_STATE_STOPPED,OUTPUT_STATE_STARTUP,OUTPUT_STATE_CONNECTED,OUTPUT_STATE_STREAMING };
struct output_device { uint64_t id;char *name;int type;const char *type_name;struct media_quality quality;int supported_formats,offset_ms,volume;void *session; };
struct output_data { struct media_quality quality; uint8_t *buffer; size_t bufsize; int samples; };
struct output_buffer { struct timespec pts; struct output_data data[7]; bool conversion_error; };
struct output_definition {
 const char *name,*cfg_name; int type,priority;
 int (*init)(void);void(*deinit)(void);
 int (*device_start)(struct output_device*,int),(*device_stop)(struct output_device*,int),(*device_flush)(struct output_device*,int),(*device_probe)(struct output_device*,int),(*device_volume_set)(struct output_device*,int);
 void(*device_cb_set)(struct output_device*,int),(*write)(struct output_buffer*);
};
#define OUTPUT_TYPE_ALSA 6
#define MEDIA_FORMAT_PCM 1
#define EV_READ 1
#define EV_WRITE 2
#define EV_PERSIST 4
#define EV_TIMEOUT 8
#define E_LOG 1
#define L_LAUDIO 1
#define DPRINTF(...) ((void)0)
struct event { void(*callback)(int,short,void*);void *arg;uint64_t delay;int active; };
static unsigned event_count,subscriptions,open_fds;
static uint64_t test_now=UINT64_C(10000000000);
static struct output_device *test_device;
static struct {int token,state;} completion[4096];
static unsigned completions,sends;
static uint8_t packets[4096][SHIRI_OUT_PACKET_MAX],reply[SHIRI_OUT_PACKET_MAX];
static size_t packet_lengths[4096],reply_bytes;
static int send_mode,connect_fail,peer_bad,random_fail,event_fail,recv_closed;
static uint64_t recv_clock_advance;
static uint64_t buffer_ms=2250;
struct event_base *evbase_player;
typedef int cfg_t;
static cfg_t *cfg;
static long config_rate=48000,config_channels=2,config_format=SHIRI_OUT_S16LE,config_uid=963;
static const char *config_room="11111111-2222-4333-8444-555555555555",*config_launch="0123456789abcdef0123456789abcdef";
static const char *config_socket="/protected/final-pcm.sock";
static cfg_t *cfg_getsec(cfg_t*a,const char*b){(void)a;(void)b;return cfg;}
static long cfg_getint(cfg_t*a,const char*k){(void)a;if(!strcmp(k,"shiri_pcm_rate"))return config_rate;if(!strcmp(k,"shiri_pcm_channels"))return config_channels;if(!strcmp(k,"shiri_pcm_format"))return config_format;if(!strcmp(k,"shiri_pcm_peer_uid"))return config_uid;abort();}
static const char *cfg_getstr(cfg_t*a,const char*k){(void)a;if(!strcmp(k,"nickname"))return"Virtual local";if(!strcmp(k,"shiri_pcm_room_id"))return config_room;if(!strcmp(k,"shiri_pcm_launch_generation"))return config_launch;if(!strcmp(k,"shiri_pcm_socket"))return config_socket;abort();}
static struct event *event_new(void*base,int fd,short what,void(*cb)(int,short,void*),void*arg){(void)base;(void)fd;(void)what;if(event_fail)return NULL;struct event*e=calloc(1,sizeof(*e));assert(e);e->callback=cb;e->arg=arg;event_count++;return e;}
#define evtimer_new(base,cb,arg) event_new(base,-1,0,cb,arg)
static int event_add(struct event*e,const struct timeval*tv){assert(e);e->active=1;if(tv)e->delay=(uint64_t)tv->tv_sec*1000000000+(uint64_t)tv->tv_usec*1000;return 0;}
#define evtimer_add event_add
static int event_del(struct event*e){assert(e);e->active=0;return 0;}
static void event_free(struct event*e){assert(e&&event_count);event_count--;free(e);}
static int fake_clock(clockid_t id,struct timespec*ts){assert(id==CLOCK_MONOTONIC);ts->tv_sec=test_now/1000000000;ts->tv_nsec=test_now%1000000000;return 0;}
static int fake_socket(int family,int type,int protocol){assert(family==AF_UNIX&&(type&SOCK_SEQPACKET)&&(type&SOCK_CLOEXEC)&&(type&SOCK_NONBLOCK)&&protocol==0);open_fds++;return 41;}
static int fake_connect(int fd,const struct sockaddr*sa,socklen_t bytes){assert(fd==41&&bytes==sizeof(struct sockaddr_un)&&sa->sa_family==AF_UNIX);if(connect_fail){errno=EACCES;return-1;}return 0;}
static int fake_getsockopt(int fd,int level,int option,void*dest,socklen_t*bytes){assert(fd==41&&level==SOL_SOCKET&&option==SO_PEERCRED);struct ucred peer={.pid=731,.uid=peer_bad?964:963,.gid=963};assert(*bytes>=sizeof(peer));memcpy(dest,&peer,sizeof(peer));*bytes=sizeof(peer);return 0;}
static ssize_t fake_getrandom(void*dest,size_t bytes,unsigned flags){assert(flags==GRND_NONBLOCK);if(random_fail){errno=EAGAIN;return-1;}memset(dest,0xa7,bytes);return (ssize_t)bytes;}
static int fake_close(int fd){assert(fd==41&&open_fds);open_fds--;return 0;}
static ssize_t fake_send(int fd,const void*data,size_t bytes,int flags){assert(fd==41&&(flags&MSG_DONTWAIT)&&(flags&MSG_NOSIGNAL));if(send_mode==1){errno=EAGAIN;return-1;}if(send_mode==2)return (ssize_t)bytes-1;if(send_mode==3){errno=EPIPE;return-1;}if(send_mode==4){errno=EINTR;return-1;}assert(sends<4096&&bytes<=SHIRI_OUT_PACKET_MAX);memcpy(packets[sends],data,bytes);packet_lengths[sends++]=bytes;return (ssize_t)bytes;}
static ssize_t fake_recv(int fd,void*dest,size_t capacity,int flags){assert(fd==41&&(flags&MSG_TRUNC));if(recv_closed)return 0;if(!reply_bytes){errno=EAGAIN;return-1;}size_t bytes=reply_bytes;memcpy(dest,reply,bytes<capacity?bytes:capacity);reply_bytes=0;test_now+=recv_clock_advance;return (ssize_t)bytes;}
#define clock_gettime fake_clock
#define socket fake_socket
#define connect fake_connect
#define getsockopt fake_getsockopt
#define getrandom fake_getrandom
#define close fake_close
#define send fake_send
#define recv fake_recv
static uint64_t outputs_buffer_duration_ms_get(void){return buffer_ms;}
static int outputs_quality_subscribe(struct media_quality*q){assert(q->sample_rate>0);subscriptions++;return 0;}
static void outputs_quality_unsubscribe(struct media_quality*q){(void)q;assert(subscriptions);subscriptions--;}
static void outputs_device_session_add(uint64_t id,void*s){assert(test_device&&id==test_device->id&&!test_device->session);test_device->session=s;}
static void outputs_device_session_remove(uint64_t id){assert(test_device&&id==test_device->id);test_device->session=NULL;}
static void outputs_cb(int token,uint64_t id,enum output_device_state state){assert(test_device&&id==test_device->id&&completions<4096);completion[completions].token=token;completion[completions++].state=state;}
static const char *outputs_name(int type){assert(type==OUTPUT_TYPE_ALSA);return "ALSA";}
static int player_device_add(struct output_device*d){assert(!test_device);test_device=d;return 0;}
static void outputs_device_free(struct output_device*d){free(d->name);free(d);}
static int quality_is_equal(const struct media_quality*a,const struct media_quality*b){return a->sample_rate==b->sample_rate&&a->channels==b->channels&&a->bits_per_sample==b->bits_per_sample;}
/* @ACTUAL_MODULE@ */

static struct shiri_out_header last_packet(void){struct shiri_out_header h;assert(sends);assert(!shiri_out_decode(&h,packets[sends-1],packet_lengths[sends-1]));return h;}
static void respond(struct shiri_out_header h){assert(!shiri_out_encode(reply,sizeof(reply),&h));reply_bytes=SHIRI_OUT_HEADER;shiri_io(41,EV_READ,test_device->session);}
static struct shiri_out_header acknowledgement(void){struct shiri_out_header h=last_packet();h.frames=h.op==SHIRI_OUT_START?0:h.op;h.op=h.op==SHIRI_OUT_START?SHIRI_OUT_READY:SHIRI_OUT_DROP_ACK;return h;}
static void reset(void){assert(!shiri_sessions&&!event_count&&!subscriptions&&!open_fds);if(test_device)outputs_device_free(test_device);test_device=NULL;sends=completions=0;reply_bytes=0;send_mode=connect_fail=peer_bad=random_fail=event_fail=recv_closed=0;recv_clock_advance=0;buffer_ms=2250;test_now=UINT64_C(10000000000);config_rate=48000;config_channels=2;config_format=SHIRI_OUT_S16LE;config_uid=963;config_socket="/protected/final-pcm.sock";config_room="11111111-2222-4333-8444-555555555555";config_launch="0123456789abcdef0123456789abcdef";assert(!shiri_init());test_device->volume=100;}
static struct shiri_session *ready(void){assert(shiri_device_start(test_device,11)==1);assert(!completions);respond(acknowledgement());assert(test_device->session&&completion[0].state==OUTPUT_STATE_CONNECTED&&completion[0].token==11);return test_device->session;}
static void terminate(void){if(test_device&&test_device->session){assert(shiri_device_stop(test_device,99)==1);respond(acknowledgement());}assert(!shiri_sessions&&!event_count&&!subscriptions&&!open_fds);}
static void native_music(unsigned raw_frames,unsigned converted_frames,uint64_t final_pts){
 static uint8_t raw[16384],samples[16384];struct output_buffer obuf={0};struct shiri_session*s=test_device->session;assert(s);
 size_t bytes=(size_t)converted_frames*shiri_out_source_width(s->identity.format)*s->identity.channels;assert(bytes<=sizeof(samples)&&raw_frames*4<=sizeof(raw));
 for(size_t i=0;i<bytes;i++)samples[i]=(i%2==0)?0:0x40;
 uint64_t input=final_pts-s->delay_ns;obuf.pts.tv_sec=input/1000000000;obuf.pts.tv_nsec=input%1000000000;
 obuf.data[0].quality=(struct media_quality){.sample_rate=48000,.bits_per_sample=16,.channels=2};
 obuf.data[0].buffer=raw;obuf.data[0].bufsize=raw_frames*4;obuf.data[0].samples=raw_frames;
 if(quality_is_equal(&obuf.data[0].quality,&shiri_quality)){
  assert(converted_frames==raw_frames);memcpy(raw,samples,bytes);
 }else if(converted_frames){obuf.data[1].quality=shiri_quality;obuf.data[1].buffer=samples;obuf.data[1].bufsize=bytes;obuf.data[1].samples=converted_frames;}
 shiri_write(&obuf);if(bytes)assert(samples[1]==0x40);
}
static void music(unsigned frames,uint64_t final_pts){native_music(frames,frames,final_pts);}
static void test_gain_and_pacing(void){reset();struct shiri_session*s=ready();uint64_t target=test_now+100000000;music(480,target);assert(sends==1&&s->queued==1&&s->timer->delay==100000000);test_device->volume=50;assert(shiri_device_volume(test_device,12)==1);test_now=target;shiri_io(-1,EV_TIMEOUT,s);assert(sends==2&&!s->queued);struct shiri_out_header h=last_packet();assert(h.op==SHIRI_OUT_DATA&&h.pts_ns==target&&h.frames==480&&h.first_frame==0&&h.sequence==1);assert(packets[1][SHIRI_OUT_HEADER]==0&&packets[1][SHIRI_OUT_HEADER+1]==8);terminate();}
static void test_drop(void){reset();struct shiri_session*s=ready();music(48,test_now+100000000);assert(s->queued==1);assert(shiri_device_flush(test_device,22)==1);assert(!s->queued&&s->pending&&s->frame_count==48); /* Reset only after exact Drop ACK. */
 assert(sends==2&&last_packet().op==SHIRI_OUT_FLUSH&&last_packet().generation==2);unsigned count=completions;test_device->volume=30;assert(shiri_device_volume(test_device,23)==-1&&s->volume==100);shiri_device_cb_set(test_device,88);assert(s->callback_id==22);assert(completions==count);respond(acknowledgement());assert(completions==count+1&&completion[count].token==22&&!s->pending&&s->identity.generation==2);music(48,test_now+100000000);test_now+=100000000;shiri_io(-1,EV_TIMEOUT,s);struct shiri_out_header h=last_packet();assert(h.op==SHIRI_OUT_DATA&&h.first_frame==0&&h.sequence==2&&h.generation==2);terminate();}
static void test_invalid_ack(void){for(unsigned field=0;field<9;field++){reset();ready();assert(shiri_device_flush(test_device,31)==1);struct shiri_out_header h=acknowledgement();switch(field){case 0:h.room[0]^=1;break;case 1:h.launch[0]^=1;break;case 2:h.stream[0]^=1;break;case 3:h.generation++;break;case 4:h.sequence++;break;case 5:h.rate++;break;case 6:h.format=SHIRI_OUT_S32LE;break;case 7:h.channels=1;break;case 8:h.frames=SHIRI_OUT_END;break;}respond(h);assert(!test_device->session&&completion[completions-1].state==OUTPUT_STATE_FAILED&&completion[completions-1].token==31);}}
static void test_backpressure(void){reset();struct shiri_session*s=ready();uint64_t target=test_now+1000000;music(48,target);send_mode=1;test_now=target;shiri_io(-1,EV_TIMEOUT,s);assert(s->queued==1&&s->sequence==0&&s->timer->delay==SHIRI_STALL_NS);test_device->volume=50;assert(shiri_device_volume(test_device,41)==1);send_mode=0;shiri_io(41,EV_WRITE,s);assert(s->queued==0&&s->sequence==1&&packets[1][SHIRI_OUT_HEADER+1]==8);terminate();reset();s=ready();music(48,test_now+1000000);send_mode=1;test_now+=1000000;shiri_io(-1,EV_TIMEOUT,s);test_now+=SHIRI_STALL_NS;shiri_io(-1,EV_TIMEOUT,s);assert(!test_device->session&&completion[completions-1].state==OUTPUT_STATE_FAILED);}
static void test_deadlines(void){reset();assert(shiri_device_start(test_device,50)==1);test_now+=SHIRI_ACK_NS;shiri_io(-1,EV_TIMEOUT,test_device->session);assert(!test_device->session&&completion[completions-1].token==50);reset();ready();assert(shiri_device_flush(test_device,51)==1);test_now+=SHIRI_ACK_NS;shiri_io(-1,EV_TIMEOUT,test_device->session);assert(!test_device->session&&completion[completions-1].token==51);for(int mode=2;mode<=4;mode++){reset();struct shiri_session*s=ready();music(48,test_now+1000000);send_mode=mode;test_now+=1000000;shiri_io(-1,EV_TIMEOUT,s);assert(!test_device->session);}}
static void test_read_before_overdue_timer(void){
 for(unsigned operation=0;operation<5;operation++)for(unsigned lateness=0;lateness<3;lateness++){
  reset();int token=70+(int)operation;
  if(operation==0)assert(shiri_device_start(test_device,token)==1);
  else if(operation==1){ready();assert(shiri_device_flush(test_device,token)==1);}
  else if(operation==2){ready();assert(shiri_device_stop(test_device,token)==1);}
  else {assert(shiri_device_probe(test_device,token)==1);if(operation==4)respond(acknowledgement());}
  struct shiri_session*s=test_device->session;assert(s&&s->pending);
  struct shiri_out_header ack=acknowledgement();unsigned before=completions;
  test_now=s->deadline+(lateness==1?1:0);
  if(lateness==2){test_now=s->deadline-1;recv_clock_advance=2;}
  /* Deliver EV_READ before EV_TIMEOUT, including crossing the deadline inside
   * recv. A stale-but-exact READY/Drop ACK must not arm/reset an output. */
  respond(ack);
  assert(!test_device->session&&!shiri_sessions&&!event_count&&!subscriptions&&!open_fds);
  assert(completions==before+1&&completion[before].token==token&&completion[before].state==OUTPUT_STATE_FAILED);
 }
}
static void test_exact_ack_before_deadline(void){
 for(unsigned operation=0;operation<5;operation++){
  reset();int token=80+(int)operation;
  if(operation==0)assert(shiri_device_start(test_device,token)==1);
  else if(operation==1){ready();assert(shiri_device_flush(test_device,token)==1);}
  else if(operation==2){ready();assert(shiri_device_stop(test_device,token)==1);}
  else {assert(shiri_device_probe(test_device,token)==1);if(operation==4)respond(acknowledgement());}
  struct shiri_session*s=test_device->session;assert(s&&s->pending);
  test_now=s->deadline-1;unsigned before=completions;respond(acknowledgement());
  if(operation==3){assert(completions==before&&test_device->session&&last_packet().op==SHIRI_OUT_END);respond(acknowledgement());}
  assert(completions==before+1&&completion[before].token==token);
  assert(completion[before].state==(operation>=2?OUTPUT_STATE_STOPPED:OUTPUT_STATE_CONNECTED));
  terminate();
 }
}
static void test_malformed_and_closed(void){for(unsigned failure=0;failure<6;failure++){reset();ready();assert(shiri_device_flush(test_device,55)==1);struct shiri_out_header h=acknowledgement();assert(!shiri_out_encode(reply,sizeof(reply),&h));reply_bytes=SHIRI_OUT_HEADER;if(failure==0)reply[0]^=1;if(failure==1)reply[9]=2;if(failure==2)reply[15]=111;if(failure==3)reply_bytes--;if(failure==4)reply_bytes++;if(failure==5)recv_closed=1;shiri_io(41,EV_READ,test_device->session);assert(!test_device->session&&completion[completions-1].token==55&&completion[completions-1].state==OUTPUT_STATE_FAILED);}}
static void test_resampled_timeline(void){reset();config_rate=44100;outputs_device_free(test_device);test_device=NULL;assert(!shiri_init());struct shiri_session*s=ready();uint64_t target=test_now+100000000;native_music(48,44,target);native_music(48,44,target+1000000);assert(s->queued==2);assert(s->head->pts==target&&s->head->next->pts==target+UINT64_C(44000000000)/44100);test_now=target+1000000;shiri_io(-1,EV_TIMEOUT,s);struct shiri_out_header h=last_packet();assert(h.first_frame==44&&h.pts_ns==target+UINT64_C(44000000000)/44100);terminate();}
static void test_converter_deficit_and_warmup(void){
 reset();config_rate=44100;outputs_device_free(test_device);test_device=NULL;assert(!shiri_init());
 struct shiri_session*s=ready();uint64_t origin=test_now+100000000;
 native_music(480,425,origin);assert(s->head->pts==origin&&s->native_frames==480&&s->frame_count==425);
 native_music(480,441,origin+10000000);assert(s->head->next->pts==origin+UINT64_C(425000000000)/44100);
 assert(s->frame_count==866&&s->native_frames==960);terminate();
 reset();config_rate=44100;outputs_device_free(test_device);test_device=NULL;assert(!shiri_init());s=ready();origin=test_now+100000000;
 for(unsigned i=0;i<18;i++)native_music(1,0,origin+(uint64_t)i*1000000000/48000);
 assert(!s->queued&&s->native_frames==18&&s->frame_count==0);
 native_music(1,1,origin+UINT64_C(18000000000)/48000);
 assert(s->head->pts==origin&&s->head->first_frame==0&&s->native_frames==19&&s->frame_count==1);terminate();
 reset();config_rate=44100;outputs_device_free(test_device);test_device=NULL;assert(!shiri_init());ready();origin=test_now+100000000;
 for(unsigned i=0;i<20;i++)native_music(480,0,origin+(uint64_t)i*10000000);
 assert(test_device->session);native_music(480,0,origin+200000000);assert(!test_device->session);
}
static void test_fresh_clock_anchors_and_cumulative_fence(void){
 for(int direction=-1;direction<=1;direction+=2){
  reset();config_rate=44100;outputs_device_free(test_device);test_device=NULL;assert(!shiri_init());
  struct shiri_session*s=ready();uint64_t origin=test_now+100000000,emitted=0;
  for(unsigned i=0;i<1000;i++){
   uint64_t raw_elapsed=(uint64_t)i*10000000;
   int64_t correction=(int64_t)(raw_elapsed/2000)*direction+((i&1)?1000000:-1000000);
   uint64_t native=origin+raw_elapsed+correction;
   uint64_t expected=native+emitted*UINT64_C(1000000000)/44100-raw_elapsed;
   native_music(480,i?441:425,native);assert(test_device->session&&s->queued==1&&s->head->pts==expected);
   test_now=expected;shiri_io(-1,EV_TIMEOUT,s);assert(!s->queued&&last_packet().pts_ns==expected);
   emitted+=i?441:425;
  }
  terminate();
 }
 /* Each3ms correction individually meets the native step bound. Its second
  * accumulation does not meet the original program deadline and must fail. */
 reset();ready();uint64_t origin=test_now+100000000;
 native_music(480,480,origin);native_music(480,480,origin+13000000);assert(test_device->session);
 native_music(480,480,origin+26000000);assert(!test_device->session);
 reset();struct shiri_session*s=ready();origin=test_now+100000000;native_music(480,480,origin);
 assert(shiri_device_flush(test_device,42)==1);respond(acknowledgement());assert(!s->native_origin_pts&&!s->stream_pts);
 native_music(480,480,origin+1000000000);assert(test_device->session&&s->head->first_frame==0);terminate();
}
static void test_conversion_failure_is_not_warmup(void){
 reset();config_rate=44100;outputs_device_free(test_device);test_device=NULL;assert(!shiri_init());ready();
 uint8_t raw[4]={0};struct output_buffer out={.conversion_error=true};
 out.pts=(struct timespec){.tv_sec=10,.tv_nsec=0};out.data[0]=(struct output_data){.quality={.sample_rate=48000,.bits_per_sample=16,.channels=2},.buffer=raw,.bufsize=4,.samples=1};
 /* Absent canonical output would be legitimate one-frame warmup unless its
  * producer reported a real conversion error. Refuse immediately, not200ms. */
 shiri_write(&out);assert(!test_device->session&&completion[completions-1].state==OUTPUT_STATE_FAILED&&sends==1);
}
static void test_start_admission(void){for(unsigned failure=0;failure<4;failure++){reset();if(failure==0)peer_bad=1;if(failure==1)connect_fail=1;if(failure==2)random_fail=1;if(failure==3)event_fail=1;assert(shiri_device_start(test_device,60)==-1);assert(!test_device->session&&!open_fds&&!subscriptions&&!event_count);}reset();assert(shiri_device_probe(test_device,61)==1);respond(acknowledgement());assert(last_packet().op==SHIRI_OUT_END&&test_device->session&&!completions);respond(acknowledgement());assert(!test_device->session&&completion[0].token==61&&completion[0].state==OUTPUT_STATE_STOPPED);}
static void test_limits_and_continuity(void){reset();ready();music(48,test_now+SHIRI_QUEUE_NS+1);assert(!test_device->session);reset();struct shiri_session*s=ready();uint64_t start=test_now+100000000;for(unsigned i=0;i<SHIRI_QUEUE_MAX;i++)music(48,start+(uint64_t)i*1000000);assert(s->queued==SHIRI_QUEUE_MAX);music(48,start+(uint64_t)SHIRI_QUEUE_MAX*1000000);assert(!test_device->session);reset();ready();music(48,test_now+100000000);music(48,test_now+105000000);assert(!test_device->session);}
static void test_packed24_source_and_final_gain(void){
 for(unsigned channels=1;channels<=2;channels++)for(unsigned format=SHIRI_OUT_S24LE;format<=SHIRI_OUT_S24_32LE;format+=0x100){
  reset();config_format=format;config_channels=channels;outputs_device_free(test_device);test_device=NULL;assert(!shiri_init());
  assert(shiri_quality.bits_per_sample==32);struct shiri_session*s=ready();uint64_t target=test_now+100000000;
  music(480,target);assert(s->queued==1&&s->head->raw_bytes==480*channels*4&&s->head->bytes==480*channels*shiri_out_width(format));
  test_device->volume=50;assert(shiri_device_volume(test_device,15)==1);test_now=target;shiri_io(-1,EV_TIMEOUT,s);
  assert(last_packet().payload_bytes==480*channels*shiri_out_width(format));terminate();
 }
 const int32_t values[]={INT32_MIN,INT32_MAX,-257,-256,-255,-1,0,1,255,256,257,123456789,-123456789};
 for(unsigned f=0;f<2;f++)for(int pct=0;pct<=100;pct++)for(unsigned i=0;i<sizeof(values)/sizeof(values[0]);i++){
  unsigned format=f?SHIRI_OUT_S24_32LE:SHIRI_OUT_S24LE,width=shiri_out_width(format);
  uint32_t value=(uint32_t)values[i];uint8_t raw[4],out[6]={0xa5,0xa5,0xa5,0xa5,0xa5,0xa5};
  for(unsigned byte=0;byte<4;byte++)raw[byte]=(uint8_t)(value>>(byte*8));
  assert(!shiri_out_gain(out+1,raw,width,format,pct)&&out[0]==0xa5&&out[width+1]==0xa5);
  int64_t scaled=(int64_t)values[i]*pct*pct*pct/1000000;
  int64_t expected=scaled<0?-((-scaled+255)/256):scaled/256;
  uint32_t actual=0;for(unsigned byte=0;byte<width;byte++)actual|=(uint32_t)out[byte+1]<<(byte*8);
  assert(actual==((uint32_t)(int32_t)expected&(width==3?UINT32_C(0xffffff):UINT32_MAX)));
  assert(shiri_out_gain(raw,raw,width,format,pct)==-1);
 }
 uint8_t src[4]={0},dst[4];assert(shiri_out_gain(dst,src,1,SHIRI_OUT_S24LE,100)==-1);
 assert(shiri_out_gain(dst,src,3,SHIRI_OUT_S24LE,100)==0);
}
static void test_wire_and_formats(void){struct shiri_out_header h={.op=SHIRI_OUT_DATA,.generation=1,.sequence=1,.pts_ns=UINT64_C(123456789012),.rate=48000,.format=SHIRI_OUT_S16LE,.channels=2,.frames=1,.payload_bytes=4,.first_frame=3},decoded;uint8_t wire[SHIRI_OUT_PACKET_MAX]={0},out[4],src[4]={0xff,0xff,0xff,0x7f};h.room[0]=1;h.launch[0]=2;h.stream[0]=3;assert(!shiri_out_encode(wire,sizeof(wire),&h));assert(!memcmp(wire,"SHRIOUT1",8)&&wire[9]==1&&wire[11]==2&&wire[15]==112&&wire[75]==0);assert(!shiri_out_decode(&decoded,wire,SHIRI_OUT_HEADER+4)&&decoded.pts_ns==h.pts_ns);for(size_t size=0;size<SHIRI_OUT_HEADER;size++)assert(shiri_out_decode(&decoded,wire,size)==-1);assert(shiri_out_decode(&decoded,wire,SHIRI_OUT_HEADER+3)==-1);wire[0]^=1;assert(shiri_out_decode(&decoded,wire,SHIRI_OUT_HEADER+4)==-1);assert(!shiri_out_gain(out,src,4,SHIRI_OUT_S24_32LE,100)&&out[0]==0xff&&out[1]==0xff&&out[2]==0x7f&&out[3]==0);src[0]=src[1]=src[2]=0;src[3]=0x80;assert(!shiri_out_gain(out,src,4,SHIRI_OUT_S24_32LE,50)&&out[0]==0&&out[1]==0&&out[2]==0xf0&&out[3]==0xff);printf("WIRE_VECTOR=");for(unsigned i=0;i<SHIRI_OUT_HEADER+4;i++)printf("%02x",wire[i]^(i==0?1:0));puts("");for(int pct=0;pct<=100;pct++){for(uint32_t v=0;v<65536;v++){uint8_t a[2]={(uint8_t)v,(uint8_t)(v>>8)},b[2];assert(!shiri_out_gain(b,a,2,SHIRI_OUT_S16LE,pct));int value=(v&32768)?(int)v-65536:(int)v;int expected=(int)((int64_t)value*pct*pct*pct/1000000);assert((uint16_t)(b[0]|b[1]<<8)==(uint16_t)expected);}}}
int main(int argc,char **argv){if(argc==2){size_t size=strlen(argv[1]);if(size%2||size/2>SHIRI_OUT_PACKET_MAX)return 2;uint8_t vector[SHIRI_OUT_PACKET_MAX];for(size_t i=0;i<size/2;i++){unsigned v;if(sscanf(argv[1]+2*i,"%2x",&v)!=1)return 2;vector[i]=(uint8_t)v;}struct shiri_out_header h;return shiri_out_decode(&h,vector,size/2)<0?2:0;}assert(argc==1);test_gain_and_pacing();test_drop();test_invalid_ack();test_backpressure();test_deadlines();test_read_before_overdue_timer();test_exact_ack_before_deadline();test_malformed_and_closed();test_resampled_timeline();test_converter_deficit_and_warmup();test_fresh_clock_anchors_and_cumulative_fence();test_conversion_failure_is_not_warmup();test_start_admission();test_limits_and_continuity();test_packed24_source_and_final_gain();test_wire_and_formats();terminate();if(test_device)outputs_device_free(test_device);puts("Actual framed-output adapter: pacing/gain, exact caps and generation ACK, Drop fencing, bounded EAGAIN/queue/deadlines, read-first late-ACK rejection, fresh500ppm/rawanchors+resamplerdeficit+boundedwarmup+cumulativefence, malformed reply and 6,619,136 gain cases passed");return 0;}

/* Actual Cast control parser/registry/STOP seams; no TLS socket or PCM writes. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <json.h>
static int json_add_fail;
static int fault_json_add(json_object *object,const char *key,json_object *value)
{if(json_add_fail){json_add_fail=0;return -1;}return json_object_object_add(object,key,value);}
#define json_object_object_add fault_json_add
#include "cast_json.h"
#undef json_object_object_add
#define DPRINTF(...) ((void)0)
#define CALLBACK_REGISTER_SIZE 32
#define USE_TRANSPORT_ID (1<<1)
#define USE_REQUEST_ID (1<<2)
#define USE_REQUEST_ID_ONLY (1<<3)
#define MAX_BUF 4096
#define CAST_CONFIG_MAX_VOLUME 11
#define REPLY_TIMEOUT 5
#define HAVE_JSON_C 1
#define GNUTLS_SHUT_RDWR 1
/* @ACTUAL_CAST_TYPES@ */
enum output_device_state {OUTPUT_STATE_FAILED,OUTPUT_STATE_PASSWORD,OUTPUT_STATE_STOPPED,
 OUTPUT_STATE_STARTUP,OUTPUT_STATE_CONNECTED,OUTPUT_STATE_STREAMING};
struct cast_session;
typedef void (*cast_reply_cb)(struct cast_session *,struct cast_msg_payload *);
/* @ACTUAL_CAST_ENTRY@ */
struct cast_session {uint64_t device_id;int callback_id;enum cast_state state,wanted_state;
 unsigned request_id,ssrc_id;struct cast_reply_entry callback_register[CALLBACK_REGISTER_SIZE];
 void *reply_timeout,*tls_session,*rtcp_ev;char *transport_id,*session_id,*devname;float volume;
 int udp_fd,server_fd;};
struct output_device {struct cast_session *session;int volume,max_volume;};
typedef struct {char *source_id,*namespace_,*destination_id,*payload_utf8;} Extensions__CoreApi__CastChannel__CastMessage;
#define EXTENSIONS__CORE_API__CAST_CHANNEL__CAST_MESSAGE__INIT {0}
static int ack_count,last_ack,last_state,wire_count,cleanup_count,shutdown_count,disconnect_count;
static int called_a,called_b,timer_fail,clock_fail,send_fail,packed_fail;
static int64_t now_ns=1000000000LL,last_wait_us;
static char wire[4096];
static struct cast_msg_basic cast_msg[PRESENTATION+2]={
 [GET_STATUS]={.type=GET_STATUS,.namespace="receiver",.payload="{'type':'GET_STATUS','requestId':%u}",.flags=USE_REQUEST_ID_ONLY},
 [RECEIVER_STATUS]={.type=RECEIVER_STATUS,.tag="RECEIVER_STATUS"},
 [LAUNCH_ERROR]={.type=LAUNCH_ERROR,.tag="LAUNCH_ERROR"},
 [STOP]={.type=STOP,.namespace="receiver",.payload="{'type':'STOP','sessionId':'%s','requestId':%u}",.flags=USE_REQUEST_ID},
 [SET_VOLUME]={.type=SET_VOLUME,.namespace="receiver",.payload="{'type':'SET_VOLUME','volume':%f,'requestId':%u}",.flags=USE_REQUEST_ID},
 [CLOSE]={.type=CLOSE,.tag="CLOSE",.namespace="receiver",.payload="{'type':'CLOSE'}"},
 [MEDIA_CLOSE]={.type=MEDIA_CLOSE,.namespace="receiver",.payload="{'type':'CLOSE'}"},
 [PONG]={.type=PONG,.namespace="heartbeat",.payload="{'type':'PONG'}"}
};
/* Parser scans until a .type==0 sentinel, so fill otherwise-unused rows too. */
static void init_table(void) {for(int i=1;i<=PRESENTATION;i++)if(!cast_msg[i].type)cast_msg[i].type=i;}
static void outputs_cb(int id,uint64_t device,enum output_device_state state)
{(void)device;ack_count++;last_ack=id;last_state=state;}
static int fake_clock_gettime(clockid_t id,struct timespec *p)
{assert(id==CLOCK_MONOTONIC);if(clock_fail)return -1;p->tv_sec=now_ns/1000000000LL;p->tv_nsec=now_ns%1000000000LL;return 0;}
#define clock_gettime fake_clock_gettime
static int event_add(void *e,const struct timeval *tv)
{(void)e;last_wait_us=tv->tv_sec*1000000LL+tv->tv_usec;return timer_fail?-1:0;}
static int evtimer_del(void *e) {(void)e;return timer_fail?-1:0;}
static int event_del(void *e) {(void)e;return 0;}
static void cast_disconnect(int fd) {(void)fd;disconnect_count++;}
static int gnutls_bye(void *tls,int how) {(void)tls;(void)how;return 0;}
static void squote_to_dquote(char *p) {while(*p){if(*p=='\'')*p='"';p++;}}
static uint32_t htobe32(uint32_t v) {return v;}
static size_t extensions__core_api__cast_channel__cast_message__get_packed_size(void *p)
{Extensions__CoreApi__CastChannel__CastMessage *m=p;snprintf(wire,sizeof(wire),"%s",m->payload_utf8);return packed_fail?4096:4;}
static size_t extensions__core_api__cast_channel__cast_message__pack(void *p,uint8_t *b) {(void)p;memset(b,0,4);return 4;}
static int gnutls_record_send(void *tls,const void *buf,size_t n) {(void)tls;(void)buf;wire_count++;return send_fail?-1:(int)n;}
static Extensions__CoreApi__CastChannel__CastMessage *extensions__core_api__cast_channel__cast_message__unpack(void *a,size_t n,const uint8_t *b)
{(void)a;assert(n<4096);static char text[4096];memcpy(text,b,n);text[n]=0;
 static Extensions__CoreApi__CastChannel__CastMessage m={"peer","receiver","sender",text};return &m;}
static void extensions__core_api__cast_channel__cast_message__free_unpacked(void *p,void *allocator) {(void)p;(void)allocator;}
static void cast_session_cleanup(struct cast_session *s) {(void)s;cleanup_count++;}
static void cast_session_shutdown(struct cast_session *,enum cast_state);
/* @ACTUAL_CAST_REGISTRY@ */
/* @ACTUAL_CAST_SEND_PARSE@ */
/* @ACTUAL_CAST_STATUS@ */
/* @ACTUAL_CAST_CALLBACKS@ */
/* @ACTUAL_CAST_SHUTDOWN@ */
/* @ACTUAL_CAST_PROCESS_TIMEOUT@ */
/* @ACTUAL_CAST_INTERFACE@ */
static struct cast_session fresh(void)
{struct cast_session s={.device_id=41,.callback_id=11,.state=CAST_STATE_STREAMING,
 .session_id="current",.devname="test",.udp_fd=5,.server_fd=6};return s;}
static void cb_a(struct cast_session *s,struct cast_msg_payload *p) {(void)s;(void)p;called_a++;}
static void cb_b(struct cast_session *s,struct cast_msg_payload *p) {(void)s;(void)p;called_b++;}
static void reentrant(struct cast_session *s,struct cast_msg_payload *p)
{(void)p;called_a++;for(int i=0;i<31;i++)assert(cast_msg_send(s,GET_STATUS,NULL)==0);
 assert(cast_msg_send(s,GET_STATUS,cb_b)==0);}
static void deliver(struct cast_session *s,const char *json) {cast_msg_process(s,(const uint8_t *)json,strlen(json));}
static void reset(void)
{ack_count=wire_count=cleanup_count=shutdown_count=disconnect_count=called_a=called_b=0;
 timer_fail=clock_fail=send_fail=packed_fail=json_add_fail=0;now_ns=1000000000LL;}
int main(void)
{
 init_table();unsigned cases=0;struct cast_session s=fresh();
 /* Old reply1 cannot run request33, even with a valid parsed receiver status. */
 reset();assert(cast_msg_send(&s,GET_STATUS,cb_a)==0);deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":1}");
 assert(called_a==1);for(int i=0;i<31;i++)cast_msg_send(&s,GET_STATUS,NULL);
 cast_msg_send(&s,GET_STATUS,cb_b);assert(s.request_id==33);
 deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":1}");assert(!called_b&&s.callback_register[1].request_id==33);
 deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":33}");assert(called_b==1);cases++;
 /* Full bounded registry rejects occupied slot instead of overwriting. */
 reset();s=fresh();for(int i=0;i<32;i++)assert(cast_msg_send(&s,GET_STATUS,cb_a)==0);
 assert(cast_msg_send(&s,GET_STATUS,cb_b)<0&&s.request_id==32&&s.callback_register[1].request_id==1);
 deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":1}");
 assert(cast_msg_send(&s,GET_STATUS,cb_b)==0&&s.request_id==33);cases++;
 /* Every request keeps its own deadline; later sends cannot extend older work. */
 reset();s=fresh();cast_msg_send(&s,GET_STATUS,cb_a);now_ns+=2000000000LL;cast_msg_send(&s,GET_STATUS,cb_b);
 assert(last_wait_us==3000000);now_ns+=3000000000LL;cast_reply_timeout_cb(0,0,&s);
 assert(called_a==1&&!called_b&&s.callback_register[2].completion==cb_b&&last_wait_us==2000000);
 now_ns+=2000000000LL;cast_reply_timeout_cb(0,0,&s);assert(called_b==1);cases++;
 /* Completion clears before calling: newly enqueued request33 survives timeout1. */
 reset();s=fresh();cast_msg_send(&s,GET_STATUS,reentrant);now_ns+=5000000000LL;cast_reply_timeout_cb(0,0,&s);
 assert(called_a==1&&s.request_id==33&&s.callback_register[1].completion==cb_b);cases++;
 /* Numbered unknown/old status cannot become an unsolicited app takeover. */
 reset();s=fresh();deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":71,\"status\":{\"applications\":[{\"sessionId\":\"retired\"}]}}");
 assert(!cleanup_count&&!ack_count&&s.state==CAST_STATE_STREAMING);cases++;
 /* A stale output operation cannot acknowledge or fail its successor. */
 reset();s=fresh();struct output_device d={&s,20,11};cast_device_volume_set(&d,101);s.callback_id=102;
 deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":1}");assert(!ack_count&&s.callback_id==102);
 cast_msg_send(&s,GET_STATUS,cb_a);s.callback_id=103;now_ns+=5000000000LL;cast_reply_timeout_cb(0,0,&s);
 assert(!called_a&&!ack_count&&s.callback_id==103);cases++;
 /* JSON integer identity is exact, never string/fraction/bool/negative/wrapped. */
 const char *bad[]={"\"1\"","1.5","true","null","0","-1","2147483648"};
 for(unsigned i=0;i<sizeof(bad)/sizeof(*bad);i++) {
  reset();s=fresh();cast_msg_send(&s,GET_STATUS,cb_a);char text[128];
  snprintf(text,sizeof(text),"{\"type\":\"RECEIVER_STATUS\",\"requestId\":%s}",bad[i]);deliver(&s,text);
  assert(!called_a&&s.callback_register[1].request_id==1);cases++;
 }
 /* Expired ID capacity is fail closed without unsigned request wrap. */
 reset();s=fresh();s.request_id=INT_MAX;assert(cast_msg_send(&s,GET_STATUS,cb_a)<0&&s.request_id==INT_MAX);cases++;
 /* Send/serialize/clock/timer faults never retain a new orphan callback. */
 for(int fault=0;fault<4;fault++) {
  reset();s=fresh();
  if (fault == 0) send_fail = 1;
  if (fault == 1) packed_fail = 1;
  if (fault == 2) clock_fail = 1;
  if (fault == 3) timer_fail = 1;
  assert(cast_msg_send(&s,GET_STATUS,cb_a)<0);assert(!s.callback_register[1].completion);cases++;
 }
 /* Remote STOP acknowledgment is mandatory, with every application inspected. */
 const char *replies[]={
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[{\"sessionId\":\"other\"}]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[{\"sessionId\":\"current\"}]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[{\"sessionId\":\"other\"},{\"sessionId\":\"current\"}]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[{}]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{}}",
  "{\"type\":\"LAUNCH_ERROR\",\"requestId\":1}"
 };
 for(unsigned i=0;i<sizeof(replies)/sizeof(*replies);i++) {
  reset();s=fresh();d.session=&s;assert(cast_device_flush(&d,301)==1);
  assert(wire_count==1&&!ack_count&&s.udp_fd==-1&&disconnect_count==1);
  assert(strstr(wire,"\"type\":\"STOP\"")&&strstr(wire,"\"sessionId\":\"current\""));
  deliver(&s,replies[i]);assert(ack_count==1&&last_ack==301&&cleanup_count==1);
  assert(last_state==(i<2?OUTPUT_STATE_STOPPED:OUTPUT_STATE_FAILED));cases++;
 }
 reset();s=fresh();d.session=&s;cast_device_flush(&d,302);now_ns+=5000000000LL;cast_reply_timeout_cb(0,0,&s);
 assert(last_ack==302&&last_state==OUTPUT_STATE_FAILED&&cleanup_count==1);cases++;
 /* Old volume reply and timeout cannot consume the real STOP request. */
 reset();s=fresh();d.session=&s;cast_device_volume_set(&d,400);cast_device_flush(&d,401);
 deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":1}");assert(!ack_count);
 deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":2,\"status\":{\"applications\":[]}}");
 assert(ack_count==1&&last_ack==401&&last_state==OUTPUT_STATE_STOPPED);cases++;
 /* A failed STOP send cancels the exact failed output once, never leaves UDP-off live state. */
 for(int fault=0;fault<3;fault++) {
  reset();s=fresh();d.session=&s;cast_device_volume_set(&d,600);
  if (fault == 0) send_fail = 1;
  if (fault == 1) packed_fail = 1;
  if (fault == 2) timer_fail = 1;
  assert(cast_device_flush(&d,601)==1);
  assert(ack_count==1&&last_ack==601&&last_state==OUTPUT_STATE_FAILED&&cleanup_count==1&&s.udp_fd==-1);cases++;
 }
 const char *ambiguous[]={
  "[]", "1", "null", "{\"type\":true,\"requestId\":1}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"seqNum\":1,\"status\":{\"applications\":[]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"requestId\":1,\"status\":{\"applications\":[]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"\\u0072equestId\":1,\"status\":{\"applications\":[]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[{\"sessionId\":\"current\"}]},\"status\":{\"applications\":[]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[{\"sessionId\":\"current\",\"sessionId\":\"other\"}]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[]}} garbage",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[],}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"unused\":NaN,\"status\":{\"applications\":[]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"unused\":Infinity,\"status\":{\"applications\":[]}}",
  "{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"unused\":-Infinity,\"status\":{\"applications\":[]}}"
 };
 for(unsigned i=0;i<sizeof(ambiguous)/sizeof(*ambiguous);i++) {
  reset();s=fresh();d.session=&s;cast_device_flush(&d,701);deliver(&s,ambiguous[i]);
  assert(!ack_count&&s.callback_register[1].completion);
  now_ns+=5000000000LL;cast_reply_timeout_cb(0,0,&s);
  assert(ack_count==1&&last_ack==701&&last_state==OUTPUT_STATE_FAILED&&cleanup_count==1);cases++;
 }
 /* Legitimate escaped object keys and trailing JSON whitespace remain valid. */
 reset();s=fresh();d.session=&s;cast_device_flush(&d,801);
 deliver(&s," {\"type\":\"RECEIVER_STATUS\",\"\\u0072equestId\":1,\"status\":{\"applications\":[]}} \n");
 assert(ack_count==1&&last_ack==801&&last_state==OUTPUT_STATE_STOPPED);cases++;
 /* Duplicate-key tracking allocation failure cannot weaken acceptance. */
 reset();s=fresh();d.session=&s;cast_device_flush(&d,901);json_add_fail=1;
 deliver(&s,"{\"type\":\"RECEIVER_STATUS\",\"requestId\":1,\"status\":{\"applications\":[]}}");
 assert(!ack_count&&s.callback_register[1].completion);cases++;
 printf("Cast actual parser/control seams: %u adversarial cases; strict receiver STOP and exact operation/deadline ownership\n",cases);
 return 0;
}

/* Exact removed Cast seam: asserts the original defect is reproduced. */

#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#define DPRINTF(...) ((void)0)
enum output_device_state { OUTPUT_STATE_FAILED, OUTPUT_STATE_PASSWORD, OUTPUT_STATE_STOPPED,
 OUTPUT_STATE_STARTUP, OUTPUT_STATE_CONNECTED, OUTPUT_STATE_STREAMING };
struct output_device { void *session; int volume, max_volume; };
static int ack_count, last_ack, last_state, cleanup_count;
static void outputs_cb(int id, uint64_t device_id, enum output_device_state state)
{ (void)device_id; ack_count++; last_ack=id; last_state=state; }

#define CALLBACK_REGISTER_SIZE 32
#define USE_TRANSPORT_ID (1<<1)
#define USE_REQUEST_ID (1<<2)
#define USE_REQUEST_ID_ONLY (1<<3)
#define MAX_BUF 2048
#define CAST_CONFIG_MAX_VOLUME 11
#define CAST_STATE_F_STARTUP         (1 << 13)
// The receiver app is ready
#define CAST_STATE_F_APP_READY       (1 << 14)
// Media is playing in the receiver app
#define CAST_STATE_F_STREAMING       (1 << 15)

// Beware, the order of this enum has meaning
enum cast_state
{
  // Something bad happened during a session
  CAST_STATE_FAILED          = 0,
  // No session allocated
  CAST_STATE_NONE            = 1,
  // Session allocated, but no connection
  CAST_STATE_DISCONNECTED    = CAST_STATE_F_STARTUP | 0x01,
  // TCP connect, TLS handshake, CONNECT and GET_STATUS request
  CAST_STATE_CONNECTED       = CAST_STATE_F_STARTUP | 0x02,
  // Receiver app has been launched
  CAST_STATE_APP_LAUNCHED    = CAST_STATE_F_STARTUP | 0x03,
  // CONNECT, GET_STATUS and OFFER made to receiver app
  CAST_STATE_APP_READY       = CAST_STATE_F_APP_READY,
  // Buffering packets (playback not started yet)
  CAST_STATE_BUFFERING       = CAST_STATE_F_APP_READY | 0x01,
  // Streaming (playback started)
  CAST_STATE_STREAMING       = CAST_STATE_F_APP_READY | CAST_STATE_F_STREAMING,
};

enum cast_msg_types
{
  UNKNOWN,
  PING,
  PONG,
  CONNECT,
  CLOSE,
  GET_STATUS,
  RECEIVER_STATUS,
  LAUNCH,
  LAUNCH_OLD,
  LAUNCH_ERROR,
  STOP,
  MEDIA_CONNECT,
  MEDIA_CLOSE,
  OFFER,
  ANSWER,
  MEDIA_GET_STATUS,
  MEDIA_STATUS,
  SET_VOLUME,
  PRESENTATION,
  GET_CAPABILITIES,
  CAPABILITIES_RESPONSE,
};

struct cast_msg_basic
{
  enum cast_msg_types type;
  char *tag;       // Used for looking up incoming message type
  char *namespace;
  char *payload;

  int flags;
};

struct cast_msg_payload
{
  enum cast_msg_types type;
  unsigned int request_id;
  const char *app_id;
  const char *session_id;
  const char *transport_id;
  const char *player_state;
  const char *result;
  unsigned int media_session_id;
  unsigned short udp_port;
};


struct cast_session;
typedef void (*cast_reply_cb)(struct cast_session *,struct cast_msg_payload *);
struct cast_session { uint64_t device_id; int callback_id; enum cast_state state,wanted_state;
 unsigned request_id,ssrc_id; cast_reply_cb callback_register[CALLBACK_REGISTER_SIZE];
 void *reply_timeout,*tls_session; char *transport_id,*session_id,*devname; float volume; };
typedef struct { char *source_id,*namespace_,*destination_id,*payload_utf8; } Extensions__CoreApi__CastChannel__CastMessage;
#define EXTENSIONS__CORE_API__CAST_CHANNEL__CAST_MESSAGE__INIT {0}
static int wire_count,shutdown_count,timer_adds,timer_deletes,called_a,called_b;
static struct timeval reply_timeout={2,0};
static struct cast_msg_payload next_payload;
static Extensions__CoreApi__CastChannel__CastMessage fake_reply={"peer","test","sender","test"};
static Extensions__CoreApi__CastChannel__CastMessage *extensions__core_api__cast_channel__cast_message__unpack(void *a,size_t n,const uint8_t *b)
{ (void)a;(void)n;(void)b;return &fake_reply; }
static void extensions__core_api__cast_channel__cast_message__free_unpacked(void *a,void *b) { (void)a;(void)b; }
static size_t extensions__core_api__cast_channel__cast_message__get_packed_size(void *a) { (void)a;return 4; }
static size_t extensions__core_api__cast_channel__cast_message__pack(void *a,uint8_t *b) { (void)a;memset(b,0,4);return 4; }
static int gnutls_record_send(void *a,const void *b,size_t n) { (void)a;(void)b;wire_count++;return (int)n; }
static void squote_to_dquote(char *a) { (void)a; }
static uint32_t htobe32(uint32_t a) { return a; } /* byte order irrelevant to mocked transport */
static int event_add(void *a,const struct timeval *b) { (void)a;(void)b;timer_adds++;return 0; }
static int evtimer_del(void *a) { (void)a;timer_deletes++;return 0; }
static void *cast_msg_parse(struct cast_msg_payload *p,const char *s) { (void)s;*p=next_payload;return &next_payload; }
static void cast_msg_parse_free(void *a) { (void)a; }
static void cast_session_shutdown(struct cast_session *s,enum cast_state state) { shutdown_count++;s->state=state; }
static struct cast_msg_basic cast_msg[PRESENTATION+1]={
 [GET_STATUS]={.type=GET_STATUS,.namespace="receiver",.payload="request:%u",.flags=USE_REQUEST_ID_ONLY},
 [SET_VOLUME]={.type=SET_VOLUME,.namespace="receiver",.payload="volume:%f request:%u",.flags=USE_REQUEST_ID},
 [PONG]={.type=PONG,.namespace="heartbeat",.payload="PONG"}
};
static void cb_a(struct cast_session *s,struct cast_msg_payload *p) { (void)s;(void)p;called_a++; }
static void cb_b(struct cast_session *s,struct cast_msg_payload *p) { (void)s;(void)p;called_b++; }
/* @ACTUAL_CAST_PREIMAGE@ */

static struct cast_session fresh(void) {
 struct cast_session s={.device_id=41,.callback_id=-1,.state=CAST_STATE_STREAMING,
 .session_id="current",.devname="test"};return s;
}
static void reentrant_cb(struct cast_session *s,struct cast_msg_payload *p) {
 (void)p;called_a++;
 /* A timeout handler can start further requests; the original clear occurs later. */
 s->request_id+=31;assert(cast_msg_send(s,GET_STATUS,cb_b)==0);
}
int main(void) {
 /* Real sender overwrites slot1 with request33; actual parser dispatch accepts request1. */
 struct cast_session s=fresh();wire_count=called_a=called_b=0;
 assert(cast_msg_send(&s,GET_STATUS,cb_a)==0 && s.request_id==1);
 for(int i=0;i<31;i++) assert(cast_msg_send(&s,GET_STATUS,NULL)==0);
 assert(cast_msg_send(&s,GET_STATUS,cb_b)==0 && s.request_id==33);
 assert(s.callback_register[1]==cb_b);
 next_payload=(struct cast_msg_payload){.type=RECEIVER_STATUS,.request_id=1};
 cast_msg_process(&s,(const uint8_t *)"x",1);
 assert(called_a==0 && called_b==1 && s.callback_register[1]==NULL);
 /* Global timeout chooses latest ID, leaving older pending callback indefinitely. */
 s=fresh();called_a=called_b=0;
 cast_msg_send(&s,GET_STATUS,cb_a);cast_msg_send(&s,GET_STATUS,cb_b);
 cast_reply_timeout_cb(0,0,&s);
 assert(called_a==0 && called_b==1 && s.callback_register[1]==cb_a && s.callback_register[2]==NULL);
 /* Timeout clear AFTER callback destroys a new same-slot request queued reentrantly. */
 s=fresh();called_a=called_b=0;
 cast_msg_send(&s,GET_STATUS,reentrant_cb);cast_reply_timeout_cb(0,0,&s);
 assert(called_a==1 && s.request_id==33 && s.callback_register[1]==NULL);
 /* An unmatched numbered old status also falls through and kills the current app. */
 s=fresh();shutdown_count=0;
 next_payload=(struct cast_msg_payload){.type=RECEIVER_STATUS,.request_id=71,.session_id="retired"};
 cast_msg_process(&s,(const uint8_t *)"x",1);
 assert(shutdown_count==1 && s.state==CAST_STATE_FAILED);
 /* Flush acknowledges immediately with zero remote commands. */
 s=fresh();struct output_device d={.session=&s,.volume=20,.max_volume=11};
 ack_count=wire_count=0;assert(cast_device_flush(&d,301)==1);
 assert(ack_count==1 && last_ack==301 && last_state==OUTPUT_STATE_CONNECTED && wire_count==0);
 /* Delayed old volume then reports the newer mutable callback token. */
 s=fresh();d.session=&s;ack_count=wire_count=0;
 assert(cast_device_volume_set(&d,401)==1);s.callback_id=402;
 next_payload=(struct cast_msg_payload){.type=RECEIVER_STATUS,.request_id=1};
 cast_msg_process(&s,(const uint8_t *)"x",1);
 assert(ack_count==1 && last_ack==402);
 /* STOP NULL or wrong reply type is treated as success by actual callback. */
 for(int kind=0;kind<2;kind++) {
  s=fresh();s.wanted_state=CAST_STATE_CONNECTED;ack_count=0;s.callback_id=501;
  next_payload=(struct cast_msg_payload){.type=LAUNCH_ERROR};
  cast_cb_stop(&s,kind==0?NULL:&next_payload);
  assert(ack_count==1 && last_ack==501 && last_state==OUTPUT_STATE_STARTUP);
 }
 puts("Cast actual preimage: request alias, orphan timeout, reentrant erase, stale status, false flush, stale volume and permissive STOP reproduced");
 return 0;
}

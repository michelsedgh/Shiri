/* Actual START_PLAYBACK sequence, DMAP helpers and placeholder payload. The
 * RTSP transport holds replies until the fixture explicitly acknowledges them.
 * No network or audio device is opened. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#define DPRINTF(...) ((void)0)
#define CHECK_NULL(domain,expression) assert((expression)!=NULL)
#define RTSP_OK 200
#define AIRPLAY_USE_AUTH_SETUP 1
#define AIRPLAY_MD_WANTS_TEXT 1
#define AIRPLAY_STATE_SETUP 2
#define AIRPLAY_STATE_CONNECTED 3
enum airplay_seq_type {AIRPLAY_SEQ_START,AIRPLAY_SEQ_START_PLAYBACK,AIRPLAY_SEQ_SEND_VOLUME,AIRPLAY_SEQ_SEND_TEXT,AIRPLAY_SEQ_SEND_PROGRESS,AIRPLAY_SEQ_SEND_ARTWORK,AIRPLAY_SEQ_FEEDBACK,AIRPLAY_SEQ_CONTINUE,AIRPLAY_SEQ_ABORT};
enum evrtsp_cmd_type {EVRTSP_REQ_POST,EVRTSP_REQ_SETUP,EVRTSP_REQ_RECORD,EVRTSP_REQ_SETPEERS,EVRTSP_REQ_SET_PARAMETER};
struct evbuffer {uint8_t bytes[256];size_t size;};
struct headers {char type[64],rtpinfo[64];};
struct evrtsp_connection {int placeholder;};
struct evrtsp_request {int response_code;const char *response_code_line;struct headers *output_headers;struct evbuffer *output_buffer;void (*callback)(struct evrtsp_request *,void *);void *arg;};
struct rtp_session {uint32_t pos;};
struct airplay_master_session {struct rtp_session *rtp_session;struct {uint32_t pos;struct {long tv_sec,tv_nsec;} ts;} cur_stamp;};
struct airplay_session {int callback_id,reqs_in_flight;struct evrtsp_connection *ctrl;const char *devname;int state;uint16_t wanted_metadata;struct airplay_master_session *master_session;char session_url[32];uint64_t device_id;bool shiri_startup_metadata_acked;uint32_t shiri_startup_metadata_rtp;};
struct output_metadata {int marker;};
struct output_device {uint64_t id;char *auth_key;bool requires_auth;};
static struct output_device saved_device;
static struct output_device *outputs_device_get(uint64_t id){assert(id==saved_device.id);return &saved_device;}
static struct output_metadata *airplay_cur_metadata;
static int cfg_value,framed=true;static int *cfg=&cfg_value;static const char *speech_socket="/private/speech.sock";
static bool fail_request_alloc,fail_request_headers,fail_rtp_header,fail_send,fail_body_add;
static int *cfg_getsec(int *value,const char *name){assert(value==cfg&&(!strcmp(name,"general")||!strcmp(name,"library")));return value;}
static const char *cfg_getstr(int *value,const char *name){assert(value==cfg&&!strcmp(name,"shiri_speech_socket"));return speech_socket;}
static int cfg_getbool(int *value,const char *name){assert(value==cfg&&!strcmp(name,"pipe_framed"));return framed;}
static size_t evbuffer_get_length(struct evbuffer *buffer){return buffer->size;}
static int evbuffer_add(struct evbuffer *buffer,const void *data,size_t bytes){if(fail_body_add){fail_body_add=false;return -1;}if(buffer->size+bytes>sizeof(buffer->bytes))return -1;memcpy(buffer->bytes+buffer->size,data,bytes);buffer->size+=bytes;return 0;}
static int evrtsp_add_header(struct headers *headers,const char *name,const char *value){if(fail_rtp_header&&!strcmp(name,"RTP-Info")){fail_rtp_header=false;return -1;}char *dest=!strcmp(name,"Content-Type")?headers->type:headers->rtpinfo;assert(strlen(value)<64);strcpy(dest,value);return 0;}
/* @ACTUAL_DMAP_HELPERS@ */
/* @ACTUAL_STARTUP_PAYLOAD@ */
/* @ACTUAL_SEQUENCE_TYPES@ */
static void sequence_start(enum airplay_seq_type,struct airplay_session *,void *,const char *);
static void sequence_continue(struct airplay_seq_ctx *);
static void sequence_continue_cb(struct evrtsp_request *,void *);
static unsigned connected_calls,failure_calls,metadata_requests,volume_requests,checks;
static struct evrtsp_request *pending[32];static unsigned sent,completed;
static void session_status(struct airplay_session *session){assert(session->state==AIRPLAY_STATE_CONNECTED);++connected_calls;}
/* @ACTUAL_SESSION_CONNECTED@ */
static void session_failure(struct airplay_session *session){++failure_calls;session->state=-1;}
/* @ACTUAL_START_FAILURE@ */
static int payload_make_auth_setup(struct evrtsp_request *req,struct airplay_session *session,void *arg){(void)req;(void)session;(void)arg;return 0;}
static int payload_make_setup_session(struct evrtsp_request *req,struct airplay_session *session,void *arg){return payload_make_auth_setup(req,session,arg);}
static int payload_make_record(struct evrtsp_request *req,struct airplay_session *session,void *arg){return payload_make_auth_setup(req,session,arg);}
static int payload_make_setpeers(struct evrtsp_request *req,struct airplay_session *session,void *arg){return payload_make_auth_setup(req,session,arg);}
static int payload_make_setup_stream(struct evrtsp_request *req,struct airplay_session *session,void *arg){return payload_make_auth_setup(req,session,arg);}
static int payload_make_set_volume(struct evrtsp_request *req,struct airplay_session *session,void *arg){volume_requests++;return payload_make_auth_setup(req,session,arg);}
static enum airplay_seq_type response_handler_setup_session(struct evrtsp_request *req,struct airplay_session *session){(void)req;(void)session;return AIRPLAY_SEQ_CONTINUE;}
static enum airplay_seq_type response_handler_record(struct evrtsp_request *req,struct airplay_session *session){return response_handler_setup_session(req,session);}
static enum airplay_seq_type response_handler_setup_stream(struct evrtsp_request *req,struct airplay_session *session){return response_handler_setup_session(req,session);}
static enum airplay_seq_type response_handler_volume_start(struct evrtsp_request *req,struct airplay_session *session){return response_handler_setup_session(req,session);}
static enum airplay_seq_type response_handler_shiri_startup_metadata(struct evrtsp_request *,struct airplay_session *);
static struct airplay_seq_definition airplay_seq_definition[AIRPLAY_SEQ_CONTINUE]={
/* @ACTUAL_STARTUP_DEFINITION@ */
};
static struct airplay_seq_request airplay_seq_request[AIRPLAY_SEQ_CONTINUE][8]={
 [AIRPLAY_SEQ_START_PLAYBACK]={
/* @ACTUAL_STARTUP_REQUESTS@ */
 }
};
static void rtsp_close_cb(struct evrtsp_connection *connection,void *arg){(void)connection;(void)arg;}
static void evrtsp_connection_set_closecb(struct evrtsp_connection *connection,void (*callback)(struct evrtsp_connection *,void *),void *arg){(void)connection;(void)callback;(void)arg;}
static struct evrtsp_request *evrtsp_request_new(void (*callback)(struct evrtsp_request *,void *),void *arg){
 if(fail_request_alloc){fail_request_alloc=false;return NULL;}
 struct evrtsp_request *req=calloc(1,sizeof(*req));assert(req);req->callback=callback;req->arg=arg;req->output_headers=calloc(1,sizeof(struct headers));req->output_buffer=calloc(1,sizeof(struct evbuffer));assert(req->output_headers&&req->output_buffer);return req;
}
static void evrtsp_request_free(struct evrtsp_request *req){free(req->output_headers);free(req->output_buffer);free(req);}
static int request_headers_add(struct evrtsp_request *req,struct airplay_session *session,enum evrtsp_cmd_type type){(void)req;(void)session;(void)type;if(fail_request_headers){fail_request_headers=false;return -1;}return 0;}
static int evrtsp_make_request(struct evrtsp_connection *connection,struct evrtsp_request *req,enum evrtsp_cmd_type type,const char *uri){
 (void)connection;(void)type;(void)uri;if(fail_send){fail_send=false;return -1;}assert(sent<32);pending[sent++]=req;
 if(!strcmp(req->output_headers->type,"application/x-dmap-tagged"))metadata_requests++;
 return 0;
}
static void deferred_session_failure(struct airplay_session *session){session_failure(session);}
/* @ACTUAL_SEQUENCE_FUNCTIONS@ */
#define CHECK(value) do{assert(value);checks++;}while(0)
static void reply(int code,bool disconnect){
 CHECK(completed<sent);struct evrtsp_request *req=pending[completed++];req->response_code=code;req->response_code_line=code==200?"OK":"Rejected";
 req->callback(disconnect?NULL:req,req->arg);evrtsp_request_free(req);
}
static struct airplay_session fresh(uint32_t pos) {
 static struct rtp_session rtp;static struct airplay_master_session master;
 rtp.pos=pos;memset(&master,0,sizeof(master));master.rtp_session=&rtp;
 sent=completed=connected_calls=failure_calls=metadata_requests=volume_requests=0;
 framed=true;speech_socket="/private/speech.sock";airplay_cur_metadata=NULL;
 fail_request_alloc=fail_request_headers=fail_rtp_header=fail_send=fail_body_add=false;
 free(saved_device.auth_key);saved_device=(struct output_device){.id=42,.auth_key=strdup("reviewed-existing-pairing")};assert(saved_device.auth_key);
 return (struct airplay_session){.device_id=42,.callback_id=10,.devname="Sonos fixture",.state=AIRPLAY_STATE_SETUP,.wanted_metadata=AIRPLAY_MD_WANTS_TEXT,.master_session=&master};
}
static void before_metadata(struct airplay_session *session) {
 sequence_start(AIRPLAY_SEQ_START_PLAYBACK,session,NULL,"test startup");
 while(completed<sent && strcmp(pending[completed]->output_headers->type,"application/x-dmap-tagged"))reply(200,false);
 if(!metadata_requests) {
   fputs("Shiri startup acknowledged CONNECTED without initial DMAP metadata\n",stderr);
   assert(metadata_requests);
 }
 CHECK(!connected_calls && !volume_requests && !session->shiri_startup_metadata_acked && session->state==AIRPLAY_STATE_SETUP && sent==completed+1);
}
static uint32_t get32(const uint8_t *p){return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];}
static void inspect_body(struct airplay_session *session){
 struct evrtsp_request *req=pending[completed];const uint8_t *body=req->output_buffer->bytes;
 CHECK(req->output_buffer->size==71 && !memcmp(body,"mlit",4) && get32(body+4)==63);
 size_t offset=8;const char *tags[]={"mikd","minm","asar","asal","astn"};const unsigned lengths[]={1,5,5,10,2};
 for(unsigned i=0;i<5;i++){CHECK(!memcmp(body+offset,tags[i],4)&&get32(body+offset+4)==lengths[i]);offset+=8+lengths[i];}
 CHECK(offset==71 && body[16]==2 && body[69]==0 && body[70]==1);
 char expected[64];snprintf(expected,sizeof(expected),"rtptime=%" PRIu32,session->master_session->rtp_session->pos);CHECK(!strcmp(req->output_headers->rtpinfo,expected));
 CHECK(!airplay_cur_metadata && !session->master_session->cur_stamp.ts.tv_sec && !session->master_session->cur_stamp.pos);
}
static void positive(void){
 const uint32_t positions[]={0,88200,UINT32_MAX-2,UINT32_MAX};
 for(unsigned i=0;i<4;i++){
   struct airplay_session session=fresh(positions[i]);before_metadata(&session);inspect_body(&session);
   reply(200,false);CHECK(!connected_calls && volume_requests==1 && session.shiri_startup_metadata_acked && session.shiri_startup_metadata_rtp==positions[i] && completed+1==sent);
   reply(200,false);CHECK(connected_calls==1 && !failure_calls && session.state==AIRPLAY_STATE_CONNECTED && !session.reqs_in_flight && completed==sent);
 }
}
static void failures_and_fence(void){
 for(unsigned fail=0;fail<3;fail++){
   struct airplay_session session=fresh(88200);before_metadata(&session);
   if(fail==2)session.callback_id++;
   reply(fail==0?400:200,fail==1);
   CHECK(!connected_calls && !volume_requests && !session.reqs_in_flight && completed==sent);
   CHECK(failure_calls==(fail==2?0:1));
   if(!saved_device.auth_key || saved_device.requires_auth){
     fputs("Initial metadata rejection erased an already verified pairing key\n",stderr);
     assert(saved_device.auth_key && !saved_device.requires_auth);
   }
   CHECK(!strcmp(saved_device.auth_key,"reviewed-existing-pairing"));
 }
}
static void unchanged_music_and_nonshiri(void){
 struct output_metadata real={12345};
 for(unsigned skip=0;skip<5;skip++){
   struct airplay_session session=fresh(88200);
   if(skip==0)airplay_cur_metadata=&real;
   if(skip==1)speech_socket=NULL;
   if(skip==2)speech_socket="";
   if(skip==3)framed=false;
   if(skip==4)session.wanted_metadata=0;
   sequence_start(AIRPLAY_SEQ_START_PLAYBACK,&session,NULL,"ordinary startup");
   while(completed<sent)reply(200,false);
   CHECK(connected_calls==1 && !metadata_requests && volume_requests==1 && !failure_calls);
   CHECK(skip==0?airplay_cur_metadata==&real:!airplay_cur_metadata);CHECK(real.marker==12345);
 }
}
static void local_transport_failures(void) {
 for(unsigned fail=0;fail<6;fail++) {
   struct airplay_session session=fresh(88200);
   sequence_start(AIRPLAY_SEQ_START_PLAYBACK,&session,NULL,"local failure");
   while(((struct airplay_seq_ctx *)pending[completed]->arg)->cur_request->payload_make!=payload_make_setup_stream)
     reply(200,false);
   CHECK(!metadata_requests && !volume_requests && !connected_calls);
   if(fail==0)fail_request_alloc=true;
   if(fail==1)fail_request_headers=true;
   if(fail==2)fail_rtp_header=true;
   if(fail==3)fail_send=true;
   if(fail==4)fail_body_add=true;
   if(fail==5)session.master_session=NULL;
   reply(200,false);
   CHECK(failure_calls==1 && !metadata_requests && !volume_requests && !connected_calls &&
         !session.reqs_in_flight && completed==sent);
   CHECK(saved_device.auth_key && !saved_device.requires_auth &&
         !strcmp(saved_device.auth_key,"reviewed-existing-pairing"));
 }
 /* Successful metadata restores the existing final-volume error policy. */
 struct airplay_session session=fresh(88200);before_metadata(&session);
 reply(200,false);reply(200,true);
 CHECK(failure_calls==1 && !connected_calls && !saved_device.auth_key && saved_device.requires_auth);
}
int main(void){positive();failures_and_fence();unchanged_music_and_nonshiri();local_transport_failures();printf("Actual OwnTone START_PLAYBACK DMAP: %u checks; 71-byte transient metadata with exact uint32 RTP-Info ACK before final volume/CONNECTED; rejection, disconnect and stale operation fail closed; verified pairing and ordinary metadata preserved\n",checks);free(saved_device.auth_key);return 0;}

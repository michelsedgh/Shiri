/* Actual RAOP request ownership, with a controlled RTSP queue and no sockets. */
#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/queue.h>
#include <unistd.h>
#define DPRINTF(...) ((void)0)
#define RTSP_OK 200
#define EVRTSP_REQ_FLUSH 1
#define RAOP_OWNED_REQUEST_MAX 32
#define RAOP_STATE_F_STARTUP (1<<13)
#define RAOP_STATE_F_CONNECTED (1<<14)
#define RAOP_STATE_F_FAILED (1<<15)
enum raop_state {RAOP_STATE_STOPPED=0,RAOP_STATE_STARTUP=RAOP_STATE_F_STARTUP|1,
 RAOP_STATE_OPTIONS=RAOP_STATE_F_STARTUP|2,RAOP_STATE_ANNOUNCE=RAOP_STATE_F_STARTUP|3,
 RAOP_STATE_SETUP=RAOP_STATE_F_STARTUP|4,RAOP_STATE_RECORD=RAOP_STATE_F_STARTUP|5,
 RAOP_STATE_CONNECTED=RAOP_STATE_F_CONNECTED|1,RAOP_STATE_STREAMING=RAOP_STATE_F_CONNECTED|2,
 RAOP_STATE_TEARDOWN=RAOP_STATE_F_CONNECTED|3,RAOP_STATE_FAILED=RAOP_STATE_F_FAILED|1,
 RAOP_STATE_PASSWORD=RAOP_STATE_F_FAILED|2};
enum output_device_state {OUTPUT_STATE_FAILED,OUTPUT_STATE_PASSWORD,OUTPUT_STATE_STOPPED,
 OUTPUT_STATE_STARTUP,OUTPUT_STATE_CONNECTED,OUTPUT_STATE_STREAMING};
struct evrtsp_request;
typedef void (*evrtsp_req_cb)(struct evrtsp_request *,void *);
struct evkeyval {TAILQ_ENTRY(evkeyval) next;char *key,*value;};
TAILQ_HEAD(evkeyvalq,evkeyval);
struct evrtsp_request {int response_code;const char *response_code_line;evrtsp_req_cb cb;void *cb_arg;
 struct evkeyvalq *input_headers,*output_headers;};
struct evrtsp_connection {struct evrtsp_request *queue[64];unsigned count;};
struct rtp_session {uint16_t seqnum;uint32_t pos;};
struct raop_master_session {struct rtp_session *rtp_session;};
struct raop_owned_request;
struct raop_session {uint64_t device_id;int callback_id,reqs_in_flight;
 struct raop_owned_request *owned_requests;unsigned owned_request_count;enum raop_state state;
 struct evrtsp_connection *ctrl;struct raop_master_session *master_session;
 char *devname,*realm,*nonce,*session,*address;const char *session_url;void *deferredev;int server_fd,cseq;};
struct output_device {struct raop_session *session;int volume;};
static unsigned allocated_requests,freed_requests,close_callbacks,cleanup_count;
static int ack_count,last_ack,last_state,send_fail,allocation_fail;
static const char *response_cseq;
static int omit_cseq,duplicate_cseq;
static void *fault_calloc(size_t n,size_t size) {if(allocation_fail){allocation_fail=0;return NULL;}return calloc(n,size);}
#define calloc fault_calloc
static void outputs_cb(int id,uint64_t device,enum output_device_state state)
{(void)device;ack_count++;last_ack=id;last_state=state;}
static void evrtsp_connection_set_closecb(struct evrtsp_connection *c,void (*cb)(struct evrtsp_connection *,void *),void *a)
{(void)c;(void)a;if(cb)close_callbacks++;}
static struct evrtsp_request *evrtsp_request_new(evrtsp_req_cb cb,void *arg)
{struct evrtsp_request *r=calloc(1,sizeof(*r));if(r){r->cb=cb;r->cb_arg=arg;allocated_requests++;
 r->input_headers=calloc(1,sizeof(*r->input_headers));r->output_headers=calloc(1,sizeof(*r->output_headers));
 assert(r->input_headers&&r->output_headers);TAILQ_INIT(r->input_headers);TAILQ_INIT(r->output_headers);}return r;}
static void evrtsp_request_free(struct evrtsp_request *r)
{struct evkeyvalq *all[]={r->input_headers,r->output_headers};for(unsigned i=0;i<2;i++) {
 struct evkeyval *h;while((h=TAILQ_FIRST(all[i]))) {TAILQ_REMOVE(all[i],h,next);free(h->key);free(h->value);free(h);}free(all[i]);}
 freed_requests++;free(r);}
static void evrtsp_add_header(struct evkeyvalq *q,const char *key,const char *value)
{struct evkeyval *h=calloc(1,sizeof(*h));assert(h);h->key=strdup(key);h->value=strdup(value);TAILQ_INSERT_TAIL(q,h,next);}
static const char *evrtsp_find_header(const struct evkeyvalq *q,const char *key)
{struct evkeyval *h;TAILQ_FOREACH(h,q,next)if(!strcasecmp(h->key,key))return h->value;return NULL;}
static int raop_add_headers(struct raop_session *s,struct evrtsp_request *r,int type)
{(void)type;char text[32];snprintf(text,sizeof(text),"%d",s->cseq++);evrtsp_add_header(r->output_headers,"CSeq",text);return 0;}
static int evrtsp_make_request(struct evrtsp_connection *c,struct evrtsp_request *r,int type,const char *url)
{(void)type;(void)url;assert(c->count<64);c->queue[c->count++]=r;return send_fail?-1:0;}
static void evrtsp_connection_free(struct evrtsp_connection *c)
{for(unsigned i=0;i<c->count;i++)evrtsp_request_free(c->queue[i]);free(c);}
static void master_session_cleanup(struct raop_master_session *s) {(void)s;}
static void event_free(void *e) {(void)e;}
static void raop_rtsp_close_cb(struct evrtsp_connection *c,void *arg) {(void)c;(void)arg;}
static void session_cleanup(struct raop_session *s);
/* @ACTUAL_RAOP_OWNERSHIP@ */
/* @ACTUAL_RAOP_FREE@ */
static void session_cleanup(struct raop_session *s) {cleanup_count++;session_free(s);}
/* @UNCHANGED_RAOP_COMPLETIONS@ */
/* @ACTUAL_RAOP_FLUSH_COMPLETION@ */
/* @ACTUAL_RAOP_FLUSH_SEND@ */
static int raop_set_volume_internal(struct raop_session *s,int volume,evrtsp_req_cb cb)
{(void)volume;struct evrtsp_request *r=raop_request_new(s,cb);if(!r)return -1;
 int ret=evrtsp_make_request(s->ctrl,r,0,"volume");if(ret<0)return ret;s->reqs_in_flight++;return 0;}
/* @ACTUAL_RAOP_ADMISSION@ */
static struct rtp_session rtp={1,960};
static struct raop_master_session master={&rtp};
static struct raop_session *fresh(void)
{struct raop_session *s=calloc(1,sizeof(*s));assert(s);s->ctrl=calloc(1,sizeof(*s->ctrl));assert(s->ctrl);
 s->state=RAOP_STATE_STREAMING;s->callback_id=-1;s->device_id=9;s->devname=strdup("test");
 s->master_session=&master;s->session_url="rtsp:test";s->server_fd=-1;s->cseq=1;return s;}
static void complete(struct evrtsp_connection *c,unsigned index,int status)
{assert(index<c->count);struct evrtsp_request *r=c->queue[index];
 memmove(&c->queue[index],&c->queue[index+1],(--c->count-index)*sizeof(*c->queue));
 r->response_code=status;r->response_code_line="test";
 const char *expected=evrtsp_find_header(r->output_headers,"CSeq");
 if(expected&&!omit_cseq) {
  evrtsp_add_header(r->input_headers,"CSeq",response_cseq?response_cseq:expected);
  if(duplicate_cseq)evrtsp_add_header(r->input_headers,"CSeq",expected);
 }
 r->cb(status<0?NULL:r,r->cb_arg);/* Callback can free c/session; r was already detached. */
 evrtsp_request_free(r);}
static void queue_metadata(struct raop_session *s)
{struct evrtsp_request *r=raop_request_new(s,raop_cb_metadata);assert(r);
 assert(evrtsp_make_request(s->ctrl,r,0,"metadata")==0);s->reqs_in_flight++;}
static void reset(void) {ack_count=cleanup_count=close_callbacks=send_fail=allocation_fail=omit_cseq=duplicate_cseq=0;response_cseq=NULL;}
int main(void)
{
 unsigned cases=0;
 for(int old=-1;old<=500;old+=(old<0?201:300)) {
  reset();struct raop_session *s=fresh();struct output_device d={s,20};
  assert(raop_set_volume_one(&d,101)==1);assert(raop_device_flush(&d,102)==1);
  assert(s->owned_request_count==2);complete(s->ctrl,0,old);
  assert(!ack_count && !cleanup_count && s->callback_id==102 && s->reqs_in_flight==1);
  assert(s->owned_request_count==1);complete(s->ctrl,0,200);
  assert(ack_count==1 && last_ack==102 && last_state==OUTPUT_STATE_CONNECTED && !cleanup_count);
  assert(!s->owned_request_count && !s->reqs_in_flight);session_free(s);cases++;
 }
 for(int old=-1;old<=500;old+=(old<0?201:300)) {
  reset();struct raop_session *s=fresh();struct output_device d={s,20};
  raop_set_volume_one(&d,101);raop_device_flush(&d,102);complete(s->ctrl,1,200);
  assert(ack_count==1 && last_ack==102);s->callback_id=103;complete(s->ctrl,0,old);
  assert(ack_count==1 && !cleanup_count && s->callback_id==103);session_free(s);cases++;
 }
 /* Metadata timeout and startup callbacks are fenced through the same factory. */
 reset();struct raop_session *s=fresh();s->callback_id=200;queue_metadata(s);
 struct output_device d={s,20};raop_device_flush(&d,201);complete(s->ctrl,0,-1);
 assert(!ack_count&&!cleanup_count&&s->owned_request_count==1);complete(s->ctrl,0,200);session_free(s);cases++;
 /* Current failure frees the exact session and all queued owned contexts. */
 reset();s=fresh();d.session=s;raop_set_volume_one(&d,300);raop_device_flush(&d,301);
 complete(s->ctrl,1,-1);assert(cleanup_count==1 && last_ack==301 && last_state==OUTPUT_STATE_FAILED);cases++;
 /* Failed make_request retains request: teardown must cancel before releasing cb_arg. */
 reset();s=fresh();d.session=s;raop_set_volume_one(&d,400);send_fail=1;
 assert(raop_device_flush(&d,401)==1);assert(cleanup_count==1&&last_ack==401&&last_state==OUTPUT_STATE_FAILED);cases++;
 /* Factory capacity/allocation failures cannot leave an unbounded context list. */
 reset();s=fresh();for(unsigned i=0;i<RAOP_OWNED_REQUEST_MAX;i++)queue_metadata(s);
 assert(raop_request_new(s,raop_cb_metadata)==NULL && s->owned_request_count==RAOP_OWNED_REQUEST_MAX);
 session_free(s);cases++;
 reset();s=fresh();allocation_fail=1;assert(!raop_request_new(s,raop_cb_metadata));
 assert(!s->owned_request_count);session_free(s);cases++;
 /* Pre-enqueue failures release exactly their context as request_free promises. */
 reset();s=fresh();struct evrtsp_request *r=raop_request_new(s,raop_cb_metadata);assert(r);
 raop_request_free(r);assert(!s->owned_request_count);session_free(s);cases++;
 assert(allocated_requests==freed_requests);
 /* A duplicate old response can be assigned by evrtsp to the new FIFO head.
  * Exact outgoing FLUSH CSeq is mandatory; NULL/mismatch/duplicate fail closed. */
 for(int kind=0;kind<3;kind++) {
  reset();s=fresh();d.session=s;assert(raop_device_flush(&d,501)==1);
  if (kind == 0)
    response_cseq = "0";
  if (kind == 1)
    omit_cseq = 1;
  if (kind == 2)
    duplicate_cseq = 1;
  complete(s->ctrl,0,200);assert(cleanup_count==1&&last_ack==501&&last_state==OUTPUT_STATE_FAILED);cases++;
 }
 assert(allocated_requests==freed_requests);
 printf("RAOP actual request/session seams: %u adversarial cases, all request/context lifetimes balanced\n",cases);
 return 0;
}

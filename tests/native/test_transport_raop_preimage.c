/* Exact removed RAOP seam: asserts the original defect is reproduced. */

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
#define RAOP_STATE_F_STARTUP    (1 << 13)
// Streaming is up (connection established)
#define RAOP_STATE_F_CONNECTED  (1 << 14)
// Couldn't start device
#define RAOP_STATE_F_FAILED     (1 << 15)

enum raop_state {
  // Device is stopped (no session)
  RAOP_STATE_STOPPED   = 0,
  // Session startup
  RAOP_STATE_STARTUP   = RAOP_STATE_F_STARTUP | 0x01,
  RAOP_STATE_OPTIONS   = RAOP_STATE_F_STARTUP | 0x02,
  RAOP_STATE_ANNOUNCE  = RAOP_STATE_F_STARTUP | 0x03,
  RAOP_STATE_SETUP     = RAOP_STATE_F_STARTUP | 0x04,
  RAOP_STATE_RECORD    = RAOP_STATE_F_STARTUP | 0x05,
  // Session established
  // - streaming ready (RECORD sent and acked, connection established)
  // - commands (SET_PARAMETER) are possible
  RAOP_STATE_CONNECTED = RAOP_STATE_F_CONNECTED | 0x01,
  // Media data is being sent
  RAOP_STATE_STREAMING = RAOP_STATE_F_CONNECTED | 0x02,
  // Session teardown in progress (-> going to STOPPED state)
  RAOP_STATE_TEARDOWN  = RAOP_STATE_F_CONNECTED | 0x03,
  // Session is failed, couldn't startup or error occurred
  RAOP_STATE_FAILED    = RAOP_STATE_F_FAILED | 0x01,
  // Password issue: unknown password or bad password, or pending PIN from user
  RAOP_STATE_PASSWORD  = RAOP_STATE_F_FAILED | 0x02,
};


#define RTSP_OK 200
struct evrtsp_request { int response_code; const char *response_code_line; const char *outgoing_cseq,*received_cseq; };
typedef void (*evrtsp_req_cb)(struct evrtsp_request *,void *);
struct raop_session { uint64_t device_id; int callback_id,reqs_in_flight; enum raop_state state; void *ctrl; const char *devname; };
static void raop_rtsp_close_cb(void *a,void *b) { (void)a;(void)b; }
static void evrtsp_connection_set_closecb(void *a,void (*b)(void *,void *),void *c) { (void)a;(void)b;(void)c; }
static void session_cleanup(struct raop_session *s) { (void)s; cleanup_count++; }
static int wire_count;
static evrtsp_req_cb volume_cb,flush_cb;
static struct raop_session *volume_arg,*flush_arg;
static int raop_set_volume_internal(struct raop_session *s,int volume,evrtsp_req_cb cb)
{ (void)volume; wire_count++; volume_cb=cb; volume_arg=s; s->reqs_in_flight++; return 0; }
static int raop_send_req_flush(struct raop_session *s,evrtsp_req_cb cb,const char *caller)
{ (void)caller; wire_count++; flush_cb=cb; flush_arg=s; s->reqs_in_flight++; return 0; }
/* @ACTUAL_RAOP_PREIMAGE@ */

int main(void) {
 struct evrtsp_request good={.response_code=200,.response_code_line="OK"},bad={.response_code=500,.response_code_line="error"};
 for (int kind=0;kind<3;kind++) {
  struct raop_session s={.device_id=19,.callback_id=-1,.state=RAOP_STATE_STREAMING,.devname="test"};
  struct output_device d={.session=&s,.volume=20};
  ack_count=cleanup_count=wire_count=0;
  assert(raop_set_volume_one(&d,101)==1); assert(raop_device_flush(&d,102)==1);
  assert(wire_count==2 && s.callback_id==102 && s.reqs_in_flight==2);
  volume_cb(kind==0?&good:kind==1?&bad:NULL,volume_arg);
  assert(ack_count==1 && last_ack==102); /* Actual stale reply steals new FLUSH. */
  assert((kind==0 && last_state==OUTPUT_STATE_STREAMING && cleanup_count==0) ||
         (kind!=0 && last_state==OUTPUT_STATE_FAILED && cleanup_count==1));
  if(kind==0) { flush_cb(&good,flush_arg); assert(ack_count==2 && last_ack==-1); }
 }
 /* The reverse order also demonstrates stale errors destroying a live owner. */
 for(int kind=1;kind<3;kind++) {
  struct raop_session s={.device_id=19,.callback_id=-1,.state=RAOP_STATE_STREAMING,.devname="test"};
  struct output_device d={.session=&s,.volume=20};
  ack_count=cleanup_count=0; raop_set_volume_one(&d,201); raop_device_flush(&d,202);
  flush_cb(&good,flush_arg); assert(ack_count==1 && last_ack==202 && s.state==RAOP_STATE_CONNECTED);
  volume_cb(kind==1?&bad:NULL,volume_arg);
  assert(cleanup_count==1 && s.state==RAOP_STATE_FAILED && last_ack==-1);
 }
 /* The RTSP library associates this old reply with the new FIFO request.
  * Upstream's real raop_check_cseq no-op then acknowledges the wrong response. */
 struct raop_session current={.device_id=19,.callback_id=702,.reqs_in_flight=1,.state=RAOP_STATE_STREAMING};
 struct evrtsp_request wrong={.response_code=200,.response_code_line="OK",.outgoing_cseq="702",.received_cseq="701"};
 ack_count=cleanup_count=0;raop_cb_flush(&wrong,&current);
 assert(ack_count==1&&last_ack==702&&last_state==OUTPUT_STATE_CONNECTED&&!cleanup_count);
 puts("RAOP actual preimage: five stale completion orders and uncorrelated old-CSeq FLUSH ACK reproduced");
 return 0;
}

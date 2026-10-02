/* The actual RTSP sequence completion must retain its admitted operation. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>

#define DPRINTF(...) ((void)0)
#define CHECK_NULL(facility, expression) assert((expression)!=NULL)
#define RTSP_OK 200
enum airplay_seq_type {
  AIRPLAY_SEQ_SEND_VOLUME, AIRPLAY_SEQ_FLUSH, AIRPLAY_SEQ_SEND_TEXT,
  AIRPLAY_SEQ_SEND_PROGRESS, AIRPLAY_SEQ_SEND_ARTWORK, AIRPLAY_SEQ_FEEDBACK,
  AIRPLAY_SEQ_CONTINUE, AIRPLAY_SEQ_ABORT
};
struct evrtsp_request { int response_code; const char *response_code_line; };
struct airplay_session { int callback_id, reqs_in_flight; void *ctrl; const char *devname; int state; };
struct airplay_seq_request {
  const char *name;
  bool proceed_on_rtsp_not_ok;
  enum airplay_seq_type (*response_handler)(struct evrtsp_request *,struct airplay_session *);
};
struct definition {
  void (*on_success)(struct airplay_session *);
  void (*on_error)(struct airplay_session *);
};
static struct definition airplay_seq_definition[6];
static struct airplay_seq_request airplay_seq_request[6][2];
static int status_calls, failure_calls, flush_effects, received_token;
static void rtsp_close_cb(void *connection, void *arg) { (void)connection; (void)arg; }
static void evrtsp_connection_set_closecb(void *connection, void (*callback)(void *,void *), void *arg) {
  (void)connection; (void)callback; (void)arg;
}
static void session_status(struct airplay_session *session) {
  status_calls++; received_token=session->callback_id; session->callback_id=-1;
}
static void session_failure(struct airplay_session *session) { (void)session; failure_calls++; }
static enum airplay_seq_type flush_response(struct evrtsp_request *request, struct airplay_session *session) {
  (void)request; flush_effects++; session->state=1; return AIRPLAY_SEQ_CONTINUE;
}

/* @ACTUAL_AIRPLAY_SEQUENCE_CONTEXT@ */

static struct airplay_seq_ctx *pending[8];
static int queued;
static void sequence_continue(struct airplay_seq_ctx *context) {
  assert(queued<8);
  context->session->reqs_in_flight++;
  pending[queued++]=context;
}
static void sequence_start(enum airplay_seq_type, struct airplay_session *, void *, const char *);

/* @ACTUAL_AIRPLAY_SEQUENCE_CALLBACKS@ */

static void reset(void) {
  status_calls=failure_calls=flush_effects=queued=0;
  airplay_seq_definition[AIRPLAY_SEQ_SEND_VOLUME]=(struct definition){session_status,session_failure};
  airplay_seq_definition[AIRPLAY_SEQ_FLUSH]=(struct definition){session_status,session_failure};
  airplay_seq_request[AIRPLAY_SEQ_SEND_VOLUME][0]=(struct airplay_seq_request){"volume",false,NULL};
  airplay_seq_request[AIRPLAY_SEQ_FLUSH][0]=(struct airplay_seq_request){"flush",false,flush_response};
}
int main(void) {
  struct evrtsp_request response={200,"OK"}, negative={500,"Failure"};
  struct airplay_session session;
  int order, failure;
  for(order=0;order<2;order++) for(failure=0;failure<3;failure++) {
    reset(); session=(struct airplay_session){10,0,NULL,"test",0};
    sequence_start(AIRPLAY_SEQ_SEND_VOLUME,&session,NULL,"old volume");
    session.callback_id=11;
    sequence_start(AIRPLAY_SEQ_FLUSH,&session,NULL,"new flush");
    if(order) {
      sequence_continue_cb(&response,pending[1]);
      assert(status_calls==1 && received_token==11 && flush_effects==1);
    }
    sequence_continue_cb(failure==0?&response:failure==1?&negative:NULL,pending[0]);
    assert(!failure_calls);
    if(!order) {
      assert(!status_calls && !flush_effects && session.callback_id==11);
      sequence_continue_cb(&response,pending[1]);
    }
    assert(status_calls==1 && received_token==11 && flush_effects==1 && !session.reqs_in_flight);
  }
  reset(); session=(struct airplay_session){12,0,NULL,"test",0};
  sequence_start(AIRPLAY_SEQ_SEND_VOLUME,&session,NULL,"current volume");
  sequence_continue_cb(&negative,pending[0]);
  assert(failure_calls==1 && !status_calls && !session.reqs_in_flight);
  puts("Actual AirPlay RTSP sequence: old volume success/failure/disconnect cannot acknowledge or mutate a later flush in either reply order");
  return 0;
}

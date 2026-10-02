/* Exercise the complete pinned request-header function, without RTSP I/O. */
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned headers, auth_calls, method_calls, logs;
static char sequence[64];
#define E_LOG 0
#define L_RAOP 0
#define RAOP_STATE_PASSWORD 17
#define DPRINTF(...) (++logs)
enum evrtsp_cmd_type { EVRTSP_REQ_OPTIONS, EVRTSP_REQ_FLUSH };
struct raop_session { int cseq, state; const char *devname, *session_url, *session; uint64_t device_id; };
struct evrtsp_request { void *output_headers; };
static void *cfg;
static uint64_t libhash = 123;
static const char *evrtsp_method(enum evrtsp_cmd_type method)
{ (void)method; method_calls++; return "FLUSH"; }
static int evrtsp_add_header(void *unused, const char *key, const char *value)
{ (void)unused; headers++; if (!strcmp(key,"CSeq")) snprintf(sequence,sizeof(sequence),"%s",value); return 0; }
static void *cfg_getsec(void *unused, const char *name)
{ (void)unused; (void)name; return NULL; }
static const char *cfg_getstr(void *unused, const char *name)
{ (void)unused; (void)name; return "test"; }
static int raop_add_auth(struct raop_session *session, struct evrtsp_request *request, const char *method, const char *url)
{ (void)session; (void)request; (void)method; (void)url; auth_calls++; return 0; }

/* @ACTUAL_REQUEST_HEADERS@ */

int main(int argc, char **argv)
{
  struct raop_session session = {1,0,"target","rtsp://target",NULL,7};
  struct evrtsp_request request = {NULL};
#ifdef TEST_PREIMAGE
  if(argc==2 && !strcmp(argv[1],"--cseq-overflow"))
    { session.cseq=INT_MAX; raop_add_headers(&session,&request,EVRTSP_REQ_FLUSH); return 0; }
  puts("Actual RAOP header preimage compiled; boundary overflow checked separately");
#else
  (void)argc; (void)argv;
  for (int n=1;n<=8;n++)
    {
      unsigned before=headers;
      assert(raop_add_headers(&session,&request,EVRTSP_REQ_FLUSH)==0);
      assert(strtol(sequence,NULL,10)==n && session.cseq==n+1);
      assert(headers==before+6 && auth_calls==(unsigned)n && method_calls==(unsigned)n);
    }
  session.cseq=INT_MAX-1;
  assert(raop_add_headers(&session,&request,EVRTSP_REQ_FLUSH)==0);
  assert(strtol(sequence,NULL,10)==INT_MAX-1 && session.cseq==INT_MAX);
  const int rejected[]={INT_MAX,0,-1,INT_MIN};
  for(unsigned i=0;i<sizeof(rejected)/sizeof(*rejected);i++)
    for(unsigned retry=0;retry<3;retry++)
      {
        session.cseq=rejected[i];
        unsigned old_headers=headers,old_auth=auth_calls,old_methods=method_calls,old_logs=logs;
        char old_sequence[sizeof(sequence)]; memcpy(old_sequence,sequence,sizeof(sequence));
        assert(raop_add_headers(&session,&request,EVRTSP_REQ_FLUSH)<0);
        assert(session.cseq==rejected[i] && headers==old_headers && auth_calls==old_auth && method_calls==old_methods);
        assert(!memcmp(sequence,old_sequence,sizeof(sequence)) && logs==old_logs+1);
      }
  puts("Actual RAOP CSeq: 21 request/admission cases; exhaustion never increments, emits headers, authorizes, or reuses IDs");
#endif
  return 0;
}

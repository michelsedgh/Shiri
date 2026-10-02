/* Exact player getter and HTTP handler inserted by check_speech_jitter.py. */
#define main original_late_speech_main
#include "test_late_speech.c"
#undef main
#include "shiri_source.h"
#include <json-c/json.h>
#include <stdbool.h>

/* @ACTUAL_STATUS_TYPE@ */
enum command_state { COMMAND_END, COMMAND_PENDING };
static struct shiri_source_state shiri_source_state;
static void *cmdbase = (void *)(uintptr_t)1;
static unsigned sync_calls;
static int command_failure;
static int commands_exec_sync(void *base, enum command_state (*callback)(void *, int *),
                              void *bottom_half, void *arg)
{
  int retval = 99;
  CHECK(base == cmdbase && !bottom_half && arg);
  ++sync_calls;
  if (command_failure) return SHIRI_SOURCE_FAILURE;
  CHECK(callback(arg, &retval) == COMMAND_END);
  return retval;
}
/* @ACTUAL_PLAYER_FUNCTIONS@ */

struct response { char text[4096]; size_t length; };
struct httpd_request { struct response *out_body; };
#define HTTP_OK 200
#define HTTP_SERVUNAVAIL 503
#define L_WEB 1
#define CHECK_NULL(section, expression) do { (void)(section); CHECK((expression) != NULL); } while (0)
static int evbuffer_add(struct response *out, const void *bytes, size_t length)
{
  CHECK(length < sizeof(out->text) - out->length);
  memcpy(out->text + out->length, bytes, length); out->length += length;
  out->text[out->length] = 0;
  return 0;
}
/* @ACTUAL_HTTP_FUNCTIONS@ */

static json_object *field(json_object *obj, const char *key, enum json_type type)
{
  json_object *value = NULL;
  CHECK(json_object_object_get_ex(obj, key, &value));
  CHECK(json_object_get_type(value) == type);
  return value;
}

int main(void)
{
  struct player_shiri_speech_status status;
  struct response response = {{0}, 0}; struct httpd_request request = {&response};
  speech = fresh(); speech_fd = -1;
  speech.sequence = 12; speech.count = 500; speech.active = speech.running = 1;
  speech.underflow_events = 7; speech.underflow_frames = 960;
  speech.priming_frames = 960; speech.expired = 480; speech.admitted_frames = UINT64_MAX;
  shiri_source_state.initialized = true; shiri_source_state.faulted = false;
  memset(shiri_source_state.last.owner.incarnation, 0x34, 16);
  memset(shiri_source_state.last.owner.session, 0x56, 16);
  shiri_source_state.last.owner.epoch = 3; shiri_source_state.last.owner.generation = 4;
  shiri_source_state.last.operation_generation = 19;
  struct speech_state before_media = speech;
  struct shiri_source_state before_source = shiri_source_state;
  CHECK(player_shiri_speech_status(NULL) == SHIRI_SOURCE_INVALID && !sync_calls);
  CHECK(player_shiri_speech_status(&status) == 0 && sync_calls == 1);
  CHECK(status.media.observed_monotonic_ns > 0 && status.media.queue_frames == 500);
  CHECK(!memcmp(&status.source, &shiri_source_state.last, sizeof(status.source)));
  CHECK(status.source_initialized && !status.source_faulted && !status.media.configured);
  CHECK(!memcmp(&before_media, &speech, sizeof(speech)));
  CHECK(!memcmp(&before_source, &shiri_source_state, sizeof(shiri_source_state)));
  CHECK(jsonapi_reply_player_shiri_speech_status(&request) == HTTP_OK);
  CHECK(sync_calls == 2 && response.length < 3000);
  json_object *reply = json_tokener_parse(response.text); CHECK(reply);
  CHECK(json_object_get_int64(field(reply, "reserve_ns", json_type_int)) == 20000000);
  CHECK(json_object_get_int64(field(reply, "version", json_type_int)) == 1);
  CHECK(!strcmp(json_object_get_string(field(reply, "scope", json_type_string)),
                "room_launch_lifetime; current_source_at_read_only_snapshot"));
  CHECK(!strcmp(json_object_get_string(field(reply, "room_id", json_type_string)),
                "b67865437eb2443d83b165b984123a76"));
  CHECK(!strcmp(json_object_get_string(field(reply, "launch_generation", json_type_string)),
                "123456789abcdef0123456789abcdef0"));
  CHECK(!strcmp(json_object_get_string(field(reply, "incarnation", json_type_string)),
                "34343434343434343434343434343434"));
  CHECK(!strcmp(json_object_get_string(field(reply, "session_id", json_type_string)),
                "56565656565656565656565656565656"));
  CHECK(json_object_get_int64(field(reply, "epoch", json_type_int)) == 3);
  CHECK(json_object_get_int64(field(reply, "generation", json_type_int)) == 4);
  CHECK(json_object_get_int64(field(reply, "operation_generation", json_type_int)) == 19);
  CHECK(json_object_get_int64(field(reply, "queued_frames", json_type_int)) == 500);
  CHECK(json_object_get_int64(field(reply, "underflow_events", json_type_int)) == 7);
  CHECK(json_object_get_int64(field(reply, "underflow_frames", json_type_int)) == 960);
  CHECK(json_object_get_int64(field(reply, "expired_frames", json_type_int)) == 480);
  CHECK(json_object_get_int64(field(reply, "admitted_frames", json_type_int)) == INT64_MAX);
  CHECK(!json_object_get_boolean(field(reply, "configured", json_type_boolean)));
  json_object_put(reply);
  CHECK(!memcmp(&before_media, &speech, sizeof(speech)));
  CHECK(!memcmp(&before_source, &shiri_source_state, sizeof(shiri_source_state)));

  /* Snapshot the current operation, not a previous readiness reply. */
  shiri_source_state.last.operation_generation = 20;
  shiri_source_state.last.owner.generation = 5;
  shiri_source_state.faulted = true;
  memset(shiri_source_state.last.owner.session, 0, 16);
  response.length = 0;
  CHECK(jsonapi_reply_player_shiri_speech_status(&request) == HTTP_OK);
  reply = json_tokener_parse(response.text); CHECK(reply);
  CHECK(json_object_get_int64(field(reply, "operation_generation", json_type_int)) == 20);
  CHECK(json_object_get_int64(field(reply, "generation", json_type_int)) == 5);
  CHECK(json_object_get_boolean(field(reply, "source_faulted", json_type_boolean)));
  field(reply, "session_id", json_type_null);
  json_object_put(reply);
  command_failure = 1; response.length = 0;
  CHECK(jsonapi_reply_player_shiri_speech_status(&request) == HTTP_SERVUNAVAIL && !response.length);
  printf("status: %u actual getter/HTTP checks; current source identity,read-only,bounded,typed\n", checks);
  return 0;
}

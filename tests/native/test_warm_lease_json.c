#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "shiri_warm_json.h"
#include "outputs/cast_json.h"
static unsigned checks;
#define CHECK(v) do { assert(v); ++checks; } while (0)
static json_object *valid(const char *action)
{
  json_object *body=json_object_new_object();
  json_object_object_add(body,"room_id",json_object_new_string("01000000000000000000000000000000"));
  json_object_object_add(body,"launch_generation",json_object_new_string("02000000000000000000000000000000"));
  json_object_object_add(body,"warm_id",json_object_new_string("03000000000000000000000000000000"));
  json_object_object_add(body,"lease_generation",json_object_new_int(1));
  json_object_object_add(body,"deadline_monotonic_ns",json_object_new_int(!strcmp(action,"acquire") || !strcmp(action,"deadline") ? 100000 : 0));
  json_object_object_add(body,"action",json_object_new_string(action)); return body;
}
int main(void)
{
  const char *actions[]={"acquire","deadline","observe","release"}; struct shiri_warm_request request;
  for (unsigned i=0;i<4;++i) { json_object *body=valid(actions[i]); CHECK(shiri_warm_parse(body,&request)==0 && request.action==(enum shiri_warm_action)i); json_object_put(body); }
  for (unsigned kind=0;kind<13;++kind) {
    json_object *body=valid("acquire");
    if (kind==0) json_object_object_add(body,"room_id",json_object_new_string("00000000000000000000000000000000"));
    if (kind==1) json_object_object_add(body,"launch_generation",json_object_new_string("0200000000000000000000000000000A"));
    if (kind==2) json_object_object_add(body,"warm_id",json_object_new_string("bad"));
    if (kind==3) json_object_object_add(body,"lease_generation",json_object_new_boolean(1));
    if (kind==4) json_object_object_add(body,"lease_generation",json_object_new_int(0));
    if (kind==5) json_object_object_add(body,"lease_generation",json_object_new_int(-1));
    if (kind==6) json_object_object_add(body,"deadline_monotonic_ns",json_object_new_double(1.5));
    if (kind==7) json_object_object_add(body,"deadline_monotonic_ns",json_object_new_int(0));
    if (kind==8) json_object_object_add(body,"deadline_monotonic_ns",json_object_new_int(-1));
    if (kind==9) json_object_object_add(body,"action",json_object_new_string("begin"));
    if (kind==10) json_object_object_add(body,"action",json_object_new_string_len("acquire\0x",9));
    if (kind==11) json_object_object_add(body,"extra",json_object_new_int(1));
    if (kind==12) json_object_object_del(body,"warm_id");
    CHECK(shiri_warm_parse(body,&request)<0); json_object_put(body);
  }
  json_object *body=valid("release"); json_object_object_add(body,"deadline_monotonic_ns",json_object_new_int(1));
  CHECK(shiri_warm_parse(body,&request)<0); json_object_put(body);
  CHECK(cast_json_parse("{\"action\":\"acquire\",\"action\":\"release\"}",1024)==NULL);
  CHECK(cast_json_parse("{} trailing",1024)==NULL);
  printf("warm1 JSON: %u actual-parser checks; exact bounded schema, null/boolean/float/duplicate/NUL rejection\n",checks);
  return 0;
}

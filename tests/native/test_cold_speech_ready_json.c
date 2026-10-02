#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include "shiri_speech_ready_json.h"
int main(void) {
 const char *base="{\"incarnation\":\"11111111111111111111111111111111\",\"session_id\":null,\"epoch\":0,\"generation\":1,\"operation_generation\":1,\"room_id\":\"22222222222222222222222222222222\",\"launch_generation\":\"33333333333333333333333333333333\",\"speech_id\":\"44444444444444444444444444444444\",\"action\":\"prepare\"}";
 json_object *body=cast_json_parse(base,2048);struct shiri_speech_request q;unsigned n=0;
 assert(body && shiri_speech_ready_parse(body,&q)==0);n++;
 json_object_object_add(body,"epoch",json_object_new_boolean(1));assert(shiri_speech_ready_parse(body,&q)<0);n++;
 json_object_put(body);
 assert(!cast_json_parse("{\"epoch\":0,\"epoch\":1}",2048));n++;
 assert(!cast_json_parse("{\"epoch\":0,\"\\u0065poch\":1}",2048));n++;
 assert(!cast_json_parse("{\"epoch\":0}garbage",2048));n++;
 body=cast_json_parse(base,2048);json_object_object_add(body,"action",json_object_new_string_len("prepare\0evil",12));assert(shiri_speech_ready_parse(body,&q)<0);n++;json_object_put(body);
 body=cast_json_parse(base,2048);json_object_object_add(body,"session_id",json_object_new_string("88888888888888888888888888888888"));assert(shiri_speech_ready_parse(body,&q)<0);n++;
 json_object_object_add(body,"action",json_object_new_string("observe"));assert(shiri_speech_ready_parse(body,&q)==0);n++;json_object_put(body);
 printf("%u strict readiness JSON checks passed\n",n);return 0;
}

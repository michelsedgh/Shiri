#include <assert.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "shiri_source_json.h"

static int parse(const char *text) {
  struct shiri_source_request request;
  json_object *body=json_tokener_parse(text);
  int result=shiri_source_parse(body,&request);
  if(body) json_object_put(body);
  return result;
}
static void bad_field(const char *name, const char *value) {
  json_object *body=json_tokener_parse("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"session_id\":null,\"epoch\":0,\"generation\":1,\"operation_generation\":1}");
  json_object *replacement=json_tokener_parse(value);
  struct shiri_source_request request;
  json_object_object_add(body,name,replacement);
  assert(shiri_source_parse(body,&request)<0);
  json_object_put(body);
}
static int volume(const char *text) {
  struct shiri_source_request request;
  json_object *body=json_tokener_parse(text);
  int result=shiri_source_volume_parse(body,&request);
  if(body) json_object_put(body);
  return result;
}
static void volume_field(const char *name, const char *value, bool valid) {
  json_object *body=json_tokener_parse("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"session_id\":\"11111111111111111111111111111111\",\"epoch\":1,\"generation\":1,\"volume\":50}");
  struct shiri_source_request request;
  json_object_object_add(body,name,json_tokener_parse(value));
  assert((shiri_source_volume_parse(body,&request)==0)==valid);
  json_object_put(body);
}
int main(void) {
  const char *invalid_ints[]={"true","null","\"1\"","1.0","-1","9223372036854775808","18446744073709551615"};
  const char *invalid_ids[]={"null","0","true","\"\"","\"00000000000000000000000000000000\"","\"1234567890ABCDEF1234567890ABCDEF\"","\"1234567890abcdef1234567890abcdeg\"","\"1234567890abcdef1234567890abcde\\u0000\"","\"1234567890abcdef1234567890abcdef0\""};
  const char *integers[]={"epoch","generation","operation_generation"};
  const char *ids[]={"incarnation","session_id"};
  size_t i,j;
  assert(parse("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"session_id\":null,\"epoch\":0,\"generation\":1,\"operation_generation\":1}")==0);
  assert(parse("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"session_id\":\"11111111111111111111111111111111\",\"epoch\":9223372036854775807,\"generation\":9223372036854775807,\"operation_generation\":9223372036854775807}")==0);
  for(i=0;i<3;i++) for(j=0;j<sizeof(invalid_ints)/sizeof(invalid_ints[0]);j++) bad_field(integers[i],invalid_ints[j]);
  bad_field("generation","0"); bad_field("operation_generation","0");
  for(i=0;i<2;i++) for(j=0;j<sizeof(invalid_ids)/sizeof(invalid_ids[0]);j++) {
    if(i==1 && j==0) continue; /* Only session_id may be null. */
    bad_field(ids[i],invalid_ids[j]);
  }
  assert(parse("null")<0); assert(parse("[]")<0); assert(parse("{}")<0);
  assert(parse("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"session_id\":null,\"epoch\":0,\"generation\":1,\"operation_generation\":1,\"extra\":true}")<0);
  assert(parse("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"epoch\":0,\"generation\":1,\"operation_generation\":1}")<0);
  volume_field("volume","0",true); volume_field("volume","100",true);
  volume_field("volume","101",false); volume_field("generation","0",false);
  for(j=0;j<sizeof(invalid_ints)/sizeof(invalid_ints[0]);j++) volume_field("volume",invalid_ints[j],false);
  for(i=0;i<2;i++) for(j=0;j<sizeof(invalid_ids)/sizeof(invalid_ids[0]);j++) volume_field(ids[i],invalid_ids[j],false);
  assert(volume("null")<0); assert(volume("[]")<0); assert(volume("{}")<0);
  assert(volume("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"session_id\":null,\"epoch\":1,\"generation\":1,\"volume\":0}")<0);
  assert(volume("{\"incarnation\":\"1234567890abcdef1234567890abcdef\",\"session_id\":\"11111111111111111111111111111111\",\"epoch\":1,\"generation\":1,\"volume\":50,\"extra\":true}")<0);
  puts("Actual source and volume JSON parsers: typed identity, idle rejection, exact fields, gain range and signed63-bit boundaries passed");
  return 0;
}

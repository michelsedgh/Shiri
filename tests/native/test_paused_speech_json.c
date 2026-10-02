#include <assert.h>
#include <stdio.h>
#include "shiri_speech_ready_json.h"
int main(void){
 const char base[2049]="{\"incarnation\":\"11111111111111111111111111111111\",\"session_id\":null,\"epoch\":0,\"generation\":1,\"operation_generation\":1,\"room_id\":\"22222222222222222222222222222222\",\"launch_generation\":\"33333333333333333333333333333333\",\"speech_id\":\"44444444444444444444444444444444\",\"action\":\"prepare\"}";
 const char *actions[]={"prepare","ready","release","observe","begin","finish","cancel"};unsigned checks=0;
 for(unsigned i=0;i<7;i++)for(unsigned music=0;music<2;music++){
  struct shiri_speech_request q;json_object *body=cast_json_parse(base,2048);
  json_object_object_add(body,"action",json_object_new_string(actions[i]));
  if(music)json_object_object_add(body,"session_id",json_object_new_string("88888888888888888888888888888888"));
  int ret=shiri_speech_ready_parse(body,&q);
  if(ret && music && i==0){fputs("paused phone JSON prepare must be accepted\n",stderr);return 1;}
  assert(i==SHIRI_SPEECH_OBSERVE && !music?ret<0:ret==0);++checks;
  json_object_object_add(body,"extra",json_object_new_int(0));assert(shiri_speech_ready_parse(body,&q)<0);++checks;json_object_put(body);
 }
 printf("Actual strict nine-field speech JSON: %u checks; exact paused source may prepare/ready/release, idle observe remains invalid\n",checks);return 0;
}

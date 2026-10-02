#define main original_readiness_json_main
#include "test_cold_speech_ready_json.c"
#undef main
int main(void) {
  assert(original_readiness_json_main()==0);
  const char *base="{\"incarnation\":\"11111111111111111111111111111111\",\"session_id\":null,\"epoch\":0,\"generation\":1,\"operation_generation\":1,\"room_id\":\"22222222222222222222222222222222\",\"launch_generation\":\"33333333333333333333333333333333\",\"speech_id\":\"44444444444444444444444444444444\",\"action\":\"begin\"}";
  const char *actions[]={"begin","finish","cancel"};unsigned checks=0;
  for(unsigned i=0;i<3;++i)for(unsigned music=0;music<2;++music){
    struct shiri_speech_request q;json_object *body=cast_json_parse(base,2048);
    json_object_object_add(body,"action",json_object_new_string(actions[i]));
    if(music)json_object_object_add(body,"session_id",json_object_new_string("88888888888888888888888888888888"));
    assert(shiri_speech_ready_parse(body,&q)==0 && q.action==(enum shiri_speech_action)(SHIRI_SPEECH_BEGIN+i));++checks;
    json_object_object_add(body,"extra",json_object_new_int(0));assert(shiri_speech_ready_parse(body,&q)<0);++checks;
    json_object_put(body);
  }
  printf("owner1: %u strict nine-field owner JSON checks passed\n",checks);return 0;
}

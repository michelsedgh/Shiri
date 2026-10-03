/* Uses the actual new OwnTone schema and validator with real libconfuse. */
#include <confuse.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define DPRINTF(...) do { } while(0)
/* @ACTUAL_TIMING_SCHEMA@ */
static cfg_opt_t options[] = {
  /* @ACTUAL_TIMING_SECTION@ */
  CFG_END()
};
/* @ACTUAL_CONFIG_VALIDATOR@ */
int main(void) {
  const char *valid[]={"", "shiri_airplay_timing \"0\" { protocol = \"ntp\" }",
    "shiri_airplay_timing \"18446744073709551615\" { protocol = \"ptp\" }",
    "shiri_airplay_timing \"92539824408726\" { protocol = \"ntp\" } shiri_airplay_timing \"92539824408727\" { protocol = \"ptp\" }"};
  const char *invalid[]={
    "shiri_airplay_timing \"Living Room\" { protocol = \"ntp\" }",
    "shiri_airplay_timing \"01\" { protocol = \"ntp\" }",
    "shiri_airplay_timing \"+1\" { protocol = \"ntp\" }",
    "shiri_airplay_timing \"18446744073709551616\" { protocol = \"ntp\" }",
    "shiri_airplay_timing \"1\" { protocol = \"auto\" }",
    "shiri_airplay_timing \"1\" { protocol = \"NTP\" }",
    "shiri_airplay_timing \"1\" { }",
    "shiri_airplay_timing \"1\" { protocol = \"ntp\" } shiri_airplay_timing \"1\" { protocol = \"ptp\" }"};
  unsigned int checks=0;
  for(unsigned int i=0;i<sizeof(valid)/sizeof(valid[0]);i++) {
    cfg_t *config=cfg_init(options,CFGF_NONE);
    if(cfg_parse_buf(config,valid[i])!=CFG_SUCCESS || shiri_airplay_timing_validate(config)!=0) abort();
    cfg_free(config);checks++;
  }
  for(unsigned int i=0;i<sizeof(invalid)/sizeof(invalid[0]);i++) {
    cfg_t *config=cfg_init(options,CFGF_NONE);
    if(cfg_parse_buf(config,invalid[i])==CFG_SUCCESS && shiri_airplay_timing_validate(config)==0) abort();
    cfg_free(config);checks++;
  }
  printf("real libconfuse parsing passed: %u cases, including duplicate title rejection\n",checks);
  return 0;
}

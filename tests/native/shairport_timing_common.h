/* Minimal external config/format services; audio_shiri.c itself is unmodified. */
#ifndef TEST_COMMON_H
#define TEST_COMMON_H
#include <stdint.h>
#include <stdlib.h>
#define SPS_FORMAT_S16_LE 1u
#define SPS_RATE_48000 1u
#define RATE_FROM_ENCODED_FORMAT(x) (((x>>6)&0x7ffff)*2)
#define CHANNELS_FROM_ENCODED_FORMAT(x) ((x>>25)&0x7f)
#define FORMAT_FROM_ENCODED_FORMAT(x) (x&0x3f)
struct test_config { void *cfg; double audio_backend_buffer_desired_length, audio_backend_latency_offset; };
extern struct test_config config;
int config_lookup_non_empty_string(void*,const char*,const char**);
int config_lookup_int(void*,const char*,int*);
uint64_t get_absolute_time_in_ns(void);
void warn(const char*,...);
void die(const char*,...);
#endif

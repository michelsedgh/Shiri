/* Link the installed SBC codec: reset must discard its old encoder history. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sbc/sbc.h>
struct io_poll {int unused;};
struct ba_transport_pcm {int unused;};
static struct {int sbc_quality;} config;
static int completion,completion_error;
static unsigned sbc_a2dp_get_bitpool(const void *c,int quality){(void)c;(void)quality;return 32;}
static void io_poll_drop_sync_complete(struct io_poll *io,struct ba_transport_pcm *pcm,int error){(void)io;(void)pcm;completion++;completion_error=error;}
static void actual_codec_reset(sbc_t *encoder) {
#define sbc (*encoder)
  const uint8_t configuration_bytes[4]={0x12,0x15,2,53};
  const uint8_t (*configuration)[4]=&configuration_bytes;
  struct io_poll io={0};struct ba_transport_pcm selected={0},*t_pcm=&selected;
  /* @ACTUAL_SBC_RESET@ */
#undef sbc
}
static void initialize(sbc_t *s){const uint8_t cfg[4]={0x12,0x15,2,53};assert(sbc_init_a2dp(s,0,cfg,sizeof(cfg))==0);s->bitpool=32;s->endian=SBC_LE;}
static ssize_t encode(sbc_t *s,const int16_t *input,uint8_t *out){ssize_t written=0;assert(sbc_encode(s,input,sbc_get_codesize(s),out,1024,&written)==(ssize_t)sbc_get_codesize(s));return written;}
int main(void){sbc_t history,fresh,unreset;initialize(&history);initialize(&fresh);initialize(&unreset);
  int16_t signal[1024],silence[1024]={0};uint8_t encoded[1024],expected[1024],after[1024],before[1024];
  assert(sbc_get_codesize(&history)<=sizeof(signal));for(unsigned i=0;i<1024;i++)signal[i]=(i%8<4)?24000:-24000;
  for(unsigned i=0;i<8;i++){encode(&history,signal,encoded);encode(&unreset,signal,encoded);}
  ssize_t n=encode(&fresh,silence,expected),old_n=encode(&unreset,silence,before);
  assert(n==old_n&&memcmp(before,expected,(size_t)n)!=0); /* Real old history survives without reset. */
  actual_codec_reset(&history);assert(completion==1&&completion_error==0);
  assert(encode(&history,silence,after)==n&&memcmp(after,expected,(size_t)n)==0);
  sbc_finish(&history);sbc_finish(&fresh);sbc_finish(&unreset);
  puts("actual linked SBC reset discards encoder history before completion; unreset codec preimage differs");return 0;}

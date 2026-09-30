/* Independent pinned-backend regression; no ALSA device is opened. */
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "pcm_volume.h"

static void encode(uint8_t *dst, unsigned width, int64_t sample) {
  uint64_t value=(uint64_t)sample;
  for(unsigned i=0;i<width;i++) dst[i]=(uint8_t)(value>>(8*i));
}
static int64_t decode(const uint8_t *src,unsigned bits) {
  uint64_t value=0;
  for(unsigned i=0;i<bits/8;i++) value |= (uint64_t)src[i]<<(8*i);
  return value & (UINT64_C(1)<<(bits-1)) ? (int64_t)value-(INT64_C(1)<<bits) : (int64_t)value;
}
static void check(unsigned bits,int volume,int64_t sample) {
  uint8_t input[12], original[12], output[14];
  memset(input,0xA6,sizeof(input));
  memset(output,0x5A,sizeof(output));
  unsigned width=bits/8;
  encode(input+1,width,sample); /* Deliberately unaligned packed source. */
  memcpy(original,input,sizeof(input));
  assert(pcm_volume_scale_copy(output+1,input+1,width,bits,volume)==0);
  assert(memcmp(input,original,sizeof(input))==0);
  assert(output[0]==0x5A && output[1+width]==0x5A);
  int percentage=volume<0?0:volume>100?100:volume;
  int64_t actual=decode(output+1,bits);
  long double reference=truncl(sample*powl((long double)percentage/100.0L,3));
  assert(fabsl((long double)actual-reference)<=1.0L);
  if(percentage==0) assert(actual==0);
  if(percentage==50) assert(actual==sample/8);
  if(percentage==100) assert(actual==sample);
}
int main(void) {
  uint32_t random=0x823A61D7;
  unsigned long tested=0;
  for(int volume=0;volume<=100;volume++)
    for(int32_t sample=-32768;sample<=32767;sample++) {
      check(16,volume,sample);
      tested++;
    }
  for(unsigned bits=24;bits<=32;bits+=8) {
    int64_t min=-(INT64_C(1)<<(bits-1)), max=-min-1;
    int64_t edges[]={min,min+1,-max,-8192,-8,-1,0,1,8,8192,max-1,max};
    for(int volume=-1;volume<=101;volume++) {
      for(unsigned i=0;i<sizeof(edges)/sizeof(edges[0]);i++) {
        check(bits,volume,edges[i]);tested++;
      }
      for(unsigned i=0;i<1000;i++) {
        random^=random<<13;random^=random>>17;random^=random<<5;
        int64_t sample=(int64_t)(random & (uint32_t)((INT64_C(1)<<bits)-1));
        if(sample>max) sample-=INT64_C(1)<<bits;
        check(bits,volume,sample);tested++;
      }
    }
  }
  uint8_t data[32], backup[32], out[32];
  memset(data,0xC7,sizeof(data));memcpy(backup,data,sizeof(data));
  assert(pcm_volume_scale_copy(data,data,16,16,100)==-1);
  assert(pcm_volume_scale_copy(data+1,data,16,16,50)==-1);
  assert(pcm_volume_scale_copy(data,data+1,16,16,50)==-1);
  assert(memcmp(data,backup,sizeof(data))==0);
  assert(pcm_volume_scale_copy(out,data,5,24,50)==-1);
  assert(pcm_volume_scale_copy(out,data,16,8,50)==-1);
  assert(pcm_volume_scale_copy(out,data,16,64,50)==-1);
  assert(pcm_volume_scale_copy(NULL,data,16,16,50)==-1);
  assert(pcm_volume_scale_copy(out,NULL,16,16,50)==-1);
  assert(pcm_volume_scale_copy(NULL,NULL,0,16,50)==0);
  assert(pcm_volume_scale_copy((uint8_t *)(UINTPTR_MAX-2),data,4,16,50)==-1);
  printf("Independent PCM review: %lu signed LE format/cubic-gain cases passed; input immutability, guard bytes, overlap/null/framing/address-overflow refusal passed\n",tested);
  return 0;
}

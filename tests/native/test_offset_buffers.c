/* Compile exact native conversion/discovery seams under ASan/UBSan. */
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define E_LOG 0
#define L_RAOP 0
#define L_AIRPLAY 0
#define L_LAUDIO 0
#define DPRINTF(...) ((void)0)
#define RAOP_AUDIO_LATENCY_MS 250
#define AIRPLAY_AUDIO_LATENCY_MS 250
#define RAOP_QUALITY_SAMPLE_RATE_DEFAULT 44100
#define RAOP_QUALITY_BITS_PER_SAMPLE_DEFAULT 16
#define RAOP_QUALITY_CHANNELS_DEFAULT 2
#define STOB(s,b,c) ((s)*(c)*(b)/8)
/* @ACTUAL_RATE_BOUND@ */
struct media_quality { int sample_rate, bits_per_sample, channels; };
struct output_device { int offset_ms; struct media_quality quality; };
/* @ACTUAL_CHECKED_CONVERSIONS@ */
/* @ACTUAL_OFFSET_AND_BUFFER_FUNCTIONS@ */
struct discovery { const char *rate, *bits, *channels; };
static const char *keyval_get(struct discovery *txt, const char *key)
{
  return strcmp(key,"sr")==0 ? txt->rate : strcmp(key,"ss")==0 ? txt->bits : txt->channels;
}
static int safe_atoi32(const char *text, int *value)
{
  char *end;
  errno=0;
  long result=strtol(text,&end,10);
  if (errno || end==text || *end || result<INT_MIN || result>INT_MAX)
    return -1;
  *value=(int)result;
  return 0;
}
static int advertised_quality(struct discovery *txt, struct media_quality *out)
{
  struct output_device device, *rd=&device;
  const char *p;
  /* @ACTUAL_DISCOVERY_ADMISSION@ */
  *out=rd->quality;
  return 0;
#ifndef TEST_PREIMAGE
 invalid_quality:
  return -1;
#endif
}

#ifndef TEST_PREIMAGE
static unsigned conversion_cases(void)
{
  const uint64_t delays[]={0,1,1000,2000,2250,4000,60000,UINT32_MAX,UINT64_MAX/44100,UINT64_MAX/44100+1,UINT64_MAX};
  const int rates[]={INT_MIN,-1,0,1,8000,44100,48000,384000,1073742,INT_MAX};
  const uint64_t limits[]={0,INT_MAX,UINT32_MAX,UINT32_MAX/2,SIZE_MAX,UINT64_MAX};
  unsigned count=0;
  for(unsigned i=0;i<sizeof(delays)/sizeof(*delays);i++)
    for(unsigned j=0;j<sizeof(rates)/sizeof(*rates);j++)
      for(unsigned k=0;k<sizeof(limits)/sizeof(*limits);k++)
        {
          uint64_t value=123;
          __uint128_t product=rates[j]>0?(__uint128_t)delays[i]*(unsigned)rates[j]:0;
          int valid=rates[j]>0 && product<=UINT64_MAX && product/1000<=limits[k];
          int result=outputs_buffer_samples_get(&value,delays[i],rates[j],limits[k]);
          assert(result==(valid?0:-1));
          assert(value==(valid?(uint64_t)(product/1000):123));
          count++;
        }
  const uint64_t frames[]={0,1,2400,INT_MAX,UINT32_MAX,UINT64_MAX/4,UINT64_MAX/4+1,UINT64_MAX};
  const int bits[]={INT_MIN,-1,0,7,8,16,24,32,64,INT_MAX-7,INT_MAX};
  const int channels[]={INT_MIN,-1,0,1,2,8,INT_MAX};
  for(unsigned i=0;i<sizeof(frames)/sizeof(*frames);i++)
    for(unsigned j=0;j<sizeof(bits)/sizeof(*bits);j++)
      for(unsigned k=0;k<sizeof(channels)/sizeof(*channels);k++)
        for(unsigned n=0;n<sizeof(limits)/sizeof(*limits);n++)
          {
            uint64_t value=123;
            int sane=bits[j]>0 && bits[j]%8==0 && channels[k]>0;
            __uint128_t product=sane?(__uint128_t)frames[i]*(unsigned)channels[k]*(unsigned)(bits[j]/8):0;
            int valid=sane && product<=UINT64_MAX && product<=limits[n];
            int result=outputs_buffer_size_get(&value,frames[i],bits[j],channels[k],limits[n]);
            assert(result==(valid?0:-1));
            assert(value==(valid?(uint64_t)product:123));
            count++;
          }
  assert(outputs_buffer_samples_get(NULL,4000,48000,INT_MAX)<0);
  assert(outputs_buffer_size_get(NULL,48000,16,2,SIZE_MAX)<0);
  return count+2;
}

static unsigned offset_cases(int64_t (*convert)(int,int), uint32_t (*stamp)(uint32_t,int64_t))
{
  const int rates[]={1,8000,44100,48000,96000,192000,384000,1073742,INT_MAX};
  const uint32_t positions[]={0,1,1234,UINT32_MAX-1,UINT32_MAX};
  unsigned count=0;
  for(unsigned i=0;i<sizeof(rates)/sizeof(*rates);i++)
    for(int offset=-2000;offset<=2000;offset++)
      {
        int64_t expected=(int64_t)offset*rates[i]/1000;
        assert(convert(offset,rates[i])==expected);
        for(unsigned j=0;j<sizeof(positions)/sizeof(*positions);j++)
          assert(stamp(positions[j],expected)==(uint32_t)((uint64_t)positions[j]-(uint64_t)expected));
        count+=1+sizeof(positions)/sizeof(*positions);
      }
  const int offsets[]={INT_MIN,INT_MIN+1,INT_MAX-1,INT_MAX};
  for(unsigned i=0;i<sizeof(offsets)/sizeof(*offsets);i++)
    for(unsigned j=0;j<sizeof(rates)/sizeof(*rates);j++)
      {
        assert(convert(offsets[i],rates[j])==(int64_t)offsets[i]*rates[j]/1000);
        count++;
      }
  return count;
}
#endif

int main(int argc,char **argv)
{
#ifdef TEST_PREIMAGE
  if(argc==2 && strcmp(argv[1],"--offset-overflow")==0)
    { printf("%" PRId64 "\n",raop_offset(2000,1073742)); return 0; }
  if(argc==2 && strcmp(argv[1],"--alsa-overflow")==0)
    { uint64_t frames,bytes; alsa_buffer(1600000,44100,16,2,&frames,&bytes); printf("%" PRIu64 "\n",bytes); return 0; }
  struct discovery malformed={"1073742","16","2"};
  struct media_quality quality;
  assert(advertised_quality(&malformed,&quality)==0 && quality.sample_rate==1073742);
  malformed.rate="0";
  assert(advertised_quality(&malformed,&quality)==0 && quality.sample_rate==0);
  uint32_t output;
  assert(raop_buffer(UINT64_MAX/44100+251,44100,&output)==0);
  assert(output<45);
  assert(pulse_buffer(UINT32_MAX,48000,16,2,&output)==0);
  assert(output==(uint32_t)((uint64_t)UINT32_MAX*48000/1000*4));
  puts("Actual preimage: unbounded/zero discovery and wrapping RTP/Pulse buffer lengths reproduced");
#else
  (void)argc; (void)argv;
  unsigned count=conversion_cases()+offset_cases(raop_offset,raop_stamp)+offset_cases(airplay_offset,airplay_stamp);
  struct media_quality quality;
  struct discovery values={NULL,NULL,NULL};
  assert(advertised_quality(&values,&quality)==0 && quality.sample_rate==44100 && quality.bits_per_sample==16 && quality.channels==2);
  const char *rates[]={"1","44100","48000","96000","192000","384000","0","-1","384001","1073742","2147483647","2147483648","1x",""};
  for(unsigned i=0;i<sizeof(rates)/sizeof(*rates);i++)
    { values.rate=rates[i]; assert(advertised_quality(&values,&quality)==(i<6?0:-1)); count++; }
  const char *bits[]={"16","24","32","0","-1","8","64","2147483647","bad"};
  values.rate="48000";
  for(unsigned i=0;i<sizeof(bits)/sizeof(*bits);i++)
    { values.bits=bits[i]; assert(advertised_quality(&values,&quality)==(i<3?0:-1)); count++; }
  const char *channels[]={"1","2","8","0","-1","9","2147483647","bad"};
  values.bits="16";
  for(unsigned i=0;i<sizeof(channels)/sizeof(*channels);i++)
    { values.channels=channels[i]; assert(advertised_quality(&values,&quality)==(i<3?0:-1)); count++; }
  uint32_t output=123;
  assert(raop_buffer(4000,44100,&output)==0 && output==165375);
  assert(airplay_buffer(4000,44100,&output)==0 && output==165375);
  assert(raop_buffer(UINT64_MAX/44100+251,44100,&output)<0);
  assert(airplay_buffer(UINT64_MAX/44100+251,44100,&output)<0);
  assert(raop_buffer((uint64_t)UINT32_MAX*1000/44100+251,44100,&output)<0);
  uint64_t frames,bytes;
  assert(alsa_buffer(4000,48000,16,2,&frames,&bytes)==0 && frames==192000 && bytes==768000);
  assert(alsa_buffer(1600000,44100,16,2,&frames,&bytes)==0 && frames==70560000 && bytes==282240000);
  assert(alsa_buffer((uint64_t)INT_MAX*1000/44100+1,44100,16,2,&frames,&bytes)<0);
  assert(pulse_buffer(4000,48000,16,2,&output)==0 && output==768000);
  assert(pulse_buffer((uint64_t)UINT32_MAX,48000,16,2,&output)<0);
  printf("Actual sample/RTP/discovery/buffer seams: %u arithmetic cases plus backend width/admission boundaries\n",count);
#endif
  return 0;
}

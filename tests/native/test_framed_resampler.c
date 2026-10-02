/* Actual complete OwnTone outputs.c + transcode.c and framed adapter.
 * Libavfilter, codecs and evbuffers are real. Only configuration, logging and
 * player registration boundaries are local; no socket/device is opened. */
#include <assert.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <event2/buffer.h>
#include <event2/event.h>
#include <confuse.h>
#include "commands.h"
#include "shiri_source.h"
#include "outputs.c"

cfg_t *cfg;
struct event_base *evbase_player;
struct output_definition output_raop={0},output_airplay={0},output_streaming={0};
struct output_definition output_dummy={0},output_fifo={0},output_rcp={0};
#ifdef HAVE_ALSA
struct output_definition output_alsa={.name="ALSA"};
#else
#error The fixture requires the configured ALSA/framed profile
#endif
#ifdef HAVE_LIBPULSE
struct output_definition output_pulse={0};
#endif
#ifdef CHROMECAST
struct output_definition output_cast={0};
#endif
static unsigned log_errors;
void DPRINTF(int severity,int domain,const char *format,...)
{
  (void)domain;
  if(severity<=E_LOG){va_list ap;log_errors++;va_start(ap,format);vfprintf(stderr,format,ap);va_end(ap);}
}
void logger_ffmpeg(void *object,int level,const char *format,va_list ap)
{(void)object;(void)level;(void)format;(void)ap;}
void DHEXDUMP(int severity,int domain,const unsigned char *data,int data_len,const char *heading)
{(void)severity;(void)domain;(void)data;(void)data_len;(void)heading;}
void log_fatal_null(int domain,const char *function,int line)
{
  (void)domain;
  fprintf(stderr,"Required allocation failed in %s at line %d\n",function,line);
  abort();
}
int logger_severity(void){return E_LOG;}
bool quality_is_equal(struct media_quality *a,struct media_quality *b)
{
  return a->sample_rate==b->sample_rate&&a->bits_per_sample==b->bits_per_sample
    &&a->channels==b->channels&&a->bit_rate==b->bit_rate;
}
int player_device_add(void *device)
{assert(!outputs_device_list);outputs_device_list=device;return 0;}

static int fixture_clock(clockid_t clock,struct timespec *value)
{assert(clock==CLOCK_MONOTONIC);value->tv_sec=20;value->tv_nsec=0;return 0;}
/* Deterministic native presentation clock, not a receive-time origin. */
#define clock_gettime fixture_clock
#include "outputs/shiri_pcm_output.c"
#undef clock_gettime

/* Execute the actual player admission/seal/reset function. Input and output
 * ACK boundaries are controlled here; converter/history is the real FFmpeg. */
static struct shiri_source_state shiri_source_state;
static int shiri_source_flush_failed,player_state=1;
static struct {unsigned read_deficit;} pb_session;
static bool fixture_sealed,fixture_input_cleared;
#define PLAY_PLAYING 1
#include "actual_seal_param.inc"
#ifdef SHIRI_NATIVE_TRANSITION_LAYER
static bool pb_timer_native;
static int shiri_source_start_failed;
static int pb_timer_stop(void) { return 0; }
static int shiri_source_wait_begin(struct shiri_source_param *param)
{ assert(param->request.operation_generation); return 0; }
#endif
#ifdef SHIRI_PAUSED_SPEECH_LAYER
/* This oracle retains actual converter/reset behavior. The additional exact
 * player seal touches only these inert output-bed clock flags here; paused
 * lifecycle/voice/output readiness is exercised by its own actual C fixture. */
static bool pb_timer_speech_only, shiri_output_end_valid;
#endif

static int input_pipe_shiri_seal(uint64_t operation)
{assert(operation);fixture_sealed=true;return 0;}
static void input_flush(void *unused)
{(void)unused;assert(fixture_sealed);fixture_input_cleared=true;}
static void device_shiri_flush_cb(struct output_device *device,enum output_device_state state)
{(void)device;(void)state;}
static int fixture_flush_ack(output_status_cb callback,int *failed)
{
  assert(callback==device_shiri_flush_cb&&fixture_sealed&&fixture_input_cleared);
  assert(outputs_got_new_subscription);*failed=0;return 0;
}
static void fixture_metadata_purge(void){ }
#define outputs_shiri_flush fixture_flush_ack
#define outputs_metadata_purge fixture_metadata_purge
#include "actual_seal_function.inc"
#undef outputs_shiri_flush
#undef outputs_metadata_purge

static cfg_opt_t library_options[]={
  CFG_STR_LIST("decode_audio_filters","{}",CFGF_NONE),
  CFG_STR_LIST("decode_video_filters","{}",CFGF_NONE),CFG_END()};
static cfg_opt_t audio_options[]={
  CFG_STR("nickname","Fixture",CFGF_NONE),
  CFG_STR("shiri_pcm_socket","/never-opened/final.sock",CFGF_NONE),
  CFG_STR("shiri_pcm_room_id","11111111-2222-4333-8444-555555555555",CFGF_NONE),
  CFG_STR("shiri_pcm_launch_generation","0123456789abcdef0123456789abcdef",CFGF_NONE),
  CFG_INT("shiri_pcm_peer_uid",963,CFGF_NONE),CFG_INT("shiri_pcm_rate",48000,CFGF_NONE),
  CFG_INT("shiri_pcm_channels",2,CFGF_NONE),CFG_INT("shiri_pcm_format",0x8210,CFGF_NONE),CFG_END()};
static cfg_opt_t general_options[]={CFG_STR("user_agent","Shiri fixture",CFGF_NONE),CFG_END()};
static cfg_opt_t options[]={CFG_SEC("library",library_options,CFGF_NONE),
  CFG_SEC("audio",audio_options,CFGF_NONE),CFG_SEC("general",general_options,CFGF_NONE),CFG_END()};

static void start(unsigned rate,unsigned channels,unsigned format)
{
  cfg_t *audio;
  struct shiri_session *session;
  unsigned i;
  assert(!shiri_sessions&&!outputs_device_list);
  cfg=cfg_init(options,CFGF_NONE);assert(cfg);
  audio=cfg_getsec(cfg,"audio");
  cfg_setint(audio,"shiri_pcm_rate",rate);cfg_setint(audio,"shiri_pcm_channels",channels);
  cfg_setint(audio,"shiri_pcm_format",format);
  assert(!shiri_init());
  evbase_player=event_base_new();assert(evbase_player);
  for(i=0;i<sizeof(output_buffer.data)/sizeof(output_buffer.data[0]);i++){
    memset(&output_buffer.data[i],0,sizeof(output_buffer.data[i]));
    output_buffer.data[i].evbuf=evbuffer_new();assert(output_buffer.data[i].evbuf);
  }
  assert(!outputs_quality_subscribe(&shiri_quality));
  session=calloc(1,sizeof(*session));assert(session);
  session->fd=-1;session->callback_id=-1;session->volume=100;
  session->state=OUTPUT_STATE_CONNECTED;session->identity=shiri_config;
  session->identity.generation=1;session->identity.stream[0]=1;
  session->delay_ns=UINT64_C(2250000000);
  session->timer=evtimer_new(evbase_player,shiri_io,session);assert(session->timer);
  shiri_sessions=session;outputs_device_list->session=session;
  log_errors=0;
}

static void finish(void)
{
  unsigned i;
  shiri_deinit();
  for(i=0;i<sizeof(output_buffer.data)/sizeof(output_buffer.data[0]);i++){
    evbuffer_free(output_buffer.data[i].evbuf);memset(&output_buffer.data[i],0,sizeof(output_buffer.data[i]));
  }
  free(outputs_device_list->name);free(outputs_device_list);outputs_device_list=NULL;
  event_base_free(evbase_player);evbase_player=NULL;cfg_free(cfg);cfg=NULL;
  assert(!output_quality_subscriptions[0].count);
}

static struct output_data *find_quality(struct media_quality *quality,bool all_slots)
{
  unsigned i;
  for(i=0;i<sizeof(output_buffer.data)/sizeof(output_buffer.data[0]);i++){
    if(!all_slots&&!output_buffer.data[i].buffer)break;
    if(output_buffer.data[i].buffer&&quality_is_equal(quality,&output_buffer.data[i].quality))
      return &output_buffer.data[i];
  }
  return NULL;
}

static int mixed_subscription(void)
{
  struct media_quality first={.sample_rate=44100,.bits_per_sample=16,.channels=2};
  struct media_quality raw={.sample_rate=48000,.bits_per_sample=16,.channels=2};
  struct timespec pts={.tv_sec=20,.tv_nsec=500000000};
  int16_t sample[2]={1000,1000};
  unsigned tick,bad=0;
  start(48000,2,SHIRI_OUT_S32LE);
  outputs_quality_unsubscribe(&shiri_quality);
  assert(!outputs_quality_subscribe(&first));assert(!outputs_quality_subscribe(&shiri_quality));
  printf("{\"case\":\"empty first converter cannot hide later quality or retain drained PCM\",\"ticks\":[");
  for(tick=0;tick<2;tick++){
    struct output_data *visible,*hidden;
    uint64_t remaining=0;
    unsigned i;
    buffer_fill(&output_buffer,sample,sizeof(sample),&raw,1,&pts);
    visible=find_quality(&shiri_quality,false);hidden=find_quality(&shiri_quality,true);
    int64_t actual=0;
    if(visible&&visible->bufsize>=4){
      uint32_t u=(uint32_t)visible->buffer[0]|(uint32_t)visible->buffer[1]<<8
        |(uint32_t)visible->buffer[2]<<16|(uint32_t)visible->buffer[3]<<24;
      actual=(u&UINT32_C(0x80000000))?(int64_t)u-INT64_C(4294967296):u;
    }
    if(!visible||visible->samples!=1||visible->bufsize!=8||actual!=(tick?0:INT64_C(65536000)))bad++;
    int frames=hidden?hidden->samples:0;
    buffer_drain(&output_buffer);
    for(i=0;i<sizeof(output_buffer.data)/sizeof(output_buffer.data[0]);i++)
      remaining+=evbuffer_get_length(output_buffer.data[i].evbuf);
    if(remaining)bad++;
    printf("%s{\"visible\":%s,\"actual_converted_frames\":%d,\"first_s32_sample\":%"PRId64",\"bytes_retained_after_drain\":%"PRIu64"}",
      tick?",":"",visible?"true":"false",frames,actual,remaining);
    sample[0]=sample[1]=0;pts.tv_nsec+=20833;
  }
  printf("],\"failures\":%u}",bad);
  outputs_quality_unsubscribe(&first);finish();return bad!=0;
}

static uint64_t converter_tail(bool sealed)
{
  struct media_quality raw={.sample_rate=48000,.bits_per_sample=16,.channels=2};
  struct timespec pts={.tv_sec=20,.tv_nsec=500000000};
  int16_t samples[960];
  struct output_data *converted;
  unsigned i,tick;
  uint64_t nonzero=0;
  start(44100,2,SHIRI_OUT_S16LE);
  for(tick=0;tick<4;tick++){
    for(i=0;i<960;i++)samples[i]=16000;
    buffer_fill(&output_buffer,samples,sizeof(samples),&raw,480,&pts);buffer_drain(&output_buffer);
    pts.tv_nsec+=10000000;
  }
  if(sealed){
    struct shiri_source_param param={0};int result;
    memset(&shiri_source_state,0,sizeof(shiri_source_state));
    param.request.owner.incarnation[0]=1;param.request.owner.generation=1;param.request.operation_generation=1;
    fixture_sealed=fixture_input_cleared=false;
    assert(shiri_source_seal_flush(&param,&result)==COMMAND_END&&result==0);
    assert(fixture_sealed&&fixture_input_cleared);
  }
  memset(samples,0,sizeof(samples));
  for(tick=0;tick<4;tick++){
    buffer_fill(&output_buffer,samples,sizeof(samples),&raw,480,&pts);
    converted=find_quality(&shiri_quality,false);assert(converted&&converted->samples>0);
    for(i=0;i<converted->bufsize;i++)nonzero+=converted->buffer[i]!=0;
    buffer_drain(&output_buffer);pts.tv_nsec+=10000000;
  }
  finish();return nonzero;
}

static int64_t sample_le(const uint8_t *p,unsigned width)
{
  uint64_t u=0;
  for(unsigned i=0;i<width;i++)u|=(uint64_t)p[i]<<(i*8);
  return (u&(UINT64_C(1)<<(width*8-1)))?(int64_t)u-(int64_t)(UINT64_C(1)<<(width*8)):(int64_t)u;
}

static int actual_packet_gain(struct shiri_session *session)
{
  unsigned width=shiri_out_width(session->identity.format);
  unsigned source_width=(session->identity.format==SHIRI_OUT_S24LE||session->identity.format==SHIRI_OUT_S24_32LE)?4:width;
  uint8_t unity[SHIRI_OUT_PAYLOAD_MAX],half[SHIRI_OUT_PAYLOAD_MAX],original[SHIRI_OUT_PAYLOAD_MAX*2];
  struct shiri_packet *packet;
  unsigned nonzero=0;
  for(packet=session->head;packet;packet=packet->next){
    size_t samples=packet->bytes/width,raw_bytes=samples*source_width;
    if(raw_bytes!=packet->raw_bytes||packet->bytes!=packet->frames*session->identity.channels*width||raw_bytes>sizeof(original))return -1;
    memcpy(original,packet->samples,raw_bytes);
    if(shiri_out_gain(unity,packet->samples,packet->bytes,session->identity.format,100)<0
      ||shiri_out_gain(half,packet->samples,packet->bytes,session->identity.format,50)<0
      ||memcmp(original,packet->samples,raw_bytes))return -1;
    for(size_t i=0;i<samples;i++){
      int64_t raw=sample_le(original+i*source_width,source_width),expected=raw/8;
      int64_t at_unity=raw;
      if(source_width!=width||session->identity.format==SHIRI_OUT_S24_32LE){
        at_unity=raw<0?-((-raw+255)/256):raw/256;
        expected=expected<0?-((-expected+255)/256):expected/256;
      }
      if(sample_le(unity+i*width,width)!=at_unity||sample_le(half+i*width,width)!=expected)return -1;
      nonzero+=at_unity!=0;
    }
  }
  return nonzero?0:-1;
}

static int observe(unsigned rate,unsigned channels,unsigned format,unsigned block)
{
  const unsigned blocks=block==1?96:100;
  struct media_quality source={.sample_rate=48000,.bits_per_sample=16,.channels=2};
  int16_t samples[960];
  unsigned n,i,empty=0,matched=0,failed=0,first=UINT32_MAX;
  uint64_t native_frame=0,total_frames=0,first_pts=0,last_pts=0;
  int bits;
  start(rate,channels,format);bits=shiri_quality.bits_per_sample;
  printf("{\"rate\":%u,\"channels\":%u,\"format\":%u,\"block_frames\":%u,\"subscription_bits\":%d,\"ticks\":[",rate,channels,format,block,bits);
  for(n=0;n<blocks;n++){
    uint64_t ns=UINT64_C(20500000000)+native_frame*UINT64_C(1000000000)/48000;
    struct timespec pts={.tv_sec=(time_t)(ns/UINT64_C(1000000000)),.tv_nsec=(long)(ns%UINT64_C(1000000000))};
    struct output_data *selected=NULL;
    for(i=0;i<block;i++){
      int16_t value=(int16_t)(16000*sin(2.0*3.14159265358979323846*440*(native_frame+i)/48000));
      samples[i*2]=value;samples[i*2+1]=value;
    }
    buffer_fill(&output_buffer,samples,block*4,&source,block,&pts);
    for(i=0;output_buffer.data[i].buffer;i++)
      if(quality_is_equal(&output_buffer.data[i].quality,&shiri_quality)){selected=&output_buffer.data[i];break;}
    if(selected){matched++;total_frames+=(unsigned)selected->samples;if(first==UINT32_MAX)first=n;}
    else empty++;
    printf("%s{\"native_pts_ns\":%"PRIu64",\"native_first_frame\":%"PRIu64",\"output_frames\":%d,\"output_bytes\":%zu}",
      n?",":"",ns,native_frame,selected?selected->samples:0,selected?selected->bufsize:0);
    /* Invoke the actual write entry point, including its empty-encoder and
     * raw native-versus-converted PTS validation. All final PTS remain ahead
     * of the frozen clock, so no send/system call is exercised. */
    shiri_write(&output_buffer);
    if(!outputs_device_list->session){failed=1;buffer_drain(&output_buffer);break;}
    if(shiri_sessions->head){first_pts=shiri_sessions->head->pts;last_pts=shiri_sessions->tail->pts;}
    buffer_drain(&output_buffer);native_frame+=block;
  }
  int gain_bad=!failed&&matched?actual_packet_gain(shiri_sessions):1;
  printf("],\"empty_ticks\":%u,\"matched_ticks\":%u,\"converted_frames\":%"PRIu64",\"first_output_tick\":%d,\"first_final_pts_ns\":%"PRIu64",\"last_final_pts_ns\":%"PRIu64",\"write_failed\":%s,\"actual_wire_gain_source_unchanged\":%s,\"logged_errors\":%u}",
    empty,matched,total_frames,first==UINT32_MAX?-1:(int)first,first_pts,last_pts,failed?"true":"false",gain_bad?"false":"true",log_errors);
  /* The declared contract uses native origin and contiguous converted frame
   * counts, not arrival time. Canonical S32 is required for both 24-bit forms. */
  int bad=failed||!matched||bits!=(format==0x8210?16:32)||first_pts!=UINT64_C(22750000000)||gain_bad;
  finish();return bad;
}

int main(int argc,char **argv)
{
  unsigned rates[]={48000,44100},formats[]={0x8210,0x8318,0x8418,0x8420};
  unsigned r,c,f,case_number=0,failures=0;
  int diagnostic=argc==2&&!strcmp(argv[1],"--observe");
  if(argc!=1&&!diagnostic)return 2;
  printf("{\"scope\":\"actual OwnTone buffer_fill/transcode/framed write; no device or socket\",\"cases\":[");
  for(r=0;r<2;r++)for(c=1;c<=2;c++)for(f=0;f<4;f++){
    if(case_number++)printf(",");failures+=observe(rates[r],c,formats[f],480);
  }
  for(f=0;f<4;f++){
    if(case_number++)printf(",");failures+=observe(44100,2,formats[f],1);
  }
  printf("],\"mixed_subscription\":");failures+=mixed_subscription();
  uint64_t old_tail=converter_tail(false),new_tail=converter_tail(true);
  printf(",\"same_quality_source_barrier\":{\"unreset_control_nonzero_bytes\":%"PRIu64",\"real_sealed_successor_nonzero_bytes\":%"PRIu64"}",old_tail,new_tail);
  if(!old_tail||new_tail)failures++;
  printf(",\"failures\":%u}\n",failures);
  return failures&&!diagnostic?1:0;
}

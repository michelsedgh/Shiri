/* Scripted clocks/configuration only; execute the exact native functions. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
#include <sys/un.h>
#ifndef TEST_WITHOUT_SHIRI
#define CONFIG_SHIRI 1
#endif
#define BUFFER_FRAMES 1024
#define SPS_FORMAT_S16_LE 1
#define SPS_RATE_48000 1
static int warning_count;
static void debug(int level,const char *format,...) {(void)level;(void)format;}
static void warn(const char *format,...) {(void)format;warning_count++;}
typedef enum { clock_ok,clock_no_anchor_info,clock_not_valid,clock_not_ready,clock_service_unavailable,clock_access_error,clock_data_unavailable,clock_no_master,clock_version_mismatch,clock_not_synchronised } clock_status_t;
enum { realtime_stream,buffered_stream };
enum { ap_1,ap_2 };
typedef struct {
  int connection_number,anchor_remote_info_is_valid,last_anchor_info_is_valid;
  int airplay_type,airplay_stream_type,ap2_play_enabled,latency_warning_issued;
  uint32_t input_rate,anchor_rtptime,last_anchor_rtptime,latency,frames_per_packet;
  uint64_t anchor_clock,anchor_time,last_anchor_validity_start_time,last_anchor_local_time,last_anchor_time_of_update;
  clock_status_t clock_status;
} rtsp_conn_info;
typedef struct {const char *name;} audio_output;
static audio_output shiri={"shiri"}, other={"alsa"};
static struct {
  double audio_backend_latency_offset,audio_backend_buffer_desired_length;
  int minimum_free_buffer_headroom,shiri_buffered_audio_advance_ms;
  void *cfg;
  audio_output *output;
} config;
static const int32_t ap2_realttime_stream_latency_fudge_factor=11025;
static int long_time_notifcation_done;
static uint64_t previous_offset,previous_clock_id;
static uint64_t get_absolute_time_in_ns(void){return UINT64_C(200000000000);}
static int ptp_get_clock_info(uint64_t *clock,uint64_t *sample,uint64_t *offset,uint64_t *start) {
  if(clock)*clock=17;
  if(sample)*sample=UINT64_C(199999000000);
  if(offset)*offset=UINT64_C(20000000);
  if(start)*start=UINT64_C(180000000000);
  return clock_ok;
}
/* The exact init runs through the same libconfig success/type/absence contract. */
static char *socket_path;
static int peer_uid,setting_exists,setting_integer,setting_value,parse_count;
static jmp_buf invalid_setting;
static void die(const char *message) {(void)message;longjmp(invalid_setting,1);}
static void *config_lookup(void *cfg,const char *key) {
  assert(cfg==&config && strcmp(key,"shiri.buffered_audio_advance_ms")==0);
  return setting_exists?&config:NULL;
}
static int config_lookup_int(void *cfg,const char *key,int *value) {
  assert(cfg==&config);
  if(strcmp(key,"shiri.peer_uid")==0){*value=1000;return 1;}
  assert(strcmp(key,"shiri.buffered_audio_advance_ms")==0);
  if(!setting_integer)return 0;
  *value=setting_value;return 1;
}
static int config_lookup_non_empty_string(void *cfg,const char *key,const char **value) {
  assert(cfg==&config);
  assert(strcmp(key,"shiri.socket")==0 || strcmp(key,"shiri.volume_socket")==0);
  *value="/private/test.sock";return 1;
}
static int volume_init(const char *path){assert(path);return 0;}
static void parse_audio_options(const char *name,unsigned format,unsigned rate,unsigned channels) {
  assert(strcmp(name,"shiri")==0 && format==2 && rate==2 && channels==4);
  assert(config.audio_backend_latency_offset==0);parse_count++;
}
#include "shairport_buffered_timing.inc"

static rtsp_conn_info fresh(unsigned rate,uint32_t anchor) {
  rtsp_conn_info c={.input_rate=rate,.airplay_type=ap_2,.airplay_stream_type=buffered_stream,.frames_per_packet=352};
  set_ptp_anchor_info(&c,17,anchor,UINT64_C(300020000000));return c;
}
static uint64_t mapped(rtsp_conn_info *c,uint32_t timestamp) {
  uint64_t time;assert(frame_to_ptp_local_time(timestamp,&time,c)==0);return time;
}
static int effective(int milliseconds) {
#ifdef CONFIG_SHIRI
  return milliseconds;
#else
  (void)milliseconds;return 0;
#endif
}
static void configuration_case(int value) {
  setting_exists=setting_integer=1;setting_value=value;parse_count=0;
  int refused=setjmp(invalid_setting);
  if(!refused)assert(init(0,NULL)==0);
  assert((refused!=0)==(value<0 || value>600));
  assert(parse_count==(refused?0:1));
  if(!refused)assert(config.shiri_buffered_audio_advance_ms==value);
  free(socket_path);socket_path=NULL;
}
static void configuration_boundaries(void) {
  const int values[]={-1,0,1,140,350,599,600,601,INT32_MAX};
  config.cfg=&config;
  for(unsigned i=0;i<sizeof(values)/sizeof(values[0]);i++)
    configuration_case(values[i]);
  setting_exists=0;setting_integer=0;config.shiri_buffered_audio_advance_ms=600;
  assert(init(0,NULL)==0 && config.shiri_buffered_audio_advance_ms==0);
  free(socket_path);socket_path=NULL;
  setting_exists=1;setting_integer=0; /* A float/string/bool is not an integer setting. */
  if(!setjmp(invalid_setting)){init(0,NULL);assert(!"Wrong-type setting admitted");}
  free(socket_path);socket_path=NULL;
}
int main(void) {
  configuration_boundaries();config.output=&shiri;
  const unsigned rates[]={44100,48000};
  const uint32_t anchors[]={17,UINT32_MAX-17,UINT32_C(0x7fffff00)};
  unsigned cases=0;int64_t max_error=0;
  for(unsigned r=0;r<2;r++)for(unsigned a=0;a<3;a++)for(int direction=-1;direction<=1;direction++){
    rtsp_conn_info c=fresh(rates[r],anchors[a]);
    uint32_t target=anchors[a]+(uint32_t)(direction*(int)rates[r]+1);
    config.audio_backend_latency_offset=0;config.shiri_buffered_audio_advance_ms=0;
    uint64_t original=mapped(&c,target);
    for(int advance=0;advance<=600;advance++){
      config.shiri_buffered_audio_advance_ms=advance;
      uint64_t shifted=mapped(&c,target);
      int64_t error=(int64_t)(shifted+(uint64_t)effective(advance)*1000000)-(int64_t)original;
      if(llabs(error)>max_error)max_error=llabs(error);
      assert(llabs(error)<=1000000000/rates[r]+2);
      assert(c.anchor_rtptime==anchors[a] && c.anchor_time==UINT64_C(300020000000));
      uint32_t roundtrip;assert(local_ptp_time_to_frame(shifted,&roundtrip,&c)==0);
      assert(abs((int32_t)(target-roundtrip))<=1);
      /* The actual player releases 150ms before mapped P. */
      uint64_t release=mapped(&c,target-(uint32_t)(.15*rates[r]));
      assert(llabs((int64_t)(shifted-release)-150000000)<=2);
      if(advance==600){
        const int buffers[]={40,250,500};
        for(unsigned b=0;b<3;b++){
          uint64_t outgoing_final=shifted+(uint64_t)effective(600)*1000000;
          uint64_t owntone_anchor=outgoing_final-(uint64_t)buffers[b]*1000000;
          assert(llabs((int64_t)(owntone_anchor+(uint64_t)buffers[b]*1000000)-(int64_t)original)<=2);
        }
      }
      cases++;
    }
    /* Other output backends, realtime AP2 and AP1 retain the old mapping. */
    config.output=&other;assert(mapped(&c,target)==original);
    config.output=NULL;assert(mapped(&c,target)==original);
    config.output=&shiri;c.airplay_stream_type=realtime_stream;assert(mapped(&c,target)==original);
    c.airplay_stream_type=buffered_stream;c.airplay_type=ap_1;assert(mapped(&c,target)==original);
    c.airplay_type=ap_2;
    /* The ordinary upstream offset remains independent, including its sign. */
    config.audio_backend_latency_offset=.1;
    uint64_t adjusted=mapped(&c,target);
    assert(llabs((int64_t)adjusted-(int64_t)original+effective(600)*1000000LL-100000000)<=2);
  }
  /* A short realtime session cannot change any later buffered session's advance. */
  for(unsigned r=0;r<2;r++) {
    config.audio_backend_buffer_desired_length=.15;
    config.audio_backend_latency_offset=0;config.shiri_buffered_audio_advance_ms=600;
    rtsp_conn_info first=fresh(rates[r],UINT32_MAX-17);
    uint64_t before=mapped(&first,first.anchor_rtptime);
    for(unsigned n=0;n<100;n++) {
      rtsp_conn_info realtime=fresh(rates[r],n);realtime.airplay_stream_type=realtime_stream;
      uint32_t frame1=UINT32_MAX-17,frame2=frame1+rates[r]/2-11025;
      warning_count=0;realtime_window(&realtime,frame1,frame2);
      assert(warning_count==0 && config.audio_backend_latency_offset==0);
      assert(realtime.latency==rates[r]*35/100);
      assert(config.shiri_buffered_audio_advance_ms==600);
      rtsp_conn_info next=fresh(rates[r],UINT32_MAX-17);
      assert(mapped(&next,next.anchor_rtptime)==before);
    }
    /* Even the upstream mutable-offset fallback cannot poison the private value. */
    rtsp_conn_info realtime=fresh(rates[r],0);realtime.airplay_stream_type=realtime_stream;
    config.audio_backend_latency_offset=-.6;warning_count=0;
    realtime_window(&realtime,0,rates[r]/2-11025);
    assert(warning_count==1 && config.audio_backend_latency_offset==0);
    assert(config.shiri_buffered_audio_advance_ms==600);
    rtsp_conn_info next=fresh(rates[r],UINT32_MAX-17);
    assert(mapped(&next,next.anchor_rtptime)==before);
    warning_count=0;assert(ap1_window(&next,rates[r]/2)==rates[r]/2 && warning_count==0);
  }
  printf("{\"ok\":true,\"mapping_cases\":%u,\"max_quantization_error_ns\":%"PRId64",\"advance_max_ms\":%d,\"realtime_sessions\":200,\"configuration_boundaries\":true}\n",cases,max_error,effective(600));
}

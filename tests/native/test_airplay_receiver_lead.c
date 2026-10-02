/* Actual source excerpts define timestamp/wire/receiver behavior; only their
 * surrounding structs are isolated. Input candidate B values come from Python
 * policy, not another C copy of that policy. No socket or device is opened. */
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE 1
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <limits.h>
#if defined(__APPLE__)
#include <libkern/OSByteOrder.h>
#define htobe32 OSSwapHostToBigInt32
#define htobe64 OSSwapHostToBigInt64
#define be32toh OSSwapBigToHostInt32
#define be64toh OSSwapBigToHostInt64
#else
#include <endian.h>
#endif
#define FRAC 4294967296.
#define NTP_EPOCH_DELTA 0x83aa7e80
#define debug(...) ((void)0)
#define warn(...) ((void)0)
struct rtcp_timestamp { uint32_t pos; struct timespec ts; };
struct ntp_timestamp { uint32_t sec, frac; };
struct rtp_session { uint32_t pos; };
struct raop_master_session { struct rtcp_timestamp cur_stamp; struct rtp_session *rtp_session; uint32_t input_buffer_samples, output_buffer_samples; };
struct airplay_master_session { struct rtcp_timestamp cur_stamp; struct rtp_session *rtp_session; uint32_t input_buffer_samples, output_buffer_samples; };
struct receiver_conn { int input_rate, latency_warning_issued; uint32_t latency; };
static struct { double audio_backend_buffer_desired_length, audio_backend_latency_offset; } config;
static const int ap2_realttime_stream_latency_fudge_factor = 11025;
#include "airplay_receiver_lead.inc"
static uint32_t read32(const uint8_t *p) { uint32_t x; memcpy(&x,p,4); return be32toh(x); }
static uint64_t read64(const uint8_t *p) { uint64_t x; memcpy(&x,p,8); return be64toh(x); }
int main(void) {
  const uint32_t first = UINT32_C(0x12345678);
  unsigned cases=0, nonpositive=0, insufficient=0, mismatches=0;
  int minimum=INT_MAX, offset;
  unsigned buffer;
  /* A single common H3000 remains fixed while each candidate B determines
   * its input calendar. This covers the final corrected worst-case B2500. */
  const uint64_t native_p_ns=UINT64_C(100000000000), horizon_ns=UINT64_C(3000000000);
  while(scanf("%d %u", &offset, &buffer)==2) {
    assert(offset==(int)cases-2000 && buffer>=500 && buffer<=4250);
    struct rtp_session rtp={.pos=first};
    struct raop_master_session ra={.rtp_session=&rtp};
    struct airplay_master_session ap={.rtp_session=&rtp};
    struct receiver_conn conn={.input_rate=44100};
    uint8_t ntp[20], ptp[28]; uint64_t samples;
    uint64_t input_ns=native_p_ns+horizon_ns-(uint64_t)buffer*1000000;
    struct timespec stamp={(time_t)(input_ns/1000000000), (long)(input_ns%1000000000)};
    int64_t off=(int64_t)offset*44100/1000;
    assert(outputs_buffer_samples_get(&samples,buffer-250,44100,UINT32_MAX)==0);
    ra.output_buffer_samples=ap.output_buffer_samples=(uint32_t)samples;
    raop_timestamp_set(&ra,stamp); airplay_timestamp_set(&ap,stamp);
    assert(ra.cur_stamp.pos==ap.cur_stamp.pos);
    ra.cur_stamp.pos-=off; ap.cur_stamp.pos-=off;
    sync_packet_ntp_make(ntp,ra.cur_stamp,first,0x90);
    sync_packet_ptp_make(ptp,ap.cur_stamp,first,0x90,UINT64_C(0xaabbcc));
    assert(ntp[1]==0xd4 && ntp[3]==7 && ptp[1]==0xd7);
    assert(read32(ntp+4)==read32(ptp+4));
    assert(read32(ntp+16)==first && read32(ptp+16)==first-11025);
    assert(read64(ptp+8)==input_ns && read64(ptp+20)==UINT64_C(0xaabbcc));
    int32_t target_frames=(int32_t)(first-(read32(ptp+4)-11025));
    int64_t target_ns=(int64_t)read64(ptp+8)+(int64_t)target_frames*1000000000/44100;
    int64_t expected_ns=(int64_t)native_p_ns+(int64_t)horizon_ns+(int64_t)offset*1000000;
    /* Offset sample conversion is intentionally integer-quantized by upstream. */
    if(target_ns-expected_ns>1000000000/44100+1 || expected_ns-target_ns>1000000000/44100+1) mismatches++;
    config.audio_backend_buffer_desired_length=.15; config.audio_backend_latency_offset=0;
    consumer_latency(&conn,read32(ptp+4),read32(ptp+16));
    int32_t window=(int32_t)conn.latency;
    if(window<=0) nonpositive++;
    if(window<4410) insufficient++;
    if(window<minimum) minimum=window;
    cases++;
  }
  assert(feof(stdin) && cases==4001);
  printf("{\"ok\":%s,\"offset_cases\":%u,\"nonpositive_windows\":%u,\"below_known_100ms_windows\":%u,\"minimum_window_samples\":%d,\"presentation_mismatches\":%u}\n",
         nonpositive||insufficient||mismatches ? "false":"true", cases,nonpositive,insufficient,minimum,mismatches);
  return nonpositive||insufficient||mismatches ? 1:0;
}

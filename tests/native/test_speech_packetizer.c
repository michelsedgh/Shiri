/* Exact full output conversion + AirPlay packetization/ALAC functions.
 * The RTP delivery boundary captures packets and decodes them through a real
 * FFmpeg ALAC decoder, comparing every stereo sample to the encoder input.
 * No socket, device or house speaker is opened. */
#include <assert.h>
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <event2/buffer.h>
#include <event2/event.h>
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include "outputs.c"

cfg_t *cfg;
struct event_base *evbase_player;
static void airplay_write(struct output_buffer *);
struct output_definition output_raop={0},output_airplay={.write=airplay_write},output_streaming={0};
struct output_definition output_dummy={0},output_fifo={0},output_rcp={0};
#ifdef HAVE_ALSA
struct output_definition output_alsa={0};
#endif
#ifdef HAVE_LIBPULSE
struct output_definition output_pulse={0};
#endif
#ifdef CHROMECAST
struct output_definition output_cast={0};
#endif
void DPRINTF(int level,int domain,const char *format,...){(void)domain;if(level<=E_LOG){va_list ap;va_start(ap,format);vfprintf(stderr,format,ap);va_end(ap);}}
void logger_ffmpeg(void *object,int level,const char *format,va_list ap){(void)object;(void)level;(void)format;(void)ap;}
void DHEXDUMP(int level,int domain,const unsigned char *data,int bytes,const char *label){(void)level;(void)domain;(void)data;(void)bytes;(void)label;}
void log_fatal_null(int domain,const char *function,int line){(void)domain;fprintf(stderr,"allocation failure %s:%d\n",function,line);abort();}
int logger_severity(void){return E_LOG;}
bool quality_is_equal(struct media_quality *a,struct media_quality *b){return a->sample_rate==b->sample_rate&&a->bits_per_sample==b->bits_per_sample&&a->channels==b->channels&&a->bit_rate==b->bit_rate;}

enum {AIRPLAY_STATE_CONNECTED=1,AIRPLAY_STATE_STREAMING=2};
#define AIRPLAY_RTP_PAYLOADTYPE 0x60
#define RTP_MARKER_BIT 0x80
struct rtcp_timestamp {uint32_t pos;struct timespec ts;};
struct rtp_session {struct media_quality quality;uint32_t pos;int sync_counter,sync_each_nsamples;int pktbuf_len,pktbuf_size,pktbuf_next;uint16_t seqnum;};
struct rtp_packet {uint8_t header[12];uint8_t *payload;size_t payload_len;uint32_t pos;unsigned samples;};
struct airplay_master_session {
 struct evbuffer *input_buffer;uint32_t input_buffer_samples;
 struct encode_ctx *encode_ctx;struct evbuffer *encoded_buffer;
 struct rtp_session *rtp_session;struct rtcp_timestamp cur_stamp;
 struct timespec shiri_input_end;bool shiri_input_end_valid,shiri_sync_pending;
 uint8_t *rawbuf;size_t rawbuf_size;uint32_t samples_per_packet;
 struct media_quality quality;bool use_ptp;uint32_t output_buffer_samples;
 struct airplay_master_session *next;
};
struct airplay_session {struct airplay_master_session *master_session;struct airplay_session *next;int state;int64_t offset_samples;const char *devname;};
static struct airplay_master_session *airplay_master_sessions;
static struct airplay_session *airplay_sessions;
static struct event *keep_alive_timer;
static struct timeval keep_alive_tv={25,0};
static uint64_t packets,decoded_samples,converted_samples,matched_bytes;
static AVCodecContext *decoder;
static AVFrame *decoded;
static uint8_t packet_payload[8192];
static struct rtp_packet current_packet;
static int sync_packet_count;
static struct rtcp_timestamp last_sync_stamp;
static uint8_t gap_decoded[16384];
static size_t gap_decoded_bytes;
static bool gap_capture;
#include "actual_timing.inc"
#include "actual_rtp.inc"
static struct rtp_packet *rtp_packet_next(struct rtp_session *s,int bytes,unsigned frames,int type){
 assert(bytes>0 && (size_t)bytes<=sizeof(packet_payload) && frames==352 && type==AIRPLAY_RTP_PAYLOADTYPE);
 memset(&current_packet,0,sizeof(current_packet));current_packet.payload=packet_payload;current_packet.payload_len=bytes;current_packet.pos=s->pos;current_packet.samples=frames;return &current_packet;
}
static struct rtp_packet *rtp_sync_packet_next(struct rtp_session *s,struct rtcp_timestamp stamp,int type){
 (void)s;last_sync_stamp=stamp;assert(type==0x90 || type==0x80);++sync_packet_count;return &current_packet;
}
static void control_packet_send(struct airplay_session *s,struct rtp_packet *p){assert(s==airplay_sessions&&p==&current_packet);}
static void packet_send(struct airplay_session *s,struct rtp_packet *p){
 struct airplay_master_session *master=s->master_session;AVPacket *packet=av_packet_alloc();assert(packet);
 assert(av_new_packet(packet,p->payload_len)==0);memcpy(packet->data,p->payload,p->payload_len);
 assert(avcodec_send_packet(decoder,packet)==0);av_packet_free(&packet);
 assert(avcodec_receive_frame(decoder,decoded)==0 && decoded->nb_samples==352);
 assert(decoded->format==AV_SAMPLE_FMT_S16P);
 const int16_t *left=(const int16_t *)decoded->data[0],*right=(const int16_t *)decoded->data[1];
 for(unsigned i=0;i<352;i++) {
   unsigned l=master->rawbuf[4*i] | ((unsigned)master->rawbuf[4*i+1]<<8);
   unsigned r=master->rawbuf[4*i+2] | ((unsigned)master->rawbuf[4*i+3]<<8);
   assert((uint16_t)left[i]==l && (uint16_t)right[i]==r);
   if(gap_capture) {
     assert(gap_decoded_bytes+4<=sizeof(gap_decoded));
     gap_decoded[gap_decoded_bytes++]=l;gap_decoded[gap_decoded_bytes++]=l>>8;
     gap_decoded[gap_decoded_bytes++]=r;gap_decoded[gap_decoded_bytes++]=r>>8;
   }
 }
 packets++;decoded_samples+=352;matched_bytes+=1408;av_frame_unref(decoded);
}
#include "actual_airplay.inc"

static cfg_opt_t library_options[]={CFG_BOOL("pipe_framed",cfg_true,CFGF_NONE),CFG_STR_LIST("decode_audio_filters","{}",CFGF_NONE),CFG_STR_LIST("decode_video_filters","{}",CFGF_NONE),CFG_END()};
static cfg_opt_t general_options[]={CFG_STR("user_agent","Silent codec fixture",CFGF_NONE),CFG_END()};
static cfg_opt_t options[]={CFG_SEC("library",library_options,CFGF_NONE),CFG_SEC("general",general_options,CFGF_NONE),CFG_END()};
static void begin_case(void) {
 struct media_quality quality={.sample_rate=44100,.bits_per_sample=16,.channels=2};
 struct transcode_encode_setup_args args={.profile=XCODE_ALAC,.quality=&quality};
 struct airplay_master_session *master=calloc(1,sizeof(*master));assert(master);
 master->rtp_session=calloc(1,sizeof(*master->rtp_session));assert(master->rtp_session);
 master->rtp_session->quality=master->quality=quality;master->rtp_session->pos=88200;
 master->rtp_session->sync_each_nsamples=44100;master->rtp_session->pktbuf_size=1000;
 master->input_buffer=evbuffer_new();master->encoded_buffer=evbuffer_new();assert(master->input_buffer&&master->encoded_buffer);
 master->samples_per_packet=352;master->rawbuf_size=1408;master->rawbuf=malloc(1408);assert(master->rawbuf);
 master->output_buffer_samples=11025;
 args.src_ctx=transcode_decode_setup_raw(XCODE_PCM16,&quality);assert(args.src_ctx);
 master->encode_ctx=transcode_encode_setup(args);transcode_decode_cleanup(&args.src_ctx);assert(master->encode_ctx);
 airplay_master_sessions=master;airplay_sessions=calloc(1,sizeof(*airplay_sessions));assert(airplay_sessions);
 airplay_sessions->master_session=master;airplay_sessions->state=AIRPLAY_STATE_CONNECTED;airplay_sessions->devname="Capture";
 assert(outputs_quality_subscribe(&quality)==0);
 outputs_got_new_subscription=true;
 for(unsigned i=0;i<ARRAY_SIZE(output_buffer.data);i++)output_buffer.data[i].evbuf=evbuffer_new();
}
static void end_case(uint64_t input_frames,uint64_t case_converted,uint64_t case_decoded) {
 struct airplay_master_session *master=airplay_master_sessions;
 /* The only pending tail after the terminal padding is zero audio. Every
  * converted speech sample, including resampler history, reached a packet. */
 uint8_t *tail=evbuffer_pullup(master->input_buffer,-1);
 for(size_t i=0;i<evbuffer_get_length(master->input_buffer);i++)assert(tail[i]==0);
 assert(case_decoded>=(input_frames*44100/48000));
 assert(case_converted>=case_decoded && case_converted-case_decoded<352);
 printf("case input=%" PRIu64 " converted=%" PRIu64 " ALAC-decoded=%" PRIu64 " zero-pending=%u; every encoded stereo sample exact\n",input_frames,case_converted,case_decoded,master->input_buffer_samples);
 outputs_quality_unsubscribe(&master->quality);
 for(unsigned i=0;i<ARRAY_SIZE(output_buffer.data);i++){evbuffer_free(output_buffer.data[i].evbuf);memset(&output_buffer.data[i],0,sizeof(output_buffer.data[i]));}
 transcode_encode_cleanup(&master->encode_ctx);evbuffer_free(master->input_buffer);evbuffer_free(master->encoded_buffer);
 free(master->rawbuf);free(master->rtp_session);free(master);free(airplay_sessions);airplay_sessions=NULL;airplay_master_sessions=NULL;
}
static void gap_write(unsigned frames,int sample,uint64_t presentation) {
 uint8_t pcm[1920];assert(frames<=480);
 for(unsigned i=0;i<frames;i++) {
   pcm[4*i]=pcm[4*i+2]=(unsigned)sample;
   pcm[4*i+1]=pcm[4*i+3]=(unsigned)sample>>8;
 }
 struct media_quality quality={.sample_rate=48000,.bits_per_sample=16,.channels=2};
 struct timespec pts={presentation/1000000000,presentation%1000000000};
 uint64_t before=decoded_samples+airplay_master_sessions->input_buffer_samples;
 outputs_write(pcm,frames*4,frames,&quality,&pts);
 uint64_t after=decoded_samples+airplay_master_sessions->input_buffer_samples;
 assert(after>=before);converted_samples+=after-before;
}
static void gap_cases(void) {
 uint8_t contiguous[16384];size_t contiguous_bytes=0;
 for(unsigned gap=0;gap<2;gap++) {
   uint64_t start_converted=converted_samples,start_decoded=decoded_samples;
   begin_case();gap_capture=true;gap_decoded_bytes=0;
   const uint64_t first=UINT64_C(32000000000);
   gap_write(480,1000,first);gap_write(480,1000,first+10000000);gap_write(64,1000,first+20000000);
   struct airplay_master_session *master=airplay_master_sessions;
   assert(master->input_buffer_samples>0 && master->input_buffer_samples<352);
   unsigned old_pending=master->input_buffer_samples;
   uint32_t first_fresh_position=master->rtp_session->pos+old_pending;
   uint64_t resume=first+UINT64_C(1024)*1000000000/48000+(gap?UINT64_C(65000000000):0);
   int before_sync=sync_packet_count;
   gap_write(384,9000,resume);
   if(gap) {
#ifdef PREIMAGE
     assert(sync_packet_count==before_sync);
     assert((uint64_t)last_sync_stamp.ts.tv_sec*1000000000+last_sync_stamp.ts.tv_nsec<resume-UINT64_C(64000000000));
     puts("exact preimage: resumed payload preceded any sync for its65s original-PTS gap");
#else
     assert(sync_packet_count==before_sync+1 && master->rtp_session->sync_counter<44100);
     assert((uint64_t)last_sync_stamp.ts.tv_sec*1000000000+last_sync_stamp.ts.tv_nsec==resume);
     assert(last_sync_stamp.pos+master->output_buffer_samples==first_fresh_position);
     printf("65s original-PTS gap: retained=%u frames; fresh sync before resumed RTP maps first converted prefix to its original P+buffer\n",old_pending);
#endif
   } else assert(sync_packet_count==before_sync);
   gap_write(480,0,resume+8000000);
   if(gap) {
     assert(gap_decoded_bytes==contiguous_bytes && !memcmp(gap_decoded,contiguous,contiguous_bytes));
     puts("gapped and contiguous real ALAC decoding identical: retained partial packet/resampler state preserves fresh prefix and tail");
   } else {contiguous_bytes=gap_decoded_bytes;memcpy(contiguous,gap_decoded,contiguous_bytes);}
   end_case(1408,converted_samples-start_converted,decoded_samples-start_decoded);gap_capture=false;
 }
#ifdef PREIMAGE
 return;
#endif
 /* Fractional source durations, permitted mapped-clock noise and a true small
  * gap use the same exact original P; they never change the source clock. */
 uint64_t start_converted=converted_samples,start_decoded=decoded_samples;
 begin_case();uint64_t base=UINT64_C(120000000000);
 gap_write(480,1000,base);int init_sync=sync_packet_count;
 uint64_t next=base+10000000;
 gap_write(64,1000,next+100);
 gap_write(384,1000,next+100+UINT64_C(64)*1000000000/48000-200);
 assert(sync_packet_count==init_sync);
 uint64_t end=(uint64_t)airplay_master_sessions->shiri_input_end.tv_sec*1000000000+airplay_master_sessions->shiri_input_end.tv_nsec;
 gap_write(384,1000,end+1000000);assert(sync_packet_count==init_sync+1);
 end=(uint64_t)airplay_master_sessions->shiri_input_end.tv_sec*1000000000+airplay_master_sessions->shiri_input_end.tv_nsec;
 gap_write(384,1000,end-1000000);assert(sync_packet_count==init_sync+2);
 end=(uint64_t)airplay_master_sessions->shiri_input_end.tv_sec*1000000000+airplay_master_sessions->shiri_input_end.tv_nsec;
 /* A source/operation handoff may reuse this master; its distinct original
  * future timeline must get a new mapping without carrying the old clock. */
 gap_write(384,1000,end+2000000000);assert(sync_packet_count==init_sync+3);
 end=(uint64_t)airplay_master_sessions->shiri_input_end.tv_sec*1000000000+airplay_master_sessions->shiri_input_end.tv_nsec;
 gap_write(480,0,end);gap_write(480,0,end+10000000);
 end_case(2080,converted_samples-start_converted,decoded_samples-start_decoded);
 puts("fractional/+-100ns mapped spacing keeps ordinary sync cadence;1ms forward/backward and source-handoff gaps synchronize original P once");
}
int main(int argc,char **argv) {
 assert(argc==2);FILE *input=fopen(argv[1],"rb");assert(input);
 cfg=cfg_init(options,CFGF_NONE);assert(cfg);evbase_player=event_base_new();assert(evbase_player);
 keep_alive_timer=event_new(evbase_player,-1,EV_PERSIST,NULL,NULL);assert(keep_alive_timer);
 /* Obtain normal FFmpeg ALAC configuration before creating the independent
  * decoder. OwnTone also opens with this4096-frame cookie then sends352 frames. */
 AVCodecContext *reference=avcodec_alloc_context3(avcodec_find_encoder(AV_CODEC_ID_ALAC));assert(reference);
 reference->sample_rate=44100;reference->sample_fmt=AV_SAMPLE_FMT_S16P;reference->bits_per_raw_sample=16;
#if LIBAVUTIL_VERSION_MAJOR >= 57
 reference->ch_layout=(AVChannelLayout)AV_CHANNEL_LAYOUT_STEREO;
#else
 reference->channels=2;reference->channel_layout=AV_CH_LAYOUT_STEREO;
#endif
 assert(avcodec_open2(reference,NULL,NULL)==0);
 decoder=avcodec_alloc_context3(avcodec_find_decoder(AV_CODEC_ID_ALAC));assert(decoder);
 decoder->extradata=av_mallocz(reference->extradata_size+AV_INPUT_BUFFER_PADDING_SIZE);assert(decoder->extradata);
 decoder->extradata_size=reference->extradata_size;memcpy(decoder->extradata,reference->extradata,reference->extradata_size);
 assert(avcodec_open2(decoder,NULL,NULL)==0);avcodec_free_context(&reference);
 decoded=av_frame_alloc();assert(decoded);
 uint64_t declared=0,start_converted=0,start_decoded=0;unsigned cases=0;uint32_t frames;
 while(fread(&frames,sizeof(frames),1,input)==1) {
   uint64_t stamp;assert(fread(&stamp,sizeof(stamp),1,input)==1);
   if(!frames) {
     if(declared)end_case(declared,converted_samples-start_converted,decoded_samples-start_decoded);
     declared=stamp;start_converted=converted_samples;start_decoded=decoded_samples;begin_case();cases++;continue;
   }
   assert(declared && frames<=480);uint8_t pcm[1920];assert(fread(pcm,frames*4,1,input)==1);
   struct media_quality quality={.sample_rate=48000,.bits_per_sample=16,.channels=2};
   struct timespec pts={stamp/1000000000,stamp%1000000000};
   uint64_t before=decoded_samples+airplay_master_sessions->input_buffer_samples;
   outputs_write(pcm,frames*4,frames,&quality,&pts);
   uint64_t after=decoded_samples+airplay_master_sessions->input_buffer_samples;
   assert(after>=before);converted_samples+=after-before;
 }
 assert(feof(input) && declared && cases==4);
 end_case(declared,converted_samples-start_converted,decoded_samples-start_decoded);
 gap_cases();
 printf("actual OwnTone AirPlay conversion/packetizer: cases=%u packets=%" PRIu64 " matched-bytes=%" PRIu64 " syncs=%d; no socket/audio device\n",cases,packets,matched_bytes,sync_packet_count);
 fclose(input);av_frame_free(&decoded);avcodec_free_context(&decoder);event_free(keep_alive_timer);event_base_free(evbase_player);cfg_free(cfg);return 0;
}

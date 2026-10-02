/* Actual packet_prepare/encode_write with real FFmpeg rational rescaling.
 * Only codec packet delivery and the final mux call are controlled seams. */
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavfilter/avfilter.h>
#include <libavutil/mathematics.h>
/* @ACTUAL_STREAM@ */
/* @ACTUAL_PREPARE@ */
#ifndef TEST_PREIMAGE
struct encode_ctx { AVPacket *encoded_pkt; AVFormatContext *ofmt_ctx; };
static AVPacket incoming;
static unsigned delivered,mux_calls;
static int fixture_send(AVCodecContext *codec,const AVFrame *frame){(void)codec;(void)frame;return 0;}
static int fixture_receive(AVCodecContext *codec,AVPacket *packet){(void)codec;if(delivered++)return AVERROR(EAGAIN);*packet=incoming;return 0;}
static int fixture_mux(AVFormatContext *context,AVPacket *packet){(void)context;(void)packet;mux_calls++;return 0;}
#define avcodec_send_frame fixture_send
#define avcodec_receive_packet fixture_receive
#define av_interleaved_write_frame fixture_mux
#define DPRINTF(...) ((void)0)
/* @ACTUAL_ENCODE@ */
#endif

static AVCodecContext codec;
static AVStream stream;
static struct stream_ctx state;
static void reset(void){memset(&codec,0,sizeof(codec));memset(&stream,0,sizeof(stream));memset(&state,0,sizeof(state));
 codec.time_base=(AVRational){1,48000};stream.time_base=(AVRational){1,1000};stream.index=2;state.codec=&codec;state.stream=&stream;}
#ifndef TEST_PREIMAGE
static void rejected(AVPacket packet){AVPacket before=packet;int64_t prev=state.prev_pts,offset=state.offset_pts;
 assert(packet_prepare(&packet,&state)<0);assert(!memcmp(&packet,&before,sizeof(packet))&&state.prev_pts==prev&&state.offset_pts==offset);}
#endif
int main(void){
 reset();AVPacket packet={.pts=AV_NOPTS_VALUE,.dts=AV_NOPTS_VALUE,.duration=480};
#ifdef TEST_PREIMAGE
 packet_prepare(&packet,&state);puts("Missing-PTS preimage failed to trigger expected signed overflow");return 99;
#else
 state.prev_pts=100;state.offset_pts=50;assert(!packet_prepare(&packet,&state));
 assert(packet.pts==AV_NOPTS_VALUE&&packet.dts==AV_NOPTS_VALUE&&packet.duration==10&&packet.stream_index==2);
 assert(state.prev_pts==100&&state.offset_pts==50);
 packet=(AVPacket){.pts=AV_NOPTS_VALUE,.dts=-48000,.duration=480};assert(!packet_prepare(&packet,&state));
 assert(packet.pts==AV_NOPTS_VALUE&&packet.dts==-1000&&state.prev_pts==100&&state.offset_pts==50);
 reset();packet=(AVPacket){.pts=48000,.dts=47900,.duration=480};assert(!packet_prepare(&packet,&state));
 assert(packet.pts==1000&&packet.dts==1000&&state.prev_pts==48000&&state.offset_pts==0);
 reset();state.prev_pts=200;packet=(AVPacket){.pts=100,.duration=480};assert(!packet_prepare(&packet,&state));
 assert(packet.pts==4&&state.prev_pts==200&&state.offset_pts==100);
 packet=(AVPacket){.pts=100,.duration=480};assert(!packet_prepare(&packet,&state)&&packet.pts==4&&state.offset_pts==100);
 reset();state.offset_pts=1;rejected((AVPacket){.pts=INT64_MAX});
 reset();state.prev_pts=INT64_MAX;rejected((AVPacket){.pts=-1});
 reset();state.prev_pts=INT64_MAX;state.offset_pts=INT64_MAX-2;rejected((AVPacket){.pts=-1});
 reset();state.offset_pts=-1;rejected((AVPacket){.pts=INT64_MIN+1});
 reset();state.prev_pts=AV_NOPTS_VALUE;rejected((AVPacket){.pts=1});
 reset();state.offset_pts=AV_NOPTS_VALUE;rejected((AVPacket){.pts=AV_NOPTS_VALUE});
 reset();codec.time_base=(AVRational){1000,1};stream.time_base=(AVRational){1,1};rejected((AVPacket){.pts=INT64_MAX/100});
 reset();codec.time_base=(AVRational){1000,1};stream.time_base=(AVRational){1,1};rejected((AVPacket){.pts=0,.duration=INT64_MAX});
 reset();codec.time_base=stream.time_base=(AVRational){1,1};packet=(AVPacket){.pts=INT64_MAX};assert(!packet_prepare(&packet,&state)&&packet.pts==INT64_MAX);
 for(unsigned field=0;field<4;field++){reset();switch(field){case 0:codec.time_base.num=0;break;case 1:codec.time_base.den=-1;break;
 case 2:stream.time_base.num=0;break;case 3:stream.time_base.den=-1;break;}rejected((AVPacket){.pts=1});}
 reset();rejected((AVPacket){.pts=1,.duration=-1});
 /* Arithmetic extrema with valid same-timebase values retain prior behavior. */
 const int64_t values[]={INT64_MIN+1,-1000000,-1,0,1,1000000,INT64_MAX-1,INT64_MAX};
 for(unsigned i=0;i<sizeof(values)/sizeof(values[0]);i++){reset();codec.time_base=stream.time_base=(AVRational){1,1};
 packet=(AVPacket){.pts=values[i]};assert(!packet_prepare(&packet,&state));
 assert(packet.pts==(values[i]<0?0:values[i])&&packet.dts==packet.pts&&state.prev_pts==packet.pts);}
 /* Prove actual encode_write refuses a bad packet before invoking the mux. */
 reset();state.offset_pts=1;AVPacket encoded={0};struct encode_ctx ctx={.encoded_pkt=&encoded};
 incoming=(AVPacket){.pts=INT64_MAX};delivered=mux_calls=0;
 assert(encode_write(&ctx,&state,NULL)==AVERROR(EOVERFLOW)&&mux_calls==0);
 reset();incoming=(AVPacket){.pts=AV_NOPTS_VALUE,.dts=AV_NOPTS_VALUE,.duration=480};delivered=mux_calls=0;
 assert(!encode_write(&ctx,&state,NULL)&&mux_calls==1&&encoded.pts==AV_NOPTS_VALUE);
 puts("Actual encoder timestamps: real FFmpeg missing/valid/reverse/extreme/rescale checks preserve state and reject overflow before mux");return 0;
#endif
}

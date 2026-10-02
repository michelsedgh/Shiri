/* Actual buffer_fill/encoding_reset with controllable converter failures.
 * Real FFmpeg DSP/empty-output behavior is covered by the separate full-source
 * fixture; here failures must remain distinct from successful zero-byte warmup. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
struct media_quality { int sample_rate,bits_per_sample,channels; };
struct evbuffer { uint8_t data[128];size_t size; };
struct output_data { struct media_quality quality;uint8_t *buffer;size_t bufsize;int samples;struct evbuffer *evbuf; };
struct output_buffer { struct timespec { long tv_sec,tv_nsec; } pts;struct output_data data[4];bool conversion_error; };
struct output_quality_subscription { int count;struct media_quality quality;void *encode_ctx; };
static struct output_quality_subscription output_quality_subscriptions[3];
static bool outputs_got_new_subscription;
enum transcode_profile { XCODE_UNKNOWN,XCODE_PCM16,XCODE_PCM32 };
struct transcode_encode_setup_args { enum transcode_profile profile;void *src_ctx;struct media_quality *quality; };
typedef int transcode_frame;
static int decoder,encoder,frame;
static bool fail_decode,fail_setup,fail_frame,fail_encode,empty;
#define DPRINTF(...) ((void)0)
#define BTOS(bytes,bits,channels) ((bytes)*8/(bits)/(channels))
#define ARRAY_SIZE(array) (sizeof(array)/sizeof((array)[0]))
static bool quality_is_equal(struct media_quality*a,struct media_quality*b){return !memcmp(a,b,sizeof(*a));}
static enum transcode_profile quality_to_xcode(struct media_quality*q){return q->bits_per_sample==16?XCODE_PCM16:q->bits_per_sample==32?XCODE_PCM32:XCODE_UNKNOWN;}
static void *transcode_decode_setup_raw(enum transcode_profile p,struct media_quality*q){(void)p;(void)q;return fail_decode?NULL:&decoder;}
static void transcode_decode_cleanup(void **ctx){*ctx=NULL;}
static void transcode_encode_cleanup(void **ctx){*ctx=NULL;}
static void *transcode_encode_setup(struct transcode_encode_setup_args a){assert(a.src_ctx);return fail_setup?NULL:&encoder;}
static transcode_frame *transcode_frame_new(void *buf,size_t bytes,int samples,struct media_quality*q){(void)buf;(void)bytes;(void)samples;(void)q;return fail_frame?NULL:&frame;}
static void transcode_frame_free(transcode_frame*f){assert(f==&frame);}
static size_t evbuffer_get_length(struct evbuffer*b){return b->size;}
static int evbuffer_drain(struct evbuffer*b,size_t bytes){assert(bytes<=b->size);b->size-=bytes;return 0;}
static int evbuffer_add(struct evbuffer*b,void*data,size_t bytes){assert(b->size+bytes<=sizeof(b->data));memcpy(b->data+b->size,data,bytes);b->size+=bytes;return 0;}
static uint8_t *evbuffer_pullup(struct evbuffer*b,int bytes){assert(bytes==-1);return b->size?b->data:NULL;}
static int transcode_encode(struct evbuffer*b,void*ctx,transcode_frame*f,int flags){(void)flags;assert(ctx==&encoder&&f==&frame);
 uint8_t sample[8]={1};if(!empty || fail_encode)evbuffer_add(b,sample,sizeof(sample));return fail_encode?-1:0;}
/* @ACTUAL_CONVERSION@ */
int main(void){
 struct evbuffer buffers[4]={0};struct output_buffer out={0};uint8_t raw[4]={0};
 struct media_quality source={48000,16,2};struct timespec pts={20,0};
 output_quality_subscriptions[0]=(struct output_quality_subscription){1,{48000,32,2},NULL};
 for(unsigned i=0;i<4;i++)out.data[i].evbuf=&buffers[i];
 /* Every failure must be observable now, not mistaken for bounded warmup. */
 for(unsigned failure=0;failure<4;failure++){
  fail_decode=failure==0;fail_setup=failure==1;fail_frame=failure==2;fail_encode=failure==3;
  outputs_got_new_subscription=true;buffer_fill(&out,raw,4,&source,1,&pts);assert(out.conversion_error);
  assert(!buffers[1].size); /* A failed encoder cannot leave a partial packet. */
  for(unsigned i=0;i<4;i++){buffers[i].size=0;out.data[i].buffer=NULL;}
  fail_decode=fail_setup=fail_frame=fail_encode=false;empty=true;outputs_got_new_subscription=true;
  buffer_fill(&out,raw,4,&source,1,&pts);assert(!out.conversion_error&&!out.data[1].buffer);
  for(unsigned i=0;i<4;i++){buffers[i].size=0;out.data[i].buffer=NULL;}
 }
 empty=false;buffer_fill(&out,raw,4,&source,1,&pts);assert(!out.conversion_error&&out.data[1].samples==1&&out.data[1].bufsize==8);
 buffer_drain(&out);for(unsigned i=0;i<4;i++)assert(!buffers[i].size&&!out.data[i].buffer);
 /* Defensive cleanup is bounded across every initialized slot, even when an
  * error left bytes behind a NULL entry. It must not depend on visible holes. */
 uint8_t pending[8]={0};evbuffer_add(&buffers[2],pending,sizeof(pending));out.data[2].bufsize=0;out.data[2].buffer=NULL;
 buffer_drain(&out);assert(!buffers[2].size);
 puts("Actual conversion failures are marked/cleared per block; successful empty resampler warmup remains distinct");return 0;
}

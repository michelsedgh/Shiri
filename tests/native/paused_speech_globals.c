#include <inttypes.h>
#ifndef TIMER_ABSTIME
#define TIMER_ABSTIME 1
struct itimerspec {struct timespec it_interval,it_value;};
#endif
static int player_state;
static bool pb_timer_native, pb_timer_native_anchored, pb_timer_native_waiting_for_pcm;
static bool pb_timer_speech_only;
static uint64_t pb_timer_native_operation;
static struct timespec pb_timer_native_anchor,pb_timer_native_wait_until;
static struct timespec shiri_output_end;
static bool shiri_output_end_valid;
static uint64_t shiri_output_bed_frames;
static struct timespec player_tick_interval={0,10000000};
static int pb_timer, timer_arms, timer_absolute, timer_failure;
static struct timespec armed_anchor;
static int mock_timer_settime(int id,int flags,const struct itimerspec *tick,void *old) {
 (void)id;(void)old;if(timer_failure)return -1;
 ++timer_arms;timer_absolute=flags;armed_anchor=tick->it_value;return 0;
}
#define timer_settime mock_timer_settime
static int mock_timer_getoverrun(int id){(void)id;return 0;}
#define timer_getoverrun mock_timer_getoverrun
struct media_quality {unsigned sample_rate,bits_per_sample,channels;};
struct source {int data_kind;};
#define DATA_KIND_PIPE 7
static uint8_t input_pcm[1920],session_buffer[1920],last_output[1920];
static struct {struct media_quality quality;void *buffer;size_t bufsize,read_deficit,read_deficit_max;uint64_t pos;struct timespec pts,start_ts;struct source *reading_now;} pb_session;
static unsigned pb_write_deficit_max=50;
static bool pb_write_recovery;
static int player_flush_pending,abort_calls,read_calls,output_calls,suspend_calls;
static size_t pending_pcm;
static int gate_state=1,peek_failure,inject_after_empty_peek;
static struct timespec injected_anchor;
static struct timespec input_anchor,last_output_pts;
static uint64_t gate_operation;
static struct timespec timespec_add(struct timespec a,struct timespec b){a.tv_sec+=b.tv_sec;a.tv_nsec+=b.tv_nsec;if(a.tv_nsec>=1000000000){++a.tv_sec;a.tv_nsec-=1000000000;}return a;}
static int timespec_cmp(struct timespec a,struct timespec b){return a.tv_sec!=b.tv_sec?(a.tv_sec>b.tv_sec?1:-1):a.tv_nsec!=b.tv_nsec?(a.tv_nsec>b.tv_nsec?1:-1):0;}
static int input_pipe_shiri_state(uint64_t *op){*op=gate_operation;return gate_state;}
static int input_peek_sync(struct timespec *anchor){*anchor=input_anchor;if(peek_failure)return -1;if(pending_pcm)return 1;if(inject_after_empty_peek){inject_after_empty_peek=0;pending_pcm=sizeof(input_pcm);input_anchor=injected_anchor;now_ns+=100000;}return 0;}
static int source_read(int *bytes,int *frames,void *pcm,size_t maximum){++read_calls;assert(maximum>=pending_pcm);*bytes=pending_pcm;*frames=pending_pcm/4;memcpy(pcm,input_pcm,pending_pcm);pb_session.pos+=*frames;pending_pcm=0;return 0;}
static void outputs_write(void *pcm,int bytes,int frames,struct media_quality *quality,struct timespec *pts){assert(bytes==frames*4 && bytes<=1920);assert(quality->sample_rate==48000 && quality->bits_per_sample==16 && quality->channels==2);++output_calls;memcpy(last_output,pcm,bytes);last_output_pts=*pts;}
static void pb_abort(void){++abort_calls;}
static int pb_suspend(void){++suspend_calls;return 0;}
static void player_playback_start(void){}
static void input_buffer_full_cb(void (*cb)(void)){(void)cb;}
static int pb_timer_native_prepare(void);
static int pb_timer_stop(void);

static int input_pipe_shiri_arm(const struct shiri_pcm_owner *owner,uint64_t operation){(void)owner;gate_operation=operation;gate_state=1;return 0;}
static int input_pipe_shiri_disarm(const struct shiri_pcm_owner *owner,uint64_t operation){(void)owner;assert(operation==gate_operation);gate_state=-1;return 0;}
static int input_pipe_shiri_seal(uint64_t operation){gate_operation=operation;gate_state=-1;return 0;}
static int input_flush(void *arg){(void)arg;pending_pcm=0;return 0;}
static void outputs_resampling_reset(void){}
static void outputs_metadata_purge(void){}
static void device_shiri_flush_cb(struct output_device *d,enum output_device_state status){(void)d;(void)status;}
static int outputs_shiri_flush(output_status_cb cb,int *failed){(void)cb;*failed=0;++flushes;return 0;}
static void pb_session_stop(void){pb_timer_stop();player_state=PLAY_STOPPED;pb_session.reading_now=NULL;}

static unsigned foreign_stops;
static void device_shiri_cleanup_cb(struct output_device *d,enum output_device_state status){(void)d;(void)status;}

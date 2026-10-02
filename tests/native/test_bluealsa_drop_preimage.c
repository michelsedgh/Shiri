#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define G_GNUC_UNUSED __attribute__((unused))
#define G_SOURCE_CONTINUE 1
#define G_SOURCE_REMOVE 0
#define BA_TRANSPORT_PCM_MODE_SINK 1
#define BA_TRANSPORT_PCM_SIGNAL_DROP 5
#define BA_TRANSPORT_PCM_SIGNAL_CLOSE 1
#define BLUEALSA_PCM_CTRL_DRAIN "Drain"
#define BLUEALSA_PCM_CTRL_DROP "Drop"
#define BLUEALSA_PCM_CTRL_PAUSE "Pause"
#define BLUEALSA_PCM_CTRL_RESUME "Resume"
#define DEBUG 0
#define error(...) ((void)0)
#define warn(...) ((void)0)
#define debug(...) ((void)0)
struct ba_transport_pcm { int mode,fd;pthread_mutex_t mutex;void *t; };
typedef struct {const char *command;char reply[32];} GIOChannel;
typedef struct {const char *message;} GError;
typedef int GIOCondition;
enum {G_IO_STATUS_NORMAL,G_IO_STATUS_AGAIN,G_IO_STATUS_ERROR,G_IO_STATUS_EOF};
static unsigned queued,flushed;
static int ba_transport_pcm_signal_send(struct ba_transport_pcm *p,int sig){(void)p;assert(sig==BA_TRANSPORT_PCM_SIGNAL_DROP||sig==BA_TRANSPORT_PCM_SIGNAL_CLOSE);queued++;return 0;}
static int io_pcm_flush(struct ba_transport_pcm *p){(void)p;flushed++;return 0;}
static int ba_transport_pcm_drain(struct ba_transport_pcm *p){(void)p;return 0;}
static int ba_transport_pcm_pause(struct ba_transport_pcm *p){(void)p;return 0;}
static int ba_transport_pcm_resume(struct ba_transport_pcm *p){(void)p;return 0;}
static int ba_transport_pcm_release(struct ba_transport_pcm *p){(void)p;return 0;}
static void ba_transport_stop_if_no_clients(void *t){(void)t;}
static int g_io_channel_read_chars(GIOChannel *c,char *b,size_t n,size_t *len,GError **e){(void)n;(void)e;*len=strlen(c->command);memcpy(b,c->command,*len);return G_IO_STATUS_NORMAL;}
static int g_io_channel_write_chars(GIOChannel *c,const char *b,int n,size_t *len,void *e){(void)n;(void)e;*len=strlen(b);strcpy(c->reply,b);return 0;}
static void g_io_channel_flush(GIOChannel *c,void *e){(void)c;(void)e;}
static void g_error_free(GError *e){(void)e;}
/* @ACTUAL_PREIMAGE_DROP@ */
/* @ACTUAL_PREIMAGE_CONTROLLER@ */
int main(void){struct ba_transport_pcm p={.mode=BA_TRANSPORT_PCM_MODE_SINK};GIOChannel c={.command="Drop"};bluealsa_pcm_controller(&c,0,&p);assert(strcmp(c.reply,"OK")==0&&queued==1&&flushed==0);c=(GIOChannel){.command="DropSync"};bluealsa_pcm_controller(&c,0,&p);assert(strcmp(c.reply,"Invalid")==0&&queued==1&&flushed==0);puts("preimage reproduced: stock Drop OK precedes selected PCM/codec flush; DropSync is unavailable");return 0;}

/* Actual guard parser and pcm_open; controlled syscall/ALSA observations. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/stat.h>
#include <unistd.h>
#include <json-c/json.h>
#ifdef __linux__
#include <sys/vfs.h>
#include <linux/magic.h>
#else
struct statfs { long f_type; };
#define SYSFS_MAGIC 0x62656572
#define O_PATH 0x200000
#endif
#define SHIRI_PCM_IDENTITY_TEST 1
#define DPRINTF(...) ((void)0)
#define SND_PCM_STREAM_PLAYBACK 0
#define SND_PCM_ACCESS_RW_INTERLEAVED 0
#define ALSA_ERROR_DEVICE -4
#define ALSA_ERROR_DEVICE_BUSY -5
#define ALSA_ERROR_IDENTITY -6
#define MANIFEST "/private/pcm-identity.json"
#define SYSFS "/sys/devices/virtual/sound/card7"
#define NODE "/dev/snd/pcmC7D1p"
#define BOOT "11111111-2222-3333-4444-555555555555"
static const char valid_json[]="{\"version\":1,\"boot_id\":\"" BOOT "\",\"card_index\":7,\"device\":1,\"subdevice\":7,\"sysfs_path\":\"" SYSFS "\",\"sysfs_st_dev\":23,\"sysfs_st_ino\":777,\"node_path\":\"" NODE "\",\"node_st_dev\":44,\"node_st_ino\":888,\"node_st_rdev\":999}";
static char document[8192];
static size_t manifest_position;
static int sysfs_opens,node_opens,boot_reads,params,closed,info_allocations;
static bool wrong_owner,writable,linked,symlinked,wrongfs,wrongboot,replaced_sysfs,replaced_node,wrong_pcm_fd;
static bool bad_card,bad_device,bad_subdevice,bad_stream,opaque_info,missing_pcm_fd,info_malloc_fails,hw_malloc_fails,hw_any_fails;
static bool extra_wrong_pcm,fd_query_fails,pcm_busy;
static const char *opened_name;
typedef struct { int unused; } snd_pcm_t;
typedef struct { int unused; } snd_pcm_info_t;
typedef struct { int unused; } snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;
static snd_pcm_t pcm_handle;
struct media_quality { unsigned sample_rate,bits_per_sample,channels; };
static int fixture_open(const char*path,int flags,...){
  if(!strcmp(path,MANIFEST)){assert(flags&O_NOFOLLOW);manifest_position=0;return symlinked?-1:100;}
  if(!strcmp(path,"/proc/sys/kernel/random/boot_id"))return 101;
  if(!strcmp(path,SYSFS)){assert((flags&O_PATH)&&(flags&O_DIRECTORY)&&(flags&O_NOFOLLOW));sysfs_opens++;return 200;}
  if(!strcmp(path,NODE)){assert((flags&O_PATH)&&(flags&O_NOFOLLOW));node_opens++;return 201;}
  return -1;
}
static ssize_t fixture_read(int fd,void*buffer,size_t size){
  if(fd==100){size_t remain=strlen(document)-manifest_position;if(size>remain)size=remain;
    memcpy(buffer,document+manifest_position,size);manifest_position+=size;return (ssize_t)size;}
  if(fd==101){
    char boot[]=BOOT "\n";
    assert(size>=sizeof(boot)-1);
    boot_reads++;
    if(wrongboot)
      boot[0]='9';
    memcpy(buffer,boot,sizeof(boot)-1);
    return sizeof(boot)-1;
  }
  assert(false);return -1;
}
static int fixture_close(int fd){assert(fd>=100);return 0;}
static int fixture_fstat(int fd,struct stat*info){
  memset(info,0,sizeof(*info));
  if(fd==100){info->st_mode=S_IFREG|0440|(writable?0020:0);info->st_uid=wrong_owner?99:0;
    info->st_nlink=linked?2:1;info->st_size=(off_t)strlen(document);info->st_dev=11;info->st_ino=42;return 0;}
  if(fd==200){info->st_mode=S_IFDIR|0555;info->st_dev=23;info->st_ino=(replaced_sysfs&&sysfs_opens>1)?778:777;return 0;}
  if(fd==201){info->st_mode=S_IFCHR|0660;info->st_dev=44;info->st_ino=(replaced_node&&node_opens>1)?889:888;info->st_rdev=999;return 0;}
  if(fd==7 || fd==8){info->st_mode=S_IFCHR|0660;info->st_dev=44;info->st_ino=wrong_pcm_fd||fd==8?9999:888;info->st_rdev=999;return 0;}
  if(fd==9){info->st_mode=S_IFSOCK;return 0;}
  return -1;
}
static int fixture_fstatfs(int fd,struct statfs*info){assert(fd==200);info->f_type=wrongfs?1:SYSFS_MAGIC;return 0;}
static int snd_pcm_info_malloc(snd_pcm_info_t**p){if(info_malloc_fails){*p=NULL;return -ENOMEM;}*p=calloc(1,sizeof(**p));info_allocations++;return *p?0:-ENOMEM;}
static void snd_pcm_info_free(snd_pcm_info_t*p){assert(p);free(p);info_allocations--;}
static int snd_pcm_info(snd_pcm_t*p,snd_pcm_info_t*i){(void)p;(void)i;return 0;}
static int snd_pcm_info_get_card(snd_pcm_info_t*i){(void)i;return opaque_info?-1:bad_card?8:7;}
static unsigned snd_pcm_info_get_device(snd_pcm_info_t*i){(void)i;return bad_device?2:1;}
static unsigned snd_pcm_info_get_subdevice(snd_pcm_info_t*i){(void)i;return bad_subdevice?0:7;}
static int snd_pcm_info_get_stream(snd_pcm_info_t*i){(void)i;return bad_stream?1:0;}
static int snd_pcm_poll_descriptors_count(snd_pcm_t*p){(void)p;return missing_pcm_fd?1:extra_wrong_pcm?2:1;}
static int snd_pcm_poll_descriptors(snd_pcm_t*p,struct pollfd*d,unsigned count){
  (void)p;if(fd_query_fails)return -EINVAL;d[0].fd=missing_pcm_fd?9:7;if(count>1)d[1].fd=8;return (int)count;
}
#define open fixture_open
#define read fixture_read
#define close fixture_close
#define fstat fixture_fstat
#define fstatfs fixture_fstatfs
#include "pcm_identity.h"
#undef open
#undef read
#undef close
#undef fstat
#undef fstatfs
static int snd_pcm_open(snd_pcm_t**p,const char*n,int stream,int flags){
  assert(stream==0&&flags==0);opened_name=n;if(pcm_busy)return -EBUSY;*p=&pcm_handle;return 0;
}
static int snd_pcm_close(snd_pcm_t*p){assert(p==&pcm_handle);closed++;return 0;}
static const char *snd_strerror(int status){(void)status;return "mock ALSA error";}
static int snd_pcm_hw_params_malloc(snd_pcm_hw_params_t**p){params++;if(hw_malloc_fails){*p=NULL;return -ENOMEM;}*p=calloc(1,sizeof(**p));return *p?0:-ENOMEM;}
static void snd_pcm_hw_params_free(snd_pcm_hw_params_t*p){assert(p);free(p);}
static int snd_pcm_hw_params_any(snd_pcm_t*p,snd_pcm_hw_params_t*q){(void)p;(void)q;return hw_any_fails?-EINVAL:0;}
static int snd_pcm_hw_params_set_access(snd_pcm_t*p,snd_pcm_hw_params_t*q,int v){(void)p;(void)q;(void)v;return 0;}
static int snd_pcm_hw_params_set_format(snd_pcm_t*p,snd_pcm_hw_params_t*q,int v){(void)p;(void)q;(void)v;return 0;}
static int snd_pcm_hw_params_set_channels(snd_pcm_t*p,snd_pcm_hw_params_t*q,unsigned v){(void)p;(void)q;(void)v;return 0;}
static int snd_pcm_hw_params_set_rate(snd_pcm_t*p,snd_pcm_hw_params_t*q,unsigned v,int d){(void)p;(void)q;(void)v;(void)d;return 0;}
static int snd_pcm_hw_params_get_buffer_size_max(snd_pcm_hw_params_t*p,snd_pcm_uframes_t*v){(void)p;*v=96000;return 0;}
static int snd_pcm_hw_params_set_buffer_size_max(snd_pcm_t*p,snd_pcm_hw_params_t*q,snd_pcm_uframes_t*v){(void)p;(void)q;(void)v;return 0;}
static int snd_pcm_hw_params(snd_pcm_t*p,snd_pcm_hw_params_t*q){(void)p;(void)q;return 0;}
static int bps2format(unsigned n){return (int)n;}

/* @ACTUAL_PCM_OPEN@ */

static int checked_open(snd_pcm_t**pcm,const char*name,struct media_quality*q,const char*manifest){
#ifdef PREIMAGE
  (void)manifest;return pcm_open(pcm,name,q);
#else
  return pcm_open(pcm,name,q,manifest);
#endif
}
static void reset(void){
  strcpy(document,valid_json);manifest_position=0;sysfs_opens=node_opens=boot_reads=params=closed=info_allocations=0;
  wrong_owner=writable=linked=symlinked=wrongfs=wrongboot=replaced_sysfs=replaced_node=wrong_pcm_fd=false;
  bad_card=bad_device=bad_subdevice=bad_stream=opaque_info=missing_pcm_fd=info_malloc_fails=false;
  hw_malloc_fails=hw_any_fails=extra_wrong_pcm=fd_query_fails=pcm_busy=false;
}
static void deny(void){
  snd_pcm_t*pcm=NULL;struct media_quality quality={48000,16,2};
  assert(checked_open(&pcm,"plughw:CARD=Loopback,DEV=1,SUBDEV=7",&quality,MANIFEST)==ALSA_ERROR_IDENTITY);
  assert(!pcm&&!params&&closed==1&&!info_allocations);
}
int main(void){
  snd_pcm_t*pcm=NULL;struct media_quality quality={48000,16,2};reset();
#ifdef PREIMAGE
  bad_card=wrong_pcm_fd=true;
  if(checked_open(&pcm,"plughw:CARD=Loopback,DEV=1,SUBDEV=7",&quality,MANIFEST)==0 && params){
    fputs("actual preimage opened the wrong physical PCM and configured it before any identity guard\n",stderr);return 1;
  }
  return 2;
#else
  assert(checked_open(&pcm,"plughw:CARD=Loopback,DEV=1,SUBDEV=7",&quality,MANIFEST)==0&&pcm&&params==1&&!closed);
  assert(!strcmp(opened_name,"plughw:CARD=Loopback,DEV=1,SUBDEV=7")&&sysfs_opens==2&&node_opens==2&&boot_reads==2);
  bool*failures[]={&wrong_owner,&writable,&linked,&symlinked,&wrongfs,&wrongboot,&replaced_sysfs,&replaced_node,&wrong_pcm_fd,
                  &bad_card,&bad_device,&bad_subdevice,&bad_stream,&opaque_info,&missing_pcm_fd,&info_malloc_fails,&extra_wrong_pcm,&fd_query_fails};
  for(size_t i=0;i<sizeof(failures)/sizeof(*failures);i++){reset();*failures[i]=true;deny();}
  reset();snprintf(document,sizeof(document),"%s garbage",valid_json);deny();
  reset();strcpy(document,"{}");deny();
  reset();memset(document,' ',4097);document[4097]=0;deny();
  reset();char*at=strstr(document,"\"version\":1");assert(at);at[10]='2';deny();
  reset();snprintf(document,sizeof(document),"{\"version\":1,%s",valid_json+1);deny(); /* duplicated key */
  reset();struct json_object*edited=json_tokener_parse(document);assert(edited);
  json_object_object_add(edited,"card_index",json_object_new_boolean(true));
  snprintf(document,sizeof(document),"%s",json_object_to_json_string_ext(edited,JSON_C_TO_STRING_PLAIN));
  json_object_put(edited);deny();
  reset();edited=json_tokener_parse(document);assert(edited);
  json_object_object_add(edited,"node_st_rdev",json_object_new_uint64(UINT64_MAX));
  snprintf(document,sizeof(document),"%s",json_object_to_json_string_ext(edited,JSON_C_TO_STRING_PLAIN));
  json_object_put(edited);deny();
  reset();assert(checked_open(&pcm,"plughw:CARD=Loopback,DEV=1,SUBDEV=7",&quality,MANIFEST)==0);
  params=closed=0;bad_card=true;deny(); /* A later successful PCM reopen is guarded again. */
  reset();hw_malloc_fails=true;pcm=NULL;
  assert(checked_open(&pcm,"hw:CARD=Loopback,DEV=1,SUBDEV=7",&quality,MANIFEST)==ALSA_ERROR_DEVICE&&closed==1&&params==1&&!pcm);
  reset();hw_any_fails=true;pcm=NULL;
  assert(checked_open(&pcm,"hw:CARD=Loopback,DEV=1,SUBDEV=7",&quality,MANIFEST)==ALSA_ERROR_DEVICE&&closed==1&&!pcm);
  reset();pcm_busy=true;assert(checked_open(&pcm,"hw:CARD=Loopback",&quality,MANIFEST)==ALSA_ERROR_DEVICE_BUSY&&!params&&!closed);
  reset();bad_card=true;pcm=NULL;assert(checked_open(&pcm,"default",&quality,NULL)==0&&pcm&&params==1&&!sysfs_opens); /* opt-in, legacy unchanged */
  puts("actual PCM guard/parser/open seam: fail-closed parser/identity cases, actual plughw preserved, hotplug inode/rdev/card/stream/poll/sysfs/boot fences, NULL hw_params cleanup, legacy opt-out; original wrong-PCM preimage rejected");
  return 0;
#endif
}

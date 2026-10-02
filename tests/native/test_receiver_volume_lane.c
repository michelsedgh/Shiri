/* Exact receiver lane callbacks; native source state is owned by PCM lock. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <errno.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#ifndef SOCK_NONBLOCK
#define SOCK_NONBLOCK 0
#define SOCK_CLOEXEC 0
#endif
#include "shiri_volume_control.h"
#include "shiri_volume_io.h"
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static struct shiri_pcm_packet state;
static int peer_uid,fd=-1,connection=-1;
static struct { double airplay_volume; } config;
static int notifications;
int shiri_ap2_volume(int number,unsigned int percent,uint64_t revision,shiri_volume_fence fence,void *context) {
  assert(number==connection && percent<=100 && revision && fence(context)==1);notifications++;return SHIRI_VOLUME_OK;
}
#include "shiri_volume_lane.inc"
int main(void) {
  struct shiri_volume_message message={0}; uint64_t start;
  peer_uid=(int)getuid();
  config.airplay_volume=-23.1;
  assert(!volume_init("/tmp/receiver-volume-test-nonexistent.sock"));
  assert(fabs(room_volume()+23.1)<1e-10);
  memset(volume_incarnation,1,16);volume_revision=1;
  message.kind=SHIRI_VOLUME_SET;memcpy(message.incarnation,volume_incarnation,16);message.revision=2;message.volume=0;
  assert(volume_command(&message)==SHIRI_VOLUME_OK && room_volume()==-30 && !notifications);
  message.revision=1;message.volume=100;assert(volume_command(&message)==SHIRI_VOLUME_STALE && room_volume()==-30);
  message.revision=3;message.volume=23;message.incarnation[0]=2;
  assert(volume_command(&message)==SHIRI_VOLUME_STALE && room_volume()==-30);message.incarnation[0]=1;
  fd=19;connection=12;memset(state.session,2,16);memcpy(state.incarnation,volume_incarnation,16);state.epoch=7;state.generation=4;
  assert(volume_command(&message)==SHIRI_VOLUME_STALE); // idle command cannot race a pending BEGIN
  memcpy(message.session,state.session,16);message.epoch=7;message.generation=4;message.notify=1;
  assert(volume_command(&message)==SHIRI_VOLUME_OK && notifications==1 && fabs(room_volume()+23.1)<1e-10);
  state.generation=5;message.volume=100;message.revision=4;
  assert(volume_command(&message)==SHIRI_VOLUME_STALE && notifications==1 && fabs(room_volume()+23.1)<1e-10);
  message.generation=5;message.notify=0;assert(volume_command(&message)==SHIRI_VOLUME_OK && notifications==1 && fabs(room_volume())<1e-10);
  pthread_mutex_lock(&lock);assert(volume_fence(&message)==-1);pthread_mutex_unlock(&lock);
  state.session[0]=3;assert(!volume_fence(&message));
  start=shiri_volume_now();volume_deinit();assert(shiri_volume_now()-start<UINT64_C(1500000000));
  assert(!volume_started && !volume_socket_path && !volume_fence(&message));
  puts("receiver-volume-lane: worker incarnation/revision, idle/BEGIN/active generation fences, default/mute, notification origin and bounded joined shutdown PASS");
  return 0;
}

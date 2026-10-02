/* Exact native receiver-volume transport with real Unix sockets. Pair/plist
 * seams exercise framing, ownership and failure; crypto itself is upstream. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#include "player.h"
#include "shiri_volume_control.h"
#include "shiri_volume_io.h"
rtsp_conn_info *principal_conn;
pthread_rwlock_t principal_conn_lock=PTHREAD_RWLOCK_INITIALIZER;
static int allocations,fail_allocation=-1, cipher_encrypts;
struct node { struct node *child[6]; int children; char *text; double value; };
static plist_t node(void) { struct node *p; if (fail_allocation==allocations++) return NULL; p=calloc(1,sizeof(*p)); assert(p); return p; }
plist_t plist_new_dict(void) { return node(); }
plist_t plist_new_string(const char *s) { struct node *p=node(); if(p) p->text=strdup(s); return p; }
plist_t plist_new_real(double value) { struct node *p=node(); if(p)p->value=value; return p; }
void plist_dict_set_item(plist_t p,const char *key,plist_t child) { (void)key; assert(p && child && p->children<6); p->child[p->children++]=child; }
void plist_free(plist_t p) { int i; if(!p)return; for(i=0;i<p->children;i++)plist_free(p->child[i]); free(p->text); free(p); }
void plist_to_bin(plist_t p,char **out,uint32_t *length) {
  char temp[200]; int n;
  assert(p->children==4 && !strcmp(p->child[0]->text,"sendMediaRemoteCommand") && !strcmp(p->child[1]->text,"dvlc"));
  assert(p->child[2]->value>=0 && p->child[2]->value<=1 && p->child[3]->child[0]->value==p->child[2]->value);
  n=snprintf(temp,sizeof(temp),"bplist00 sendMediaRemoteCommand dvlc %.2f",p->child[2]->value);
  *out=strdup(temp); *length=(uint32_t)n;
}
static int decrypt_calls,fail_authentication;
static size_t framed(uint8_t *out,const uint8_t *in,size_t length) {
  assert(length>0 && length<=1024);out[0]=(uint8_t)length;out[1]=(uint8_t)(length>>8);
  memcpy(out+2,in,length);memset(out+2+length,0x5a,16);return length+18;
}
ssize_t pair_encrypt(uint8_t **out,size_t *length,const uint8_t *in,size_t n,struct pair_cipher_context *ctx) {
  (void)ctx;cipher_encrypts++;*out=malloc(n+18);assert(*out);*length=framed(*out,in,n);return (ssize_t)n;
}
ssize_t pair_decrypt(uint8_t **out,size_t *length,const uint8_t *in,size_t n,struct pair_cipher_context *ctx) {
  size_t at=0,count=0;(void)ctx;decrypt_calls++;assert(n>=2);*out=malloc(n);assert(*out);*length=0;
  if(fail_authentication) { free(*out);return -1; } // Actual pinned nonNULL freed output.
  while(at<n) {
    assert(n-at>=2);size_t block=(size_t)in[at]|((size_t)in[at+1]<<8);
    assert(block>0 && block<=1024 && n-at>=block+18);
    for(size_t i=at+2+block;i<at+block+18;i++)assert(in[i]==0x5a);
    memcpy(*out+count,in+at+2,block);count+=block;at+=block+18;
  }
  *length=count;return (ssize_t)at;
}
#include "shiri_ap2_volume.c"
static int live=1,remaining_calls=-1;
static int fence(void *unused) { (void)unused; if(remaining_calls==0)return 0;if(remaining_calls>0)remaining_calls--;return live; }
struct server_args { int fd; const char *reply; int fragment; int close_after_read; };
static void *server(void *arg) {
  struct server_args *s=arg; uint8_t data[5000],reply[5000]; size_t count=0,need=0,i; ssize_t n;
  do { n=recv(s->fd,data+count,sizeof(data)-count,0); if(n<=0)goto done; count+=(size_t)n;
       if(count>=2)need=((size_t)data[0]|((size_t)data[1]<<8))+18;
  } while (!need || count<need);
  assert(count==need && memmem(data+2,count-18,"POST /command RTSP/1.0",21));
  assert(memmem(data+2,count-18,"sendMediaRemoteCommand dvlc",26));
  if(s->close_after_read)goto done;
  need=framed(reply,(const uint8_t *)s->reply,strlen(s->reply));
  for(i=0;i<need;) { size_t chunk=s->fragment ? 1 : need-i; n=send(s->fd,reply+i,chunk,MSG_NOSIGNAL); assert(n==(ssize_t)chunk); i+=chunk; if(s->fragment)usleep(500); }
done: close(s->fd); return NULL;
}
static rtsp_conn_info connection;
static struct pair_cipher_context cipher;
static void setup(int descriptor) {
  memset(&connection,0,sizeof(connection)); pthread_mutex_init(&connection.event_sender_mutex,NULL);
  connection.connection_number=12; connection.airplay_type=ap_2; connection.event_channel_fd=descriptor;
  connection.ap2_pairing_context.event_cipher_bundle.cipher_ctx=&cipher; principal_conn=&connection; live=1;remaining_calls=-1; allocations=0; fail_allocation=-1;
}
static void cleanup(void) {
  if(connection.event_channel_fd>0)close(connection.event_channel_fd);
  free(connection.ap2_pairing_context.event_cipher_bundle.encrypted_read_buffer.data);
  pthread_mutex_destroy(&connection.event_sender_mutex); principal_conn=NULL;
}
static void response_cases(void) {
  const char *good="RTSP/1.0 200 OK\r\nCSeq: 7\r\nContent-Length: 3\r\n\r\nabc"; size_t i;
  for(i=0;i<strlen(good);i++)assert(shiri_volume_response((const uint8_t *)good,i,7)==0);
  assert(shiri_volume_response((const uint8_t *)good,strlen(good),7)==1);
  assert(shiri_volume_response((const uint8_t *)good,strlen(good),8)==-1);
  const char binary[]="RTSP/1.0 200 OK\r\nContent-Length: 3\r\n\r\n\xff\0x";
  assert(shiri_volume_response((const uint8_t *)binary,sizeof(binary)-1,7)==1);
  assert(shiri_volume_response((const uint8_t *)binary,sizeof(binary)-2,7)==0);
  const char *bad[]={"RTSP/1.0 500 Error\r\n\r\n","RTSP/1.0 200 OK\r\nContent-Length: 999999\r\n\r\n","RTSP/1.0 200 OK\r\nCSeq: 184467440737095516160\r\n\r\n","RTSP/1.0 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n","RTSP/1.0 200 OK\r\nBad Header\r\n\r\n","RTSP/1.0 200 OK\r\n\r\nexcess"};
  for(i=0;i<sizeof(bad)/sizeof(*bad);i++)assert(shiri_volume_response((const uint8_t *)bad[i],strlen(bad[i]),7)==-1);
}
int main(void) {
  int sockets[2],ret,i; pthread_t thread; uint64_t start;
  struct shiri_volume_message message={0},decoded; uint8_t bytes[SHIRI_VOLUME_BYTES];
  message.kind=SHIRI_VOLUME_SET; message.revision=7; message.volume=42;
  shiri_volume_encode(bytes,&message); assert(!shiri_volume_decode(&decoded,bytes,sizeof(bytes))); assert(decoded.volume==42);
  bytes[72]=1; assert(shiri_volume_decode(&decoded,bytes,sizeof(bytes))); bytes[72]=0;
  bytes[24]=1; assert(shiri_volume_decode(&decoded,bytes,sizeof(bytes))); bytes[24]=0;
  assert(shiri_volume_decode(&decoded,bytes,sizeof(bytes)-1)); response_cases();
  for(i=0;i<3;i++) {
    assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets)); setup(sockets[0]);
    struct server_args args={sockets[1],i==2 ? "RTSP/1.0 500 Error\r\n\r\n" : "RTSP/1.0 200 OK\r\nCSeq: 7\r\nContent-Length: 0\r\n\r\n",i==1,0};
    assert(!pthread_create(&thread,NULL,server,&args));
    ret=shiri_ap2_volume(12,42,7,fence,NULL);
    assert(ret==(i==2 ? SHIRI_VOLUME_PROTOCOL : SHIRI_VOLUME_OK));
    assert(connection.shiri_event_volume_faulted==(i==2)); pthread_join(thread,NULL); cleanup();
  }
  assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets)); setup(sockets[0]);
  start=shiri_volume_now(); ret=shiri_ap2_volume(12,42,7,fence,NULL);
  assert(ret==SHIRI_VOLUME_TIMEOUT && shiri_volume_now()-start<UINT64_C(1500000000));
  assert(connection.shiri_event_volume_faulted); start=shiri_volume_now();
  assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_UNAVAILABLE && shiri_volume_now()-start<UINT64_C(100000000)); close(sockets[1]); cleanup();
  assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets)); setup(sockets[0]);
  struct server_args args={sockets[1],"",0,1}; assert(!pthread_create(&thread,NULL,server,&args));
  assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_IO); assert(connection.shiri_event_volume_faulted); pthread_join(thread,NULL);cleanup();
  assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets));setup(sockets[0]);remaining_calls=4;
  struct server_args stale_args={sockets[1],"RTSP/1.0 200 OK\r\n\r\n",0,0};
  assert(!pthread_create(&thread,NULL,server,&stale_args));
  assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_STALE);
  assert(connection.shiri_event_volume_faulted);pthread_join(thread,NULL);cleanup();
  setup(-1); assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_UNAVAILABLE); assert(!connection.shiri_event_volume_faulted);
  assert(shiri_ap2_volume(13,42,7,fence,NULL)==SHIRI_VOLUME_STALE); connection.airplay_type=ap_1;
  assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_UNSUPPORTED); cleanup();
  for(i=0;i<6;i++) { setup(-1);fail_allocation=i;assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_IO);cleanup(); }
  setup(-1);live=0;assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_STALE);cleanup();
  setup(-1);pthread_mutex_lock(&connection.event_sender_mutex);start=shiri_volume_now();
  assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_TIMEOUT);assert(shiri_volume_now()-start<UINT64_C(1500000000));
  pthread_mutex_unlock(&connection.event_sender_mutex);assert(!connection.shiri_event_volume_faulted);cleanup();
  puts("receiver-volume-records: pinned-format codec/parser, fragmented socket exchange, bounded deadline, EOF, channel retirement, source/unsupported fences and six allocation failures PASS");
  return 0;
}

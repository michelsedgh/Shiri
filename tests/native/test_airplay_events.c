#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <fcntl.h>
#include <time.h>
#include "airplay_events.c"

static unsigned checks, player_calls, status_calls, decrypt_calls;
static int playback_status = PLAY_PLAYING;
#define CHECK(v) do { checks++; assert(v); } while (0)
void player_get_status(struct player_status *s) { status_calls++; s->status=playback_status; }
void player_playback_pause(void) { player_calls++; }
void player_playback_start(void) { player_calls++; }
void player_playback_next(void) { player_calls++; }
void player_playback_prev(void) { player_calls++; }
struct pair_cipher_context *pair_cipher_new(int type,int direction,const uint8_t *key,size_t n) { (void)type;(void)direction;(void)key;(void)n;return calloc(1,sizeof(struct pair_cipher_context)); }
void pair_cipher_free(struct pair_cipher_context *c) { free(c); }
const char *pair_cipher_errmsg(struct pair_cipher_context *c) { (void)c;return "test cipher seam"; }
ssize_t pair_decrypt(uint8_t **out,size_t *length,const uint8_t *in,size_t n,struct pair_cipher_context *c) {
  size_t at=0,total=0,block,i; (void)c;decrypt_calls++;
  *out=malloc(n); CHECK(*out); *length=0;
  while(at<n) { CHECK(n-at>=2);block=in[at]|((size_t)in[at+1]<<8);CHECK(block>0 && block<=1024 && n-at>=block+18);
    for(i=at+2+block;i<at+block+18;i++) if(in[i]) { /* Match pinned pair_decrypt: allocation is already freed, pointer left dangling. */ free(*out);return -1; }
    memcpy(*out+total,in+at+2,block);total+=block;at+=block+18;
  }
  *length=total;return (ssize_t)at;
}
int pair_encrypt(uint8_t **out,size_t *length,const uint8_t *in,size_t n,struct pair_cipher_context *c) {
  size_t at=0,total=0,block; (void)c;*out=calloc(1,n+18*((n+1023)/1024));CHECK(*out);
  while(at<n) {block=n-at>1024?1024:n-at;(*out)[total]=(uint8_t)block;(*out)[total+1]=(uint8_t)(block>>8);memcpy(*out+total+2,in+at,block);total+=block+18;at+=block;}
  *length=total;return 0;
}
static uint8_t *fixture; static size_t fixture_len;
static uint8_t *request(plist_t p,const char *extra,size_t *length) {
  char *body=NULL;uint32_t n=0;char header[1000];int h;uint8_t *out;
  plist_to_bin(p,&body,&n);CHECK(body && n);h=snprintf(header,sizeof(header),"POST /command RTSP/1.0\r\nContent-Length: %u\r\nContent-Type: application/x-apple-binary-plist\r\n%s\r\n",n,extra?extra:"");CHECK(h>0&&(size_t)h<sizeof(header));
  out=malloc((size_t)h+n);CHECK(out);memcpy(out,header,h);memcpy(out+h,body,n);free(body);*length=(size_t)h+n;return out;
}
static plist_t command(const char *type,plist_t value) {plist_t p=plist_new_dict();plist_dict_set_item(p,"type",plist_new_string(type));if(value)plist_dict_set_item(p,"value",value);return p;}
static int parse(const uint8_t *bytes,size_t n,enum airplay_events expected) {enum airplay_events e;struct rtsp_message m;int r=rtsp_parse(&e,&m,bytes,n);if(!r)CHECK(e==expected&&m.datalen<=n);return r;}
static void bad_header(const char *header) {size_t n=strlen(header);enum airplay_events e;struct rtsp_message m;CHECK(rtsp_parse(&e,&m,(const uint8_t *)header,n)==-1);}
static void parser_cases(void) {
  size_t i,n;uint8_t *bytes;plist_t p;struct rtsp_message m;enum airplay_events e;
  for(i=0;i<fixture_len;i++)CHECK(rtsp_parse(&e,&m,fixture,i)==1);
  CHECK(parse(fixture,fixture_len,AIRPLAY_EVENT_UNKNOWN)==0);
  const char *values[]={"play","paus","nitm","pitm","dvlc"};
  enum airplay_events events[]={AIRPLAY_EVENT_PLAY,AIRPLAY_EVENT_PAUSE,AIRPLAY_EVENT_NEXT,AIRPLAY_EVENT_PREV,AIRPLAY_EVENT_UNKNOWN};
  for(i=0;i<5;i++){p=command("sendMediaRemoteCommand",plist_new_string(values[i]));bytes=request(p,"CSeq: 7\r\n",&n);CHECK(rtsp_parse(&e,&m,bytes,n)==0&&e==events[i]&&m.cseq==7&&m.datalen==n);plist_free(p);free(bytes);}
  for(i=0;i<6;i++) {plist_t value=i==0?NULL:i==1?plist_new_string("bad"):i==2?plist_new_uint(3):i==3?plist_new_array():i==4?plist_new_bool(1):plist_new_data("x",1);p=command("updateInfo",value);bytes=request(p,NULL,&n);CHECK(parse(bytes,n,AIRPLAY_EVENT_UNKNOWN)==-1);plist_free(p);free(bytes);}
  p=plist_new_array();bytes=request(p,NULL,&n);CHECK(parse(bytes,n,AIRPLAY_EVENT_UNKNOWN)==-1);plist_free(p);free(bytes);
  p=command("unknown",plist_new_dict());bytes=request(p,NULL,&n);CHECK(parse(bytes,n,AIRPLAY_EVENT_UNKNOWN)==-1);plist_free(p);free(bytes);
  p=command("updateInfo",plist_new_dict());plist_dict_set_item(p,"type",plist_new_uint(1));bytes=request(p,NULL,&n);CHECK(parse(bytes,n,AIRPLAY_EVENT_UNKNOWN)==-1);plist_free(p);free(bytes);
  const char *bad[]={
   "POST /wrong RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Type: application/x-apple-binary-plist\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 0\r\nContent-Type: application/x-apple-binary-plist\r\n\r\n",
   "POST /command RTSP/1.0\r\nContent-Length: -1\r\nContent-Type: application/x-apple-binary-plist\r\n\r\n",
   "POST /command RTSP/1.0\r\nContent-Length: 65537\r\nContent-Type: application/x-apple-binary-plist\r\n\r\n",
   "POST /command RTSP/1.0\r\nContent-Length: 184467440737095516160\r\nContent-Type: application/x-apple-binary-plist\r\n\r\n",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\nCSeq: 0\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\nCSeq: 18446744073709551616\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\nCSeq: 1\r\nCSeq: 1\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: text/xml\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\nContent-Type: application/x-apple-binary-plist\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\nBad Header: x\r\n\r\nx",
   "POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\nUnknown: x\ny\r\n\r\nx"
  };
  for(i=0;i<sizeof(bad)/sizeof(*bad);i++)bad_header(bad[i]);
  bytes=malloc(RTSP_EVENT_HEADERS_MAX);CHECK(bytes);memset(bytes,'x',RTSP_EVENT_HEADERS_MAX);CHECK(parse(bytes,RTSP_EVENT_HEADERS_MAX,AIRPLAY_EVENT_UNKNOWN)==-1);free(bytes);
  bytes=malloc(fixture_len);CHECK(bytes);memcpy(bytes,fixture,fixture_len);bytes[fixture_len-1]^=0x80;CHECK(parse(bytes,fixture_len,AIRPLAY_EVENT_UNKNOWN)==-1);free(bytes);
}
static struct airplay_events_client *client(int fd) {
  struct airplay_events_client *c=calloc(1,sizeof(*c));CHECK(c);c->fd=fd;c->name=strdup("isolated-test");c->incoming=evbuffer_new();c->pending=evbuffer_new();c->cipher_ctx=pair_cipher_new(0,0,NULL,0);CHECK(c->name&&c->incoming&&c->pending&&c->cipher_ctx);CHECK(!airplay_events_clients);airplay_events_clients=c;return c;
}
static void cipher_cases(void) {
  struct evbuffer *in=evbuffer_new(),*out=evbuffer_new();uint8_t *wire;size_t n,i;unsigned before;
  CHECK(!pair_encrypt(&wire,&n,fixture,fixture_len,NULL));
  for(i=0;i<n;i++){evbuffer_drain(in,(size_t)-1);evbuffer_drain(out,(size_t)-1);evbuffer_add(in,wire,i);before=decrypt_calls;CHECK(buffer_decrypt(out,in,NULL)==0);CHECK(evbuffer_get_length(in)<=1042);if(i<1042)CHECK(decrypt_calls==before);}
  evbuffer_drain(in,(size_t)-1);evbuffer_drain(out,(size_t)-1);evbuffer_add(in,wire,n);CHECK(buffer_decrypt(out,in,NULL)==0&&evbuffer_get_length(in)==0&&evbuffer_get_length(out)==fixture_len);CHECK(!memcmp(evbuffer_pullup(out,-1),fixture,fixture_len));
  wire[n-1]=1;evbuffer_add(in,wire,n);CHECK(buffer_decrypt(out,in,NULL)==-1);free(wire);
  const uint8_t bad[][2]={{0,0},{1,4},{255,255}};
  for(i=0;i<3;i++){evbuffer_drain(in,(size_t)-1);evbuffer_add(in,bad[i],2);before=decrypt_calls;CHECK(buffer_decrypt(out,in,NULL)==-1&&decrypt_calls==before);}
  evbuffer_free(in);evbuffer_free(out);
}
static size_t receive_plain(int fd,uint8_t *plain,size_t capacity) {
  uint8_t wire[10000],*out;size_t total=0,n,length;ssize_t got;
  while((got=recv(fd,wire+total,sizeof(wire)-total,MSG_DONTWAIT))>0)total+=(size_t)got;
  CHECK(total>0);n=(size_t)pair_decrypt(&out,&length,wire,total,NULL);CHECK(n==total&&length<=capacity);memcpy(plain,out,length);free(out);return length;
}
static void socket_cases(void) {
  size_t i,n,length;int s[2];uint8_t *wire,plain[10000];unsigned before;struct airplay_events_client *c;
  CHECK(!pair_encrypt(&wire,&n,fixture,fixture_len,NULL));
  /* Every encrypted byte split invokes the actual callback and real evbuffer. */
  for(i=1;i<n;i++){CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,s));c=client(s[0]);before=player_calls;CHECK(send(s[1],wire,i,0)==(ssize_t)i);incoming_cb(s[0],EV_READ,c);CHECK(airplay_events_clients==c&&player_calls==before);CHECK(recv(s[1],plain,sizeof(plain),MSG_DONTWAIT)==-1&&errno==EAGAIN);CHECK(send(s[1],wire+i,n-i,0)==(ssize_t)(n-i));incoming_cb(s[0],EV_READ,c);CHECK(airplay_events_clients==c&&evbuffer_get_length(c->pending)==0&&player_calls==before);length=receive_plain(s[1],plain,sizeof(plain)-1);plain[length]=0;CHECK(strstr((char *)plain,"RTSP/1.0 200 OK\r\n")&& !strstr((char *)plain,"CSeq"));client_remove(c);close(s[0]);close(s[1]);}
  free(wire);
  const char *sequences[]={"CSeq: 2147483648\r\n", "CSeq: 18446744073709551615\r\n"};
  for(i=0;i<2;i++) {plist_t metadata=command("updateInfo",plist_new_dict());size_t m;uint8_t *r=request(metadata,sequences[i],&m);plist_free(metadata);CHECK(!pair_encrypt(&wire,&n,r,m,NULL));CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,s));c=client(s[0]);unsigned mutations=player_calls, statuses=status_calls;CHECK(send(s[1],wire,n,0)==(ssize_t)n);incoming_cb(s[0],EV_READ,c);CHECK(airplay_events_clients==c&&player_calls==mutations&&status_calls==statuses);length=receive_plain(s[1],plain,sizeof(plain)-1);plain[length]=0;CHECK(strstr((char *)plain,sequences[i]));client_remove(c);close(s[0]);close(s[1]);free(r);free(wire);}
  /* Coalesced metadata + two actual commands each drain and ACK exactly once. */
  plist_t p=command("sendMediaRemoteCommand",plist_new_string("play"));size_t a,b;uint8_t *one=request(p,"CSeq: 9\r\n",&a);plist_free(p);p=command("sendMediaRemoteCommand",plist_new_string("nitm"));uint8_t *two=request(p,"CSeq: 10\r\n",&b);plist_free(p);
  uint8_t *all=malloc(fixture_len+a+b);CHECK(all);memcpy(all,fixture,fixture_len);memcpy(all+fixture_len,one,a);memcpy(all+fixture_len+a,two,b);CHECK(!pair_encrypt(&wire,&n,all,fixture_len+a+b,NULL));CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,s));c=client(s[0]);before=player_calls;unsigned statuses=status_calls;CHECK(send(s[1],wire,n,0)==(ssize_t)n);incoming_cb(s[0],EV_READ,c);CHECK(airplay_events_clients==c&&player_calls==before+2&&status_calls==statuses+2&&evbuffer_get_length(c->pending)==0);length=receive_plain(s[1],plain,sizeof(plain)-1);plain[length]=0;char *reply=(char *)plain;unsigned replies=0;while((reply=strstr(reply,"RTSP/1.0 200 OK"))){replies++;reply++;}CHECK(replies==3&&strstr((char *)plain,"CSeq: 9\r\n")&&strstr((char *)plain,"CSeq: 10\r\n"));client_remove(c);close(s[0]);close(s[1]);free(all);free(one);free(two);free(wire);
  /* Malformed complete request fails the channel; never ACKs/player-mutates. */
  const char *bad="POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\n\r\nx";CHECK(!pair_encrypt(&wire,&n,(const uint8_t *)bad,strlen(bad),NULL));CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,s));c=client(s[0]);before=player_calls;CHECK(send(s[1],wire,n,0)==(ssize_t)n);incoming_cb(s[0],EV_READ,c);CHECK(!airplay_events_clients&&player_calls==before&&recv(s[1],plain,sizeof(plain),0)==0);close(s[0]);close(s[1]);free(wire);
  /* An ACK peer refusing reads cannot block the source event thread. */
  CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,s));c=client(s[0]);memset(plain,1,sizeof(plain));while(send(s[0],plain,sizeof(plain),MSG_DONTWAIT)>0){}CHECK(errno==EAGAIN);CHECK(!pair_encrypt(&wire,&n,fixture,fixture_len,NULL));CHECK(send(s[1],wire,n,0)==(ssize_t)n);struct timespec start,end;clock_gettime(CLOCK_MONOTONIC,&start);incoming_cb(s[0],EV_READ,c);clock_gettime(CLOCK_MONOTONIC,&end);double elapsed=end.tv_sec-start.tv_sec+(end.tv_nsec-start.tv_nsec)/1e9;CHECK(!airplay_events_clients&&elapsed<0.1);close(s[0]);close(s[1]);free(wire);
  /* EOF while a fragment is pending frees the exact client and its buffers. */
  CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,s));c=client(s[0]);CHECK(send(s[1],"x",1,0)==1);incoming_cb(s[0],EV_READ,c);CHECK(airplay_events_clients==c);close(s[1]);incoming_cb(s[0],EV_READ,c);CHECK(!airplay_events_clients);close(s[0]);
}
int main(int argc,char **argv) {
  CHECK(argc==2);FILE *f=fopen(argv[1],"rb");CHECK(f);CHECK(!fseek(f,0,SEEK_END));long n=ftell(f);CHECK(n>0);CHECK(!fseek(f,0,SEEK_SET));fixture=malloc((size_t)n);CHECK(fixture);CHECK(fread(fixture,1,(size_t)n,f)==(size_t)n);fclose(f);fixture_len=(size_t)n;
  parser_cases();cipher_cases();socket_cases();free(fixture);printf("event1: %u checks PASS; actual-C/libplist/libevent parser/callback, every encrypted split, metadata noop, playback/CSeq, malformed bounds, coalescing, EOF/backpressure; crypto is an explicit upstream seam\n",checks);return 0;
}

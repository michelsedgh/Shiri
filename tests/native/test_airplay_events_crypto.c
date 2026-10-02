/* Actual pinned pair_ap + libgcrypt/libsodium, exact production event callback.
 * Only unrelated player/service actors are inert. No TCP/device actors. */
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <fcntl.h>
#include <gcrypt.h>
#include "airplay_events.c"
#include "pair_ap/pair-internal.h"
static unsigned checks,player_calls,status_calls;
#define CHECK(v) do {checks++;assert(v);} while(0)
void player_get_status(struct player_status *s){status_calls++;s->status=PLAY_PLAYING;}
void player_playback_pause(void){player_calls++;}
void player_playback_start(void){player_calls++;}
void player_playback_next(void){player_calls++;}
void player_playback_prev(void){player_calls++;}
static const uint8_t shared_secret[32]={1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32};
static struct pair_cipher_context *receiver(void){struct pair_cipher_context *c=pair_cipher_new(PAIR_CLIENT_HOMEKIT_NORMAL,1,shared_secret,sizeof(shared_secret));CHECK(c);return c;}
static struct pair_cipher_context *sender(void){struct pair_cipher_context *c=pair_cipher_new(PAIR_SERVER_HOMEKIT,3,shared_secret,sizeof(shared_secret));CHECK(c);return c;}
static struct airplay_events_client *client(int fd,struct pair_cipher_context *cipher){struct airplay_events_client *c=calloc(1,sizeof(*c));CHECK(c&&!airplay_events_clients);c->fd=fd;c->cipher_ctx=cipher;c->name=strdup("isolated-real-crypto-test");c->incoming=evbuffer_new();c->pending=evbuffer_new();CHECK(c->name&&c->incoming&&c->pending);airplay_events_clients=c;return c;}
static void buffer_crypto(const uint8_t *message,size_t length){
  struct pair_cipher_context *s=sender(),*r;uint8_t *wire;size_t n,i;struct evbuffer *in=evbuffer_new(),*out=evbuffer_new();CHECK(in&&out);CHECK(pair_encrypt(&wire,&n,message,length,s)==(ssize_t)length);CHECK(n>1042);
  for(i=0;i<n;i++){
    r=receiver();evbuffer_drain(in,(size_t)-1);evbuffer_drain(out,(size_t)-1);evbuffer_add(in,wire,i);
    CHECK(buffer_decrypt(out,in,r)==0);CHECK(r->decryption_counter==(i>=1042?1:0));
    evbuffer_add(in,wire+i,n-i);CHECK(buffer_decrypt(out,in,r)==0);CHECK(evbuffer_get_length(in)==0&&evbuffer_get_length(out)==length);CHECK(!memcmp(evbuffer_pullup(out,-1),message,length));pair_cipher_free(r);
  }
  /* Auth failure in the second record follows a successfully decoded first.
   * Pinned pair_decrypt frees *plaintext without nulling it and rolls counters
   * back. The production caller must never free that dangling error pointer. */
  r=receiver();evbuffer_drain(in,(size_t)-1);evbuffer_drain(out,(size_t)-1);wire[n-1]^=1;evbuffer_add(in,wire,n);CHECK(buffer_decrypt(out,in,r)==-1);CHECK(r->decryption_counter==0&&evbuffer_get_length(in)==n&&evbuffer_get_length(out)==0);
  wire[n-1]^=1;evbuffer_drain(in,(size_t)-1);evbuffer_add(in,wire,n);CHECK(buffer_decrypt(out,in,r)==0&&r->decryption_counter==2&&evbuffer_get_length(out)==length);CHECK(!memcmp(evbuffer_pullup(out,-1),message,length));pair_cipher_free(r);
  r=receiver();evbuffer_drain(in,(size_t)-1);evbuffer_drain(out,(size_t)-1);evbuffer_add(in,"\0\0",2);CHECK(buffer_decrypt(out,in,r)==-1&&r->decryption_counter==0);pair_cipher_free(r);
  free(wire);pair_cipher_free(s);evbuffer_free(in);evbuffer_free(out);
}
static void callback_crypto(const uint8_t *message,size_t length){
  size_t i,n;uint8_t *wire,reply[5000],*plain;size_t plain_len;int fd[2];struct pair_cipher_context *s;struct airplay_events_client *c;
  for(i=1;i<length+36;i++){
    s=sender();CHECK(pair_encrypt(&wire,&n,message,length,s)==(ssize_t)length);CHECK(n==length+36&&i<n);CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,fd));c=client(fd[0],receiver());unsigned mutations=player_calls,statuses=status_calls;
    CHECK(send(fd[1],wire,i,0)==(ssize_t)i);incoming_cb(fd[0],EV_READ,c);CHECK(airplay_events_clients==c&&player_calls==mutations&&status_calls==statuses);CHECK(recv(fd[1],reply,sizeof(reply),MSG_DONTWAIT)==-1&&errno==EAGAIN);
    CHECK(send(fd[1],wire+i,n-i,0)==(ssize_t)(n-i));incoming_cb(fd[0],EV_READ,c);CHECK(airplay_events_clients==c&&evbuffer_get_length(c->incoming)==0&&evbuffer_get_length(c->pending)==0&&player_calls==mutations&&status_calls==statuses);ssize_t got=recv(fd[1],reply,sizeof(reply),0);CHECK(got>0);plain=NULL;plain_len=0;CHECK(pair_decrypt(&plain,&plain_len,reply,(size_t)got,s)==got);CHECK(plain_len>0&&plain_len<sizeof(reply));memcpy(reply,plain,plain_len);reply[plain_len]=0;free(plain);CHECK(strstr((char *)reply,"RTSP/1.0 200 OK\r\n"));client_remove(c);close(fd[0]);close(fd[1]);pair_cipher_free(s);free(wire);
  }
  /* Corrupted real authenticated request retires only its exact event client. */
  s=sender();CHECK(pair_encrypt(&wire,&n,message,length,s)==(ssize_t)length);wire[n-1]^=1;CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,fd));c=client(fd[0],receiver());unsigned before=player_calls;CHECK(send(fd[1],wire,n,0)==(ssize_t)n);incoming_cb(fd[0],EV_READ,c);CHECK(!airplay_events_clients&&player_calls==before&&recv(fd[1],reply,sizeof(reply),0)==0);close(fd[0]);close(fd[1]);pair_cipher_free(s);free(wire);
  /* A full-width worker revision is not truncated in the genuine cipher ACK. */
  const char *header="POST /command RTSP/1.0\r\nContent-Type: application/x-apple-binary-plist\r\nContent-Length: 1282\r\nCSeq: 18446744073709551615\r\n\r\n";const uint8_t *body=(const uint8_t *)strstr((const char *)message,"\r\n\r\n")+4;size_t h=strlen(header);uint8_t *request=malloc(h+1282);CHECK(request);memcpy(request,header,h);memcpy(request+h,body,1282);s=sender();CHECK(pair_encrypt(&wire,&n,request,h+1282,s)==(ssize_t)(h+1282));CHECK(!socketpair(AF_UNIX,SOCK_STREAM,0,fd));c=client(fd[0],receiver());CHECK(send(fd[1],wire,n,0)==(ssize_t)n);incoming_cb(fd[0],EV_READ,c);CHECK(airplay_events_clients==c);ssize_t got=recv(fd[1],reply,sizeof(reply),0);CHECK(got>0);plain=NULL;CHECK(pair_decrypt(&plain,&plain_len,reply,(size_t)got,s)==got);CHECK(plain_len<sizeof(reply));memcpy(reply,plain,plain_len);reply[plain_len]=0;free(plain);CHECK(strstr((char *)reply,"CSeq: 18446744073709551615\r\n"));client_remove(c);close(fd[0]);close(fd[1]);pair_cipher_free(s);free(wire);free(request);
}
int main(int argc,char **argv){
  CHECK(argc==2);CHECK(gcry_check_version(GCRYPT_VERSION));gcry_control(GCRYCTL_DISABLE_SECMEM,0);gcry_control(GCRYCTL_INITIALIZATION_FINISHED,0);
  FILE *f=fopen(argv[1],"rb");CHECK(f);CHECK(!fseek(f,0,SEEK_END));long length=ftell(f);CHECK(length==1378);CHECK(!fseek(f,0,SEEK_SET));uint8_t *message=calloc(1,(size_t)length+1);CHECK(message);CHECK(fread(message,1,(size_t)length,f)==(size_t)length);fclose(f);buffer_crypto(message,(size_t)length);callback_crypto(message,(size_t)length);free(message);printf("event1 real pair_ap: %u checks PASS; actual authenticated LE records/every split/updateInfo noop ACK/UINT64_MAX/auth-failure library-side free+counter rollback/EOF; no TCP/device actors\n",checks);return 0;
}

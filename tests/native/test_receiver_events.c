/* Current exact bounded module plus the unchanged original volume cases. */
#define main receiver_volume_original_main
#include "test_receiver_volume_records.c"
#undef main
#include <stdatomic.h>

static const char metadata_request[]="POST /command RTSP/1.0\r\nContent-Length: 18\r\n\r\nbplist00 updateInfo";
struct metadata_peer { int fd,requests,fragment,silent; const char *reply; _Atomic int received; };
static void *metadata_server(void *arg) {
  struct metadata_peer *peer=arg;
  for (int operation=0;operation<peer->requests;operation++) {
    uint8_t input[5000],reply[5000]; size_t count=0,need=0; ssize_t n;
    do {
      n=recv(peer->fd,input+count,sizeof(input)-count,0); if(n<=0)goto done;
      count+=(size_t)n; if(count>=2)need=((size_t)input[0]|((size_t)input[1]<<8))+18;
    } while(!need || count<need);
    assert(count==need && memmem(input+2,count-18,"POST /command RTSP/1.0",21));
    assert(memmem(input+2,count-18,operation ? "sendMediaRemoteCommand dvlc" : "updateInfo",operation ? 26 : 10));
    atomic_fetch_add(&peer->received,1);
    if(peer->silent) { while(recv(peer->fd,input,sizeof(input),0)>0) {} goto done; }
    if(peer->fragment==2) {
      // Send one complete record plus the first byte of the next header.
      // The pinned decoder must never receive that trailing single byte.
      size_t split=strlen(peer->reply)-3;
      need=framed(reply,(const uint8_t *)peer->reply,split);
      size_t second=framed(reply+need,(const uint8_t *)peer->reply+split,3);
      n=send(peer->fd,reply,need+1,MSG_NOSIGNAL);assert(n==(ssize_t)need+1);usleep(10000);
      n=send(peer->fd,reply+need+1,second-1,MSG_NOSIGNAL);assert(n==(ssize_t)second-1);
    } else {
      need=framed(reply,(const uint8_t *)peer->reply,strlen(peer->reply));
      for(size_t at=0;at<need;) {
        size_t size=peer->fragment ? 1 : need-at;
        n=send(peer->fd,reply+at,size,MSG_NOSIGNAL);assert(n==(ssize_t)size);at+=size;
        if(peer->fragment)usleep(500);
      }
    }
  }
done:close(peer->fd);return NULL;
}
static void unlocked(void) {
  assert(!pthread_mutex_trylock(&connection.event_sender_mutex));pthread_mutex_unlock(&connection.event_sender_mutex);
  assert(!pthread_rwlock_trywrlock(&principal_conn_lock));pthread_rwlock_unlock(&principal_conn_lock);
}
static void *cancelled_metadata(void *unused) {
  (void)unused;shiri_ap2_event_exchange(&connection,metadata_request,sizeof(metadata_request)-1);
  pthread_testcancel();return NULL;
}
int main(void) {
  receiver_volume_original_main();
  for(int fragment=0;fragment<3;fragment++) {
    int sockets[2];pthread_t thread;assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets));setup(sockets[0]);
    struct metadata_peer peer={.fd=sockets[1],.requests=2,.fragment=fragment,.reply=fragment==2 ? "RTSP/1.0 200 OK\r\nContent-Length: 3\r\n\r\nabc" : "RTSP/1.0 200 OK\r\n\r\n"};
    assert(!pthread_create(&thread,NULL,metadata_server,&peer));
    assert(shiri_ap2_event_exchange(&connection,metadata_request,sizeof(metadata_request)-1)==sizeof(metadata_request)-1);
    assert(!connection.shiri_event_volume_faulted);unlocked();
    assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_OK);
    assert(atomic_load(&peer.received)==2);pthread_join(thread,NULL);cleanup();
  }
  // Reproduce the unsupported updateInfo peer from actual network128. The
  // metadata call returns on the unchanged750ms deadline; volume immediately
  // reports the retired optional lane instead of waiting on a stuck mutex.
  int sockets[2];pthread_t thread;uint64_t started;assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets));setup(sockets[0]);
  started=shiri_volume_now();assert(shiri_ap2_event_exchange(&connection,metadata_request,sizeof(metadata_request)-1)==-1);
  assert(shiri_volume_now()-started<UINT64_C(1500000000));assert(connection.shiri_event_volume_faulted);unlocked();
  started=shiri_volume_now();assert(shiri_ap2_volume(12,42,7,fence,NULL)==SHIRI_VOLUME_UNAVAILABLE);
  assert(shiri_volume_now()-started<UINT64_C(100000000));close(sockets[1]);cleanup();
  for(int rejected=0;rejected<2;rejected++) {
    assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets));setup(sockets[0]);
    struct metadata_peer peer={.fd=sockets[1],.requests=1,.reply=rejected ? "RTSP/1.0 200 OK\r\nBad Header\r\n\r\n" : "RTSP/1.0 500 Error\r\n\r\n"};
    assert(!pthread_create(&thread,NULL,metadata_server,&peer));
    assert(shiri_ap2_event_exchange(&connection,metadata_request,sizeof(metadata_request)-1)==-1);
    assert(connection.shiri_event_volume_faulted);unlocked();pthread_join(thread,NULL);cleanup();
  }
  // Authentication failure owns and frees the upstream output buffer even
  // though it leaves the pointer nonNULL. Shared cleanup must not double free.
  assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets));setup(sockets[0]);fail_authentication=1;
  struct metadata_peer auth={.fd=sockets[1],.requests=1,.reply="RTSP/1.0 200 OK\r\n\r\n"};
  assert(!pthread_create(&thread,NULL,metadata_server,&auth));
  assert(shiri_ap2_event_exchange(&connection,metadata_request,sizeof(metadata_request)-1)==-1);
  assert(connection.shiri_event_volume_faulted);unlocked();pthread_join(thread,NULL);cleanup();fail_authentication=0;
  size_t admitted=999;const uint8_t short_header[]={4};
  assert(!volume_complete_records(short_header,sizeof(short_header),&admitted) && admitted==0);
  const uint8_t zero_record[]={0,0},large_record[]={1,4};
  assert(volume_complete_records(zero_record,sizeof(zero_record),&admitted)==-1);
  assert(volume_complete_records(large_record,sizeof(large_record),&admitted)==-1);
  // Connection replacement before any cipher mutation cannot send metadata.
  setup(-1);principal_conn=NULL;int before=cipher_encrypts;
  assert(shiri_ap2_event_exchange(&connection,metadata_request,sizeof(metadata_request)-1)==-1);
  assert(cipher_encrypts==before && !connection.shiri_event_volume_faulted);unlocked();cleanup();
  // Cancellation during the blocked exchange is bounded. Locks and cipher
  // buffers retire before existing AP2 receiver cleanup can join this thread.
  assert(!socketpair(AF_UNIX,SOCK_STREAM,0,sockets));setup(sockets[0]);started=shiri_volume_now();
  assert(!pthread_create(&thread,NULL,cancelled_metadata,NULL));usleep(30000);assert(!pthread_cancel(thread));
  void *answer=NULL;assert(!pthread_join(thread,&answer));assert(answer==PTHREAD_CANCELED);
  assert(shiri_volume_now()-started<UINT64_C(1500000000));assert(connection.shiri_event_volume_faulted);unlocked();close(sockets[1]);cleanup();
  // Teardown owns the principal write lock while cancelling/joining the
  // event receiver. A metadata call waiting for that lock must expire and
  // honor cancellation without sending or deadlocking its owner.
  setup(-1);started=shiri_volume_now();before=cipher_encrypts;
  assert(!pthread_rwlock_wrlock(&principal_conn_lock));
  assert(!pthread_create(&thread,NULL,cancelled_metadata,NULL));usleep(30000);assert(!pthread_cancel(thread));
  answer=NULL;assert(!pthread_join(thread,&answer));assert(answer==PTHREAD_CANCELED);
  assert(shiri_volume_now()-started<UINT64_C(1500000000));
  assert(cipher_encrypts==before && !connection.shiri_event_volume_faulted);
  assert(!pthread_rwlock_unlock(&principal_conn_lock));unlocked();cleanup();
  puts("receiver-events: real socket metadata ACK then exact-volume ACK; fragmented ACK; noACK/malformed rejection, poisoned optional lane, bounded cancellation and released lifetime/mutex ownership PASS");
  return 0;
}

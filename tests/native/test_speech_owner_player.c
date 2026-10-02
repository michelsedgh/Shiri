/* Appended to the exact extracted readiness scaffold; no command mirror. */
static size_t owner_packet(uint8_t *p,const struct shiri_speech_request *q,uint64_t seq) {
  memset(p,0,SPEECH_HEADER+960);memcpy(p,"SHRITTS1",8);p[8]=2;p[9]=1;p[11]=SPEECH_HEADER;
  put32(p+12,960);put64(p+16,seq);memcpy(p+24,q->room,16);memcpy(p+40,q->launch,16);
  memcpy(p+80,q->speech,16);put64(p+56,now_ns);put32(p+64,480);put32(p+68,13107);put32(p+72,1);
  for(unsigned i=0;i<480;++i){p[SPEECH_HEADER+2*i]=100;p[SPEECH_HEADER+2*i+1]=0;}
  return SPEECH_HEADER+960;
}
static struct shiri_speech_request active_voice(void) {
  struct shiri_speech_request q=fresh();struct shiri_speech_reply r;
  q.source.owner.session[0]=8;shiri_source_state.last=q.source;
  device.session=&device;device.state=OUTPUT_STATE_CONNECTED;
  player_state=PLAY_PLAYING;pb_timer_native=pb_timer_native_anchored=1;shiri_speech_mix_ready();
  q.action=SHIRI_SPEECH_BEGIN;
  CHECK(prepare_all(&q,&r)==0 && r.ready && r.connected && !starts && !cancels && !stops && !flushes);
  return q;
}
static void authenticated_owner_commands(void) {
  struct shiri_speech_request q=active_voice(),b=q,stale;
  struct shiri_speech_reply r;struct speech_state before;
  uint8_t p[SPEECH_HEADER+960],pcm[1920];size_t n=owner_packet(p,&q,1);
  CHECK(speech_receive(&speech,p,n,now_ns)==0 && speech.count==480);
  q.action=SHIRI_SPEECH_FINISH;CHECK(prepare_all(&q,&r)==0 && !r.ready && !r.connected && !r.outputs);
  CHECK(speech.count==480 && !speech.accepting && !speech.active);
  q.action=SHIRI_SPEECH_BEGIN;CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT);
  b.speech[0]++;b.action=SHIRI_SPEECH_BEGIN;
  CHECK(prepare_all(&b,&r)==0 && !r.ready && r.connected && speech.count==480);
  now_ns+=SPEECH_RESERVE_NS;memset(pcm,0,sizeof(pcm));speech_mix(&speech,pcm,sizeof(pcm),480,48000,16,2,now_ns);
  for(unsigned i=0;i<480;++i)CHECK(pcm[i*4]==100 && pcm[i*4+2]==100);
  shiri_speech_mix_ready();CHECK(prepare_all(&b,&r)==0 && r.ready && speech.accepting);
  before=speech;q.action=SHIRI_SPEECH_CANCEL;
  CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT && !memcmp(&before,&speech,sizeof(speech)));
  q.action=SHIRI_SPEECH_FINISH;CHECK(prepare_all(&q,&r)==SHIRI_SOURCE_CONFLICT);
  stale=b;stale.source.operation_generation++;
  CHECK(prepare_all(&stale,&r)==SHIRI_SOURCE_CONFLICT);
  n=owner_packet(p,&b,2);CHECK(speech_receive(&speech,p,n,now_ns)==0 && speech.count==480);
  /* Music transitions after admission do not prevent exact voice cleanup. */
  shiri_source_state.last.owner.generation++;shiri_source_state.last.operation_generation++;
  b.action=SHIRI_SPEECH_CANCEL;CHECK(prepare_all(&b,&r)==0 && !r.ready && !r.outputs);
  CHECK(!speech.count && !speech.active && !speech.accepting);
  CHECK(shiri_source_state.last.owner.generation==2 && shiri_source_state.last.operation_generation==2);
  CHECK(!starts && !cancels && !stops && !flushes);
  b.action=SHIRI_SPEECH_BEGIN;CHECK(prepare_all(&b,&r)==SHIRI_SOURCE_CONFLICT);
  b.action=SHIRI_SPEECH_CANCEL;CHECK(prepare_all(&b,&r)==0); /* lost cancel ACK */
  q=fresh();q.action=SHIRI_SPEECH_CANCEL;
  CHECK(prepare_all(&q,&r)==0 && !speech.accepting && shiri_voice_initialized);
  CHECK(speech_begin(&speech,q.speech,now_ns)<0); /* delayed BEGIN remains terminal */
}
int main(void) {
  setup();identities();failures();music();missing();(void)prefix;
  authenticated_owner_commands();shiri_speech_setup_event_clear();speech_fd=-1;
  printf("owner1: %u exact-player checks; admission/source fence, retirement echo, music unchanged\n",checks);
  return 0;
}

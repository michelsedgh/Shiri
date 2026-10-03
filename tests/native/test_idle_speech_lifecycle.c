/* Reuse the actual paused-player, output, admission and media implementations.
 * A ZERO owner must prepare without refilling an exhausted program FIFO. */
#define PLAY_PAUSED 2
static struct shiri_speech_request fresh_idle(void) {
 struct shiri_speech_request q=fresh_phone(false);
 memset(q.source.owner.session,0,16);q.source.owner.generation=1;
 shiri_source_state.last=q.source;player_state=PLAY_PAUSED;foreign_stops=0;
 return q;
}
static void prepare_idle(struct shiri_speech_request *q,struct shiri_speech_reply *r) {
 CHECK(execute(q,SHIRI_SPEECH_PREPARE,r)==0 && r->connected && !r->ready);
 if(!pb_timer_speech_only){fputs("idle owner preparation did not arm an output-only clock\n",stderr);assert(pb_timer_speech_only);}
 CHECK(pb_timer_speech_only && timer_watch && !pb_timer_native && !pending_pcm);
 tick();
 CHECK(execute(q,SHIRI_SPEECH_READY,r)==0 && r->ready && r->mixed_ns>=r->prepared_ns);
 CHECK(execute(q,SHIRI_SPEECH_BEGIN,r)==0 && r->ready && speech.accepting);
}
static void idle_successors(bool natural) {
 struct shiri_speech_request q=fresh_idle(),old;struct shiri_speech_reply r;
 typeof(pb_session) program=pb_session;struct shiri_source_request source=shiri_source_state.last;
 /* Cold after a prior FIFO underrun; successful fresh mix takes one tick. */
 prepare_idle(&q,&r);
 CHECK(read_calls==0 && pb_session.pos==program.pos && player_state==PLAY_PAUSED);
 packet_voice(&q,480,1);tick();tick();tick();CHECK(speech.mixed_frames==480 && !speech.expired);
 packet_voice(&q,480,2);
 CHECK(execute(&q,natural?SHIRI_SPEECH_FINISH:SHIRI_SPEECH_CANCEL,&r)==0);
 CHECK(natural?speech.count==480:!speech.count);
 tick();tick();CHECK(!speech.count && !speech.accepting && !pb_timer_speech_only);
 CHECK(speech.mixed_frames==(natural?960:480) && selected_stop_pending && !foreign_stops);
 CHECK(!memcmp(&program,&pb_session,sizeof(program)) && !memcmp(&source,&shiri_source_state.last,sizeof(source)) && !read_calls && !abort_calls && !suspend_calls);
 unsigned prior_outputs=output_calls;for(unsigned i=0;i<700;i++)tick();CHECK(output_calls==prior_outputs);
 /* Warm restart on the same actor, output sessions, source and paused item. */
 old=q;++q.speech[0];prepare_idle(&q,&r);
 CHECK(!selected_stop_pending && !read_calls && player_state==PLAY_PAUSED);
 CHECK(execute(&old,SHIRI_SPEECH_CANCEL,&r)==SHIRI_SOURCE_CONFLICT && speech.accepting);
 packet_voice(&q,480,speech.sequence+1);tick();tick();tick();CHECK(speech.mixed_frames==(natural?1440:960));
 CHECK(execute(&q,SHIRI_SPEECH_CANCEL,&r)==0);tick();CHECK(!pb_timer_speech_only && selected_stop_pending);
 CHECK(!memcmp(&program,&pb_session,sizeof(program)) && !memcmp(&source,&shiri_source_state.last,sizeof(source)) && !read_calls && !foreign_stops);
}
static void idle_fences(void) {
 struct shiri_speech_request q=fresh_idle();struct shiri_speech_reply r;
 CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);++gate_operation;tick();
 CHECK(!output_calls && !read_calls && !pb_timer_speech_only);
 q=fresh_idle();CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);device.session=&r;tick();
 CHECK(!output_calls && !read_calls && !pb_timer_speech_only);
 q=fresh_idle();CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);now_ns+=SHIRI_SPEECH_SETUP_NS;tick();
 CHECK(!output_calls && !pb_timer_speech_only && selected_stop_pending && !read_calls);
 /* A genuine queued program anchor has priority; the bed never consumes it. */
 q=fresh_idle();CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);
 pending_pcm=sizeof(input_pcm);input_anchor=(struct timespec){32,50000000};tick();
 CHECK(!output_calls && !read_calls && pending_pcm==sizeof(input_pcm) && !pb_timer_speech_only);
 CHECK(execute(&q,SHIRI_SPEECH_READY,&r)==0 && !r.ready);
}
static void idle_never_produced(void) {
 struct shiri_speech_request q=fresh_idle();struct shiri_speech_reply r;
 player_state=PLAY_STOPPED;typeof(pb_session) program=pb_session;
 prepare_idle(&q,&r);packet_voice(&q,480,1);tick();tick();tick();
 CHECK(speech.mixed_frames==480 && !read_calls && player_state==PLAY_STOPPED);
 CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);tick();
 CHECK(!pb_timer_speech_only && selected_stop_pending && !foreign_stops);
 CHECK(!memcmp(&program,&pb_session,sizeof(program)) && !abort_calls && !suspend_calls);
}
int main(void) {
 CHECK(paused_fixture_main()==0);
 idle_successors(false);idle_successors(true);idle_fences();idle_never_produced();
 printf("Actual OwnTone C idle speech lifecycle: %u checks; paused ZERO owner cold/warm FINISH/CANCEL, fresh mix without program FIFO, exact retirement/session/source fences, finite bed, unchanged program P/counters and genuine music priority\n",checks);
 return 0;
}

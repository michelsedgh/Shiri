/* The exact player command must consume the bounded pending receive burst
 * before natural EOF closes admission. PCM capture is at outputs_write, before
 * conversion/device encoding; it cannot establish acoustic speaker readiness. */
static void pending_voice(struct shiri_speech_request *q, unsigned frames, uint64_t sequence,
                          unsigned first) {
 uint8_t *packet=pending_packet;
 assert(!pending_bytes && frames<=960);
 memset(packet,0,sizeof(pending_packet));memcpy(packet,"SHRITTS1",8);
 packet[8]=2;packet[9]=1;packet[11]=96;
 PUT32(packet+12,frames*2);PUT64(packet+16,sequence);
 memcpy(packet+24,q->room,16);memcpy(packet+40,q->launch,16);
 PUT64(packet+56,now_ns);PUT32(packet+64,frames);PUT32(packet+68,32768);
 PUT32(packet+72,1);memcpy(packet+80,q->speech,16);
 for(unsigned i=0;i<frames;i++) {
   unsigned sample=1+(first+i)%30000;
   packet[96+2*i]=sample;packet[97+2*i]=sample>>8;
 }
 pending_bytes=SPEECH_HEADER+frames*2;
}
static void pending_finish_race(void) {
 struct shiri_speech_request q=fresh_idle();struct shiri_speech_reply r;
 prepare_idle(&q,&r);pending_voice(&q,480,1,0);
 /* The IPC write succeeded, but there has been no next player tick. */
 CHECK(!speech.count && pending_bytes && speech.accepting);
 CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);
 if(speech.count!=480 || pending_bytes) {
   fputs("FINISH sealed admission before its pending final PCM datagram\n",stderr);
   assert(speech.count==480 && !pending_bytes);
 }
 CHECK(!speech.accepting && speech.admitted_frames==480 && !speech.refused);
 capture_count=0;capture_enabled=true;capture_case(480);
 for(unsigned i=0;i<8;i++)tick();
 capture_enabled=false;
 CHECK(speech.mixed_frames==480 && capture_count==480 && !speech.expired && !speech.refused);
 for(unsigned i=0;i<480;i++)CHECK(captured_voice[i]==1+(int)i);
 CHECK(!pb_timer_speech_only && selected_stop_pending);
}
static void whole_utterance(bool warm) {
 struct shiri_speech_request q=fresh_idle();struct shiri_speech_reply r;
 const unsigned samples=220800; /*4.6seconds with a distinct prefix and tail.*/
 if(warm) {
   prepare_idle(&q,&r);packet_voice(&q,480,1);tick();tick();tick();
   CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);tick();
   ++q.speech[0];
 }
 uint64_t baseline=speech.mixed_frames, sequence=speech.sequence+1;
 prepare_idle(&q,&r);capture_count=0;capture_enabled=true;capture_case(samples);
 for(unsigned first=0;first<samples;first+=960) {
   unsigned frames=samples-first<960?samples-first:960;
   pending_voice(&q,frames,sequence++,first);
   if(first+frames<samples){tick();tick();}
 }
 CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);
 for(unsigned i=0;i<15;i++)tick();
 capture_enabled=false;
 CHECK(capture_count==samples && speech.mixed_frames-baseline==samples && !speech.expired && !speech.refused);
 for(unsigned i=0;i<samples;i++)CHECK(captured_voice[i]==1+(int)(i%30000));
 CHECK(!pb_timer_speech_only && selected_stop_pending && !read_calls && !abort_calls && !suspend_calls);
}
static void pending_cancel_and_foreign(void) {
 struct shiri_speech_request q=fresh_idle(),wrong;struct shiri_speech_reply r;
 prepare_idle(&q,&r);pending_voice(&q,480,1,0);wrong=q;++wrong.speech[0];
 CHECK(execute(&wrong,SHIRI_SPEECH_FINISH,&r)==SHIRI_SOURCE_CONFLICT);
 CHECK(pending_bytes && speech.accepting && !speech.count);
 CHECK(execute(&q,SHIRI_SPEECH_CANCEL,&r)==0 && pending_bytes && !speech.accepting);
 tick();CHECK(!pending_bytes && !speech.count && !speech.mixed_frames && speech.refused==1);
}
static void short_terminal_interval(void) {
 struct shiri_speech_request q=fresh_idle();struct shiri_speech_reply r;
 prepare_idle(&q,&r);pending_voice(&q,480,1,0);
 CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);
 capture_count=0;capture_enabled=true;capture_case(480);
 for(unsigned i=0;i<12 && shiri_speech_bed_wanted();i++)tick();
 CHECK(capture_count==480 && !speech.running && !speech.count);
 uint64_t baseline=shiri_output_bed_frames;
 /* A timer's catch-up callback can occur only1ms after the preceding bed.
  * That48-frame interval alone cannot flush352 output frames plus history. */
 now_ns=(uint64_t)shiri_output_end.tv_sec*1000000000+shiri_output_end.tv_nsec+1000000;
 playback_cb(0,0,NULL);
 CHECK(shiri_output_bed_frames-baseline==48);
 if(!pb_timer_speech_only) {
   fputs("Short terminal interval retired before the finite packet tail was released\n",stderr);
   assert(pb_timer_speech_only);
 }
 for(unsigned i=0;i<4;i++)tick();
 CHECK(shiri_output_bed_frames-baseline==480 && !pb_timer_speech_only && selected_stop_pending);
 capture_enabled=false;
 CHECK(capture_count==480 && !speech.expired && !speech.refused && !read_calls);
}
static struct shiri_speech_request terminal_tail(bool phone) {
 struct shiri_speech_request q=phone?fresh_phone(true):fresh_idle();struct shiri_speech_reply r;
 CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);tick();
 CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==0 && r.ready);
 pending_voice(&q,480,1,0);CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);
 for(unsigned i=0;i<12 && shiri_speech_bed_wanted();i++)tick();
 CHECK(!speech.running && !speech.count && speech.mixed_frames==480);
 return q;
}
static void terminal_tail_fences(void) {
 struct shiri_speech_request q=terminal_tail(false);struct shiri_speech_reply r;
 unsigned baseline=output_calls;
 device.session=&r;tick();
 CHECK(output_calls==baseline && !pb_timer_speech_only && !read_calls);
 q=terminal_tail(false);baseline=output_calls;
 ++shiri_source_state.last.operation_generation;gate_operation=shiri_source_state.last.operation_generation;
 tick();CHECK(output_calls==baseline && !pb_timer_speech_only && !read_calls);
 q=terminal_tail(false);baseline=output_calls;
 CHECK(execute(&q,SHIRI_SPEECH_CANCEL,&r)==0);tick();
 CHECK(output_calls==baseline && !pb_timer_speech_only && !read_calls);
 q=terminal_tail(true);baseline=output_calls;uint64_t original_pos=pb_session.pos;
 input_anchor=(struct timespec){now_ns/1000000000,now_ns%1000000000+30000000};
 if(input_anchor.tv_nsec>=1000000000){++input_anchor.tv_sec;input_anchor.tv_nsec-=1000000000;}
 pending_pcm=sizeof(input_pcm);tick();
 CHECK(output_calls==baseline && !read_calls && pb_timer_native_anchored &&
       timespec_cmp(pb_session.pts,input_anchor)==0 && pb_session.pos==original_pos);
}
int main(int argc,char **argv) {
 if(argc==2){capture_stream=fopen(argv[1],"wb");assert(capture_stream);}
 else assert(argc==1);
 CHECK(paused_fixture_main()==0);
 pending_finish_race();whole_utterance(false);whole_utterance(true);pending_cancel_and_foreign();short_terminal_interval();terminal_tail_fences();
 printf("Actual OwnTone player C speech drain: %u checks; pending FINISH preserves final PCM, cold/warm4.6s every prefix/tail sample reaches outputs_write, exact foreign/cancel fence retained\n",checks);
 if(capture_stream)assert(fclose(capture_stream)==0);
 return 0;
}

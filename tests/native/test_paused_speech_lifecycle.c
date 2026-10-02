static struct shiri_speech_request fresh_phone(bool playing) {
 struct shiri_speech_request q;static struct event player_timer;static struct source native_pipe={DATA_KIND_PIPE};
 shiri_speech_setup_event_clear();memset(outputs_cb_register,0,sizeof(outputs_cb_register));outputs_callback_token=0;
 memset(&q,0,sizeof(q));memset(&shiri_source_state,0,sizeof(shiri_source_state));memset(&shiri_speech_start,0,sizeof(shiri_speech_start));memset(&shiri_last_mix,0,sizeof(shiri_last_mix));
 memset(&device,0,sizeof(device));memset(&foreign,0,sizeof(foreign));memset(&speech,0,sizeof(speech));memset(&pb_session,0,sizeof(pb_session));
 q.source.owner.incarnation[0]=1;q.source.owner.session[0]=8;q.source.owner.epoch=4;q.source.owner.generation=5;q.source.operation_generation=6;q.room[0]=2;q.launch[0]=3;q.speech[0]=4;q.action=SHIRI_SPEECH_PREPARE;
 shiri_source_state.initialized=true;shiri_source_state.last=q.source;
 memcpy(speech.room,q.room,16);memcpy(speech.launch,q.launch,16);speech_fd=999;speech.gain=1;
 outputs_device_list=&device;device.id=5;device.type=6;device.selected=true;device.state=OUTPUT_STATE_CONNECTED;device.session=&device;
 device.stop_timer=&selected_timer;device.next=&foreign;foreign.id=99;foreign.session=&foreign;foreign.state=OUTPUT_STATE_CONNECTED;
 now_ns=UINT64_C(32000000000);gate_operation=6;gate_state=1;peek_failure=pending_pcm=inject_after_empty_peek=0;
 shiri_voice_initialized=false;memset(&shiri_voice_request,0,sizeof(shiri_voice_request));
 player_state=playing?PLAY_PLAYING:PLAY_STOPPED;pb_timer_native=playing;pb_timer_native_anchored=false;pb_timer_native_waiting_for_pcm=playing;pb_timer_native_operation=6;
 pb_timer_speech_only=false;shiri_output_end_valid=false;shiri_output_bed_frames=0;
 pb_session.quality=(struct media_quality){48000,16,2};pb_session.buffer=session_buffer;pb_session.bufsize=sizeof(session_buffer);pb_session.read_deficit_max=192000;pb_session.pos=1234567;pb_session.reading_now=playing?&native_pipe:NULL;pb_session.pts=(struct timespec){31,123000000};pb_session.start_ts=(struct timespec){30,321000000};
 timer_arms=timer_absolute=timer_failure=timer_watch=0;selected_stop_pending=false;event_fail=fail_clock=start_failure=0;starts=stops=cancels=flushes=callback_ends=foreign_cancels=0;read_calls=output_calls=abort_calls=suspend_calls=0;pending_callback=NULL;mutate_source_between_commands=0;
 pb_timer_ev=&player_timer;
 return q;
}
static int execute(struct shiri_speech_request *q,enum shiri_speech_action action,struct shiri_speech_reply *r){q->action=action;return player_shiri_speech_ready(q,r);}
static void packet_voice(struct shiri_speech_request *q,unsigned frames,uint64_t seq) {
 uint8_t packet[SPEECH_HEADER+1920]={0};memcpy(packet,"SHRITTS1",8);packet[8]=2;packet[9]=frames?1:2;packet[11]=96;
 #define PUT32(p,n) do{uint32_t v=(n);(p)[0]=v>>24;(p)[1]=v>>16;(p)[2]=v>>8;(p)[3]=v;}while(0)
 #define PUT64(p,n) do{uint64_t v64=(n);PUT32((p),(uint32_t)(v64>>32));PUT32((p)+4,(uint32_t)v64);}while(0)
 PUT32(packet+12,frames*2);PUT64(packet+16,seq);memcpy(packet+24,q->room,16);memcpy(packet+40,q->launch,16);PUT64(packet+56,now_ns);PUT32(packet+64,frames);PUT32(packet+68,32768);PUT32(packet+72,1);memcpy(packet+80,q->speech,16);
 for(unsigned i=0;i<frames;i++){packet[96+i*2]=0xd2;packet[97+i*2]=4;}
 CHECK(speech_receive(&speech,packet,SPEECH_HEADER+frames*2,now_ns)==0);
}
static void tick(void){if(pb_timer_native || pb_timer_speech_only || player_state==PLAY_PLAYING)playback_cb(0,0,NULL);now_ns+=10000000;}
static void paused_lifecycle(bool initially_playing){
 struct shiri_speech_request q=fresh_phone(initially_playing);struct shiri_speech_reply r;typeof(pb_session) before=pb_session;struct shiri_source_request binding=shiri_source_state.last;
 int ret=execute(&q,SHIRI_SPEECH_PREPARE,&r);
 if(ret){fputs("paused phone preparation must succeed\n",stderr);assert(ret==0);}
 CHECK(r.connected && !r.ready && starts==1 && stops==0 && !foreign_cancels);
 CHECK(initially_playing?!pb_timer_speech_only:pb_timer_speech_only);
 tick();CHECK(output_calls==1 && read_calls==0 && shiri_output_bed_frames==480);
 CHECK(!memcmp(&before,&pb_session,sizeof(before)) && !memcmp(&binding,&shiri_source_state.last,sizeof(binding)) && !pb_timer_native_anchored);
 CHECK(execute(&q,SHIRI_SPEECH_READY,&r)==0 && r.ready && r.mixed_ns>=r.prepared_ns);
 CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==0 && r.ready && speech.accepting);
 packet_voice(&q,480,1);tick();tick();tick();
 CHECK(speech.mixed_frames==480 && !speech.expired && read_calls==0);
 for(unsigned i=0;i<480;i++)CHECK(last_output[4*i]==0xd2 && last_output[4*i+1]==4 && last_output[4*i+2]==0xd2 && last_output[4*i+3]==4);
 CHECK(!memcmp(&before,&pb_session,sizeof(before)) && !memcmp(&binding,&shiri_source_state.last,sizeof(binding)));
 /* Exact FINISH retains queued natural tail while future input stays absent. */
 packet_voice(&q,480,2);CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0 && speech.count==480);
 tick();CHECK(speech.mixed_frames==960 && !speech.count);tick();
 CHECK(read_calls==0 && !abort_calls && !suspend_calls && stops==0);
 CHECK(!initially_playing?!pb_timer_speech_only:pb_timer_native_waiting_for_pcm);
 CHECK(!memcmp(&before,&pb_session,sizeof(before)));
 /* More than30seconds of valid phone pause produces no fake program frames. */
 unsigned previous_outputs=output_calls;for(unsigned i=0;i<3100;i++)tick();
 CHECK(output_calls==previous_outputs && read_calls==0 && !memcmp(&before,&pb_session,sizeof(before)));
}
static void resume_during_voice(void){
 struct shiri_speech_request q=fresh_phone(true);struct shiri_speech_reply r;
 CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);tick();CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==0 && r.ready);
 packet_voice(&q,480,1);tick();tick();
 input_anchor=(struct timespec){now_ns/1000000000,(now_ns%1000000000)+30000000};if(input_anchor.tv_nsec>=1000000000){++input_anchor.tv_sec;input_anchor.tv_nsec-=1000000000;}
 pending_pcm=sizeof(input_pcm);for(unsigned i=0;i<sizeof(input_pcm);i+=2){input_pcm[i]=0xe8;input_pcm[i+1]=3;}
 unsigned prior_outputs=output_calls;uint64_t prior_pos=pb_session.pos;
 tick();CHECK(pb_timer_native_anchored && timer_absolute==TIMER_ABSTIME && timespec_cmp(armed_anchor,input_anchor)==0 && output_calls==prior_outputs && read_calls==0);
 CHECK(timespec_cmp(pb_session.pts,input_anchor)==0 && pb_session.pos==prior_pos);
 tick();tick();CHECK(output_calls==prior_outputs && read_calls==0);
 tick();CHECK(output_calls==prior_outputs+1 && read_calls==1 && pb_session.pos==prior_pos+480 && timespec_cmp(last_output_pts,input_anchor)==0 && speech.mixed_frames==480);
 CHECK(last_output[0] || last_output[1]); /* actual admitted voice is mixed over actual resumed music */
 /* Retirement stays fenced to the admission request across source successor. */
 ++shiri_source_state.last.operation_generation;++shiri_source_state.last.owner.generation;
 struct shiri_speech_request stale=q;stale.speech[0]++;
 CHECK(execute(&stale,SHIRI_SPEECH_CANCEL,&r)==SHIRI_SOURCE_CONFLICT && speech.accepting);
 CHECK(execute(&q,SHIRI_SPEECH_CANCEL,&r)==0 && !speech.accepting && !speech.count);
 CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==SHIRI_SOURCE_CONFLICT);
}
static void fences_and_expiry(void){
 struct shiri_speech_request q=fresh_phone(false);struct shiri_speech_reply r;
 CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);device.session=&r;tick();CHECK(output_calls==0 && read_calls==0 && !pb_timer_speech_only);
 q=fresh_phone(false);CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);gate_operation++;tick();CHECK(output_calls==0 && read_calls==0 && !pb_timer_speech_only);
 q=fresh_phone(false);CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);now_ns+=SHIRI_SPEECH_SETUP_NS;tick();CHECK(!output_calls && !pb_timer_speech_only && !read_calls && !stops);
 q=fresh_phone(false);CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);tick();CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==0);
 packet_voice(&q,480,1);now_ns+=SPEECH_AGE_NS;tick();CHECK(speech.expired==480 && !speech.mixed_frames);
 CHECK(execute(&q,SHIRI_SPEECH_CANCEL,&r)==0);tick();CHECK(!pb_timer_speech_only && !read_calls);
}
static void source_end_during_voice(void){
 struct shiri_speech_request q=fresh_phone(true);struct shiri_speech_reply r;struct shiri_source_param param={0};int ret;
 CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);tick();CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==0);
 packet_voice(&q,960,1);tick(); /* exact admitted tail is still priming */
 uint64_t original_pos=pb_session.pos;struct timespec original_p=pb_session.pts;
 param.request=shiri_source_state.last;memset(param.request.owner.session,0,16);param.request.owner.generation=1;++param.request.operation_generation;
 CHECK(shiri_source_seal_flush(&param,&ret)==COMMAND_END && ret==0 && shiri_source_state.faulted && !pb_timer_native && !pb_timer_speech_only);
 CHECK(shiri_source_arm(&param,&ret)==COMMAND_END && ret==0 && !shiri_source_state.faulted && player_state==PLAY_STOPPED && pb_timer_speech_only);
 CHECK(!shiri_source_state.last.owner.session[0] && speech.count==960 && speech.accepting && !selected_stop_pending);
 tick();tick();tick();CHECK(speech.mixed_frames==960 && pb_session.pos==original_pos && timespec_cmp(pb_session.pts,original_p)==0 && read_calls==0);
 /* Original exact admission request still retires successfully after END. */
 CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);tick();CHECK(!pb_timer_speech_only && !speech.count && !speech.accepting && stops>=1 && selected_stop_pending);
}
static void short_lead_race_and_jitter(void){
 struct shiri_speech_request q=fresh_phone(true);struct shiri_speech_reply r;
 CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);
 uint64_t t=now_ns;injected_anchor=(struct timespec){t/1000000000,t%1000000000+1000000};inject_after_empty_peek=1;
 CHECK(shiri_speech_bed_tick() && output_calls==1 && read_calls==0 && !pb_timer_native_anchored);
 CHECK(shiri_output_end.tv_sec==(time_t)(t/1000000000) && shiri_output_end.tv_nsec==(long)(t%1000000000));
 CHECK(pb_timer_native_prepare()==0 && pb_timer_native_anchored && timespec_cmp(pb_timer_native_anchor,injected_anchor)==0 && !abort_calls);
 now_ns=t+1000000;playback_cb(0,0,NULL);CHECK(output_calls==2 && read_calls==1 && timespec_cmp(last_output_pts,injected_anchor)==0);
 q=fresh_phone(true);CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);tick();CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==0);
 struct timespec previous=shiri_output_end;
 uint64_t sequence=1;
 for(unsigned i=0;i<20000;i++){
   if(i%10==0)packet_voice(&q,0,sequence++); /* real validated current-voice control/lease */
   now_ns+=(i%2?11000000:9000000);
   uint64_t captured=now_ns;playback_cb(0,0,NULL);
   uint64_t end=(uint64_t)shiri_output_end.tv_sec*1000000000+(uint64_t)shiri_output_end.tv_nsec;
   CHECK(end<=captured && timespec_cmp(shiri_output_end,previous)>=0 && read_calls==0 && !abort_calls);
   CHECK(captured-end<=20000000);previous=shiri_output_end;
 }
 CHECK(execute(&q,SHIRI_SPEECH_CANCEL,&r)==0);tick();CHECK(read_calls==0);
}
static void flush_and_long_end_voice(void){
 struct shiri_speech_request q=fresh_phone(true);struct shiri_speech_reply r;struct shiri_source_param param={0};int ret;
 CHECK(execute(&q,SHIRI_SPEECH_PREPARE,&r)==0);tick();CHECK(execute(&q,SHIRI_SPEECH_BEGIN,&r)==0);
 packet_voice(&q,960,1);uint64_t original_pos=pb_session.pos;struct timespec original_p=pb_session.pts;
 param.request=shiri_source_state.last;++param.request.owner.generation;++param.request.operation_generation;
 CHECK(shiri_source_seal_flush(&param,&ret)==COMMAND_END && ret==0);
 CHECK(shiri_source_arm(&param,&ret)==COMMAND_END && ret==0 && pb_timer_native_waiting_for_pcm && !selected_stop_pending);
 for(unsigned i=0;i<4;i++) { tick(); }
 CHECK(speech.mixed_frames==960 && pb_session.pos==original_pos && timespec_cmp(pb_session.pts,original_p)==0 && read_calls==0);
 /* Active voice remains admitted when the music generation changes. */
 packet_voice(&q,0,2);
 memset(&param,0,sizeof(param));param.request=shiri_source_state.last;memset(param.request.owner.session,0,16);param.request.owner.generation=1;++param.request.operation_generation;
 CHECK(shiri_source_seal_flush(&param,&ret)==COMMAND_END && ret==0);
 CHECK(shiri_source_arm(&param,&ret)==COMMAND_END && ret==0 && pb_timer_speech_only && !selected_stop_pending);
 for(unsigned i=0;i<1200;i++){
   if(i%10==0)packet_voice(&q,0,3+i/10);
   tick();CHECK(pb_timer_speech_only && !selected_stop_pending && read_calls==0);
 }
 /* Twelve seconds exceeds OwnTone's10s delayed release; active exact voice
  * retains its selected transports until its own retirement. */
 CHECK(execute(&q,SHIRI_SPEECH_FINISH,&r)==0);tick();
 CHECK(!pb_timer_speech_only && selected_stop_pending && !speech.accepting && !speech.count && read_calls==0);
}
int main(void){paused_lifecycle(true);paused_lifecycle(false);resume_during_voice();fences_and_expiry();source_end_during_voice();short_lead_race_and_jitter();flush_and_long_end_voice();printf("Actual OwnTone C paused speech lifecycle: %u checks; retained pause>30s, never-produced STOPPED source, audible exact voice/tail, source/session fences, bounded expiry, original future music P/counters unchanged until actual resume\n",checks);return 0;}

/* Unchanged actual pinned/composed source excerpts. Provenance: airplay_receiver_lead-origin.json. */
int
outputs_buffer_samples_get(uint64_t *samples, uint64_t duration_ms, int sample_rate, uint64_t limit)
{
  uint64_t value;
  if (!samples || sample_rate <= 0 || duration_ms > UINT64_MAX / (unsigned)sample_rate)
    return -1;
  value = duration_ms * (unsigned)sample_rate / 1000;
  if (value > limit)
    return -1;
  *samples = value;
  return 0;
}
#define timestamp_set raop_timestamp_set
static inline void
timestamp_set(struct raop_master_session *rms, struct timespec ts)
{
  // The last write from the player had a timestamp which has been passed to
  // this function as ts. This is the player clock, which is more precise than
  // the actual clock because it gives us a calculated time reference, which is
  // independent of how busy the thread is. We save that here, we need this for
  // reference when sending sync packets and progress.
  rms->cur_stamp.ts = ts;

  // So what rtptime should be playing, i.e. coming out of the speaker, at time
  // ts (which is normally "now")? Let's calculate by example:
  //   - we started playback with a rtptime (pos) of X
  //   - up until time ts we have received a 1000 samples from the player
  //   - rms->output_buffer_samples is configured to 400 samples
  //   -> we should be playing rtptime X + 600
  //
  // So how do we measure samples received from player? We know that from the
  // pos, which says how much has been sent to the device, and from rms->input_buffer,
  // which is the unsent stuff being buffered:
  //   - received = (pos - X) + rms->input_buffer_samples
  //
  // This means the rtptime is computed as:
  //   - rtptime = X + received - rms->output_buffer_samples
  //   -> rtptime = X + (pos - X) + rms->input_buffer_samples - rms->out_buffer_samples
  //   -> rtptime = pos + rms->input_buffer_samples - rms->output_buffer_samples
  rms->cur_stamp.pos = rms->rtp_session->pos + rms->input_buffer_samples - rms->output_buffer_samples;
}
#undef timestamp_set
#define timestamp_set airplay_timestamp_set
static inline void
timestamp_set(struct airplay_master_session *ams, struct timespec ts)
{
  // The last write from the player had a timestamp which has been passed to
  // this function as ts. This is the player clock, which is more precise than
  // the actual clock because it gives us a calculated time reference, which is
  // independent of how busy the thread is. We save that here, we need this for
  // reference when sending sync packets and progress.
  ams->cur_stamp.ts = ts;

  // So what rtptime should be playing, i.e. coming out of the speaker, at time
  // ts (which is normally "now")? Let's calculate by example:
  //   - we started playback with a rtptime (pos) of X
  //   - up until time ts we have received a 1000 samples from the player
  //   - ams->output_buffer_samples is configured to 400 samples
  //   -> we should be playing rtptime X + 600
  //
  // So how do we measure samples received from player? We know that from the
  // pos, which says how much has been sent to the device, and from ams->input_buffer,
  // which is the unsent stuff being buffered:
  //   - received = (pos - X) + ams->input_buffer_samples
  //
  // This means the rtptime is computed as:
  //   - rtptime = X + received - ams->output_buffer_samples
  //   -> rtptime = X + (pos - X) + ams->input_buffer_samples - ams->out_buffer_samples
  //   -> rtptime = pos + ams->input_buffer_samples - ams->output_buffer_samples
  ams->cur_stamp.pos = ams->rtp_session->pos + ams->input_buffer_samples - ams->output_buffer_samples;
}
#undef timestamp_set
static inline void
timespec_to_ntp(struct timespec *ts, struct ntp_timestamp *ns)
{
  /* Seconds since NTP Epoch (1900-01-01) */
  ns->sec = ts->tv_sec + NTP_EPOCH_DELTA;

  ns->frac = (uint32_t)((double)ts->tv_nsec * 1e-9 * FRAC);
}
static void
sync_packet_ptp_make(uint8_t *data, struct rtcp_timestamp cur_stamp, uint32_t pos, uint8_t type, uint64_t ptp_clock_id)
{
  uint32_t cur_pos;
  uint64_t cur_ns;
  uint32_t rtptime;
  uint64_t clock_id;

  data[0] = type; // 0x90 with stream start marker (M=1)
  data[1] = 0xd7; // PT 215 Time announce

  data[2] = 0x00;
  data[3] = 0x06;

  cur_pos = htobe32(cur_stamp.pos);
  memcpy(data + 4, &cur_pos, 4);

  cur_ns = cur_stamp.ts.tv_sec;
  cur_ns *= 1000000000;
  cur_ns += cur_stamp.ts.tv_nsec;
  cur_ns = htobe64(cur_ns);
  memcpy(data + 8, &cur_ns, 8);

  // My best understanding of rtptime is that it is the earliest packet the
  // device should start playing. Why Apple has 77175 isn't completely clear to
  // me, but seems to be related to AIRPLAY_AUDIO_LATENCY_MS. Note that the
  // rtptime sent here will not be the rtptime of the first packet (contrary to
  // the ntp sync, see below).
  rtptime = htobe32(pos - 11025);
  memcpy(data + 16, &rtptime, 4);

  clock_id = htobe64(ptp_clock_id);
  memcpy(data + 20, &clock_id, 8);
}
static void
sync_packet_ntp_make(uint8_t *data, struct rtcp_timestamp cur_stamp, uint32_t pos, uint8_t type)
{
  struct ntp_timestamp cur_ts;
  uint32_t rtptime;
  uint32_t cur_pos;

  data[0] = type;
  data[1] = 0xd4;

  // AirPlay 1/RAOP:
  // These values are used by iTunes/Apple Music, and according to
  // shairport-sync's rtp.c they tell the speaker to add a 11025 sample latency.
  // rtp.c says pure AirPlay uses 0x04, which doesn't add the latency. However,
  // testing with my own AirPlay receiver, I couldn't confirm that 0x04 doesn't
  // add a latency. Safest bet is to use the same as iTunes/Apple Music.
  // AirPlay 2:
  // Values don't seem to be used, instead the latency is communicated via
  // latencyMin in the SETUP stream request.
  data[2] = 0x00;
  data[3] = 0x07;

  timespec_to_ntp(&cur_stamp.ts, &cur_ts);

  cur_pos = htobe32(cur_stamp.pos);
  memcpy(data + 4, &cur_pos, 4);

  cur_ts.sec = htobe32(cur_ts.sec);
  cur_ts.frac = htobe32(cur_ts.frac);
  memcpy(data + 8, &cur_ts.sec, 4);
  memcpy(data + 12, &cur_ts.frac, 4);

  // This rtptime must match the start rtptime communicated to the device via
  // RTSP, otherwise some devices will fail e.g. Eufy Genie
  rtptime = htobe32(pos);
  memcpy(data + 16, &rtptime, 4);
}
static void consumer_latency(struct receiver_conn *conn, uint32_t frame_1, uint32_t frame_2) {
int32_t stream_specified_latency = frame_2 - frame_1; // this is the latency expected
              if (stream_specified_latency != 77175)
                debug(1, "Stream-specified latency is %d frames. Normally it is 77175.", stream_specified_latency);
              int32_t net_source_latency = stream_specified_latency + ap2_realttime_stream_latency_fudge_factor;

              // Now to accommodate a backend buffer of the desired length.
              // Note that it's in input-rate frames, not output-rate frames!
              
              net_source_latency = net_source_latency - (int32_t)(config.audio_backend_buffer_desired_length *
                                                    conn->input_rate);
                                                                               
              // Now we want to check the user-specified latency offset.

              // We want to warn the user if they have asked for a negative latency that is too great --
              // one that would require packets to arrive before they actually do,
              // (which is about two seconds before they are to be played).
              
              int32_t net_latency = net_source_latency + (int32_t)(config.audio_backend_latency_offset * conn->input_rate);
              
              if (net_latency <= 0) {
                if (conn->latency_warning_issued == 0) {
                  warn("The stream latency (%g seconds) is too short to accommodate an audio backend latency offset of "
                       "%g seconds and a backend buffer of %g seconds. The audio_backend_latency_offset has been set to zero.",
                       ((stream_specified_latency + ap2_realttime_stream_latency_fudge_factor) * 1.0) / conn->input_rate,
                       config.audio_backend_latency_offset,
                       config.audio_backend_buffer_desired_length);
                  config.audio_backend_latency_offset = 0.0;
                  net_latency = net_source_latency;
                  conn->latency_warning_issued = 1;
                }
              }
              conn->latency = net_latency; // this is the time window within which packets can be accepted without being too late
              
}

/*
 * Apple RTP protocol handler. This file is part of Shairport.
 * Copyright (c) James Laird 2013
 * Copyright (c) Mike Brady 2014--2025
 * All rights reserved.
 *
 * Permission is hereby granted, free of charge, to any person
 * obtaining a copy of this software and associated documentation
 * files (the "Software"), to deal in the Software without
 * restriction, including without limitation the rights to use,
 * copy, modify, merge, publish, distribute, sublicense, and/or
 * sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be
 * included in all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
 * OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
 * HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
 * WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
 * OTHER DEALINGS IN THE SOFTWARE.
 */
/* Exact pinned C excerpts; provenance in shairport_buffered_timing-origin.json.
 * Harness wrappers name existing AP1/realtime statement blocks only. */
void set_ptp_anchor_info(rtsp_conn_info *conn, uint64_t clock_id, uint32_t rtptime,
                         uint64_t networktime) {
  if ((conn->anchor_clock != 0) && (conn->anchor_clock == clock_id) &&
      (conn->anchor_remote_info_is_valid != 0)) {
    // check change in timing
    int64_t time_difference = networktime - conn->anchor_time;
    int32_t frame_difference = rtptime - conn->anchor_rtptime;
    double time_difference_in_frames = (1.0 * time_difference * conn->input_rate) / 1000000000;
    double frame_change = frame_difference - time_difference_in_frames;
    debug(3,
          "Connection %d: set_ptp_anchor_info: clock: %" PRIx64 ", rtptime: %" PRIu32
          ", networktime: %" PRIx64 ", frame adjustment: %7.3f.",
          conn->connection_number, clock_id, rtptime, networktime, frame_change);
  } else {
    debug(2,
          "Connection %d: set_ptp_anchor_info: clock: %" PRIx64 ", rtptime: %" PRIu32
          ", networktime: %" PRIx64 ".",
          conn->connection_number, clock_id, rtptime, networktime);
  }
  if (conn->anchor_clock != clock_id) {
    debug(2, "Connection %d: Set Anchor Clock: %" PRIx64 ".", conn->connection_number, clock_id);
  }
  // debug(1,"set anchor info clock: %" PRIx64", rtptime: %u, networktime: %" PRIx64 ".", clock_id,
  // rtptime, networktime);

  // if the clock is the same but any details change, and if the last_anchor_info has not been
  // valid for some minimum time (and thus may not be reliable), we need to invalidate
  // last_anchor_info

  if ((conn->airplay_stream_type == buffered_stream) && (conn->ap2_play_enabled != 0) &&
      ((clock_id != conn->anchor_clock) || (conn->anchor_rtptime != rtptime) ||
       (conn->anchor_time != networktime))) {
    uint64_t master_clock_id = 0;
    ptp_get_clock_info(&master_clock_id, NULL, NULL, NULL);
    debug(1,
          "Connection %d: Note: anchor parameters have changed. Old clock: %" PRIx64
          ", rtptime: %u, networktime: %" PRIu64 ". New clock: %" PRIx64
          ", rtptime: %u, networktime: %" PRIu64 ". Current master clock: %" PRIx64 ".",
          conn->connection_number, conn->anchor_clock, conn->anchor_rtptime, conn->anchor_time,
          clock_id, rtptime, networktime, master_clock_id);
  }

  if ((clock_id == conn->anchor_clock) &&
      ((conn->anchor_rtptime != rtptime) || (conn->anchor_time != networktime))) {
    uint64_t time_now = get_absolute_time_in_ns();
    int64_t last_anchor_validity_duration = time_now - conn->last_anchor_validity_start_time;
    if (last_anchor_validity_duration < 5000000000) {
      if (conn->airplay_stream_type == buffered_stream)
        debug(2,
              "Connection %d: Note: anchor parameters have changed before clock %" PRIx64
              " has stabilised.",
              conn->connection_number, clock_id);
      conn->last_anchor_info_is_valid = 0;
    }
  }

  conn->anchor_remote_info_is_valid = 1;

  // these can be modified if the master clock changes over time

  conn->anchor_rtptime = rtptime;
  conn->anchor_time = networktime;
  conn->anchor_clock = clock_id;
  debug(2, "set_ptp_anchor_info done.");
}

int get_ptp_anchor_local_time_info(rtsp_conn_info *conn, uint32_t *anchorRTP,
                                   uint64_t *anchorLocalTime) {
  int response = clock_no_anchor_info; // no anchor information
  if (conn->anchor_remote_info_is_valid != 0) {
    response = clock_not_valid;
    uint64_t actual_clock_id;
    uint64_t actual_time_of_sample, actual_offset, start_of_mastership;
    response = ptp_get_clock_info(&actual_clock_id, &actual_time_of_sample, &actual_offset,
                                  &start_of_mastership);
    if (response == clock_ok) {
      uint64_t time_now = get_absolute_time_in_ns();
      int64_t time_since_start_of_mastership = time_now - start_of_mastership;
      if (time_since_start_of_mastership >= 400000000L) {
        int64_t time_since_sample = time_now - actual_time_of_sample;
        if (time_since_sample > 300000000000L) {
          if (long_time_notifcation_done == 0) {
            debug(1, "The last PTP timing sample is pretty old: %f seconds.",
                  0.000000001 * time_since_sample);
            long_time_notifcation_done = 1;
          }
        } else if ((time_since_sample < 2000000000) && (long_time_notifcation_done != 0)) {
          debug(1, "The last PTP timing sample is no longer too old: %f seconds.",
                0.000000001 * time_since_sample);
          long_time_notifcation_done = 0;
        }

        int64_t jitter = actual_offset - previous_offset;

        if ((previous_offset != 0) && (previous_clock_id == actual_clock_id) &&
            ((jitter > 3000000) || (jitter < -3000000)))
          debug(1,
                "Clock jitter: %.3f mS. Time since sample: %.3f mS. Time since start of "
                "mastership: %.3f "
                "seconds.",
                jitter * 0.000001, time_since_sample * 0.000001,
                time_since_start_of_mastership * 0.000000001);

        previous_offset = actual_offset;
        previous_clock_id = actual_clock_id;

        if (actual_clock_id == conn->anchor_clock) {
          conn->last_anchor_rtptime = conn->anchor_rtptime;
          conn->last_anchor_local_time = conn->anchor_time - actual_offset;
          conn->last_anchor_time_of_update = time_now;
          if (conn->last_anchor_info_is_valid == 0)
            conn->last_anchor_validity_start_time = start_of_mastership;
          conn->last_anchor_info_is_valid = 1;
        } else {
          debug(3, "Current master clock %" PRIx64 " and anchor_clock %" PRIx64 " are different",
                actual_clock_id, conn->anchor_clock);
          // the anchor clock and the actual clock are different

          if (conn->last_anchor_info_is_valid != 0) {

            int64_t time_since_last_update =
                get_absolute_time_in_ns() - conn->last_anchor_time_of_update;
            if (time_since_last_update > 5000000000) {
              int64_t duration_of_mastership = time_now - start_of_mastership;
              debug(2,
                    "Connection %d: Master clock has changed to %" PRIx64
                    ". History: %.3f milliseconds.",
                    conn->connection_number, actual_clock_id, 0.000001 * duration_of_mastership);

              // Now, the thing is that while the anchor clock and master clock for a
              // buffered session start off the same,
              // the master clock can change without the anchor clock changing.
              // SPS gives the new master clock time to settle down and then
              // calculates the appropriate offset to it by
              // calculating back from the local anchor information and the new clock's
              // advertised offset.

              conn->anchor_time = conn->last_anchor_local_time + actual_offset;
              conn->anchor_clock = actual_clock_id;
            }

          } else {
            response = clock_not_valid; // no current clock information and no previous clock info
          }
        }

      } else {
        // debug(1, "mastership time: %f s.", time_since_start_of_mastership * 0.000000001);
        response = clock_not_valid; // hasn't been master for long enough...
      }
    }

    // here, check and update the clock status
    if ((clock_status_t)response != conn->clock_status) {
      switch (response) {
      case clock_ok:
        debug(2, "Connection %d: NQPTP master clock %" PRIx64 ".", conn->connection_number,
              actual_clock_id);
        break;
      case clock_not_ready:
        debug(2, "Connection %d: NQPTP master clock %" PRIx64 " is available but not ready.",
              conn->connection_number, actual_clock_id);
        break;
      case clock_service_unavailable:
        debug(1, "Connection %d: NQPTP clock is not available.", conn->connection_number);
        warn("Can't access the NQPTP clock. Is NQPTP running?");
        break;
      case clock_access_error:
        debug(2, "Connection %d: Error accessing the NQPTP clock interface.",
              conn->connection_number);
        break;
      case clock_data_unavailable:
        debug(1, "Connection %d: Can not access NQPTP clock information.", conn->connection_number);
        break;
      case clock_no_master:
        debug(2, "Connection %d: No NQPTP master clock.", conn->connection_number);
        break;
      case clock_no_anchor_info:
        debug(2, "Connection %d: Awaiting clock anchor information.", conn->connection_number);
        break;
      case clock_version_mismatch:
        debug(2, "Connection %d: NQPTP clock interface mismatch.", conn->connection_number);
        warn(
            "This version of Shairport Sync is not compatible with the installed version of NQPTP. "
            "Please update.");
        break;
      case clock_not_synchronised:
        debug(1, "Connection %d: NQPTP clock is not synchronised.", conn->connection_number);
        break;
      case clock_not_valid:
        debug(2, "Connection %d: NQPTP clock information is not valid.", conn->connection_number);
        break;
      default:
        debug(1, "Connection %d: NQPTP clock reports an unrecognised status: %u.",
              conn->connection_number, response);
        break;
      }
      conn->clock_status = response;
    }

    if (conn->last_anchor_info_is_valid != 0) {
      if (anchorRTP != NULL) {
        // Use the current rate in case the stream format has changed.
        int32_t added_latency = (int32_t)(config.audio_backend_latency_offset * conn->input_rate);
        *anchorRTP = conn->last_anchor_rtptime - added_latency;
#ifdef CONFIG_SHIRI
        // Advance buffered AP2 within the bounded private handoff budget.
        // Advance only this backend mapping; retain the source clock/anchor and
        // leave realtime/AP1 latency negotiation and fallback behaviour unchanged.
        if (config.output && strcmp(config.output->name, "shiri") == 0 &&
            conn->airplay_type == ap_2 && conn->airplay_stream_type == buffered_stream)
          *anchorRTP += (uint32_t)(((uint64_t)config.shiri_buffered_audio_advance_ms *
                                    conn->input_rate) / 1000);
#endif
      }
      if (anchorLocalTime != NULL)
        *anchorLocalTime = conn->last_anchor_local_time;
    }
  }
  return response;
}

int frame_to_ptp_local_time(uint32_t timestamp, uint64_t *time, rtsp_conn_info *conn) {
  int result = -1;
  uint32_t anchor_rtptime = 0;
  uint64_t anchor_local_time = 0;
  if ((conn->input_rate != 0) && (get_ptp_anchor_local_time_info(conn, &anchor_rtptime, &anchor_local_time) == clock_ok)) {
    int32_t frame_difference = timestamp - anchor_rtptime;
    int64_t time_difference = frame_difference;
    time_difference = time_difference * 1000000000;
    time_difference = time_difference / conn->input_rate;
    uint64_t ltime = anchor_local_time + time_difference;
    *time = ltime;
    result = 0;
  } else {
    debug(4, "frame_to_ptp_local_time can't get anchor local time information");
  }
  return result;
}

int local_ptp_time_to_frame(uint64_t time, uint32_t *frame, rtsp_conn_info *conn) {
  int result = -1;
  uint32_t anchor_rtptime = 0;
  uint64_t anchor_local_time = 0;
  if ((conn->input_rate != 0) && (get_ptp_anchor_local_time_info(conn, &anchor_rtptime, &anchor_local_time) == clock_ok)) {
    int64_t time_difference = time - anchor_local_time;
    int64_t frame_difference = time_difference;
    frame_difference = frame_difference * conn->input_rate; // but this is by 10^9
    frame_difference = frame_difference / 1000000000;
    int32_t fd32 = frame_difference;
    uint32_t lframe = anchor_rtptime + fd32;
    *frame = lframe;
    result = 0;
  } else {
    debug(2, "local_ptp_time_to_frame can't get anchor local time information");
  }
  return result;
}

static void realtime_window(rtsp_conn_info *conn,uint32_t frame_1,uint32_t frame_2){
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
static uint32_t ap1_window(rtsp_conn_info *conn,uint32_t la){
                int32_t latency_offset =
                    (int32_t)(config.audio_backend_latency_offset * conn->input_rate);

                // debug(1,"latency offset is %" PRId32 ", input rate is %u", latency_offset,
                // conn->input_rate);
                int32_t adjusted_latency = latency_offset + (int32_t)la;
                if ((adjusted_latency < 0) ||
                    (adjusted_latency >
                     (int32_t)(conn->frames_per_packet *
                               (BUFFER_FRAMES - config.minimum_free_buffer_headroom))))
                  warn("audio_backend_latency_offset out of range -- ignored.");
                else
                  la = adjusted_latency;

return la;}
static int init(int argc, char **argv) {
  const char *path, *volume_path;
  (void)argv;
  if ((argc < -1 || argc > 0) || !config.cfg || !config_lookup_non_empty_string(config.cfg, "shiri.socket", &path) ||
      !config_lookup_int(config.cfg, "shiri.peer_uid", &peer_uid) || peer_uid < 0)
    die("Shiri backend requires a private socket and exact peer_uid.");
  socket_path = strdup(path);
  if (!socket_path || strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path))
    die("Shiri native PCM socket path is too long.");
  if (!config_lookup_non_empty_string(config.cfg,"shiri.volume_socket",&volume_path) || volume_init(volume_path))
    die("Shiri backend requires a private receiver volume endpoint.");
  config.shiri_buffered_audio_advance_ms = 0;
  if (config_lookup(config.cfg, "shiri.buffered_audio_advance_ms") &&
      (!config_lookup_int(config.cfg, "shiri.buffered_audio_advance_ms",
                          &config.shiri_buffered_audio_advance_ms) ||
       config.shiri_buffered_audio_advance_ms < 0 ||
       config.shiri_buffered_audio_advance_ms > 600))
    die("Shiri buffered_audio_advance_ms must be an integer from 0 to 600.");
  config.audio_backend_buffer_desired_length = 0.15;
  config.audio_backend_latency_offset = 0;
  parse_audio_options("shiri", (1u << SPS_FORMAT_S16_LE), (1u << SPS_RATE_48000), (1u << 2));
  return 0;
}

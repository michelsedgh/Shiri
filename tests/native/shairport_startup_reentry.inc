      if (conn->player_thread) {
        debug(1, "stopping a running player during setup phase 2");
        player_stop(conn); // this nulls the player_thread and cancels the threads...
        activity_monitor_signify_activity(0); // inactive, and should be after command_stop()
      } else if (conn->rtp_realtime_audio_started || conn->rtp_buffered_audio_started ||
                 conn->rtp_ap2_control_started) {
        /* A prior PTP type130 SETUP can own a control receiver without a
         * player. Retire it before this SETUP replaces any socket or handle. */
        player_unstarted_session_abort(conn);
        activity_monitor_signify_activity(0);
      }


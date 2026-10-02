#ifdef CONFIG_AIRPLAY_2
/* Only a successful pthread_create transfers a receive thread to this conn.
 * Clear ownership before joining so failed SETUP and normal player teardown
 * share one cleanup path without joining absent or already-retired handles. */
static void player_ap2_receivers_stop(rtsp_conn_info *conn) {
  if (conn->rtp_realtime_audio_started) {
    conn->rtp_realtime_audio_started = 0;
    pthread_cancel(conn->rtp_realtime_audio_thread);
    pthread_join(conn->rtp_realtime_audio_thread, NULL);
  }
  if (conn->rtp_buffered_audio_started) {
    conn->rtp_buffered_audio_started = 0;
    pthread_cancel(conn->rtp_buffered_audio_thread);
    pthread_join(conn->rtp_buffered_audio_thread, NULL);
  }
  if (conn->rtp_ap2_control_started) {
    conn->rtp_ap2_control_started = 0;
    pthread_cancel(conn->rtp_ap2_control_thread);
    pthread_join(conn->rtp_ap2_control_thread, NULL);
  }
}
#endif

static void player_unstarted_session_abort_locked(rtsp_conn_info *conn) {
  int old_state;
  if (!conn || conn->player_thread)
    return; /* A published player owns its receive threads and exact route. */
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_state);
  if (config.output->session_end)
    config.output->session_end(conn->connection_number);
#ifdef CONFIG_AIRPLAY_2
  if (conn->airplay_type == ap_2) {
    int receiving = conn->rtp_realtime_audio_started || conn->rtp_buffered_audio_started ||
                    conn->rtp_ap2_control_started;
    player_ap2_receivers_stop(conn);
    if (receiving)
      ptp_send_control_message_string("E");
    conn->rtp_running = 0;
  }
#endif
  pthread_setcancelstate(old_state, NULL);
}

void player_unstarted_session_abort(rtsp_conn_info *conn) {
  if (!conn)
    return;
  /* player_stop clears player_thread before joining it while holding this
   * mutex. A canceled RTSP thread must wait for that existing cleanup rather
   * than race its receive-thread joins after seeing a transient NULL pointer. */
  pthread_mutex_lock_and_cleanup_push(&conn->player_create_delete_mutex);
  player_unstarted_session_abort_locked(conn);
  pthread_cleanup_pop(1);
}

struct player_unstarted_session {
  rtsp_conn_info *conn;
  pthread_t *thread;
};

static void player_unstarted_session_cleanup(void *arg) {
  struct player_unstarted_session *pending = arg;
  player_unstarted_session_abort_locked(pending->conn);
  free(pending->thread);
}

int player_play(rtsp_conn_info *conn) {
  int result = 0;
  debug(2, "Connection %d: player_play.", conn->connection_number);
  command_start(); // before startup, and before the prepare() method runs
  // give the output device as much advance warning as possible to get ready
  // and make sure it's done before launching the player thread
  if (config.output->prepare)
    config.output->prepare(); // give the backend its first chance to prepare itself, knowing it has
                              // access to the output device (i.e. knowing that it should not be in
                              // use by another program at this time).

  pthread_mutex_lock_and_cleanup_push(&conn->player_create_delete_mutex);
  if (conn->player_thread == NULL) {
    pthread_t *pt = malloc(sizeof(pthread_t));
    if (pt == NULL)
      die("Couldn't allocate space for pthread_t");

    struct player_unstarted_session pending = {conn, pt};
    pthread_cleanup_push(player_unstarted_session_cleanup, (void *)&pending);
    /* Complete BEGIN and its exact selected-output START barrier before the
     * stream SETUP response lets the sender establish the first presentation
     * anchor. Reuse the existing fenced route; do not create a warm-up owner. */
    if (config.output->session_begin) {
#ifdef CONFIG_AIRPLAY_2
      config.output->session_begin(conn->connection_number, conn->airplay_gid,
                                   conn->groupContainsGroupLeader, conn->airplay_type == ap_2,
                                   suggested_volume(conn));
#else
      config.output->session_begin(conn->connection_number, NULL, 0, 0, suggested_volume(conn));
#endif
    }

    if (config.output->session_retired && config.output->session_retired(conn->connection_number)) {
      warn("Connection %d: output preparation failed before playback.", conn->connection_number);
      result = EIO;
    } else {
      result = named_pthread_create_with_priority(pt, 3, player_thread_func, (void *)conn,
                                                  "player_%d", conn->connection_number);
      if (result)
        debug(1, "Connection %d: error creating player_thread: %s", conn->connection_number,
              strerror(result));
      else
        conn->player_thread = pt; // transfer cleanup ownership after creation
    }
    pthread_cleanup_pop(result != 0);
  } else {
    debug(1, "Connection %d: player thread already exists.", conn->connection_number);
  }
  pthread_cleanup_pop(1); // release the player_create_delete_mutex
  if (result)
    return result;
#ifdef CONFIG_METADATA
  send_ssnc_metadata('pbeg', NULL, 0, 1); // contains cancellation points
#endif
  conn->is_playing = 1;
  return 0;
}

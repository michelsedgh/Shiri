/* Unchanged pinned OwnTone d6fb3edf5831de38134ebd92fcf09a730ddd37aa excerpts.
 * Actual --source checks verify these exact functions before executing them. */

static void
raop_status(struct raop_session *rs)
{
  enum output_device_state state;

  switch (rs->state)
    {
      case RAOP_STATE_PASSWORD:
	state = OUTPUT_STATE_PASSWORD;
	break;
      case RAOP_STATE_FAILED:
	state = OUTPUT_STATE_FAILED;
	break;
      case RAOP_STATE_STOPPED:
	state = OUTPUT_STATE_STOPPED;
	break;
      case RAOP_STATE_STARTUP ... RAOP_STATE_RECORD:
	state = OUTPUT_STATE_STARTUP;
	break;
      case RAOP_STATE_CONNECTED:
	state = OUTPUT_STATE_CONNECTED;
	break;
      case RAOP_STATE_STREAMING:
	state = OUTPUT_STATE_STREAMING;
	break;
      case RAOP_STATE_TEARDOWN:
	DPRINTF(E_LOG, L_RAOP, "Bug! raop_status() called with transitional state (TEARDOWN)\n");
	state = OUTPUT_STATE_STOPPED;
	break;
      default:
	DPRINTF(E_LOG, L_RAOP, "Bug! Unhandled state in raop_status(): %d\n", rs->state);
	state = OUTPUT_STATE_FAILED;
    }

  outputs_cb(rs->callback_id, rs->device_id, state);
  rs->callback_id = -1;
}

static void
session_failure(struct raop_session *rs)
{
  /* Session failed, let our user know */
  if (rs->state != RAOP_STATE_PASSWORD)
    rs->state = RAOP_STATE_FAILED;

  raop_status(rs);

  session_cleanup(rs);
}

static int
raop_check_cseq(struct raop_session *rs, struct evrtsp_request *req)
{
  return 0;
}

static void
raop_cb_set_volume(struct evrtsp_request *req, void *arg)
{
  struct raop_session *rs = arg;
  int ret;

  rs->reqs_in_flight--;

  if (!req)
    goto error;

  if (req->response_code != RTSP_OK)
    {
      DPRINTF(E_LOG, L_RAOP, "SET_PARAMETER request to '%s' failed for stream volume: %d %s\n", rs->devname, req->response_code, req->response_code_line);

      goto error;
    }

  ret = raop_check_cseq(rs, req);
  if (ret < 0)
    goto error;

  /* Let our user know */
  raop_status(rs);

  if (!rs->reqs_in_flight)
    evrtsp_connection_set_closecb(rs->ctrl, raop_rtsp_close_cb, rs);

  return;

 error:
  session_failure(rs);
}

static void
raop_cb_flush(struct evrtsp_request *req, void *arg)
{
  struct raop_session *rs = arg;
  int ret;

  rs->reqs_in_flight--;

  if (!req)
    goto error;

  if (req->response_code != RTSP_OK)
    {
      DPRINTF(E_LOG, L_RAOP, "FLUSH request to '%s' failed: %d %s\n", rs->devname, req->response_code, req->response_code_line);

      goto error;
    }

  ret = raop_check_cseq(rs, req);
  if (ret < 0)
    goto error;

  rs->state = RAOP_STATE_CONNECTED;

  /* Let our user know */
  raop_status(rs);

  if (!rs->reqs_in_flight)
    evrtsp_connection_set_closecb(rs->ctrl, raop_rtsp_close_cb, rs);

  return;

 error:
  session_failure(rs);
}

static void
raop_cb_metadata(struct evrtsp_request *req, void *arg)
{
  struct raop_session *rs = arg;
  int ret;

  rs->reqs_in_flight--;

  if (!req)
    goto error;

  if (req->response_code != RTSP_OK)
    DPRINTF(E_WARN, L_RAOP, "SET_PARAMETER metadata/artwork/progress request to '%s' failed (proceeding anyway): %d %s\n", rs->devname, req->response_code, req->response_code_line);

  ret = raop_check_cseq(rs, req);
  if (ret < 0)
    goto error;

  /* No callback to player, user doesn't want/need to know about the status
   * of metadata requests unless they cause the session to fail.
   */

  if (!rs->reqs_in_flight)
    evrtsp_connection_set_closecb(rs->ctrl, raop_rtsp_close_cb, rs);

  return;

 error:
  session_failure(rs);
}

#define CAST_STATE_F_STARTUP         (1 << 13)
// The receiver app is ready
#define CAST_STATE_F_APP_READY       (1 << 14)
// Media is playing in the receiver app
#define CAST_STATE_F_STREAMING       (1 << 15)

// Beware, the order of this enum has meaning
enum cast_state
{
  // Something bad happened during a session
  CAST_STATE_FAILED          = 0,
  // No session allocated
  CAST_STATE_NONE            = 1,
  // Session allocated, but no connection
  CAST_STATE_DISCONNECTED    = CAST_STATE_F_STARTUP | 0x01,
  // TCP connect, TLS handshake, CONNECT and GET_STATUS request
  CAST_STATE_CONNECTED       = CAST_STATE_F_STARTUP | 0x02,
  // Receiver app has been launched
  CAST_STATE_APP_LAUNCHED    = CAST_STATE_F_STARTUP | 0x03,
  // CONNECT, GET_STATUS and OFFER made to receiver app
  CAST_STATE_APP_READY       = CAST_STATE_F_APP_READY,
  // Buffering packets (playback not started yet)
  CAST_STATE_BUFFERING       = CAST_STATE_F_APP_READY | 0x01,
  // Streaming (playback started)
  CAST_STATE_STREAMING       = CAST_STATE_F_APP_READY | CAST_STATE_F_STREAMING,
};


enum cast_msg_types
{
  UNKNOWN,
  PING,
  PONG,
  CONNECT,
  CLOSE,
  GET_STATUS,
  RECEIVER_STATUS,
  LAUNCH,
  LAUNCH_OLD,
  LAUNCH_ERROR,
  STOP,
  MEDIA_CONNECT,
  MEDIA_CLOSE,
  OFFER,
  ANSWER,
  MEDIA_GET_STATUS,
  MEDIA_STATUS,
  SET_VOLUME,
  PRESENTATION,
  GET_CAPABILITIES,
  CAPABILITIES_RESPONSE,
};

struct cast_msg_basic
{
  enum cast_msg_types type;
  char *tag;       // Used for looking up incoming message type
  char *namespace;
  char *payload;

  int flags;
};

struct cast_msg_payload
{
  enum cast_msg_types type;
  unsigned int request_id;
  const char *app_id;
  const char *session_id;
  const char *transport_id;
  const char *player_state;
  const char *result;
  json_object *receiver_applications;
  bool receiver_status_valid;
  unsigned int media_session_id;
  unsigned short udp_port;
};


struct cast_rtcp_packet_feedback;

static void
cast_status(struct cast_session *cs)
{
  enum output_device_state state;

  switch (cs->state)
    {
      case CAST_STATE_FAILED:
	state = OUTPUT_STATE_FAILED;
	break;
      case CAST_STATE_NONE:
	state = OUTPUT_STATE_STOPPED;
	break;
      case CAST_STATE_DISCONNECTED ... CAST_STATE_APP_LAUNCHED:
	state = OUTPUT_STATE_STARTUP;
	break;
      case CAST_STATE_APP_READY ... CAST_STATE_BUFFERING:
	state = OUTPUT_STATE_CONNECTED;
	break;
      case CAST_STATE_STREAMING:
	state = OUTPUT_STATE_STREAMING;
	break;
      default:
	DPRINTF(E_LOG, L_CAST, "Bug! Unhandled state in cast_status()\n");
	state = OUTPUT_STATE_FAILED;
    }

  outputs_cb(cs->callback_id, cs->device_id, state);
  cs->callback_id = -1;
}

static void
cast_cb_volume(struct cast_session *cs, struct cast_msg_payload *payload)
{
  cast_status(cs);
}

static void
cast_session_shutdown(struct cast_session *cs, enum cast_state wanted_state)
{
  int pending;
  int ret;

  if (cs->state == wanted_state)
    {
      cast_status(cs);
      return;
    }
  else if (cs->state < wanted_state)
    {
      DPRINTF(E_LOG, L_CAST, "Bug! Shutdown request got wanted_state (%d) that is higher than current state (%d)\n", wanted_state, cs->state);
      return;
    }

  cs->wanted_state = wanted_state;

  pending = 0;
  switch (cs->state)
    {
      case CAST_STATE_STREAMING:
      case CAST_STATE_BUFFERING:
      case CAST_STATE_APP_READY:
	cast_disconnect(cs->udp_fd);
	cs->udp_fd = -1;
	ret = cast_msg_send(cs, MEDIA_CLOSE, NULL);
	cs->state = CAST_STATE_APP_LAUNCHED;
	if ((ret < 0) || (wanted_state >= CAST_STATE_APP_LAUNCHED))
	  break;

	/* FALLTHROUGH */

      case CAST_STATE_APP_LAUNCHED:
	ret = cast_msg_send(cs, STOP, cast_cb_stop);
	pending = 1;
	break;

      case CAST_STATE_CONNECTED:
	ret = cast_msg_send(cs, CLOSE, NULL);
	if (ret == 0)
	  gnutls_bye(cs->tls_session, GNUTLS_SHUT_RDWR);
	cast_disconnect(cs->server_fd);
	cs->server_fd = -1;
	cs->state = CAST_STATE_DISCONNECTED;
	break;

      case CAST_STATE_DISCONNECTED:
	ret = 0;
	break;

      default:
	DPRINTF(E_LOG, L_CAST, "Bug! Shutdown doesn't know how to handle current state\n");
	ret = -1;
    }

  // We couldn't talk to the device, tell the user and clean up
  if (ret < 0)
    {
      cs->state = CAST_STATE_FAILED;
      cast_status(cs);
      cast_session_cleanup(cs);
      return;
    }

  // If pending callbacks then we let them take care of the rest
  if (pending)
    return;

  // Asked to destroy the session
  if (wanted_state == CAST_STATE_NONE || wanted_state == CAST_STATE_FAILED)
    {
      cs->state = wanted_state;
      cast_status(cs);
      cast_session_cleanup(cs);
      return;
    }

  cast_status(cs);
}
static void
cast_msg_parse_free(void *haystack)
{
#ifdef HAVE_JSON_C_OLD
  json_object_put((json_object *)haystack);
#else
  if (json_object_put((json_object *)haystack) != 1)
    DPRINTF(E_LOG, L_CAST, "Memleak: JSON parser did not free object\n");
#endif
}

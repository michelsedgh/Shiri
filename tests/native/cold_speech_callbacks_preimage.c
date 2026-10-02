/* Exact composed pinned29.3 callback registry, source-transition layer c9120094. */
static void
callback_remove(struct output_device *device)
{
  int callback_id;

  if (!device)
    return;

  for (callback_id = 0; callback_id < ARRAY_SIZE(outputs_cb_register); callback_id++)
    {
      if (outputs_cb_register[callback_id].device == device)
	{
	  DPRINTF(E_DBG, L_PLAYER, "Removing callback to %s, id %d\n", player_pmap(outputs_cb_register[callback_id].cb), callback_id);
	  memset(&outputs_cb_register[callback_id], 0, sizeof(struct outputs_callback_register));
	}
    }
}

static int
callback_add(struct output_device *device, output_status_cb cb)
{
  int callback_id;

  if (!cb)
    return -1;

  if (outputs_callback_token >= INT_MAX)
    {
      DPRINTF(E_LOG, L_PLAYER, "Output callback identities exhausted; restart this backend\n");
      return -1; /* Never wrap and mistake a late old callback for a new one. */
    }

  // We will replace any previously registered callbacks, since that's what the
  // player expects
  callback_remove(device);

  // Find a free slot in the queue
  for (callback_id = 0; callback_id < ARRAY_SIZE(outputs_cb_register); callback_id++)
    {
      if (outputs_cb_register[callback_id].cb == NULL)
	break;
    }

  if (callback_id == ARRAY_SIZE(outputs_cb_register))
    {
      DPRINTF(E_LOG, L_PLAYER, "Output callback queue is full! (size is %d)\n", OUTPUTS_MAX_CALLBACKS);
      return -1;
    }

  outputs_cb_register[callback_id].token = (int)++outputs_callback_token;
  outputs_cb_register[callback_id].device_id = device->id;
  outputs_cb_register[callback_id].cb = cb;
  outputs_cb_register[callback_id].device = device; // Don't dereference this later, it might become invalid!

  DPRINTF(E_DBG, L_PLAYER, "Registered callback to %s with id %d (device %p, %s)\n", player_pmap(cb), callback_id, device, device->name);

  int active = 0;
  for (int i = 0; i < ARRAY_SIZE(outputs_cb_register); i++)
    if (outputs_cb_register[i].cb)
      active++;

  DPRINTF(E_DBG, L_PLAYER, "Number of active callbacks: %d\n", active);

  return outputs_cb_register[callback_id].token;
}

void
outputs_cb(int callback_id, uint64_t device_id, enum output_device_state state)
{
  size_t slot;

  if (callback_id < 0)
    return;
  for (slot = 0; slot < ARRAY_SIZE(outputs_cb_register); slot++)
    if (outputs_cb_register[slot].cb && outputs_cb_register[slot].token == callback_id)
      break;
  if (slot == ARRAY_SIZE(outputs_cb_register)
      || outputs_cb_register[slot].device_id != device_id)
    {
      DPRINTF(E_DBG, L_PLAYER, "Ignoring stale or mismatched output callback %d\n", callback_id);
      return;
    }

  outputs_cb_register[slot].ready = true;
  outputs_cb_register[slot].state = state;
  event_active(outputs_deferredev, 0, 0);
}


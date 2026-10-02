# Room volume and saved speaker balance

A room has one master volume from 0 to 100. A phone volume event and a web room
volume change edit this same saved value. Ducking for speech changes the music
mix gain; it does not change the room master or a speaker's saved balance.

Each physical speaker also has a saved `balance_percent` from 0 to 100, default
100. This is an attenuation setting for balancing speakers. Its OwnTone output
gain is `floor(room_volume * balance_percent / 100)`. A room at 15 with balances
60 and 80 therefore keeps master 15 and sends output gains 9 and 12. Even when
every speaker is attenuated, the largest output gain does not become the master.
Muting, reconnecting, or removing and reassigning a speaker preserves its trim.
A Bluetooth speaker group presented by one Bluetooth endpoint has one trim.

SQLite schema 3 retains balance by the same physical identity used for speaker
timing profiles. Migrations audit existing schema 1/2 intent before adding the
table. Replacing a local audio card loads the replacement's own profile. Default
100 is omitted from calibration fingerprints so saved older measurement evidence
remains readable; changing a trim makes a previous calibration configuration
stale. Back up the database before upgrading; an older schema 2 binary cannot
open a schema 3 database.

`RoomService.balance(room_id, speaker_id, balance_percent, expected_revision)`
persists the setting before reconciliation. The room actor stages all saved trims
with `POST /api/player/shiri-volume-settings` before changing output selection,
under the existing speaker lease. The request contains `volume` and an `outputs`
array of exact `id`/`balance_percent` entries. The client requires an exact
acknowledgment and separate master/trim readback. Volume and balance edits on an
unchanged running assignment do not pause, reselect, or restart the route. A lost
gain acknowledgment leaves the route owned and playing, marks it pending, and
retries the same durable settings. Process or network failures still use normal
health recovery.

The OwnTone `balance1` patch enables an independent master and stages gains before
selection. It retains the assigned group's trims across output rediscovery and
AirPlay protocol replacement. Native phone volume continues through the exact
source-fenced `shiri-volume` endpoint, which uses the same master and saved trims.
The runtime requires the matching `owner1-balance1` backend version; deploying
the Python changes alone against the old OwnTone build is unsupported.

The current pinned Shairport receiver does not provide a web-to-iPhone display
update. Two-way display synchronization requires the separate AP2 event-channel
backport, a bounded receiver control path, exact launch/session fences and phone
echo suppression. A successful OwnTone volume readback proves backend intent,
not that the iPhone slider or a physical speaker has acknowledged the change.

Verification includes real SQLite migration/reopen/reassignment transactions,
native phone-event revision bridge tests, an HTTP client acknowledgment/readback
test, and room-actor tests that forbid selection/restart on gain changes. The
native build check compiles actual OwnTone gain/selection functions and the JSON
parser with address/undefined-behavior sanitizers. It exercises cold staging,
all-trimmed/single-trimmed outputs, mute/unmute, rediscovery, lost acknowledgment
replay, and invalid identities; the original master-recalculation algorithm fails
the same fixture. Actual phone, speaker gain and playback continuity tests remain
necessary after a coherent backend deployment.

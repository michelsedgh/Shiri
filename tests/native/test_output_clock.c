/* Actual OwnTone config/discovery/JSON bodies are inserted by the checker.
 * All dependencies are local bounded fakes. No socket or audio is opened. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>

#define DPRINTF(...) do { } while (0)
#define CHECK_NULL(log, expression) assert((expression) != NULL)
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define AIRPLAY_QUALITY_SAMPLE_RATE_DEFAULT 44100
#define AIRPLAY_QUALITY_BITS_PER_SAMPLE_DEFAULT 16
#define AIRPLAY_QUALITY_CHANNELS_DEFAULT 2
#define AIRPLAY_MD_WANTS_ARTWORK 1
#define AIRPLAY_MD_WANTS_PROGRESS 2
#define AIRPLAY_MD_WANTS_TEXT 4
#define OUTPUTS_DEVICE_DISPLAY_SELECTED(device) ((device)->selected)

enum output_types { OUTPUT_TYPE_AIRPLAY, OUTPUT_TYPE_RAOP, OUTPUT_TYPE_ALSA };
enum output_device_state { OUTPUT_STATE_STOPPED };
enum media_format { MEDIA_FORMAT_UNKNOWN = 0, MEDIA_FORMAT_ALAC = 1, MEDIA_FORMAT_PCM = 2,
                    MEDIA_FORMAT_FIRST = 1, MEDIA_FORMAT_LAST = 2 };
#define MEDIA_FORMAT_NEXT(format) ((enum media_format)((format) << 1))
struct media_quality { int sample_rate, bits_per_sample, channels; };
struct event;
/* @ACTUAL_AIRPLAY_ENUM@ */
/* @ACTUAL_AIRPLAY_EXTRA@ */
/* @ACTUAL_OUTPUT_DEVICE@ */
/* @ACTUAL_SPEAKER_INFO@ */

typedef struct cfg_t {
  const char *title, *protocol, *nickname, *password;
  bool ptp_disable, exclude, permanent, exclusive, airplay2_disable;
  struct cfg_t *sections[32];
  int count;
} cfg_t;
typedef struct cfg_opt_t { int nvalues; } cfg_opt_t;
static cfg_t root_config, legacy_config;
static cfg_t *cfg = &root_config;
static bool legacy_present, airplay_ptp_is_disabled;
static int cfg_size(cfg_t *root, const char *name) {
  assert(strcmp(name, "shiri_airplay_timing") == 0); return root->count;
}
static cfg_t *cfg_getnsec(cfg_t *root, const char *name, unsigned int index) {
  assert(strcmp(name, "shiri_airplay_timing") == 0); assert(index < (unsigned int)root->count);
  return root->sections[index];
}
static const char *cfg_title(cfg_t *section) { return section->title; }
static cfg_t *cfg_gettsec(cfg_t *root, const char *kind, const char *title) {
  if (strcmp(kind, "airplay") == 0) return legacy_present ? &legacy_config : NULL;
  assert(strcmp(kind, "shiri_airplay_timing") == 0);
  for (int i = 0; i < root->count; i++)
    if (strcmp(root->sections[i]->title, title) == 0) return root->sections[i];
  return NULL;
}
static const char *cfg_getstr(cfg_t *section, const char *key) {
  if (strcmp(key, "protocol") == 0) return section->protocol;
  if (strcmp(key, "nickname") == 0) return section->nickname;
  if (strcmp(key, "password") == 0) return section->password;
  assert(false); return NULL;
}
static bool cfg_getbool(cfg_t *section, const char *key) {
  if (strcmp(key, "ptp_disable") == 0) return section->ptp_disable;
  if (strcmp(key, "exclude") == 0) return section->exclude;
  if (strcmp(key, "permanent") == 0) return section->permanent;
  if (strcmp(key, "exclusive") == 0) return section->exclusive;
  if (strcmp(key, "airplay2_disable") == 0) return section->airplay2_disable;
  assert(false); return false;
}
static cfg_opt_t *cfg_getopt(cfg_t *section, const char *key) {
  (void)section; assert(strcmp(key, "reconnect") == 0); return NULL;
}
static bool cfg_opt_getnbool(cfg_opt_t *option, unsigned int index) {
  (void)option; (void)index; return false;
}

struct keyval { char *keys[64], *values[64]; int count; };
static const char *keyval_get(struct keyval *values, const char *key) {
  for (int i = 0; i < values->count; i++)
    if (strcmp(values->keys[i], key) == 0) return values->values[i];
  return NULL;
}
static void keyval_add(struct keyval *values, const char *key, const char *value) {
  assert(values->count < 64); int i = values->count++;
  values->keys[i] = strdup(key); values->values[i] = strdup(value);
}
static void keyval_clear(struct keyval *values) {
  for (int i = 0; i < values->count; i++) { free(values->keys[i]); free(values->values[i]); }
  values->count = 0;
}
static int safe_hextou32(const char *text, uint32_t *result) {
  char *end; unsigned long long value = strtoull(text, &end, 16);
  if (end == text || value > UINT32_MAX) return -1;
  *result = value; return 0;
}
static int safe_hextou64(const char *text, uint64_t *result) {
  char *end; unsigned long long value = strtoull(text, &end, 16);
  if (end == text || *end) return -1;
  *result = value; return 0;
}
static bool quality_is_equal(struct media_quality *a, struct media_quality *b) {
  return a->sample_rate == b->sample_rate && a->bits_per_sample == b->bits_per_sample && a->channels == b->channels;
}
static const char *outputs_name(enum output_types type) {
  return type == OUTPUT_TYPE_AIRPLAY ? "AirPlay" : type == OUTPUT_TYPE_RAOP ? "RAOP" : "ALSA";
}
static bool outputs_exclusive_mode_get(void) { return false; }
static struct output_device *captured;
static unsigned int additions, removals;
static int player_device_add(struct output_device *device) {
  assert(!captured); captured = device; additions++; return 0;
}
static int player_device_remove(struct output_device *device) { (void)device; removals++; return -1; }
static int device_id_find_byname(uint64_t *id, const char *name) { (void)id; (void)name; return -1; }
static void outputs_device_free(struct output_device *device) {
  if (!device) return;
  struct airplay_extra *extra = device->extra_device_info;
  if (extra) { free(extra->mdns_name); free(extra); }
  free(device->name); free(device->v4_address); free(device->v6_address); free(device);
}
static struct media_quality airplay_quality_default = {44100,16,2};
static const char *airplay_devtype[] = { "apex2", "apex3", "atv", "atv4", "homepod", "other" };
/* @ACTUAL_FEATURES_MAP@ */
/* @ACTUAL_CONFIG_VALIDATOR@ */
/* @ACTUAL_DEVICE_ID_PARSE@ */
/* @ACTUAL_FEATURES_PARSE@ */
/* @ACTUAL_TIMING_SELECTOR@ */
/* @ACTUAL_DISCOVERY_CALLBACK@ */
/* @ACTUAL_DEVICE_INFO_COPY@ */

typedef struct json_object {
  char text[256];
  struct json_object *children[32];
  char keys[32][64];
  int count;
} json_object;
static json_object *json_object_new_object(void) { return calloc(1,sizeof(json_object)); }
static json_object *json_object_new_array(void) { return json_object_new_object(); }
static json_object *json_object_new_string(const char *value) {
  json_object *obj = json_object_new_object(); snprintf(obj->text,sizeof(obj->text),"%s",value); return obj;
}
static json_object *json_object_new_int(int value) {
  json_object *obj = json_object_new_object(); snprintf(obj->text,sizeof(obj->text),"%d",value); return obj;
}
static json_object *json_object_new_boolean(bool value) { return json_object_new_int(value); }
static void json_object_object_add(json_object *parent, const char *key, json_object *value) {
  assert(parent->count < 32); int i = parent->count++;
  snprintf(parent->keys[i],sizeof(parent->keys[i]),"%s",key); parent->children[i] = value;
}
static void json_object_array_add(json_object *parent, json_object *value) { json_object_object_add(parent,"item",value); }
static json_object *get(json_object *obj, const char *key) {
  for (int i=0;i<obj->count;i++) if (strcmp(obj->keys[i],key)==0) return obj->children[i];
  return NULL;
}
static void json_object_put(json_object *obj) {
  for (int i=0;i<obj->count;i++) { json_object_put(obj->children[i]); }
  free(obj);
}
static const char *media_format_to_string(enum media_format format) { return format == MEDIA_FORMAT_ALAC ? "alac" : "pcm"; }
/* @ACTUAL_JSON_SERIALIZATION@ */

struct airplay_master_session { bool use_ptp; };
struct airplay_session { struct airplay_master_session *master_session; };
struct evrtsp_request { int marker; };
static unsigned int ntp_setup, ptp_setup;
static int payload_make_setup_session_ntp(struct evrtsp_request *request, struct airplay_session *session, void *arg) {
  (void)request; (void)session; (void)arg; ntp_setup++; return 17;
}
static int payload_make_setup_session_ptp(struct evrtsp_request *request, struct airplay_session *session, void *arg) {
  (void)request; (void)session; (void)arg; ptp_setup++; return 23;
}
/* @ACTUAL_SETUP_DISPATCH@ */

static unsigned long assertions;
#define REQUIRE(test) do { assertions++; if (!(test)) { fprintf(stderr,"check failed at line %d\n",__LINE__); abort(); } } while(0)
static void reset(void) {
  outputs_device_free(captured); captured=NULL; memset(&root_config,0,sizeof(root_config));
  memset(&legacy_config,0,sizeof(legacy_config)); legacy_present=false; airplay_ptp_is_disabled=false;
}
static void discovered(const char *name, const char *id, bool ptp, bool audio, bool encryption, int family) {
  struct keyval txt={0}; uint64_t features = (audio ? UINT64_C(1)<<9 : 0) |
    (encryption ? UINT64_C(1)<<48 : 0) | (ptp ? UINT64_C(1)<<41 : 0);
  char encoded[48]; snprintf(encoded,sizeof(encoded),"0x%08" PRIx32 ",0x%08" PRIx32,(uint32_t)features,(uint32_t)(features>>32));
  keyval_add(&txt,"deviceid",id); keyval_add(&txt,"features",encoded); keyval_add(&txt,"model","test");
  airplay_device_cb(name,"_airplay._tcp","local","speaker.local",family,family==AF_INET?"192.0.2.1":"2001:db8::1",7000,&txt);
  keyval_clear(&txt);
}
static void verify_device(const char *mode, const char *expected_id) {
  REQUIRE(captured != NULL);
  struct airplay_extra *extra=captured->extra_device_info;
  REQUIRE(extra->use_ptp == (strcmp(mode,"ptp")==0));
  REQUIRE(captured->shiri_airplay_use_ptp == extra->use_ptp);
  struct player_speaker_info info;
  device_to_speaker_info(&info,captured,7); REQUIRE(strcmp(info.airplay_timing,mode)==0);
  json_object *obj=speaker_to_json(&info);
  REQUIRE(get(obj,"airplay_timing") != NULL); REQUIRE(strcmp(get(obj,"airplay_timing")->text,mode)==0);
  REQUIRE(strcmp(get(obj,"id")->text,expected_id)==0); json_object_put(obj);
  struct airplay_master_session master={.use_ptp=extra->use_ptp};
  struct airplay_session session={.master_session=&master};
  REQUIRE(payload_make_setup_session(NULL,&session,NULL)==(extra->use_ptp?23:17));
}
int main(void) {
  const char *invalid_ids[]={NULL,"","00","01","+1","-1"," 1","1 ","0x1","1.0","1e3","a","18446744073709551616","99999999999999999999999999999999999"};
  const char *invalid_protocols[]={NULL,"","auto","NTP","PTP","ntp "," ptp","udp"};
  cfg_t section={.title="92539824408726",.protocol="ntp"};
  reset(); REQUIRE(shiri_airplay_timing_validate(cfg)==0);
  root_config.sections[0]=&section; root_config.count=1;
  REQUIRE(shiri_airplay_timing_validate(cfg)==0);
  for (unsigned int i=0;i<ARRAY_SIZE(invalid_ids);i++) { section.title=invalid_ids[i]; REQUIRE(shiri_airplay_timing_validate(cfg)<0); }
  section.title="0"; REQUIRE(shiri_airplay_timing_validate(cfg)==0);
  section.title="18446744073709551615"; REQUIRE(shiri_airplay_timing_validate(cfg)==0);
  section.title="92539824408726";
  for (unsigned int i=0;i<ARRAY_SIZE(invalid_protocols);i++) { section.protocol=invalid_protocols[i]; REQUIRE(shiri_airplay_timing_validate(cfg)<0); }
  section.protocol="ptp"; REQUIRE(shiri_airplay_timing_validate(cfg)==0);
  cfg_t duplicate=section; root_config.sections[1]=&duplicate; root_config.count=2;
  REQUIRE(shiri_airplay_timing_validate(cfg)<0); root_config.count=1;
  for (unsigned int service=0;service<2;service++) for(unsigned int support=0;support<2;support++) {
    for (unsigned int legacy=0;legacy<2;legacy++) {
      reset(); legacy_present=true; legacy_config.ptp_disable=legacy; airplay_ptp_is_disabled=service;
      discovered("Living Room","54:2A:1B:5C:88:96",support,true,true,AF_INET);
      verify_device(support&&!service&&!legacy?"ptp":"ntp","92539824408726");
      for (unsigned int protocol=0;protocol<2;protocol++) {
        outputs_device_free(captured);captured=NULL;
        section.title="92539824408726";section.protocol=protocol?"ptp":"ntp";
        root_config.sections[0]=&section;root_config.count=1;
        discovered("Renamed Room","54:2A:1B:5C:88:96",support,true,true,AF_INET6);
        if(protocol&&(!support||service)) REQUIRE(captured==NULL);
        else verify_device(protocol?"ptp":"ntp","92539824408726");
        root_config.count=0;
      }
    }
  }
  reset(); section.title="92539824408726";section.protocol="ntp";
  root_config.sections[0]=&section;root_config.count=1;
  discovered("Duplicate Name","54:2A:1B:5C:88:96",true,true,true,AF_INET);verify_device("ntp","92539824408726");
  outputs_device_free(captured);captured=NULL;
  discovered("Duplicate Name","54:2A:1B:5C:88:97",true,true,true,AF_INET);verify_device("ptp","92539824408727");
  outputs_device_free(captured);captured=NULL;
  discovered("Duplicate Name","54:2A:1B:5C:88:96",true,false,true,AF_INET);REQUIRE(captured==NULL);
  discovered("Duplicate Name","54:2A:1B:5C:88:96",true,true,false,AF_INET);REQUIRE(captured==NULL);
  section.protocol="broken";discovered("Name","54:2A:1B:5C:88:96",true,true,true,AF_INET);REQUIRE(captured==NULL);
  reset();struct keyval features={0};keyval_add(&features,"SupportsPTP","1");
  section.title="18446744073709551615";section.protocol="ntp";root_config.sections[0]=&section;root_config.count=1;
  REQUIRE(airplay_shiri_timing_select(UINT64_MAX,NULL,&features)==0);
  section.title="0";REQUIRE(airplay_shiri_timing_select(0,NULL,&features)==0);keyval_clear(&features);
  struct output_device other={.id=17,.name="Other",.type_name="RAOP",.type=OUTPUT_TYPE_RAOP,.shiri_airplay_use_ptp=true};
  struct player_speaker_info info;device_to_speaker_info(&info,&other,1);
  REQUIRE(info.airplay_timing[0]==0);json_object *obj=speaker_to_json(&info);REQUIRE(get(obj,"airplay_timing")==NULL);json_object_put(obj);
  REQUIRE(ntp_setup>0&&ptp_setup>0);reset();
  printf("actual config/discovery/feature/JSON/SETUP dispatch passed: %lu assertions, %u accepted devices, %u removals\n",assertions,additions,removals);
  return 0;
}

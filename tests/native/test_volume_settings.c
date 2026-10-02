#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "shiri_volume_settings_json.h"
struct output_device { uint64_t id; int volume, relvol, shiri_balance_percent; bool shiri_balance_set, selected, prevent_playback, busy; struct output_device *next; const char *name; };
typedef void (*output_status_cb)(struct output_device *, int);
static struct output_device *outputs_device_list;
static int outputs_master_volume;
static bool shiri_volume_settings_active;
static struct shiri_volume_settings shiri_saved_volume_settings;
static int calls;
#define OUTPUTS_DEVICE_DISPLAY_SELECTED(d) ((d)->selected)
static struct output_device *outputs_device_get(uint64_t id) { for (struct output_device *d=outputs_device_list; d; d=d->next) if(d->id==id)return d; return NULL; }
static int outputs_device_volume_set(struct output_device *d, output_status_cb cb) { (void)cb; assert(d->selected); calls++; return 1; }
/* @ACTUAL_OUTPUT_VOLUME_FUNCTIONS@ */
static int parse(const char *text, struct shiri_volume_settings *s) { json_object *o=json_tokener_parse(text); int r=shiri_volume_settings_parse(o,s); if(o)json_object_put(o); return r; }
int main(void) {
 struct output_device b={.id=202,.volume=90}, a={.id=101,.volume=99,.next=&b}; outputs_device_list=&a; outputs_master_volume=99;
 struct shiri_volume_settings s;
 assert(parse("{\"volume\":27,\"outputs\":[{\"id\":\"101\",\"balance_percent\":60},{\"id\":\"202\",\"balance_percent\":80}]}",&s)==0);
 assert(outputs_shiri_volume_settings_set(&s,NULL)==0 && calls==0);
 assert(outputs_master_volume==27 && a.volume==16 && b.volume==21);
 outputs_device_select(&a,27); outputs_device_select(&b,27);
 assert(outputs_master_volume==27 && a.relvol==60 && b.relvol==80);
 assert(outputs_volume_set(15,NULL)==2 && a.volume==9 && b.volume==12);
 assert(outputs_master_volume==15 && calls==2);
 assert(outputs_volume_set(0,NULL)==2 && a.volume==0 && b.volume==0 && a.relvol==60 && b.relvol==80);
 outputs_device_deselect(&a); outputs_device_deselect(&b);
 assert(outputs_master_volume==0 && a.relvol==60 && b.relvol==80);
 assert(outputs_volume_set(85,NULL)==0);
 outputs_device_select(&a,85); outputs_device_select(&b,85);
 assert(outputs_master_volume==85 && a.volume==51 && b.volume==68);
 outputs_device_deselect(&b);
 assert(outputs_master_volume==85 && a.volume==51);
 struct output_device replacement={.id=101,.volume=99,.selected=true,.next=&b}; outputs_device_list=&replacement; vol_adjust();
 assert(outputs_master_volume==85 && replacement.relvol==60 && replacement.volume==51);
 outputs_device_list=&a;
 s.volume=85; s.count=1; s.outputs[0].id=101; s.outputs[0].balance_percent=50;
 assert(outputs_shiri_volume_settings_set(&s,NULL)==1 && a.volume==42 && a.relvol==50 && outputs_master_volume==85);
 int prior=calls; assert(outputs_shiri_volume_settings_set(&s,NULL)==0 && calls==prior);
 outputs_device_volume_register(&a,99,-1);
 assert(outputs_master_volume==85 && a.volume==42 && a.relvol==50);
 s.volume=10; s.outputs[0].id=999;
 assert(outputs_shiri_volume_settings_set(&s,NULL)==-1 && outputs_master_volume==85 && a.volume==42);
 s.count=2; s.outputs[0].id=101; s.outputs[1]=s.outputs[0];
 assert(outputs_shiri_volume_settings_set(&s,NULL)==-1 && outputs_master_volume==85);
 const char *bad[]={"{}","{\"volume\":true,\"outputs\":[]}","{\"volume\":27.0,\"outputs\":[]}","{\"volume\":-1,\"outputs\":[]}","{\"volume\":101,\"outputs\":[]}","{\"volume\":27,\"outputs\":[],\"extra\":0}","{\"volume\":27,\"outputs\":[{\"id\":\"01\",\"balance_percent\":60}]}","{\"volume\":27,\"outputs\":[{\"id\":\"18446744073709551616\",\"balance_percent\":60}]}","{\"volume\":27,\"outputs\":[{\"id\":101,\"balance_percent\":60}]}","{\"volume\":27,\"outputs\":[{\"id\":\"101\",\"balance_percent\":false}]}","{\"volume\":27,\"outputs\":[{\"id\":\"101\",\"balance_percent\":101}]}","{\"volume\":27,\"outputs\":[{\"id\":\"101\",\"balance_percent\":60},{\"id\":\"101\",\"balance_percent\":80}]}"};
 for(size_t i=0;i<sizeof(bad)/sizeof(bad[0]);i++)assert(parse(bad[i],&s)<0);
 assert(parse("{\"volume\":100,\"outputs\":[{\"id\":\"18446744073709551615\",\"balance_percent\":0}]}",&s)==0 && s.outputs[0].id==UINT64_MAX);
 json_object *obj=json_object_new_object(), *arr=json_object_new_array(); json_object_object_add(obj,"volume",json_object_new_int(50));json_object_object_add(obj,"outputs",arr);
 for(int i=0;i<513;i++){json_object *entry=json_object_new_object();char text[32];snprintf(text,sizeof(text),"%d",i);json_object_object_add(entry,"id",json_object_new_string(text));json_object_object_add(entry,"balance_percent",json_object_new_int(75));json_object_array_add(arr,entry);if(i==511)assert(shiri_volume_settings_parse(obj,&s)==0);}
 assert(shiri_volume_settings_parse(obj,&s)<0);json_object_put(obj);
 puts("stable master, saved trims, cold staging, mute/reselection, replay and strict parser: PASS"); return 0;
}

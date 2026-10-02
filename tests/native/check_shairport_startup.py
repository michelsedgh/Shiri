#!/usr/bin/python3
"""Compile exact native SETUP preparation and teardown with controlled callbacks.

Without --source, compile retained reference bodies. The installer supplies its
actual composed backend source and verifies exact bodies plus the clock gate.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
PATCH = REPOSITORY / "install/patches/shairport-5.5.2-native-startup.patch"
PATCH_SHA = 'bab272cab5764fc3ebb0f8169b6a94b8318b78f63e09fd7e368058901b9a71d8'
REFERENCE_SHA = '44ff6b55a3d67a84f004c3f4de1c4d42498d104df6653b17ac73e11e3f38f646'
REENTRY_SHA = '0c032639d89e6c4e937a608d92d7be3098c0d0e481009d405ed25cead1f95d58'

PRELUDE = r'''
#define _POSIX_C_SOURCE 200809L
#define CONFIG_AIRPLAY_2 1
#define CONFIG_METADATA 1
#include <assert.h>
#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

enum { ap_1=1, ap_2=2, realtime_stream=1, buffered_stream=2 };
typedef struct {
  int connection_number, groupContainsGroupLeader, airplay_type, airplay_stream_type;
  int own_airplay_volume_set, is_playing, rtp_running;
  int rtp_ap2_control_started, rtp_realtime_audio_started, rtp_buffered_audio_started;
  double own_airplay_volume;
  char *airplay_gid;
  pthread_t *player_thread;
  pthread_t rtp_realtime_audio_thread, rtp_buffered_audio_thread, rtp_ap2_control_thread;
  pthread_mutex_t player_create_delete_mutex;
} rtsp_conn_info;
struct output {
  void (*prepare)(void);
  void (*session_begin)(int,const char *,int,int,double);
  void (*session_end)(int);
  int (*session_retired)(int);
};
static struct { struct output *output; double airplay_volume; } config;
static int begin_count, end_count, created, metadata_count, prepared, fail_admission, fail_create;
static int joined, cancelled, ptp_end, started, block_begin, entered, release_begin;
static pthread_t cancel_ids[4], join_ids[4];
static pthread_mutex_t gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gate_condition = PTHREAD_COND_INITIALIZER;
static double expected_volume;
static rtsp_conn_info *expected_conn;
#define debug(...) ((void)0)
#define warn(...) ((void)0)
#define die(...) abort()
static void mutex_unlock(void *arg) { assert(pthread_mutex_unlock(arg)==0); }
#define pthread_mutex_lock_and_cleanup_push(mu) if(pthread_mutex_lock(mu)==0) pthread_cleanup_push(mutex_unlock,(void *)mu)
static int stub_cancel(pthread_t id) { assert(cancelled<4); cancel_ids[cancelled++]=id; return 0; }
static int stub_join(pthread_t id,void **arg) { (void)arg; assert(joined<4); join_ids[joined++]=id; return 0; }
#define pthread_cancel stub_cancel
#define pthread_join stub_join
static void ptp_send_control_message_string(const char *s) { assert(strcmp(s,"E")==0); ptp_end++; }
static void command_start(void) { started++; }
static void send_ssnc_metadata(int code,void *data,int n,int block) { assert(code=='pbeg');(void)data;(void)n;(void)block;metadata_count++; }
static double suggested_volume(rtsp_conn_info *conn) { return conn->own_airplay_volume_set ? conn->own_airplay_volume : config.airplay_volume; }
static void *player_thread_func(void *arg) { return arg; }
static int named_pthread_create_with_priority(pthread_t *pt,int priority,void *(*fn)(void *),void *arg,const char *fmt,int connection) {
  assert(prepared && priority==3 && fn==player_thread_func && arg==expected_conn);
  assert(strcmp(fmt,"player_%d")==0 && connection==expected_conn->connection_number);
  if(fail_create) return EAGAIN;
  created++; *pt=(pthread_t)(uintptr_t)91;return 0;
}
static void begin(int number,const char *group,int leader,int ap2,double volume) {
  int old_state;
  assert(number==expected_conn->connection_number && group==expected_conn->airplay_gid);
  assert(leader==expected_conn->groupContainsGroupLeader && ap2==(expected_conn->airplay_type==ap_2));
  assert(fabs(volume-expected_volume)<1e-12 && expected_conn->player_thread==NULL);
  begin_count++;
  // The real native callback disables cancellation until its bounded GRANT wait returns.
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE,&old_state);
  assert(pthread_mutex_lock(&gate)==0);
  entered=1; assert(pthread_cond_broadcast(&gate_condition)==0);
  while(block_begin && !release_begin) assert(pthread_cond_wait(&gate_condition,&gate)==0);
  assert(pthread_mutex_unlock(&gate)==0);
  prepared=1;
  pthread_setcancelstate(old_state,NULL);
  pthread_testcancel();
}
static void end(int number) { assert(number==expected_conn->connection_number);end_count++; }
static int retired(int number) { assert(number==expected_conn->connection_number);return fail_admission; }
static struct output out = {.session_begin=begin,.session_end=end,.session_retired=retired};
'''

SETUP_HELPERS = r'''
static int normal_stops, activity_stops;
static void activity_monitor_signify_activity(int value) { assert(value==0);activity_stops++; }
static int player_stop(rtsp_conn_info *conn) {
  assert(conn->player_thread);normal_stops++;
  free(conn->player_thread);conn->player_thread=NULL;
  player_ap2_receivers_stop(conn);return 0;
}
static void stream_setup_reentry(rtsp_conn_info *conn) {
'''

TRAILER = r'''
#undef pthread_cancel
#undef pthread_join
static void reset(rtsp_conn_info *c,int type) {
  memset(c,0,sizeof(*c));
  c->connection_number=13;c->airplay_gid="fenced-group";c->groupContainsGroupLeader=1;
  c->airplay_type=ap_2;c->airplay_stream_type=type;c->rtp_running=1;
  c->rtp_realtime_audio_thread=(pthread_t)(uintptr_t)21;
  c->rtp_buffered_audio_thread=(pthread_t)(uintptr_t)22;
  c->rtp_ap2_control_thread=(pthread_t)(uintptr_t)23;
  c->rtp_ap2_control_started=1;
  c->rtp_realtime_audio_started=(type==realtime_stream);
  c->rtp_buffered_audio_started=(type==buffered_stream);
  assert(pthread_mutex_init(&c->player_create_delete_mutex,NULL)==0);
  begin_count=end_count=created=metadata_count=prepared=fail_admission=fail_create=0;
  joined=cancelled=ptp_end=started=block_begin=entered=release_begin=0;
  config.output=&out;config.airplay_volume=-15;expected_volume=-15;expected_conn=c;
}
static void finish(rtsp_conn_info *c) {
  if(c->player_thread) free(c->player_thread);
  assert(pthread_mutex_trylock(&c->player_create_delete_mutex)==0);
  assert(pthread_mutex_unlock(&c->player_create_delete_mutex)==0);
  assert(pthread_mutex_destroy(&c->player_create_delete_mutex)==0);
}
static void check_failure(rtsp_conn_info *c,pthread_t audio) {
  assert(begin_count==1 && end_count==1 && created==0 && metadata_count==0 && !c->is_playing);
  assert(c->player_thread==NULL && c->rtp_running==0 && joined==2 && cancelled==2 && ptp_end==1);
  assert(cancel_ids[0]==audio && cancel_ids[1]==c->rtp_ap2_control_thread);
  assert(join_ids[0]==audio && join_ids[1]==c->rtp_ap2_control_thread);
}
static void *invoke(void *arg) { int result=player_play(arg); return (void *)(uintptr_t)result; }
static void *abort_invoke(void *arg) {
  assert(pthread_mutex_lock(&gate)==0);entered=1;
  assert(pthread_cond_broadcast(&gate_condition)==0);assert(pthread_mutex_unlock(&gate)==0);
  player_unstarted_session_abort(arg);return NULL;
}
static void await_begin(void) {
  assert(pthread_mutex_lock(&gate)==0);
  while(!entered) assert(pthread_cond_wait(&gate_condition,&gate)==0);
  assert(pthread_mutex_unlock(&gate)==0);
}
static void release(void) {
  assert(pthread_mutex_lock(&gate)==0);release_begin=1;
  assert(pthread_cond_broadcast(&gate_condition)==0);assert(pthread_mutex_unlock(&gate)==0);
}
int main(void) {
  rtsp_conn_info c;pthread_t caller;void *result;
  reset(&c,buffered_stream);block_begin=1;
  assert(pthread_create(&caller,NULL,invoke,&c)==0);await_begin();
  assert(created==0 && c.player_thread==NULL && metadata_count==0 && !c.is_playing);
  release();assert(pthread_join(caller,&result)==0 && result==NULL);
  assert(begin_count==1 && created==1 && metadata_count==1 && end_count==0 && c.is_playing);finish(&c);
  reset(&c,realtime_stream);c.own_airplay_volume_set=1;c.own_airplay_volume=-9;expected_volume=-9;
  assert(player_play(&c)==0 && begin_count==1 && created==1);finish(&c);
  reset(&c,buffered_stream);fail_admission=1;
  assert(player_play(&c)==EIO);check_failure(&c,c.rtp_buffered_audio_thread);finish(&c);
  reset(&c,realtime_stream);fail_create=1;
  assert(player_play(&c)==EAGAIN);check_failure(&c,c.rtp_realtime_audio_thread);finish(&c);
  reset(&c,buffered_stream);block_begin=1;
  assert(pthread_create(&caller,NULL,invoke,&c)==0);await_begin();
  assert(pthread_cancel(caller)==0);release();assert(pthread_join(caller,&result)==0 && result==PTHREAD_CANCELED);
  check_failure(&c,c.rtp_buffered_audio_thread);finish(&c);
  reset(&c,realtime_stream);c.player_thread=malloc(sizeof(pthread_t));assert(c.player_thread);
  assert(player_play(&c)==0 && begin_count==0 && created==0 && end_count==0);finish(&c);
  reset(&c,realtime_stream);c.airplay_type=ap_1;fail_admission=1;
  assert(player_play(&c)==EIO && end_count==1 && cancelled==0 && joined==0 && ptp_end==0);finish(&c);
  reset(&c,buffered_stream);c.rtp_ap2_control_started=c.rtp_buffered_audio_started=0;fail_admission=1;
  assert(player_play(&c)==EIO && end_count==1 && cancelled==0 && joined==0 && ptp_end==0);finish(&c);
  reset(&c,buffered_stream);c.rtp_buffered_audio_started=0;fail_admission=1;
  assert(player_play(&c)==EIO && cancelled==1 && joined==1 && cancel_ids[0]==c.rtp_ap2_control_thread);
  assert(!c.rtp_ap2_control_started);player_unstarted_session_abort(&c);
  assert(cancelled==1 && joined==1 && ptp_end==1);finish(&c);
  reset(&c,realtime_stream);c.player_thread=malloc(sizeof(pthread_t));assert(c.player_thread);
  player_unstarted_session_abort(&c);
  assert(end_count==0 && joined==0 && cancelled==0 && c.rtp_realtime_audio_started && c.rtp_ap2_control_started);finish(&c);
  reset(&c,buffered_stream);
  // A real player_stop owns this mutex while it clears the pointer and joins.
  assert(pthread_mutex_lock(&c.player_create_delete_mutex)==0);
  assert(pthread_create(&caller,NULL,abort_invoke,&c)==0);await_begin();
  assert(end_count==0 && joined==0 && cancelled==0);
  player_ap2_receivers_stop(&c);
  assert(joined==2 && cancelled==2);
  assert(pthread_mutex_unlock(&c.player_create_delete_mutex)==0);
  assert(pthread_join(caller,&result)==0 && result==NULL);
  assert(joined==2 && cancelled==2 && ptp_end==0);finish(&c);
  reset(&c,buffered_stream);normal_stops=activity_stops=0;c.rtp_buffered_audio_started=0;
  stream_setup_reentry(&c);
  assert(normal_stops==0 && activity_stops==1 && end_count==1 && joined==1 && cancelled==1);
  assert(!c.rtp_ap2_control_started && ptp_end==1);finish(&c);
  reset(&c,realtime_stream);normal_stops=activity_stops=0;
  c.player_thread=malloc(sizeof(pthread_t));assert(c.player_thread);
  stream_setup_reentry(&c);
  assert(normal_stops==1 && activity_stops==1 && end_count==0 && joined==2 && cancelled==2);finish(&c);
  reset(&c,buffered_stream);normal_stops=activity_stops=0;
  c.rtp_ap2_control_started=c.rtp_buffered_audio_started=0;
  stream_setup_reentry(&c);
  assert(normal_stops==0 && activity_stops==0 && end_count==0 && joined==0 && cancelled==0);finish(&c);
  puts("PASS synchronous cold BEGIN before player creation; exact volume; cold/warm/failure/cancellation AP2 retirement");
  return 0;
}
'''


def extract(source):
    start = source.index("struct player_unstarted_session {")
    end = source.index("\nint player_stop(", start)
    helper_start = source.index("#ifdef CONFIG_AIRPLAY_2\n/* Only a successful pthread_create")
    helper_end = source.index("void player_thread_cleanup_handler(", helper_start)
    return source[helper_start:helper_end] + source[start:end]


def reentry(source):
    start = source.index("      if (conn->player_thread) {", source.index("void handle_setup_2"))
    return source[start:source.index("      set_client_as_ptp_clock(conn);", start)]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def run_tests(*, source=None, compiler=None, sanitize=True):
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA:
        raise ValueError("Native startup patch differs from the reviewed layer")
    functions = (HERE / "shairport_startup_reference.inc").read_text()
    guard = (HERE / "shairport_startup_reentry.inc").read_text()
    if digest(functions) != REFERENCE_SHA or digest(guard) != REENTRY_SHA:
        raise ValueError("Native startup reference bodies changed")
    if source is not None:
        player, rtsp = (source / "player.c").read_text(), (source / "rtsp.c").read_text()
        if extract(player) != functions or reentry(rtsp) != guard:
            raise ValueError("Actual native startup/retirement bodies differ from the reviewed layer")
        thread = player[player.index("void *player_thread_func"):player.index("void player_send_volume_metadata")]
        if "config.output->session_begin(" in thread or player.count("config.output->session_begin(") != 2:
            raise ValueError("Native BEGIN still runs asynchronously or has duplicate admission")
        common = (source / "common.c").read_text()
        if 'strcat(version_string, "-shiri-timed3");\n    strcat(version_string, "-startup1");' not in common:
            raise ValueError("Native startup layer lacks its required backend feature token")
    compiler = compiler or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        raise RuntimeError("A C compiler is required for native startup checks")
    # GCC/glibc otherwise expands pthread cleanup handlers with setjmp, which
    # reports the exact player_play result local as clobbered. Compile real
    # cancellation unwind tables; retain the native bodies and all diagnostics.
    flags = ["-std=c99", "-D_DARWIN_C_SOURCE=1", "-O1", "-fexceptions", "-Wall", "-Wextra", "-Werror", "-Wno-multichar"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    environment = {**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"}
    with tempfile.TemporaryDirectory(prefix="shiri-startup-c-") as temporary:
        directory = Path(temporary)
        unit, binary = directory / "startup.c", directory / "startup"
        unit.write_text(PRELUDE + "\n" + functions + "\n" + SETUP_HELPERS + guard + "}\n" + TRAILER)
        subprocess.run([compiler, *flags, str(unit), "-lm", "-pthread", "-o", str(binary)],
                       capture_output=True, text=True, check=True, timeout=30)
        result = subprocess.run([str(binary)], env=environment, capture_output=True,
                                text=True, check=True, timeout=10)
    clock = None
    if source is not None:
        path = HERE / "check_shairport_clock_recovery.py"
        spec = importlib.util.spec_from_file_location("startup_original_clock_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        clock = module.run_tests(shairport_source=source, compiler=compiler, sanitize=sanitize)
    return {"ok": True, "sanitized": sanitize, "actual_source": source is not None,
            "result": result.stdout.strip(), "startup_cases": 14, "patch_sha256": PATCH_SHA,
            "functions_sha256": REFERENCE_SHA, "reentry_sha256": REENTRY_SHA, "clock_gate": clock}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--no-sanitizers", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, compiler=args.compiler, sanitize=not args.no_sanitizers), indent=2))
    except subprocess.CalledProcessError as error:
        raise SystemExit("Native startup check failed: " + (error.stderr or error.stdout)) from None


if __name__ == "__main__":
    main()

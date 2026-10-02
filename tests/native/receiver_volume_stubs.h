#ifndef RECEIVER_VOLUME_STUBS_H
#define RECEIVER_VOLUME_STUBS_H
#include <pthread.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
struct node;
typedef struct node *plist_t;
plist_t plist_new_dict(void);
plist_t plist_new_string(const char *);
plist_t plist_new_real(double);
void plist_dict_set_item(plist_t,const char *,plist_t);
void plist_free(plist_t);
void plist_to_bin(plist_t,char **,uint32_t *);
struct pair_cipher_context { int unused; };
ssize_t pair_encrypt(uint8_t **,size_t *,const uint8_t *,size_t,struct pair_cipher_context *);
ssize_t pair_decrypt(uint8_t **,size_t *,const uint8_t *,size_t,struct pair_cipher_context *);
typedef struct { uint8_t *data; size_t length,size; } sized_buffer;
typedef struct { struct pair_cipher_context *cipher_ctx; sized_buffer encrypted_read_buffer,plaintext_read_buffer; } pair_cipher_bundle;
enum { ap_1, ap_2 };
typedef struct { int connection_number,airplay_type,event_channel_fd,shiri_event_volume_faulted; pthread_mutex_t event_sender_mutex; struct { pair_cipher_bundle event_cipher_bundle; } ap2_pairing_context; } rtsp_conn_info;
#endif

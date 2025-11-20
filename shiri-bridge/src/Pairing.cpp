#include "Pairing.h"
#include <string>
#include <vector>
#include <iostream>
#include <algorithm>
#include <cctype>
#include <cstring>

#include "openssl/ssl.h"
#include "openssl/sha.h"
#include "openssl/srp.h"

extern "C" {
#include "cross_log.h"
#include "cross_util.h"
#include "cross_net.h"
#include "raop_client.h"
}

#include "bplist.h"

#define KEYSIZE 32
static BIGNUM* A = nullptr;
static BIGNUM* a = nullptr;
static char K[20*2];
static uint8_t scratch[1024];

static std::vector<uint8_t> computeM1(std::vector<uint8_t> pk, std::vector<uint8_t> salt, const char* user, const char* passwd) {
    // initialize SRP context
    SRP_gN* gN = SRP_get_default_gN("2048");

    // transform pk (B) and salt (s)
    BIGNUM* B = BN_new();
    BN_bin2bn(pk.data(), pk.size(), B);
    BIGNUM* s = BN_new();
    BN_bin2bn(salt.data(), salt.size(), s);

    // verify B
    // int verify = SRP_Verify_B_mod_N(B, gN->N); // Optional check

    A = SRP_Calc_A(a, gN->N, gN->g);
    BIGNUM* x = SRP_Calc_x(s, user, passwd);
    BIGNUM* u = SRP_Calc_u(A, B, gN->N);
    BIGNUM* S = SRP_Calc_client_key(gN->N, B, gN->g, x, a, u);

    // M1 = SHA1(SHA1(N) ^ SHA1(g) | SHA1(I) | s | PAD(A) | PAD(B) | K)
    std::vector<uint8_t> data;
    size_t lenN = BN_num_bytes(gN->N);
    
    // do sha1(N)
    uint8_t sha[20];
    SHA1(scratch, BN_bn2bin(gN->N, scratch), sha);
    data.insert(data.begin(), sha, sha + sizeof(sha));

    // do sha1(g) and xor in place with sha1(N)
    SHA1(scratch, BN_bn2bin(gN->g, scratch), sha);
    for (size_t i = 0; i < sizeof(sha); i++) data[i] ^= sha[i];

    // append sha1(user) (I)
    SHA1((uint8_t*)user, strlen(user), sha);
    data.insert(data.end(), sha, sha + sizeof(sha));

    // append salt (s)
    size_t len = BN_bn2bin(s, scratch);
    data.insert(data.end(), scratch, scratch + len);

    // append PAD(A) and PAD(B)
    BN_bn2binpad(A, scratch, lenN);
    data.insert(data.end(), scratch, scratch + lenN);
    BN_bn2binpad(B, scratch, lenN);
    data.insert(data.end(), scratch, scratch + lenN);

    // append K = SHA1(S | \x00\x00\x00\x00) | SHA(S | \x00\x00\x00\x01)
    memcpy(scratch + BN_bn2binpad(S, scratch, lenN), "\0\0\0\0", 4);
    SHA1(scratch, lenN + 4, sha);
    memcpy(K, sha, sizeof(sha));
    data.insert(data.end(), sha, sha + sizeof(sha));

    memcpy(scratch + BN_bn2binpad(S, scratch, lenN), "\0\0\0\1", 4);
    SHA1(scratch, lenN + 4, sha);
    memcpy(K + sizeof(sha), sha, sizeof(sha));
    data.insert(data.end(), sha, sha + sizeof(sha));

    // SHA1 of everything
    SHA1(data.data(), data.size(), sha);

    BN_free(B);
    BN_free(u);
    BN_free(x);
    BN_free(S);

    std::vector<uint8_t> M1;
    M1.insert(M1.begin(), sha, sha + sizeof(sha));
    return M1;
}

std::string pairDevice(const std::string& ip, int port, const std::string& udn) {
    struct sockaddr_in peer = { };
    key_data_t headers[16] = { };
    int sock = -1;
    std::string resultSecret;

    // Clean up globals
    if (A) { BN_free(A); A = nullptr; }
    if (a) { BN_free(a); a = nullptr; }

    peer.sin_family = AF_INET;
    peer.sin_addr.s_addr = inet_addr(ip.c_str());
    peer.sin_port = htons(port);

    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (!tcp_connect(sock, peer)) {
        std::cerr << "Failed to connect to " << ip << ":" << port << std::endl;
        return "";
    }

    kd_add(headers, "Connection", "keep-alive");
    kd_add(headers, "Content-Type", "application/octet-stream");
    
    char *buffer = http_send(sock, "POST /pair-pin-start HTTP/1.1", headers);
    NFREE(buffer);
    kd_free(headers);

    char method[16] = {0}, resource[16] = {0};
    int len;

    a = BN_new();
    BN_rand(a, 256, -1, 0);

    // Wait for 200 OK
    if (http_parse(sock, method, resource, NULL, headers, NULL, &len) && strcasestr(resource, "200")) {
        kd_free(headers);
        
        std::cout << "Enter PIN displayed on device: " << std::flush;
        std::string pin;
        std::getline(std::cin, pin);
        // Strip whitespace
        pin.erase(std::remove_if(pin.begin(), pin.end(), ::isspace), pin.end());

        if (pin.empty()) {
            closesocket(sock);
            return "";
        }

        char rawUDN[17] = { };
        sscanf(udn.c_str(), "%16[^@]", rawUDN);

        bplist list;
        list.add(2, "method", bplist::STRING, "pin",
                    "user", bplist::STRING, rawUDN);

        auto data = list.toData();
        kd_add(headers, "Server", "spotraop");
        kd_add(headers, "Connection", "keep-alive");
        kd_add(headers, "Content-Type", "application/x-apple-binary-plist");
        kd_vadd(headers, "Content-Length", "%zu", data.size());

        char* httpStr = http_send(sock, "POST /pair-setup-pin HTTP/1.1", headers);
        send(sock, (const char*) data.data(), data.size(), 0);
        NFREE(httpStr);
        kd_free(headers);

        char* body = NULL;
        if (http_parse(sock, method, resource, NULL, headers, &body, &len) && strcasestr(resource, "200")) {
            kd_free(headers);
            
            bplist ATVresp((uint8_t*)body, len);
            auto pk = ATVresp.getValueData("pk");
            auto salt = ATVresp.getValueData("salt");

            // compute M1
            auto M1 = computeM1(pk, salt, rawUDN, pin.c_str());

            // send A and M1
            bplist clientResponse;
            std::vector<uint8_t> bufferA(BN_num_bytes(A));
            BN_bn2bin(A, bufferA.data());

            clientResponse.add(2, "pk", bplist::DATA, bufferA.data(), bufferA.size(),
                                  "proof", bplist::DATA, M1.data(), M1.size());

            data = clientResponse.toData();
            kd_add(headers, "Server", "spotraop");
            kd_add(headers, "Connection", "keep-alive");
            kd_add(headers, "Content-Type", "application/x-apple-binary-plist");
            kd_vadd(headers, "Content-Length", "%zu", data.size());

            httpStr = http_send(sock, "POST /pair-setup-pin HTTP/1.1", headers);
            send(sock, (const char*)data.data(), data.size(), 0);
            NFREE(httpStr);
            kd_free(headers);

            NFREE(body);
            body = NULL;

            // Step 3 (M2 verification + Sign keys)
            if (http_parse(sock, method, resource, NULL, headers, &body, &len) && strcasestr(resource, "200")) {
                kd_free(headers);

                // We skip M2 verification for simplicity (as in original code)
                
                // Get 'a' public key for signing
                uint8_t a_pub[KEYSIZE];
                BN_bn2bin(a, scratch);
                EVP_PKEY* privKey = EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, NULL, scratch, KEYSIZE);
                size_t size = KEYSIZE;
                EVP_PKEY_get_raw_public_key(privKey, a_pub, &size);
                EVP_PKEY_free(privKey);

                SHA512_CTX digest;
                uint8_t aesKey[16], aesIV[16];

                SHA512_Init(&digest);
                const char *feed = "Pair-Setup-AES-Key";
                SHA512_Update(&digest, feed, strlen(feed));
                SHA512_Update(&digest, K, sizeof(K));
                SHA512_Final(scratch, &digest);
                memcpy(aesKey, scratch, 16);

                SHA512_Init(&digest);
                feed = "Pair-Setup-AES-IV";
                SHA512_Update(&digest, feed, strlen(feed));
                SHA512_Update(&digest, K, sizeof(K));
                SHA512_Final(scratch, &digest);
                memcpy(aesIV, scratch, 16);
                aesIV[15]++;

                uint8_t epk[KEYSIZE], tag[KEYSIZE/2];
                int outlen;

                EVP_CIPHER_CTX* gcm = EVP_CIPHER_CTX_new();
                EVP_EncryptInit(gcm, EVP_aes_128_gcm(), NULL, NULL);
                EVP_CIPHER_CTX_ctrl(gcm, EVP_CTRL_GCM_SET_IVLEN, sizeof(aesIV), NULL);
                EVP_EncryptInit(gcm, NULL, aesKey, aesIV);
                EVP_EncryptUpdate(gcm, epk, &outlen, a_pub, sizeof(a_pub));
                EVP_EncryptFinal(gcm, NULL, &outlen);
                EVP_CIPHER_CTX_ctrl(gcm, EVP_CTRL_GCM_GET_TAG, sizeof(tag), tag);
                EVP_CIPHER_CTX_free(gcm);

                bplist listStep3;
                listStep3.add(2, "epk", bplist::DATA, epk, sizeof(epk),
                            "authTag", bplist::DATA, tag, sizeof(tag));

                data = listStep3.toData();
                kd_add(headers, "Server", "spotraop");
                kd_add(headers, "Connection", "keep-alive");
                kd_add(headers, "Content-Type", "application/x-apple-binary-plist");
                kd_vadd(headers, "Content-Length", "%zu", data.size());

                httpStr = http_send(sock, "POST /pair-setup-pin HTTP/1.1", headers);
                send(sock, (const char*)data.data(), data.size(), 0);
                NFREE(httpStr);
                kd_free(headers);

                NFREE(body);
                body = NULL;

                if (http_parse(sock, method, resource, NULL, headers, &body, &len) && strcasestr(resource, "200")) {
                    kd_free(headers);
                    auto a_hex = BN_bn2hex(a);
                    resultSecret = std::string(a_hex);
                    OPENSSL_free(a_hex);
                } else {
                    std::cerr << "Pairing failed at step 3" << std::endl;
                }
            } else {
                std::cerr << "Pairing failed at step 2" << std::endl;
            }
        } else {
            std::cerr << "Pairing failed at step 1 (PIN rejected?)" << std::endl;
        }
        NFREE(body);
    } else {
        std::cerr << "Failed to start pairing (no PIN displayed?)" << std::endl;
    }

    if (a) { BN_free(a); a = nullptr; }
    if (A) { BN_free(A); A = nullptr; }
    if (sock != -1) closesocket(sock);

    return resultSecret;
}


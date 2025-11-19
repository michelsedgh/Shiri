cmake_minimum_required(VERSION 3.10)
project(shiri-bridge)

set(CMAKE_CXX_STANDARD 17)

# --- Dependencies ---
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

# --- Platform Detection for Prebuilts ---
if(APPLE)
    execute_process(COMMAND uname -m OUTPUT_VARIABLE ARCH OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(ARCH STREQUAL "arm64")
        set(TARGET_PLATFORM "macos/arm64")
    else()
        set(TARGET_PLATFORM "macos/x86_64")
    endif()
elseif(UNIX)
    # Simple detection for Linux
    execute_process(COMMAND uname -m OUTPUT_VARIABLE ARCH OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(ARCH STREQUAL "aarch64")
        set(TARGET_PLATFORM "linux/aarch64")
    elseif(ARCH STREQUAL "x86_64")
        set(TARGET_PLATFORM "linux/x86_64")
    else()
        set(TARGET_PLATFORM "linux/x86") # Fallback
    endif()
endif()

message(STATUS "Target Platform for prebuilts: ${TARGET_PLATFORM}")

# --- LibRAOP Library ---
# Sources
file(GLOB RAOP_SOURCES
    "libraop/src/*.c"
    "libraop/src/*.cpp"
    "libraop/crosstools/src/*.c"
    "libraop/dmap-parser/dmap_parser.c"
)
# Exclude CLI tool main
list(FILTER RAOP_SOURCES EXCLUDE REGEX ".*cliraop.c$")

add_library(raop STATIC ${RAOP_SOURCES})

# Includes
target_include_directories(raop PUBLIC
    "libraop/src"
    "libraop/crosstools/src"
    "libraop/dmap-parser"
    "libraop/libcodecs/targets/include"
    "libraop/libmdns/targets/include/mdnssvc"
    "libraop/libmdns/targets/include/mdnssd"
)

# Prebuilt Libraries Paths
set(LIBMDNS_PATH "${CMAKE_CURRENT_SOURCE_DIR}/libraop/libmdns/targets/${TARGET_PLATFORM}")
set(LIBCODECS_PATH "${CMAKE_CURRENT_SOURCE_DIR}/libraop/libcodecs/targets/${TARGET_PLATFORM}")

# Find Prebuilts
find_library(MDNS_LIB mdns PATHS ${LIBMDNS_PATH} NO_DEFAULT_PATH)
find_library(CODECS_LIB codecs PATHS ${LIBCODECS_PATH} NO_DEFAULT_PATH)
# We might need specific codec libs if libcodecs.a is not enough or monolithic
find_library(ALAC_LIB alac PATHS ${LIBCODECS_PATH} NO_DEFAULT_PATH)
find_library(ADDONS_LIB addons PATHS ${LIBCODECS_PATH} NO_DEFAULT_PATH)

# Link Libraop dependencies
target_link_libraries(raop PUBLIC
    OpenSSL::SSL OpenSSL::Crypto
    Threads::Threads
    ${MDNS_LIB}
    ${CODECS_LIB}
    ${ALAC_LIB}
    ${ADDONS_LIB}
    ${CMAKE_DL_LIBS}
)

# --- Shiri Bridge Executable ---
add_subdirectory(shiri-bridge)


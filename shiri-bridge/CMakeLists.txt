cmake_minimum_required(VERSION 3.20)
project(shiri-bridge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

option(SHIRI_ENABLE_SANITIZERS "Build shiri-bridge with ASan/UBSan" OFF)

# --- Auto-detect HOST and PLATFORM for libraop ---
if(CMAKE_SYSTEM_NAME MATCHES "Linux")
    set(HOST "linux")
elseif(CMAKE_SYSTEM_NAME MATCHES "Darwin")
    set(HOST "macos")
elseif(CMAKE_SYSTEM_NAME MATCHES "Windows")
    set(HOST "win32")
endif()

# Normalize processor to what libraop expects (aarch64, x86_64, arm, etc.)
string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" SYSTEM_ARCH)
if(SYSTEM_ARCH MATCHES "aarch64" OR SYSTEM_ARCH MATCHES "arm64")
    set(PLATFORM "aarch64")
elseif(SYSTEM_ARCH MATCHES "x86_64" OR SYSTEM_ARCH MATCHES "amd64")
    set(PLATFORM "x86_64")
elseif(SYSTEM_ARCH MATCHES "arm.*")
    set(PLATFORM "arm") 
else()
    # Fallback for unknown architectures (or let user override)
    if(NOT PLATFORM)
        set(PLATFORM "${SYSTEM_ARCH}")
    endif()
endif()

if(NOT HOST)
    set(HOST "linux")
    message(WARNING "Could not detect HOST, defaulting to 'linux'")
endif()

if(NOT PLATFORM)
    set(PLATFORM "x86_64")
    message(WARNING "Could not detect PLATFORM, defaulting to 'x86_64'")
endif()

# Export these to cache so libraop sees them
set(HOST ${HOST} CACHE STRING "Target OS" FORCE)
set(PLATFORM ${PLATFORM} CACHE STRING "Target Architecture" FORCE)

message(STATUS "Configuring for HOST=${HOST}, PLATFORM=${PLATFORM}")
# -------------------------------------------------

set(SRC_DIR "${CMAKE_CURRENT_SOURCE_DIR}/src")
set(THIRD_PARTY_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party")

# Add libraop as a subdirectory
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../libraop ${CMAKE_CURRENT_BINARY_DIR}/libraop)

# We do NOT build our own mdnssd anymore to avoid symbol collisions with libraop's libmdns.
# Instead we will link against raop_lib which should provide mDNS capabilities.

add_executable(shiri-bridge
    ${SRC_DIR}/main.cpp
    ${SRC_DIR}/Discovery.cpp
    ${SRC_DIR}/Shairport.cpp
    ${SRC_DIR}/RaopHostage.cpp
    ${SRC_DIR}/Tui.cpp
)

# Include libraop headers and its dependencies (mdnssd)
target_include_directories(shiri-bridge
    PRIVATE
        ${SRC_DIR}
        ${THIRD_PARTY_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/../libraop/src
)

if(SHIRI_ENABLE_SANITIZERS)
    target_compile_options(shiri-bridge PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
    target_link_options(shiri-bridge PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
endif()

if(APPLE)
    find_library(DNSSD_LIBRARY NAMES dns_sd)
    if(NOT DNSSD_LIBRARY)
        message(FATAL_ERROR "dns_sd framework not found. Install the Xcode Command Line Tools.")
    endif()
    target_link_libraries(shiri-bridge PRIVATE ${DNSSD_LIBRARY} raop_lib)
else()
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(AVAHI REQUIRED avahi-client)
    target_include_directories(shiri-bridge PRIVATE ${AVAHI_INCLUDE_DIRS})
    target_link_libraries(shiri-bridge PRIVATE ${AVAHI_LIBRARIES})
    target_link_libraries(shiri-bridge PRIVATE raop_lib pthread)
endif()


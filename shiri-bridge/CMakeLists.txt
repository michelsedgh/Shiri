cmake_minimum_required(VERSION 3.20)
project(shiri-bridge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

option(SHIRI_ENABLE_SANITIZERS "Build shiri-bridge with ASan/UBSan" OFF)

set(SRC_DIR "${CMAKE_CURRENT_SOURCE_DIR}/src")
set(THIRD_PARTY_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party")

if(NOT APPLE)
    add_library(mdnssd STATIC
        ${THIRD_PARTY_DIR}/mdnssd.c
    )
    target_include_directories(mdnssd
        PUBLIC
            ${THIRD_PARTY_DIR}
    )
    target_compile_definitions(mdnssd PRIVATE _GNU_SOURCE)
endif()

add_executable(shiri-bridge
    ${SRC_DIR}/main.cpp
    ${SRC_DIR}/Discovery.cpp
    ${SRC_DIR}/Shairport.cpp
)

target_include_directories(shiri-bridge
    PRIVATE
        ${SRC_DIR}
        ${THIRD_PARTY_DIR}
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
    target_link_libraries(shiri-bridge PRIVATE ${DNSSD_LIBRARY})
else()
    target_link_libraries(shiri-bridge PRIVATE mdnssd pthread)
endif()


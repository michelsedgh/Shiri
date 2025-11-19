add_executable(shiri-bridge
    main.cpp
    # Add other source files here as we implement them
)

target_include_directories(shiri-bridge PRIVATE include)
target_link_libraries(shiri-bridge PRIVATE raop)

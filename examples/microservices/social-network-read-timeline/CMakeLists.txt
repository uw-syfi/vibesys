cmake_minimum_required(VERSION 3.14)
project(social_network_read_timeline CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(CURL REQUIRED)
find_package(Threads REQUIRED)

include(FetchContent)
if(POLICY CMP0135)
  cmake_policy(SET CMP0135 NEW)
endif()
FetchContent_Declare(
  json
  URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
)
FetchContent_MakeAvailable(json)

include_directories(${CMAKE_SOURCE_DIR})
include_directories(${CMAKE_SOURCE_DIR}/include)

add_executable(checker accuracy_checker/checker.cpp)
target_link_libraries(checker CURL::libcurl Threads::Threads nlohmann_json::nlohmann_json)

add_executable(benchmark benchmark/benchmark.cpp)
target_link_libraries(benchmark CURL::libcurl Threads::Threads nlohmann_json::nlohmann_json)

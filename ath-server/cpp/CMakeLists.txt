cmake_minimum_required(VERSION 3.24)
project(ath_server CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

include(FetchContent)

# DuckDB 2.0 ships the C API (duckdb.h + libduckdb) as a prebuilt per platform
set(DUCKDB_VERSION 2.0.0)
if(APPLE)
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "arm64|aarch64")
        set(DUCKDB_PLATFORM osx-arm64)
    else()
        set(DUCKDB_PLATFORM osx-amd64)
    endif()
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "arm64|aarch64")
    set(DUCKDB_PLATFORM linux-arm64)
else()
    set(DUCKDB_PLATFORM linux-amd64)
endif()
FetchContent_Declare(duckdb
    URL https://github.com/duckdb/duckdb/releases/download/v${DUCKDB_VERSION}/duckdb-shared-libs-${DUCKDB_PLATFORM}.tar.gz
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)

# cpp-httplib: single-header HTTP server, plain HTTP only
set(HTTPLIB_USE_OPENSSL_IF_AVAILABLE OFF CACHE BOOL "" FORCE)
set(HTTPLIB_USE_ZLIB_IF_AVAILABLE OFF CACHE BOOL "" FORCE)
set(HTTPLIB_USE_BROTLI_IF_AVAILABLE OFF CACHE BOOL "" FORCE)
set(HTTPLIB_USE_ZSTD_IF_AVAILABLE OFF CACHE BOOL "" FORCE)
FetchContent_Declare(httplib
    URL https://github.com/yhirose/cpp-httplib/archive/refs/tags/v0.64.0.tar.gz
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)

FetchContent_MakeAvailable(duckdb httplib)

add_library(duckdb SHARED IMPORTED)
set_target_properties(duckdb PROPERTIES
    IMPORTED_LOCATION ${duckdb_SOURCE_DIR}/libduckdb${CMAKE_SHARED_LIBRARY_SUFFIX}
    INTERFACE_INCLUDE_DIRECTORIES ${duckdb_SOURCE_DIR})

add_executable(ath-server server.cpp)
target_link_libraries(ath-server PRIVATE duckdb httplib::httplib)
set_target_properties(ath-server PROPERTIES BUILD_RPATH ${duckdb_SOURCE_DIR})

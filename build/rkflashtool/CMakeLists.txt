cmake_minimum_required(VERSION 3.18)
project(tools)

include(ExternalProject)

message(STATUS "CMAKE_SYSTEM_NAME: ${CMAKE_SYSTEM_NAME}")
message(STATUS "CMAKE_SYSTEM_VERSION: ${CMAKE_SYSTEM_VERSION}")
message(STATUS "CMAKE_BUILD_TYPE: ${CMAKE_BUILD_TYPE}")
message(STATUS "CMAKE_CURRENT_BINARY_DIR: ${CMAKE_CURRENT_BINARY_DIR}")
message(STATUS "CMAKE_BINARY_DIR: ${CMAKE_BINARY_DIR}")
message(STATUS "CMAKE_SOURCE_DIR: ${CMAKE_SOURCE_DIR}")
message(STATUS "CMAKE_C_COMPILER: ${CMAKE_C_COMPILER}")
message(STATUS "CMAKE_CXX_COMPILER: ${CMAKE_CXX_COMPILER}")
message(STATUS "CMAKE_C_FLAGS: ${CMAKE_C_FLAGS}")
message(STATUS "CMAKE_CXX_FLAGS: ${CMAKE_CXX_FLAGS}")
message(STATUS "CMAKE_C_FLAGS_DEBUG: ${CMAKE_C_FLAGS_DEBUG}")
message(STATUS "CMAKE_CXX_FLAGS_DEBUG: ${CMAKE_CXX_FLAGS_DEBUG}")
message(STATUS "CMAKE_C_FLAGS_RELEASE: ${CMAKE_C_FLAGS_RELEASE}")
message(STATUS "CMAKE_CXX_FLAGS_RELEASE: ${CMAKE_CXX_FLAGS_RELEASE}")
message(STATUS "CMAKE_EXE_LINKER_FLAGS: ${CMAKE_EXE_LINKER_FLAGS}")
message(STATUS "CMAKE_SHARED_LINKER_FLAGS: ${CMAKE_SHARED_LINKER_FLAGS}")
message(STATUS "CMAKE_MODULE_LINKER_FLAGS: ${CMAKE_MODULE_LINKER_FLAGS}")

ExternalProject_Add(libusb
    DOWNLOAD_DIR ${CMAKE_BINARY_DIR}
    URL https://github.com/libusb/libusb/releases/download/v1.0.26/libusb-1.0.26.tar.bz2
    UPDATE_COMMAND ""
    BUILD_IN_SOURCE 1
    CONFIGURE_COMMAND ./configure --host=${EXT_CONFIGURE_HOST} --prefix=${CMAKE_BINARY_DIR}/ext/libusb --disable-shared
    BUILD_COMMAND make
    INSTALL_COMMAND make install
)

set(RKFLASHTOOL_HEADERS
    rkflashtool/version.h
    rkflashtool/rkflashtool.h
)
set(RKFLASHTOOL_EXES rkflashtool)

foreach(EXE IN ITEMS ${RKFLASHTOOL_EXES})
    add_executable(${EXE} rkflashtool/${EXE}.c ${RKFLASHTOOL_HEADERS})
    add_dependencies(${EXE} libusb)
    target_compile_definitions(${EXE} PUBLIC _XOPEN_SOURCE=600)
    target_include_directories(${EXE} PUBLIC ${CMAKE_BINARY_DIR}/ext/libusb/include ${CMAKE_BINARY_DIR}/ext/libusb/include/libusb-1.0)
    if(CMAKE_SYSTEM_NAME STREQUAL "Darwin")
        target_link_directories(${EXE} PUBLIC ${CMAKE_BINARY_DIR}/ext/libusb/lib)
        target_link_libraries(${EXE} -lusb-1.0 "-framework CoreFoundation" "-framework IOKit" "-framework Security")
    endif()
    if(CMAKE_SYSTEM_NAME STREQUAL "Windows")
        target_link_libraries(${EXE} ${CMAKE_BINARY_DIR}/ext/libusb/lib/libusb-1.0.a -lpthread)
        target_link_options(${EXE} PUBLIC -static)
    endif()
endforeach()

install(TARGETS ${RKFLASHTOOL_EXES})
